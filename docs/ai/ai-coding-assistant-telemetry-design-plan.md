# AI Coding Assistant Telemetry — Design Plan

Nov 26, 2026 · @Emre

## Summary

Build a local-first tool, working name **agentlog**, that captures every prompt/response turn from Claude Code and GitHub Copilot into one SQLite store. It reports cost, latency, tokens, tool use and feedback, and exposes the data over MCP. The attached draft is a solid start, but its core needs four changes:

- **Normalized event pipeline:** hooks only save raw events, after a quick secret scrub. One worker then turns them into a shared schema (sessions → turns → calls) for both tools.
- **Per-turn token sums:** add up every model call in a turn, not just the last one. The draft undercounts.
- **A real OTLP receiver:** a local listener on port 4318, so Copilot's token data is actually stored.
- **Redaction:** strip secrets before anything is saved.

**Goals**

- One schema for both clients: sessions → turns → LLM calls → tool calls.
- Correct numbers: tokens summed per turn, cache tokens split out, cost from a versioned price table.
- Zero impact on the agent: hooks never block, never fail the turn, and finish in under 50 ms.
- Private by default: data stays on the machine, with secrets redacted before write.
- Queryable by people (CLI, dashboard) and by agents (read-only MCP tools).

**Non-goals (v1)**

- Team or cloud aggregation. That comes later via OTLP export, not in v1.
- Copilot inline completions (ghost text). Hooks and OTel only cover chat and agent sessions.
- Billing reconciliation. Copilot bills by premium requests, not tokens, so its USD figure is an estimate only.

## Architecture

&#91;embedded content: agentlog architecture · 3 sources, 1 store, 2 consumers\]

Everything on the agent's path is a quick regex redaction plus an append. Turn correlation, token sums, pricing and full redaction run in one worker, so a parser bug never shows up inside Claude or Copilot.

## Review of the attached draft

The draft gets the architecture right: hooks feed a shared SQLite WAL store, which an MCP server reads. But its token and cost numbers will be wrong for most agentic turns, and its Copilot config targets the wrong format.

**Keep:** one logger shared by both clients, SQLite with WAL, the `inflight` idea of correlating prompt and stop events (now done by the worker from raw\_events, not a separate table), and FastMCP for the server.

| # | Issue in draft | Impact | Fix |
| --- | --- | --- | --- |
| 1 | `parse_transcript` keeps only the **last** assistant entry | A turn with N tool calls reports roughly 1/N of its tokens | Sum `usage` over every assistant entry after the last user prompt. Dedupe on `message.id`, because streamed chunks repeat the same usage |
| 2 | `input_tokens + cache_read_input_tokens`, while `cache_creation_input_tokens` is ignored | Cost is wrong: cache reads cost about 0.1× the input rate, cache writes about 1.25× | Store four token columns and price each one separately |
| 3 | Both hooks are `async: true` | Stop can run before UserPromptSubmit commits, so the prompt is lost and latency reads as 0 | Make UserPromptSubmit synchronous (it is a fast insert). Keep Stop async |
| 4 | Latency = time of hook execution | Includes hook queueing; misses time-to-first-token | Use transcript timestamps, from the user entry to the last assistant entry |
| 5 | `trace_id = uuid4()[:8]` | Collisions are likely at around 65k traces | Use a full UUIDv7 (time-sortable) |
| 6 | Hard-coded `PRICING` with substring match | Stale prices; `gpt-4o` also matches `gpt-4o-mini`; results depend on dict order | Versioned `prices` table, matched on the longest prefix, with an `effective_from` date |
| 7 | Copilot hook JSON copies Claude's nesting, with no `version` | May not load in Copilot CLI or VS Code Copilot sessions | Use the Copilot format: `"version": 1` and flat command entries (§ Client integration) |
| 8 | OTel points at `localhost:4317`, but nothing listens there | All Copilot token data is dropped | Add an OTLP/HTTP receiver on :4318 that appends spans to raw\_events in the same store |
| 9 | MCP `log_turn` lets the model write traces | An agent can fabricate or alter telemetry | Make MCP read-only, plus `record_feedback` |
| 10 | Full prompt/answer stored raw; `captureContent: true` | API keys and secrets in prompts are kept on disk in plain text | Regex redaction in the hook before any write, then a full pass in the normalizer; make content capture opt-in; set file mode 0600 |
| 11 | No `SubagentStop`, `SessionEnd` or tool events | Subagent spend is invisible, and tool usage is lost | Also subscribe to `PostToolUse`, `SubagentStop` and `SessionEnd` |
| 12 | An uncaught exception exits with code 1 | Non-blocking, but noisy in the UI. A bug that exits 2 on Stop would block the agent | Wrap `main()`, log errors to a file, and always exit 0 |

## Components

There are seven components with one rule: hooks only **append raw events** after a quick regex redaction. All parsing, pricing, aggregation and full redaction happen off the agent's critical path.

| Component | Responsibility | Tech | Notes |
| --- | --- | --- | --- |
| `agentlog hook <client>` | Read the hook JSON from stdin, add `received_at`, insert it into `raw_events`, exit 0 | Python stdlib only (or a single Go binary) | No parsing and no network. A fast regex redaction pass runs before the insert. Target p99 < 50 ms |
| Adapters | Map client payloads to the canonical model: Claude transcript JSONL, Copilot hook payloads, Copilot OTel spans | Python, one module per source | Versioned by source schema; unknown fields are kept in `payload_json` |
| Normalizer / worker | Process `raw_events`: correlate the turn, sum usage, compute cost and latency, run stage-2 redaction | Background process (launchd/systemd) or lazy run on the next hook | Idempotent: replays are safe |
| OTLP receiver | Accept Copilot OTLP/HTTP traces and metrics on `127.0.0.1:4318` and append GenAI spans to raw\_events, normalized into `llm_calls` | `opentelemetry-proto` + a small HTTP server, or an OTel Collector with a SQLite exporter | Binds to loopback only |
| Store | SQLite in WAL mode, schema migrations, retention job | `~/.local/share/agentlog/agentlog.db`, mode 0600 | `busy_timeout = 5000` |
| MCP server | Read-only queries plus `record_feedback` | FastMCP over stdio | Results capped at 200 rows; previews only by default |
| CLI / dashboard | `agentlog stats`, `agentlog tail`, `agentlog export`, a local HTML report | Typer + Jinja, or Datasette for zero-code | Useful for teaching demos too |

**Why the worker is separate:** transcript parsing grows with session length. Running it inside Stop would add seconds to long sessions, and a parser bug would surface in the agent's UI.

## Data model

The store has five normalized tables plus a raw event log. A turn is one user prompt through the Stop event that follows it, and it owns many LLM calls and tool calls.

```sql
CREATE TABLE raw_events (            -- append-only; written by hooks (stage-1 redacted) and the OTLP receiver
  id INTEGER PRIMARY KEY, client TEXT, event TEXT, session_id TEXT,
  received_at REAL, payload_json TEXT, processed INTEGER DEFAULT 0);

CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY, client TEXT, client_version TEXT,
  cwd TEXT, repo TEXT, git_branch TEXT,
  started_at REAL, ended_at REAL, end_reason TEXT);

CREATE TABLE turns (
  turn_id TEXT PRIMARY KEY,           -- UUIDv7
  session_id TEXT REFERENCES sessions, turn_index INTEGER,
  prompt_redacted TEXT, answer_redacted TEXT, prompt_sha256 TEXT,
  started_at REAL, first_token_at REAL, ended_at REAL,
  latency_ms REAL, stop_reason TEXT, is_subagent INTEGER DEFAULT 0,
  parent_turn_id TEXT, feedback_score INTEGER, feedback_note TEXT);

CREATE TABLE llm_calls (
  call_id TEXT PRIMARY KEY, turn_id TEXT REFERENCES turns,
  provider_msg_id TEXT UNIQUE,        -- dedupe key
  model TEXT, input_tokens INT, output_tokens INT,
  cache_read_tokens INT, cache_write_tokens INT,
  cost_usd REAL, price_version TEXT, source TEXT); -- 'transcript' | 'otel'

CREATE TABLE tool_calls (
  tool_call_id TEXT PRIMARY KEY, turn_id TEXT REFERENCES turns,
  tool_name TEXT, ok INTEGER, duration_ms REAL, input_preview TEXT);

CREATE TABLE prices (
  model_prefix TEXT, effective_from TEXT, in_per_m REAL, out_per_m REAL,
  cache_read_per_m REAL, cache_write_per_m REAL,
  PRIMARY KEY (model_prefix, effective_from));

CREATE INDEX ix_turns_session ON turns(session_id, turn_index);
CREATE INDEX ix_turns_time ON turns(started_at);
CREATE INDEX ix_calls_turn ON llm_calls(turn_id);
```

- **Cost is derived, not stored as truth.** Keep tokens and `price_version`. Then a price change only needs a recompute, not a re-ingest.
- **Raw events are kept for 7 days** so the adapters can be fixed and replayed. The hook has already redacted them, so no secret in a known key format is ever written to disk in clear text. Normalized rows follow a configurable retention (default 90 days).
- **Views for consumers:** `v_daily_spend`, `v_turn_summary` and `v_tool_stats`. The MCP server and CLI query views only, so the schema can change underneath them.

## Client integration

Claude Code's hooks plus its transcript give complete data. Copilot needs hooks for prompts and turn boundaries, and OTel for model and token usage.

| Client | Capture via | Config location | Token source |
| --- | --- | --- | --- |
| Claude Code (CLI, IDE) | `UserPromptSubmit` (sync), `Stop`, `SubagentStop`, `PostToolUse`, `SessionEnd` (async) | `~/.claude/settings.json`, matcher-group format | Transcript JSONL (`transcript_path`) |
| Copilot CLI | Same events, in PascalCase for snake\_case payloads | `~/.copilot/hooks/*.json` or `.github/hooks/*.json`, with `"version": 1` | OTel: `COPILOT_OTEL_ENABLED=true`, endpoint `http://127.0.0.1:4318` (agentlog receiver) |
| VS Code, Copilot harness | Same hook files as Copilot CLI (shared runtime) | Same as above | OTel: `chat.agentHost.otel.*`, endpoint `http://127.0.0.1:4318` (agentlog receiver) |
| VS Code, Copilot Chat (Local harness) | VS Code Local hooks (preview) | `.github/hooks/*.json` | OTel: `github.copilot.chat.otel.*`, endpoint `http://127.0.0.1:4318` (agentlog receiver) |
| VS Code, Claude harness | Claude hooks (Claude Agent SDK) | Claude settings | Transcript |

**Copilot hook file (sketch):**

```json
{
  "version": 1,
  "hooks": {
    "UserPromptSubmit": [{ "type": "command", "command": "agentlog hook copilot", "timeoutSec": 5 }],
    "Stop":             [{ "type": "command", "command": "agentlog hook copilot", "timeoutSec": 5 }],
    "SubagentStop":     [{ "type": "command", "command": "agentlog hook copilot", "timeoutSec": 5 }]
  }
}
```

**Pitfalls to design for**

- **Double counting.** Copilot CLI also reads `.claude/settings.json`, so a Claude hook can fire inside a Copilot session. Detect the client from the payload shape (for example `agent_name` or `timestamp` format), not only from argv, and dedupe on `(session_id, event, timestamp)`.
- **Joining OTel to hooks.** Match on session or conversation ID when the span carries one. Otherwise fall back to a time window: OTel spans that fall between UserPromptSubmit and Stop.
- **Content capture is off by default in Copilot OTel.** Keep it off. The hook already provides the prompt, and the redactor processes it.
- **Installer:** `agentlog install --claude --copilot` merges JSON non-destructively, backs up the original, and prints a diff. `agentlog doctor` fires a synthetic event end to end.

## Privacy and security

Prompts often contain secrets, customer code and personal data, so the default is to redact on ingest and keep everything on loopback.

- **Redaction pipeline** in two stages. Stage 1 runs in the hook before anything reaches disk: precompiled regexes for known key formats, adding about 1–3 ms. Stage 2 runs in the normalizer and does a full pass: regex rules for common key formats (AWS, GitHub, Anthropic, OpenAI, JWT, private-key blocks), entropy checks for long random tokens, and user rules in `~/.config/agentlog/redact.toml`.
- **Capture levels:** `metadata` (tokens, timings, tool names only), `preview` (first 200 characters, redacted; the default), `full` (redacted). Levels can be set per repo, so a client repo can be `metadata` only.
- **Storage:** DB mode 0600 in a user-only directory. Optional SQLCipher for full encryption at rest.
- **Network:** the OTLP receiver binds to `127.0.0.1` only. No outbound calls in v1.
- **MCP surface:** read-only views, with no raw SQL tool. Content previews are off unless the user asks for them, which limits prompt-injection replay from logged text.
- **Untrusted input:** hook payloads and transcripts are data. Use parameterized SQL only, and never run anything taken from a payload.
- **Corporate policy (AVL context):** check whether logging prompts locally is allowed under existing source-code and AI-usage policies before rolling out beyond personal use.

## Reliability and edge cases

| Case | Handling |
| --- | --- |
| Stop with no matching prompt (hook missed, crash) | Create the turn with `prompt = NULL`, flag it `orphan`, and recover the prompt from the transcript |
| Prompt with no Stop (user abort, Ctrl-C) | `SessionEnd` or a sweep after 30 min closes the turn with `stop_reason = 'aborted'` and the partial usage |
| Parallel sessions, or several windows on one session | The worker correlates raw events by `(client, session_id)`. The turn index comes from transcript order, not wall clock |
| Context compaction or resumed sessions | Track the byte offset of the transcript read per session. If the file shrinks or rotates, re-scan it |
| Subagents | `SubagentStop` produces a child turn linked by `parent_turn_id`. Roll up cost to the parent in views, without double counting |
| SQLite `database is locked` | WAL plus `busy_timeout=5000`. If the write still fails, append to a JSONL spool file that the worker ingests later |
| Transcript format change (it is undocumented) | Pin the parser to known fields, keep `payload_json`, add a contract test against a sample transcript, and alert in `agentlog doctor` |
| Unknown model or price | Set `cost_usd = NULL` (never 0), list the model in `agentlog doctor`, and recompute once a price is added |
| Clock skew (OTel vs hooks) | Use the monotonic `received_at` for ordering; source timestamps for display only |

## Phased roadmap

The plan has four phases of roughly one week each for one engineer. Each phase ends at a gate that can be checked.

1. **P0: Claude Code MVP.** Raw-event hook, transcript adapter with per-turn sums, schema and migrations, `agentlog stats`.
   - Gate: token totals match within 1% of Claude Code's own `/cost` or usage readout on 20 sample sessions.
2. **P1: Copilot.** Copilot hook files, OTLP/HTTP receiver, OTel-to-turn join, client detection and dedupe.
   - Gate: a VS Code agent session and a Copilot CLI session both appear with model and tokens filled in; no double rows.
3. **P2: Safety and MCP.** Redactor with test corpus, capture levels, read-only MCP server with views, `record_feedback`.
   - Gate: the secret corpus yields 0 known-format leaks into raw\_events or the normalized tables, and hook p99 is still under 50 ms; the MCP tools return correct data in both clients.
4. **P3: Insight.** HTML dashboard (spend per day, per repo, per model; p50/p95 latency; tool failure rate), `agentlog export` to CSV or OTLP for later team use.
   - Gate: a weekly report generated in under 2 s on a 50k-turn DB.

## Known gaps

The plan is solid on capture and storage. Its weakest points are missing requirements, no build-vs-buy check, and no support for Windows or remote setups. Resolve the high-priority rows before P0.

| Priority | Gap | Proposed resolution |
| --- | --- | --- |
| High | No requirements: users and the questions the tool must answer are undefined | Add a short section with 5–8 target questions (for example, cost per repo per week, slowest tools) and derive views from them |
| High | No build-vs-buy check against Langfuse, Opik, SigNoz or ccusage | Compare them on local-only operation, Claude Code coverage, and setup effort; record the decision as an ADR |
| High | Claude Code's native OTel export is not considered | Evaluate it as a second token source, or as a cross-check for the transcript parser |
| High | Windows unsupported: `~/.local` paths, launchd/systemd worker, bash-only hooks | Use platform dirs (for example `platformdirs`), the `powershell` field in Copilot hooks, and Task Scheduler or lazy-run on Windows |
| Medium | Remote setups (devcontainer, WSL, Remote-SSH): hooks run on the remote host | Decide between a DB per host with export/merge, or OTLP forwarding to the local receiver |
| Medium | USD cost misleads subscription users (Claude Pro/Max as well as Copilot) | Label it "API-equivalent cost"; add a plan setting per client |
| Medium | No owner or source for price updates | Ship `prices.toml` with the release; `agentlog prices update` from a pinned, reviewed source |
| Medium | Feedback only via the agent calling MCP | Add `agentlog rate <score> [note]`, which applies to the last turn |
| Medium | Worker mode undecided (service vs lazy run) | Pick lazy run by default for zero setup, with an optional service for large volumes |
| Low | No uninstall, rollback or config migration | `agentlog uninstall` restores the JSON backups; versioned config |
| Low | No delete-by-session, backup or audit export | `agentlog forget <session>`, `agentlog backup`, CSV/JSON export |
| Low | No self-telemetry for dropped or unparsed events | Counters in a `health` table, shown by `agentlog doctor` |
| Low | No Claude Code hook config sketch with sync/async split and timeouts | Add it next to the Copilot sketch |
| Low | P0 gate relies on `/cost`, which may not show tokens for subscription users | Use a hand-checked transcript sum as the reference |

## Testing, open questions, sources

**Testing**

- **Golden fixtures:** recorded hook payloads and transcripts per client version, with adapter output compared to expected rows.
- **Contract tests:** one canary per release of Claude Code and Copilot that fails loudly when fields move.
- **Property tests:** replaying `raw_events` in any order or twice gives identical normalized tables, which proves idempotence.
- **Performance:** hook p99 under 50 ms with 4 concurrent sessions; a 100 MB transcript parses incrementally.
- **Redaction corpus:** 200+ synthetic secrets, each of which must be masked in raw\_events and in the normalized tables. Check stage 1 alone against the corpus too.

**Open questions**

- Does the Copilot OTel span carry a session or conversation ID that matches the hook `session_id`? This decides whether the join is exact or time-window based.
- Should Copilot cost be shown as premium requests instead of USD?
- Python with `uv tool install`, or a single Go binary for faster hook start-up (about 30 ms vs about 2 ms)?
- Is this personal tooling, a Lunacraft product, or a course lab? The answer changes how much packaging polish is worth.

**Confidence:** about 85% on the Claude Code details and the review findings; about 70% on the Copilot hook and OTel specifics. VS Code agent hooks are in preview and the settings namespaces have changed recently, so verify against a live session before P1.

**Sources**

- [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)
- [GitHub Copilot CLI hooks reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference)
- [Configure agent hooks in VS Code](https://code.visualstudio.com/docs/agent-customization/hooks)
- [Monitor agent usage with OpenTelemetry (VS Code)](https://code.visualstudio.com/docs/copilot/guides/monitoring-agents)

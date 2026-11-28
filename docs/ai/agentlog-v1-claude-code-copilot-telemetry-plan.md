# agentlog v1 — Claude Code + GitHub Copilot Telemetry Plan

Nov 27, 2026 · @Emre

## Summary

v1 writes one SQLite row per prompt for both Claude Code and GitHub Copilot. Each row holds tokens, cost, latency, model and tool count. No worker, no OTLP receiver, no MCP server and no dashboard.

- **Claude Code:** hooks plus the transcript JSONL. Unchanged from the Claude-only v1 draft.
- **GitHub Copilot (new):** Copilot's built-in OTel **file exporter** writes JSON-lines to a local file. `agentlog` imports that file incrementally. No network listener is needed.
- **Copilot turn = one OTel trace.** One root `invoke_agent` span is one prompt. Its child `chat` spans are the LLM calls. The Copilot CLI hooks are optional: they add a redacted prompt preview and trigger the import.
- **Two cost units.** Claude: API-equivalent USD. Copilot: AI units (from `github.copilot.nano_aiu`) as the primary metric, plus an optional API-equivalent USD where a price row exists.
- **Same correctness rules for both clients:** sum every call in a turn, dedupe on a stable key, four token columns, cost NULL for unknown models.

## Scope: in v1 vs deferred

| Item | v1? | Trade-off |
| --- | --- | --- |
| Per-prompt rows for Claude Code (hooks + transcript) | IN | Complete token data, no extra infra |
| Copilot CLI: OTel file import + optional hooks | IN | No receiver. Risk: the file format is not officially documented, so the parser must be tolerant |
| VS Code Copilot Chat: OTel file import only | IN | Same importer. No prompt preview without hooks (content capture stays off) |
| VS Code Copilot CLI harness (`chat.agentHost.otel.*`) | v1.1 | Whether it supports a file exporter is unverified |
| Copilot cost in AI units, optional API-equivalent USD | IN | Copilot bills in AI units/premium requests, not tokens |
| Generic `file_offsets` table for transcripts and OTel files | IN | One incremental-read routine for both sources |
| Separate worker / `raw_events` table | v2 | Parsing in hooks plus `agentlog sync` is enough at personal scale |
| OTLP receiver on :4318 | Dropped | The file exporter covers local use. Revisit only for team aggregation |
| MCP server, dashboard, subagent trees | v2 | No consumer yet. Subagent tokens fold into the parent prompt |
| Two-stage redaction, SQLCipher, retention jobs | v2 | v1 stores only a 200-char redacted preview, with file mode 0600 |
| Answer text | Out | Not needed for cost or latency, and it is the largest privacy risk |

## Architecture

&#91;embedded content: agentlog v1 architecture · 2 clients, 2 local files, 1 store\]

Claude data comes from hooks plus the transcript. Copilot data comes from its OTel file, with optional hooks for previews. One Python CLI does all the parsing, and nothing listens on a port.

## Data model

There are five tables. `prompts` is the per-prompt record. `llm_calls` is the dedupe backbone, and prompt totals are always recomputed from it. Changes from the Claude-only draft: `client`, `cost_basis` and `ai_units` columns, plus a generic `file_offsets` table.

```sql
PRAGMA journal_mode = WAL;  -- once at init
-- per connection: busy_timeout=5000, synchronous=NORMAL, foreign_keys=ON

CREATE TABLE sessions (
  session_id  TEXT PRIMARY KEY,   -- Claude session_id | Copilot sessionId / gen_ai.conversation.id
  client      TEXT NOT NULL,      -- 'claude-code' | 'copilot-cli' | 'copilot-vscode'
  cwd TEXT, repo TEXT, git_branch TEXT,
  started_at  TEXT NOT NULL,      -- ISO 8601 UTC
  ended_at    TEXT, end_reason TEXT
);

CREATE TABLE prompts (            -- one row per user prompt (turn)
  prompt_id      TEXT PRIMARY KEY, -- Claude: hook prompt_id | Copilot: 'cp:' || trace_id
  session_id     TEXT NOT NULL REFERENCES sessions(session_id),
  client         TEXT NOT NULL,
  trace_id       TEXT,             -- Copilot OTel trace (NULL for Claude)
  kind           TEXT NOT NULL DEFAULT 'user',  -- user | auto
  prompt_preview TEXT,             -- redacted, max 200 chars, NULL if capture=off
  prompt_sha256  TEXT,
  started_at     TEXT NOT NULL,
  ended_at       TEXT,
  latency_ms     INTEGER,
  status         TEXT NOT NULL DEFAULT 'open',  -- open | done | aborted | error
  model          TEXT,             -- dominant model (most output tokens)
  llm_call_count INTEGER NOT NULL DEFAULT 0,
  tool_call_count INTEGER NOT NULL DEFAULT 0,
  input_tokens   INTEGER NOT NULL DEFAULT 0,  -- uncached input only
  output_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd       REAL,             -- API-equivalent; NULL if any call is unpriced
  ai_units       REAL,             -- Copilot only: sum(nano_aiu) / 1e9
  cost_basis     TEXT NOT NULL     -- 'api_list' | 'copilot_aiu'
);
CREATE INDEX ix_prompts_session ON prompts(session_id, started_at);
CREATE INDEX ix_prompts_started ON prompts(started_at);
CREATE UNIQUE INDEX ux_prompts_trace ON prompts(trace_id) WHERE trace_id IS NOT NULL;

CREATE TABLE llm_calls (
  call_key   TEXT PRIMARY KEY,  -- Claude: message.id:requestId | Copilot: trace_id:span_id
  prompt_id  TEXT NOT NULL REFERENCES prompts(prompt_id),
  client     TEXT NOT NULL,
  model      TEXT, ts TEXT, duration_ms INTEGER,
  is_subagent INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
  tool_uses  INTEGER NOT NULL DEFAULT 0,
  nano_aiu   INTEGER,           -- Copilot only
  cost_usd   REAL, price_id INTEGER REFERENCES prices(price_id)
);
CREATE INDEX ix_calls_prompt ON llm_calls(prompt_id);

CREATE TABLE file_offsets (       -- incremental reads of transcripts and OTel files
  path TEXT PRIMARY KEY, kind TEXT NOT NULL,  -- 'claude_transcript' | 'copilot_otel'
  byte_offset INTEGER NOT NULL DEFAULT 0, inode INTEGER, size INTEGER, updated_at TEXT
);

CREATE TABLE prices (             -- USD per million tokens
  price_id INTEGER PRIMARY KEY, model_prefix TEXT NOT NULL, effective_from TEXT NOT NULL,
  min_prompt_tokens INTEGER NOT NULL DEFAULT 0,
  in_per_m REAL NOT NULL, out_per_m REAL NOT NULL,
  cache_write_5m_per_m REAL NOT NULL, cache_write_1h_per_m REAL NOT NULL, cache_read_per_m REAL NOT NULL,
  UNIQUE (model_prefix, effective_from, min_prompt_tokens)
);
```

- Create the DB with `os.umask(0o077)`, then `chmod 0600` on the `.db`, `-wal` and `-shm` files.
- Copilot hook rows and OTel rows meet on `prompt_id`. See the Copilot section for the join rule.

## Claude Code integration

A sync `UserPromptSubmit` hook opens the row. An async `Stop` hook parses only the new transcript bytes and closes it. The key is the `prompt_id` that Claude Code sends in every hook payload.

```json
// ~/.claude/settings.json (user level, never the repo's .claude/settings.json)
{
  "hooks": {
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "agentlog", "args": ["hook", "claude"], "timeout": 5 }] }],
    "Stop":         [{ "hooks": [{ "type": "command", "command": "agentlog", "args": ["hook", "claude"], "async": true }] }],
    "SubagentStop": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "agentlog", "args": ["hook", "claude"], "async": true }] }],
    "SessionEnd":   [{ "matcher": "*", "hooks": [{ "type": "command", "command": "agentlog", "args": ["hook", "claude"], "timeout": 2 }] }]
  }
}
```

Hook flow:

1. Read the stdin JSON. Wrap everything in try/except, log errors to a file, **print nothing**, and always exit 0. On `UserPromptSubmit`, stdout is injected into Claude's context, and exit 2 blocks the prompt.
2. **Client guard:** if the payload has camelCase keys (`sessionId`) or no `transcript_path`, Copilot CLI fired a Claude hook. Exit 0 without writing anything.
3. `UserPromptSubmit`: upsert the session, then upsert `prompts(prompt_id)` with `started_at`, the redacted preview and the hash. `cost_basis='api_list'`.
4. `Stop` (async): read the transcript from `file_offsets`, up to the last complete line only. Retry for up to about 3 s, because the transcript may lag the hook.
5. For each assistant entry with `message.usage`, upsert `llm_calls` on `message.id:requestId`. The last copy wins, because streamed chunks repeat the usage.
6. Advance the offset, then recompute the prompt row: sums, tool count, dominant model, cost, latency, `status='done'`.
7. `SubagentStop`: the same loop over `agent_transcript_path`, with `is_subagent=1`, credited to the parent `prompt_id`.
8. `SessionEnd`: set `ended_at` only. No parsing, because of the short time budget.

## GitHub Copilot integration

Copilot hooks carry no token data, so OTel is the source of truth for tokens, model and AI units. The OTel file exporter writes JSON-lines locally, and `agentlog` reads that file from a stored offset. That is the same pattern used for Claude transcripts.

### 1. Enable the OTel file exporter (content capture stays off)

| Surface | Setting |
| --- | --- |
| Copilot CLI | `export COPILOT_OTEL_FILE_EXPORTER_PATH=~/.local/share/agentlog/otel/copilot-cli.jsonl` (this enables OTel and selects the file exporter) |
| VS Code Copilot Chat | `"github.copilot.chat.otel.enabled": true`, `"github.copilot.chat.otel.exporterType": "file"`, `"github.copilot.chat.otel.outfile": "~/.local/share/agentlog/otel/vscode.jsonl"` |
| VS Code Copilot CLI harness | v1.1: check whether `chat.agentHost.otel.*` supports a file exporter |

- Leave `COPILOT_OTEL_CAPTURE_CONTENT` / `github.copilot.chat.otel.captureContent` off. The prompt preview comes from the hook, through the redactor.
- `agentlog init --copilot` prints these lines. It never edits shell profiles silently.

### 2. Optional Copilot CLI hooks (preview + import trigger)

```json
// ~/.copilot/hooks/agentlog.json
{
  "version": 1,
  "hooks": {
    "userPromptSubmitted": [{ "type": "command", "exec": "agentlog", "args": ["hook", "copilot"], "timeoutSec": 5 }],
    "agentStop":           [{ "type": "command", "exec": "agentlog", "args": ["hook", "copilot"], "timeoutSec": 5 }],
    "sessionEnd":          [{ "type": "command", "exec": "agentlog", "args": ["hook", "copilot"], "timeoutSec": 5 }]
  }
}
```

- camelCase event names select the camelCase payload (`sessionId`, `timestamp`, `cwd`, `prompt`).
- Copilot hooks run synchronously, so the hook does bounded work only: insert a row, or import at most 2 MB or 300 ms of new OTel lines.
- `exec` + `args` is CLI-only and avoids shell quoting. On Windows it also works without a `powershell` entry.

### 3. Import flow (`agentlog import copilot`, also run by hooks and by `stats`)

1. Read new complete lines from each OTel file in `file_offsets`. Reset to 0 if the file shrank or its inode changed.
2. Parse tolerantly. Accept both OTLP-JSON (`resourceSpans[].scopeSpans[].spans[]`) and the flat SDK span shape, because GitHub does not document the file format. Count unknown shapes in the error log.
3. **Root `invoke_agent` span** → upsert `prompts` with `prompt_id = 'cp:' || trace_id`, the session from `gen_ai.conversation.id`, start/end from the span, and latency from the span duration.
4. **`chat` spans** → upsert `llm_calls` on `trace_id:span_id`, using `gen_ai.response.model`, the token attributes and `github.copilot.nano_aiu`.
5. **`execute_tool` spans** → increment `tool_call_count`.
6. **Never sum root `invoke_agent` totals with child `chat` spans.** The root repeats the totals. Sum only `chat` spans, and keep the root total as a cross-check that is logged on mismatch.
7. Spans whose root has not been flushed yet create a provisional `open` prompt from their `trace_id`. The root fills it in later.

### 4. Joining hook rows to OTel traces

- The hook writes a pending row: `prompt_id = 'cph:' || sessionId || ':' || timestamp`.
- On import, attach the hook row to the trace whose `gen_ai.conversation.id` equals `sessionId` and whose root span starts within 5 s after the hook `timestamp`. Copy the preview to the `cp:` row, then delete the pending row.
- **Fallback** (if the conversation ID is not the hook `sessionId`, which is unverified): match on time only, and only when exactly one Copilot trace is open in that window. Otherwise the trace keeps no preview, and its tokens still count.
- Hook rows with no trace after 10 minutes → `status='aborted'`, with zero tokens.

### 5. Token semantics and cost

- **Normalize input tokens before storing.** OTel GenAI conventions count cached tokens inside `gen_ai.usage.input_tokens`. Store `input_tokens = input - cache_read - cache_write`, clamped at 0. Confirm this on a recorded fixture before trusting the totals.
- `ai_units = SUM(nano_aiu) / 1e9` is the primary Copilot cost. `cost_basis = 'copilot_aiu'`.
- `cost_usd` for Copilot is an optional API-equivalent estimate, filled only when a price row matches `gen_ai.response.model`. Label it as an estimate in all output.
- `github.copilot.cost` is a model multiplier, not money. Store it nowhere in v1.

### 6. Double counting

- Copilot CLI reads a repo's `.claude/settings.json`, so keep Claude hooks at user level. The client guard in the Claude hook drops Copilot payloads.
- Each OTel span is keyed by `trace_id:span_id`, so importing the same file twice is idempotent.

## Cost calculation and seed prices

Cost is computed per call, then summed per prompt. A prompt's `cost_usd` is NULL if any of its calls is unpriced.

```latex
\text{cost} = \frac{in \cdot p_{in} + out \cdot p_{out} + (cw - cw_{1h}) \cdot p_{cw5m} + cw_{1h} \cdot p_{cw1h} + cr \cdot p_{cr}}{10^6}
```

- The four token columns are disjoint (input excludes cache reads and writes), so nothing is counted twice.
- **Price lookup:** `model` starts with `model_prefix` and the next character is end-of-string, `-`, `@` or `[`. Take the longest prefix, then the latest `effective_from` on or before the call date, then the matching `min_prompt_tokens` tier.
- **Copilot models:** add rows for the model IDs that `gen_ai.response.model` reports (Claude, GPT, Gemini). An unknown ID gives `cost_usd = NULL`, while `ai_units` is still recorded.

Seed rows (USD per million tokens, from Anthropic's pricing page as of Nov 27, 2026; ships as `prices.csv`):

| model\_prefix | in | out | cache write 5m | cache write 1h | cache read |
| --- | --- | --- | --- | --- | --- |
| claude-fable-5-2 | 8 | 40 | 10 | 16 | 0.20 |
| claude-opus-5-5 | 4 | 20 | 5 | 8 | 0.20 |
| claude-sonnet-5-5 | 2 | 10 | 2.50 | 4 | 0.10 |
| claude-haiku-5-5 (prompt ≤ 100K) | 0.10 | 0.50 | 0.125 | 0.20 | 0.01 |
| claude-haiku-5-5 (prompt > 100K) | 0.50 | 2.50 | 0.625 | 1 | 0.05 |
| claude-opus-5 | 5 | 25 | 6.25 | 10 | 0.50 |
| claude-sonnet-5 | 2 | 10 | 2.50 | 4 | 0.20 |
| claude-sonnet-4-x | 3 | 15 | 3.75 | 6 | 0.30 |
| claude-haiku-4-5 | 1 | 5 | 1.25 | 2 | 0.10 |

Verify before seeding. Sonnet 5 shows conflicting rates across Anthropic pages ($2 vs $3 input), and the exact model ID strings should be checked against real transcripts and spans.

## CLI and example queries

| Command | Purpose |
| --- | --- |
| `agentlog init [--claude] [--copilot]` | Create the DB (WAL, 0600), load `prices.csv`, print the hook and OTel config snippets |
| `agentlog hook claude` / `agentlog hook copilot` | Hook entry points (stdin JSON) |
| `agentlog import copilot [file]` | Import new OTel lines. `stats` and `tail` run it first |
| `agentlog stats [--days 7] [--by day\|repo\|model\|client]` | Totals, with USD and AI units in separate columns |
| `agentlog tail [-n 20]` | Last N prompts across both clients |
| `agentlog rescan --all` | Reset offsets and re-parse, after a parser fix or price change |
| `agentlog doctor` | Hooks installed, OTel file growing, DB permissions, last error, unpriced models |

```sql
-- Daily spend per client (USD and AI units kept apart)
SELECT date(started_at,'localtime') AS day, client, COUNT(*) AS prompts,
       ROUND(SUM(cost_usd),2) AS usd_api_equiv, ROUND(SUM(ai_units),2) AS ai_units,
       SUM(cost_usd IS NULL) AS unpriced
FROM prompts GROUP BY day, client ORDER BY day DESC, client;

-- Cost per repo, last 30 days
SELECT s.repo, p.client, COUNT(*) AS prompts, ROUND(SUM(p.cost_usd),2) AS usd,
       ROUND(SUM(p.ai_units),2) AS aiu, ROUND(AVG(p.latency_ms)/1000.0,1) AS avg_s
FROM prompts p JOIN sessions s USING(session_id)
WHERE p.started_at >= datetime('now','-30 days')
GROUP BY s.repo, p.client ORDER BY usd DESC;

-- Slowest prompts
SELECT p.client, s.repo, p.latency_ms/1000.0 AS secs, p.llm_call_count, p.tool_call_count,
       substr(p.prompt_preview,1,60) AS preview
FROM prompts p JOIN sessions s USING(session_id)
WHERE p.status='done' ORDER BY p.latency_ms DESC LIMIT 10;

-- Same model, both clients: tokens per prompt
SELECT model, client, COUNT(DISTINCT prompt_id) AS prompts,
       SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens)/COUNT(DISTINCT prompt_id) AS tok_per_prompt
FROM llm_calls GROUP BY model, client ORDER BY model;
```

## Edge cases

| Case | Client | v1 handling |
| --- | --- | --- |
| Aborted prompt (Esc / Ctrl-C), no Stop | Claude | Row stays `open`. The next prompt in the session marks it `aborted`. Its tokens are still attributed by offset parsing |
| API error ends the turn (`StopFailure`) | Claude | Becomes `aborted`. v1.1 adds a StopFailure hook that sets `error` |
| Streamed duplicates / resumed-session copies | Claude | `message.id:requestId`, last copy wins |
| Transcript lags the hook | Claude | Retry for about 3 s. The next Stop picks up leftover lines |
| OTel spans flushed after `agentStop` (batch exporter delay) | Copilot | The trace is imported later by the next hook, `stats` or `import`. Attribution uses `trace_id`, not import time |
| Root `invoke_agent` not flushed yet | Copilot | Provisional `open` prompt from the child spans. The root fills it in later |
| Root totals vs child `chat` spans | Copilot | Sum children only. Log a mismatch with the root total |
| `input_tokens` includes cached tokens | Copilot | Subtract cache read/write before storing. Verify on a fixture |
| Hook fired, but the OTel exporter is off | Copilot | Preview-only row. After 10 min it becomes `aborted` with 0 tokens. `doctor` warns that the exporter is off |
| Conversation ID does not match the hook `sessionId` | Copilot | Time-window fallback when unambiguous. Otherwise the trace gets no preview, and its tokens still count |
| OTel file rotated or truncated | Copilot | Inode/size check, reset to 0, idempotent upserts |
| Claude hook fired inside Copilot CLI | Both | Client guard drops it. Hooks live at user level |
| Claude model used through Copilot | Both | Counted once, under `copilot`, from OTel only. No Claude transcript exists for it |
| Parallel sessions / windows | Both | Keyed by session/prompt/trace. WAL + `busy_timeout=5000` + one short `BEGIN IMMEDIATE` per write |
| Unknown model | Both | `cost_usd` NULL, shown by `stats` and `doctor` |
| Subscription users (Pro/Max, Copilot plans) | Both | Output says "API-equivalent USD, not your bill". Copilot shows AI units first |

## Privacy minimum

Nothing leaves the machine in v1. There is no network code, and the OTel exporters write only to local files.

- **Capture levels:** `off` (hash and length only) or `preview` (200 chars, redacted; the default). No answer text is stored.
- **OTel content capture stays off** in both Copilot surfaces. With content capture off, the OTel file holds metadata and token counts only.
- **Regex redaction before write:** Anthropic/OpenAI keys, GitHub tokens (`ghp_`, `gho_`, `github_pat_`), AWS keys, JWTs, PEM private keys, `password=`/`token=`/`secret=` pairs, Bearer headers, and `user:pass@` connection strings.
- **Files:** the DB, its WAL/SHM files and the `otel/` directory are user-only (0600 files, 0700 directories). Parameterized SQL only.
- **Retention:** `agentlog import` truncates fully imported OTel files above 50 MB, because they hold no content.
- **AVL:** check the corporate source-code and AI-usage policies before enabling `preview` on work repos. Default work machines to `off`.

## Tests and acceptance gates

| Gate | Pass criterion |
| --- | --- |
| Claude token correctness | On 20 real sessions, per-session totals for all four token columns match `ccusage session` within 1% |
| Claude cost cross-check | On 5 API-key sessions, the `cost_usd` sum from Claude Code's native OTel `api_request` events (console exporter) matches within 2% |
| Copilot token correctness | On 10 Copilot CLI and 5 VS Code sessions, the `chat`-span sums match the root `invoke_agent` totals exactly. Cross-check against `ccusage` Copilot CLI output where available |
| Copilot input semantics | A recorded fixture with a known cached prompt confirms whether `input_tokens` includes cache tokens |
| Copilot join | With 2 parallel Copilot CLI sessions, every hook preview lands on the right trace, or is left unmatched. Never on the wrong one |
| Fixtures | Streamed duplicates, compaction, subagent file, half-written line, rotated file, OTLP-JSON vs flat span shape, missing root span, unknown model |
| Non-blocking | Claude `UserPromptSubmit` and Copilot hooks have p99 < 50 ms on a 10k-prompt DB. stdout is always empty |
| Never fails the turn | Fuzzed stdin (bad JSON, missing fields, locked DB, unreadable file) always exits 0 |
| No double counting | A Claude-hooked repo used from Copilot CLI produces Copilot rows only |
| Idempotency | `rescan --all` twice gives identical totals |

## Implementation language and roadmap

**Decision: Python 3.11+ with the stdlib only, installed with `uv tool install`.** Revisit Rust only for the hook entry point, and only if the latency gate fails.

| Factor | Python | Rust |
| --- | --- | --- |
| Hook cold start | About 20–40 ms with lazy imports | About 1–3 ms |
| Fits the 50 ms p99 gate | Yes, with margin, if the hook imports only `sys`, `json`, `sqlite3`, `re`, `hashlib` | Easily |
| Iteration on undocumented formats (transcript JSONL, Copilot OTel file) | Fast: edit, rerun, no compile step | Slower: serde structs need updating with every field change |
| Dependencies | `sqlite3`, `json`, `re` are in the stdlib | `rusqlite` (bundled SQLite), `serde_json`, `regex` |
| Distribution | Needs Python + `uv`. Windows works, with a slightly slower start | One static binary per OS. Easiest on Windows and in devcontainers |
| Teaching / course-lab reuse | Students can read and extend it | Higher barrier |

- **Why Python wins for v1:** the risk is format drift, not speed. Copilot hooks are synchronous, so each Copilot prompt pays two hook starts (about 80 ms). That is small next to a multi-second LLM turn.
- **Escape hatch:** keep `agentlog hook` as a thin module with no shared heavy imports. If p99 exceeds 50 ms, rewrite only that entry point in Rust (about 300 lines). The importer and CLI stay in Python, and both write the same schema.

Roadmap (about 12 working days):

1. **Days 1–2:** schema, `init`, `file_offsets`, prices CSV and lookup, with unit tests.
2. **Days 3–4:** Claude transcript parser and Claude hooks. Gate: ccusage within 1%.
3. **Days 5–7:** Copilot OTel importer (tolerant parser, chat-span sums, provisional prompts, token normalization). Gate: chat sums equal root totals.
4. **Days 8–9:** Copilot CLI hooks, the hook-to-trace join and the client guard. Gate: 2-session join test, no double rows.
5. **Day 10:** `stats`, `tail`, `rescan`, `doctor`, redaction tests.
6. **Days 11–12:** dogfood across both clients, fix drift, write the README, tag v1.0.
7. **v1.1:** VS Code Copilot CLI harness, Claude StopFailure hook, `agentlog rate` for feedback.

## Open questions, confidence, sources

**Open questions**

- Does Copilot's `gen_ai.conversation.id` equal the hook `sessionId`? Check this on day 8 with one live session. It decides between an exact join and the time-window fallback.
- Is there one root `invoke_agent` span per user turn in both Copilot CLI and VS Code? Confirm from a recorded file.
- Show Copilot cost as AI units only, or also as an estimated USD?
- Personal tool, Lunacraft product or course lab? This decides the v2 focus (MCP/dashboard vs packaging).

**Confidence**

| Claim area | Rating |
| --- | --- |
| Claude Code hooks, `prompt_id`, async, exit codes | Certain (\~95%) |
| Copilot hook events, `version: 1`, `exec`/`args`, file locations | In-between (\~85%) |
| Copilot OTel file exporter variables and VS Code `exporterType`/`outfile` | In-between (\~85%) |
| Copilot span model (root `invoke_agent` per turn, child `chat` spans) and the conversation-ID join | In-between (\~65%), verify live |
| Copilot `input_tokens` including cached tokens | Uncertain (\~60%), verify on a fixture |
| Claude transcript structure and dedupe key | In-between (\~75%), community-sourced |
| Seed prices | In-between (\~85%), Sonnet 5 conflict noted |
| Python start-up figures and effort estimate | Uncertain (\~65%), measure in the gate |
| **Overall plan** | **In-between (\~78%)** |

**Sources**

- [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)
- [Claude Code monitoring (OTel)](https://code.claude.com/docs/en/monitoring-usage)
- [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- [GitHub Copilot hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference)
- [Copilot CLI hooks reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference)
- [About hooks for GitHub Copilot](https://docs.github.com/en/copilot/concepts/agents/hooks)
- [Copilot OpenTelemetry reference](https://docs.github.com/en/copilot/reference/enterprise-administrators/opentelemetry-reference)
- [VS Code: Monitor agent usage with OpenTelemetry](https://code.visualstudio.com/docs/agents/guides/monitoring-agents)
- [Copilot SDK telemetry config (file exporter)](https://docs.github.com/en/copilot/how-tos/copilot-sdk/observability/opentelemetry)
- [ccusage](https://ccusage.com/guide/)

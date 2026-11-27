# Prompt Telemetry

Python 3.11+, stdlib only. Nothing listens on a port and nothing leaves the machine.

## About

**Prompt Telemetry** (CLI: `prompt_telemetry`) is a local-first telemetry tool for AI coding assistants. It records usage for each prompt you send to Claude Code or GitHub Copilot (CLI and VS Code Chat) in a single SQLite database. This lets you see where tokens, spend, and time go without a hosted service.

- **Collection:** Claude Code hooks and Copilot OpenTelemetry file exports feed one shared schema.
- **Per-prompt metrics:** tokens (input, output, cached), estimated USD cost, latency, model, repo, and tool-call count.
- **Reporting:** built-in `stats` and `tail` commands summarise usage by day, repo, model, or client.
- **Privacy:** no network listeners and no uploads. Prompt previews are redacted and truncated, and capture can be turned off so only a hash is stored.
- **Lightweight:** Python 3.11+, standard library only.

## Install

```sh
uv tool install .            # or: pip install .
prompt_telemetry init             # creates the DB (WAL, 0600) and prints the config snippets
```

`init` never edits your settings or shell profile; paste the printed snippets yourself.

- **Claude Code:** add the hooks to `~/.claude/settings.json` (user level, not the repo's).
- **Copilot CLI:** `export COPILOT_OTEL_FILE_EXPORTER_PATH=~/.local/share/prompt_telemetry/otel/copilot-cli.jsonl`; optionally add `~/.copilot/hooks/prompt_telemetry.json` for prompt previews.
- **VS Code Copilot Chat:** set `github.copilot.chat.otel.{enabled,exporterType,outfile}`.
- Keep OTel content capture off.

## Commands

| Command | Purpose |
| --- | --- |
| `prompt_telemetry init [--claude] [--copilot]` | Create DB, load `prices.csv`, print config |
| `prompt_telemetry hook claude\|copilot` | Hook entry points (stdin JSON; silent, always exit 0) |
| `prompt_telemetry import copilot [file]` | Import new OTel lines |
| `prompt_telemetry stats [--days 7] [--by day\|repo\|model\|client]` | Totals |
| `prompt_telemetry tail [-n 20]` | Last prompts |
| `prompt_telemetry rescan --all` | Reset offsets, re-parse, reprice |
| `prompt_telemetry doctor` | Check hooks, OTel files, permissions, unpriced models |

## Notes

- Data dir: `$PROMPT_TELEMETRY_HOME` or `~/.local/share/prompt_telemetry`. Set `PROMPT_TELEMETRY_CAPTURE=off` (or `{"capture": "off"}` in `config.json`) to store only a prompt hash.
- Prompt previews are 200 chars, redacted before truncation. Answer text is never stored.
- USD is API-equivalent (an estimate, not your bill). Copilot's primary cost is AI units (`github.copilot.nano_aiu`).
- Copilot `input_tokens` is assumed to include cached tokens and is normalized to the uncached remainder; verify on a recorded fixture.
- Besides the five planned tables there are two helpers: `tool_calls` (dedupes tool uses for idempotent re-imports) and `otel_roots` (root-span totals for the cross-check, logged to `errors.log` on mismatch).
- The `claude-sonnet-4-x` seed row is stored as prefix `claude-sonnet-4`. Verify seed prices before relying on them.

## Tests

```sh
python -m unittest discover -s tests
```

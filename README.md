# agentlog

One SQLite row per prompt for **Claude Code** and **GitHub Copilot**: tokens, cost, latency, model, tool count.
Python 3.11+, stdlib only. Nothing listens on a port and nothing leaves the machine.

## Install

```sh
uv tool install .          # or: pip install .
agentlog init              # creates the DB (WAL, 0600) and prints the config snippets
```

`init` never edits your settings or shell profile; paste the printed snippets yourself.

- **Claude Code:** add the hooks to `~/.claude/settings.json` (user level, not the repo's).
- **Copilot CLI:** `export COPILOT_OTEL_FILE_EXPORTER_PATH=~/.local/share/agentlog/otel/copilot-cli.jsonl`; optionally add `~/.copilot/hooks/agentlog.json` for prompt previews.
- **VS Code Copilot Chat:** set `github.copilot.chat.otel.{enabled,exporterType,outfile}`.
- Keep OTel content capture off.

## Commands

| Command | Purpose |
| --- | --- |
| `agentlog init [--claude] [--copilot]` | Create DB, load `prices.csv`, print config |
| `agentlog hook claude\|copilot` | Hook entry points (stdin JSON; silent, always exit 0) |
| `agentlog import copilot [file]` | Import new OTel lines |
| `agentlog stats [--days 7] [--by day\|repo\|model\|client]` | Totals |
| `agentlog tail [-n 20]` | Last prompts |
| `agentlog rescan --all` | Reset offsets, re-parse, reprice |
| `agentlog doctor` | Check hooks, OTel files, permissions, unpriced models |

## Notes

- Data dir: `$AGENTLOG_HOME` or `~/.local/share/agentlog`. Set `AGENTLOG_CAPTURE=off` (or `{"capture": "off"}` in `config.json`) to store only a prompt hash.
- Prompt previews are 200 chars, redacted before truncation. Answer text is never stored.
- USD is API-equivalent (an estimate, not your bill). Copilot's primary cost is AI units (`github.copilot.nano_aiu`).
- Copilot `input_tokens` is assumed to include cached tokens and is normalized to the uncached remainder; verify on a recorded fixture.
- Besides the five planned tables there are two helpers: `tool_calls` (dedupes tool uses for idempotent re-imports) and `otel_roots` (root-span totals for the cross-check, logged to `errors.log` on mismatch).
- The `claude-sonnet-4-x` seed row is stored as prefix `claude-sonnet-4`. Verify seed prices before relying on them.

## Tests

```sh
python -m unittest discover -s tests
```

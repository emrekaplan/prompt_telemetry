import json
import sys

CLAUDE_HOOKS = {
    "hooks": {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "prompt_telemetry", "args": ["hook", "claude"], "timeout": 5}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "prompt_telemetry", "args": ["hook", "claude"], "async": True}]}],
        "SubagentStop": [{"matcher": "*", "hooks": [{"type": "command", "command": "prompt_telemetry", "args": ["hook", "claude"], "async": True}]}],
        "SessionEnd": [{"matcher": "*", "hooks": [{"type": "command", "command": "prompt_telemetry", "args": ["hook", "claude"], "timeout": 2}]}],
    }
}

COPILOT_HOOKS = {
    "version": 1,
    "hooks": {
        ev: [{"type": "command", "exec": "prompt_telemetry", "args": ["hook", "copilot"], "timeoutSec": 5}]
        for ev in ("userPromptSubmitted", "agentStop", "sessionEnd")
    },
}


def _init(args):
    from . import db
    conn = db.connect()
    db.init_db(conn)
    db.ensure_dirs()
    db.secure_files()
    conn.close()
    print(f"Database ready: {db.db_path()} (WAL, mode 0600)")
    both = not (args.claude or args.copilot)
    if args.claude or both:
        print("\n# Claude Code: merge into ~/.claude/settings.json (user level, not the repo's)")
        print(json.dumps(CLAUDE_HOOKS, indent=2))
    if args.copilot or both:
        otel = db.otel_dir()
        print("\n# Copilot CLI: add to your shell profile (prompt_telemetry never edits it)")
        print(f"export COPILOT_OTEL_FILE_EXPORTER_PATH={otel}/copilot-cli.jsonl")
        print("# Leave COPILOT_OTEL_CAPTURE_CONTENT unset/off.")
        print("\n# VS Code Copilot Chat: settings.json")
        print(json.dumps({
            "github.copilot.chat.otel.enabled": True,
            "github.copilot.chat.otel.exporterType": "file",
            "github.copilot.chat.otel.outfile": f"{otel}/vscode.jsonl"}, indent=2))
        print("\n# Optional Copilot CLI hooks (prompt preview + import trigger): ~/.copilot/hooks/prompt_telemetry.json")
        print(json.dumps(COPILOT_HOOKS, indent=2))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "hook":
        from .hook import run
        return run(argv[1:])

    import argparse
    ap = argparse.ArgumentParser(prog="prompt_telemetry", description="Per-prompt telemetry for Claude Code and Copilot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="create the DB and print config snippets")
    p.add_argument("--claude", action="store_true")
    p.add_argument("--copilot", action="store_true")
    p = sub.add_parser("hook", help="hook entry point (reads stdin JSON)")
    p.add_argument("client", choices=["claude", "copilot"])
    p = sub.add_parser("import", help="import new telemetry")
    p.add_argument("source", choices=["copilot"])
    p.add_argument("file", nargs="?")
    p = sub.add_parser("stats", help="totals")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--by", choices=["day", "repo", "model", "client"], default="day")
    p = sub.add_parser("tail", help="last N prompts")
    p.add_argument("-n", type=int, default=20)
    p = sub.add_parser("rescan", help="reset offsets and re-parse")
    p.add_argument("--all", action="store_true", required=True)
    sub.add_parser("doctor", help="check the setup")
    args = ap.parse_args(argv)

    from . import db, report
    if args.cmd == "init":
        _init(args)
        return 0
    if args.cmd == "doctor":
        report.doctor(db.connect)
        return 0
    conn = db.connect()
    try:
        if args.cmd == "import":
            from . import copilot
            n = copilot.import_all(conn, [args.file] if args.file else None, retention=True)
            print(f"imported {n} new lines")
        elif args.cmd == "stats":
            report.stats(conn, args.days, args.by)
        elif args.cmd == "tail":
            report.tail(conn, args.n)
        elif args.cmd == "rescan":
            report.rescan(conn)
            print("rescan complete")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

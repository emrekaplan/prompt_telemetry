"""Thin hook entry point: stdlib-only, never prints, never fails the turn."""
import json
import sys


def run(argv):
    try:
        client = argv[0] if argv else ""
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if client == "claude":
            from . import claude
            claude.handle_hook(payload)
        elif client == "copilot":
            from . import copilot
            copilot.handle_hook(payload, argv[1] if len(argv) > 1 else None)
    except BaseException as e:  # noqa: BLE001 - a hook must always exit 0
        try:
            from . import db
            db.log_error(f"hook {argv[:1]}: {e!r}")
        except BaseException:
            pass
    return 0

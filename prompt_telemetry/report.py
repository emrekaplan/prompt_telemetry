import json
import os
import time

from . import copilot, db

NOTE = "USD is API-equivalent (an estimate, not your bill). Copilot cost is shown in AI units first."


def _table(headers, rows):
    rows = [["" if v is None else str(v) for v in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths))]
    lines += ["  ".join(v.ljust(w) for v, w in zip(r, widths)) for r in rows]
    return "\n".join(lines)


def sync(conn):
    try:
        copilot.import_all(conn, retention=True)
    except Exception as e:
        db.log_error(f"import before report: {e!r}")


GROUPS = {
    "day": ("date(p.started_at,'localtime')", ""),
    "repo": ("COALESCE(s.repo,'(unknown)')", "LEFT JOIN sessions s ON s.session_id=p.session_id"),
    "model": ("COALESCE(p.model,'(none)')", ""),
    "client": ("p.client", ""),
}


def stats(conn, days=7, by="day"):
    sync(conn)
    expr, join = GROUPS[by]
    since = db.add_seconds(db.now_iso(), -days * 86400)
    rows = conn.execute(
        f"""SELECT {expr} AS g, COUNT(*), ROUND(SUM(p.cost_usd),2), ROUND(SUM(p.ai_units),2),
                   SUM(p.input_tokens+p.output_tokens+p.cache_read_tokens+p.cache_write_tokens),
                   ROUND(AVG(p.latency_ms)/1000.0,1), SUM(p.cost_usd IS NULL AND p.llm_call_count>0)
            FROM prompts p {join} WHERE p.started_at>=? AND p.prompt_id NOT LIKE 'cph:%'
            GROUP BY g ORDER BY g DESC""", (since,)).fetchall()
    print(_table([by, "prompts", "usd_api_equiv", "ai_units", "tokens", "avg_s", "unpriced"], rows))
    print(f"\n{NOTE}")


def tail(conn, n=20):
    sync(conn)
    rows = conn.execute(
        """SELECT substr(p.started_at,1,19), p.client, p.status, p.model, p.latency_ms/1000.0,
                  p.llm_call_count, p.tool_call_count, p.cost_usd, p.ai_units, substr(p.prompt_preview,1,50)
           FROM prompts p ORDER BY p.started_at DESC LIMIT ?""", (n,)).fetchall()
    rows = [[r[0], r[1], r[2], r[3], None if r[4] is None else f"{r[4]:.1f}", r[5], r[6],
             None if r[7] is None else f"{r[7]:.4f}", None if r[8] is None else f"{r[8]:.2f}", r[9]]
            for r in rows]
    print(_table(["started_utc", "client", "status", "model", "secs", "llm", "tools", "usd_est", "aiu", "preview"], rows))
    print(f"\n{NOTE}")


def rescan(conn):
    from . import claude
    with db.tx(conn):
        conn.execute("UPDATE file_offsets SET byte_offset=0, inode=NULL, size=NULL")
    paths = conn.execute("SELECT path, kind FROM file_offsets").fetchall()
    for path, kind in paths:
        if not os.path.exists(path):
            continue
        if kind == "claude_transcript":
            claude.ingest_transcript(conn, path)
        elif kind == "copilot_otel":
            copilot.import_file(conn, path)
    with db.tx(conn):
        db.reprice_all(conn)
        copilot.join_pending(conn)
        for (pid,) in conn.execute("SELECT prompt_id FROM prompts").fetchall():
            db.recompute_prompt(conn, pid)
    for (pid,) in conn.execute("SELECT prompt_id FROM prompts WHERE client='claude-code'").fetchall():
        claude.finalize(conn, pid, close=False)


def doctor(conn_factory):
    ok = lambda b: "ok  " if b else "WARN"  # noqa: E731
    print(f"prompt_telemetry home: {db.home()}")
    path = db.db_path()
    if not os.path.exists(path):
        print(f"WARN db missing: run `prompt_telemetry init`")
        return
    for p in (path, path + "-wal", path + "-shm"):
        if os.path.exists(p):
            mode = os.stat(p).st_mode & 0o777
            print(f"{ok(mode & 0o077 == 0)} {os.path.basename(p)} mode {mode:o}")
    claude_cfg = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(claude_cfg, encoding="utf-8") as f:
            installed = "prompt_telemetry" in f.read()
    except OSError:
        installed = False
    print(f"{ok(installed)} Claude hooks in {claude_cfg}")
    hooks = os.path.expanduser("~/.copilot/hooks/prompt_telemetry.json")
    print(f"{ok(os.path.exists(hooks))} Copilot CLI hooks (optional) at {hooks}")
    env = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH")
    print(f"{ok(bool(env))} COPILOT_OTEL_FILE_EXPORTER_PATH = {env or '(unset)'}")
    files = copilot.default_files()
    if not files:
        print("WARN no Copilot OTel files found (exporter off?)")
    for f in files:
        age = time.time() - os.stat(f).st_mtime
        print(f"ok   {f}: {os.stat(f).st_size} bytes, last write {int(age)}s ago")
    conn = conn_factory()
    try:
        aborted = conn.execute("SELECT COUNT(*) FROM prompts WHERE prompt_id LIKE 'cph:%' "
                               "AND status='aborted'").fetchone()[0]
        if aborted:
            print(f"WARN {aborted} Copilot hook prompts never matched an OTel trace (exporter off?)")
        unpriced = conn.execute("SELECT model, COUNT(*) FROM llm_calls WHERE cost_usd IS NULL "
                                "GROUP BY model").fetchall()
        for m, n in unpriced:
            print(f"WARN unpriced model {m!r}: {n} calls")
        if not unpriced:
            print("ok   all models priced")
    finally:
        conn.close()
    try:
        with open(db.error_log_path(), encoding="utf-8") as f:
            last = f.readlines()[-1:]
        print(f"last error: {last[0].strip()}" if last else "ok   no errors logged")
    except OSError:
        print("ok   no errors logged")

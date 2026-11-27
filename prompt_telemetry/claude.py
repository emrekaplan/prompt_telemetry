import json
import os
import time

from . import db
from .redact import preview, sha256

CLIENT = "claude-code"


def _git_info(cwd):
    """(repo name, branch) from the nearest .git, without spawning git."""
    if not cwd:
        return None, None
    d = os.path.abspath(cwd)
    while True:
        g = os.path.join(d, ".git")
        if os.path.exists(g):
            branch = None
            try:
                if os.path.isfile(g):  # worktree / submodule pointer
                    with open(g, encoding="utf-8") as f:
                        gd = f.read().strip().removeprefix("gitdir:").strip()
                    g = gd if os.path.isabs(gd) else os.path.join(d, gd)
                with open(os.path.join(g, "HEAD"), encoding="utf-8") as f:
                    head = f.read().strip()
                if head.startswith("ref:"):
                    branch = head.rsplit("/", 1)[-1]
            except OSError:
                pass
            return os.path.basename(d), branch
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.basename(os.path.abspath(cwd)), None
        d = parent


def git_info(cwd):
    return _git_info(cwd)


def prompt_for_ts(conn, session_id, ts):
    """Latest prompt of the session that started at or before ts (fallback: earliest)."""
    row = conn.execute(
        "SELECT prompt_id FROM prompts WHERE session_id=? AND client=? AND started_at<=? "
        "ORDER BY started_at DESC LIMIT 1", (session_id, CLIENT, ts or db.now_iso())).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT prompt_id FROM prompts WHERE session_id=? AND client=? ORDER BY started_at LIMIT 1",
            (session_id, CLIENT)).fetchone()
    return row[0] if row else None


def _parse_batch(conn, lines, forced_prompt, forced_session, forced_sub):
    calls: dict[str, dict] = {}
    tools: dict[str, tuple[str, str]] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            db.log_error("claude transcript: bad JSON line skipped")
            continue
        if not isinstance(e, dict) or e.get("type") != "assistant":
            continue
        msg = e.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
            continue
        model = msg.get("model")
        if model == "<synthetic>":
            continue
        mid = msg.get("id") or e.get("uuid")
        if not mid:
            continue
        key = f"{mid}:{e['requestId']}" if e.get("requestId") else mid
        u = msg["usage"]
        cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        cw1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
        cw = int(u.get("cache_creation_input_tokens") or 0) or (
            cw1h + int(cc.get("ephemeral_5m_input_tokens") or 0))
        ts = db.norm_ts(e.get("timestamp"))
        # Subagent files are credited to the parent prompt; main transcripts are attributed by
        # timestamp so an aborted prompt keeps its own tokens.
        pid = (forced_prompt if forced_sub else None) or prompt_for_ts(
            conn, forced_session or e.get("sessionId"), ts) or forced_prompt
        if not pid:
            continue
        tool_ids = []
        for i, b in enumerate(msg.get("content") or []):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tool_ids.append(b.get("id") or f"{key}#{i}")
        prev = calls.get(key)
        calls[key] = {  # last copy wins: streamed chunks repeat the usage
            "call_key": key, "prompt_id": pid, "client": CLIENT, "model": model, "ts": ts,
            "is_subagent": int(bool(forced_sub or e.get("isSidechain"))),
            "input_tokens": int(u.get("input_tokens") or 0),
            "output_tokens": int(u.get("output_tokens") or 0),
            "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
            "cache_write_tokens": cw, "cache_write_1h_tokens": min(cw1h, cw),
            "_tools": (prev["_tools"] if prev else []) + tool_ids,
        }
    touched = set()
    for key, d in calls.items():
        tool_ids = list(dict.fromkeys(d.pop("_tools")))
        d["tool_uses"] = len(tool_ids)
        old = conn.execute("SELECT tool_uses FROM llm_calls WHERE call_key=?", (key,)).fetchone()
        if old:
            d["tool_uses"] = max(d["tool_uses"], old[0])
        db.upsert_call(conn, d)
        for t in tool_ids:
            conn.execute(
                "INSERT INTO tool_calls (call_key, prompt_id) VALUES (?,?) "
                "ON CONFLICT(call_key) DO UPDATE SET prompt_id=excluded.prompt_id", (t, d["prompt_id"]))
        touched.add(d["prompt_id"])
    for pid in touched:
        db.recompute_prompt(conn, pid)
    return touched


def ingest_transcript(conn, path, forced_prompt=None, forced_session=None, forced_sub=False,
                      retry_s=0.0):
    """Parse new transcript bytes. Retries briefly while the transcript lags the hook."""
    deadline = time.monotonic() + retry_s
    touched: set = set()

    def handler(lines):
        touched.update(_parse_batch(conn, lines, forced_prompt, forced_session, forced_sub))

    while True:
        if os.path.exists(path):
            db.ingest_file(conn, path, "claude_transcript", handler)
        if touched or time.monotonic() >= deadline:
            return touched
        time.sleep(0.25)


def finalize(conn, prompt_id, close=True):
    with db.tx(conn):
        row = conn.execute("SELECT started_at, status FROM prompts WHERE prompt_id=?", (prompt_id,)).fetchone()
        if not row:
            return
        last = conn.execute("SELECT MAX(ts) FROM llm_calls WHERE prompt_id=?", (prompt_id,)).fetchone()[0]
        ended = last if (last and last >= row[0]) else db.now_iso()
        db.recompute_prompt(conn, prompt_id)
        status = "done" if (close and row[1] in ("open", "done")) else row[1]
        conn.execute("UPDATE prompts SET ended_at=?, latency_ms=?, status=? WHERE prompt_id=?",
                     (ended, db.ms_between(row[0], ended), status, prompt_id))


def _current_prompt(conn, session_id):
    row = conn.execute("SELECT prompt_id FROM prompts WHERE session_id=? AND client=? "
                       "ORDER BY started_at DESC LIMIT 1", (session_id, CLIENT)).fetchone()
    return row[0] if row else None


def handle_hook(payload: dict):
    if not isinstance(payload, dict):
        return
    # Copilot CLI also fires Claude-style hooks configured in a repo: drop its payloads.
    if "sessionId" in payload or not payload.get("transcript_path"):
        return
    event = payload.get("hook_event_name")
    sid = payload.get("session_id")
    if not sid:
        return
    conn = db.connect()
    try:
        if event == "UserPromptSubmit":
            _on_prompt(conn, payload, sid)
        elif event == "Stop":
            _on_stop(conn, payload, sid, payload["transcript_path"], False)
        elif event == "SubagentStop":
            _on_stop(conn, payload, sid, payload.get("agent_transcript_path"), True)
        elif event == "SessionEnd":
            with db.tx(conn):
                conn.execute("UPDATE sessions SET ended_at=?, end_reason=? WHERE session_id=?",
                             (db.now_iso(), payload.get("reason"), sid))
    finally:
        conn.close()


def _on_prompt(conn, p, sid):
    now = db.now_iso()
    cwd = p.get("cwd")
    repo, branch = git_info(cwd)
    text = p.get("prompt") if isinstance(p.get("prompt"), str) else None
    pid = p.get("prompt_id") or f"cc:{sid}:{int(time.time() * 1000)}"
    with db.tx(conn):
        db.upsert_session(conn, sid, CLIENT, now, cwd, repo, branch)
        conn.execute("UPDATE prompts SET status='aborted' WHERE session_id=? AND client=? "
                     "AND status='open' AND prompt_id<>?", (sid, CLIENT, pid))
        conn.execute(
            """INSERT INTO prompts (prompt_id, session_id, client, prompt_preview, prompt_sha256,
                                    started_at, status, cost_basis)
               VALUES (?,?,?,?,?,?,'open','api_list')
               ON CONFLICT(prompt_id) DO UPDATE SET
                 prompt_preview=excluded.prompt_preview, prompt_sha256=excluded.prompt_sha256""",
            (pid, sid, CLIENT, preview(text, db.capture_mode()), sha256(text), now))


def _on_stop(conn, p, sid, path, sub):
    if not path:
        return
    pid = p.get("prompt_id")
    if not pid or not conn.execute("SELECT 1 FROM prompts WHERE prompt_id=?", (pid,)).fetchone():
        pid = _current_prompt(conn, sid)
    if not pid:
        return
    touched = ingest_transcript(conn, path, forced_prompt=pid, forced_session=sid, forced_sub=sub,
                                retry_s=0.0 if sub else 3.0)
    for t in touched | {pid}:
        finalize(conn, t, close=not sub)

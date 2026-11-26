import glob
import json
import os
import time

from . import db
from .redact import preview, sha256

CLI = "copilot-cli"
VSCODE = "copilot-vscode"
PENDING_TIMEOUT_S = 600
JOIN_WINDOW_S = 5.0
JOIN_SLACK_S = 1.0
RETENTION_BYTES = 50 * 1024 * 1024

_IN = ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens")
_OUT = ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens")
_CR = ("gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_read_input_tokens",
       "gen_ai.usage.cache_read_tokens")
_CW = ("gen_ai.usage.cache_creation.input_tokens", "gen_ai.usage.cache_creation_input_tokens",
       "gen_ai.usage.cache_write.input_tokens", "gen_ai.usage.cache_write_tokens")
_IGNORED_ROOTS = ("resourceMetrics", "resourceLogs", "resource_metrics", "resource_logs",
                  "scopeMetrics", "scopeLogs", "logRecords")


# ---- tolerant span parsing ---------------------------------------------

def _anyvalue(v):
    if not isinstance(v, dict):
        return v
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return v[k]
    return None


def _attrs(raw):
    if isinstance(raw, dict):
        return {k: _anyvalue(v) if isinstance(v, dict) else v for k, v in raw.items()}
    out = {}
    if isinstance(raw, list):
        for a in raw:
            if isinstance(a, dict) and "key" in a:
                out[a["key"]] = _anyvalue(a.get("value"))
    return out


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _first(attrs, names):
    for n in names:
        if attrs.get(n) is not None:
            v = _num(attrs[n])
            if v is not None:
                return v
    return None


def _empty_id(v):
    return not v or set(str(v)) <= {"0"}


def normalize_span(sp: dict):
    """Both OTLP-JSON and flat SDK shapes -> one dict; None if it isn't a span."""
    ctx = sp.get("spanContext") or sp.get("span_context") or sp.get("context") or {}
    trace = sp.get("traceId") or sp.get("trace_id") or ctx.get("traceId") or ctx.get("trace_id")
    span = sp.get("spanId") or sp.get("span_id") or ctx.get("spanId") or ctx.get("span_id")
    if not trace or not span:
        return None
    parent = sp.get("parentSpanId") or sp.get("parent_span_id") or sp.get("parentId") or sp.get("parent_id")
    if not parent and isinstance(sp.get("parentSpanContext"), dict):
        parent = sp["parentSpanContext"].get("spanId")
    start = db.to_ns(sp.get("startTimeUnixNano", sp.get("startTime", sp.get("start_time"))))
    end = db.to_ns(sp.get("endTimeUnixNano", sp.get("endTime", sp.get("end_time"))))
    status = sp.get("status")
    code = status.get("code") if isinstance(status, dict) else status
    return {
        "trace": str(trace), "span": str(span), "parent": None if _empty_id(parent) else str(parent),
        "name": str(sp.get("name") or ""), "start": start, "end": end,
        "attrs": _attrs(sp.get("attributes")), "error": str(code).upper() in ("2", "ERROR", "STATUS_CODE_ERROR"),
    }


def iter_spans(obj, counters):
    if isinstance(obj, list):
        for o in obj:
            yield from iter_spans(o, counters)
    elif isinstance(obj, dict):
        if "resourceSpans" in obj or "resource_spans" in obj:
            for rs in obj.get("resourceSpans") or obj.get("resource_spans") or []:
                scopes = (rs.get("scopeSpans") or rs.get("scope_spans")
                          or rs.get("instrumentationLibrarySpans") or [])
                for ss in scopes:
                    for sp in ss.get("spans") or []:
                        n = normalize_span(sp)
                        if n:
                            yield n
                        else:
                            counters["unknown"] += 1
        elif any(k in obj for k in _IGNORED_ROOTS) or obj.get("type") in ("metric", "log"):
            return
        else:
            n = normalize_span(obj)
            if n:
                yield n
            else:
                counters["unknown"] += 1
    else:
        counters["unknown"] += 1


def _op(s):
    op = s["attrs"].get("gen_ai.operation.name")
    if op:
        return str(op)
    first = s["name"].split(" ", 1)[0]
    return first if first in ("invoke_agent", "chat", "execute_tool") else None


# ---- import -------------------------------------------------------------

def client_for(path: str) -> str:
    return VSCODE if os.path.basename(path).lower().startswith("vscode") else CLI


def _ensure_trace(conn, trace, client, conv, ts_iso):
    pid = f"cp:{trace}"
    sid = conv or f"cp-trace:{trace}"
    db.upsert_session(conn, sid, client, ts_iso)
    conn.execute(
        """INSERT INTO prompts (prompt_id, session_id, client, trace_id, started_at, status, cost_basis)
           VALUES (?,?,?,?,?,'open','copilot_aiu') ON CONFLICT(prompt_id) DO NOTHING""",
        (pid, sid, client, trace, ts_iso))
    if conv:
        conn.execute("UPDATE prompts SET session_id=? WHERE prompt_id=? AND session_id<>?", (conv, pid, conv))
    conn.execute("UPDATE prompts SET started_at=? WHERE prompt_id=? AND started_at>?", (ts_iso, pid, ts_iso))
    return pid


def _handle_batch(conn, lines, client, counters):
    spans = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            counters["bad"] += 1
            continue
        spans.extend(iter_spans(obj, counters))
    touched, roots = set(), {}
    for s in spans:
        op = _op(s)
        if op not in ("invoke_agent", "chat", "execute_tool"):
            continue
        a = s["attrs"]
        start = db.ns_to_iso(s["start"]) if s["start"] is not None else db.now_iso()
        conv = a.get("gen_ai.conversation.id")
        conv = str(conv) if conv else None
        pid = _ensure_trace(conn, s["trace"], client, conv, start)
        touched.add(pid)
        if op == "invoke_agent":
            if s["parent"] is not None:
                continue  # nested agents fold into the parent turn through their chat spans
            end = db.ns_to_iso(s["end"]) if s["end"] is not None else None
            lat = (s["end"] - s["start"]) // 1_000_000 if s["end"] is not None and s["start"] is not None else None
            conn.execute(
                "UPDATE prompts SET started_at=?, ended_at=?, latency_ms=?, status=? WHERE prompt_id=?",
                (start, end, lat, "error" if s["error"] else "done", pid))
            conn.execute(
                "INSERT INTO otel_roots (trace_id, input_tokens, output_tokens) VALUES (?,?,?) "
                "ON CONFLICT(trace_id) DO UPDATE SET input_tokens=excluded.input_tokens, "
                "output_tokens=excluded.output_tokens",
                (s["trace"], _first(a, _IN), _first(a, _OUT)))
            roots[s["trace"]] = pid
        elif op == "chat":
            raw_in = _first(a, _IN) or 0
            cr, cw = _first(a, _CR) or 0, _first(a, _CW) or 0
            nano = _first(a, ("github.copilot.nano_aiu",))
            db.upsert_call(conn, {
                "call_key": f"{s['trace']}:{s['span']}", "prompt_id": pid, "client": client,
                "model": a.get("gen_ai.response.model") or a.get("gen_ai.request.model"),
                "ts": start,
                "duration_ms": (s["end"] - s["start"]) // 1_000_000
                if s["end"] is not None and s["start"] is not None else None,
                # OTel counts cached tokens inside input_tokens; store the disjoint remainder.
                "input_tokens": max(0, raw_in - cr - cw), "output_tokens": _first(a, _OUT) or 0,
                "cache_read_tokens": cr, "cache_write_tokens": cw, "nano_aiu": nano})
        else:
            conn.execute(
                "INSERT INTO tool_calls (call_key, prompt_id) VALUES (?,?) "
                "ON CONFLICT(call_key) DO UPDATE SET prompt_id=excluded.prompt_id",
                (f"{s['trace']}:{s['span']}", pid))
    for pid in touched:
        db.recompute_prompt(conn, pid)
        _crosscheck(conn, pid)


def _crosscheck(conn, pid):
    r = conn.execute(
        "SELECT r.input_tokens, r.output_tokens FROM otel_roots r JOIN prompts p ON p.trace_id=r.trace_id "
        "WHERE p.prompt_id=?", (pid,)).fetchone()
    if not r:
        return
    p = conn.execute("SELECT input_tokens+cache_read_tokens+cache_write_tokens, output_tokens "
                     "FROM prompts WHERE prompt_id=?", (pid,)).fetchone()
    if (r[0] is not None and r[0] != p[0]) or (r[1] is not None and r[1] != p[1]):
        db.log_error(f"copilot root/chat mismatch {pid}: root in/out={r[0]}/{r[1]} chat in/out={p[0]}/{p[1]}")


def import_file(conn, path, max_bytes=None, budget_s=None):
    client = client_for(path)
    counters = {"unknown": 0, "bad": 0}
    n = db.ingest_file(conn, path, "copilot_otel",
                       lambda lines: _handle_batch(conn, lines, client, counters),
                       max_bytes=max_bytes, budget_s=budget_s)
    if counters["unknown"] or counters["bad"]:
        db.log_error(f"copilot import {path}: {counters['unknown']} unknown shapes, "
                     f"{counters['bad']} unparsable lines")
    return n


def default_files():
    files = []
    env = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH")
    if env:
        files.append(os.path.expanduser(env))
    files += sorted(glob.glob(os.path.join(db.otel_dir(), "*.jsonl")))
    return list(dict.fromkeys(f for f in files if os.path.isfile(f)))


def import_all(conn, files=None, max_bytes=None, budget_s=None, retention=False):
    deadline = time.monotonic() + budget_s if budget_s else None
    total = 0
    for path in files or default_files():
        left = max(0.001, deadline - time.monotonic()) if deadline else None
        try:
            total += import_file(conn, path, max_bytes, left)
            if retention:
                _truncate_if_large(conn, path)
        except Exception as e:  # one unreadable file must not block the others
            db.log_error(f"copilot import {path}: {e!r}")
        if deadline and time.monotonic() > deadline:
            break
    with db.tx(conn):
        join_pending(conn)
    return total


def _truncate_if_large(conn, path):
    st = os.stat(path)
    row = conn.execute("SELECT byte_offset FROM file_offsets WHERE path=?", (path,)).fetchone()
    if st.st_size > RETENTION_BYTES and row and row[0] == st.st_size:
        with open(path, "r+b") as f:
            f.truncate(0)
        st = os.stat(path)
        with db.tx(conn):
            db._set_offset(conn, path, "copilot_otel", 0, st)


# ---- hook <-> trace join -----------------------------------------------

def join_pending(conn):
    now = db.now_iso()
    stale = db.add_seconds(now, -PENDING_TIMEOUT_S)
    pend = conn.execute("SELECT prompt_id, session_id, started_at, prompt_preview, prompt_sha256 FROM prompts "
                        "WHERE prompt_id LIKE 'cph:%' AND status='open' ORDER BY started_at").fetchall()
    windows = {p[0]: (db.add_seconds(p[2], -JOIN_SLACK_S), db.add_seconds(p[2], JOIN_WINDOW_S)) for p in pend}
    claimed = set()

    def candidates(pid):
        lo, hi = windows[pid]
        return conn.execute(
            "SELECT prompt_id, session_id FROM prompts WHERE prompt_id LIKE 'cp:%' AND prompt_sha256 IS NULL "
            "AND started_at BETWEEN ? AND ? ORDER BY started_at", (lo, hi)).fetchall()

    for hook_id, hsid, hstart, prev, sha in pend:
        cands = [c for c in candidates(hook_id) if c[0] not in claimed]
        exact = [c for c in cands if c[1] == hsid]
        target = exact[0] if exact else None
        if target is None and len(cands) == 1:
            # Time-only fallback, only when nothing else could claim this trace.
            c = cands[0]
            rivals = [o for o in pend if o[0] != hook_id and (
                o[1] == c[1] or windows[o[0]][0] <= conn.execute(
                    "SELECT started_at FROM prompts WHERE prompt_id=?", (c[0],)).fetchone()[0] <= windows[o[0]][1])]
            if not rivals:
                target = c
        if target is None:
            continue
        claimed.add(target[0])
        conn.execute("UPDATE prompts SET prompt_preview=?, prompt_sha256=? WHERE prompt_id=?",
                     (prev, sha, target[0]))
        if target[1].startswith("cp-trace:"):
            conn.execute("UPDATE prompts SET session_id=? WHERE prompt_id=?", (hsid, target[0]))
        conn.execute("DELETE FROM prompts WHERE prompt_id=?", (hook_id,))
    conn.execute("UPDATE prompts SET status='aborted' WHERE prompt_id LIKE 'cph:%' AND status='open' "
                 "AND started_at<?", (stale,))


# ---- hooks --------------------------------------------------------------

HOOK_IMPORT_BYTES = 2 * 1024 * 1024
HOOK_IMPORT_BUDGET_S = 0.3


def _infer_event(p, explicit):
    ev = explicit or p.get("hookEventName") or p.get("hook_event_name") or p.get("event")
    if ev:
        return str(ev)
    if "prompt" in p:
        return "userPromptSubmitted"
    return "sessionEnd" if "reason" in p else "agentStop"


def handle_hook(payload: dict, event: str | None = None):
    if not isinstance(payload, dict) or not payload.get("sessionId"):
        return
    sid = str(payload["sessionId"])
    ev = _infer_event(payload, event)
    conn = db.connect()
    try:
        raw_ts = payload.get("timestamp")
        ts = db.norm_ts(raw_ts) or db.now_iso()
        if ev == "userPromptSubmitted":
            text = payload.get("prompt") if isinstance(payload.get("prompt"), str) else None
            cwd = payload.get("cwd")
            from .claude import git_info
            repo, branch = git_info(cwd)
            with db.tx(conn):
                db.upsert_session(conn, sid, CLI, ts, cwd, repo, branch)
                conn.execute(
                    """INSERT INTO prompts (prompt_id, session_id, client, prompt_preview, prompt_sha256,
                                            started_at, status, cost_basis)
                       VALUES (?,?,?,?,?,?,'open','copilot_aiu') ON CONFLICT(prompt_id) DO NOTHING""",
                    (f"cph:{sid}:{raw_ts if raw_ts is not None else ts}", sid, CLI,
                     preview(text, db.capture_mode()), sha256(text), ts))
        elif ev == "sessionEnd":
            with db.tx(conn):
                conn.execute("UPDATE sessions SET ended_at=?, end_reason=? WHERE session_id=?",
                             (db.now_iso(), payload.get("reason"), sid))
        import_all(conn, max_bytes=HOOK_IMPORT_BYTES, budget_s=HOOK_IMPORT_BUDGET_S)
    finally:
        conn.close()

import io
import json
import os
import sys

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from prompt_telemetry import claude, copilot, db, hook, redact, report
from prompt_telemetry.prices import lookup, price_call


class Patcher:
    def __init__(self):
        self.stack = []

    def setattr(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.stack.append(p)

    def setenv(self, k, v):
        p = mock.patch.dict(os.environ, {k: v})
        p.start()
        self.stack.append(p)

    def stop(self):
        for p in reversed(self.stack):
            p.stop()


def approx(a, b):
    assert abs(a - b) <= 1e-9 * max(1, abs(b)), (a, b)
    return True


def case(fn):
    """Run a (conn, tmp_path, monkeypatch) test function as a unittest method with a fresh home."""
    def method(self):
        with tempfile.TemporaryDirectory() as d:
            mp = Patcher()
            mp.setenv("PROMPT_TELEMETRY_HOME", str(Path(d) / "home"))
            os.environ.pop("COPILOT_OTEL_FILE_EXPORTER_PATH", None)
            conn = db.connect()
            try:
                fn(conn, Path(d), mp)
            finally:
                conn.close()
                mp.stop()
    method.__name__ = fn.__name__
    return method


def run_hook(monkeypatch, argv, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)))
    return hook.run(argv)


def wjsonl(path, rows, mode="w"):
    with open(path, mode) as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


# ---- prices / redaction -------------------------------------------------

@case
def price_lookup_prefix_and_tiers(conn, tmp_path, monkeypatch):
    assert lookup(conn, "claude-opus-5-5-20261101", "2026-12-01")[1] == "claude-opus-5-5"
    assert lookup(conn, "claude-opus-5-20261101", "2026-12-01")[1] == "claude-opus-5"
    assert lookup(conn, "claude-opus-50", "2026-12-01") is None
    assert lookup(conn, "claude-sonnet-4.5", "2026-12-01")[1] == "claude-sonnet-4"
    assert lookup(conn, "claude-haiku-5-5", "2026-12-01", 50_000)[3] == 0
    assert lookup(conn, "claude-haiku-5-5", "2026-12-01", 150_000)[3] == 100001
    cost, _ = price_call(conn, "claude-opus-5", "2026-12-01", 1_000_000, 0, 0, 0)
    assert approx(cost, 5.0)
    assert price_call(conn, "mystery-model", None, 1, 1, 0, 0)[0] is None


def redaction(self=None):
    s = "key sk-ant-abc123456789xyz ghp_" + "a" * 30 + " password=hunter2 postgres://u:p@h/db Bearer abc.def"
    out = redact.redact(s)
    for bad in ("sk-ant", "ghp_", "hunter2", "u:p@", "abc.def"):
        assert bad not in out
    assert len(redact.preview("x" * 500)) == 200
    assert redact.preview("hi", "off") is None


# ---- Claude -------------------------------------------------------------

def claude_entry(mid, req, ts, out, tools=(), sid="s1", **extra):
    return {"type": "assistant", "sessionId": sid, "requestId": req, "timestamp": ts,
            "message": {"id": mid, "model": "claude-opus-5",
                        "content": [{"type": "tool_use", "id": t} for t in tools],
                        "usage": {"input_tokens": 100, "output_tokens": out,
                                  "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 50}}, **extra}


@case
def claude_flow(conn, tmp_path, monkeypatch):
    tp = tmp_path / "t.jsonl"
    base = {"session_id": "s1", "transcript_path": str(tp), "cwd": str(tmp_path)}
    run_hook(monkeypatch, ["claude"], {**base, "hook_event_name": "UserPromptSubmit", "prompt_id": "p1",
                                       "prompt": "first token=abc123"})
    run_hook(monkeypatch, ["claude"], {**base, "hook_event_name": "UserPromptSubmit", "prompt_id": "p2",
                                       "prompt": "second"})
    assert conn.execute("SELECT status FROM prompts WHERE prompt_id='p1'").fetchone()[0] == "aborted"
    assert "abc123" not in conn.execute("SELECT prompt_preview FROM prompts WHERE prompt_id='p1'").fetchone()[0]
    t0 = db.now_iso()
    wjsonl(tp, [
        claude_entry("m1", "r1", t0, 10, ["t1"]),
        claude_entry("m1", "r1", t0, 20, ["t2"]),          # streamed duplicate: last usage wins
        {"type": "user"}, "{bad json",
    ])
    with open(tp, "a") as f:
        f.write(json.dumps(claude_entry("m2", "r2", t0, 5)))  # half-written line, no newline
    run_hook(monkeypatch, ["claude"], {**base, "hook_event_name": "Stop", "prompt_id": "p2"})
    row = conn.execute("SELECT status, llm_call_count, tool_call_count, input_tokens, output_tokens, "
                       "cache_read_tokens, cache_write_tokens, cost_usd FROM prompts WHERE prompt_id='p2'").fetchone()
    assert row[:7] == ("done", 1, 2, 100, 20, 1000, 50)
    assert approx(row[7], (100 * 5 + 20 * 25 + 50 * 6.25 + 1000 * 0.5) / 1e6)
    # The partial line is picked up once completed, and re-running adds nothing twice.
    with open(tp, "a") as f:
        f.write("\n")
    run_hook(monkeypatch, ["claude"], {**base, "hook_event_name": "Stop", "prompt_id": "p2"})
    assert conn.execute("SELECT llm_call_count FROM prompts WHERE prompt_id='p2'").fetchone()[0] == 2
    # Subagent credited to parent prompt.
    sp = tmp_path / "sub.jsonl"
    wjsonl(sp, [claude_entry("m3", "r3", t0, 7, sid="s1", isSidechain=True)])
    run_hook(monkeypatch, ["claude"], {**base, "hook_event_name": "SubagentStop", "prompt_id": "p2",
                                       "agent_transcript_path": str(sp)})
    assert conn.execute("SELECT SUM(is_subagent) FROM llm_calls WHERE prompt_id='p2'").fetchone()[0] == 1
    run_hook(monkeypatch, ["claude"], {**base, "hook_event_name": "SessionEnd", "reason": "exit"})
    assert conn.execute("SELECT ended_at FROM sessions WHERE session_id='s1'").fetchone()[0]

    def totals():
        return conn.execute("SELECT SUM(input_tokens),SUM(output_tokens),SUM(cost_usd),SUM(llm_call_count) "
                            "FROM prompts").fetchone()
    before = totals()
    report.rescan(conn)
    report.rescan(conn)
    assert totals() == before


@case
def claude_hook_guard_drops_copilot_payload(conn, tmp_path, monkeypatch):
    run_hook(monkeypatch, ["claude"], {"sessionId": "x", "hook_event_name": "UserPromptSubmit",
                                       "transcript_path": "/x", "prompt": "hi"})
    run_hook(monkeypatch, ["claude"], {"session_id": "x", "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0


@case
def hooks_never_fail(conn, tmp_path, monkeypatch):
    out = io.StringIO()
    with mock.patch.object(sys, "stdout", out):
        for payload in ["not json", "[]", "{}", '{"session_id": 5}', ""]:
            for client in ("claude", "copilot"):
                assert run_hook(monkeypatch, [client], payload) == 0
    assert out.getvalue() == ""


# ---- Copilot ------------------------------------------------------------

def otlp_span(trace, span, name, op, parent="", start=0, end=0, **attrs):
    attrs = {"gen_ai.operation.name": op, **attrs}
    def val(v):
        return {"intValue": str(v)} if isinstance(v, int) else {"stringValue": v}
    return {"traceId": trace, "spanId": span, "parentSpanId": parent, "name": name,
            "startTimeUnixNano": str(start), "endTimeUnixNano": str(end),
            "attributes": [{"key": k, "value": val(v)} for k, v in attrs.items()]}


def turn(trace, conv, t0_ns):
    """One turn: 2 chat calls + 1 tool, root repeating the totals."""
    return [
        otlp_span(trace, "c1", "chat m", "chat", "r", t0_ns + 1, t0_ns + 9, **{
            "gen_ai.conversation.id": conv, "gen_ai.response.model": "claude-sonnet-4.5",
            "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 50,
            "gen_ai.usage.cache_read.input_tokens": 600, "github.copilot.nano_aiu": 2_000_000_000}),
        otlp_span(trace, "t1", "execute_tool x", "execute_tool", "r", t0_ns + 10, t0_ns + 11),
        otlp_span(trace, "c2", "chat m", "chat", "r", t0_ns + 12, t0_ns + 19, **{
            "gen_ai.conversation.id": conv, "gen_ai.response.model": "claude-sonnet-4.5",
            "gen_ai.usage.input_tokens": 1200, "gen_ai.usage.output_tokens": 70,
            "gen_ai.usage.cache_read.input_tokens": 1000, "github.copilot.nano_aiu": 3_000_000_000}),
        otlp_span(trace, "r", "invoke_agent a", "invoke_agent", "", t0_ns, t0_ns + 2_000_000_000, **{
            "gen_ai.conversation.id": conv, "gen_ai.usage.input_tokens": 2200,
            "gen_ai.usage.output_tokens": 120}),
    ]


def as_otlp(spans):
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


def as_flat(s):
    return {"spanContext": {"traceId": s["traceId"], "spanId": s["spanId"]},
            "parentSpanId": s["parentSpanId"] or None, "name": s["name"],
            "startTime": [int(s["startTimeUnixNano"]) // 10**9, int(s["startTimeUnixNano"]) % 10**9],
            "endTime": [int(s["endTimeUnixNano"]) // 10**9, int(s["endTimeUnixNano"]) % 10**9],
            "attributes": {a["key"]: next(iter(a["value"].values())) for a in s["attributes"]}}


@case
def copilot_import_shapes_and_idempotency(conn, tmp_path, monkeypatch):
    t0 = 1_800_000_000 * 10**9
    f = tmp_path / "copilot-cli.jsonl"
    # Child spans flushed before the root, in a separate line; second trace uses the flat shape.
    spans = turn("a" * 32, "conv1", t0)
    wjsonl(f, [as_otlp(spans[:3]), as_otlp(spans[3:])] + [as_flat(s) for s in turn("b" * 32, "conv1", t0 + 10**11)]
           + [{"resourceMetrics": []}, "{broken"])
    copilot.import_all(conn, [str(f)])
    rows = conn.execute("SELECT prompt_id, status, llm_call_count, tool_call_count, input_tokens, output_tokens, "
                        "cache_read_tokens, ai_units, cost_usd, latency_ms, session_id, model FROM prompts "
                        "ORDER BY started_at").fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r[1:] == ("done", 2, 1, 600, 120, 1600, 5.0, r[8], 2000, "conv1", "claude-sonnet-4.5")
    assert rows[0][8] is not None
    assert not os.path.exists(db.error_log_path()) or "mismatch" not in Path(db.error_log_path()).read_text()

    def snapshot():
        return conn.execute("SELECT COUNT(*), SUM(input_tokens), SUM(ai_units), SUM(tool_call_count) FROM prompts").fetchone()
    snap = snapshot()
    copilot.import_all(conn, [str(f)])
    report.rescan(conn)
    report.rescan(conn)
    assert snapshot() == snap
    # Rotation: a smaller replacement file restarts from 0 and stays idempotent.
    wjsonl(f, [as_otlp(spans)])
    copilot.import_all(conn, [str(f)])
    assert snapshot() == snap


@case
def copilot_unknown_model_and_missing_root(conn, tmp_path, monkeypatch):
    f = tmp_path / "vscode.jsonl"
    s = otlp_span("c" * 32, "x1", "chat z", "chat", "r", 10**18, 10**18 + 5, **{
        "gen_ai.response.model": "mystery", "gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 1,
        "github.copilot.nano_aiu": 1_000_000_000})
    wjsonl(f, [as_otlp([s])])
    copilot.import_all(conn, [str(f)])
    r = conn.execute("SELECT status, client, cost_usd, ai_units, session_id FROM prompts").fetchone()
    assert r == ("open", "copilot-vscode", None, 1.0, "cp-trace:" + "c" * 32)


def hook_payload(sid, ts_ms, prompt=None):
    p = {"sessionId": sid, "timestamp": ts_ms, "cwd": "/tmp"}
    if prompt:
        p["prompt"] = prompt
    return p


@case
def copilot_hook_join_two_sessions(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_OTEL_FILE_EXPORTER_PATH", str(tmp_path / "copilot-cli.jsonl"))
    t0 = 1_800_000_000 * 10**9
    run_hook(monkeypatch, ["copilot"], hook_payload("A", t0 // 10**6, "prompt for A token=sekrit"))
    run_hook(monkeypatch, ["copilot"], hook_payload("B", t0 // 10**6 + 1000, "prompt for B"))
    wjsonl(tmp_path / "copilot-cli.jsonl",
           [as_otlp(turn("b" * 32, "B", t0 + 1_000_000_000 + 10**6)), as_otlp(turn("a" * 32, "A", t0 + 10**6))])
    run_hook(monkeypatch, ["copilot", "agentStop"], {"sessionId": "A", "timestamp": t0 // 10**6 + 5000})
    got = dict(conn.execute("SELECT prompt_id, prompt_preview FROM prompts").fetchall())
    assert got == {"cp:" + "a" * 32: "prompt for A [REDACTED]", "cp:" + "b" * 32: "prompt for B"}


@case
def copilot_join_never_guesses_when_ambiguous(conn, tmp_path, monkeypatch):
    t0 = 1_800_000_000 * 10**9
    # Conversation ids differ from hook sessionIds and two hooks compete for the traces.
    for sid, off in (("A", 0), ("B", 500)):
        run_hook(monkeypatch, ["copilot", "userPromptSubmitted"], hook_payload(sid, t0 // 10**6 + off, "p " + sid))
    f = tmp_path / "copilot-cli.jsonl"
    wjsonl(f, [as_otlp(turn("a" * 32, "X", t0 + 10**6)), as_otlp(turn("b" * 32, "Y", t0 + 600 * 10**6))])
    copilot.import_all(conn, [str(f)])
    assert conn.execute("SELECT COUNT(*) FROM prompts WHERE prompt_preview IS NOT NULL AND prompt_id LIKE 'cp:%'"
                        ).fetchone()[0] == 0
    assert conn.execute("SELECT SUM(output_tokens) FROM prompts").fetchone()[0] == 240


@case
def copilot_unmatched_hook_aborts(conn, tmp_path, monkeypatch):
    old = db.now_iso()
    run_hook(monkeypatch, ["copilot"], hook_payload("Z", 1_000_000_000_000, "old"))  # year 2001
    copilot.import_all(conn, [])
    assert conn.execute("SELECT status, input_tokens FROM prompts WHERE prompt_id LIKE 'cph:%'").fetchone() == ("aborted", 0)


class PromptTelemetryTests(unittest.TestCase):
    pass


for _fn in (price_lookup_prefix_and_tiers, redaction, claude_flow, claude_hook_guard_drops_copilot_payload,
            hooks_never_fail, copilot_import_shapes_and_idempotency, copilot_unknown_model_and_missing_root,
            copilot_hook_join_two_sessions, copilot_join_never_guesses_when_ambiguous,
            copilot_unmatched_hook_aborts):
    setattr(PromptTelemetryTests, "test_" + _fn.__name__, _fn if _fn is redaction else (lambda self, f=_fn: f(self)))

if __name__ == "__main__":
    unittest.main()

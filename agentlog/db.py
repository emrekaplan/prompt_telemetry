import contextlib
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from .prices import load_prices, price_call

os.umask(0o077)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id  TEXT PRIMARY KEY,
  client      TEXT NOT NULL,
  cwd TEXT, repo TEXT, git_branch TEXT,
  started_at  TEXT NOT NULL,
  ended_at    TEXT, end_reason TEXT
);
CREATE TABLE IF NOT EXISTS prompts (
  prompt_id      TEXT PRIMARY KEY,
  session_id     TEXT NOT NULL REFERENCES sessions(session_id),
  client         TEXT NOT NULL,
  trace_id       TEXT,
  kind           TEXT NOT NULL DEFAULT 'user',
  prompt_preview TEXT,
  prompt_sha256  TEXT,
  started_at     TEXT NOT NULL,
  ended_at       TEXT,
  latency_ms     INTEGER,
  status         TEXT NOT NULL DEFAULT 'open',
  model          TEXT,
  llm_call_count INTEGER NOT NULL DEFAULT 0,
  tool_call_count INTEGER NOT NULL DEFAULT 0,
  input_tokens   INTEGER NOT NULL DEFAULT 0,
  output_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd       REAL,
  ai_units       REAL,
  cost_basis     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_prompts_session ON prompts(session_id, started_at);
CREATE INDEX IF NOT EXISTS ix_prompts_started ON prompts(started_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_prompts_trace ON prompts(trace_id) WHERE trace_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS prices (
  price_id INTEGER PRIMARY KEY, model_prefix TEXT NOT NULL, effective_from TEXT NOT NULL,
  min_prompt_tokens INTEGER NOT NULL DEFAULT 0,
  in_per_m REAL NOT NULL, out_per_m REAL NOT NULL,
  cache_write_5m_per_m REAL NOT NULL, cache_write_1h_per_m REAL NOT NULL, cache_read_per_m REAL NOT NULL,
  UNIQUE (model_prefix, effective_from, min_prompt_tokens)
);
CREATE TABLE IF NOT EXISTS llm_calls (
  call_key   TEXT PRIMARY KEY,
  prompt_id  TEXT NOT NULL REFERENCES prompts(prompt_id),
  client     TEXT NOT NULL,
  model      TEXT, ts TEXT, duration_ms INTEGER,
  is_subagent INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
  tool_uses  INTEGER NOT NULL DEFAULT 0,
  nano_aiu   INTEGER,
  cost_usd   REAL, price_id INTEGER REFERENCES prices(price_id)
);
CREATE INDEX IF NOT EXISTS ix_calls_prompt ON llm_calls(prompt_id);
CREATE TABLE IF NOT EXISTS file_offsets (
  path TEXT PRIMARY KEY, kind TEXT NOT NULL,
  byte_offset INTEGER NOT NULL DEFAULT 0, inode INTEGER, size INTEGER, updated_at TEXT
);
-- Helper tables (not in the plan's five): dedupe tool calls and keep root-span totals for the cross-check.
CREATE TABLE IF NOT EXISTS tool_calls (
  call_key TEXT PRIMARY KEY, prompt_id TEXT NOT NULL REFERENCES prompts(prompt_id)
);
CREATE INDEX IF NOT EXISTS ix_tools_prompt ON tool_calls(prompt_id);
CREATE TABLE IF NOT EXISTS otel_roots (
  trace_id TEXT PRIMARY KEY, input_tokens INTEGER, output_tokens INTEGER
);
"""


def home() -> str:
    return os.environ.get("AGENTLOG_HOME") or os.path.expanduser("~/.local/share/agentlog")


def db_path() -> str:
    return os.path.join(home(), "agentlog.db")


def otel_dir() -> str:
    return os.path.join(home(), "otel")


def error_log_path() -> str:
    return os.path.join(home(), "errors.log")


def ensure_dirs():
    for d in (home(), otel_dir()):
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)


def log_error(msg: str):
    try:
        ensure_dirs()
        with open(error_log_path(), "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {msg}\n")
        os.chmod(error_log_path(), 0o600)
    except Exception:
        pass


def secure_files():
    base = db_path()
    for p in (base, base + "-wal", base + "-shm"):
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def capture_mode() -> str:
    mode = os.environ.get("AGENTLOG_CAPTURE")
    if not mode:
        try:
            with open(os.path.join(home(), "config.json"), encoding="utf-8") as f:
                mode = json.load(f).get("capture")
        except Exception:
            mode = None
    return mode if mode in ("off", "preview") else "preview"


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    load_prices(conn)


def connect(create: bool = True) -> sqlite3.Connection:
    path = db_path()
    fresh = not os.path.exists(path)
    if fresh:
        if not create:
            raise FileNotFoundError(path)
        ensure_dirs()
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if fresh:
        init_db(conn)
    secure_files()
    return conn


@contextlib.contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# ---- time helpers -------------------------------------------------------

def ns_to_iso(ns: int) -> str:
    sec, rem = divmod(int(ns), 1_000_000_000)
    dt = datetime.fromtimestamp(sec, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{rem // 1_000_000:03d}Z"


def now_iso() -> str:
    return ns_to_iso(time.time_ns())


def to_ns(v) -> int | None:
    """Accepts OTLP nano strings, [sec, nanos] pairs, epoch s/ms/us/ns numbers, and ISO strings."""
    if v is None or v == "":
        return None
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return int(v[0]) * 1_000_000_000 + int(v[1])
    if isinstance(v, str):
        s = v.strip()
        try:
            v = float(s) if ("." in s or "e" in s.lower()) else int(s)
        except ValueError:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1e9)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    a = abs(v)
    if a >= 1e17:
        return int(v)
    if a >= 1e14:
        return int(v * 1_000)
    if a >= 1e11:
        return int(v * 1_000_000)
    return int(v * 1_000_000_000)


def norm_ts(v) -> str | None:
    ns = to_ns(v)
    return ns_to_iso(ns) if ns is not None else None


def add_seconds(iso: str, seconds: float) -> str:
    ns = to_ns(iso)
    return ns_to_iso(ns + int(seconds * 1e9))


def ms_between(a: str | None, b: str | None) -> int | None:
    na, nb = to_ns(a), to_ns(b)
    if na is None or nb is None:
        return None
    return max(0, (nb - na) // 1_000_000)


# ---- shared writes ------------------------------------------------------

def upsert_session(conn, session_id, client, started_at=None, cwd=None, repo=None, branch=None):
    conn.execute(
        """INSERT INTO sessions (session_id, client, cwd, repo, git_branch, started_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(session_id) DO UPDATE SET
             cwd=COALESCE(excluded.cwd, cwd), repo=COALESCE(excluded.repo, repo),
             git_branch=COALESCE(excluded.git_branch, git_branch)""",
        (session_id, client, cwd, repo, branch, started_at or now_iso()),
    )


CALL_COLS = ("call_key", "prompt_id", "client", "model", "ts", "duration_ms", "is_subagent",
             "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
             "cache_write_1h_tokens", "tool_uses", "nano_aiu", "cost_usd", "price_id")
_UPSERT_CALL = (
    f"INSERT INTO llm_calls ({', '.join(CALL_COLS)}) VALUES ({', '.join('?' * len(CALL_COLS))}) "
    "ON CONFLICT(call_key) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in CALL_COLS if c != "call_key")
)


def upsert_call(conn, d: dict):
    d = {"duration_ms": None, "is_subagent": 0, "input_tokens": 0, "output_tokens": 0,
         "cache_read_tokens": 0, "cache_write_tokens": 0, "cache_write_1h_tokens": 0,
         "tool_uses": 0, "nano_aiu": None, "model": None, "ts": None, **d}
    d["cost_usd"], d["price_id"] = price_call(
        conn, d["model"], d["ts"], d["input_tokens"], d["output_tokens"],
        d["cache_read_tokens"], d["cache_write_tokens"], d["cache_write_1h_tokens"])
    conn.execute(_UPSERT_CALL, tuple(d[c] for c in CALL_COLS))


def reprice_all(conn):
    rows = conn.execute(
        "SELECT call_key, model, ts, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, cache_write_1h_tokens FROM llm_calls").fetchall()
    for key, *args in rows:
        cost, pid = price_call(conn, *args)
        conn.execute("UPDATE llm_calls SET cost_usd=?, price_id=? WHERE call_key=?", (cost, pid, key))


def recompute_prompt(conn, prompt_id: str):
    """Prompt totals are always derived from llm_calls / tool_calls."""
    n, i, o, cr, cw, unpriced, usd, ncount, aiu = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                  COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_write_tokens),0),
                  COALESCE(SUM(cost_usd IS NULL),0), COALESCE(SUM(cost_usd),0.0),
                  COUNT(nano_aiu), COALESCE(SUM(nano_aiu),0)
           FROM llm_calls WHERE prompt_id=?""", (prompt_id,)).fetchone()
    tools = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE prompt_id=?", (prompt_id,)).fetchone()[0]
    top = conn.execute(
        """SELECT model FROM llm_calls WHERE prompt_id=? AND model IS NOT NULL
           GROUP BY model ORDER BY SUM(output_tokens) DESC LIMIT 1""", (prompt_id,)).fetchone()
    basis = conn.execute("SELECT cost_basis FROM prompts WHERE prompt_id=?", (prompt_id,)).fetchone()
    if basis is None:
        return
    ai_units = (aiu / 1e9) if (basis[0] == "copilot_aiu" and ncount) else None
    conn.execute(
        """UPDATE prompts SET llm_call_count=?, tool_call_count=?, input_tokens=?, output_tokens=?,
             cache_read_tokens=?, cache_write_tokens=?, cost_usd=?, ai_units=?, model=? WHERE prompt_id=?""",
        (n, tools, i, o, cr, cw, None if unpriced else usd, ai_units, top[0] if top else None, prompt_id))


# ---- incremental file reads --------------------------------------------

def ingest_file(conn, path: str, kind: str, handler, max_bytes: int | None = None,
                budget_s: float | None = None, batch_lines: int = 500) -> int:
    """Feed new complete lines to handler(lines); the offset advances in the same transaction.

    Resets to offset 0 if the file shrank or its inode changed. Returns lines processed.
    """
    deadline = time.monotonic() + budget_s if budget_s else None
    st = os.stat(path)
    row = conn.execute("SELECT byte_offset, inode, size FROM file_offsets WHERE path=?", (path,)).fetchone()
    offset = 0
    if row and row[1] == st.st_ino and st.st_size >= (row[2] or 0) and st.st_size >= row[0]:
        offset = row[0]
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(max_bytes) if max_bytes else f.read()
    end = data.rfind(b"\n")
    data = data[: end + 1] if end >= 0 else b""
    lines = data.split(b"\n")[:-1] if data else []
    done = 0
    pos = offset
    if not lines:
        with tx(conn):
            _set_offset(conn, path, kind, offset, st)
        return 0
    for start in range(0, len(lines), batch_lines):
        chunk = lines[start:start + batch_lines]
        pos += sum(len(x) + 1 for x in chunk)
        with tx(conn):
            handler([x.decode("utf-8", "replace") for x in chunk])
            _set_offset(conn, path, kind, pos, st)
        done += len(chunk)
        if deadline and time.monotonic() > deadline:
            break
    return done


def _set_offset(conn, path, kind, offset, st):
    conn.execute(
        """INSERT INTO file_offsets (path, kind, byte_offset, inode, size, updated_at) VALUES (?,?,?,?,?,?)
           ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, byte_offset=excluded.byte_offset,
             inode=excluded.inode, size=excluded.size, updated_at=excluded.updated_at""",
        (path, kind, offset, st.st_ino, st.st_size, now_iso()))

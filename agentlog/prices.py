import csv
import os

_TERMINATORS = ("", "-", "@", "[")
_cache: dict[int, list] = {}


def csv_path() -> str:
    return os.path.join(os.path.dirname(__file__), "prices.csv")


def load_prices(conn, path: str | None = None) -> int:
    n = 0
    with open(path or csv_path(), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            conn.execute(
                """INSERT INTO prices (model_prefix, effective_from, min_prompt_tokens, in_per_m, out_per_m,
                       cache_write_5m_per_m, cache_write_1h_per_m, cache_read_per_m)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(model_prefix, effective_from, min_prompt_tokens) DO UPDATE SET
                       in_per_m=excluded.in_per_m, out_per_m=excluded.out_per_m,
                       cache_write_5m_per_m=excluded.cache_write_5m_per_m,
                       cache_write_1h_per_m=excluded.cache_write_1h_per_m,
                       cache_read_per_m=excluded.cache_read_per_m""",
                (r["model_prefix"].strip(), r["effective_from"].strip(), int(r["min_prompt_tokens"]),
                 float(r["in_per_m"]), float(r["out_per_m"]), float(r["cache_write_5m_per_m"]),
                 float(r["cache_write_1h_per_m"]), float(r["cache_read_per_m"])),
            )
            n += 1
    _cache.pop(id(conn), None)
    return n


def _rows(conn):
    rows = _cache.get(id(conn))
    if rows is None:
        rows = conn.execute(
            """SELECT price_id, model_prefix, effective_from, min_prompt_tokens, in_per_m, out_per_m,
                      cache_write_5m_per_m, cache_write_1h_per_m, cache_read_per_m FROM prices"""
        ).fetchall()
        _cache[id(conn)] = rows
    return rows


def lookup(conn, model: str | None, date: str | None, prompt_tokens: int = 0):
    """Longest matching prefix, then latest effective_from <= date, then highest tier reached."""
    if not model:
        return None
    # Copilot reports ids like claude-sonnet-4.5; prices use dashes.
    m = model.lower().replace(".", "-")
    date = (date or "9999")[:10]
    best = None
    for r in _rows(conn):
        prefix = r[1]
        if not m.startswith(prefix) or m[len(prefix):len(prefix) + 1] not in _TERMINATORS:
            continue
        if r[2] > date or r[3] > prompt_tokens:
            continue
        key = (len(prefix), r[2], r[3])
        if best is None or key > best[0]:
            best = (key, r)
    return best[1] if best else None


def price_call(conn, model, ts, inp, out, cr, cw, cw1h=0):
    """Returns (cost_usd, price_id); (None, None) for unknown models."""
    r = lookup(conn, model, ts, inp + cr + cw)
    if r is None:
        return None, None
    _, _, _, _, p_in, p_out, p_cw5, p_cw1, p_cr = r
    cw1h = min(cw1h, cw)
    cost = (inp * p_in + out * p_out + (cw - cw1h) * p_cw5 + cw1h * p_cw1 + cr * p_cr) / 1e6
    return cost, r[0]

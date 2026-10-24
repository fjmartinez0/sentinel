"""
sentinel_db.py -- access layer for research.db. This is the ONLY code the scripts share, and it
knows nothing about what any script does with the data.

  python3 sentinel_db.py --init       create research.db (or upgrade it) and show what's inside
  python3 sentinel_db.py --info       show version, tables, row counts and recent runs (read-only)
  python3 sentinel_db.py --selftest   run the built-in checks on a throwaway database

Rules this file enforces:
  * One file, in your project folder. Keep it OUT of iCloud / Dropbox / OneDrive: sync
    clients can corrupt a SQLite file. Override the location with $RESEARCH_DB or --path.
  * Schema changes are additive migrations in sentinel_schema.sql, applied in order, each in one
    transaction. A database newer than this code is refused, never downgraded.
  * Tables so far: screen_runs, screen_results (raw scanner data), screen_tags (ledger tags), dossier_snapshots (per-ticker detail pages),
    calendar_runs / earnings_events / quote_snapshots / positions_snapshots (the calendar's history),
    price_history (daily closes), escalations and tier1_runs (Tier 1's queue and audit log),
    tier2_reviews (Tier 2's written reviews, with token counts and cost).
  * Writers never block readers (WAL mode), so the calendar can read while the scanner writes.
  * WAL mode creates research.db-wal and research.db-shm next to the file. To back up, use
    the sqlite3 backup API (Phase 8), not a plain file copy while a script is running.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "research.db"
SCHEMA_FILE = HERE / "sentinel_schema.sql"
BUSY_TIMEOUT_SECONDS = 5.0
SYNC_MARKERS = ("Mobile Documents", "iCloud", "Dropbox", "OneDrive", "Google Drive", "GoogleDrive")

# Finviz and FMP sometimes spell the same ticker differently (share classes are the usual
# culprit). Per the FMP lesson, nothing is guessed here: add an entry only after the
# calendar or scanner shows a real mismatch.   {"BRK-B": "BRK.B"}   (example format only)
SYMBOL_OVERRIDES: dict = {}


class SchemaError(RuntimeError):
    pass


# ---------------------------------------------------------------- connection

def db_path(explicit=None) -> Path:
    """Priority: explicit argument, then $RESEARCH_DB, then research.db next to this file."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("RESEARCH_DB")
    return Path(env).expanduser().resolve() if env else DEFAULT_DB


def warn_if_synced(path: Path) -> None:
    hit = next((m for m in SYNC_MARKERS if m.lower() in str(path).lower()), None)
    if hit:
        print(f"WARNING: {path} looks like it is inside a synced folder ('{hit}'). Sync clients "
              "can corrupt SQLite files. Keep research.db outside it (set $RESEARCH_DB).",
              file=sys.stderr)


def connect(path=None, create: bool = True) -> sqlite3.Connection:
    """Open the database. Autocommit mode: transactions are explicit (see transaction())."""
    p = db_path(path)
    if not create and not p.exists():
        raise FileNotFoundError(f"{p} does not exist yet. Run: python3 sentinel_db.py --init")
    p.parent.mkdir(parents=True, exist_ok=True)
    warn_if_synced(p)
    conn = sqlite3.connect(str(p), timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """All-or-nothing write. BEGIN IMMEDIATE takes the write lock up front, so two scripts
    writing at once queue politely (up to the busy timeout) instead of failing midway."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------- migrations

_MIGRATION_HEADER = re.compile(r"^--\s*migration:\s*(\d+)\s*$", re.MULTILINE)
_FORBIDDEN = re.compile(r"^\s*(BEGIN|COMMIT|ROLLBACK|END)\b", re.IGNORECASE | re.MULTILINE)


def load_migrations(schema_text=None) -> list:
    """Parse sentinel_schema.sql into [(version, sql), ...], checking versions run 1, 2, 3... with no gaps."""
    text = schema_text if schema_text is not None else SCHEMA_FILE.read_text(encoding="utf-8")
    parts = _MIGRATION_HEADER.split(text)          # [preamble, "1", body1, "2", body2, ...]
    migrations = [(int(parts[i]), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]
    if not migrations:
        raise SchemaError("sentinel_schema.sql has no '-- migration: N' sections")
    if [v for v, _ in migrations] != list(range(1, len(migrations) + 1)):
        raise SchemaError("migration numbers must run 1, 2, 3... in order with no gaps: "
                          f"found {[v for v, _ in migrations]}")
    for v, body in migrations:
        if _FORBIDDEN.search(body):
            raise SchemaError(f"migration {v} contains BEGIN/COMMIT/ROLLBACK; sentinel_db.py manages the transaction")
    return migrations


def _statements(sql: str):
    """Split a SQL script into single statements. sqlite3.complete_statement understands
    strings, comments and trigger bodies, so we never split on a semicolon that isn't the end."""
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt:
                yield stmt
            buf = ""
    tail = "\n".join(l for l in buf.splitlines() if l.strip() and not l.strip().startswith("--"))
    if tail:
        raise SchemaError(f"unterminated SQL statement (missing semicolon?): {tail[:80]!r}")


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def init_schema(conn: sqlite3.Connection, schema_text=None) -> tuple:
    """Bring the database up to the latest migration. Returns (version_before, version_after).
    Safe to run repeatedly, and safe if two scripts start at the same moment."""
    migrations = load_migrations(schema_text)
    latest = migrations[-1][0]
    before = current_version(conn)
    if before > latest:
        raise SchemaError(f"database is at version {before} but this sentinel_db.py only knows up to {latest}. "
                          "Use the newer sentinel_db.py; downgrades are refused on purpose.")
    for version, body in migrations:
        with transaction(conn):
            if current_version(conn) >= version:      # re-check under the write lock
                continue
            for stmt in _statements(body):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {int(version)}")
    return before, current_version(conn)


# ------------------------------------------------------------------- helpers

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_symbol(finviz_symbol: str) -> str:
    s = str(finviz_symbol).strip().upper()
    return SYMBOL_OVERRIDES.get(s, s)


def _json_safe(v):
    """Make a scraped/pandas value JSON-safe: NaN/inf/NaT -> null, numpy scalars -> Python."""
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if hasattr(v, "item") and callable(v.item) and not isinstance(v, (str, bytes)):
        try:
            return _json_safe(v.item())
        except Exception:                              # noqa: BLE001
            pass
    try:
        if v != v:                                     # NaN / NaT compare unequal to themselves
            return None
    except Exception:                                  # noqa: BLE001
        pass
    if isinstance(v, float) and math.isinf(v):
        return None
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def insert_screen_run(conn, *, source, results, filters=None, views_ok=None,
                      csv_path=None, notes=None, run_at=None, symbol_key="Ticker") -> int:
    """Store one scanner run and all its hits, atomically. Returns the new run_id.

    results: list of dicts, one per hit (e.g. DataFrame rows via .to_dict("records")), each
             containing the Finviz symbol under `symbol_key`. Every other column is stored as-is.
    """
    rows, seen = [], set()
    for r in results:
        raw = r.get(symbol_key)
        if raw is None or not str(raw).strip():
            raise ValueError(f"a result has no '{symbol_key}' value: {str(r)[:120]}")
        ticker = canonical_symbol(raw)
        if ticker in seen:
            raise ValueError(f"duplicate ticker in one run: {ticker}")
        seen.add(ticker)
        rows.append((ticker, str(raw).strip(), json.dumps(_json_safe(r), ensure_ascii=False)))

    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO screen_runs (run_at, source, filters, views_ok, n_hits, csv_path, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_at or utc_now(), source, filters,
             json.dumps(views_ok) if views_ok is not None else None, len(rows), csv_path, notes))
        run_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO screen_results (run_id, ticker, finviz_symbol, row_json) VALUES (?, ?, ?, ?)",
            [(run_id, *row) for row in rows])
    return run_id


def get_run(conn, run_id=None):
    """One run with its results parsed back into dicts. run_id=None means the latest run."""
    if run_id is None:
        head = conn.execute("SELECT * FROM screen_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    else:
        head = conn.execute("SELECT * FROM screen_runs WHERE run_id = ?", (run_id,)).fetchone()
    if head is None:
        return None
    run = dict(head)
    run["views_ok"] = json.loads(run["views_ok"]) if run["views_ok"] else None
    run["results"] = [
        {"ticker": r["ticker"], "finviz_symbol": r["finviz_symbol"], "row": json.loads(r["row_json"])}
        for r in conn.execute("SELECT * FROM screen_results WHERE run_id = ? ORDER BY ticker", (head["run_id"],))
    ]
    return run


def list_runs(conn, limit: int = 10) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT run_id, run_at, source, n_hits, csv_path FROM screen_runs ORDER BY run_id DESC LIMIT ?", (limit,))]


def table_counts(conn) -> dict:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def latest_run_id(conn):
    row = conn.execute("SELECT MAX(run_id) FROM screen_runs").fetchone()
    return row[0]


def run_tickers(conn, run_id) -> list:
    return [r[0] for r in conn.execute("SELECT ticker FROM screen_results WHERE run_id = ? ORDER BY ticker", (run_id,))]


def insert_screen_tags(conn, run_id, tags, *, positions_as_of=None, log_as_of=None, tagged_at=None) -> int:
    """Replace the tags for one run, atomically (delete + insert in one transaction).

    tags: list of dicts with 'ticker' and 'tag', optionally 'note' and 'revisit'. Every ticker
    must already be a hit in that run: the database refuses tags for names that are not.
    """
    now = tagged_at or utc_now()
    rows = [(run_id, canonical_symbol(t["ticker"]), t["tag"], t.get("note"), t.get("revisit"),
             positions_as_of, log_as_of, now) for t in tags]
    with transaction(conn):
        conn.execute("DELETE FROM screen_tags WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO screen_tags (run_id, ticker, tag, note, revisit, positions_as_of, log_as_of, tagged_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def get_tags(conn, run_id) -> dict:
    return {r["ticker"]: dict(r) for r in conn.execute(
        "SELECT * FROM screen_tags WHERE run_id = ? ORDER BY ticker", (run_id,))}


def insert_dossier(conn, *, ticker, data, parse_ok, parse_notes=None, source="finviz_quote",
                   run_id=None, reason=None, html_sha1=None, fetched_at=None) -> int:
    """Store one fetched detail page (already parsed into sections). Returns the snapshot_id."""
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO dossier_snapshots (ticker, fetched_at, source, run_id, reason, parse_ok, parse_notes, html_sha1, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (canonical_symbol(ticker), fetched_at or utc_now(), source, run_id, reason, 1 if parse_ok else 0,
             parse_notes, html_sha1, json.dumps(_json_safe(data), ensure_ascii=False)))
    return cur.lastrowid


def _dossier_row(row):
    d = dict(row)
    d["data"] = json.loads(d.pop("data_json"))
    return d


def latest_dossier(conn, ticker, ok_only: bool = True):
    """The newest snapshot for a ticker (by default only ones that parsed properly), or None."""
    sql = "SELECT * FROM dossier_snapshots WHERE ticker = ?" + (" AND parse_ok = 1" if ok_only else "") + \
          " ORDER BY snapshot_id DESC LIMIT 1"
    row = conn.execute(sql, (canonical_symbol(ticker),)).fetchone()
    return _dossier_row(row) if row else None


def recent_dossier(conn, ticker, max_age_days: float, now=None):
    """A good snapshot no older than max_age_days, else None. This is the fetch cache."""
    d = latest_dossier(conn, ticker)
    if d is None:
        return None
    age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(d["fetched_at"])).total_seconds() / 86400
    return d if age <= max_age_days else None


def latest_two_dossiers(conn, ticker):
    """(newest, the one before it), each a parsed snapshot dict or None. Failed parses are skipped."""
    rows = conn.execute("SELECT * FROM dossier_snapshots WHERE ticker = ? AND parse_ok = 1 ORDER BY snapshot_id DESC LIMIT 2",
                        (canonical_symbol(ticker),)).fetchall()
    cur = _dossier_row(rows[0]) if rows else None
    prev = _dossier_row(rows[1]) if len(rows) > 1 else None
    return cur, prev


def insert_calendar_run(conn, *, as_of_date, window_from, window_to, events, quotes, notes=None, run_at=None) -> int:
    """Store one calendar run atomically. events: dicts with ticker, event_date, kind and optional
    time, confirmed, eps_est, eps_act, rev_est, rev_act. quotes: {ticker: {"price", "change_pct"}}."""
    rows, seen = [], set()
    for e in events:
        key = (canonical_symbol(e["ticker"]), e["event_date"])
        if key in seen:
            continue
        seen.add(key)
        rows.append((*key, e["kind"], e.get("time") or None, None if e.get("confirmed") is None else int(bool(e["confirmed"])),
                     e.get("eps_est"), e.get("eps_act"), e.get("rev_est"), e.get("rev_act")))
    qrows = [(canonical_symbol(t), q["price"], q.get("change_pct")) for t, q in quotes.items() if q.get("price") is not None]
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO calendar_runs (run_at, as_of_date, window_from, window_to, n_events, n_quotes, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_at or utc_now(), as_of_date, window_from, window_to, len(rows), len(qrows), notes))
        rid = cur.lastrowid
        conn.executemany(
            "INSERT INTO earnings_events (calendar_run_id, ticker, event_date, kind, fmp_time, fmp_confirmed, eps_est, eps_act, rev_est, rev_act) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [(rid, *r) for r in rows])
        conn.executemany("INSERT INTO quote_snapshots (calendar_run_id, ticker, price, change_pct) VALUES (?, ?, ?, ?)",
                         [(rid, *q) for q in qrows])
    return rid


def latest_calendar_run(conn):
    row = conn.execute("SELECT * FROM calendar_runs ORDER BY calendar_run_id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def latest_calendar_events(conn) -> list:
    """Every earnings event the most recent calendar run saved (tracked names only), oldest first. Lets the
    calendar keep its dates and past results when the earnings source is down (DECISIONS.md 52)."""
    run = latest_calendar_run(conn)
    if not run:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT ticker, event_date, kind, fmp_time, fmp_confirmed, eps_est, eps_act, rev_est, rev_act FROM earnings_events "
        "WHERE calendar_run_id = ? ORDER BY event_date", (run["calendar_run_id"],))]


def calendar_event_dates(conn, calendar_run_id) -> dict:
    """{ticker: [event dates, ascending]} as one calendar run saw them."""
    out = {}
    for r in conn.execute("SELECT ticker, event_date FROM earnings_events WHERE calendar_run_id = ? ORDER BY event_date", (calendar_run_id,)):
        out.setdefault(r["ticker"], []).append(r["event_date"])
    return out


def save_positions_snapshot(conn, data, sha1, as_of=None):
    """Save a copy of positions data unless the newest saved copy has the same fingerprint.
    Returns (snapshot_id, saved) where saved is False when it was a duplicate."""
    last = conn.execute("SELECT snapshot_id, sha1 FROM positions_snapshots ORDER BY snapshot_id DESC LIMIT 1").fetchone()
    if last and last["sha1"] == sha1:
        return last["snapshot_id"], False
    with transaction(conn):
        cur = conn.execute("INSERT INTO positions_snapshots (saved_at, as_of, sha1, data_json) VALUES (?, ?, ?, ?)",
                           (utc_now(), as_of, sha1, json.dumps(_json_safe(data), ensure_ascii=False)))
    return cur.lastrowid, True


def upsert_prices(conn, ticker, rows, source="fmp_eod", fetched_at=None) -> int:
    """Store daily closes. rows: [(YYYY-MM-DD, close)] or [(YYYY-MM-DD, close, change_pct)], where change_pct is
    the day's own change against the previous close when the source reports it. New dates are inserted; a
    changed close or a newly known change_pct is updated. Returns how many rows were new or changed."""
    t, now, changed = canonical_symbol(ticker), fetched_at or utc_now(), 0
    with transaction(conn):
        for row in rows:
            d, c = row[0], float(row[1])
            chg = float(row[2]) if len(row) > 2 and row[2] is not None else None
            old = conn.execute("SELECT close, change_pct FROM price_history WHERE ticker = ? AND price_date = ?", (t, d)).fetchone()
            differs = old is None or abs(old[0] - c) > 1e-9 or (chg is not None and (old[1] is None or abs(old[1] - chg) > 1e-9))
            if differs:
                conn.execute(
                    "INSERT INTO price_history (ticker, price_date, close, source, fetched_at, change_pct) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (ticker, price_date) DO UPDATE SET close = excluded.close, source = excluded.source, "
                    "fetched_at = excluded.fetched_at, change_pct = COALESCE(excluded.change_pct, price_history.change_pct)",
                    (t, d, c, source, now, chg))
                changed += 1
    return changed


def latest_price_date(conn, ticker):
    return conn.execute("SELECT MAX(price_date) FROM price_history WHERE ticker = ?", (canonical_symbol(ticker),)).fetchone()[0]


def price_rows(conn, ticker, since=None) -> list:
    """Full price rows, ascending: dicts with date, close, change_pct (None if the source did not report one) and source."""
    sql, args = "SELECT price_date, close, change_pct, source FROM price_history WHERE ticker = ?", [canonical_symbol(ticker)]
    if since:
        sql, args = sql + " AND price_date >= ?", args + [since]
    return [{"date": r[0], "close": r[1], "change_pct": r[2], "source": r[3]} for r in conn.execute(sql + " ORDER BY price_date", args)]


def latest_price(conn, ticker):
    """The newest stored price row for a ticker, or None."""
    row = conn.execute("SELECT price_date, close, change_pct, source FROM price_history WHERE ticker = ? ORDER BY price_date DESC LIMIT 1",
                       (canonical_symbol(ticker),)).fetchone()
    return {"date": row[0], "close": row[1], "change_pct": row[2], "source": row[3]} if row else None


def price_series(conn, ticker, since=None) -> list:
    """[(date, close)] ascending, optionally from a date onward."""
    sql, args = "SELECT price_date, close FROM price_history WHERE ticker = ?", [canonical_symbol(ticker)]
    if since:
        sql, args = sql + " AND price_date >= ?", args + [since]
    return [(r[0], r[1]) for r in conn.execute(sql + " ORDER BY price_date", args)]


ESCALATION_STATUSES = ("open", "done", "dismissed")


def insert_escalation(conn, *, ticker, funnel, trigger, trigger_ref, reasons, evidence=None, rules_version=None, created_at=None):
    """Queue a name for Tier 2. Returns (escalation_id, created). The same (ticker, trigger, ref) is only
    ever queued once, so re-running Tier 1 cannot pile up duplicates: created is False for a repeat."""
    with transaction(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO escalations (created_at, ticker, funnel, trigger, trigger_ref, reasons_json, evidence_json, rules_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (created_at or utc_now(), canonical_symbol(ticker), funnel, trigger, str(trigger_ref),
             json.dumps(_json_safe(list(reasons)), ensure_ascii=False),
             json.dumps(_json_safe(evidence), ensure_ascii=False) if evidence is not None else None, rules_version))
        if cur.rowcount == 1:
            return cur.lastrowid, True
        row = conn.execute("SELECT escalation_id FROM escalations WHERE ticker = ? AND trigger = ? AND trigger_ref = ?",
                           (canonical_symbol(ticker), trigger, str(trigger_ref))).fetchone()
        return row[0], False


def _escalation_row(r):
    d = dict(r)
    d["reasons"] = json.loads(d.pop("reasons_json"))
    d["evidence"] = json.loads(d.pop("evidence_json")) if d.get("evidence_json") else None
    d.pop("evidence_json", None)
    return d


def list_escalations(conn, status=None) -> list:
    sql, args = "SELECT * FROM escalations", []
    if status:
        sql, args = sql + " WHERE status = ?", [status]
    return [_escalation_row(r) for r in conn.execute(sql + " ORDER BY escalation_id", args)]


def set_escalation_status(conn, escalation_id, status, note=None) -> bool:
    if status not in ESCALATION_STATUSES:
        raise ValueError(f"status must be one of {', '.join(ESCALATION_STATUSES)}")
    with transaction(conn):
        cur = conn.execute("UPDATE escalations SET status = ?, status_at = ?, note = COALESCE(?, note) WHERE escalation_id = ?",
                           (status, utc_now(), note, int(escalation_id)))
    return cur.rowcount == 1


def insert_tier1_run(conn, *, as_of_date, rules_version, n_checked, n_escalated, details, run_at=None) -> int:
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO tier1_runs (run_at, as_of_date, rules_version, n_checked, n_escalated, details_json) VALUES (?, ?, ?, ?, ?, ?)",
            (run_at or utc_now(), as_of_date, rules_version, n_checked, n_escalated, json.dumps(_json_safe(details), ensure_ascii=False)))
    return cur.lastrowid


def recent_tier1_runs(conn, since_date) -> list:
    out = []
    for r in conn.execute("SELECT * FROM tier1_runs WHERE as_of_date >= ? ORDER BY tier1_run_id", (since_date,)):
        d = dict(r)
        d["details"] = json.loads(d.pop("details_json"))
        out.append(d)
    return out



# ---------------------------------------------------------------- Tier 2

def insert_tier2_review(conn, *, escalation_id, ticker, mode, batch, model, ok, verdict=None, expected=None,
                        result=None, report_md=None, error=None, usage=None, cost_usd=0.0, created_at=None) -> int:
    """Store one Tier 2 review. usage: {input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, searches}."""
    u = usage or {}
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO tier2_reviews (escalation_id, ticker, created_at, mode, batch, model, ok, verdict, expected, result_json, "
            "report_md, error, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, searches, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (escalation_id, canonical_symbol(ticker), created_at or utc_now(), mode, batch, model, 1 if ok else 0, verdict, expected,
             json.dumps(_json_safe(result), ensure_ascii=False) if result is not None else None, report_md, error,
             u.get("input_tokens"), u.get("output_tokens"), u.get("cache_write_tokens"), u.get("cache_read_tokens"),
             u.get("searches"), float(cost_usd or 0.0)))
    return cur.lastrowid


NEWS_SENTIMENTS = ("positive", "neutral", "negative", "none")


def insert_news_digest(conn, *, ticker, digest_date, sentiment, gate_relevant=False, reason=None, summary=None,
                       headlines=None, model=None, created_at=None) -> bool:
    """One holding's news for one trading day (DECISIONS.md 60). False if that day is already stored (never overwritten)."""
    if sentiment not in NEWS_SENTIMENTS:
        raise ValueError(f"sentiment must be one of {', '.join(NEWS_SENTIMENTS)}")
    with transaction(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO news_digest (ticker, digest_date, created_at, sentiment, gate_relevant, reason, summary, "
            "headlines_json, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (canonical_symbol(ticker), digest_date, created_at or utc_now(), sentiment, 1 if gate_relevant else 0, reason, summary,
             json.dumps(_json_safe(headlines or []), ensure_ascii=False), model))
    return cur.rowcount == 1


def news_digests(conn, *, ticker=None, since=None) -> list:
    """Digest rows, newest day first."""
    sql, args, where = "SELECT * FROM news_digest", [], []
    if ticker:
        where.append("ticker = ?"); args.append(canonical_symbol(ticker))
    if since:
        where.append("digest_date >= ?"); args.append(since)
    if where:
        sql += " WHERE " + " AND ".join(where)
    out = []
    for r in conn.execute(sql + " ORDER BY digest_date DESC, ticker", args):
        d = dict(r)
        d["headlines"] = json.loads(d.pop("headlines_json") or "[]")
        d["gate_relevant"] = bool(d["gate_relevant"])
        out.append(d)
    return out


def _tier2_row(r):
    d = dict(r)
    d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
    d.pop("result_json", None)
    return d


def list_tier2_reviews(conn, *, batch=None, mode=None, limit=None) -> list:
    """Reviews, newest first."""
    sql, args, where = "SELECT * FROM tier2_reviews", [], []
    if batch:
        where.append("batch = ?"); args.append(batch)
    if mode:
        where.append("mode = ?"); args.append(mode)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY review_id DESC" + (f" LIMIT {int(limit)}" if limit else "")
    return [_tier2_row(r) for r in conn.execute(sql, args)]


def reviewed_escalation_ids(conn) -> set:
    """Escalations that already have a successful review (so a live run does not pay to review them twice)."""
    return {r[0] for r in conn.execute("SELECT DISTINCT escalation_id FROM tier2_reviews WHERE ok = 1 AND escalation_id IS NOT NULL")}


def tier2_cost_since(conn, since_iso) -> float:
    """Total dollars spent on Tier 2 reviews created at or after since_iso (UTC ISO 8601)."""
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM tier2_reviews WHERE created_at >= ?", (since_iso,)).fetchone()
    return float(row[0])

# ----------------------------------------------------------------------- CLI

def _report(conn, path: Path) -> None:
    size_kb = path.stat().st_size / 1024 if path.exists() else 0
    print(f"  file     : {path}  ({size_kb:.0f} KB)")
    print(f"  version  : {current_version(conn)} (code knows up to {load_migrations()[-1][0]})")
    print(f"  journal  : {conn.execute('PRAGMA journal_mode').fetchone()[0]}")
    for name, n in table_counts(conn).items():
        print(f"  table    : {name:<16} {n:>6} rows")


def cmd_init(path) -> None:
    conn = connect(path)
    before, after = init_schema(conn)
    p = db_path(path)
    print("Created a new database." if before == 0 and after > 0 else
          "Already up to date; nothing changed." if before == after else f"Upgraded version {before} -> {after}.")
    _report(conn, p)


def cmd_info(path) -> None:
    conn = connect(path, create=False)
    _report(conn, db_path(path))
    runs = list_runs(conn, 5)
    print("  recent runs:" if runs else "  recent runs: none yet")
    for r in runs:
        print(f"    #{r['run_id']:<4} {r['run_at']}  {r['source']}  {r['n_hits']} hits")


def selftest() -> int:
    passed, failed = 0, []

    def check(label, ok, detail=""):
        nonlocal passed
        if ok:
            passed += 1
            print(f"  ok    {label}")
        else:
            failed.append(label)
            print(f"  FAIL  {label} {detail}")

    def raises(exc, fn):
        try:
            fn()
        except exc:
            return True
        except Exception:                              # noqa: BLE001
            return False
        return False

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        conn = connect(path)
        latest = load_migrations()[-1][0]

        before, after = init_schema(conn)
        check("fresh database migrates to the latest version", (before, after) == (0, latest), f"{before}->{after}")
        check("expected tables exist", {"screen_runs", "screen_results", "screen_tags"} <= set(table_counts(conn)))
        check("WAL mode is on", conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
        check("foreign keys are on", conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1)

        b2, a2 = init_schema(conn)
        check("re-running init changes nothing", (b2, a2) == (latest, latest))

        hits = [
            {"Ticker": "psn", "Price": 44.82, "Recom": 1.60, "P/E": float("nan"), "Company": "Parsons"},
            {"Ticker": "RBA", "Price": 87.02, "Recom": 1.90, "P/E": 31.0, "Company": "RB Global"},
            {"Ticker": "TXT", "Price": 70.10, "Recom": 2.00, "P/E": 14.2, "Company": "Textron"},
        ]
        run_id = insert_screen_run(conn, source="finviz_screener", results=hits, filters="cap_midover,ta_perf_ytd20u",
                                   views_ok=["Overview", "Valuation"], csv_path="candidates_test.csv",
                                   run_at="2026-09-20T12:00:00+00:00")
        back = get_run(conn, run_id)
        check("round trip: run header", back["n_hits"] == 3 and back["source"] == "finviz_screener"
              and back["views_ok"] == ["Overview", "Valuation"] and back["run_at"] == "2026-09-20T12:00:00+00:00")
        by = {r["ticker"]: r for r in back["results"]}
        check("round trip: tickers are upper-cased canonical keys", set(by) == {"PSN", "RBA", "TXT"})
        check("round trip: values survive, NaN becomes null",
              by["PSN"]["row"]["Price"] == 44.82 and by["PSN"]["row"]["P/E"] is None and by["PSN"]["finviz_symbol"] == "psn")
        check("get_run() with no id returns the latest run", get_run(conn)["run_id"] == run_id)

        before_counts = table_counts(conn)
        check("duplicate ticker in one run is rejected", raises(ValueError, lambda: insert_screen_run(
            conn, source="x", results=[{"Ticker": "AAA"}, {"Ticker": "aaa"}])))
        check("missing ticker is rejected", raises(ValueError, lambda: insert_screen_run(
            conn, source="x", results=[{"Ticker": "BBB"}, {"Price": 1}])))
        check("rejected runs leave no partial rows", table_counts(conn) == before_counts)

        n = insert_screen_tags(conn, run_id, [
            {"ticker": "psn", "tag": "WATCH", "note": "Watch, real deterioration", "revisit": None},
            {"ticker": "RBA", "tag": "WATCH", "note": "two-sided"},
            {"ticker": "TXT", "tag": "NEW"}], positions_as_of="2026-08-28", log_as_of="2026-09-20")
        tg = get_tags(conn, run_id)
        check("tags round trip, keyed by canonical ticker",
              n == 3 and set(tg) == {"PSN", "RBA", "TXT"} and tg["PSN"]["note"] == "Watch, real deterioration"
              and tg["PSN"]["positions_as_of"] == "2026-08-28" and tg["TXT"]["note"] is None)
        insert_screen_tags(conn, run_id, [{"ticker": "PSN", "tag": "HELD"}])
        tg = get_tags(conn, run_id)
        check("re-tagging replaces the old tags for that run", set(tg) == {"PSN"} and tg["PSN"]["tag"] == "HELD")
        check("tags for a ticker that was not a hit are refused, and nothing is lost", raises(sqlite3.IntegrityError, lambda: insert_screen_tags(
            conn, run_id, [{"ticker": "PSN", "tag": "WATCH"}, {"ticker": "ZZZZ", "tag": "NEW"}])) and get_tags(conn, run_id)["PSN"]["tag"] == "HELD")
        check("run_tickers and latest_run_id", run_tickers(conn, run_id) == ["PSN", "RBA", "TXT"] and latest_run_id(conn) == run_id)

        d1 = insert_dossier(conn, ticker="psn", data={"snapshot": {"Price": "44.82"}, "ratings": []}, parse_ok=True,
                            run_id=run_id, reason="NEW", fetched_at="2026-09-10T12:00:00+00:00")
        d2 = insert_dossier(conn, ticker="PSN", data={"snapshot": {}}, parse_ok=False, parse_notes="no snapshot table found",
                            fetched_at="2026-09-19T12:00:00+00:00")
        now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        check("latest_dossier ignores failed parses by default", latest_dossier(conn, "PSN")["snapshot_id"] == d1
              and latest_dossier(conn, "PSN", ok_only=False)["snapshot_id"] == d2)
        check("dossier data round-trips", latest_dossier(conn, "PSN")["data"]["snapshot"]["Price"] == "44.82")
        check("the cache honours max age, and a failed parse never counts as cached",
              recent_dossier(conn, "PSN", 14, now=now)["snapshot_id"] == d1 and recent_dossier(conn, "PSN", 7, now=now) is None)
        check("no snapshot means no cache hit", recent_dossier(conn, "ZZZZ", 7, now=now) is None)

        d3 = insert_dossier(conn, ticker="PSN", data={"snapshot": {"Price": "45.00"}}, parse_ok=True, fetched_at="2026-09-20T09:00:00+00:00")
        cur_d, prev_d = latest_two_dossiers(conn, "psn")
        check("latest_two_dossiers returns newest first and skips failed parses", cur_d["snapshot_id"] == d3 and prev_d["snapshot_id"] == d1)
        check("latest_two_dossiers copes with none and with one", latest_two_dossiers(conn, "ZZZZ") == (None, None)
              and latest_two_dossiers(conn, "NEWONE") == (None, None))

        cr = insert_calendar_run(conn, as_of_date="2026-09-20", window_from="2026-08-21", window_to="2026-11-30",
                                 events=[{"ticker": "duol", "event_date": "2026-11-04", "kind": "held", "time": "amc", "confirmed": True, "eps_est": 0.6},
                                         {"ticker": "DUOL", "event_date": "2026-11-04", "kind": "held"},
                                         {"ticker": "PSN", "event_date": "2026-11-04", "kind": "watch", "confirmed": False}],
                                 quotes={"DUOL": {"price": 144.5, "change_pct": -1.2}, "BAD": {"price": None}}, run_at="2026-09-20T10:00:00+00:00")
        crow = latest_calendar_run(conn)
        check("calendar run: duplicates and price-less quotes are dropped, counts recorded",
              crow["calendar_run_id"] == cr and crow["n_events"] == 2 and crow["n_quotes"] == 1 and crow["as_of_date"] == "2026-09-20")
        check("calendar run: dates come back per ticker", calendar_event_dates(conn, cr) == {"DUOL": ["2026-11-04"], "PSN": ["2026-11-04"]})
        check("calendar run: FMP's confirmed flag round-trips as 0/1 and unknown as null",
              conn.execute("SELECT fmp_confirmed FROM earnings_events WHERE ticker='DUOL'").fetchone()[0] == 1
              and conn.execute("SELECT fmp_confirmed FROM earnings_events WHERE ticker='PSN'").fetchone()[0] == 0)
        conn.execute("DELETE FROM calendar_runs WHERE calendar_run_id = ?", (cr,))
        check("deleting a calendar run cascades to its events and quotes", table_counts(conn)["earnings_events"] == 0 and table_counts(conn)["quote_snapshots"] == 0)

        p1, s1 = save_positions_snapshot(conn, {"held": [{"ticker": "DUOL"}]}, "aaa", as_of="2026-08-28")
        p2, s2 = save_positions_snapshot(conn, {"held": [{"ticker": "DUOL"}]}, "aaa")
        p3, s3 = save_positions_snapshot(conn, {"held": []}, "bbb")
        check("positions snapshots are saved only when the content changes", (s1, s2, s3) == (True, False, True) and p1 == p2 and p3 != p1
              and table_counts(conn)["positions_snapshots"] == 2)

        n1 = upsert_prices(conn, "nyt", [("2026-08-04", 80.0), ("2026-08-05", 70.0)])
        n2 = upsert_prices(conn, "NYT", [("2026-08-04", 80.0), ("2026-08-05", 70.5), ("2026-08-06", 71.0)])
        check("price history: new dates insert, an unchanged close is skipped, a changed close updates",
              (n1, n2) == (2, 2) and price_series(conn, "NYT") == [("2026-08-04", 80.0), ("2026-08-05", 70.5), ("2026-08-06", 71.0)])
        check("price history: latest date and the 'since' filter",
              latest_price_date(conn, "NYT") == "2026-08-06" and price_series(conn, "NYT", since="2026-08-06") == [("2026-08-06", 71.0)]
              and latest_price_date(conn, "ZZZZ") is None and price_series(conn, "ZZZZ") == [])

        n3 = upsert_prices(conn, "QQQQ", [("2026-09-17", 100.0), ("2026-09-18", 84.0, -16.0)], source="finviz_screener")
        n4 = upsert_prices(conn, "QQQQ", [("2026-09-17", 100.0, -1.5), ("2026-09-18", 84.0, -16.0)], source="finviz_screener")
        n5 = upsert_prices(conn, "QQQQ", [("2026-09-17", 100.0), ("2026-09-18", 84.0)], source="finviz_screener")
        check("price history: a day's own change_pct is stored, a newly known one updates the row, and unknown never erases a known one",
              (n3, n4, n5) == (2, 1, 0) and [r["change_pct"] for r in price_rows(conn, "QQQQ")] == [-1.5, -16.0]
              and price_rows(conn, "QQQQ")[1]["source"] == "finviz_screener")
        check("price history: latest_price returns the newest row with its change and source",
              latest_price(conn, "qqqq") == {"date": "2026-09-18", "close": 84.0, "change_pct": -16.0, "source": "finviz_screener"} and latest_price(conn, "ZZZZ") is None
              and [r["date"] for r in price_rows(conn, "QQQQ", since="2026-09-18")] == ["2026-09-18"])

        e1, c1 = insert_escalation(conn, ticker="nyt", funnel="A", trigger="price_move", trigger_ref="2026-08-05",
                                   reasons=["-12.5% in a day"], evidence={"ret_pct": -12.5}, rules_version="1@2026-09-20")
        e2, c2 = insert_escalation(conn, ticker="NYT", funnel="A", trigger="price_move", trigger_ref="2026-08-05", reasons=["again"])
        e3, c3 = insert_escalation(conn, ticker="NYT", funnel="A", trigger="price_move", trigger_ref="2026-08-06", reasons=["next day"])
        check("escalations: the same (ticker, trigger, ref) is queued once; a different ref is a new escalation",
              (c1, c2, c3) == (True, False, True) and e1 == e2 and e3 != e1 and len(list_escalations(conn)) == 2)
        check("escalations: reasons and evidence round-trip", list_escalations(conn)[0]["reasons"] == ["-12.5% in a day"] and list_escalations(conn)[0]["evidence"] == {"ret_pct": -12.5})
        check("escalations: status changes, is filtered, and bad values are refused",
              set_escalation_status(conn, e1, "done", note="reviewed") and [e["escalation_id"] for e in list_escalations(conn, "open")] == [e3]
              and list_escalations(conn, "done")[0]["note"] == "reviewed" and not set_escalation_status(conn, 999, "done")
              and raises(ValueError, lambda: set_escalation_status(conn, e1, "banana")))
        tr = insert_tier1_run(conn, as_of_date="2026-09-20", rules_version="1@2026-09-20", n_checked=31, n_escalated=1, details={"near": [{"ticker": "NYT"}]})
        check("tier1 runs are stored with their details and can be listed by date",
              recent_tier1_runs(conn, "2026-09-01")[0]["details"]["near"][0]["ticker"] == "NYT" and recent_tier1_runs(conn, "2026-10-01") == [] and tr == 1)

        conn.execute("DELETE FROM screen_runs WHERE run_id = ?", (run_id,))
        check("deleting a run keeps the dossier snapshot, just detached from the run",
              latest_dossier(conn, "PSN")["run_id"] is None)
        check("deleting a run cascades to its results and tags",
              table_counts(conn)["screen_results"] == 0 and table_counts(conn)["screen_tags"] == 0)

        run_id = insert_screen_run(conn, source="finviz_screener", results=hits[:1])
        reader = connect(path)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO screen_runs (run_at, source, n_hits) VALUES ('x', 'uncommitted', 0)")
        seen = reader.execute("SELECT COUNT(*) FROM screen_runs").fetchone()[0]
        conn.execute("ROLLBACK")
        reader.close()
        check("a reader is not blocked by an open write, and sees only committed data", seen == 1, f"saw {seen}")

    with tempfile.TemporaryDirectory() as tmp:
        # your real upgrade path: a database created at version 1, then upgraded, keeps its data
        conn = connect(Path(tmp) / "upg.db")
        full = SCHEMA_FILE.read_text(encoding="utf-8")
        v1_only = full.split("-- migration: 2")[0]
        init_schema(conn, v1_only)
        old_id = insert_screen_run(conn, source="finviz_screener", results=[{"Ticker": "PSN", "Price": 44.82}])
        b, a = init_schema(conn)
        check("a version-1 database upgrades to the latest and keeps its runs",
              b == 1 and a == load_migrations()[-1][0] and get_run(conn, old_id)["results"][0]["row"]["Price"] == 44.82 and "screen_tags" in table_counts(conn))
        conn.close()

    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(Path(tmp) / "mig.db")
        good = "-- migration: 1\nCREATE TABLE a (x INTEGER);\n"
        bad = good + "-- migration: 2\nCREATE TABLE b (x INTEGER);\nCREATE TABLE b (x INTEGER);\n"
        init_schema(conn, good)
        check("a failing migration raises", raises(sqlite3.Error, lambda: init_schema(conn, bad)))
        check("a failing migration rolls back completely",
              current_version(conn) == 1 and "b" not in table_counts(conn))
        conn.execute("PRAGMA user_version = 99")
        check("a database newer than the code is refused", raises(SchemaError, lambda: init_schema(conn, good)))
        check("a gap in migration numbers is rejected",
              raises(SchemaError, lambda: load_migrations("-- migration: 1\nSELECT 1;\n-- migration: 3\nSELECT 1;\n")))
        check("BEGIN/COMMIT inside a migration is rejected",
              raises(SchemaError, lambda: load_migrations("-- migration: 1\nBEGIN;\nSELECT 1;\nCOMMIT;\n")))

    total = passed + len(failed)
    print(f"\n{'PASS' if not failed else 'FAIL'}: {passed}/{total} checks")
    return 0 if not failed else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", action="store_true", help="create or upgrade research.db")
    ap.add_argument("--info", action="store_true", help="show version, tables and recent runs")
    ap.add_argument("--selftest", action="store_true", help="run built-in checks on a throwaway database")
    ap.add_argument("--path", default=None, help="database file (default: $RESEARCH_DB or research.db beside sentinel_db.py)")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())
    try:
        if args.init:
            cmd_init(args.path)
        elif args.info:
            cmd_info(args.path)
        else:
            ap.print_help()
    except (FileNotFoundError, SchemaError, sqlite3.Error) as e:
        sys.exit(f"sentinel_db.py: {e}")


if __name__ == "__main__":
    main()

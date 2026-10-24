"""
Scrapes YOUR Finviz screener across five views (Overview, Valuation,
Financial, Ownership, Performance) and merges them into one row per
ticker, so you see the actual filter values (debt/equity, margin,
insider %, YTD performance) behind each name that passed, not just
that it passed. Meant to run occasionally, weekly at most, not daily.

New in this version: every run is also saved to research.db (through sentinel_db.py), so
weekly results accumulate instead of living only in whatever CSV you kept, and the
scanner tells you what is new and what dropped off since the last comparable run.
The CSV is written first and is unchanged; a database problem never blocks it.

Also new: each hit is tagged against your ledger files, so you see at a glance which names
are NEW and which you already hold, watch, or decided on (and why). Tags come from
positions.yaml (HELD, WATCH) and research_log.yaml (everything you reviewed); positions.yaml wins.

Also new: a "dossier" stage. For names worth a closer look, it fetches Finviz's quote page
(analyst actions, insider trades, news, peers, ~84 fields) ONE page per name, politely, caches it
for 7 days, and stores each fetch so later runs can show what changed. It is OFF unless you ask
(--dossier), and it only fetches NEW names unless you add --tracked. The page reading lives in
sentinel_finviz.py; run sentinel_probe.py once to check it against the real page first.

Also new: --quotes fetches today's price and the day's change for your US held and watch names plus the
NEW candidates, about 2 requests in all (Finviz lists up to 20 tickers per page), and stores them in
research.db as daily closes. Tier 1 uses them to spot one-day moves. Your FMP plan does not serve these names.

Setup:
  pip3 install requests pandas lxml pyyaml
  (sentinel_finviz.py must also sit in the same folder)
  (sentinel_db.py and sentinel_schema.sql must sit in the same folder as this script)

Run:
  python3 sentinel_scan.py                       # scan, write CSV, save the run
  python3 sentinel_scan.py --no-db               # CSV only, exactly like before
  python3 sentinel_scan.py --import-csv candidates_*.csv
                                                          # load old CSVs as past runs (no scraping)
  python3 sentinel_scan.py --retag               # re-tag the latest saved run after editing your YAML
  python3 sentinel_scan.py --retag 2             # ...or a specific run number (no scraping)
  python3 sentinel_scan.py --dossier             # scan, then fetch detail pages for the NEW names
  python3 sentinel_scan.py --dossier-only        # detail pages for the latest saved run, no screener scan
  python3 sentinel_scan.py --dossier-only --tickers PSN,RBA   # exactly these names
  python3 sentinel_scan.py --dossier-only --held --watch --cap 30   # every US name in positions.yaml
  python3 sentinel_scan.py --quotes             # daily prices for tracked names + NEW candidates (run after 4pm ET)
  (--tracked adds held/watch names that hit this screen; --cap N, --max-age DAYS, --force to ignore the cache)
  python3 sentinel_scan.py --db /path/to/research.db

Output:
  candidates_<date>.csv -- one row per ticker, columns from all five views.
  One new run in research.db (unless --no-db).
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import io
import random
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests

try:
    import sentinel_db as db       # shared helper, sits next to this file
except ImportError:                # the scanner still works without it, CSV only
    db = None

try:
    import yaml                    # only needed for ledger tagging
except ImportError:
    yaml = None

try:
    import sentinel_finviz as finviz_quote  # only needed for the dossier stage
except ImportError:
    finviz_quote = None

# Your actual screener URLs, one per view, confirmed from the Finviz browser UI.
# IF YOU HAVE CHANGED THESE since the handoff, paste your own five URLs over this block.
VIEWS = {
    "Overview": "https://finviz.com/screener?v=111&f=an_recom_buybetter,cap_midover,fa_debteq_u1,fa_opermargin_pos,fa_pfcf_u40,sh_avgvol_o300,sh_insidertrans_pos,ta_perf_ytd20u&ft=4",
    "Valuation": "https://finviz.com/screener?v=121&f=an_recom_buybetter,cap_midover,fa_debteq_u1,fa_opermargin_pos,fa_pfcf_u40,sh_avgvol_o300,sh_insidertrans_pos,ta_perf_ytd20u&ft=4",
    "Financial": "https://finviz.com/screener?v=161&f=an_recom_buybetter,cap_midover,fa_debteq_u1,fa_opermargin_pos,fa_pfcf_u40,sh_avgvol_o300,sh_insidertrans_pos,ta_perf_ytd20u&ft=4",
    "Ownership": "https://finviz.com/screener?v=131&f=an_recom_buybetter,cap_midover,fa_debteq_u1,fa_opermargin_pos,fa_pfcf_u40,sh_avgvol_o300,sh_insidertrans_pos,ta_perf_ytd20u&ft=4",
    "Performance": "https://finviz.com/screener?v=141&f=an_recom_buybetter,cap_midover,fa_debteq_u1,fa_opermargin_pos,fa_pfcf_u40,sh_avgvol_o300,sh_insidertrans_pos,ta_perf_ytd20u&ft=4",
}

SOURCE = "finviz_screener"

HERE = Path(__file__).resolve().parent
DEFAULT_POSITIONS = HERE / "positions.yaml"
DEFAULT_LOG = HERE / "research_log.yaml"

QUOTE_URL = "https://finviz.com/quote.ashx?t={symbol}&p=d"
QUOTES_URL = "https://finviz.com/screener?v=141&t={tickers}&r=1"    # Performance view: Price and the day's Change, for a ticker list
QUOTES_PER_PAGE = 20
DEBUG_DIR = HERE / "dossier_debug"          # pages that failed to parse are saved here for inspection
DEFAULT_CAP = 12                            # most detail pages fetched per invocation
DEFAULT_MAX_AGE_DAYS = 7                    # a good snapshot newer than this is reused, not re-fetched
NON_US_SUFFIXES = {"TO", "V", "CN", "NE"}   # Finviz covers US listings only

# research_log.yaml status -> tag. HELD / WATCH come from positions.yaml; NEW means in neither.
STATUS_TO_TAG = {"disqualified": "DISQUALIFIED", "passed": "PASSED", "parked": "PARKED",
                 "dormant": "DORMANT", "removed": "REMOVED", "closed": "CLOSED"}
TAG_ORDER = ["HELD", "WATCH", "PARKED", "DORMANT", "REMOVED", "PASSED", "DISQUALIFIED", "CLOSED"]

# A small pool of realistic, current browser UAs -- one is picked at random
# for this run and reused throughout, since a real browser doesn't change
# its identity mid-session.
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": random.choice(USER_AGENTS),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Referer": "https://finviz.com/screener.ashx",
})

MAX_PAGES = 10
PAGE_SIZE = 20

# Real US tickers, with an optional share-class suffix (BRK-B, BF.B). Anything else is
# probably a parsing artifact, and a wrong key would now persist in the database.
TICKER_RE = re.compile(r"^[A-Z]{1,5}([.-][A-Z]{1,2})?$")
DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")


def polite_delay(low=2.0, high=5.0):
    """Randomized delay so requests don't arrive at a suspiciously
    regular interval -- a real person doesn't click exactly every 1.5s."""
    time.sleep(random.uniform(low, high))


def fetch_with_retry(url, max_retries=2):
    for attempt in range(max_retries + 1):
        try:
            resp = SESSION.get(url, timeout=15)
        except requests.RequestException as e:
            print(f"    request error: {e}")
            resp = None

        if resp is not None and resp.status_code == 200:
            return resp
        if attempt < max_retries:
            backoff = random.uniform(5, 12) * (attempt + 1)
            status = resp.status_code if resp is not None else "no response"
            print(f"    got {status}, backing off {backoff:.1f}s before retry...")
            time.sleep(backoff)
    return None


def fix_ticker_column(t):
    """Finviz's ticker cell is merged with a small logo/icon cell during
    parsing, which duplicates the first letter (e.g. 'AADMA' instead of
    'ADMA'). Strip it when the artifact is present."""
    t = t.copy()
    t["Ticker"] = t["Ticker"].apply(
        lambda s: s[1:] if isinstance(s, str) and len(s) > 1 and s[0] == s[1] else s
    )
    return t


def _has_column(t, need):
    """need is a column name, or a tuple of acceptable alternative names (Finviz has renamed columns before)."""
    return any(c in t.columns for c in need) if isinstance(need, (tuple, list)) else need in t.columns


def header_label(cell):
    """A column header's full name. Since Oct 2026 Finviz puts a long and a short label in each header
    ("Change %" + "Chg %"), so the plain text reads "Change %Chg %" (found Oct 24, 2026). The full name is in
    aria-label, or in the span with class fv-table_header-label; plain text is the last resort."""
    label = (cell.get("aria-label") or "").strip()
    if label:
        return label
    spans = cell.xpath('.//span[contains(concat(" ", normalize-space(@class), " "), " fv-table_header-label ")]')
    if spans:
        return spans[0].text_content().strip()
    return cell.text_content().strip()


def grid_table(html):
    """Finviz's redesigned screener (seen Oct 17, 2026) draws the results as <div role="grid"> instead of a <table>.
    Read it the same way: the header labels become the columns ("Ticker", "Price", "Change %", ...) and every cell
    keeps the text it shows, exactly like the old table, so nothing downstream changes. The ticker comes from the
    cell's data-ticker attribute, which is clean (the shown text has the logo letter in front: "AALB")."""
    try:
        import lxml.html
        doc = lxml.html.fromstring(html)
    except ImportError:
        raise
    except Exception:                           # noqa: BLE001  empty or unparseable page
        return None
    for grid in doc.xpath('//*[@role="grid"]'):
        headers = {h.get("aria-colindex"): header_label(h) for h in grid.xpath('.//*[@role="columnheader"]')}
        if "Ticker" not in headers.values():
            continue
        records = []
        for row in grid.xpath('.//*[@role="row"]'):
            cells = row.xpath('.//*[@role="gridcell"]')
            if not cells:
                continue                        # the header row
            rec = {}
            for c in cells:
                label = headers.get(c.get("aria-colindex"))
                if label is None:
                    continue
                rec[label] = c.get("data-ticker") if label == "Ticker" and c.get("data-ticker") else c.text_content().strip()
            if rec.get("Ticker"):
                records.append(rec)
        if records:
            return pd.DataFrame(records, columns=list(dict.fromkeys(headers.values())))
    return None


def _acceptable(t, min_rows, required):
    if "Ticker" not in t.columns or any(not _has_column(t, need) for need in required):
        return False
    # Reject tables that don't actually look like Finviz's stock grid: too many columns, too few rows, or cells
    # containing walls of text instead of short values (filter menus, hidden data blobs, etc).
    if len(t.columns) > 20 or len(t) < min_rows:
        return False
    return t.astype(str).apply(lambda col: col.str.len().max()).max() <= 100


def find_result_table(html, min_rows=2, required=()):
    """The stock grid on a Finviz page. min_rows and required only ever tighten or relax the
    screener's own checks for a caller that knows what it expects (the quotes fetch: 1 row is fine,
    but the Price and Change columns must be there, which also rules out the decoy tables).
    The redesigned page (a div grid, Oct 2026) is read first; the old <table> page is still understood."""
    t = grid_table(html)
    if t is not None and _acceptable(t, min_rows, required):
        return t                                # tickers are already clean: no fix_ticker_column
    try:
        # lxml only: pandas' fallback parser needs html5lib, which is not installed, and on Oct 17, 2026 a Finviz page
        # with no <table> at all made that fallback crash the whole step with a misleading "install html5lib" error.
        tables = pd.read_html(io.StringIO(html), flavor="lxml")
    except ImportError:
        raise                                   # lxml itself missing: a real setup problem, say so
    except Exception:                           # noqa: BLE001  no table at all: changed page, or a block/consent page
        return None
    for t in tables:
        if _acceptable(t, min_rows, required):
            return fix_ticker_column(t)
    return None


def fetch_view(view_name, base_url):
    all_rows = []
    for page in range(MAX_PAGES):
        offset = page * PAGE_SIZE + 1
        url = base_url + f"&r={offset}"
        print(f"  [{view_name}] fetching row {offset}...")

        resp = fetch_with_retry(url)
        if resp is None:
            print(f"  [{view_name}] gave up after retries -- likely blocked.")
            break

        table = find_result_table(resp.text)
        if table is None or table.empty:
            if table is None and page == 0:
                out = HERE / "probe_out"
                out.mkdir(exist_ok=True)
                (out / f"screen_{view_name}.html").write_text(resp.text, encoding="utf-8")
                print(f"  [{view_name}] PROBLEM: no stock table on the page (Finviz may have changed it, or refused the "
                      f"request). Saved to probe_out/screen_{view_name}.html")
            break

        seen = set().union(*(set(t["Ticker"]) for t in all_rows)) if all_rows else set()
        if all_rows and set(table["Ticker"]) <= seen:
            print(f"  [{view_name}] page {page + 1} repeated rows already read -- stopping (the page offset was ignored).")
            break                               # without this, an ignored &r= would fetch the same page 10 times
        all_rows.append(table)
        if len(table) < PAGE_SIZE:
            break
        polite_delay()

    if not all_rows:
        return None
    return pd.concat(all_rows, ignore_index=True).drop_duplicates(subset="Ticker")


# ------------------------------------------------------------------ database

def extract_filters(url):
    """The f= part of a screener URL, so two runs can be compared like for like."""
    f = parse_qs(urlparse(url).query).get("f", [])
    return ",".join(f) or None


def _require_db():
    if db is None:
        sys.exit("sentinel_db.py was not found next to this script, so there is nothing to save to.")


def print_diff(conn, run_id):
    """What is new / dropped since the last COMPARABLE run: same filters if there is one,
    otherwise the latest imported CSV run (filters unknown, so flagged as a weaker comparison)."""
    now_row = conn.execute("SELECT filters FROM screen_runs WHERE run_id = ?", (run_id,)).fetchone()
    filters = now_row["filters"]
    caveat = ""
    prev = conn.execute(
        "SELECT run_id, run_at FROM screen_runs WHERE run_id < ? AND filters IS ? ORDER BY run_id DESC LIMIT 1",
        (run_id, filters)).fetchone()
    if prev is None and filters is not None:
        prev = conn.execute(
            "SELECT run_id, run_at FROM screen_runs WHERE run_id < ? AND filters IS NULL ORDER BY run_id DESC LIMIT 1",
            (run_id,)).fetchone()
        caveat = " [imported CSV, filters unknown: may not be a like-for-like screen]"
    if prev is None:
        print("  No earlier run to compare with yet.")
        return

    before = {r["ticker"] for r in conn.execute("SELECT ticker FROM screen_results WHERE run_id = ?", (prev["run_id"],))}
    now = {r["ticker"] for r in conn.execute("SELECT ticker FROM screen_results WHERE run_id = ?", (run_id,))}
    new, dropped = sorted(now - before), sorted(before - now)
    print(f"  Compared with run #{prev['run_id']} ({prev['run_at'][:10]}){caveat}:")
    print(f"    new     ({len(new)}): {', '.join(new) or '-'}")
    print(f"    dropped ({len(dropped)}): {', '.join(dropped) or '-'}")
    print(f"    same    ({len(now & before)})")


def _iso(v):
    return None if v is None else (v.isoformat() if hasattr(v, "isoformat") else str(v))


def load_ledger(positions_path, log_path):
    """Build {TICKER: {tag, note, revisit}} from your two YAML files.
    Precedence, highest first: held, watch (positions.yaml), then research_log.yaml.
    Returns (index, meta, warnings). A missing file is a warning, never a crash."""
    if yaml is None:
        raise RuntimeError("pyyaml is not installed. Run: pip3 install pyyaml")
    index, warnings = {}, []
    meta = {"positions_as_of": None, "log_as_of": None}

    if Path(log_path).exists():
        with open(log_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        meta["log_as_of"] = _iso(raw.get("as_of"))
        for e in raw.get("entries") or []:
            tk = str(e.get("ticker", "")).strip().upper()
            st = str(e.get("status", "")).strip().lower()
            if not tk:
                warnings.append("research_log.yaml: an entry has no ticker")
            elif st not in STATUS_TO_TAG:
                warnings.append(f"research_log.yaml: {tk} has status '{st}'; allowed: {', '.join(STATUS_TO_TAG)}. Entry ignored.")
            else:
                if tk in index:
                    warnings.append(f"research_log.yaml: {tk} appears more than once; the last entry wins")
                index[tk] = {"tag": STATUS_TO_TAG[st], "note": e.get("reason"), "revisit": e.get("revisit")}
    else:
        warnings.append(f"{Path(log_path).name} not found, so nothing counts as already reviewed")

    if Path(positions_path).exists():
        with open(positions_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        meta["positions_as_of"] = _iso(raw.get("as_of"))
        for kind, tag in (("watch", "WATCH"), ("held", "HELD")):
            for p in raw.get(kind) or []:
                tk = str(p.get("ticker", "")).strip().upper()
                if not tk:
                    continue
                if index.get(tk, {}).get("tag") not in (None, "HELD", "WATCH"):
                    warnings.append(f"{tk} is in positions.yaml ({kind}) and in research_log.yaml; positions.yaml wins")
                if kind == "held":
                    note = f"Held in {p.get('account', 'an account')}." + (f" Gate: {p['gate']}" if p.get("gate") else "")
                else:
                    note = f"{p.get('status') or 'Watch'}." + (f" Gate: {p['gate']}" if p.get("gate") else "")
                index[tk] = {"tag": tag, "note": note, "revisit": None}
    else:
        warnings.append(f"{Path(positions_path).name} not found, so nothing counts as held or watched")
    return index, meta, warnings


def classify(ticker, index):
    e = index.get(ticker)
    if e is None:
        return {"ticker": ticker, "tag": "NEW", "note": None, "revisit": None}
    return {"ticker": ticker, "tag": e["tag"], "note": e["note"], "revisit": e["revisit"]}


def _short(text, n=78):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "\u2026"


def print_tags(run_id, tags, meta):
    new = sorted(t["ticker"] for t in tags if t["tag"] == "NEW")
    known = [t for t in tags if t["tag"] != "NEW"]
    print(f"\nLedger tags for run #{run_id} ({len(tags)} hits). positions.yaml as of {meta['positions_as_of'] or 'n/a'}, "
          f"research_log.yaml as of {meta['log_as_of'] or 'n/a'}:")
    print(f"\n  NEW, not in your ledger ({len(new)}): {', '.join(new) or '-'}")
    print(f"\n  Already known ({len(known)}):")
    for t in sorted(known, key=lambda t: (TAG_ORDER.index(t["tag"]), t["ticker"])):
        print(f"    {t['ticker']:<6} {t['tag']:<13} {_short(t['note'])}")
        if t["revisit"]:
            print(f"    {'':<6} {'':<13} revisit if: {_short(t['revisit'], 70)}")


def tag_run(conn, run_id, positions_path=None, log_path=None):
    """Tag every hit in a saved run against the ledger files, and store the tags."""
    index, meta, warnings = load_ledger(positions_path or DEFAULT_POSITIONS, log_path or DEFAULT_LOG)
    for w in warnings:
        print(f"  ledger warning: {w}")
    tags = [classify(t, index) for t in db.run_tickers(conn, run_id)]
    db.insert_screen_tags(conn, run_id, tags, positions_as_of=meta["positions_as_of"], log_as_of=meta["log_as_of"])
    print_tags(run_id, tags, meta)


def retag(run_arg, db_path, positions_path, log_path):
    """Re-tag a saved run (default: the latest) after you edit your YAML. No scraping."""
    _require_db()
    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        run_id = db.latest_run_id(conn) if run_arg in (None, "latest") else int(run_arg)
    except ValueError:
        sys.exit("--retag takes a run number (or nothing, for the latest run).")
    if run_id is None or not conn.execute("SELECT 1 FROM screen_runs WHERE run_id = ?", (run_id,)).fetchone():
        sys.exit(f"No such run ({run_arg}). See: python3 sentinel_db.py --info")
    tag_run(conn, run_id, positions_path, log_path)
    conn.close()


def save_run_to_db(merged, views_ok, csv_path, args):
    """Save this run. Never raises: the CSV is already on disk, so a database problem is a warning."""
    if db is None:
        print("\n(sentinel_db.py not found next to this script; skipping the database save. CSV is written.)")
        return
    try:
        conn = db.connect(args.db)
        db.init_schema(conn)
        partial = len(views_ok) < len(VIEWS)
        run_id = db.insert_screen_run(
            conn, source=SOURCE, results=merged.to_dict("records"),
            filters=extract_filters(VIEWS["Overview"]), views_ok=views_ok,
            csv_path=Path(csv_path).name,
            notes=f"PARTIAL: only {', '.join(views_ok)} loaded" if partial else None)
        print(f"\nSaved to research.db as run #{run_id} ({len(merged)} hits).")
        print_diff(conn, run_id)
        try:
            tag_run(conn, run_id, args.positions, args.log)
        except Exception as e:  # noqa: BLE001 -- the run is already saved; tagging is a bonus
            print(f"\nWARNING: the run was saved but could not be tagged against your ledger ({e}).")
        if args.dossier:
            try:
                run_dossier_stage(conn, run_id, args)
            except Exception as e:  # noqa: BLE001 -- the run and tags are already safe
                print(f"\nWARNING: the dossier stage failed ({e}). The run and its tags are saved.")
        conn.close()
    except Exception as e:  # noqa: BLE001 -- deliberately broad, see docstring
        print(f"\nWARNING: could not save to the database ({e}). The CSV is safe; nothing else was affected.")


def run_date_of(path):
    """A CSV's run date: the YYYY-MM-DD in its name, else the file's modified date."""
    m = DATE_IN_NAME.search(path.name)
    return m.group(1) if m else datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()


def import_csvs(patterns, db_path=None):
    """Load old candidates_*.csv files into the database as past runs. No scraping. Safe to repeat:
    a CSV whose file name is already recorded is skipped. Imported runs have no filter string or
    view list (the CSV doesn't contain them), and are stamped noon UTC on the date in the filename."""
    _require_db()
    paths = []
    for pat in patterns:
        paths += [Path(p) for p in (glob.glob(pat) or [pat])]
    paths = sorted(set(paths), key=lambda p: (run_date_of(p) if p.exists() else "", p.name))

    conn = db.connect(db_path)
    db.init_schema(conn)
    added = skipped = 0
    for p in paths:
        if not p.exists():
            print(f"  skip {p}: file not found")
            skipped += 1
            continue
        if conn.execute("SELECT 1 FROM screen_runs WHERE csv_path = ?", (p.name,)).fetchone():
            print(f"  skip {p.name}: already in the database")
            skipped += 1
            continue
        try:
            df = pd.read_csv(p)
            if "Ticker" not in df.columns or df.empty:
                raise ValueError("no 'Ticker' column or no rows")
            d = run_date_of(p)
            run_id = db.insert_screen_run(
                conn, source=SOURCE, results=df.to_dict("records"), csv_path=p.name,
                notes="imported from CSV; filters and views unknown", run_at=f"{d}T12:00:00+00:00")
        except Exception as e:  # noqa: BLE001
            print(f"  skip {p.name}: {e}")
            skipped += 1
            continue
        print(f"  imported {p.name}: run #{run_id}, {len(df)} hits, dated {d}")
        added += 1
    conn.close()
    print(f"\nDone: {added} imported, {skipped} skipped. Check with: python3 sentinel_db.py --info")


# ---------------------------------------------------------------------- quotes

def _now_et():
    """The current time in New York, or None if the time-zone database is unavailable."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001
        return None


def _prev_business_day(d):
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def market_state(now_et):
    """What a Finviz price means right now: (state, trading_date_iso). trading_date is the session the price
    belongs to, or None while the US market is open (the price is in progress, not a close). US holidays are
    not modelled: on a holiday the same close is simply stored again under the next day, which is harmless."""
    wd, t = now_et.weekday(), now_et.time()
    if wd >= 5:
        return "weekend", (now_et.date() - timedelta(days=wd - 4)).isoformat()
    if t < datetime.strptime("09:30", "%H:%M").time():
        return "before the open", _prev_business_day(now_et.date()).isoformat()
    if t < datetime.strptime("16:00", "%H:%M").time():
        return "market open", None
    return "after the close", now_et.date().isoformat()


def _pct(x):
    try:
        return float(str(x).strip().rstrip("%").replace(",", ""))
    except ValueError:
        return None


def fetch_quote_rows(symbols):
    """Price and day change for a list of tickers, 20 per page. Returns ({ticker: {price, change_pct}}, problems).
    If Finviz returns something unexpected, the page is saved to probe_out/ and described, so it can be fixed."""
    rows, problems = {}, []
    chunks = [symbols[i:i + QUOTES_PER_PAGE] for i in range(0, len(symbols), QUOTES_PER_PAGE)]
    for n, chunk in enumerate(chunks, 1):
        if n > 1:
            polite_delay(3.0, 7.0)
        print(f"  fetching quotes for {len(chunk)} ticker(s) (page {n} of {len(chunks)})...")
        resp = fetch_with_retry(QUOTES_URL.format(tickers=",".join(chunk)))
        if resp is None:
            problems.append(f"page {n}: no usable response after retries")
            continue
        # Finviz's column is "Change %" (seen in the real page, Sep 2026); "Change" is accepted in case it is renamed back.
        table = find_result_table(resp.text, min_rows=1, required=("Ticker", "Price", ("Change %", "Change")))
        if table is None:
            out = HERE / "probe_out"
            out.mkdir(exist_ok=True)
            (out / f"quotes_page{n}.html").write_text(resp.text, encoding="utf-8")
            seen = grid_table(resp.text)
            cols = f" Columns found instead: {', '.join(map(str, list(seen.columns)[:8]))}." if seen is not None else ""
            problems.append(f"page {n}: no table with Ticker, Price and Change columns was found (Finviz may have changed the page, "
                            f"or refused the request).{cols} The page was saved to probe_out/quotes_page{n}.html")
            continue
        change_col = "Change %" if "Change %" in table.columns else "Change"
        unexpected = []
        for _, r in table.iterrows():
            price = _pct(r["Price"])
            if not isinstance(r["Ticker"], str) or price is None:
                continue
            if r["Ticker"] not in chunk:      # a ticker we did not ask for is a parsing artifact: never store it under a wrong key
                unexpected.append(r["Ticker"])
                continue
            rows[r["Ticker"]] = {"price": price, "change_pct": _pct(r[change_col])}
        if unexpected:
            problems.append(f"page {n}: ignored {len(unexpected)} row(s) whose ticker was not one of the {len(chunk)} asked for: "
                            f"{', '.join(unexpected[:8])}")
    return rows, problems


def quote_targets(conn, args):
    """US tickers to price: --tickers if given, else held + watch from positions.yaml, then NEW candidates of the latest run."""
    if args.tickers:
        names = [db.canonical_symbol(t) for t in args.tickers.split(",") if t.strip()]
    else:
        held, watch = positions_names(args.positions)
        names = held + watch
        run_id = db.latest_run_id(conn)
        if run_id is not None:
            names += [t for t, row in db.get_tags(conn, run_id).items() if row["tag"] == "NEW"]
    return [t for t in dict.fromkeys(names) if not is_non_us(t)]


def run_quotes(args):
    """Store today's price and day change for the tracked US names. Only ever stores a CLOSE."""
    _require_db()
    if args.quotes_date:
        state, when = "date given by you", args.quotes_date
    else:
        now = _now_et()
        if now is None:
            sys.exit("The time-zone database is not available, so I cannot tell whether the market is open. "
                     "Give the trading date yourself: --quotes-date YYYY-MM-DD")
        state, when = market_state(now)
        if when is None:
            print(f"The US market is open right now ({now.strftime('%H:%M')} New York time), so Finviz shows a price in progress, "
                  "not a close. Nothing was stored. Run this after 4:00 pm New York time (or before 9:30 am, which gives yesterday's "
                  "close), or force a date with --quotes-date YYYY-MM-DD.")
            return
    conn = db.connect(args.db)
    db.init_schema(conn)
    symbols = quote_targets(conn, args)
    if not symbols:
        print("Nothing to price: no US tickers in positions.yaml or among the NEW candidates.")
        return
    print(f"Quotes: {len(symbols)} US ticker(s), stamped as the close of {when} ({state}).")
    rows, problems = fetch_quote_rows(symbols)
    for t, q in rows.items():
        db.upsert_prices(conn, t, [(when, q["price"], q["change_pct"])], source="finviz_screener")
    missing = [t for t in symbols if t not in rows]
    print(f"\nStored {len(rows)} of {len(symbols)} price(s) for {when}." + (f" Not returned by Finviz: {', '.join(missing)}." if missing else ""))
    movers = sorted(((t, q["change_pct"]) for t, q in rows.items() if q["change_pct"] is not None and abs(q["change_pct"]) >= 5), key=lambda x: -abs(x[1]))
    if movers:
        print("Biggest movers: " + ", ".join(f"{t} {c:+.1f}%" for t, c in movers[:8]))
    for pr in problems:
        print(f"PROBLEM {pr}")
    if rows:
        print("Tier 1 can use these now: python3 sentinel_tier1.py --dry-run")
    conn.close()


# ------------------------------------------------------------------- dossier

def is_non_us(ticker):
    return "." in ticker and ticker.rsplit(".", 1)[1] in NON_US_SUFFIXES


def fetch_quote_html(symbol):
    """One quote page, through the same polite session, retries and backoff as the screener."""
    resp = fetch_with_retry(QUOTE_URL.format(symbol=symbol))
    return resp.text if resp is not None else None


def positions_names(positions_path):
    """(held, watch): the tickers in positions.yaml, upper-cased, in file order."""
    if yaml is None:
        raise RuntimeError("pyyaml is not installed. Run: pip3 install pyyaml")
    with open(positions_path or DEFAULT_POSITIONS, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    names = lambda key: [str(p.get("ticker", "")).strip().upper() for p in raw.get(key) or [] if str(p.get("ticker", "")).strip()]  # noqa: E731
    return names("held"), names("watch")


def choose_dossier_targets(conn, run_id, args):
    """[(ticker, reason)] in fetch order: NEW hits of the run first, then held, then watch.
    --tickers overrides everything. --tracked adds held/watch names that hit this run's screen;
    --held / --watch add every name in positions.yaml, whether or not it hit the screen."""
    if args.tickers:
        return [(db.canonical_symbol(t), "MANUAL") for t in args.tickers.split(",") if t.strip()]
    picked = []
    if run_id is not None:
        tags = db.get_tags(conn, run_id)
        want = {"NEW"} | ({"HELD", "WATCH"} if args.tracked else set())
        picked = [(t, row["tag"]) for t, row in tags.items() if row["tag"] in want]
    if args.held or args.watch:
        held, watch = positions_names(args.positions)
        picked += [(t, "HELD") for t in (held if args.held else [])] + [(t, "WATCH") for t in (watch if args.watch else [])]
    order = {"NEW": 0, "HELD": 1, "WATCH": 2}
    seen, out = set(), []
    for t, reason in sorted(picked, key=lambda x: (order.get(x[1], 3), x[0])):
        if t not in seen:
            seen.add(t)
            out.append((t, reason))
    return out


def _finviz_symbol(conn, run_id, ticker):
    if run_id is not None:
        r = conn.execute("SELECT finviz_symbol FROM screen_results WHERE run_id = ? AND ticker = ?", (run_id, ticker)).fetchone()
        if r:
            return r["finviz_symbol"]
    return ticker


def print_dossier_summary(ticker, data, ok, notes, prev):
    snap = data.get("snapshot", {})
    price, target = finviz_quote.num(snap.get("Price")), finviz_quote.num(snap.get("Target Price"))
    upside = f" ({(target / price - 1) * 100:+.0f}%)" if price and target else ""
    if not ok:
        print(f"    {ticker}: COULD NOT READ THE PAGE. {'; '.join(notes)}")
        return
    prof = data.get("profile") or {}
    if prof:
        print(f"    {ticker}: {prof.get('sector', '?')} / {prof.get('industry', '?')}")
    asof = f" (close {data['quote_asof']})" if data.get("quote_asof") else ""
    print(f"    {ticker}: price {snap.get('Price', '?')}{asof}, target {snap.get('Target Price', '?')}{upside}, "
          f"recom {snap.get('Recom', '?')}, insider trans {snap.get('Insider Trans', '?')}, short float {snap.get('Short Float', '?')}")
    ratings = data.get("ratings", [])
    if ratings:
        r = ratings[0]
        print("      " + _short(f"analysts: {len(ratings)} action(s), latest {r.get('date')} {r.get('action')} {r.get('analyst')} {r.get('rating_change') or ''}", 104).rstrip())
    ins = data.get("insiders", [])
    if ins:
        print("      " + _short(f"insiders: {len(ins)} trade(s), latest {ins[0].get('date', '?')} {ins[0].get('insider_trading', '?')} {ins[0].get('transaction', '?')}", 104))
    news = data.get("news", [])
    if news:
        print("      " + _short(f"news: {len(news)} headline(s), latest {news[0].get('date')} {news[0].get('headline', '')}", 104))
    if data.get("peers"):
        print(f"      peers: {' '.join(data['peers'])}")
    if prev is not None:
        changes = finviz_quote.describe_changes(prev["data"], data)
        print(f"      since {prev['fetched_at'][:10]}: " + ("; ".join(changes) if changes else "no changes"))
    else:
        print("      first snapshot for this name")
    for n in notes:
        print(f"      note: {n}")


def run_dossier_stage(conn, run_id, args):
    """Fetch and store detail pages for the chosen names. Polite by construction: one page at a
    time with a random pause, a cap per invocation, a 7-day cache, and it stops early if two
    pages in a row cannot be read (a sign Finviz is blocking you or has changed its layout)."""
    if finviz_quote is None:
        print("\nDossier stage skipped: sentinel_finviz.py was not found next to this script.")
        return
    targets = choose_dossier_targets(conn, run_id, args)
    if not targets:
        print("\nDossier stage: nothing to fetch (no NEW names" + (", held or watch names" if args.tracked else "")
              + " in this run). Use --held, --watch, --tracked or --tickers to widen it.")
        return

    plan, cached, skipped = [], [], []
    for ticker, reason in targets:
        if is_non_us(ticker):
            skipped.append(ticker)
        elif not args.force and db.recent_dossier(conn, ticker, args.max_age):
            cached.append(ticker)
        else:
            plan.append((ticker, reason))
    cap = args.cap
    print(f"\nDossier stage: {len(plan)} to fetch (cap {cap}), {len(cached)} already fresh (< {args.max_age:g} days)"
          + (f", {len(skipped)} skipped (not US-listed: {', '.join(skipped)})" if skipped else "") + ".")
    if cached:
        print(f"  cached: {', '.join(cached)}")
    if len(plan) > cap:
        print(f"  over the cap; not fetching this time: {', '.join(t for t, _ in plan[cap:])}")
        plan = plan[:cap]

    fails_in_row = fetched = 0
    for i, (ticker, reason) in enumerate(plan):
        if i:
            polite_delay(3.0, 7.0)
        symbol = _finviz_symbol(conn, run_id, ticker)
        print(f"  [{i + 1}/{len(plan)}] {ticker} ({reason})...")
        html = fetch_quote_html(symbol)
        if html is None:
            print(f"    could not fetch {symbol}: no usable response after retries.")
            fails_in_row += 1
        else:
            data, ok, notes = finviz_quote.parse_quote_page(html)
            sha = hashlib.sha1(html.encode("utf-8", "ignore")).hexdigest()
            prev = db.latest_dossier(conn, ticker)
            db.insert_dossier(conn, ticker=ticker, data=data, parse_ok=ok, parse_notes="; ".join(notes) or None,
                              run_id=run_id, reason=reason, html_sha1=sha)
            fetched += 1
            print_dossier_summary(ticker, data, ok, notes, prev)
            if ok:
                fails_in_row = 0
            else:
                fails_in_row += 1
                DEBUG_DIR.mkdir(exist_ok=True)
                path = DEBUG_DIR / f"{ticker}_{date.today().isoformat()}.html"
                path.write_text(html, encoding="utf-8")
                print(f"    saved the page to {path} so it can be inspected")
        if fails_in_row >= 2:
            print("  Stopping early: two pages in a row could not be read. Finviz may be blocking requests or "
                  "have changed its layout. Wait a while, then run sentinel_probe.py on one name.")
            break
    print(f"\nDossier stage done: {fetched} page(s) stored. Check with: python3 sentinel_db.py --info")


def dossier_only(args):
    """Run the dossier stage on a saved run without scanning the screener again."""
    _require_db()
    conn = db.connect(args.db)
    db.init_schema(conn)
    try:
        run_id = db.latest_run_id(conn) if args.dossier_only in (None, "latest") else int(args.dossier_only)
    except ValueError:
        sys.exit("--dossier-only takes a run number (or nothing, for the latest run). For tickers use: --tickers PSN,RBA")
    if run_id is not None and not conn.execute("SELECT 1 FROM screen_runs WHERE run_id = ?", (run_id,)).fetchone():
        sys.exit(f"No such run ({args.dossier_only}). See: python3 sentinel_db.py --info")
    if run_id is None and not (args.tickers or args.held or args.watch):
        sys.exit("There are no saved runs yet. Scan first, or name tickers: --tickers PSN,RBA (or use --held / --watch)")
    if run_id is not None and not args.tickers and not db.get_tags(conn, run_id):
        print(f"Run #{run_id} has no ledger tags yet; tagging it first.")
        tag_run(conn, run_id, args.positions, args.log)
    run_dossier_stage(conn, run_id, args)
    conn.close()


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-db", action="store_true", help="write the CSV only; do not touch research.db")
    ap.add_argument("--import-csv", nargs="+", metavar="FILE", help="load old CSV files as past runs; no scraping")
    ap.add_argument("--db", default=None, help="database file (default: $RESEARCH_DB or research.db beside sentinel_db.py)")
    ap.add_argument("--retag", nargs="?", const="latest", metavar="RUN_ID",
                    help="re-tag a saved run (default: the latest) against your YAML files; no scraping")
    ap.add_argument("--positions", default=None, help="positions.yaml (default: beside this script)")
    ap.add_argument("--log", default=None, help="research_log.yaml (default: beside this script)")
    ap.add_argument("--dossier", action="store_true", help="after the scan, fetch detail pages for the NEW names")
    ap.add_argument("--dossier-only", nargs="?", const="latest", metavar="RUN_ID",
                    help="fetch detail pages for a saved run (default: the latest); no screener scan")
    ap.add_argument("--tracked", action="store_true", help="dossier: also include HELD and WATCH names that hit this screen")
    ap.add_argument("--held", action="store_true", help="dossier: also include every held name in positions.yaml")
    ap.add_argument("--watch", action="store_true", help="dossier: also include every watch-list name in positions.yaml")
    ap.add_argument("--tickers", default=None, help="dossier: exactly these tickers, comma-separated (e.g. PSN,RBA)")
    ap.add_argument("--cap", type=int, default=DEFAULT_CAP, help=f"dossier: most pages per invocation (default {DEFAULT_CAP})")
    ap.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_DAYS, help=f"dossier: reuse a snapshot newer than this many days (default {DEFAULT_MAX_AGE_DAYS:g})")
    ap.add_argument("--force", action="store_true", help="dossier: ignore the cache and fetch again")
    ap.add_argument("--quotes", action="store_true", help="store today's price and day change for tracked US names and NEW candidates")
    ap.add_argument("--quotes-date", default=None, metavar="YYYY-MM-DD", help="quotes: stamp the prices with this trading date instead of working it out")
    args = ap.parse_args()

    if args.import_csv:
        import_csvs(args.import_csv, args.db)
        return
    if args.retag is not None:
        retag(args.retag, args.db, args.positions, args.log)
        return
    if args.dossier_only is not None:
        dossier_only(args)
        return
    if args.quotes:
        run_quotes(args)
        return

    print(f"Filters: {extract_filters(VIEWS['Overview'])}\n")
    dfs = {}
    view_names = list(VIEWS.items())
    for i, (name, url) in enumerate(view_names):
        dfs[name] = fetch_view(name, url)
        if i < len(view_names) - 1:
            polite_delay(low=3.0, high=7.0)  # longer pause between views

    if dfs["Overview"] is None:
        sys.exit("\nCouldn't fetch the Overview view -- check the URLs or "
                 "try again later.")

    merged = dfs["Overview"]
    views_ok = ["Overview"]
    for name in ["Valuation", "Financial", "Ownership", "Performance"]:
        df = dfs.get(name)
        if df is None:
            print(f"Warning: {name} view failed, skipping its columns.")
            continue
        views_ok.append(name)
        # Keep only new columns from this view -- avoid duplicating
        # Market Cap / Price / Change / Volume across every view.
        new_cols = [c for c in df.columns if c not in merged.columns or c == "Ticker"]
        merged = merged.merge(df[new_cols], on="Ticker", how="left")

    print(f"\n{len(merged)} candidates found, {len(merged.columns)} columns merged:\n")
    print(merged.to_string(index=False))

    out_path = f"candidates_{date.today().isoformat()}.csv"
    merged.to_csv(out_path, index=False)
    print(f"\nSaved to {out_path}")

    odd = [t for t in merged["Ticker"] if not (isinstance(t, str) and TICKER_RE.match(t))]
    if odd:
        print(f"WARNING: {len(odd)} ticker(s) don't look like real symbols and may be parsing artifacts: "
              f"{', '.join(map(str, odd))}. They are saved as-is; check them before relying on them.")

    if not args.no_db:
        save_run_to_db(merged, views_ok, out_path, args)


if __name__ == "__main__":
    main()

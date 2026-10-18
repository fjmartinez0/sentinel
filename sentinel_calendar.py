"""
Earnings calendar with position-aware decision cards.

What changed vs the first version:
  * Holdings and the watch list come from positions.yaml, not a hardcoded dict.
  * Click a held name and you get a card: the print-day test (gate), position and
    P&L, target and resting GTC order, ledger date vs FMP date, consensus, and
    anything in the ledger that looks inconsistent.
  * "Recently reported" applies your locked Funnel A rule (EPS or revenue surprise
    of 10% or more) to the last 30 days of prints.
  * Ledger-only gates (Analyst Day, trial window...) appear on the calendar too, and
    are flagged once they pass without a recorded outcome.
  * If research.db exists (see sentinel_db.py), the page also shows the scanner's NEW candidates and
    their earnings dates, a "changed since the last Finviz snapshot" badge on held, watch and
    candidate names, and warns when FMP moves a date. Each run then saves its own history
    (dates, prices, a copy of positions.yaml) back to the database, and keeps a table of daily
    closes for every tracked name, which sentinel_tier1.py needs to measure one-day moves. With no
    database, or with --share / --no-db, the page is exactly as before. A database problem never blocks the page.

Earnings data comes from Alpha Vantage since Oct 2026 (FMP made its earnings calendar a paid feature;
DECISIONS.md 52): one request a day for every US company's expected earnings over the next 3 months, plus one
request per tracked name that has just reported, for its actual EPS against the estimate. If Alpha Vantage is
down or the daily limit is reached, the page is still made from the dates the last run stored, and the console
says WARNING (the pipeline then reports WARN, not FAIL). FMP is now only used by the opt-in --fmp-* options.

Setup:
  python3 sentinel_pipeline.py av-key          # stores ALPHAVANTAGE_API_KEY in .env (hidden prompt)
  pip3 install requests pandas lxml pyyaml

Run:
  python3 sentinel_calendar.py                 # normal
  python3 sentinel_calendar.py --fmp-quotes    # also try FMP live quotes (most plans only serve a few symbols)
  python3 sentinel_calendar.py --share         # S&P 500 only, NO positions data, never touches research.db
  python3 sentinel_calendar.py --no-db         # ignore research.db (read and write)
  python3 sentinel_calendar.py --fmp-history   # also fetch daily closes from FMP (see sentinel_tier1.py; Finviz is the default source)
  python3 sentinel_calendar.py --fmp-check     # ~12 calls: which FMP endpoints and symbols your plan allows
  python3 sentinel_calendar.py --offline       # fixture data, no network, no API key
  python3 sentinel_calendar.py --av-check      # 2 Alpha Vantage requests: is the key working, what does it return

Output:
  earnings_calendar.html -- contains cost basis and share counts. Local use only.
  The API key is never written into the HTML.
"""

import argparse
import html
import io
import json
import math
import os
import sys
from calendar import monthrange
import hashlib
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:
    raise SystemExit("Missing dependency. Run: pip3 install pyyaml")

try:
    import sentinel_db as db       # optional: research.db access, sits next to this file
except ImportError:
    db = None
try:
    import sentinel_finviz as finviz_quote  # optional: only for describing what changed between two dossiers
except ImportError:
    finviz_quote = None

BASE = "https://financialmodelingprep.com"

# Locked in the decisions log (item 8). Surprise % on a tiny EPS base is noisy
# (0.05 -> 0.10 is +100%), which is why this only flags for a human/Tier 2 look.
SURPRISE_THRESHOLD_PCT = 10.0
LOOKBACK_DAYS = 30          # how far back the "recently reported" list looks
AV_URL = "https://www.alphavantage.co/query"
AV_MAX_ACTUALS = 6          # at most this many per-company result requests per run (free tier: ~25 requests a day)
AV_ACTUALS_DAYS = 5         # look for results of prints in the last N days
AV_DELAY = 13               # seconds between Alpha Vantage requests (the free tier also limits requests per minute)
STALE_REVIEW_DAYS = 21      # a position's last_reviewed older than this gets a note
STALE_LEDGER_DAYS = 14      # positions.yaml as_of older than this gets a banner
GTC_TOLERANCE = 0.005       # 0.5%: GTC price vs target counts as "matching"
SCREEN_STALE_DAYS = 10      # a scanner run older than this gets a warning banner
CURRENCY_SYMBOL = {"USD": "$", "CAD": "C$"}


# ---------------------------------------------------------------- formatting

def iso(v):
    """YAML turns unquoted 2026-08-28 into a date object; normalise to a string."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def to_float(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def money(v, cur="USD"):
    if v is None:
        return "n/a"
    sym = CURRENCY_SYMBOL.get(cur, cur + " ")
    return f"{sym}{v:,.0f}" if float(v).is_integer() else f"{sym}{v:,.2f}"


def fmt_target(t, cur):
    if t is None:
        return None
    if isinstance(t, (list, tuple)):
        return f"{money(t[0], cur)} to {money(t[1], cur)}"
    return money(t, cur)


def target_ref(t):
    """The number we measure upside against: the point target, or the low end of a range."""
    if t is None:
        return None
    return t[0] if isinstance(t, (list, tuple)) else t


def human(v):
    if v is None:
        return "n/a"
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.0f}M"
    return f"{v:,.2f}"


def surprise_pct(actual, est):
    if actual is None or est is None or est == 0:
        return None
    return (actual - est) / abs(est) * 100


def days_from(d_iso, today):
    return (date.fromisoformat(d_iso) - today).days


def when_text(n):
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    return f"in {n} days" if n > 0 else f"{-n} days ago"


def short_date(d_iso):
    d = date.fromisoformat(d_iso)
    return f"{d.strftime('%b')} {d.day}"


# ------------------------------------------------------------- positions.yaml

def load_positions(path):
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    out = {
        "as_of": iso(raw.get("as_of")),
        "held": [],
        "watch": [],
        "needs_confirmation": raw.get("needs_confirmation") or [],
    }
    problems, seen = [], set()

    for kind in ("held", "watch"):
        for p in raw.get(kind) or []:
            p = dict(p)
            p["kind"] = kind
            tk = str(p.get("ticker", "")).strip().upper()
            if not tk:
                problems.append(f"a {kind} entry has no ticker")
                continue
            if tk in seen:
                problems.append(f"{tk} appears more than once")
            seen.add(tk)
            p["ticker"] = tk
            p["name"] = p.get("name") or tk
            e = dict(p.get("earnings") or {})
            e["date"] = iso(e.get("date"))
            e["month"] = str(e["month"]) if e.get("month") else None
            p["earnings"] = e
            p["last_price_date"] = iso(p.get("last_price_date"))
            p["last_reviewed"] = iso(p.get("last_reviewed"))
            evs = []
            for ev in p.get("events") or []:
                ev = dict(ev)
                ev["date"] = iso(ev.get("date"))
                ev["until"] = iso(ev.get("until"))
                ev["month"] = str(ev["month"]) if ev.get("month") else None
                evs.append(ev)
            p["events"] = evs
            if kind == "held":
                for k in ("shares", "avg_cost", "currency"):
                    if p.get(k) is None:
                        problems.append(f"{tk}: held entry is missing '{k}'")
            out[kind].append(p)

    if problems:
        raise SystemExit("positions.yaml problems:\n  - " + "\n  - ".join(problems))
    return out


# --------------------------------------------------------------- data sources

def fmp_key():
    key = os.environ.get("FMP_API_KEY")
    if not key:
        raise SystemExit("Set the FMP_API_KEY environment variable first (or use --offline).")
    return key


def fetch_sp500_constituents():
    """Free, public source -- FMP's own constituent endpoint requires a paid plan."""
    import pandas as pd
    import requests
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()
    df = pd.read_html(io.StringIO(resp.text))[0]
    return dict(zip(df["Symbol"], df["Security"]))


class AvError(RuntimeError):
    """An Alpha Vantage problem, described WITHOUT the request URL (the URL contains the API key)."""


def av_key():
    """ALPHAVANTAGE_API_KEY from the environment, or from .env beside this file (so a manual run works too)."""
    key = os.environ.get("ALPHAVANTAGE_API_KEY")
    envf = Path(__file__).with_name(".env")
    if not key and envf.exists():
        for ln in envf.read_text(encoding="utf-8").splitlines():
            if ln.strip().startswith("ALPHAVANTAGE_API_KEY="):
                key = ln.split("=", 1)[1].strip().strip('"').strip("'")
    return key or None


def _av_get(params, key):
    """One Alpha Vantage request. Returns the body text. Never lets the URL (and so the key) into an error."""
    import requests
    try:
        r = requests.get(AV_URL, params={**params, "apikey": key}, timeout=60)
    except Exception as e:  # noqa: BLE001
        raise AvError(f"could not reach Alpha Vantage ({type(e).__name__})") from None
    if r.status_code != 200:
        raise AvError(f"Alpha Vantage answered HTTP {r.status_code}")
    body = r.text or ""
    if body.lstrip().startswith("{"):
        try:
            j = json.loads(body)
        except ValueError:
            j = None
        if isinstance(j, dict):
            for k in ("Error Message", "Information", "Note"):
                if j.get(k):
                    raise AvError(f"Alpha Vantage: {' '.join(str(j[k]).split())[:160]}")
    return body


def _av_num(v):
    v = (v or "").strip() if isinstance(v, str) else v
    return None if v in (None, "", "None", "-") else to_float(v)


def fetch_av_calendar(key, horizon="3month"):
    """Every US company's expected earnings for the next 3 months, as FMP-shaped rows
    ({symbol, date, time, epsEstimated, confirmed})."""
    import csv
    body = _av_get({"function": "EARNINGS_CALENDAR", "horizon": horizon}, key)
    rows = list(csv.DictReader(io.StringIO(body)))
    if not rows:
        raise AvError("Alpha Vantage returned an empty earnings calendar")
    cols = {c.lower(): c for c in rows[0].keys() if c}
    if "symbol" not in cols or "reportdate" not in cols:
        raise AvError(f"unexpected earnings-calendar columns: {', '.join(list(cols.values())[:8])}")
    tcol = next((cols[c] for c in cols if "time" in c), None)
    out = []
    for r in rows:
        sym, d = (r.get(cols["symbol"]) or "").strip().upper(), (r.get(cols["reportdate"]) or "").strip()[:10]
        if not sym or len(d) != 10:
            continue
        tm = (r.get(tcol) or "").strip().lower() if tcol else ""
        out.append({"symbol": sym, "date": d, "time": "bmo" if tm.startswith("pre") else "amc" if tm.startswith("post") else "",
                    "epsEstimated": _av_num(r.get(cols["estimate"])) if "estimate" in cols else None, "confirmed": False})
    return out


def fetch_av_actual(key, symbol, since_iso, until_iso):
    """The newest quarterly result reported between the two dates, FMP-shaped
    ({symbol, date, epsEstimated, epsActual}), or None if it has not been published yet."""
    body = _av_get({"function": "EARNINGS", "symbol": symbol}, key)
    try:
        q = json.loads(body).get("quarterlyEarnings") or []
    except (ValueError, AttributeError):
        raise AvError(f"unexpected earnings-history answer for {symbol}") from None
    for row in q:
        d = str(row.get("reportedDate") or "")[:10]
        if since_iso <= d <= until_iso and _av_num(row.get("reportedEPS")) is not None:
            return {"symbol": symbol, "date": d, "epsEstimated": _av_num(row.get("estimatedEPS")),
                    "epsActual": _av_num(row.get("reportedEPS")), "confirmed": True}
    return None


def _stored_rows(conn):
    """The last calendar run's saved events, FMP-shaped, so they can be reused."""
    if conn is None or db is None:
        return []
    try:
        return [{"symbol": e["ticker"], "date": e["event_date"], "time": e["fmp_time"] or "", "confirmed": bool(e["fmp_confirmed"]),
                 "epsEstimated": e["eps_est"], "epsActual": e["eps_act"], "revenueEstimated": e["rev_est"],
                 "revenueActual": e["rev_act"]} for e in db.latest_calendar_events(conn)]
    except Exception:  # noqa: BLE001
        return []


def get_earnings_rows(key, today, tracked, conn, sleep=None):
    """Earnings rows for the page: Alpha Vantage's upcoming calendar, plus actual results for tracked names that
    reported in the last few days, plus the past events the last run stored. Returns (rows, warnings).
    A warning means the page used stored data for part of it; it never raises for a data-source problem."""
    import time
    sleep = sleep or time.sleep
    warnings, today_iso = [], today.isoformat()
    stored = _stored_rows(conn)
    lookback = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    calls = 0
    try:
        upcoming = [r for r in fetch_av_calendar(key) if r["date"] >= today_iso]
        calls += 1
    except AvError as e:
        upcoming = [r for r in stored if r["date"] >= today_iso]
        run = db.latest_calendar_run(conn) if (conn is not None and db is not None) else None
        warnings.append(f"earnings source unavailable ({e}); using the {len(upcoming)} upcoming date(s) stored "
                        + (f"by the calendar run of {run['as_of_date']}" if run else "earlier (none stored)"))
    rows = {}
    for r in [r for r in stored if lookback <= r["date"] < today_iso] + upcoming:     # later sources fill gaps, never erase
        k = (r["symbol"], r["date"])
        rows[k] = {**r, **{f: v for f, v in rows.get(k, {}).items() if v not in (None, "")}} if k in rows else dict(r)
    # results for tracked names that reported in the last few days (or today) and have no actual yet
    since = (today - timedelta(days=AV_ACTUALS_DAYS)).isoformat()
    due = sorted({s for (s, d), r in rows.items() if s in tracked and since <= d <= today_iso
                  and r.get("epsActual") is None and not _non_us(s)})
    if due and not warnings:
        for sym in due[:AV_MAX_ACTUALS]:
            if calls:
                sleep(AV_DELAY)
            try:
                got = fetch_av_actual(key, sym, since, today_iso)
                calls += 1
            except AvError as e:
                warnings.append(f"could not fetch {sym}'s result ({e})")
                break
            if not got:
                continue
            near = [k for k in rows if k[0] == sym and abs((date.fromisoformat(k[1]) - date.fromisoformat(got["date"])).days) <= 3]
            base = rows.pop(near[0]) if near else {}
            for k in near[1:]:
                rows.pop(k, None)
            rows[(sym, got["date"])] = {**base, **{f: v for f, v in got.items() if v is not None}}
        if len(due) > AV_MAX_ACTUALS:
            warnings.append(f"{len(due) - AV_MAX_ACTUALS} result(s) left for the next run (at most {AV_MAX_ACTUALS} per run)")
    return sorted(rows.values(), key=lambda r: (r["date"], r["symbol"])), warnings


def av_check(key, today):
    """Two real requests: the calendar and one company's history. Prints what came back, never the key."""
    if not key:
        raise SystemExit("No ALPHAVANTAGE_API_KEY. Run:  python3 sentinel_pipeline.py av-key")
    try:
        cal = fetch_av_calendar(key)
        print(f"  ok    earnings calendar: {len(cal)} upcoming report(s), first on {min(r['date'] for r in cal)}")
    except AvError as e:
        print(f"  FAIL  earnings calendar: {e}")
        return 1
    import time
    time.sleep(AV_DELAY)
    try:
        got = fetch_av_actual(key, "IBM", (today - timedelta(days=400)).isoformat(), today.isoformat())
        print(f"  ok    earnings history (IBM): latest result {got['date']}, EPS {got['epsActual']} vs estimate {got['epsEstimated']}"
              if got else "  ok    earnings history (IBM): answered, no result in the last year")
    except AvError as e:
        print(f"  FAIL  earnings history: {e}")
        return 1
    print("Alpha Vantage works. 2 of your daily requests were used.")
    return 0


def fetch_earnings_calendar(key, from_date, to_date):
    import requests
    resp = requests.get(
        f"{BASE}/stable/earnings-calendar",
        params={"from": from_date, "to": to_date, "apikey": key},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


NON_US_SUFFIXES = {"TO", "V", "CN", "NE"}      # FMP plans often cover US listings only


def _non_us(sym):
    return "." in sym and sym.rsplit(".", 1)[1] in NON_US_SUFFIXES


def _why(r):
    """FMP's own refusal text (it says why), trimmed to one line. Never includes the API key."""
    txt = " ".join((getattr(r, "text", "") or "").split())
    return f"HTTP {r.status_code}" + (f": {txt[:110]}" if txt else "")


def fetch_quotes(key, symbols):
    """Live prices for held names. Free-tier coverage is UNTESTED (see --fmp-check). US listings are tried
    first, and only US refusals count toward giving up (a plan that refuses TSX tickers may still serve US
    ones). Anything that fails falls back to the ledger price, with FMP's own reason shown."""
    import requests
    out, errs, denied = {}, [], 0
    for s in sorted(symbols, key=_non_us):            # stable sort: US first, file order kept within each group
        try:
            r = requests.get(f"{BASE}/stable/quote", params={"symbol": s, "apikey": key}, timeout=15)
            if r.status_code in (402, 403):
                errs.append((s, _why(r)))
                denied += 0 if _non_us(s) else 1
                if denied >= 3 and not out:
                    print("  Quote endpoint looks unavailable on this plan for US tickers; using ledger prices.")
                    break
                continue
            r.raise_for_status()
            data = r.json()
            q = data[0] if isinstance(data, list) and data else None
            price = to_float(q.get("price")) if q else None
            if price is None:
                errs.append((s, "no data"))
                continue
            chg = q.get("changePercentage", q.get("changesPercentage"))
            out[s] = {"price": price, "change_pct": to_float(chg)}
        except Exception as e:  # noqa: BLE001 -- a bad quote must never kill the calendar
            errs.append((s, str(e)[:60]))
    return out, errs


def fmp_check(key, today=None):
    """One-off diagnostic: a dozen small calls that show which FMP endpoints and symbols your plan allows,
    with FMP's own words when it refuses. Prints (and returns) the report; the key is never printed."""
    import requests
    today = today or date.today()
    d0, d1 = (today - timedelta(days=10)).isoformat(), today.isoformat()
    tests = [("earnings-calendar", {"from": d0, "to": d1}, "the one endpoint the calendar page depends on")]
    for sym in ("AAPL", "NYT", "QTWO", "ATRL.TO"):
        tests.append(("quote", {"symbol": sym}, "live price"))
    tests += [("quote-short", {"symbol": "AAPL"}, "lighter live price"), ("batch-quote", {"symbols": "AAPL,NYT,QTWO"}, "several prices in one call")]
    for sym in ("AAPL", "NYT", "QTWO", "ATRL.TO"):
        tests.append(("historical-price-eod/light", {"symbol": sym, "from": d0, "to": d1}, "daily closes"))
    tests.append(("historical-price-eod/full", {"symbol": "AAPL", "from": d0, "to": d1}, "daily closes, full"))
    lines = [f"FMP PLAN CHECK, {today.isoformat()}", f"  {'endpoint':<27}{'symbol':<12}{'result'}"]
    ok = {}
    for path, params, _note in tests:
        label = params.get("symbol") or params.get("symbols") or "-"
        try:
            r = requests.get(f"{BASE}/stable/{path}", params={**params, "apikey": key}, timeout=20)
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    data = None
                rows = data.get("historical", data) if isinstance(data, dict) else data
                if isinstance(rows, list) and rows:
                    keys = ", ".join(list(rows[0].keys())[:6]) if isinstance(rows[0], dict) else ""
                    res = f"OK, {len(rows)} row(s); fields: {keys}"
                    ok[(path, label)] = True
                else:
                    res = "HTTP 200 but no rows" + (f": {str(data)[:80]}" if data not in (None, [], {}) else "")
            else:
                res = _why(r)
        except Exception as e:  # noqa: BLE001
            res = f"error: {str(e)[:80]}"
        lines.append(f"  {path:<27}{label:<12}{res}")
    q_us = [ok.get(("quote", s)) for s in ("AAPL", "NYT", "QTWO")]
    h_us = [ok.get(("historical-price-eod/light", s)) for s in ("AAPL", "NYT", "QTWO")]
    lines.append("\nWhat this suggests:")
    lines.append("  earnings calendar: " + ("works" if ok.get(("earnings-calendar", "-")) else "NOT working: the calendar page itself will fail"))
    lines.append("  live quotes: " + ("all three US names answered" if all(q_us) else "some US names answered (plan may limit which symbols)" if any(q_us)
                                     else "none of AAPL, NYT, QTWO answered: quotes look restricted on this plan"))
    lines.append("  daily closes: " + ("all three US names answered" if all(h_us) else "some US names answered (plan may limit which symbols)" if any(h_us)
                                      else "none of AAPL, NYT, QTWO answered: daily history looks restricted on this plan"))
    lines.append("  Send this report back and I will choose the price source accordingly.")
    report = "\n".join(lines)
    print(report)
    out_dir = Path(__file__).resolve().parent / "probe_out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "fmp_check.txt").write_text(report + "\n", encoding="utf-8")
    print(f"\nSaved: {out_dir / 'fmp_check.txt'}")
    return report


HISTORY_DAYS = 45           # how far back the first daily-close fetch goes; later runs only top up


def fetch_price_history(key, symbols, from_dates, to_date):
    """Daily closes from FMP's end-of-day endpoint: ({ticker: [(YYYY-MM-DD, close)]}, errors). Whether your
    plan includes this endpoint is UNTESTED (per the FMP lesson), so a refusal is reported, never fatal.
    Both response shapes are accepted: a plain list, or {"historical": [...]}; the close may be called
    close, price or adjClose."""
    import requests
    out, errs, denied = {}, [], 0
    for s in sorted(symbols, key=_non_us):            # US first; only US refusals count toward giving up
        try:
            r = requests.get(f"{BASE}/stable/historical-price-eod/light",
                             params={"symbol": s, "from": from_dates[s], "to": to_date, "apikey": key}, timeout=20)
            if r.status_code in (402, 403):
                errs.append((s, _why(r)))
                denied += 0 if _non_us(s) else 1
                if denied >= 3 and not out:
                    print("  Price-history endpoint looks unavailable on this plan for US tickers; skipping the rest.")
                    break
                continue
            r.raise_for_status()
            data = r.json()
            rows = data.get("historical", []) if isinstance(data, dict) else data
            parsed = []
            for row in rows or []:
                d = str(row.get("date") or "")[:10]
                close = next((to_float(row[k]) for k in ("close", "price", "adjClose") if row.get(k) is not None), None)
                if len(d) == 10 and close is not None:
                    parsed.append((d, close))
            if parsed:
                out[s] = sorted(parsed)
            else:
                errs.append((s, "no rows"))
        except Exception as e:  # noqa: BLE001 -- one bad ticker must never stop the rest
            errs.append((s, str(e)[:60]))
    return out, errs


def refresh_price_history(conn, key, today, symbols):
    """Top up price_history for the tracked names. A name whose newest close is under 3 days old is skipped,
    so running the calendar twice in a day costs no extra FMP calls."""
    todo, from_dates = [], {}
    for s in symbols:
        last = db.latest_price_date(conn, s)
        if last and last >= (today - timedelta(days=2)).isoformat():
            continue
        from_dates[s] = (date.fromisoformat(last) - timedelta(days=3)).isoformat() if last else (today - timedelta(days=HISTORY_DAYS)).isoformat()
        todo.append(s)
    fetched, errs = fetch_price_history(key, todo, from_dates, today.isoformat()) if todo else ({}, [])
    new_closes = sum(db.upsert_prices(conn, s, rows) for s, rows in fetched.items())
    return {"todo": len(todo), "ok": len(fetched), "fresh": len(symbols) - len(todo), "new_closes": new_closes, "errs": errs}


def end_of_month_two_ahead(today):
    """Last calendar day of (current month + 2), so 3 full months are covered."""
    month, year = today.month + 2, today.year
    while month > 12:
        month -= 12
        year += 1
    return date(year, month, monthrange(year, month)[1])


# Offline fixtures: NOT real FMP data. They exist so the page can be previewed and
# tested without a key or a network. The page carries a visible banner when used.
DEMO_SP500 = {"AAPL": "Apple", "MSFT": "Microsoft", "NKE": "Nike", "COST": "Costco",
              "ORCL": "Oracle", "FDX": "FedEx", "ADBE": "Adobe", "MU": "Micron"}


def demo_calendar(today):
    y = today.year
    rows = [
        {"symbol": "COST", "date": f"{y}-09-24", "epsEstimated": 5.10, "revenueEstimated": 8.6e10},
        {"symbol": "NKE", "date": f"{y}-09-25", "epsEstimated": 0.62, "revenueEstimated": 1.15e10},
        {"symbol": "MU", "date": f"{y}-09-29", "epsEstimated": 2.80, "revenueEstimated": 9.2e9},
        {"symbol": "ASML", "date": f"{y}-10-14", "epsEstimated": 6.90, "revenueEstimated": 8.1e9, "confirmed": True},
        {"symbol": "LBRT", "date": f"{y}-10-21", "epsEstimated": 0.18, "revenueEstimated": 1.0e9},
        {"symbol": "QTWO", "date": f"{y}-10-29", "epsEstimated": 0.85, "revenueEstimated": 2.1e8},
        {"symbol": "META", "date": f"{y}-10-29", "epsEstimated": 6.20, "revenueEstimated": 5.9e10},
        {"symbol": "SPGI", "date": f"{y}-10-30", "epsEstimated": 4.40, "revenueEstimated": 3.7e9},
        {"symbol": "PSN", "date": f"{y}-11-04", "epsEstimated": 0.55, "revenueEstimated": 1.6e9},
        # ledger says Nov 4 (confirmed); FMP disagrees by a day -> should raise a check
        {"symbol": "DUOL", "date": f"{y}-11-05", "epsEstimated": 0.60, "revenueEstimated": 2.6e8},
        {"symbol": "HUBS", "date": f"{y}-11-05", "epsEstimated": 2.35, "revenueEstimated": 8.0e8},
        {"symbol": "UBER", "date": f"{y}-11-05", "epsEstimated": 0.70, "revenueEstimated": 1.4e10},
        {"symbol": "NYT", "date": f"{y}-11-05", "epsEstimated": 0.62, "revenueEstimated": 6.9e8},
        # a held name that reported recently, with a >= 10% surprise
        {"symbol": "ALB", "date": (today - timedelta(days=8)).isoformat(),
         "epsEstimated": 0.80, "epsActual": 0.95, "revenueEstimated": 1.4e9, "revenueActual": 1.43e9},
        # a watch name that reported recently, inside the threshold
        {"symbol": "APP", "date": (today - timedelta(days=12)).isoformat(),
         "epsEstimated": 2.10, "epsActual": 2.14, "revenueEstimated": 1.9e9, "revenueActual": 1.92e9},
    ]
    # ATRL.TO / EFN.TO deliberately absent, to exercise the "no FMP date" list.
    return rows


DEMO_QUOTES = {
    "DUOL": {"price": 132.10, "change_pct": -1.2},
    "META": {"price": 611.20, "change_pct": 0.8},
    "LBRT": {"price": 21.05, "change_pct": 2.1},
}


# ------------------------------------------------------------------- analysis

def position_flags(pos, quote, today):
    """Checks that belong to the POSITION (independent of which earnings row we show)."""
    flags = []
    cur = pos.get("currency", "USD")
    if pos["kind"] == "held":
        price = quote["price"] if quote else pos.get("last_price")
        if price is None:
            flags.append(("warn", "No price on file, so P&L and distance to target are unknown."))

        gtc, shares, tgt = pos.get("gtc"), float(pos["shares"]), pos.get("target")
        whole = math.floor(shares + 1e-9)
        if gtc is None:
            if abs(shares - round(shares)) > 1e-9:
                flags.append(("warn", "No resting exit order: fractional shares can't carry a GTC here, so this exit is manual."))
            else:
                flags.append(("warn", "No resting GTC order on file."))
        else:
            gp = float(gtc["price"])
            if isinstance(tgt, (list, tuple)):
                lo, hi = tgt
                if gp < lo * (1 - GTC_TOLERANCE) or gp > hi * (1 + GTC_TOLERANCE):
                    flags.append(("warn", f"GTC at {money(gp, cur)} sits outside the ledger target range ({fmt_target(tgt, cur)})."))
            elif tgt:
                if abs(gp - tgt) / tgt > GTC_TOLERANCE:
                    flags.append(("warn", f"GTC at {money(gp, cur)} differs from target {money(tgt, cur)} ({(gp / tgt - 1) * 100:+.1f}%)."))
            if float(gtc["shares"]) < whole - 1e-9:
                flags.append(("warn", f"GTC covers {gtc['shares']:g} of {whole} whole shares."))

        if not pos.get("gate"):
            flags.append(("info", "No print-day test written yet. Add a gate to positions.yaml before this reports."))

    if pos.get("last_reviewed") is None:
        flags.append(("info", "No review date on file."))
    return flags


def date_flag(pos, next_ev):
    """Ledger date vs FMP date. FMP's own 'confirmed' field is not proof (see handoff
    section 4), so a disagreement means: go check the company's IR page."""
    if next_ev is None:
        return None
    e, fmp = pos["earnings"], next_ev["date"]
    if e.get("date") and fmp != e["date"]:
        return ("warn", f"Ledger has {e['date']} ({e.get('confidence', 'unstated')}); the earnings source has {fmp}. "
                        "Re-verify against the company IR page before acting.")
    if not e.get("date") and e.get("month") and fmp[:7] != e["month"]:
        return ("warn", f"Ledger expects {e['month']}; the earnings source has {fmp}. Re-verify against the IR page.")
    return None


def build_card(pos, ev, next_ev, quote, today):
    cur = pos.get("currency", "USD")
    rows, flags = [], []

    if pos["kind"] == "held":
        price = quote["price"] if quote else pos.get("last_price")
        if quote:
            lab = quote.get("label")           # a stored daily close names its source and date; a live quote has no label
            src = (lab or "live") + (f", {quote['change_pct']:+.1f}% {'that day' if lab else 'today'}" if quote.get("change_pct") is not None else "")
        elif price is not None:
            note = pos.get("last_price_note")
            src = f"ledger, {short_date(pos['last_price_date'])}" + (f", {note}" if note else "") if pos.get("last_price_date") else "ledger"
        else:
            src = None

        rows.append(["Position", f"{pos['shares']:g} sh at {money(pos['avg_cost'], cur)}, {pos['account']}"])
        rows.append(["Price", f"{money(price, cur)} ({src})" if price is not None else "not on file"])
        if price is not None:
            pnl = (price / pos["avg_cost"] - 1) * 100
            rows.append(["Value / P&L", f"{money(price * pos['shares'], cur)}, {pnl:+.1f}%"])
        tgt = pos.get("target")
        if tgt is not None:
            line = fmt_target(tgt, cur)
            ref = target_ref(tgt)
            if price is not None and ref:
                line += f", {(ref / price - 1) * 100:+.1f}% from price" + (" (to low end)" if isinstance(tgt, (list, tuple)) else "")
            if pos.get("stretch"):
                line += f"; stretch {fmt_target(pos['stretch'], cur)}"
            rows.append(["Target", line])
        gtc = pos.get("gtc")
        rows.append(["Resting exit", f"GTC {money(gtc['price'], cur)} x {gtc['shares']:g}" if gtc else "none"])
        if pos.get("gtc_note"):
            rows.append(["Order note", pos["gtc_note"]])
    else:
        rows.append(["Status", pos.get("status") or "Watch"])

    e = pos["earnings"]
    led = e.get("date") or e.get("month") or e.get("note") or "no date on file"
    if e.get("note") and (e.get("date") or e.get("month")):
        led += f" ({e['note']})"
    rows.append(["Ledger date", f"{led}, {e.get('confidence', 'unstated')}"])
    rows.append(["Source date", f"{ev['date']}, {'confirmed' if ev['confirmed'] else 'estimated'} (not proof)"])

    if ev["eps_est"] is not None or ev["rev_est"] is not None:
        rows.append(["Consensus", est_text(ev["eps_est"], ev["rev_est"])])
    s_eps, s_rev = surprise_pct(ev["eps_act"], ev["eps_est"]), surprise_pct(ev["rev_act"], ev["rev_est"])
    if ev["eps_act"] is not None or ev["rev_act"] is not None:
        rows.append(["Result", result_text(ev)])
        hit = [abs(s) >= SURPRISE_THRESHOLD_PCT for s in (s_eps, s_rev) if s is not None]
        if any(hit):
            flags.append(("warn", f"Surprise at or above {SURPRISE_THRESHOLD_PCT:.0f}%: escalate to Tier 2 under the locked rule."))
        else:
            flags.append(("info", f"Surprise below {SURPRISE_THRESHOLD_PCT:.0f}%: log only."))

    if pos.get("last_reviewed"):
        age = (today - date.fromisoformat(pos["last_reviewed"])).days
        rows.append(["Last reviewed", f"{pos['last_reviewed']}, {age} days ago" + (" (stale)" if age > STALE_REVIEW_DAYS else "")])

    if ev is next_ev:
        df = date_flag(pos, next_ev)
        if df:
            flags.append(df)
    flags += position_flags(pos, quote, today)

    return {
        "gate": pos.get("gate"),
        "rows": rows,
        "flags": [{"level": lv, "text": tx} for lv, tx in flags],
        "open_items": pos.get("open_items") or [],
    }


def est_text(eps, rev):
    parts = []
    if eps is not None:
        parts.append(f"EPS est. {eps:.2f}")
    if rev is not None:
        parts.append(f"revenue est. {human(rev)}")
    return ", ".join(parts)


def result_text(ev):
    parts = []
    s = surprise_pct(ev["eps_act"], ev["eps_est"])
    if ev["eps_act"] is not None:
        parts.append(f"EPS {ev['eps_act']:.2f}" + (f" vs {ev['eps_est']:.2f} est. ({s:+.1f}%)" if s is not None else ""))
    s = surprise_pct(ev["rev_act"], ev["rev_est"])
    if ev["rev_act"] is not None:
        parts.append(f"revenue {human(ev['rev_act'])}" + (f" vs {human(ev['rev_est'])} est. ({s:+.1f}%)" if s is not None else ""))
    return ", ".join(parts)


# ------------------------------------------------------------------- research.db

def _days(n):
    return f"{n} day" if n == 1 else f"{n} days"


def _age_days(ts, today):
    try:
        return (today - datetime.fromisoformat(ts).date()).days
    except (TypeError, ValueError):
        return None


def load_db_context(conn, today, tracked):
    """Everything the page reads from research.db. Read-only. A missing piece just means nothing to show."""
    ctx = {"screen": None, "candidates": [], "dossiers": {}, "prev_calendar": None, "prices": {}}
    run = conn.execute("SELECT * FROM screen_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    if run:
        tags = db.get_tags(conn, run["run_id"])
        ctx["screen"] = {"run_id": run["run_id"], "run_at": run["run_at"], "n_hits": run["n_hits"],
                         "age": _age_days(run["run_at"], today), "untagged": not tags}
        names = {r["ticker"]: (json.loads(r["row_json"]).get("Company") or r["ticker"])
                 for r in conn.execute("SELECT ticker, row_json FROM screen_results WHERE run_id = ?", (run["run_id"],))}
        ctx["candidates"] = [{"ticker": t, "name": names.get(t, t)} for t, row in sorted(tags.items()) if row["tag"] == "NEW"]
    for t in set(tracked) | {c["ticker"] for c in ctx["candidates"]}:
        cur, prev = db.latest_two_dossiers(conn, t)
        if cur:
            ctx["dossiers"][t] = {"cur": cur, "prev": prev}
    try:
        for t in tracked:
            row = db.latest_price(conn, t)
            if row:
                ctx["prices"][t] = row
    except sqlite3.OperationalError:
        pass                                        # not upgraded to the price tables yet
    try:
        pc = db.latest_calendar_run(conn)
    except sqlite3.OperationalError:
        pc = None                                   # not upgraded to the calendar tables yet
    if pc:
        ctx["prev_calendar"] = {"run_at": pc["run_at"], "as_of_date": pc["as_of_date"],
                                "dates": db.calendar_event_dates(conn, pc["calendar_run_id"])}
    return ctx


def dossier_view(dos, today):
    """Turn a ticker's newest (and previous) stored Finviz snapshot into card rows and a change list."""
    if not dos or not dos.get("cur") or finviz_quote is None:
        return None
    cur, prev = dos["cur"], dos.get("prev")
    d = cur["data"]
    snap = d.get("snapshot", {})
    price, target = finviz_quote.num(snap.get("Price")), finviz_quote.num(snap.get("Target Price"))
    upside = (target / price - 1) * 100 if price and target else None
    age = _age_days(cur["fetched_at"], today)
    rows = [["Dossier", f"Finviz page fetched {cur['fetched_at'][:10]}" + (f" ({_days(age)} ago)" if age is not None else "")
             + (f", close {d['quote_asof']}" if d.get("quote_asof") else "")],
            ["Street", f"Recom {snap.get('Recom', '?')}, target {snap.get('Target Price', '?')}"
                       + (f", {upside:+.0f}% from price" if upside is not None else "")]]
    ratings = d.get("ratings") or []
    if ratings:
        r = ratings[0]
        rows.append(["Analysts", f"{len(ratings)} action(s) on file; latest {r.get('date')} {r.get('action')} {r.get('analyst')}"
                     + (f", {r['rating_change']}" if r.get("rating_change") else "")
                     + (f", {r['price_target_change']}" if r.get("price_target_change") else "")])
    ins = d.get("insiders") or []
    if ins:
        kinds = Counter(i.get("transaction", "?") for i in ins)
        rows.append(["Insiders", f"{len(ins)} trade(s): " + ", ".join(f"{n} {k}" for k, n in kinds.most_common())
                     + f"; latest {ins[0].get('date', '?')} {ins[0].get('insider_trading', '?')} {ins[0].get('transaction', '?')}"])
    news = d.get("news") or []
    if news:
        rows.append(["News", f"{news[0].get('date')}: {str(news[0].get('headline', ''))[:110]}"
                     + (f" ({news[0]['source']})" if news[0].get("source") else "")])
    changes = finviz_quote.describe_changes(prev["data"], d) if prev else []
    return {"rows": rows, "changes": changes, "since": prev["fetched_at"][:10] if prev else None, "age": age,
            "snap": snap, "price": price, "target": target, "upside": upside, "ratings": ratings, "insiders": ins}


def attach_dossier(card, view):
    if not view:
        return
    card["rows"] += view["rows"]
    if view["changes"]:
        card["flags"].insert(0, {"level": "change", "text": f"Since the Finviz snapshot of {view['since']}: " + "; ".join(view["changes"])})
        card["changed"] = True
    elif view["since"]:
        card["flags"].append({"level": "info", "text": f"No changes since the Finviz snapshot of {view['since']}."})


def date_moved(sym, next_ev, prev_cal, today_iso):
    """Did FMP move an upcoming date since the last calendar run? A date that has simply passed is a new
    quarter, not a move."""
    if not prev_cal:
        return None
    dates = prev_cal["dates"].get(sym)
    if not dates:
        return None
    was = next((d for d in dates if d >= prev_cal["as_of_date"]), None)
    if was is None or was < today_iso:
        return None
    now = next_ev["date"] if next_ev else None
    if now == was:
        return None
    if now is None:
        return ("warn", f"FMP listed {was} at the last calendar run ({prev_cal['as_of_date']}) but shows no upcoming date in this window now.")
    return ("warn", f"The earnings source moved this date: {was} -> {now} since the last calendar run ({prev_cal['as_of_date']}). Re-verify against the company IR page.")


def build_candidate_card(cand, ev, ctx, view):
    scr = ctx["screen"]
    rows = [["Company", cand["name"]],
            ["Status", f"NEW in scanner run #{scr['run_id']} ({scr['run_at'][:10]}); not in your ledger yet"],
            ["Source date", f"{ev['date']}, {'confirmed' if ev['confirmed'] else 'estimated'} (not proof)"]]
    if ev["eps_est"] is not None or ev["rev_est"] is not None:
        rows.append(["Consensus", est_text(ev["eps_est"], ev["rev_est"])])
    if ev["eps_act"] is not None or ev["rev_act"] is not None:
        rows.append(["Result", result_text(ev)])
    card = {"kind": "candidate", "box_label": "Why it is here",
            "box_text": "It passed the standing screen and is not in your ledger. This is not a verdict: the pre-entry diligence "
                        "(insider selling class, 12-month price, peer comparison) is still to do.",
            "gate": None, "rows": rows, "flags": [], "open_items": []}
    if not view:
        card["flags"].append({"level": "info", "text": "No Finviz dossier stored yet. Run: python3 sentinel_scan.py --dossier-only"})
    attach_dossier(card, view)
    return card


def save_calendar_run(conn, today, positions, data, quotes, window_from, window_to):
    """Save this run's history. Returns (calendar_run_id, positions_snapshot_saved)."""
    rid = db.insert_calendar_run(conn, as_of_date=today.isoformat(), window_from=window_from, window_to=window_to,
                                 events=data["tracked_events"], quotes=quotes)
    blob = json.dumps({"as_of": positions["as_of"], "held": positions["held"], "watch": positions["watch"],
                       "needs_confirmation": positions["needs_confirmation"]}, sort_keys=True, default=str)
    _, saved = db.save_positions_snapshot(conn, json.loads(blob), hashlib.sha1(blob.encode("utf-8")).hexdigest(), as_of=positions["as_of"])
    return rid, saved


def build(today, positions, sp500, cal_rows, quotes, ctx=None):
    held = {p["ticker"]: p for p in positions["held"]}
    watch = {p["ticker"]: p for p in positions["watch"]}
    cand = {c["ticker"]: c for c in ctx["candidates"]} if ctx and ctx.get("screen") else {}
    dossiers = ctx["dossiers"] if ctx else {}
    prev_cal = ctx["prev_calendar"] if ctx else None
    views = {}

    def view_for(sym):
        if sym not in views:
            views[sym] = dossier_view(dossiers.get(sym), today)
        return views[sym]

    def quote_for(sym):
        """A live FMP quote if there is one, else the newest stored daily close (at most 5 days old)."""
        q = quotes.get(sym)
        if q:
            return q
        st = (ctx or {}).get("prices", {}).get(sym)
        age = _age_days(st["date"] + "T00:00:00", today) if st else None
        if st and age is not None and age <= 5 and st.get("close") is not None:
            return {"price": st["close"], "change_pct": st.get("change_pct"), "label": f"{st['source'].split('_')[0].capitalize()} close {short_date(st['date'])}"}
        return None

    grid_start, grid_end = today.replace(day=1), end_of_month_two_ahead(today)
    today_iso = today.isoformat()

    # 1. normalise FMP rows we care about
    raw, seen, by_symbol = [], set(), defaultdict(list)
    for row in cal_rows:
        sym = row.get("symbol")
        kind = "held" if sym in held else "watch" if sym in watch else "candidate" if sym in cand else "sp500" if sym in sp500 else None
        d = str(row.get("date") or "")[:10]
        if not kind or len(d) != 10 or (sym, d) in seen:
            continue
        seen.add((sym, d))
        ev = {"symbol": sym, "kind": kind, "date": d, "time": row.get("time") or "",
              "confirmed": bool(row.get("confirmed")),
              "eps_est": to_float(row.get("epsEstimated")), "eps_act": to_float(row.get("epsActual")),
              "rev_est": to_float(row.get("revenueEstimated")), "rev_act": to_float(row.get("revenueActual"))}
        raw.append(ev)
        if kind != "sp500":
            by_symbol[sym].append(ev)
    next_ev = {}
    for sym, evs in by_symbol.items():
        evs.sort(key=lambda e: e["date"])
        next_ev[sym] = next((e for e in evs if e["date"] >= today_iso), None)

    # 2. calendar items (earnings rows inside the 3 visible months)
    items = []
    for ev in raw:
        if not (grid_start.isoformat() <= ev["date"] <= grid_end.isoformat()):
            continue
        pos = held.get(ev["symbol"]) or watch.get(ev["symbol"])
        cinfo = cand.get(ev["symbol"]) if not pos else None
        card = None
        if pos:
            card = build_card(pos, ev, next_ev.get(ev["symbol"]), quote_for(ev["symbol"]), today)
            attach_dossier(card, view_for(ev["symbol"]))
        elif cinfo:
            card = build_candidate_card(cinfo, ev, ctx, view_for(ev["symbol"]))
        if card is not None and ev is next_ev.get(ev["symbol"]):
            moved = date_moved(ev["symbol"], ev, prev_cal, today_iso)
            if moved:
                card["flags"].insert(0, {"level": moved[0], "text": moved[1]})
        items.append({
            "type": "earnings", "kind": ev["kind"], "symbol": ev["symbol"],
            "name": pos["name"] if pos else cinfo["name"] if cinfo else sp500.get(ev["symbol"], ev["symbol"]),
            "date": ev["date"], "time": ev["time"], "confirmed": ev["confirmed"],
            "est_text": est_text(ev["eps_est"], ev["rev_est"]),
            "result_text": result_text(ev) if (ev["eps_act"] is not None or ev["rev_act"] is not None) else "",
            "card": card,
        })

    # 3. ledger-only gates
    offgrid_gates, checks = [], []
    for pos in positions["held"] + positions["watch"]:
        for g in pos["events"]:
            end = g.get("until") or g.get("date")
            passed = bool(end) and end < today_iso
            item = {"type": "gate", "kind": pos["kind"], "symbol": pos["ticker"], "name": pos["name"],
                    "date": g.get("date"), "until": g.get("until"), "label": g.get("label", ""),
                    "outcome": g.get("outcome"), "passed": passed, "card": None}
            in_grid = g.get("date") and grid_start.isoformat() <= g["date"] <= grid_end.isoformat()
            if in_grid:
                items.append(item)
            else:
                offgrid_gates.append(item)
            if passed and not g.get("outcome"):
                checks.append({"level": "warn", "ticker": pos["ticker"],
                               "text": f"Gate passed without a recorded outcome: {g.get('label', '')} ({g['date']}"
                                       + (f" to {g['until']}" if g.get("until") else "") + "). Fill in `outcome` in positions.yaml."})

    # 4. checks that live on the position or the ledger itself
    for nc in positions["needs_confirmation"]:
        checks.append({"level": "warn", "ticker": nc.get("ticker", ""), "text": " ".join(str(nc.get("issue", "")).split())})
    for pos in positions["held"] + positions["watch"]:
        q = quote_for(pos["ticker"])
        for lv, tx in position_flags(pos, q, today):
            if lv == "warn":
                checks.append({"level": lv, "ticker": pos["ticker"], "text": tx})
        df = date_flag(pos, next_ev.get(pos["ticker"]))
        if df:
            checks.append({"level": df[0], "ticker": pos["ticker"], "text": df[1]})

    # 5. recent prints for tracked names (Funnel A, Tier 1 rule)
    recent = []
    for ev in sorted(raw, key=lambda e: e["date"], reverse=True):
        if ev["kind"] == "sp500" or not (today_iso >= ev["date"] >= (today - timedelta(days=LOOKBACK_DAYS)).isoformat()):
            continue
        if ev["eps_act"] is None and ev["rev_act"] is None:
            continue
        pos = held.get(ev["symbol"]) or watch.get(ev["symbol"])
        se, sr = surprise_pct(ev["eps_act"], ev["eps_est"]), surprise_pct(ev["rev_act"], ev["rev_est"])
        recent.append({"symbol": ev["symbol"], "name": pos["name"], "kind": ev["kind"], "date": ev["date"],
                       "eps_text": (f"{ev['eps_act']:.2f} vs {ev['eps_est']:.2f}" if ev["eps_act"] is not None and ev["eps_est"] is not None else "n/a"),
                       "eps_s": se, "rev_s": sr,
                       "hit": any(abs(s) >= SURPRISE_THRESHOLD_PCT for s in (se, sr) if s is not None)})
        if recent[-1]["hit"]:
            checks.append({"level": "warn", "ticker": ev["symbol"],
                           "text": f"Reported {ev['date']} with a surprise at or above {SURPRISE_THRESHOLD_PCT:.0f}% ({result_text(ev)}). Tier 2 candidate."})

    # 6. tracked names with an expected date but no upcoming FMP row
    no_date = []
    for pos in positions["held"] + positions["watch"]:
        e = pos["earnings"]
        if next_ev.get(pos["ticker"]) is not None:
            continue
        if pos["kind"] == "held" or e.get("date") or e.get("month"):
            no_date.append({"symbol": pos["ticker"], "name": pos["name"], "kind": pos["kind"],
                            "ledger": (e.get("date") or e.get("month") or "no date on file")
                                      + (f" ({e['note']})" if e.get("note") else ""),
                            "confidence": e.get("confidence", "unstated")})

    # 7. research.db extras: date moves for every tracked name, what changed in dossiers, the candidates list
    changes, cand_rows = [], []
    if ctx:
        for sym in [p["ticker"] for p in positions["held"] + positions["watch"]] + list(cand):
            mv = date_moved(sym, next_ev.get(sym), prev_cal, today_iso)
            if mv:
                checks.append({"level": mv[0], "ticker": sym, "text": mv[1]})
            v = view_for(sym)
            if v and v["changes"]:
                changes.append({"ticker": sym, "since": v["since"], "changes": v["changes"]})
        for sym, c in cand.items():
            v, nxt = view_for(sym), next_ev.get(sym)
            snap = v["snap"] if v else {}
            ins = Counter(i.get("transaction", "?") for i in v["insiders"]) if v else {}
            r0 = v["ratings"][0] if v and v["ratings"] else None
            cand_rows.append({"symbol": sym, "name": c["name"], "next_date": nxt["date"] if nxt else None,
                              "has_dossier": v is not None, "price": snap.get("Price"), "target": snap.get("Target Price"),
                              "upside": v["upside"] if v else None, "recom": snap.get("Recom"),
                              "insiders": ", ".join(f"{n} {k}" for k, n in Counter(ins).most_common()) if ins else None,
                              "analyst": f"{r0.get('date')} {r0.get('action')} {r0.get('analyst')}" if r0 else None,
                              "age": v["age"] if v else None, "changed": bool(v and v["changes"])})

    checks.sort(key=lambda c: (c["ticker"] == "", c["ticker"]))
    return {"items": items, "offgrid_gates": offgrid_gates, "checks": checks,
            "recent": recent, "no_date": no_date, "grid_end": grid_end.isoformat(),
            "tracked_events": [{"ticker": e["symbol"], "event_date": e["date"], "kind": e["kind"], "time": e["time"],
                                "confirmed": e["confirmed"], "eps_est": e["eps_est"], "eps_act": e["eps_act"],
                                "rev_est": e["rev_est"], "rev_act": e["rev_act"]} for e in raw if e["kind"] != "sp500"],
            "changes": changes, "candidates": cand_rows}


# ------------------------------------------------------------- static HTML parts

def esc(s):
    return html.escape(str(s), quote=True)


def render_static(today, positions, data, meta):
    e = esc
    out_top, out_bottom = [], []

    # banners
    banners = []
    if meta["offline"]:
        banners.append('<div class="banner bad">Offline demo. Every date, price and estimate on this page is fixture data, not FMP.</div>')
    if meta["share"]:
        banners.append('<div class="banner">Share mode: S&amp;P 500 only. No positions, gates or prices are included.</div>')
    else:
        banners.append('<div class="banner">Contains cost basis and share counts. Local use only; generate with <code>--share</code> to send it to anyone.</div>')
        if positions["as_of"]:
            age = (today - date.fromisoformat(positions["as_of"])).days
            passed = sum(1 for c in data["checks"] if c["text"].startswith("Gate passed"))
            if age > STALE_LEDGER_DAYS:
                extra = f" {passed} gate(s) have passed with no recorded outcome." if passed else ""
                banners.append(f'<div class="banner warn">positions.yaml is as of {e(positions["as_of"])}, {age} days old. '
                               f'Prices, dates and gates below may have moved.{e(extra)}</div>')
    dbm = meta.get("db")
    if dbm and not meta["share"]:
        if dbm["state"] == "error":
            banners.append(f'<div class="banner warn">Could not read research.db ({e(dbm["message"])}). Showing the calendar without scanner data.</div>')
        elif dbm["state"] == "ok":
            scr = dbm["screen"]
            if scr is None:
                banners.append('<div class="banner">research.db has no scanner runs yet, so there are no candidates to show.</div>')
            elif scr["untagged"]:
                banners.append(f'<div class="banner warn">Scanner run #{scr["run_id"]} has no ledger tags, so no candidates are shown. Run: python3 sentinel_scan.py --retag</div>')
            else:
                stale = scr["age"] is not None and scr["age"] > SCREEN_STALE_DAYS
                banners.append(f'<div class="banner{" warn" if stale else ""}">Scanner data: run #{scr["run_id"]} from {e(scr["run_at"][:10])} '
                               f'({_days(scr["age"])} old). {len(data["candidates"])} NEW candidate(s), {dbm["n_dossiers"]} stored Finviz dossier(s).'
                               f'{" Older than " + str(SCREEN_STALE_DAYS) + " days: run the scanner for fresh candidates." if stale else ""}</div>')
    out_top.append('<div class="banners">' + "".join(banners) + "</div>")

    # legend
    out_top.append('<div class="legend">'
                   '<span><span class="dot accent"></span> S&amp;P 500</span>'
                   + ('' if meta["share"] else
                      '<span><span class="dot held"></span> Your holding</span>'
                      '<span><span class="dot watch"></span> Watch list or ledger gate</span>'
                      + ('<span><span class="dot cand"></span> New candidate from the latest screen</span>' if data["candidates"] else ''))
                   + '</div>')

    if not meta["share"]:
        # checks
        n_warn = sum(1 for c in data["checks"] if c["level"] == "warn")
        lis = "".join(f'<li><span class="tk">{e(c["ticker"])}</span>{e(c["text"])}</li>' for c in data["checks"])
        out_top.append(f'<details class="checks"><summary>Ledger checks ({n_warn})</summary>'
                       f'<ul>{lis or "<li>Nothing to check.</li>"}</ul></details>')

        # upcoming chips
        up = [i for i in data["items"] if i["kind"] in ("held", "watch") and i["date"] >= today.isoformat()]
        up.sort(key=lambda i: (i["date"], 0 if i["kind"] == "held" else 1, i["symbol"]))
        chips = []
        for i in up[:8]:
            n = days_from(i["date"], today)
            label = i["label"] if i["type"] == "gate" else "Earnings"
            chips.append(f'<button class="chip {i["kind"] if i["type"] == "earnings" else "watch"}" data-goto="{e(i["date"])}" title="{e(label)}">'
                         f'<span class="c-sym">{e(i["symbol"])}</span>'
                         f'<span class="c-what">{e("Gate" if i["type"] == "gate" else "Earnings")}</span>'
                         f'<span class="c-when">{e(short_date(i["date"]))}, {e(when_text(n))}</span></button>')
        out_top.append('<section class="strip"><h2>Coming up for your names</h2>'
                       + ('<div class="chips">' + "".join(chips) + '</div>' if chips else '<div class="empty">No held or watch-list dates in the next three months from FMP or the ledger.</div>')
                       + '</section>')

        # what changed + candidates (only when research.db supplied them)
        if dbm and dbm["state"] == "ok":
            if dbm["n_dossiers"]:
                lis = "".join(f'<li><span class="tk">{e(c["ticker"])}</span>since {e(c["since"])}: {e("; ".join(c["changes"]))}</li>' for c in data["changes"])
                out_top.append(f'<details class="checks"><summary>Changed since the last Finviz snapshot ({len(data["changes"])})</summary>'
                               f'<ul>{lis or "<li>Nothing changed for the names with two or more snapshots.</li>"}</ul></details>')
            scr = dbm["screen"]
            if scr and not scr["untagged"]:
                if data["candidates"]:
                    trs = ""
                    for c in data["candidates"]:
                        when = (f'<button class="linkbtn" data-goto="{e(c["next_date"])}">{e(short_date(c["next_date"]))}</button>'
                                if c["next_date"] else "no date in view")
                        street = (f'{e(c["price"])} to {e(c["target"])}' + (f' ({c["upside"]:+.0f}%)' if c["upside"] is not None else "") + f', recom {e(c["recom"])}'
                                  if c["has_dossier"] else '<span class="name">no dossier yet</span>')
                        badge = '<span class="tag changed">Changed</span>' if c["changed"] else ""
                        trs += (f'<tr><td><span class="symbol">{e(c["symbol"])}</span> <span class="name">{e(c["name"])}</span>{badge}</td>'
                                f'<td>{when}</td><td>{street}</td><td>{e(c["insiders"] or "-")}</td><td>{e(c["analyst"] or "-")}</td>'
                                f'<td>{(_days(c["age"]) + " old") if c["age"] is not None else "-"}</td></tr>')
                    body = ('<div class="scroll"><table><thead><tr><th>Name</th><th>Next report</th><th>Street (price to target)</th>'
                            f'<th>Insider trades</th><th>Latest analyst action</th><th>Dossier</th></tr></thead><tbody>{trs}</tbody></table></div>')
                else:
                    body = '<div class="empty">No NEW names in the latest screen run.</div>'
                out_top.append(f'<section class="strip"><h2>Candidates from the latest screen</h2>{body}</section>')

        # recent prints
        if data["recent"]:
            trs = ""
            for r in data["recent"]:
                fs = lambda v: "n/a" if v is None else f"{v:+.1f}%"  # noqa: E731
                trs += (f'<tr class="{"hit" if r["hit"] else ""}"><td><span class="symbol">{e(r["symbol"])}</span> <span class="name">{e(r["name"])}</span></td>'
                        f'<td>{e(short_date(r["date"]))}</td><td>{e(r["eps_text"])}</td><td>{fs(r["eps_s"])}</td><td>{fs(r["rev_s"])}</td>'
                        f'<td>{"At or above " + str(int(SURPRISE_THRESHOLD_PCT)) + "%: Tier 2 candidate" if r["hit"] else "Below threshold: log only"}</td></tr>')
            body = ('<div class="scroll"><table><thead><tr><th>Name</th><th>Date</th><th>EPS vs est.</th><th>EPS surprise</th>'
                    f'<th>Revenue surprise</th><th>Rule ({int(SURPRISE_THRESHOLD_PCT)}% on EPS or revenue)</th></tr></thead><tbody>{trs}</tbody></table></div>')
        else:
            body = f'<div class="empty">No held or watch-list name has reported with actuals in the last {LOOKBACK_DAYS} days.</div>'
        out_bottom.append(f'<section class="strip"><h2>Recently reported</h2>{body}</section>')

        # off-grid
        parts = []
        if data["no_date"]:
            lis = "".join(f'<li><span class="tk">{e(n["symbol"])}</span>{e(n["name"])}: ledger says {e(n["ledger"])}, {e(n["confidence"])}. No upcoming FMP row.</li>'
                          for n in data["no_date"])
            parts.append(f'<h3>Expected, but the earnings source has no date</h3><ul>{lis}</ul>')
        if data["offgrid_gates"]:
            lis = ""
            for g in data["offgrid_gates"]:
                when = g["date"] or "no date"
                lis += f'<li><span class="tk">{e(g["symbol"])}</span>{e(g["label"])} ({e(when)})</li>'
            parts.append(f'<h3>Ledger gates outside this view</h3><ul>{lis}</ul>')
        if parts:
            out_bottom.append('<section class="strip offgrid"><h2>Not on the calendar</h2>' + "".join(parts) + '</section>')

    return "".join(out_top), "".join(out_bottom)


# ------------------------------------------------------------------ HTML shell

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Earnings Calendar</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --ink: #14213D; --paper: #FBFAF7; --rule: #DAD6CC; --accent: #9C6B1F;
    --held: #1D4E89; --watch: #6E4A78; --cand: #2F6D6A; --cand-bg: #EAF4F3; --change: #1D6F8A; --change-bg: #E8F3F7; --confirmed: #2F6D4F; --estimated: #8B8676;
    --warn: #9B3D2F; --warn-bg: #F7ECE8; --held-bg: #EEF3FA; --watch-bg: #F4EFF6;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--paper); color: var(--ink); font-family: 'Inter', sans-serif; padding: 32px 16px 80px; }
  .wrap { max-width: 980px; margin: 0 auto; }
  h1 { font-family: 'Source Serif 4', serif; font-size: 26px; font-weight: 700; margin: 0 0 4px; }
  h2 { font-family: 'Source Serif 4', serif; font-size: 18px; margin: 0 0 12px; }
  h3 { font-size: 13px; font-weight: 600; margin: 14px 0 6px; }
  code { font-size: 12px; background: #EFEBE0; padding: 1px 5px; border-radius: 3px; }
  .sub { color: var(--estimated); font-size: 13px; margin-bottom: 12px; }
  .banners { display: grid; gap: 6px; margin-bottom: 12px; }
  .banner { font-size: 12px; padding: 7px 10px; border: 1px solid var(--rule); border-radius: 4px; color: #5A5648; }
  .banner.warn, .banner.bad { border-color: var(--warn); background: var(--warn-bg); color: var(--warn); }
  .legend { display: flex; flex-wrap: wrap; gap: 18px; font-size: 12px; color: var(--estimated); margin-bottom: 18px; }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
  .dot.accent { background: var(--accent); } .dot.held { background: var(--held); } .dot.watch { background: var(--watch); } .dot.cand { background: var(--cand); }

  details.checks { border: 1px solid var(--rule); border-radius: 4px; margin-bottom: 22px; }
  details.checks summary { cursor: pointer; padding: 9px 12px; font-size: 13px; font-weight: 600; }
  details.checks ul, .offgrid ul { margin: 0; padding: 0 12px 10px; list-style: none; font-size: 13px; }
  details.checks li, .offgrid li { padding: 6px 0; border-top: 1px solid var(--rule); line-height: 1.45; }
  .tk { display: inline-block; min-width: 64px; font-weight: 600; margin-right: 8px; }

  .strip { margin-bottom: 26px; }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; }
  .chip { font: inherit; text-align: left; cursor: pointer; background: #fff; border: 1px solid var(--rule); border-left-width: 4px; border-radius: 4px; padding: 7px 11px; display: grid; gap: 1px; }
  .chip.held { border-left-color: var(--held); } .chip.watch { border-left-color: var(--watch); }
  .chip:hover { background: #F1EEE5; }
  .chip:focus-visible, .day.has-events:focus-visible, summary:focus-visible { outline: 2px solid var(--held); outline-offset: 2px; }
  .c-sym { font-weight: 600; font-size: 13px; } .c-what, .c-when { font-size: 12px; color: var(--estimated); }
  .empty { color: var(--estimated); font-size: 14px; padding: 12px 0; }

  .months { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 28px; }
  @media (max-width: 720px) { .months { grid-template-columns: 1fr; } }
  .month-title { font-family: 'Source Serif 4', serif; font-size: 15px; font-weight: 600; text-align: center; margin-bottom: 8px; }
  .grid { display: grid; grid-template-columns: repeat(7, 1fr); border-top: 1px solid var(--rule); border-left: 1px solid var(--rule); }
  .dow { font-size: 10px; color: var(--estimated); text-align: center; padding: 4px 0; border-right: 1px solid var(--rule); border-bottom: 1px solid var(--rule); }
  .day { min-height: 42px; border-right: 1px solid var(--rule); border-bottom: 1px solid var(--rule); padding: 3px 4px; position: relative; }
  .day.has-events { cursor: pointer; } .day.has-events:hover { background: #F1EEE5; }
  .day.selected { background: #EFE6D3; } .day.outside { color: #C9C5B8; }
  .day.today .daynum { color: var(--accent); font-weight: 700; }
  .daynum { font-size: 11px; font-weight: 500; }
  .badge { display: inline-block; margin-top: 3px; font-size: 9px; background: var(--accent); color: #fff; border-radius: 8px; padding: 0 5px; }
  .badge.badge-held { background: var(--held); } .badge.badge-watch { background: var(--watch); } .badge.badge-cand { background: var(--cand); }

  .panel { margin-top: 12px; border-top: 2px solid var(--ink); padding-top: 16px; margin-bottom: 30px; }
  .row { border-bottom: 1px solid var(--rule); padding: 0; }
  .row-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; padding: 10px 0; list-style: none; }
  .row-head::-webkit-details-marker { display: none; }
  details.row > summary { cursor: pointer; }
  .row.held { background: var(--held-bg); margin: 0 -8px; padding: 0 8px; }
  .row.watch { background: var(--watch-bg); margin: 0 -8px; padding: 0 8px; }
  .row.candidate { background: var(--cand-bg); margin: 0 -8px; padding: 0 8px; }
  .symbol { font-weight: 600; } .name { font-size: 13px; color: var(--estimated); margin-left: 8px; }
  .tag { font-size: 10px; color: #fff; border-radius: 8px; padding: 1px 7px; margin-left: 8px; }
  .tag.held { background: var(--held); } .tag.watch { background: var(--watch); } .tag.gate { background: var(--watch); } .tag.candidate { background: var(--cand); } .tag.changed { background: var(--change); }
  .status { font-size: 12px; text-align: right; } .status.confirmed { color: var(--confirmed); } .status.estimated { color: var(--estimated); }
  .status.passed { color: var(--warn); }
  .sub2 { font-size: 12px; color: var(--estimated); padding: 0 0 10px; }
  .card { padding: 4px 0 16px; display: grid; gap: 12px; font-size: 13px; }
  .gate-box { border-left: 3px solid var(--held); background: #fff; padding: 8px 12px; line-height: 1.5; }
  .gate-box.none { border-left-color: var(--rule); color: var(--estimated); }
  .gate-box .k { display: block; font-size: 11px; color: var(--estimated); margin-bottom: 2px; }
  .kv { display: grid; grid-template-columns: 110px 1fr; gap: 4px 12px; margin: 0; }
  .kv dt { color: var(--estimated); } .kv dd { margin: 0; }
  .flags { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
  .flags li { padding: 5px 9px; border-radius: 3px; line-height: 1.4; background: #fff; border: 1px solid var(--rule); }
  .flags li.warn { background: var(--warn-bg); border-color: var(--warn); color: var(--warn); }
  .flags li.change { background: var(--change-bg); border-color: var(--change); color: var(--change); }
  .linkbtn { font: inherit; background: none; border: 0; padding: 0; color: var(--held); text-decoration: underline; cursor: pointer; }
  .flags b { font-weight: 600; margin-right: 6px; }
  .open ul { margin: 4px 0 0; padding-left: 18px; } .open .k { color: var(--estimated); font-size: 11px; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-weight: 500; color: var(--estimated); font-size: 12px; padding: 6px 8px; border-bottom: 1px solid var(--ink); }
  td { padding: 8px; border-bottom: 1px solid var(--rule); }
  tr.hit td { background: var(--warn-bg); }
  .scroll { overflow-x: auto; }
  .tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--rule); margin-bottom: 18px; }
  .tabs a { padding: 8px 14px; font-size: 14px; font-weight: 500; color: var(--estimated); text-decoration: none; border: 1px solid transparent; border-bottom: 0; border-radius: 6px 6px 0 0; margin-bottom: -1px; }
  .tabs a.on { color: var(--ink); background: var(--paper); border-color: var(--rule); }
  .tabs a:hover:not(.on) { color: var(--ink); }
</style>
</head>
<body>
<div class="wrap">
  <h1>Earnings Calendar</h1>
  <div class="sub">__SUB__</div>
  __TABS__
  __TOP__
  <div class="months" id="monthsContainer"></div>
  <div class="panel" id="panel">
    <h2 id="panelTitle">Select a date</h2>
    <div id="panelBody" class="empty">Click a highlighted day to see who is reporting.</div>
  </div>
  __BOTTOM__
</div>
<script>
const DATA = __DATA__;
const byDate = {};
DATA.items.forEach(function (it) { if (it.date) (byDate[it.date] = byDate[it.date] || []).push(it); });

const panelTitle = document.getElementById('panelTitle');
const panelBody = document.getElementById('panelBody');
const ORDER = { held: 0, watch: 1, candidate: 2, sp500: 3 };

function pad(n) { return (n < 10 ? '0' : '') + n; }
function keyOf(y, m, d) { return y + '-' + pad(m + 1) + '-' + pad(d); }   // local date, no UTC shift
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function renderMonth(offset, titleEl, gridEl, base) {
  const view = new Date(base.getFullYear(), base.getMonth() + offset, 1);
  titleEl.textContent = view.toLocaleString('en-US', { month: 'long', year: 'numeric' });
  ['S', 'M', 'T', 'W', 'T', 'F', 'S'].forEach(function (d) { gridEl.appendChild(el('div', 'dow', d)); });

  const year = view.getFullYear(), month = view.getMonth();
  const firstDow = new Date(year, month, 1).getDay();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const total = Math.ceil((firstDow + daysInMonth) / 7) * 7;

  for (let i = 0; i < total; i++) {
    const cell = el('div', 'day');
    const dayNum = i - firstDow + 1;
    const outside = dayNum < 1 || dayNum > daysInMonth;
    const cd = new Date(year, month, dayNum);
    const key = keyOf(cd.getFullYear(), cd.getMonth(), cd.getDate());
    const evs = outside ? null : byDate[key];   // only the month's own days carry events
    cell.dataset.key = key;
    cell.className = 'day' + (outside ? ' outside' : '') + (evs ? ' has-events' : '') + (key === DATA.today ? ' today' : '');
    cell.appendChild(el('div', 'daynum', String(cd.getDate())));
    if (evs) {
      const rank = Math.min.apply(null, evs.map(function (e) { return ORDER[e.kind]; }));
      const b = el('div', 'badge' + (rank === 0 ? ' badge-held' : rank === 1 ? ' badge-watch' : rank === 2 ? ' badge-cand' : ''), String(evs.length));
      cell.appendChild(b);
      cell.tabIndex = 0;
      cell.addEventListener('click', function () { selectDay(key, cell); });
      cell.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectDay(key, cell); } });
    }
    gridEl.appendChild(cell);
  }
}

function renderCard(card) {
  const box = el('div', 'card');
  const gate = el('div', 'gate-box' + (card.gate || card.box_text ? '' : ' none'));
  gate.appendChild(el('span', 'k', card.box_label || 'Decision test for this print'));
  gate.appendChild(document.createTextNode(card.gate || card.box_text || 'None written yet. Add a gate to positions.yaml before this reports.'));
  box.appendChild(gate);

  const dl = el('dl', 'kv');
  card.rows.forEach(function (r) { dl.appendChild(el('dt', '', r[0])); dl.appendChild(el('dd', '', r[1])); });
  box.appendChild(dl);

  if (card.flags.length) {
    const ul = el('ul', 'flags');
    card.flags.forEach(function (f) {
      const li = el('li', f.level);
      li.appendChild(el('b', '', f.level === 'warn' ? 'Check' : f.level === 'change' ? 'Changed' : 'Note'));
      li.appendChild(document.createTextNode(f.text));
      ul.appendChild(li);
    });
    box.appendChild(ul);
  }
  if (card.open_items.length) {
    const o = el('div', 'open');
    o.appendChild(el('div', 'k', 'Open items'));
    const ul = el('ul');
    card.open_items.forEach(function (t) { ul.appendChild(el('li', '', t)); });
    o.appendChild(ul);
    box.appendChild(o);
  }
  return box;
}

function buildRow(it) {
  const expandable = !!it.card;
  const wrap = el(expandable ? 'details' : 'div', 'row ' + it.kind);
  if (expandable && (it.kind === 'held' || it.card.changed)) wrap.open = true;
  const head = el(expandable ? 'summary' : 'div', 'row-head');

  const left = el('div');
  left.appendChild(el('span', 'symbol', it.symbol));
  left.appendChild(el('span', 'name', it.name));
  if (it.type === 'gate') left.appendChild(el('span', 'tag gate', 'Gate'));
  else if (it.kind === 'held') left.appendChild(el('span', 'tag held', 'Held'));
  else if (it.kind === 'watch') left.appendChild(el('span', 'tag watch', 'Watch'));
  else if (it.kind === 'candidate') left.appendChild(el('span', 'tag candidate', 'Candidate'));
  if (it.card && it.card.changed) left.appendChild(el('span', 'tag changed', 'Changed'));
  head.appendChild(left);

  let status;
  if (it.type === 'gate') {
    const unresolved = it.passed && !it.outcome;
    status = el('div', 'status ' + (unresolved ? 'passed' : 'estimated'),
      it.outcome ? 'Outcome: ' + it.outcome : unresolved ? 'Passed, outcome not recorded' : 'Upcoming' + (it.until ? ', through ' + it.until : ''));
  } else {
    status = el('div', 'status ' + (it.confirmed ? 'confirmed' : 'estimated'),
      'FMP: ' + (it.confirmed ? 'confirmed' : 'estimated') + (it.time ? ', ' + it.time : ''));
  }
  head.appendChild(status);
  wrap.appendChild(head);

  const note = it.type === 'gate' ? it.label : [it.result_text, it.est_text].filter(Boolean).join(' | ');
  if (note) wrap.appendChild(el('div', 'sub2', note));
  if (expandable) wrap.appendChild(renderCard(it.card));
  return wrap;
}

function selectDay(key, cellEl) {
  document.querySelectorAll('.day.selected').forEach(function (n) { n.classList.remove('selected'); });
  if (!cellEl) cellEl = document.querySelector('.day[data-key="' + key + '"]:not(.outside)');
  if (cellEl) cellEl.classList.add('selected');

  const dayItems = (byDate[key] || []).slice().sort(function (a, b) {
    if (ORDER[a.kind] !== ORDER[b.kind]) return ORDER[a.kind] - ORDER[b.kind];
    if (a.type !== b.type) return a.type === 'earnings' ? -1 : 1;
    return a.symbol.localeCompare(b.symbol);
  });
  const d = new Date(key + 'T00:00:00');
  panelTitle.textContent = d.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' });
  panelBody.className = '';
  panelBody.textContent = '';
  if (!dayItems.length) { panelBody.className = 'empty'; panelBody.textContent = 'Nothing on this day.'; return; }
  dayItems.forEach(function (it) { panelBody.appendChild(buildRow(it)); });
  document.getElementById('panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

const monthsContainer = document.getElementById('monthsContainer');
const base = new Date(DATA.today + 'T00:00:00');
base.setDate(1);
for (let off = 0; off < 3; off++) {
  const block = el('div'), title = el('div', 'month-title'), grid = el('div', 'grid');
  block.appendChild(title); block.appendChild(grid);
  monthsContainer.appendChild(block);
  renderMonth(off, title, grid, base);
}
document.querySelectorAll('[data-goto]').forEach(function (b) {
  b.addEventListener('click', function () { selectDay(b.dataset.goto, null); });
});
</script>
</body>
</html>
"""


def render_html(today, positions, data, meta):
    top, bottom = render_static(today, positions, data, meta)
    payload = {"today": today.isoformat(), "items": data["items"]}
    scope = "S&amp;P 500" if meta["share"] else "S&amp;P 500, your holdings and your watch list"
    sub = f"{scope}. Current month plus two. Generated {today.isoformat()}."
    if not meta["share"] and positions["as_of"]:
        sub += f" Ledger as of {esc(positions['as_of'])}."
    return (HTML_TEMPLATE
            .replace("__SUB__", sub)
            .replace("__TABS__", "" if meta["share"] else       # the Weekly reviews tab (DECISIONS 56) is local only
                     '<nav class="tabs"><a class="on" href="earnings_calendar.html">Calendar</a>'
                     '<a href="tier2_reviews.html">Weekly reviews</a></nav>')
            .replace("__TOP__", top)
            .replace("__BOTTOM__", bottom)
            .replace("__DATA__", json.dumps(payload).replace("</", "<\\/")))


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--positions", default=str(Path(__file__).with_name("positions.yaml")))
    ap.add_argument("--out", default=None)
    ap.add_argument("--offline", action="store_true", help="fixture data, no network, no API key")
    ap.add_argument("--fmp-quotes", action="store_true", help="also try FMP live quotes (most plans serve only a few symbols; see --fmp-check)")
    ap.add_argument("--no-quotes", action="store_true", help=argparse.SUPPRESS)      # old name: FMP quotes are now opt-in
    ap.add_argument("--share", action="store_true", help="S&P 500 only; no positions data in the output; never touches research.db")
    ap.add_argument("--no-db", action="store_true", help="ignore research.db: do not read it and do not save this run to it")
    ap.add_argument("--fmp-history", action="store_true", help="also fetch daily closes from FMP (most plans serve only a few symbols)")
    ap.add_argument("--no-history", action="store_true", help=argparse.SUPPRESS)     # old name: FMP history is now opt-in
    ap.add_argument("--fmp-check", action="store_true", help="diagnostic: test which FMP endpoints and symbols your plan allows, then stop")
    ap.add_argument("--av-check", action="store_true", help="diagnostic: 2 Alpha Vantage requests, to prove the key works, then stop")
    ap.add_argument("--db", default=None, help="database file (default: $RESEARCH_DB or research.db beside sentinel_db.py). With --offline, "
                                               "the database is only READ, and only if you name it here")
    ap.add_argument("--today", default=None, help="override today's date (YYYY-MM-DD), for testing")
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    if args.fmp_check:
        fmp_check(fmp_key(), today)
        return
    if args.av_check:
        raise SystemExit(av_check(av_key(), today))
    positions = load_positions(args.positions)
    if args.share:
        positions = {"as_of": None, "held": [], "watch": [], "needs_confirmation": []}
    out_path = args.out or ("earnings_calendar_shareable.html" if args.share else "earnings_calendar.html")

    # research.db: used when it exists. --share and --no-db never touch it; --offline only reads a database you name.
    conn, dbstate = None, None
    if db is not None and not args.share and not args.no_db and (not args.offline or args.db):
        try:
            conn = db.connect(args.db, create=False)
            if not args.offline:
                db.init_schema(conn)          # brings an older database up to date; never deletes anything
        except FileNotFoundError:
            conn = None                       # no research.db yet: the page is simply the classic calendar
        except Exception as e:  # noqa: BLE001
            conn, dbstate = None, {"state": "error", "message": str(e)}

    key, quotes, quote_errs = None, {}, []
    if args.offline:
        print("OFFLINE demo: fixture data, nothing is fetched.")
        sp500, cal = DEMO_SP500, demo_calendar(today)
        quotes = {k: v for k, v in DEMO_QUOTES.items() if k in {p["ticker"] for p in positions["held"]}}
    else:
        akey = av_key()
        if not akey:
            raise SystemExit("Set ALPHAVANTAGE_API_KEY first:  python3 sentinel_pipeline.py av-key   (or use --offline).")
        print("Fetching S&P 500 constituents (Wikipedia) and the Alpha Vantage earnings calendar...")
        sp500 = fetch_sp500_constituents()
        tracked = {p["ticker"] for p in positions["held"] + positions["watch"]}
        cal, av_warnings = get_earnings_rows(akey, today, tracked, conn)
        for w in av_warnings:
            print(f"WARNING: {w}")
        if args.fmp_quotes or args.fmp_history:
            key = fmp_key()
        if positions["held"] and args.fmp_quotes:
            print(f"Fetching live prices for {len(positions['held'])} holdings...")
            quotes, quote_errs = fetch_quotes(key, [p["ticker"] for p in positions["held"]])

    ctx = None
    if conn is not None:
        try:
            ctx = load_db_context(conn, today, [p["ticker"] for p in positions["held"] + positions["watch"]])
            dbstate = {"state": "ok", "screen": ctx["screen"], "n_dossiers": len(ctx["dossiers"])}
        except Exception as e:  # noqa: BLE001 -- the database is a bonus; the page must still be made
            ctx, dbstate = None, {"state": "error", "message": str(e)}

    data = build(today, positions, sp500, cal, quotes, ctx)
    meta = {"offline": args.offline, "share": args.share, "db": dbstate}
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(render_html(today, positions, data, meta))

    # console summary, so you don't have to open the page to see what's off
    kinds = defaultdict(int)
    for i in data["items"]:
        kinds[(i["type"], i["kind"])] += 1
    print(f"\n{sum(kinds.values())} items on the calendar "
          f"({kinds[('earnings', 'held')]} held, {kinds[('earnings', 'watch')]} watch, "
          f"{kinds[('earnings', 'sp500')]} S&P 500, {sum(v for (t, _), v in kinds.items() if t == 'gate')} ledger gates).")
    if positions["held"] and not args.offline and args.fmp_quotes:
        print(f"Live prices: {len(quotes)}/{len(positions['held'])} holdings."
              + (" Failed: " + ", ".join(f"{s} ({why})" for s, why in quote_errs) if quote_errs else ""))
    if ctx is not None and positions["held"]:
        have = sum(1 for p in positions["held"] if p["ticker"] in ctx["prices"])
        print(f"Stored daily prices: {have} of {len(positions['held'])} holdings have one"
              + ("." if have else ". None yet: run  python3 sentinel_scan.py --quotes  (after 4pm New York time)."))
    if ctx is not None:
        scr = ctx["screen"]
        print("research.db: " + (f"scanner run #{scr['run_id']} from {scr['run_at'][:10]} ({_days(scr['age'])} old), "
                                 f"{len(ctx['candidates'])} NEW candidate(s), {len(ctx['dossiers'])} name(s) with a Finviz dossier, "
                                 f"{len(data['changes'])} changed since the last snapshot." if scr else "no scanner runs yet."))
    elif dbstate and dbstate["state"] == "error":
        print(f"research.db: could not be read ({dbstate['message']}). The calendar was made without it.")
    warns = [c for c in data["checks"] if c["level"] == "warn"]
    if warns:
        print(f"\n{len(warns)} ledger checks:")
        for c in warns:
            print(f"  {c['ticker'] or '-':8} {c['text']}")
    if conn is not None and not args.offline:
        try:
            rid, saved = save_calendar_run(conn, today, positions, data, quotes,
                                           (today - timedelta(days=LOOKBACK_DAYS)).isoformat(), end_of_month_two_ahead(today).isoformat())
            print(f"Saved calendar run #{rid} to research.db" + (" (positions.yaml changed, so a new copy was saved too)." if saved else "."))
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: could not save this run to research.db ({e}). The page is fine.")
    if conn is not None and not args.offline and args.fmp_history and key:
        try:
            syms = list(dict.fromkeys([p["ticker"] for p in positions["held"] + positions["watch"]]
                                      + [c["ticker"] for c in (ctx["candidates"] if ctx else [])]))
            h = refresh_price_history(conn, key, today, syms)
            print(f"Price history: {h['ok']}/{h['todo']} name(s) fetched ({h['fresh']} already fresh), "
                  f"{h['new_closes']} new or changed close(s) stored."
                  + (" Failed: " + ", ".join(f"{s} ({why})" for s, why in h["errs"]) if h["errs"] else ""))
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: could not update price history ({e}). The page is fine.")
    if conn is not None:
        conn.close()
    print(f"\nDone. Open {out_path} in your browser.")


if __name__ == "__main__":
    main()

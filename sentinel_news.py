#!/usr/bin/env python3
"""
sentinel_news.py -- the daily news digest for your holdings (DECISIONS.md 60): the Holdings tab of the site.

Once per weekday, after the close, for every HELD US name in positions.yaml (watch-list and Canadian names are
skipped by your choice):
  1. reads the name's Finviz page (the same page the dossiers use) and keeps only headlines it has not seen;
  2. asks Claude ONCE for all names together (your subscription, no web search, $0): for each name a 2-3 sentence
     summary, the sentiment of that day's news for a shareholder (positive / neutral / negative), a one-line reason,
     and whether the news touches the name's own gate or thesis. Names with no new headlines skip Claude ('none');
  3. stores one row per name per day in research.db (table news_digest), never overwritten;
  4. with --site, writes site/data/news.json (the last 60 days) for the Holdings tab.

Display only: Tier 1 and Tier 2 never read the digest. It never trades and never edits your files.

  python3 sentinel_news.py                 today's digest (or the previous trading day's before 9:30 / on weekends)
  python3 sentinel_news.py --site          ... and write site/data/news.json
  python3 sentinel_news.py --site-only     only rewrite site/data/news.json from the database (no network, no Claude)
  python3 sentinel_news.py --show          the last few days, in the Terminal

The 08:15 retry of the daily job runs it too, but it only does the names still missing for that trading day, so a
normal morning does nothing and costs nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sentinel_db as db  # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None

MODEL = "claude-sonnet-5"
SITE_DAYS = 60                     # days shown on the Holdings tab (all days stay in the database)
FIRST_RUN_DAYS = 3                 # on a name's first digest, only headlines from the last few days count as new
SEEN_DAYS = 21                     # a headline already used in the last 3 weeks is never "new" again
REUSE_PAGE_HOURS = 6               # a Finviz page fetched this recently (e.g. by the Saturday run) is reused
MAX_HEADLINES = 15                 # per name per day, newest first
SENTIMENTS = ("positive", "neutral", "negative")
NON_US = {"TO", "V", "CN", "NE"}
QUOTE_URL = "https://finviz.com/quote.ashx?t={}"

SYSTEM = """You write the daily news digest for one private investor's stock holdings. For each holding you get the
investor's own gate (the condition the investor is watching for that stock) and the NEW headlines published since the
last digest (headline text, source, date). Work only from those headlines: do not add facts, numbers or events that
are not in them, do not search, do not predict prices or the outcome of trials, rulings or deals, and give no
buy/sell advice.

For each holding decide:
- sentiment: the tone of THESE headlines for someone who owns the stock: "positive", "neutral" or "negative".
  Routine items (conference appearances, generic market wraps, lists of 'stocks to watch') are neutral.
- gate_relevant: true only if a headline bears directly on the holding's stated gate or thesis.
- reason: one line (max 140 characters) saying why.
- summary: two or three plain sentences on what the news says.

Answer with exactly one JSON object inside <result></result> tags and nothing after it:
{"digests": [{"ticker": "...", "sentiment": "...", "gate_relevant": true|false, "reason": "...", "summary": "..."}]}
Include every ticker you were given, once. Educational summary only; not investment advice."""


# ------------------------------------------------------------------------------------------------ time
def digest_date(now_et):
    """The trading day the news belongs to: today after the close, the previous trading day before the open or on
    a weekend; today while the market is open (a manual run during the day)."""
    wd = now_et.weekday()
    if wd >= 5:
        return (now_et.date() - timedelta(days=wd - 4)).isoformat()
    if now_et.hour * 60 + now_et.minute < 9 * 60 + 30:
        d = now_et.date() - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.isoformat()
    return now_et.date().isoformat()


def now_et():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:                  # noqa: BLE001
        return None


# ------------------------------------------------------------------------------------------------ holdings
def holdings(positions_path):
    """[(ticker, name, gate)] for the HELD US names in positions.yaml, in file order."""
    if yaml is None:
        raise RuntimeError("pyyaml is not installed. Run: python3 -m pip install pyyaml")
    raw = yaml.safe_load(Path(positions_path).read_text(encoding="utf-8")) or {}
    out, seen = [], set()
    for p in raw.get("held") or []:
        t = str(p.get("ticker", "")).strip().upper()
        if not t or t in seen or ("." in t and t.rsplit(".", 1)[-1] in NON_US):
            continue
        seen.add(t)
        out.append((t, p.get("name") or t, p.get("gate") or p.get("thesis") or ""))
    return out


# ------------------------------------------------------------------------------------------------ headlines
def fetch_page(ticker):
    """Fetch and parse one Finviz quote page (the dossier parser); returns the parsed data dict or None.
    Replaced by a fake in the tests."""
    import hashlib
    import sentinel_scan as scan
    import sentinel_finviz as fq
    html = scan.fetch_quote_html(ticker)
    if html is None:
        return None
    data, ok, notes = fq.parse_quote_page(html)
    return {"data": data, "ok": ok, "notes": notes, "sha": hashlib.sha1(html.encode("utf-8", "ignore")).hexdigest()}


def page_for(conn, ticker, fetch=fetch_page, reuse_hours=REUSE_PAGE_HOURS):
    """The newest Finviz data for a ticker: reused if fetched in the last few hours, else fetched and stored as a
    dossier (reason NEWS). (data, fetched_now) or (None, False)."""
    recent = db.recent_dossier(conn, ticker, reuse_hours / 24) if reuse_hours > 0 else None
    if recent:
        return recent["data"], False
    got = fetch(ticker)
    if not got:
        return None, False
    db.insert_dossier(conn, ticker=ticker, data=got["data"], parse_ok=got["ok"], parse_notes="; ".join(got["notes"]) or None,
                      reason="NEWS", html_sha1=got.get("sha"))
    return (got["data"], True) if got["ok"] else (None, True)


def new_headlines(conn, ticker, news, day):
    """Headlines not used in any digest of the last 3 weeks, from the last few days on a first run, newest first."""
    past = db.news_digests(conn, ticker=ticker, since=(date.fromisoformat(day) - timedelta(days=SEEN_DAYS)).isoformat())
    seen = {(h.get("date_iso") or h.get("date"), h.get("headline")) for d in past for h in d["headlines"]}
    earlier = [d["digest_date"] for d in past if d["digest_date"] < day]
    # from the previous digest's day on (the seen-set drops what was already used); a first digest looks back a few days
    floor = max(earlier) if earlier else (date.fromisoformat(day) - timedelta(days=FIRST_RUN_DAYS)).isoformat()
    out = []
    for n in news or []:
        iso = n.get("date_iso")
        key = (iso or n.get("date"), n.get("headline"))
        if not n.get("headline") or key in seen or (iso and (iso < floor or iso > day)):
            continue
        seen.add(key)
        out.append({"date_iso": iso, "date": n.get("date"), "time": n.get("time"), "headline": n["headline"][:300],
                    "source": n.get("source")})
    return out[:MAX_HEADLINES]


# ------------------------------------------------------------------------------------------------ Claude
def ask_claude(system, prompt):
    """One Claude Code run on the subscription, no tools. Returns the answer text. Replaced by a fake in the tests."""
    import sentinel_tier2 as t2
    token = t2.env_value("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        raise RuntimeError("no CLAUDE_CODE_OAUTH_TOKEN (claude setup-token, then python3 sentinel_tier2.py --save-token)")
    d = t2.subscription_run(MODEL, system, prompt, token, tools="", max_turns=1, timeout=900)
    return str(d.get("result") or "")


def build_prompt(day, batch):
    lines = [f"Trading day: {day}. Holdings with new headlines: {len(batch)}.", ""]
    for t, name, gate, heads in batch:
        lines.append(f"## {t} ({name})")
        lines.append(f"Gate: {gate or 'none written'}")
        for h in heads:
            lines.append(f"- [{h.get('date_iso') or h.get('date') or '?'} {h.get('time') or ''}] {h['headline']}"
                         + (f" ({h['source']})" if h.get("source") else ""))
        lines.append("")
    lines.append("Follow the output format exactly.")
    return "\n".join(lines)


_RESULT = re.compile(r"<result>\s*(.*?)\s*</result>", re.S)


def parse_answer(text, asked):
    """{ticker: {sentiment, gate_relevant, reason, summary}} for the tickers asked; anything else is dropped."""
    found = _RESULT.findall(text or "")
    if not found:
        raise ValueError("the answer had no <result> block")
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", found[-1].strip())
    obj = json.loads(raw)
    out = {}
    for d in (obj.get("digests") if isinstance(obj, dict) else None) or []:
        if not isinstance(d, dict):
            continue
        t = str(d.get("ticker", "")).strip().upper()
        s = str(d.get("sentiment", "")).strip().lower()
        if t not in asked or s not in SENTIMENTS or t in out:
            continue
        out[t] = {"sentiment": s, "gate_relevant": d.get("gate_relevant") is True,
                  "reason": str(d.get("reason") or "")[:200].strip(), "summary": str(d.get("summary") or "")[:900].strip()}
    return out


# ------------------------------------------------------------------------------------------------ the run
def run(conn, positions_path, day, fetch=fetch_page, ask=ask_claude, sleep=None, reuse_hours=REUSE_PAGE_HOURS):
    """Make the digest for one trading day. Returns (code, lines). 0 = done (or nothing left to do), 1 = some names
    could not be done (they are retried by the next run)."""
    import time
    sleep = sleep or (lambda s: time.sleep(s))
    names = holdings(positions_path)
    if not names:
        return 0, ["No held US names in positions.yaml: nothing to do."]
    done = {d["ticker"] for d in db.news_digests(conn, since=day) if d["digest_date"] == day}
    todo = [n for n in names if n[0] not in done]
    if not todo:
        return 0, [f"News for {day}: already done for all {len(names)} holdings."]
    lines, batch, quiet, failed, fails_in_row, fetched = [], [], [], [], 0, 0
    for i, (t, name, gate) in enumerate(todo):
        data, fetched_now = page_for(conn, t, fetch, reuse_hours)
        if fetched_now:
            fetched += 1
            sleep(3)                                       # polite: one page at a time
        if data is None:
            failed.append(t)
            fails_in_row += 1
            if fails_in_row >= 2:
                failed += [n[0] for n in todo[i + 1:]]
                lines.append("Stopped reading pages: two in a row could not be read (Finviz blocking or changed).")
                break
            continue
        fails_in_row = 0
        heads = new_headlines(conn, t, data.get("news"), day)
        (batch.append((t, name, gate, heads)) if heads else quiet.append(t))
    for t in quiet:
        db.insert_news_digest(conn, ticker=t, digest_date=day, sentiment="none", summary="No new headlines.", model=None)
    answered = {}
    if batch:
        try:
            answered = parse_answer(ask(SYSTEM, build_prompt(day, batch)), {b[0] for b in batch})
        except Exception as e:                             # noqa: BLE001  (no answer: nothing stored, retried next run)
            lines.append(f"Claude did not give a usable digest ({type(e).__name__}: {str(e)[:160]}); retried next run.")
        for t, name, gate, heads in batch:
            a = answered.get(t)
            if a is None:
                failed.append(t)
                continue
            db.insert_news_digest(conn, ticker=t, digest_date=day, sentiment=a["sentiment"], gate_relevant=a["gate_relevant"],
                                  reason=a["reason"], summary=a["summary"], headlines=heads, model=MODEL + " (subscription)")
    rows = {d["ticker"]: d for d in db.news_digests(conn, since=day) if d["digest_date"] == day}
    shown = [f"{t} {rows[t]['sentiment']}" + (" (gate)" if rows[t]["gate_relevant"] else "")
             for t, _, _ in names if t in rows and rows[t]["sentiment"] != "none"]
    lines.insert(0, f"News for {day}: " + (", ".join(shown) if shown else "no new headlines")
                 + (f"; {len(quiet)} with no new headlines" if quiet and shown else "")
                 + f" ({fetched} page(s) read, {'1 Claude call' if batch else 'no Claude call'}).")
    alerts = [f"{t} {rows[t]['sentiment']}" + (" (gate)" if rows[t]["gate_relevant"] else "")
              for t, _, _ in names if t in rows and (rows[t]["sentiment"] == "negative" or rows[t]["gate_relevant"])
              and rows[t]["digest_date"] == day and t in {b[0] for b in batch}]
    if alerts:
        lines.append("NEWS ALERTS: " + "; ".join(alerts))
    if failed:
        lines.append(f"Not done (retried by the next run): {', '.join(dict.fromkeys(failed))}.")
    return (1 if failed else 0), lines


# ------------------------------------------------------------------------------------------------ site + show
def site_payload(conn, positions_path, today_iso):
    names = holdings(positions_path)
    since = (date.fromisoformat(today_iso) - timedelta(days=SITE_DAYS)).isoformat()
    rows = db.news_digests(conn, since=since)
    by = {}
    for r in rows:
        by.setdefault(r["ticker"], []).append({"date": r["digest_date"], "sentiment": r["sentiment"], "gate_relevant": r["gate_relevant"],
                                               "reason": r["reason"], "summary": r["summary"],
                                               "headlines": [{k: h.get(k) for k in ("date_iso", "time", "headline", "source")}
                                                             for h in r["headlines"]]})
    return {"schema": 1, "days_shown": SITE_DAYS,
            "holdings": [{"ticker": t, "name": n, "gate": g, "finviz": QUOTE_URL.format(t), "days": by.get(t, [])} for t, n, g in names]}


def write_site(conn, positions_path, today_iso, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(site_payload(conn, positions_path, today_iso), ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def show(conn, days=5):
    rows = db.news_digests(conn)
    dates = sorted({r["digest_date"] for r in rows}, reverse=True)[:days]
    if not dates:
        print("No news digests yet.")
        return
    for d in dates:
        print(f"\n{d}")
        for r in [x for x in rows if x["digest_date"] == d]:
            print(f"  {r['ticker']:<6} {r['sentiment']:<9}{' GATE' if r['gate_relevant'] else '     '} {r['reason'] or r['summary'] or ''}"[:150])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--positions", default=str(HERE / "positions.yaml"))
    ap.add_argument("--db", default=None)
    ap.add_argument("--date", default=None, metavar="YYYY-MM-DD", help="the trading day (default: worked out from New York time)")
    ap.add_argument("--site", nargs="?", const=str(HERE / "site" / "data" / "news.json"), default=None, metavar="FILE",
                    help="also write the Holdings tab's data (default site/data/news.json)")
    ap.add_argument("--site-only", action="store_true", help="only rewrite the site file from the database")
    ap.add_argument("--show", action="store_true", help="print the last few days")
    a = ap.parse_args(argv)
    conn = db.connect(a.db)
    try:
        db.init_schema(conn)
        if a.show:
            show(conn)
            return 0
        et = now_et()
        day = a.date or (digest_date(et) if et else date.today().isoformat())
        code, lines = (0, []) if a.site_only else run(conn, a.positions, day)
        print("\n".join(lines))
        if a.site or a.site_only:
            target = a.site or str(HERE / "site" / "data" / "news.json")
            write_site(conn, a.positions, day, target)
            print(f"Site data written: {target}")
        return code
    except (RuntimeError, FileNotFoundError, db.SchemaError) as e:
        print(f"sentinel_news.py: {e}")
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

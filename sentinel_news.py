#!/usr/bin/env python3
"""
sentinel_news.py -- the daily news digest for your holdings (DECISIONS.md 60): the Holdings tab of the site.

Once per weekday, after the close, for every HELD US name in positions.yaml (watch-list and Canadian names are
skipped by your choice):
  1. gathers candidate headlines from two free sources: the name's Finviz page (the dossier page) and Alpha Vantage's
     news feed (with links; one request per holding), keeping only items it has not used before (DECISIONS.md 61);
  2. asks Claude ONCE for all names together (your subscription, $0) with web search allowed but capped: at most one
     search per holding (11 in all) to catch what the list misses, then the TOP 2-3 items per name from reliable
     sources (company releases, SEC, Reuters, Bloomberg, WSJ, FT, AP, CNBC). From those only: a 2-3 sentence summary,
     the sentiment for a shareholder (positive / neutral / negative / none), a one-line reason, and whether the news
     touches the name's own gate or thesis;
  3. stores one row per name per day in research.db (table news_digest), never overwritten;
  4. with --site, writes site/data/news.json (the last 60 days) for the Holdings tab.

If one source is down (Finviz changed, Alpha Vantage daily limit reached), the others carry on and the run says so.
Display only: Tier 1 and Tier 2 never read the digest. It never trades and never edits your files.

  python3 sentinel_news.py                 today's digest (or the previous trading day's before 9:30 / on weekends)
  python3 sentinel_news.py --site          ... and write site/data/news.json
  python3 sentinel_news.py --site-only     only rewrite site/data/news.json from the database (no network, no Claude)
  python3 sentinel_news.py --show          the last few days, in the Terminal
  python3 sentinel_news.py --check-sources one Alpha Vantage news request, to prove the free key includes news

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

RELIABLE = ("reuters.com", "bloomberg.com", "wsj.com", "ft.com", "apnews.com", "cnbc.com", "barrons.com", "marketwatch.com",
            "nytimes.com", "economist.com", "sec.gov", "businesswire.com", "prnewswire.com", "globenewswire.com", "investing.com")
MAX_SEARCHES = 11                  # Claude's web searches per daily run, all holdings together (DECISIONS.md 61)
MAX_TURNS = 16                     # agent turns per run: the searches plus the answer
AV_MIN_RELEVANCE = 0.3             # Alpha Vantage relevance of an article to the ticker (0-1)
AV_PER_TICKER = 20                 # newest articles asked per holding
TOP_PER_NAME = 3                   # the summary rests on at most this many items

SYSTEM = f"""You write the daily news digest for one private investor's stock holdings. For each holding you get the
investor's own gate (the condition the investor is watching for that stock) and a list of NEW candidate headlines
gathered since the last digest from Finviz and Alpha Vantage (title, source, date, link when known).

How to work, in this order:
1. Read the candidate headlines.
2. You MAY run web searches to catch anything important the list misses, at most ONE search per holding and at most
   {MAX_SEARCHES} searches in total. Search only for news from the last few days. Skip the search when the list is already
   clear. Prefer primary and reliable sources: the company's own press releases and investor-relations pages, SEC
   filings, Reuters, Bloomberg, the Wall Street Journal, the Financial Times, AP, CNBC.
3. For each holding pick the TOP 2-3 most relevant items, from reliable sources where possible, and base the summary
   and sentiment ONLY on those. Never state a fact that is not in an item you picked. Do not predict prices or the
   outcome of trials, rulings or deals, and give no buy/sell advice.

For each holding decide:
- sentiment: the tone of the picked items for someone who owns the stock: "positive", "neutral", "negative", or "none"
  when there is no relevant news at all. Routine items (conference appearances, market wraps, 'stocks to watch'
  lists) are neutral.
- gate_relevant: true only if a picked item bears directly on the holding's stated gate or thesis.
- reason: one line (max 140 characters) saying why.
- summary: two or three plain sentences on what the picked items say (empty when "none").
- top: the 2-3 picked items, each {{"title", "source", "url", "date"}} (url must be the real link; date YYYY-MM-DD).

Answer with exactly one JSON object inside <result></result> tags and nothing after it:
{{"digests": [{{"ticker": "...", "sentiment": "...", "gate_relevant": true|false, "reason": "...", "summary": "...",
               "top": [{{"title": "...", "source": "...", "url": "https://...", "date": "YYYY-MM-DD"}}]}}]}}
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


def finviz_items(news):
    """Finviz headlines in the common item shape."""
    return [{"date_iso": n.get("date_iso"), "time": n.get("time"), "headline": n.get("headline"), "source": n.get("source"),
             "url": None, "via": "finviz"} for n in news or [] if n.get("headline")]


def fetch_av_news(key, ticker, since_iso, until_iso):
    """Alpha Vantage NEWS_SENTIMENT for one ticker: articles where the ticker's relevance is at least AV_MIN_RELEVANCE,
    as common items (with links). Raises the calendar's AvError on refusals (daily limit, bad key...). Replaced by a
    fake in the tests."""
    import sentinel_calendar as cal
    body = cal._av_get({"function": "NEWS_SENTIMENT", "tickers": ticker, "sort": "LATEST", "limit": str(AV_PER_TICKER),
                        "time_from": since_iso.replace("-", "") + "T0000"}, key)
    try:
        feed = (json.loads(body) or {}).get("feed") or []
    except ValueError:
        raise cal.AvError("Alpha Vantage news: the answer was not JSON") from None
    out = []
    for a in feed:
        rel = next((float(x.get("relevance_score") or 0) for x in a.get("ticker_sentiment") or []
                    if str(x.get("ticker", "")).upper() == ticker.upper()), 0.0)
        tp = str(a.get("time_published") or "")
        iso = f"{tp[:4]}-{tp[4:6]}-{tp[6:8]}" if len(tp) >= 8 else None
        if rel < AV_MIN_RELEVANCE or not a.get("title") or (iso and iso > until_iso):
            continue
        out.append({"date_iso": iso, "time": f"{tp[9:11]}:{tp[11:13]}" if len(tp) >= 13 else None, "headline": a["title"],
                    "source": a.get("source"), "url": a.get("url"), "via": "alphavantage"})
    return out


def _key(item):
    return (item.get("date_iso"), " ".join(str(item.get("headline") or "").lower().split())[:120])


def new_items(conn, ticker, items, day):
    """Candidate items not used in any digest of the last 3 weeks, from the previous digest's day on (a first digest
    looks back a few days), duplicates across sources merged (the one with a link wins), newest first."""
    past = db.news_digests(conn, ticker=ticker, since=(date.fromisoformat(day) - timedelta(days=SEEN_DAYS)).isoformat())
    seen = {_key(h) for d in past for h in d["headlines"]}
    earlier = [d["digest_date"] for d in past if d["digest_date"] < day]
    floor = max(earlier) if earlier else (date.fromisoformat(day) - timedelta(days=FIRST_RUN_DAYS)).isoformat()
    picked = {}
    for it in items:
        iso, k = it.get("date_iso"), _key(it)
        if not it.get("headline") or k in seen or (iso and (iso < floor or iso > day)):
            continue
        if k not in picked or (it.get("url") and not picked[k].get("url")):
            picked[k] = {**it, "headline": str(it["headline"])[:300]}
    return sorted(picked.values(), key=lambda x: (x.get("date_iso") or "", x.get("time") or ""), reverse=True)[:MAX_HEADLINES]


def reliable(url):
    m = re.match(r"https?://([^/]+)", url or "")
    host = (m.group(1).lower() if m else "")
    return any(host == d or host.endswith("." + d) for d in RELIABLE) or host.startswith("investor") or host.startswith("ir.")


# ------------------------------------------------------------------------------------------------ Claude
def ask_claude(system, prompt):
    """One Claude Code run on the subscription with web search only (capped by the prompt and by MAX_TURNS).
    Returns (answer text, searches made). Replaced by a fake in the tests."""
    import sentinel_tier2 as t2
    token = t2.env_value("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        raise RuntimeError("no CLAUDE_CODE_OAUTH_TOKEN (claude setup-token, then python3 sentinel_tier2.py --save-token)")
    d = t2.subscription_run(MODEL, system, prompt, token, tools="WebSearch", max_turns=MAX_TURNS, timeout=1500)
    return str(d.get("result") or ""), int(d.get("_searches") or 0)


def build_prompt(day, batch):
    lines = [f"Trading day: {day}. Holdings: {len(batch)}. Web searches allowed: at most one per holding, "
             f"{min(MAX_SEARCHES, len(batch))} in total.", ""]
    for t, name, gate, items in batch:
        lines.append(f"## {t} ({name})")
        lines.append(f"Gate: {gate or 'none written'}")
        if not items:
            lines.append("- (no candidate headlines from Finviz or Alpha Vantage)")
        for h in items:
            lines.append(f"- [{h.get('date_iso') or '?'} {h.get('time') or ''}] {h['headline']}"
                         + (f" ({h['source']})" if h.get("source") else "") + (f" <{h['url']}>" if h.get("url") else ""))
        lines.append("")
    lines.append("Follow the output format exactly.")
    return "\n".join(lines)


_RESULT = re.compile(r"<result>\s*(.*?)\s*</result>", re.S)


def parse_answer(text, asked):
    """{ticker: {sentiment, gate_relevant, reason, summary, top}} for the tickers asked; anything else is dropped.
    Only http(s) links are kept, at most TOP_PER_NAME items, each marked reliable or not."""
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
        if t not in asked or s not in SENTIMENTS + ("none",) or t in out:
            continue
        top = []
        for it in d.get("top") or []:
            if not isinstance(it, dict) or not it.get("title"):
                continue
            url = str(it.get("url") or "")
            url = url if url.startswith(("https://", "http://")) else None
            iso = str(it.get("date") or "")[:10]
            top.append({"headline": str(it["title"])[:300], "source": str(it.get("source") or "")[:80] or None, "url": url,
                        "date_iso": iso if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso) else None, "top": True,
                        "reliable": reliable(url)})
        out[t] = {"sentiment": s, "gate_relevant": s != "none" and d.get("gate_relevant") is True,
                  "reason": str(d.get("reason") or "")[:200].strip(), "summary": str(d.get("summary") or "")[:900].strip(),
                  "top": top[:TOP_PER_NAME]}
    return out


# ------------------------------------------------------------------------------------------------ the run
def run(conn, positions_path, day, fetch=fetch_page, ask=ask_claude, av_fetch=fetch_av_news, av_key=None, sleep=None,
        reuse_hours=REUSE_PAGE_HOURS):
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
    if av_key is None:
        import sentinel_calendar as cal
        av_key = cal.av_key() or ""
    lines, batch, failed, notes = [], [], [], []
    pages = av_ok = 0
    finviz_down = av_down = False
    fails_in_row = 0
    for t, name, gate in todo:
        items = []
        if not finviz_down:
            data, fetched_now = page_for(conn, t, fetch, reuse_hours)
            if fetched_now:
                pages += 1
                sleep(3)                                   # polite: one page at a time
            if data is None:
                fails_in_row += 1
                if fails_in_row >= 2:
                    finviz_down = True
                    notes.append("Finviz pages could not be read (two in a row): the other sources carried on.")
            else:
                fails_in_row = 0
                items += finviz_items(data.get("news"))
        if av_key and not av_down:
            past = [d["digest_date"] for d in db.news_digests(conn, ticker=t) if d["digest_date"] < day]
            since = max(past) if past else (date.fromisoformat(day) - timedelta(days=FIRST_RUN_DAYS)).isoformat()
            try:
                items += av_fetch(av_key, t, since, day)
                av_ok += 1
            except Exception as e:                         # noqa: BLE001  (limit reached, bad key...: carry on without it)
                av_down = True
                notes.append(f"Alpha Vantage news stopped ({str(e)[:120]}): the other sources carried on.")
            sleep(13)                                      # the free tier limits requests per minute
        batch.append((t, name, gate, new_items(conn, t, items, day)))
    answered, searches = {}, 0
    try:
        text, searches = ask(SYSTEM, build_prompt(day, batch))
        answered = parse_answer(text, {b[0] for b in batch})
    except Exception as e:                                 # noqa: BLE001  (no answer: nothing stored, retried next run)
        lines.append(f"Claude did not give a usable digest ({type(e).__name__}: {str(e)[:160]}); retried next run.")
    for t, name, gate, items in batch:
        a = answered.get(t)
        if a is None:
            failed.append(t)
            continue
        top_keys = {_key(x) for x in a["top"]}
        considered = [{**x, "top": False} for x in items if _key(x) not in top_keys]
        db.insert_news_digest(conn, ticker=t, digest_date=day, sentiment=a["sentiment"], gate_relevant=a["gate_relevant"],
                              reason=a["reason"], summary=a["summary"] or ("No relevant news." if a["sentiment"] == "none" else ""),
                              headlines=a["top"] + considered, model=MODEL + " (subscription)")
    rows = {d["ticker"]: d for d in db.news_digests(conn, since=day) if d["digest_date"] == day}
    shown = [f"{t} {rows[t]['sentiment']}" + (" (gate)" if rows[t]["gate_relevant"] else "")
             for t, _, _ in names if t in rows and rows[t]["sentiment"] != "none"]
    quiet = sum(1 for t, _, _ in names if t in rows and rows[t]["sentiment"] == "none")
    lines.insert(0, f"News for {day}: " + (", ".join(shown) if shown else "no relevant news")
                 + (f"; {quiet} with no relevant news" if quiet and shown else "")
                 + f" ({pages} Finviz page(s), {av_ok} Alpha Vantage request(s), {searches} web search(es), 1 Claude call).")
    lines += notes
    alerts = [f"{t} {rows[t]['sentiment']}" + (" (gate)" if rows[t]["gate_relevant"] else "")
              for t, _, _ in names if t in rows and t in {b[0] for b in batch}
              and (rows[t]["sentiment"] == "negative" or rows[t]["gate_relevant"])]
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
                                               "headlines": [{k: h.get(k) for k in ("date_iso", "time", "headline", "source", "url", "top", "reliable")}
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
    ap.add_argument("--check-sources", action="store_true", help="one Alpha Vantage news request for the first holding")
    a = ap.parse_args(argv)
    conn = db.connect(a.db)
    try:
        db.init_schema(conn)
        if a.show:
            show(conn)
            return 0
        if a.check_sources:
            import sentinel_calendar as cal
            key = cal.av_key()
            names = holdings(a.positions)
            if not key or not names:
                print("No ALPHAVANTAGE_API_KEY (python3 sentinel_pipeline.py av-key) or no held US names.")
                return 2
            t = names[0][0]
            since = (date.today() - timedelta(days=7)).isoformat()
            try:
                items = fetch_av_news(key, t, since, date.today().isoformat())
            except Exception as e:                         # noqa: BLE001
                print(f"Alpha Vantage news: NOT available ({e}). The digest will use Finviz and Claude's searches only.")
                return 1
            print(f"Alpha Vantage news works: {len(items)} relevant article(s) about {t} in the last 7 days"
                  + (f", e.g. '{items[0]['headline'][:80]}' ({items[0].get('source')})" if items else "") + ". 1 request used.")
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

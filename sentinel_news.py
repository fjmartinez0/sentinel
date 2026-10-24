#!/usr/bin/env python3
"""
sentinel_news.py -- the daily news digest for your holdings (DECISIONS.md 60): the Holdings tab of the site.

Once per weekday, after the close, for every HELD US name in positions.yaml (watch-list and Canadian names are
skipped by your choice):
  1. gathers candidate headlines from three free sources, keeping only items it has not used before: the name's
     Finviz page (the dossier page), Finnhub's company news (articles with links; free key) and the company's own SEC
     filings (8-K, 6-K, 10-Q, 10-K: official events, no key) (DECISIONS.md 61-62);
  2. asks Claude ONCE for all names together (your subscription, $0) with web search allowed but capped: at most one
     search per holding (11 in all) to catch what the list misses, then the TOP 2-3 items per name from reliable
     sources (company releases, SEC, Reuters, Bloomberg, WSJ, FT, AP, CNBC). From those only: a 2-3 sentence summary,
     the sentiment for a shareholder (positive / neutral / negative / none), a one-line reason, and whether the news
     touches the name's own gate or thesis;
  3. stores one row per name per day in research.db (table news_digest), never overwritten;
  4. with --site, writes site/data/news.json (the last 60 days) for the Holdings tab.

If one source is down (Finviz changed, Finnhub refused, SEC unreachable), the others carry on and the run says so.
Display only: Tier 1 and Tier 2 never read the digest. It never trades and never edits your files.

  python3 sentinel_news.py                 today's digest (or the previous trading day's before 9:30 / on weekends)
  python3 sentinel_news.py --site          ... and write site/data/news.json
  python3 sentinel_news.py --site-only     only rewrite site/data/news.json from the database (no network, no Claude)
  python3 sentinel_news.py --show          the last few days, in the Terminal
  python3 sentinel_news.py --check-sources prove Finnhub (1 request) and the SEC (2 requests) answer, then stop
  python3 sentinel_news.py --finnhub-key   save your free Finnhub key in .env (hidden prompt)
  python3 sentinel_news.py --sec-contact   save the contact e-mail the SEC asks every program to send (in .env)

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
FINNHUB_URL = "https://finnhub.io/api/v1/company-news"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
SEC_FORMS = {"8-K": "8-K", "8-K/A": "8-K (amended)", "6-K": "6-K", "10-Q": "10-Q quarterly report",
             "10-K": "10-K annual report", "20-F": "20-F annual report", "40-F": "40-F annual report"}
SEC_ITEMS = {"1.01": "material agreement", "1.02": "agreement terminated", "1.03": "bankruptcy", "1.05": "cybersecurity incident",
             "2.01": "acquisition or disposal", "2.02": "results of operations", "2.03": "new debt", "2.05": "exit or restructuring costs",
             "2.06": "impairment", "3.01": "listing notice", "3.02": "unregistered share sale", "4.01": "auditor change",
             "4.02": "earlier financials no longer reliable", "5.01": "change of control", "5.02": "executive or director change",
             "5.03": "bylaws change", "5.07": "shareholder vote", "7.01": "Regulation FD disclosure", "8.01": "other events"}
CACHE = HERE / "cache"
FINNHUB_PER_TICKER = 20            # newest articles kept per holding
TOP_PER_NAME = 3                   # the summary rests on at most this many items

SYSTEM = f"""You write the daily news digest for one private investor's stock holdings. For each holding you get the
investor's own gate (the condition the investor is watching for that stock) and a list of NEW candidate headlines
gathered since the last digest from Finviz, Finnhub and the company's SEC filings (title, source, date, link when
known). Items marked [OFFICIAL FILING] are the company's own filings with the SEC: when one exists, it is normally among
the top items.

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


def env(key):
    import sentinel_tier2 as t2
    return t2.env_value(key)


def _get(url, params=None, headers=None, timeout=30):
    """One GET. Returns the parsed JSON. Errors never contain the URL (a key may be in it)."""
    import requests
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
    except Exception as e:                 # noqa: BLE001
        raise RuntimeError(f"could not reach {url.split('/')[2]} ({type(e).__name__})") from None
    if r.status_code != 200:
        hint = {401: "the key was refused", 403: "access refused (a paid plan or a missing contact e-mail?)",
                429: "too many requests"}.get(r.status_code, "")
        raise RuntimeError(f"{url.split('/')[2]} answered HTTP {r.status_code}" + (f": {hint}" if hint else ""))
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"{url.split('/')[2]} did not answer with JSON") from None


def fetch_finnhub(key, ticker, since_iso, until_iso):
    """Finnhub company news for one ticker as common items (with links). Replaced by a fake in the tests."""
    data = _get(FINNHUB_URL, params={"symbol": ticker, "from": since_iso, "to": until_iso, "token": key})
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Finnhub: {str(data['error'])[:120]}")
    out = []
    for a in (data if isinstance(data, list) else [])[:FINNHUB_PER_TICKER * 2]:
        if not a.get("headline"):
            continue
        try:
            dt = datetime.fromtimestamp(int(a.get("datetime") or 0)).astimezone()
            iso, hm = dt.date().isoformat(), dt.strftime("%H:%M")
        except (TypeError, ValueError, OSError):
            iso, hm = None, None
        if iso and (iso < since_iso or iso > until_iso):
            continue
        out.append({"date_iso": iso, "time": hm, "headline": str(a["headline"]), "source": a.get("source"),
                    "url": a.get("url") if str(a.get("url") or "").startswith(("https://", "http://")) else None, "via": "finnhub"})
    return out[:FINNHUB_PER_TICKER]


def sec_headers():
    contact = env("SEC_CONTACT")
    if not contact:
        raise RuntimeError("no SEC contact e-mail yet (python3 sentinel_news.py --sec-contact)")
    return {"User-Agent": f"Sentinel personal stock research {contact}", "Accept-Encoding": "gzip, deflate"}


def sec_cik(ticker, headers=None):
    """The SEC's company number for a ticker, from its public ticker list (cached for 30 days in cache/)."""
    CACHE.mkdir(exist_ok=True)
    f = CACHE / "sec_company_tickers.json"
    fresh = f.exists() and (datetime.now().timestamp() - f.stat().st_mtime) < 30 * 86400
    if not fresh:
        data = _get(SEC_TICKERS_URL, headers=headers or sec_headers())
        f.write_text(json.dumps(data), encoding="utf-8")
    table = json.loads(f.read_text(encoding="utf-8"))
    for row in table.values() if isinstance(table, dict) else []:
        if str(row.get("ticker", "")).upper() == ticker.upper():
            return int(row["cik_str"])
    return None


def fetch_sec(ticker, since_iso, until_iso):
    """The company's own SEC filings (8-K, 6-K, 10-Q, 10-K...) filed in the window, as common items with links.
    Replaced by a fake in the tests."""
    headers = sec_headers()
    cik = sec_cik(ticker, headers)
    if cik is None:
        return []
    rec = ((_get(SEC_SUBMISSIONS_URL.format(cik=cik), headers=headers).get("filings") or {}).get("recent") or {})
    out = []
    for i, form in enumerate(rec.get("form") or []):
        day = (rec.get("filingDate") or [None] * (i + 1))[i]
        if form not in SEC_FORMS or not day or day < since_iso or day > until_iso:
            continue
        acc = str((rec.get("accessionNumber") or [""] * (i + 1))[i]).replace("-", "")
        doc = (rec.get("primaryDocument") or [""] * (i + 1))[i]
        items = [x.strip() for x in str((rec.get("items") or [""] * (i + 1))[i] or "").split(",") if x.strip()]
        what = ", ".join(SEC_ITEMS[x] for x in items if x in SEC_ITEMS) or (rec.get("primaryDocDescription") or [""] * (i + 1))[i] or ""
        out.append({"date_iso": day, "time": None, "headline": f"{SEC_FORMS[form]} filed" + (f": {what}" if what else ""),
                    "source": "SEC filing", "url": SEC_DOC_URL.format(cik=cik, acc=acc, doc=doc) if acc and doc else None,
                    "via": "sec"})
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


def official(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return bool(m) and m.group(1).lower().endswith("sec.gov")


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
            lines.append("- (no candidate headlines from Finviz, Finnhub or the SEC)")
        for h in items:
            lines.append(f"- {'[OFFICIAL FILING] ' if h.get('via') == 'sec' else ''}[{h.get('date_iso') or '?'} {h.get('time') or ''}] {h['headline']}"
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
                        "reliable": reliable(url), "official": official(url)})
        out[t] = {"sentiment": s, "gate_relevant": s != "none" and d.get("gate_relevant") is True,
                  "reason": str(d.get("reason") or "")[:200].strip(), "summary": str(d.get("summary") or "")[:900].strip(),
                  "top": top[:TOP_PER_NAME]}
    return out


# ------------------------------------------------------------------------------------------------ the run
def run(conn, positions_path, day, fetch=fetch_page, ask=ask_claude, finnhub=fetch_finnhub, sec=fetch_sec, finnhub_key=None,
        sleep=None, reuse_hours=REUSE_PAGE_HOURS):
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
    if finnhub_key is None:
        finnhub_key = env("FINNHUB_API_KEY") or ""
    lines, batch, failed, notes = [], [], [], []
    pages = fh_ok = sec_ok = 0
    finviz_down = fh_down = sec_down = False
    if not finnhub_key:
        notes.append("Finnhub skipped: no key yet (python3 sentinel_news.py --finnhub-key).")
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
        past = [d["digest_date"] for d in db.news_digests(conn, ticker=t) if d["digest_date"] < day]
        since = max(past) if past else (date.fromisoformat(day) - timedelta(days=FIRST_RUN_DAYS)).isoformat()
        if finnhub_key and not fh_down:
            try:
                items += finnhub(finnhub_key, t, since, day)
                fh_ok += 1
            except Exception as e:                         # noqa: BLE001  (refused, limit...: carry on without it)
                fh_down = True
                notes.append(f"Finnhub stopped ({str(e)[:120]}): the other sources carried on.")
            sleep(1.1)                                     # the free plan allows 60 requests a minute
        if not sec_down:
            try:
                items += sec(t, since, day)
                sec_ok += 1
            except Exception as e:                         # noqa: BLE001
                sec_down = True
                notes.append(f"SEC filings stopped ({str(e)[:120]}): the other sources carried on.")
            sleep(0.3)                                     # the SEC asks for at most 10 requests a second
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
                 + f" ({pages} Finviz page(s), {fh_ok} Finnhub and {sec_ok} SEC lookup(s), {searches} web search(es), 1 Claude call).")
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
                                               "headlines": [{k: h.get(k) for k in ("date_iso", "time", "headline", "source", "url", "top", "reliable", "official", "via")}
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


def save_setting(finnhub):
    import getpass
    import sentinel_tier2 as t2
    if finnhub:
        print("Create a free account at https://finnhub.io (Dashboard shows your API key), then paste it here.")
        v = getpass.getpass("Finnhub API key (it will not be shown), then Enter: ").strip()
        if not re.fullmatch(r"[A-Za-z0-9]{12,64}", v):
            print("That does not look like a Finnhub key (letters and digits). .env was not changed.")
            return 1
        t2.save_env_value("FINNHUB_API_KEY", v)
        print("FINNHUB_API_KEY saved in .env (private; not printed). Check it: python3 sentinel_news.py --check-sources")
        return 0
    print("The SEC asks every program that reads its data to send a contact e-mail with each request (it is only")
    print("used by the SEC to reach you if your program misbehaves). It is stored in .env, never published.")
    v = input("Your e-mail address: ").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
        print("That does not look like an e-mail address. .env was not changed.")
        return 1
    t2.save_env_value("SEC_CONTACT", v)
    print("SEC_CONTACT saved in .env. Check it: python3 sentinel_news.py --check-sources")
    return 0


def check_sources(positions_path):
    names = holdings(positions_path)
    if not names:
        print("No held US names in positions.yaml.")
        return 2
    t, today = names[0][0], date.today().isoformat()
    since = (date.today() - timedelta(days=7)).isoformat()
    ok = True
    key = env("FINNHUB_API_KEY")
    if not key:
        print("Finnhub: no key yet. Run: python3 sentinel_news.py --finnhub-key")
        ok = False
    else:
        try:
            items = fetch_finnhub(key, t, since, today)
            print(f"Finnhub works: {len(items)} article(s) about {t} in the last 7 days"
                  + (f", e.g. '{items[0]['headline'][:70]}' ({items[0].get('source')})" if items else "") + ".")
        except Exception as e:                             # noqa: BLE001
            print(f"Finnhub: NOT available ({e}).")
            ok = False
    try:
        items = fetch_sec(t, (date.today() - timedelta(days=120)).isoformat(), today)
        print(f"SEC works: {len(items)} filing(s) by {t} in the last 120 days"
              + (f", newest: {items[0]['headline'][:70]} ({items[0]['date_iso']})" if items else "") + ".")
    except Exception as e:                                 # noqa: BLE001
        print(f"SEC: NOT available ({e}).")
        ok = False
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--positions", default=str(HERE / "positions.yaml"))
    ap.add_argument("--db", default=None)
    ap.add_argument("--date", default=None, metavar="YYYY-MM-DD", help="the trading day (default: worked out from New York time)")
    ap.add_argument("--site", nargs="?", const=str(HERE / "site" / "data" / "news.json"), default=None, metavar="FILE",
                    help="also write the Holdings tab's data (default site/data/news.json)")
    ap.add_argument("--site-only", action="store_true", help="only rewrite the site file from the database")
    ap.add_argument("--show", action="store_true", help="print the last few days")
    ap.add_argument("--check-sources", action="store_true", help="prove Finnhub and the SEC answer (3 requests)")
    ap.add_argument("--finnhub-key", action="store_true", help="save your Finnhub key in .env (hidden prompt)")
    ap.add_argument("--sec-contact", action="store_true", help="save the contact e-mail the SEC requires (in .env)")
    a = ap.parse_args(argv)
    conn = db.connect(a.db)
    try:
        db.init_schema(conn)
        if a.show:
            show(conn)
            return 0
        if a.finnhub_key or a.sec_contact:
            return save_setting(finnhub=a.finnhub_key)
        if a.check_sources:
            return check_sources(a.positions)
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

"""Tests for the holdings' daily news digest (sentinel_news.py, DECISIONS.md 60-62). Fake Finviz pages, fake Finnhub
and SEC answers and a fake Claude: nothing is fetched and nothing is spent."""
import json, sys, tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sentinel_db as db, sentinel_news as news, sentinel_validate as v

results = []
def check(label, ok, detail=""):
    ok = bool(ok); results.append(ok); print(("  ok    " if ok else "  FAIL  ") + label + ("" if ok else f"  {detail}"))

ET = ZoneInfo("America/New_York")
POS = """as_of: 2026-10-20
held:
  - {ticker: NYT, name: New York Times, gate: "Subscriber growth holds in Q3"}
  - {ticker: META, name: Meta Platforms, gate: "Child-safety trial outcome; no structural remedies"}
  - {ticker: ATRL.TO, name: AtkinsRealis}
watch:
  - {ticker: PSN, name: Parsons}
"""

print("Which day, and which names")
check("after the close it is today's news", news.digest_date(datetime(2026, 10, 26, 16, 30, tzinfo=ET)) == "2026-10-26")
check("the 08:15 retry belongs to the previous trading day", news.digest_date(datetime(2026, 10, 27, 8, 15, tzinfo=ET)) == "2026-10-26")
check("Monday 08:15 belongs to Friday; Saturday belongs to Friday", news.digest_date(datetime(2026, 10, 26, 8, 15, tzinfo=ET)) == "2026-10-23"
      and news.digest_date(datetime(2026, 10, 24, 9, 37, tzinfo=ET)) == "2026-10-23")
check("reliable sources are recognised by their address", news.reliable("https://www.reuters.com/x") and news.reliable("https://investor.atmeta.com/x")
      and news.reliable("https://www.sec.gov/x") and not news.reliable("https://some-blog.example.com/x") and not news.reliable(None))


class Fake:
    def __init__(self):
        self.pages, self.fh, self.sec = {}, {}, {}   # ticker -> items (None = that source fails)
        self.fetched, self.fh_calls, self.sec_calls, self.asked, self.answer, self.searches = [], [], [], [], None, 4

    def fetch(self, t):
        self.fetched.append(t)
        n = self.pages.get(t, [])
        return None if n is None else {"data": {"snapshot": {"Price": "1"}, "news": n}, "ok": True, "notes": [], "sha": "x"}

    def finnhub(self, key, t, since, until):
        self.fh_calls.append((t, since, until))
        n = self.fh.get(t, [])
        if n is None:
            raise RuntimeError("finnhub.io answered HTTP 403: access refused")
        return n

    def sec_fetch(self, t, since, until):
        self.sec_calls.append((t, since, until))
        n = self.sec.get(t, [])
        if n is None:
            raise RuntimeError("could not reach data.sec.gov (ConnectionError)")
        return n

    def ask(self, system, prompt):
        self.asked.append((system, prompt))
        text = self.answer(prompt) if callable(self.answer) else self.answer
        return text, self.searches


def fv(d, text, src="Reuters", t="10:00AM"):
    return {"date": d, "date_iso": d, "time": t, "headline": text, "source": src}


def fh(d, text, src, url):
    return {"date_iso": d, "time": "14:00", "headline": text, "source": src, "url": url, "via": "finnhub"}


def filing(d, what, url):
    return {"date_iso": d, "time": None, "headline": f"8-K filed: {what}", "source": "SEC filing", "url": url, "via": "sec"}


def answer_for(items):
    return "Here you go.\n<result>" + json.dumps({"digests": items}) + "</result>"


def run(conn, base, day, f, **kw):
    return news.run(conn, base / "positions.yaml", day, fetch=f.fetch, ask=f.ask, finnhub=f.finnhub, sec=f.sec_fetch,
                    finnhub_key=kw.pop("finnhub_key", "KEY"), sleep=lambda s: None, reuse_hours=kw.pop("reuse_hours", 0), **kw)


print("\nReading the real answer formats (Finnhub, SEC)")
calls = []
def fake_get(url, params=None, headers=None, timeout=30):
    calls.append((url, params, headers))
    if "finnhub" in url:
        return [{"category": "company", "datetime": 1793023200, "headline": "Meta beats on revenue", "source": "Reuters",
                 "url": "https://www.reuters.com/a", "summary": "..."},
                {"datetime": 1700000000, "headline": "Too old", "source": "X", "url": "https://x.com"},
                {"datetime": 1793023200, "headline": "No link", "source": "Y", "url": "ftp://bad"}]
    if url.endswith("company_tickers.json"):
        return {"0": {"cik_str": 1326801, "ticker": "META", "title": "Meta Platforms, Inc."}}
    return {"filings": {"recent": {"form": ["8-K", "4", "10-Q", "8-K"], "filingDate": ["2026-10-29", "2026-10-29", "2026-10-30", "2026-09-01"],
                                   "accessionNumber": ["0001326801-26-000101", "x", "0001326801-26-000102", "old"],
                                   "primaryDocument": ["meta-8k.htm", "f4.xml", "meta-10q.htm", "o.htm"],
                                   "items": ["2.02,9.01", "", "", "8.01"], "primaryDocDescription": ["8-K", "", "10-Q", "8-K"]}}}
_orig_get, _orig_env, _orig_cache = news._get, news.env, news.CACHE
with tempfile.TemporaryDirectory() as td2:
    news._get, news.CACHE = fake_get, Path(td2) / "cache"
    news.env = lambda k: {"SEC_CONTACT": "me@example.com"}.get(k)
    got = news.fetch_finnhub("KEY", "META", "2026-10-25", "2026-10-31")
    check("Finnhub: articles in the window become items with links; old ones are dropped and a non-web link is removed",
          [g["headline"] for g in got] == ["Meta beats on revenue", "No link"] and got[0]["url"] == "https://www.reuters.com/a"
          and got[1]["url"] is None and got[0]["via"] == "finnhub", got)
    check("the Finnhub key travels only as a request parameter", calls[0][1]["token"] == "KEY" and "KEY" not in calls[0][0])
    filings = news.fetch_sec("META", "2026-10-25", "2026-10-31")
    check("SEC: only company filings in the window (8-K, 10-Q; not insider Form 4s, not September), described in words, with links",
          [f["headline"] for f in filings] == ["8-K filed: results of operations", "10-Q quarterly report filed: 10-Q"]
          and filings[0]["url"] == "https://www.sec.gov/Archives/edgar/data/1326801/000132680126000101/meta-8k.htm", filings)
    check("every SEC request carries the contact e-mail the SEC requires", all("me@example.com" in (c[2] or {}).get("User-Agent", "")
                                                                            for c in calls if "sec.gov" in c[0]))
    check("the SEC ticker list is cached (one download a month)", (news.CACHE / "sec_company_tickers.json").exists()
          and sum(1 for c in calls if c[0].endswith("company_tickers.json")) == 1)
    news.fetch_sec("META", "2026-10-25", "2026-10-31")
    check("...a second lookup does not download it again", sum(1 for c in calls if c[0].endswith("company_tickers.json")) == 1)
    news.env = lambda k: None
    try:
        news.fetch_sec("META", "2026-10-25", "2026-10-31"); err = ""
    except RuntimeError as e:
        err = str(e)
    check("without a contact e-mail the SEC is not asked, and the message says how to add it", "--sec-contact" in err, err)
news._get, news.env, news.CACHE = _orig_get, _orig_env, _orig_cache

with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    (base / "positions.yaml").write_text(POS)
    conn = db.connect(base / "r.db"); db.init_schema(conn)
    names = news.holdings(base / "positions.yaml")
    check("only HELD US names: Canadian (ATRL.TO) and watch-list (PSN) names are skipped",
          [n[0] for n in names] == ["NYT", "META"] and names[1][2].startswith("Child-safety"), names)

    print("\nA first day: Finviz + Finnhub + SEC filings, then one capped Claude call")
    f = Fake()
    f.pages = {"META": [fv("2026-10-26", "Meta settles child-safety case with modest conduct terms"),
                        fv("2026-10-20", "An old story from last week")], "NYT": []}
    f.fh = {"META": [fh("2026-10-26", "New Mexico court orders $25B privacy penalty on Meta", "Bloomberg", "https://www.bloomberg.com/meta-25b"),
                     fh("2026-10-26", "Meta settles child-safety case with modest conduct terms", "Reuters", "https://www.reuters.com/meta-settle")],
            "NYT": []}
    f.sec = {"META": [filing("2026-10-26", "other events", "https://www.sec.gov/Archives/edgar/data/1326801/0001/meta8k.htm")], "NYT": []}
    f.answer = answer_for([
        {"ticker": "META", "sentiment": "negative", "gate_relevant": True, "reason": "Big new penalty, remedies pending",
         "summary": "Meta settled one case. A separate $25B penalty landed.",
         "top": [{"title": "New Mexico court orders $25B privacy penalty on Meta", "source": "Bloomberg", "url": "https://www.bloomberg.com/meta-25b", "date": "2026-10-26"},
                 {"title": "Meta settles child-safety case", "source": "Reuters", "url": "https://www.reuters.com/meta-settle", "date": "2026-10-26"},
                 {"title": "8-K filed: other events", "source": "SEC filing", "url": "https://www.sec.gov/Archives/edgar/data/1326801/0001/meta8k.htm", "date": "2026-10-26"},
                 {"title": "Blog take", "source": "blog", "url": "javascript:alert(1)", "date": "soon"},
                 {"title": "Fourth item", "source": "x", "url": "https://x.example.com", "date": "2026-10-26"}]},
        {"ticker": "NYT", "sentiment": "none", "gate_relevant": True, "reason": "Nothing found", "summary": "", "top": []},
        {"ticker": "PSN", "sentiment": "positive", "gate_relevant": False, "reason": "not asked", "summary": "x", "top": []}])
    code, lines = run(conn, base, "2026-10-26", f)
    rows = {r["ticker"]: r for r in db.news_digests(conn)}
    system, prompt = f.asked[0]
    check("all three sources are read for every holding, then ONE Claude call covers all of them",
          sorted(f.fetched) == ["META", "NYT"] and sorted(c[0] for c in f.fh_calls) == ["META", "NYT"]
          and sorted(c[0] for c in f.sec_calls) == ["META", "NYT"] and len(f.asked) == 1)
    check("SEC filings reach Claude marked [OFFICIAL FILING], with their link", "[OFFICIAL FILING]" in f.asked[0][1]
          and "meta8k.htm" in f.asked[0][1])
    check("the instructions cap web searches (one per holding, 11 in all) and ask for the top 2-3 from reliable sources",
          "at most ONE search per holding" in system and "11 searches in total" in system and "TOP 2-3" in system and "Reuters" in system)
    check("Claude gets the gate, the new candidates with their links, and a holding with no candidates is still listed (it may search)",
          "Child-safety trial outcome" in prompt and "<https://www.bloomberg.com/meta-25b>" in prompt and "old story" not in prompt
          and "## NYT" in prompt and "no candidate headlines" in prompt, prompt)
    check("the same story from Finviz and Finnhub is sent once, with the link", prompt.count("Meta settles child-safety case") == 1
          and "<https://www.reuters.com/meta-settle>" in prompt, prompt)
    top = [h for h in rows["META"]["headlines"] if h.get("top")]
    check("META stored: negative, touches the gate, at most 3 top items, reliable sources and the official filing marked",
          rows["META"]["sentiment"] == "negative" and rows["META"]["gate_relevant"] and len(top) == 3
          and top[0]["reliable"] and top[0]["url"].startswith("https://www.bloomberg.com") and top[2]["official"] and top[2]["reliable"], top)
    f_bad = news.parse_answer(answer_for([{"ticker": "META", "sentiment": "neutral", "reason": "", "summary": "",
                                           "top": [{"title": "x", "url": "javascript:alert(1)", "date": "soon"}]}]), {"META"})
    check("a link that is not http(s) is dropped, and a malformed date is ignored", f_bad["META"]["top"][0]["url"] is None
          and f_bad["META"]["top"][0]["date_iso"] is None)
    check("'none' from Claude is stored as no relevant news, and can never be gate-relevant",
          rows["NYT"]["sentiment"] == "none" and rows["NYT"]["gate_relevant"] is False and rows["NYT"]["summary"] == "No relevant news.")
    check("an answer about a ticker that was not asked (PSN) is dropped", "PSN" not in rows)
    check("the run reports sources used, searches made, and an alert line for negative / gate-relevant news",
          code == 0 and lines[0].startswith("News for 2026-10-26: META negative (gate)") and "2 Finnhub and 2 SEC lookup(s), 4 web search(es)" in lines[0]
          and "NEWS ALERTS: META negative (gate)" in lines, lines)

    print("\nThe 08:15 retry and the next day")
    f2 = Fake(); f2.answer = "should not be asked"
    code, lines = run(conn, base, "2026-10-26", f2)
    check("running again for the same day does nothing: no page, no Finnhub or SEC request, no Claude call",
          code == 0 and not f2.fetched and not f2.fh_calls and not f2.sec_calls and not f2.asked and "already done" in lines[0], lines)
    check("a day is never overwritten", db.insert_news_digest(conn, ticker="META", digest_date="2026-10-26", sentiment="positive") is False)

    f3 = Fake()
    f3.pages = {"META": [fv("2026-10-27", "Meta appeals the New Mexico ruling"), fv("2026-10-26", "New Mexico court orders $25B privacy penalty on Meta", "Bloomberg")],
                "NYT": [fv("2026-10-27", "NYT beats subscriber estimates", "CNBC")]}
    f3.answer = answer_for([{"ticker": "META", "sentiment": "neutral", "gate_relevant": False, "reason": "Appeal filed", "summary": "Appeal.", "top": []},
                            {"ticker": "NYT", "sentiment": "positive", "gate_relevant": True, "reason": "Beat", "summary": "Beat.", "top": []}])
    code, lines = run(conn, base, "2026-10-27", f3)
    p3 = f3.asked[0][1]
    check("the next day only items not used before are sent (yesterday's $25B story is not repeated)",
          "appeals" in p3 and "$25B" not in p3 and "subscriber estimates" in p3, p3)
    check("Finnhub and the SEC are asked from the previous digest's day on", ("META", "2026-10-26", "2026-10-27") in f3.fh_calls
          and ("META", "2026-10-26", "2026-10-27") in f3.sec_calls, f3.fh_calls)
    check("positive + gate is an alert too; a neutral day is not", "NEWS ALERTS: NYT positive (gate)" in lines, lines)

    print("\nWhen a source or Claude fails")
    f4 = Fake()
    f4.pages = {"META": None, "NYT": None}
    f4.fh = {"NYT": None, "META": [fh("2026-10-28", "Meta news", "Reuters", "https://www.reuters.com/m")]}
    f4.sec = {"NYT": None}
    f4.answer = answer_for([{"ticker": "META", "sentiment": "neutral", "gate_relevant": False, "reason": "r", "summary": "s", "top": []},
                            {"ticker": "NYT", "sentiment": "neutral", "gate_relevant": False, "reason": "r", "summary": "s", "top": []}])
    code, lines = run(conn, base, "2026-10-28", f4)
    check("Finviz down, Finnhub refused and the SEC unreachable: the digest still runs (Claude can search), naming each failed source",
          code == 0 and len(f4.asked) == 1 and any("Finviz pages could not be read" in ln for ln in lines)
          and any("Finnhub stopped" in ln for ln in lines) and any("SEC filings stopped" in ln for ln in lines), lines)
    check("...and a source that failed is not asked again in the same run", [c[0] for c in f4.fh_calls] == ["NYT"]
          and [c[0] for c in f4.sec_calls] == ["NYT"], (f4.fh_calls, f4.sec_calls))
    f4b = Fake(); f4b.answer = "unused"
    code, lines = news.run(conn, base / "positions.yaml", "2026-11-02", fetch=f4b.fetch, ask=lambda s, p: ("x", 0), finnhub=f4b.finnhub,
                           sec=f4b.sec_fetch, finnhub_key="", sleep=lambda s: None, reuse_hours=0)
    check("without a Finnhub key it is skipped (not called) and the run says how to add one",
          not f4b.fh_calls and any("--finnhub-key" in ln for ln in lines), lines)
    f5 = Fake(); f5.answer = "I could not do it."
    code, lines = run(conn, base, "2026-10-29", f5)
    check("no usable answer: nothing stored, exit 1, every name retried by the next run",
          code == 1 and not [r for r in db.news_digests(conn) if r["digest_date"] == "2026-10-29"]
          and any("Not done" in ln and "META" in ln and "NYT" in ln for ln in lines), lines)
    f6 = Fake()
    f6.answer = answer_for([{"ticker": "META", "sentiment": "very bad", "reason": "x", "summary": "x"},
                            {"ticker": "NYT", "sentiment": "neutral", "gate_relevant": "yes", "reason": "ok", "summary": "ok"}])
    code, lines = run(conn, base, "2026-10-29", f6)
    got = {r["ticker"]: r for r in db.news_digests(conn) if r["digest_date"] == "2026-10-29"}
    check("a made-up sentiment word is refused (META stays to do), and only a real true counts as gate-relevant",
          code == 1 and "META" not in got and got["NYT"]["gate_relevant"] is False, (got, lines))
    db.insert_dossier(conn, ticker="META", data={"news": [fv("2026-10-30", "Fresh from the Saturday run")]}, parse_ok=True, reason="WEEKLY")
    f7 = Fake()
    f7.answer = answer_for([{"ticker": "META", "sentiment": "neutral", "gate_relevant": False, "reason": "r", "summary": "s", "top": []},
                            {"ticker": "NYT", "sentiment": "none", "gate_relevant": False, "reason": "", "summary": "", "top": []}])
    run(conn, base, "2026-10-30", f7, reuse_hours=news.REUSE_PAGE_HOURS)
    check("a Finviz page fetched in the last few hours (e.g. by the Saturday run) is reused, not fetched again",
          "META" not in f7.fetched and "Fresh from the Saturday run" in f7.asked[0][1], f7.fetched)

    print("\nThe Holdings tab")
    p = news.site_payload(conn, base / "positions.yaml", "2026-10-30")
    meta = next(x for x in p["holdings"] if x["ticker"] == "META")
    d26 = next(d for d in meta["days"] if d["date"] == "2026-10-26")
    check("news.json: holdings in positions.yaml order, each with gate, Finviz link and days newest first",
          [x["ticker"] for x in p["holdings"]] == ["NYT", "META"] and meta["finviz"] == "https://finviz.com/quote.ashx?t=META"
          and meta["days"][0]["date"] == "2026-10-30" and meta["days"][-1]["date"] == "2026-10-26")
    check("each day carries its top items (with links and the reliable mark) before the other headlines considered",
          d26["headlines"][0]["top"] and d26["headlines"][0]["url"] and d26["headlines"][0]["reliable"]
          and any(not h.get("top") for h in d26["headlines"]), d26["headlines"])
    site = base / "site"; (site / "data").mkdir(parents=True)
    (site / "index.html").write_text((HERE / "site" / "index.html").read_text())
    (site / "data" / "meta.json").write_text('{"last_run_at": "x"}')
    news.write_site(conn, base / "positions.yaml", "2026-10-30", site / "data" / "news.json")
    check("the site check accepts it", v.check_site(site) == [], v.check_site(site))
    (site / "data" / "news.json").write_text('{"holdings": [{"ticker": "META"}]}')
    check("...and refuses a broken one", any("news.json" in x for x in v.check_site(site)))
    idx = (HERE / "site" / "index.html").read_text()
    check("the page has a Holdings tab next to Weekly reviews, loading data/news.json and showing links for top items",
          'id="btn-holdings"' in idx and idx.index('id="btn-reviews"') < idx.index('id="btn-holdings"') and "load('news')" in idx
          and "x.top" in idx and "Reliable source" in idx and "Official filing" in idx)
    conn.close()

print(f"\n{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} checks")
sys.exit(0 if all(results) else 1)

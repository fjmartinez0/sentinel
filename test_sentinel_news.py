"""Tests for the holdings' daily news digest (sentinel_news.py, DECISIONS.md 60-61). Fake Finviz pages, a fake
Alpha Vantage news feed and a fake Claude: nothing is fetched and nothing is spent."""
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
        self.pages, self.av = {}, {}      # ticker -> news list (None = cannot fetch / AvError)
        self.fetched, self.av_calls, self.asked, self.answer, self.searches = [], [], [], None, 4

    def fetch(self, t):
        self.fetched.append(t)
        n = self.pages.get(t, [])
        return None if n is None else {"data": {"snapshot": {"Price": "1"}, "news": n}, "ok": True, "notes": [], "sha": "x"}

    def av_fetch(self, key, t, since, until):
        self.av_calls.append((t, since, until))
        n = self.av.get(t, [])
        if n is None:
            raise RuntimeError("Alpha Vantage: rate limit is 25 requests per day")
        return n

    def ask(self, system, prompt):
        self.asked.append((system, prompt))
        text = self.answer(prompt) if callable(self.answer) else self.answer
        return text, self.searches


def fv(d, text, src="Reuters", t="10:00AM"):
    return {"date": d, "date_iso": d, "time": t, "headline": text, "source": src}


def av(d, text, src, url):
    return {"date_iso": d, "time": "14:00", "headline": text, "source": src, "url": url, "via": "alphavantage"}


def answer_for(items):
    return "Here you go.\n<result>" + json.dumps({"digests": items}) + "</result>"


def run(conn, base, day, f, **kw):
    return news.run(conn, base / "positions.yaml", day, fetch=f.fetch, ask=f.ask, av_fetch=f.av_fetch, av_key="KEY",
                    sleep=lambda s: None, reuse_hours=kw.pop("reuse_hours", 0), **kw)


with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    (base / "positions.yaml").write_text(POS)
    conn = db.connect(base / "r.db"); db.init_schema(conn)
    names = news.holdings(base / "positions.yaml")
    check("only HELD US names: Canadian (ATRL.TO) and watch-list (PSN) names are skipped",
          [n[0] for n in names] == ["NYT", "META"] and names[1][2].startswith("Child-safety"), names)

    print("\nA first day: Finviz + Alpha Vantage, then one capped Claude call")
    f = Fake()
    f.pages = {"META": [fv("2026-10-26", "Meta settles child-safety case with modest conduct terms"),
                        fv("2026-10-20", "An old story from last week")], "NYT": []}
    f.av = {"META": [av("2026-10-26", "New Mexico court orders $25B privacy penalty on Meta", "Bloomberg", "https://www.bloomberg.com/meta-25b"),
                     av("2026-10-26", "Meta settles child-safety case with modest conduct terms", "Reuters", "https://www.reuters.com/meta-settle")],
            "NYT": []}
    f.answer = answer_for([
        {"ticker": "META", "sentiment": "negative", "gate_relevant": True, "reason": "Big new penalty, remedies pending",
         "summary": "Meta settled one case. A separate $25B penalty landed.",
         "top": [{"title": "New Mexico court orders $25B privacy penalty on Meta", "source": "Bloomberg", "url": "https://www.bloomberg.com/meta-25b", "date": "2026-10-26"},
                 {"title": "Meta settles child-safety case", "source": "Reuters", "url": "https://www.reuters.com/meta-settle", "date": "2026-10-26"},
                 {"title": "Blog take", "source": "blog", "url": "javascript:alert(1)", "date": "soon"},
                 {"title": "Fourth item", "source": "x", "url": "https://x.example.com", "date": "2026-10-26"}]},
        {"ticker": "NYT", "sentiment": "none", "gate_relevant": True, "reason": "Nothing found", "summary": "", "top": []},
        {"ticker": "PSN", "sentiment": "positive", "gate_relevant": False, "reason": "not asked", "summary": "x", "top": []}])
    code, lines = run(conn, base, "2026-10-26", f)
    rows = {r["ticker"]: r for r in db.news_digests(conn)}
    system, prompt = f.asked[0]
    check("both sources are read for every holding, then ONE Claude call covers all of them",
          sorted(f.fetched) == ["META", "NYT"] and sorted(c[0] for c in f.av_calls) == ["META", "NYT"] and len(f.asked) == 1)
    check("the instructions cap web searches (one per holding, 11 in all) and ask for the top 2-3 from reliable sources",
          "at most ONE search per holding" in system and "11 searches in total" in system and "TOP 2-3" in system and "Reuters" in system)
    check("Claude gets the gate, the new candidates with their links, and a holding with no candidates is still listed (it may search)",
          "Child-safety trial outcome" in prompt and "<https://www.bloomberg.com/meta-25b>" in prompt and "old story" not in prompt
          and "## NYT" in prompt and "no candidate headlines" in prompt, prompt)
    check("the same story from Finviz and Alpha Vantage is sent once, with the link", prompt.count("Meta settles child-safety case") == 1
          and "<https://www.reuters.com/meta-settle>" in prompt, prompt)
    top = [h for h in rows["META"]["headlines"] if h.get("top")]
    check("META stored: negative, touches the gate, at most 3 top items, only real links kept, reliable sources marked",
          rows["META"]["sentiment"] == "negative" and rows["META"]["gate_relevant"] and len(top) == 3
          and top[0]["reliable"] and top[0]["url"].startswith("https://www.bloomberg.com") and top[2]["url"] is None and not top[2]["reliable"], top)
    check("'none' from Claude is stored as no relevant news, and can never be gate-relevant",
          rows["NYT"]["sentiment"] == "none" and rows["NYT"]["gate_relevant"] is False and rows["NYT"]["summary"] == "No relevant news.")
    check("an answer about a ticker that was not asked (PSN) is dropped", "PSN" not in rows)
    check("the run reports sources used, searches made, and an alert line for negative / gate-relevant news",
          code == 0 and lines[0].startswith("News for 2026-10-26: META negative (gate)") and "2 Alpha Vantage request(s), 4 web search(es)" in lines[0]
          and "NEWS ALERTS: META negative (gate)" in lines, lines)

    print("\nThe 08:15 retry and the next day")
    f2 = Fake(); f2.answer = "should not be asked"
    code, lines = run(conn, base, "2026-10-26", f2)
    check("running again for the same day does nothing: no page, no Alpha Vantage request, no Claude call",
          code == 0 and not f2.fetched and not f2.av_calls and not f2.asked and "already done" in lines[0], lines)
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
    check("Alpha Vantage is asked from the previous digest's day on", ("META", "2026-10-26", "2026-10-27") in f3.av_calls, f3.av_calls)
    check("positive + gate is an alert too; a neutral day is not", "NEWS ALERTS: NYT positive (gate)" in lines, lines)

    print("\nWhen a source or Claude fails")
    f4 = Fake()
    f4.pages = {"META": None, "NYT": None}
    f4.av = {"META": [av("2026-10-28", "Meta news", "Reuters", "https://www.reuters.com/m")], "NYT": None}
    f4.answer = answer_for([{"ticker": "META", "sentiment": "neutral", "gate_relevant": False, "reason": "r", "summary": "s", "top": []},
                            {"ticker": "NYT", "sentiment": "neutral", "gate_relevant": False, "reason": "r", "summary": "s", "top": []}])
    code, lines = run(conn, base, "2026-10-28", f4)
    check("Finviz down and Alpha Vantage at its daily limit: the digest still runs on what is left, and says which source failed",
          code == 0 and len(f4.asked) == 1 and any("Finviz pages could not be read" in ln for ln in lines)
          and any("Alpha Vantage news stopped" in ln for ln in lines), lines)
    check("...and after the limit is hit, no more Alpha Vantage requests are made that run", [c[0] for c in f4.av_calls] == ["NYT"], f4.av_calls)
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
          and "x.top" in idx and "Reliable source" in idx)
    conn.close()

print(f"\n{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} checks")
sys.exit(0 if all(results) else 1)

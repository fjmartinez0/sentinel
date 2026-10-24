"""Tests for the holdings' daily news digest (sentinel_news.py, DECISIONS.md 60). Fake Finviz pages and a fake
Claude: nothing is fetched and nothing is spent."""
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


class Fake:
    def __init__(self):
        self.pages = {}          # ticker -> list of news dicts, or None = page cannot be fetched
        self.fetched, self.asked, self.answer = [], [], None

    def fetch(self, t):
        self.fetched.append(t)
        n = self.pages.get(t, [])
        if n is None:
            return None
        return {"data": {"snapshot": {"Price": "1"}, "news": n}, "ok": True, "notes": [], "sha": "x"}

    def ask(self, system, prompt):
        self.asked.append(prompt)
        return self.answer(prompt) if callable(self.answer) else self.answer


def h(d, text, src="Reuters", t="10:00AM"):
    return {"date": d, "date_iso": d, "time": t, "headline": text, "source": src}


def answer_for(items):
    return "Here you go.\n<result>" + json.dumps({"digests": items}) + "</result>"


with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    (base / "positions.yaml").write_text(POS)
    conn = db.connect(base / "r.db"); db.init_schema(conn)
    names = news.holdings(base / "positions.yaml")
    check("only HELD US names: Canadian (ATRL.TO) and watch-list (PSN) names are skipped",
          [n[0] for n in names] == ["NYT", "META"] and names[1][2].startswith("Child-safety"), names)

    print("\nA first day")
    f = Fake()
    f.pages = {"META": [h("2026-10-26", "Meta settles child-safety case with modest conduct terms"),
                        h("2026-10-26", "New Mexico court orders $25B privacy penalty", "Bloomberg"),
                        h("2026-10-20", "An old story from last week")],
               "NYT": []}
    f.answer = answer_for([{"ticker": "META", "sentiment": "negative", "gate_relevant": True, "reason": "Big new penalty, remedies pending",
                            "summary": "Meta settled one case. A separate $25B penalty landed."},
                           {"ticker": "PSN", "sentiment": "positive", "gate_relevant": False, "reason": "not asked", "summary": "x"}])
    code, lines = news.run(conn, base / "positions.yaml", "2026-10-26", fetch=f.fetch, ask=f.ask, sleep=lambda s: None, reuse_hours=0)
    rows = {r["ticker"]: r for r in db.news_digests(conn)}
    check("one page per holding is read, and ONE Claude call covers all names with news",
          sorted(f.fetched) == ["META", "NYT"] and len(f.asked) == 1, (f.fetched, len(f.asked)))
    check("Claude gets the gate and only the NEW headlines (last week's story is left out on a first run)",
          "Child-safety trial outcome" in f.asked[0] and "$25B" in f.asked[0] and "old story" not in f.asked[0] and "NYT" not in f.asked[0], f.asked[0])
    check("META stored: negative, touches the gate, with reason, summary and the headlines used",
          rows["META"]["sentiment"] == "negative" and rows["META"]["gate_relevant"] and len(rows["META"]["headlines"]) == 2
          and "subscription" in rows["META"]["model"], rows.get("META"))
    check("a holding with no new headlines is stored as 'none' without asking Claude", rows["NYT"]["sentiment"] == "none")
    check("an answer about a ticker that was not asked (PSN) is dropped", "PSN" not in rows)
    check("the run reports the day, and an alert line for negative / gate-relevant news",
          code == 0 and lines[0].startswith("News for 2026-10-26: META negative (gate)") and "NEWS ALERTS: META negative (gate)" in lines, lines)

    print("\nThe 08:15 retry and the next day")
    f2 = Fake(); f2.answer = "should not be asked"
    code, lines = news.run(conn, base / "positions.yaml", "2026-10-26", fetch=f2.fetch, ask=f2.ask, sleep=lambda s: None, reuse_hours=0)
    check("running again for the same day does nothing: no page, no Claude call", code == 0 and not f2.fetched and not f2.asked
          and "already done" in lines[0], lines)
    check("a day is never overwritten", db.insert_news_digest(conn, ticker="META", digest_date="2026-10-26", sentiment="positive") is False
          and db.news_digests(conn, ticker="META")[0]["sentiment"] == "negative")

    f3 = Fake()
    f3.pages = {"META": [h("2026-10-27", "Meta appeals the New Mexico ruling"), h("2026-10-26", "New Mexico court orders $25B privacy penalty", "Bloomberg")],
                "NYT": [h("2026-10-27", "NYT beats subscriber estimates", "CNBC")]}
    f3.answer = lambda p: answer_for([{"ticker": "META", "sentiment": "neutral", "gate_relevant": False, "reason": "Appeal filed", "summary": "Appeal."},
                                      {"ticker": "NYT", "sentiment": "positive", "gate_relevant": True, "reason": "Beat", "summary": "Beat."}])
    code, lines = news.run(conn, base / "positions.yaml", "2026-10-27", fetch=f3.fetch, ask=f3.ask, sleep=lambda s: None, reuse_hours=0)
    check("the next day only headlines not used before are sent (yesterday's $25B story is not repeated)",
          "appeals" in f3.asked[0] and "$25B" not in f3.asked[0] and "subscriber estimates" in f3.asked[0], f3.asked[0])
    check("positive + gate is an alert too; a neutral day is not", "NEWS ALERTS: NYT positive (gate)" in lines, lines)

    print("\nWhen things go wrong")
    f4 = Fake()
    f4.pages = {"META": [h("2026-10-28", "Meta news")], "NYT": [h("2026-10-28", "NYT news")]}
    f4.answer = "I could not do it."
    code, lines = news.run(conn, base / "positions.yaml", "2026-10-28", fetch=f4.fetch, ask=f4.ask, sleep=lambda s: None, reuse_hours=0)
    check("no usable answer: nothing stored for those names, exit 1, and they are retried by the next run",
          code == 1 and not [r for r in db.news_digests(conn) if r["digest_date"] == "2026-10-28"]
          and any("retried" in ln for ln in lines), lines)
    f5 = Fake(); f5.pages = f4.pages
    f5.answer = answer_for([{"ticker": "META", "sentiment": "very bad", "reason": "x", "summary": "x"},
                            {"ticker": "NYT", "sentiment": "neutral", "gate_relevant": "yes", "reason": "ok", "summary": "ok"}])
    code, lines = news.run(conn, base / "positions.yaml", "2026-10-28", fetch=f5.fetch, ask=f5.ask, sleep=lambda s: None, reuse_hours=0)
    got = {r["ticker"]: r for r in db.news_digests(conn) if r["digest_date"] == "2026-10-28"}
    check("the retry asks again; a made-up sentiment word is refused (META stays to do), and only a real true counts as gate",
          code == 1 and "META" not in got and got["NYT"]["sentiment"] == "neutral" and got["NYT"]["gate_relevant"] is False, (got, lines))
    f6 = Fake(); f6.pages = {"META": None, "NYT": None}; f6.answer = "unused"
    code, lines = news.run(conn, base / "positions.yaml", "2026-10-29", fetch=f6.fetch, ask=f6.ask, sleep=lambda s: None, reuse_hours=0)
    check("pages that cannot be read: it stops after two, asks nothing, and says which names are not done",
          code == 1 and not f6.asked and any("Not done" in ln and "META" in ln and "NYT" in ln for ln in lines), lines)
    db.insert_dossier(conn, ticker="META", data={"news": [h("2026-10-30", "Fresh from the Saturday run")]}, parse_ok=True, reason="WEEKLY")
    f7 = Fake(); f7.pages = {"NYT": []}
    f7.answer = answer_for([{"ticker": "META", "sentiment": "neutral", "gate_relevant": False, "reason": "r", "summary": "s"}])
    news.run(conn, base / "positions.yaml", "2026-10-30", fetch=f7.fetch, ask=f7.ask, sleep=lambda s: None)
    check("a page fetched in the last few hours (e.g. by the Saturday run) is reused, not fetched again",
          "META" not in f7.fetched and f7.asked and "Fresh from the Saturday run" in f7.asked[0], (f7.fetched, f7.asked))

    print("\nThe Holdings tab")
    p = news.site_payload(conn, base / "positions.yaml", "2026-10-30")
    meta = next(x for x in p["holdings"] if x["ticker"] == "META")
    check("news.json lists the holdings in positions.yaml order, each with gate, Finviz link and days newest first",
          [x["ticker"] for x in p["holdings"]] == ["NYT", "META"] and meta["gate"].startswith("Child-safety")
          and meta["finviz"] == "https://finviz.com/quote.ashx?t=META" and meta["days"][0]["date"] == "2026-10-30"
          and meta["days"][-1]["date"] == "2026-10-26", meta["days"])
    site = base / "site"; (site / "data").mkdir(parents=True)
    (site / "index.html").write_text((HERE / "site" / "index.html").read_text())
    (site / "data" / "meta.json").write_text('{"last_run_at": "x"}')
    news.write_site(conn, base / "positions.yaml", "2026-10-30", site / "data" / "news.json")
    check("the site check accepts it", v.check_site(site) == [], v.check_site(site))
    (site / "data" / "news.json").write_text('{"holdings": [{"ticker": "META"}]}')
    check("...and refuses a broken one", any("news.json" in x for x in v.check_site(site)))
    idx = (HERE / "site" / "index.html").read_text()
    check("the page has a Holdings tab next to Weekly reviews, loading data/news.json",
          'id="btn-holdings"' in idx and idx.index('id="btn-reviews"') < idx.index('id="btn-holdings"') and "load('news')" in idx)
    conn.close()

print(f"\n{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} checks")
sys.exit(0 if all(results) else 1)

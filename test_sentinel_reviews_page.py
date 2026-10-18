"""Tests for the Weekly reviews tab (sentinel_reviews_page.py). Uses a temporary database; nothing is fetched."""
import os, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sentinel_db as db, sentinel_reviews_page as rp

results = []
def check(label, ok, detail=""):
    ok = bool(ok); results.append(ok); print(("  ok    " if ok else "  FAIL  ") + label + ("" if ok else f"  {detail}"))


def res(t, v, scores=(3, 2, 4, 3), **kw):
    r = {"ticker": t, "verdict": v, "scores": dict(zip(rp.SCORE_KEYS, scores)) if scores else None,
         "total": sum(scores) if scores else None, "gate": kw.get("gate"), "gate_date": kw.get("gate_date"),
         "readd_condition": kw.get("readd"), "checks": {"insiders": "done", "history_peers": "partial", "fraud": "done"},
         "index_member": kw.get("index", "neither"), "one_line": kw.get("one", f"{t} in one line"),
         "sources": [{"url": f"https://www.sec.gov/{t}.htm", "title": f"{t} 8-K"}, {"url": "javascript:alert(1)", "title": "bad"}]}
    return r


print("Weekly reviews page")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "r.db"
    c = db.connect(p); db.init_schema(c)
    ids = {}
    for t, f in (("CRWV", "A"), ("AMRZ", "B"), ("COO", "B"), ("NEWX", "B"), ("WAIT", "B")):
        ids[t], _ = db.insert_escalation(c, ticker=t, funnel=f, trigger="candidate" if f == "B" else "move", trigger_ref="2026-10-17", reasons=["r"])
    db.set_escalation_status(c, ids["AMRZ"], "done")
    md = ("# CRWV review\n\n## Why it moved\nBonds yielded **~15%** after a [$3B raise](https://example.com/raise).\n\n"
          "- first point\n- second <script>alert(1)</script>\n\n## Verdict\n**Verdict: at_risk**")
    db.insert_tier2_review(c, escalation_id=ids["CRWV"], ticker="CRWV", mode="live", batch="b1", model="claude-sonnet-5", ok=True,
                           verdict="at_risk", result=res("CRWV", "at_risk", (3, 2, 3, 2), gate="Leverage risk"), report_md=md,
                           usage={"searches": 10}, cost_usd=0.54, created_at="2026-10-18T15:12:00+00:00")
    db.insert_tier2_review(c, escalation_id=ids["NEWX"], ticker="NEWX", mode="live", batch="b2", model="claude-sonnet-5 (subscription)",
                           ok=True, verdict="watch", result=res("NEWX", "watch", gate="Q3 print", gate_date="2026-10-28"),
                           report_md="Fine.", usage={"searches": 6}, created_at="2026-10-18T19:00:00+00:00")
    db.insert_tier2_review(c, escalation_id=ids["NEWX"], ticker="NEWX", mode="live", batch="b0", model="claude-sonnet-5", ok=False,
                           error="credit balance too low", created_at="2026-10-18T15:13:00+00:00")
    db.insert_tier2_review(c, escalation_id=ids["AMRZ"], ticker="AMRZ", mode="live", batch="old", model="claude-sonnet-5", ok=True,
                           verdict="watch", result=res("AMRZ", "watch"), report_md="Old.", created_at="2026-10-10T15:00:00+00:00")
    for t, v, x in (("AMRZ", "watch", "watch"), ("COO", "dismiss", "watch")):
        db.insert_tier2_review(c, escalation_id=ids[t], ticker=t, mode="calibration", batch="cal1", model="claude-sonnet-5 (subscription)",
                               ok=True, verdict=v, expected=x, result=res(t, v, None if v == "dismiss" else (3, 4, 4, 3), readd="two clean quarters"),
                               report_md="x", created_at="2026-10-18T17:00:00+00:00")
    from datetime import date
    page = rp.render(c, date(2026, 10, 18))
    c.close()

    check("both tabs are on the page, Weekly reviews marked as the current one",
          'href="earnings_calendar.html">Calendar</a>' in page and 'class="on" href="tier2_reviews.html">Weekly reviews' in page)
    check("this week (Sat Oct 17 to Fri Oct 23) holds the Oct 18 reviews, open; last week's are a separate, closed section",
          "Week of Oct 17, 2026" in page and "Week of Oct 10, 2026" in page
          and page.index("Week of Oct 17") < page.index("Week of Oct 10") and '<details class="week" open><summary><h2>Week of Oct 17' in page)
    check("a re-run in the same week shows only the newest review of a name (NEWX watch, not the failed credit attempt)",
          page.count("credit balance too low") == 0 and "NEWX" in page)
    check("at-risk / broken holdings are listed first", page.index('id="r1"') < page.index('id="r2"'))
    check("verdict, score bars and total are shown", "At risk" in page and 'class="total">10<span>/20' in page and page.count('<i class="on">') >= 10)
    check("the gate and its date are shown", "Q3 print (by 2026-10-28)" in page)
    check("an open alert shows the exact command to close it; a closed one says so",
          f"--mark {ids['CRWV']} done" in page and "Alert done" in page)
    check("the full review is rendered (headings, bold, links), not shown as plain text",
          "<h4>Why it moved</h4>" in page and "<strong>~15%</strong>" in page and '<a href="https://example.com/raise"' in page)
    check("HTML in a review is escaped, never run", "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page)
    check("only http(s) source links are kept", "javascript:" not in page and "https://www.sec.gov/CRWV.htm" in page)
    check("cost: $0 for a subscription review, dollars for an API one", "$0 (subscription)" in page and "$0.54" in page)
    check("calibration runs are kept apart, with matches and differences marked",
          "Calibration of 2026-10-18" in page and "1/2 matched your verdicts" in page and "different" in page)
    check("names still waiting for Tier 2 are listed (WAIT, COO)", "Waiting for Tier 2: " in page and "WAIT" in page)

    out = Path(td) / "page.html"
    os.environ.pop("RESEARCH_DB", None)
    code = rp.main(["--db", str(p), "--out", str(out)])
    check("the command writes the page and returns 0", code == 0 and out.exists())
    check("a missing database is a clear message, not a crash", rp.main(["--db", str(Path(td) / "nope.db"), "--out", str(out)]) == 2)

print(f"\n{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} checks")
sys.exit(0 if all(results) else 1)

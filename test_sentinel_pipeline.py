"""Phase 8 tests: sentinel_pipeline.py. Fake step scripts misbehave on command; the real database code is used."""
import contextlib, io, json, os, shutil, sqlite3, sys, tempfile, textwrap
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

PROJ = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJ))
import sentinel_db as db
import sentinel_pipeline as rp

results = []
def check(label, ok, detail=""):
    ok = bool(ok); results.append(ok); print(("  ok    " if ok else "  FAIL  ") + label + ("" if ok else f"  {detail}"))

UTC = timezone.utc
def et_to_utc(y, m, d, hh, mm):
    from zoneinfo import ZoneInfo
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)

# --------------------------------------------------------------------------- expected close / business days
print("Time rules")
from zoneinfo import ZoneInfo
E = lambda *a: rp.expected_close(et_to_utc(*a).astimezone(ZoneInfo("America/New_York")))
check("Monday 16:30 ET: today's close is expected", E(2026, 9, 21, 16, 30) == date(2026, 9, 21))
check("Monday 08:15 ET: Friday's close", E(2026, 9, 21, 8, 15) == date(2026, 9, 18))
check("Monday 11:00 ET (market open): still the previous close, never an intraday price", E(2026, 9, 21, 11, 0) == date(2026, 9, 18))
check("Saturday: Friday's close", E(2026, 9, 19, 9, 0) == date(2026, 9, 18))
check("Wednesday 08:15 ET: Tuesday's close", E(2026, 9, 23, 8, 15) == date(2026, 9, 22))
check("weekdays strictly between two dates skip the weekend", rp.business_days_between(date(2026, 9, 18), date(2026, 9, 22)) == [date(2026, 9, 21)])
check("Canadian tickers are not US", rp.is_non_us("ATRL.TO") and rp.is_non_us("X.V") and not rp.is_non_us("BRK-B") and not rp.is_non_us("NYT"))

# --------------------------------------------------------------------------- a project folder with fake programs
tmp = Path(tempfile.mkdtemp())
proj, fakes = tmp / "proj", tmp / "fakes"
proj.mkdir(); fakes.mkdir()
(proj / "positions.yaml").write_text("as_of: 2026-09-01\nheld:\n  - {ticker: NYT, gate: \"Decide whether the guidance cut is a thesis break at the Q3 print\"}\n  - {ticker: QTWO}\n  - {ticker: ATRL.TO}\nwatch:\n  - {ticker: PSN}\n  - {ticker: RBA}\n")
os.environ["RESEARCH_DB"] = str(proj / "research.db")
c = db.connect(); db.init_schema(c); c.close()
rp.configure(proj)

HDR = f"import os, sys\nsys.path.insert(0, {str(PROJ)!r})\nimport sentinel_db as db\nMODE = os.environ.get('FAKE_{{name}}', 'ok')\n"
(fakes / "scan.py").write_text(HDR.format(name="SCAN") + textwrap.dedent("""
    args = sys.argv[1:]
    if '--quotes' in args:                      # daily prices (FAKE_QUOTES: ok | zero | partial)
        QM = os.environ.get('FAKE_QUOTES', 'ok')
        d = os.environ.get('FAKE_CLOSE_DATE', '2026-09-21')
        if QM == 'zero':
            print('Stored 0 of 4 price(s) for ' + d + '.'); print('PROBLEM page 1: no table with Ticker, Price and Change columns was found'); sys.exit(0)
        names = ['NYT', 'QTWO', 'PSN', 'RBA'] if QM == 'ok' else ['NYT', 'QTWO', 'PSN']      # 'partial' leaves RBA out (75%)
        c = db.connect()
        for t in names: db.upsert_prices(c, t, [(d, 50.0, 1.0)], source='finviz_screener')
        print('Stored %d of 4 price(s) for %s.' % (len(names), d))
        if os.environ.get('FAKE_OPEN'): print('The US market is open right now (11:00 New York time), so Finviz shows a price in progress')
    elif '--dossier-only' in args:              # tracked dossiers (FAKE_DOSSIER: ok | blocked | crash)
        DM = os.environ.get('FAKE_DOSSIER', 'ok')
        if DM == 'crash': sys.exit('dossier boom')
        if DM == 'blocked': print('  Stopping early: two pages in a row could not be read.')
        print('dossiers done')
    else:                                       # the weekly scan (FAKE_SCAN: ok | crash | nothing | hits0)
        if MODE == 'crash': sys.exit("Couldn't fetch the Overview view -- check the URLs")
        if MODE == 'hits0':
            c = db.connect(); db.insert_screen_run(c, source='finviz_screener', results=[]); print('Saved run with 0 hits')
        elif MODE != 'nothing':
            c = db.connect(); db.insert_screen_run(c, source='finviz_screener', results=[{'Ticker': 'AAA'}, {'Ticker': 'BBB'}])
            print('Saved to research.db as run #1 (2 hits).')
"""))
(fakes / "calendar.py").write_text(HDR.format(name="CALENDAR") + textwrap.dedent("""
    import time
    if '--share' in sys.argv:                   # the SHAREABLE page (FAKE_SHARE: ok | leak | leakgate | notshare | crash)
        out = sys.argv[sys.argv.index('--out') + 1]
        SM = os.environ.get('FAKE_SHARE', 'ok')
        if SM == 'crash': sys.exit('share boom')
        body = '<html><body>S&amp;P 500 only. No positions, gates or prices are included.</body></html>'
        if SM == 'leak': body += ' Resting exit: GTC $50 x 2'
        if SM == 'leakgate': body += ' Decide whether the guidance cut is a thesis break at the Q3 print '
        if SM == 'notshare': body = '<html>the full private page</html>'
        body += os.environ.get('FAKE_TAG', '')
        open(out, 'w').write(body); print('share page written'); sys.exit(0)
    if MODE == 'crash': sys.exit('Set ALPHAVANTAGE_API_KEY first:  python3 sentinel_pipeline.py av-key   (or use --offline).')
    if MODE == 'hang': time.sleep(60)
    if MODE == 'fmp402': sys.exit('requests.exceptions.HTTPError: 402 Client Error: Payment Required for url: https://financialmodelingprep.com/stable/earnings-calendar?from=2026-09-02&apikey=' + os.environ.get('ALPHAVANTAGE_API_KEY', ''))
    if MODE == 'avdown':
        open('earnings_calendar.html', 'w').write('<html></html>')
        print('WARNING: earnings source unavailable (Alpha Vantage: rate limit is 25 requests per day); using the 9 upcoming date(s) stored by the calendar run of 2026-09-30')
        print('40 items on the calendar (2 held, 1 watch, 0 S&P 500, 3 ledger gates).'); sys.exit(0)
    if MODE != 'nowrite': open('earnings_calendar.html', 'w').write('<html></html>')
    print('56 items on the calendar (2 held, 1 watch, 50 S&P 500, 3 ledger gates).')
"""))
(fakes / "sentinel_tier1.py").write_text(HDR.format(name="TIER1") + textwrap.dedent("""
    if MODE == 'new':
        c = db.connect(); db.insert_escalation(c, ticker='AMRZ', funnel='B', trigger='candidate', trigger_ref=os.environ.get('FAKE_REF', 'r1'), reasons=['x'])
    print('Tier 1 report'); 
    if MODE == 'stale': print('    STALE (a move since then would be missed): ATRL.TO (last 2026-09-10)')
"""))
(fakes / "sentinel_tier2.py").write_text(HDR.format(name="TIER2") + textwrap.dedent("""
    if MODE == 'refused':
        print('Live review refused: no calibration has been run yet. Run  python3 sentinel_tier2.py --calibrate  first.'); sys.exit(2)
    c = db.connect()
    esc = db.list_escalations(c, 'open')
    if not esc:
        print('Nothing to review.'); sys.exit(0)
    for e in esc:
        db.insert_tier2_review(c, escalation_id=e['escalation_id'], ticker=e['ticker'], mode='live', batch='b', model='m',
                               ok=MODE != 'fail', verdict=None if MODE == 'fail' else 'watch',
                               error='the answer had no <result> block' if MODE == 'fail' else None, cost_usd=0.5)
    print('Written to reviews/2026-09-26_tier2.md')
"""))
(fakes / "reviews_page.py").write_text("import sys\nprint('Weekly reviews page written: tier2_reviews.html')\n")
rp.SCRIPTS = {"scan": fakes / "scan.py", "calendar": fakes / "calendar.py", "tier1": fakes / "sentinel_tier1.py", "tier2": fakes / "sentinel_tier2.py",
              "reviews": fakes / "reviews_page.py"}
os.environ["ANTHROPIC_API_KEY"] = "test-anthropic-key"
_orig_steps = rp.job_steps
rp.job_steps = lambda job: [dict(s, limit=4) for s in _orig_steps(job)]
os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
_real_notify_fn = rp.notify
rp.notify = lambda title, body: NOTES.append((title, body)) or True
NOTES = []

def reset(**modes):
    for k in ("FAKE_QUOTES", "FAKE_CALENDAR", "FAKE_TIER1", "FAKE_TIER2", "FAKE_SCAN", "FAKE_DOSSIER", "FAKE_OPEN", "FAKE_REF", "FAKE_CLOSE_DATE", "FAKE_SHARE", "FAKE_TAG", "PUBLISH_DIR", "PUBLISH_GIT", "PUBLISH_FILE"):
        os.environ.pop(k, None)
    for k, v in modes.items(): os.environ["FAKE_" + k.upper()] = v
    NOTES.clear()
    if rp.STATUS.exists(): rp.STATUS.unlink()
    cc = db.connect(); cc.execute("DELETE FROM price_history"); cc.execute("DELETE FROM tier2_reviews"); cc.execute("DELETE FROM escalations"); cc.close()     # each scenario starts clean
    for f in (proj / "earnings_calendar.html", ):
        if f.exists(): f.unlink()
def last():
    return rp.read_status()["runs"][-1]
def step(rec, name):
    return next(s for s in rec["steps"] if s["name"] == name)
MON_AFTER_CLOSE = et_to_utc(2026, 9, 21, 16, 30)

# --------------------------------------------------------------------------- daily: the happy path
print("\nDaily job, everything works")
reset()
code = rp.run_job("daily", now=MON_AFTER_CLOSE)
r = last()
check("all four steps ran (prices, calendar, Tier 1, the Weekly reviews tab) and the job is OK with exit code 0",
      code == 0 and r["level"] == "OK" and [s["name"] for s in r["steps"]] == ["quotes", "calendar", "tier1", "reviews page"], str(r))
check("quotes are judged from the DATABASE: 4 of 4 tracked US names have the expected close (the Canadian name is not counted)", "4/4 tracked US names have the 2026-09-21 close" in step(r, "quotes")["detail"], step(r, "quotes")["detail"])
check("a full log and the status file are written", (rp.LOGS / r["log"]).exists() and rp.STATUS.exists() and "=== quotes:" in (rp.LOGS / r["log"]).read_text())
check("ONE summary notification even when everything is fine (DECISIONS.md 50: silence must mean 'did not run')",
      len(NOTES) == 1 and NOTES[0][1].startswith("daily OK") and "no new alerts" in NOTES[0][1], str(NOTES))

# --------------------------------------------------------------------------- the acceptance criterion
print("\nA broken step never stops the ones after it (the Phase 8 acceptance test)")
reset(quotes="zero")
code = rp.run_job("daily", now=MON_AFTER_CLOSE)
r = last()
check("QUOTES store nothing while exiting 0 (the real first-run failure): caught, level FAIL, and the exit code is 2",
      code == 2 and step(r, "quotes")["level"] == "FAIL" and "0/4" in step(r, "quotes")["detail"], step(r, "quotes")["detail"])
check("...and the calendar AND Tier 1 still ran", step(r, "calendar")["level"] == "OK" and step(r, "tier1")["level"] in ("OK", "WARN"), str(r["steps"]))
check("...and the person is told: one notification naming the failure", len(NOTES) == 1 and "FAIL" in NOTES[0][1] and "quotes" in NOTES[0][1], str(NOTES))

reset(calendar="crash")
code = rp.run_job("daily", now=MON_AFTER_CLOSE)
r = last()
check("a crashing calendar is FAIL with its own message, and Tier 1 still runs", step(r, "calendar")["level"] == "FAIL" and "ALPHAVANTAGE_API_KEY" in step(r, "calendar")["detail"] and step(r, "tier1")["level"] == "OK", str(r["steps"]))

reset(calendar="hang")
t0 = datetime.now()
rp.run_job("daily", now=MON_AFTER_CLOSE)
r = last()
check("a hung step is killed at its time limit, marked FAIL, and the next step still runs (took %.0fs)" % (datetime.now() - t0).total_seconds(),
      step(r, "calendar")["level"] == "FAIL" and "timed out" in step(r, "calendar")["detail"] and step(r, "tier1")["level"] == "OK")

reset(calendar="nowrite")
rp.run_job("daily", now=MON_AFTER_CLOSE)
check("exit code 0 without the page being written is not accepted as success", step(last(), "calendar")["level"] == "FAIL" and "not (re)written" in step(last(), "calendar")["detail"])

reset()
saved = os.environ.pop("ALPHAVANTAGE_API_KEY")
rp.run_job("daily", now=MON_AFTER_CLOSE)
r = last()
check("without ALPHAVANTAGE_API_KEY the calendar is not even started, says exactly why (and the fix), and the others run",
      step(r, "calendar")["level"] == "FAIL" and "ALPHAVANTAGE_API_KEY" in step(r, "calendar")["detail"] and "av-key" in step(r, "calendar")["detail"] and step(r, "quotes")["level"] == "OK" and step(r, "tier1")["level"] == "OK")
(proj / ".env").write_text('# comment\nALPHAVANTAGE_API_KEY="from-dotenv"\n')
reset()
rp.run_job("daily", now=MON_AFTER_CLOSE)
check("...but a key in the private .env file is found (this is how a scheduled job gets it)", step(last(), "calendar")["level"] == "OK")
(proj / ".env").unlink(); os.environ["ALPHAVANTAGE_API_KEY"] = saved

# --------------------------------------------------------------------------- freshness thresholds
print("\nPrice freshness")
reset(quotes="partial")
rp.run_job("daily", now=MON_AFTER_CLOSE)
q = step(last(), "quotes")
check("3 of 4 names is 75%: FAIL (under 90%), naming the missing name and that a missed day cannot be filled in", q["level"] == "FAIL" and "RBA" in q["detail"] and "cannot be filled in" in q["detail"], q["detail"])
reset(close_date="2026-09-18")
rp.run_job("daily", now=MON_AFTER_CLOSE)
q = step(last(), "quotes")
check("prices stamped with an old date (Friday) when Monday's close is expected are stale: FAIL", q["level"] == "FAIL" and "0/4" in q["detail"], q["detail"])
reset(close_date="2026-09-18", open="1")
rp.run_job("daily", now=et_to_utc(2026, 9, 21, 8, 15))
q = step(last(), "quotes")
check("the 08:15 retry only expects Friday's close, so Friday's prices are fine", q["level"] == "OK", q["detail"])
reset(close_date="2026-09-18", open="1")
rp.run_job("daily", now=et_to_utc(2026, 9, 21, 11, 0))
check("during market hours the previous close is what counts (no false alarm for a run that woke up late)", step(last(), "quotes")["level"] == "OK")

# --------------------------------------------------------------------------- Tier 1
print("\nTier 1 step")
reset(tier1="new")
rp.run_job("daily", now=MON_AFTER_CLOSE)
r = last()
check("a new escalation is counted from the queue, reported, and triggers a notification even though nothing failed",
      r["level"] == "OK" and r["new_escalations"] == 1 and "1 new escalation" in step(r, "tier1")["detail"] and len(NOTES) == 1 and "python3 sentinel_tier1.py --show" in NOTES[0][1], str((r, NOTES)))
NOTES.clear(); os.environ["FAKE_REF"] = "r1"
rp.run_job("daily", now=MON_AFTER_CLOSE)
check("re-running does not re-announce the same escalation", last()["new_escalations"] == 0 and len(NOTES) == 1 and "NEW alert" not in NOTES[0][1], str(NOTES))
reset(tier1="stale")
rp.run_job("daily", now=MON_AFTER_CLOSE)
check("Tier 1's own STALE warning is surfaced as a WARN, not buried in a log", step(last(), "tier1")["level"] == "WARN" and "STALE" in step(last(), "tier1")["detail"] and last()["level"] == "WARN")

# --------------------------------------------------------------------------- missed runs
print("\nRuns that did not happen")
reset()
os.environ["FAKE_CLOSE_DATE"] = "2026-09-21"
rp.run_job("daily", now=et_to_utc(2026, 9, 21, 16, 30))       # Monday
os.environ["FAKE_CLOSE_DATE"] = "2026-09-24"
rp.run_job("daily", now=et_to_utc(2026, 9, 24, 16, 30))       # Thursday: Tue and Wed never ran
r = last()
check("a skipped weekday is reported by the next run (the Mac slept or was off)", any("2026-09-22" in n and "2026-09-23" in n for n in r["notes"]) and r["level"] == "WARN", str(r["notes"]))
reset()
os.environ["FAKE_CLOSE_DATE"] = "2026-09-18"
rp.run_job("daily", now=et_to_utc(2026, 9, 18, 16, 30))       # Friday
os.environ["FAKE_CLOSE_DATE"] = "2026-09-21"
rp.run_job("daily", now=et_to_utc(2026, 9, 21, 16, 30))       # Monday: only the weekend between
check("a normal weekend is not a missed run", not any("no daily run" in n for n in last()["notes"]), str(last()["notes"]))
check("a brand-new install is NOT nagged about a weekly scan that has not had its Sunday yet", not any("weekly scan" in n for n in last()["notes"]), str(last()["notes"]))
reset()
old_run = dict(job="daily", started="2026-09-08T16:30:00-04:00", level="OK", steps=[], notes=[])
rp.write_status({"runs": [old_run]})
os.environ["FAKE_CLOSE_DATE"] = "2026-09-21"
rp.run_job("daily", now=et_to_utc(2026, 9, 21, 16, 30))
check("...but once the scheduler has existed for over 9 days with no weekly run, it is called out", any("weekly scan has not run since the scheduler started" in n for n in last()["notes"]), str(last()["notes"]))

# --------------------------------------------------------------------------- weekly + backup
print("\nWeekly job")
reset()
code = rp.run_job("weekly", now=MON_AFTER_CLOSE)
r = last()
check("scan, tracked dossiers, Tier 1, Tier 2 and the backup all ran and are OK",
      code == 0 and [s["name"] for s in r["steps"]] == ["scan+dossier", "tracked dossiers", "tier1", "tier2", "reviews page", "backup"] and r["level"] == "OK", str(r["steps"]))
check("with nothing in the queue, Tier 2 reports 'nothing new to review' and spends nothing", step(r, "tier2")["detail"] == "nothing new to review", step(r, "tier2")["detail"])
check("the scan is verified by the NEW run in the database, with its hit count", "2 hits" in step(r, "scan+dossier")["detail"], step(r, "scan+dossier")["detail"])
bfiles = sorted(rp.BACKUPS.glob("research_*.db"))
bc = sqlite3.connect(str(bfiles[-1])); nruns = bc.execute("select count(*) from screen_runs").fetchone()[0]; integ = bc.execute("pragma integrity_check").fetchone()[0]; bc.close()
check("the backup is a real, readable copy (integrity ok) that contains the scan just made", len(bfiles) == 1 and integ == "ok" and nruns >= 1, f"{bfiles} {integ} {nruns}")

reset(scan="crash")
rp.run_job("weekly", now=MON_AFTER_CLOSE)
r = last()
check("a BROKEN SCANNER does not stop the dossiers or the backup (acceptance test), and is reported as FAIL",
      step(r, "scan+dossier")["level"] == "FAIL" and "Overview" in step(r, "scan+dossier")["detail"] and step(r, "tracked dossiers")["level"] == "OK" and step(r, "backup")["level"] == "OK", str(r["steps"]))
reset(scan="nothing")
c = db.connect(); c.execute("UPDATE screen_runs SET run_at = '2026-09-01T00:00:00+00:00'"); c.close()
rp.run_job("weekly", now=MON_AFTER_CLOSE)
r = last()
check("exit 0 but no new run saved is not accepted as a successful scan", step(r, "scan+dossier")["level"] == "FAIL" and "no NEW run" in step(r, "scan+dossier")["detail"], step(r, "scan+dossier")["detail"])
reset(scan="hits0")
rp.run_job("weekly", now=MON_AFTER_CLOSE)
check("a scan that saved 0 hits is a WARN pointing at the Finviz URLs", step(last(), "scan+dossier")["level"] == "WARN" and "0 hits" in step(last(), "scan+dossier")["detail"])
reset(dossier="blocked")
rp.run_job("weekly", now=MON_AFTER_CLOSE)
check("Finviz blocking the dossier pages ('Stopping early') is a WARN", step(last(), "tracked dossiers")["level"] == "WARN" and "stopped early" in step(last(), "tracked dossiers")["detail"], step(last(), "tracked dossiers")["detail"])

print("\nSaturday: Tier 1 then Tier 2 inside the weekly job")
reset(tier1="new")
rp.run_job("weekly", now=MON_AFTER_CLOSE)
r = last()
check("a new candidate from the fresh screen is escalated AND reviewed in the same run", r["new_escalations"] == 1 and r["new_tickers"] == ["AMRZ"]
      and step(r, "tier2")["level"] == "OK" and "reviewed 1: AMRZ watch" in step(r, "tier2")["detail"], str(r["steps"]))
check("the Saturday notification names the screen, the new alert and Tier 2's verdict",
      len(NOTES) == 1 and "weekly OK" in NOTES[0][1] and "screen 2 hits" in NOTES[0][1] and "1 NEW alert(s): AMRZ" in NOTES[0][1] and "Tier 2: AMRZ watch" in NOTES[0][1], str(NOTES))
check("the notification carries tickers and verdicts only, never money", "$" not in NOTES[0][1], NOTES[0][1])
reset(tier1="new", tier2="refused")
rp.run_job("weekly", now=MON_AFTER_CLOSE)
check("Tier 2 refusing (calibration not passed) is a WARN with the fix, and the backup still runs",
      step(last(), "tier2")["level"] == "WARN" and "calibrate" in step(last(), "tier2")["detail"] and step(last(), "backup")["level"] == "OK", str(last()["steps"]))
reset(tier1="new", tier2="fail")
rp.run_job("weekly", now=MON_AFTER_CLOSE)
check("a review with no usable verdict is a WARN naming the problem", step(last(), "tier2")["level"] == "WARN" and "1 failed" in step(last(), "tier2")["detail"], step(last(), "tier2")["detail"])
reset(tier1="new")
_k = os.environ.pop("ANTHROPIC_API_KEY")
rp.run_job("weekly", now=MON_AFTER_CLOSE)
check("no Anthropic key: Tier 2 is FAIL and not run, everything else still runs", step(last(), "tier2")["level"] == "FAIL" and "ANTHROPIC_API_KEY" in step(last(), "tier2")["detail"]
      and step(last(), "backup")["level"] == "OK", str(last()["steps"]))
os.environ["TIER2_BACKEND"] = "subscription"
reset(tier1="new")
rp.run_job("weekly", now=MON_AFTER_CLOSE)
check("on the subscription, a missing token is FAIL naming --save-token, and no API key is asked for",
      step(last(), "tier2")["level"] == "FAIL" and "CLAUDE_CODE_OAUTH_TOKEN" in step(last(), "tier2")["detail"]
      and "--save-token" in step(last(), "tier2")["detail"], step(last(), "tier2")["detail"])
os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "sk-ant-oat01-faketoken"
check("with the token present, the Tier 2 step needs the token, not the API key", rp.tier2_key() == "CLAUDE_CODE_OAUTH_TOKEN"
      and [s for s in rp.job_steps("weekly") if s["name"] == "tier2"][0]["needs_key"] == "CLAUDE_CODE_OAUTH_TOKEN")
os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN"); os.environ.pop("TIER2_BACKEND")
check("without TIER2_BACKEND the API key is needed, as before", rp.tier2_key() == "ANTHROPIC_API_KEY")
os.environ["ANTHROPIC_API_KEY"] = _k
reset(tier1="new")
rp.run_job("daily", now=MON_AFTER_CLOSE)
check("a weekday alert says Tier 2 will review it on Saturday", len(NOTES) == 1 and "AMRZ" in NOTES[0][1] and "Tier 2 reviews them Saturday" in NOTES[0][1], str(NOTES))
check("the daily job does NOT run Tier 2 (it costs money; Saturdays only)", "tier2" not in [s["name"] for s in last()["steps"]])

reset()
rp.run_job("daily", now=et_to_utc(2026, 9, 22, 8, 15))
check("the 08:15 morning retry stays SILENT when all is fine (one message per weekday, not two)", NOTES == [], str(NOTES))
reset(tier1="new")
rp.run_job("daily", now=et_to_utc(2026, 9, 22, 8, 15))
check("...but speaks up when it finds a new alert", len(NOTES) == 1 and "AMRZ" in NOTES[0][1], str(NOTES))
reset(quotes="zero")
os.environ["FAKE_CLOSE_DATE"] = "2026-09-21"
rp.run_job("daily", now=et_to_utc(2026, 9, 22, 8, 15))
check("...or when something failed", len(NOTES) == 1 and "FAIL" in NOTES[0][1], str(NOTES))

print("\nBackups")
for i in range(11):
    rp.backup_db(datetime(2026, 9, 21, 10, i, tzinfo=UTC))
check("only the newest 8 backups are kept", len(list(rp.BACKUPS.glob("research_*.db"))) == 8)
os.environ["RESEARCH_DB"] = str(tmp / "nowhere.db")
lvl, detail = rp.backup_db(datetime.now(UTC))
check("a missing database is a FAIL with the path, not a crash", lvl == "FAIL" and "nowhere.db" in detail, detail)
os.environ["RESEARCH_DB"] = str(proj / "research.db")


# --------------------------------------------------------------------------- publishing
print("\nPublishing the shareable page")
web = tmp / "web"; web.mkdir()
(tmp / "elsewhere").mkdir()
E1 = rp.validate_publish_dir
check("a folder that does not exist is refused", "does not exist" in (E1(tmp / "nope") or ""))
check("the project folder itself is refused (a web server showing it would expose research.db and .env)", "expose research.db" in (E1(proj) or ""))
check("a folder that CONTAINS the project is refused too (for example the home folder)", "expose research.db" in (E1(tmp) or ""))
leaky = tmp / "leaky"; leaky.mkdir(); (leaky / "positions.yaml").write_text("x")
check("a folder that already holds private files is refused", "contains positions.yaml" in (E1(leaky) or ""))
check("an ordinary web folder is accepted", E1(web) is None)

reset()
buf = io.StringIO()
with contextlib.redirect_stdout(buf): code = rp.cmd_publish_dir(str(proj), False)
check("publish-dir REFUSES the project folder and writes nothing", code == 2 and "REFUSED" in buf.getvalue() and not (proj / ".env").exists(), buf.getvalue())
(proj / ".env").write_text('OTHER=1\nFMP_API_KEY="k"\n')
buf = io.StringIO()
with contextlib.redirect_stdout(buf): code = rp.cmd_publish_dir(str(web), False)
envtxt = (proj / ".env").read_text()
check("publish-dir saves the folder in .env (mode 600), keeps the other lines, and says only the shareable page is published",
      code == 0 and f'PUBLISH_DIR="{web.resolve()}"' in envtxt and "OTHER=1" in envtxt and 'FMP_API_KEY="k"' in envtxt
      and oct((proj / ".env").stat().st_mode & 0o777) == "0o600" and "SHAREABLE" in buf.getvalue(), buf.getvalue() + envtxt)
check("...and a scheduled daily job now includes the publish step, after the calendar and before Tier 1",
      [s["name"] for s in rp.job_steps("daily")] == ["quotes", "calendar", "publish", "tier1", "reviews page"])
buf = io.StringIO()
with contextlib.redirect_stdout(buf): rp.run_job("daily", dry_run=True)
check("--dry-run lists the publish step", "publish" in buf.getvalue(), buf.getvalue())

# happy path
reset(); OLD = "OLD PUBLIC PAGE. No positions, gates or prices are included."
(web / "earnings_calendar.html").write_text(OLD)
os.environ["FAKE_CLOSE_DATE"] = "2026-09-21"
rp.run_job("daily", now=MON_AFTER_CLOSE)
r = last(); pub = step(r, "publish")
new_html = (web / "earnings_calendar.html").read_text()
check("the daily job publishes the shareable page: OK, and the file is replaced", pub["level"] == "OK" and "privacy check passed" in pub["detail"] and new_html != OLD and "No positions, gates or prices" in new_html, str(pub))
check("nothing but the page is in the public folder (no temp files, no database, no yaml, no .env)", sorted(p.name for p in web.iterdir()) == ["earnings_calendar.html"], str(list(web.iterdir())))
check("the PRIVATE page in the project folder is a different file and was not copied", (proj / "earnings_calendar.html").read_text() != new_html)
check("the temp file is cleaned up", not (rp.LOGS / "publish_tmp.html").exists())

# every failure leaves the published file exactly as it was, and never stops the other steps
def try_publish(**modes):
    reset(**modes)
    (web / "earnings_calendar.html").write_text(OLD)
    os.environ["FAKE_CLOSE_DATE"] = "2026-09-21"
    rp.run_job("daily", now=MON_AFTER_CLOSE)
    return last(), (web / "earnings_calendar.html").read_text()
r, txt = try_publish(share="leak")
check("a page containing 'Resting exit' / 'GTC' is NOT published (FAIL) and the old public page is untouched",
      step(r, "publish")["level"] == "FAIL" and "privacy check" in step(r, "publish")["detail"] and txt == OLD and step(r, "tier1")["level"] == "OK", str(step(r, "publish")))
r, txt = try_publish(share="leakgate")
check("a page containing a private free-text note from positions.yaml is NOT published, even with no marker words in it",
      step(r, "publish")["level"] == "FAIL" and "free-text note" in step(r, "publish")["detail"] and txt == OLD, str(step(r, "publish")))
r, txt = try_publish(share="notshare")
check("a page that is not the shareable page (e.g. the full one) is NOT published", step(r, "publish")["level"] == "FAIL" and "not the shareable page" in step(r, "publish")["detail"] and txt == OLD)
r, txt = try_publish(share="crash")
check("if the generator fails nothing is published and the old page stays", step(r, "publish")["level"] == "FAIL" and "could not be generated" in step(r, "publish")["detail"] and txt == OLD)
check("...the calendar and Tier 1 steps ran regardless", step(r, "calendar")["level"] == "OK" and step(r, "tier1")["level"] == "OK")
(web / "positions.yaml").write_text("x")
r, txt = try_publish()
check("if the public folder later comes to hold private files, publishing stops with the reason", step(r, "publish")["level"] == "FAIL" and "contains positions.yaml" in step(r, "publish")["detail"] and txt == OLD, str(step(r, "publish")))
(web / "positions.yaml").unlink()

# status, standalone command, off
reset()
buf = io.StringIO()
with contextlib.redirect_stdout(buf): rp.cmd_status()
check("status shows the public page, its age, and that it is the shareable version only", "Public page:" in buf.getvalue() and "shareable version only" in buf.getvalue(), buf.getvalue())
reset(); (web / "earnings_calendar.html").write_text(OLD)
os.environ["FAKE_CLOSE_DATE"] = "2026-09-21"
code = rp.main(["publish", "--no-notify"])
check("'publish' runs just that step on demand", code == 0 and (web / "earnings_calendar.html").read_text() != OLD and [s["name"] for s in last()["steps"]] == ["publish"])
buf = io.StringIO()
with contextlib.redirect_stdout(buf): code = rp.cmd_publish_dir(None, True)
check("--off turns publishing off, keeps the other .env lines, and does not delete what was published",
      code == 0 and "PUBLISH_DIR" not in (proj / ".env").read_text() and "OTHER=1" in (proj / ".env").read_text() and (web / "earnings_calendar.html").exists())
check("with publishing off, the daily job has no publish step", [s["name"] for s in rp.job_steps("daily")] == ["quotes", "calendar", "tier1", "reviews page"])
buf = io.StringIO()
with contextlib.redirect_stdout(buf): code = rp.main(["publish"])
check("'publish' with no folder set explains how to turn it on and exits 2", code == 2 and "publish-dir" in buf.getvalue())
(proj / ".env").unlink()


# --------------------------------------------------------------------------- publishing through git / GitHub
print("\nPublishing through git (a real local repository stands in for GitHub)")
import subprocess as sp
def G(*args, cwd, check=True):
    r = sp.run(["git", "-c", "user.name=Test", "-c", "user.email=t@example.com"] + list(args), cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode: raise RuntimeError(f"git {args}: {r.stderr}")
    return r.stdout.strip()
bare = tmp / "origin.git"; sp.run(["git", "init", "--bare", "-b", "main", str(bare)], capture_output=True, check=True)
seed = tmp / "seed"; sp.run(["git", "clone", str(bare), str(seed)], capture_output=True)
(seed / "README.md").write_text("public calendar\n"); G("checkout", "-b", "main", cwd=seed, check=False); G("add", "README.md", cwd=seed); G("commit", "-m", "init", cwd=seed); G("push", "origin", "main", cwd=seed)
work = tmp / "clone"; sp.run(["git", "clone", str(bare), str(work)], capture_output=True, check=True)
G("config", "user.name", "Test", cwd=work); G("config", "user.email", "t@example.com", cwd=work)
N = lambda: int(G("rev-list", "--count", "main", cwd=bare))
def pub_git(dir_=None, name=None, **modes):
    reset(**modes)
    os.environ["PUBLISH_DIR"] = str(dir_ or work); os.environ["PUBLISH_GIT"] = "1"
    if name: os.environ["PUBLISH_FILE"] = name
    os.environ["FAKE_CLOSE_DATE"] = "2026-09-21"
    rp.run_job("publish", now=MON_AFTER_CLOSE)
    return last()["steps"][0]

# validation
nogit = tmp / "plainfolder"; nogit.mkdir()
check("a folder that is not in a git repository is refused for git publishing", "not inside a git repository" in (rp.validate_git_target(nogit) or ""))
noremote = tmp / "noremote"; sp.run(["git", "init", "-b", "main", str(noremote)], capture_output=True); G("config", "user.email", "a@b.c", cwd=noremote)
(noremote / "f").write_text("x"); G("add", "f", cwd=noremote); G("commit", "-m", "c", cwd=noremote)
check("a repository with no 'origin' remote is refused", "no 'origin' remote" in (rp.validate_git_target(noremote) or ""))
empty = tmp / "emptyrepo"; sp.run(["git", "init", "-b", "main", str(empty)], capture_output=True); G("remote", "add", "origin", str(bare), cwd=empty); G("config", "user.email", "a@b.c", cwd=empty)
check("a repository with no commits yet is refused, with the fix", "no commits yet" in (rp.validate_git_target(empty) or ""))
detached = tmp / "detached"; sp.run(["git", "clone", str(bare), str(detached)], capture_output=True); G("config", "user.email", "a@b.c", cwd=detached); G("checkout", "--detach", cwd=detached)
check("a detached HEAD is refused", "detached HEAD" in (rp.validate_git_target(detached) or ""), str(rp.validate_git_target(detached)))
noid = tmp / "noid"; sp.run(["git", "clone", str(bare), str(noid)], capture_output=True)
G("config", "--unset", "user.email", cwd=noid, check=False)
# Hide the machine's own git identity (~/.gitconfig), or this test only passes on a machine that has none.
_empty_cfg = tmp / "empty_gitconfig"; _empty_cfg.write_text("")
_saved_git = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
os.environ["GIT_CONFIG_GLOBAL"], os.environ["GIT_CONFIG_NOSYSTEM"] = str(_empty_cfg), "1"
try:
    _e = rp.validate_git_target(noid)
finally:
    for _k, _v in _saved_git.items():
        if _v is None: os.environ.pop(_k, None)
        else: os.environ[_k] = _v
check("a repository with no git identity is refused (a commit would fail)", "user.name and user.email" in (_e or ""), str(_e))
saved_home = rp.HERE; rp.HERE = work / "inner"; rp.HERE.mkdir()
check("a repository that also contains the PROJECT folder is refused", "also contains your project folder" in (rp.validate_git_target(work) or ""), str(rp.validate_git_target(work)))
rp.HERE = saved_home; (work / "inner").rmdir()
check("the clone used below passes validation", rp.validate_git_target(work) is None, str(rp.validate_git_target(work)))

# enabling: the token in a remote URL must never be printed
tokrepo = tmp / "tokrepo"; sp.run(["git", "clone", str(bare), str(tokrepo)], capture_output=True)
G("config", "user.email", "a@b.c", cwd=tokrepo); G("config", "user.name", "T", cwd=tokrepo)
G("remote", "set-url", "origin", "https://someone:ghp_SECRETTOKEN123@github.com/someone/site.git", cwd=tokrepo)
reset(); buf = io.StringIO()
with contextlib.redirect_stdout(buf): code = rp.cmd_publish_dir(str(tokrepo), False, True, None)
out = buf.getvalue()
check("enabling git publishing shows the remote WITHOUT the embedded token, and warns that history is permanent",
      code == 0 and "ghp_SECRETTOKEN123" not in out and "github.com/someone/site.git" in out and "permanent" in out, out)
buf = io.StringIO()
with contextlib.redirect_stdout(buf): rp.cmd_status(); rp.cmd_publish_dir(None, False)
check("status and the settings view never show the token either", "ghp_SECRETTOKEN123" not in buf.getvalue(), buf.getvalue())
with contextlib.redirect_stdout(io.StringIO()): rp.cmd_publish_dir(None, True)
buf = io.StringIO()
with contextlib.redirect_stdout(buf): code = rp.cmd_publish_dir(str(work), False, True, "../evil.html")
check("a page name with a path in it is refused", code == 2 and "not a valid page name" in buf.getvalue())

# happy path
n0 = N()
s = pub_git()
check("publishing commits ONE file and pushes it (GitHub gets exactly one new commit)", s["level"] == "OK" and "pushed" in s["detail"] and N() == n0 + 1, str(s))
check("...the commit contains only earnings_calendar.html", G("show", "--name-only", "--format=", "main", cwd=bare) == "earnings_calendar.html", G("show", "--name-only", "--format=", "main", cwd=bare))
check("...and the pushed page is the shareable one", "No positions, gates or prices are included" in G("show", "main:earnings_calendar.html", cwd=bare))
s = pub_git()
check("running again with an unchanged page makes no new commit and says so", s["level"] == "OK" and "unchanged" in s["detail"] and N() == n0 + 1, str(s))
reset(); os.environ["PUBLISH_DIR"] = str(work); os.environ["PUBLISH_GIT"] = "1"; os.environ["FAKE_TAG"] = "<p>v2</p>"; os.environ["FAKE_CLOSE_DATE"] = "2026-09-21"
rp.run_job("publish", now=MON_AFTER_CLOSE); s = last()["steps"][0]
check("a changed page becomes a new commit", s["level"] == "OK" and N() == n0 + 2, str(s))

# other people's work in the same repo is left alone
(work / "notes.txt").write_text("my private notes"); G("add", "notes.txt", cwd=work); (work / "secret.txt").write_text("untracked secret")
os.environ["FAKE_TAG"] = "<p>v3</p>"
reset(); os.environ["PUBLISH_DIR"] = str(work); os.environ["PUBLISH_GIT"] = "1"; os.environ["FAKE_TAG"] = "<p>v3</p>"; os.environ["FAKE_CLOSE_DATE"] = "2026-09-21"
rp.run_job("publish", now=MON_AFTER_CLOSE); s = last()["steps"][0]
files = G("show", "--name-only", "--format=", "main", cwd=bare)
check("something else you had staged in that repo is NOT swept into the public commit, and an untracked file is not added either",
      s["level"] == "OK" and files == "earnings_calendar.html" and (work / "notes.txt").read_text() == "my private notes" and (work / "secret.txt").exists(), f"{s} {files}")
G("reset", "-q", "notes.txt", cwd=work, check=False); (work / "notes.txt").unlink(missing_ok=True); (work / "secret.txt").unlink()

# a leak never reaches git
n1 = N(); before = (work / "earnings_calendar.html").read_text()
s = pub_git(share="leak")
check("a page that fails the privacy check is NOT committed or pushed, and the repository is unchanged",
      s["level"] == "FAIL" and "privacy check" in s["detail"] and N() == n1 and (work / "earnings_calendar.html").read_text() == before and G("status", "--porcelain", cwd=work) == "", str(s))
s = pub_git(share="leakgate")
check("...also when the only leak is a private note from positions.yaml", s["level"] == "FAIL" and N() == n1)

# a remote that rejects the push: the commit stays local and is retried next time
hook = bare / "hooks" / "pre-receive"; hook.write_text("#!/bin/sh\necho rejected by test >&2\nexit 1\n"); hook.chmod(0o755)
os.environ["FAKE_TAG"] = "<p>v4</p>"
reset(); os.environ["PUBLISH_DIR"] = str(work); os.environ["PUBLISH_GIT"] = "1"; os.environ["FAKE_TAG"] = "<p>v4</p>"; os.environ["FAKE_CLOSE_DATE"] = "2026-09-21"
rp.run_job("publish", now=MON_AFTER_CLOSE); s = last()["steps"][0]
check("if GitHub refuses the push it is a FAIL that says the commit is saved locally and will be retried",
      s["level"] == "FAIL" and "committed locally but the push failed" in s["detail"] and "retried" in s["detail"] and N() == n1, str(s))
buf = io.StringIO()
with contextlib.redirect_stdout(buf): rp.cmd_status()
check("status warns about the unpushed commit", "1 commit(s) not pushed" in buf.getvalue(), buf.getvalue())
hook.unlink()
reset(); os.environ["PUBLISH_DIR"] = str(work); os.environ["PUBLISH_GIT"] = "1"; os.environ["FAKE_TAG"] = "<p>v4</p>"; os.environ["FAKE_CLOSE_DATE"] = "2026-09-21"
rp.run_job("publish", now=MON_AFTER_CLOSE); s = last()["steps"][0]
check("once the remote accepts pushes again, the next run pushes the waiting commit", s["level"] == "OK" and "pushed" in s["detail"] and N() == n1 + 1, str(s))

# somebody else pushed in the meantime (for example a README edit on GitHub)
(seed / "README.md").write_text("edited on GitHub\n"); G("add", "README.md", cwd=seed); G("commit", "-m", "readme edit", cwd=seed); G("pull", "--rebase", "origin", "main", cwd=seed); G("push", "origin", "main", cwd=seed)
n2 = N()
reset(); os.environ["PUBLISH_DIR"] = str(work); os.environ["PUBLISH_GIT"] = "1"; os.environ["FAKE_TAG"] = "<p>v5</p>"; os.environ["FAKE_CLOSE_DATE"] = "2026-09-21"
rp.run_job("publish", now=MON_AFTER_CLOSE); s = last()["steps"][0]
check("when the repository moved on (someone edited it elsewhere) the run updates first, then pushes, and keeps both changes",
      s["level"] == "OK" and N() == n2 + 1 and "edited on GitHub" in G("show", "main:README.md", cwd=bare), str(s))

# never overwrite someone else's page
(work / "index.html").write_text("<html>the other project's home page</html>"); G("add", "index.html", cwd=work); G("commit", "-m", "other page", cwd=work); G("push", "origin", "main", cwd=work)
n3 = N()
s = pub_git(name="index.html")
check("an existing page this tool did not write (your other project's index.html) is never overwritten",
      s["level"] == "FAIL" and "already exists and is not a page this tool wrote" in s["detail"] and (work / "index.html").read_text() == "<html>the other project's home page</html>" and N() == n3, str(s))

# a subfolder such as GitHub Pages' /docs
(work / "docs").mkdir(); (work / "docs" / ".keep").write_text(""); G("add", "docs/.keep", cwd=work); G("commit", "-m", "docs", cwd=work); G("push", "origin", "main", cwd=work)
s = pub_git(dir_=work / "docs")
check("a subfolder of the repository works (for example docs/ for GitHub Pages)", s["level"] == "OK" and G("show", "--name-only", "--format=", "main", cwd=bare) == "docs/earnings_calendar.html", f"{s} {G('show', '--name-only', '--format=', 'main', cwd=bare)}")

# selfcheck proves GitHub is reachable without a password
reset(); os.environ["PUBLISH_DIR"] = str(work); os.environ["PUBLISH_GIT"] = "1"
buf = io.StringIO()
with contextlib.redirect_stdout(buf): rp.cmd_selfcheck()
check("the self-check tests GitHub access non-interactively (git ls-remote) when git publishing is on", "can reach GitHub without a password prompt" in buf.getvalue() and "FAIL  can reach GitHub" not in buf.getvalue(), buf.getvalue())
G("remote", "set-url", "origin", str(tmp / "does-not-exist.git"), cwd=work)
buf = io.StringIO()
with contextlib.redirect_stdout(buf): code = rp.cmd_selfcheck()
check("...and fails clearly when it cannot", code == 2 and "FAIL  can reach GitHub" in buf.getvalue())
G("remote", "set-url", "origin", str(bare), cwd=work)

# turning it off
(proj / ".env").write_text("")
with contextlib.redirect_stdout(io.StringIO()): rp.cmd_publish_dir(str(work), False, True, None)
env_on = (proj / ".env").read_text()
with contextlib.redirect_stdout(io.StringIO()): rp.cmd_publish_dir(None, True)
env_off = (proj / ".env").read_text()
check("--off removes the folder, git mode and name settings", "PUBLISH_GIT" in env_on and "PUBLISH_DIR" in env_on and not any(k in env_off for k in ("PUBLISH_DIR", "PUBLISH_GIT", "PUBLISH_FILE")), env_on + "|" + env_off)
(proj / ".env").unlink()
reset()

# --------------------------------------------------------------------------- lock, dry run, status
print("\nLock, dry run, status")
import fcntl
reset()
holder = open(str(rp.LOCK), "w"); fcntl.flock(holder, fcntl.LOCK_EX)
code = rp.run_job("daily", now=MON_AFTER_CLOSE)
holder.close()
r = last()
check("a second run while one is in progress is skipped and recorded, not run in parallel", code == 1 and r.get("skipped") and r["steps"] == [] and "in progress" in r["notes"][0], str(r))
reset()
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf): code = rp.run_job("daily", dry_run=True)
out = buf.getvalue()
check("--dry-run lists the steps and whether the key is present, and runs nothing", code == 0 and "quotes" in out and "ALPHAVANTAGE_API_KEY: found" in out and not rp.STATUS.exists(), out)
rp.run_job("daily", now=MON_AFTER_CLOSE); rp.run_job("weekly", now=MON_AFTER_CLOSE)
buf = io.StringIO()
with contextlib.redirect_stdout(buf): rp.cmd_status()
out = buf.getvalue()
check("status shows both jobs, price freshness, the queue, the newest backup and the ledger age",
      "daily run" in out and "weekly run" in out and "Prices:" in out and "Tier 1 queue:" in out and "Backup: research_" in out and "Ledger: positions.yaml as_of is" in out and "refresh it" in out, out)

# --------------------------------------------------------------------------- Alpha Vantage switch + masking secrets
print("\nSkipping one run, and noticing a schedule that is off (DECISIONS.md 53)")
reset()
with contextlib.redirect_stdout(io.StringIO()) as _b:
    rp.main(["skip", "weekly"])
check("'skip weekly' sets a one-time skip and says weekday runs continue", "SKIP_NEXT_WEEKLY" in (rp.HERE / ".env").read_text()
      and "Weekday runs continue" in _b.getvalue(), _b.getvalue())
_steps_run = []
_orig_rs = rp.run_subprocess
rp.run_subprocess = lambda step, env, log: _steps_run.append(step["name"]) or _orig_rs(step, env, log)
code = rp.run_job("weekly", now=et_to_utc(2026, 10, 10, 9, 30))
check("the next Saturday run is skipped: nothing runs, nothing is spent", code == 0 and _steps_run == [] and last().get("user_skip"), str(last()))
check("...you are told it was skipped (so silence still means 'did not run')", NOTES and "weekly skipped as you asked" in NOTES[-1][1], str(NOTES))
check("...and the skip is used up: the following Saturday runs normally", "SKIP_NEXT_WEEKLY" not in (rp.HERE / ".env").read_text())
rp.run_job("weekly", now=et_to_utc(2026, 10, 17, 9, 30))
check("the following Saturday's steps all run", "scan+dossier" in _steps_run and "backup" in [s["name"] for s in last()["steps"]], str(_steps_run))
reset()
with contextlib.redirect_stdout(io.StringIO()):
    rp.main(["skip", "weekly"])
rp.run_job("daily", now=MON_AFTER_CLOSE)
check("skipping the weekly run does NOT skip weekday runs", [s["name"] for s in last()["steps"]][:1] == ["quotes"] and "SKIP_NEXT_WEEKLY" in (rp.HERE / ".env").read_text())
with contextlib.redirect_stdout(io.StringIO()) as _b:
    rp.main(["skip", "--off"])
check("'skip --off' cancels it", "SKIP_NEXT" not in (rp.HERE / ".env").read_text() and "No runs will be skipped" in _b.getvalue())
rp.run_subprocess = _orig_rs

# replay of Oct 2-9, 2026: the schedule was removed, so the last record is Oct 2's run
reset()
rp.write_status({"runs": [dict(job="daily", started="2026-10-02T16:30:00-04:00", level="FAIL", log="x.log",
                               steps=[dict(name="calendar", level="FAIL", seconds=1,
                                           detail="HTTPError: 402 for url: https://financialmodelingprep.com/stable/earnings-calendar?from=2026-09-02&apikey=FAKEOLDKEY9999")],
                               notes=[])]})
check("an old entry's leaked key is cleaned out of status.json the next time it is written", "FAKEOLDKEY9999" not in rp.STATUS.read_text()
      and "apikey=***" in rp.STATUS.read_text())
_ss, _today = rp.schedule_state, rp.date
rp.schedule_state = lambda: {"daily": False, "weekly": False}
class _D(date):
    @classmethod
    def today(cls): return date(2026, 10, 9)
rp.date = _D
with contextlib.redirect_stdout(io.StringIO()) as _b:
    rp.cmd_status()
_out = _b.getvalue()
check("status says loudly when the schedule is OFF, and how to turn it on", "Schedule: OFF" in _out and "NOTHING runs automatically" in _out
      and "python3 sentinel_schedule.py" in _out, _out[:400])
check("status says how many weekdays were missed since the last daily run (Oct 5-8 here)", "Last daily run: 2026-10-02 (4 weekday(s) missed since)" in _out, _out[:600])
rp.schedule_state = lambda: {"daily": True, "weekly": True}
rp.date = date
rp.write_status({"runs": [dict(job="daily", started=date.today().isoformat() + "T16:30:00-04:00", level="OK", steps=[], notes=[])]})
with contextlib.redirect_stdout(io.StringIO()) as _b:
    rp.cmd_status()
check("when all is well, status says the schedule is ON and reports no missed days", "Schedule: ON" in _b.getvalue() and "missed" not in _b.getvalue(), _b.getvalue()[:300])
rp.schedule_state = _ss

print("\nEarnings source and secrets (DECISIONS.md 52)")
reset(calendar="avdown")
rp.run_job("daily", now=MON_AFTER_CLOSE)
r = last()
check("Alpha Vantage down: the calendar is a WARN (page made from stored dates), not a FAIL, and the others run",
      step(r, "calendar")["level"] == "WARN" and "stored" in step(r, "calendar")["detail"] and step(r, "tier1")["level"] == "OK" and r["level"] == "WARN", str(r["steps"]))
reset(calendar="fmp402")
rp.run_job("daily", now=MON_AFTER_CLOSE)
r = last()
check("an error containing a web address with a key is MASKED in status.json", "apikey=***" in step(r, "calendar")["detail"]
      and "test-key" not in json.dumps(rp.read_status()), step(r, "calendar")["detail"])
check("...and in the notification (the Oct 1 leak cannot repeat)", NOTES and "test-key" not in NOTES[-1][1] and "apikey=***" in NOTES[-1][1], str(NOTES))
logtxt = (rp.LOGS / r["log"]).read_text()
check("...and in the log file", "test-key" not in logtxt and "apikey=***" in logtxt)
check("redact() masks key=value pairs, Anthropic keys and the literal value of every key or topic in .env",
      rp.redact("x?apikey=ABC&token=DEF sk-ant-api03-zzz test-key") == "x?apikey=***&token=*** sk-ant-*** ***", rp.redact("x?apikey=ABC&token=DEF sk-ant-api03-zzz test-key"))
check("redact() leaves ordinary text alone", rp.redact("daily OK. 2 NEW alert(s): AMRZ, COO") == "daily OK. 2 NEW alert(s): AMRZ, COO")
import getpass as _gp
_saved_gp = _gp.getpass
_gp.getpass = lambda prompt="": "NEWAVKEY12345"
envf = rp.HERE / ".env"
_env_before = envf.read_text() if envf.exists() else None
with contextlib.redirect_stdout(io.StringIO()) as _b:
    rc = rp.main(["av-key"])
check("'av-key' saves the key to .env (mode 600) without printing it", rc == 0 and 'ALPHAVANTAGE_API_KEY="NEWAVKEY12345"' in envf.read_text()
      and "NEWAVKEY12345" not in _b.getvalue() and oct(envf.stat().st_mode)[-3:] == "600", _b.getvalue())
_gp.getpass = lambda prompt="": "not a key!"
with contextlib.redirect_stdout(io.StringIO()):
    rc = rp.main(["av-key"])
check("'av-key' refuses something that is not a key, and changes nothing", rc == 1 and 'NEWAVKEY12345' in envf.read_text())
_gp.getpass = _saved_gp
if _env_before is None:
    envf.unlink()
else:
    envf.write_text(_env_before)

# --------------------------------------------------------------------------- iPhone notifications (ntfy)
print("\niPhone notifications")
import types as _types, stat as _stat
posts = []
fake_req = _types.ModuleType("requests")
class _R:
    def __init__(self, code): self.status_code = code
def _post(url, data=None, headers=None, timeout=None):
    if os.environ.get("FAKE_NTFY") == "down": raise ConnectionError("no network")
    posts.append((url, data.decode("utf-8"), headers)); return _R(200)
fake_req.post = _post
_real_req = sys.modules.get("requests")
sys.modules["requests"] = fake_req
os.environ.pop("NTFY_TOPIC", None)
envf = rp.HERE / ".env"
_env_before = envf.read_text() if envf.exists() else None
rp.env_set("NTFY_TOPIC", None)
check("with no topic set, nothing is sent to a phone", rp.push_phone("Sentinel", "daily OK") is False and posts == [])
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = rp.main(["phone"])
topic = rp.phone_target()[1]
check("'phone' creates a long, random private topic, saves it in .env (mode 600) and sends a test",
      rc == 0 and topic and topic.startswith("sentinel-") and len(topic) >= 30 and f'NTFY_TOPIC="{topic}"' in envf.read_text()
      and _stat.S_IMODE(envf.stat().st_mode) == 0o600 and posts and posts[-1][0] == f"https://ntfy.sh/{topic}" and "Test" in posts[-1][1], buf.getvalue())
check("the setup tells you exactly what to type in the ntfy app", topic in buf.getvalue() and "App Store" in buf.getvalue())
with contextlib.redirect_stdout(io.StringIO()):
    rp.main(["phone"])
check("running it again keeps the same topic (your phone stays subscribed)", rp.phone_target()[1] == topic)
posts.clear()
rp.push_phone("Sentinel", "daily OK. no new alerts")
check("an all-clear goes at normal priority", posts[-1][2]["Priority"] == "default" and posts[-1][1] == "daily OK. no new alerts")
rp.push_phone("Sentinel", "daily OK. 1 NEW alert(s): AMRZ")
check("a new alert goes at HIGH priority", posts[-1][2]["Priority"] == "high")
rp.push_phone("Sentinel", "daily FAIL. quotes: 0/4")
check("a failure goes at HIGH priority", posts[-1][2]["Priority"] == "high")
os.environ["FAKE_NTFY"] = "down"
check("a phone that cannot be reached never breaks a run (returns False, no exception)", rp.push_phone("Sentinel", "x") is False)
os.environ.pop("FAKE_NTFY")
_saved_notify = rp.notify
posts.clear()
_which = rp.shutil.which
rp.shutil.which = lambda name: None
check("the real notify() sends to the phone even where there is no Mac notification centre", _real_notify_fn("Sentinel", "weekly OK") is True and posts and posts[-1][1] == "weekly OK")
rp.shutil.which = _which
with contextlib.redirect_stdout(io.StringIO()):
    rp.main(["phone", "--new"])
check("'phone --new' replaces the topic (in case the old name leaked)", rp.phone_target()[1] not in (None, topic))
with contextlib.redirect_stdout(io.StringIO()):
    rp.main(["phone", "--off"])
check("'phone --off' removes it; nothing more is sent", rp.phone_target()[1] is None and rp.push_phone("Sentinel", "x") is False)
if _real_req is not None:
    sys.modules["requests"] = _real_req
else:
    sys.modules.pop("requests", None)
if _env_before is None:
    envf.unlink(missing_ok=True)
else:
    envf.write_text(_env_before)

# --------------------------------------------------------------------------- selfcheck + notify
print("\nSelfcheck and notification")
buf = io.StringIO()
with contextlib.redirect_stdout(buf): code = rp.cmd_selfcheck()
out = buf.getvalue()
check("selfcheck verifies imports, folder access, the database, the key and the time zone, and says PASSED", "SELFCHECK PASSED" in out and "can open research.db" in out and code == 0, out)
saved = os.environ.pop("ALPHAVANTAGE_API_KEY")
buf = io.StringIO()
with contextlib.redirect_stdout(buf): code = rp.cmd_selfcheck()
check("selfcheck FAILS when the key is missing", code == 2 and "SELFCHECK FAILED" in buf.getvalue())
os.environ["ALPHAVANTAGE_API_KEY"] = saved
import subprocess
calls = []
real_which, real_run = shutil.which, subprocess.run
rp_notify = rp.notify
del rp.notify
import importlib; importlib.reload(rp); rp.configure(proj)
shutil.which = lambda n: "/usr/bin/osascript" if n == "osascript" else real_which(n)
subprocess.run = lambda *a, **k: calls.append(a[0]) or None
rp.notify('Stock "research"', 'quote " and \\ backslash')
shutil.which, subprocess.run = real_which, real_run
check("the notification text is escaped so quotes cannot break the AppleScript", calls and calls[0][0] == "osascript" and '\\"research\\"' in calls[0][2] and "\\\\ backslash" in calls[0][2], str(calls))
shutil.which = lambda n: None if n == "osascript" else real_which(n)
check("without osascript (not a Mac) notifying is a quiet no-op", rp.notify("t", "b") is False)
shutil.which = real_which

shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} checks")
sys.exit(0 if all(results) else 1)

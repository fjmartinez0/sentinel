"""Alpha Vantage as the calendar's earnings source (DECISIONS.md 52), the stored-data fallback, and key masking.
A fake `requests` answers like Alpha Vantage, so nothing is fetched and no request is used up."""
import contextlib, io, json, os, sys, tempfile, types
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sentinel_db as db
import sentinel_calendar as cal

results = []
def check(label, ok, detail=""):
    ok = bool(ok); results.append(ok); print(("  ok    " if ok else "  FAIL  ") + label + ("" if ok else f"  {detail}"))

KEY = "AVSECRET12345"
TODAY = date(2026, 10, 2)
CSV = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\n"
       "LBRT,Liberty Energy,2026-10-15,2026-09-30,0.18,USD,post-market\n"
       "ASML,ASML Holding,2026-10-14,2026-09-30,6.9,USD,pre-market\n"
       "NEWCO,New Co,2026-11-20,2026-09-30,,USD,\n"
       ",Broken row,2026-10-20,2026-09-30,1,USD,\n"
       "BAD,Bad date,soon,2026-09-30,1,USD,\n")
HIST = {"NYT": [{"fiscalDateEnding": "2026-09-30", "reportedDate": "2026-09-30", "reportedEPS": "0.70", "estimatedEPS": "0.62",
                 "surprise": "0.08", "surprisePercentage": "12.9", "reportTime": "post-market"},
                {"fiscalDateEnding": "2026-06-30", "reportedDate": "2026-08-05", "reportedEPS": "0.50", "estimatedEPS": "0.55"}],
        "MELI": [{"reportedDate": "2026-09-29", "reportedEPS": "None", "estimatedEPS": "9.1"}]}


class Resp:
    def __init__(self, text, status=200): self.text, self.status_code = text, status


class FakeAV:
    def __init__(self, mode="ok"):
        self.mode, self.calls = mode, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params))
        if self.mode == "boom":
            raise ConnectionError(f"Max retries exceeded with url: /query?function=X&apikey={params['apikey']}")
        if self.mode == "http500":
            return Resp("oops", 500)
        if self.mode == "limit":
            return Resp(json.dumps({"Information": "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day."}))
        if params["function"] == "EARNINGS_CALENDAR":
            return Resp(CSV)
        return Resp(json.dumps({"symbol": params["symbol"], "quarterlyEarnings": HIST.get(params["symbol"], [])}))


def use(fake):
    sys.modules["requests"] = types.SimpleNamespace(get=fake.get)
    return fake


# ------------------------------------------------------------------------------------------------ parsing
print("Reading Alpha Vantage's answers")
f = use(FakeAV())
rows = cal.fetch_av_calendar(KEY)
by = {r["symbol"]: r for r in rows}
check("the calendar CSV becomes rows in the shape the page already uses", by["LBRT"] == {"symbol": "LBRT", "date": "2026-10-15", "time": "amc",
      "epsEstimated": 0.18, "confirmed": False}, by.get("LBRT"))
check("pre-market becomes 'bmo', a blank time stays blank, a blank estimate is None", by["ASML"]["time"] == "bmo" and by["NEWCO"]["time"] == ""
      and by["NEWCO"]["epsEstimated"] is None)
check("rows with no symbol or a bad date are skipped", set(by) == {"LBRT", "ASML", "NEWCO"})
check("one request, with the key sent only as a parameter", len(f.calls) == 1 and f.calls[0]["function"] == "EARNINGS_CALENDAR" and f.calls[0]["apikey"] == KEY)
got = cal.fetch_av_actual(KEY, "NYT", "2026-09-27", "2026-10-02")
check("a result inside the window is returned with actual and estimate", got == {"symbol": "NYT", "date": "2026-09-30", "epsEstimated": 0.62,
      "epsActual": 0.70, "confirmed": True}, got)
check("an older result outside the window is ignored", cal.fetch_av_actual(KEY, "NYT", "2026-10-01", "2026-10-02") is None)
check("'None' (not yet published) is treated as no result", cal.fetch_av_actual(KEY, "MELI", "2026-09-27", "2026-10-02") is None)

for mode, want in (("limit", "rate limit is 25 requests per day"), ("http500", "HTTP 500"), ("boom", "could not reach Alpha Vantage")):
    use(FakeAV(mode))
    try:
        cal.fetch_av_calendar(KEY); err = ""
    except cal.AvError as e:
        err = str(e)
    check(f"{mode}: a clear error, and the key never appears in it", want in err and KEY not in err and "apikey" not in err, err)
use(FakeAV("ok"))
sys.modules["requests"] = types.SimpleNamespace(get=lambda url, params=None, timeout=None: Resp("symbol,foo\nX,1\n"))
try:
    cal.fetch_av_calendar(KEY); err = ""
except cal.AvError as e:
    err = str(e)
check("unexpected columns are reported, not guessed", "unexpected earnings-calendar columns" in err, err)

# ------------------------------------------------------------------------------------------------ the merge + fallback
print("Building the page's rows")


def seeded_db(td):
    p = Path(td) / "r.db"
    c = db.connect(p)
    db.init_schema(c)
    db.insert_calendar_run(c, as_of_date="2026-09-28", window_from="2026-08-29", window_to="2026-11-30", quotes={}, events=[
        {"ticker": "NYT", "event_date": "2026-09-29", "kind": "held", "time": "amc", "confirmed": False, "eps_est": 0.62},
        {"ticker": "DUOL", "event_date": "2026-09-10", "kind": "held", "eps_est": 0.6, "eps_act": 0.75},
        {"ticker": "LBRT", "event_date": "2026-10-15", "kind": "held", "eps_est": 0.18},
        {"ticker": "ATRL.TO", "event_date": "2026-09-30", "kind": "held", "eps_est": 0.9},
        {"ticker": "MELI", "event_date": "2026-09-29", "kind": "held", "eps_est": 9.1}])
    return c


tracked = {"NYT", "DUOL", "LBRT", "ATRL.TO", "MELI", "ASML"}
with tempfile.TemporaryDirectory() as td:
    conn = seeded_db(td)
    f, waits = use(FakeAV()), []
    rows, warns = cal.get_earnings_rows(KEY, TODAY, tracked, conn, sleep=waits.append)
    k = {(r["symbol"], r["date"]): r for r in rows}
    check("upcoming dates come from Alpha Vantage", ("ASML", "2026-10-14") in k and ("NEWCO", "2026-11-20") in k)
    check("past results already stored are kept (DUOL), so 'recently reported' still works", k[("DUOL", "2026-09-10")]["epsActual"] == 0.75)
    check("a tracked name that just reported gets its result fetched and merged into ONE row (NYT, date moved by a day)",
          k.get(("NYT", "2026-09-30"), {}).get("epsActual") == 0.70 and ("NYT", "2026-09-29") not in k, sorted(k))
    check("only names without a result, in the last 5 days, US-listed, are asked for (MELI and NYT; not DUOL, not ATRL.TO)",
          sorted(c["symbol"] for c in f.calls if c["function"] == "EARNINGS") == ["MELI", "NYT"], f.calls)
    check("requests are spaced out for the free tier's per-minute limit", waits == [cal.AV_DELAY, cal.AV_DELAY], waits)
    check("a normal day uses 3 requests, well inside 25 a day, and has no warnings", len(f.calls) == 3 and warns == [], (len(f.calls), warns))
    check("a result not yet published leaves the row as it was (MELI)", k[("MELI", "2026-09-29")].get("epsActual") is None)

    f = use(FakeAV("limit"))
    rows, warns = cal.get_earnings_rows(KEY, TODAY, tracked, conn, sleep=lambda s: None)
    k = {(r["symbol"], r["date"]): r for r in rows}
    check("daily limit reached: the page uses the stored upcoming dates and says so", ("LBRT", "2026-10-15") in k and warns
          and "earnings source unavailable" in warns[0] and "2026-09-28" in warns[0], warns)
    check("...and makes no further requests that day", len(f.calls) == 1)
    check("...and the warning carries no key", KEY not in " ".join(warns))

    old_cap = cal.AV_MAX_ACTUALS
    cal.AV_MAX_ACTUALS = 1
    f = use(FakeAV())
    rows, warns = cal.get_earnings_rows(KEY, TODAY, tracked, conn, sleep=lambda s: None)
    check("never more than the per-run cap; the rest wait for the next run, with a note",
          sum(1 for c in f.calls if c["function"] == "EARNINGS") == 1 and any("left for the next run" in w for w in warns), warns)
    cal.AV_MAX_ACTUALS = old_cap
    conn.close()

f = use(FakeAV("limit"))
rows, warns = cal.get_earnings_rows(KEY, TODAY, tracked, None, sleep=lambda s: None)
check("with no database and no source, the page is empty rather than broken", rows == [] and "none stored" in warns[0], warns)

# ------------------------------------------------------------------------------------------------ the key
print("The key")
saved = os.environ.pop("ALPHAVANTAGE_API_KEY", None)
envf = HERE / ".env"
before = envf.read_text() if envf.exists() else None
envf.write_text('OTHER=1\nALPHAVANTAGE_API_KEY="FROMDOTENV123"\n')
check("a manual run finds the key in .env", cal.av_key() == "FROMDOTENV123")
os.environ["ALPHAVANTAGE_API_KEY"] = "FROMENV456"
check("the environment wins over .env", cal.av_key() == "FROMENV456")
if before is None:
    envf.unlink()
else:
    envf.write_text(before)
os.environ.pop("ALPHAVANTAGE_API_KEY", None)
if saved:
    os.environ["ALPHAVANTAGE_API_KEY"] = saved

use(FakeAV())
buf = io.StringIO()
cal.AV_DELAY = 0
with contextlib.redirect_stdout(buf):
    rc = cal.av_check(KEY, TODAY)
check("--av-check makes 2 requests, reports what came back and never prints the key",
      rc == 0 and "earnings calendar: 3 upcoming" in buf.getvalue() and KEY not in buf.getvalue(), buf.getvalue())

print(f"\n{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} checks")
sys.exit(0 if all(results) else 1)

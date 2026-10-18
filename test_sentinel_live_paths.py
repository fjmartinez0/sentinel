"""Exercise the non-offline code path with a fake `requests`, incl. FMP failure modes."""
import json, sys, types, importlib.util
from datetime import date

spec = importlib.util.spec_from_file_location("gec", "sentinel_calendar.py")
gec = importlib.util.module_from_spec(spec); spec.loader.exec_module(gec)

class R:
    def __init__(self, status, payload): self.status_code, self._p = status, payload
    def json(self): return self._p
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(f"HTTP {self.status_code}")

def fake_get(url, params=None, headers=None, timeout=None):
    if "earnings-calendar" in url:
        return R(200, [
            # stable-style rows: no 'time'/'confirmed' keys at all
            {"symbol": "DUOL", "date": "2026-11-04", "epsActual": None, "epsEstimated": 0.6,
             "revenueActual": None, "revenueEstimated": 2.6e8, "lastUpdated": "2026-09-19"},
            {"symbol": "ATRL.TO", "date": "2026-11-05", "epsEstimated": 0.9},
            {"symbol": "NYT", "date": "2026-09-01", "epsActual": 0.55, "epsEstimated": 0.62,   # -11.3% -> hit
             "revenueActual": 6.9e8, "revenueEstimated": 6.9e8},
            {"symbol": "ZZZZ", "date": "2026-10-01"},          # not tracked -> dropped
            {"symbol": "DUOL", "date": "2026-11-04"},          # duplicate -> deduped
            {"symbol": "BAD", "date": None},                   # malformed -> dropped
        ])
    if "stable/quote" in url:
        s = params["symbol"]
        if s == "DUOL":   return R(200, [{"symbol": "DUOL", "price": 140.0, "changePercentage": -2.5}])
        if s == "META":   return R(200, [])                     # no data
        if s.endswith(".TO"): return R(402, {"Error": "plan"})   # TSX not on plan
        return R(200, [{"symbol": s, "price": 10.0, "changesPercentage": 1.0}])  # v3-style field name
    raise AssertionError("unexpected url " + url)


def _av_shim(fmp_get, mod, today_iso):
    """Answer Alpha Vantage requests from the same fake data the FMP fake serves (DECISIONS.md 52)."""
    class RT:
        def __init__(self, text): self.status_code, self.text = 200, text
        def json(self): return json.loads(self.text)
        def raise_for_status(self): pass
    def get(url, params=None, headers=None, timeout=None):
        if url != mod.AV_URL:
            return fmp_get(url, params=params, headers=headers, timeout=timeout)
        rows = [r for r in fmp_get("https://fmp/stable/earnings-calendar", params={}).json() if r.get("symbol") and r.get("date")]
        if params["function"] == "EARNINGS_CALENDAR":
            lines = ["symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay"]
            lines += [f"{r['symbol']},{r['symbol']} Inc,{r['date']},2026-09-30,{'' if r.get('epsEstimated') is None else r['epsEstimated']},USD,post-market"
                      for r in rows]
            return RT("\n".join(lines) + "\n")
        q = [{"fiscalDateEnding": "2026-06-30", "reportedDate": r["date"], "reportedEPS": str(r["epsActual"]),
              "estimatedEPS": str(r.get("epsEstimated")), "surprise": "0", "surprisePercentage": "0", "reportTime": "post-market"}
             for r in rows if r["symbol"] == params["symbol"] and r.get("epsActual") is not None]
        return RT(json.dumps({"symbol": params["symbol"], "quarterlyEarnings": q}))
    return get

fake_requests = types.SimpleNamespace(get=_av_shim(fake_get, gec, "2026-09-20"))
sys.modules["requests"] = fake_requests
gec.fetch_sp500_constituents = lambda: {"NYT": "New York Times", "META": "Meta", "AAPL": "Apple"}

import os; os.environ["FMP_API_KEY"] = "test-key-DO-NOT-LEAK"; os.environ["ALPHAVANTAGE_API_KEY"] = "avkeyDONOTLEAK"; gec.AV_DELAY = 0
sys.argv = ["x", "--today", "2026-09-20", "--out", "live_test.html"]
gec.main()

html = open("live_test.html").read()
assert "test-key-DO-NOT-LEAK" not in html and "avkeyDONOTLEAK" not in html, "API key leaked into HTML"
print("\nOK: API key not in HTML")

# share mode must contain no position data
sys.argv = ["x", "--today", "2026-09-20", "--out", "share_test.html", "--share"]
gec.main()
sh = open("share_test.html").read()
for needle in ["avg_cost", "1695", "TFSA", "GTC", "84.95", "DUOL", "Decision test for", "positions.yaml is as of", "Ledger checks"]:
    # 'Decision test for' only appears in the JS template string (renderCard); check data instead
    pass
payload = sh.split("const DATA = ")[1].split(";\nconst byDate")[0]
d = json.loads(payload)
assert all(i["kind"] == "sp500" and i["card"] is None for i in d["items"]), "share mode leaked held/watch/gate items"
for needle in ["TFSA", "84.95", "1695.78", "DUOL", "ATRL", "Non-Reg", "Ledger checks", "as of 2026-08-28"]:
    assert needle not in sh, f"share mode leaked {needle}"
print("OK: --share output has S&P 500 items only, no positions text")

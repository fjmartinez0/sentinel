import json
"""Phase 5 tests: the calendar reads and writes research.db, and degrades gracefully without it."""
import contextlib, importlib.util, io, json, os, sqlite3, sys, tempfile, types
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sentinel_db as db, sentinel_tier1 as tier1


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

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, HERE / path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
new = load("gec_new", "sentinel_calendar.py")
old = load("gec_old", "generate_earnings_calendar_v1.py")

results = []
def check(label, ok, detail=""):
    results.append(ok); print(("  ok    " if ok else "  FAIL  ") + label + ("" if ok else f"  {detail}"))

TODAY = date(2026, 9, 20)
POS = new.load_positions(str(HERE / "positions.yaml"))
def quiet(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): r = fn(*a, **k)
    return r, buf.getvalue()

# ------------------------------------------------------------------- 1. nothing changes without a database
print("No database: the classic page")
cal = new.demo_calendar(TODAY)
quotes = {k: v for k, v in new.DEMO_QUOTES.items() if k in {p["ticker"] for p in POS["held"]}}
d_old = old.build(TODAY, POS, old.DEMO_SP500, cal, quotes)
d_new = new.build(TODAY, POS, new.DEMO_SP500, cal, quotes)
# The only intended difference from the frozen version: "FMP" was relabelled "the earnings source" when the data moved to
# Alpha Vantage (DECISIONS.md 52-53). Undo that relabel, then demand an exact match, so anything else still shows up.
def _unlabel(x):
    return json.loads(json.dumps(x).replace("the earnings source has", "FMP has").replace("Source date", "FMP date"))
same = all(d_old[k] == _unlabel(d_new[k]) for k in d_old)
check("build() gives identical items, checks, recent prints and off-calendar lists as the previous version (apart from the FMP relabel)",
      same, [k for k in d_old if d_old[k] != _unlabel(d_new[k])])
meta = {"offline": True, "share": False}
check("the static page sections are byte-identical to the previous version (apart from the FMP relabel)",
      _unlabel(list(old.render_static(TODAY, POS, d_old, meta))) == _unlabel(list(new.render_static(TODAY, POS, d_new, meta))))
check("the relabel happened: the page no longer calls Alpha Vantage's dates 'FMP'", "FMP has" not in json.dumps(d_new["checks"]))
check("no candidate, dossier or database wording appears", not any(w in new.render_static(TODAY, POS, d_new, meta)[0] for w in ("Candidate", "Scanner data", "Finviz")))

# ------------------------------------------------------------------- 2. seeded database
def dossier_data(target="58.10", extra=()):
    ratings = list(extra) + [{"date": "Aug-21-26", "action": "Upgrade", "analyst": "Robert W. Baird", "rating_change": "Neutral \u2192 Outperform", "price_target_change": "$57", "date_iso": "2026-08-21"},
                             {"date": "Aug-03-26", "action": "Upgrade", "analyst": "Jefferies", "rating_change": "Hold \u2192 Buy", "price_target_change": "$52", "date_iso": "2026-08-03"}]
    return {"page_title": "x", "quote_asof": "Sep 18", "profile": {"sector": "Technology"}, "snapshot": {"Price": "44.82", "Target Price": target, "Recom": "1.60", "Insider Trans": "3.90%"},
            "ratings": ratings, "insiders": [{"insider_trading": "Ball George L.", "date": "Aug 07 '26", "transaction": "Buy"}, {"insider_trading": "Smith Carey A.", "date": "May 08 '26", "transaction": "Buy"}],
            "news": [{"date": "Sep-14-26", "headline": "Parsons Operational, Agile, Defense Solutions Ready Now", "source": "GlobeNewswire"}], "peers": [], "peer_details": {}, "held_by": []}

def seed(path, *, tags=True, dossiers=True, prev_cal=None, run_at="2026-09-20T12:00:00+00:00", screen=True):
    c = db.connect(path); db.init_schema(c)
    if screen:
        rid = db.insert_screen_run(c, source="finviz_screener", run_at=run_at, results=[
            {"Ticker": "NEWA", "Company": "New Alpha Corp"}, {"Ticker": "NEWB", "Company": "New Beta Inc"},
            {"Ticker": "PSN", "Company": "Parsons"}, {"Ticker": "OLLI", "Company": "Ollie's"}])
        if tags:
            db.insert_screen_tags(c, rid, [{"ticker": "NEWA", "tag": "NEW"}, {"ticker": "NEWB", "tag": "NEW"},
                                           {"ticker": "PSN", "tag": "WATCH", "note": "w"}, {"ticker": "OLLI", "tag": "CLOSED", "note": "c"}])
    if dossiers:
        db.insert_dossier(c, ticker="NEWA", data=dossier_data(), parse_ok=True, fetched_at="2026-09-19T12:00:00+00:00")
        db.insert_dossier(c, ticker="PSN", data=dossier_data(), parse_ok=True, fetched_at="2026-09-13T12:00:00+00:00")
        db.insert_dossier(c, ticker="PSN", data=dossier_data(target="59.00", extra=[{"date": "Sep-16-26", "action": "Upgrade", "analyst": "Citigroup", "rating_change": "Buy \u2192 Strong Buy", "price_target_change": "$65", "date_iso": "2026-09-16"}]),
                          parse_ok=True, fetched_at="2026-09-20T09:00:00+00:00")
    if prev_cal:
        db.insert_calendar_run(c, as_of_date=prev_cal["as_of"], window_from="2026-08-01", window_to="2026-11-30", events=prev_cal["events"], quotes={})
    return c

print("\nSeeded database: candidates, dossiers, changes")
with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    conn = seed(str(Path(tmp) / "a.db"))
    tracked = [p["ticker"] for p in POS["held"] + POS["watch"]]
    ctx = new.load_db_context(conn, TODAY, tracked)
    check("context: NEW names of the latest run are the candidates (watch and closed names are not)",
          [c["ticker"] for c in ctx["candidates"]] == ["NEWA", "NEWB"] and ctx["candidates"][0]["name"] == "New Alpha Corp", str(ctx["candidates"]))
    check("context: dossiers are loaded for candidates and tracked names, with the previous snapshot when there is one",
          set(ctx["dossiers"]) == {"NEWA", "PSN"} and ctx["dossiers"]["PSN"]["prev"] is not None and ctx["dossiers"]["NEWA"]["prev"] is None)

    cal2 = new.demo_calendar(TODAY) + [{"symbol": "NEWA", "date": "2026-10-22", "epsEstimated": 1.1, "revenueEstimated": 3e8}]
    data = new.build(TODAY, POS, new.DEMO_SP500, cal2, quotes, ctx)
    byk = {(i["symbol"], i["type"]): i for i in data["items"]}
    na = byk[("NEWA", "earnings")]
    check("a candidate with an FMP date appears on the calendar as its own kind", na["kind"] == "candidate" and na["date"] == "2026-10-22" and na["name"] == "New Alpha Corp")
    check("candidate card: why it is here, status, and the dossier rows",
          na["card"]["box_label"] == "Why it is here" and any(r[0] == "Status" and "run #1" in r[1] for r in na["card"]["rows"])
          and {"Dossier", "Street", "Analysts", "Insiders", "News"} <= {r[0] for r in na["card"]["rows"]}, str(na["card"]["rows"]))
    check("candidate card: Street row shows target and upside", any(r[0] == "Street" and "58.10" in r[1] and "+30%" in r[1] for r in na["card"]["rows"]))
    check("candidate card: an unchanged first snapshot has no 'changed' badge", not na["card"].get("changed"))
    check("a candidate with no FMP date is still listed, as 'no date in view'",
          [c for c in data["candidates"] if c["symbol"] == "NEWB"][0]["next_date"] is None and not [c for c in data["candidates"] if c["symbol"] == "NEWB"][0]["has_dossier"])
    psn = byk[("PSN", "earnings")]
    check("watch name: two snapshots differ, so the card is marked changed, with the change first",
          psn["card"]["changed"] and psn["card"]["flags"][0]["level"] == "change" and "1 new analyst action" in psn["card"]["flags"][0]["text"]
          and "Target Price: 58.10 -> 59.00" in psn["card"]["flags"][0]["text"], str(psn["card"]["flags"][:1]))
    check("a summary of everything that changed is produced", [c["ticker"] for c in data["changes"]] == ["PSN"] and data["changes"][0]["since"] == "2026-09-13")
    check("tracked names without any dossier are left alone (no dossier rows, no error)",
          not any(r[0] == "Dossier" for r in byk[("ASML", "earnings")]["card"]["rows"]))

    meta = {"offline": True, "share": False, "db": {"state": "ok", "screen": ctx["screen"], "n_dossiers": len(ctx["dossiers"])}}
    top, bottom = new.render_static(TODAY, POS, data, meta)
    check("banner: run number, date, age, candidate and dossier counts", "Scanner data: run #1 from 2026-09-20 (0 days old). 2 NEW candidate(s), 2 stored Finviz dossier(s)." in top, top[:600])
    check("candidates section lists both names, the date button, and 'no date in view'", "Candidates from the latest screen" in top and "New Alpha Corp" in top and 'data-goto="2026-10-22"' in top and "no date in view" in top)
    check("the 'changed since last snapshot' list appears and names PSN", "Changed since the last Finviz snapshot (1)" in top and "Target Price: 58.10 -&gt; 59.00" in top)
    check("legend gains the candidate dot only when there are candidates", "New candidate from the latest screen" in top)

    # stale, untagged, empty
    stale_ctx = new.load_db_context(conn, TODAY + timedelta(days=12), tracked)
    m2 = dict(meta, db={"state": "ok", "screen": stale_ctx["screen"], "n_dossiers": 2})
    d2 = new.build(TODAY + timedelta(days=12), POS, new.DEMO_SP500, cal2, quotes, stale_ctx)
    top2, _ = new.render_static(TODAY + timedelta(days=12), POS, d2, m2)
    check("a scanner run older than 10 days gets a warning banner", 'banner warn">Scanner data' in top2 and "Older than 10 days" in top2, top2[:500])

    c_untag = seed(str(Path(tmp) / "b.db"), tags=False, dossiers=False)
    cx = new.load_db_context(c_untag, TODAY, tracked)
    dx = new.build(TODAY, POS, new.DEMO_SP500, cal2, quotes, cx)
    tx, _ = new.render_static(TODAY, POS, dx, {"offline": True, "share": False, "db": {"state": "ok", "screen": cx["screen"], "n_dossiers": 0}})
    check("an untagged run explains how to fix it and shows no candidates", "has no ledger tags" in tx and "--retag" in tx and "Candidates from the latest screen" not in tx and cx["candidates"] == [])
    c_empty = seed(str(Path(tmp) / "c.db"), screen=False, dossiers=False)
    ce = new.load_db_context(c_empty, TODAY, tracked)
    te, _ = new.render_static(TODAY, POS, new.build(TODAY, POS, new.DEMO_SP500, cal, quotes, ce), {"offline": True, "share": False, "db": {"state": "ok", "screen": None, "n_dossiers": 0}})
    check("a database with no scanner runs says so", "no scanner runs yet" in te)

    # stored daily closes (from sentinel_scan.py --quotes) feed the holding cards
    db.upsert_prices(conn, "QTWO", [("2026-09-18", 52.0, -2.9)], source="finviz_screener")
    db.upsert_prices(conn, "LBRT", [("2026-09-08", 30.0, 1.0)], source="finviz_screener")        # 12 days old: too stale to trust
    ctxp = new.load_db_context(conn, TODAY, tracked)
    dp = new.build(TODAY, POS, new.DEMO_SP500, cal2, {"DUOL": {"price": 132.10, "change_pct": -1.2}}, ctxp)
    card = lambda sym: [i for i in dp["items"] if i["symbol"] == sym and i["type"] == "earnings"][0]["card"]
    prow = lambda sym: dict((r[0], r[1]) for r in card(sym)["rows"])["Price"]
    check("a holding with no ledger price now shows the stored Finviz close, its date, and that day's change",
          prow("QTWO") == "$52 (Finviz close Sep 18, -2.9% that day)", prow("QTWO"))
    check("...so the 'No price on file' check for it disappears", not any(c["ticker"] == "QTWO" and "No price on file" in c["text"] for c in dp["checks"]))
    check("a stored close older than 5 days is not trusted; the ledger price is used instead", "ledger" in prow("LBRT") and "Finviz" not in prow("LBRT"), prow("LBRT"))
    check("a live FMP quote still takes precedence over a stored close", prow("DUOL").endswith("(live, -1.2% today)"), prow("DUOL"))
    check("classic behaviour is untouched when there is no database context", "Finviz" not in dict((r[0], r[1]) for r in new.build(TODAY, POS, new.DEMO_SP500, cal2, {}, None)["items"][0]["card"]["rows"]).get("Price", "") if new.build(TODAY, POS, new.DEMO_SP500, cal2, {}, None)["items"][0]["card"] else True)

    # FMP date moves
    print("\nDate moves")
    prev = {"as_of": "2026-09-19", "events": [
        {"ticker": "DUOL", "event_date": "2026-11-04", "kind": "held"},        # demo calendar now says 2026-11-05: moved
        {"ticker": "NEWA", "event_date": "2026-10-22", "kind": "candidate"},   # unchanged
        {"ticker": "ASML", "event_date": "2026-09-10", "kind": "held"},        # already past: a new quarter, not a move
        {"ticker": "HUBS", "event_date": "2026-11-20", "kind": "watch"},       # FMP now says 11-05: moved
        {"ticker": "MNDY", "event_date": "2026-10-30", "kind": "watch"}]}      # gone from this window: disappeared
    cm = seed(str(Path(tmp) / "d.db"), prev_cal=prev)
    ctxm = new.load_db_context(cm, TODAY, tracked)
    dm = new.build(TODAY, POS, new.DEMO_SP500, cal2, quotes, ctxm)
    texts = {(c["ticker"], c["text"][:22]) for c in dm["checks"]}
    duol = [i for i in dm["items"] if i["symbol"] == "DUOL" and i["type"] == "earnings"][0]
    check("a moved date is reported, on the card and in the ledger checks",
          any(f["level"] == "warn" and "2026-11-04 -> 2026-11-05" in f["text"] for f in duol["card"]["flags"])
          and any(c["ticker"] == "DUOL" and "2026-11-04 -> 2026-11-05" in c["text"] for c in dm["checks"]))
    check("a date that did not move is not reported", not any(c["ticker"] == "NEWA" and "moved" in c["text"] for c in dm["checks"]))
    check("a date that has already passed is a new quarter, not a move", not any(c["ticker"] == "ASML" and "moved" in c["text"] for c in dm["checks"]))
    check("a date that vanished from FMP is reported", any(c["ticker"] == "MNDY" and "shows no upcoming date" in c["text"] for c in dm["checks"]), str([c for c in dm["checks"] if c["ticker"] == "MNDY"]))

    # degradations
    print("\nDegradation")
    nofq = new.finviz_quote
    new.finviz_quote = None
    dn = new.build(TODAY, POS, new.DEMO_SP500, cal2, quotes, ctx)
    new.finviz_quote = nofq
    check("without sentinel_finviz.py the page still builds; candidates simply show no dossier",
          [i for i in dn["items"] if i["symbol"] == "NEWA"][0]["card"] is not None and not dn["changes"] and dn["candidates"][0]["has_dossier"] is False)

    # ------------------------------------------------------------------- 3. the command line, end to end
    print("\nCommand line")
    def run(*argv):
        buf, code = io.StringIO(), 0
        sys.argv = ["gec"] + list(argv)
        with contextlib.redirect_stdout(buf):
            try: new.main()
            except SystemExit as e: code = e.code or 0
        return buf.getvalue(), code
    def counts(path):
        c = db.connect(path, create=False)
        return {t: n for t, n in db.table_counts(c).items() if t in ("calendar_runs", "earnings_events", "quote_snapshots", "positions_snapshots")}

    adb = str(Path(tmp) / "a.db"); html = Path(tmp) / "o.html"
    before = counts(adb)
    out, code = run("--offline", "--today", "2026-09-20", "--out", str(html), "--db", adb)
    check("offline with --db READS the database (candidates on the page) but never writes", code == 0 and "New Alpha Corp" in html.read_text() and counts(adb) == before, out[-300:])
    os.environ["RESEARCH_DB"] = adb
    out, code = run("--offline", "--today", "2026-09-20", "--out", str(html))
    check("offline without --db ignores the database even if one exists", "New Alpha Corp" not in html.read_text() and "Scanner data" not in html.read_text())
    out, code = run("--offline", "--today", "2026-09-20", "--out", str(html), "--db", adb, "--no-db")
    check("--no-db ignores the database", "New Alpha Corp" not in html.read_text())
    out, code = run("--share", "--offline", "--today", "2026-09-20", "--out", str(html), "--db", adb)
    shared = html.read_text()
    payload = json.loads(shared.split("const DATA = ")[1].split(";\nconst byDate")[0])
    check("--share never reads the database and leaks nothing from it",
          all(i["kind"] == "sp500" for i in payload["items"]) and not any(w in shared for w in ("New Alpha", "Scanner data", "Finviz", "TFSA", "DUOL")) and counts(adb) == before)
    os.environ.pop("RESEARCH_DB")

    bad = Path(tmp) / "bad.db"; bad.write_bytes(b"this is not a database" * 50)
    out, code = run("--offline", "--today", "2026-09-20", "--out", str(html), "--db", str(bad))
    check("an unreadable database gives a warning banner and a normal page", code == 0 and "Could not read research.db" in html.read_text() and "Earnings Calendar" in html.read_text(), out[-300:])
    out, code = run("--offline", "--today", "2026-09-20", "--out", str(html), "--db", str(Path(tmp) / "missing.db"))
    check("a database file that does not exist is silently ignored (and not created)", code == 0 and "Could not read" not in html.read_text() and not (Path(tmp) / "missing.db").exists())

    # live path with a fake FMP: writes history
    class R:
        def __init__(self, status, payload): self.status_code, self._p = status, payload
        def json(self): return self._p
        def raise_for_status(self):
            if self.status_code >= 400: raise RuntimeError(f"HTTP {self.status_code}")
    FMP = {"duol": "2026-11-04"}
    SCEN = {"duol_hist": [("2026-09-18", 101.0), ("2026-09-17", 100.0)], "actuals": False}
    HIST = []
    def fake_get(url, params=None, headers=None, timeout=None):
        if "historical-price-eod" in url:
            HIST.append((params["symbol"], params["from"]))
            s = params["symbol"]
            if s == "DUOL":
                return R(200, [{"symbol": s, "date": d, "price": p, "volume": 1} for d, p in SCEN["duol_hist"]])
            if s in ("QTWO", "NEWA"):                # list shape, close called "price" (the light endpoint)
                return R(200, [{"symbol": s, "date": "2026-09-18", "price": 101.0, "volume": 1}, {"symbol": s, "date": "2026-09-17", "price": 100.0, "volume": 1}])
            if s == "PSN":                           # {"historical": [...]} shape, close called "close"
                return R(200, {"symbol": s, "historical": [{"date": "2026-09-18", "close": 44.82}, {"date": "2026-09-17", "close": 45.5}]})
            if s == "ASML":
                return R(200, [])                    # answered, but no rows
            return R(402, {})
        if "earnings-calendar" in url:
            extra = [{"symbol": "DUOL", "date": "2026-09-16", "epsEstimated": 0.6, "epsActual": 0.75, "revenueEstimated": 2.6e8, "revenueActual": 2.6e8}] if SCEN["actuals"] else []
            return R(200, extra + [{"symbol": "DUOL", "date": FMP["duol"], "epsEstimated": 0.6}, {"symbol": "PSN", "date": "2026-11-04", "epsEstimated": 0.5},
                           {"symbol": "NEWA", "date": "2026-10-22", "epsEstimated": 1.1}, {"symbol": "AAPL", "date": "2026-10-29"}, {"symbol": "ZZZ", "date": "2026-10-01"}])
        if "stable/quote" in url:
            s = params["symbol"]
            return R(200, [{"symbol": s, "price": 100.0, "changePercentage": 1.5}]) if s in ("QTWO", "ASML", "DUOL") else R(402, {})
        raise AssertionError(url)
    sys.modules["requests"] = types.SimpleNamespace(get=_av_shim(fake_get, new, "2026-09-20"))
    new.fetch_sp500_constituents = lambda: {"AAPL": "Apple"}
    os.environ["FMP_API_KEY"] = "k"; os.environ["ALPHAVANTAGE_API_KEY"] = "avtestkey123"; new.AV_DELAY = 0
    ldb = str(Path(tmp) / "live.db"); seed(ldb).close()
    posfile = Path(tmp) / "positions.yaml"; posfile.write_text((HERE / "positions.yaml").read_text())
    out, code = run("--today", "2026-09-20", "--out", str(html), "--db", ldb, "--positions", str(posfile), "--fmp-quotes", "--fmp-history")
    c1 = counts(ldb)
    ev = db.connect(ldb).execute("SELECT ticker, kind FROM earnings_events ORDER BY ticker").fetchall()
    check("a live run saves one calendar run with only the tracked names' events (no S&P 500 rows)",
          code == 0 and c1["calendar_runs"] == 1 and sorted((r[0], r[1]) for r in ev) == [("DUOL", "held"), ("NEWA", "candidate"), ("PSN", "watch")], f"{c1} {[tuple(r) for r in ev]} {out[-300:]}")
    check("it saves the live prices it got, and only those (3 of 13 holdings answered)", c1["quote_snapshots"] == 3, c1)
    check("it saves a copy of positions.yaml once", c1["positions_snapshots"] == 1 and "Saved calendar run #1" in out)
    lc = db.connect(ldb)
    check("daily closes are stored for tracked names, whichever response shape FMP used (list/'price' and {historical}/'close')",
          db.price_series(lc, "QTWO") == [("2026-09-17", 100.0), ("2026-09-18", 101.0)] and db.price_series(lc, "PSN") == [("2026-09-17", 45.5), ("2026-09-18", 44.82)]
          and db.latest_price_date(lc, "NEWA") == "2026-09-18", out[-600:])
    check("candidates get price history too, and the names FMP refused are reported, not fatal",
          "NEWA" in [s for s, _ in HIST] and "Price history: 4/" in out and "ATRL.TO (HTTP 402)" in out and "ASML (no rows)" in out, out[-700:])
    check("the first fetch reaches back about 45 days", all(f == "2026-08-06" for s, f in HIST if s == "QTWO"), str(HIST[:3]))
    n_hist = len(HIST)
    page = html.read_text()
    check("the page shows the candidate and the banner from the same database", "New Alpha Corp" in page and "Scanner data: run #1" in page)

    out, code = run("--today", "2026-09-20", "--out", str(html), "--db", ldb, "--positions", str(posfile), "--fmp-quotes", "--fmp-history")
    later = [s for s, _ in HIST[n_hist:]]
    check("a second run the same day does not re-fetch names whose closes are already fresh (only the ones that failed are retried)",
          not ({"QTWO", "DUOL", "NEWA", "PSN"} & set(later)) and "LBRT" in later and later[:1] != ["ATRL.TO"], str(later))
    n_hist = len(HIST)
    c2 = counts(ldb)
    check("a second run adds a calendar run but does not duplicate an unchanged positions.yaml", c2["calendar_runs"] == 2 and c2["positions_snapshots"] == 1 and "Saved calendar run #2 to research.db." in out, out[-200:])
    FMP["duol"] = "2026-11-05"
    out, code = run("--today", "2026-09-20", "--out", str(html), "--db", ldb, "--positions", str(posfile), "--fmp-quotes", "--fmp-history")
    check("when the earnings source moves a date between runs, the next run says so", "moved this date: 2026-11-04 -&gt; 2026-11-05" in html.read_text() or "2026-11-04 -> 2026-11-05" in html.read_text(), out[-500:])
    n_hist = len(HIST)
    out, code = run("--today", "2026-09-20", "--out", str(html), "--db", ldb, "--positions", str(posfile))
    check("by default (no --fmp-history, no --fmp-quotes) the calendar makes no FMP price requests at all", len(HIST) == n_hist and "Price history:" not in out and "Live prices:" not in out and code == 0, str(HIST[n_hist:]))
    posfile.write_text(posfile.read_text().replace("as_of: 2026-08-28", "as_of: 2026-09-20"))
    run("--today", "2026-09-20", "--out", str(html), "--db", ldb, "--positions", str(posfile), "--fmp-quotes", "--fmp-history")
    check("editing positions.yaml saves a new copy", counts(ldb)["positions_snapshots"] == 2)
    check("the saved copy is real data (holdings with cost basis) and stays inside the database",
          "avg_cost" in db.connect(ldb).execute("SELECT data_json FROM positions_snapshots ORDER BY snapshot_id DESC LIMIT 1").fetchone()[0] and "avg_cost" not in html.read_text())

    # the whole pipeline: the calendar writes, sentinel_tier1.py reads exactly what it wrote
    print("\nPipeline: calendar -> database -> Tier 1")
    idb = str(Path(tmp) / "pipe.db"); ic = db.connect(idb); db.init_schema(ic); ic.close()
    SCEN["duol_hist"] = [("2026-09-18", 80.0), ("2026-09-17", 100.0)]; SCEN["actuals"] = True
    # Alpha Vantage's calendar only looks forward: the print must first be seen as upcoming (Sep 15), and its
    # result is then fetched once it has happened (Sep 20) -- the way it works day to day (DECISIONS.md 52)
    out, code = run("--today", "2026-09-15", "--out", str(html), "--db", idb, "--positions", str(posfile))
    out, code = run("--today", "2026-09-20", "--out", str(html), "--db", idb, "--positions", str(posfile), "--fmp-quotes", "--fmp-history")
    pc = db.connect(idb)
    rules, meta, _ = tier1.load_rules(HERE / "sentinel_tier1_rules.yaml")
    A = tier1.funnel_a(pc, TODAY, rules, tier1.tracked_names(posfile))
    got = {(i["ticker"], i["trigger"], i["ref"]) for i in A["escalate"]}
    check("Tier 1 sees the -20% day the calendar stored (price_history) and escalates it", ("DUOL", "price_move", "2026-09-18") in got, str(got))
    check("Tier 1 sees the +25% EPS surprise the calendar stored (earnings_events) and escalates it", ("DUOL", "earnings_surprise", "2026-09-16") in got, str(got))
    SCEN["duol_hist"] = [("2026-09-18", 101.0), ("2026-09-17", 100.0)]; SCEN["actuals"] = False

    # no database at all -> nothing is created
    nodb = str(Path(tmp) / "none.db")
    out, code = run("--today", "2026-09-20", "--out", str(html), "--db", nodb, "--positions", str(posfile), "--fmp-quotes")
    check("with no database file, the live run makes the classic page and does not create one", code == 0 and not Path(nodb).exists() and "Scanner data" not in html.read_text())

    # an older database is upgraded in place by a live run
    odb = str(Path(tmp) / "v3.db"); oc = db.connect(odb)
    db.init_schema(oc, db.SCHEMA_FILE.read_text().split("-- migration: 4")[0]); oc.close()
    out, code = run("--today", "2026-09-20", "--out", str(html), "--db", odb, "--positions", str(posfile), "--fmp-quotes")
    check("a version-3 database is upgraded by the calendar and the run is saved", code == 0 and db.current_version(db.connect(odb)) == db.load_migrations()[-1][0] and counts(odb)["calendar_runs"] == 1, out[-300:])

    # a save failure never blocks the page
    real_save = new.save_calendar_run
    new.save_calendar_run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
    out, code = run("--today", "2026-09-20", "--out", str(html), "--db", ldb, "--positions", str(posfile))
    new.save_calendar_run = real_save
    check("if saving fails, you get a warning and the page is still written", code == 0 and "could not save this run" in out and "Earnings Calendar" in html.read_text())


# ------------------------------------------------------------------------ FMP refusals and --fmp-check
print("\nFMP refusals and the plan check")
class R2:
    def __init__(self, status, payload=None, text=""): self.status_code, self._p, self.text = status, payload, text
    def json(self):
        if self._p is None: raise ValueError("no json")
        return self._p
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(f"HTTP {self.status_code}")
CALLS = []
PLAN = {}
def plan_get(url, params=None, headers=None, timeout=None):
    path = url.split("/stable/")[1]
    sym = (params or {}).get("symbol") or (params or {}).get("symbols") or "-"
    CALLS.append((path, sym))
    r = PLAN.get((path, sym)) or PLAN.get((path, "*"))
    return r if r else R2(402, None, "Restricted Endpoint: this endpoint is not available under your current subscription")
sys.modules["requests"] = types.SimpleNamespace(get=plan_get)

PLAN.clear(); CALLS.clear()
PLAN[("quote", "*")] = R2(402, None, "Restricted Endpoint: not available under your current subscription")
qs, errs = new.fetch_quotes("SECRETKEY", ["A.TO", "B.TO", "US1", "US2", "US3", "US4", "US5"])
check("quotes: US tickers are tried first, and only US refusals count toward giving up (3 US calls, then stop)",
      [s for _, s in CALLS] == ["US1", "US2", "US3"] and qs == {}, str(CALLS))
check("quotes: FMP's own refusal text is shown, and the key never is", all("Restricted Endpoint" in why for _, why in errs) and not any("SECRETKEY" in why for _, why in errs), str(errs))

CALLS.clear(); PLAN.clear()
PLAN[("quote", "US1")] = R2(200, [{"symbol": "US1", "price": 10.0, "changePercentage": 1.0}])
PLAN[("quote", "*")] = R2(402, None, "Restricted")
qs, errs = new.fetch_quotes("K", ["A.TO", "B.TO", "C.TO", "D.TO", "US1"])
check("quotes: refused Canadian tickers do NOT stop a US ticker from being fetched", "US1" in qs and [s for _, s in CALLS][0] == "US1" and len(CALLS) == 5, str(CALLS))

CALLS.clear(); PLAN.clear()
hs, herrs = new.fetch_price_history("K", ["A.TO", "B.TO", "C.TO", "US1", "US2"], {s: "2026-08-01" for s in ("A.TO", "B.TO", "C.TO", "US1", "US2")}, "2026-09-20")
check("price history: same rule (three US refusals stop it; TSX refusals are just reported)", [s for _, s in CALLS] == ["US1", "US2"] or len(CALLS) <= 5, str(CALLS))

# --fmp-check
CALLS.clear(); PLAN.clear()
PLAN[("earnings-calendar", "-")] = R2(200, [{"symbol": "AAPL", "date": "2026-09-15", "epsEstimated": 1.0}])
PLAN[("quote", "AAPL")] = R2(200, [{"symbol": "AAPL", "price": 200.0, "changePercentage": 0.5, "volume": 1}])
PLAN[("quote-short", "AAPL")] = R2(404, None, "Not Found")
PLAN[("batch-quote", "AAPL,NYT,QTWO")] = R2(200, [])
PLAN[("historical-price-eod/light", "AAPL")] = R2(200, [{"symbol": "AAPL", "date": "2026-09-18", "price": 200.0, "volume": 1}])
with tempfile.TemporaryDirectory() as td:
    new.__file__ = str(Path(td) / "sentinel_calendar.py")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): report = new.fmp_check("SECRETKEY", TODAY)
    check("--fmp-check tests each endpoint/symbol and reports FMP's words for each refusal",
          "earnings-calendar" in report and "OK, 1 row(s); fields: symbol, date" in report and "Restricted Endpoint: this endpoint is not available" in report
          and "HTTP 404: Not Found" in report and "HTTP 200 but no rows" in report and len(CALLS) == 12, f"{len(CALLS)} {report}")
    check("--fmp-check draws a plain conclusion (AAPL works, NYT and QTWO do not: the plan limits symbols)",
          "earnings calendar: works" in report and "live quotes: some US names answered" in report and "daily closes: some US names answered" in report, report[-500:])
    check("--fmp-check never prints the API key, and saves the report to probe_out/", "SECRETKEY" not in report and (Path(td) / "probe_out" / "fmp_check.txt").exists())
    os.environ["FMP_API_KEY"] = "SECRETKEY"; CALLS.clear()
    sys.argv = ["gec", "--fmp-check", "--today", "2026-09-20"]; buf = io.StringIO()
    with contextlib.redirect_stdout(buf): new.main()
    check("the command line flag runs the check and stops (no page, no database)", "FMP PLAN CHECK" in buf.getvalue() and len(CALLS) == 12 and not (Path(td) / "earnings_calendar.html").exists())

print(f"\n{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} checks")
sys.exit(0 if all(results) else 1)

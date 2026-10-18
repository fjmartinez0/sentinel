"""Tests for sentinel_scan.py --quotes: market state, ticker-list fetch, storage, and the hand-off to Tier 1."""
import contextlib, importlib.util, io, os, sys, tempfile, time
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sentinel_db as db, sentinel_tier1 as tier1
spec = importlib.util.spec_from_file_location("scan", HERE / "sentinel_scan.py")
scan = importlib.util.module_from_spec(spec); sys.modules["sentinel_scan"] = scan; spec.loader.exec_module(scan)
time.sleep = lambda s: None

results = []
def check(label, ok, detail=""):
    ok = bool(ok); results.append(ok); print(("  ok    " if ok else "  FAIL  ") + label + ("" if ok else f"  {detail}"))

def et(y, m, d, hh, mm):
    from zoneinfo import ZoneInfo
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/New_York"))

# ----------------------------------------------------------------------- market state
print("Market state (what a Finviz price means right now)")
S = lambda *a: scan.market_state(et(*a))
check("Sunday: Friday's close", S(2026, 9, 20, 12, 0) == ("weekend", "2026-09-18"))
check("Saturday: Friday's close", S(2026, 9, 19, 23, 0) == ("weekend", "2026-09-18"))
check("Monday before the open: the previous Friday's close", S(2026, 9, 21, 8, 0) == ("before the open", "2026-09-18"))
check("Wednesday before the open: Tuesday's close", S(2026, 9, 23, 9, 29) == ("before the open", "2026-09-22"))
check("during the session there is NO close to store", S(2026, 9, 23, 10, 0) == ("market open", None) and S(2026, 9, 23, 9, 30) == ("market open", None) and S(2026, 9, 23, 15, 59) == ("market open", None))
check("from 4:00 pm on, it is today's close", S(2026, 9, 23, 16, 0) == ("after the close", "2026-09-23") and S(2026, 9, 23, 21, 30) == ("after the close", "2026-09-23"))
check("_pct reads Finviz's cells", scan._pct("-2.92%") == -2.92 and scan._pct("15.00%") == 15.0 and scan._pct("1,234.5") == 1234.5 and scan._pct("-") is None and scan._pct(None) is None)

# ---------------------------------------------------------------- a fake Finviz screener
class Resp:
    def __init__(self, status, text=""): self.status_code, self.text = status, text
PRICES = {}       # ticker -> (price, change)
REQ = []
MODE = {"broken": False, "status": 200, "notable": False}
def screener_html(tickers):
    decoy = "<table><tr><th>Ticker</th><th>Menu</th></tr><tr><td>menu</td><td>x</td></tr></table>"     # has Ticker but no Price/Change
    head = "<tr>" + "".join(f"<th>{c}</th>" for c in ["No.", "Ticker", "Perf Week", "Perf Month", "Price", "Change %", "Volume"]) + "</tr>"
    rows = "".join(f"<tr><td>{i}</td><td>{t[0] + t}</td><td>1.00%</td><td>2.00%</td><td>{PRICES[t][0]}</td><td>{PRICES[t][1]:+.2f}%</td><td>1,000</td></tr>" for i, t in enumerate(tickers, 1) if t in PRICES)
    if MODE["notable"]:      # Oct 17, 2026: a page with no <table> at all (changed layout or a refusal page)
        return "<html><body><div class='blocked'>Please enable JavaScript</div></body></html>"
    if MODE["broken"]:
        return f"<html><body>{decoy}<table><tr><th>Ticker</th><th>Perf Week</th></tr><tr><td>xx</td><td>1%</td></tr></table></body></html>"
    return f"<html><body>{decoy}<table>{head}{rows}</table></body></html>"
def fake_get(url, timeout=None):
    REQ.append(url)
    if MODE["status"] != 200: return Resp(MODE["status"])
    tickers = url.split("&t=")[1].split("&")[0].split(",")
    return Resp(200, screener_html(tickers))
scan.SESSION.get = fake_get

def run(*argv):
    buf, code = io.StringIO(), 0
    sys.argv = ["scan"] + list(argv)
    with contextlib.redirect_stdout(buf):
        try: scan.main()
        except SystemExit as e: code = e.code if isinstance(e.code, int) else 1; buf.write(str(e.code) if isinstance(e.code, str) else "")
    return buf.getvalue(), code
def reset(): REQ.clear(); MODE.update(broken=False, status=200, notable=False)

with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp); scan.HERE = Path(tmp)           # so probe_out/ lands in the temp folder
    pos = Path(tmp) / "positions.yaml"
    pos.write_text("as_of: 2026-09-20\nheld:\n  - {ticker: NYT}\n  - {ticker: QTWO}\n  - {ticker: ATRL.TO}\nwatch:\n  - {ticker: PSN}\n  - {ticker: CJT.TO}\n")
    dbp = str(Path(tmp) / "q.db"); c = db.connect(dbp); db.init_schema(c)
    rid = db.insert_screen_run(c, source="finviz_screener", results=[{"Ticker": t, "Company": t} for t in ("NEWA", "PSN", "OLDK")], run_at="2026-09-20T12:00:00+00:00")
    db.insert_screen_tags(c, rid, [{"ticker": "NEWA", "tag": "NEW"}, {"ticker": "PSN", "tag": "WATCH"}, {"ticker": "OLDK", "tag": "CLOSED"}]); c.close()
    PRICES.update({"NYT": (84.0, -16.0), "QTWO": (50.0, 1.2), "PSN": (44.82, -2.92), "NEWA": (20.0, 6.5)})

    print("\nFetching")
    reset(); rows, problems = scan.fetch_quote_rows(["NYT", "QTWO", "PSN"])
    check("one page, the right view, and prices and day changes parsed (tickers fixed of the doubled-letter artifact)",
          len(REQ) == 1 and "v=141" in REQ[0] and "t=NYT,QTWO,PSN" in REQ[0] and rows["NYT"] == {"price": 84.0, "change_pct": -16.0} and rows["PSN"]["change_pct"] == -2.92 and problems == [], str((REQ, rows)))
    reset(); many = [f"T{i}" for i in range(21)]; PRICES.update({t: (10.0 + i, 0.5) for i, t in enumerate(many)})
    rows, problems = scan.fetch_quote_rows(many)
    check("21 tickers make 2 requests (20 + 1), and a page with a single row still works", len(REQ) == 2 and len(rows) == 21 and "T20" in rows, f"{len(REQ)} {len(rows)}")
    reset(); MODE["broken"] = True
    rows, problems = scan.fetch_quote_rows(["NYT"])
    check("a page without Price/Change columns is reported, saved for inspection, and stores nothing",
          rows == {} and "no table with Ticker, Price and Change" in problems[0] and (Path(tmp) / "probe_out" / "quotes_page1.html").exists(), str(problems))
    reset(); MODE["notable"] = True; (Path(tmp) / "probe_out" / "quotes_page1.html").unlink()
    rows, problems = scan.fetch_quote_rows(["NYT"])
    check("a page with NO table at all no longer crashes (the Oct 17 html5lib error): it is reported and saved",
          rows == {} and "no table with Ticker, Price and Change" in problems[0] and "Please enable JavaScript" in (Path(tmp) / "probe_out" / "quotes_page1.html").read_text(), str(problems))
    reset(); MODE["notable"] = True
    _fr = scan.fetch_with_retry
    scan.fetch_with_retry = lambda url, max_retries=2: Resp(200, "<html><body><div>no tables here</div></body></html>")
    _pd = scan.polite_delay; scan.polite_delay = lambda *a, **k: None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = scan.fetch_view("Overview", "https://finviz.com/screener.ashx?v=111&f=x")
    scan.fetch_with_retry, scan.polite_delay = _fr, _pd
    check("the Saturday screen: a page with no table returns nothing (no crash), says PROBLEM and saves the page",
          got is None and "PROBLEM: no stock table" in buf.getvalue() and (Path(tmp) / "probe_out" / "screen_Overview.html").exists(), buf.getvalue())
    reset(); MODE["status"] = 403
    rows, problems = scan.fetch_quote_rows(["NYT"])
    check("a refused request is a reported problem, not a crash", rows == {} and "no usable response" in problems[0])

    # ---- Finviz's redesigned page (Oct 17, 2026): a <div role="grid">, no <table> at all
    print("\nThe redesigned Finviz page (div grid)")
    def grid_html(rows_, cols=("No.", "Ticker", "Perf Week", "Price", "Change %", "Volume")):
        ids = {"No.": "row", "Ticker": "ticker", "Perf Week": "perf1w", "Price": "price", "Change %": "change", "Volume": "volume"}
        head = "".join(f'<div role="columnheader" aria-colindex="{i}" data-column-id="{ids[c]}"><span>{c}</span><svg></svg></div>' for i, c in enumerate(cols, 1))
        body = ""
        for n, (t, p, ch) in enumerate(rows_, 1):
            vals = {"No.": str(n), "Perf Week": "1.00%", "Price": f"{p}", "Change %": f"{ch:+.2f}%", "Volume": "1,000"}
            cells = "".join(
                (f'<div role="gridcell" aria-colindex="{i}" data-ticker="{t}"><a><span aria-hidden="true"><span>{t[0]}</span></span><span>{t}</span></a></div>'
                 if c == "Ticker" else f'<div role="gridcell" aria-colindex="{i}">{vals[c]}</div>') for i, c in enumerate(cols, 1))
            body += f'<div role="row" data-row-key="{t}"><div role="presentation">{cells}</div></div>'
        return (f'<html><body><div role="tablist"><div role="tab">Overview</div></div><div role="grid" aria-label="Screener results">'
                f'<div role="rowgroup"><div role="row">{head}</div>{body}</div></div></body></html>')
    g = scan.find_result_table(grid_html([("ALB", 95.48, -2.75), ("AAL", 12.5, 1.0), ("NYT", 84.0, -16.0)]), min_rows=1,
                               required=("Ticker", "Price", ("Change %", "Change")))
    check("the div grid is read into the same columns as the old table", g is not None and list(g.columns) == ["No.", "Ticker", "Perf Week", "Price", "Change %", "Volume"], None if g is None else list(g.columns))
    check("tickers come out clean from data-ticker (ALB, not AALB) and AAL is NOT shortened to AL", g is not None and list(g["Ticker"]) == ["ALB", "AAL", "NYT"], None if g is None else list(g["Ticker"]))
    check("cells keep the text Finviz shows, so the rest of the code is unchanged", g is not None and g.iloc[0]["Price"] == "95.48" and g.iloc[0]["Change %"] == "-2.75%")
    _get = scan.SESSION.get
    scan.SESSION.get = lambda url, timeout=None: Resp(200, grid_html([("NYT", 84.0, -16.0), ("PSN", 44.82, -2.92)]))
    reset(); rows, problems = scan.fetch_quote_rows(["NYT", "PSN"])
    check("the quotes step stores prices from the new page", rows == {"NYT": {"price": 84.0, "change_pct": -16.0}, "PSN": {"price": 44.82, "change_pct": -2.92}} and problems == [], str((rows, problems)))
    calls = []
    def same_page(url, timeout=None):
        calls.append(url); return Resp(200, grid_html([(f"T{i}", 10.0, 0.1) for i in range(20)]))
    scan.SESSION.get = same_page
    _pd = scan.polite_delay; scan.polite_delay = lambda *a, **k: None
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        got = scan.fetch_view("Overview", "https://finviz.com/screener?v=111&f=x")
    scan.polite_delay = _pd
    check("the screen: if Finviz ignores the page offset, it stops after one repeat instead of fetching the same page 10 times",
          got is not None and len(got) == 20 and len(calls) == 2 and "repeated rows" in buf.getvalue(), f"{len(calls)} calls")
    G1 = HERE / "QUOTES_grid_page1.html"
    if G1.exists():
        scan.SESSION.get = lambda url, timeout=None: Resp(200, G1.read_text(encoding="utf-8"))
        asked = "QTWO ASML LBRT NYT ALB HLNE UBER SPGI MELI META DUOL SQM CHWY ONON HUBS MNDY CRCL PSN RBA TXT".split()
        rows, problems = scan.fetch_quote_rows(asked)
        check("REAL Oct 17 PAGE: all 20 tickers read, no problems", len(rows) == 20 and problems == [], f"{len(rows)} {problems}")
        check("REAL Oct 17 PAGE: values match what Finviz showed (ALB 95.48 -2.75%, ASML 1641.52 -0.96%)",
              rows.get("ALB") == {"price": 95.48, "change_pct": -2.75} and rows.get("ASML") == {"price": 1641.52, "change_pct": -0.96}, str({k: rows.get(k) for k in ("ALB", "ASML")}))
    else:
        print("  (skipped: QUOTES_grid_page1.html not next to this test)")
    scan.SESSION.get = _get

    # ---- the REAL Finviz pages saved from the first live run (Sep 2026): headers are "Change %", tickers carry the doubled-letter artifact
    P1, P2 = HERE / "QUOTES_real_page1.html", HERE / "QUOTES_real_page2.html"
    if P1.exists() and P2.exists():
        pages = [P1.read_text(encoding="utf-8"), P2.read_text(encoding="utf-8")]
        asked = ("QTWO ASML LBRT NYT ALB HLNE UBER SPGI MELI META DUOL SQM CHWY ONON HUBS MNDY CRCL PSN RBA TXT "
                 "RDDT SOFI APP CRWV NBIS IREN GPOR AMRZ BMI COO EXE GEHC OLED OTF").split()
        seen = []
        def real_get(url, timeout=None):
            seen.append(url); return Resp(200, pages[len(seen) - 1])
        scan.SESSION.get = real_get
        rows, problems = scan.fetch_quote_rows(asked)
        check("REAL PAGES: all 34 tickers are read from the two live pages, with no problems (the bug was the header 'Change %')",
              len(rows) == 34 and problems == [] and [t for t in asked if t not in rows] == [], f"{len(rows)} {problems}")
        check("REAL PAGES: values match what Finviz showed (PSN 44.82 -2.92%, ALB 110.91 -3.60%, ASML 1679.92 +3.08%, IREN +7.36%)",
              rows["PSN"] == {"price": 44.82, "change_pct": -2.92} and rows["ALB"] == {"price": 110.91, "change_pct": -3.6}
              and rows["ASML"] == {"price": 1679.92, "change_pct": 3.08} and rows["IREN"]["change_pct"] == 7.36, str({k: rows[k] for k in ("PSN", "ALB", "ASML", "IREN")}))
        scan.SESSION.get = lambda url, timeout=None: Resp(200, pages[1])
        rows, problems = scan.fetch_quote_rows(["OTF"])          # page 2 holds 14 names; only one was asked for
        check("REAL PAGES: rows for tickers that were not asked for are ignored and reported, never stored",
              list(rows) == ["OTF"] and len(problems) == 1 and "ignored 13 row(s)" in problems[0], str((rows, problems)))
        scan.SESSION.get = fake_get
    else:
        print("  (skipped: QUOTES_real_page1.html / QUOTES_real_page2.html not next to this test)")
    legacy = "<html><body><table><tr><th>No.</th><th>Ticker</th><th>Price</th><th>Change</th></tr><tr><td>1</td><td>NNYT</td><td>70.21</td><td>0.39%</td></tr></table></body></html>"
    check("a Finviz page with NO table at all returns None (no html5lib crash, Oct 17, 2026)",
          scan.find_result_table("<html><body><div>Access denied</div></body></html>", min_rows=1, required=("Ticker", "Price")) is None)
    check("an empty page returns None too", scan.find_result_table("", min_rows=1) is None)
    check("a plain 'Change' header is still accepted, in case Finviz renames the column back", scan.find_result_table(legacy, min_rows=1, required=("Ticker", "Price", ("Change %", "Change"))) is not None)

    print("\nThe --quotes command")
    reset()
    out, code = run("--db", dbp, "--positions", str(pos), "--quotes", "--quotes-date", "2026-09-18")
    c = db.connect(dbp)
    check("it prices US held + watch names and the NEW candidates only (no Canadian, no closed/known names) in ONE request",
          len(REQ) == 1 and sorted(t for t in "NYT QTWO PSN NEWA ATRL.TO CJT.TO OLDK".split() if db.latest_price(c, t)) == ["NEWA", "NYT", "PSN", "QTWO"]
          and "t=NYT,QTWO,PSN,NEWA" in REQ[0], f"{REQ} {out[-300:]}")
    row = db.latest_price(c, "NYT")
    check("each price is stored as the close of the stamped date, with Finviz's own day change and its source",
          row == {"date": "2026-09-18", "close": 84.0, "change_pct": -16.0, "source": "finviz_screener"}, str(row))
    check("the summary states the stamp and the biggest movers", "stamped as the close of 2026-09-18" in out and "NYT -16.0%" in out and "NEWA +6.5%" in out and "Stored 4 of 4" in out, out)
    n_before = c.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    reset(); out, code = run("--db", dbp, "--positions", str(pos), "--quotes", "--quotes-date", "2026-09-18")
    check("running it again for the same day changes nothing (idempotent)", c.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == n_before)

    reset(); PRICES["QTWO"] = (50.0, 1.2); del PRICES["NEWA"]
    out, code = run("--db", dbp, "--positions", str(pos), "--quotes", "--quotes-date", "2026-09-19")
    check("a name Finviz did not return is listed, and the others are still stored", "Stored 3 of 4" in out and "Not returned by Finviz: NEWA" in out, out)
    PRICES["NEWA"] = (20.0, 6.5)

    reset(); out, code = run("--db", dbp, "--positions", str(pos), "--quotes", "--tickers", "NYT,ATRL.TO", "--quotes-date", "2026-09-21")
    check("--tickers overrides the list, and Canadian tickers are still skipped", "t=NYT " not in REQ[0] and REQ[0].split("&t=")[1].split("&")[0] == "NYT", str(REQ))

    reset(); scan._now_et = lambda: et(2026, 9, 23, 11, 15)
    n_before = c.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    out, code = run("--db", dbp, "--positions", str(pos), "--quotes")
    check("while the US market is open it refuses to store an intraday price, explains, and makes no request",
          REQ == [] and "market is open right now (11:15" in out and "Nothing was stored" in out and c.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == n_before, out)
    scan._now_et = lambda: et(2026, 9, 20, 15, 0)       # a Sunday afternoon
    reset(); out, code = run("--db", dbp, "--positions", str(pos), "--quotes")
    check("on a Sunday it stores Friday's close automatically", "stamped as the close of 2026-09-18 (weekend)" in out and len(REQ) == 1, out[:300])
    scan._now_et = lambda: None
    out, code = run("--db", dbp, "--positions", str(pos), "--quotes")
    check("if the time zone cannot be determined it asks for --quotes-date instead of guessing", code != 0 and "--quotes-date" in out, out)

    print("\nHand-off to Tier 1")
    rules, meta, _ = tier1.load_rules(HERE / "sentinel_tier1_rules.yaml")
    c2 = db.connect(dbp)
    A = tier1.funnel_a(c2, date(2026, 9, 20), rules, tier1.tracked_names(pos))
    hit = [i for i in A["escalate"] if i["ticker"] == "NYT"]
    check("Tier 1 escalates the -16% move that --quotes stored (one row, no history needed)", hit and hit[0]["ref"] == "2026-09-18" and "Finviz" in hit[0]["reasons"][0], str(A["escalate"]))
    check("Tier 1 reports which tracked names still have no price data (the Canadian ones)", {"ATRL.TO", "CJT.TO"} <= set(A["coverage"]["none"]), str(A["coverage"]))

print(f"\n{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} checks")
sys.exit(0 if all(results) else 1)

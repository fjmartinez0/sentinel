"""Phase 4 tests. The quote-page fixtures are built from the CONTENT of your PSN screenshots
(the 84-cell snapshot grid, the 10 analyst rows, the news headlines, the peers line) but the
MARKUP is my guess at Finviz's HTML. sentinel_probe.py is what checks the real markup."""
import contextlib, importlib.util, io, os, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sentinel_db as db, sentinel_finviz as fq
spec = importlib.util.spec_from_file_location("scan", HERE / "sentinel_scan.py")
scan = importlib.util.module_from_spec(spec); sys.modules["sentinel_scan"] = scan; spec.loader.exec_module(scan)
time.sleep = lambda s: None

SNAP = [
 [("Index","-"),("P/E","31.00"),("EPS (ttm)","1.45"),("Insider Own","1.56%"),("Shs Outstand","106.80M"),("Perf Week","-2.03%")],
 [("Market Cap","4.79B"),("Forward P/E","13.22"),("EPS next Y","3.39"),("Insider Trans","3.90%"),("Shs Float","105.14M"),("Perf Month","-4.88%")],
 [("Enterprise Value","6.28B"),("PEG","1.93"),("EPS next Q","0.78"),("Inst Own","103.69%"),("Short Float","5.19%"),("Perf Quarter","-14.15%")],
 [("Income","157.41M"),("P/S","0.76"),("EPS this Y","-23.18%"),("Inst Trans","4.95%"),("Short Ratio","3.90"),("Perf Half Y","-10.65%")],
 [("Sales","6.29B"),("P/B","1.82"),("EPS next Y","39.18%"),("ROA","2.66%"),("Short Interest","5.46M"),("Perf YTD","-27.48%")],
 [("Book/sh","24.64"),("P/C","17.99"),("EPS next 5Y","6.86%"),("ROE","6.12%"),("52W High","89.50 -49.92%"),("Perf Year","-44.96%")],
 [("Cash/sh","2.49"),("P/FCF","15.54"),("EPS past 3/5Y","37.17% 17.69%"),("ROIC","3.72%"),("52W Low","36.26 23.61%"),("Perf 3Y","-20.32%")],
 [("Dividend Est.","-"),("EV/EBITDA","13.36"),("Sales past 3/5Y","14.90% 10.18%"),("Gross Margin","21.96%"),("Volatility","3.04% 3.26%"),("Perf 5Y","29.99%")],
 [("Dividend TTM","-"),("EV/Sales","1.00"),("EPS Y/Y TTM","-60.23%"),("Oper. Margin","5.40%"),("ATR (14)","1.80"),("Perf 10Y","-")],
 [("Dividend Ex-Date","-"),("Quick Ratio","1.66"),("Sales Y/Y TTM","-5.84%"),("Profit Margin","2.50%"),("RSI (14)","39.49"),("Recom","1.60")],
 [("Dividend Gr. 3/5Y","--"),("Current Ratio","1.66"),("EPS Q/Q","-128.34%"),("SMA20","-5.01%"),("Beta","0.70"),("Target Price","58.10")],
 [("Payout","0.00%"),("Debt/Eq","0.63"),("Sales Q/Q","-0.53%"),("SMA50","-9.51%"),("Rel Volume","2.20"),("Prev Close","46.17")],
 [("Employees","21000"),("LT Debt/Eq","0.61"),("Earnings","Jul 29 BMO"),("SMA200","-21.65%"),("Avg Volume","1.40M"),("Price","44.82")],
 [("IPO","May 08, 2019"),("Option/Short","Yes / Yes"),("EPS/Sales Surpr.","-107.94% -1.94%"),("Trades",""),("Volume","3,078,285"),("Change %","-2.92%")],
]
RATINGS = [
 ("Aug-21-26","Upgrade","Robert W. Baird","Neutral \u2192 Outperform","$57"),
 ("Aug-03-26","Upgrade","Jefferies","Hold \u2192 Buy","$52"),
 ("May-26-26","Initiated","BNP Paribas Exane","Underperform",""),
 ("Apr-15-26","Downgrade","KeyBanc Capital Markets","Overweight \u2192 Sector Weight",""),
 ("Apr-13-26","Downgrade","Robert W. Baird","Outperform \u2192 Neutral","$60"),
 ("Dec-19-25","Initiated","Barclays","Overweight","$70"),
 ("Dec-12-25","Initiated","Citigroup","Buy","$86"),
 ("Dec-10-25","Upgrade","TD Cowen","Hold \u2192 Buy","$75"),
 ("Dec-05-25","Downgrade","Raymond James","Strong Buy \u2192 Mkt Perform",""),
 ("Nov-20-25","Upgrade","William Blair","Mkt Perform \u2192 Outperform",""),
]
NEWS = [
 ("Sep-14-26 06:30AM","Parsons Operational, Agile, Defense Solutions Ready Now on Department of War's Digital Marketplaces (GlobeNewswire)"),
 ("Sep-03-26 06:30AM","Parsons to Present at the 2026 Jefferies Global Industrials Conference (GlobeNewswire)"),
 ("Aug-26-26 10:09AM","Parsons shares rise after securing position on $14 billion missile intelligence contract (InvestorsHub)"),
 ("06:30AM","Parsons Awarded Position on $14B COMET Missile and Space Intelligence Effort (GlobeNewswire)"),
 ("Aug-25-26 06:15AM","SealingTech, a Parsons Corporation company, receives $750 Million Joint Cyber Hunt Kit (JCHK) Award (PR Newswire)"),
 ("Aug-21-26 09:38AM","Nvidia initiated, Teradyne downgraded: Wall Street's top analyst calls (The Fly) +6.49%"),
]
INSIDERS_HDR = ["Insider Trading","Relationship","Date","Transaction","Cost","#Shares","Value ($)","#Shares Total","SEC Form 4"]
INSIDERS = [
 ["Ball George E","Director","Aug 07 '26","Buy","47.97","20,000","959,400","61,000","Aug 10 06:31 PM"],
 ["Smith Carey A","CEO","May 12 '26","Buy","50.00","12,500","625,000","210,000","May 14 04:12 PM"],
]

def tbl(rows, hdr=None, th=True, cls=""):
    h = "" if hdr is None else "<tr>" + "".join(f"<{'th' if th else 'td'}>{c}</{'th' if th else 'td'}>" for c in hdr) + "</tr>"
    return f'<table class="{cls}">{h}' + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>"

PEER_NAMES = {"SAIC": "Science Applications International Corp", "CACI": "Caci International Inc", "LDOS": "Leidos Holdings Inc"}

def snapshot_block(price, target, layout, unknown_label=False):
    rows = [[(l, {"Price": price, "Target Price": target}.get(l, v)) for (l, v) in row] for row in SNAP]
    if unknown_label:
        rows[0][0] = ("Brand New Field", rows[0][0][1])
    if layout == "columns":     # today's real layout: six two-column tables, one per column of the grid
        return "<div class='wrapper'>" + "".join(
            "<table class='js-snapshot-table snapshot-table2'>" + "".join(f"<tr><td>{row[c][0]}</td><td><b>{row[c][1]}</b></td></tr>" for row in rows) + "</table>"
            for c in range(6)) + "</div>"
    body = []                   # older / alternative layout: one wide table, label|value x6, optionally with a leading icon cell
    for row in rows:
        cells = (["<img src='x.png'>"] if layout == "shifted" else []) + [x for (l, v) in row for x in (l, f"<b>{v}</b>")]
        body.append(cells)
    return tbl(body, cls="snapshot-table2")

def quote_html(price="44.82", target="58.10", extra_ratings=(), ratings=True, insiders=True, news=True, peers=True,
               unknown_label=False, layout="columns", ratings_th=True):
    rat = list(extra_ratings) + RATINGS
    decoy = tbl([["menu", "x", "y"]] * 4, hdr=["Date", "Action", "Menu"])          # partial header match, must be ignored
    parts = ["<html><head><title>PSN - Parsons Corp Stock Price and Quote</title><script>var Peers = 'X Y Z';</script></head><body>",
             "<div class='hdr'>Last Close 44.82 Sep 18 \u2022 3:59 PM ET</div>",
             "<div class='links'><a href='screener?v=111&f=sec_technology'>Technology</a> <a href='screener?v=111&f=ind_informationtechnologyservices'>Information Technology Services</a> "
             "<a href='screener?v=111&f=geo_usa'>USA</a> <a href='screener?v=111&f=exch_nyse'>NYSE</a></div>"]
    if peers:
        span = lambda t: f"<span data-boxover-ticker='{t}' data-boxover-company='{PEER_NAMES.get(t, t + ' Inc')}' data-boxover-industry='IT Services' data-boxover-country='USA' data-boxover-value='5.58B'><a>{t}</a></span> "
        parts.append("<table class='fullview-links'><tr><td><div><a>Peers</a>:<span> " + "".join(span(t) for t in "SAIC CACI LDOS EXLS ACN BAH AMTM KBR ICFI MMS".split()) +
                     "</span> | <a>Held by</a>:<span> " + "".join(span(t) for t in "IJH VTI VB SHLD VBR VXF IDEF MDY KOMP PPA".split()) + "</span> <a>Scroll to Statements</a></div></td></tr></table>")
    parts.append("<table class='layout'><tr><td>" + snapshot_block(price, target, layout, unknown_label) + "</td></tr>")
    if ratings:
        parts.append("<tr><td>" + tbl(rat, hdr=["Date", "Action", "Analyst", "Rating Change", "Price Target Change"], th=ratings_th) + "</td></tr>")
    if news:
        parts.append("<tr><td>" + tbl(NEWS) + "</td></tr>")
    if insiders:
        parts.append("<tr><td>" + tbl(INSIDERS, hdr=INSIDERS_HDR) + "</td></tr>")
    parts.append(f"<tr><td>{decoy}</td></tr></table></body></html>")
    return "".join(parts)

results = []
def check(label, ok, detail=""):
    results.append(ok); print(("  ok    " if ok else "  FAIL  ") + label + ("" if ok else f"  {detail}"))

# =============================================================== parser
print("Parser (sentinel_finviz.py)")
data, ok, notes = fq.parse_quote_page(quote_html())
snap = data["snapshot"]
check("full page parses OK with no notes", ok and notes == [], str(notes))
check("profile, last-close date and peer details are captured",
      data["profile"] == {"sector": "Technology", "industry": "Information Technology Services", "country": "USA", "exchange": "NYSE"}
      and data["quote_asof"] == "Sep 18" and data["peer_details"]["SAIC"]["company"] == "Science Applications International Corp"
      and set(data["peer_details"]) == set(data["peers"]), str(data["peer_details"].get("SAIC")))
dw, okw, nw = fq.parse_quote_page(quote_html(layout="wide"))
check("the older single wide-table layout still parses (positional method)", okw and len(dw["snapshot"]) == 84 and nw == [], str(nw))
check("all 84 snapshot cells are read from the six column tables", len(snap) == 84, len(snap))
check("raw values keep Finviz's own formatting", snap["Price"] == "44.82" and snap["52W High"] == "89.50 -49.92%" and snap["Volume"] == "3,078,285" and snap["Index"] == "-")
check("a duplicated label is kept, not overwritten", snap["EPS next Y"] == "3.39" and snap["EPS next Y (2)"] == "39.18%")
check("an empty value cell is kept as an empty string", snap["Trades"] == "")
r = data["ratings"]
check("analyst table: 10 rows with the right columns",
      len(r) == 10 and r[0]["analyst"] == "Robert W. Baird" and r[0]["rating_change"] == "Neutral \u2192 Outperform" and r[0]["price_target_change"] == "$57" and r[2]["price_target_change"] == "", str(r[0]))
check("analyst dates get an ISO twin", r[0]["date"] == "Aug-21-26" and r[0]["date_iso"] == "2026-08-21")
check("the decoy table with a partial header is ignored", all(x["analyst"] for x in r))
n = data["news"]
check("news: rows read, date carried down to time-only rows", len(n) == 6 and n[3]["date"] == "Aug-26-26" and n[3]["time"] == "06:30AM" and n[3]["date_iso"] == "2026-08-26", str(n[3]))
check("news: source split off, including one followed by a price-move chip", n[0]["source"] == "GlobeNewswire" and n[5]["source"] == "The Fly" and not n[5]["headline"].endswith(")"), str(n[5]))
check("news: a parenthesis inside the headline is left alone", "(JCHK)" in n[4]["headline"] and n[4]["source"] == "PR Newswire")
i = data["insiders"]
check("insider table: rows keyed by Finviz's column titles", len(i) == 2 and i[0]["insider_trading"] == "Ball George E" and i[0]["transaction"] == "Buy" and i[0]["value"] == "959,400", str(i[0]))
check("peers and held-by lines are read", data["peers"] == "SAIC CACI LDOS EXLS ACN BAH AMTM KBR ICFI MMS".split() and data["held_by"][:3] == ["IJH", "VTI", "VB"] and len(data["held_by"]) == 10, f"{data['peers']} {data['held_by']}")
check("page title is captured; script text is not mistaken for content", data["page_title"] == "PSN - Parsons Corp Stock Price and Quote")

d2, ok2, n2 = fq.parse_quote_page(quote_html(ratings_th=False))
check("analyst headers written as plain cells (not <th>) still work", ok2 and len(d2["ratings"]) == 10)
d3, ok3, n3 = fq.parse_quote_page(quote_html(unknown_label=True))
check("an unknown label is kept and reported, not fatal", ok3 and "Brand New Field" in d3["snapshot"] and any("not in the known list" in x for x in n3), str(n3))
d4, ok4, n4 = fq.parse_quote_page(quote_html(layout="shifted"))
check("if columns shift (an icon cell), it falls back to pairing by label and says so", ok4 and len(d4["snapshot"]) >= 80 and any("paired by label" in x for x in n4), f"{ok4} {len(d4['snapshot'])} {n4}")
d5, ok5, n5 = fq.parse_quote_page(quote_html(ratings=False, insiders=False, news=False, peers=False))
check("missing sections are reported but the core parse still passes", ok5 and sum("no " in x for x in n5) == 5, str(n5))

blocked = "<html><body><h1>Just a moment...</h1><p>Please verify you are human. captcha</p></body></html>"
d6, ok6, n6 = fq.parse_quote_page(blocked)
check("a verification page fails clearly and mentions blocking", not ok6 and any("block" in x for x in n6), str(n6))
check("empty and garbage input never raise", all(fq.parse_quote_page(x)[1] is False for x in ("", "not html at all <<<", "<table><tr><td>x</td></tr></table>")))
part = "<html><body>" + tbl([[l, v] for l, v in [(l, v) for row in SNAP[:2] for (l, v) in row]]) + "</body></html>"
check("a snapshot with too few fields fails", fq.parse_quote_page(part)[1] is False)

prev, curr = data, fq.parse_quote_page(quote_html(target="59.00", extra_ratings=[("Sep-16-26","Upgrade","Citigroup","Buy \u2192 Strong Buy","$65")]))[0]
ch = fq.describe_changes(prev, curr)
check("describe_changes: new analyst action and a moved target are reported",
      any("1 new analyst action" in c and "Citigroup" in c for c in ch) and "Target Price: 58.10 -> 59.00" in ch, str(ch))
check("describe_changes: identical snapshots report nothing", fq.describe_changes(prev, prev) == [])
check("num() reads Finviz's number formats",
      fq.num("58.10") == 58.1 and fq.num("-27.48%") == -27.48 and fq.num("4.79B") == 4.79e9 and fq.num("3,078,285") == 3078285 and fq.num("-") is None and fq.num("89.50 -49.92%") == 89.5)


REAL = HERE / "PSN_real.html"
if REAL.exists():
    print("\nReal page (PSN_real.html, saved from your probe run)")
    rd, rok, rn = fq.parse_quote_page(REAL.read_text(encoding="utf-8", errors="ignore"))
    rs = rd["snapshot"]
    check("REAL: parses OK with no notes", rok and rn == [], str(rn))
    check("REAL: 84 snapshot fields with your screenshot's values", len(rs) == 84 and rs["P/E"] == "31.00" and rs["Market Cap"] == "4.79B"
          and rs["Target Price"] == "58.10" and rs["Recom"] == "1.60" and rs["Price"] == "44.82" and rs["Insider Trans"] == "3.90%" and rs["EPS next Y (2)"] == "39.18%", str(len(rs)))
    check("REAL: 20 analyst rows, newest first", len(rd["ratings"]) == 20 and rd["ratings"][0]["analyst"] == "Robert W. Baird" and rd["ratings"][0]["date_iso"] == "2026-08-21"
          and rd["ratings"][1]["analyst"] == "Jefferies")
    check("REAL: 5 insider rows, all buys, matching your ledger (Ball $47.97, Smith $624,645)",
          len(rd["insiders"]) == 5 and all(i["transaction"] == "Buy" for i in rd["insiders"]) and rd["insiders"][0]["cost"] == "47.97"
          and any(i["insider_trading"].startswith("Smith Carey") and i["value"] == "624,645" for i in rd["insiders"]))
    check("REAL: 100 news rows, dated, sources split off", len(rd["news"]) == 100 and all(n["date_iso"] for n in rd["news"]) and rd["news"][0]["source"] == "GlobeNewswire")
    check("REAL: peers, held-by, profile, last-close", rd["peers"] == "SAIC CACI LDOS EXLS ACN BAH AMTM KBR ICFI MMS".split() and rd["held_by"][:2] == ["IJH", "VTI"]
          and rd["profile"]["industry"] == "Information Technology Services" and rd["quote_asof"] == "Sep 18" and len(rd["peer_details"]) == 10)

# ================================================================= scanner stage
print("\nDossier stage (sentinel_scan.py)")
import pandas as pd

class Resp:
    def __init__(self, status, text=""): self.status_code, self.text = status, text
REQUESTS, PAGES, STATUS = [], {}, {}
def fake_get(url, timeout=None):
    REQUESTS.append(url)
    sym = url.split("t=")[1].split("&")[0]
    if STATUS.get(sym, 200) != 200: return Resp(STATUS[sym])
    return Resp(200, PAGES.get(sym, PAGES.get("*", quote_html())))
scan.SESSION.get = fake_get

def run(*argv):
    buf, code = io.StringIO(), 0
    sys.argv = ["scan"] + list(argv)
    with contextlib.redirect_stdout(buf):
        try: scan.main()
        except SystemExit as e: code = e.code or 0
    return buf.getvalue(), code
def fetched():  return [u.split("t=")[1].split("&")[0] for u in REQUESTS]
def reset():    REQUESTS.clear(); PAGES.clear(); STATUS.clear()

with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp); scan.DEBUG_DIR = Path(tmp) / "dbg"
    dbf = str(Path(tmp) / "d.db")
    pd.DataFrame({"Ticker": ["NEWA", "NEWB", "NEWC", "PSN", "OLLI"], "Price": [1, 2, 3, 4, 5]}).to_csv("candidates_2026-09-19.csv", index=False)
    run("--db", dbf, "--import-csv", "candidates_2026-09-19.csv")
    conn = db.connect(dbf)

    reset(); out, code = run("--db", dbf, "--dossier-only")
    check("--dossier-only tags the run first when it has no tags yet", "no ledger tags yet; tagging it first" in out, out[:300])
    check("default targets are the NEW names only, in order", fetched() == ["NEWA", "NEWB", "NEWC"], fetched())
    check("the plan line states counts and the cap", "3 to fetch (cap 12)" in out and "0 already fresh" in out, out)
    s = db.latest_dossier(conn, "NEWA")
    check("each page is stored with reason, run and a page fingerprint", s and s["reason"] == "NEW" and s["run_id"] == 1 and s["parse_ok"] == 1 and len(s["html_sha1"]) == 40 and s["data"]["snapshot"]["Price"] == "44.82")
    check("the summary shows price, target, upside and the latest analyst action", "target 58.10 (+30%)" in out and "latest Aug-21-26 Upgrade Robert W. Baird" in out and "first snapshot for this name" in out, out[:900])

    reset(); out, code = run("--db", dbf, "--dossier-only")
    check("a second run within 7 days makes ZERO requests (cache)", fetched() == [] and "3 already fresh" in out and "nothing" not in out.lower().split("dossier stage:")[0], f"{fetched()} {out[:300]}")

    reset(); PAGES["*"] = quote_html(target="59.00", extra_ratings=[("Sep-16-26","Upgrade","Citigroup","Buy \u2192 Strong Buy","$65")])
    out, code = run("--db", dbf, "--dossier-only", "--force", "--tickers", "NEWA")
    check("--force re-fetches, and the diff against the earlier snapshot is printed",
          fetched() == ["NEWA"] and "1 new analyst action" in out and "Target Price: 58.10 -> 59.00" in out, out)

    reset(); out, code = run("--db", dbf, "--dossier-only", "--tracked", "--force")
    check("--tracked adds HELD/WATCH names (PSN is WATCH); closed names are not included", "PSN" in fetched() and "OLLI" not in fetched() and set(fetched()) == {"NEWA", "NEWB", "NEWC", "PSN"}, fetched())

    reset(); out, code = run("--db", dbf, "--dossier-only", "--force", "--cap", "2")
    check("the cap limits fetches and names what was left", len(fetched()) == 2 and "over the cap; not fetching this time: NEWC" in out, f"{fetched()} {out[:400]}")

    reset(); out, code = run("--db", dbf, "--dossier-only", "--tickers", "RY.TO,ZZZ")
    check("non-US tickers are skipped with a reason, US ones fetched", fetched() == ["ZZZ"] and "skipped (not US-listed: RY.TO)" in out, f"{fetched()} {out[:300]}")

    # --held / --watch read positions.yaml directly, whether or not the names hit the screen
    pos = Path(tmp) / "pos.yaml"
    pos.write_text("as_of: 2026-09-20\nheld:\n  - {ticker: QTWO}\n  - {ticker: ATRL.TO}\n  - {ticker: nyt}\nwatch:\n  - {ticker: PSN}\n  - {ticker: CJT.TO}\n  - {ticker: HUBS}\n")
    reset(); out, code = run("--db", dbf, "--dossier-only", "--force", "--held", "--positions", str(pos), "--cap", "30")
    check("--held adds the US names in positions.yaml after the NEW ones; Canadian names are skipped with a reason",
          fetched() == ["NEWA", "NEWB", "NEWC", "NYT", "QTWO"] and "skipped (not US-listed: ATRL.TO)" in out, f"{fetched()} {out[:300]}")
    reset(); out, code = run("--db", dbf, "--dossier-only", "--force", "--watch", "--positions", str(pos), "--cap", "30")
    check("--watch adds the watch names; PSN appears once even though it is also in the run", fetched() == ["NEWA", "NEWB", "NEWC", "HUBS", "PSN"], fetched())
    reset(); out, code = run("--db", str(Path(tmp) / "nornd.db"), "--dossier-only", "--held", "--watch", "--positions", str(pos), "--cap", "30")
    check("--held --watch work with no saved scanner run at all (run_id null, reasons HELD/WATCH)",
          fetched() == ["NYT", "QTWO", "HUBS", "PSN"] and db.latest_dossier(db.connect(str(Path(tmp) / "nornd.db")), "NYT")["reason"] == "HELD", f"{fetched()} {out[-300:]}")

    # early stop + debug pages + failed parses are not cached
    reset(); PAGES["*"] = blocked
    out, code = run("--db", dbf, "--dossier-only", "--tickers", "F1,F2,F3,F4")
    check("two unreadable pages in a row stop the stage early", fetched() == ["F1", "F2"] and "Stopping early" in out, f"{fetched()} {out[-400:]}")
    check("unreadable pages are saved for inspection and stored as parse_ok=0", (scan.DEBUG_DIR / f"F1_{__import__('datetime').date.today().isoformat()}.html").exists() and db.latest_dossier(conn, "F1", ok_only=False)["parse_ok"] == 0)
    reset(); PAGES["*"] = quote_html()
    run("--db", dbf, "--dossier-only", "--tickers", "F1")
    check("a failed parse is never treated as cached: it is fetched again", fetched() == ["F1"])

    reset(); STATUS["H1"] = 403; STATUS["H2"] = 403
    out, code = run("--db", dbf, "--dossier-only", "--tickers", "H1,H2,H3")
    check("HTTP failures count toward the early stop too", "H3" not in fetched() and "Stopping early" in out, f"{fetched()} {out[-300:]}")

    # manual tickers with no saved run at all
    fresh = str(Path(tmp) / "fresh.db")
    reset(); out, code = run("--db", fresh, "--dossier-only", "--tickers", "PSN")
    s = db.latest_dossier(db.connect(fresh), "PSN")
    check("--tickers works on an empty database (run_id null, reason MANUAL)", s and s["run_id"] is None and s["reason"] == "MANUAL", out[-300:])
    out, code = run("--db", str(Path(tmp) / "none.db"), "--dossier-only")
    check("with no runs and no --tickers it explains what to do", code != 0)
    out, code = run("--db", dbf, "--dossier-only", "PSN")
    check("passing a ticker where a run number goes gives a clear message", code != 0)

    # symbol spelling: the Finviz page is requested under Finviz's own spelling
    db.SYMBOL_OVERRIDES["BRK-B"] = "BRK.B"
    pd.DataFrame({"Ticker": ["BRK-B"]}).to_csv("candidates_2026-09-18.csv", index=False)
    sdb = str(Path(tmp) / "sym.db"); run("--db", sdb, "--import-csv", "candidates_2026-09-18.csv")
    reset(); out, code = run("--db", sdb, "--dossier-only")
    check("Finviz is asked for its own spelling (BRK-B); the row is stored under the canonical key (BRK.B)",
          fetched() == ["BRK-B"] and db.latest_dossier(db.connect(sdb), "BRK.B") is not None, fetched())
    db.SYMBOL_OVERRIDES.clear()

    # end to end: a live scan followed by --dossier, using a fake screener AND fake quote pages
    def scr_page(cols, rows):
        return "<html><body><table><tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>" + \
               "".join("<tr>" + "".join(f"<td>{x}</td>" for x in r) + "</tr>" for r in rows) + "</table></body></html>"
    LIVE = ["NEWX", "PSN"]
    def screener(url):
        v = url.split("v=")[1].split("&")[0]
        cols = {"111": ["No.", "Ticker", "Company", "Price"], "121": ["No.", "Ticker", "P/E"], "161": ["No.", "Ticker", "Debt/Eq"],
                "131": ["No.", "Ticker", "Insider Trans"], "141": ["No.", "Ticker", "Perf YTD"]}[v]
        return Resp(200, scr_page(cols, [[i + 1, t[0] + t] + [1.5] * (len(cols) - 2) for i, t in enumerate(LIVE)]))
    scan.SESSION.get = lambda url, timeout=None: fake_get(url) if "quote.ashx" in url else screener(url)

    e2e = str(Path(tmp) / "e2e.db"); reset()
    out, code = run("--db", e2e, "--dossier")
    conn2 = db.connect(e2e)
    check("live scan + --dossier: run saved, tagged, and only the NEW name gets a detail page",
          code == 0 and db.latest_run_id(conn2) == 1 and fetched() == ["NEWX"] and db.latest_dossier(conn2, "NEWX")["run_id"] == 1, f"{fetched()} {out[-500:]}")
    check("without --dossier the scan never touches a quote page", (reset() or True) and "quote.ashx" not in " ".join(REQUESTS) and
          (run("--db", e2e) and not [u for u in REQUESTS if "quote.ashx" in u]))

    real_parse = fq.parse_quote_page
    fq.parse_quote_page = lambda html: (_ for _ in ()).throw(RuntimeError("boom"))
    out, code = run("--db", e2e, "--dossier", "--force")
    fq.parse_quote_page = real_parse
    check("if the dossier stage blows up, the scan, run and tags are still saved", code == 0 and "the dossier stage failed" in out and db.latest_run_id(conn2) == 3, out[-400:])

    # ---------------------------------------------------------------- the probe
    print("\nProbe (sentinel_probe.py)")
    import sentinel_probe as probe
    probe.OUT_DIR = Path(tmp) / "probe_out"
    reset(); PAGES["*"] = quote_html()
    sys.argv = ["probe", "PSN"]; buf = io.StringIO()
    with contextlib.redirect_stdout(buf): probe.main()
    rep = buf.getvalue()
    check("probe makes exactly one request and saves the page and the report",
          fetched() == ["PSN"] and (probe.OUT_DIR / "PSN.html").exists() and (probe.OUT_DIR / "PSN_report.txt").exists(), fetched())
    check("probe report says PARSE OK and shows the sections", "PARSE OK" in rep and "SNAPSHOT     : 84 fields" in rep and "ANALYST TABLE: 10 rows" in rep and "PEERS        : SAIC CACI" in rep, rep[-900:])
    sys.argv = ["probe", "--from-file", str(probe.OUT_DIR / "PSN.html")]; buf = io.StringIO(); n_before = len(REQUESTS)
    with contextlib.redirect_stdout(buf): probe.main()
    check("--from-file re-checks a saved page with no request", len(REQUESTS) == n_before and "PARSE OK" in buf.getvalue())
    reset(); PAGES["*"] = blocked
    sys.argv = ["probe", "ZZZ"]; buf = io.StringIO()
    with contextlib.redirect_stdout(buf): probe.main()
    check("probe on a blocked page says PARSE FAILED and MISSING, so you know what to send me", "PARSE FAILED" in buf.getvalue() and "MISSING" in buf.getvalue() and "block" in buf.getvalue().lower(), buf.getvalue()[-500:])

print(f"\n{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} checks")
sys.exit(0 if all(results) else 1)

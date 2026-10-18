"""Exercise the scanner end to end with a fake Finviz (I can't reach the real one from here)."""
import contextlib, importlib.util, io, json, os, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("scan", HERE / "sentinel_scan.py")
scan = importlib.util.module_from_spec(spec); spec.loader.exec_module(scan)
import sentinel_db as db

time.sleep = lambda s: None            # no waiting in tests

# ---- fake Finviz pages: decoy tables + the real grid with the doubled-first-letter artifact
def page(cols, rows):
    junk = "x" * 150
    decoy_wide = "<table><tr>" + "".join(f"<th>{'Ticker' if i == 0 else 'c'+str(i)}</th>" for i in range(30)) + \
                 "</tr><tr>" + "".join(f"<td>{junk}</td>" for _ in range(30)) + "</tr></table>"
    decoy_tiny = "<table><tr><th>Ticker</th><th>Menu</th></tr><tr><td>menu</td><td>x</td></tr></table>"
    head = "<tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>"
    body = "".join("<tr>" + "".join(f"<td>{v}</td>" for v in r) + "</tr>" for r in rows)
    return f"<html><body>{decoy_wide}{decoy_tiny}<table>{head}{body}</table></body></html>"

DATA = {  # ticker -> per-view values
    "PSN": dict(Company="Parsons", Mcap="4.79B", PE=31.0, Fwd=13.22, DE=0.63, Own=3.90, Ytd="-27.48%", Price=44.82),
    "RBA": dict(Company="RB Global", Mcap="16.1B", PE=38.2, Fwd=24.1, DE=0.55, Own=1.10, Ytd="-21.00%", Price=87.02),
    "TXT": dict(Company="Textron", Mcap="13.0B", PE=17.5, Fwd=14.0, DE=0.40, Own=0.80, Ytd="-22.10%", Price=70.10),
    "ONON": dict(Company="On Holding", Mcap="9.9B", PE=45.0, Fwd=30.0, DE=0.20, Own=2.00, Ytd="-35.41%", Price=30.02),
}
CURRENT = ["PSN", "RBA", "TXT"]
BLOCKED_VIEWS = set()

def html_for(v):
    tick = lambda t: t[0] + t     # simulate the parsing artifact: PSN -> PPSN
    if v == "111":
        return page(["No.", "Ticker", "Company", "Market Cap", "P/E", "Price"],
                    [[i + 1, tick(t), DATA[t]["Company"], DATA[t]["Mcap"], DATA[t]["PE"], DATA[t]["Price"]] for i, t in enumerate(CURRENT)])
    if v == "121":
        return page(["No.", "Ticker", "Market Cap", "P/E", "Fwd P/E", "Price"],
                    [[i + 1, tick(t), DATA[t]["Mcap"], DATA[t]["PE"], DATA[t]["Fwd"], DATA[t]["Price"]] for i, t in enumerate(CURRENT)])
    if v == "161":
        return page(["No.", "Ticker", "Debt/Eq", "Market Cap"], [[i + 1, tick(t), DATA[t]["DE"], DATA[t]["Mcap"]] for i, t in enumerate(CURRENT)])
    if v == "131":
        return page(["No.", "Ticker", "Insider Trans", "Market Cap"], [[i + 1, tick(t), DATA[t]["Own"], DATA[t]["Mcap"]] for i, t in enumerate(CURRENT)])
    if v == "141":
        return page(["No.", "Ticker", "Perf YTD", "Price"], [[i + 1, tick(t), DATA[t]["Ytd"], DATA[t]["Price"]] for i, t in enumerate(CURRENT)])

class Resp:
    def __init__(self, status, text=""): self.status_code, self.text = status, text

def fake_get(url, timeout=None):
    v = url.split("v=")[1].split("&")[0]
    if v in BLOCKED_VIEWS:
        return Resp(403)
    return Resp(200, html_for(v))
scan.SESSION.get = fake_get

def run(*argv):
    """Run scanner main() with argv; return (stdout, exit) without letting SystemExit escape."""
    buf, code = io.StringIO(), 0
    sys.argv = ["scan"] + list(argv)
    with contextlib.redirect_stdout(buf):
        try: scan.main()
        except SystemExit as e: code = e.code or 0
    return buf.getvalue(), code

results = []
def check(label, ok, detail=""):
    results.append(ok); print(("  ok    " if ok else "  FAIL  ") + label + ("" if ok else f"  {detail}"))

with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    dbfile = str(Path(tmp) / "t.db")

    # 1. first live run
    out, code = run("--db", dbfile)
    csvs = sorted(Path(tmp).glob("candidates_*.csv"))
    check("live run exits cleanly and writes the CSV", code == 0 and len(csvs) == 1)
    import pandas as pd
    df = pd.read_csv(csvs[0])
    check("doubled-letter tickers are fixed in the CSV", list(df["Ticker"]) == ["PSN", "RBA", "TXT"], list(df["Ticker"]))
    check("all five views merged into one row per ticker", {"Fwd P/E", "Debt/Eq", "Insider Trans", "Perf YTD"} <= set(df.columns), list(df.columns))
    check("first run says there is nothing to compare with", "No earlier run to compare with yet." in out, out[-400:])

    conn = db.connect(dbfile)
    run1 = db.get_run(conn)
    check("run #1 is in the database with all 5 views and the filter string",
          run1["n_hits"] == 3 and run1["views_ok"] == list(scan.VIEWS) and "cap_midover" in run1["filters"], str(run1)[:200])
    check("stored row keeps every merged column", "Fwd P/E" in run1["results"][0]["row"] and "Perf YTD" in run1["results"][0]["row"])
    check("no partial-run note on a complete run", run1["notes"] is None)

    # 2. second live run with a changed universe -> diff
    CURRENT[:] = ["PSN", "RBA", "ONON"]
    out, code = run("--db", dbfile)
    check("second run adds a second run", db.get_run(conn)["run_id"] == 2)
    check("diff reports the new and dropped tickers",
          "new     (1): ONON" in out and "dropped (1): TXT" in out and "same    (2)" in out, out)

    # 3. partial run: Financial view blocked
    CURRENT[:] = ["PSN", "RBA", "TXT"]; BLOCKED_VIEWS.add("161")
    out, code = run("--db", dbfile)
    r3 = db.get_run(conn)
    check("a blocked view is recorded as a partial run", "Financial" not in r3["views_ok"] and "PARTIAL" in (r3["notes"] or ""), str(r3["views_ok"]))
    check("scanner still completes when a view is blocked", code == 0 and "Financial view failed" in out)
    BLOCKED_VIEWS.clear()

    # 4. --no-db leaves the database alone
    n_before = len(db.list_runs(conn, 99))
    run("--db", dbfile, "--no-db")
    check("--no-db writes no run", len(db.list_runs(conn, 99)) == n_before)

    # 5. database failure never blocks the CSV
    for f in Path(tmp).glob("candidates_*.csv"): f.unlink()
    out, code = run("--db", "/dev/null/cannot/x.db")
    check("a broken database path is a warning, not a failure",
          code == 0 and "WARNING: could not save to the database" in out and any(Path(tmp).glob("candidates_*.csv")), out[-300:])

    # 6. CSV import: history + idempotence + ordering
    d2 = Path(tmp) / "hist"; d2.mkdir()
    pd.DataFrame({"Ticker": ["APTV", "RBA", "TXT"], "Price": [80.5, 90.1, 71.0], "P/E": [10.2, None, 17.0]}).to_csv(d2 / "candidates_2026-08-22.csv", index=False)
    pd.DataFrame({"Ticker": ["RBA", "TXT", "PSN"], "Price": [88.0, 70.5, 45.1]}).to_csv(d2 / "candidates_2026-09-06.csv", index=False)
    (d2 / "notes.csv").write_text("a,b\n1,2\n")
    db2 = str(Path(tmp) / "hist.db")
    out, code = run("--db", db2, "--import-csv", str(d2 / "candidates_*.csv"), str(d2 / "notes.csv"))
    c2 = db.connect(db2)
    runs = db.list_runs(c2, 99)
    check("import loads both CSVs as dated past runs in chronological order",
          [r["run_at"][:10] for r in reversed(runs)] == ["2026-08-22", "2026-09-06"], str(runs))
    check("a CSV without a Ticker column is skipped with a reason", "skip notes.csv" in out and "Ticker" in out, out)
    check("imported rows keep values and turn blanks into null",
          db.get_run(c2, 1)["results"][0]["row"]["Price"] == 80.5 and any(r["row"]["P/E"] is None for r in db.get_run(c2, 1)["results"]))
    out, code = run("--db", db2, "--import-csv", str(d2 / "candidates_*.csv"))
    check("re-importing is idempotent", len(db.list_runs(c2, 99)) == 2 and "already in the database" in out)

    # 7. first live run after imports: compared, with the weaker-comparison caveat
    CURRENT[:] = ["PSN", "RBA", "TXT"]
    out, code = run("--db", db2)
    check("first live run after imports is compared against the latest imported CSV, flagged as weaker",
          "imported CSV, filters unknown" in out and "Compared with run #2 (2026-09-06)" in out, out)
    out, code = run("--db", db2)
    check("second live run is compared like-for-like (no caveat)", "imported CSV, filters unknown" not in out and "Compared with run #3" in out, out)

    # 8. ticker sanity warning
    CURRENT[:] = ["PSN", "RBA"]; DATA["PSN"]["Company"] = "Parsons"
    orig_find = scan.find_result_table
    def weird(html):
        t = orig_find(html)
        if t is not None and "Ticker" in t.columns:
            t = t.copy(); t.loc[0, "Ticker"] = "P$N"
        return t
    scan.find_result_table = weird
    out, code = run("--db", db2, "--no-db")
    scan.find_result_table = orig_find
    check("odd-looking tickers trigger a warning but are still saved", "don't look like real symbols" in out and "P$N" in out, out[-300:])


    # ------------------------------------------------------------- Phase 3: ledger tagging
    import shutil, yaml as _y
    tdb = str(Path(tmp) / "tags.db")
    ledger = HERE  # the real positions.yaml + research_log.yaml sit beside the scanner

    # 9. live run tags the hits and stores the tags
    CURRENT[:] = ["PSN", "RBA", "TXT"]
    out, code = run("--db", tdb)
    c9 = db.connect(tdb)
    tg = db.get_tags(c9, db.latest_run_id(c9))
    check("live run stores a tag for every hit", set(tg) == {"PSN", "RBA", "TXT"} and all(t["tag"] == "WATCH" for t in tg.values()), str(tg)[:200])
    check("tag output names the ledger's own status text", "Ledger tags for run #1" in out and "PSN" in out and "WATCH" in out, out[-500:])
    check("tags record which YAML versions produced them", tg["PSN"]["positions_as_of"] == "2026-08-28" and tg["PSN"]["log_as_of"] == "2026-09-20", str(tg["PSN"]))

    # 10. the Aug 22 replay + one of each status, via CSV import and --retag (no scraping)
    d3 = Path(tmp) / "replay"; d3.mkdir()
    hits = ["APTV", "RBA", "TXT", "ONON", "PSN", "LBRT", "BSX", "OLLI", "ICE", "IONQ", "GNRC", "HD", "SE", "XYZQ"]
    pd.DataFrame({"Ticker": hits, "Price": range(len(hits))}).to_csv(d3 / "candidates_2026-08-22.csv", index=False)
    rdb = str(Path(tmp) / "replay.db")
    run("--db", rdb, "--import-csv", str(d3 / "candidates_2026-08-22.csv"))
    check("imported runs are NOT tagged automatically (their tags would be anachronistic)", db.get_tags(db.connect(rdb), 1) == {})
    out, code = run("--db", rdb, "--retag", "1")
    tg = db.get_tags(db.connect(rdb), 1)
    want = {"APTV": "PASSED", "RBA": "WATCH", "TXT": "WATCH", "ONON": "WATCH", "PSN": "WATCH", "LBRT": "HELD", "BSX": "DISQUALIFIED",
            "OLLI": "CLOSED", "ICE": "PASSED", "IONQ": "PARKED", "GNRC": "PASSED", "HD": "REMOVED", "SE": "DORMANT", "XYZQ": "NEW"}
    got = {k: v["tag"] for k, v in tg.items()}
    check("every status maps to the right tag (Aug 22 replay + one of each)", got == want, f"diff: { {k:(got.get(k), want[k]) for k in want if got.get(k)!=want[k]} }")
    check("NEW names are listed first and by themselves", "NEW, not in your ledger (1): XYZQ" in out, out)
    check("revisit conditions are shown for names that have one", "revisit if:" in out and "Price about $280" in out, out)
    check("--retag does not touch Finviz or add a run", len(db.list_runs(db.connect(rdb), 99)) == 1)

    # 11. editing the log and re-tagging changes the tags for that run only
    log2 = Path(tmp) / "log2.yaml"; shutil.copy(ledger / "research_log.yaml", log2)
    doc = _y.safe_load(log2.read_text()); [e.update(status="parked") for e in doc["entries"] if e["ticker"] == "BSX"]
    log2.write_text(_y.safe_dump(doc, sort_keys=False, allow_unicode=True))
    run("--db", rdb, "--retag", "1", "--log", str(log2))
    check("re-tagging after a YAML edit updates the tag", db.get_tags(db.connect(rdb), 1)["BSX"]["tag"] == "PARKED")

    # 12. precedence: positions.yaml beats research_log.yaml, and the conflict is reported
    doc = _y.safe_load((ledger / "research_log.yaml").read_text())
    doc["entries"].append({"ticker": "PSN", "status": "disqualified", "reason": "conflict test"})
    doc["entries"].append({"ticker": "WEIRD", "status": "banana", "reason": "bad status"})
    log3 = Path(tmp) / "log3.yaml"; log3.write_text(_y.safe_dump(doc, sort_keys=False, allow_unicode=True))
    out, _ = run("--db", rdb, "--retag", "1", "--log", str(log3))
    check("positions.yaml wins a conflict, with a warning", db.get_tags(db.connect(rdb), 1)["PSN"]["tag"] == "WATCH" and "PSN is in positions.yaml" in out, out)
    check("an unknown status is reported and ignored", "WEIRD has status 'banana'" in out, out)

    # 13. tagging trouble never costs you the saved run
    bad = Path(tmp) / "bad.yaml"; bad.write_text("entries: [unclosed\n  - {")
    CURRENT[:] = ["PSN", "RBA", "TXT"]
    bdb = str(Path(tmp) / "bad.db")
    out, code = run("--db", bdb, "--log", str(bad))
    check("a malformed YAML file is a warning; the run is still saved", code == 0 and "saved but could not be tagged" in out and db.latest_run_id(db.connect(bdb)) == 1, out[-400:])
    out, code = run("--db", bdb, "--log", str(Path(tmp) / "nope.yaml"), "--positions", str(Path(tmp) / "nope2.yaml"))
    tg = db.get_tags(db.connect(bdb), 2)
    check("with no ledger files, everything is NEW and you are told why", code == 0 and all(t["tag"] == "NEW" for t in tg.values()) and "not found" in out, out[-400:])

    # 14. errors are clear
    out, code = run("--db", rdb, "--retag", "99")
    check("--retag on a missing run exits with a clear message", code != 0)

    # 15. your real path: a version-1 database is upgraded by the scanner, then tagged
    udb = str(Path(tmp) / "v1.db"); cu = db.connect(udb)
    db.init_schema(cu, db.SCHEMA_FILE.read_text().split("-- migration: 2")[0])
    db.insert_screen_run(cu, source="finviz_screener", results=[{"Ticker": "OLD", "Price": 1}], csv_path="candidates_2026-09-20.csv")
    cu.close()
    out, code = run("--db", udb)
    cu = db.connect(udb)
    check("scanner upgrades a version-1 database, keeps its old run, and tags the new one",
          db.current_version(cu) == db.load_migrations()[-1][0] and len(db.list_runs(cu, 9)) == 2 and set(db.get_tags(cu, 2)) == {"PSN", "RBA", "TXT"}, out[-300:])

print(f"\n{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} checks")
sys.exit(0 if all(results) else 1)

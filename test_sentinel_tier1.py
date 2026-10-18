"""Phase 6 tests: Tier 1 rules, Funnel A (moves + surprises), Funnel B, dedupe, storage, CLI."""
import contextlib, io, json, os, sys, tempfile
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sentinel_db as db, sentinel_finviz as fq, sentinel_tier1 as tier1

results = []
def check(label, ok, detail=""):
    ok = bool(ok); results.append(ok); print(("  ok    " if ok else "  FAIL  ") + label + ("" if ok else f"  {detail}"))
def raises(exc, fn):
    try: fn()
    except exc: return True
    except Exception: return False
    return False

TODAY = date(2026, 9, 20)
RULES, META, WARN = tier1.load_rules(HERE / "sentinel_tier1_rules.yaml")

# ----------------------------------------------------------------------------- rules
print("Rules")
check("the real rules file loads with your locked numbers", RULES["funnel_a"]["price_move_pct"] == 15.0 and RULES["funnel_a"]["earnings_surprise_pct"] == 10.0 and WARN == [], str(WARN))
check("the 15% decision is recorded in the rules file, with the NYT caveat",
      any("Keep 15%" in n["decision"] and "NYT" in n["caveat"] and n["date"].isoformat() == "2026-09-20" for n in META["notes"]))
check("the rules version is a readable string", tier1.rules_version(META) == "3@2026-09-20")
import tempfile as _t
with _t.TemporaryDirectory() as td:
    r2, m2, w2 = tier1.load_rules(Path(td) / "nope.yaml")
    check("a missing rules file falls back to the built-in defaults, with a warning", r2 == tier1.DEFAULTS and any("not found" in w for w in w2))
    bad = Path(td) / "bad.yaml"
    def load(text): bad.write_text(text); return tier1.load_rules(bad)
    check("an unknown setting is reported, not fatal", any("unknown setting 'funnel_a.banana'" in w for w in load("funnel_a: {banana: 1}\n")[2]))
    try: load("funnel_a: {price_move_pct: -1}\n"); msg0 = ""
    except tier1.RulesError as e: msg0 = str(e)
    check("a nonsense value names the setting", "funnel_a.price_move_pct" in msg0, msg0)
    try: load("funnel_a: {near_miss_ratio: 2}\n"); msg = ""
    except tier1.RulesError as e: msg = str(e)
    check("near_miss_ratio must be within (0, 1]", "near_miss_ratio" in msg, msg)
    check("whole-number settings reject fractions and groups reject scalars",
          raises(tier1.RulesError, lambda: load("funnel_a: {lookback_days: 2.5}\n")) and raises(tier1.RulesError, lambda: load("funnel_b: {dislocation: 5}\n")))

# ------------------------------------------------------------------------ measurements
print("\nMeasurements")
mv = tier1.daily_moves([("2026-09-11", 100.0), ("2026-09-14", 84.0), ("2026-09-24", 60.0)], 4)
check("daily_moves: Friday to Monday counts as one trading day", mv[0]["single_day"] and abs(mv[0]["ret_pct"] + 16.0) < 1e-9 and mv[0]["gap_days"] == 3)
check("daily_moves: a 10-day hole is flagged as not a single-day move", not mv[1]["single_day"] and mv[1]["gap_days"] == 10)
real = fq.parse_quote_page((HERE / "PSN_real.html").read_text(encoding="utf-8", errors="ignore"))[0] if (HERE / "PSN_real.html").exists() else None
check("pct_from_high reads Finviz's '89.50 -49.92%' cell", tier1.pct_from_high({"52W High": "89.50 -49.92%"}) == -49.92 and tier1.pct_from_high({"52W High": "-"}) is None and tier1.pct_from_high({}) is None)
check("insider_date reads Finviz's \"Aug 07 '26\"", tier1.insider_date("Aug 07 '26") == date(2026, 8, 7) and tier1.insider_date("nonsense") is None and tier1.insider_date(None) is None)

# --------------------------------------------------------------------------- seeding
def series(pairs): return [(f"2026-09-{d:02d}", c) for d, c in pairs]
def seed_db(path):
    c = db.connect(path); db.init_schema(c)
    P = lambda t, pairs: db.upsert_prices(c, t, series(pairs))
    P("NYT",  [(10, 100), (11, 100), (14, 84.0), (15, 84.5), (16, 84.2), (17, 84.0), (18, 84.4)])      # -16.0% on 09-14: escalates
    P("PSN",  [(14, 51.2), (15, 51.0), (16, 44.8), (17, 45.0), (18, 44.82)])                            # -12.2% on 09-16: near miss (the NYT case)
    P("LBRT", [(14, 20.0), (15, 20.5), (16, 20.3), (17, 20.4), (18, 20.36)])                            # quiet
    P("EDGE", [(16, 100.0), (17, 85.0), (18, 85.0)])                                                    # exactly -15.0%: escalates
    P("UPPER", [(16, 100.0), (17, 115.5), (18, 115.0)])                                                 # +15.5%: up-moves count too
    P("HUBS", [(1, 100.0), (14, 80.0), (15, 80.0)])                                                     # -20% but 13 days apart: a hole
    P("OLDMV", [(1, 100.0), (2, 70.0), (16, 70.0), (17, 70.5), (18, 70.6)])                             # big move, but before the window
    P("MNDY", [(1, 100.0), (2, 101.0)])                                                                 # stale: last close 09-02
    # quote-only names: no price history, only the day change stored with a live quote
    db.insert_calendar_run(c, as_of_date="2026-09-18", window_from="2026-08-19", window_to="2026-11-30", run_at="2026-09-18T21:00:00+00:00",
        events=[], quotes={"QUOTEA": {"price": 50.0, "change_pct": -15.2}, "QUOTEB": {"price": 50.0, "change_pct": -14.9}, "QUOTEC": {"price": 50.0, "change_pct": -3.0}})
    return c

def positions_file(td):
    p = Path(td) / "positions.yaml"
    p.write_text("as_of: 2026-09-20\nheld:\n" + "".join(f"  - {{ticker: {t}}}\n" for t in ("NYT", "LBRT", "EDGE", "UPPER", "OLDMV", "QUOTEA", "QUOTEB", "QUOTEC")) +
                 "watch:\n" + "".join(f"  - {{ticker: {t}}}\n" for t in ("PSN", "HUBS", "MNDY", "NODATA")))
    return p

def names(items, trig=None): return sorted(i["ticker"] for i in items if trig is None or i["trigger"] == trig)

with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    conn = seed_db(str(Path(tmp) / "t.db")); pos = positions_file(tmp)
    tracked = tier1.tracked_names(pos)
    check("tracked names come from positions.yaml, held first", tracked[0] == ("NYT", "held") and ("PSN", "watch") in tracked and len(tracked) == 12)

    # -------------------------------------------------------------- Funnel A: price moves
    print("\nFunnel A: price moves")
    A = tier1.funnel_a(conn, TODAY, RULES, tracked)
    esc = {i["ticker"]: i for i in A["escalate"] if i["trigger"] == "price_move"}
    check("a -16% one-day move escalates, with the date and the numbers in the reason",
          "NYT" in esc and esc["NYT"]["ref"] == "2026-09-14" and "-16.0%" in esc["NYT"]["reasons"][0] and "100.00 to 84.00" in esc["NYT"]["reasons"][0], str(esc.get("NYT")))
    check("exactly 15.0% escalates (at or above)", "EDGE" in esc)
    check("an UP move of 15.5% escalates too", "UPPER" in esc and esc["UPPER"]["value"] > 15)
    near = {i["ticker"]: i for i in A["near"] if i["trigger"] == "price_move"}
    check("THE NYT CASE: a ~12% drop does NOT escalate, but is logged as a near miss with its share of the threshold",
          "PSN" not in esc and "PSN" in near and abs(near["PSN"]["value"] + 12.16) < 0.05 and "81% of the 15% threshold" in near["PSN"]["reasons"][0], str(near.get("PSN")))
    check("a quiet name produces nothing", "LBRT" not in esc and "LBRT" not in near)
    check("a -20% change across a 13-day hole in the data is NOT counted as a single-day move, and says why",
          "HUBS" not in esc and any("HUBS" in n and "13 days apart" in n for n in A["notes"]), str(A["notes"]))
    check("a big move before the lookback window is ignored", "OLDMV" not in esc and "OLDMV" not in near)
    check("no history: the live-quote day change is used (-15.2% escalates, source stated)",
          "QUOTEA" in esc and esc["QUOTEA"]["source"] == "live quote" and "approximate" in esc["QUOTEA"]["reasons"][0])
    check("no history: -14.9% does not escalate but is a near miss", "QUOTEB" not in esc and "QUOTEB" in near)
    check("coverage names what data each tracked name had",
          "NYT" in A["coverage"]["history"] and set(A["coverage"]["quotes"]) == {"QUOTEA", "QUOTEB", "QUOTEC"} and "NODATA" in A["coverage"]["none"], str(A["coverage"]))
    stale = [tuple(x) for x in A["coverage"]["stale"]]
    check("coverage flags stale price data, even when it sits outside the window (a move since then would be missed)",
          ("MNDY", "2026-09-02") in stale and ("HUBS", "2026-09-15") in stale and not any(t == "NYT" for t, _ in stale), str(stale))

    # -------------------------------------------------------------- Funnel A: earnings
    print("\nFunnel A: earnings surprises")
    ev = lambda t, d, **k: {"ticker": t, "event_date": d, "kind": "held", **k}
    db.insert_calendar_run(conn, as_of_date="2026-09-16", window_from="2026-08-17", window_to="2026-11-30", run_at="2026-09-16T21:00:00+00:00", quotes={}, events=[
        ev("NYT", "2026-09-15", eps_est=0.80, rev_est=1.4e9),                       # no actuals yet at this run
        ev("LBRT", "2026-09-14", eps_est=0.50, eps_act=0.50, rev_est=1.0e9, rev_act=1.0e9),
    ])
    db.insert_calendar_run(conn, as_of_date="2026-09-18", window_from="2026-08-19", window_to="2026-11-30", run_at="2026-09-18T21:00:00+00:00", quotes={}, events=[
        ev("NYT", "2026-09-15", eps_est=0.80, eps_act=0.95, rev_est=1.4e9, rev_act=1.43e9),           # EPS +18.75%
        ev("LBRT", "2026-09-14", eps_est=0.50, eps_act=0.50, rev_est=1.0e9, rev_act=1.0e9),
        ev("PSN", "2026-09-16", kind="watch", eps_est=1.00, eps_act=1.0, rev_est=1.0e9, rev_act=1.11e9),  # revenue only +11%
        ev("HUBS", "2026-09-16", kind="watch", eps_est=2.00, eps_act=2.0, rev_est=1.0e9, rev_act=1.09e9), # revenue +9%: near miss
        ev("MNDY", "2026-09-16", kind="watch", eps_est=1.00, eps_act=1.05),                                # +5%: nothing
        ev("EDGE", "2026-09-16", eps_est=0.0, eps_act=0.10),                                               # zero estimate: cannot compute
        ev("SMALL", "2026-09-16", eps_est=0.05, eps_act=0.07),                                             # +40% on a tiny base... but not tracked
        ev("UPPER", "2026-09-01", eps_est=1.0, eps_act=2.0),                                                # outside the lookback window
        ev("QUOTEC", "2026-09-16", eps_est=0.05, eps_act=0.06, rev_est=None),                              # tracked, +20% on a 0.05 base
    ])
    A = tier1.funnel_a(conn, TODAY, RULES, tracked)
    es = {i["ticker"]: i for i in A["escalate"] if i["trigger"] == "earnings_surprise"}
    nm = {i["ticker"]: i for i in A["near"] if i["trigger"] == "earnings_surprise"}
    check("EPS +18.75% on the print escalates, using the LATEST calendar run's actuals (not the earlier run without them)",
          "NYT" in es and es["NYT"]["ref"] == "2026-09-15" and "EPS +18.7%" in es["NYT"]["reasons"][0] and "0.95 vs 0.80" in es["NYT"]["reasons"][0], str(es.get("NYT")))
    check("a revenue-only surprise of 11% escalates", "PSN" in es and "revenue +11.0%" in es["PSN"]["reasons"][0])
    check("9% is a near miss, not an escalation", "HUBS" not in es and "HUBS" in nm and "90% of the 10% threshold" in nm["HUBS"]["reasons"][0], str(nm.get("HUBS")))
    check("a 5% surprise, an in-line print, a zero estimate and an untracked name produce nothing",
          not ({"MNDY", "LBRT", "EDGE", "SMALL"} & (set(es) | set(nm))) or ("EDGE" in esc and not {"MNDY", "LBRT", "SMALL"} & (set(es) | set(nm))))
    check("a print outside the lookback window is ignored", not any(i["ref"] == "2026-09-01" for i in A["escalate"] + A["near"] if i["trigger"] == "earnings_surprise"))
    check("a tiny EPS base is flagged as such in the reason", "QUOTEC" in es and "small EPS base" in es["QUOTEC"]["reasons"][0], str(es.get("QUOTEC")))

    # -------------------------------------------------------------- store + dedupe
    print("\nStorage and dedupe")
    B0 = {"escalate": [], "over_cap": [], "not_eligible": [], "pending": [], "recent": [], "notes": [], "screen": None, "n_checked": 0}
    n1, run1 = tier1.store_results(conn, TODAY, "1@2026-09-20", A, B0, store=True)
    q = conn.execute("SELECT COUNT(*) FROM escalations").fetchone()[0]
    check("escalations are queued once each, with ids", n1 == q and n1 == len(A["escalate"]) and all(i["is_new"] and i["escalation_id"] for i in A["escalate"]))
    A2 = tier1.funnel_a(conn, TODAY, RULES, tracked)
    n2, run2 = tier1.store_results(conn, TODAY, "1@2026-09-20", A2, B0, store=True)
    check("running again queues NOTHING new (no duplicates), but the items are recognised as already queued",
          n2 == 0 and conn.execute("SELECT COUNT(*) FROM escalations").fetchone()[0] == q and all(not i["is_new"] for i in A2["escalate"]))
    check("every evaluation is logged, near misses included", conn.execute("SELECT COUNT(*) FROM tier1_runs").fetchone()[0] == 2
          and any(i["ticker"] == "PSN" for i in json.loads(conn.execute("SELECT details_json FROM tier1_runs WHERE tier1_run_id = ?", (run1,)).fetchone()[0])["funnel_a"]["near"]))
    A3 = tier1.funnel_a(conn, TODAY, RULES, tracked)
    n3, _ = tier1.store_results(conn, TODAY, "1@2026-09-20", A3, B0, store=False)
    check("a dry run stores nothing", conn.execute("SELECT COUNT(*) FROM escalations").fetchone()[0] == q and conn.execute("SELECT COUNT(*) FROM tier1_runs").fetchone()[0] == 2 and n3 == 0)
    A4 = tier1.funnel_a(conn, date(2026, 9, 14) + timedelta(days=1), RULES, tracked)
    check("replaying an earlier date (--as-of) sees the move that had just happened then", any(i["ticker"] == "NYT" and i["ref"] == "2026-09-14" for i in A4["escalate"]))

    # -------------------------------------------------------------- Funnel B
    print("\nFunnel B: screen candidates")
    def dossier(price="44.82", target="58.10", high="89.50 -49.92%", buys=(("Aug 07 '26", "Buy"),), ups=(("2026-08-21", "Upgrade"),), extra_ups=()):
        return {"page_title": "x", "quote_asof": "Sep 18", "profile": {}, "peers": [], "peer_details": {}, "held_by": [], "news": [],
                "snapshot": {"Price": price, "Target Price": target, "52W High": high},
                "insiders": [{"insider_trading": "Ball George L.", "date": d, "transaction": t} for d, t in buys],
                "ratings": [{"date": "x", "date_iso": d, "action": a, "analyst": "Baird"} for d, a in list(extra_ups) + list(ups)]}
    cb = db.connect(str(Path(tmp) / "b.db")); db.init_schema(cb)
    def screen(tags, when="2026-09-20T12:00:00+00:00"):
        rid = db.insert_screen_run(cb, source="finviz_screener", run_at=when, results=[{"Ticker": t, "Company": f"{t} Corp"} for t in tags])
        db.insert_screen_tags(cb, rid, [{"ticker": t, "tag": tg} for t, tg in tags.items()]); return rid
    put = lambda t, d, ok=True, at="2026-09-19T12:00:00+00:00": db.insert_dossier(cb, ticker=t, data=d, parse_ok=ok, fetched_at=at)

    if real:
        rid = screen({"PSN": "NEW"}); put("PSN", real)
        Bp = tier1.funnel_b(cb, TODAY, RULES)
        c = Bp["escalate"][0] if Bp["escalate"] else {}
        check("YOUR REAL PSN PAGE escalates: 50% below its high, one insider buy in 90 days, an upgrade in 30, target 30% above price",
              c.get("ticker") == "PSN" and set(c.get("signals", [])) == {"drawdown", "insider_buys", "upgrade", "target_upside"}, str(c.get("signals")))
        check("the reasons say it in plain English, with the numbers",
              "50% below its 52-week high" in c["reasons"][0] and "1 open-market insider buy" in c["reasons"][1] and "Ball George L." in c["reasons"][1] and "1 analyst upgrade" in c["reasons"][1], str(c.get("reasons")))
        cb.execute("DELETE FROM screen_runs")
        cb.execute("DELETE FROM dossier_snapshots")

    tickers = {f"K{i}": "NEW" for i in range(1, 8)}
    tickers.update({"NOD": "NEW", "FAIL": "NEW", "DISLO": "NEW", "CONV": "NEW", "WATCHED": "WATCH", "CLOSED": "CLOSED"})
    rid = screen(tickers)
    for i in range(1, 8):      # 7 eligible names with different strengths
        put(f"K{i}", dossier(high=f"100.00 -{30 + i * 5}.00%", target="45.00", ups=(("2026-09-10", "Upgrade"),) if i % 2 else ()))
    put("FAIL", dossier(), ok=False)                                                                  # a failed parse is not a dossier
    put("DISLO", dossier(high="100.00 -45.00%", buys=(), ups=(), target="46.00"))                     # dislocation only
    put("CONV", dossier(high="100.00 -10.00%"))                                                       # conviction only
    put("WATCHED", dossier()); put("CLOSED", dossier())
    Bx = tier1.funnel_b(cb, TODAY, RULES)
    check("only NEW names are evaluated (watch and closed names are ignored)", Bx["n_checked"] == 11 and not any(c["ticker"] in ("WATCHED", "CLOSED") for c in Bx["escalate"] + Bx["over_cap"] + Bx["not_eligible"]))
    check("a candidate with no dossier, or only a failed parse, is 'pending', not judged", sorted(p["ticker"] for p in Bx["pending"]) == ["FAIL", "NOD"], str(Bx["pending"]))
    ne = {c["ticker"]: c["why_not"] for c in Bx["not_eligible"]}
    check("dislocation without conviction is not eligible, and says which half is missing", "DISLO" in ne and "no conviction signal" in ne["DISLO"] and "no dislocation" not in ne["DISLO"], str(ne.get("DISLO")))
    check("conviction without dislocation is not eligible either", "CONV" in ne and "no dislocation signal (10% below its 52-week high, needs 30%)" in ne["CONV"], str(ne.get("CONV")))
    check("the per-run cap holds: 5 escalate, the other 2 wait", len(Bx["escalate"]) == 5 and len(Bx["over_cap"]) == 2)
    check("ranking: more signals first, then the deeper drawdown (top 5 escalate, K4 and K2 wait)",
          [c["ticker"] for c in Bx["escalate"]] == ["K7", "K5", "K3", "K1", "K6"] and [c["ticker"] for c in Bx["over_cap"]] == ["K4", "K2"],
          str([c["ticker"] + str(len(c["signals"])) for c in Bx["escalate"] + Bx["over_cap"]]))

    nB, _ = tier1.store_results(cb, TODAY, "1@2026-09-20", {"escalate": [], "near": [], "coverage": {"history": [], "quotes": [], "none": [], "stale": []}, "notes": [], "n_checked": 0, "n_prints": 0}, Bx, store=True)
    check("Funnel B escalations are stored as 'candidate' with the evaluation date as the ref", nB == 5 and cb.execute("SELECT DISTINCT trigger, trigger_ref FROM escalations").fetchall()[0][:] == ("candidate", "2026-09-20"))
    B2 = tier1.funnel_b(cb, TODAY + timedelta(days=5), RULES)
    check("five days later, with the same evidence, the same names are skipped as 'escalated recently'", len(B2["recent"]) == 5 and sorted(c["ticker"] for c in B2["escalate"]) == sorted(c["ticker"] for c in Bx["over_cap"]), str(len(B2["recent"])))
    put("K7", dossier(high="100.00 -65.00%", ups=(("2026-09-10", "Upgrade"),), buys=(("Aug 07 '26", "Buy"), ("Sep 15 '26", "Buy")), target="90.00"), at="2026-09-24T12:00:00+00:00")
    B3 = tier1.funnel_b(cb, TODAY + timedelta(days=5), RULES)
    k7 = [c for c in B3["escalate"] + B3["over_cap"] if c["ticker"] == "K7"]
    check("a name that shows NEW evidence (a target now 100% above price) is re-escalated inside the 30 days, and the reason names it",
          bool(k7) and k7[0].get("new_evidence") == ["target_upside"] and any("New evidence since the escalation on 2026-09-20" in r for r in k7[0].get("reasons", [])) and k7[0] in B3["escalate"],
          str(k7))
    B4 = tier1.funnel_b(cb, TODAY + timedelta(days=35), RULES)
    check("after 30+ days a name is evaluated afresh", not any(r["ticker"] == "K7" for r in B4["recent"]))

    # -------------------------------------------------------------- tolerance band, target-alone, day-change rows
    print("\nFunnel B tolerance and Finviz day-change rows")
    import copy
    tb = db.connect(str(Path(tmp) / "tol.db")); db.init_schema(tb)
    def tscreen(tags):
        rid = db.insert_screen_run(tb, source="finviz_screener", run_at="2026-09-20T12:00:00+00:00", results=[{"Ticker": k, "Company": f"{k} Corp"} for k in tags])
        db.insert_screen_tags(tb, rid, [{"ticker": k, "tag": "NEW"} for k in tags])
    tput = lambda k, d: db.insert_dossier(tb, ticker=k, data=d, parse_ok=True, fetched_at="2026-09-19T12:00:00+00:00")
    tscreen(["GEHC", "FAR", "FULL", "HALF", "OLED", "OLEDUP", "UPB"])
    tput("GEHC", dossier(high="100.00 -29.00%", target="45.00"))                                # 29% vs 30%: the case that started this
    tput("FAR", dossier(high="100.00 -26.00%", target="45.00"))                                 # 26%: outside the 10% band
    tput("FULL", dossier(high="100.00 -40.00%", target="45.00"))                                # drawdown full + insider full
    tput("HALF", dossier(high="100.00 -29.00%", target="45.00"))                                # drawdown borderline + insider full
    tput("OLED", dossier(high="100.00 -49.00%", target="65.00", buys=(), ups=()))               # target gap only
    tput("OLEDUP", dossier(high="100.00 -49.00%", target="65.00", buys=(), ups=(("2026-09-10", "Upgrade"),)))
    tput("UPB", dossier(high="100.00 -40.00%", target="55.00", buys=(("Aug 07 '26", "Buy"),), ups=()))  # target upside 22.7% vs 25%: borderline
    Bt = tier1.funnel_b(tb, TODAY, RULES)
    ok_by = {c["ticker"]: c for c in Bt["escalate"] + Bt["over_cap"]}
    ne_by = {c["ticker"]: c["why_not"] for c in Bt["not_eligible"]}
    check("THE GEHC CASE: 29% below the high (rule 30%) now counts, marked borderline, and the reason says so",
          "GEHC" in ok_by and ok_by["GEHC"]["borderline"] == ["drawdown"] and "borderline: the rule is 30%, tolerance 10%" in ok_by["GEHC"]["reasons"][0], str(ok_by.get("GEHC", ne_by.get("GEHC"))))
    check("26% is outside the band and still not eligible, with the numbers", "FAR" in ne_by and "26% below its 52-week high, needs 30%" in ne_by["FAR"], str(ne_by.get("FAR")))
    check("a target-upside gap of 22.7% (rule 25%) is borderline conviction, and the evidence records which signals are borderline",
          "UPB" in ok_by and "target_upside" in ok_by["UPB"]["borderline"] and ok_by["UPB"]["evidence"]["strength"]["target_upside"] == "borderline")
    ordered = [c["ticker"] for c in Bt["escalate"] + Bt["over_cap"]]
    check("full-strength signals rank ahead of borderline ones with the same number of signals (FULL before HALF)", ordered.index("FULL") < ordered.index("HALF"), str(ordered))
    check("whole-number signals (insider buys) get no tolerance", "insider_buys" not in ok_by["HALF"]["borderline"] and ok_by["HALF"]["strength"]["insider_buys"] == "full")
    strict = copy.deepcopy(RULES); strict["funnel_b"]["borderline_ratio"] = 1.0
    Bs = tier1.funnel_b(tb, TODAY, strict)
    check("borderline_ratio 1.0 switches the tolerance off (29% fails again)", "GEHC" in {c["ticker"] for c in Bs["not_eligible"]} and not any(c["borderline"] for c in Bs["escalate"] + Bs["over_cap"]))

    check("OLED-type candidate (deep drawdown, only a target gap) is NOT eligible under the shipped rules file (decision 40), and says why",
          "OLED" not in ok_by and "OLED" in ne_by and "rests on the consensus target alone" in ne_by["OLED"], str(ne_by.get("OLED")))
    loose = copy.deepcopy(RULES); loose["funnel_b"]["conviction"]["target_upside_counts_alone"] = True
    Bl = tier1.funnel_b(tb, TODAY, loose)
    ok_l = {c["ticker"]: c for c in Bl["escalate"] + Bl["over_cap"]}
    check("...and the old behaviour is one setting away: with target_upside_counts_alone: true it escalates on the target gap alone",
          "OLED" in ok_l and set(ok_l["OLED"]["conviction"]) == {"target_upside"})
    strict2 = copy.deepcopy(RULES); strict2["funnel_b"]["conviction"]["target_upside_counts_alone"] = False
    Bo = tier1.funnel_b(tb, TODAY, strict2)
    ne2 = {c["ticker"]: c["why_not"] for c in Bo["not_eligible"]}
    ok2 = {c["ticker"] for c in Bo["escalate"] + Bo["over_cap"]}
    check("with target_upside_counts_alone: false it is not eligible, and says why", "OLED" in ne2 and "rests on the consensus target alone" in ne2["OLED"], str(ne2.get("OLED")))
    check("...but the same name WITH an upgrade still qualifies, and a name with insider buys is unaffected", "OLEDUP" in ok2 and "UPB" in ok2)
    with tempfile.TemporaryDirectory() as td2:
        rf = Path(td2) / "r.yaml"
        def load2(text): rf.write_text(text); return tier1.load_rules(rf)
        check("the new rules settings are validated (ratio above 1, and a non-boolean flag, are refused)",
              raises(tier1.RulesError, lambda: load2("funnel_b: {borderline_ratio: 1.5}\n")) and raises(tier1.RulesError, lambda: load2("funnel_b: {conviction: {target_upside_counts_alone: 3}}\n")))

    dq = db.connect(str(Path(tmp) / "dchg.db")); db.init_schema(dq)
    db.upsert_prices(dq, "DCH", [("2026-09-11", 100.0, -1.0), ("2026-09-18", 84.0, -16.0)], source="finviz_screener")     # a week apart: gaps must not matter
    db.upsert_prices(dq, "NEAR", [("2026-09-18", 86.0, -13.8)], source="finviz_screener")
    db.upsert_prices(dq, "ONEROW", [("2026-09-17", 90.0, 15.0)], source="finviz_screener")
    Ad = tier1.funnel_a(dq, TODAY, RULES, [("DCH", "held"), ("NEAR", "watch"), ("ONEROW", "held"), ("NONE", "watch")])
    e_d = {i["ticker"]: i for i in Ad["escalate"]}
    check("a stored day change of -16% escalates, even with days missing between the rows (no gap problem), and says where the number came from",
          "DCH" in e_d and e_d["DCH"]["ref"] == "2026-09-18" and "Finviz" in e_d["DCH"]["reasons"][0] and e_d["DCH"]["evidence"]["basis"] == "day_change", str(e_d.get("DCH")))
    check("a single stored row is enough (its own day change is the move); +15.0% escalates", "ONEROW" in e_d and e_d["ONEROW"]["value"] == 15.0)
    check("-13.8% is logged as a near miss (92% of the threshold), not escalated", "NEAR" not in e_d and any(i["ticker"] == "NEAR" for i in Ad["near"]))
    check("coverage counts a name with day-change rows as having history", set(Ad["coverage"]["history"]) == {"DCH", "NEAR", "ONEROW"} and Ad["coverage"]["none"] == ["NONE"], str(Ad["coverage"]))

    ce = db.connect(str(Path(tmp) / "empty.db")); db.init_schema(ce)
    check("no scanner run: says so", "no scanner run yet" in tier1.funnel_b(ce, TODAY, RULES)["notes"][0].lower())
    db.insert_screen_run(ce, source="x", results=[{"Ticker": "AAA"}], run_at="2026-09-20T12:00:00+00:00")
    check("an untagged run: says how to fix it", "--retag" in tier1.funnel_b(ce, TODAY, RULES)["notes"][0])

    # -------------------------------------------------------------- the report and the command line
    print("\nReport and command line")
    Areport = tier1.funnel_a(conn, TODAY, RULES, tracked)
    tier1.store_results(conn, TODAY, "1@2026-09-20", Areport, B0, store=False)
    text = tier1.format_report(TODAY, META, "1@2026-09-20", Areport, tier1.funnel_b(conn, TODAY, RULES), True, [])
    check("the report carries the decision note, the near misses, and the data coverage",
          "Note (2026-09-20): Keep 15%" in text and "Near misses, logged only" in text and "Data coverage:" in text and "STALE" in text and "[DRY RUN" in text, text[:600])


    def cli(*argv):
        buf, code = io.StringIO(), 0
        sys.argv = ["tier1"] + list(argv)
        with contextlib.redirect_stdout(buf):
            try: tier1.main()
            except SystemExit as e: code = e.code if isinstance(e.code, int) else 1; buf.write(str(e.code) if isinstance(e.code, str) else "")
        return buf.getvalue(), code
    fresh = str(Path(tmp) / "fresh.db"); seed_db(fresh).close()
    out, code = cli("--db", fresh, "--dry-run", "--positions", str(pos), "--as-of", "2026-09-20")
    check("a dry-run report says 'would be queued' rather than claiming it queued anything", code == 0 and "would be queued" in out and "queued as escalation" not in out, out[-400:])
    check("...and a dry run on a fresh database really stored nothing", db.connect(fresh).execute("SELECT COUNT(*) FROM escalations").fetchone()[0] == 0)
    dbp = str(Path(tmp) / "cli.db"); cc = seed_db(dbp); cc.close()
    out, code = cli("--db", dbp, "--positions", str(pos), "--as-of", "2026-09-20")
    check("the real command line runs end to end and stores a run", code == 0 and "FUNNEL A" in out and "Stored Tier 1 run #1" in out and "NYT" in out, out[-500:])
    n_before = db.connect(dbp).execute("SELECT COUNT(*) FROM escalations").fetchone()[0]
    out, code = cli("--db", dbp, "--positions", str(pos), "--as-of", "2026-09-20")
    check("running it again stores no new escalations", "0 new escalation(s)" in out and db.connect(dbp).execute("SELECT COUNT(*) FROM escalations").fetchone()[0] == n_before, out[-300:])
    out, code = cli("--db", dbp, "--show")
    check("--show lists the open escalations with their reasons", f"{n_before} open escalation(s)" in out and "One-day move of -16.0%" in out, out[:400])
    out, code = cli("--db", dbp, "--mark", "1", "done", "--note", "reviewed by hand")
    out2, _ = cli("--db", dbp, "--show")
    check("--mark closes one, and it leaves the open list", "marked done" in out and f"{n_before - 1} open escalation(s)" in out2)
    check("a bad id or status gives a clear message, not a traceback", "No escalation with id 999" in cli("--db", dbp, "--mark", "999", "done")[0] and cli("--db", dbp, "--mark", "1", "banana")[1] != 0)
    out, code = cli("--db", dbp, "--near-misses", "--as-of", "2026-09-20")
    check("--near-misses lists what almost escalated, the NYT-style case", "PSN" in out and "-12.2%" in out and "DECISIONS.md item 13" in out, out)
    out, code = cli("--db", dbp, "--dry-run", "--positions", str(pos), "--as-of", "2026-09-20")
    check("--dry-run on an up-to-date database works and stores nothing", code == 0 and "[DRY RUN" in out and db.connect(dbp).execute("SELECT COUNT(*) FROM tier1_runs").fetchone()[0] == 2)
    old = str(Path(tmp) / "old.db"); oc = db.connect(old); db.init_schema(oc, db.SCHEMA_FILE.read_text().split("-- migration: 5")[0]); oc.close()
    out, code = cli("--db", old, "--dry-run", "--positions", str(pos))
    check("--dry-run on a database that needs upgrading refuses politely instead of failing", code != 0 and "has not been upgraded" in out, out)
    out, code = cli("--db", str(Path(tmp) / "missing.db"))
    check("a missing database is a clear message", code != 0 and "does not exist" in out, out)
    out, code = cli("--db", dbp, "--rules", str(Path(tmp) / "r.yaml"), "--positions", str(pos)) if (Path(tmp) / "r.yaml").write_text("funnel_a: {price_move_pct: 0}\n") else ("", 0)
    check("a bad rules file stops the run and names the setting", code != 0 and "funnel_a.price_move_pct" in out, out)

print(f"\n{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} checks")
sys.exit(0 if all(results) else 1)

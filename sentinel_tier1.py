"""
sentinel_tier1.py -- Tier 1: the mechanical gate that decides which names deserve a full Tier 2 (Claude) look.

It reads what the scanner and the calendar have already stored in research.db, compares numbers
with the thresholds in sentinel_tier1_rules.yaml, and queues the names that cross them in the `escalations`
table. It never interprets and never calls Claude. Everything it considered, including the near
misses that did NOT escalate, is logged in `tier1_runs`, so the thresholds can be tuned on evidence.

  python3 sentinel_tier1.py                     evaluate today, queue escalations, print a report
  python3 sentinel_tier1.py --dry-run           the same, but store nothing (safe to try any time)
  python3 sentinel_tier1.py --show              list the escalations still waiting for a Tier 2 look
  python3 sentinel_tier1.py --mark 3 done       close an escalation (done, dismissed or open) with --note "..."
  python3 sentinel_tier1.py --near-misses       everything that came close but did not escalate (default: last 90 days)
  python3 sentinel_tier1.py --as-of 2026-08-06  replay a past date against the stored history

Funnel A (held + watch names in positions.yaml), the numbers you locked in the decisions log:
  * a single-day close-to-close move of 15% or more, either direction
  * an EPS or revenue surprise of 10% or more on an earnings print
Funnel B (NEW candidates from the latest screen): needs a dislocation signal AND a conviction signal
from the stored Finviz dossier, at most N per run (a starter rule; see sentinel_tier1_rules.yaml).

Data it needs: price_history (the calendar fetches it), earnings_events and quote_snapshots (the calendar
saves them), and dossier_snapshots plus screen tags (the scanner saves them). Missing data is reported
as missing coverage, never silently treated as "nothing happened".
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import sentinel_db as db

try:
    import yaml
except ImportError:
    yaml = None
try:
    import sentinel_finviz as finviz_quote
except ImportError:
    finviz_quote = None

HERE = Path(__file__).resolve().parent
DEFAULT_RULES = HERE / "sentinel_tier1_rules.yaml"
DEFAULT_POSITIONS = HERE / "positions.yaml"

# Used when sentinel_tier1_rules.yaml is missing. Keep in step with the file.
DEFAULTS = {
    "funnel_a": {"price_move_pct": 15.0, "earnings_surprise_pct": 10.0, "near_miss_ratio": 0.8,
                 "lookback_days": 10, "max_gap_days": 4},
    "funnel_b": {"dislocation": {"drawdown_from_high_pct": 30.0, "single_day_drop_pct": 10.0, "single_day_lookback_days": 30},
                 "conviction": {"insider_buys_days": 90, "insider_buys_min": 1, "upgrade_days": 30, "upgrades_min": 1,
                                "target_upside_pct": 25.0, "target_upside_counts_alone": True},
                 "borderline_ratio": 0.9, "max_escalations_per_run": 5, "reescalate_after_days": 30},
}
TAG_LABEL = {"held": "held", "watch": "watch"}


class RulesError(ValueError):
    pass


# ------------------------------------------------------------------------- rules

def _merge(base: dict, over: dict, where: str, warnings: list) -> None:
    for k, v in over.items():
        if k not in base:
            warnings.append(f"sentinel_tier1_rules.yaml: unknown setting '{where}.{k}' ignored")
        elif isinstance(base[k], dict):
            if isinstance(v, dict):
                _merge(base[k], v, f"{where}.{k}", warnings)
            else:
                raise RulesError(f"'{where}.{k}' should be a group of settings, not a single value")
        else:
            base[k] = v


def load_rules(path=None):
    """Returns (rules, meta, warnings). A missing file means built-in defaults, with a warning.
    A setting that makes no sense raises RulesError with the setting's name."""
    if yaml is None:
        raise RuntimeError("pyyaml is not installed. Run: pip3 install pyyaml")
    rules, warnings = copy.deepcopy(DEFAULTS), []
    meta = {"version": 0, "as_of": None, "notes": []}
    p = Path(path) if path else DEFAULT_RULES
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        meta = {"version": raw.get("version", 0), "as_of": str(raw["as_of"]) if raw.get("as_of") else None,
                "notes": raw.get("decision_notes") or []}
        for section in ("funnel_a", "funnel_b"):
            _merge(rules[section], raw.get(section) or {}, section, warnings)
    else:
        warnings.append(f"{p.name} not found; using the built-in defaults")

    def positive(section_path, value, integer=False, allow_zero=False):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and (value >= 0 if allow_zero else value > 0)
        if ok and integer and int(value) != value:
            ok = False
        if not ok:
            raise RulesError(f"'{section_path}' must be a {'whole ' if integer else ''}number "
                             f"{'of 0 or more' if allow_zero else 'above 0'}, got {value!r}")

    fa, fb = rules["funnel_a"], rules["funnel_b"]
    positive("funnel_a.price_move_pct", fa["price_move_pct"])
    positive("funnel_a.earnings_surprise_pct", fa["earnings_surprise_pct"])
    if not (isinstance(fa["near_miss_ratio"], (int, float)) and 0 < fa["near_miss_ratio"] <= 1):
        raise RulesError(f"'funnel_a.near_miss_ratio' must be above 0 and at most 1, got {fa['near_miss_ratio']!r}")
    positive("funnel_a.lookback_days", fa["lookback_days"], integer=True)
    positive("funnel_a.max_gap_days", fa["max_gap_days"], integer=True)
    for k in ("drawdown_from_high_pct", "single_day_drop_pct"):
        positive(f"funnel_b.dislocation.{k}", fb["dislocation"][k])
    positive("funnel_b.dislocation.single_day_lookback_days", fb["dislocation"]["single_day_lookback_days"], integer=True)
    for k in ("insider_buys_days", "insider_buys_min", "upgrade_days", "upgrades_min"):
        positive(f"funnel_b.conviction.{k}", fb["conviction"][k], integer=True)
    if not isinstance(fb["conviction"]["target_upside_counts_alone"], bool):
        raise RulesError(f"'funnel_b.conviction.target_upside_counts_alone' must be true or false, got {fb['conviction']['target_upside_counts_alone']!r}")
    if not (isinstance(fb["borderline_ratio"], (int, float)) and not isinstance(fb["borderline_ratio"], bool) and 0 < fb["borderline_ratio"] <= 1):
        raise RulesError(f"'funnel_b.borderline_ratio' must be above 0 and at most 1 (1 means no tolerance), got {fb['borderline_ratio']!r}")
    positive("funnel_b.conviction.target_upside_pct", fb["conviction"]["target_upside_pct"])
    positive("funnel_b.max_escalations_per_run", fb["max_escalations_per_run"], integer=True)
    positive("funnel_b.reescalate_after_days", fb["reescalate_after_days"], integer=True, allow_zero=True)
    return rules, meta, warnings


def rules_version(meta) -> str:
    return f"{meta['version']}@{meta['as_of']}"


def tracked_names(positions_path=None) -> list:
    """[(ticker, 'held'|'watch')] from positions.yaml, held first."""
    if yaml is None:
        raise RuntimeError("pyyaml is not installed. Run: pip3 install pyyaml")
    raw = yaml.safe_load(Path(positions_path or DEFAULT_POSITIONS).read_text(encoding="utf-8")) or {}
    out = []
    for kind in ("held", "watch"):
        out += [(str(p["ticker"]).strip().upper(), kind) for p in raw.get(kind) or [] if str(p.get("ticker", "")).strip()]
    return out


# ------------------------------------------------------------------- measurements

def daily_moves(series, max_gap_days):
    """[(date, close)] ascending -> one dict per consecutive pair. single_day is False when the two
    closes are further apart than max_gap_days (a hole in the data), so a multi-day change is never
    mistaken for a single-day move."""
    out = []
    for (d0, c0), (d1, c1) in zip(series, series[1:]):
        if not c0:
            continue
        gap = (date.fromisoformat(d1) - date.fromisoformat(d0)).days
        out.append({"date": d1, "prev_date": d0, "close": c1, "prev_close": c0, "ret_pct": (c1 / c0 - 1) * 100,
                    "gap_days": gap, "single_day": gap <= max_gap_days})
    return out


def surprise_pct(actual, est):
    if actual is None or est is None or est == 0:
        return None
    return (actual - est) / abs(est) * 100


def human(v):
    if v is None:
        return "n/a"
    a = abs(v)
    return f"{v / 1e9:.2f}B" if a >= 1e9 else f"{v / 1e6:.0f}M" if a >= 1e6 else f"{v:,.2f}"


def pct_from_high(snapshot) -> float | None:
    """Finviz's '52W High' cell reads '89.50 -49.92%': the price, then how far the current price is from it."""
    parts = str((snapshot or {}).get("52W High", "")).split()
    if len(parts) >= 2 and parts[-1].endswith("%") and finviz_quote is not None:
        return finviz_quote.num(parts[-1])
    return None


_INSIDER_DATE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2})\s+'(\d{2})$")


def insider_date(s):
    m = _INSIDER_DATE.match(str(s or "").strip())
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {2000 + int(m.group(3))}", "%b %d %Y").date()
    except ValueError:
        return None


# --------------------------------------------------------------------- Funnel A

def unified_moves(conn, ticker, win_start, today, max_gap_days):
    """One-day moves for a ticker from win_start to today. Two kinds of stored price row are understood:
      * a row that carries its own day change (Finviz reports it), read directly, so gaps in the history
        do not matter;
      * a plain close (FMP), compared with the previous close, and only counted as a single-day move if the
        two closes are no more than max_gap_days apart.
    Returns (moves, usable, last_price_date). `usable` is False when there is too little data to say anything."""
    rows = [r for r in db.price_rows(conn, ticker, since=win_start) if r["date"] <= today.isoformat()]
    prior = conn.execute("SELECT price_date, close FROM price_history WHERE ticker = ? AND price_date < ? ORDER BY price_date DESC LIMIT 1",
                         (ticker, win_start)).fetchone()          # the close just before the window, so a hole is visible
    prev = (prior[0], prior[1]) if prior else None
    moves = []
    for r in rows:
        if r["change_pct"] is not None:
            ret = r["change_pct"]
            moves.append({"date": r["date"], "prev_date": None, "close": r["close"], "ret_pct": ret, "gap_days": 1, "single_day": True,
                          "prev_close": r["close"] / (1 + ret / 100) if ret > -99.9 else None, "basis": "day_change"})
        elif prev is not None and prev[1]:
            gap = (date.fromisoformat(r["date"]) - date.fromisoformat(prev[0])).days
            moves.append({"date": r["date"], "prev_date": prev[0], "close": r["close"], "prev_close": prev[1],
                          "ret_pct": (r["close"] / prev[1] - 1) * 100, "gap_days": gap, "single_day": gap <= max_gap_days, "basis": "closes"})
        prev = (r["date"], r["close"])
    usable = any(r["change_pct"] is not None for r in rows) or len(rows) + (1 if prior else 0) >= 2
    last = rows[-1]["date"] if rows else (prior[0] if prior else None)
    return moves, usable, last


def _price_events(conn, ticker, kind, today, fa):
    """Price-move findings for one tracked name, from stored daily prices if there are any, else from the
    day-change stored with each live quote. Returns (findings, near_misses, notes, source, last_date)."""
    thr, near = fa["price_move_pct"], fa["near_miss_ratio"] * fa["price_move_pct"]
    win_start = (today - timedelta(days=fa["lookback_days"])).isoformat()
    esc, nm, notes = [], [], []
    moves, usable, last = unified_moves(conn, ticker, win_start, today, fa["max_gap_days"])
    if usable:
        for m in moves:
            a = abs(m["ret_pct"])
            if not m["single_day"]:
                if a >= thr:
                    notes.append(f"{ticker}: {m['ret_pct']:+.1f}% between {m['prev_date']} and {m['date']}, but the closes are "
                                 f"{m['gap_days']} days apart (a hole in the price history), so it is not counted as a single-day move")
                continue
            how = (f"(close {m['close']:.2f}; the day's own change as reported by Finviz)" if m["basis"] == "day_change"
                   else f"({m['prev_close']:.2f} to {m['close']:.2f})")
            item = {"ticker": ticker, "kind": kind, "funnel": "A", "trigger": "price_move", "ref": m["date"],
                    "value": round(m["ret_pct"], 2), "threshold": thr, "source": "price history",
                    "reasons": [f"One-day move of {m['ret_pct']:+.1f}% on {m['date']} {how}, at or above the {thr:g}% threshold"],
                    "evidence": {"date": m["date"], "prev_date": m["prev_date"], "ret_pct": round(m["ret_pct"], 3), "close": m["close"],
                                 "prev_close": m["prev_close"], "source": "price_history", "basis": m["basis"]}}
            if a >= thr:
                esc.append(item)
            elif a >= near:
                item["reasons"] = [f"Near miss: one-day move of {m['ret_pct']:+.1f}% on {m['date']}, {a / thr * 100:.0f}% of the {thr:g}% threshold"]
                nm.append(item)
        return esc, nm, notes, "history", last

    try:
        rows = conn.execute(
            "SELECT q.change_pct, q.price, r.as_of_date FROM quote_snapshots q JOIN calendar_runs r ON r.calendar_run_id = q.calendar_run_id "
            "WHERE q.ticker = ? AND r.as_of_date >= ? AND r.as_of_date <= ? AND q.change_pct IS NOT NULL ORDER BY r.as_of_date",
            (ticker, win_start, today.isoformat())).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        a = abs(r["change_pct"])
        item = {"ticker": ticker, "kind": kind, "funnel": "A", "trigger": "price_move", "ref": r["as_of_date"],
                "value": round(r["change_pct"], 2), "threshold": thr, "source": "live quote",
                "reasons": [f"Day change of {r['change_pct']:+.1f}% in the live quote taken {r['as_of_date']} (no price history for "
                            f"this name, so the move date is approximate), at or above the {thr:g}% threshold"],
                "evidence": {"date": r["as_of_date"], "change_pct": r["change_pct"], "price": r["price"], "source": "quote_snapshot"}}
        if a >= thr:
            esc.append(item)
        elif a >= near:
            item["reasons"] = [f"Near miss: day change of {r['change_pct']:+.1f}% in the quote taken {r['as_of_date']}, "
                               f"{a / thr * 100:.0f}% of the {thr:g}% threshold"]
            nm.append(item)
    last = rows[-1]["as_of_date"] if rows else last
    return esc, nm, notes, ("quotes" if rows else "none"), last


def _earnings_events(conn, tracked_kind, today, fa):
    """Earnings-surprise findings for tracked names' prints inside the lookback window. Uses the most
    recent calendar run's numbers for each print (FMP fills in actuals after the report)."""
    thr = fa["earnings_surprise_pct"]
    near = fa["near_miss_ratio"] * thr
    since = (today - timedelta(days=fa["lookback_days"])).isoformat()
    try:
        rows = conn.execute(
            "SELECT e.ticker, e.event_date, e.eps_est, e.eps_act, e.rev_est, e.rev_act FROM earnings_events e "
            "WHERE e.event_date >= ? AND e.event_date <= ? AND (e.eps_act IS NOT NULL OR e.rev_act IS NOT NULL) "
            "AND e.calendar_run_id = (SELECT MAX(x.calendar_run_id) FROM earnings_events x WHERE x.ticker = e.ticker AND x.event_date = e.event_date) "
            "ORDER BY e.event_date, e.ticker", (since, today.isoformat())).fetchall()
    except sqlite3.OperationalError:
        return [], [], None
    esc, nm = [], []
    for r in rows:
        if r["ticker"] not in tracked_kind:
            continue
        s_eps, s_rev = surprise_pct(r["eps_act"], r["eps_est"]), surprise_pct(r["rev_act"], r["rev_est"])
        measured = [abs(x) for x in (s_eps, s_rev) if x is not None]
        if not measured:
            continue
        top = max(measured)
        parts = []
        if s_eps is not None:
            parts.append(f"EPS {s_eps:+.1f}% vs consensus ({r['eps_act']:.2f} vs {r['eps_est']:.2f})")
        if s_rev is not None:
            parts.append(f"revenue {s_rev:+.1f}% ({human(r['rev_act'])} vs {human(r['rev_est'])})")
        small = r["eps_est"] is not None and abs(r["eps_est"]) < 0.10 and s_eps is not None and abs(s_eps) >= near
        item = {"ticker": r["ticker"], "kind": tracked_kind[r["ticker"]], "funnel": "A", "trigger": "earnings_surprise",
                "ref": r["event_date"], "value": round(max((x for x in (s_eps, s_rev) if x is not None), key=abs), 2),
                "threshold": thr, "source": "earnings_events",
                "evidence": {"event_date": r["event_date"], "eps_est": r["eps_est"], "eps_act": r["eps_act"],
                             "rev_est": r["rev_est"], "rev_act": r["rev_act"], "eps_surprise_pct": s_eps, "rev_surprise_pct": s_rev}}
        tail = " (small EPS base: consensus under 0.10, so percentages swing widely)" if small else ""
        if top >= thr:
            item["reasons"] = [f"Reported {r['event_date']}: " + "; ".join(parts) + f", at or above the {thr:g}% threshold" + tail]
            esc.append(item)
        elif top >= near:
            item["reasons"] = [f"Near miss, reported {r['event_date']}: " + "; ".join(parts) + f", {top / thr * 100:.0f}% of the {thr:g}% threshold" + tail]
            nm.append(item)
    return esc, nm, len(rows)


def funnel_a(conn, today, rules, tracked):
    fa = rules["funnel_a"]
    kind_of = dict(tracked)
    esc, nm, notes = [], [], []
    cov = {"history": [], "quotes": [], "none": [], "stale": []}
    for ticker, kind in tracked:
        e, n, ns, source, last = _price_events(conn, ticker, kind, today, fa)
        esc, nm, notes = esc + e, nm + n, notes + ns
        cov[source if source in cov else "none"].append(ticker)
        if last is not None and (today - date.fromisoformat(last)).days > fa["max_gap_days"]:
            cov["stale"].append((ticker, last))
    e, n, n_prints = _earnings_events(conn, kind_of, today, fa)
    if n_prints is None:
        notes.append("The calendar has not saved any earnings history yet (run sentinel_calendar.py), so no earnings surprises were checked.")
    return {"escalate": esc + e, "near": nm + n, "coverage": cov, "notes": notes, "n_checked": len(tracked), "n_prints": n_prints or 0}


# --------------------------------------------------------------------- Funnel B

def _grade(value, threshold, ratio):
    """'full' when the value meets the threshold, 'borderline' when it falls short but is inside the tolerance
    band just below it (a value of 29 against a threshold of 30 is not meaningfully different), else None."""
    if value is None:
        return None
    if value >= threshold:
        return "full"
    return "borderline" if value >= threshold * ratio else None


def funnel_b(conn, today, rules):
    fb = rules["funnel_b"]
    dis, conv, ratio = fb["dislocation"], fb["conviction"], fb["borderline_ratio"]
    tol = round((1 - ratio) * 100, 1)
    res = {"escalate": [], "over_cap": [], "not_eligible": [], "pending": [], "recent": [], "notes": [], "screen": None, "n_checked": 0}
    run = conn.execute("SELECT * FROM screen_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    if not run:
        res["notes"].append("There is no scanner run yet, so there are no candidates to check.")
        return res
    tags = db.get_tags(conn, run["run_id"])
    res["screen"] = {"run_id": run["run_id"], "run_at": run["run_at"]}
    if not tags:
        res["notes"].append(f"Scanner run #{run['run_id']} has no ledger tags. Run: python3 sentinel_scan.py --retag")
        return res
    names = {r["ticker"]: (json.loads(r["row_json"]).get("Company") or r["ticker"])
             for r in conn.execute("SELECT ticker, row_json FROM screen_results WHERE run_id = ?", (run["run_id"],))}
    new = sorted(t for t, row in tags.items() if row["tag"] == "NEW")
    res["n_checked"] = len(new)
    cutoff_buys = today - timedelta(days=conv["insider_buys_days"])
    cutoff_up = (today - timedelta(days=conv["upgrade_days"])).isoformat()
    eligible = []
    for t in new:
        dos = db.latest_dossier(conn, t)
        if not dos:
            res["pending"].append({"ticker": t, "name": names.get(t, t)})
            continue
        d = dos["data"]
        snap = d.get("snapshot", {})
        dislocation, conviction, strength = {}, {}, {}

        def note(name, group, grade, text, threshold):
            group[name] = text + ("" if grade == "full" else f" (borderline: the rule is {threshold:g}%, tolerance {tol:g}%)")
            strength[name] = grade

        hp = pct_from_high(snap)
        g = _grade(-hp if hp is not None else None, dis["drawdown_from_high_pct"], ratio)
        if g:
            note("drawdown", dislocation, g, f"{-hp:.0f}% below its 52-week high" + (f" (needs {dis['drawdown_from_high_pct']:g}%)" if g == "full" else ""), dis["drawdown_from_high_pct"])
        moves, _, _ = unified_moves(conn, t, (today - timedelta(days=dis["single_day_lookback_days"])).isoformat(), today, rules["funnel_a"]["max_gap_days"])
        drops = [m for m in moves if m["single_day"]]
        worst = min(drops, key=lambda m: m["ret_pct"]) if drops else None
        g = _grade(-worst["ret_pct"] if worst else None, dis["single_day_drop_pct"], ratio)
        if g:
            note("drop", dislocation, g, f"a one-day drop of {worst['ret_pct']:.1f}% on {worst['date']}" + (f" (needs {dis['single_day_drop_pct']:g}%)" if g == "full" else ""), dis["single_day_drop_pct"])

        buys = [r for r in d.get("insiders", []) if str(r.get("transaction", "")).strip().lower() == "buy"
                and (insider_date(r.get("date")) or date.min) >= cutoff_buys]
        if len(buys) >= conv["insider_buys_min"]:
            latest = max(buys, key=lambda r: insider_date(r.get("date")))
            conviction["insider_buys"] = (f"{len(buys)} open-market insider buy(s) in {conv['insider_buys_days']} days "
                                          f"(latest {latest.get('date')}, {latest.get('insider_trading', '?')})")
            strength["insider_buys"] = "full"
        ups = [r for r in d.get("ratings", []) if r.get("action") == "Upgrade" and (r.get("date_iso") or "") >= cutoff_up]
        if len(ups) >= conv["upgrades_min"]:
            conviction["upgrade"] = f"{len(ups)} analyst upgrade(s) in {conv['upgrade_days']} days (latest {ups[0].get('date')}, {ups[0].get('analyst')})"
            strength["upgrade"] = "full"
        price, target = (finviz_quote.num(snap.get("Price")), finviz_quote.num(snap.get("Target Price"))) if finviz_quote else (None, None)
        upside = (target / price - 1) * 100 if price and target else None
        g = _grade(upside, conv["target_upside_pct"], ratio)
        if g:
            note("target_upside", conviction, g, f"consensus target {snap.get('Target Price')} is {upside:.0f}% above the price {snap.get('Price')}", conv["target_upside_pct"])

        row = {"ticker": t, "name": names.get(t, t), "funnel": "B", "trigger": "candidate", "kind": "candidate",
               "dislocation": dislocation, "conviction": conviction, "strength": strength, "from_high": hp, "upside": upside,
               "signals": sorted(list(dislocation) + list(conviction)),
               "borderline": sorted(k for k, v in strength.items() if v == "borderline"),
               "n_full": sum(1 for v in strength.values() if v == "full"),
               "dossier_id": dos["snapshot_id"], "dossier_fetched": dos["fetched_at"][:10]}

        why = []
        if not dislocation:
            why.append("no dislocation signal" + (f" ({-hp:.0f}% below its 52-week high, needs {dis['drawdown_from_high_pct']:g}%)" if hp is not None else " (52-week distance not available)"))
        if not conviction:
            why.append(f"no conviction signal ({len(buys)} insider buy(s), {len(ups)} upgrade(s), "
                       + (f"target {upside:+.0f}% from price" if upside is not None else "no target") + ")")
        elif not conv["target_upside_counts_alone"] and set(conviction) == {"target_upside"}:
            why.append("conviction rests on the consensus target alone (no insider buys or upgrades), and the rules say that is not enough")
        if why:
            row["why_not"] = "; ".join(why)
            res["not_eligible"].append(row)
            continue

        prev = conn.execute("SELECT created_at, evidence_json FROM escalations WHERE ticker = ? AND trigger = 'candidate' AND created_at >= ? "
                            "ORDER BY escalation_id DESC LIMIT 1",
                            (t, (today - timedelta(days=fb["reescalate_after_days"])).isoformat())).fetchone()
        if prev:
            old_signals = set((json.loads(prev["evidence_json"]) if prev["evidence_json"] else {}).get("signals", []))
            fresh = sorted(set(row["signals"]) - old_signals)
            if not fresh:
                res["recent"].append({"ticker": t, "name": row["name"], "when": prev["created_at"][:10]})
                continue
            row["new_evidence"] = fresh
            row["previous"] = prev["created_at"][:10]
        eligible.append(row)

    # full-strength signals rank ahead of borderline ones, then total signals, then the deeper drawdown
    eligible.sort(key=lambda c: (-c["n_full"], -len(c["signals"]), c["from_high"] if c["from_high"] is not None else 0.0, c["ticker"]))
    cap = fb["max_escalations_per_run"]
    for c in eligible[:cap]:
        c["ref"] = today.isoformat()
        c["reasons"] = ["Dislocation: " + "; ".join(c["dislocation"].values()), "Conviction: " + "; ".join(c["conviction"].values())]
        if c.get("new_evidence"):
            c["reasons"].append(f"New evidence since the escalation on {c['previous']}: {', '.join(c['new_evidence'])}")
        c["evidence"] = {"signals": c["signals"], "strength": c["strength"], "borderline": c["borderline"], "from_high_pct": c["from_high"],
                         "target_upside_pct": c["upside"], "dossier_snapshot_id": c["dossier_id"], "dossier_fetched": c["dossier_fetched"],
                         "dislocation": c["dislocation"], "conviction": c["conviction"]}
        res["escalate"].append(c)
    res["over_cap"] = eligible[cap:]
    return res


# ------------------------------------------------------------------ store + report

def _already_queued(conn, item):
    r = conn.execute("SELECT escalation_id FROM escalations WHERE ticker = ? AND trigger = ? AND trigger_ref = ?",
                     (item["ticker"], item["trigger"], str(item["ref"]))).fetchone()
    return r[0] if r else None


def store_results(conn, today, version, a, b, store=True):
    """Queue the escalations (unless dry run) and log the evaluation. Marks each item with is_new / escalation_id."""
    for item in a["escalate"] + b["escalate"]:
        if store:
            eid, created = db.insert_escalation(conn, ticker=item["ticker"], funnel=item["funnel"], trigger=item["trigger"],
                                                trigger_ref=item["ref"], reasons=item["reasons"], evidence=item["evidence"], rules_version=version)
        else:
            eid = _already_queued(conn, item)
            created = eid is None
        item["escalation_id"], item["is_new"] = eid, created
    n_new = sum(1 for i in a["escalate"] + b["escalate"] if i["is_new"])
    run_id = None
    if store:
        slim = lambda items: [{k: v for k, v in i.items() if k not in ("evidence",)} for i in items]  # noqa: E731
        details = {"funnel_a": {"escalate": slim(a["escalate"]), "near": slim(a["near"]), "coverage": a["coverage"], "notes": a["notes"], "n_prints": a["n_prints"]},
                   "funnel_b": {"escalate": slim(b["escalate"]), "over_cap": slim(b["over_cap"]), "not_eligible": slim(b["not_eligible"]),
                                "pending": b["pending"], "recent": b["recent"], "notes": b["notes"], "screen": b["screen"]}}
        run_id = db.insert_tier1_run(conn, as_of_date=today.isoformat(), rules_version=version,
                                     n_checked=a["n_checked"] + b["n_checked"], n_escalated=n_new, details=details)
    return n_new, run_id


def _wrap(text, indent, width=100):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width - indent:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return lines + ([cur] if cur else [])


RULES_LOOKBACK = [10]     # set by main() from the rules, only used for the wording of one report line


def format_report(today, meta, version, a, b, dry_run, rule_warnings):
    L = [f"Tier 1 as of {today.isoformat()}" + ("  [DRY RUN: nothing is stored]" if dry_run else ""),
         f"Rules: sentinel_tier1_rules.yaml, version {version}"]
    for w in rule_warnings:
        L.append(f"  rules warning: {w}")
    for n in meta.get("notes", [])[:1]:
        L.append(f"Note ({n.get('date')}): {n.get('decision')} Near misses are logged so this can be revisited (see DECISIONS.md, item 13).")

    def item_lines(i, prefix="    ", extra=""):
        head = f"{prefix}{i['ticker']:<6} {i['trigger'].replace('_', ' '):<17}{extra}"
        tag = ""
        if "is_new" in i:
            tag = (f"  -> queued as escalation #{i['escalation_id']}" if i["is_new"] else f"  (already queued as #{i['escalation_id']})") if not dry_run \
                else ("  -> would be queued" if i["is_new"] else f"  (already queued as #{i['escalation_id']})")
        out = [(head + tag).rstrip()]
        out += [f"{prefix}       {ln}" for r in i["reasons"] for ln in _wrap(r, len(prefix) + 7)]
        return out

    L += ["", f"FUNNEL A: tracked names ({a['n_checked']} held + watch)"]
    if a["escalate"]:
        L.append(f"  Escalate ({len(a['escalate'])}):")
        for i in a["escalate"]:
            L += item_lines(i)
    else:
        L.append("  Escalate: none")
    if a["near"]:
        L.append(f"  Near misses, logged only ({len(a['near'])}):")
        for i in a["near"]:
            L += item_lines(i)
    cov = a["coverage"]
    L.append(f"  Data coverage: price history for {len(cov['history'])}, live-quote fallback for {len(cov['quotes'])}, "
             f"no price data for {len(cov['none'])}; {a['n_prints']} recent earnings print(s) with actuals checked.")
    if cov["none"]:
        L.append(f"    no price data in the last {RULES_LOOKBACK[0]} days: {', '.join(cov['none'])}")
    if cov["stale"]:
        L.append("    STALE (a move since then would be missed): " + ", ".join(f"{t} (last {d})" for t, d in cov["stale"]))
    for n in a["notes"]:
        ls = _wrap(n, 8)
        L += [f"  note: {ls[0]}"] + [f"        {ln}" for ln in ls[1:]]

    L += ["", "FUNNEL B: NEW candidates from the latest screen" + (f" (run #{b['screen']['run_id']}, {b['screen']['run_at'][:10]}; {b['n_checked']} NEW)" if b["screen"] else "")]
    for n in b["notes"]:
        ls = _wrap(n, 8)
        L += [f"  note: {ls[0]}"] + [f"        {ln}" for ln in ls[1:]]
    if b["escalate"]:
        L.append(f"  Escalate ({len(b['escalate'])}):")
        for i in b["escalate"]:
            L += item_lines(i, extra=f" {i['name']}")
    elif b["screen"] and b["n_checked"]:
        L.append("  Escalate: none")
    if b["over_cap"]:
        L.append(f"  Eligible but over the per-run cap, will be picked up next run ({len(b['over_cap'])}): " + ", ".join(c["ticker"] for c in b["over_cap"]))
    if b["recent"]:
        L.append("  Skipped, escalated recently with the same evidence: " + ", ".join(f"{r['ticker']} ({r['when']})" for r in b["recent"]))
    if b["not_eligible"]:
        L.append(f"  Not eligible ({len(b['not_eligible'])}):")
        for c in b["not_eligible"]:
            L += [f"    {c['ticker']:<6} {ln}" for ln in _wrap(c["why_not"], 11)[:1]] + [f"    {'':<6} {ln}" for ln in _wrap(c["why_not"], 11)[1:]]
    if b["pending"]:
        L.append(f"  Waiting for a Finviz dossier ({len(b['pending'])}): " + ", ".join(p["ticker"] for p in b["pending"]))
        L.append("    fetch them with: python3 sentinel_scan.py --dossier-only")
    return "\n".join(L)


# -------------------------------------------------------------------------- CLI

def cmd_show(conn):
    rows = db.list_escalations(conn, "open")
    if not rows:
        print("No open escalations.")
        return
    print(f"{len(rows)} open escalation(s), oldest first:\n")
    for r in rows:
        print(f"  #{r['escalation_id']:<4} {r['ticker']:<6} Funnel {r['funnel']}, {r['trigger'].replace('_', ' ')} ({r['trigger_ref']}), queued {r['created_at'][:10]}")
        for reason in r["reasons"]:
            for ln in _wrap(reason, 10):
                print(f"          {ln}")
    print("\nClose one with: python3 sentinel_tier1.py --mark ID done   (or dismissed)")


def cmd_near_misses(conn, today, days):
    since = (today - timedelta(days=days)).isoformat()
    runs = db.recent_tier1_runs(conn, since)
    seen = {}
    for r in runs:
        for i in r["details"].get("funnel_a", {}).get("near", []):
            seen.setdefault((i["ticker"], i["trigger"], i["ref"]), i)
    if not seen:
        print(f"No near misses recorded in the last {days} days ({len(runs)} Tier 1 run(s) on file).")
        return
    print(f"Near misses in the last {days} days ({len(runs)} Tier 1 run(s) on file). These did NOT escalate:\n")
    for (t, trig, ref), i in sorted(seen.items(), key=lambda kv: -abs(kv[1]["value"])):
        print(f"  {t:<6} {trig.replace('_', ' '):<17} {ref}  {i['value']:+.1f}% vs threshold {i['threshold']:g}%  ({i['source']})")
    print("\nIf moves like these keep turning out to matter, see DECISIONS.md item 13 before changing the threshold.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="evaluate and report, but store nothing")
    ap.add_argument("--show", action="store_true", help="list open escalations")
    ap.add_argument("--mark", nargs=2, metavar=("ID", "STATUS"), help="set an escalation's status: open, done or dismissed")
    ap.add_argument("--note", default=None, help="with --mark: a note to keep with it")
    ap.add_argument("--near-misses", nargs="?", const=90, type=int, metavar="DAYS", help="list logged near misses (default last 90 days)")
    ap.add_argument("--as-of", default=None, help="evaluate as of this date (YYYY-MM-DD)")
    ap.add_argument("--db", default=None, help="database file (default: $RESEARCH_DB or research.db beside sentinel_db.py)")
    ap.add_argument("--rules", default=None, help="rules file (default: sentinel_tier1_rules.yaml beside this script)")
    ap.add_argument("--positions", default=None, help="positions.yaml (default: beside this script)")
    args = ap.parse_args()

    try:
        today = date.fromisoformat(args.as_of) if args.as_of else date.today()
        conn = db.connect(args.db, create=False)
        readonly = args.dry_run or args.near_misses is not None or args.show
        if readonly:
            if db.current_version(conn) < db.load_migrations()[-1][0]:
                sys.exit("sentinel_tier1.py: research.db has not been upgraded for Tier 1 yet. Run the calendar or the scanner once, "
                         "or run `python3 sentinel_tier1.py` without --dry-run to upgrade it.")
        else:
            db.init_schema(conn)

        if args.show:
            return cmd_show(conn)
        if args.near_misses is not None:
            return cmd_near_misses(conn, today, args.near_misses)
        if args.mark:
            ok = db.set_escalation_status(conn, int(args.mark[0]), args.mark[1], args.note)
            print(f"Escalation #{args.mark[0]} marked {args.mark[1]}." if ok else f"No escalation with id {args.mark[0]}.")
            return

        rules, meta, warnings = load_rules(args.rules)
        RULES_LOOKBACK[0] = rules["funnel_a"]["lookback_days"]
        version = rules_version(meta)
        tracked = tracked_names(args.positions)
        a = funnel_a(conn, today, rules, tracked)
        b = funnel_b(conn, today, rules)
        n_new, run_id = store_results(conn, today, version, a, b, store=not args.dry_run)
        print(format_report(today, meta, version, a, b, args.dry_run, warnings))
        if not args.dry_run:
            print(f"\nStored Tier 1 run #{run_id}: {n_new} new escalation(s). Open ones: python3 sentinel_tier1.py --show")
    except (FileNotFoundError, RulesError, ValueError, sqlite3.Error, db.SchemaError) as e:
        sys.exit(f"sentinel_tier1.py: {e}")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()

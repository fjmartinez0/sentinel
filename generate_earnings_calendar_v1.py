"""
Earnings calendar with position-aware decision cards.

What changed vs the first version:
  * Holdings and the watch list come from positions.yaml, not a hardcoded dict.
  * Click a held name and you get a card: the print-day test (gate), position and
    P&L, target and resting GTC order, ledger date vs FMP date, consensus, and
    anything in the ledger that looks inconsistent.
  * "Recently reported" applies your locked Funnel A rule (EPS or revenue surprise
    of 10% or more) to the last 30 days of prints.
  * Ledger-only gates (Analyst Day, trial window...) appear on the calendar too, and
    are flagged once they pass without a recorded outcome.

Setup:
  export FMP_API_KEY="your-key-here"
  pip3 install requests pandas lxml pyyaml

Run:
  python3 generate_earnings_calendar.py                 # normal
  python3 generate_earnings_calendar.py --no-quotes     # skip live prices (ledger prices only)
  python3 generate_earnings_calendar.py --share         # S&P 500 only, NO positions data
  python3 generate_earnings_calendar.py --offline       # fixture data, no network, no API key

Output:
  earnings_calendar.html -- contains cost basis and share counts. Local use only.
  The API key is never written into the HTML.
"""

import argparse
import html
import io
import json
import math
import os
import sys
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:
    raise SystemExit("Missing dependency. Run: pip3 install pyyaml")

BASE = "https://financialmodelingprep.com"

# Locked in the decisions log (item 8). Surprise % on a tiny EPS base is noisy
# (0.05 -> 0.10 is +100%), which is why this only flags for a human/Tier 2 look.
SURPRISE_THRESHOLD_PCT = 10.0
LOOKBACK_DAYS = 30          # how far back we ask FMP, for the "recently reported" list
STALE_REVIEW_DAYS = 21      # a position's last_reviewed older than this gets a note
STALE_LEDGER_DAYS = 14      # positions.yaml as_of older than this gets a banner
GTC_TOLERANCE = 0.005       # 0.5%: GTC price vs target counts as "matching"
CURRENCY_SYMBOL = {"USD": "$", "CAD": "C$"}


# ---------------------------------------------------------------- formatting

def iso(v):
    """YAML turns unquoted 2026-08-28 into a date object; normalise to a string."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def to_float(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def money(v, cur="USD"):
    if v is None:
        return "n/a"
    sym = CURRENCY_SYMBOL.get(cur, cur + " ")
    return f"{sym}{v:,.0f}" if float(v).is_integer() else f"{sym}{v:,.2f}"


def fmt_target(t, cur):
    if t is None:
        return None
    if isinstance(t, (list, tuple)):
        return f"{money(t[0], cur)} to {money(t[1], cur)}"
    return money(t, cur)


def target_ref(t):
    """The number we measure upside against: the point target, or the low end of a range."""
    if t is None:
        return None
    return t[0] if isinstance(t, (list, tuple)) else t


def human(v):
    if v is None:
        return "n/a"
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.0f}M"
    return f"{v:,.2f}"


def surprise_pct(actual, est):
    if actual is None or est is None or est == 0:
        return None
    return (actual - est) / abs(est) * 100


def days_from(d_iso, today):
    return (date.fromisoformat(d_iso) - today).days


def when_text(n):
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    return f"in {n} days" if n > 0 else f"{-n} days ago"


def short_date(d_iso):
    d = date.fromisoformat(d_iso)
    return f"{d.strftime('%b')} {d.day}"


# ------------------------------------------------------------- positions.yaml

def load_positions(path):
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    out = {
        "as_of": iso(raw.get("as_of")),
        "held": [],
        "watch": [],
        "needs_confirmation": raw.get("needs_confirmation") or [],
    }
    problems, seen = [], set()

    for kind in ("held", "watch"):
        for p in raw.get(kind) or []:
            p = dict(p)
            p["kind"] = kind
            tk = str(p.get("ticker", "")).strip().upper()
            if not tk:
                problems.append(f"a {kind} entry has no ticker")
                continue
            if tk in seen:
                problems.append(f"{tk} appears more than once")
            seen.add(tk)
            p["ticker"] = tk
            p["name"] = p.get("name") or tk
            e = dict(p.get("earnings") or {})
            e["date"] = iso(e.get("date"))
            e["month"] = str(e["month"]) if e.get("month") else None
            p["earnings"] = e
            p["last_price_date"] = iso(p.get("last_price_date"))
            p["last_reviewed"] = iso(p.get("last_reviewed"))
            evs = []
            for ev in p.get("events") or []:
                ev = dict(ev)
                ev["date"] = iso(ev.get("date"))
                ev["until"] = iso(ev.get("until"))
                ev["month"] = str(ev["month"]) if ev.get("month") else None
                evs.append(ev)
            p["events"] = evs
            if kind == "held":
                for k in ("shares", "avg_cost", "currency"):
                    if p.get(k) is None:
                        problems.append(f"{tk}: held entry is missing '{k}'")
            out[kind].append(p)

    if problems:
        raise SystemExit("positions.yaml problems:\n  - " + "\n  - ".join(problems))
    return out


# --------------------------------------------------------------- data sources

def fmp_key():
    key = os.environ.get("FMP_API_KEY")
    if not key:
        raise SystemExit("Set the FMP_API_KEY environment variable first (or use --offline).")
    return key


def fetch_sp500_constituents():
    """Free, public source -- FMP's own constituent endpoint requires a paid plan."""
    import pandas as pd
    import requests
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()
    df = pd.read_html(io.StringIO(resp.text))[0]
    return dict(zip(df["Symbol"], df["Security"]))


def fetch_earnings_calendar(key, from_date, to_date):
    import requests
    resp = requests.get(
        f"{BASE}/stable/earnings-calendar",
        params={"from": from_date, "to": to_date, "apikey": key},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_quotes(key, symbols):
    """Live prices for held names. Free-tier coverage is UNTESTED (per the FMP lesson in
    the handoff), including .TO tickers. Anything that fails falls back to the ledger
    price, and the printout below tells you which endpoints your plan actually allows."""
    import requests
    out, errs, denied = {}, [], 0
    for s in symbols:
        try:
            r = requests.get(f"{BASE}/stable/quote", params={"symbol": s, "apikey": key}, timeout=15)
            if r.status_code in (402, 403):
                denied += 1
                errs.append((s, f"HTTP {r.status_code}"))
                if denied >= 3 and not out:
                    print("  Quote endpoint looks unavailable on this plan; using ledger prices.")
                    break
                continue
            r.raise_for_status()
            data = r.json()
            q = data[0] if isinstance(data, list) and data else None
            price = to_float(q.get("price")) if q else None
            if price is None:
                errs.append((s, "no data"))
                continue
            chg = q.get("changePercentage", q.get("changesPercentage"))
            out[s] = {"price": price, "change_pct": to_float(chg)}
        except Exception as e:  # noqa: BLE001 -- a bad quote must never kill the calendar
            errs.append((s, str(e)[:60]))
    return out, errs


def end_of_month_two_ahead(today):
    """Last calendar day of (current month + 2), so 3 full months are covered."""
    month, year = today.month + 2, today.year
    while month > 12:
        month -= 12
        year += 1
    return date(year, month, monthrange(year, month)[1])


# Offline fixtures: NOT real FMP data. They exist so the page can be previewed and
# tested without a key or a network. The page carries a visible banner when used.
DEMO_SP500 = {"AAPL": "Apple", "MSFT": "Microsoft", "NKE": "Nike", "COST": "Costco",
              "ORCL": "Oracle", "FDX": "FedEx", "ADBE": "Adobe", "MU": "Micron"}


def demo_calendar(today):
    y = today.year
    rows = [
        {"symbol": "COST", "date": f"{y}-09-24", "epsEstimated": 5.10, "revenueEstimated": 8.6e10},
        {"symbol": "NKE", "date": f"{y}-09-25", "epsEstimated": 0.62, "revenueEstimated": 1.15e10},
        {"symbol": "MU", "date": f"{y}-09-29", "epsEstimated": 2.80, "revenueEstimated": 9.2e9},
        {"symbol": "ASML", "date": f"{y}-10-14", "epsEstimated": 6.90, "revenueEstimated": 8.1e9, "confirmed": True},
        {"symbol": "LBRT", "date": f"{y}-10-21", "epsEstimated": 0.18, "revenueEstimated": 1.0e9},
        {"symbol": "QTWO", "date": f"{y}-10-29", "epsEstimated": 0.85, "revenueEstimated": 2.1e8},
        {"symbol": "META", "date": f"{y}-10-29", "epsEstimated": 6.20, "revenueEstimated": 5.9e10},
        {"symbol": "SPGI", "date": f"{y}-10-30", "epsEstimated": 4.40, "revenueEstimated": 3.7e9},
        {"symbol": "PSN", "date": f"{y}-11-04", "epsEstimated": 0.55, "revenueEstimated": 1.6e9},
        # ledger says Nov 4 (confirmed); FMP disagrees by a day -> should raise a check
        {"symbol": "DUOL", "date": f"{y}-11-05", "epsEstimated": 0.60, "revenueEstimated": 2.6e8},
        {"symbol": "HUBS", "date": f"{y}-11-05", "epsEstimated": 2.35, "revenueEstimated": 8.0e8},
        {"symbol": "UBER", "date": f"{y}-11-05", "epsEstimated": 0.70, "revenueEstimated": 1.4e10},
        {"symbol": "NYT", "date": f"{y}-11-05", "epsEstimated": 0.62, "revenueEstimated": 6.9e8},
        # a held name that reported recently, with a >= 10% surprise
        {"symbol": "ALB", "date": (today - timedelta(days=8)).isoformat(),
         "epsEstimated": 0.80, "epsActual": 0.95, "revenueEstimated": 1.4e9, "revenueActual": 1.43e9},
        # a watch name that reported recently, inside the threshold
        {"symbol": "APP", "date": (today - timedelta(days=12)).isoformat(),
         "epsEstimated": 2.10, "epsActual": 2.14, "revenueEstimated": 1.9e9, "revenueActual": 1.92e9},
    ]
    # ATRL.TO / EFN.TO deliberately absent, to exercise the "no FMP date" list.
    return rows


DEMO_QUOTES = {
    "DUOL": {"price": 132.10, "change_pct": -1.2},
    "META": {"price": 611.20, "change_pct": 0.8},
    "LBRT": {"price": 21.05, "change_pct": 2.1},
}


# ------------------------------------------------------------------- analysis

def position_flags(pos, quote, today):
    """Checks that belong to the POSITION (independent of which earnings row we show)."""
    flags = []
    cur = pos.get("currency", "USD")
    if pos["kind"] == "held":
        price = quote["price"] if quote else pos.get("last_price")
        if price is None:
            flags.append(("warn", "No price on file, so P&L and distance to target are unknown."))

        gtc, shares, tgt = pos.get("gtc"), float(pos["shares"]), pos.get("target")
        whole = math.floor(shares + 1e-9)
        if gtc is None:
            if abs(shares - round(shares)) > 1e-9:
                flags.append(("warn", "No resting exit order: fractional shares can't carry a GTC here, so this exit is manual."))
            else:
                flags.append(("warn", "No resting GTC order on file."))
        else:
            gp = float(gtc["price"])
            if isinstance(tgt, (list, tuple)):
                lo, hi = tgt
                if gp < lo * (1 - GTC_TOLERANCE) or gp > hi * (1 + GTC_TOLERANCE):
                    flags.append(("warn", f"GTC at {money(gp, cur)} sits outside the ledger target range ({fmt_target(tgt, cur)})."))
            elif tgt:
                if abs(gp - tgt) / tgt > GTC_TOLERANCE:
                    flags.append(("warn", f"GTC at {money(gp, cur)} differs from target {money(tgt, cur)} ({(gp / tgt - 1) * 100:+.1f}%)."))
            if float(gtc["shares"]) < whole - 1e-9:
                flags.append(("warn", f"GTC covers {gtc['shares']:g} of {whole} whole shares."))

        if not pos.get("gate"):
            flags.append(("info", "No print-day test written yet. Add a gate to positions.yaml before this reports."))

    if pos.get("last_reviewed") is None:
        flags.append(("info", "No review date on file."))
    return flags


def date_flag(pos, next_ev):
    """Ledger date vs FMP date. FMP's own 'confirmed' field is not proof (see handoff
    section 4), so a disagreement means: go check the company's IR page."""
    if next_ev is None:
        return None
    e, fmp = pos["earnings"], next_ev["date"]
    if e.get("date") and fmp != e["date"]:
        return ("warn", f"Ledger has {e['date']} ({e.get('confidence', 'unstated')}); FMP has {fmp}. "
                        "Re-verify against the company IR page before acting.")
    if not e.get("date") and e.get("month") and fmp[:7] != e["month"]:
        return ("warn", f"Ledger expects {e['month']}; FMP has {fmp}. Re-verify against the IR page.")
    return None


def build_card(pos, ev, next_ev, quote, today):
    cur = pos.get("currency", "USD")
    rows, flags = [], []

    if pos["kind"] == "held":
        price = quote["price"] if quote else pos.get("last_price")
        if quote:
            src = "live" + (f", {quote['change_pct']:+.1f}% today" if quote.get("change_pct") is not None else "")
        elif price is not None:
            note = pos.get("last_price_note")
            src = f"ledger, {short_date(pos['last_price_date'])}" + (f", {note}" if note else "") if pos.get("last_price_date") else "ledger"
        else:
            src = None

        rows.append(["Position", f"{pos['shares']:g} sh at {money(pos['avg_cost'], cur)}, {pos['account']}"])
        rows.append(["Price", f"{money(price, cur)} ({src})" if price is not None else "not on file"])
        if price is not None:
            pnl = (price / pos["avg_cost"] - 1) * 100
            rows.append(["Value / P&L", f"{money(price * pos['shares'], cur)}, {pnl:+.1f}%"])
        tgt = pos.get("target")
        if tgt is not None:
            line = fmt_target(tgt, cur)
            ref = target_ref(tgt)
            if price is not None and ref:
                line += f", {(ref / price - 1) * 100:+.1f}% from price" + (" (to low end)" if isinstance(tgt, (list, tuple)) else "")
            if pos.get("stretch"):
                line += f"; stretch {fmt_target(pos['stretch'], cur)}"
            rows.append(["Target", line])
        gtc = pos.get("gtc")
        rows.append(["Resting exit", f"GTC {money(gtc['price'], cur)} x {gtc['shares']:g}" if gtc else "none"])
        if pos.get("gtc_note"):
            rows.append(["Order note", pos["gtc_note"]])
    else:
        rows.append(["Status", pos.get("status") or "Watch"])

    e = pos["earnings"]
    led = e.get("date") or e.get("month") or e.get("note") or "no date on file"
    if e.get("note") and (e.get("date") or e.get("month")):
        led += f" ({e['note']})"
    rows.append(["Ledger date", f"{led}, {e.get('confidence', 'unstated')}"])
    rows.append(["FMP date", f"{ev['date']}, {'confirmed' if ev['confirmed'] else 'estimated'} (not proof)"])

    if ev["eps_est"] is not None or ev["rev_est"] is not None:
        rows.append(["Consensus", est_text(ev["eps_est"], ev["rev_est"])])
    s_eps, s_rev = surprise_pct(ev["eps_act"], ev["eps_est"]), surprise_pct(ev["rev_act"], ev["rev_est"])
    if ev["eps_act"] is not None or ev["rev_act"] is not None:
        rows.append(["Result", result_text(ev)])
        hit = [abs(s) >= SURPRISE_THRESHOLD_PCT for s in (s_eps, s_rev) if s is not None]
        if any(hit):
            flags.append(("warn", f"Surprise at or above {SURPRISE_THRESHOLD_PCT:.0f}%: escalate to Tier 2 under the locked rule."))
        else:
            flags.append(("info", f"Surprise below {SURPRISE_THRESHOLD_PCT:.0f}%: log only."))

    if pos.get("last_reviewed"):
        age = (today - date.fromisoformat(pos["last_reviewed"])).days
        rows.append(["Last reviewed", f"{pos['last_reviewed']}, {age} days ago" + (" (stale)" if age > STALE_REVIEW_DAYS else "")])

    if ev is next_ev:
        df = date_flag(pos, next_ev)
        if df:
            flags.append(df)
    flags += position_flags(pos, quote, today)

    return {
        "gate": pos.get("gate"),
        "rows": rows,
        "flags": [{"level": lv, "text": tx} for lv, tx in flags],
        "open_items": pos.get("open_items") or [],
    }


def est_text(eps, rev):
    parts = []
    if eps is not None:
        parts.append(f"EPS est. {eps:.2f}")
    if rev is not None:
        parts.append(f"revenue est. {human(rev)}")
    return ", ".join(parts)


def result_text(ev):
    parts = []
    s = surprise_pct(ev["eps_act"], ev["eps_est"])
    if ev["eps_act"] is not None:
        parts.append(f"EPS {ev['eps_act']:.2f}" + (f" vs {ev['eps_est']:.2f} est. ({s:+.1f}%)" if s is not None else ""))
    s = surprise_pct(ev["rev_act"], ev["rev_est"])
    if ev["rev_act"] is not None:
        parts.append(f"revenue {human(ev['rev_act'])}" + (f" vs {human(ev['rev_est'])} est. ({s:+.1f}%)" if s is not None else ""))
    return ", ".join(parts)


def build(today, positions, sp500, cal_rows, quotes):
    held = {p["ticker"]: p for p in positions["held"]}
    watch = {p["ticker"]: p for p in positions["watch"]}
    grid_start, grid_end = today.replace(day=1), end_of_month_two_ahead(today)
    today_iso = today.isoformat()

    # 1. normalise FMP rows we care about
    raw, seen, by_symbol = [], set(), defaultdict(list)
    for row in cal_rows:
        sym = row.get("symbol")
        kind = "held" if sym in held else "watch" if sym in watch else "sp500" if sym in sp500 else None
        d = str(row.get("date") or "")[:10]
        if not kind or len(d) != 10 or (sym, d) in seen:
            continue
        seen.add((sym, d))
        ev = {"symbol": sym, "kind": kind, "date": d, "time": row.get("time") or "",
              "confirmed": bool(row.get("confirmed")),
              "eps_est": to_float(row.get("epsEstimated")), "eps_act": to_float(row.get("epsActual")),
              "rev_est": to_float(row.get("revenueEstimated")), "rev_act": to_float(row.get("revenueActual"))}
        raw.append(ev)
        if kind != "sp500":
            by_symbol[sym].append(ev)
    next_ev = {}
    for sym, evs in by_symbol.items():
        evs.sort(key=lambda e: e["date"])
        next_ev[sym] = next((e for e in evs if e["date"] >= today_iso), None)

    # 2. calendar items (earnings rows inside the 3 visible months)
    items = []
    for ev in raw:
        if not (grid_start.isoformat() <= ev["date"] <= grid_end.isoformat()):
            continue
        pos = held.get(ev["symbol"]) or watch.get(ev["symbol"])
        items.append({
            "type": "earnings", "kind": ev["kind"], "symbol": ev["symbol"],
            "name": pos["name"] if pos else sp500.get(ev["symbol"], ev["symbol"]),
            "date": ev["date"], "time": ev["time"], "confirmed": ev["confirmed"],
            "est_text": est_text(ev["eps_est"], ev["rev_est"]),
            "result_text": result_text(ev) if (ev["eps_act"] is not None or ev["rev_act"] is not None) else "",
            "card": build_card(pos, ev, next_ev.get(ev["symbol"]), quotes.get(ev["symbol"]), today) if pos else None,
        })

    # 3. ledger-only gates
    offgrid_gates, checks = [], []
    for pos in positions["held"] + positions["watch"]:
        for g in pos["events"]:
            end = g.get("until") or g.get("date")
            passed = bool(end) and end < today_iso
            item = {"type": "gate", "kind": pos["kind"], "symbol": pos["ticker"], "name": pos["name"],
                    "date": g.get("date"), "until": g.get("until"), "label": g.get("label", ""),
                    "outcome": g.get("outcome"), "passed": passed, "card": None}
            in_grid = g.get("date") and grid_start.isoformat() <= g["date"] <= grid_end.isoformat()
            if in_grid:
                items.append(item)
            else:
                offgrid_gates.append(item)
            if passed and not g.get("outcome"):
                checks.append({"level": "warn", "ticker": pos["ticker"],
                               "text": f"Gate passed without a recorded outcome: {g.get('label', '')} ({g['date']}"
                                       + (f" to {g['until']}" if g.get("until") else "") + "). Fill in `outcome` in positions.yaml."})

    # 4. checks that live on the position or the ledger itself
    for nc in positions["needs_confirmation"]:
        checks.append({"level": "warn", "ticker": nc.get("ticker", ""), "text": " ".join(str(nc.get("issue", "")).split())})
    for pos in positions["held"] + positions["watch"]:
        q = quotes.get(pos["ticker"])
        for lv, tx in position_flags(pos, q, today):
            if lv == "warn":
                checks.append({"level": lv, "ticker": pos["ticker"], "text": tx})
        df = date_flag(pos, next_ev.get(pos["ticker"]))
        if df:
            checks.append({"level": df[0], "ticker": pos["ticker"], "text": df[1]})

    # 5. recent prints for tracked names (Funnel A, Tier 1 rule)
    recent = []
    for ev in sorted(raw, key=lambda e: e["date"], reverse=True):
        if ev["kind"] == "sp500" or not (today_iso >= ev["date"] >= (today - timedelta(days=LOOKBACK_DAYS)).isoformat()):
            continue
        if ev["eps_act"] is None and ev["rev_act"] is None:
            continue
        pos = held.get(ev["symbol"]) or watch.get(ev["symbol"])
        se, sr = surprise_pct(ev["eps_act"], ev["eps_est"]), surprise_pct(ev["rev_act"], ev["rev_est"])
        recent.append({"symbol": ev["symbol"], "name": pos["name"], "kind": ev["kind"], "date": ev["date"],
                       "eps_text": (f"{ev['eps_act']:.2f} vs {ev['eps_est']:.2f}" if ev["eps_act"] is not None and ev["eps_est"] is not None else "n/a"),
                       "eps_s": se, "rev_s": sr,
                       "hit": any(abs(s) >= SURPRISE_THRESHOLD_PCT for s in (se, sr) if s is not None)})
        if recent[-1]["hit"]:
            checks.append({"level": "warn", "ticker": ev["symbol"],
                           "text": f"Reported {ev['date']} with a surprise at or above {SURPRISE_THRESHOLD_PCT:.0f}% ({result_text(ev)}). Tier 2 candidate."})

    # 6. tracked names with an expected date but no upcoming FMP row
    no_date = []
    for pos in positions["held"] + positions["watch"]:
        e = pos["earnings"]
        if next_ev.get(pos["ticker"]) is not None:
            continue
        if pos["kind"] == "held" or e.get("date") or e.get("month"):
            no_date.append({"symbol": pos["ticker"], "name": pos["name"], "kind": pos["kind"],
                            "ledger": (e.get("date") or e.get("month") or "no date on file")
                                      + (f" ({e['note']})" if e.get("note") else ""),
                            "confidence": e.get("confidence", "unstated")})

    checks.sort(key=lambda c: (c["ticker"] == "", c["ticker"]))
    return {"items": items, "offgrid_gates": offgrid_gates, "checks": checks,
            "recent": recent, "no_date": no_date, "grid_end": grid_end.isoformat()}


# ------------------------------------------------------------- static HTML parts

def esc(s):
    return html.escape(str(s), quote=True)


def render_static(today, positions, data, meta):
    e = esc
    out_top, out_bottom = [], []

    # banners
    banners = []
    if meta["offline"]:
        banners.append('<div class="banner bad">Offline demo. Every date, price and estimate on this page is fixture data, not FMP.</div>')
    if meta["share"]:
        banners.append('<div class="banner">Share mode: S&amp;P 500 only. No positions, gates or prices are included.</div>')
    else:
        banners.append('<div class="banner">Contains cost basis and share counts. Local use only; generate with <code>--share</code> to send it to anyone.</div>')
        if positions["as_of"]:
            age = (today - date.fromisoformat(positions["as_of"])).days
            passed = sum(1 for c in data["checks"] if c["text"].startswith("Gate passed"))
            if age > STALE_LEDGER_DAYS:
                extra = f" {passed} gate(s) have passed with no recorded outcome." if passed else ""
                banners.append(f'<div class="banner warn">positions.yaml is as of {e(positions["as_of"])}, {age} days old. '
                               f'Prices, dates and gates below may have moved.{e(extra)}</div>')
    out_top.append('<div class="banners">' + "".join(banners) + "</div>")

    # legend
    out_top.append('<div class="legend">'
                   '<span><span class="dot accent"></span> S&amp;P 500</span>'
                   + ('' if meta["share"] else
                      '<span><span class="dot held"></span> Your holding</span>'
                      '<span><span class="dot watch"></span> Watch list or ledger gate</span>')
                   + '</div>')

    if not meta["share"]:
        # checks
        n_warn = sum(1 for c in data["checks"] if c["level"] == "warn")
        lis = "".join(f'<li><span class="tk">{e(c["ticker"])}</span>{e(c["text"])}</li>' for c in data["checks"])
        out_top.append(f'<details class="checks"><summary>Ledger checks ({n_warn})</summary>'
                       f'<ul>{lis or "<li>Nothing to check.</li>"}</ul></details>')

        # upcoming chips
        up = [i for i in data["items"] if i["kind"] in ("held", "watch") and i["date"] >= today.isoformat()]
        up.sort(key=lambda i: (i["date"], 0 if i["kind"] == "held" else 1, i["symbol"]))
        chips = []
        for i in up[:8]:
            n = days_from(i["date"], today)
            label = i["label"] if i["type"] == "gate" else "Earnings"
            chips.append(f'<button class="chip {i["kind"] if i["type"] == "earnings" else "watch"}" data-goto="{e(i["date"])}" title="{e(label)}">'
                         f'<span class="c-sym">{e(i["symbol"])}</span>'
                         f'<span class="c-what">{e("Gate" if i["type"] == "gate" else "Earnings")}</span>'
                         f'<span class="c-when">{e(short_date(i["date"]))}, {e(when_text(n))}</span></button>')
        out_top.append('<section class="strip"><h2>Coming up for your names</h2>'
                       + ('<div class="chips">' + "".join(chips) + '</div>' if chips else '<div class="empty">No held or watch-list dates in the next three months from FMP or the ledger.</div>')
                       + '</section>')

        # recent prints
        if data["recent"]:
            trs = ""
            for r in data["recent"]:
                fs = lambda v: "n/a" if v is None else f"{v:+.1f}%"  # noqa: E731
                trs += (f'<tr class="{"hit" if r["hit"] else ""}"><td><span class="symbol">{e(r["symbol"])}</span> <span class="name">{e(r["name"])}</span></td>'
                        f'<td>{e(short_date(r["date"]))}</td><td>{e(r["eps_text"])}</td><td>{fs(r["eps_s"])}</td><td>{fs(r["rev_s"])}</td>'
                        f'<td>{"At or above " + str(int(SURPRISE_THRESHOLD_PCT)) + "%: Tier 2 candidate" if r["hit"] else "Below threshold: log only"}</td></tr>')
            body = ('<div class="scroll"><table><thead><tr><th>Name</th><th>Date</th><th>EPS vs est.</th><th>EPS surprise</th>'
                    f'<th>Revenue surprise</th><th>Rule ({int(SURPRISE_THRESHOLD_PCT)}% on EPS or revenue)</th></tr></thead><tbody>{trs}</tbody></table></div>')
        else:
            body = f'<div class="empty">No held or watch-list name has reported with actuals in the last {LOOKBACK_DAYS} days.</div>'
        out_bottom.append(f'<section class="strip"><h2>Recently reported</h2>{body}</section>')

        # off-grid
        parts = []
        if data["no_date"]:
            lis = "".join(f'<li><span class="tk">{e(n["symbol"])}</span>{e(n["name"])}: ledger says {e(n["ledger"])}, {e(n["confidence"])}. No upcoming FMP row.</li>'
                          for n in data["no_date"])
            parts.append(f'<h3>Expected, but FMP has no date</h3><ul>{lis}</ul>')
        if data["offgrid_gates"]:
            lis = ""
            for g in data["offgrid_gates"]:
                when = g["date"] or "no date"
                lis += f'<li><span class="tk">{e(g["symbol"])}</span>{e(g["label"])} ({e(when)})</li>'
            parts.append(f'<h3>Ledger gates outside this view</h3><ul>{lis}</ul>')
        if parts:
            out_bottom.append('<section class="strip offgrid"><h2>Not on the calendar</h2>' + "".join(parts) + '</section>')

    return "".join(out_top), "".join(out_bottom)


# ------------------------------------------------------------------ HTML shell

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Earnings Calendar</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --ink: #14213D; --paper: #FBFAF7; --rule: #DAD6CC; --accent: #9C6B1F;
    --held: #1D4E89; --watch: #6E4A78; --confirmed: #2F6D4F; --estimated: #8B8676;
    --warn: #9B3D2F; --warn-bg: #F7ECE8; --held-bg: #EEF3FA; --watch-bg: #F4EFF6;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--paper); color: var(--ink); font-family: 'Inter', sans-serif; padding: 32px 16px 80px; }
  .wrap { max-width: 980px; margin: 0 auto; }
  h1 { font-family: 'Source Serif 4', serif; font-size: 26px; font-weight: 700; margin: 0 0 4px; }
  h2 { font-family: 'Source Serif 4', serif; font-size: 18px; margin: 0 0 12px; }
  h3 { font-size: 13px; font-weight: 600; margin: 14px 0 6px; }
  code { font-size: 12px; background: #EFEBE0; padding: 1px 5px; border-radius: 3px; }
  .sub { color: var(--estimated); font-size: 13px; margin-bottom: 12px; }
  .banners { display: grid; gap: 6px; margin-bottom: 12px; }
  .banner { font-size: 12px; padding: 7px 10px; border: 1px solid var(--rule); border-radius: 4px; color: #5A5648; }
  .banner.warn, .banner.bad { border-color: var(--warn); background: var(--warn-bg); color: var(--warn); }
  .legend { display: flex; flex-wrap: wrap; gap: 18px; font-size: 12px; color: var(--estimated); margin-bottom: 18px; }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
  .dot.accent { background: var(--accent); } .dot.held { background: var(--held); } .dot.watch { background: var(--watch); }

  details.checks { border: 1px solid var(--rule); border-radius: 4px; margin-bottom: 22px; }
  details.checks summary { cursor: pointer; padding: 9px 12px; font-size: 13px; font-weight: 600; }
  details.checks ul, .offgrid ul { margin: 0; padding: 0 12px 10px; list-style: none; font-size: 13px; }
  details.checks li, .offgrid li { padding: 6px 0; border-top: 1px solid var(--rule); line-height: 1.45; }
  .tk { display: inline-block; min-width: 64px; font-weight: 600; margin-right: 8px; }

  .strip { margin-bottom: 26px; }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; }
  .chip { font: inherit; text-align: left; cursor: pointer; background: #fff; border: 1px solid var(--rule); border-left-width: 4px; border-radius: 4px; padding: 7px 11px; display: grid; gap: 1px; }
  .chip.held { border-left-color: var(--held); } .chip.watch { border-left-color: var(--watch); }
  .chip:hover { background: #F1EEE5; }
  .chip:focus-visible, .day.has-events:focus-visible, summary:focus-visible { outline: 2px solid var(--held); outline-offset: 2px; }
  .c-sym { font-weight: 600; font-size: 13px; } .c-what, .c-when { font-size: 12px; color: var(--estimated); }
  .empty { color: var(--estimated); font-size: 14px; padding: 12px 0; }

  .months { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 28px; }
  @media (max-width: 720px) { .months { grid-template-columns: 1fr; } }
  .month-title { font-family: 'Source Serif 4', serif; font-size: 15px; font-weight: 600; text-align: center; margin-bottom: 8px; }
  .grid { display: grid; grid-template-columns: repeat(7, 1fr); border-top: 1px solid var(--rule); border-left: 1px solid var(--rule); }
  .dow { font-size: 10px; color: var(--estimated); text-align: center; padding: 4px 0; border-right: 1px solid var(--rule); border-bottom: 1px solid var(--rule); }
  .day { min-height: 42px; border-right: 1px solid var(--rule); border-bottom: 1px solid var(--rule); padding: 3px 4px; position: relative; }
  .day.has-events { cursor: pointer; } .day.has-events:hover { background: #F1EEE5; }
  .day.selected { background: #EFE6D3; } .day.outside { color: #C9C5B8; }
  .day.today .daynum { color: var(--accent); font-weight: 700; }
  .daynum { font-size: 11px; font-weight: 500; }
  .badge { display: inline-block; margin-top: 3px; font-size: 9px; background: var(--accent); color: #fff; border-radius: 8px; padding: 0 5px; }
  .badge.badge-held { background: var(--held); } .badge.badge-watch { background: var(--watch); }

  .panel { margin-top: 12px; border-top: 2px solid var(--ink); padding-top: 16px; margin-bottom: 30px; }
  .row { border-bottom: 1px solid var(--rule); padding: 0; }
  .row-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; padding: 10px 0; list-style: none; }
  .row-head::-webkit-details-marker { display: none; }
  details.row > summary { cursor: pointer; }
  .row.held { background: var(--held-bg); margin: 0 -8px; padding: 0 8px; }
  .row.watch { background: var(--watch-bg); margin: 0 -8px; padding: 0 8px; }
  .symbol { font-weight: 600; } .name { font-size: 13px; color: var(--estimated); margin-left: 8px; }
  .tag { font-size: 10px; color: #fff; border-radius: 8px; padding: 1px 7px; margin-left: 8px; }
  .tag.held { background: var(--held); } .tag.watch { background: var(--watch); } .tag.gate { background: var(--watch); }
  .status { font-size: 12px; text-align: right; } .status.confirmed { color: var(--confirmed); } .status.estimated { color: var(--estimated); }
  .status.passed { color: var(--warn); }
  .sub2 { font-size: 12px; color: var(--estimated); padding: 0 0 10px; }
  .card { padding: 4px 0 16px; display: grid; gap: 12px; font-size: 13px; }
  .gate-box { border-left: 3px solid var(--held); background: #fff; padding: 8px 12px; line-height: 1.5; }
  .gate-box.none { border-left-color: var(--rule); color: var(--estimated); }
  .gate-box .k { display: block; font-size: 11px; color: var(--estimated); margin-bottom: 2px; }
  .kv { display: grid; grid-template-columns: 110px 1fr; gap: 4px 12px; margin: 0; }
  .kv dt { color: var(--estimated); } .kv dd { margin: 0; }
  .flags { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
  .flags li { padding: 5px 9px; border-radius: 3px; line-height: 1.4; background: #fff; border: 1px solid var(--rule); }
  .flags li.warn { background: var(--warn-bg); border-color: var(--warn); color: var(--warn); }
  .flags b { font-weight: 600; margin-right: 6px; }
  .open ul { margin: 4px 0 0; padding-left: 18px; } .open .k { color: var(--estimated); font-size: 11px; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-weight: 500; color: var(--estimated); font-size: 12px; padding: 6px 8px; border-bottom: 1px solid var(--ink); }
  td { padding: 8px; border-bottom: 1px solid var(--rule); }
  tr.hit td { background: var(--warn-bg); }
  .scroll { overflow-x: auto; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Earnings Calendar</h1>
  <div class="sub">__SUB__</div>
  __TOP__
  <div class="months" id="monthsContainer"></div>
  <div class="panel" id="panel">
    <h2 id="panelTitle">Select a date</h2>
    <div id="panelBody" class="empty">Click a highlighted day to see who is reporting.</div>
  </div>
  __BOTTOM__
</div>
<script>
const DATA = __DATA__;
const byDate = {};
DATA.items.forEach(function (it) { if (it.date) (byDate[it.date] = byDate[it.date] || []).push(it); });

const panelTitle = document.getElementById('panelTitle');
const panelBody = document.getElementById('panelBody');
const ORDER = { held: 0, watch: 1, sp500: 2 };

function pad(n) { return (n < 10 ? '0' : '') + n; }
function keyOf(y, m, d) { return y + '-' + pad(m + 1) + '-' + pad(d); }   // local date, no UTC shift
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function renderMonth(offset, titleEl, gridEl, base) {
  const view = new Date(base.getFullYear(), base.getMonth() + offset, 1);
  titleEl.textContent = view.toLocaleString('en-US', { month: 'long', year: 'numeric' });
  ['S', 'M', 'T', 'W', 'T', 'F', 'S'].forEach(function (d) { gridEl.appendChild(el('div', 'dow', d)); });

  const year = view.getFullYear(), month = view.getMonth();
  const firstDow = new Date(year, month, 1).getDay();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const total = Math.ceil((firstDow + daysInMonth) / 7) * 7;

  for (let i = 0; i < total; i++) {
    const cell = el('div', 'day');
    const dayNum = i - firstDow + 1;
    const outside = dayNum < 1 || dayNum > daysInMonth;
    const cd = new Date(year, month, dayNum);
    const key = keyOf(cd.getFullYear(), cd.getMonth(), cd.getDate());
    const evs = outside ? null : byDate[key];   // only the month's own days carry events
    cell.dataset.key = key;
    cell.className = 'day' + (outside ? ' outside' : '') + (evs ? ' has-events' : '') + (key === DATA.today ? ' today' : '');
    cell.appendChild(el('div', 'daynum', String(cd.getDate())));
    if (evs) {
      const rank = Math.min.apply(null, evs.map(function (e) { return ORDER[e.kind]; }));
      const b = el('div', 'badge' + (rank === 0 ? ' badge-held' : rank === 1 ? ' badge-watch' : ''), String(evs.length));
      cell.appendChild(b);
      cell.tabIndex = 0;
      cell.addEventListener('click', function () { selectDay(key, cell); });
      cell.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectDay(key, cell); } });
    }
    gridEl.appendChild(cell);
  }
}

function renderCard(card) {
  const box = el('div', 'card');
  const gate = el('div', 'gate-box' + (card.gate ? '' : ' none'));
  gate.appendChild(el('span', 'k', 'Decision test for this print'));
  gate.appendChild(document.createTextNode(card.gate || 'None written yet. Add a gate to positions.yaml before this reports.'));
  box.appendChild(gate);

  const dl = el('dl', 'kv');
  card.rows.forEach(function (r) { dl.appendChild(el('dt', '', r[0])); dl.appendChild(el('dd', '', r[1])); });
  box.appendChild(dl);

  if (card.flags.length) {
    const ul = el('ul', 'flags');
    card.flags.forEach(function (f) {
      const li = el('li', f.level);
      li.appendChild(el('b', '', f.level === 'warn' ? 'Check' : 'Note'));
      li.appendChild(document.createTextNode(f.text));
      ul.appendChild(li);
    });
    box.appendChild(ul);
  }
  if (card.open_items.length) {
    const o = el('div', 'open');
    o.appendChild(el('div', 'k', 'Open items'));
    const ul = el('ul');
    card.open_items.forEach(function (t) { ul.appendChild(el('li', '', t)); });
    o.appendChild(ul);
    box.appendChild(o);
  }
  return box;
}

function buildRow(it) {
  const expandable = !!it.card;
  const wrap = el(expandable ? 'details' : 'div', 'row ' + it.kind);
  if (expandable && it.kind === 'held') wrap.open = true;
  const head = el(expandable ? 'summary' : 'div', 'row-head');

  const left = el('div');
  left.appendChild(el('span', 'symbol', it.symbol));
  left.appendChild(el('span', 'name', it.name));
  if (it.type === 'gate') left.appendChild(el('span', 'tag gate', 'Gate'));
  else if (it.kind === 'held') left.appendChild(el('span', 'tag held', 'Held'));
  else if (it.kind === 'watch') left.appendChild(el('span', 'tag watch', 'Watch'));
  head.appendChild(left);

  let status;
  if (it.type === 'gate') {
    const unresolved = it.passed && !it.outcome;
    status = el('div', 'status ' + (unresolved ? 'passed' : 'estimated'),
      it.outcome ? 'Outcome: ' + it.outcome : unresolved ? 'Passed, outcome not recorded' : 'Upcoming' + (it.until ? ', through ' + it.until : ''));
  } else {
    status = el('div', 'status ' + (it.confirmed ? 'confirmed' : 'estimated'),
      'FMP: ' + (it.confirmed ? 'confirmed' : 'estimated') + (it.time ? ', ' + it.time : ''));
  }
  head.appendChild(status);
  wrap.appendChild(head);

  const note = it.type === 'gate' ? it.label : [it.result_text, it.est_text].filter(Boolean).join(' | ');
  if (note) wrap.appendChild(el('div', 'sub2', note));
  if (expandable) wrap.appendChild(renderCard(it.card));
  return wrap;
}

function selectDay(key, cellEl) {
  document.querySelectorAll('.day.selected').forEach(function (n) { n.classList.remove('selected'); });
  if (!cellEl) cellEl = document.querySelector('.day[data-key="' + key + '"]:not(.outside)');
  if (cellEl) cellEl.classList.add('selected');

  const dayItems = (byDate[key] || []).slice().sort(function (a, b) {
    if (ORDER[a.kind] !== ORDER[b.kind]) return ORDER[a.kind] - ORDER[b.kind];
    if (a.type !== b.type) return a.type === 'earnings' ? -1 : 1;
    return a.symbol.localeCompare(b.symbol);
  });
  const d = new Date(key + 'T00:00:00');
  panelTitle.textContent = d.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' });
  panelBody.className = '';
  panelBody.textContent = '';
  if (!dayItems.length) { panelBody.className = 'empty'; panelBody.textContent = 'Nothing on this day.'; return; }
  dayItems.forEach(function (it) { panelBody.appendChild(buildRow(it)); });
  document.getElementById('panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

const monthsContainer = document.getElementById('monthsContainer');
const base = new Date(DATA.today + 'T00:00:00');
base.setDate(1);
for (let off = 0; off < 3; off++) {
  const block = el('div'), title = el('div', 'month-title'), grid = el('div', 'grid');
  block.appendChild(title); block.appendChild(grid);
  monthsContainer.appendChild(block);
  renderMonth(off, title, grid, base);
}
document.querySelectorAll('[data-goto]').forEach(function (b) {
  b.addEventListener('click', function () { selectDay(b.dataset.goto, null); });
});
</script>
</body>
</html>
"""


def render_html(today, positions, data, meta):
    top, bottom = render_static(today, positions, data, meta)
    payload = {"today": today.isoformat(), "items": data["items"]}
    scope = "S&amp;P 500" if meta["share"] else "S&amp;P 500, your holdings and your watch list"
    sub = f"{scope}. Current month plus two. Generated {today.isoformat()}."
    if not meta["share"] and positions["as_of"]:
        sub += f" Ledger as of {esc(positions['as_of'])}."
    return (HTML_TEMPLATE
            .replace("__SUB__", sub)
            .replace("__TOP__", top)
            .replace("__BOTTOM__", bottom)
            .replace("__DATA__", json.dumps(payload).replace("</", "<\\/")))


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--positions", default=str(Path(__file__).with_name("positions.yaml")))
    ap.add_argument("--out", default=None)
    ap.add_argument("--offline", action="store_true", help="fixture data, no network, no API key")
    ap.add_argument("--no-quotes", action="store_true", help="skip live prices; use ledger prices")
    ap.add_argument("--share", action="store_true", help="S&P 500 only; no positions data in the output")
    ap.add_argument("--today", default=None, help="override today's date (YYYY-MM-DD), for testing")
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    positions = load_positions(args.positions)
    if args.share:
        positions = {"as_of": None, "held": [], "watch": [], "needs_confirmation": []}
    out_path = args.out or ("earnings_calendar_shareable.html" if args.share else "earnings_calendar.html")

    quotes, quote_errs = {}, []
    if args.offline:
        print("OFFLINE demo: fixture data, nothing is fetched.")
        sp500, cal = DEMO_SP500, demo_calendar(today)
        quotes = {k: v for k, v in DEMO_QUOTES.items() if k in {p["ticker"] for p in positions["held"]}}
    else:
        key = fmp_key()
        print("Fetching S&P 500 constituents (Wikipedia) and the FMP earnings calendar...")
        sp500 = fetch_sp500_constituents()
        cal = fetch_earnings_calendar(key, (today - timedelta(days=LOOKBACK_DAYS)).isoformat(),
                                      end_of_month_two_ahead(today).isoformat())
        if positions["held"] and not args.no_quotes:
            print(f"Fetching live prices for {len(positions['held'])} holdings...")
            quotes, quote_errs = fetch_quotes(key, [p["ticker"] for p in positions["held"]])

    data = build(today, positions, sp500, cal, quotes)
    meta = {"offline": args.offline, "share": args.share}
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(render_html(today, positions, data, meta))

    # console summary, so you don't have to open the page to see what's off
    kinds = defaultdict(int)
    for i in data["items"]:
        kinds[(i["type"], i["kind"])] += 1
    print(f"\n{sum(kinds.values())} items on the calendar "
          f"({kinds[('earnings', 'held')]} held, {kinds[('earnings', 'watch')]} watch, "
          f"{kinds[('earnings', 'sp500')]} S&P 500, {sum(v for (t, _), v in kinds.items() if t == 'gate')} ledger gates).")
    if positions["held"] and not args.offline and not args.no_quotes:
        print(f"Live prices: {len(quotes)}/{len(positions['held'])} holdings."
              + (" Failed: " + ", ".join(f"{s} ({why})" for s, why in quote_errs) if quote_errs else ""))
    warns = [c for c in data["checks"] if c["level"] == "warn"]
    if warns:
        print(f"\n{len(warns)} ledger checks:")
        for c in warns:
            print(f"  {c['ticker'] or '-':8} {c['text']}")
    print(f"\nDone. Open {out_path} in your browser.")


if __name__ == "__main__":
    main()

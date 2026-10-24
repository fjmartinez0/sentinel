"""
sentinel_finviz.py -- reads ONE Finviz quote page (quote.ashx?t=PSN) and returns its parts as
plain data. It does no network and no database work, so it can be tested on a saved page.

    data, ok, notes = parse_quote_page(html)

data is {"page_title", "quote_asof", "profile", "snapshot", "ratings", "insiders", "news", "peers", "peer_details", "held_by"}:
  quote_asof  the "Last Close" date shown on the page (e.g. "Sep 18"): how fresh the price is
  profile   sector, industry, country, exchange (from the header links)
  snapshot  the ~84 label/value cells (P/E, Market Cap, Target Price, Recom...), raw strings
  ratings   analyst actions: date, action, analyst, rating_change, price_target_change
  insiders  insider trades, columns as Finviz titles them (raw strings)
  news      headlines: date, time, headline, source
  peers     Finviz's peer tickers; peer_details their company, industry, country, market cap
  held_by   the ETFs that hold the stock
ok is True when the core snapshot was found and sane. notes lists anything missing or odd.

Verified against a real page (PSN, Sep 2026): the snapshot is SIX two-column tables side by side
(one per column of the grid), the peers line reads "Peers: A B C | Held by: X Y Z", and the analyst,
insider and news tables are recognised by their titles. Re-run sentinel_probe.py if that changes.

Why it is written this way: Finviz has no API and its markup changes. So nothing here relies on
CSS class names. Each table is recognised by WHAT IT CONTAINS (analyst-table column titles, the
known snapshot labels, the time-stamp shape of news rows), the same defensive idea as the
screener's find_result_table. Values stay raw strings: interpretation belongs to later layers.
If the page changes shape, this file is the only thing to fix, and parse_ok=0 will tell you.
"""

from __future__ import annotations

import re
from datetime import date, datetime

# Every label seen in the snapshot grid of a Finviz quote page (6 label/value pairs per row).
KNOWN_LABELS = frozenset([
    "Index", "P/E", "EPS (ttm)", "Insider Own", "Shs Outstand", "Perf Week",
    "Market Cap", "Forward P/E", "EPS next Y", "Insider Trans", "Shs Float", "Perf Month",
    "Enterprise Value", "PEG", "EPS next Q", "Inst Own", "Short Float", "Perf Quarter",
    "Income", "P/S", "EPS this Y", "Inst Trans", "Short Ratio", "Perf Half Y",
    "Sales", "P/B", "ROA", "Short Interest", "Perf YTD",
    "Book/sh", "P/C", "EPS next 5Y", "ROE", "52W High", "Perf Year",
    "Cash/sh", "P/FCF", "EPS past 3/5Y", "ROIC", "52W Low", "Perf 3Y",
    "Dividend Est.", "EV/EBITDA", "Sales past 3/5Y", "Gross Margin", "Volatility", "Perf 5Y",
    "Dividend TTM", "EV/Sales", "EPS Y/Y TTM", "Oper. Margin", "ATR (14)", "Perf 10Y",
    "Dividend Ex-Date", "Quick Ratio", "Sales Y/Y TTM", "Profit Margin", "RSI (14)", "Recom",
    "Dividend Gr. 3/5Y", "Current Ratio", "EPS Q/Q", "SMA20", "Beta", "Target Price",
    "Payout", "Debt/Eq", "Sales Q/Q", "SMA50", "Rel Volume", "Prev Close",
    "Employees", "LT Debt/Eq", "Earnings", "SMA200", "Avg Volume", "Price",
    "IPO", "Option/Short", "EPS/Sales Surpr.", "Trades", "Volume", "Change %",
])

MIN_LABEL_HITS = 30        # a table must contain at least this many known labels to be the snapshot
MIN_PAIRS = 50             # ...and yield at least this many pairs to count as parsed
CORE_LABELS = ("Price", "Market Cap")

_NEWS_TIME = re.compile(r"^(?:([A-Z][a-z]{2}-\d{2}-\d{2}|Today)\s+)?(\d{1,2}:\d{2}\s?(?:AM|PM))$")   # "Today 09:45AM" too
_NEWS_SOURCE = re.compile(r"\(([^()]{1,40})\)\s*(?:[+-]?\d+(?:\.\d+)?%)?\s*$")
_TICKERISH = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")
_BLOCK_HINTS = ("captcha", "are you a robot", "access denied", "too many requests", "just a moment", "verify you are human")


# ------------------------------------------------------------------ extraction

def _norm(text) -> str:
    return " ".join(str(text or "").split())


def extract_tables(root) -> list:
    """Every <table> as a list of rows, each row a list of cell texts (raw strings, whitespace-normalised).
    Only direct rows are read, so a nested table's text shows up in its parent cell, not as extra rows."""
    tables = []
    for t in root.iter("table"):
        rows = []
        for tr in t.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr"):
            cells = [_norm(td.text_content()) for td in tr.xpath("./td|./th")]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def _parse_root(html: str):
    import lxml.html
    root = lxml.html.fromstring(html)
    for bad in root.xpath("//script|//style|//noscript"):
        bad.drop_tree()
    return root


def _iso_date(s):
    if s == "Today":                               # Finviz labels the current day's first headline "Today"
        return date.today().isoformat()
    try:
        return datetime.strptime(s, "%b-%d-%y").date().isoformat()
    except (TypeError, ValueError):
        return None


# -------------------------------------------------------------------- sections

def _snapshot_column_tables(tables):
    """The layout Finviz uses today: several two-column tables (label | value), one per column of the
    grid. Every table whose first cells are known labels contributes; together they make the snapshot."""
    pairs, seen, unknown = {}, {}, 0
    for rows in tables:
        two = [r for r in rows if len(r) == 2]
        if len(two) < 5 or len(two) / len(rows) < 0.8:
            continue
        hits = sum(1 for r in two if r[0] in KNOWN_LABELS)
        if hits < 5 or hits / len(two) < 0.6:
            continue
        for label, value in two:
            if not label or len(label) > 40:
                continue
            unknown += label not in KNOWN_LABELS
            seen[label] = seen.get(label, 0) + 1
            pairs[label if seen[label] == 1 else f"{label} ({seen[label]})"] = value
    return (pairs, "column tables", unknown) if len(pairs) >= MIN_LABEL_HITS else (None, None, 0)


def _parse_snapshot(tables):
    """Find the label/value grid. Three layouts are tried in order: several two-column tables (today's
    layout), one wide table with alternating label/value cells, and finally any table that merely
    contains the known labels (pair each label with the cell after it). Returns
    (dict, method, unknown_label_count) or (None, None, 0)."""
    found = _snapshot_column_tables(tables)
    if found[0] is not None:
        return found
    best = None
    for rows in tables:
        even = [c for r in rows for c in r[0::2]]
        hits = sum(1 for c in even if c in KNOWN_LABELS)
        if even and hits >= MIN_LABEL_HITS and hits / len(even) >= 0.6 and (best is None or hits > best[0]):
            best = (hits, rows)
    if best:
        pairs, unknown, seen = {}, 0, {}
        for r in best[1]:
            for i in range(0, len(r) - 1, 2):
                label, value = r[i], r[i + 1]
                if not label or len(label) > 40:
                    continue
                unknown += label not in KNOWN_LABELS
                seen[label] = seen.get(label, 0) + 1
                pairs[label if seen[label] == 1 else f"{label} ({seen[label]})"] = value
        return pairs, "positional", unknown

    best = None
    for rows in tables:
        cells = [c for r in rows for c in r]
        hits = sum(1 for c in cells if c in KNOWN_LABELS)
        if hits >= MIN_LABEL_HITS and (best is None or hits > best[0]):
            best = (hits, cells)
    if best:
        pairs, seen = {}, {}
        cells = best[1]
        for i, c in enumerate(cells[:-1]):
            if c in KNOWN_LABELS and cells[i + 1] not in KNOWN_LABELS:
                seen[c] = seen.get(c, 0) + 1
                pairs[c if seen[c] == 1 else f"{c} ({seen[c]})"] = cells[i + 1]
        return pairs, "by label", 0
    return None, None, 0


def _header_index(rows, wanted, minimum):
    """Index of the first of the top rows whose cells contain at least `minimum` of `wanted` titles."""
    for i, r in enumerate(rows[:3]):
        # Column titles are short. A long cell is a layout table swallowing a nested table's text,
        # which would otherwise look like it contains every title at once.
        low = [c.lower() for c in r if len(c) <= 40]
        if sum(1 for w in wanted if any(w in c for c in low)) >= minimum:
            return i
    return None


def _parse_ratings(tables):
    for rows in tables:
        h = _header_index(rows, ("date", "action", "analyst", "rating change", "price target"), 4)
        if h is None:
            continue
        cols = {}
        for j, c in enumerate(rows[h]):
            low = c.lower()
            for key, needle in (("date", "date"), ("action", "action"), ("analyst", "analyst"),
                                ("rating_change", "rating"), ("price_target_change", "target")):
                if needle in low and key not in cols:
                    cols[key] = j
        if len(cols) < 4:
            continue
        out = []
        for r in rows[h + 1:]:
            row = {k: (r[j] if j < len(r) else "") for k, j in cols.items()}
            if row.get("date") or row.get("action"):
                row["date_iso"] = _iso_date(row.get("date"))
                out.append(row)
        if out:
            return out
    return None


def _parse_insiders(tables):
    for rows in tables:
        h = _header_index(rows, ("insider trading", "relationship", "date", "transaction", "cost", "#shares", "value", "sec form"), 4)
        if h is None:
            continue
        keys = [re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_") or f"col{j}" for j, c in enumerate(rows[h])]
        out = [dict(zip(keys, r)) for r in rows[h + 1:] if len(r) >= 4]
        if out:
            return out
    return None


def _parse_news(tables):
    best = None
    for rows in tables:
        matches = [r for r in rows if len(r) >= 2 and _NEWS_TIME.match(r[0])]
        if len(matches) >= 3 and (best is None or len(matches) > len(best)):
            best = matches
    if not best:
        return None
    out, current_date = [], None
    for r in best:
        d, t = _NEWS_TIME.match(r[0]).groups()
        current_date = d or current_date          # Finviz shows the date only on the first row of a day
        text = _norm(" ".join(r[1:]))
        m = _NEWS_SOURCE.search(text)
        out.append({"date": current_date, "date_iso": _iso_date(current_date), "time": t,
                    "headline": _norm(text[: m.start()]) if m else text, "source": m.group(1) if m else None})
    return out


def _tickers_after(text: str, label: str) -> list:
    m = re.search(rf"\b{re.escape(label)}\b\s*:?\s*(.*)", text)
    if not m:
        return []
    out = []
    for tok in m.group(1).split():
        if not _TICKERISH.match(tok):
            break
        out.append(tok)
    return out


def _profile(root) -> dict:
    """Sector / industry / country / exchange from the header links (their hrefs carry the filter)."""
    out = {}
    for key, needle in (("sector", "f=sec_"), ("industry", "f=ind_"), ("country", "f=geo_"), ("exchange", "f=exch_")):
        for a in root.iter("a"):
            if needle in (a.get("href") or ""):
                out[key] = _norm(a.text_content())
                break
    return out


def _peer_details(root, peers) -> dict:
    """Company, industry, country and market cap for each peer, from the hover attributes on the peers line."""
    out = {}
    for sp in root.xpath("//span[@data-boxover-ticker]"):
        t = sp.get("data-boxover-ticker")
        if t in peers and t not in out:
            out[t] = {"company": sp.get("data-boxover-company"), "industry": sp.get("data-boxover-industry"),
                      "country": sp.get("data-boxover-country"), "market_cap": sp.get("data-boxover-value")}
    return out


def looks_blocked(html: str) -> bool:
    low = html.lower()
    return any(h in low for h in _BLOCK_HINTS)


# ------------------------------------------------------------------------ main

def parse_quote_page(html: str):
    """Returns (data, ok, notes). Never raises on a strange page: it reports instead."""
    notes = []
    empty = {"page_title": None, "quote_asof": None, "profile": {}, "snapshot": {}, "ratings": [], "insiders": [],
             "news": [], "peers": [], "peer_details": {}, "held_by": []}
    try:
        root = _parse_root(html)
    except Exception as e:  # noqa: BLE001
        return empty, False, [f"page could not be parsed as HTML ({e})"]

    title = root.findtext(".//title")
    tables = extract_tables(root)
    text = _norm(" ".join(root.itertext()))

    snapshot, method, unknown = _parse_snapshot(tables)
    ratings, insiders, news = _parse_ratings(tables), _parse_insiders(tables), _parse_news(tables)
    peers, held_by = _tickers_after(text, "Peers"), _tickers_after(text, "Held by")

    asof = re.search(r"Last Close\s+[\d,]+\.?\d*\s+([A-Z][a-z]{2}\s+\d{1,2})", text)
    data = {"page_title": _norm(title) or None, "quote_asof": asof.group(1) if asof else None, "profile": _profile(root),
            "snapshot": snapshot or {}, "ratings": ratings or [], "insiders": insiders or [], "news": news or [],
            "peers": peers, "peer_details": _peer_details(root, peers), "held_by": held_by}

    ok = True
    if snapshot is None:
        ok = False
        notes.append("no snapshot table found" + (" (the page looks like a block or verification screen)" if looks_blocked(html) else
                                                   ": Finviz may have changed its layout"))
    else:
        if len(snapshot) < MIN_PAIRS:
            ok = False
            notes.append(f"snapshot has only {len(snapshot)} fields (expected about 84)")
        missing = [k for k in CORE_LABELS if not snapshot.get(k)]
        if missing:
            ok = False
            notes.append(f"snapshot is missing core fields: {', '.join(missing)}")
        if method == "by label":
            notes.append("snapshot was paired by label because the page layout was not recognised; check it")
        if unknown:
            notes.append(f"{unknown} snapshot label(s) not in the known list (new Finviz fields?)")
    for name, val in (("analyst ratings table", ratings), ("insider trades table", insiders), ("news", news),
                      ("peers line", peers), ("held-by line", held_by), ("sector/industry links", data["profile"])):
        if not val:
            notes.append(f"no {name} found")
    return data, ok, notes


# ---------------------------------------------------------------------- helpers

def num(s):
    """First number in a raw Finviz string: '58.10' -> 58.1, '-27.48%' -> -27.48, '4.79B' -> 4.79e9."""
    m = re.match(r"^\s*(-?\d[\d,]*\.?\d*)\s*([KMBT]?)", str(s or ""))
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return v * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[m.group(2)]


def _rating_key(r):
    return (r.get("date"), r.get("action"), r.get("analyst"), r.get("rating_change"), r.get("price_target_change"))


def describe_changes(prev: dict, curr: dict) -> list:
    """Short human-readable lines: what is new in `curr` compared with the earlier snapshot `prev`."""
    out = []
    seen = {_rating_key(r) for r in prev.get("ratings", [])}
    new = [r for r in curr.get("ratings", []) if _rating_key(r) not in seen]
    if new:
        parts = [f"{r.get('date')} {r.get('action')} {r.get('analyst')}" + (f" ({r['price_target_change']})" if r.get("price_target_change") else "") for r in new[:3]]
        out.append(f"{len(new)} new analyst action(s): " + "; ".join(parts) + (f"; +{len(new) - 3} more" if len(new) > 3 else ""))

    seen = {tuple(sorted(r.items())) for r in prev.get("insiders", [])}
    new = [r for r in curr.get("insiders", []) if tuple(sorted(r.items())) not in seen]
    if new:
        first = new[0]
        out.append(f"{len(new)} new insider trade(s), e.g. {first.get('insider_trading', '?')} {first.get('transaction', '?')} on {first.get('date', '?')}")

    seen = {(n.get("date"), n.get("headline")) for n in prev.get("news", [])}
    n_new = sum(1 for n in curr.get("news", []) if (n.get("date"), n.get("headline")) not in seen)
    if n_new:
        out.append(f"{n_new} new headline(s)")

    for label in ("Target Price", "Recom", "Insider Trans", "Short Float"):
        a, b = prev.get("snapshot", {}).get(label), curr.get("snapshot", {}).get(label)
        if a is not None and b is not None and a != b:
            out.append(f"{label}: {a} -> {b}")
    return out

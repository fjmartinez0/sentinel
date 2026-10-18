"""
sentinel_probe.py -- ONE-OFF check: can the dossier parser read a real Finviz quote page?

It makes a single request for one ticker (through the same polite session as the scanner),
saves the page, runs the parser, and prints a short report of what it found. Nothing is written
to research.db. Run it once before you trust --dossier, and again if Finviz ever changes.

  python3 sentinel_probe.py PSN
  python3 sentinel_probe.py --from-file probe_out/PSN.html     # re-check a saved page, no request

Outputs (in ./probe_out/):  PSN.html (the page)  and  PSN_report.txt (the report below).
If anything says MISSING or FAILED, send me PSN_report.txt (and PSN.html if it is not huge).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import sentinel_finviz as finviz_quote

OUT_DIR = Path(__file__).resolve().parent / "probe_out"
SAMPLE_FIELDS = ("Price", "Market Cap", "P/E", "Forward P/E", "Target Price", "Recom", "Earnings",
                 "Insider Trans", "Short Float", "Perf YTD", "52W High", "Debt/Eq")


def _clip(s, n=26):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "~"


def build_report(html: str, symbol: str, source: str) -> str:
    L = []
    root = finviz_quote._parse_root(html)
    tables = finviz_quote.extract_tables(root)
    data, ok, notes = finviz_quote.parse_quote_page(html)

    L.append(f"PROBE REPORT for {symbol}")
    L.append(f"source     : {source}")
    L.append(f"page       : {len(html):,} bytes, sha1 {hashlib.sha1(html.encode('utf-8', 'ignore')).hexdigest()[:12]}, title {data['page_title']!r}")
    if finviz_quote.looks_blocked(html):
        L.append("WARNING    : the page contains block/verification wording (captcha, access denied, ...)")

    L.append(f"\nTABLES on the page: {len(tables)}   (rows x widest row; first cells of the first row)")
    shown = 0
    for i, rows in enumerate(tables):
        if len(rows) < 3 or shown >= 20:
            continue
        shown += 1
        L.append(f"  #{i:<3} {len(rows):>3} x {max(len(r) for r in rows):<3} | " + " | ".join(_clip(c, 16) for c in rows[0][:5]))

    snap = data["snapshot"]
    L.append(f"\nSNAPSHOT     : {len(snap)} fields  (expect about 84)")
    for k in SAMPLE_FIELDS:
        L.append(f"  {k:<14} {_clip(snap.get(k, 'MISSING'), 40)}")
    L.append(f"\nANALYST TABLE: {len(data['ratings'])} rows")
    for r in data["ratings"][:3]:
        L.append("  " + " | ".join(_clip(r.get(k, ""), 22) for k in ("date", "action", "analyst", "rating_change", "price_target_change")))
    L.append(f"\nINSIDER TABLE: {len(data['insiders'])} rows" + (f"   columns: {', '.join(data['insiders'][0].keys())}" if data["insiders"] else ""))
    for r in data["insiders"][:2]:
        L.append("  " + " | ".join(_clip(v, 16) for v in list(r.values())[:7]))
    L.append(f"\nNEWS         : {len(data['news'])} rows")
    for n in data["news"][:3]:
        L.append(f"  {n['date']} {n['time']} | {_clip(n['headline'], 60)} | source={n['source']}")
    L.append(f"\nPROFILE      : {data['profile'] or 'MISSING'}    last close date on page: {data['quote_asof'] or 'MISSING'}")
    L.append(f"PEERS        : {' '.join(data['peers']) or 'MISSING'}   (details for {len(data['peer_details'])})")
    L.append(f"HELD BY      : {' '.join(data['held_by']) or 'MISSING'}")

    L.append("\nVERDICT      : " + ("PARSE OK" if ok else "PARSE FAILED"))
    for n in notes:
        L.append(f"  note: {n}")
    L.append("\nCheck by eye: open the same ticker in your browser and compare Price, Target Price, the newest analyst")
    L.append("row, and the newest headline with the lines above.")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticker", nargs="?", help="a US ticker, e.g. PSN")
    ap.add_argument("--from-file", metavar="HTML", help="parse a saved page instead of fetching")
    args = ap.parse_args()
    if not args.ticker and not args.from_file:
        ap.print_help()
        sys.exit(1)

    OUT_DIR.mkdir(exist_ok=True)
    if args.from_file:
        path = Path(args.from_file)
        html, symbol, source = path.read_text(encoding="utf-8", errors="ignore"), args.ticker or path.stem, f"file {path}"
    else:
        import sentinel_scan as scan          # reuse its polite session, retries and user agent
        symbol = args.ticker.strip().upper()
        print(f"Fetching one page for {symbol} ...")
        html = scan.fetch_quote_html(symbol)
        if html is None:
            sys.exit("Could not fetch the page (blocked, offline, or the ticker does not exist). Nothing was saved.")
        (OUT_DIR / f"{symbol}.html").write_text(html, encoding="utf-8")
        source = f"fetched from {scan.QUOTE_URL.format(symbol=symbol)}"

    report = build_report(html, symbol, source)
    (OUT_DIR / f"{symbol}_report.txt").write_text(report + "\n", encoding="utf-8")
    print("\n" + report)
    print(f"\nSaved: {OUT_DIR / (symbol + '_report.txt')}" + ("" if args.from_file else f" and {OUT_DIR / (symbol + '.html')}"))


if __name__ == "__main__":
    main()

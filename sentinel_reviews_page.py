#!/usr/bin/env python3
"""
sentinel_reviews_page.py -- the "Weekly reviews" tab of the tracker (DECISIONS.md 56).

Reads the Tier 2 reviews from research.db and writes tier2_reviews.html next to earnings_calendar.html. Both
pages carry the same two tabs (Calendar | Weekly reviews), so they behave like one tracker. Nothing is fetched
from the network and nothing is written anywhere else: it is safe to run at any time.

  python3 sentinel_reviews_page.py            write tier2_reviews.html
  open tier2_reviews.html                     look at it

The weekly job rebuilds it after Tier 2; the daily job rebuilds it after Tier 1, so an alert you close with
`sentinel_tier1.py --mark` shows as closed the next evening. Local use only: the reviews quote your own gates.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sentinel_db as db  # noqa: E402

OUT = "tier2_reviews.html"
CALENDAR = "earnings_calendar.html"
SCORE_KEYS = ("asymmetry", "catalyst", "proof", "exit")
SCORE_LABELS = {"asymmetry": "Asymmetry", "catalyst": "Catalyst", "proof": "Proof", "exit": "Exit"}
VERDICT_TEXT = {"watch": "Watch", "dismiss": "Dismiss", "insufficient": "Insufficient", "intact": "Intact",
                "at_risk": "At risk", "broken": "Broken"}
CHECK_TEXT = {"done": "done", "partial": "partial", "not_done": "not done"}
INDEX_TEXT = {"top15_qqq": "Top-15 QQQ", "sp500": "S&P 500", "neither": "Neither index", "unknown": "Index unknown"}
WEEKS_SHOWN = 8


def e(s):
    return html.escape("" if s is None else str(s), quote=True)


# ------------------------------------------------------------------------------------------------ markdown (small, safe)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITAL = re.compile(r"(?<![\w*])\*(?!\s)([^*]+?)\*(?![\w*])")
_CODE = re.compile(r"`([^`]+)`")


def inline(text):
    """Escape first, then turn **bold**, *italic*, `code` and [links](https://...) into HTML. Only http(s) links."""
    t = e(text)
    t = _CODE.sub(r"<code>\1</code>", t)
    t = _LINK.sub(lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>', t)
    t = _BOLD.sub(r"<strong>\1</strong>", t)
    return _ITAL.sub(r"<em>\1</em>", t)


def markdown(md):
    out, para, items = [], [], []

    def flush():
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for raw in (md or "").splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not s or s == "---":
            flush()
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            flush()
            level = min(len(m.group(1)) + 2, 5)               # # -> h3, ## -> h4: the card already has a title
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            continue
        m = re.match(r"^(?:[-*]|\d+\.)\s+(.*)$", s)
        if m:
            if para:
                flush()
            items.append(m.group(1))
            continue
        if items:
            flush()
        para.append(s)
    flush()
    return "".join(out)


# ------------------------------------------------------------------------------------------------ data
def load(conn):
    escs = {x["escalation_id"]: x for x in db.list_escalations(conn)}
    rows = sorted(db.list_tier2_reviews(conn), key=lambda r: r["created_at"], reverse=True)    # newest first, by time
    live = [r for r in rows if r["mode"] in ("live", "manual")]
    calib = [r for r in rows if r["mode"] == "calibration"]
    weeks = OrderedDict()
    for r in live:
        weeks.setdefault(week_of(r["created_at"]), []).append(r)
    batches = OrderedDict()
    for r in calib:
        batches.setdefault(r["batch"], []).append(r)
    return escs, weeks, batches


def week_of(created_at):
    """The Saturday that starts the review week (Sat-Fri), so a Saturday run and a Sunday re-run land together."""
    d = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone().date()
    return date.fromordinal(d.toordinal() - (d.weekday() - 5) % 7)


# ------------------------------------------------------------------------------------------------ html pieces
def verdict_badge(v):
    return f'<span class="verdict v-{e(v or "none")}">{e(VERDICT_TEXT.get(v, "No verdict"))}</span>'


def scores_block(res):
    s = (res or {}).get("scores")
    if not s:
        return '<div class="noscores">Not scored</div>'
    bars = "".join(
        f'<div class="sc"><span class="sc-l">{SCORE_LABELS[k]}</span><span class="sc-bar">'
        + "".join(f'<i class="{"on" if i <= s[k] else ""}"></i>' for i in range(1, 6))
        + f'</span><span class="sc-n">{s[k]}</span></div>' for k in SCORE_KEYS)
    return f'<div class="scores">{bars}<div class="total">{sum(s[k] for k in SCORE_KEYS)}<span>/20</span></div></div>'


def card(r, escs, expected=None):
    res = r.get("result") or {}
    esc_row = escs.get(r["escalation_id"]) or {}
    funnel = esc_row.get("funnel")
    kind = "Your holding or watch name" if funnel == "A" else "New candidate"
    status = esc_row.get("status")
    status_html = ""
    if status:
        status_html = (f'<span class="estatus s-{e(status)}">{"Alert open" if status == "open" else "Alert " + e(status)}</span>')
    facts = []
    if res.get("gate"):
        facts.append(("Gate", res["gate"] + (f' (by {res["gate_date"]})' if res.get("gate_date") else "")))
    if res.get("readd_condition"):
        facts.append(("Comes back if", res["readd_condition"]))
    ch = res.get("checks") or {}
    checks = "".join(f'<span class="chk c-{e(ch.get(k, "not_done"))}">{label}: {e(CHECK_TEXT.get(ch.get(k), "?"))}</span>'
                     for k, label in (("insiders", "Insiders"), ("history_peers", "History &amp; peers"), ("fraud", "Fraud")))
    idx = INDEX_TEXT.get(res.get("index_member"), "")
    dl = "".join(f"<dt>{e(k)}</dt><dd>{inline(v)}</dd>" for k, v in facts)
    sources = res.get("sources") or []
    src = ""
    if sources:
        src = ('<details class="sources"><summary>Sources (' + str(len(sources)) + ')</summary><ul>'
               + "".join(f'<li><a href="{e(s["url"])}" target="_blank" rel="noopener">{e(s.get("title") or host(s["url"]))}</a></li>'
                         for s in sources if str(s.get("url", "")).startswith(("http://", "https://"))) + "</ul></details>")
    calib = ""
    if expected:
        same = expected == r.get("verdict")
        calib = f'<div class="calib {"same" if same else "diff"}">Your verdict: {e(VERDICT_TEXT.get(expected, expected))} &middot; {"match" if same else "different"}</div>'
    err = "" if r["ok"] else f'<div class="err">No usable verdict: {e(r.get("error") or "unknown")}</div>'
    close = ""
    if status == "open" and r["escalation_id"]:
        close = (f'<div class="close">When you have decided: <code>python3 sentinel_tier1.py --mark {r["escalation_id"]} done</code> '
                 f'or <code>... --mark {r["escalation_id"]} dismissed</code></div>')
    body = markdown(r.get("report_md") or "")
    return f"""
<article class="card v-{e(r.get('verdict') or 'none')}">
  <header>
    <div class="who"><span class="sym">{e(r['ticker'])}</span><span class="kind">{kind}</span>{status_html}</div>
    {verdict_badge(r.get('verdict'))}
  </header>
  {err}{calib}
  {f'<p class="oneline">{inline(res["one_line"])}</p>' if res.get("one_line") else ""}
  <div class="grid2">
    {scores_block(res)}
    <div>
      <dl>{dl}</dl>
      <div class="chks">{checks}{f'<span class="chk idx">{e(idx)}</span>' if idx else ""}</div>
    </div>
  </div>
  {close}
  {src}
  {f'<details class="full"><summary>Full review</summary><div class="md">{body}</div></details>' if body else ""}
  <footer>Escalation #{e(r['escalation_id'] or '-')} &middot; {e(r['created_at'][:10])} &middot; {e(r['mode'])} run &middot; {e(r['model'])}
  &middot; {e(r.get('searches') or 0)} searches &middot; {"$0 (subscription)" if "(subscription)" in (r.get("model") or "") else f"${r.get('cost_usd') or 0:.2f}"}</footer>
</article>"""


def host(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1) if m else url


def total_text(res):
    sc = (res or {}).get("scores")
    return f"{sum(sc[k] for k in SCORE_KEYS)}/20" if sc else "-"


def summary_table(rows):
    trs = "".join(f'<tr><td><a href="#r{r["review_id"]}">{e(r["ticker"])}</a></td><td>{verdict_badge(r.get("verdict"))}</td>'
                  f'<td>{total_text(r.get("result"))}</td>'
                  f'<td>{inline((r.get("result") or {}).get("one_line") or (r.get("error") or ""))}</td></tr>' for r in rows)
    return f'<table class="sum"><thead><tr><th>Name</th><th>Verdict</th><th>Score</th><th>In one line</th></tr></thead><tbody>{trs}</tbody></table>'


def render(conn, today):
    p = parts(conn, today)
    return TEMPLATE.replace("__TODAY__", e(today.isoformat())).replace("__WAITING__", p["waiting_html"]).replace("__BODY__", p["body_html"])


def site_payload(conn, today):
    """The Weekly reviews tab of the public site (DECISIONS.md 57), as JSON: the same fragments as the local page."""
    p = parts(conn, today)
    return {"schema": 1, "waiting_html": p["waiting_html"], "body_html": p["body_html"], "n_reviews": p["n_reviews"]}
    # no timestamp in here on purpose: the file changes only when the reviews change, so git shows real changes


def parts(conn, today):
    escs, weeks, batches = load(conn)
    open_unreviewed = [x for x in escs.values() if x["status"] == "open"
                       and x["escalation_id"] not in db.reviewed_escalation_ids(conn)]
    out = []
    if not weeks:
        out.append('<div class="empty">No Tier 2 reviews yet. They appear here after the Saturday run.</div>')
    for i, (wk, rows) in enumerate(list(weeks.items())[:WEEKS_SHOWN]):
        latest = {}
        for r in rows:                              # newest first: keep the latest review per escalation in a week
            latest.setdefault(r["escalation_id"] or r["ticker"], r)
        rows = sorted(latest.values(), key=lambda r: (0 if r.get("verdict") in ("broken", "at_risk") else
                                                       1 if r.get("verdict") == "watch" else 2, r["ticker"]))
        cards = "".join(f'<div id="r{r["review_id"]}">{card(r, escs)}</div>' for r in rows)
        title = f"Week of {wk.strftime('%b %-d, %Y')}" if hasattr(wk, "strftime") else str(wk)
        out.append(f'<details class="week" {"open" if i == 0 else ""}><summary><h2>{e(title)}</h2>'
                     f'<span class="count">{len(rows)} review(s)</span></summary>{summary_table(rows)}{cards}</details>')
    if batches:
        bl = []
        for b, rows in list(batches.items())[:3]:
            cards = "".join(card(r, escs, expected=r.get("expected")) for r in sorted(rows, key=lambda r: r["ticker"]))
            same = sum(1 for r in rows if r.get("expected") == r.get("verdict"))
            bl.append(f'<details class="week calibw"><summary><h2>Calibration of {e(rows[0]["created_at"][:10])}</h2>'
                      f'<span class="count">{same}/{len(rows)} matched your verdicts</span></summary>{cards}</details>')
        out.append('<h2 class="section">Calibration runs</h2>' + "".join(bl))
    waiting = ""
    if open_unreviewed:
        waiting = ('<div class="banner warn">Waiting for Tier 2: ' + ", ".join(e(x["ticker"]) for x in open_unreviewed)
                   + ". They are reviewed on the next Saturday run (or now: <code>python3 sentinel_tier2.py</code>).</div>")
    n = sum(len(v) for v in weeks.values()) + sum(len(v) for v in batches.values())
    return {"waiting_html": waiting, "body_html": "".join(out), "n_reviews": n}


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weekly Reviews</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root { --ink:#14213D; --paper:#FBFAF7; --rule:#DAD6CC; --muted:#8B8676; --held:#1D4E89; --watch:#6E4A78;
          --good:#2F6D4F; --good-bg:#EAF3EE; --warn:#9B3D2F; --warn-bg:#F7ECE8; --amber:#8A5A00; --amber-bg:#FBF1DC; --card:#fff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--paper); color:var(--ink); font-family:'Inter',sans-serif; padding:32px 16px 80px; }
  .wrap { max-width:980px; margin:0 auto; }
  h1 { font-family:'Source Serif 4',serif; font-size:26px; margin:0 0 4px; }
  h2 { font-family:'Source Serif 4',serif; font-size:18px; margin:0; display:inline; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:14px; }
  .tabs { display:flex; gap:4px; border-bottom:1px solid var(--rule); margin-bottom:18px; }
  .tabs a { padding:8px 14px; font-size:14px; font-weight:500; color:var(--muted); text-decoration:none; border:1px solid transparent; border-bottom:0; border-radius:6px 6px 0 0; margin-bottom:-1px; }
  .tabs a.on { color:var(--ink); background:var(--paper); border-color:var(--rule); }
  .tabs a:hover:not(.on) { color:var(--ink); }
  .banner { font-size:12px; padding:7px 10px; border:1px solid var(--rule); border-radius:4px; color:#5A5648; margin-bottom:12px; }
  .banner.warn { border-color:var(--warn); background:var(--warn-bg); color:var(--warn); }
  code { font-size:12px; background:#EFEBE0; padding:1px 5px; border-radius:3px; }
  details.week { border-top:2px solid var(--ink); padding:12px 0 6px; margin-bottom:18px; }
  details.week > summary { cursor:pointer; list-style:none; display:flex; align-items:baseline; gap:12px; }
  details.week > summary::-webkit-details-marker { display:none; }
  details.week > summary::before { content:'\25B8'; color:var(--muted); font-size:12px; }
  details.week[open] > summary::before { content:'\25BE'; }
  .count { color:var(--muted); font-size:13px; }
  .section { display:block; margin:30px 0 8px; font-size:15px; color:var(--muted); }
  table.sum { width:100%; border-collapse:collapse; font-size:13px; margin:12px 0 16px; }
  table.sum th { text-align:left; font-weight:500; color:var(--muted); font-size:12px; padding:6px 8px; border-bottom:1px solid var(--ink); }
  table.sum td { padding:8px; border-bottom:1px solid var(--rule); vertical-align:top; }
  table.sum a { color:var(--ink); font-weight:600; }
  .card { background:var(--card); border:1px solid var(--rule); border-left:4px solid var(--rule); border-radius:6px; padding:14px 16px; margin:12px 0; }
  .card.v-watch { border-left-color:var(--held); } .card.v-intact { border-left-color:var(--good); }
  .card.v-at_risk, .card.v-insufficient { border-left-color:var(--amber); } .card.v-broken { border-left-color:var(--warn); }
  .card header { display:flex; justify-content:space-between; align-items:center; gap:10px; }
  .who { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
  .sym { font-family:'Source Serif 4',serif; font-size:20px; font-weight:700; }
  .kind { font-size:12px; color:var(--muted); }
  .estatus { font-size:11px; padding:1px 8px; border-radius:9px; border:1px solid var(--rule); color:var(--muted); }
  .estatus.s-open { border-color:var(--held); color:var(--held); }
  .verdict { font-size:12px; font-weight:600; padding:3px 10px; border-radius:12px; white-space:nowrap; background:#EEE; color:#555; }
  .v-watch.verdict { background:#E6EDF7; color:var(--held); } .v-intact.verdict { background:var(--good-bg); color:var(--good); }
  .v-at_risk.verdict, .v-insufficient.verdict { background:var(--amber-bg); color:var(--amber); }
  .v-broken.verdict { background:var(--warn-bg); color:var(--warn); } .v-dismiss.verdict { background:#EFEDE7; color:#6B675A; }
  .oneline { font-size:14px; line-height:1.5; margin:10px 0 12px; }
  .grid2 { display:grid; grid-template-columns:240px 1fr; gap:18px; align-items:start; }
  @media (max-width:640px) { .grid2 { grid-template-columns:1fr; } }
  .scores { display:grid; gap:6px; font-size:12px; }
  .sc { display:grid; grid-template-columns:72px 1fr 14px; align-items:center; gap:8px; }
  .sc-l { color:var(--muted); } .sc-n { font-weight:600; text-align:right; }
  .sc-bar { display:grid; grid-template-columns:repeat(5,1fr); gap:3px; }
  .sc-bar i { height:8px; border-radius:2px; background:#ECE8DE; } .sc-bar i.on { background:var(--ink); }
  .total { font-family:'Source Serif 4',serif; font-size:22px; font-weight:700; margin-top:4px; }
  .total span { font-size:13px; color:var(--muted); font-weight:500; }
  .noscores { font-size:12px; color:var(--muted); padding-top:4px; }
  dl { display:grid; grid-template-columns:100px 1fr; gap:6px 12px; margin:0 0 10px; font-size:13px; line-height:1.45; }
  dt { color:var(--muted); } dd { margin:0; }
  .chks { display:flex; flex-wrap:wrap; gap:6px; }
  .chk { font-size:11px; padding:2px 8px; border-radius:9px; border:1px solid var(--rule); }
  .chk.c-done { border-color:var(--good); color:var(--good); } .chk.c-partial { border-color:var(--amber); color:var(--amber); }
  .chk.c-not_done { border-color:var(--warn); color:var(--warn); } .chk.idx { color:var(--muted); }
  .calib { font-size:12px; margin:8px 0 0; } .calib.same { color:var(--good); } .calib.diff { color:var(--warn); font-weight:600; }
  .err { font-size:13px; color:var(--warn); background:var(--warn-bg); padding:6px 10px; border-radius:4px; margin-top:8px; }
  .close { font-size:12px; color:var(--muted); margin-top:12px; }
  details.sources, details.full { margin-top:10px; font-size:13px; }
  details.sources summary, details.full summary { cursor:pointer; color:var(--held); font-weight:500; }
  details.sources ul { margin:6px 0 0; padding-left:18px; } details.sources li { margin:3px 0; }
  .md { font-size:14px; line-height:1.6; border-top:1px solid var(--rule); margin-top:8px; padding-top:4px; }
  .md h3, .md h4, .md h5 { font-family:'Source Serif 4',serif; margin:16px 0 4px; } .md h3 { font-size:17px; } .md h4 { font-size:15px; }
  .md a { color:var(--held); } .md ul { padding-left:20px; }
  footer { font-size:11px; color:var(--muted); margin-top:12px; }
  .empty { color:var(--muted); padding:20px 0; }
  a:focus-visible, summary:focus-visible { outline:2px solid var(--held); outline-offset:2px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Tracker</h1>
  <div class="sub">Tier 2 reviews from the weekly runs. Advice only: you decide. Generated __TODAY__.</div>
  <nav class="tabs"><a href="earnings_calendar.html">Calendar</a><a class="on" href="tier2_reviews.html">Weekly reviews</a></nav>
  __WAITING__
  __BODY__
</div>
</body>
</html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="database file (default: $RESEARCH_DB or research.db)")
    ap.add_argument("--out", default=str(HERE / OUT))
    ap.add_argument("--site", nargs="?", const=str(HERE / "site" / "data" / "reviews.json"), default=None, metavar="FILE",
                    help="also write the public site's Weekly reviews data (default site/data/reviews.json)")
    args = ap.parse_args(argv)
    try:
        conn = db.connect(args.db, create=False)
    except FileNotFoundError:
        print("research.db not found: nothing to show yet.")
        return 2
    try:
        page = render(conn, date.today())
        n = len(db.list_tier2_reviews(conn))
        if args.site:
            import json
            site = Path(args.site)
            site.parent.mkdir(parents=True, exist_ok=True)
            tmp = site.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(site_payload(conn, date.today()), ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(site)
            print(f"Site data written: {site}")
    finally:
        conn.close()
    Path(args.out).write_text(page, encoding="utf-8")
    print(f"Weekly reviews page written: {Path(args.out).name} ({n} review(s) in the database). Open it with: open {Path(args.out).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

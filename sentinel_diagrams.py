#!/usr/bin/env python3
"""
sentinel_diagrams.py -- draws the four architecture diagrams as SVG files, from this script.

    python3 sentinel_diagrams.py            writes the four files into ./diagrams/
    python3 sentinel_diagrams.py --out DIR  writes them somewhere else

  system_overview_v4.svg   the programs, your files, outside services, outputs and the database, numbered
  database_v4.svg          the 12 tables, grouped by the program that writes them, and who reads what
  orchestration_v2.svg     the schedule, the daily and Saturday jobs, health checks, notifications, publishing
  flows_v4.svg             each program's steps in order, with the real Python function names

The diagrams are CODE: to change one, edit the data below and run this again (never hand-edit the SVGs,
or the next regeneration loses the edit). Colour key, used everywhere:
  teal  = Python code      amber = a file you edit by hand      gray = a file the code creates
  white = outside service  dashed outline = planned, optional or off
Checked against the code on 2026-10-04 (schema v7, DECISIONS.md 1-52).
"""
import argparse
from pathlib import Path
from xml.sax.saxutils import escape

STYLE = """
  .t  { font-size: 14px; fill: #2C2C2A; }
  .th { font-size: 14px; font-weight: 500; fill: #2C2C2A; }
  .ts { font-size: 12px; fill: #5F5E5A; }
  .tt { font-size: 18px; font-weight: 600; fill: #2C2C2A; }
  .arr { fill: none; stroke: #5F5E5A; stroke-width: 1.5; }
  .box { fill: #FFFFFF; stroke: #888780; }
  .c-teal  > rect, rect.c-teal  { fill: #E1F5EE; stroke: #0F6E56; }
  .c-teal  > .th { fill: #085041; }  .c-teal  > .ts { fill: #0F6E56; }
  .c-amber > rect, rect.c-amber { fill: #FAEEDA; stroke: #854F0B; }
  .c-amber > .th { fill: #633806; }  .c-amber > .ts { fill: #854F0B; }
  .c-gray  > rect, rect.c-gray  { fill: #F1EFE8; stroke: #5F5E5A; }
  .c-gray  > .th { fill: #444441; }  .c-gray  > .ts { fill: #5F5E5A; }
  .panel { fill: none; stroke: #B4B2A9; }
  .pill { fill: #2C2C2A; }  .pilltext { font-size: 11px; font-weight: 600; fill: #FFFFFF; }
"""


class Svg:
    def __init__(self, w, h, title, desc):
        self.w, self.h, self.parts = w, h, []
        self.head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
                     f'font-family="-apple-system, \'Segoe UI\', Helvetica, Arial, sans-serif">\n<title>{escape(title)}</title>\n'
                     f'<desc>{escape(desc)}</desc>\n<style>{STYLE}</style>\n'
                     '<defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" '
                     'orient="auto-start-reverse"><path d="M2 1L8 5L2 9" fill="none" stroke="#5F5E5A" stroke-width="1.5" '
                     'stroke-linecap="round" stroke-linejoin="round"/></marker></defs>\n'
                     f'<rect width="{w}" height="{h}" fill="#FFFFFF"/>\n')

    def box(self, x, y, w, h, title, subs=(), cls=None, dashed=False):
        lines = [title] + list(subs)
        y0 = y + h / 2 - (len(lines) - 1) * 9
        dash = ' style="stroke-dasharray:4 3"' if dashed else ""
        rect = (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" stroke-width="0.5"{dash}/>' if cls
                else f'<rect class="box" x="{x}" y="{y}" width="{w}" height="{h}" rx="6" stroke-width="0.5"{dash}/>')
        txt = "".join(f'<text class="{"th" if i == 0 else "ts"}" x="{x + w / 2}" y="{y0 + 18 * i}" text-anchor="middle" '
                      f'dominant-baseline="central">{escape(s)}</text>' for i, s in enumerate(lines))
        self.parts.append(f'<g{f" class=\"c-{cls}\"" if cls else ""}>{rect}{txt}</g>')

    def text(self, x, y, s, cls="ts", anchor="start"):
        self.parts.append(f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}" dominant-baseline="central">{escape(s)}</text>')

    def line(self, x1, y1, x2, y2, head=True, tail=False, dashed=False):
        m = (' marker-end="url(#arrow)"' if head else "") + (' marker-start="url(#arrow)"' if tail else "")
        d = ' style="stroke-dasharray:4 3"' if dashed else ""
        self.parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="arr"{m}{d}/>')

    def path(self, d, head=True, dashed=False):
        dd = ' style="stroke-dasharray:4 3"' if dashed else ""
        self.parts.append(f'<path d="{d}" class="arr"{" marker-end=\"url(#arrow)\"" if head else ""}{dd}/>')

    def pill(self, x, y, label):
        w = 12 + 6.5 * len(label)
        self.parts.append(f'<rect class="pill" x="{x - w / 2}" y="{y - 9}" width="{w}" height="18" rx="9"/>'
                          f'<text class="pilltext" x="{x}" y="{y}" text-anchor="middle" dominant-baseline="central">{escape(label)}</text>')

    def panel(self, x, y, w, h):
        self.parts.append(f'<rect class="panel" x="{x}" y="{y}" width="{w}" height="{h}" rx="8" stroke-width="0.8"/>')

    def legend(self, y, extra=()):
        x = 35
        for cls, label, dashed in (("teal", "Python code", False), ("amber", "File you edit by hand", False),
                                   ("gray", "File the code creates", False), (None, "Outside service", False),
                                   (None, "Dashed: planned, optional or off", True)):
            self.box(x, y, 26, 18, "", (), cls, dashed)
            self.text(x + 34, y + 9, label)
            x += 56 + 6.1 * len(label)
        for i, s in enumerate(extra):
            self.text(35, y + 36 + 18 * i, s)

    def save(self, path):
        Path(path).write_text(self.head + "\n".join(self.parts) + "\n</svg>\n", encoding="utf-8")


COLS = [35, 265, 495, 725]            # four columns, 200 wide, 30 apart


# ------------------------------------------------------------------------------------------------ 1. overview
def system_overview(out):
    s = Svg(960, 1175, "Sentinel: system overview", "Your files, outside services, the four programs, what they "
            "write, the database, and the scheduler that runs them. Numbers give the order of one cycle.")
    s.text(35, 22, "Sentinel: how the pieces fit (Oct 4, 2026)", "tt")
    s.box(35, 44, 890, 50, "Your files (you edit these)", ["positions.yaml · research_log.yaml · sentinel_tier1_rules.yaml · "
          "Investment_Strategy_Framework.md · .env (keys, private)"], "amber")
    services = [("Finviz", ["Screener, quotes, closes"]), ("Alpha Vantage", ["Earnings · Wiki: S&P 500"]),
                None, ("Claude API", ["Sonnet 5 + web search"])]
    for c, sv in zip(COLS, services):
        if sv:
            s.box(c, 128, 160, 50, sv[0], sv[1])
    progs = [("Scanner", ["sentinel_scan.py", "+ sentinel_finviz.py (parser)", "Sat screen · daily prices"]),
             ("Calendar", ["sentinel_calendar.py", "The front-end page", "Daily"]),
             ("Tier 1", ["sentinel_tier1.py", "Rules only, no AI", "Daily + Saturday"]),
             ("Tier 2", ["sentinel_tier2.py", "Claude reviews alerts", "Saturday only"])]
    PY, PH = 236, 90
    for c, (t, sub) in zip(COLS, progs):
        s.box(c, PY, 200, PH, t, sub, "teal")
    OY = 392
    outs = [("Candidates CSV", ["candidates_DATE.csv"]), ("Calendar page", ["earnings_calendar.html"]),
            ("Alert queue", ["tier1 --show (console)"]), ("Reviews", ["reviews/DATE_tier2.md"])]
    for c, (t, sub) in zip(COLS, outs):
        s.box(c, OY, 160, 50, t, sub, "gray")
    GY = 498
    s.box(35, GY, 890, 46, "sentinel_db.py (+ sentinel_schema.sql)", ["The only code that talks to the database"], "teal")
    s.box(35, GY + 80, 890, 46, "research.db", ["12 tables · schema v7 · one file · WAL mode · weekly verified backups"], "gray")
    s.line(480, GY + 48, 480, GY + 78, head=True, tail=True)
    s.line(115, 180, 115, PY - 2, tail=True); s.pill(115, 207, "1")
    s.line(345, 180, 345, PY - 2); s.pill(345, 207, "5")
    s.line(805, 180, 805, PY - 2, tail=True); s.pill(805, 207, "13")
    for c, n in zip(COLS, ("2", "6", "9", "12")):
        s.line(c + 185, 96, c + 185, PY - 2); s.pill(c + 185, 112, n)
    for c, n in zip(COLS, ("3", "7", "11", "14")):
        s.line(c + 80, PY + PH + 2, c + 80, OY - 2); s.pill(c + 80, (PY + PH + OY) / 2, n)
    for c, n, two in zip(COLS, ("4", "8", "10", "15"), (False, True, True, True)):
        s.line(c + 185, PY + PH + 2, c + 185, GY - 2, tail=two); s.pill(c + 185, GY - 30, n)
    BY = GY + 160
    s.box(35, BY, 690, 62, "sentinel_pipeline.py, started by launchd (sentinel_schedule.py installs it)",
          ["Runs all four as separate programs, on a schedule · checks results in research.db, not exit codes",
           "Weekdays 16:30 ET (retry 08:15) · Saturday 09:30 ET (see the orchestration diagram)"], "teal")
    s.box(755, BY, 170, 62, "Notifications", ["Mac + iPhone (ntfy.sh)", "one line per run"])
    s.line(727, BY + 31, 753, BY + 31); s.pill(740, BY + 12, "16")
    s.legend(BY + 92)
    rows = [("1", "Finviz to and from the scanner: Saturday screener views, quote pages (dossiers), weekday closes"),
            ("2", "positions.yaml and research_log.yaml to the scanner: each hit is tagged HELD, WATCH, NEW ..."),
            ("3", "Candidates CSV, for you to open"), ("4", "Scanner to the database: runs, tags, dossiers, prices"),
            ("5", "Alpha Vantage earnings dates and results, and the Wikipedia S&P 500 list, to the calendar"),
            ("6", "positions.yaml (holdings, gates, targets) to the calendar"),
            ("7", "The calendar page: the front end, rebuilt every weekday"),
            ("8", "Calendar reads scanner data and writes its own history (earnings, snapshots)"),
            ("9", "sentinel_tier1_rules.yaml and positions.yaml to Tier 1"),
            ("10", "Tier 1 reads prices, earnings, dossiers and tags; writes the alert queue and an audit log"),
            ("11", "Tier 1 report (console) and the open alerts: python3 sentinel_tier1.py --show"),
            ("12", "Investment_Strategy_Framework.md (every review) and positions.yaml (not during a calibration) to Tier 2"),
            ("13", "Tier 2 to and from Claude: one streamed request per alert, web search, cost capped"),
            ("14", "One review file per Saturday: verdict, four scores, 1A checks, sources, cost"),
            ("15", "Tier 2 reads open alerts and dossiers; writes tier2_reviews (never changes an alert's status)"),
            ("16", "One summary per run: OK or not, new alerts by ticker, Tier 2 verdicts. Never prices or cost.")]
    TY = BY + 136
    s.text(35, TY, "What each number is", "th")
    for i, (n, t) in enumerate(rows):
        s.pill(50, TY + 26 + 20 * i, n)
        s.text(72, TY + 26 + 20 * i, t, "t")
    s.text(35, TY + 26 + 20 * len(rows) + 10, "Nothing here trades or edits your files: you decide, then act at your broker.")
    s.save(out / "system_overview_v4.svg")


# ------------------------------------------------------------------------------------------------ 2. database
def database(out):
    s = Svg(960, 920, "Sentinel: the database", "The 12 tables in research.db, grouped by the program that writes "
            "them, and which program reads which tables.")
    s.text(35, 22, "research.db: 12 tables, schema v7 (Oct 4, 2026)", "tt")
    for c, (t, sub) in zip(COLS, [("Scanner", "Writes 5 tables"), ("Calendar", "Writes 4 tables"),
                                  ("Tier 1", "Writes 2 tables"), ("Tier 2", "Writes 1 table")]):
        s.box(c, 44, 190, 48, t, [sub], "teal")
        s.line(c + 170, 94, c + 170, 122)
        s.line(c + 170, 172, c + 170, 214)
    s.box(35, 124, 880, 46, "sentinel_db.py (+ sentinel_schema.sql)", ["The gate: every read and write goes through it · "
          "migrations are append-only (7 so far)"], "teal")
    s.panel(35, 216, 880, 440)
    s.text(55, 238, "research.db", "th")
    groups = [("Written by the scanner", [("screen_runs", "One row per scan"), ("screen_results", "Each hit, raw JSON"),
                                          ("screen_tags", "HELD, WATCH, NEW ..."), ("dossier_snapshots", "Parsed quote pages"),
                                          ("price_history", "Daily close and change")]),
              ("Written by the calendar", [("calendar_runs", "One row per page build"), ("earnings_events", "Held, watch, candidates"),
                                           ("quote_snapshots", "Live quotes, opt-in"), ("positions_snapshots", "Copy of positions.yaml")]),
              ("Written by Tier 1", [("escalations", "The alert queue"), ("tier1_runs", "Audit log, near misses")]),
              ("Written by Tier 2", [("tier2_reviews", "Verdict, scores, cost"), ]), ]
    for c, (label, tables) in zip(COLS, groups):
        s.text(c + 5, 266, label, "th")
        for i, (name, desc) in enumerate(tables):
            s.box(c, 282 + 62 * i, 190, 50, name, [desc], "gray")
    s.text(55, 610, "positions_snapshots holds your cost basis: keep research.db and its backups private.")
    s.text(55, 630, "The calendar can also write price_history (--fmp-history). Backups: backups/db/research_*.db, weekly, newest 8.")
    # read arrows, below the panel, drawn as labelled pills
    s.text(35, 690, "Who reads whose tables (reads also pass through sentinel_db.py)", "th")
    reads = [("1", "The calendar reads the scanner's tables: candidates, dossiers, stored prices."),
             ("2", "Tier 1 reads the calendar's earnings_events: the prints it needs for earnings surprises."),
             ("3", "Tier 1 reads the scanner's tables: prices (15% moves), dossiers and tags (Funnel B)."),
             ("4", "Tier 2 reads Tier 1's escalations (the open queue) and the scanner's dossier_snapshots."),
             ("5", "sentinel_pipeline.py reads price_history, screen_runs, escalations and tier2_reviews to judge each step.")]
    for i, (n, t) in enumerate(reads):
        s.pill(50, 716 + 22 * i, n)
        s.text(72, 716 + 22 * i, t, "t")
    s.text(35, 840, "The scanner never reads the calendar's, Tier 1's or Tier 2's tables. Tier 2 never changes an alert's status: you do, with")
    s.text(35, 858, "python3 sentinel_tier1.py --mark ID done|dismissed.")
    s.legend(884)
    s.save(out / "database_v4.svg")


# ------------------------------------------------------------------------------------------------ 3. orchestration
def orchestration(out):
    s = Svg(1000, 900, "Sentinel: scheduling and orchestration", "launchd starts sentinel_pipeline.py; the daily and "
            "Saturday jobs, how success is judged, what each run writes, notifications and optional publishing.")
    s.text(35, 22, "Schedule, jobs, checks and notifications (Oct 4, 2026)", "tt")
    s.box(35, 44, 930, 56, "Scheduler: launchd (installed by sentinel_schedule.py)",
          ["Daily: Mon-Fri 16:30 ET · retry Tue-Sat 08:15 ET (silent unless a problem or a new alert)   ·   Weekly: SATURDAY 09:30 ET"])
    s.line(500, 102, 500, 122)
    s.box(35, 124, 930, 46, "sentinel_pipeline.py", ["One command per job · a failed, hung or empty step never stops the steps after it (decisions 41-42)"], "teal")
    s.text(35, 192, "daily job (weekdays)", "th")
    s.text(520, 192, "weekly job (Saturday)", "th")
    daily = [("1. quotes", "sentinel_scan.py --quotes", False), ("2. calendar", "sentinel_calendar.py  (the front-end page)", False),
             ("3. publish (optional, OFF)", "--share page, only if publish-dir was run", True), ("4. tier1", "sentinel_tier1.py", False)]
    weekly = [("1. scan + dossier", "sentinel_scan.py --dossier", False), ("2. tracked dossiers", "--dossier-only --held --watch", False),
              ("3. tier1", "sentinel_tier1.py (the new screen's candidates)", False),
              ("4. tier2", "sentinel_tier2.py: Claude reviews every open alert (costs money)", False),
              ("5. backup", "SQLite backup API, verified, newest 8 kept", False)]
    for i, (t, sub, dashed) in enumerate(daily):
        s.box(35, 206 + 54 * i, 445, 44, t, [sub], "gray" if dashed else "teal", dashed)
        if i:
            s.line(257.5, 206 + 54 * i - 10, 257.5, 206 + 54 * i - 2)
    for i, (t, sub, dashed) in enumerate(weekly):
        s.box(520, 206 + 54 * i, 445, 44, t, [sub], "teal", dashed)
        if i:
            s.line(742.5, 206 + 54 * i - 10, 742.5, 206 + 54 * i - 2)
    s.box(35, 488, 930, 62, "Success is judged from research.db, not the exit code (decision 42)",
          ["quotes: enough tracked US names have the expected close · calendar: the page was rewritten · scan: a NEW run was saved",
           "tier2: reviews were stored in tier2_reviews (or 'nothing to review') · backup: PRAGMA integrity_check"])
    s.text(35, 576, "What each run writes", "th")
    for i, (t, sub) in enumerate([("logs/*.log", "Full output"), ("logs/status.json", "Read by 'status'"),
                                  ("backups/db/", "Saturday, newest 8"), ("reviews/", "Tier 2, Saturday")]):
        s.box(35 + 235 * i, 590, 225, 46, t, [sub], "gray")
    s.text(35, 664, "Notifications: one line per run, tickers and verdicts only", "th")
    s.box(35, 678, 300, 50, "macOS notification", ["Built: every run (decision 50)"], "teal")
    s.box(350, 678, 300, 50, "iPhone via ntfy.sh", ["Built: same line, urgent on FAIL/new (51)"], "teal")
    s.box(665, 678, 300, 50, "healthchecks.io", ["Not built: alerts if the Mac never runs"], None, True)
    s.text(35, 756, "Publishing: built, OFF until you run publish-dir (decision 44)", "th")
    for i, (t, sub) in enumerate([("Shareable page", "calendar --share"), ("Privacy check", "refuses cost / GTC / notes"),
                                  ("Web folder or git push", "one file, never overwrites"), ("GitHub Pages / web server", "off")]):
        s.box(35 + 235 * i, 770, 225, 46, t, [sub], "gray", True)
        if i:
            s.line(35 + 235 * i - 8, 793, 35 + 235 * i - 2, 793)
    s.legend(836)
    s.save(out / "orchestration_v2.svg")


# ------------------------------------------------------------------------------------------------ 4. flows
PANELS = [
    ("Scanner: Saturday screen", "sentinel_scan.py, no flags (inside the weekly job)", [
        ("1. Fetch the Finviz screener views", "fetch_view(), polite_delay()", ("Finviz", "Screener pages", None)),
        ("2. Merge the views with pandas", "Joined on ticker", ("In memory", "One row per ticker", "gray")),
        ("3. Write the CSV", "Your human copy, written first", ("Candidates CSV", "candidates_DATE.csv", "gray")),
        ("4. Save the run", "save_run_to_db() -> db.insert_screen_run", ("research.db", "screen_runs, results", "gray")),
        ("5. Diff against the last comparable run", "print_diff()", ("research.db", "Reads earlier run", "gray")),
        ("6. Tag every hit against your files", "tag_run(): load_ledger(), classify() -> db.insert_screen_tags", ("Your files", "positions, research_log", "amber")),
        ("7. Optional dossier stage", "run_dossier_stage()", ("Next panel", "Scanner: dossiers", "gray"))]),
    ("Scanner: dossier stage", "--dossier, or --dossier-only for a saved run (dossier_only())", [
        ("1. Choose which names to fetch", "choose_dossier_targets()", ("positions.yaml", "And saved tags", "amber")),
        ("2. Fetch each quote page", "fetch_quote_html()", ("Finviz", "One page per name", None)),
        ("3. Parse the page", "sentinel_finviz.parse_quote_page", ("dossier_debug/", "Only if parse fails", "gray")),
        ("4. Save the snapshot", "db.insert_dossier", ("research.db", "dossier_snapshots", "gray")),
        ("5. Print what changed", "sentinel_finviz.describe_changes", ("Console", "Since last snapshot", "gray"))]),
    ("Scanner: daily prices", "--quotes, after 4 pm New York time (run_quotes())", [
        ("1. Check the New York clock", "market_state()", ("Clock", "Refuses while open", None)),
        ("2. Pick the names", "quote_targets()", ("positions.yaml", "US tracked, NEW", "amber")),
        ("3. Fetch price and day change", "fetch_quote_rows()", ("Finviz", "Ticker-list pages", None)),
        ("4. Store the closes", "db.upsert_prices", ("research.db", "price_history", "gray"))]),
    ("Calendar: daily (the front end)", "sentinel_calendar.py", [
        ("1. Read your holdings and gates", "load_positions()", ("positions.yaml", "Holdings and gates", "amber")),
        ("2. Fetch index list, earnings dates and results", "fetch_sp500_constituents(), get_earnings_rows() (stored dates if the source is down)", ("Wikipedia, Alpha Vantage", "~3-8 requests a day", None)),
        ("3. Read scanner data", "load_db_context()", ("research.db", "Candidates, prices", "gray")),
        ("4. Compare and flag", "build(), build_card(), build_candidate_card(), date_flag(), position_flags()", ("In memory", "Cards and flags", "gray")),
        ("5. Write the page", "render_static(), render_html()", ("Calendar page", "earnings_calendar.html", "gray")),
        ("6. Save the run", "save_calendar_run()", ("research.db", "Events, snapshots", "gray"))]),
    ("Tier 1: daily, and Saturday after the screen", "sentinel_tier1.py", [
        ("1. Load the thresholds", "load_rules()", ("sentinel_tier1_rules.yaml", "15%, 10%, Funnel B", "amber")),
        ("2. List the tracked names", "tracked_names()", ("positions.yaml", "Held and watch", "amber")),
        ("3. Funnel A: tracked names", "funnel_a(), unified_moves()", ("research.db", "Prices, earnings", "gray")),
        ("4. Funnel B: screen candidates", "funnel_b()", ("research.db", "Dossiers, tags", "gray")),
        ("5. Queue and log", "store_results() -> db.insert_escalation, db.insert_tier1_run", ("research.db", "Queue, audit log", "gray")),
        ("6. Print the report", "format_report()", ("Console", "Incl. near misses", "gray"))]),
    ("Tier 2: Saturday, after Tier 1 (built Sep 26-27)", "sentinel_tier2.py (DECISIONS.md 49)", [
        ("1. Pick the alerts", "open, not yet reviewed (db.reviewed_escalation_ids); calibration must have passed", ("research.db", "escalations", "gray")),
        ("2. Check the budget", "db.tier2_cost_since(); $15/month, 5 names/run", ("research.db", "tier2_reviews", "gray")),
        ("3. Build the request", "build_user_message(), build_request(): framework cached", ("Your files", "framework, positions", "amber")),
        ("4. Ask Claude, streamed", "run_conversation(), http_post(): web search, max 10", ("Claude API", "Sonnet 5", None)),
        ("5. Read and check the answer", "parse_result(): verdict vocabulary, scores 1-5", ("In memory", "Sources collected", "gray")),
        ("6. Store and write the review", "db.insert_tier2_review, write_report()", ("reviews/", "DATE_tier2.md", "gray"))]),
]


def flows(out):
    step_h, gap, head = 46, 10, 56
    heights = [head + len(p[2]) * (step_h + gap) + 14 for p in PANELS]
    total = 70 + sum(heights) + 20 * len(PANELS) + 100
    s = Svg(1000, total, "Sentinel: one run of each program", "Six panels: the scanner's screen, dossiers and daily "
            "prices, the calendar, Tier 1 and Tier 2, each with its steps in order and the Python functions that do the work.")
    s.text(35, 22, "One run of each program, with the Python function names (Oct 4, 2026)", "tt")
    y = 50
    for (title, sub, steps), h in zip(PANELS, heights):
        s.panel(25, y, 950, h)
        s.text(45, y + 20, title, "th")
        s.text(45, y + 38, sub)
        for i, (st, fn, (ft, fs, fcls)) in enumerate(steps):
            yy = y + head + i * (step_h + gap)
            s.box(45, yy, 640, step_h, st, [fn], "teal")
            s.box(745, yy, 210, step_h, ft, [fs], fcls, False)
            s.line(687, yy + step_h / 2, 743, yy + step_h / 2, head=True)
        y += h + 20
    s.text(35, y + 4, "Arrows point from each step to the file or service it touches. Every db.* call is a function in sentinel_db.py.")
    s.text(35, y + 24, "Since Phase 8, sentinel_pipeline.py runs these as separate programs, with a time limit and a database check.")
    s.legend(y + 50)
    s.save(out / "flows_v4.svg")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "diagrams"))
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in (system_overview, database, orchestration, flows):
        f(out)
    print(f"Wrote 4 diagrams to {out}/: system_overview_v4.svg, database_v4.svg, orchestration_v2.svg, flows_v4.svg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

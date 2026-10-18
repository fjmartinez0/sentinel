# Decisions log (continued)

Continues the numbered "Key decisions log" in PROJECT_HANDOFF.md (items 1-12). Newest last.
A decision here is only ever changed by adding a NEW entry that says it supersedes the old one.

13. **2026-09-20: The single-day price-move threshold for tracked names stays at 15%.** Decided by you
    when Tier 1 was built (Phase 6). The caveat from item 8 stands: NYT's real deterioration surfaced at a
    12-15% one-day drop, right at the edge, so a move like that would not escalate. The 10% earnings-surprise
    threshold is unchanged. Instead of changing the number, Tier 1 logs every near miss (80% or more of a
    threshold) so this can be revisited with evidence. Revisit after one earnings season of history; if 12-15%
    moves keep mattering, prefer adding a second trigger (guidance cut, repeated target cuts) over lowering the
    number. The rule and its reasoning also live in `tier1_rules.yaml`.

14. **2026-09-20: Funnel B (screen candidates) starter rule: dislocation AND conviction, capped per run.**
    PROPOSED by Claude and built as the default; not yet reviewed by you, so tune freely. A NEW candidate
    escalates to Tier 2 only if it shows at least one dislocation signal (52-week drawdown of 30% or more, or a
    one-day drop of 10% or more in the last 30 days) AND at least one conviction signal (an open-market insider
    buy in 90 days, an analyst upgrade in 30 days, or a consensus target 25% or more above the price), with at
    most 5 escalations per run. Reason: the earlier sketch ("insider buy, or upgrade, or no target cuts") would
    have escalated almost everything, since the screen already requires insider buying and buy-rated analysts.
    Resolves open item 1 of the handoff (Funnel B escalation depth) provisionally.

15. **Sep 2026: The scripts stay separate and are linked through shared files, not merged.** The calendar,
    the Finviz scanner, and (later) the analysis step share only `positions.yaml`, `research_log.yaml`,
    `research.db`, and small helper modules. Each database table has one writer. Stated by you when the
    database was designed.

16. **Sep 2026: `positions.yaml` is the master for structured position data; the ledger markdown stays the
    narrative.** When a trade fills or a target changes, edit `positions.yaml` first.

17. **Sep 2026: Phase 0 prep (Finviz Canadian-ticker check, Custom view, framework/scorecard text) is deferred.**
    Still open, and still needed before Phase 7: the sleeve definitions and the scoring rubric behind "18/20"
    were not in the handoff and must be supplied as `framework.md`.

18. **2026-09-20: Funnel B thresholds get a 10% tolerance band.** Requested by you after GEHC (29% below its
    52-week high) was rejected against a 30% rule. A signal within 10% of its threshold now counts, marked
    "borderline", and ranks below full-strength signals; set `borderline_ratio: 1.0` in `tier1_rules.yaml` to
    switch it off. Funnel B only: the Funnel A thresholds (15% move, 10% surprise) are unchanged, per item 13, and
    a value just under them is logged as a near miss. If you want a tolerance on Funnel A too, say so and add it
    as a new entry here.

19. **2026-09-20: Open question, target gap as the only conviction signal.** In the first real run OLED escalated
    on a consensus target 46% above its price alone. Sell-side targets lag a falling price, so that gap is close
    to automatic for a stock that has fallen a lot. Recommendation: set `target_upside_counts_alone: false`
    (a target gap can support and rank a candidate but not qualify it). Left as `true` until you decide.

20. **2026-09-20: Daily prices come from Finviz, not FMP.** `--fmp-check` showed the FMP plan serves quotes and
    daily history only for a few symbols (AAPL answered; NYT, QTWO and ATRL.TO were refused as "not available
    under your current subscription"). `scan_finviz_candidates.py --quotes` fetches Price and the day's change
    for all US tracked names and NEW candidates in about two requests, and stores them in `price_history`.
    FMP is still used for the earnings calendar. Canadian names have no daily price source until one is found.

## Design decisions taken during the build (Sep 2026)

These were made while building Phases 1-6 and were not written down at the time. Each was either instructed by
you, agreed by you when proposed, or (marked "Claude's default") built as a sensible default for you to change.

21. **The ledger snapshot is Aug 28, 2026 and is the latest one; refreshing it was deferred.** `positions.yaml`
    carries an `as_of` date and the calendar warns when it is more than 14 days old. Several dated gates in it
    (CHWY Sep 2-10, HUBS Sep 17) have already passed with no outcome recorded.

22. **One database file, `research.db`, in the project folder.** It must stay out of iCloud, Dropbox and OneDrive
    (sync clients can corrupt SQLite; `db.py` warns). WAL mode. Schema changes are additive numbered migrations in
    `schema.sql`, each in one transaction; a database newer than the code is refused. Tickers are keyed by the
    FMP-style symbol (`ticker`) with the Finviz spelling kept (`finviz_symbol`); no symbol overrides are guessed.

23. **Ingestion stores what was observed, uninterpreted.** Scanner rows and dossiers are stored as raw strings
    (Finviz's own formatting). Judgment (routine vs discretionary insider selling, "is this a dislocation") belongs
    to Tier 1 rules and the future Tier 2, never to ingestion.

24. **One writer per table**, with one honest exception: `price_history` is written by the scanner (`--quotes`,
    Finviz, the default) and, only if you opt in with `--fmp-history`, by the calendar (FMP).

25. **The CSV stays as the human copy; the database is the machine path.** The CSV is written first and a database
    failure never blocks it (same for the calendar page). Nothing reads the CSV except you (and `--import-csv`).

26. **Scanner tagging.** Every hit is tagged HELD / WATCH (from `positions.yaml`) or a `research_log.yaml` status, else
    NEW. `positions.yaml` wins any conflict. Runs imported from old CSVs are not tagged automatically (today's
    ledger applied to an old run would mislead); use `--retag`. "New / dropped since last run" compares like for
    like (same filter string) and flags an imported-CSV baseline as weaker.

27. **`research_log.yaml` is seeded from ledger sections 2-6 (73 names)** and is your judgment record, only
    transcribed. Not seeded because the handoff never names them: the July 2026 semiconductor/AI-capex basket (19
    names) and the gold/silver miners (10). SOFI and RDDT are on the ledger's reject lists AND the watch list; watch
    wins. Only CSGP was seeded for "CSGP/CSGO". Canadian tickers use the `.TO` suffix.

28. **The Finviz dossier stage** reads a quote page per name. Off unless asked (`--dossier` / `--dossier-only`);
    NEW names only by default (`--tracked`, `--held`, `--watch`, `--tickers` widen it); at most 12 pages per run; a
    good snapshot under 7 days old is reused; it stops after two unreadable pages in a row; failed parses are stored
    with `parse_ok = 0` and the page saved to `dossier_debug/`; Canadian tickers are skipped (Finviz is US-only).
    The parser lives in its own module (`finviz_quote.py`) and recognises tables by their contents, not CSS classes.

29. **Calendar and the database.** The calendar reads scanner results (NEW candidates as a fourth event kind,
    dossier summaries, "changed since last snapshot" badges, FMP date-move flags) and saves its own history for
    tracked names only (not the S&P 500). `--share` never reads or writes the database and never includes positions.
    `--offline` never writes and reads a database only if you name one. With no database the page is exactly as it
    was before this feature (verified against the previous version). A copy of `positions.yaml` (with cost basis)
    is saved to the database only when it changes; treat `research.db` as private.

30. **Calendar page rules.** A held name's card leads with its decision test (the gate) or, if none is written, a
    prompt to write one. Flags: GTC price vs target mismatch, fractional shares that cannot carry a GTC, ledger date
    vs FMP date (FMP's `confirmed` field is not proof), passed gates with no recorded outcome, price missing. The
    "recently reported" table applies the 10% surprise rule. Ledger gates that are not earnings appear as events.

31. **Tier 1 is mechanical and logs everything.** No Claude call. Near misses (80% or more of a threshold) are
    logged in `tier1_runs` and listed by `--near-misses`. A move only counts as single-day if measured that way
    (Finviz's own day change is read directly; plain closes more than 4 days apart are not compared). Earnings
    surprises come from the calendar's latest numbers for each print. Each event escalates once (unique key), so
    re-running never duplicates. Funnel B: a NEW candidate with a stored dossier needs a dislocation signal AND a
    conviction signal, at most 5 per run, and is not re-escalated within 30 days unless it shows new evidence.
    Escalations wait in a queue (`open` / `done` / `dismissed`) for a Tier 2 look.

32. **Daily prices come from Finviz and only ever as closes.** `--quotes` stamps the right trading date: after 4 pm
    New York time it is today; before 9:30 am or on a weekend it is the previous session; while the market is open
    it refuses (the price would be intraday). US holidays are not modelled. Tier 1 only sees a move on a day
    `--quotes` ran, so it needs a daily run. Canadian names have no daily price source yet.

33. **FMP is used for the earnings calendar only.** Quotes and history are opt-in (`--fmp-quotes`,
    `--fmp-history`) because the plan refuses most symbols; `--fmp-check` diagnoses this. When FMP refuses, US
    tickers are tried first and only US refusals count toward giving up.

34. **Holding cards use the stored Finviz close** (at most 5 days old) when there is no live FMP quote, then the
    ledger price. A live quote takes precedence.

35. **Diagram conventions.** Teal = Python code, amber = files you edit by hand, gray = files the code creates,
    plain outlined box = outside service, dashed outline or arrow = planned / not built, numbered badges = order in
    one cycle. Do not change a diagram's layout without saying so. The diagrams are currently out of date (they
    predate Tier 1, the dossier, the quotes mode and the calendar's database features); the handoff file tells the
    next assistant to redraw them first.

36. **Build phases.** 0 prep (deferred by you) - 1 database - 2 scanner saves runs - 3 research log and tagging - 4
    dossier fetch - 5 calendar reads the database - 6 Tier 1 - 7 analysis script with the Claude API (needs
    `framework.md`, an API key, credits and a spend limit) - 8 run it as a routine (scheduling, logs, backups).
    Phases 1-6 are done. Claude recommends Phase 8 before 7.

37. **Rules for the future Tier 2 (not built).** Verdicts use the ledger's vocabulary (watch / held / disqualified /
    parked) plus a numeric fit score; every hard date or figure must carry a source URL or be rejected; no
    binary-catalyst predictions (item, standing non-goal); it never edits `positions.yaml` or places orders; token
    and search caps apply and cost is logged; you decide. It is a separate script, in line with item 15.

38. **Company names in `positions.yaml` and `research_log.yaml` for tickers the ledger did not name (the Canadian
    ones and several others) were filled in from Claude's general knowledge.** Verify them.

39. **Testing approach.** The assistant's sandbox cannot reach FMP, Finviz or the Anthropic API, so network code is
    tested with mocks and fixtures (including your real PSN page) and then verified by you on the first real run.
    A first real run of new network code doubles as its probe: it is built to save the page and describe any
    surprise instead of failing silently.

40. **2026-09-20: Resolves item 19. `target_upside_counts_alone` is now `false`.** A consensus-target gap can
    support and rank a Funnel B candidate but cannot qualify it alone (OLED-style escalations stop; a candidate
    now needs an insider buy or an analyst upgrade as its conviction signal). This follows the recommendation
    in item 19 and was applied by Claude when you said this should not be a manual step. It was not argued on
    the merits by you, so treat it as provisional: to reverse it, set the flag back to `true` in
    `tier1_rules.yaml` and add a new entry here. The rules file version goes from 2 to 3 so the audit log
    (`tier1_runs`) can tell runs made under the old and new settings apart.

41. **2026-09-20: Phase 8 is a Python orchestrator, not shell scripts.** `run_pipeline.py` (daily, weekly, backup, status,
    selfcheck) runs the existing programs as separate subprocesses, so item 15 stands. launchd user agents start it:
    daily Mon-Fri 16:30 New York time, a retry Tue-Sat 08:15 (Finviz still shows the previous close before the open),
    weekly Sunday 09:00. `install_schedule.py` installs and removes it. A step that fails, hangs or stores nothing never
    stops the steps after it. Exit code 0 = OK, 1 = warnings, 2 = a failure.

42. **2026-09-20: Success is judged from the database, not the exit code.** The programs exit 0 even when they store
    nothing (the first `--quotes` run did). So the runner checks that the tracked US names have the expected close
    (FAIL under 90%), that a scan saved a NEW run, that the calendar page was rewritten, and that the backup passes
    `PRAGMA integrity_check`. Failures, new Tier 1 escalations and missed runs raise one macOS notification; every run
    writes `logs/*.log` and `logs/status.json`; `python3 run_pipeline.py status` shows it all. A missed daily price
    cannot be filled in later (Finviz shows only the latest close), which is why the morning retry exists.

43. **2026-09-20: Operating conditions.** Weekly backups use SQLite's backup API into `backups/db/` (newest 8 kept).
    The FMP key is read from a private `.env` (mode 600) because launchd jobs have no shell environment. The Mac must
    not sleep (`sudo pmset -a sleep 0`) and you must be logged in (these are user agents). Scheduled jobs on macOS
    cannot read Documents, Desktop or Downloads without extra permission, so the project folder should live elsewhere;
    the installer's self-check tests this through launchd itself.

44. **2026-09-26: The project is named Sentinel; every script renamed under a `sentinel_` prefix.** Reason:
    generic names like `db.py` and `tier1.py` were becoming hard to track across a growing file set and risked
    colliding with other Python projects on import. Old name -> new name, one-for-one, no logic changed beyond
    the cross-references each rename required (imports, the orchestrator's `SCRIPTS` dict, the installer's
    `REQUIRED_FILES` list and its launchd `ProgramArguments`, every docstring and help line naming another
    file). Old->new: `db.py`->`sentinel_db.py`, `schema.sql`->`sentinel_schema.sql`,
    `finviz_quote.py`->`sentinel_finviz.py`, `scan_finviz_candidates.py`->`sentinel_scan.py`,
    `generate_earnings_calendar.py`->`sentinel_calendar.py`, `tier1.py`->`sentinel_tier1.py`,
    `tier1_rules.yaml`->`sentinel_tier1_rules.yaml`, `run_pipeline.py`->`sentinel_pipeline.py`,
    `install_schedule.py`->`sentinel_schedule.py`, `probe_quote_page.py`->`sentinel_probe.py`. Test files
    renamed to match (`test_sentinel_*.py`). **Not renamed:** `positions.yaml`, `research_log.yaml`,
    `research.db`, `.env`, `DECISIONS.md`, the ledger and framework `.md` files, and
    `generate_earnings_calendar_v1.py` (a frozen historical test fixture, not an active script). Entries above
    this one use the old names; this entry is the mapping to read them by.

45. **2026-09-26: Project folder moved, from `/Users/fernandomartinez/Documents/python_projects/first_project` to `/Users/fernandomartinez/personal_projects/sentinel`.** Was under `~/Documents`, which macOS restricts for scheduled jobs and which iCloud's "Desktop & Documents" sync (if enabled) can silently corrupt a SQLite database mid-write (Decision 22's warning, never fully closed out). Done by
    `relocate_to_sentinel.py`: research.db, positions.yaml, research_log.yaml, .env, logs/, backups/, and
    DECISIONS.md itself were copied over as-is (never overwriting anything already at the destination); the
    schedule was uninstalled from the old location and reinstalled from the new one. The old folder was left
    untouched, nothing deleted -- delete it yourself once `sentinel_pipeline.py status` looks right from the
    new location.

46. **2026-09-26: Log reconciliation -- three decisions restored, number collision explained.** Three
    decisions made in earlier chats were only ever written to a working copy of this file, never to this
    one. When `sentinel_rename` and `relocate_to_sentinel.py` appended here, the file ended at 43, so they
    took numbers 44 and 45. As a result, PROJECT_HANDOFF.md and the diagrams (`orchestration_v1.svg`,
    `system_overview_v3.svg`) use different numbers from this file. Mapping, handoff/diagram number ->
    where it lives now: "Decision 44" (publishing) -> 46a below; "Decision 45" (iPhone notifications) ->
    46b; "Decision 46" (framework ready) -> 46c; "Decision 47" (rename) -> entry 44 of this file.
    Items 1-12 live in the older PROJECT_HANDOFF.md, not in the current handoff package.
    (a) **Publishing: built, off.** `sentinel_pipeline.py` can publish the `--share` calendar page to a
    web folder or, since 2026-09-21, commit and push it to a git remote for GitHub Pages. A privacy check
    refuses cost basis, GTC orders and notes. Nothing is published until `publish-dir` is run once.
    Standing recommendation: the shareable page only. FMP's terms for republishing earnings dates were
    never checked.
    (b) **iPhone notifications: designed, not built, deferred by you on 2026-09-26.** Two options:
    ntfy (push to the phone on FAIL/WARN or a new escalation, mirroring the macOS alert) and
    healthchecks.io (a dead-man's switch that alerts when an expected run never checks in, e.g. the
    Mac is off). No choice made.
    (c) **Framework: `Investment_Strategy_Framework.md`, revised 2026-09-25, is the `framework.md`
    that items 17, 36 and 37 require for Phase 7.** Open conflict: the handoff replaced item 37's
    "numeric fit score" with a written verdict, but framework section 1 (revised the same day) now
    requires the four scores to be recorded. Claude's proposal: Tier 2 outputs both, the verdict in
    ledger vocabulary plus the four sub-scores as proposals you confirm. Not yet decided by you.

47. **2026-09-26: First manual review of the Tier 1 queue (escalations 1-4, all Funnel B from the
    2026-09-20 scan).** AMRZ -> watch (Sleeve 1, gate: Q3 print 2026-10-27). COO -> watch (Sleeve 1B
    candidate, needs a dated corporate action from the Jana activist campaign). BMI -> dismissed
    (chronic multi-quarter decline, framework 1A check 2; re-add after two straight quarters of
    year-over-year sales growth). OTF -> dismissed (a BDC: the screen's margin, P/FCF and debt filters
    are not meaningful for it, and the framework has no rules for credit vehicles). AMRZ and COO added
    to `positions.yaml`. Findings, PROPOSED and not built: (1) Tier 1 should count distinct insiders,
    not transactions -- AMRZ's "13 buys" were almost all one buyer, the CEO; (2) exclude BDCs and
    other credit vehicles from Funnel B; (3) Tier 1 has no chronic-decline check, which is correctly
    left to Tier 2 (BMI shows why it is required). These four are the first calibration set for
    Phase 7. Also confirmed: "tracked dossiers: 0 page(s) stored" on 2026-09-26 was the 7-day
    snapshot reuse (item 28), not a failure; the status line should say "reused N" (proposed).

48. **2026-09-26: Framework sync; Tier 2 deferred.** `Investment_Strategy_Framework.md` updated (the
    previous version, if present, backed up as `Investment_Strategy_Framework_backup_2026-09-25.md`).
    Brings the framework in line with changes the ledger had already made and with Sentinel running.
    Decided by you: (1) the index rule is a presumption, not an exclusion, and covers the top-15 QQQ
    names AND every S&P 500 constituent, because you hold both index funds; accepted side effect: most
    Large-cap screen hits need a named, dateable catalyst before they are scored. (2) The +20% exit
    floor binds new entries and new targets; existing GTC orders below it (ATRL, EFN, ALB, MELI, DUOL,
    plus ASML's target) are re-checked at each position's next earnings. (3) Tier 2 (Phase 7) is
    DEFERRED: Tier 1 produced 4 escalations in its first week, few enough to review by hand in a Claude
    chat; revisit when the queue regularly outgrows one session. Its output format (verdict and/or
    scores) stays undecided until then. This supersedes item 36's "Phase 7 is next". Also written in:
    count distinct insiders, not transactions; BDCs and credit vehicles out of scope; the screen
    description now matches the Aug 22 verified filters, with `sentinel_scan.py` as the authority
    (not yet cross-checked against it).

49. **2026-09-26/27: Tier 2 built and calibrated (reverses item 48's deferral).** `sentinel_tier2.py` reviews Tier 1
    escalations with Claude (claude-sonnet-5, Anthropic API, web search, streamed), writes reviews to `reviews/`
    and the `tier2_reviews` table (schema migration 7). Advice only: never closes alerts, never edits files, never
    trades. Output: verdict plus the four scores. Spending rails: 5 names per run, 10 searches per name, $15/month
    budget, prepaid credits with auto-reload OFF as the hard cap. Real cost so far: about $0.50 per name ($2.54 total
    including one lost timed-out call, which led to switching to streaming). Calibration PASSED 4/4 on 2026-09-27
    (AMRZ watch, COO watch, BMI dismiss, OTF dismiss), BUT the answers were visible to it at the time (framework
    section 10 and positions.yaml), so it is weak evidence: for the first 2-3 weeks every live review is also checked
    by hand in a Claude chat. Fixed since: section 10 no longer holds the answers, and calibrations no longer see
    positions.yaml. These four names are now framework precedents, so any future calibration needs NEW names.
    Correction: item 47's claim that AMRZ's insider buying was "almost all the CEO" was WRONG; SEC filings show at
    least seven executives buying in Aug-Sep 2026. Framework updated: that example corrected; trivial purchases
    excluded from insider counts; section 1A check 3 clarified so a securities class action alone does not
    disqualify a name. Known, unrelated: one Tier 1 test (K7 re-escalation) was already failing before this work.

50. **2026-09-27: The weekly run moves to Saturday 09:30 ET and now includes Tier 2; every run sends a
    summary notification.** Reason: reviews should be ready on the weekend, so orders can be placed on Sunday
    before Monday's open (use limit orders: weekend orders fill at the open, which can gap). Weekly job:
    scan + dossiers -> tracked dossiers -> Tier 1 -> Tier 2 -> backup (was Sunday 09:00, without Tier 1/Tier 2).
    Tier 2 never runs in the daily job, because every review costs money; weekday alerts wait for Saturday.
    Every run sends ONE macOS notification (tickers and verdicts only, never money); the 08:15 retry stays
    silent unless something failed or is new, so silence on a weekday means the run did not happen. Tier 2
    success is judged from the tier2_reviews table, not its exit code. The installer and selfcheck now also
    require sentinel_tier2.py, the framework file, the anthropic package and ANTHROPIC_API_KEY.
    Notifications are Mac-only; iPhone (ntfy) still undecided. Framework sections 6, 6A and 10 updated.

51. **2026-09-27: iPhone notifications via ntfy (resolves the ntfy half of item 46b).** Every run's one-line
    summary also goes to a private ntfy.sh topic the iPhone app subscribes to (the topic name, stored in .env,
    works like a password: long and random, made by `python3 sentinel_pipeline.py phone`). High priority for
    failures and new alerts, normal for the daily all-clear; tickers and verdicts only, never money. A phone
    that cannot be reached never breaks a run. `phone --new` replaces the topic; `phone --off` stops it.
    healthchecks.io (a dead-man's switch for the Mac being off) was NOT added: the daily message already
    makes silence meaningful. Revisit if missing messages go unnoticed.

52. **2026-10-04: Earnings data moves from FMP to Alpha Vantage; secrets masked everywhere.** From Oct 1 FMP
    answered "402 Payment Required": its earnings calendar is now a paid feature. Every weekday's calendar step
    FAILED, and the error text carried the FMP key (in the request URL) into notifications sent to ntfy.sh.
    Now: (1) the calendar gets upcoming earnings from Alpha Vantage's free EARNINGS_CALENDAR (one request a day,
    all US companies, 3 months ahead) and, for tracked US names that reported in the last 5 days without a result
    yet, actual vs estimated EPS from EARNINGS (at most 6 per run, 13 s apart). About 3-8 requests a day against
    a free limit of about 25. Revenue surprises are no longer available, so Tier 1's 10% rule works on EPS only.
    (2) If Alpha Vantage fails or the daily limit is reached, the page is made from the dates the last run
    stored and the pipeline reports WARN, not FAIL. (3) The pipeline masks secrets before anything is logged,
    stored in status.json or notified: key=value pairs in web addresses, Anthropic keys, and the literal value
    of every key or topic in .env. (4) FMP is optional (only the --fmp-* extras); the calendar's required key
    is ALPHAVANTAGE_API_KEY, saved with `python3 sentinel_pipeline.py av-key`, checked with
    `python3 sentinel_calendar.py --av-check`. Known small loss: past S&P 500 reports drop off the page once
    their date passes (Alpha Vantage's calendar only looks forward); tracked names keep theirs.

53. **2026-10-10: The schedule was off Oct 2-10 without anyone noticing; fixes so it cannot happen silently.**
    Cause: to stop the Oct 3 Saturday run, the schedule was uninstalled (one of two options Claude offered),
    and the Alpha Vantage install steps (item 52) never said to reinstall it; `sentinel_schedule.py --selfcheck`
    only tests, it does not reinstall. No runs Oct 3-9: no prices, alerts or notifications. The code was not at
    fault (a daily job run in a bare, launchd-like environment worked). Fixes: (1) `status` now starts with
    "Schedule: ON" or "Schedule: OFF ... NOTHING runs automatically" (checked with launchctl) and reports weekdays
    missed since the last daily run. (2) `python3 sentinel_pipeline.py skip weekly` (or `skip daily`) skips only
    the next run of that job, sends a "skipped as you asked" notification, and clears itself; `skip --off` cancels.
    Use this instead of uninstalling. (3) `--uninstall` now warns that nothing runs until reinstalled. (4) Secrets
    left in old status.json entries are masked on the next write. (5) Calendar wording "FMP has" became "the
    earnings source has". Rule for future instructions: any step that turns the schedule off must be paired with
    the step that turns it back on.

54. **2026-10-18: Finviz redesigned its screener; prices and the Saturday screen failed Oct 17, fixed Oct 18.**
    From Oct 17 08:15 the screener pages (used for daily prices and the Saturday screen) came back with no
    <table>: the results are now a `<div role="grid">` with header and cell roles. pandas fell back to a parser
    that is not installed and the whole step crashed with a misleading "install html5lib" error, so the
    Oct 17 daily prices and the Saturday screen stored nothing (Tier 2 that day only reviewed names already
    open). Not a block: the saved page held every price. Fixes: (1) `find_result_table` reads the new grid
    first (header labels become the same column names as before, cell text unchanged, ticker from the cell's
    `data-ticker`, so the doubled-letter fix is not applied there, which would have turned AAL into AL), and
    still reads the old table. (2) A page with no table returns nothing instead of crashing, and is saved to
    probe_out/ with a PROBLEM line. (3) The screen stops if Finviz ignores the page offset (it would otherwise
    fetch the same page 10 times). Verified on the Mac Oct 18: 34/34 prices, screen 18 candidates x 55 columns.
    The real Oct 17 page is kept as QUOTES_grid_page1.html for the tests. Same day: BMI and OTF added to
    research_log.yaml as disqualified (BMI dismissed twice by Tier 2; OTF is a BDC, out of scope), so they stop
    being re-reviewed.

55. **2026-10-18: Tier 2 moved from the API to the Claude Pro subscription (the investor's decision).**
    Claude recommended staying on the API (Pro limits are shared with chat; an unattended run can hit them;
    login expiry). The investor chose the subscription to stop spending credits. How: `claude -p` (Claude Code
    non-interactive mode) authenticated with the documented long-lived token from `claude setup-token`
    (CLAUDE_CODE_OAUTH_TOKEN, valid one year, stored in .env by `python3 sentinel_tier2.py --save-token`).
    Same rules, framework, message, verdicts, calibration and <result> block as the API path; only WebSearch is
    allowed, no prompts, no saved session, no MCP servers, an empty working folder and a private config folder
    (.claude_tier2/). Every API credential is removed from that process's environment, because Claude Code
    uses an API key over the subscription when both are present. Reviews are stored at $0 (the review file shows
    what the API would have cost). If the plan's usage limit is hit, the run stops and the remaining names stay
    open for the next run (not recorded as failed). Switch with `--use-api` / `--use-subscription`;
    `--sub-check` proves it works. The API key and code remain as the fallback. Renew the token before
    Oct 2027 (`claude setup-token`, then `--save-token`). Pending: recalibrate once on the subscription.

56. **2026-10-18: First calibration on the subscription; COO answer changed; reviews get their own tab in the tracker.**
    (1) The subscription calibration matched on AMRZ, BMI and OTF and differed on COO (Tier 2 dismiss 11/20 vs the
    investor's Sep 26 watch 12/20). Same facts both times; the split was the section 1A(b) rule. Tier 2 counted the
    Sep 10 cut as the fourth guidance cut in two years (the BSX/ZTS repeated-cut precedent); the investor had weighed
    JANA's activist letter and three directors' buying more heavily. Claude judged Tier 2's reading more faithful to the
    framework as written, and the investor accepted it: COO's answer is now dismiss, and COO goes into
    research_log.yaml as disqualified with Tier 2's re-add condition (two clean quarters, or a concrete JANA outcome).
    Rule kept: never re-run a calibration hoping it flips. (2) The "0 searches" on subscription reviews was a counting
    bug, not missing research (each review cited 4-8 sources); searches are now counted from Claude Code's WebSearch
    calls (stream-json output), and the duplicated Sources list in the review file was removed. (3) The tracker
    (earnings_calendar.html) now has two tabs: Calendar | Weekly reviews. The second is tier2_reviews.html, written by
    sentinel_reviews_page.py from research.db (no network): one section per week, newest open, a summary table, then a
    card per name with verdict, score bars, gate and date, re-add condition, the 1A checks, sources, the full review
    rendered, whether the alert is still open and the exact command to close it; calibration runs kept apart. Rebuilt
    by the weekly job after Tier 2 and by the daily job after Tier 1. The shareable page has no reviews tab, and the
    reviews page is local only (it quotes the investor's gates). The markdown files in reviews/ are still written,
    as the record. Note: CRWV is on the watch list (Sleeve 2), not held, so its at_risk verdict needs no order.

57. **2026-10-18: Sentinel moves to the scheduled-pipeline blueprint; one PUBLIC GitHub repository; full data published.**
    The investor supplied a blueprint (scheduled AI pipeline on a home Mac: git as the audit trail, a static site on
    GitHub Pages fed by JSON, GitHub Actions as a second validation gate, a deterministic checker of the AI's claims,
    a coverage check). Adopted: git, Actions as second gate, static index.html + JSON, checker (prices, earnings
    dates, index membership in Tier 2 reviews; flags only, never changes a verdict), coverage check (90% prices, all
    5 Finviz views, 3+ searches per review; quiet note). Not adopted: separate ntfy topics, standard-library-only
    rewrite, the whole dataset as JSON (SQLite stays the source of truth; the site gets an exported subset).
    Repository: `fjmartinez0/sentinel`, public, one repo for code, framework, decisions, positions.yaml,
    research_log.yaml, reviews/ and (stage 2) the site; site at https://fjmartinez0.github.io/sentinel/.
    **Privacy rule changed by the investor's explicit choice:** "there is nothing confidential here", so the site and
    the repo publish everything, including holdings, share counts, cost basis, accounts, gates and resting orders
    (Claude noted that git history is permanent and public). This replaces the "tickers and verdicts only" rule of
    44/46a/52 for the repo and the site; notifications keep that rule. **Never published, enforced in code:** `.env`
    (keys and tokens), research.db, logs, backups, `.claude_tier2/`. `sentinel_validate.py --secrets` refuses any file
    holding an .env value, an Anthropic or GitHub token, or a forbidden file; it runs as a git pre-commit hook
    (`.githooks/pre-commit`, `git config core.hooksPath .githooks`) and, from stage 2, in GitHub Actions.
    Current site design kept (not the blueprint's). Stages: 1 git + repo (this entry), 2 site + Actions + pipeline
    publish step, 3 checker + coverage (+ one recalibration, because Tier 2's result gains three fact fields),
    then the scoreboard tab. The old OFF publishing feature will be removed in stage 2.

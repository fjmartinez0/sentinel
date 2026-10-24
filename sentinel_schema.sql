-- research.db schema.
--
-- ADDITIVE ONLY. Never edit or delete a migration that has already run on your
-- database: append a new "-- migration: N" section instead. Each section runs once,
-- in order, inside a single transaction, and bumps PRAGMA user_version to N.
-- If a section fails, everything in it is rolled back and the version does not move.
-- Do not put BEGIN / COMMIT / ROLLBACK inside a section; db.py manages the transaction.
--
-- Principle carried over from the design: tables store what was observed, uninterpreted.
-- Judgment (routine vs discretionary selling, "is this a dislocation") lives in the
-- reasoning layer, not here.

-- migration: 1
-- Scanner history. One row per scanner run, one row per hit per run.
-- Rows are only ever appended, so "what changed since last week" is a comparison of
-- two runs, not a lookup of current state.

CREATE TABLE screen_runs (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at     TEXT    NOT NULL,   -- UTC ISO 8601, e.g. 2026-09-27T14:03:00+00:00
    source     TEXT    NOT NULL,   -- e.g. 'finviz_screener'
    filters    TEXT,               -- the f= filter string, so two runs can be compared like for like
    views_ok   TEXT,               -- JSON list of views that fetched OK; a short list means a partial run
    n_hits     INTEGER NOT NULL,
    csv_path   TEXT,               -- the CSV written for this run, if any
    notes      TEXT
);

CREATE INDEX idx_screen_runs_run_at ON screen_runs (run_at);

CREATE TABLE screen_results (
    run_id         INTEGER NOT NULL REFERENCES screen_runs (run_id) ON DELETE CASCADE,
    ticker         TEXT    NOT NULL,   -- canonical key (FMP-style) used to join across tables and scripts
    finviz_symbol  TEXT    NOT NULL,   -- exactly as parsed from Finviz, kept for debugging symbol mismatches
    row_json       TEXT    NOT NULL,   -- the merged row, every column from every view, uninterpreted
    PRIMARY KEY (run_id, ticker)
) WITHOUT ROWID;

CREATE INDEX idx_screen_results_ticker ON screen_results (ticker, run_id);

-- migration: 2
-- What the ledger said about each hit at the time of a run. Kept apart from screen_results on
-- purpose: that table stays raw Finviz data, while this one is interpretation, and it can be
-- recomputed after you edit the YAML files (python3 scan_finviz_candidates.py --retag).

CREATE TABLE screen_tags (
    run_id           INTEGER NOT NULL,
    ticker           TEXT    NOT NULL,
    tag              TEXT    NOT NULL,   -- NEW, HELD, WATCH, PARKED, DORMANT, REMOVED, PASSED, DISQUALIFIED, CLOSED
    note             TEXT,               -- the ledger's own reason, as written
    revisit          TEXT,               -- the condition that should bring the name back, if any
    positions_as_of  TEXT,               -- as_of of the positions.yaml that produced this tag
    log_as_of        TEXT,               -- as_of of the research_log.yaml that produced this tag
    tagged_at        TEXT    NOT NULL,   -- UTC ISO 8601
    PRIMARY KEY (run_id, ticker),
    FOREIGN KEY (run_id, ticker) REFERENCES screen_results (run_id, ticker) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX idx_screen_tags_tag ON screen_tags (tag, run_id);

-- migration: 3
-- Per-ticker detail pages (the Finviz quote page: snapshot KPIs, analyst actions, insider trades,
-- news, peers). Append-only: every fetch is a new row, so "what changed since last time" is a
-- comparison of two snapshots. The page is stored as parsed sections (JSON), uninterpreted.

CREATE TABLE dossier_snapshots (
    snapshot_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT    NOT NULL,      -- canonical key, same as screen_results.ticker
    fetched_at   TEXT    NOT NULL,      -- UTC ISO 8601
    source       TEXT    NOT NULL,      -- 'finviz_quote'
    run_id       INTEGER REFERENCES screen_runs (run_id) ON DELETE SET NULL,   -- scanner run that triggered it, if any
    reason       TEXT,                  -- why it was fetched: NEW, HELD, WATCH or MANUAL
    parse_ok     INTEGER NOT NULL,      -- 1 = the core snapshot parsed; 0 = something is wrong, see parse_notes
    parse_notes  TEXT,                  -- what was missing or suspicious
    html_sha1    TEXT,                  -- fingerprint of the page, to spot markup changes
    data_json    TEXT    NOT NULL       -- {snapshot, ratings, insiders, news, peers, held_by, page_title}
);

CREATE INDEX idx_dossier_ticker ON dossier_snapshots (ticker, fetched_at);

-- migration: 4
-- The calendar's own history. One row per calendar run, with the earnings dates it saw for the
-- names you track (held, watch, screen candidates), the live prices it fetched, and a copy of
-- positions.yaml whenever that file changed. S&P 500 names are NOT stored: only tracked names
-- need history (date moves, surprises, price moves), and it keeps the file small.

CREATE TABLE calendar_runs (
    calendar_run_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at           TEXT    NOT NULL,   -- UTC ISO 8601
    as_of_date       TEXT    NOT NULL,   -- the date the run treated as "today"
    window_from      TEXT    NOT NULL,   -- earliest earnings date requested from FMP
    window_to        TEXT    NOT NULL,   -- latest earnings date requested from FMP
    n_events         INTEGER NOT NULL,
    n_quotes         INTEGER NOT NULL,
    notes            TEXT
);

CREATE TABLE earnings_events (
    calendar_run_id  INTEGER NOT NULL REFERENCES calendar_runs (calendar_run_id) ON DELETE CASCADE,
    ticker           TEXT    NOT NULL,
    event_date       TEXT    NOT NULL,
    kind             TEXT    NOT NULL,   -- held, watch or candidate: what the calendar called it at the time
    fmp_time         TEXT,
    fmp_confirmed    INTEGER,            -- FMP's own flag, which is NOT proof of a confirmed date
    eps_est          REAL,
    eps_act          REAL,
    rev_est          REAL,
    rev_act          REAL,
    PRIMARY KEY (calendar_run_id, ticker, event_date)
) WITHOUT ROWID;

CREATE INDEX idx_earnings_events_ticker ON earnings_events (ticker, event_date);

CREATE TABLE quote_snapshots (
    calendar_run_id  INTEGER NOT NULL REFERENCES calendar_runs (calendar_run_id) ON DELETE CASCADE,
    ticker           TEXT    NOT NULL,
    price            REAL    NOT NULL,
    change_pct       REAL,               -- FMP's day change at the time of the run
    PRIMARY KEY (calendar_run_id, ticker)
) WITHOUT ROWID;

-- A point-in-time copy of positions.yaml (holdings, cost basis, targets), saved only when its
-- content changes. PRIVATE: this is your position data. The calendar's --share mode never touches it.
CREATE TABLE positions_snapshots (
    snapshot_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    saved_at     TEXT    NOT NULL,
    as_of        TEXT,                   -- the as_of date written in the file
    sha1         TEXT    NOT NULL,       -- fingerprint of the content, to skip duplicates
    data_json    TEXT    NOT NULL
);

CREATE INDEX idx_positions_snapshots_saved ON positions_snapshots (saved_at);

-- migration: 5
-- Tier 1. price_history holds daily closes (written by the calendar, read by tier1.py) so a one-day
-- move can be measured whatever day you happen to run things. escalations is the queue of names that
-- crossed a Tier 1 threshold and are waiting for a Tier 2 look. tier1_runs is the audit log: one row per
-- evaluation, including the near misses that did NOT escalate ("log only", as the design says).

CREATE TABLE price_history (
    ticker      TEXT NOT NULL,
    price_date  TEXT NOT NULL,          -- the trading day, YYYY-MM-DD
    close       REAL NOT NULL,
    source      TEXT NOT NULL,          -- e.g. fmp_eod
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (ticker, price_date)
) WITHOUT ROWID;

CREATE TABLE escalations (
    escalation_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    ticker         TEXT NOT NULL,
    funnel         TEXT NOT NULL,       -- A = tracked name (held or watch), B = screen candidate
    trigger        TEXT NOT NULL,       -- price_move, earnings_surprise or candidate
    trigger_ref    TEXT NOT NULL,       -- what fired: the date of the move or print; for a candidate, the evaluation date
    reasons_json   TEXT NOT NULL,       -- plain-English reasons with the numbers
    evidence_json  TEXT,                -- the raw numbers behind them
    rules_version  TEXT,                -- which version of tier1_rules.yaml produced it
    status         TEXT NOT NULL DEFAULT 'open',   -- open, done or dismissed
    status_at      TEXT,
    note           TEXT,
    UNIQUE (ticker, trigger, trigger_ref)
);

CREATE INDEX idx_escalations_status ON escalations (status, created_at);

CREATE TABLE tier1_runs (
    tier1_run_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at         TEXT NOT NULL,
    as_of_date     TEXT NOT NULL,
    rules_version  TEXT,
    n_checked      INTEGER NOT NULL,
    n_escalated    INTEGER NOT NULL,    -- newly stored escalations in this run
    details_json   TEXT NOT NULL        -- everything considered: escalated, near misses, pending, coverage
);

-- migration: 6
-- Daily prices now come from Finviz (the FMP plan only serves a few symbols). Finviz reports each day's own
-- change against the previous close, so it is stored with the close: a single-day move can then be read
-- directly, even when some days are missing from the history.
ALTER TABLE price_history ADD COLUMN change_pct REAL;

-- migration: 7
-- Tier 2 (Phase 7). One row per review of an escalated name by Claude, written by sentinel_tier2.py only.
-- The review is advice for you to read: nothing here changes an escalation's status, positions.yaml or the
-- ledger. mode says why it ran (live = the open queue, manual = one escalation by id, calibration = the
-- known-answer test set). batch groups the reviews made by one invocation, so a calibration can be judged as
-- a whole. Token counts and the dollar cost are kept per review, so spending can be checked against a budget.

CREATE TABLE tier2_reviews (
    review_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    escalation_id       INTEGER REFERENCES escalations (escalation_id) ON DELETE SET NULL,
    ticker              TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,   -- UTC ISO 8601
    mode                TEXT    NOT NULL,   -- live, manual or calibration
    batch               TEXT    NOT NULL,   -- one id per invocation of sentinel_tier2.py
    model               TEXT    NOT NULL,
    ok                  INTEGER NOT NULL,   -- 1 = a valid verdict was read from the answer
    verdict             TEXT,               -- watch / dismiss / insufficient, or intact / at_risk / broken / insufficient
    expected            TEXT,               -- calibration only: the verdict you reached by hand
    result_json         TEXT,               -- the structured result (scores, gate, checks, sources)
    report_md           TEXT,               -- the written review
    error               TEXT,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_write_tokens  INTEGER,
    cache_read_tokens   INTEGER,
    searches            INTEGER,
    cost_usd            REAL    NOT NULL DEFAULT 0
);

CREATE INDEX idx_tier2_escalation ON tier2_reviews (escalation_id, ok);
CREATE INDEX idx_tier2_created ON tier2_reviews (created_at);

-- migration: 8
-- Daily news digest for the holdings (DECISIONS.md 60). One row per held US name per trading day, written by
-- sentinel_news.py only. sentiment is the tone of THAT day's new headlines for a shareholder (positive, neutral,
-- negative), or 'none' when there were no new headlines (then Claude is not asked). gate_relevant = the news bears
-- on the name's own gate or thesis. Display only: Tier 1 and Tier 2 never read this table. Never overwritten.

CREATE TABLE news_digest (
    digest_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    digest_date     TEXT    NOT NULL,   -- the trading day the news belongs to (New York), YYYY-MM-DD
    created_at      TEXT    NOT NULL,   -- UTC ISO 8601
    sentiment       TEXT    NOT NULL,   -- positive / neutral / negative / none
    gate_relevant   INTEGER NOT NULL DEFAULT 0,
    reason          TEXT,               -- one line: why this sentiment
    summary         TEXT,               -- two or three sentences
    headlines_json  TEXT,               -- the new headlines it was based on: [{date, time, headline, source}]
    model           TEXT,
    UNIQUE (ticker, digest_date)
);

CREATE INDEX idx_news_date ON news_digest (digest_date);

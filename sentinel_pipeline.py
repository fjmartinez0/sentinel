#!/usr/bin/env python3
"""sentinel_pipeline.py -- Phase 8: run the whole cycle unattended, and say so loudly when something is wrong.

    python3 sentinel_pipeline.py daily       quotes -> calendar -> Tier 1      (scheduled: weekdays after the close)
    python3 sentinel_pipeline.py weekly      scan + dossiers -> tracked dossiers -> Tier 1 -> Tier 2 -> backup   (Saturdays)
    python3 sentinel_pipeline.py backup      just the database backup
    python3 sentinel_pipeline.py status      what ran, what it found, what is stale, and whether the schedule is on
    python3 sentinel_pipeline.py skip weekly skip ONLY the next Saturday run (weekday runs continue); also: skip daily,
                                             skip --off (cancel), skip (show). Use this instead of uninstalling (DECISIONS.md 53)
    python3 sentinel_pipeline.py publish-dir PATH   publish the SHAREABLE calendar page into PATH (a folder your web server shows)
    python3 sentinel_pipeline.py publish-dir PATH --git   ... and commit + push it (PATH is inside a GitHub clone, e.g. for GitHub Pages)
    python3 sentinel_pipeline.py publish-dir --off  stop publishing;   publish-dir alone shows the current setting
    python3 sentinel_pipeline.py publish     publish the shareable page now (the daily job also does it once a folder is set)
    python3 sentinel_pipeline.py selfcheck   can a scheduled job do everything it needs? (run by sentinel_schedule.py)
    add --dry-run to see what a job would do, --no-notify to silence macOS notifications.

Design rules (DECISIONS.md items 41-43):
  * The programs stay separate (item 15): each step is a subprocess of an existing script. A step that fails,
    hangs or stores nothing NEVER stops the steps after it.
  * The programs exit 0 even when they store nothing (the first --quotes run did exactly that), so success is
    judged from what actually landed in research.db, not from the exit code alone.
  * Every run writes a full log (logs/), a summary (logs/status.json) and ONE macOS notification summing it up
    (OK or not, new alerts by ticker, Tier 2 verdicts). A run that never happens is detected by the next one, and
    by you: no notification on a weekday means the run did not happen (DECISIONS.md 50).
  * Exit code: 0 = all OK, 1 = warnings, 2 = at least one failure.
"""
import argparse
import re
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:                     # not on this platform; runs then are simply not locked
    fcntl = None

OK, WARN, FAIL = "OK", "WARN", "FAIL"
RANK = {OK: 0, WARN: 1, FAIL: 2}
NON_US_SUFFIXES = {"TO", "V", "CN", "NE"}
KEEP_BACKUPS, KEEP_LOG_DAYS, KEEP_STATUS_RUNS = 8, 90, 80
LEDGER_STALE_DAYS = 14
PUBLISH_NAME = "earnings_calendar.html"
SENSITIVE_FILES = ("research.db", "positions.yaml", "research_log.yaml", ".env", "sentinel_tier1_rules.yaml")
# Text that only the PRIVATE calendar page contains (positions, cost basis, resting orders). None may reach the public page.
LEAK_MARKERS = ("cost basis", "your holdings", "your watch list", "resting exit", "gtc", " sh at ", "value / p", "local use only")
SHARE_MARKER = "No positions, gates or prices are included"

HERE = Path(__file__).resolve().parent
LOGS = BACKUPS = STATUS = LOCK = SCRIPTS = None


def configure(base):
    """Point every path at one project folder (tests use a temporary one)."""
    global HERE, LOGS, BACKUPS, STATUS, LOCK, SCRIPTS
    HERE = Path(base).resolve()
    LOGS, BACKUPS = HERE / "logs", HERE / "backups" / "db"
    STATUS, LOCK = LOGS / "status.json", LOGS / ".pipeline.lock"
    SCRIPTS = {"scan": HERE / "sentinel_scan.py", "calendar": HERE / "sentinel_calendar.py",
               "tier1": HERE / "sentinel_tier1.py", "tier2": HERE / "sentinel_tier2.py",
               "reviews": HERE / "sentinel_reviews_page.py", "publish": HERE / "sentinel_publish.py",
               "news": HERE / "sentinel_news.py"}
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))


configure(HERE)


# ------------------------------------------------------------------------------------------------ time
def now_et(now=None):
    try:
        from zoneinfo import ZoneInfo
        return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    except Exception:                   # noqa: BLE001  (no time-zone database)
        return None


def expected_close(et):
    """The trading date whose CLOSE should be the newest price right now (holidays are not modelled: a holiday
    only ever makes this one day too new, which shows up as a harmless warning)."""
    d, wd = et.date(), et.weekday()
    if wd >= 5:
        return d - timedelta(days=wd - 4)
    if et.time() >= time(16, 0):
        return d
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev


def business_days_between(a, b):
    """Weekdays strictly between two dates."""
    out, d = [], a + timedelta(days=1)
    while d < b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------------------------------------ env
def load_env_file(path):
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def child_env():
    """Environment for the programs: yours, plus anything in .env that is not already set. launchd jobs do not
    see your shell profile, which is where FMP_API_KEY normally lives."""
    env = dict(os.environ)
    for k, v in load_env_file(HERE / ".env").items():
        env.setdefault(k, v)
    env["PYTHONUNBUFFERED"] = "1"
    return env


# ------------------------------------------------------------------------------------------------ database helpers
def _db():
    import sentinel_db as db
    return db


def is_non_us(t):
    return "." in t and t.rsplit(".", 1)[-1] in NON_US_SUFFIXES


def tracked_us():
    import sentinel_tier1 as tier1
    return [t for t, _ in tier1.tracked_names(HERE / "positions.yaml") if not is_non_us(t)]


def price_freshness(expected):
    """(fresh, total, missing, newest) for the tracked US names against the expected close date."""
    db = _db()
    conn = db.connect(create=False)
    try:
        names = tracked_us()
        missing, newest = [], None
        for t in names:
            d = db.latest_price_date(conn, t)
            if d is None or d < expected.isoformat():
                missing.append(t)
            if d and (newest is None or d > newest):
                newest = d
        return len(names) - len(missing), len(names), missing, newest
    finally:
        conn.close()


def open_escalations():
    db = _db()
    conn = db.connect(create=False)
    try:
        return len(db.list_escalations(conn, "open"))
    finally:
        conn.close()


def escalation_rows():
    db = _db()
    conn = db.connect(create=False)
    try:
        return db.list_escalations(conn)
    finally:
        conn.close()


def last_lines(text, n=1):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return " | ".join(lines[-n:]) if lines else "(no output)"


# ------------------------------------------------------------------------------------------------ health checks
# Each check gets (result, ctx) and returns (level, detail). result: code, timed_out, seconds, output.
def check_exit(res):
    if res["timed_out"]:
        return FAIL, f"timed out after {res['limit']}s and was stopped"
    if res["code"] != 0:
        return FAIL, f"exit code {res['code']}: {last_lines(res['output'])}"
    return OK, ""


def check_quotes(res, ctx):
    lvl, detail = check_exit(res)
    if lvl == FAIL:
        return lvl, detail
    et = now_et(ctx["now"])
    if et is None:
        return WARN, "time-zone database missing, so price freshness could not be verified"
    expected = expected_close(et)
    try:
        fresh, total, missing, newest = price_freshness(expected)
    except Exception as e:              # noqa: BLE001
        return FAIL, f"could not read prices back from research.db ({e})"
    note = " (the market was open, so no close could be stored today)" if "market is open right now" in res["output"] else ""
    if total == 0:
        return WARN, "no tracked US names in positions.yaml"
    if fresh == total:
        return (WARN if "PROBLEM" in res["output"] else OK), f"{fresh}/{total} tracked US names have the {expected} close"
    msg = (f"{fresh}/{total} tracked US names have the {expected} close (newest stored: {newest or 'none'}); missing: "
           f"{', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}{note}. Finviz only shows the latest close, "
           "so a missed day cannot be filled in later and Tier 1 cannot see that day's move.")
    return (WARN if fresh / total >= 0.9 else FAIL), msg


def check_calendar(res, ctx):
    lvl, detail = check_exit(res)
    if lvl == FAIL:
        return lvl, detail
    page = HERE / "earnings_calendar.html"
    if not page.exists() or page.stat().st_mtime < ctx["t0"] - 5:
        return FAIL, "exit code 0 but earnings_calendar.html was not (re)written"
    marker = next((ln.strip() for ln in res["output"].splitlines() if "items on the calendar" in ln), "page written")
    warn = next((ln.strip()[len("WARNING: "):] for ln in res["output"].splitlines()
                 if ln.startswith("WARNING: earnings source") or ln.startswith("WARNING: could not fetch")), None)
    if warn:
        return WARN, f"page made from stored dates: {warn}"[:300]
    return OK, marker


def check_tier1(res, ctx):
    lvl, detail = check_exit(res)
    if lvl == FAIL:
        return lvl, detail
    out = res["output"]
    try:
        n_open = open_escalations()
    except Exception as e:              # noqa: BLE001
        return WARN, f"ran, but the escalation queue could not be read ({e})"
    new = max(0, n_open - ctx.get("open_before", n_open))
    ctx["new_escalations"] = new
    try:
        before = ctx.get("ids_before")
        ctx["new_tickers"] = [e["ticker"] for e in escalation_rows() if before is not None and e["escalation_id"] not in before and e["status"] == "open"]
    except Exception:                   # noqa: BLE001
        ctx["new_tickers"] = []
    msg = f"{new} new escalation(s), {n_open} open in the queue"
    if "STALE" in out:
        return WARN, msg + "; Tier 1 reports STALE prices: " + next((ln.strip() for ln in out.splitlines() if "STALE" in ln), "")[:160]
    return OK, msg


def check_tier2(res, ctx):
    """Judged from the tier2_reviews table: which names were reviewed in this step, with what verdict."""
    if res["timed_out"]:
        return FAIL, f"timed out after {res['limit']}s and was stopped" + ("" if "subscription" in res["output"] else
                                                                           " (Anthropic may have charged for the review in progress)")
    out = res["output"]
    if "Live review refused" in out:
        return WARN, "did not run: its calibration has not passed (python3 sentinel_tier2.py --calibrate)"
    if "No ANTHROPIC_API_KEY" in out:
        return FAIL, "no Anthropic API key (python3 sentinel_tier2.py --save-key)"
    if "No CLAUDE_CODE_OAUTH_TOKEN" in out:
        return FAIL, "no subscription token (claude setup-token, then python3 sentinel_tier2.py --save-token)"
    db = _db()
    since = datetime.fromtimestamp(ctx["t0"] - 1, timezone.utc).isoformat(timespec="seconds")
    try:
        conn = db.connect(create=False)
        rows = [r for r in reversed(db.list_tier2_reviews(conn, mode="live", limit=50)) if r["created_at"] >= since]
        conn.close()
    except Exception as e:              # noqa: BLE001
        return FAIL, f"could not read the reviews back from research.db ({e})"
    good = [r for r in rows if r["ok"]]
    bad = [r for r in rows if not r["ok"]]
    ctx["tier2_summary"] = ", ".join(f"{r['ticker']} {r['verdict']}" for r in good)
    if not rows:
        if "Nothing to review" in out:
            return OK, "nothing new to review"
        if "usage limit was reached" in out:
            return WARN, "your Claude plan's usage limit was reached before any review; the names wait for the next run"
        if res["code"] not in (0, None):
            return FAIL, f"exit code {res['code']}: {last_lines(out)}"
        return WARN, "ran but stored no review: " + last_lines(out)[:160]
    cost = sum(r["cost_usd"] or 0 for r in rows)
    msg = f"reviewed {len(good)}: {ctx['tier2_summary'] or 'none'} (${cost:.2f}); read reviews/<date>_tier2.md"
    if "subscription" in out:
        msg = msg.replace(f" (${cost:.2f})", " (subscription)")
    limit_hit = "usage limit was reached" in out
    if bad or "Stopped before" in out or "more waiting" in out or limit_hit:
        extra = ([f"{len(bad)} failed ({bad[0]['error'][:80]})"] if bad else []) + \
                (["monthly budget reached"] if "Stopped before" in out else []) + (["more names waiting"] if "more waiting" in out else []) + \
                (["plan usage limit reached; the rest wait for next Saturday"] if limit_hit else [])
        return WARN, msg + "; " + "; ".join(extra)
    return OK, msg


def check_reviews_page(res, ctx):
    """The Weekly reviews tab of the tracker (DECISIONS.md 56). A failure here never affects the research itself."""
    lvl, detail = check_exit(res)
    if lvl == FAIL:
        return WARN, "the Weekly reviews page was not rebuilt: " + detail
    return OK, "tier2_reviews.html rebuilt (open it from the tracker's Weekly reviews tab)"


def check_news(res, ctx):
    """The holdings' news digest (DECISIONS.md 60). Display only, so never a FAIL; names not done are retried."""
    if res["timed_out"]:
        return WARN, f"news not done: timed out after {res['limit']}s (retried next run)"
    lines = [ln.strip() for ln in res["output"].splitlines() if ln.strip()]
    alert = next((ln for ln in lines if ln.startswith("NEWS ALERTS:")), None)
    if alert:
        ctx["news_alerts"] = alert.split(":", 1)[1].strip()
    head = next((ln for ln in lines if ln.startswith("News for")), lines[0] if lines else "")
    if res["code"] == 0:
        return OK, head[:220]
    rest = next((ln for ln in lines if ln.startswith(("Not done", "Claude did not", "Stopped", "sentinel_news.py"))), "")
    return WARN, ("news: " + " ".join(x for x in (head, rest) if x))[:260]


def check_site(res, ctx):
    """The public site (DECISIONS.md 57): commit the new data and push; GitHub Actions deploys. Never a FAIL: the
    research itself is done by now, and a push that did not happen is retried at the next run."""
    if res["timed_out"]:
        return WARN, f"site not updated: timed out after {res['limit']}s (retried next run)"
    out = res["output"]
    tail = [ln for ln in out.splitlines() if ln.strip()]
    if res["code"] == 0:
        return OK, "site: " + ("; ".join(tail[-2:]) if tail else "published")[:220]
    if res["code"] == 3:
        return WARN, "site: committed, but the push to GitHub failed (retried next run): " + " ".join(tail[-2:])[:200]
    return WARN, "site not updated: " + (" ".join(tail[:3]) if tail else f"exit code {res['code']}")[:240]


def check_scan(res, ctx):
    lvl, detail = check_exit(res)
    if lvl == FAIL:
        return lvl, detail
    db = _db()
    try:
        conn = db.connect(create=False)
        runs = db.list_runs(conn, 1)
        conn.close()
    except Exception as e:              # noqa: BLE001
        return FAIL, f"could not read the run back from research.db ({e})"
    if not runs:
        return FAIL, "exit code 0 but no screen run exists in research.db"
    r = runs[0]
    when = datetime.fromisoformat(r["run_at"]).timestamp()
    if when < ctx["t0"] - 5:
        return FAIL, f"exit code 0 but no NEW run was saved (newest is run #{r['run_id']} from {r['run_at'][:16]})"
    if not r["n_hits"]:
        return WARN, f"run #{r['run_id']} saved with 0 hits (the screen returned nothing; check the Finviz URLs)"
    out = res["output"]
    if "the dossier stage failed" in out or "Stopping early" in out:
        return WARN, f"run #{r['run_id']}: {r['n_hits']} hits, but the dossier stage had trouble: " + \
                     next((ln.strip() for ln in out.splitlines() if "dossier stage failed" in ln or "Stopping early" in ln), "")[:160]
    return OK, f"run #{r['run_id']}: {r['n_hits']} hits"


def check_dossiers(res, ctx):
    lvl, detail = check_exit(res)
    if lvl == FAIL:
        return lvl, detail
    if "Stopping early" in res["output"]:
        return WARN, "Finviz pages could not be read (stopped early): " + next(
            (ln.strip() for ln in res["output"].splitlines() if "Stopping early" in ln), "")[:160]
    return OK, last_lines(res["output"])[:160]


# ------------------------------------------------------------------------------------------------ backup (internal step)
def backup_db(now):
    db = _db()
    src_path = db.db_path(None)
    if not Path(src_path).exists():
        return FAIL, f"{src_path} does not exist"
    BACKUPS.mkdir(parents=True, exist_ok=True)
    dest = BACKUPS / f"research_{now.astimezone().strftime('%Y-%m-%d_%H%M%S')}.db"
    src, dst = db.connect(create=False), sqlite3.connect(str(dest))
    try:
        src.backup(dst)                              # SQLite's own online backup: safe while the database is in use
        verdict = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if verdict != "ok":
        if dest.exists():
            dest.unlink()
        return FAIL, f"the backup failed its integrity check ({verdict})"
    files = sorted(BACKUPS.glob("research_*.db"))
    for old in files[:-KEEP_BACKUPS]:
        old.unlink()
    return OK, f"{dest.name} ({dest.stat().st_size // 1024} KB, integrity ok); keeping the newest {min(len(files), KEEP_BACKUPS)}"


# ------------------------------------------------------------------------------------------------ publishing
def env_set(key, value):
    """Set or remove (value None) one line of the private .env file, keeping every other line."""
    path = HERE / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [ln for ln in lines if not ln.startswith(key + "=")]
    if value is not None:
        lines.append(f'{key}="{value}"')
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.chmod(str(path), 0o600)


def publish_dir():
    v = child_env().get("PUBLISH_DIR")
    return Path(v).expanduser() if v else None


def publish_git():
    return child_env().get("PUBLISH_GIT") == "1"


def valid_name(name):
    import re
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.html$", name or ""))


def publish_name():
    return child_env().get("PUBLISH_FILE") or PUBLISH_NAME


def safe_url(u):
    """A remote URL with any embedded user:token@ removed, so a token can never be printed or logged."""
    import re
    return re.sub(r"//[^/@\s]*@", "//", (u or "").strip())


def git(args, cwd, timeout=120):
    """Run git without ever waiting for a password or a signing prompt. Returns (returncode, output)."""
    env = child_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    try:
        p = subprocess.run(["git", "-c", "commit.gpgsign=false"] + list(args), cwd=str(cwd), env=env, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, f"git {args[0]} timed out after {timeout}s"
    except Exception as e:              # noqa: BLE001
        return 127, str(e)


def validate_publish_dir(d):
    """None if the folder is safe to publish into, else the reason it is not."""
    p = Path(d).expanduser()
    if not p.is_dir():
        return f"{p} does not exist or is not a folder"
    rp_, home = p.resolve(), HERE.resolve()
    if rp_ == home or rp_ in home.parents:
        return (f"{rp_} is your project folder or a folder that contains it, so a web server showing it would expose "
                "research.db, positions.yaml and .env")
    for name in SENSITIVE_FILES:
        if (rp_ / name).exists():
            return f"{rp_} contains {name}: refusing to publish into a folder that holds private files"
    if not os.access(str(rp_), os.W_OK):
        return f"{rp_} is not writable"
    return None


def git_repo_top(d):
    rc, out = git(["rev-parse", "--show-toplevel"], Path(d).expanduser())
    return Path(out.strip()).resolve() if rc == 0 else None


def validate_git_target(d):
    """None if this folder is inside a git repository we can safely commit to and push from, else the reason."""
    if not shutil.which("git"):
        return "git is not installed (in Terminal: xcode-select --install)"
    top = git_repo_top(d)
    if top is None:
        return f"{d} is not inside a git repository (clone your GitHub repository first)"
    home = HERE.resolve()
    if top == home or top in home.parents:
        return f"the git repository at {top} also contains your project folder; use a separate repository for the public page"
    if git(["remote", "get-url", "origin"], top)[0] != 0:
        return "the repository has no 'origin' remote to push to"
    if git(["symbolic-ref", "--short", "HEAD"], top)[0] != 0:
        return "the repository is on a detached HEAD; check out a branch first"
    if git(["rev-parse", "--verify", "HEAD"], top)[0] != 0:
        return "the repository has no commits yet; make an initial commit first (for example add a README on GitHub, then clone)"
    rc, em = git(["config", "user.email"], top)
    if rc != 0 or not em.strip():
        return "git user.name and user.email are not set for this repository"
    return None


def git_info(d):
    top = git_repo_top(d)
    if top is None:
        return {}
    branch = git(["symbolic-ref", "--short", "HEAD"], top)[1].strip()
    url = safe_url(git(["remote", "get-url", "origin"], top)[1])
    rc, n = git(["rev-list", "--count", f"origin/{branch}..HEAD"], top)
    return {"top": top, "branch": branch, "remote": url, "unpushed": int(n.strip()) if rc == 0 and n.strip().isdigit() else None}


def _long_strings(obj, min_len=25):
    if isinstance(obj, str):
        if len(obj) >= min_len:
            yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _long_strings(v, min_len)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _long_strings(v, min_len)


def leak_check(html):
    """Reasons this page must NOT be published (empty list = clean). Independent of --share: it looks at the output."""
    import html as _html
    low, problems = html.lower(), []
    if SHARE_MARKER not in html:
        problems.append("it is not the shareable page (the 'No positions, gates or prices' notice is missing)")
    for m in LEAK_MARKERS:
        if m in low:
            problems.append(f"contains '{m.strip()}'")
    try:
        import yaml
        raw = yaml.safe_load((HERE / "positions.yaml").read_text(encoding="utf-8")) or {}
        plain = _html.unescape(html)
        hits = [s for s in _long_strings(raw) if s[:40] in html or s[:40] in plain]
        if hits:
            problems.append(f"contains {len(hits)} free-text note(s) from positions.yaml, for example '{hits[0][:50]}...'")
    except Exception as e:              # noqa: BLE001  (cannot verify => do not publish)
        problems.append(f"could not compare against positions.yaml ({e})")
    return problems


def publish_calendar(now):
    d = publish_dir()
    if d is None:
        return WARN, "no publish folder is set (python3 sentinel_pipeline.py publish-dir PATH)"
    err = validate_publish_dir(d)
    if err:
        return FAIL, "not published: " + err
    name = publish_name()
    if not valid_name(name):
        return FAIL, f"not published: '{name}' is not a valid page name (letters, digits, - _ . and ending in .html)"
    use_git = publish_git()
    if use_git:
        err = validate_git_target(d)
        if err:
            return FAIL, "not published: " + err
    LOGS.mkdir(parents=True, exist_ok=True)
    tmp = LOGS / "publish_tmp.html"
    if tmp.exists():
        tmp.unlink()
    try:
        try:
            p = subprocess.run([py(), str(SCRIPTS["calendar"]), "--share", "--out", str(tmp)], cwd=str(HERE), env=child_env(),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
        except subprocess.TimeoutExpired:
            return FAIL, "not published: the shareable page took more than 600s to generate"
        out = p.stdout.decode("utf-8", "replace")
        if p.returncode != 0 or not tmp.exists():
            return FAIL, f"not published: the shareable page could not be generated ({last_lines(out)})"
        html = tmp.read_text(encoding="utf-8")
        problems = leak_check(html)
        if problems:
            return FAIL, "NOT published, the page failed the privacy check: " + "; ".join(problems[:3])
        dest = Path(d).expanduser().resolve()
        final, part = dest / name, dest / (name + ".part")
        if final.exists() and SHARE_MARKER not in final.read_text(encoding="utf-8", errors="replace"):
            return FAIL, f"not published: {final} already exists and is not a page this tool wrote, so it was not overwritten"
        top, branch = None, None
        if use_git:
            top = git_repo_top(dest)
            branch = git(["symbolic-ref", "--short", "HEAD"], top)[1].strip()
            rc, gout = git(["pull", "--rebase", "--autostash", "origin", branch], top, timeout=180)
            if rc != 0:
                git(["rebase", "--abort"], top)
                return FAIL, f"not published: could not update the repository from GitHub ({last_lines(gout)})"
        shutil.copyfile(str(tmp), str(part))
        os.replace(str(part), str(final))          # atomic: a visitor never sees half a page
        size = final.stat().st_size // 1024
        if not use_git:
            return OK, f"published the shareable page ({size} KB, privacy check passed) to {final}"
        rel = os.path.relpath(str(final), str(top))
        rc, gout = git(["add", "--", rel], top)     # ONLY this one file is ever added
        if rc != 0:
            return FAIL, f"could not stage the page ({last_lines(gout)})"
        if git(["diff", "--cached", "--quiet", "--", rel], top)[0] != 0:
            rc, gout = git(["commit", "-m", f"Update earnings calendar {now.astimezone().date().isoformat()} (automated)", "--", rel], top)
            if rc != 0:
                return FAIL, f"could not commit the page ({last_lines(gout)})"
        rc, n = git(["rev-list", "--count", f"origin/{branch}..HEAD"], top)
        ahead = int(n.strip()) if rc == 0 and n.strip().isdigit() else 1
        if ahead == 0:
            return OK, f"the page is unchanged since the last push ({size} KB, privacy check passed); nothing to push"
        rc, gout = git(["push", "origin", branch], top, timeout=180)
        if rc != 0:
            return FAIL, f"committed locally but the push failed ({last_lines(gout)}); it will be retried at the next run"
        return OK, f"pushed the shareable page ({size} KB, privacy check passed) to {safe_url(git(['remote', 'get-url', 'origin'], top)[1])} ({branch}, {ahead} commit)"
    finally:
        if tmp.exists():
            tmp.unlink()


def cmd_publish_dir(path, off, use_git=False, name=None):
    cur = publish_dir()
    if off:
        for k in ("PUBLISH_DIR", "PUBLISH_GIT", "PUBLISH_FILE"):
            env_set(k, None)
        print("Publishing is off. Nothing already published was deleted; remove the file yourself if you want it gone.")
        return 0
    if path is None:
        if cur is None:
            print("Publishing is off. Turn it on with:\n"
                  "  python3 sentinel_pipeline.py publish-dir /path/to/web/folder            (a folder a web server shows)\n"
                  "  python3 sentinel_pipeline.py publish-dir /path/to/your/clone --git      (a GitHub repository: commit and push)")
        else:
            err = validate_git_target(cur) if publish_git() else validate_publish_dir(cur)
            if err is None and publish_git():
                gi = git_info(cur)
                extra = f" -> pushes to {gi['remote']} ({gi['branch']})"
            else:
                extra = ""
            print(f"Publishing the shareable calendar page to: {cur / publish_name()}{extra}" + (f"\n  PROBLEM: {err}" if err else ""))
        return 0
    if name is not None and not valid_name(name):
        print(f"REFUSED: '{name}' is not a valid page name (letters, digits, - _ . and ending in .html)")
        return 2
    err = validate_publish_dir(path) or (validate_git_target(path) if use_git else None)
    if err:
        print("REFUSED: " + err)
        return 2
    final_dir = Path(path).expanduser().resolve()
    env_set("PUBLISH_DIR", str(final_dir))
    env_set("PUBLISH_GIT", "1" if use_git else None)
    env_set("PUBLISH_FILE", name)
    page = final_dir / (name or PUBLISH_NAME)
    print(f"Publishing is on: {page}")
    if page.exists() and SHARE_MARKER not in page.read_text(encoding="utf-8", errors="replace"):
        print(f"  NOTE: {page.name} already exists there and was not written by this tool. Publishing will REFUSE to overwrite it;\n"
              "        choose another name with --name something.html")
    print("  Only the SHAREABLE page (S&P 500 dates; no positions, gates, prices or notes) is ever published, and only after a privacy check.")
    if use_git:
        gi = git_info(final_dir)
        print(f"  Each daily run commits that ONE file and pushes it to {gi['remote']} (branch {gi['branch']}).\n"
              "  Git history is permanent: whatever is pushed stays in the repository's history even if deleted later.")
    print("  Your full page stays in the project folder. To publish once right now:  python3 sentinel_pipeline.py publish")
    return 0


# ------------------------------------------------------------------------------------------------ jobs
def py():
    return sys.executable


def tier2_key():
    """The credential Saturday's Tier 2 needs: the subscription token, or the API key (DECISIONS.md 55)."""
    b = (child_env().get("TIER2_BACKEND") or "api").strip().lower()
    return "CLAUDE_CODE_OAUTH_TOKEN" if b in ("subscription", "sub", "pro") else "ANTHROPIC_API_KEY"


def job_steps(job):
    """The ordered steps of a job. argv=None means an internal step (function)."""
    s = SCRIPTS
    if job == "daily":
        steps = [dict(name="quotes", argv=[py(), str(s["scan"]), "--quotes"], limit=600, check=check_quotes),
                 dict(name="calendar", argv=[py(), str(s["calendar"]), "--site"], limit=900, check=check_calendar, needs_key="ALPHAVANTAGE_API_KEY"),
                 dict(name="news", argv=[py(), str(s["news"]), "--site"], limit=900, check=check_news),
                 dict(name="tier1", argv=[py(), str(s["tier1"])], limit=300, check=check_tier1, before="open_escalations"),
                 dict(name="reviews page", argv=[py(), str(s["reviews"]), "--site"], limit=120, check=check_reviews_page),
                 dict(name="site", argv=[py(), str(s["publish"]), "--job", "daily"], limit=300, check=check_site)]
        if publish_dir() is not None:
            steps.insert(2, dict(name="publish", argv=None, internal=publish_calendar, needs_key="ALPHAVANTAGE_API_KEY"))
        return steps
    if job == "publish":
        return [dict(name="publish", argv=None, internal=publish_calendar, needs_key="ALPHAVANTAGE_API_KEY")]
    if job == "weekly":
        return [dict(name="scan+dossier", argv=[py(), str(s["scan"]), "--dossier"], limit=2400, check=check_scan),
                dict(name="tracked dossiers", argv=[py(), str(s["scan"]), "--dossier-only", "--held", "--watch", "--cap", "30"],
                     limit=2400, check=check_dossiers),
                dict(name="tier1", argv=[py(), str(s["tier1"])], limit=300, check=check_tier1, before="open_escalations"),
                dict(name="tier2", argv=[py(), str(s["tier2"])], limit=5400, check=check_tier2, needs_key=tier2_key()),
                dict(name="reviews page", argv=[py(), str(s["reviews"]), "--site"], limit=120, check=check_reviews_page),
                dict(name="site", argv=[py(), str(s["publish"]), "--job", "weekly"], limit=300, check=check_site),
                dict(name="backup", argv=None, internal=backup_db)]
    if job == "backup":
        return [dict(name="backup", argv=None, internal=backup_db)]
    raise ValueError(job)


_SECRET_PATTERNS = [(re.compile(r"(?i)\b(apikey|api_key|apiKey|token|access_token|key)=([^&\s\"']+)"), r"\1=***"),
                    (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), "sk-ant-***")]


def redact(text):
    """Mask every secret before it is logged, stored in status.json or sent as a notification (DECISIONS.md 52):
    key=value pairs in web addresses, Anthropic keys, and the literal value of every key/topic in .env."""
    if not text:
        return text
    s = str(text)
    for pat, rep_ in _SECRET_PATTERNS:
        s = pat.sub(rep_, s)
    try:
        env = child_env()
    except Exception:                   # noqa: BLE001
        env = {}
    for k, v in env.items():
        if v and len(v) >= 8 and any(w in k.upper() for w in ("KEY", "TOKEN", "SECRET", "TOPIC", "PASSWORD")):
            s = s.replace(v, "***")
    return s


def run_subprocess(step, env, log):
    t0 = _time.time()
    log.write(f"\n=== {step['name']}: {' '.join(step['argv'])}\n")
    timed_out, code, out = False, None, ""
    try:
        p = subprocess.run(step["argv"], cwd=str(HERE), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=step["limit"])
        code, out = p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        timed_out = True
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
    except Exception as e:              # noqa: BLE001  (script missing, not executable, ...)
        code, out = 127, f"could not start: {e}"
    out = redact(out)
    log.write(out + ("\n" if not out.endswith("\n") else ""))
    return dict(code=code, timed_out=timed_out, limit=step["limit"], output=out, seconds=round(_time.time() - t0, 1))


NTFY_SERVER_DEFAULT = "https://ntfy.sh"


def phone_target():
    """(server, topic) for iPhone notifications through ntfy, from .env; topic None = off (DECISIONS.md 51)."""
    env = child_env()
    return (env.get("NTFY_SERVER") or NTFY_SERVER_DEFAULT).rstrip("/"), env.get("NTFY_TOPIC") or None


def public_site_url():
    """The GitHub Pages address, worked out from the git remote; None if there is no repository (DECISIONS.md 57)."""
    try:
        import sentinel_publish
        return sentinel_publish.site_url(HERE)
    except Exception:                   # noqa: BLE001
        return None


def push_phone(title, body):
    """Send the same line to the ntfy topic your iPhone subscribes to. Never raises: a phone that cannot be
    reached must not break a run. High priority when something failed or a new alert appeared."""
    server, topic = phone_target()
    if not topic:
        return False
    urgent = "FAIL" in body or "NEW alert" in body
    try:
        import requests
        headers = {"Title": title.encode("ascii", "replace").decode("ascii"),
                   "Priority": "high" if urgent else "default",
                   "Tags": "warning" if urgent else "white_check_mark"}
        url = public_site_url()
        if url:
            headers["Click"] = url      # tapping the notification opens the public site (DECISIONS.md 57)
        r = requests.post(f"{server}/{topic}", data=body[:1000].encode("utf-8"), timeout=15, headers=headers)
        return r.status_code == 200
    except Exception:                   # noqa: BLE001
        return False


def notify(title, body):
    """One macOS notification (on a Mac) and, if set up, the same line on your iPhone via ntfy."""
    sent = push_phone(title, body)
    if not shutil.which("osascript"):
        return sent
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')       # noqa: E731
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(body)[:230]}" with title "{esc(title)}"'],
                       timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:                   # noqa: BLE001
        return sent


SKIP_KEYS = {"daily": "SKIP_NEXT_DAILY", "weekly": "SKIP_NEXT_WEEKLY"}


def pending_skips():
    env = child_env()
    return [job for job, k in SKIP_KEYS.items() if env.get(k)]


def cmd_skip(job=None, off=False):
    """Skip only the next run of a job, instead of uninstalling the whole schedule (DECISIONS.md 53)."""
    if off:
        for k in SKIP_KEYS.values():
            env_set(k, None)
        print("No runs will be skipped. The schedule runs as normal.")
        return 0
    if job is None:
        s = pending_skips()
        print("Skipping the next " + " and the next ".join(s) + " run." if s else "Nothing is set to be skipped.")
        return 0
    if job not in SKIP_KEYS:
        print("Say which run to skip: python3 sentinel_pipeline.py skip weekly   (or: skip daily)")
        return 2
    env_set(SKIP_KEYS[job], datetime.now().astimezone().isoformat(timespec="seconds"))
    print(f"The next {job} run will be skipped (once), and you will get a notification saying so. "
          + ("Weekday runs continue as normal. " if job == "weekly" else "")
          + "Cancel with:  python3 sentinel_pipeline.py skip --off")
    return 0


def cmd_av_key():
    """Store the Alpha Vantage key in .env (private) without it appearing on screen or in shell history."""
    import getpass
    key = getpass.getpass("Paste your Alpha Vantage API key (it will not be shown), then press Enter: ").strip()
    if not key or not re.fullmatch(r"[A-Za-z0-9]{8,64}", key):
        print("That does not look like an Alpha Vantage key (letters and digits only). .env was not changed.")
        return 1
    had = bool(child_env().get("ALPHAVANTAGE_API_KEY"))
    env_set("ALPHAVANTAGE_API_KEY", key)
    print(f"ALPHAVANTAGE_API_KEY {'updated' if had else 'saved'} in .env (private, mode 600). The key was not printed.\n"
          "Check it with:  python3 sentinel_calendar.py --av-check   (uses 2 of your daily requests)")
    return 0


def cmd_phone(off=False, new=False):
    """Turn iPhone notifications on (a private, unguessable ntfy topic saved in .env) or off."""
    if off:
        env_set("NTFY_TOPIC", None)
        print("iPhone notifications are OFF. (Mac notifications continue.) In the ntfy app you can delete the subscription.")
        return 0
    server, topic = phone_target()
    if topic and not new:
        print(f"Already set up. Topic: {topic}")
    else:
        import secrets
        topic = "sentinel-" + secrets.token_urlsafe(18).replace("_", "x").replace("-", "y")
        env_set("NTFY_TOPIC", topic)
        print(f"New private topic saved in .env: {topic}")
    ok = push_phone("Sentinel", "Test: iPhone notifications are working. You will get one line per run.")
    print(("  ok    test message accepted by " if ok else "  FAIL  the test message could not be sent to ") + server)
    print(f"""
On your iPhone:
  1. Install "ntfy" from the App Store (free) and allow notifications when it asks.
  2. In the app, tap +, type this topic exactly, keep the server as {server}, tap Subscribe:
        {topic}
  3. Run this again to resend the test:  python3 sentinel_pipeline.py phone

The topic name works like a password: anyone who knows it can read your messages. They contain tickers and
verdicts only, never prices, cost or orders. To start over with a new name:  python3 sentinel_pipeline.py phone --new
To turn it off:  python3 sentinel_pipeline.py phone --off""")
    return 0 if ok else 1


def read_status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except Exception:                   # noqa: BLE001
        return {"runs": []}


def write_status(st):
    LOGS.mkdir(parents=True, exist_ok=True)
    st["runs"] = st["runs"][-KEEP_STATUS_RUNS:]
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(redact(json.dumps(st, indent=1)), encoding="utf-8")     # also cleans entries from before masking existed
    os.replace(str(tmp), str(STATUS))


def housekeeping(now):
    cutoff = now.timestamp() - KEEP_LOG_DAYS * 86400
    for f in LOGS.glob("*.log"):
        if f.stat().st_mtime < cutoff:
            f.unlink()


def schedule_notes(job, now, st):
    """Things that only show up over time: runs that did not happen."""
    notes = []
    today = now.astimezone().date()
    prior = [r for r in st["runs"] if r["job"] == job and not r.get("skipped")]
    if job == "daily" and prior:
        last = date.fromisoformat(prior[-1]["started"][:10])
        missed = business_days_between(last, today)
        if missed:
            notes.append((WARN, "no daily run on " + ", ".join(d.isoformat() for d in missed[-5:])
                          + " (Mac asleep, off or logged out? A missed close cannot be filled in later; holidays also show up here)"))
    if job == "daily":
        wk = [r for r in st["runs"] if r["job"] == "weekly" and not r.get("skipped")]
        first = date.fromisoformat(prior[0]["started"][:10]) if prior else today
        if not wk and (today - first).days > 9:
            notes.append((WARN, f"the weekly scan has not run since the scheduler started on {first.isoformat()}"))
        elif wk and (today - date.fromisoformat(wk[-1]["started"][:10])).days > 9:
            notes.append((WARN, f"the weekly scan last ran {wk[-1]['started'][:10]}, more than 9 days ago"))
    return notes


LAUNCHD_LABELS = {"daily": "local.stockresearch.daily", "weekly": "local.stockresearch.weekly"}


def schedule_state():
    """{job: True/False/None}: is the launchd agent installed and loaded? None = cannot tell (not a Mac)."""
    out = {}
    agents = Path.home() / "Library" / "LaunchAgents"
    for job, label in LAUNCHD_LABELS.items():
        if not shutil.which("launchctl"):
            out[job] = None
            continue
        plist = (agents / f"{label}.plist").exists()
        try:
            rc = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15).returncode
        except Exception:               # noqa: BLE001
            rc = 1
        out[job] = plist and rc == 0
    return out


def ledger_age():
    try:
        import yaml
        raw = yaml.safe_load((HERE / "positions.yaml").read_text(encoding="utf-8")) or {}
        d = raw.get("as_of")
        d = d if isinstance(d, date) else date.fromisoformat(str(d))
        return (date.today() - d).days
    except Exception:                   # noqa: BLE001
        return None


def run_job(job, dry_run=False, no_notify=False, now=None):
    now = now or datetime.now(timezone.utc)
    steps = job_steps(job)
    env = child_env()
    if not dry_run and job in SKIP_KEYS and env.get(SKIP_KEYS[job]):
        env_set(SKIP_KEYS[job], None)                    # one run only
        st = read_status()
        rec = dict(job=job, started=now.astimezone().isoformat(timespec="seconds"), level=OK, user_skip=True, steps=[],
                   notes=[f"skipped as you asked (set {env[SKIP_KEYS[job]][:16]})"])
        st["runs"].append(rec)
        write_status(st)
        msg = f"{job} skipped as you asked. It runs again at its next scheduled time."
        print(msg)
        if not no_notify:
            notify("Sentinel", msg)
        return 0
    if dry_run:
        print(f"{job}: {len(steps)} step(s) (dry run, nothing executed)")
        for s in steps:
            key = s.get("needs_key")
            print(f"  - {s['name']}: " + (" ".join(s["argv"]) if s["argv"] else "internal function")
                  + (f"   [{key}: {'found' if env.get(key) else 'MISSING'}]" if key else ""))
        return 0
    LOGS.mkdir(parents=True, exist_ok=True)
    lock = None
    if fcntl:
        lock = open(str(LOCK), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            st = read_status()
            st["runs"].append(dict(job=job, started=now.astimezone().isoformat(timespec="seconds"), level=WARN, skipped=True,
                                   steps=[], notes=["skipped: another pipeline run was still in progress"]))
            write_status(st)
            print("Another pipeline run is in progress; this one was skipped.")
            return 1
    log_path = LOGS / f"{now.astimezone().strftime('%Y-%m-%d_%H%M')}_{job}.log"
    st = read_status()
    record = dict(job=job, started=now.astimezone().isoformat(timespec="seconds"), steps=[], notes=[])
    ctx = {"now": now, "t0": _time.time()}
    with open(str(log_path), "w", encoding="utf-8") as log:
        log.write(f"{job} run started {record['started']}\npython: {py()}\nfolder: {HERE}\n")
        for level, msg in schedule_notes(job, now, st):
            record["notes"].append(f"{level}: {msg}")
            log.write(f"{level}: {msg}\n")
        for step in steps:
            ctx["t0"] = _time.time()
            key = step.get("needs_key")
            if key and not env.get(key):
                res = None
                fix = {"ALPHAVANTAGE_API_KEY": "python3 sentinel_pipeline.py av-key",
                       "ANTHROPIC_API_KEY": "python3 sentinel_tier2.py --save-key",
                       "CLAUDE_CODE_OAUTH_TOKEN": "python3 sentinel_tier2.py --save-token"}.get(key, "python3 sentinel_schedule.py")
                level, detail = FAIL, f"{key} is not set and not in .env, so this step was not run ({fix} fixes it)"
                seconds = 0
            elif step["argv"] is None:
                t0 = _time.time()
                try:
                    level, detail = step["internal"](now)
                except Exception as e:  # noqa: BLE001
                    level, detail = FAIL, f"{type(e).__name__}: {e}"
                seconds = round(_time.time() - t0, 1)
                log.write(f"\n=== {step['name']} (internal)\n{level}: {detail}\n")
            else:
                if step.get("before") == "open_escalations":
                    try:
                        ctx["open_before"] = open_escalations()
                        ctx["ids_before"] = {e["escalation_id"] for e in escalation_rows()}
                    except Exception:   # noqa: BLE001
                        pass
                res = run_subprocess(step, env, log)
                seconds = res["seconds"]
                try:
                    level, detail = step["check"](res, ctx)
                except Exception as e:  # noqa: BLE001
                    level, detail = FAIL, f"health check crashed: {type(e).__name__}: {e}"
            log.write(f"--> {step['name']}: {level} {detail}\n")
            detail = redact(detail)
            record["steps"].append(dict(name=step["name"], level=level, detail=detail, seconds=seconds))
        worst = max([RANK[s["level"]] for s in record["steps"]] + [RANK[n.split(":")[0]] for n in record["notes"]] + [0])
        record["level"] = [OK, WARN, FAIL][worst]
        record["new_escalations"] = ctx.get("new_escalations", 0)
        record["new_tickers"] = ctx.get("new_tickers", [])
        record["tier2"] = ctx.get("tier2_summary", "")
        record["news"] = ctx.get("news_alerts", "")
        record["log"] = log_path.name
        log.write(f"\nRESULT: {record['level']}\n")
    st["runs"].append(record)
    write_status(st)
    housekeeping(now)
    print(render_run(record))
    et = now_et(now)
    quiet_retry = job == "daily" and et is not None and et.hour < 12 and record["level"] == OK and not record["new_escalations"] \
        and not record.get("news")
    if not no_notify and not quiet_retry:      # the 08:15 retry only speaks up when something is wrong or new
        notify("Sentinel", redact(summary_text(job, record)))
    if lock:
        lock.close()
    return worst


def summary_text(job, r):
    """The one-line notification every run sends: OK or not, new alerts by ticker, Tier 2 verdicts. Counts and
    tickers only: never prices, cost basis or orders."""
    parts = [f"{job} {r['level']}"]
    for s in r["steps"]:
        if s["name"] == "scan+dossier" and s["level"] == OK:
            parts.append("screen " + s["detail"].split(": ", 1)[-1])
    n_new, tickers = r.get("new_escalations", 0), r.get("new_tickers") or []
    if n_new:
        parts.append(f"{n_new} NEW alert(s)" + (f": {', '.join(tickers[:6])}" if tickers else "") + ". See: python3 sentinel_tier1.py --show"
                     + ("" if job == "weekly" else ". Tier 2 reviews them Saturday"))
    else:
        parts.append("no new alerts")
    if r.get("tier2"):
        parts.append(f"Tier 2: {r['tier2']}. Read reviews/")
    if r.get("news"):
        parts.append(f"News: {r['news']}")             # negative or gate-relevant days only (DECISIONS.md 60)
    bad = [f"{s['name']}: {s['detail']}" for s in r["steps"] if s["level"] != OK] + r.get("notes", [])
    return ". ".join(parts + bad[:2])


def render_run(r):
    lines = [f"{r['job']} run {r['started'][:16].replace('T', ' ')}: {r['level']}"
             + (f"   (log: logs/{r['log']})" if r.get("log") else "")]
    for n in r.get("notes", []):
        lines.append(f"  note  {n}")
    for s in r["steps"]:
        lines.append(f"  {s['level']:<4}  {s['name']:<17} {s['detail']}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------ status / selfcheck
def cmd_status():
    st = read_status()
    print("Pipeline status\n")
    sched = schedule_state()
    if all(v is None for v in sched.values()):
        print("Schedule: cannot be checked here (not a Mac)\n")
    elif all(sched.values()):
        print("Schedule: ON (weekdays 16:30 ET, retry 08:15; Saturday 09:30 ET)\n")
    else:
        off = [j for j, v in sched.items() if v is False]
        print(f"Schedule: OFF for {', '.join(off)}  <- NOTHING runs automatically. Turn it on:  python3 sentinel_schedule.py\n")
    daily = [r for r in st["runs"] if r["job"] == "daily" and not r.get("skipped")]
    if daily:
        last = date.fromisoformat(daily[-1]["started"][:10])
        missed = business_days_between(last, date.today())
        if missed:
            print(f"Last daily run: {last} ({len(missed)} weekday(s) missed since)  <- check the schedule above, "
                  "and that the Mac is on and logged in\n")
    skips = pending_skips()
    if skips:
        print("Skipping: the next " + " and the next ".join(skips) + " run (python3 sentinel_pipeline.py skip --off cancels)\n")
    for job in ("daily", "weekly"):
        runs = [r for r in st["runs"] if r["job"] == job and not r.get("skipped")]
        if runs:
            print(redact(render_run(runs[-1])) + "\n")
        else:
            print(f"{job}: has not run under the scheduler yet\n")
    et = now_et()
    try:
        if et:
            fresh, total, missing, newest = price_freshness(expected_close(et))
            print(f"Prices: {fresh}/{total} tracked US names have the {expected_close(et)} close; newest stored {newest}."
                  + (f" Missing: {', '.join(missing[:8])}" if missing else ""))
        print(f"Tier 1 queue: {open_escalations()} open escalation(s)   (python3 sentinel_tier1.py --show)")
    except Exception as e:              # noqa: BLE001
        print(f"Database: could not be read ({e})")
    b = sorted(BACKUPS.glob("research_*.db")) if BACKUPS.exists() else []
    if b:
        age = (datetime.now().timestamp() - b[-1].stat().st_mtime) / 86400
        print(f"Backup: {b[-1].name}, {age:.0f} day(s) old ({len(b)} kept)")
    else:
        print("Backup: none yet")
    d = publish_dir()
    if d is None:
        print("Public page: not published (off)")
    else:
        f = Path(d).expanduser() / publish_name()
        err = validate_git_target(d) if publish_git() else validate_publish_dir(d)
        if err:
            print(f"Public page: PROBLEM with {d}: {err}")
        elif f.exists():
            line = f"Public page: {f}, {(datetime.now().timestamp() - f.stat().st_mtime) / 3600:.0f} h old (shareable version only)"
            if publish_git():
                gi = git_info(d)
                line += f"; git: {gi['remote']} ({gi['branch']})" + (f", WARNING {gi['unpushed']} commit(s) not pushed" if gi.get("unpushed") else "")
            print(line)
        else:
            print(f"Public page: {f} (not published yet)")
    age = ledger_age()
    if age is not None:
        print(f"Ledger: positions.yaml as_of is {age} day(s) old" + ("  <- refresh it (gates and outcomes go stale)" if age > LEDGER_STALE_DAYS else ""))
    return 0


def cmd_selfcheck():
    """Printed to stdout so that launchd captures it even if the project folder itself is off limits."""
    ok = True

    def line(good, what, detail=""):
        nonlocal ok
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {what}" + (f": {detail}" if detail else ""))
    print(f"selfcheck {datetime.now().isoformat(timespec='seconds')}  python {sys.version.split()[0]} at {sys.executable}")
    for mod in ("pandas", "requests", "lxml", "yaml"):
        try:
            __import__(mod)
            line(True, f"import {mod}")
        except ImportError as e:
            line(False, f"import {mod}", str(e))
    try:
        line((HERE / "positions.yaml").exists(), "can see positions.yaml in the project folder", str(HERE))
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / ".selfcheck").write_text("ok", encoding="utf-8")
        line(True, "can write to logs/")
    except OSError as e:
        line(False, "project folder is readable and writable from a scheduled job", f"{e}  (macOS blocks Documents/Desktop/Downloads for scheduled jobs)")
    try:
        conn = _db().connect(create=False)
        line(True, "can open research.db", f"{len(_db().table_counts(conn))} tables")
        conn.close()
    except Exception as e:              # noqa: BLE001
        line(False, "can open research.db", str(e))
    line(bool(child_env().get("ALPHAVANTAGE_API_KEY")), "ALPHAVANTAGE_API_KEY is available to scheduled jobs (.env), for the calendar",
         "" if child_env().get("ALPHAVANTAGE_API_KEY") else "python3 sentinel_pipeline.py av-key")
    k2 = tier2_key()
    line(bool(child_env().get(k2)), f"{k2} is available to scheduled jobs (.env), for Saturday's Tier 2",
         "" if child_env().get(k2) else ("python3 sentinel_tier2.py --save-token" if k2 == "CLAUDE_CODE_OAUTH_TOKEN"
                                         else "python3 sentinel_tier2.py --save-key"))
    if k2 == "CLAUDE_CODE_OAUTH_TOKEN":
        import sentinel_tier2 as t2
        exe = t2.claude_bin()
        line(bool(exe), "Claude Code (`claude`) is found by scheduled jobs, for Tier 2 on your subscription",
             exe or "install Claude Code, or set CLAUDE_BIN=/full/path/to/claude in .env")
    try:
        __import__("anthropic")
        line(True, "import anthropic (Tier 2)")
    except ImportError as e:
        line(False, "import anthropic (Tier 2)", f"{e}  (python3 -m pip install anthropic)")
    line(now_et() is not None, "New York time zone is available")
    if publish_dir() is not None and publish_git():
        err = validate_git_target(publish_dir())
        if err:
            line(False, "public page: git repository is ready", err)
        else:
            rc, out = git(["ls-remote", "--exit-code", "origin", "HEAD"], git_repo_top(publish_dir()), timeout=60)
            line(rc == 0, "can reach GitHub without a password prompt (git ls-remote)", "" if rc == 0 else last_lines(out))
    if phone_target()[1]:
        line(push_phone("Sentinel", "Selfcheck: the schedule can reach your iPhone."), "iPhone notifications (ntfy) reachable from a scheduled job")
    else:
        print("  info  iPhone notifications are off (python3 sentinel_pipeline.py phone turns them on)")
    if shutil.which("osascript"):
        print("  ok    notifications available (osascript)")
        if ok:
            notify("Stock research", "Scheduling is installed. This is a test notification.")
    else:
        print("  info  osascript not found: this is not a Mac, so notifications will be skipped")
    print("SELFCHECK " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 2


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["daily", "weekly", "backup", "status", "selfcheck", "publish", "publish-dir", "phone", "av-key", "skip"])
    ap.add_argument("path", nargs="?", help="publish-dir: the folder your web server shows")
    ap.add_argument("--off", action="store_true", help="publish-dir / phone: turn it off")
    ap.add_argument("--new", action="store_true", help="phone: make a new private topic")
    ap.add_argument("--git", action="store_true", help="publish-dir: PATH is inside a git clone; commit and push the page each run")
    ap.add_argument("--name", default=None, help="publish-dir: page file name (default earnings_calendar.html)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    a = ap.parse_args(argv)
    if a.command == "status":
        return cmd_status()
    if a.command == "selfcheck":
        return cmd_selfcheck()
    if a.command == "skip":
        return cmd_skip(a.path, a.off)
    if a.command == "av-key":
        return cmd_av_key()
    if a.command == "phone":
        return cmd_phone(off=a.off, new=a.new)
    if a.command == "publish-dir":
        return cmd_publish_dir(a.path, a.off, a.git, a.name)
    if a.command == "publish" and publish_dir() is None:
        print("No publish folder is set. Turn publishing on with:  python3 sentinel_pipeline.py publish-dir /path/to/the/folder/your/web/server/shows")
        return 2
    return run_job(a.command, dry_run=a.dry_run, no_notify=a.no_notify)


if __name__ == "__main__":
    sys.exit(main())

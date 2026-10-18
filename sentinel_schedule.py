#!/usr/bin/env python3
"""sentinel_schedule.py -- put the pipeline on a schedule (macOS launchd). Run it once, in your project folder:

    python3 sentinel_schedule.py               install (safe to run again; it replaces the old schedule)
    python3 sentinel_schedule.py --uninstall   remove the schedule entirely (to skip ONE run, use: sentinel_pipeline.py skip weekly)
    python3 sentinel_schedule.py --print-only  write the launchd files to ./schedule_preview/ and load nothing
    python3 sentinel_schedule.py --selfcheck   only re-run the check that a scheduled job can do its work

Schedule (New York time; your Mac's clock is used, converted if it is not on Eastern time):
    daily   Mon-Fri 16:30   quotes -> calendar -> Tier 1
    daily   Tue-Sat 08:15   the same, as a RETRY (before the open, Finviz still shows the previous close)
    weekly  Sat     09:30   scan + dossiers -> tracked dossiers -> Tier 1 -> Tier 2 -> database backup  (Saturday: review
                            on the weekend, place orders before Monday's open; DECISIONS.md 50)

What it does, in order: checks the folder and Python packages; saves your FMP_API_KEY in a private .env file
(launchd jobs do not see your shell profile); writes and loads the launchd agents; runs a self-check THROUGH
launchd to prove a scheduled job can read this folder and reach everything it needs; warns if the Mac is set to
sleep; records decisions 41-43 in DECISIONS.md. Nothing is changed outside this folder except
~/Library/LaunchAgents and ~/Library/Logs/stockresearch.
"""
import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
AGENTS = HOME / "Library" / "LaunchAgents"
OUTLOG = HOME / "Library" / "Logs" / "stockresearch"
LABEL = {"daily": "local.stockresearch.daily", "weekly": "local.stockresearch.weekly", "selfcheck": "local.stockresearch.selfcheck"}
# (launchd weekday: Sunday 0, Monday 1 ... Saturday 6), hour, minute, all in New York time
DAILY_SLOTS = [(d, 16, 30) for d in range(1, 6)] + [(d, 8, 15) for d in range(2, 7)]
WEEKLY_SLOTS = [(6, 9, 30)]     # Saturday 09:30, after the 08:15 retry has finished (it was Sunday 09:00 until item 50)
REQUIRED_FILES = ["sentinel_pipeline.py", "sentinel_db.py", "positions.yaml", "sentinel_scan.py", "sentinel_calendar.py", "sentinel_tier1.py",
                  "sentinel_tier2.py", "sentinel_reviews_page.py", "Investment_Strategy_Framework.md"]
PROTECTED = ("Documents", "Desktop", "Downloads", "Library/Mobile Documents", "iCloud", "Dropbox", "OneDrive")

DECISIONS = """
41. **2026-09-20: Phase 8 is a Python orchestrator, not shell scripts.** `sentinel_pipeline.py` (daily, weekly, backup, status,
    selfcheck) runs the existing programs as separate subprocesses, so item 15 stands. launchd user agents start it:
    daily Mon-Fri 16:30 New York time, a retry Tue-Sat 08:15 (Finviz still shows the previous close before the open),
    weekly Sunday 09:00. `sentinel_schedule.py` installs and removes it. A step that fails, hangs or stores nothing never
    stops the steps after it. Exit code 0 = OK, 1 = warnings, 2 = a failure.

42. **2026-09-20: Success is judged from the database, not the exit code.** The programs exit 0 even when they store
    nothing (the first `--quotes` run did). So the runner checks that the tracked US names have the expected close
    (FAIL under 90%), that a scan saved a NEW run, that the calendar page was rewritten, and that the backup passes
    `PRAGMA integrity_check`. Failures, new Tier 1 escalations and missed runs raise one macOS notification; every run
    writes `logs/*.log` and `logs/status.json`; `python3 sentinel_pipeline.py status` shows it all. A missed daily price
    cannot be filled in later (Finviz shows only the latest close), which is why the morning retry exists.

43. **2026-09-20: Operating conditions.** Weekly backups use SQLite's backup API into `backups/db/` (newest 8 kept).
    The FMP key is read from a private `.env` (mode 600) because launchd jobs have no shell environment. The Mac must
    not sleep (`sudo pmset -a sleep 0`) and you must be logged in (these are user agents). Scheduled jobs on macOS
    cannot read Documents, Desktop or Downloads without extra permission, so the project folder should live elsewhere;
    the installer's self-check tests this through launchd itself.
"""


def sh(cmd, timeout=60):
    """(returncode, combined output). One place to run commands, so tests can replace it."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except Exception as e:              # noqa: BLE001
        return 127, str(e)


# ------------------------------------------------------------------------------------------------ schedule maths
def utc_offset_minutes(tzname, when):
    from zoneinfo import ZoneInfo
    return int(when.astimezone(ZoneInfo(tzname)).utcoffset().total_seconds() // 60)


def to_local_slots(slots, local_minus_ny_minutes):
    """Shift New York slots to the Mac's clock. Returns launchd interval dicts."""
    out = []
    for wd, hh, mm in slots:
        total = hh * 60 + mm + local_minus_ny_minutes
        day_shift, minutes = divmod(total, 1440)
        out.append({"Weekday": (wd + day_shift) % 7, "Hour": minutes // 60, "Minute": minutes % 60})
    return out


def local_offset_difference(now=None):
    """Minutes by which this Mac's clock differs from New York's right now (0 when it is on Eastern time)."""
    now = now or datetime.now(timezone.utc)
    try:
        local = int(now.astimezone().utcoffset().total_seconds() // 60)
        return local - utc_offset_minutes("America/New_York", now)
    except Exception:                   # noqa: BLE001
        return 0


def build_plist(kind, intervals, python=None, folder=None, run_at_load=False):
    folder = Path(folder or HERE)
    d = {
        "Label": LABEL[kind],
        "ProgramArguments": [python or sys.executable, str(folder / "sentinel_pipeline.py"), kind],
        "WorkingDirectory": str(folder),
        "StandardOutPath": str(OUTLOG / f"{kind}.out"),
        "StandardErrorPath": str(OUTLOG / f"{kind}.err"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(HOME)},
        "ProcessType": "Background",
    }
    if intervals:
        d["StartCalendarInterval"] = intervals
    if run_at_load:
        d["RunAtLoad"] = True
    return d


# ------------------------------------------------------------------------------------------------ pieces
def parse_pmset_sleep(text):
    m = re.search(r"^\s*sleep\s+(\d+)", text or "", re.M)
    return int(m.group(1)) if m else None


def protected_folder(folder):
    s = str(folder)
    home = str(HOME)
    rel = s[len(home) + 1:] if s.startswith(home) else s
    return next((p for p in PROTECTED if rel == p or rel.startswith(p + "/") or f"/{p}/" in s), None)


def write_env(folder, environ):
    """Save FMP_API_KEY where a scheduled job can find it. Never prints the key. Returns a status string."""
    path = Path(folder) / ".env"
    key = environ.get("FMP_API_KEY")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    have = next((ln.split("=", 1)[1].strip().strip('"') for ln in lines if ln.startswith("FMP_API_KEY=")), None)
    if not key:
        return "kept" if have else "missing"
    if have == key:
        status = "kept"
    else:
        lines = [ln for ln in lines if not ln.startswith("FMP_API_KEY=")] + [f'FMP_API_KEY="{key}"']
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        status = "updated" if have else "saved"
    os.chmod(str(path), 0o600)
    return status


def record_decisions(folder):
    p = Path(folder) / "DECISIONS.md"
    if not p.exists():
        return "DECISIONS.md not found, skipped"
    t = p.read_text(encoding="utf-8")
    if "41. **2026-09-20: Phase 8" in t:
        return "already recorded"
    p.write_text((t if t.endswith("\n") else t + "\n") + DECISIONS, encoding="utf-8")
    return "added entries 41-43"


def launchctl_load(kind, plist_path, uid):
    """Replace any loaded copy, then load. Falls back to the older `load` if `bootstrap` is refused."""
    sh(["launchctl", "bootout", f"gui/{uid}/{LABEL[kind]}"])
    rc, out = sh(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)])
    if rc != 0:
        rc2, out2 = sh(["launchctl", "load", "-w", str(plist_path)])
        if rc2 != 0:
            return False, (out.strip() + " / " + out2.strip())[:300]
    rc, out = sh(["launchctl", "print", f"gui/{uid}/{LABEL[kind]}"])
    return rc == 0, "" if rc == 0 else out.strip()[:300]


def launchctl_unload(kind, uid):
    sh(["launchctl", "bootout", f"gui/{uid}/{LABEL[kind]}"])


def run_selfcheck_via_launchd(uid, folder, python, wait=45):
    """Load a one-shot agent that runs `sentinel_pipeline.py selfcheck` inside launchd and read what it printed."""
    OUTLOG.mkdir(parents=True, exist_ok=True)
    out = OUTLOG / "selfcheck.out"
    if out.exists():
        out.unlink()
    path = AGENTS / f"{LABEL['selfcheck']}.plist"
    with open(str(path), "wb") as f:
        plistlib.dump(build_plist("selfcheck", None, python, folder, run_at_load=True), f)
    ok, why = launchctl_load("selfcheck", path, uid)
    text = ""
    deadline = time.time() + wait
    while time.time() < deadline:
        if out.exists():
            text = out.read_text(encoding="utf-8", errors="replace")
            if "SELFCHECK PASSED" in text or "SELFCHECK FAILED" in text:
                break
        time.sleep(1)
    launchctl_unload("selfcheck", uid)
    if path.exists():
        path.unlink()
    if not text:
        err = OUTLOG / "selfcheck.err"
        text = err.read_text(encoding="utf-8", errors="replace")[-600:] if err.exists() else (why or "the self-check printed nothing")
    return "SELFCHECK PASSED" in text, text.strip()


# ------------------------------------------------------------------------------------------------ main
def uninstall(uid):
    for kind in ("daily", "weekly", "selfcheck"):
        launchctl_unload(kind, uid)
        p = AGENTS / f"{LABEL[kind]}.plist"
        if p.exists():
            p.unlink()
    print("Schedule REMOVED. Nothing will run automatically -- no prices, no calendar, no alerts, no notifications --\n"
          "until you turn it back on with:  python3 sentinel_schedule.py\n"
          "Your data, logs and backups were not touched.\n"
          "Tip: to skip just one run, don't uninstall; use:  python3 sentinel_pipeline.py skip weekly   (or: skip daily)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--print-only", action="store_true")
    ap.add_argument("--selfcheck", action="store_true", help="only re-run the self-check through launchd (for example after turning on GitHub publishing)")
    ap.add_argument("--yes-sleep", action="store_true", help="do not ask; prevent system sleep (needs your password)")
    a = ap.parse_args(argv)
    uid = os.getuid()
    on_mac = sys.platform == "darwin"

    if a.uninstall:
        return uninstall(uid)
    if a.selfcheck:
        if not on_mac:
            print("The launchd self-check only runs on a Mac.")
            return 2
        passed, text = run_selfcheck_via_launchd(uid, HERE, sys.executable)
        print("\n".join("  " + ln for ln in text.splitlines()))
        return 0 if passed else 1

    print("Installing the stock-research schedule\n")
    missing = [f for f in REQUIRED_FILES if not (HERE / f).exists()]
    if missing:
        print("STOPPED: these files are not in this folder: " + ", ".join(missing))
        return 2
    bad = []
    for mod in ("pandas", "requests", "lxml", "yaml", "anthropic"):
        try:
            __import__(mod)
        except ImportError:
            bad.append(mod)
    if bad:
        print("STOPPED: missing Python packages: " + ", ".join(bad) + "\n  Run: python3 -m pip install requests pandas lxml pyyaml anthropic")
        return 2
    print(f"  ok    folder {HERE}\n  ok    Python {sys.version.split()[0]} at {sys.executable}")

    st = write_env(HERE, os.environ)
    msg = {"saved": "FMP_API_KEY saved to .env (private, mode 600)", "updated": "FMP_API_KEY updated in .env",
           "kept": "FMP_API_KEY already in .env", "missing": None}[st]
    if msg:
        print("  ok    " + msg + " (optional since Oct 2026: only the --fmp-* extras use it)")
    envtxt = (HERE / ".env").read_text(encoding="utf-8") if (HERE / ".env").exists() else ""
    if "ALPHAVANTAGE_API_KEY=" in envtxt or os.environ.get("ALPHAVANTAGE_API_KEY"):
        print("  ok    ALPHAVANTAGE_API_KEY found (the calendar's earnings source)")
    else:
        print("  WARN  ALPHAVANTAGE_API_KEY is not in .env, so the calendar step will FAIL.\n"
              "        Fix: python3 sentinel_pipeline.py av-key")

    (HERE / "logs").mkdir(exist_ok=True)
    (HERE / "backups" / "db").mkdir(parents=True, exist_ok=True)
    OUTLOG.mkdir(parents=True, exist_ok=True)

    diff = local_offset_difference()
    if diff:
        print(f"  note  this Mac's clock is {diff / 60:+.1f} h from New York; the schedule was converted. "
              "(A daylight-saving change on one side only can move it by an hour for a few weeks.)")
    prot = protected_folder(HERE)
    if prot:
        print(f"  WARN  this folder is under '{prot}'. macOS blocks scheduled jobs from reading Documents, Desktop and\n"
              "        Downloads, and cloud-synced folders can corrupt SQLite. The self-check below will tell you whether it\n"
              "        works; if not, move the whole folder to your home folder (for example ~/first_project) and re-run this.")

    plists = {"daily": build_plist("daily", to_local_slots(DAILY_SLOTS, diff)),
              "weekly": build_plist("weekly", to_local_slots(WEEKLY_SLOTS, diff))}
    target = HERE / "schedule_preview" if (a.print_only or not on_mac) else AGENTS
    target.mkdir(parents=True, exist_ok=True)
    for kind, d in plists.items():
        with open(str(target / f"{LABEL[kind]}.plist"), "wb") as f:
            plistlib.dump(d, f)
    if a.print_only or not on_mac:
        print(f"\n{'Print-only mode' if a.print_only else 'This is not a Mac'}: the launchd files are in {target}/ and nothing was loaded.")
        print("  " + record_decisions(HERE))
        return 0

    for kind in ("daily", "weekly"):
        ok, why = launchctl_load(kind, AGENTS / f"{LABEL[kind]}.plist", uid)
        print(f"  {'ok  ' if ok else 'FAIL'}  {kind} schedule " + ("loaded" if ok else f"could not be loaded: {why}"))
        if not ok:
            return 2

    print("\nSelf-check, run by launchd itself (this proves a scheduled job can do its work) ...")
    passed, text = run_selfcheck_via_launchd(uid, HERE, sys.executable)
    print("\n".join("    " + ln for ln in text.splitlines()))
    if not passed:
        print("\nThe schedule is installed, but the self-check FAILED. Fix what is marked FAIL above and run this installer again.")

    rc, out = sh(["pmset", "-g"])
    sleep_min = parse_pmset_sleep(out)
    if sleep_min not in (None, 0):
        print(f"\n  WARN  this Mac is set to sleep after {sleep_min} min of inactivity. A sleeping Mac skips the 16:30 run and a missed "
              "close cannot be recovered.")
        answer = "y" if a.yes_sleep else (input("        Stop it sleeping now? Runs 'sudo pmset -a sleep 0' and asks for your password [y/N] ").strip().lower()
                                          if sys.stdin.isatty() else "n")
        if answer == "y":
            rc = subprocess.call(["sudo", "pmset", "-a", "sleep", "0"])
            print("  ok    sleep disabled (the display can still turn off)" if rc == 0 else "  FAIL  could not change the setting")
        else:
            print("        Later, in Terminal:  sudo pmset -a sleep 0")

    print("\n  " + record_decisions(HERE))
    print("""
Done. What happens now:
  - Mon-Fri 16:30 New York time: quotes, calendar page, Tier 1.  Tue-Sat 08:15: the same as a retry.
  - Saturday 09:30: scan, dossiers, Tier 1, Tier 2 reviews (reviews/), backup.
  - Every run sends ONE macOS notification summing it up. No notification on a weekday = the run did not happen.
  - One screen with everything:   python3 sentinel_pipeline.py status
  - Full logs:                    logs/     Backups: backups/db/
  - Remove the schedule:          python3 sentinel_schedule.py --uninstall
Your Mac must stay on and you must stay logged in.""")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

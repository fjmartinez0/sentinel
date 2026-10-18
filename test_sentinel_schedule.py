"""Phase 8 tests: sentinel_schedule.py. launchctl, pmset and sudo are replaced by fakes; nothing touches the real system."""
import contextlib, io, os, plistlib, stat, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJ))
import sentinel_schedule as ins

results = []
def check(label, ok, detail=""):
    ok = bool(ok); results.append(ok); print(("  ok    " if ok else "  FAIL  ") + label + ("" if ok else f"  {detail}"))

tmp = Path(tempfile.mkdtemp())
ins.HOME = tmp / "home"; ins.AGENTS = ins.HOME / "Library" / "LaunchAgents"; ins.OUTLOG = ins.HOME / "Library" / "Logs" / "stockresearch"
ins.AGENTS.mkdir(parents=True)

# ------------------------------------------------------------------------------------------ schedule maths
print("Schedule")
S = ins.to_local_slots(ins.DAILY_SLOTS, 0)
check("on Eastern time: 5 weekday closes at 16:30 (Mon-Fri) and 5 retries at 08:15 (Tue-Sat)",
      len(S) == 10 and [d for d in S if d["Hour"] == 16] == [{"Weekday": w, "Hour": 16, "Minute": 30} for w in range(1, 6)]
      and [d["Weekday"] for d in S if d["Hour"] == 8] == [2, 3, 4, 5, 6])
check("the weekly job is Saturday (launchd weekday 6) 09:30, after the 08:15 retry (DECISIONS.md 50)", ins.to_local_slots(ins.WEEKLY_SLOTS, 0) == [{"Weekday": 6, "Hour": 9, "Minute": 30}])
C = ins.to_local_slots(ins.DAILY_SLOTS, -60)
check("a Mac on Central time (one hour behind) runs at 15:30 and 07:15", {(d["Hour"], d["Minute"]) for d in C} == {(15, 30), (7, 15)})
T = ins.to_local_slots([(5, 16, 30)], 13 * 60)       # a Friday close, Mac 13 h ahead of New York
check("a Mac far ahead of New York rolls over midnight into the NEXT weekday (Fri 16:30 -> Sat 05:30)", T == [{"Weekday": 6, "Hour": 5, "Minute": 30}], str(T))
W = ins.to_local_slots([(0, 0, 30)], -60)
check("...and behind it wraps to the previous weekday (Sun 00:30 -> Sat 23:30)", W == [{"Weekday": 6, "Hour": 23, "Minute": 30}], str(W))
check("there is no Monday-morning retry (a Sunday has no close to retry); retries are Tue-Sat", all(d[0] != 1 for d in ins.DAILY_SLOTS if d[1] == 8))
import time as _t
_now = datetime(2026, 9, 21, 20, 30, tzinfo=timezone.utc)
_old_tz = os.environ.get("TZ")
try:
    res = {}
    for tz in ("America/New_York", "America/Chicago", "Europe/London"):
        os.environ["TZ"] = tz; _t.tzset(); res[tz] = ins.local_offset_difference(_now)
finally:
    if _old_tz is None: os.environ.pop("TZ", None)
    else: os.environ["TZ"] = _old_tz
    _t.tzset()
check("the Mac's clock vs New York is measured: Eastern 0, Central -60, London +300 minutes (in September)", res == {"America/New_York": 0, "America/Chicago": -60, "Europe/London": 300}, str(res))

# ------------------------------------------------------------------------------------------ plist
print("\nlaunchd files")
pl = ins.build_plist("daily", S, python="/usr/bin/python3", folder="/Users/x/first_project")
buf = plistlib.dumps(pl); back = plistlib.loads(buf)
check("the plist is valid XML that round-trips", back == pl)
check("it runs the ABSOLUTE python and script paths (launchd has no PATH or shell), from the project folder",
      back["ProgramArguments"] == ["/usr/bin/python3", "/Users/x/first_project/sentinel_pipeline.py", "daily"] and back["WorkingDirectory"] == "/Users/x/first_project")
check("launchd's own output goes to ~/Library/Logs (never blocked), not into the project folder",
      back["StandardOutPath"].startswith(str(ins.OUTLOG)) and back["StandardErrorPath"].startswith(str(ins.OUTLOG)))
check("labels are distinct and it does not run at load (only on schedule)", len({ins.LABEL[k] for k in ins.LABEL}) == 3 and "RunAtLoad" not in back)
check("the self-check agent runs once at load and has no schedule", (lambda p: p.get("RunAtLoad") is True and "StartCalendarInterval" not in p and p["ProgramArguments"][-1] == "selfcheck")(ins.build_plist("selfcheck", None, "/p", "/f", run_at_load=True)))

# ------------------------------------------------------------------------------------------ .env
print("\nPrivate key file")
proj = tmp / "proj"; proj.mkdir()
check("no key anywhere: reported as missing, nothing written", ins.write_env(proj, {}) == "missing" and not (proj / ".env").exists())
check("a key in the environment is saved", ins.write_env(proj, {"FMP_API_KEY": "SECRET123"}) == "saved")
mode = stat.S_IMODE((proj / ".env").stat().st_mode)
check("the file is private (owner read/write only, mode 600)", mode == 0o600, oct(mode))
check("running again with the same key changes nothing", ins.write_env(proj, {"FMP_API_KEY": "SECRET123"}) == "kept")
(proj / ".env").write_text('OTHER=1\nFMP_API_KEY="SECRET123"\n')
check("a different key replaces the old line and keeps other settings", ins.write_env(proj, {"FMP_API_KEY": "NEW456"}) == "updated"
      and (proj / ".env").read_text() == 'OTHER=1\nFMP_API_KEY="NEW456"\n', (proj / ".env").read_text())
check("with no key in this Terminal but one already in .env, it is kept", ins.write_env(proj, {}) == "kept")

# ------------------------------------------------------------------------------------------ small helpers
print("\nHelpers")
PM = "System-wide power settings:\nCurrently in use:\n standby              1\n Sleep On Power Button 1\n disksleep            10\n sleep                1 (sleep prevented by coreaudiod)\n displaysleep         10\n"
check("pmset output: reads the system sleep minutes, not disksleep or 'Sleep On Power Button'", ins.parse_pmset_sleep(PM) == 1)
check("...0 means never sleeps; unreadable output is None", ins.parse_pmset_sleep(PM.replace("sleep                1", "sleep                0")) == 0 and ins.parse_pmset_sleep("nothing") is None)
check("folders macOS blocks for scheduled jobs are recognised; a plain home folder is fine",
      ins.protected_folder(ins.HOME / "Documents" / "first_project") == "Documents" and ins.protected_folder(ins.HOME / "Desktop" / "x") == "Desktop"
      and ins.protected_folder(ins.HOME / "first_project") is None and ins.protected_folder(ins.HOME / "Library" / "Mobile Documents" / "x") == "Library/Mobile Documents")
d = tmp / "d"; d.mkdir(); (d / "DECISIONS.md").write_text("# log\n\n39. old entry\n")
r1 = ins.record_decisions(d); after = (d / "DECISIONS.md").read_text(); r2 = ins.record_decisions(d)
check("decisions 41-43 are appended once, after the existing text, and never twice",
      r1 == "added entries 41-43" and after.startswith("# log\n\n39. old entry\n") and "41. **2026-09-20: Phase 8" in after and "43. **2026-09-20: Operating" in after
      and r2 == "already recorded" and (d / "DECISIONS.md").read_text() == after)

# ------------------------------------------------------------------------------------------ launchctl sequence
print("\nlaunchctl (fake)")
CALLS = []
def fake_sh_factory(bootstrap_rc=0, load_rc=0, print_rc=0):
    def fake(cmd, timeout=60):
        CALLS.append(cmd)
        if cmd[:2] == ["launchctl", "bootstrap"]: return bootstrap_rc, "Bootstrap failed: 5: Input/output error" if bootstrap_rc else ""
        if cmd[:2] == ["launchctl", "load"]: return load_rc, "load failed" if load_rc else ""
        if cmd[:2] == ["launchctl", "print"]: return print_rc, "" if print_rc == 0 else "Could not find service"
        if cmd[:1] == ["pmset"]: return 0, PM
        return 0, ""
    return fake
ins.sh = fake_sh_factory()
ok, why = ins.launchctl_load("daily", Path("/x/a.plist"), 501)
check("loading replaces any old copy first (bootout), then bootstraps into the user's GUI domain, then verifies with print",
      ok and [c[1] for c in CALLS] == ["bootout", "bootstrap", "print"] and CALLS[0][2] == "gui/501/local.stockresearch.daily" and CALLS[1][2] == "gui/501", str(CALLS))
CALLS.clear(); ins.sh = fake_sh_factory(bootstrap_rc=5)
ok, why = ins.launchctl_load("daily", Path("/x/a.plist"), 501)
check("if bootstrap is refused it falls back to the older 'load -w'", ok and ["launchctl", "load", "-w", "/x/a.plist"] in CALLS, str(CALLS))
CALLS.clear(); ins.sh = fake_sh_factory(bootstrap_rc=5, load_rc=1)
ok, why = ins.launchctl_load("daily", Path("/x/a.plist"), 501)
check("if both fail it says so, with the reason", not ok and "Input/output error" in why)
CALLS.clear(); ins.sh = fake_sh_factory(print_rc=113)
ok, why = ins.launchctl_load("daily", Path("/x/a.plist"), 501)
check("loaded-but-not-visible to launchctl print is reported as a failure, not assumed fine", not ok and "Could not find" in why)

# ------------------------------------------------------------------------------------------ self-check through launchd
print("\nSelf-check through launchd (fake)")
def selfcheck_sh(text):
    def fake(cmd, timeout=60):
        CALLS.append(cmd)
        if cmd[:2] == ["launchctl", "bootstrap"]:
            ins.OUTLOG.mkdir(parents=True, exist_ok=True); (ins.OUTLOG / "selfcheck.out").write_text(text)
        return 0, ""
    return fake
CALLS.clear(); ins.sh = selfcheck_sh("selfcheck ...\n  ok    import pandas\nSELFCHECK PASSED\n")
passed, text = ins.run_selfcheck_via_launchd(501, tmp / "proj", "/usr/bin/python3", wait=3)
check("a passing self-check is read back and reported", passed and "SELFCHECK PASSED" in text)
check("the one-shot agent is unloaded and its plist deleted afterwards", CALLS[-1][1] == "bootout" and not (ins.AGENTS / "local.stockresearch.selfcheck.plist").exists())
CALLS.clear(); ins.sh = selfcheck_sh("  FAIL  project folder is readable and writable from a scheduled job: [Errno 1] Operation not permitted\nSELFCHECK FAILED\n")
passed, text = ins.run_selfcheck_via_launchd(501, tmp / "proj", "/usr/bin/python3", wait=3)
check("a failing self-check (for example the macOS folder block) is reported with its reason", not passed and "Operation not permitted" in text)
ins.sh = lambda cmd, timeout=60: (0, "")
passed, text = ins.run_selfcheck_via_launchd(501, tmp / "proj", "/usr/bin/python3", wait=2)
check("if the job prints nothing at all that is a failure with an explanation, not a hang", not passed and text)

# ------------------------------------------------------------------------------------------ whole installer
print("\nWhole installer")
def make_home(name):
    h = tmp / name; h.mkdir()
    for f in ins.REQUIRED_FILES: (h / f).write_text("# stub\n")
    (h / "DECISIONS.md").write_text("39. x\n")
    return h
def run_main(argv, env=None, tty=False):
    out = io.StringIO(); code = None
    old_env = dict(os.environ)
    os.environ.pop("FMP_API_KEY", None); os.environ.update(env or {})
    try:
        with contextlib.redirect_stdout(out): code = ins.main(argv)
    finally:
        os.environ.clear(); os.environ.update(old_env)
    return code, out.getvalue()

ins.HERE = make_home("h1"); ins.sh = fake_sh_factory()
CALLS.clear()
code, out = run_main(["--print-only"], {"FMP_API_KEY": "TOPSECRET"})
files = sorted(p.name for p in (ins.HERE / "schedule_preview").glob("*.plist"))
check("--print-only writes the two launchd files", code == 0 and files == ["local.stockresearch.daily.plist", "local.stockresearch.weekly.plist"], f"{code} {files}\n{out}")
check("--print-only loads nothing (no launchctl calls) and never prints the key", CALLS == [] and "TOPSECRET" not in out, f"{CALLS} {out}")
check("...and the key is in the private .env, the decisions were recorded", stat.S_IMODE((ins.HERE / ".env").stat().st_mode) == 0o600 and "TOPSECRET" in (ins.HERE / ".env").read_text() and "41. **2026-09-20" in (ins.HERE / "DECISIONS.md").read_text())

code, out = run_main(["--print-only"])
ins.HERE = tmp / "empty"; ins.HERE.mkdir()
code, out = run_main([])
check("a folder missing the programs is refused with the list of what is missing", code == 2 and "sentinel_pipeline.py" in out and "STOPPED" in out, out)

ins.HERE = make_home("h2")
real_platform, real_call = sys.platform, ins.subprocess.call
sys.platform = "darwin"; SUDO = []
ins.subprocess.call = lambda cmd, *a, **k: SUDO.append(cmd) or 0
ins.sh = (lambda base: (lambda cmd, timeout=60: (ins.OUTLOG.mkdir(parents=True, exist_ok=True) or (ins.OUTLOG / "selfcheck.out").write_text("SELFCHECK PASSED\n")) and (0, "") if cmd[:2] == ["launchctl", "bootstrap"] and "selfcheck" in cmd[-1] else base(cmd, timeout)))(fake_sh_factory())
code, out = run_main(["--yes-sleep"], {"FMP_API_KEY": "K"})
loaded = sorted(p.name for p in ins.AGENTS.glob("*.plist"))
check("full install (fake Mac): daily and weekly agents are written to LaunchAgents, the self-check agent is cleaned up",
      code == 0 and loaded == ["local.stockresearch.daily.plist", "local.stockresearch.weekly.plist"], f"{code} {loaded}\n{out}")
check("the self-check result is shown, and the sleeping Mac is flagged", "SELFCHECK PASSED" in out and "set to sleep after 1 min" in out, out)
check("with --yes-sleep it runs exactly 'sudo pmset -a sleep 0' (and only that)", SUDO == [["sudo", "pmset", "-a", "sleep", "0"]], str(SUDO))
check("the closing summary tells you how to check status and how to remove it", "sentinel_pipeline.py status" in out and "--uninstall" in out)

SUDO.clear(); ins.HERE = make_home("h3")
ins.sh = (lambda base: (lambda cmd, timeout=60: (ins.OUTLOG.mkdir(parents=True, exist_ok=True) or (ins.OUTLOG / "selfcheck.out").write_text("  FAIL  can open research.db: no such table\nSELFCHECK FAILED\n")) and (0, "") if cmd[:2] == ["launchctl", "bootstrap"] and "selfcheck" in cmd[-1] else base(cmd, timeout)))(fake_sh_factory())
code, out = run_main([], {"FMP_API_KEY": "K"})
check("without --yes-sleep and without a terminal it does NOT run sudo, and tells you the one command", SUDO == [] and "sudo pmset -a sleep 0" in out, out)
check("a failed self-check gives exit code 1 and says what to do", code == 1 and "self-check FAILED" in out and "can open research.db" in out, out)
sys.platform, ins.subprocess.call = real_platform, real_call

print("\nSelf-check only")
ins.HERE = make_home("h4"); sys.platform = "darwin"
for _p in ins.AGENTS.glob("*.plist"): _p.unlink()
ins.sh = (lambda base: (lambda cmd, timeout=60: (ins.OUTLOG.mkdir(parents=True, exist_ok=True) or (ins.OUTLOG / "selfcheck.out").write_text("  ok    x\nSELFCHECK PASSED\n")) and (0, "") if cmd[:2] == ["launchctl", "bootstrap"] and "selfcheck" in cmd[-1] else base(cmd, timeout)))(fake_sh_factory())
code, out = run_main(["--selfcheck"])
check("--selfcheck runs only the launchd self-check: it prints the result, installs no schedule and touches no files",
      code == 0 and "SELFCHECK PASSED" in out and not list(ins.AGENTS.glob("*.plist")) and not (ins.HERE / ".env").exists() and "Installing" not in out, out)
sys.platform = real_platform

print("\nUninstall")
CALLS.clear(); ins.sh = fake_sh_factory()
for k in ("daily", "weekly"): (ins.AGENTS / f"{ins.LABEL[k]}.plist").write_text("x")
buf = io.StringIO()
with contextlib.redirect_stdout(buf): code = ins.uninstall(501)
check("uninstall unloads all three agents and deletes their files, leaving data alone",
      code == 0 and sorted(c[2] for c in CALLS if c[1] == "bootout") == sorted(f"gui/501/{v}" for v in ins.LABEL.values())
      and not list(ins.AGENTS.glob("*.plist")) and "not touched" in buf.getvalue(), str(CALLS))

import shutil; shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} checks")
sys.exit(0 if all(results) else 1)

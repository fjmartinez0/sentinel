#!/usr/bin/env python3
"""migrate_to_sentinel.py -- run ONCE, after copying all the new sentinel_*.py files into this folder
alongside your existing (old-named) files:

    python3 migrate_to_sentinel.py

What it does, in order:
  1. Checks the new sentinel_*.py files are actually here (you copy those in yourself first).
  2. Uninstalls your current launchd schedule (it points at the old filenames -- it must go first).
  3. Moves every old-named file to backups/pre_sentinel_rename/ -- nothing is deleted.
  4. Runs sentinel_schedule.py to install the schedule fresh, under the new names.

Safe to re-run: already-migrated files are skipped, not re-backed-up.
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKUP = HERE / "backups" / "pre_sentinel_rename"

# old name -> new name, code files only (positions.yaml, research_log.yaml, research.db, DECISIONS.md,
# .env, the ledger and framework .md files are untouched -- this only retires the old SCRIPT names).
RENAMES = {
    "db.py": "sentinel_db.py",
    "schema.sql": "sentinel_schema.sql",
    "finviz_quote.py": "sentinel_finviz.py",
    "scan_finviz_candidates.py": "sentinel_scan.py",
    "generate_earnings_calendar.py": "sentinel_calendar.py",
    "tier1.py": "sentinel_tier1.py",
    "tier1_rules.yaml": "sentinel_tier1_rules.yaml",
    "run_pipeline.py": "sentinel_pipeline.py",
    "install_schedule.py": "sentinel_schedule.py",
    "probe_quote_page.py": "sentinel_probe.py",
}
REQUIRED_NEW = ["sentinel_pipeline.py", "sentinel_schedule.py", "sentinel_db.py"]


def main():
    missing = [f for f in REQUIRED_NEW if not (HERE / f).exists()]
    if missing:
        sys.exit("STOPPED, nothing changed: copy the new sentinel_*.py files into this folder first "
                 f"(missing: {', '.join(missing)}).")

    old_here = [old for old in RENAMES if (HERE / old).exists()]
    if not old_here:
        print("No old-named files found -- looks like this is already migrated. Nothing to do.")
    else:
        # 1. uninstall the OLD schedule (it still points at the old filenames)
        old_installer = HERE / "install_schedule.py"
        if old_installer.exists():
            print("Uninstalling the schedule installed under the old names ...")
            r = subprocess.run([sys.executable, str(old_installer), "--uninstall"], cwd=str(HERE))
            if r.returncode != 0:
                print("  (uninstall reported a problem -- continuing anyway; sentinel_schedule.py will "
                      "replace it below regardless.)")
        else:
            print("No old install_schedule.py found here -- assuming no schedule is installed under the old names.")

        # 2. move every old-named file aside, never delete
        BACKUP.mkdir(parents=True, exist_ok=True)
        for old in old_here:
            dest = BACKUP / old
            if dest.exists():
                dest.unlink()
            shutil.move(str(HERE / old), str(dest))
            print(f"  moved {old} -> backups/pre_sentinel_rename/{old}")

    # 3. install fresh under the new names
    print("\nInstalling the schedule under the new names ...")
    r = subprocess.run([sys.executable, str(HERE / "sentinel_schedule.py")], cwd=str(HERE))
    if r.returncode != 0:
        sys.exit("\nsentinel_schedule.py reported a problem -- see its output above. The old files are safe in "
                 "backups/pre_sentinel_rename/ if you need to look at them.")

    print("\nDone. Old files are kept, unused, in backups/pre_sentinel_rename/ -- delete that folder yourself "
          "once you've confirmed 'python3 sentinel_pipeline.py status' looks right.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
sentinel_publish.py -- put the latest data on the public site (DECISIONS.md 57, stage 2).

Runs as the last data step of the daily and Saturday jobs:
  1. stamps site/data/meta.json with this run's time and job (proof of life on the page);
  2. checks the site files (sentinel_validate.py --site) and refuses to go on if they are broken;
  3. commits ONLY the generated files (site/data/ and reviews/) -- the git hook scans them for keys first;
  4. pushes to GitHub. GitHub Actions then checks everything again and deploys the page.

It never commits anything you edit by hand (code, positions.yaml, the framework...): you commit those yourself,
after looking at `git status`. A push that fails is retried at the next run (the commit stays on the Mac).

  python3 sentinel_publish.py --job daily            what the pipeline runs
  python3 sentinel_publish.py --job manual --no-push commit only (to look first)
  python3 sentinel_publish.py --site-url             print the public address

Exit codes: 0 published (or nothing new), 2 not set up / refused, 3 committed but the push failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sentinel_validate as validate  # noqa: E402

PUBLISHED = ("site/data", "reviews")          # the only paths this program ever stages


def git(args, cwd, timeout=120):
    """Run git without ever waiting for a password prompt. Returns (returncode, output)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    try:
        p = subprocess.run(["git"] + list(args), cwd=str(cwd), env=env, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, p.stdout.decode("utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        return 124, f"git {args[0]} timed out after {timeout}s"
    except OSError as e:
        return 127, str(e)


def site_url(base=HERE):
    """https://OWNER.github.io/REPO/ from the 'origin' remote (https or ssh form), or None."""
    rc, out = git(["remote", "get-url", "origin"], base, timeout=15)
    if rc != 0:
        return None
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", out)
    return f"https://{m.group(1).lower()}.github.io/{m.group(2)}/" if m else None


def write_meta(base, job, now=None):
    p = base / "site" / "data" / "meta.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    meta = {"schema": 1, "last_run_at": (now or datetime.now().astimezone()).isoformat(timespec="seconds"), "job": job}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    tmp.replace(p)
    return meta


def problem_state(base):
    """Why it is not safe to commit right now, or None."""
    if not (base / ".git").exists():
        return "this folder is not a git repository yet (stage 1 of DECISIONS.md 57)"
    rc, out = git(["remote", "get-url", "origin"], base, timeout=15)
    if rc != 0:
        return "no 'origin' remote (git remote add origin https://github.com/OWNER/REPO.git)"
    rc, branch = git(["rev-parse", "--abbrev-ref", "HEAD"], base, timeout=15)
    if rc != 0 or branch != "main":
        return f"the folder is on branch '{branch}', not 'main': publishing waits until you switch back"
    for marker in ("MERGE_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD"):
        if (base / ".git" / marker).exists():
            return "a git merge or rebase is in progress: finish it first"
    rc, staged = git(["diff", "--cached", "--name-only"], base, timeout=30)
    others = [f for f in staged.splitlines() if f and not f.startswith(PUBLISHED)]
    if others:
        return (f"you have {len(others)} file(s) of your own staged ({', '.join(others[:3])}): commit or unstage them, "
                "so a data commit never sweeps them in")
    return None


def publish(base=HERE, job="manual", push=True, now=None):
    """(exit code, message lines)."""
    lines = []
    why = problem_state(base)
    if why:
        return 2, [f"NOT published: {why}."]
    write_meta(base, job, now)
    broken = validate.check_site(base / "site")
    if broken:
        return 2, ["NOT published: the site files failed their check:"] + [f"  {b}" for b in broken[:10]]
    existing = [p for p in PUBLISHED if (base / p).exists()]
    rc, out = git(["add", "--"] + existing, base)
    if rc != 0:
        return 2, [f"NOT published: git add failed ({out[-200:]})."]
    rc, staged = git(["diff", "--cached", "--name-only"], base)
    files = [f for f in staged.splitlines() if f]
    if not files:
        lines.append("Nothing new to commit.")
    else:
        stamp = (now or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
        rc, out = git(["commit", "-q", "-m", f"Data: {job} run {stamp}"], base)
        if rc != 0:                                   # the hook found a key, or git refused: never leave it staged
            git(["reset", "-q", "--"] + existing, base)
            return 2, ["NOT published: the commit was refused (the safety check ran; nothing was committed):"] + \
                      [f"  {ln}" for ln in out.splitlines()[-12:]]
        lines.append(f"Committed {len(files)} file(s): " + ", ".join(sorted({f.split('/')[0] + ('/' + f.split('/')[1] if f.count('/') > 1 else '') for f in files})))
    if not push:
        return 0, lines + ["Not pushed (--no-push)."]
    rc, ahead = git(["rev-list", "--count", "@{u}..HEAD"], base, timeout=30)
    if rc == 0 and ahead.strip() == "0":
        return 0, lines + ["GitHub is already up to date."]
    rc, out = git(["push", "-q", "origin", "main"], base, timeout=180)
    if rc != 0:
        tail = " ".join(out.splitlines()[-3:])[:300]
        return 3, lines + [f"PUSH FAILED: {tail}", "The commit is kept on the Mac and goes up with the next run's push."]
    url = site_url(base)
    return 0, lines + ["Pushed to GitHub. GitHub Actions checks it again and updates the page"
                       + (f": {url}" if url else ".")]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", default="manual", help="daily, weekly or manual (shown on the page)")
    ap.add_argument("--no-push", action="store_true", help="commit, but do not push")
    ap.add_argument("--site-url", action="store_true", help="print the public address and stop")
    ap.add_argument("--dir", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    base = Path(a.dir).resolve() if a.dir else HERE
    if a.site_url:
        url = site_url(base)
        print(url or "No GitHub 'origin' remote found.")
        return 0 if url else 2
    code, lines = publish(base, a.job, push=not a.no_push)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())

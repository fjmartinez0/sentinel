#!/usr/bin/env python3
"""
sentinel_validate.py -- the gate in front of git (DECISIONS.md 57). Standard library only, so it runs with any
python3 on the Mac (the git hook) and, later, in GitHub Actions.

  python3 sentinel_validate.py --secrets            check every file git would publish (tracked + new, not ignored)
  python3 sentinel_validate.py --secrets --staged   check only what is staged for the next commit (the git hook)

What it refuses:
  * files that must never be published: .env, any *.db / *.db-wal / *.db-shm, .claude_tier2/
  * any file containing the value of a key, token or ntfy topic from .env (8+ characters), or an Anthropic key/token
    (sk-ant-...), or a GitHub token (ghp_..., github_pat_...)
It never prints a secret: only the file name, the line number and which .env name matched.

The repository is public by the investor's choice (decision 57): holdings, costs and gates may be published.
Keys and tokens may not, ever.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORBIDDEN_NAMES = (".env", "*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3")
FORBIDDEN_DIRS = (".claude_tier2/", "logs/", "backups/")
PATTERNS = [("an Anthropic key or token", re.compile(r"sk-ant-[A-Za-z0-9_\-]{12,}")),
            ("a GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"))]
# Test files carry obviously fake keys on purpose; the patterns skip a match containing one of these words.
FAKE_MARKERS = ("test", "fake", "abc", "zzz", "x" * 20, "example", "dummy", "otherproject", "shouldnotpass")
MIN_SECRET_LEN = 8
SECRET_NAME = re.compile(r"KEY|TOKEN|TOPIC|SECRET|PASS|AUTH|CREDENTIAL", re.I)   # which .env names hold secrets


def read_env(path):
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def secret_values(base):
    """{value: NAME} for every .env value long enough to be a secret (budgets, backend names and flags are skipped)."""
    vals = {}
    for k, v in read_env(base / ".env").items():
        if not SECRET_NAME.search(k):          # settings like NTFY_SERVER or CLAUDE_BIN are not secrets
            continue
        if len(v) >= MIN_SECRET_LEN and not re.fullmatch(r"[0-9.]+|true|false|api|subscription", v, re.I):
            vals[v] = k
    return vals


def git_files(base, staged):
    cmd = (["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"] if staged
           else ["git", "ls-files", "--cached", "--others", "--exclude-standard"])
    p = subprocess.run(cmd, cwd=base, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "git failed (is this folder a git repository?)")
    return [f for f in p.stdout.splitlines() if f.strip()]


def forbidden(path):
    name = path.rsplit("/", 1)[-1]
    if any(fnmatch.fnmatch(name, pat) for pat in FORBIDDEN_NAMES):
        return "this kind of file is never published"
    if any(path.startswith(d) or f"/{d}" in path for d in FORBIDDEN_DIRS):
        return "this folder is never published"
    return None


def scan(base, files):
    """[(file, line or None, reason)] -- empty means safe."""
    secrets = secret_values(base)
    problems = []
    for f in files:
        why = forbidden(f)
        if why:
            problems.append((f, None, why))
            continue
        p = base / f
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue                                   # binary file: nothing to read as text
        text = data.decode("utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            for value, name in secrets.items():
                if value in line:
                    problems.append((f, n, f"contains the value of {name} from .env"))
            for label, rx in PATTERNS:
                for m in rx.finditer(line):
                    if not any(w in m.group(0).lower() for w in FAKE_MARKERS):
                        problems.append((f, n, f"looks like {label}"))
    return problems


SITE_FILES = {"meta.json": ("last_run_at",), "calendar.json": ("today", "top_html", "items"),
              "reviews.json": ("body_html",)}
SITE_REQUIRED = ("meta.json",)                     # calendar/reviews appear after their first run; the page copes
MAX_JSON_BYTES = 8_000_000


def check_site(site):
    """Integrity of the public site folder (DECISIONS.md 57). [] = fine. Runs on the Mac before a commit and again
    in GitHub Actions before deploying, so a broken export never goes live."""
    site = Path(site)
    problems = []
    if not (site / "index.html").is_file():
        problems.append("site/index.html is missing")
    elif 'id="tab-calendar"' not in (site / "index.html").read_text(encoding="utf-8", errors="replace"):
        problems.append("site/index.html is not Sentinel's page (no Calendar tab): wrong file?")
    data = site / "data"
    for name, keys in SITE_FILES.items():
        p = data / name
        if not p.exists():
            if name in SITE_REQUIRED:
                problems.append(f"site/data/{name} is missing")
            continue
        if p.stat().st_size > MAX_JSON_BYTES:
            problems.append(f"site/data/{name} is {p.stat().st_size // 1_000_000} MB, larger than expected")
            continue
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            problems.append(f"site/data/{name} is not valid JSON ({e})")
            continue
        if not isinstance(obj, dict):
            problems.append(f"site/data/{name} is not a JSON object")
            continue
        missing = [k for k in keys if k not in obj]
        if missing:
            problems.append(f"site/data/{name} lacks {', '.join(missing)}")
        if name == "calendar.json" and not isinstance(obj.get("items"), list):
            problems.append("site/data/calendar.json: items is not a list")
    for p in data.glob("*.tmp") if data.exists() else []:
        problems.append(f"site/data/{p.name} is a half-written file")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--secrets", action="store_true", help="check for keys, tokens and private working files")
    ap.add_argument("--staged", action="store_true", help="only the files staged for the next commit")
    ap.add_argument("--site", nargs="?", const="site", default=None, metavar="DIR",
                    help="check the public site folder (default: site): index.html and the data files")
    ap.add_argument("--dir", default=None, help="project folder (default: this script's folder, or the git hook's)")
    args = ap.parse_args(argv)
    if not args.secrets and not args.site:
        ap.print_help()
        return 2
    base = Path(args.dir).resolve() if args.dir else Path.cwd() if (Path.cwd() / ".git").exists() else HERE
    if args.site:
        broken = check_site(base / args.site)
        if broken:
            print("STOPPED: the site is not fit to publish:")
            for b in broken:
                print(f"  {b}")
            return 1
        print(f"OK: {args.site}/ is complete and readable.")
        if not args.secrets:
            return 0
    try:
        files = git_files(base, args.staged)
    except (RuntimeError, OSError) as e:
        print(f"sentinel_validate.py: {e}")
        return 2
    problems = scan(base, files)
    if problems:
        print(f"STOPPED: {len(problems)} problem(s). Nothing should be committed until these are fixed:")
        for f, n, why in problems[:40]:
            print(f"  {f}{':' + str(n) if n else ''}  {why}")
        print("Fix: remove the secret from the file (keys live only in .env), or add the file to .gitignore, then try again.")
        return 1
    print(f"OK: {len(files)} file(s) checked, no keys, tokens or private working files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

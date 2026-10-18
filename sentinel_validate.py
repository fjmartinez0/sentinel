#!/usr/bin/env python3
"""
sentinel_validate.py -- the gate in front of git (DECISIONS.md 57). Standard library only, so it runs with any
python3 on the Mac (the git hook) and, later, in GitHub Actions.

  python3 sentinel_validate.py --secrets            check every file git would publish (tracked + new, not ignored)
  python3 sentinel_validate.py --secrets --staged   check only what is staged for the next commit (the git hook)

What it refuses:
  * files that must never be published: .env, any *.db / *.db-wal / *.db-shm, .claude_tier2/
  * any file containing the value of a key or token from .env (8+ characters), or an Anthropic key/token
    (sk-ant-...), or a GitHub token (ghp_..., github_pat_...)
It never prints a secret: only the file name, the line number and which .env name matched.

The repository is public by the investor's choice (decision 57): holdings, costs and gates may be published.
Keys and tokens may not, ever.
"""

from __future__ import annotations

import argparse
import fnmatch
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--secrets", action="store_true", help="check for keys, tokens and private working files")
    ap.add_argument("--staged", action="store_true", help="only the files staged for the next commit")
    ap.add_argument("--dir", default=None, help="project folder (default: this script's folder, or the git hook's)")
    args = ap.parse_args(argv)
    if not args.secrets:
        ap.print_help()
        return 2
    base = Path(args.dir).resolve() if args.dir else Path.cwd() if (Path.cwd() / ".git").exists() else HERE
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

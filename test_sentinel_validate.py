"""Tests for sentinel_validate.py (the gate in front of git) and the pre-commit hook. Uses temporary git repositories."""
import os, shutil, stat, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sentinel_validate as v

results = []
def check(label, ok, detail=""):
    ok = bool(ok); results.append(ok); print(("  ok    " if ok else "  FAIL  ") + label + ("" if ok else f"  {detail}"))

# isolate git from the Mac's own settings (a global identity or hooks broke a test once)
_empty = tempfile.NamedTemporaryFile(delete=False); _empty.close()
ENV = dict(os.environ, GIT_CONFIG_GLOBAL=_empty.name, GIT_CONFIG_NOSYSTEM="1",
           GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
os.environ.update({k: ENV[k] for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")})


TOK = "sk-" + "ant-oat01-" + "REALTOKENvalue1234567890"     # built at run time, so this file itself holds no token-shaped text


def git(base, *a):
    return subprocess.run(["git", *a], cwd=base, capture_output=True, text=True, env=ENV)


def repo(td):
    base = Path(td)
    git(base, "init", "-q", "-b", "main")
    shutil.copy(HERE / ".gitignore", base / ".gitignore")
    (base / ".githooks").mkdir()
    shutil.copy(HERE / ".githooks" / "pre-commit", base / ".githooks" / "pre-commit")
    os.chmod(base / ".githooks" / "pre-commit", 0o755)
    shutil.copy(HERE / "sentinel_validate.py", base / "sentinel_validate.py")
    (base / ".env").write_text('ALPHAVANTAGE_API_KEY="AVREALKEY777"\nCLAUDE_CODE_OAUTH_TOKEN="' + TOK + '"\n'
                               'NTFY_TOPIC="sentinel_q8x2k9z7"\nTIER2_BACKEND="subscription"\nTIER2_MONTHLY_BUDGET="15"\n')
    (base / "positions.yaml").write_text("held:\n  - {ticker: NYT, avg_cost: 52.1, shares: 10, account: TFSA}\n")
    (base / "research.db").write_bytes(b"SQLite format 3\0...")
    for d in ("logs", "backups/db", ".claude_tier2", "reviews"):
        (base / d).mkdir(parents=True, exist_ok=True)
    (base / "logs" / "x.log").write_text("token AVREALKEY777 in a log")
    (base / "reviews" / "2026-10-18_tier2.md").write_text("# reviews\nCRWV at_risk")
    (base / "test_thing.py").write_text('KEY = "sk-ant-oat01-' + "x" * 60 + '"\nOTHER = "sk-ant-api03-SHOULDNOTPASS-123456"\n')
    return base


print("The gate in front of git")
with tempfile.TemporaryDirectory() as td:
    base = repo(td)
    files = v.git_files(base, staged=False)
    check(".gitignore keeps out .env, the database, logs, backups and the Claude Code folder",
          not any(f in files for f in (".env", "research.db")) and not any(f.startswith(("logs/", "backups/", ".claude_tier2/")) for f in files), files)
    check("...while positions.yaml (holdings, costs) and the reviews ARE published, by choice (decision 57)",
          "positions.yaml" in files and "reviews/2026-10-18_tier2.md" in files)
    check("a clean folder passes; fake test keys and budget/backend values are not mistaken for secrets", v.scan(base, files) == [], v.scan(base, files))

    (base / "notes.md").write_text("my key is AVREALKEY777, do not share\n")
    (base / "site.json").write_text('{"x": "' + TOK + '"}\n')
    (base / "gh.txt").write_text("ghp_" + "A1b2C3d4" * 4 + "\n")
    probs = v.scan(base, v.git_files(base, staged=False))
    got = {(p[0], p[2].split(" from")[0]) for p in probs}
    check("an .env value pasted into any file is caught, naming the variable, never printing the value",
          ("notes.md", "contains the value of ALPHAVANTAGE_API_KEY") in got and all("AVREALKEY777" not in p[2] for p in probs), probs)
    check("a real-looking Anthropic token and a GitHub token are caught", any(p[0] == "site.json" for p in probs)
          and any(p[0] == "gh.txt" and "GitHub" in p[2] for p in probs), probs)
    code = v.main(["--secrets", "--dir", str(base)])
    check("the command exits 1 (stop) when anything is found", code == 1)
    for f in ("notes.md", "site.json", "gh.txt"):
        (base / f).unlink()

    check("a forced add of .env or the database is refused even past .gitignore",
          v.forbidden(".env") and v.forbidden("research.db") and v.forbidden("backups/db/research_1.db") and not v.forbidden("positions.yaml"))

    # the hook, through a real commit
    git(base, "config", "core.hooksPath", ".githooks")
    git(base, "add", "-A")
    c = git(base, "commit", "-q", "-m", "first")
    check("the pre-commit hook lets a clean first commit through", c.returncode == 0, c.stdout + c.stderr)
    (base / "leak.md").write_text("AVREALKEY777\n")
    git(base, "add", "leak.md")
    c = git(base, "commit", "-q", "-m", "oops")
    check("the hook BLOCKS a commit that contains a key, and says which file", c.returncode != 0 and "leak.md" in (c.stdout + c.stderr), c.stdout + c.stderr)
    git(base, "rm", "-q", "--cached", "leak.md"); (base / "leak.md").unlink()
    git(base, "add", "-f", ".env")
    c = git(base, "commit", "-q", "-m", "force env")
    check("the hook BLOCKS .env even when added with --force", c.returncode != 0 and ".env" in (c.stdout + c.stderr))
    git(base, "rm", "-q", "--cached", ".env")
    check("--staged checks only what is about to be committed", v.main(["--secrets", "--staged", "--dir", str(base)]) == 0)

with tempfile.TemporaryDirectory() as td:
    check("outside a git repository it says so instead of crashing", v.main(["--secrets", "--dir", td]) == 2)

# the real project files: nothing in the code itself may trip the gate
with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    git(base, "init", "-q", "-b", "main")
    for f in HERE.iterdir():
        if f.is_file() and f.suffix in (".py", ".sql", ".yaml", ".md", ".html") or f.name == ".gitignore":
            shutil.copy(f, base / f.name)
    probs = v.scan(base, v.git_files(base, staged=False))
    check("the project's own code and tests pass the gate (no false alarms on the fake keys in tests)", probs == [], probs[:5])

os.unlink(_empty.name)
print(f"\n{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} checks")
sys.exit(0 if all(results) else 1)

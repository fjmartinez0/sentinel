"""Tests for the public site (DECISIONS.md 57, stage 2): the data files, the integrity check, and the publisher
(commit + push) against a local stand-in for GitHub. Nothing touches the network or the real repository."""
import json, os, shutil, subprocess, sys, tempfile
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sentinel_validate as v, sentinel_publish as pub, sentinel_calendar as cal, sentinel_reviews_page as rp, sentinel_db as db

results = []
def check(label, ok, detail=""):
    ok = bool(ok); results.append(ok); print(("  ok    " if ok else "  FAIL  ") + label + ("" if ok else f"  {detail}"))

_empty = tempfile.NamedTemporaryFile(delete=False); _empty.close()
GENV = {"GIT_CONFIG_GLOBAL": _empty.name, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
os.environ.update(GENV)


def git(base, *a):
    return subprocess.run(["git", *a], cwd=base, capture_output=True, text=True)


TOK = "sk-" + "ant-oat01-" + "REALTOKENvalue1234567890"

# ------------------------------------------------------------------------------------------------ data files
print("The site's data files")
TODAY = date(2026, 10, 18)
pos = {"as_of": "2026-08-28", "held": [], "watch": [], "needs_confirmation": []}
data = cal.build(TODAY, pos, cal.DEMO_SP500, cal.demo_calendar(TODAY), {}, None)
meta = {"offline": False, "share": False, "db": None}
c = cal.site_payload(TODAY, pos, data, meta)
check("calendar.json carries the same pieces as the local page (sub, top, bottom, items)",
      set(c) >= {"today", "sub", "top_html", "bottom_html", "items"} and c["items"] == data["items"])
check("the public page drops the 'Local use only' banner (decision 57); the local page keeps it",
      "Local use only" not in c["top_html"] and "Local use only" in cal.render_html(TODAY, pos, data, meta))
check("calendar.json has no run timestamp, so it only changes in git when the calendar changes", "generated_at" not in c)

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "r.db"
    conn = db.connect(p); db.init_schema(conn)
    eid, _ = db.insert_escalation(conn, ticker="AMRZ", funnel="B", trigger="candidate", trigger_ref="2026-10-17", reasons=["r"])
    db.insert_tier2_review(conn, escalation_id=eid, ticker="AMRZ", mode="live", batch="b", model="m (subscription)", ok=True,
                           verdict="watch", result={"verdict": "watch", "scores": {"asymmetry": 3, "catalyst": 4, "proof": 4, "exit": 3}},
                           report_md="## Why\nok", created_at="2026-10-18T15:00:00+00:00")
    r1 = rp.site_payload(conn, TODAY)
    r2 = rp.site_payload(conn, date(2026, 10, 19))
    conn.close()
    check("reviews.json holds the reviews tab, and is identical from one day to the next when nothing changed",
          "AMRZ" in r1["body_html"] and r1 == r2 and r1["n_reviews"] == 1)
    out = Path(td) / "site" / "data" / "reviews.json"
    code = rp.main(["--db", str(p), "--out", str(Path(td) / "page.html"), "--site", str(out)])
    check("the reviews command writes site/data/reviews.json only when asked with --site", code == 0 and json.loads(out.read_text())["n_reviews"] == 1)

# ------------------------------------------------------------------------------------------------ integrity
print("The site check (Mac and GitHub Actions)")
with tempfile.TemporaryDirectory() as td:
    site = Path(td) / "site"
    shutil.copytree(HERE / "site", site, ignore=shutil.ignore_patterns("data"))
    (site / "data").mkdir()
    check("missing meta.json is refused", any("meta.json is missing" in x for x in v.check_site(site)))
    (site / "data" / "meta.json").write_text('{"last_run_at": "2026-10-18T20:00:00-04:00"}')
    check("index + meta is enough for a first publish (calendar and reviews arrive with their first run)", v.check_site(site) == [], v.check_site(site))
    (site / "data" / "calendar.json").write_text('{"today": "2026-10-18", "top_html": "", "items": {}}')
    check("a calendar.json whose items are not a list is refused", any("items is not a list" in x for x in v.check_site(site)))
    (site / "data" / "calendar.json").write_text('{"today": ')
    check("a half-written JSON file is refused", any("not valid JSON" in x for x in v.check_site(site)))
    (site / "data" / "calendar.json").write_text(json.dumps(c))
    (site / "data" / "reviews.json").write_text(json.dumps(r1))
    (site / "data" / "x.json.tmp").write_text("{}")
    check("a leftover temporary file is refused", any("half-written" in x for x in v.check_site(site)))
    (site / "data" / "x.json.tmp").unlink()
    check("real calendar and reviews data pass", v.check_site(site) == [], v.check_site(site))
    (site / "index.html").write_text("<html>something else</html>")
    check("a wrong index.html is refused", any("not Sentinel's page" in x for x in v.check_site(site)))

# ------------------------------------------------------------------------------------------------ publisher
print("Publishing: commit the data, push to GitHub (a local stand-in)")


def project(td):
    base = Path(td) / "sentinel"
    remote = Path(td) / "github.git"
    git(Path(td), "init", "-q", "--bare", "-b", "main", str(remote))
    base.mkdir()
    git(base, "init", "-q", "-b", "main")
    for f in (".gitignore", "sentinel_validate.py", "sentinel_publish.py"):
        shutil.copy(HERE / f, base / f)
    shutil.copytree(HERE / ".githooks", base / ".githooks")
    shutil.copytree(HERE / "site", base / "site", ignore=shutil.ignore_patterns("data"))
    (base / "site" / "data").mkdir()
    (base / "site" / "data" / "calendar.json").write_text(json.dumps(c))
    (base / "reviews").mkdir()
    (base / "reviews" / "2026-10-18_tier2.md").write_text("# AMRZ watch\n")
    (base / "positions.yaml").write_text("held: []\n")
    (base / ".env").write_text(f'CLAUDE_CODE_OAUTH_TOKEN="{TOK}"\nALPHAVANTAGE_API_KEY="AVREALKEY777"\n')
    git(base, "config", "core.hooksPath", ".githooks")
    git(base, "remote", "add", "origin", str(remote))
    git(base, "add", "-A"); git(base, "commit", "-q", "-m", "first"); git(base, "push", "-q", "-u", "origin", "main")
    return base, remote


NOW = datetime(2026, 10, 19, 16, 45, tzinfo=timezone.utc)
with tempfile.TemporaryDirectory() as td:
    base, remote = project(td)
    (base / "site" / "data" / "reviews.json").write_text(json.dumps(r1))
    (base / "reviews" / "2026-10-19_tier2.md").write_text("# new review\n")
    (base / "positions.yaml").write_text("held: [{ticker: NYT}]\n")          # your own edit: must NOT be committed
    code, lines = pub.publish(base, "daily", now=NOW)
    log = git(remote, "log", "--oneline", "-1").stdout
    shown = git(remote, "show", "--stat", "--format=", "HEAD").stdout
    check("the data is committed and pushed in one commit named after the run", code == 0 and "Data: daily run 2026-10-19" in log, (code, lines, log))
    check("...containing site/data and reviews/ only: your edited positions.yaml stays uncommitted",
          "site/data/reviews.json" in shown and "reviews/2026-10-19_tier2.md" in shown and "positions.yaml" not in shown
          and "positions.yaml" in git(base, "status", "--short").stdout, shown)
    meta_pushed = json.loads(git(remote, "show", "HEAD:site/data/meta.json").stdout)
    check("meta.json is stamped with the run time and job (the page shows it as proof of life)",
          meta_pushed["job"] == "daily" and meta_pushed["last_run_at"].startswith("2026-10-19"), meta_pushed)
    check("the message points at the public address", any("Pushed to GitHub" in ln for ln in lines))

    code, lines = pub.publish(base, "daily", now=NOW)
    check("running again with nothing new: nothing committed, nothing pushed", code == 0 and "Nothing new to commit." in lines
          and "GitHub is already up to date." in lines, lines)

    git(base, "add", "positions.yaml")
    code, lines = pub.publish(base, "daily", now=NOW)
    check("if you have your own file staged, it refuses rather than sweeping it into a data commit",
          code == 2 and "positions.yaml" in lines[0], lines)
    git(base, "reset", "-q", "positions.yaml")

    (base / "reviews" / "leak.md").write_text("pasted key AVREALKEY777\n")
    code, lines = pub.publish(base, "weekly", now=NOW)
    staged = git(base, "diff", "--cached", "--name-only").stdout
    check("a key in a review file: the hook refuses the commit, nothing is pushed, nothing is left staged",
          code == 2 and "refused" in lines[0] and staged.strip() == "" and "leak.md" not in git(remote, "show", "--stat", "HEAD").stdout, (lines, staged))
    (base / "reviews" / "leak.md").unlink()

    git(base, "remote", "set-url", "origin", str(Path(td) / "nowhere.git"))
    (base / "reviews" / "2026-10-20_tier2.md").write_text("# another\n")
    code, lines = pub.publish(base, "daily", now=NOW)
    check("GitHub unreachable: committed on the Mac, exit 3, and it says the next run pushes it",
          code == 3 and any("PUSH FAILED" in ln for ln in lines) and "next run" in lines[-1], lines)
    git(base, "remote", "set-url", "origin", str(remote))
    code, lines = pub.publish(base, "daily", now=NOW)
    check("...and the next run pushes the waiting commit", code == 0 and "reviews/2026-10-20_tier2.md" in
          git(remote, "log", "--name-only", "--format=", "-3").stdout, lines)

    git(base, "checkout", "-q", "-b", "experiment")
    code, lines = pub.publish(base, "daily", now=NOW)
    check("on another branch it waits instead of publishing", code == 2 and "not 'main'" in lines[0], lines)
    git(base, "checkout", "-q", "main")

with tempfile.TemporaryDirectory() as td:
    code, lines = pub.publish(Path(td), "daily", now=NOW)
    check("a folder that is not a repository: a clear refusal, nothing written", code == 2 and "not a git repository" in lines[0]
          and not (Path(td) / "site").exists(), lines)

with tempfile.TemporaryDirectory() as td:
    git(Path(td), "init", "-q", "-b", "main")
    for url, want in (("https://github.com/fjmartinez0/sentinel.git", "https://fjmartinez0.github.io/sentinel/"),
                      ("git@github.com:FJMartinez0/sentinel.git", "https://fjmartinez0.github.io/sentinel/"),
                      ("https://github.com/fjmartinez0/sentinel", "https://fjmartinez0.github.io/sentinel/")):
        git(Path(td), "remote", "remove", "origin"); git(Path(td), "remote", "add", "origin", url)
        check(f"the public address is worked out from the remote ({url.split(':')[0]}...)", pub.site_url(Path(td)) == want, pub.site_url(Path(td)))

wf = (HERE / ".github" / "workflows" / "pages.yml").read_text()
check("GitHub Actions runs both checks before deploying, and stamps when the data (not the run stamp) last changed",
      "--secrets" in wf and "--site" in wf and "deploy-pages" in wf and ":(exclude)site/data/meta.json" in wf
      and wf.index("--site") < wf.index("upload-pages-artifact"))

os.unlink(_empty.name)
print(f"\n{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} checks")
sys.exit(0 if all(results) else 1)

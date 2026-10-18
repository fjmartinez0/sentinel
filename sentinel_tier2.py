#!/usr/bin/env python3
"""
sentinel_tier2.py -- Tier 2 (Phase 7): Claude reviews the names Tier 1 flagged, using your framework.

For each escalation it sends Claude (through the Anthropic API, with web search) the Tier 1 alert, the stored
Finviz page and your framework file, and gets back a written review: why it moved, the framework's 1A checks,
the index rule, four scores and a verdict. The review is ADVICE. This program never closes an escalation,
never edits positions.yaml, research_log.yaml, the ledger or the framework, and never trades. You read the
review and decide, then close the alert yourself with sentinel_tier1.py --mark.

  python3 sentinel_tier2.py --save-key             store your Anthropic API key privately in .env (hidden prompt)
  python3 sentinel_tier2.py --calibrate --dry-run  show what would be sent and the estimated cost; calls nothing
  python3 sentinel_tier2.py --calibrate            review the known-answer set (AMRZ, COO, BMI, OTF) and compare
  python3 sentinel_tier2.py                        review the open queue (refused until a calibration has passed)
  python3 sentinel_tier2.py --id 7                 review one escalation, whatever its status
  python3 sentinel_tier2.py --show                 recent reviews and this month's spending

Two ways to reach Claude (DECISIONS.md 55), chosen by TIER2_BACKEND in .env:
  subscription  Claude Code (`claude -p`) on your Claude plan: no API credits; uses the plan's usage limits.
                python3 sentinel_tier2.py --save-token   (after `claude setup-token`), then --sub-check
  api           the Anthropic API with your prepaid credits (the original setup, and the default).
  Switch any time:  --use-api   /   --use-subscription

Reviews are written to reviews/YYYY-MM-DD_tier2.md and stored in research.db (table tier2_reviews).
Needs Anthropic's Python library, once:  python3 -m pip install anthropic

Safety rails (framework section 10, DECISIONS.md 46-49):
  * Read-only on your files. The only things it writes are its own table, the reviews/ folder and, with
    --save-key, one line of .env.
  * Spending: at most MAX_PER_RUN reviews per run, at most MAX_SEARCHES web searches per review, and a
    monthly budget (default $15, override with TIER2_MONTHLY_BUDGET in .env). Set a hard limit in the
    Anthropic Console as well: this budget only counts what this program has recorded.
  * Live runs are refused until the latest calibration matched your own verdicts on every name
    (override with --force, deliberately).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import ssl
import sys
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import sentinel_db as db

try:
    import yaml
except ImportError:
    yaml = None

# ------------------------------------------------------------------------------------------------ settings
# Model and prices checked against platform.claude.com/docs/en/about-claude/pricing on 2026-09-26.
# If Anthropic changes prices, update PRICES; the recorded costs are estimates computed from these numbers.
MODEL = "claude-sonnet-5"
PRICES = {                                    # USD per million tokens: (input, output)
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}
UNKNOWN_MODEL_PRICE = (10.00, 50.00)          # deliberately pessimistic for a model not in the table
CACHE_WRITE_MULT, CACHE_READ_MULT = 1.25, 0.10
SEARCH_USD = 0.01                              # $10 per 1,000 web searches
MAX_SEARCHES = 10                              # per review
MAX_PER_RUN = 5                                # reviews per invocation
MAX_TOKENS = 12000                             # output cap per API request
MAX_CONTINUATIONS = 6                          # pause_turn continuations per review
DEFAULT_MONTHLY_BUDGET = 15.00
EST_COST_PER_REVIEW = 0.60                     # used only to stop BEFORE a review that would cross the budget
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
# Newest first. If the account or model rejects the newer tool version, the older one is tried once.
SEARCH_TOOL_VERSIONS = ("web_search_20260318", "web_search_20250305")
RETRY_WAITS = (10, 30)                         # seconds, for rate limits and overloads
STREAM_TOTAL_SECONDS = 1800.0                  # a single review may take a while; this is the hard stop
STREAM_IDLE_SECONDS = 300.0                    # longest silence allowed on the stream (Anthropic sends keep-alive pings)

# The verdicts you reached by hand on 2026-09-26 (DECISIONS.md 47). Tier 2 must match these before going live.
# COO changed from watch to dismiss on 2026-10-18 (DECISIONS.md 56): the investor accepted Tier 2's reading that a fourth
# guidance cut in two years is the section 1A(b) repeated-cut pattern, not a one-off.
CALIBRATION = {"AMRZ": "watch", "COO": "dismiss", "BMI": "dismiss", "OTF": "dismiss"}
CALIBRATION_SCORES = {"AMRZ": "3-2-4-3 = 12/20"}

VERDICTS = {"B": ("watch", "dismiss", "insufficient"), "A": ("intact", "at_risk", "broken", "insufficient")}
SCORE_KEYS = ("asymmetry", "catalyst", "proof", "exit")

HERE = FRAMEWORK = ENV_FILE = REVIEWS = POSITIONS = None


def configure(base):
    """Point every path at one project folder (tests use a temporary one)."""
    global HERE, FRAMEWORK, ENV_FILE, REVIEWS, POSITIONS
    HERE = Path(base).resolve()
    FRAMEWORK = HERE / "Investment_Strategy_Framework.md"
    ENV_FILE = HERE / ".env"
    REVIEWS = HERE / "reviews"
    POSITIONS = HERE / "positions.yaml"


configure(Path(__file__).resolve().parent)


# ------------------------------------------------------------------------------------------------ .env
def read_env():
    out = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def env_value(key):
    return os.environ.get(key) or read_env().get(key)


def save_env_value(key, value):
    """Set one line of .env, keep every other line, and keep the file private (mode 600)."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    lines = [ln for ln in lines if not ln.startswith(key + "=")] + [f'{key}="{value}"']
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(str(ENV_FILE), 0o600)


def cmd_save_key():
    key = getpass.getpass("Paste your Anthropic API key (it will not be shown), then press Enter: ").strip()
    if not key:
        print("Nothing entered; .env was not changed.")
        return 1
    if not key.startswith("sk-ant-"):
        print("That does not look like an Anthropic API key (they start with sk-ant-). .env was not changed.")
        return 1
    had = bool(read_env().get("ANTHROPIC_API_KEY"))
    save_env_value("ANTHROPIC_API_KEY", key)
    print(f"ANTHROPIC_API_KEY {'updated' if had else 'saved'} in {ENV_FILE.name} (private, mode 600). The key was not printed.")
    return 0


def cmd_save_token():
    """Store the long-lived subscription token from `claude setup-token` and switch Tier 2 to the subscription."""
    print("In another Terminal window run:  claude setup-token   , approve it in the browser, and copy the token it prints.")
    tok = getpass.getpass("Paste that token here (it will not be shown), then press Enter: ").strip()
    if not tok:
        print("Nothing entered; .env was not changed.")
        return 1
    if tok.startswith("sk-ant-api"):
        print("That is an API key, not a subscription token (those come from `claude setup-token`). .env was not changed.")
        return 1
    if not tok.startswith("sk-ant-") or len(tok) < 40:
        print("That does not look like a token from `claude setup-token` (they start with sk-ant-). .env was not changed.")
        return 1
    save_env_value("CLAUDE_CODE_OAUTH_TOKEN", tok)
    save_env_value("TIER2_BACKEND", "subscription")
    print("Token saved in .env (private, mode 600; not printed). Tier 2 now uses your Claude subscription, not API credits.\n"
          "Check it works:  python3 sentinel_tier2.py --sub-check\n"
          "To go back to the API at any time:  python3 sentinel_tier2.py --use-api")
    return 0


def cmd_set_backend(name):
    save_env_value("TIER2_BACKEND", name)
    if name == "api":
        print("Tier 2 now uses the API (your prepaid credits). The subscription token stays in .env, unused.\n"
              "To switch back to the subscription:  python3 sentinel_tier2.py --use-subscription")
    else:
        print("Tier 2 now uses your Claude subscription.\nTo switch back to the API:  python3 sentinel_tier2.py --use-api")
    return 0


def backend():
    """'subscription' (Claude Code on your Pro plan, no API credits) or 'api' (the default until you switch)."""
    b = (env_value("TIER2_BACKEND") or "api").strip().lower()
    return "subscription" if b in ("subscription", "sub", "pro") else "api"


def monthly_budget():
    try:
        return float(env_value("TIER2_MONTHLY_BUDGET") or DEFAULT_MONTHLY_BUDGET)
    except ValueError:
        return DEFAULT_MONTHLY_BUDGET


# ------------------------------------------------------------------------------------------------ cost
def cost_usd(model, usage):
    p_in, p_out = PRICES.get(model, UNKNOWN_MODEL_PRICE)
    return round((usage.get("input_tokens", 0) * p_in
                  + usage.get("cache_write_tokens", 0) * p_in * CACHE_WRITE_MULT
                  + usage.get("cache_read_tokens", 0) * p_in * CACHE_READ_MULT
                  + usage.get("output_tokens", 0) * p_out) / 1e6
                 + usage.get("searches", 0) * SEARCH_USD, 4)


def add_usage(total, api_usage):
    api_usage = api_usage or {}
    total["input_tokens"] += api_usage.get("input_tokens") or 0
    total["output_tokens"] += api_usage.get("output_tokens") or 0
    total["cache_write_tokens"] += api_usage.get("cache_creation_input_tokens") or 0
    total["cache_read_tokens"] += api_usage.get("cache_read_input_tokens") or 0
    total["searches"] += (api_usage.get("server_tool_use") or {}).get("web_search_requests") or 0
    return total


def month_start_utc(now=None):
    n = now or datetime.now(timezone.utc)
    return datetime(n.year, n.month, 1, tzinfo=timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------------------------ prompt
SYSTEM_RULES = """You are Tier 2 of Sentinel, a personal stock-research pipeline run by one individual investor.
Tier 1 (mechanical rules, no judgment) flagged ONE name. Your job is to review it by applying the investor's
framework, given below in <framework>, exactly as written. You apply the framework; you never extend,
relax or override it. You advise; the investor decides. You cannot trade or edit any file.

How to work:
1. Research with web search. Prefer primary sources (SEC filings and Form 4s, company investor-relations
   releases, earnings-call transcripts) over secondhand summaries. Every hard date or figure you state must be
   supported by a source you found. If you cannot source it, do not state it. The stored Finviz data you are
   given may be several days old: verify anything that matters.
2. Run the framework's section 1A checks in full, and say plainly which ones you could not complete:
   (a) insiders: count DISTINCT insiders, not transactions, and classify buying/selling as planned (10b5-1)
       or discretionary;
   (b) 12-month price and earnings history plus 2-3 named peers: is the decline a one-off or a pattern
       (repeated guidance cuts, falls after several reports in a row, chronic underperformance vs peers)?
   (c) short-seller or fraud allegations about the reported numbers: credible and unrebutted = disqualified.
3. Apply the section 3 index presumption: a current top-15 QQQ constituent or any S&P 500 member needs a
   real, named, dateable catalyst before it is scored. Say whether the name is a member, if you can verify it.
4. Out-of-scope vehicles (section 6A: business development companies and other credit vehicles) get the
   verdict "dismiss" with the reason out of scope, and no scores.
5. Never predict the outcome of a binary event (a trial, a regulatory decision, a contract award). You may say
   that a dated binary event exists.
6. Score the four section 1 factors, 1-5 each, as proposals: asymmetry, catalyst, proof, exit.

Verdicts:
- Funnel B (a new screen candidate): "watch" (give a specific gate: the one thing to check, and when),
  "dismiss" (give a specific condition that would bring it back), or "insufficient" (you could not complete
  enough of the checks to judge responsibly).
- Funnel A (a name the investor already holds or watches, after a large one-day move or earnings surprise):
  judge it against that name's own stated gate, following section 6B (the gate condition, not the headline;
  not day-one price action alone): "intact", "at_risk", "broken" or "insufficient".

Output format, in this order:
1. A concise review in markdown with these headings: "Why it moved", "1A checks", "Index rule", "Scores",
   "Verdict". Plain English. No investment advice language beyond the framework's own verdicts.
2. Then exactly one JSON object inside <result></result> tags, with these keys:
   {"ticker": str, "verdict": str, "scores": {"asymmetry": int, "catalyst": int, "proof": int, "exit": int} or null,
    "gate": str or null, "gate_date": "YYYY-MM-DD" or null, "readd_condition": str or null,
    "checks": {"insiders": "done"|"partial"|"not_done", "history_peers": "done"|"partial"|"not_done",
               "fraud": "done"|"partial"|"not_done"},
    "index_member": "top15_qqq"|"sp500"|"neither"|"unknown", "out_of_scope": bool, "one_line": str}
Educational analysis only; not investment advice."""


def load_framework():
    if not FRAMEWORK.exists():
        raise FileNotFoundError(f"{FRAMEWORK.name} is not in {HERE}. Tier 2 cannot review anything without your framework.")
    return FRAMEWORK.read_text(encoding="utf-8")


def positions_entry(ticker):
    """The investor's own record for this ticker from positions.yaml, if any (held or watch)."""
    if yaml is None or not POSITIONS.exists():
        return None, None
    raw = yaml.safe_load(POSITIONS.read_text(encoding="utf-8")) or {}
    for kind in ("held", "watch"):
        for p in raw.get(kind) or []:
            if str(p.get("ticker", "")).strip().upper() == ticker.upper():
                return kind, p
    return None, None


def build_user_message(conn, esc, today, include_positions=True):
    """include_positions=False during a calibration, so your own notes (which hold your verdict) are not shown."""
    t = esc["ticker"]
    lines = [f"Today is {today.isoformat()}. Review this Tier 1 escalation.", "",
             f"Escalation #{esc['escalation_id']}: {t}, Funnel {esc['funnel']}, trigger {esc['trigger']}, "
             f"reference {esc['trigger_ref']}, queued {esc['created_at'][:10]}.", "", "Tier 1 reasons:"]
    lines += [f"- {r}" for r in esc["reasons"]]
    if esc.get("evidence"):
        lines += ["", "Tier 1 evidence (raw numbers):", json.dumps(esc["evidence"], ensure_ascii=False, default=str)]
    dos = db.latest_dossier(conn, t)
    if dos:
        d = dos["data"]
        lines += ["", f"Stored Finviz quote page for {t}, fetched {dos['fetched_at'][:10]} (may be stale; verify what matters):",
                  "Snapshot: " + json.dumps(d.get("snapshot", {}), ensure_ascii=False),
                  "Insider trades (as listed, up to 25): " + json.dumps((d.get("insiders") or [])[:25], ensure_ascii=False),
                  "Analyst actions (up to 15): " + json.dumps((d.get("ratings") or [])[:15], ensure_ascii=False),
                  "Recent headlines (up to 15): " + json.dumps([{k: n.get(k) for k in ("date", "headline", "source")}
                                                                for n in (d.get("news") or [])[:15]], ensure_ascii=False),
                  "Peers listed by Finviz: " + json.dumps(d.get("peers") or [], ensure_ascii=False)]
    else:
        lines += ["", f"No stored Finviz page for {t}: research it from scratch."]
    kind, entry = positions_entry(t) if include_positions else (None, None)
    if entry:
        lines += ["", f"The investor's own record for {t} ({kind} list, positions.yaml):",
                  json.dumps(entry, ensure_ascii=False, default=str)]
    lines += ["", "Follow the output format exactly, ending with the <result> JSON."]
    return "\n".join(lines)


def build_request(model, framework, user_message, tool_version):
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": SYSTEM_RULES},
                   {"type": "text", "text": "<framework>\n" + framework + "\n</framework>",
                    "cache_control": {"type": "ephemeral"}}],
        "tools": [search_tool(tool_version)],
        "messages": [{"role": "user", "content": user_message}],
    }


def search_tool(version):
    tool = {"type": version, "name": "web_search", "max_uses": MAX_SEARCHES}
    if version >= "web_search_20260318":
        tool["response_inclusion"] = "excluded"
    return tool


# ------------------------------------------------------------------------------------------------ API
class ApiError(RuntimeError):
    """status: the HTTP code; 0 = the network (worth retrying); -1 = this Mac's setup (retrying cannot help)."""
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}" if status > 0 else message)
        self.status, self.message = status, message


def ssl_context():
    """Trust the same certificate list the scanner already uses (certifi, installed with requests). Python from
    python.org on a Mac has no certificates of its own until 'Install Certificates.command' is run, so the default
    would fail with CERTIFICATE_VERIFY_FAILED."""
    ctx = ssl.create_default_context()           # the system's own list (whatever this Python already trusts)
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())   # plus certifi's, so neither source alone can break it
    except Exception:                  # noqa: BLE001  (certifi missing: the system list alone)
        pass
    return ctx


def http_post(payload, api_key):
    """One Messages API request, STREAMED through Anthropic's official library (pip3 install anthropic).
    Streaming keeps the connection alive while Claude searches, which can take many minutes; the library then
    assembles the complete answer. Replaced by a fake in the tests."""
    try:
        import anthropic
    except ImportError:
        raise ApiError(-1, "Anthropic's Python library is not installed. Fix: python3 -m pip install anthropic") from None
    client = anthropic.Anthropic(api_key=api_key, max_retries=0,
                                 timeout=anthropic.Timeout(STREAM_TOTAL_SECONDS, connect=30.0, read=STREAM_IDLE_SECONDS),
                                 http_client=anthropic.DefaultHttpxClient(verify=ssl_context()))
    try:
        with client.messages.stream(**payload) as stream:
            final = stream.get_final_message()
        return final.model_dump(mode="json", exclude_none=True)
    except anthropic.APITimeoutError:
        raise ApiError(-2, "the request timed out while Claude was still working. Anthropic may already have charged for it: "
                           "check Usage in the Console. It was not retried.") from None
    except anthropic.APIConnectionError as e:
        cause = f"{e.__cause__ or e}"
        if "CERTIFICATE" in cause.upper() or "SSL" in cause.upper():
            raise ApiError(-1, f"this Mac's Python could not verify the secure connection ({cause[:120]}). "
                               "Fix: python3 -m pip install certifi   (or run 'Install Certificates.command' in your Python folder "
                               "under /Applications), then try again.") from None
        raise ApiError(-2, f"the connection to the Anthropic API was lost ({cause[:120]}). If it happened mid-answer, Anthropic may "
                           "already have charged for it: check Usage in the Console. It was not retried.") from None
    except anthropic.APIStatusError as e:
        body = e.body if isinstance(e.body, dict) else {}
        msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else None
        raise ApiError(e.status_code, (msg or str(e))[:500]) from None


def post_with_retries(payload, api_key, sleep=time.sleep):
    """Retries only answers where Anthropic explicitly turned the request away (rate limit, overload), which are
    not charged. A timeout or a dropped connection is never retried: it may already have been paid for."""
    for i, wait in enumerate((0,) + RETRY_WAITS):
        if wait:
            sleep(wait)
        try:
            return http_post(payload, api_key)
        except ApiError as e:
            if e.status not in (429, 500, 502, 503, 529) or i == len(RETRY_WAITS):
                raise


def run_conversation(payload, api_key, sleep=time.sleep):
    """Send the request, follow pause_turn continuations, and return (responses, usage)."""
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0, "searches": 0}
    responses, messages = [], list(payload["messages"])
    for _ in range(MAX_CONTINUATIONS + 1):
        resp = post_with_retries(dict(payload, messages=messages), api_key, sleep)
        responses.append(resp)
        add_usage(usage, resp.get("usage"))
        if resp.get("stop_reason") != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": resp.get("content", [])}]
    return responses, usage


# ------------------------------------------------------------------------------------------------ subscription (Claude Code)
# DECISIONS.md 55: on Oct 18, 2026 Tier 2 moved to the investor's Claude Pro subscription through Claude Code's
# non-interactive mode (`claude -p`), authenticated with the documented long-lived token from `claude setup-token`
# (CLAUDE_CODE_OAUTH_TOKEN). No API credits are spent; reviews draw on the plan's usage limits, shared with chat.
SUB_MAX_TURNS = 30                             # agent turns per review (each web search is about one)
SUB_SEARCH_NOTE = (f"\n\nUse at most {MAX_SEARCHES} web searches for this review. End the review with a 'Sources' list of the "
                   "URLs you relied on, then the <result> block.")
CLAUDE_DIRS = ("~/.local/bin", "~/.claude/local", "/opt/homebrew/bin", "/usr/local/bin")


def claude_bin():
    """Where the `claude` command is. Scheduled jobs do not see your shell's PATH, so the usual install places are tried."""
    import shutil
    if env_value("CLAUDE_BIN"):
        return env_value("CLAUDE_BIN")
    path = os.pathsep.join([os.environ.get("PATH", "")] + [os.path.expanduser(d) for d in CLAUDE_DIRS])
    return shutil.which("claude", path=path)


def subscription_env(token):
    """The environment for `claude -p`: an API key in the environment would WIN over the subscription and bill the API
    (Claude Code's documented precedence), so every API credential is removed. A private config folder keeps your own
    Claude Code memory, hooks and settings out of the review, and theirs out of yours."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_PROFILE")
           and not k.startswith("CLAUDE_CODE_USE_")}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env["CLAUDE_CONFIG_DIR"] = str(HERE / ".claude_tier2")
    env["PATH"] = os.pathsep.join([env.get("PATH", "")] + [os.path.expanduser(d) for d in CLAUDE_DIRS])
    return env


def subscription_run(model, system_text, prompt, token, tools="WebSearch", max_turns=SUB_MAX_TURNS, timeout=STREAM_TOTAL_SECONDS):
    """One `claude -p` run. Returns the parsed JSON result. Raises ApiError: -1 setup, -2 timeout, -3 plan usage limit.
    Replaced by a fake in the tests."""
    import subprocess, tempfile
    exe = claude_bin()
    if not exe:
        raise ApiError(-1, "the `claude` command (Claude Code) was not found. Install it from https://code.claude.com, "
                           "or put its full path in .env as CLAUDE_BIN=...")
    (HERE / ".claude_tier2").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as work:        # an empty folder: no project files or settings get read
        sysfile = Path(work) / "system.txt"
        sysfile.write_text(system_text, encoding="utf-8")
        cmd = [exe, "-p", "--output-format", "stream-json", "--verbose", "--model", model, "--system-prompt-file", str(sysfile),
               "--tools", tools, "--permission-mode", "dontAsk", "--max-turns", str(max_turns),
               "--no-session-persistence", "--strict-mcp-config"]
        if tools:
            cmd += ["--allowedTools", tools]
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=work, env=subscription_env(token), timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ApiError(-2, f"Claude Code did not finish within {int(timeout)}s and was stopped. No money was spent; "
                               "it used part of your plan's limits. It was not retried.") from None
        except OSError as e:
            raise ApiError(-1, f"could not start Claude Code ({e})") from None
    d = parse_stream(p.stdout)
    if not isinstance(d, dict):
        raise ApiError(-1, f"Claude Code gave no readable answer (exit {p.returncode}): {(p.stderr or p.stdout)[-300:].strip()}")
    if d.get("is_error") or d.get("subtype") not in (None, "success"):
        msg = str(d.get("result") or d.get("subtype") or "unknown error")[:300]
        low = msg.lower()
        if any(w in low for w in ("usage limit", "session limit", "rate limit", "limit reached", "hit your", "out of extra usage")):
            raise ApiError(-3, f"your Claude plan's usage limit was reached ({msg}). The rest wait for the next run.")
        if any(w in low for w in ("login", "oauth", "token", "authenticat", "401", "403", "not logged")):
            raise ApiError(-1, f"Claude Code could not sign in with the saved token ({msg}). "
                               "Fix: claude setup-token, then python3 sentinel_tier2.py --save-token")
        if d.get("subtype") == "error_max_turns":
            return d                    # it ran out of turns; the text it has is still parsed (it may lack <result>)
        raise ApiError(-1, f"Claude Code reported an error: {msg}")
    return d


def parse_stream(stdout):
    """`claude -p --output-format stream-json` prints one JSON event per line; the last one, type "result", carries the
    answer. Web searches are counted from the assistant's WebSearch tool calls, because the result's usage figures do
    not include the searches Claude Code's WebSearch tool makes (Oct 18 calibration showed '0 searches' with 4-8 sources)."""
    result, searches, last = None, 0, None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        last = ev
        if ev.get("type") == "result":
            result = ev
        elif ev.get("type") == "assistant" and not ev.get("parent_tool_use_id"):
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "WebSearch":
                    searches += 1
    d = result or last
    if isinstance(d, dict):
        d["_searches"] = searches
    return d


def subscription_review(model, framework, user_message, token, run=None):
    """Same rules, framework and message as the API path. Returns (text, usage). usage counts tokens for information;
    nothing is charged."""
    run = run or subscription_run
    system_text = SYSTEM_RULES + SUB_SEARCH_NOTE + "\n\n<framework>\n" + framework + "\n</framework>"
    d = run(model, system_text, user_message, token)
    usage = add_usage({"input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0, "searches": 0},
                      d.get("usage"))
    usage["searches"] = max(usage["searches"], int(d.get("_searches") or 0))
    usage["api_equivalent_usd"] = round(float(d.get("total_cost_usd") or 0), 4)
    return str(d.get("result") or ""), usage


_URL = re.compile(r"https?://[^\s)\]>\"']+")


def sources_from_text(text):
    seen, out = set(), []
    for u in _URL.findall(text or ""):
        u = u.rstrip(".,;")
        if u not in seen:
            seen.add(u)
            out.append({"url": u, "title": None})
    return out


def cmd_sub_check():
    """A tiny run with no tools, to prove the scheduled job can reach your subscription. Uses a sliver of your limits."""
    token = env_value("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        print("No CLAUDE_CODE_OAUTH_TOKEN found. Run: python3 sentinel_tier2.py --save-token")
        return 2
    print(f"Claude Code: {claude_bin() or 'NOT FOUND'}")
    try:
        d = subscription_run(MODEL, "Reply with exactly the word OK.", "Say OK.", token, tools="", max_turns=1, timeout=180)
    except ApiError as e:
        print(f"FAILED: {e}")
        return 1
    print(f"Answer: {str(d.get('result', '')).strip()[:40]!r}. Subscription works; no API credits were used. Tier 2 backend: {backend()}.")
    return 0


def review_text_and_sources(responses):
    """All text Claude wrote, in order, and the sources it cited (deduplicated, in order of first use)."""
    parts, sources, seen = [], [], set()
    for resp in responses:
        for block in resp.get("content", []):
            if block.get("type") != "text":
                continue
            parts.append(block.get("text", ""))
            for c in block.get("citations") or []:
                url = c.get("url")
                if url and url not in seen:
                    seen.add(url)
                    sources.append({"url": url, "title": c.get("title")})
    return "".join(parts), sources


_RESULT = re.compile(r"<result>\s*(.*?)\s*</result>", re.DOTALL)


def parse_result(text, funnel):
    """(result dict, error). The LAST <result> block is used."""
    found = _RESULT.findall(text or "")
    if not found:
        return None, "the answer had no <result> block"
    raw = found[-1].strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        res = json.loads(raw)
    except ValueError as e:
        return None, f"the <result> block is not valid JSON ({e})"
    if not isinstance(res, dict):
        return None, "the <result> block is not a JSON object"
    verdict = str(res.get("verdict", "")).strip().lower()
    allowed = VERDICTS.get(funnel, VERDICTS["B"])
    if verdict not in allowed:
        return None, f"verdict {verdict!r} is not one of {', '.join(allowed)} (Funnel {funnel})"
    res["verdict"] = verdict
    scores = res.get("scores")
    if scores is not None:
        if not isinstance(scores, dict) or any(not isinstance(scores.get(k), int) or not 1 <= scores[k] <= 5 for k in SCORE_KEYS):
            return None, f"scores must be four integers 1-5 ({', '.join(SCORE_KEYS)}) or null, got {scores!r}"
        res["total"] = sum(scores[k] for k in SCORE_KEYS)
    else:
        res["total"] = None
    return res, None


def score_line(res):
    s = (res or {}).get("scores")
    return "no scores" if not s else "-".join(str(s[k]) for k in SCORE_KEYS) + f" = {res['total']}/20"


def strip_result(text):
    return _RESULT.sub("", text or "").strip()


# ------------------------------------------------------------------------------------------------ one review
def review_one(conn, esc, *, mode, batch, model, api_key, framework, today, expected=None, sleep=time.sleep, sub_token=None):
    """Review one escalation, store it, and return the stored row as a dict. With sub_token, through the subscription."""
    user_message = build_user_message(conn, esc, today, include_positions=(mode != "calibration"))
    if sub_token:
        text, usage = subscription_review(model, framework, user_message, sub_token)
        result, error = parse_result(text, esc["funnel"])
        sources = sources_from_text(text)
        if result is not None:
            result["sources"] = sources
        rid = db.insert_tier2_review(conn, escalation_id=esc["escalation_id"], ticker=esc["ticker"], mode=mode, batch=batch,
                                     model=model + " (subscription)", ok=result is not None, verdict=result["verdict"] if result else None,
                                     expected=expected, result=result, report_md=strip_result(text) or None, error=error,
                                     usage=usage, cost_usd=0.0)
        return dict(review_id=rid, escalation_id=esc["escalation_id"], ticker=esc["ticker"], funnel=esc["funnel"], ok=result is not None,
                    verdict=result["verdict"] if result else None, expected=expected, result=result, report=strip_result(text),
                    error=error, usage=usage, cost=0.0, sources=sources)
    responses, usage, error, last_exc = [], None, None, None
    for version in SEARCH_TOOL_VERSIONS:
        try:
            responses, usage = run_conversation(build_request(model, framework, user_message, version), api_key, sleep)
            break
        except ApiError as e:
            last_exc = e
            m = e.message.lower()
            if e.status == 400 and "not enabled" in m and "web search" in m:
                raise ApiError(400, "web search is disabled for your Anthropic organization. Turn it on in the Console "
                                    "(Settings > Privacy), then run again.") from None
            if e.status == 400 and version != SEARCH_TOOL_VERSIONS[-1] and any(w in m for w in ("web_search", "allowed_callers", "code_execution", "tool")):
                continue                 # this account or model does not accept the newer tool version; try the older one
            raise
    else:
        raise last_exc
    text, sources = review_text_and_sources(responses)
    result, error = parse_result(text, esc["funnel"])
    if result is not None:
        result["sources"] = sources
    cost = cost_usd(model, usage)
    rid = db.insert_tier2_review(conn, escalation_id=esc["escalation_id"], ticker=esc["ticker"], mode=mode, batch=batch,
                                 model=model, ok=result is not None, verdict=result["verdict"] if result else None,
                                 expected=expected, result=result, report_md=strip_result(text) or None, error=error,
                                 usage=usage, cost_usd=cost)
    return dict(review_id=rid, escalation_id=esc["escalation_id"], ticker=esc["ticker"], funnel=esc["funnel"], ok=result is not None,
                verdict=result["verdict"] if result else None, expected=expected, result=result, report=strip_result(text),
                error=error, usage=usage, cost=cost, sources=sources)


# ------------------------------------------------------------------------------------------------ report file
def write_report(rows, mode, today):
    REVIEWS.mkdir(parents=True, exist_ok=True)
    path = REVIEWS / f"{today.isoformat()}_tier2.md"
    new = not path.exists()
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write(f"# Tier 2 reviews, {today.isoformat()}\n\nAdvice only: you decide, then close each alert with "
                    "`python3 sentinel_tier1.py --mark ID done|dismissed`. Educational analysis, not investment advice.\n")
        for r in rows:
            head = f"{r['ticker']}: {r['verdict'] or 'NO VERDICT'}" + (f" ({score_line(r['result'])})" if r["ok"] else "")
            f.write(f"\n---\n\n## {head}\n\nEscalation #{r['escalation_id']} (Funnel {r['funnel']}), {mode} run"
                    + (f", your own verdict: {r['expected']}" if r.get("expected") else "") + "\n\n")
            if r["ok"]:
                res = r["result"]
                for label, key in (("Gate", "gate"), ("Gate date", "gate_date"), ("Re-add condition", "readd_condition"), ("In one line", "one_line")):
                    if res.get(key):
                        f.write(f"- **{label}:** {res[key]}\n")
                ch = res.get("checks") or {}
                f.write(f"- **1A checks:** insiders {ch.get('insiders', '?')}, history and peers {ch.get('history_peers', '?')}, "
                        f"fraud {ch.get('fraud', '?')}; index member: {res.get('index_member', '?')}\n\n")
            else:
                f.write(f"**No usable verdict:** {r['error']}\n\n")
            f.write((r["report"] or "(no text)") + "\n\n")
            if r["sources"] and "api_equivalent_usd" not in r["usage"]:     # on the subscription the review lists its own sources
                f.write("Sources:\n" + "".join(f"- {s.get('title') or s['url']}: {s['url']}\n" for s in r["sources"]) + "\n")
            u = r["usage"]
            toks = f"{u['input_tokens'] + u['cache_write_tokens'] + u['cache_read_tokens']:,} input and {u['output_tokens']:,} output tokens"
            if "api_equivalent_usd" in u:
                f.write(f"_Cost: $0 (your Claude subscription; {u['searches']} searches, {toks}; about "
                        f"${u['api_equivalent_usd']:.2f} if it had been billed to the API)._\n")
            else:
                f.write(f"_Cost: ${r['cost']:.2f} ({u['searches']} searches, {toks})._\n")
    return path


# ------------------------------------------------------------------------------------------------ selection
def calibration_escalations(conn):
    """The most recent candidate escalation for each calibration ticker, whatever its status."""
    out, missing = [], []
    rows = db.list_escalations(conn)
    for t in CALIBRATION:
        mine = [e for e in rows if e["ticker"] == t]
        (out.append(mine[-1]) if mine else missing.append(t))
    return out, missing


def calibration_status(conn):
    """(passed, detail) for the most recent calibration batch."""
    rows = db.list_tier2_reviews(conn, mode="calibration")
    if not rows:
        return False, "no calibration has been run yet"
    batch = rows[0]["batch"]
    mine = [r for r in rows if r["batch"] == batch]
    got = {r["ticker"]: r for r in mine}
    wrong = [t for t, want in CALIBRATION.items() if t not in got or not got[t]["ok"] or got[t]["verdict"] != want]
    when = mine[-1]["created_at"][:10]
    if wrong:
        return False, f"the calibration of {when} did not match on: {', '.join(wrong)}"
    return True, f"the calibration of {when} matched on all {len(CALIBRATION)} names"


# ------------------------------------------------------------------------------------------------ commands
def cmd_show(conn):
    rows = db.list_tier2_reviews(conn, limit=15)
    spent = db.tier2_cost_since(conn, month_start_utc())
    budget = monthly_budget()
    passed, detail = calibration_status(conn)
    print(f"Tier 2: ${spent:.2f} spent this month of a ${budget:.2f} budget. Calibration: {'PASSED' if passed else 'not passed'} ({detail}).\n")
    if not rows:
        print("No reviews yet.")
        return 0
    print("Recent reviews, newest first:")
    for r in rows:
        exp = f"  (yours: {r['expected']})" if r["expected"] else ""
        print(f"  {r['created_at'][:10]}  {r['ticker']:<6} #{r['escalation_id'] or '-':<4} {r['mode']:<11} "
              f"{(r['verdict'] or 'NO VERDICT'):<12} {score_line(r['result']) if r['ok'] else r['error'][:60]}{exp}  ${r['cost_usd']:.2f}")
    print("\nFull text: reviews/<date>_tier2.md")
    return 0


def cmd_review(conn, escs, *, mode, model, dry_run, today, sleep=time.sleep):
    if not escs:
        print("Nothing to review.")
        return 0
    framework = load_framework()
    if dry_run:
        REVIEWS.mkdir(parents=True, exist_ok=True)
        path = REVIEWS / f"dry_run_{today.isoformat()}.md"
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# Tier 2 dry run, {today.isoformat()}: exactly what would be sent (besides the rules and your framework)\n\n")
            for e in escs:
                f.write(f"## {e['ticker']} (escalation #{e['escalation_id']})\n\n```\n{build_user_message(conn, e, today)}\n```\n\n")
        chars = len(SYSTEM_RULES) + len(framework)
        sub = backend() == "subscription"
        print(f"Dry run: {len(escs)} review(s) would be sent to {model} through "
              + ("your Claude subscription (Claude Code)" if sub else "the API") + "; nothing was called and nothing was spent.")
        print(f"  Names: {', '.join(e['ticker'] for e in escs)}")
        print(f"  Rules + framework: about {chars // 4:,} tokens per review.")
        print("  Cost: $0, it uses your plan's usage limits (shared with your chat)." if sub else
              f"  Estimated cost: about ${EST_COST_PER_REVIEW * len(escs):.2f} at most (up to {MAX_SEARCHES} web searches per name).")
        print(f"  What each review would receive: {path.relative_to(HERE)}")
        return 0
    sub_token, api_key = None, None
    if backend() == "subscription":
        sub_token = env_value("CLAUDE_CODE_OAUTH_TOKEN")
        if not sub_token:
            print("No CLAUDE_CODE_OAUTH_TOKEN found. Run: python3 sentinel_tier2.py --save-token   "
                  "(or go back to the API: python3 sentinel_tier2.py --use-api)")
            return 2
        print("Tier 2 through your Claude subscription: no API credits are used.")
    else:
        api_key = env_value("ANTHROPIC_API_KEY")
        if not api_key:
            print("No ANTHROPIC_API_KEY found. Run: python3 sentinel_tier2.py --save-key")
            return 2
    budget, batch = monthly_budget(), datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    done = []
    for e in escs[:MAX_PER_RUN]:
        spent = db.tier2_cost_since(conn, month_start_utc())
        if not sub_token and spent + EST_COST_PER_REVIEW > budget:
            print(f"Stopped before {e['ticker']}: ${spent:.2f} spent this month, budget ${budget:.2f} "
                  "(raise TIER2_MONTHLY_BUDGET in .env if you mean to).")
            break
        print(f"Reviewing {e['ticker']} (escalation #{e['escalation_id']}) ...", flush=True)
        try:
            row = review_one(conn, e, mode=mode, batch=batch, model=model, api_key=api_key, framework=framework, today=today,
                             expected=CALIBRATION.get(e["ticker"]) if mode == "calibration" else None, sleep=sleep, sub_token=sub_token)
        except ApiError as err:
            print(f"  FAILED: {err}")
            if err.status == -3:
                print("  Stopping here: the remaining names stay open and are reviewed on the next run.")
                break                   # a usage limit is not this name's failure: record nothing, so it is retried later
            db.insert_tier2_review(conn, escalation_id=e["escalation_id"], ticker=e["ticker"], mode=mode, batch=batch, model=model,
                                   ok=False, error=str(err)[:500])
            if err.status in (-2, -1, 400, 401, 403):
                print("  Stopping here, so the same problem cannot repeat (and cost) for every remaining name.")
                break
            continue
        except Exception as err:        # noqa: BLE001  (anything unexpected: record it, stop, never crash mid-batch)
            print(f"  FAILED unexpectedly: {type(err).__name__}: {err}")
            db.insert_tier2_review(conn, escalation_id=e["escalation_id"], ticker=e["ticker"], mode=mode, batch=batch, model=model,
                                   ok=False, error=f"{type(err).__name__}: {err}"[:500])
            print("  Stopping here. Anthropic may have charged for this attempt: check Usage in the Console.")
            break
        done.append(row)
        print(f"  {row['verdict'] or 'NO VERDICT'}" + (f", {score_line(row['result'])}" if row["ok"] else f": {row['error']}")
              + f"   (${row['cost']:.2f}, {row['usage']['searches']} searches)")
    if len(escs) > MAX_PER_RUN:
        print(f"{len(escs) - MAX_PER_RUN} more waiting: at most {MAX_PER_RUN} per run. Run again to continue.")
    if done:
        path = write_report(done, mode, today)
        print(f"\nWritten to {path.relative_to(HERE)}  (total ${sum(r['cost'] for r in done):.2f})")
    if mode == "calibration":
        passed, detail = calibration_status(conn)
        print("\nCalibration " + ("PASSED" if passed else "NOT passed") + f": {detail}.")
        for r in done:
            mark = "match" if r["verdict"] == r["expected"] else "DIFFERENT"
            yours = f", your scores {CALIBRATION_SCORES[r['ticker']]}" if r["ticker"] in CALIBRATION_SCORES else ""
            print(f"  {r['ticker']:<5} Tier 2: {r['verdict'] or 'none':<12} yours: {r['expected']:<8} {mark}"
                  + (f"   (Tier 2 scores {score_line(r['result'])}{yours})" if r["ok"] else ""))
        if not passed:
            print("Read the differing reviews: if the reasoning is sound, the difference may be new information since Sep 26. "
                  "Live runs stay refused until a calibration matches (or you use --force deliberately).")
    return 0 if done and all(r["ok"] for r in done) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save-key", action="store_true", help="store ANTHROPIC_API_KEY in .env (hidden prompt)")
    ap.add_argument("--calibrate", action="store_true", help="review the known-answer set and compare with your verdicts")
    ap.add_argument("--id", type=int, default=None, help="review one escalation by id, whatever its status")
    ap.add_argument("--show", action="store_true", help="recent reviews and this month's spending")
    ap.add_argument("--dry-run", action="store_true", help="show what would be sent and the cost estimate; call nothing")
    ap.add_argument("--force", action="store_true", help="allow a live run without a passed calibration")
    ap.add_argument("--model", default=MODEL, help=f"model (default {MODEL})")
    ap.add_argument("--db", default=None, help="database file (default: $RESEARCH_DB or research.db)")
    ap.add_argument("--save-token", action="store_true", help="store the token from `claude setup-token` and use your subscription")
    ap.add_argument("--sub-check", action="store_true", help="prove the subscription works with a tiny run (no tools)")
    ap.add_argument("--use-api", action="store_true", help="switch Tier 2 to the API (prepaid credits)")
    ap.add_argument("--use-subscription", action="store_true", help="switch Tier 2 to your Claude subscription")
    args = ap.parse_args(argv)

    if args.save_key:
        return cmd_save_key()
    if args.save_token:
        return cmd_save_token()
    if args.use_api:
        return cmd_set_backend("api")
    if args.use_subscription:
        if not env_value("CLAUDE_CODE_OAUTH_TOKEN"):
            print("No subscription token saved yet. Run: python3 sentinel_tier2.py --save-token")
            return 2
        return cmd_set_backend("subscription")
    if args.sub_check:
        return cmd_sub_check()
    today = date.today()
    conn = None
    try:
        conn = db.connect(args.db, create=False)
        if args.dry_run or args.show:
            if db.current_version(conn) < db.load_migrations()[-1][0]:
                print("research.db has not been upgraded for Tier 2 yet. Run: python3 sentinel_db.py --init")
                return 2
        else:
            db.init_schema(conn)
        if args.show:
            return cmd_show(conn)
        if args.calibrate:
            escs, missing = calibration_escalations(conn)
            if missing:
                print(f"Not in the escalation history, so they cannot be calibrated: {', '.join(missing)}")
            return cmd_review(conn, escs, mode="calibration", model=args.model, dry_run=args.dry_run, today=today)
        if args.id is not None:
            esc = next((e for e in db.list_escalations(conn) if e["escalation_id"] == args.id), None)
            if esc is None:
                print(f"No escalation with id {args.id}.")
                return 2
            return cmd_review(conn, [esc], mode="manual", model=args.model, dry_run=args.dry_run, today=today)
        passed, detail = calibration_status(conn)
        if not passed and not args.force and not args.dry_run:
            print(f"Live review refused: {detail}. Run  python3 sentinel_tier2.py --calibrate  first.")
            return 2
        reviewed = db.reviewed_escalation_ids(conn)
        escs = [e for e in db.list_escalations(conn, "open") if e["escalation_id"] not in reviewed]
        return cmd_review(conn, escs, mode="live", model=args.model, dry_run=args.dry_run, today=today)
    except (FileNotFoundError, db.SchemaError, ApiError) as e:
        print(f"sentinel_tier2.py: {e}")
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())

"""Phase 7 tests: Tier 2 reviews. Uses a fake Anthropic API, so nothing is called and nothing is spent."""
import contextlib, hashlib, io, json, os, re, stat, sys, tempfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sentinel_db as db, sentinel_tier2 as t2
_orig_post = t2.http_post

results = []
def check(label, ok, detail=""):
    ok = bool(ok); results.append(ok); print(("  ok    " if ok else "  FAIL  ") + label + ("" if ok else f"  {detail}"))

TODAY = date(2026, 9, 28)
FRAMEWORK_TEXT = "# Investment Strategy - Framework (test copy)\n## 1. Shared scorecard\nAsymmetry, catalyst, proof, exit.\n"
POSITIONS_TEXT = """as_of: 2026-08-28
held:
  - ticker: LBRT
    name: Liberty Energy
    avg_cost: 19.5066
    gate: "Thesis check: sector-wide weakness only."
watch:
  - ticker: PSN
    name: Parsons
    gate: "Hard gate Q3."
"""


def result_block(ticker, verdict, scores=(3, 2, 4, 3), extra=None):
    res = {"ticker": ticker, "verdict": verdict, "scores": dict(zip(t2.SCORE_KEYS, scores)) if scores else None,
           "gate": "Q3 print" if verdict == "watch" else None, "gate_date": "2026-10-27" if verdict == "watch" else None,
           "readd_condition": "two quarters of growth" if verdict == "dismiss" else None,
           "checks": {"insiders": "done", "history_peers": "done", "fraud": "done"}, "index_member": "neither",
           "out_of_scope": False, "one_line": f"{ticker} one line"}
    res.update(extra or {})
    return f"<result>{json.dumps(res)}</result>"


def response(text, stop="end_turn", inp=3000, out=1500, cw=0, cr=0, searches=4, url="https://example.com/a"):
    return {"content": [{"type": "text", "text": text, "citations": [{"url": url, "title": "Source A"}]}],
            "stop_reason": stop, "usage": {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cw,
                                           "cache_read_input_tokens": cr, "server_tool_use": {"web_search_requests": searches}}}


class FakeApi:
    """Answers by ticker. verdicts: {ticker: verdict}; errors: list of ApiError to raise first, in order."""
    def __init__(self, verdicts=None, errors=None, pause_first=False):
        self.verdicts, self.errors, self.pause_first = verdicts or {}, list(errors or []), pause_first
        self.calls = []

    def __call__(self, payload, api_key):
        self.calls.append(json.loads(json.dumps(payload)))
        if self.errors:
            raise self.errors.pop(0)
        msg = payload["messages"][0]["content"]
        ticker = re.search(r"Escalation #\d+: (\S+),", msg).group(1)
        if self.pause_first and len(payload["messages"]) == 1:
            return response("## Why it moved\nHalf way. ", stop="pause_turn", searches=2)
        verdict = self.verdicts.get(ticker, "watch")
        scores = None if verdict == "dismiss" else (3, 2, 4, 3)
        return response(f"## Why it moved\nReview of {ticker}.\n## Verdict\n{verdict}\n" + result_block(ticker, verdict, scores))


@contextlib.contextmanager
def project():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / "Investment_Strategy_Framework.md").write_text(FRAMEWORK_TEXT, encoding="utf-8")
        (base / "positions.yaml").write_text(POSITIONS_TEXT, encoding="utf-8")
        (base / ".env").write_text('FMP_API_KEY="fmp-test"\nANTHROPIC_API_KEY="sk-ant-test"\n', encoding="utf-8")
        t2.configure(base)
        dbp = base / "research.db"
        conn = db.connect(dbp)
        db.init_schema(conn)
        ids = {}
        for tk in ("AMRZ", "BMI", "OTF", "COO"):
            ids[tk], _ = db.insert_escalation(conn, ticker=tk, funnel="B", trigger="candidate", trigger_ref="2026-09-20",
                                              reasons=[f"Dislocation: {tk} down", "Conviction: insider buys"], evidence={"from_high_pct": -40})
            db.set_escalation_status(conn, ids[tk], "done" if tk in ("AMRZ", "COO") else "dismissed")
        db.insert_dossier(conn, ticker="AMRZ", data={"snapshot": {"Price": "37.54", "52W High": "65.94 -43.07%"},
                                                     "insiders": [{"insider_trading": "Jenisch Jan Philipp", "transaction": "Buy", "date": "Sep 02 '26"}],
                                                     "ratings": [{"date": "Sep-09-26", "action": "Downgrade", "analyst": "Wells Fargo"}],
                                                     "news": [{"date": "Sep-10-26", "headline": "Cement costs rise", "source": "X"}], "peers": ["VMC", "MLM"]},
                          parse_ok=True)
        old = t2.http_post
        try:
            yield base, dbp, conn, ids
        finally:
            t2.http_post = old
            conn.close()


def run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = t2.main(argv)
    return code, buf.getvalue()


def sha(p):
    return hashlib.sha1(Path(p).read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------------------ schema + db
print("Database")
with project() as (base, dbp, conn, ids):
    check("the database migrates to version 7 with a tier2_reviews table",
          db.current_version(conn) == 7 and "tier2_reviews" in db.table_counts(conn))
    rid = db.insert_tier2_review(conn, escalation_id=ids["AMRZ"], ticker="amrz", mode="manual", batch="b1", model="m", ok=True,
                                 verdict="watch", result={"total": 12}, usage={"input_tokens": 10, "searches": 2}, cost_usd=0.25,
                                 created_at="2026-09-10T00:00:00+00:00")
    db.insert_tier2_review(conn, escalation_id=ids["BMI"], ticker="BMI", mode="manual", batch="b1", model="m", ok=False, error="x",
                           cost_usd=0.10, created_at="2026-08-31T23:00:00+00:00")
    rows = db.list_tier2_reviews(conn)
    check("reviews round-trip, newest first, ticker normalised, result parsed", rows[0]["review_id"] == rid + 1 and rows[1]["ticker"] == "AMRZ"
          and rows[1]["result"] == {"total": 12} and rows[1]["searches"] == 2)
    check("only SUCCESSFUL reviews count as already reviewed", db.reviewed_escalation_ids(conn) == {ids["AMRZ"]})
    check("month-to-date cost counts only this month", abs(db.tier2_cost_since(conn, "2026-09-01T00:00:00+00:00") - 0.25) < 1e-9)
    check("month start is the first of the month, UTC", t2.month_start_utc(__import__("datetime").datetime(2026, 9, 28, 5, tzinfo=__import__("datetime").timezone.utc))
          == "2026-09-01T00:00:00+00:00")

# ------------------------------------------------------------------------------------------------ parsing
print("Reading the answer")
r, e = t2.parse_result("text " + result_block("AMRZ", "watch"), "B")
check("a valid Funnel B verdict is read, with the total computed", e is None and r["verdict"] == "watch" and r["total"] == 12, e)
r, e = t2.parse_result(result_block("X", "watch", extra={"verdict": "WATCH"}), "B")
check("the verdict is case-insensitive", e is None and r["verdict"] == "watch", e)
r, e = t2.parse_result(result_block("X", "dismiss", scores=None), "B")
check("a dismiss with no scores is valid (e.g. out of scope)", e is None and r["total"] is None, e)
check("a verdict outside the funnel's vocabulary is rejected", t2.parse_result(result_block("X", "intact"), "B")[0] is None)
check("Funnel A uses intact / at_risk / broken / insufficient", t2.parse_result(result_block("X", "at_risk"), "A")[0] is not None
      and t2.parse_result(result_block("X", "watch"), "A")[0] is None)
check("an answer with no <result> block is reported, not guessed", t2.parse_result("no block here", "B") == (None, "the answer had no <result> block"))
check("broken JSON is reported", "not valid JSON" in t2.parse_result("<result>{nope</result>", "B")[1])
check("scores outside 1-5 are rejected", t2.parse_result(result_block("X", "watch", scores=(3, 2, 9, 3)), "B")[0] is None)
check("a JSON code fence inside the block is tolerated", t2.parse_result("<result>```json\n" + result_block("X", "watch")[8:-9] + "\n```</result>", "B")[0] is not None)
r, _ = t2.parse_result(result_block("X", "dismiss", scores=None) + " later " + result_block("X", "watch"), "B")
check("the LAST <result> block wins", r["verdict"] == "watch")
check("the score line reads like the ledger", t2.score_line(t2.parse_result(result_block("X", "watch"), "B")[0]) == "3-2-4-3 = 12/20")

# ------------------------------------------------------------------------------------------------ cost
print("Cost")
u = {"input_tokens": 100_000, "output_tokens": 5_000, "cache_write_tokens": 20_000, "cache_read_tokens": 50_000, "searches": 8}
check("cost follows the published Sonnet 5 prices (0.20 + 0.05 + 0.01 + 0.05 + 0.08 = $0.39)", t2.cost_usd("claude-sonnet-5", u) == 0.39, t2.cost_usd("claude-sonnet-5", u))
check("an unknown model is priced pessimistically, never as free", t2.cost_usd("claude-future-9", u) > t2.cost_usd("claude-sonnet-5", u))
tot = t2.add_usage({"input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0, "searches": 0},
                   response("x", inp=5, out=6, cw=7, cr=8, searches=3)["usage"])
check("API usage fields map onto the stored counts", tot == {"input_tokens": 5, "output_tokens": 6, "cache_write_tokens": 7, "cache_read_tokens": 8, "searches": 3})

# ------------------------------------------------------------------------------------------------ request
print("What is sent")
with project() as (base, dbp, conn, ids):
    esc = next(e for e in db.list_escalations(conn) if e["ticker"] == "AMRZ")
    msg = t2.build_user_message(conn, esc, TODAY)
    check("the message carries today's date, the Tier 1 reasons and the stored Finviz page",
          "Today is 2026-09-28" in msg and "Dislocation: AMRZ down" in msg and "Jenisch Jan Philipp" in msg and "VMC" in msg)
    check("a new candidate gets no positions.yaml record", "positions.yaml" not in msg)
    db.insert_escalation(conn, ticker="LBRT", funnel="A", trigger="price_move", trigger_ref="2026-09-25", reasons=["-16% in a day"])
    lb = next(e for e in db.list_escalations(conn) if e["ticker"] == "LBRT")
    mlb = t2.build_user_message(conn, lb, TODAY)
    check("a held name carries its own positions.yaml record (gate included)", "held list" in mlb and "sector-wide weakness only" in mlb)
    check("a name with no stored page is researched from scratch", "No stored Finviz page for LBRT" in mlb)
    check("during a calibration your positions.yaml notes are NOT shown (they contain your verdict)",
          "positions.yaml" not in t2.build_user_message(conn, lb, TODAY, include_positions=False))
    req = t2.build_request("claude-sonnet-5", FRAMEWORK_TEXT, msg, "web_search_20260318")
    check("the framework is in the system prompt and cached", req["system"][1]["cache_control"] == {"type": "ephemeral"}
          and FRAMEWORK_TEXT in req["system"][1]["text"])
    check("the rules come first and carry no date, so the cache is reused across days", "Today is" not in req["system"][0]["text"])
    check("web search is capped per review", req["tools"][0]["max_uses"] == t2.MAX_SEARCHES and req["tools"][0]["name"] == "web_search")
    check("the newer tool version drops raw search blocks from the answer; the older one has no such field",
          req["tools"][0].get("response_inclusion") == "excluded" and "response_inclusion" not in t2.search_tool("web_search_20250305"))
    check("the rules forbid binary predictions and require distinct-insider counting and sources",
          "Never predict the outcome of a binary event" in t2.SYSTEM_RULES and "DISTINCT insiders" in t2.SYSTEM_RULES and "source" in t2.SYSTEM_RULES)

# ------------------------------------------------------------------------------------------------ conversation mechanics
print("API mechanics")
fake = FakeApi(pause_first=True)
t2.http_post = fake
payload = {"model": "m", "messages": [{"role": "user", "content": "Escalation #1: AMRZ, Funnel B"}]}
resps, usage = t2.run_conversation(payload, "k", sleep=lambda s: None)
check("a paused turn is continued by sending the paused answer back", len(resps) == 2 and len(fake.calls[1]["messages"]) == 2
      and fake.calls[1]["messages"][1]["role"] == "assistant")
check("usage is added up across the continuation", usage["searches"] == 6 and usage["input_tokens"] == 6000)
text, sources = t2.review_text_and_sources(resps)
check("the text of both parts is joined and cited sources are collected once", text.startswith("## Why it moved\nHalf way. ## Why it moved")
      and sources == [{"url": "https://example.com/a", "title": "Source A"}])

waits = []
t2.http_post = FakeApi(errors=[t2.ApiError(529, "overloaded")])
t2.post_with_retries({"messages": [{"role": "user", "content": "Escalation #1: AMRZ,"}]}, "k", sleep=waits.append)
check("an overload is retried after a wait", waits == [10])
fk = FakeApi(errors=[t2.ApiError(401, "invalid x-api-key")])
t2.http_post = fk
try:
    t2.post_with_retries({"messages": []}, "k", sleep=waits.append); ok401 = False
except t2.ApiError as e:
    ok401 = e.status == 401 and len(fk.calls) == 1
check("a bad key is NOT retried", ok401)

# A stand-in for Anthropic's library, so the real request path is exercised without a network or a key.
import ssl as _ssl, types as _types
fa = _types.ModuleType("anthropic")
class _E(Exception): pass
class APITimeoutError(_E): pass
class APIConnectionError(_E): pass
class APIStatusError(_E):
    def __init__(self, status_code, body):
        super().__init__(f"Error code: {status_code}"); self.status_code, self.body = status_code, body
fa.APITimeoutError, fa.APIConnectionError, fa.APIStatusError = APITimeoutError, APIConnectionError, APIStatusError
fa.Timeout = lambda total, connect=None, read=None: ("timeout", total, connect, read)
fa.DefaultHttpxClient = lambda verify=None: ("client", verify)
behaviour = {"raise": None, "seen": []}
class _Final:
    def model_dump(self, mode=None, exclude_none=False):
        return {"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn", "usage": {"input_tokens": 1}}
class _Stream:
    def __enter__(self):
        if behaviour["raise"]: raise behaviour["raise"]
        return self
    def __exit__(self, *a): return False
    def get_final_message(self): return _Final()
class _Messages:
    def stream(self, **payload):
        behaviour["seen"].append(payload); return _Stream()
class Anthropic:
    def __init__(self, **kw):
        behaviour["client_kw"] = kw; self.messages = _Messages()
fa.Anthropic = Anthropic
_saved_mod = sys.modules.get("anthropic")
sys.modules["anthropic"] = fa
got = _orig_post({"model": "m", "messages": []}, "k")
kw = behaviour["client_kw"]
check("the request is STREAMED through Anthropic's library and the whole answer is returned", got["stop_reason"] == "end_turn" and behaviour["seen"] == [{"model": "m", "messages": []}])
check("the library's own retries are off (this program decides what may be retried)", kw["max_retries"] == 0)
check("the connection trusts the system list plus certifi", isinstance(kw["http_client"][1], _ssl.SSLContext))
check("a long review is allowed up to 30 minutes, with at most 5 minutes of silence", kw["timeout"] == ("timeout", 1800.0, 30.0, 300.0))
def post_err(exc):
    behaviour["raise"] = exc
    try:
        _orig_post({"messages": []}, "k"); return None
    except t2.ApiError as e:
        return e
    finally:
        behaviour["raise"] = None
e1 = post_err(APITimeoutError("read timed out"))
check("a timeout says it may already have been charged and points at Console Usage", e1.status == -2 and "Usage in the Console" in str(e1), e1)
e2 = post_err(APIConnectionError("x"))
check("a dropped connection is treated the same way", e2.status == -2)
ce = APIConnectionError("x"); ce.__cause__ = _ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED")
e3 = post_err(ce)
check("a certificate failure is a setup problem, with the fix", e3.status == -1 and "certifi" in str(e3) and "HTTP" not in str(e3), e3)
e4 = post_err(APIStatusError(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
check("an API refusal keeps its status and Anthropic's message", e4.status == 401 and e4.message == "invalid x-api-key", e4)
sys.modules["anthropic"] = None
e5 = None
try:
    _orig_post({"messages": []}, "k")
except t2.ApiError as e:
    e5 = e
check("without the library: a plain install instruction", e5 is not None and e5.status == -1 and "pip install anthropic" in str(e5), e5)
if _saved_mod is not None:
    sys.modules["anthropic"] = _saved_mod
else:
    sys.modules.pop("anthropic", None)
for status, retried in ((-2, False), (-1, False), (529, True), (429, True), (400, False)):
    w = []
    t2.http_post = FakeApi(errors=[t2.ApiError(status, "x")])
    try:
        t2.post_with_retries({"messages": [{"role": "user", "content": "Escalation #1: AMRZ,"}]}, "k", sleep=w.append)
    except t2.ApiError:
        pass
    check(f"status {status} is {'retried' if retried else 'NOT retried (it may already be paid for, or will not change)'}", bool(w) == retried, w)

with project() as (base, dbp, conn, ids):
    esc = next(e for e in db.list_escalations(conn) if e["ticker"] == "AMRZ")
    fk = FakeApi(errors=[t2.ApiError(400, "web_search_20260318 requires allowed_callers direct for this model")])
    t2.http_post = fk
    row = t2.review_one(conn, esc, mode="manual", batch="b", model="claude-sonnet-5", api_key="k", framework=FRAMEWORK_TEXT, today=TODAY, sleep=lambda s: None)
    check("if the newer search tool is refused, the older one is used once", row["ok"] and fk.calls[1]["tools"][0]["type"] == "web_search_20250305")
    t2.http_post = FakeApi(errors=[t2.ApiError(400, "Web search is not enabled for this organization")])
    try:
        t2.review_one(conn, esc, mode="manual", batch="b", model="m", api_key="k", framework=FRAMEWORK_TEXT, today=TODAY); msg = ""
    except t2.ApiError as e:
        msg = str(e)
    check("web search switched off in the Console gives a plain instruction", "Turn it on in the Console" in msg, msg)

# ------------------------------------------------------------------------------------------------ calibration + live
print("Calibration and live runs")
with project() as (base, dbp, conn, ids):
    pos_before, st_before = sha(base / "positions.yaml"), [(e["escalation_id"], e["status"]) for e in db.list_escalations(conn)]
    t2.http_post = FakeApi()
    code, out = run(["--db", str(dbp)])
    check("a live run is refused before any calibration", code == 2 and "Live review refused" in out and not t2.http_post.calls, out)

    fake = FakeApi(verdicts={"AMRZ": "watch", "COO": "dismiss", "BMI": "dismiss", "OTF": "watch"})
    t2.http_post = fake
    code, out = run(["--calibrate", "--db", str(dbp)])
    check("calibration reviews exactly the four known names", sorted(re.search(r"Escalation #\d+: (\S+),", c["messages"][0]["content"]).group(1)
                                                                   for c in fake.calls) == ["AMRZ", "BMI", "COO", "OTF"])
    check("a mismatch (OTF) fails the calibration and says where", "Calibration NOT passed" in out and "OTF" in out and "DIFFERENT" in out, out[-600:])
    code, out = run(["--db", str(dbp)])
    check("live stays refused after a failed calibration", code == 2 and "did not match on: OTF" in out, out)

    (base / "positions.yaml").write_text(POSITIONS_TEXT + '  - ticker: COO\n    name: CooperCompanies\n    status: "Watch, Sleeve 1B candidate"\n', encoding="utf-8")
    pos_before = sha(base / "positions.yaml")
    fake = FakeApi(verdicts=t2.CALIBRATION)
    t2.http_post = fake
    code, out = run(["--calibrate", "--db", str(dbp)])
    check("a matching calibration passes", "Calibration PASSED" in out, out[-400:])
    check("a calibration never sends your positions.yaml notes, even for a name on your watch list",
          all("positions.yaml" not in c["messages"][0]["content"] for c in fake.calls) and len(fake.calls) == 4)
    rep = (base / "reviews" / f"{date.today().isoformat()}_tier2.md").read_text(encoding="utf-8")
    check("the written reviews show verdict, scores, your verdict, sources and cost",
          "## AMRZ: watch (3-2-4-3 = 12/20)" in rep and "your own verdict: watch" in rep and "https://example.com/a" in rep and "_Cost: $" in rep)
    check("the <result> JSON is not pasted into the readable review", "<result>" not in rep)
    check("Tier 2 changed NO escalation status and did not touch positions.yaml",
          [(e["escalation_id"], e["status"]) for e in db.list_escalations(conn)] == st_before and sha(base / "positions.yaml") == pos_before)

    new1, _ = db.insert_escalation(conn, ticker="NEWA", funnel="B", trigger="candidate", trigger_ref="2026-09-28", reasons=["r"])
    new2, _ = db.insert_escalation(conn, ticker="NEWB", funnel="B", trigger="candidate", trigger_ref="2026-09-28", reasons=["r"])
    fake = FakeApi()
    t2.http_post = fake
    code, out = run(["--db", str(dbp)])
    check("after calibration, a live run reviews only the OPEN queue", code == 0 and len(fake.calls) == 2 and "NEWA" in out and "NEWB" in out, out)
    code, out = run(["--db", str(dbp)])
    check("a second live run does not pay to review the same names again", "Nothing to review" in out and len(fake.calls) == 2, out)
    code, out = run(["--show", "--db", str(dbp)])
    check("--show lists reviews, spending and calibration state", "Calibration: PASSED" in out and "NEWA" in out and "spent this month" in out, out)

with project() as (base, dbp, conn, ids):
    for i in range(7):
        db.insert_escalation(conn, ticker=f"Z{i}", funnel="B", trigger="candidate", trigger_ref="2026-09-28", reasons=["r"])
    fake = FakeApi()
    t2.http_post = fake
    code, out = run(["--db", str(dbp), "--force"])
    check("--force allows a live run without calibration, still capped per run", len(fake.calls) == t2.MAX_PER_RUN and "2 more waiting" in out, out)

# ------------------------------------------------------------------------------------------------ guards
print("Guards")
with project() as (base, dbp, conn, ids):
    fake = FakeApi()
    t2.http_post = fake
    code, out = run(["--calibrate", "--dry-run", "--db", str(dbp)])
    dry = base / "reviews" / f"dry_run_{date.today().isoformat()}.md"
    check("a dry run calls nothing, stores nothing and shows what would be sent",
          code == 0 and not fake.calls and dry.exists() and "Jenisch Jan Philipp" in dry.read_text() and not db.list_tier2_reviews(conn), out)
    check("the dry run states names and an estimated cost", "AMRZ" in out and "Estimated cost" in out)

    (base / ".env").write_text('ANTHROPIC_API_KEY="sk-ant-test"\nTIER2_MONTHLY_BUDGET="0.50"\n', encoding="utf-8")
    code, out = run(["--calibrate", "--db", str(dbp)])
    check("the monthly budget stops a review BEFORE any money is spent", "Stopped before" in out and not fake.calls, out)

    (base / ".env").write_text('FMP_API_KEY="fmp-test"\n', encoding="utf-8")
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    code, out = run(["--calibrate", "--db", str(dbp)])
    check("no key: a plain instruction, no call", code == 2 and "--save-key" in out and not fake.calls, out)
    if saved:
        os.environ["ANTHROPIC_API_KEY"] = saved

    t2.getpass.getpass = lambda prompt="": "sk-ant-api03-abc"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        t2.cmd_save_key()
    env = (base / ".env").read_text()
    check("--save-key stores the key, keeps the other lines, never prints it",
          'ANTHROPIC_API_KEY="sk-ant-api03-abc"' in env and 'FMP_API_KEY="fmp-test"' in env and "sk-ant-api03-abc" not in buf.getvalue())
    check(".env stays private (mode 600)", stat.S_IMODE((base / ".env").stat().st_mode) == 0o600)
    t2.getpass.getpass = lambda prompt="": "not-a-key"
    with contextlib.redirect_stdout(io.StringIO()):
        rc = t2.cmd_save_key()
    check("something that is not an Anthropic key is refused", rc == 1 and 'sk-ant-api03-abc' in (base / ".env").read_text())

    fake = FakeApi(errors=[t2.ApiError(401, "invalid x-api-key")])
    t2.http_post = fake
    code, out = run(["--calibrate", "--db", str(dbp)])
    check("a bad key stops the whole run after the first name (no four identical failures)", len(fake.calls) == 1 and "Stopping" in out, out)
    check("the failure is recorded, at no cost", any(r["error"] and "401" in r["error"] and r["cost_usd"] == 0 for r in db.list_tier2_reviews(conn)))

    fake = FakeApi(errors=[t2.ApiError(-1, "this Mac's Python could not verify the secure connection")])
    t2.http_post = fake
    code, out = run(["--calibrate", "--db", str(dbp)])
    check("a setup failure stops the run after the first name", len(fake.calls) == 1 and "Stopping" in out and "HTTP" not in out.split("FAILED:")[1].splitlines()[0], out)

    fake = FakeApi(errors=[t2.ApiError(-2, "the request timed out while Claude was still working")])
    t2.http_post = fake
    code, out = run(["--calibrate", "--db", str(dbp)])
    check("a timeout stops the run after the first name, so it cannot cost four times", len(fake.calls) == 1 and "Stopping here" in out, out)

    class Boom:
        calls = 0
        def __call__(self, payload, key):
            Boom.calls += 1; raise KeyError("surprise")
    t2.http_post = Boom()
    code, out = run(["--calibrate", "--db", str(dbp)])
    check("an unexpected error is recorded and stops the run, with no crash", Boom.calls == 1 and "FAILED unexpectedly" in out
          and any("KeyError" in (r["error"] or "") for r in db.list_tier2_reviews(conn)), out)

    (base / "Investment_Strategy_Framework.md").unlink()
    code, out = run(["--calibrate", "--db", str(dbp)])
    check("without the framework file nothing is reviewed", code == 2 and "Investment_Strategy_Framework.md is not in" in out, out)

    code, out = run(["--id", "999", "--db", str(dbp)])
    check("an unknown escalation id is reported", code == 2 and "No escalation with id 999" in out)

# ------------------------------------------------------------------------------------------------ subscription (DECISIONS 55)
print("Subscription (Claude Code, no API credits)")
TOKEN = "sk-ant-oat01-" + "x" * 60


class FakeSub:
    """Stands in for `claude -p`: answers by ticker like FakeApi. errors: ApiErrors to raise, keyed by call number (1-based)."""
    def __init__(self, verdicts=None, errors=None):
        self.verdicts, self.errors, self.calls = verdicts or {}, dict(errors or {}), []

    def __call__(self, model, system_text, prompt, token, **kw):
        self.calls.append(dict(model=model, system=system_text, prompt=prompt, token=token))
        if len(self.calls) in self.errors:
            raise self.errors[len(self.calls)]
        tk = re.search(r"Escalation #\d+: (\S+),", prompt).group(1)
        v = self.verdicts.get(tk, "watch")
        text = (f"## Why it moved\nReview of {tk}.\n## Verdict\n{v}\nSources:\n- https://sec.gov/{tk}.htm\n"
                + result_block(tk, v, None if v == "dismiss" else (3, 2, 4, 3)))
        return {"type": "result", "subtype": "success", "is_error": False, "result": text, "total_cost_usd": 0.31,
                "usage": {"input_tokens": 9000, "output_tokens": 2000, "server_tool_use": {"web_search_requests": 3}}}


_orig_sub_run, _orig_getpass = t2.subscription_run, t2.getpass.getpass
# a token or backend exported in YOUR shell (e.g. for another project) would win over the test's .env: hide them here
_saved_env = {k: os.environ.pop(k) for k in ("CLAUDE_CODE_OAUTH_TOKEN", "TIER2_BACKEND", "CLAUDE_BIN") if k in os.environ}
with project() as (base, dbp, conn, ids):
    t2.getpass.getpass = lambda prompt="": "sk-ant-api03-abc"
    code, out = run(["--save-token"])
    check("--save-token refuses an API key pasted by mistake", code == 1 and "CLAUDE_CODE_OAUTH_TOKEN" not in (base / ".env").read_text())
    t2.getpass.getpass = lambda prompt="": TOKEN
    code, out = run(["--save-token"])
    envtxt = (base / ".env").read_text()
    check("--save-token stores the token, switches the backend, keeps .env private and never prints the token",
          code == 0 and f'CLAUDE_CODE_OAUTH_TOKEN="{TOKEN}"' in envtxt and 'TIER2_BACKEND="subscription"' in envtxt
          and stat.S_IMODE(os.stat(base / ".env").st_mode) == 0o600 and TOKEN not in out and "--use-api" in out, out)
    t2.getpass.getpass = _orig_getpass

    code, out = run(["--calibrate", "--dry-run", "--db", str(dbp)])
    check("a dry run says it would use the subscription and cost $0", "your Claude subscription" in out and "Cost: $0" in out, out)

    api = FakeApi(); t2.http_post = api
    sub = FakeSub(verdicts=t2.CALIBRATION); t2.subscription_run = sub
    code, out = run(["--calibrate", "--db", str(dbp)])
    check("calibration runs through the subscription, and the API is never called", "Calibration PASSED" in out and len(sub.calls) == 4
          and not api.calls and all(c["token"] == TOKEN for c in sub.calls), out[-500:])
    check("the same rules and framework are sent, plus the search cap and a request to list sources",
          all(t2.SYSTEM_RULES in c["system"] and FRAMEWORK_TEXT in c["system"] and f"at most {t2.MAX_SEARCHES} web searches" in c["system"] for c in sub.calls))
    check("calibration still hides positions.yaml", all("positions.yaml" not in c["prompt"] for c in sub.calls))
    rows = db.list_tier2_reviews(conn, mode="calibration")
    check("reviews are stored at $0 and marked as subscription", rows and all(r["cost_usd"] == 0 and r["model"].endswith("(subscription)") for r in rows))
    check("...so they never count against the API monthly budget", db.tier2_cost_since(conn, t2.month_start_utc()) == 0)
    rep = (base / "reviews" / f"{date.today().isoformat()}_tier2.md").read_text(encoding="utf-8")
    check("the review file says $0, and what the same review would have cost on the API", "_Cost: $0 (your Claude subscription" in rep
          and "about $0.31 if it had been billed to the API" in rep and "https://sec.gov/AMRZ.htm" in rep, rep[-400:])
    check("...and the sources are listed once (the review's own list), not twice", rep.count("https://sec.gov/AMRZ.htm") == 1)

    n1, _ = db.insert_escalation(conn, ticker="NEWA", funnel="B", trigger="candidate", trigger_ref="2026-10-17", reasons=["r"])
    n2, _ = db.insert_escalation(conn, ticker="NEWB", funnel="B", trigger="candidate", trigger_ref="2026-10-17", reasons=["r"])
    n3, _ = db.insert_escalation(conn, ticker="NEWC", funnel="B", trigger="candidate", trigger_ref="2026-10-17", reasons=["r"])
    sub = FakeSub(errors={2: t2.ApiError(-3, "your Claude plan's usage limit was reached (You've hit your session limit). The rest wait for the next run.")})
    t2.subscription_run = sub
    code, out = run(["--db", str(dbp)])
    check("a plan usage limit stops the run, keeps the first review, and makes no further attempts",
          len(sub.calls) == 2 and "usage limit was reached" in out and "stay open" in out, out)
    check("...and the name that hit the limit is NOT marked as reviewed, so the next run picks it up",
          n2 not in db.reviewed_escalation_ids(conn) and n1 in db.reviewed_escalation_ids(conn))
    sub = FakeSub(); t2.subscription_run = sub
    code, out = run(["--db", str(dbp)])
    check("the next run reviews exactly the two that were left", code == 0 and len(sub.calls) == 2 and "NEWB" in out and "NEWC" in out, out)

    code, out = run(["--use-api"])
    check("--use-api switches back, and says how to switch again", code == 0 and 'TIER2_BACKEND="api"' in (base / ".env").read_text()
          and "--use-subscription" in out)
    n4, _ = db.insert_escalation(conn, ticker="NEWD", funnel="B", trigger="candidate", trigger_ref="2026-10-17", reasons=["r"])
    api = FakeApi(); t2.http_post = api; sub = FakeSub(); t2.subscription_run = sub
    code, out = run(["--db", str(dbp)])
    check("...after which the API is used again and Claude Code is not", len(api.calls) == 1 and not sub.calls, out)
    code, out = run(["--use-subscription"])
    check("--use-subscription switches back to the subscription", code == 0 and t2.backend() == "subscription")

    # the environment handed to `claude -p`
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api03-SHOULDNOTPASS"
    env = t2.subscription_env(TOKEN)
    os.environ.pop("ANTHROPIC_API_KEY")
    check("an API key in the environment is removed (it would otherwise win and bill the API)",
          "ANTHROPIC_API_KEY" not in env and env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN)
    check("a private Claude Code config folder is used, inside the project", Path(env["CLAUDE_CONFIG_DIR"]).resolve() == (base / ".claude_tier2").resolve())  # macOS: /var is /private/var

    # the real subscription_run, with a fake `claude` process
    import subprocess as _sp
    seen = {}
    def fake_proc(stdout, rc=0):
        def _run(cmd, input=None, capture_output=None, text=None, cwd=None, env=None, timeout=None):
            seen.update(cmd=cmd, input=input, cwd=cwd, env=env)
            return _sp.CompletedProcess(cmd, rc, stdout=stdout, stderr="")
        return _run
    _orig_run, _orig_bin = _sp.run, t2.claude_bin
    t2.subscription_run = _orig_sub_run
    t2.claude_bin = lambda: "/usr/local/bin/claude"
    stream = "\n".join(json.dumps(e) for e in (
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "WebSearch", "input": {"query": "a"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}, {"type": "tool_use", "name": "WebSearch", "input": {}}]}},
        {"type": "assistant", "parent_tool_use_id": "t1", "message": {"content": [{"type": "tool_use", "name": "WebSearch"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "OK", "usage": {}}))
    _sp.run = fake_proc(stream)
    d = t2.subscription_run("claude-sonnet-5", "SYSTEM", "PROMPT", TOKEN)
    c = seen["cmd"]
    check("`claude -p` gets JSON output, the model, only WebSearch, no prompts, no saved session and no MCP servers",
          c[:2] == ["/usr/local/bin/claude", "-p"] and c[c.index("--tools") + 1] == "WebSearch" and c[c.index("--permission-mode") + 1] == "dontAsk"
          and "--no-session-persistence" in c and "--strict-mcp-config" in c and c[c.index("--model") + 1] == "claude-sonnet-5"
          and c[c.index("--output-format") + 1] == "stream-json" and "--verbose" in c, c)
    check("web searches are counted from Claude Code's WebSearch calls (the Oct 18 '0 searches' bug); a subagent's are not double-counted",
          d["_searches"] == 2, d.get("_searches"))
    check("the prompt goes in on stdin, the system prompt from a file, and it runs in an empty temporary folder",
          seen["input"] == "PROMPT" and "--system-prompt-file" in c and Path(seen["cwd"]) != base and not Path(seen["cwd"]).exists() and d["result"] == "OK")
    for label, payload, want in (
            ("a session limit becomes 'usage limit' (stop, retry next run)", {"is_error": True, "subtype": "success", "result": "You've hit your session limit · resets 3pm"}, -3),
            ("an expired or bad token says how to fix it", {"is_error": True, "subtype": "success", "result": "Invalid bearer token · Please run /login"}, -1)):
        _sp.run = fake_proc(json.dumps(payload), rc=1)
        try:
            t2.subscription_run("m", "s", "p", TOKEN); got = None
        except t2.ApiError as e:
            got = e
        check(label, got is not None and got.status == want and (want != -1 or "setup-token" in got.message), got)
    _sp.run = fake_proc("not json at all", rc=1)
    try:
        t2.subscription_run("m", "s", "p", TOKEN); got = None
    except t2.ApiError as e:
        got = e
    check("garbage output is a clear setup error, never a crash", got is not None and got.status == -1)
    t2.claude_bin = lambda: None
    try:
        t2.subscription_run("m", "s", "p", TOKEN); got = None
    except t2.ApiError as e:
        got = e
    check("Claude Code not installed: a clear message with the fix", got is not None and got.status == -1 and "CLAUDE_BIN" in got.message)
    _sp.run, t2.claude_bin = _orig_run, _orig_bin
t2.subscription_run = _orig_sub_run
os.environ.update(_saved_env)

t2.configure(HERE)
print(f"\n{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} checks")
sys.exit(0 if all(results) else 1)

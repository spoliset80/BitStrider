"""Offline tests for the 2026-09-08 daily automation controller + observation
layer. No network, no broker calls, no repo mutation: every artifact goes to a
temp dir, every network/subprocess dependency is stubbed at module level
(mirrors the shimming style of test_guardian_and_deploy.py).

Covers: ET window + phase deadlines, secret redaction, lock semantics
(fresh refusal / stale recovery / release), evidence gates (sample size,
minimum effect, relative improvement, drawdown tail, allowed files, .env
ban, prohibited-change declaration, runtime health, market day), the test
gate (never --skip-tests), deploy refusal, unexpected-change filtering, and
end-to-end main() paths (OBSERVE_ONLY, DRY_RUN, blocked, deployed) with
artifact + lock verification.
"""
import datetime
import json
import sys
import tempfile
import time
from pathlib import Path

import pytz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import daily_automation as da  # noqa: E402
import analyze_daily_portfolio as an  # noqa: E402

ET = pytz.timezone("America/New_York")
_PASS = _FAIL = 0


def check(name: str, cond: bool) -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name}")


def et(hhmm: str) -> datetime.datetime:
    h, m = hhmm.split(":")
    return datetime.datetime(2026, 9, 8, int(h), int(m), tzinfo=ET)


def healthy_obs() -> dict:
    return {
        "schema_version": 1, "as_of_et": "2026-09-08T12:06:00-04:00",
        "market_day_open_today": True, "fills_count": 0, "notes": [],
        "roundtrips": {"count": 80, "total_pnl": 41.0},
        "daily": [], "max_drawdown_daily": -30.0,
        "per_symbol_worst": [], "per_symbol_best": [], "churn_chains": [],
        "entry_bands": {}, "window_violations": 0,
        "runtime": {"heartbeat_age_s": 5.0, "heartbeat_stale": False,
                    "guardian_halted_today": False, "flat_request_flag": False,
                    "deploy_request_flag": False},
    }


def good_plan() -> dict:
    return {
        "schema_version": 1, "decision": "IMPLEMENT", "saturation": False,
        "problem": "repeated symbol churn",
        "evidence": {"baseline_days": 30, "baseline_trades": 80, "baseline_pnl": 40.0,
                     "candidate_pnl": 60.0, "baseline_max_drawdown": -30.0,
                     "candidate_max_drawdown": -25.0, "source": "30d ladder replay"},
        "allowed_files": ["engine/execution/enhanced.py"],
        "prohibited_changes": ["no leverage increase", "no guardian change"],
        "acceptance_tests": ["max 2 losing chains per symbol"],
        "summary": "per-symbol daily chain budget",
    }


print("== ET window / deadlines ==")
check("12:04 outside", not da.in_automation_window(et("12:04")))
check("12:05 inside", da.in_automation_window(et("12:05")))
check("13:59 inside", da.in_automation_window(et("13:59")))
check("14:00 outside (end-exclusive)", not da.in_automation_window(et("14:00")))
check("plan deadline 12:34 not past", not da.past_deadline("plan", et("12:34")))
check("plan deadline 12:35 past", da.past_deadline("plan", et("12:35")))
check("deploy deadline 13:59 not past", not da.past_deadline("deploy", et("13:59")))
check("deploy deadline 14:00 past", da.past_deadline("deploy", et("14:00")))
check("remaining plan at 12:10 = 1500s", da.remaining_seconds("plan", et("12:10")) == 1500)
check("remaining never negative", da.remaining_seconds("deploy", et("14:30")) == 0)

print("== redaction ==")
txt = "key LIVE_ALPACA_API_SECRET=abc123 and api_key = xyz and password: hunter2"
check("secret values scrubbed", "abc123" not in da.redact_text(txt)
      and "xyz" not in da.redact_text(txt) and "hunter2" not in da.redact_text(txt))
check("labels preserved", "LIVE_ALPACA_API_SECRET" in da.redact_text(txt))
obj = da.redact_obj({"API_SECRET": "s3cret", "nested": {"api_key": "k9"}, "sym": "SNOW"})
check("dict keys scrubbed", obj["API_SECRET"] == "REDACTED" and obj["nested"]["api_key"] == "REDACTED")
check("plain values untouched", obj["sym"] == "SNOW")

print("== lock semantics ==")
with tempfile.TemporaryDirectory() as tmp:
    lock = Path(tmp) / "daily_automation.lock"
    t0 = time.time()
    check("acquire fresh lock", da.acquire_lock(lock, now=t0))
    check("second acquire refused", not da.acquire_lock(lock, now=t0 + 60))
    check("stale lock recovered", da.acquire_lock(lock, stale_seconds=10, now=t0 + 3600))
    da.release_lock(lock)
    check("release removes lock", not lock.exists())
    check("re-acquire after release", da.acquire_lock(lock, now=t0 + 3601))
    da.release_lock(lock)

print("== within_repo / forbidden files ==")
check("repo file inside", da.within_repo("engine/config.py"))
check("traversal outside", not da.within_repo("../other/secret.py"))

print("== evidence gates ==")
obs = healthy_obs()
ok, reasons = da.evaluate_candidate(good_plan(), obs)
check("valid IMPLEMENT plan passes", ok and not reasons)

p = good_plan(); p["decision"] = "OBSERVE_ONLY"
ok, reasons = da.evaluate_candidate(p, obs)
check("non-IMPLEMENT rejected", not ok and "decision_not_implement" in reasons)

p = good_plan(); del p["evidence"]["baseline_pnl"]
ok, reasons = da.evaluate_candidate(p, obs)
check("missing evidence fails closed", not ok and any(r.startswith("evidence_missing") for r in reasons))

p = good_plan(); p["evidence"]["baseline_days"] = 3
ok, reasons = da.evaluate_candidate(p, obs)
check("insufficient trading days", not ok and any(r.startswith("insufficient_days") for r in reasons))

p = good_plan(); p["evidence"]["baseline_trades"] = 5
ok, reasons = da.evaluate_candidate(p, obs)
check("insufficient trades", not ok and any(r.startswith("insufficient_trades") for r in reasons))

p = good_plan(); p["evidence"]["candidate_pnl"] = 42.0
ok, reasons = da.evaluate_candidate(p, obs)
check("effect below minimum", not ok and any(r.startswith("effect_below_minimum") for r in reasons))

p = good_plan(); p["evidence"]["candidate_pnl"] = 41.0  # +2.5% < +5% relative
ok, reasons = da.evaluate_candidate(p, obs)
check("relative improvement below threshold", not ok and "relative_improvement_below_threshold" in reasons)

p = good_plan(); p["evidence"]["candidate_max_drawdown"] = -40.0  # worse tail
ok, reasons = da.evaluate_candidate(p, obs)
check("worse drawdown tail rejected", not ok and any(r.startswith("drawdown_tail_worse") for r in reasons))

p = good_plan(); p["allowed_files"] = [".env"]
ok, reasons = da.evaluate_candidate(p, obs)
check(".env forbidden", not ok and any(r.startswith("forbidden_file") for r in reasons))

p = good_plan(); p["allowed_files"] = ["../../etc/passwd"]
ok, reasons = da.evaluate_candidate(p, obs)
check("outside-repo file rejected", not ok and any(r.startswith("file_outside_repo") for r in reasons))

p = good_plan(); del p["prohibited_changes"]
ok, reasons = da.evaluate_candidate(p, obs)
check("prohibited changes must be declared", not ok and "prohibited_changes_not_declared" in reasons)

p = good_plan(); del p["acceptance_tests"]
ok, reasons = da.evaluate_candidate(p, obs)
check("acceptance tests must be declared", not ok and "acceptance_tests_not_declared" in reasons)

bad_rt = healthy_obs(); bad_rt["runtime"]["flat_request_flag"] = True
ok, reasons = da.evaluate_candidate(good_plan(), bad_rt)
check("flat flag blocks promotion", not ok and "runtime_unhealthy:flat_request_flag_present" in reasons)

bad_rt = healthy_obs(); bad_rt["runtime"]["heartbeat_stale"] = True
ok, reasons = da.evaluate_candidate(good_plan(), bad_rt)
check("stale heartbeat blocks promotion", not ok and "runtime_unhealthy:heartbeat_stale" in reasons)

bad_rt = healthy_obs(); bad_rt["runtime"]["guardian_halted_today"] = True
ok, reasons = da.evaluate_candidate(good_plan(), bad_rt)
check("guardian halt blocks promotion", not ok and "runtime_unhealthy:guardian_halted_today" in reasons)

bad_rt = healthy_obs(); bad_rt["runtime"]["deploy_request_flag"] = True
ok, reasons = da.evaluate_candidate(good_plan(), bad_rt)
check("pending deploy flag blocks new deploy", not ok and "deploy_flag_already_pending" in reasons)

closed = healthy_obs(); closed["market_day_open_today"] = False
ok, reasons = da.evaluate_candidate(good_plan(), closed)
check("market closed blocks promotion", not ok and "market_closed_today" in reasons)

print("== analyzer: ladder reconstruction (offline) ==")
fills = [
    {"symbol": "AAA", "side": "buy", "price": 10.0, "qty": 10, "client_order_id": "apex-entry-x",
     "transaction_time": "2026-09-07T14:00:00Z"},
    {"symbol": "AAA", "side": "sell", "price": 11.0, "qty": 10, "client_order_id": "apex-close-x",
     "transaction_time": "2026-09-07T15:00:00Z"},
    {"symbol": "BBB", "side": "sell", "price": 20.0, "qty": 5, "client_order_id": "apex-entry-y",
     "transaction_time": "2026-09-07T14:30:00Z"},
    {"symbol": "BBB", "side": "buy", "price": 19.0, "qty": 5, "client_order_id": "apex-close-y",
     "transaction_time": "2026-09-07T15:30:00Z"},
    # churn: 3 same-day round trips on CCC, net negative
    {"symbol": "CCC", "side": "buy", "price": 30.0, "qty": 2, "client_order_id": "apex-entry-z",
     "transaction_time": "2026-09-07T14:00:00Z"},
    {"symbol": "CCC", "side": "sell", "price": 29.0, "qty": 2, "client_order_id": "apex-close-z",
     "transaction_time": "2026-09-07T14:20:00Z"},
    {"symbol": "CCC", "side": "buy", "price": 30.0, "qty": 2, "client_order_id": "apex-reentry-trail-z",
     "transaction_time": "2026-09-07T14:40:00Z"},
    {"symbol": "CCC", "side": "sell", "price": 29.0, "qty": 2, "client_order_id": "apex-close-z",
     "transaction_time": "2026-09-07T15:00:00Z"},
    {"symbol": "CCC", "side": "buy", "price": 30.0, "qty": 2, "client_order_id": "apex-entry-z2",
     "transaction_time": "2026-09-07T15:10:00Z"},
    {"symbol": "CCC", "side": "sell", "price": 29.5, "qty": 2, "client_order_id": "apex-close-z2",
     "transaction_time": "2026-09-07T15:20:00Z"},
]
rts = an.reconstruct_roundtrips(fills)
check("5 round trips reconstructed (1 long + 1 short + 3 CCC churn)",
      len(rts) == 5)
long_rt = [r for r in rts if r["symbol"] == "AAA"][0]
check("long pnl 10*1.0", abs(long_rt["pnl"] - 10.0) < 1e-6)
short_rt = [r for r in rts if r["symbol"] == "BBB"][0]
check("short pnl 5*1.0", abs(short_rt["pnl"] - 5.0) < 1e-6 and short_rt["kind"] == "short")
ccc = [r for r in rts if r["symbol"] == "CCC"]
chains = an.churn_chains(rts)
check("churn chain flagged (3 RT, net neg)", chains and chains[0]["count"] == 3 and chains[0]["pnl"] < 0)
check("reentry kind classified", any(r["entry_kind"] == "reentry" for r in ccc))
check("daily series sorted", [d["date"] for d in an.daily_pnl_series(rts)] ==
      sorted(d["date"] for d in an.daily_pnl_series(rts)))
check("max drawdown negative or zero", an.max_drawdown(an.daily_pnl_series(rts)) <= 0)
check("band classifier: lunch violation", an.band_for_minutes(12 * 60) == "lunch_violation")
check("band classifier: pm window", an.band_for_minutes(14 * 60 + 20) == "pm_1415_1544")
check("offline observation builds", an.build_observation(offline=True)["roundtrips"]["count"] == 0)

print("== test gate / deploy guard ==")
calls = []


def fake_run_ok(*a, **k):
    calls.append(list(a[0]) if a else [])
    return type("R", (), {"returncode": 0, "stdout": "OK", "stderr": ""})()


_orig_run = da.subprocess.run
with tempfile.TemporaryDirectory() as tmp:
    fake_root = Path(tmp)
    (fake_root / "scripts").mkdir()
    (fake_root / "scripts" / "test_dummy.py").write_text("print('OK')", encoding="utf-8")
    da.subprocess.run = fake_run_ok
    try:
        ok, results = da.run_tests(root=fake_root)
        check("test gate ok when all suites pass", ok)
        check("compileall included", any(r["name"] == "compileall" for r in results))
        check("--skip-tests never passed", not any("--skip-tests" in c for c in calls))
    finally:
        da.subprocess.run = _orig_run

with tempfile.TemporaryDirectory() as tmp:
    out_dir = Path(tmp)
    da.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("deploy attempted"))
    try:
        ok, tail = da.deploy_phase("fix --skip-tests bypass", out_dir, python="python")
        check("deploy refused on skip-tests in reason", not ok and "refused" in tail)
    finally:
        da.subprocess.run = _orig_run

print("== unexpected-change filtering ==")
orig_changed = da.changed_files
da.changed_files = lambda root=da.ROOT: [
    "engine/execution/enhanced.py",           # allowed
    "scripts/test_new_thing.py",              # new focused test -> allowed
    "data/ti_primary.json",                   # runtime noise -> allowed
    "engine/.daily_state.json",               # runtime noise -> allowed
    "engine/config.py",                       # NOT allowed
    ".env",                                   # NOT allowed
]
try:
    bad = da.unexpected_changes(good_plan())
    check("allowed/noise excluded, surprises flagged",
          bad == ["engine/config.py", ".env"])
finally:
    da.changed_files = orig_changed

print("== end-to-end main() paths ==")


def run_main(monkey: dict, argv: list) -> tuple[int, Path]:
    saved = {k: getattr(da, k) for k in monkey}
    for k, v in monkey.items():
        setattr(da, k, v)
    try:
        rc = da.main(argv)
    finally:
        for k, v in saved.items():
            setattr(da, k, v)
    return rc, Path(argv[argv.index("--out") + 1])


with tempfile.TemporaryDirectory() as tmp:
    out = str(Path(tmp) / "run1")
    lock = Path(tmp) / "l.lock"
    monkey = {
        "LOCK_FILE": lock,
        "run_observation": lambda days=30, offline=False: healthy_obs(),
        "plan_phase": lambda obs, out_dir, now=None: (good_plan(), {"cline": None}),
    }
    rc, o = run_main(monkey, ["--force", "--offline", "--dry-run", "--out", out])
    st = json.loads((o / "run-state.json").read_text(encoding="utf-8"))
    check("dry-run: candidate passes gates, nothing runs",
          rc == 0 and st["decision"] == "DRY_RUN_CANDIDATE_OK")
    check("dry-run: no deploy attempt", not st["deploy"]["attempted"])
    check("artifacts written (observation/candidate/handoff/run-state/log)",
          all((o / f).exists() for f in ("observation.json", "candidate.json",
                                         "compact-handoff.md", "run-state.json",
                                         "daily-run.log")))
    check("lock released after run", not lock.exists())

with tempfile.TemporaryDirectory() as tmp:
    out = str(Path(tmp) / "run2")
    lock = Path(tmp) / "l.lock"

    def _no_tests(*a, **k):
        raise AssertionError("run_tests must not run after act failure")
    monkey = {
        "LOCK_FILE": lock,
        "run_observation": lambda days=30, offline=False: healthy_obs(),
        "plan_phase": lambda obs, out_dir, now=None: (good_plan(), {"cline": None}),
        "act_phase": lambda plan, out_dir, now=None: (False, {"output_tail": "boom"}),
        "run_tests": _no_tests,
        "deploy_phase": lambda *a, **k: (_ for _ in ()).throw(AssertionError("deploy on failed act")),
    }
    rc, o = run_main(monkey, ["--force", "--offline", "--out", out, "--allow-deploy"])
    st = json.loads((o / "run-state.json").read_text(encoding="utf-8"))
    check("act failure blocks pipeline",
          rc == 2 and st["decision"] == "IMPLEMENT_BLOCKED")

with tempfile.TemporaryDirectory() as tmp:
    out = str(Path(tmp) / "run3")
    lock = Path(tmp) / "l.lock"
    monkey = {
        "LOCK_FILE": lock,
        "run_observation": lambda days=30, offline=False: healthy_obs(),
        "plan_phase": lambda obs, out_dir, now=None: (good_plan(), {"cline": None}),
        "act_phase": lambda plan, out_dir, now=None: (True, {"output_tail": ""}),
        "run_tests": lambda *a, **k: (True, [{"name": "test_dummy.py", "ok": True, "tail": []}]),
        "verify_phase": lambda plan, tests_ok, results, out_dir, now=None: (True, []),
        "deploy_phase": lambda reason, out_dir, **k: (True, "flag written"),
    }
    rc, o = run_main(monkey, ["--force", "--offline", "--out", out, "--allow-deploy"])
    st = json.loads((o / "run-state.json").read_text(encoding="utf-8"))
    check("happy path deploys via gated flag",
          rc == 0 and st["decision"] == "DEPLOYED")
    check("test gate ran before deploy", st["tests"]["ok"] is True)

with tempfile.TemporaryDirectory() as tmp:
    out = str(Path(tmp) / "run4")
    lock = Path(tmp) / "l.lock"
    observe_only = {"decision": "OBSERVE_ONLY", "saturation": True,
                    "reason": "no_qualified_candidate", "summary": "nothing clears the bar"}
    monkey = {
        "LOCK_FILE": lock,
        "run_observation": lambda days=30, offline=False: healthy_obs(),
        "plan_phase": lambda obs, out_dir, now=None: (observe_only, {"cline": None}),
        "act_phase": lambda plan, out_dir, now=None: (_ for _ in ()).throw(
            AssertionError("act on observe day")),
    }
    rc, o = run_main(monkey, ["--force", "--offline", "--out", out])
    st = json.loads((o / "run-state.json").read_text(encoding="utf-8"))
    check("saturation day recorded, no code touched",
          rc == 0 and st["decision"] == "OBSERVE_ONLY" and st["saturation"] is True)
    handoff = (o / "compact-handoff.md").read_text(encoding="utf-8")
    check("handoff bounded and records saturation",
          len(handoff.splitlines()) <= da.HANDOFF_MAX_LINES and "saturation: True" in handoff)

with tempfile.TemporaryDirectory() as tmp:
    out = str(Path(tmp) / "run5")
    lock = Path(tmp) / "l.lock"
    monkey = {"LOCK_FILE": lock}
    rc, o = run_main(monkey, ["--force", "--offline", "--skip-agent", "--out", out])
    st = json.loads((o / "run-state.json").read_text(encoding="utf-8"))
    check("skip-agent observe-only run", rc == 0 and st["decision"] == "OBSERVE_ONLY")

print("== provider fallback chain (default -> deepseek -> moonshot) ==")
_saved_env_fb = da.os.environ.get("CLINE_PROVIDER_FALLBACKS")
_saved_subprocess_run = da.subprocess.run
try:
    with tempfile.TemporaryDirectory() as tmp:
        # Chain parsed from the module default: default + 2 fallbacks.
        da.os.environ.pop("CLINE_PROVIDER_FALLBACKS", None)
        chain = [None] + da._provider_fallbacks()
        check("default chain = default+2 fallbacks", len(chain) == 3
              and chain[1][0] == "deepseek" and chain[2][0] == "moonshot"
              and bool(chain[1][1]) and bool(chain[2][1]))
        # Env override parsed correctly (provider without model is allowed).
        da.os.environ["CLINE_PROVIDER_FALLBACKS"] = "openrouter:x-ai/grok"
        chain2 = [None] + da._provider_fallbacks()
        check("env override parsed", chain2 == [None, ("openrouter", "x-ai/grok")])
        # "off" / empty disables fallbacks entirely.
        da.os.environ["CLINE_PROVIDER_FALLBACKS"] = "off"
        check("'off' disables chain", da._provider_fallbacks() == [])
        da.os.environ["CLINE_PROVIDER_FALLBACKS"] = ""
        check("empty disables chain", da._provider_fallbacks() == [])
        # Restore the default chain for the subprocess simulations below.
        da.os.environ.pop("CLINE_PROVIDER_FALLBACKS", None)

        # Live-subprocess double: default fails, deepseek fails, moonshot ok.
        class _R:
            def __init__(self, code):
                self.returncode = code
                self.stdout = '{"type":"done"}'
                self.stderr = ""

        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            return _R(3 if len(calls) < 3 else 0)

        da.subprocess.run = fake_run
        ok, tail = da.run_cline("probe", Path(tmp) / "p1", "probe", False, 30, "cline")
        check(f"fallback lands on 3rd provider (ok={ok}, calls={len(calls)})",
              ok and len(calls) == 3)
        check("fallback flags passed per attempt",
              "-P" in calls[1] and "deepseek" in calls[1]
              and "-P" in calls[2] and "moonshot" in calls[2])
        check("attempt 1 has no -P (uses CLI default)",
              "-P" not in calls[0] and "--plan" not in calls[0])

        # Provider error can surface INSIDE the JSON stream with exit 0
        # (e.g. credits exhausted) -> must still trigger the fallback.
        calls.clear()

        def fake_exit0_error(args, **kw):
            calls.append(args)
            r0 = _R(0)
            if len(calls) == 1:
                r0.stdout = ('{"type":"run_result","finishReason":"error",'
                             '"text":"credits exhausted"}')
            return r0

        da.subprocess.run = fake_exit0_error
        ok4, tail4 = da.run_cline("probe", Path(tmp) / "p4", "probe", False, 30, "cline")
        check(f"exit-0 error-output still falls back (ok={ok4}, calls={len(calls)})",
              ok4 and len(calls) == 2
              and "-P" in calls[1] and "deepseek" in calls[1])
        check("error-output audited", "error-output" in tail4)

        da.subprocess.run = fake_run
        check("attempts audited in tail", "provider=default" in tail
              and "provider=deepseek" in tail and "provider=moonshot" in tail)

        # First attempt succeeds -> single call, no fallback attempted.
        calls.clear()

        def fake_ok(args, **kw):
            calls.append(args)
            return _R(0)

        da.subprocess.run = fake_ok
        ok2, tail2 = da.run_cline("probe", Path(tmp) / "p2", "probe", True, 30, "cline")
        check("healthy default stops the chain", ok2 and len(calls) == 1)
        check("plan mode flag preserved", "--plan" in calls[0])
        check("success note in tail", "provider=default -> ok" in tail2)

        # Every attempt fails -> (False, audited tail) -> fail-closed downstream.
        calls.clear()

        def fake_all_fail(args, **kw):
            calls.append(args)
            return _R(1)

        da.subprocess.run = fake_all_fail
        ok3, tail3 = da.run_cline("probe", Path(tmp) / "p3", "probe", False, 30, "cline")
        check("all-fail returns not-ok", not ok3 and len(calls) == 3)
        check("all-fail tail audited", "exit 1" in tail3 and "provider=moonshot" in tail3)
finally:
    da.subprocess.run = _saved_subprocess_run
    if _saved_env_fb is None:
        da.os.environ.pop("CLINE_PROVIDER_FALLBACKS", None)
    else:
        da.os.environ["CLINE_PROVIDER_FALLBACKS"] = _saved_env_fb

print(f"\nTOTAL: {_PASS} passed, {_FAIL} failed")
if _FAIL:
    print("FAILURES PRESENT")
    sys.exit(1)
print("OK: test_daily_automation.py")

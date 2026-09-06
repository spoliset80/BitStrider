"""Daily automation controller (2026-09-08) -- observe -> plan -> act ->
verify -> deploy, all evidence-gated and deadline-aware.

Deterministic safety layer around the daily improvement loop on a LIVE-money
bot (see scripts/Agenticdeploy.md's non-goal: the fast path stays
deterministic; the LLM (Cline CLI, when installed) is confined to bounded
plan/act/verify sessions whose outputs the controller re-validates).

Hard rules enforced here:
  - ET window [12:05, 14:00) on an open market day, else no-op (record only).
  - Machine-local lock: overlapping runs are refused; stale locks recovered.
  - OBSERVE_ONLY when Cline CLI is unavailable, the plan artifact is missing
    or invalid, evidence gates fail, or the plan declares saturation.
  - Implementation is restricted to the plan's allowed_files (verified against
    git diff before deployment); .env is never allowed.
  - Deployment ONLY via scripts/deploy.py (the existing test-gated flag
    writer). --skip-tests is never passed. The watchdog still applies the
    restart only inside its flat windows, so a late flag defers to the
    post-15:44 window by design.
  - Default is observe-only: live deployment additionally requires
    --allow-deploy or AUTOMATION_ALLOW_DEPLOY=1.

Artifacts (machine-local): %LOCALAPPDATA%\\ApexTrader\\automation\\<date>\\
  observation.json/.md, candidate.json, test-results.json, run-state.json,
  compact-handoff.md, daily-run.log, prompts/*.txt

Usage:
  python scripts/daily_automation.py [--days 30] [--force] [--offline]
      [--dry-run] [--skip-agent] [--allow-deploy] [--out DIR]
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze_daily_portfolio as analyzer  # noqa: E402

ET = pytz.timezone("America/New_York")
LOCAL_BASE = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
STATE_DIR = LOCAL_BASE / "ApexTrader" / "state"
AUTOMATION_DIR = LOCAL_BASE / "ApexTrader" / "automation"
LOCK_FILE = STATE_DIR / "daily_automation.lock"
VENV_PY = LOCAL_BASE / "ApexTrader" / "venv" / "Scripts" / "python.exe"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy.py"

WINDOW_START_ET = "12:05"
WINDOW_END_ET = "14:00"
PHASE_DEADLINES_ET = {"plan": "12:35", "act": "13:30", "test": "13:50", "deploy": "14:00"}
STALE_LOCK_SECONDS = 3 * 3600
CLINE_PLAN_TIMEOUT_S = 1200
CLINE_ACT_TIMEOUT_S = 2400
# Provider fallback chain after the CLI default (default -> deepseek ->
# moonshot). Override/disable via env CLINE_PROVIDER_FALLBACKS (see
# _provider_fallbacks below). Models verified live 2026-09-06 against each
# provider's /models endpoint with the keys stored in Cline.
DEFAULT_PROVIDER_FALLBACKS = "deepseek:deepseek-v4-flash,moonshot:kimi-k2.7-code"
HANDOFF_MAX_LINES = 160

GATES = {
    "min_trading_days": 5,
    "min_trades": 20,
    "min_effect_dollars": 5.0,
    "min_relative_improvement": 0.05,
    "max_drawdown_worsening": 0.10,
}

# Allowlist for Cline CLI shell commands (deny wins; no redirects).
CLINE_COMMAND_PERMISSIONS = json.dumps({
    "allow": [
        "git status*", "git diff*", "git log*", "git rev-parse*",
        "*python.exe scripts\\test_*.py*", "*python.exe -m compileall*",
        "*python.exe scripts/analyze_daily_portfolio.py*",
    ],
    "deny": [
        "git reset --hard*", "git clean*", "git push*", "git checkout -- *",
        "rm *", "del *", "Remove-Item*", "*--skip-tests*", "*deploy.py*",
        "*.env*", "schtasks*", "reg *", "net *",
    ],
    "allowRedirects": False,
})

# Runtime noise that may be dirty without being part of a candidate
# (convention #5 in AGENT_CONTEXT.md -- never committed, never gating).
RUNTIME_NOISE_PREFIXES = (
    "data/", "graphify-out/", "heartbeat.txt", "engine/.daily_state.json",
    "engine/predictions/", ".claude/", "apextrader/", "predictions/",
)


def et_now() -> datetime.datetime:
    return datetime.datetime.now(ET)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_automation_window(t: datetime.datetime) -> bool:
    """ET half-open window [12:05, 14:00)."""
    m = t.hour * 60 + t.minute
    return _minutes(WINDOW_START_ET) <= m < _minutes(WINDOW_END_ET)


def past_deadline(phase: str, t: datetime.datetime | None = None) -> bool:
    t = t or et_now()
    return (t.hour * 60 + t.minute) >= _minutes(PHASE_DEADLINES_ET[phase])


def remaining_seconds(phase: str, t: datetime.datetime | None = None) -> int:
    t = t or et_now()
    total = _minutes(PHASE_DEADLINES_ET[phase])
    deadline = t.replace(hour=total // 60, minute=total % 60, second=0, microsecond=0)
    return max(0, int((deadline - t).total_seconds()))


_SECRET_RE = re.compile(r"(?i)(api[_-]?secret|api[_-]?key|token|password|secret)(\s*[=:]\s*)(\S+)")
_SECRET_KEY_RE = re.compile(r"(?i)(secret|token|password|api[_-]?key)")


def redact_text(s: str) -> str:
    return _SECRET_RE.sub(r"\1\2REDACTED", str(s))


def redact_obj(o):
    if isinstance(o, dict):
        return {k: ("REDACTED" if _SECRET_KEY_RE.search(str(k)) else redact_obj(v))
                for k, v in o.items()}
    if isinstance(o, list):
        return [redact_obj(v) for v in o]
    if isinstance(o, str):
        return redact_text(o)
    return o


def acquire_lock(path: Path | None = None, stale_seconds: int = STALE_LOCK_SECONDS,
                 now: float | None = None) -> bool:
    """True if we own the lock. Refuses a fresh lock held by another run;
    recovers a stale one (older than stale_seconds) so a crashed run cannot
    wedge the loop forever."""
    path = path or LOCK_FILE
    now = time.time() if now is None else now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                started = float(data.get("epoch", 0))
            except Exception:
                started = 0.0
            if started and (now - started) < stale_seconds:
                return False
        path.write_text(json.dumps({
            "pid": os.getpid(), "epoch": now,
            "started_et": et_now().isoformat(),
        }), encoding="utf-8")
        return True
    except Exception:
        return False


def release_lock(path: Path | None = None) -> None:
    path = path or LOCK_FILE
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def within_repo(rel_path: str, root: Path = ROOT) -> bool:
    try:
        p = (root / rel_path).resolve()
        return str(p).startswith(str(root.resolve()))
    except Exception:
        return False


def evaluate_candidate(plan: dict, obs: dict, gates: dict | None = None) -> tuple[bool, list]:
    """Objective promotion gates (pure function; fully unit-tested).

    Returns (ok, failure_reasons). A plan that is not an explicit IMPLEMENT
    with complete evidence, minimum sample size, holdout improvement beyond
    the minimum effect, no worse drawdown tail, allowed files inside the repo
    (never .env), declared prohibited changes + acceptance tests, healthy
    runtime, and an open market day FAILS -- fail closed, every time."""
    gates = gates or GATES
    reasons: list = []
    if plan.get("decision") != "IMPLEMENT":
        return False, ["decision_not_implement"]
    ev = plan.get("evidence") or {}
    required = ("baseline_days", "baseline_trades", "baseline_pnl", "candidate_pnl",
                "baseline_max_drawdown", "candidate_max_drawdown")
    for k in required:
        if k not in ev:
            reasons.append(f"evidence_missing:{k}")
    if reasons:
        return False, reasons
    if ev["baseline_days"] < gates["min_trading_days"]:
        reasons.append(f"insufficient_days:{ev['baseline_days']}<{gates['min_trading_days']}")
    if ev["baseline_trades"] < gates["min_trades"]:
        reasons.append(f"insufficient_trades:{ev['baseline_trades']}<{gates['min_trades']}")
    effect = ev["candidate_pnl"] - ev["baseline_pnl"]
    if effect < gates["min_effect_dollars"]:
        reasons.append(f"effect_below_minimum:{effect:.2f}<{gates['min_effect_dollars']}")
    if ev["baseline_pnl"] > 0 and \
            ev["candidate_pnl"] < ev["baseline_pnl"] * (1 + gates["min_relative_improvement"]):
        reasons.append("relative_improvement_below_threshold")
    dd_floor = ev["baseline_max_drawdown"] * (1 + gates["max_drawdown_worsening"])
    if ev["candidate_max_drawdown"] < dd_floor:
        reasons.append(f"drawdown_tail_worse:{ev['candidate_max_drawdown']}<{dd_floor:.2f}")
    allowed = plan.get("allowed_files") or []
    if not allowed:
        reasons.append("allowed_files_empty")
    for f in allowed:
        if not within_repo(f):
            reasons.append(f"file_outside_repo:{f}")
        elif str(f).replace("\\", "/").endswith(".env") or "secret" in str(f).lower():
            reasons.append(f"forbidden_file:{f}")
    if not (plan.get("prohibited_changes") or []):
        reasons.append("prohibited_changes_not_declared")
    if not (plan.get("acceptance_tests") or []):
        reasons.append("acceptance_tests_not_declared")
    rt = obs.get("runtime") or {}
    if rt.get("heartbeat_stale"):
        reasons.append("runtime_unhealthy:heartbeat_stale")
    if rt.get("flat_request_flag"):
        reasons.append("runtime_unhealthy:flat_request_flag_present")
    if rt.get("guardian_halted_today"):
        reasons.append("runtime_unhealthy:guardian_halted_today")
    if rt.get("deploy_request_flag"):
        reasons.append("deploy_flag_already_pending")
    if obs.get("market_day_open_today") is False:
        reasons.append("market_closed_today")
    return (not reasons), reasons


def log(out_dir: Path, line: str) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "daily-run.log", "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.datetime.now(ET).isoformat()} {redact_text(line)}\n")
    except Exception:
        pass


def run_observation(days: int = 30, offline: bool = False) -> dict:
    return analyzer.build_observation(days=days, offline=offline)


def find_cline() -> str | None:
    """Locate the Cline CLI (scheduled tasks run with a minimal PATH, so also
    probe common install locations). None = CLI unavailable -> OBSERVE_ONLY."""
    exe = "cline.cmd" if os.name == "nt" else "cline"
    try:
        found = subprocess.run(["where", exe] if os.name == "nt" else ["which", exe],
                               capture_output=True, text=True, timeout=15).stdout.strip()
        if found:
            return found.splitlines()[0].strip()
    except Exception:
        pass
    for cand in (
        Path(os.environ.get("APPDATA", "")) / "npm" / exe,
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "cline" / exe,
    ):
        if str(cand) and cand.exists():
            return str(cand)
    return None


def _provider_fallbacks() -> list[tuple[str | None, str | None]]:
    """Cline provider fallback chain (after the CLI default provider).
    Default: default -> deepseek -> moonshot (verified live 2026-09-06:
    both stored API keys valid; models deepseek-v4-flash and kimi-k2.7-code
    confirmed via each provider's /models endpoint). Override with env
    CLINE_PROVIDER_FALLBACKS="provider:model,provider2:model2"; set it to
    "off" (or empty) to disable fallbacks entirely."""
    raw = os.environ.get("CLINE_PROVIDER_FALLBACKS",
                         DEFAULT_PROVIDER_FALLBACKS).strip()
    if not raw or raw.lower() == "off":
        return []
    chain: list[tuple[str | None, str | None]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        provider, _, model = part.partition(":")
        chain.append((provider.strip() or None, model.strip() or None))
    return chain


def run_cline(prompt: str, out_dir: Path, name: str, plan_mode: bool,
              timeout_s: int, cline_path: str) -> tuple[bool, str]:
    """One bounded Cline CLI session with provider fallback. Returns
    (ok, combined_output_tail). If the CLI default provider fails (e.g.
    credits exhausted), each entry of _provider_fallbacks() is tried in
    order via -P/-m until one succeeds; every attempt is logged into the
    returned tail (which lands in run-state.json for audit). Command
    permissions are always enforced via CLINE_COMMAND_PERMISSIONS."""
    try:
        prompts_dir = out_dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / f"{name}.txt").write_text(redact_text(prompt), encoding="utf-8")
    except Exception:
        pass
    env = os.environ.copy()
    env["CLINE_COMMAND_PERMISSIONS"] = CLINE_COMMAND_PERMISSIONS
    attempts: list[tuple[str | None, str | None]] = [(None, None)]
    attempts += _provider_fallbacks()
    tails: list[str] = []
    for idx, (provider, model) in enumerate(attempts):
        tag = provider or "default"
        args = [cline_path]
        if plan_mode:
            args.append("--plan")
        if provider:
            args += ["-P", provider]
        if model:
            args += ["-m", model]
        args += ["--auto-approve", "true", "--json", "--timeout", str(timeout_s),
                 "--cwd", str(ROOT), prompt]
        try:
            r = subprocess.run(args, env=env, cwd=str(ROOT), capture_output=True,
                               text=True, timeout=timeout_s)
            out = (r.stdout or "") + (r.stderr or "")
            tail = redact_text(out[-4000:])
            # Some provider failures (bogus provider, exhausted credits,
            # auth errors) surface as an error run_result inside the JSON
            # stream -- sometimes even with exit code 0. Both shapes count
            # as failure so the fallback chain actually engages.
            errored = ('"finishReason":"error"' in out
                       or '"finishReason": "error"' in out
                       or '"reason":"error"' in out or '"reason": "error"' in out)
            if r.returncode == 0 and not errored:
                note = (f"[provider-fallback] attempt {idx + 1}/{len(attempts)} "
                        f"provider={tag} -> ok")
                prefix = ("\n".join(tails) + "\n") if tails else ""
                return True, prefix + tail + "\n" + note
            reason = "error-output" if (r.returncode == 0 and errored) else f"exit {r.returncode}"
            tails.append(f"[provider-fallback] attempt {idx + 1}/{len(attempts)} "
                         f"provider={tag} -> {reason}: {tail[-500:]}")
        except Exception as exc:
            tails.append(f"[provider-fallback] attempt {idx + 1}/{len(attempts)} "
                         f"provider={tag} -> {type(exc).__name__}: "
                         f"{redact_text(str(exc))[:300]}")
    return False, "\n".join(tails)


PLAN_PROMPT = """You are the PLAN phase of the ApexTrader daily improvement loop \
(PLAN MODE: read-only analysis; your ONLY permitted write is one JSON artifact).

Read, in this order:
1. AGENT_CONTEXT.md (repo root) - environment, live behavior, hard rules.
2. The newest "## Snapshot" heading in AGENT_CHECKPOINT.md.
3. Today's observation: {obs_json}

Decide whether ONE rigorously evidence-supported improvement to the trading \
code is justified TODAY. Rules:
- Evidence must span >= {min_days} trading days and >= {min_trades} round trips \
(use the observation's roundtrips/per_symbol/churn/entry_bands sections).
- Never propose: leverage increases, guardian threshold loosening, removal of \
stop protection, overnight holds, or blanket rejection of high-momentum entries.
- If no change clears that bar (including repeated/noisy findings), set \
decision="OBSERVE_ONLY", saturation=true if this repeats a prior day's finding.

Write EXACTLY ONE file: {candidate_path} with this schema:
{{
  "schema_version": 1,
  "decision": "IMPLEMENT" | "OBSERVE_ONLY",
  "saturation": true | false,
  "problem": "...",
  "evidence": {{"baseline_days": N, "baseline_trades": N, "baseline_pnl": X,
                "candidate_pnl": X, "baseline_max_drawdown": -X,
                "candidate_max_drawdown": -X, "source": "..."}},
  "allowed_files": ["engine/...", "scripts/test_...py"],
  "prohibited_changes": ["...", "..."],
  "acceptance_tests": ["...", "..."],
  "summary": "..."
}}
Do not edit any other file. Do not read or print .env contents."""

ACT_PROMPT = """You are the ACT phase of the ApexTrader daily improvement loop. \
Implement EXACTLY the candidate described in {candidate_path} (already \
approved by the controller's evidence gates).

Hard rules:
- Touch ONLY files listed in allowed_files (plus new scripts/test_*.py).
- Do not modify .env, config guardrails, leverage, guardian thresholds, or \
stop protection.
- Add or update a focused scripts/test_*.py suite (script-style asserts, \
print("OK: ...") at the end, no pytest) covering the acceptance_tests.
- Run your new test with: "{venv_py}" scripts\\test_<yourtest>.py
- Do NOT deploy, do NOT write any flag, do NOT commit.

When done, write {act_report_path} containing \
{{"ok": true, "files_changed": [...], "test_file": "...", "notes": "..."}}."""

VERIFY_PROMPT = """You are the VERIFY phase of the ApexTrader daily improvement \
loop. Independently re-check the implementation:
1. Read {candidate_path} and inspect `git status` / `git diff` (read-only).
2. Confirm ONLY allowed_files (plus the new test file) changed, the acceptance \
tests are actually covered, and no prohibited change slipped in.
3. Do NOT edit anything; do NOT deploy.
Write {verify_report_path} containing {{"verdict": "APPROVE", "reasons": [], \
"files_checked": [...]}} (or "REJECT" with reasons)."""


def plan_phase(obs: dict, out_dir: Path,
               now: datetime.datetime | None = None) -> tuple[dict, dict]:
    """PLAN phase. Returns (plan_dict, info). Deterministic fallbacks:
    no CLI / invalid artifact -> OBSERVE_ONLY (fail closed)."""
    info: dict = {"cline": None, "session_ok": False, "output_tail": ""}
    candidate_path = out_dir / "candidate.json"
    try:
        if candidate_path.exists():
            candidate_path.unlink()
    except Exception:
        pass
    cline = find_cline()
    info["cline"] = cline
    if not cline:
        info["output_tail"] = "cline CLI not found"
        return {"decision": "OBSERVE_ONLY", "saturation": False,
                "reason": "cline_cli_unavailable",
                "summary": "Cline CLI not installed; recorded observation only."}, info
    budget = remaining_seconds("plan", now) or CLINE_PLAN_TIMEOUT_S
    prompt = PLAN_PROMPT.format(
        obs_json=json.dumps(redact_obj(obs), default=str)[:6000],
        min_days=GATES["min_trading_days"], min_trades=GATES["min_trades"],
        candidate_path=candidate_path)
    ok, tail = run_cline(prompt, out_dir, "plan", plan_mode=True,
                         timeout_s=max(60, min(CLINE_PLAN_TIMEOUT_S, budget)),
                         cline_path=cline)
    info["session_ok"] = ok
    info["output_tail"] = tail
    try:
        plan = json.loads(candidate_path.read_text(encoding="utf-8"))
        if not isinstance(plan, dict) or "decision" not in plan:
            raise ValueError("bad schema")
        return plan, info
    except Exception as exc:
        info["output_tail"] += f" | candidate.json invalid: {type(exc).__name__}"
        return {"decision": "OBSERVE_ONLY", "saturation": False,
                "reason": "plan_artifact_missing_or_invalid",
                "summary": "Plan agent produced no valid candidate artifact."}, info


def act_phase(plan: dict, out_dir: Path,
              now: datetime.datetime | None = None) -> tuple[bool, dict]:
    """ACT phase: bounded implementation session; report artifact required."""
    info: dict = {"session_ok": False, "output_tail": ""}
    if past_deadline("act", now):
        info["output_tail"] = "act deadline passed"
        return False, info
    cline = find_cline()
    if not cline:
        info["output_tail"] = "cline CLI not found"
        return False, info
    candidate_path = out_dir / "candidate.json"
    act_report = out_dir / "act-report.json"
    budget = remaining_seconds("act", now) or 600
    prompt = ACT_PROMPT.format(candidate_path=candidate_path,
                               act_report_path=act_report, venv_py=str(VENV_PY))
    ok, tail = run_cline(prompt, out_dir, "act", plan_mode=False,
                         timeout_s=max(60, min(CLINE_ACT_TIMEOUT_S, budget)),
                         cline_path=cline)
    info["session_ok"] = ok
    info["output_tail"] = tail
    try:
        report = json.loads(act_report.read_text(encoding="utf-8"))
        info["report"] = report
        return bool(report.get("ok")), info
    except Exception as exc:
        info["output_tail"] += f" | act-report.json invalid: {type(exc).__name__}"
        return False, info


def changed_files(root: Path = ROOT) -> list:
    try:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=str(root),
                           capture_output=True, text=True, timeout=60)
        rows = []
        for ln in (r.stdout or "").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            path = ln[3:].strip().strip('"')
            if path.startswith("->"):
                continue
            rows.append(path.replace("\\", "/"))
        return rows
    except Exception:
        return []


def unexpected_changes(plan: dict, root: Path = ROOT) -> list:
    """Files changed that are neither allowed_files, new scripts/test_*.py,
    nor known runtime noise (never committed per convention #5)."""
    allowed = {str(f).replace("\\", "/") for f in (plan.get("allowed_files") or [])}
    bad = []
    for path in changed_files(root):
        if path in allowed:
            continue
        if path.startswith("scripts/test_") and path.endswith(".py"):
            continue
        if any(path.startswith(p) for p in RUNTIME_NOISE_PREFIXES):
            continue
        if path.endswith((".log", ".pid", ".lock")):
            continue
        bad.append(path)
    return bad


def run_tests(python: str | None = None, root: Path = ROOT) -> tuple[bool, list]:
    """The same gate deploy.py runs: every scripts/test_*.py + compileall.
    --skip-tests is deliberately impossible here."""
    python = python or (str(VENV_PY) if VENV_PY.exists() else sys.executable)
    tests = sorted((root / "scripts").glob("test_*.py"))
    results = []
    if not tests:
        return False, [{"name": "<none>", "ok": False, "tail": "no tests found"}]
    for t in tests:
        try:
            r = subprocess.run([python, str(t)], cwd=str(root), capture_output=True,
                               text=True, timeout=600)
            ok = r.returncode == 0
            tail = "" if ok else ((r.stdout or r.stderr or "").splitlines()[-6:])
        except Exception as exc:
            ok, tail = False, [f"{type(exc).__name__}: {exc}"]
        results.append({"name": t.name, "ok": ok, "tail": tail})
    try:
        r = subprocess.run([python, "-m", "compileall", "-q", "engine", "scripts"],
                           cwd=str(root), capture_output=True, text=True, timeout=600)
        results.append({"name": "compileall", "ok": r.returncode == 0, "tail": []})
    except Exception as exc:
        results.append({"name": "compileall", "ok": False, "tail": [f"{type(exc).__name__}: {exc}"]})
    return all(x["ok"] for x in results), results


def verify_phase(plan: dict, tests_ok: bool, results: list, out_dir: Path,
                 now: datetime.datetime | None = None) -> tuple[bool, list]:
    """Controller-side verification + an independent Cline verify session when
    available. Fails closed on unexpected file changes or test failures."""
    reasons: list = []
    if not tests_ok:
        reasons.append("tests_failed:" + ",".join(r["name"] for r in results if not r["ok"]))
    bad = unexpected_changes(plan)
    if bad:
        reasons.append("unexpected_changes:" + ",".join(bad[:8]))
    cline = find_cline()
    if cline and not past_deadline("test", now):
        verify_report = out_dir / "verify-report.json"
        budget = remaining_seconds("test", now) or 600
        prompt = VERIFY_PROMPT.format(candidate_path=out_dir / "candidate.json",
                                      verify_report_path=verify_report)
        ok, _tail = run_cline(prompt, out_dir, "verify", plan_mode=False,
                              timeout_s=max(60, min(900, budget)), cline_path=cline)
        try:
            report = json.loads(verify_report.read_text(encoding="utf-8"))
            if str(report.get("verdict", "")).upper() != "APPROVE":
                reasons.append("verify_agent_rejected:" + ";".join(report.get("reasons", [])[:4]))
        except Exception:
            if ok:
                reasons.append("verify_report_missing")
    return (not reasons), reasons


def deploy_phase(reason: str, out_dir: Path, python: str | None = None,
                 root: Path = ROOT) -> tuple[bool, str]:
    """Deployment ONLY through the existing test-gated flag writer. The
    watchdog then restarts main.py in its flat window (lunch 11:00-14:15 ET or
    after 15:44 ET) -- a late flag defers to the post-close window by design."""
    python = python or (str(VENV_PY) if VENV_PY.exists() else sys.executable)
    if "--skip-tests" in reason or "skip-tests" in reason:
        return False, "refused: skip-tests in deploy reason"
    if not in_automation_window(et_now()):
        window = "post-15:44-ET window"
    else:
        window = "lunch flat window"
    try:
        r = subprocess.run([python, str(root / "scripts" / "deploy.py"),
                            "--reason", reason],
                           cwd=str(root), capture_output=True, text=True, timeout=3600)
        tail = redact_text(((r.stdout or "") + (r.stderr or ""))[-2000:])
        log(out_dir, f"[DEPLOY] rc={r.returncode} expected-consumption={window}")
        return r.returncode == 0, tail
    except Exception as exc:
        return False, redact_text(f"{type(exc).__name__}: {exc}")


def write_handoff(out_dir: Path, obs: dict, plan: dict, run_state: dict) -> Path:
    """Compact cross-session handoff (bounded) -- the context-compaction
    mechanism between the daily Plan/Act/Verify sessions."""
    lines = [
        "# Compact handoff — daily automation",
        f"- date_et: {run_state.get('date')}",
        f"- decision: {plan.get('decision')}",
        f"- saturation: {plan.get('saturation', False)}",
        f"- gates_ok: {run_state.get('gates', {}).get('ok')}",
        f"- tests_ok: {run_state.get('tests', {}).get('ok')}",
        f"- deployed: {run_state.get('deploy', {}).get('attempted', False)}",
        f"- problem: {str(plan.get('problem', plan.get('summary', '')))[:300]}",
        f"- summary: {str(plan.get('summary', ''))[:500]}",
        f"- allowed_files: {plan.get('allowed_files')}",
        f"- prohibited_changes: {plan.get('prohibited_changes')}",
        f"- acceptance_tests: {plan.get('acceptance_tests')}",
        f"- evidence: {json.dumps(plan.get('evidence', {}), default=str)[:500]}",
        f"- roundtrips: {json.dumps(obs.get('roundtrips', {}), default=str)[:300]}",
        f"- churn_chains: {json.dumps(obs.get('churn_chains', [])[:5], default=str)[:400]}",
        f"- runtime: {json.dumps(obs.get('runtime', {}), default=str)[:300]}",
        f"- gate_reasons: {run_state.get('gates', {}).get('reasons')}",
        "- Next session: read AGENT_CONTEXT.md, newest AGENT_CHECKPOINT.md snapshot, "
        "then this file. Do not re-derive the observation.",
    ]
    lines = lines[:HANDOFF_MAX_LINES]
    path = out_dir / "compact-handoff.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_run_state(out_dir: Path, run_state: dict) -> Path:
    path = out_dir / "run-state.json"
    path.write_text(json.dumps(redact_obj(run_state), indent=2, default=str),
                    encoding="utf-8")
    return path


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--force", "-Force", action="store_true",
                    help="run even outside the ET window (manual/testing runs)")
    ap.add_argument("--offline", "-Offline", action="store_true",
                    help="observation without network (Alpaca unreachable)")
    ap.add_argument("--dry-run", "-DryRun", action="store_true",
                    help="observe + plan only; never implement, test-gate or deploy")
    ap.add_argument("--skip-agent", "-SkipAgent", action="store_true",
                    help="skip Cline sessions (pure observation + artifacts)")
    ap.add_argument("--allow-deploy", "-AllowDeploy", action="store_true",
                    help="permit the test-gated deploy flag write (also "
                         "AUTOMATION_ALLOW_DEPLOY=1); default is observe-only")
    ap.add_argument("--out", default="", help="override artifact directory")
    args = ap.parse_args(argv)

    now = et_now()
    out_dir = Path(args.out) if args.out else (AUTOMATION_DIR / now.date().isoformat())
    out_dir.mkdir(parents=True, exist_ok=True)
    allow_deploy = args.allow_deploy or os.environ.get("AUTOMATION_ALLOW_DEPLOY", "0") == "1"

    run_state: dict = {
        "schema_version": 1,
        "date": now.date().isoformat(),
        "started_et": now.isoformat(),
        "window": {"start": WINDOW_START_ET, "end": WINDOW_END_ET,
                   "in_window": in_automation_window(now), "forced": args.force},
        "mode": {"dry_run": args.dry_run, "skip_agent": args.skip_agent,
                 "offline": args.offline, "allow_deploy": allow_deploy},
        "decision": "SKIPPED", "saturation": False,
        "gates": {"ok": False, "reasons": []},
        "act": {"ran": False, "ok": False},
        "tests": {"ok": False, "failed": []},
        "verify": {"ok": False, "reasons": []},
        "deploy": {"attempted": False, "ok": False, "reason": ""},
        "plan": {},
    }

    log(out_dir, f"[START] mode={run_state['mode']} window={run_state['window']}")
    if not args.force and not in_automation_window(now):
        log(out_dir, "[SKIP] outside ET automation window (12:05-14:00)")
        run_state["decision"] = "OUTSIDE_WINDOW"
        write_run_state(out_dir, run_state)
        return 0

    if not acquire_lock():
        log(out_dir, "[SKIP] another automation run holds the lock")
        run_state["decision"] = "LOCKED"
        write_run_state(out_dir, run_state)
        return 3
    try:
        # ---- OBSERVE -----------------------------------------------------
        try:
            obs = run_observation(days=args.days, offline=args.offline)
        except Exception as exc:
            log(out_dir, f"[ERROR] observation failed: {type(exc).__name__}: {exc}")
            obs = analyzer.build_observation(days=args.days, offline=True)
            obs["notes"].append(f"observation degraded: {type(exc).__name__}")
        analyzer.write_artifacts(obs, out_dir)
        log(out_dir, f"[OBSERVE] rt={obs.get('roundtrips', {}).get('count')} "
                     f"pnl=${obs.get('roundtrips', {}).get('total_pnl')}")

        # ---- PLAN --------------------------------------------------------
        if args.skip_agent:
            plan = {"decision": "OBSERVE_ONLY", "saturation": False,
                    "reason": "skip_agent", "summary": "agent phases skipped by flag"}
            plan_info: dict = {"cline": None}
        else:
            plan, plan_info = plan_phase(obs, out_dir, now)
        run_state["plan"] = {k: plan.get(k) for k in
                             ("decision", "saturation", "reason", "summary", "problem")}
        run_state["saturation"] = bool(plan.get("saturation", False))
        run_state["plan"]["cline"] = plan_info.get("cline")
        (out_dir / "candidate.json").write_text(
            json.dumps(redact_obj(plan), indent=2, default=str), encoding="utf-8")
        log(out_dir, f"[PLAN] decision={plan.get('decision')} "
                     f"saturation={plan.get('saturation', False)} "
                     f"reason={plan.get('reason', '')}")

        # ---- GATE --------------------------------------------------------
        if plan.get("decision") == "IMPLEMENT":
            gates_ok, reasons = evaluate_candidate(plan, obs)
        else:
            gates_ok, reasons = False, [f"decision_{str(plan.get('decision', 'UNKNOWN')).lower()}"]
        run_state["gates"] = {"ok": gates_ok, "reasons": reasons}
        log(out_dir, f"[GATE] ok={gates_ok} reasons={reasons[:6]}")

        run_state = _implement_phase(plan, run_state, out_dir, now, args, allow_deploy)
        run_state["finished_et"] = et_now().isoformat()
        write_handoff(out_dir, obs, plan, run_state)
        write_run_state(out_dir, run_state)
        log(out_dir, f"[DONE] decision={run_state['decision']}")
        return 2 if run_state["decision"] == "IMPLEMENT_BLOCKED" else 0
    finally:
        release_lock()


def _implement_phase(plan: dict, run_state: dict, out_dir: Path,
                     now: datetime.datetime, args, allow_deploy: bool) -> dict:
    """ACT -> TEST -> VERIFY -> DEPLOY, every step fail-closed."""
    if plan.get("decision") != "IMPLEMENT" or not run_state["gates"]["ok"]:
        run_state["decision"] = ("CANDIDATE_REJECTED"
                                 if plan.get("decision") == "IMPLEMENT" else "OBSERVE_ONLY")
        return run_state
    if args.dry_run:
        run_state["decision"] = "DRY_RUN_CANDIDATE_OK"
        log(out_dir, "[DRY-RUN] candidate passed gates; no implementation run")
        return run_state
    act_ok, act_info = act_phase(plan, out_dir, now)
    run_state["act"] = {"ran": True, "ok": act_ok,
                        "tail": act_info.get("output_tail", "")[:600]}
    log(out_dir, f"[ACT] ok={act_ok}")
    if not act_ok:
        run_state["decision"] = "IMPLEMENT_BLOCKED"
        return run_state
    tests_ok, results = run_tests()
    run_state["tests"] = {"ok": tests_ok,
                          "failed": [r["name"] for r in results if not r["ok"]]}
    (out_dir / "test-results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8")
    log(out_dir, f"[TEST] ok={tests_ok} failed={run_state['tests']['failed'][:8]}")
    if not tests_ok:
        run_state["decision"] = "IMPLEMENT_BLOCKED"
        return run_state
    verify_ok, vreasons = verify_phase(plan, tests_ok, results, out_dir, now)
    run_state["verify"] = {"ok": verify_ok, "reasons": vreasons}
    log(out_dir, f"[VERIFY] ok={verify_ok} reasons={vreasons[:6]}")
    if not verify_ok:
        run_state["decision"] = "IMPLEMENT_BLOCKED"
        return run_state
    if allow_deploy:
        ok, tail = deploy_phase(
            f"daily evidence-gated improvement: {str(plan.get('problem', ''))[:120]}",
            out_dir)
        run_state["deploy"] = {"attempted": True, "ok": ok, "reason": tail[-400:]}
        run_state["decision"] = "DEPLOYED" if ok else "IMPLEMENT_BLOCKED"
        log(out_dir, f"[DEPLOY] ok={ok}")
    else:
        run_state["deploy"]["reason"] = ("deploy_not_enabled "
                                         "(set AUTOMATION_ALLOW_DEPLOY=1 or --allow-deploy)")
        run_state["decision"] = "IMPLEMENT_TESTED_NOT_DEPLOYED"
        log(out_dir, f"[DEPLOY] {run_state['deploy']['reason']}")
    return run_state


if __name__ == "__main__":
    sys.exit(main())





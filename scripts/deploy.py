"""ApexTrader -- auto-deploy gate (2026-09-02).

Runs the full offline test gate (every scripts/test_*.py + compileall) and only
if ALL green writes deploy_requested.flag into the LOCAL state dir
(%LOCALAPPDATA%\\ApexTrader\\state). The watchdog (engine/watchdog.py) polls
that flag and restarts main.py on the new code -- but only inside the flat
deploy windows (lunch 11:00-14:15 ET rule-enforced flat, or after the 15:44 ET
EOD flat until 09:05 ET next prep). Outside those windows the flag stays and
the watchdog applies it at the next window.

This is the "zero-PowerShell, test-gated" deploy path:
  1. agent edits code + runs tests here
  2. agent (or user) runs:  python scripts/deploy.py --reason "..."
  3. watchdog restarts main.py on the new code within ~2s of the next window

Usage:
  python scripts/deploy.py --reason "close the re-entry daily-loss gap"
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(__import__("os").environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ApexTrader" / "state"
FLAG_FILE = STATE_DIR / "deploy_requested.flag"


def run(python: str, argv: list) -> subprocess.CompletedProcess:
    return subprocess.run([python, *argv], cwd=str(ROOT), capture_output=True, text=True, timeout=1800)


def test_gate(python: str) -> bool:
    """Every scripts/test_*.py must pass; engine/scripts must compile."""
    tests = sorted((ROOT / "scripts").glob("test_*.py"))
    if not tests:
        print("[GATE] no tests found -- refusing to deploy untested code")
        return False
    failed = []
    for t in tests:
        r = run(python, [str(t)])
        if r.returncode != 0:
            failed.append(t.name)
            tail = "\n".join((r.stdout or r.stderr or "").splitlines()[-12:])
            print(f"[GATE] FAIL {t.name}:\n{tail}")
        else:
            print(f"[GATE] ok {t.name}")
    rc = run(python, ["-m", "compileall", "-q", "engine", "scripts"])
    if rc.returncode != 0:
        failed.append("compileall")
    print(f"[GATE] {len(tests) - len([f for f in failed if f != 'compileall'])}/{len(tests)} tests + compileall "
          f"{'OK' if not failed else 'FAILED: ' + ', '.join(failed)}")
    return not failed


def git_head() -> str:
    try:
        r = run("git", ["rev-parse", "--short", "HEAD"])
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reason", default="", help="what this deploy changes (logged in autobot.log)")
    ap.add_argument("--skip-tests", action="store_true", help="DANGER: skip the test gate")
    args = ap.parse_args()

    python = sys.executable
    if not args.skip_tests and not test_gate(python):
        print("[DEPLOY] test gate failed -- no flag written. Use --skip-tests only if you know why.")
        return 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    content = f"{int(time.time())},{args.reason or 'no reason given'},git={git_head()}"
    FLAG_FILE.write_text(content, encoding="utf-8")
    print(f"[DEPLOY] flag written -> {FLAG_FILE}")
    print(f"[DEPLOY] watchdog will restart main.py on the new code at the next flat deploy window "
          f"(11:00-14:15 ET / after 15:44 ET). No PowerShell needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

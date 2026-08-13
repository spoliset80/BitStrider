
import argparse
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Path for the lock file to prevent double-running
LOCK_FILE = Path(__file__).parent / ".mainbot.lock"


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil
        if not psutil.pid_exists(pid):
            return False
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        # Guard against pure PID-number reuse by an unrelated process.
        return "python" in proc.name().lower()
    except Exception:
        return False


def handle_lock():
    """Checks for an existing lock file and creates one if it doesn't exist."""
    pid = str(os.getpid())
    if LOCK_FILE.exists():
        existing_pid = None
        try:
            existing_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            existing_pid = None

        if existing_pid and _is_pid_running(existing_pid):
            print("Another instance is already running. Exiting.")
            sys.exit(0)

        # Remove stale or invalid lock file and continue.
        LOCK_FILE.unlink(missing_ok=True)

    try:
        with LOCK_FILE.open("x", encoding="utf-8") as fh:
            fh.write(pid)
    except FileExistsError:
        print("Another instance is already running. Exiting.")
        sys.exit(0)

    import atexit
    atexit.register(lambda: LOCK_FILE.unlink(missing_ok=True))


def main():
    # 1. Load credentials
    load_dotenv()

    # 1a. Ensure we are running under the machine-local ApexTrader virtualenv,
    # if it exists. Machine-local (not inside this OneDrive-synced repo) to
    # match watchdog.py / run_local_ps.ps1 — see engine/watchdog.py's VENV_DIR
    # comment for why a repo-local venv breaks across synced machines.
    managed_venv = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ApexTrader" / "venv" / "Scripts" / "python.exe"
    if managed_venv.exists() and Path(sys.executable).resolve() != managed_venv.resolve():
        print(
            f"ERROR: Please run ApexTrader using {managed_venv}.\n"
            f"Current interpreter: {sys.executable}"
        )
        sys.exit(1)

    # 2. Prevent duplicate runs
    handle_lock()

    # 3. Setup CLI Arguments
    parser = argparse.ArgumentParser(description="ApexTrader")
    parser.add_argument("--once", action="store_true", help="Single scan and exit")
    parser.add_argument("--force", action="store_true", help="Bypass market hours")
    parser.add_argument("--top3-only", action="store_true", help="Report only, no trades")
    args = parser.parse_args()

    # 4. Delegate to the actual logic
    from engine.orchestrator import run
    print(f"Starting Orchestrator (Mode: {os.getenv('TRADE_MODE', 'paper')})")
    run(force=args.force, once=args.once, top3_only=args.top3_only)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

import os
import sys
import time
import subprocess
import logging
import threading
import atexit
import smtplib
import datetime
from email.message import EmailMessage
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MAIN_SCRIPT = BASE_DIR / "main.py"
# Machine-local, deliberately NOT inside the OneDrive-synced project folder.
# A venv's pyvenv.cfg bakes in an absolute path to the base Python install;
# when the venv itself lived in BASE_DIR ("apextrader/"), OneDrive would sync
# one machine's pyvenv.cfg over another's and break python.exe on whichever
# machine synced second ("No Python at ..."). Each machine now builds and
# keeps its own venv here, so syncing the repo can never clobber it.
VENV_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ApexTrader" / "venv"
REQUIREMENTS_FILE = BASE_DIR / "requirements.txt"
ENV_FILE = BASE_DIR / ".env"
LOG_FILE = BASE_DIR / "autobot.log"
PID_FILE = BASE_DIR / "autobot.pid"
BOOTSTRAP_MARKER = VENV_DIR / ".bootstrapped"
RESTART_BACKOFF_SECONDS = [5, 10, 20, 30, 60]

# engine/orchestrator.py writes this file's contents fresh on every completed
# main-loop iteration. If it goes stale, the trading engine has stopped doing
# anything useful — whether crash-looping or just hung — regardless of whether
# this watchdog's own crash-restart logic thinks everything is fine. This is
# what the 2026-07-09 -> 2026-07-19 ten-day silent outage needed: the watchdog
# DID eventually repair the broken venv, but nothing ever surfaced that trading
# had actually stopped for over a week.
HEARTBEAT_FILE = BASE_DIR / "heartbeat.txt"
HEARTBEAT_STALE_SECONDS = 3600   # alert if no completed cycle in over an hour
HEARTBEAT_CHECK_INTERVAL = 300   # poll every 5 min — no need for tighter granularity at an hour-scale threshold


class AutoBotWatchdog:
    def __init__(self) -> None:
        self.logger = self._setup_logging()

    def _setup_logging(self) -> logging.Logger:
        logger = logging.getLogger("autobot")
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")

            # LOG_FILE lives inside a OneDrive-synced folder, so opening it can
            # transiently fail (cloud placeholder rehydration, sync lock, etc.)
            # even though the path is valid. Retry briefly rather than letting a
            # one-off I/O hiccup kill the whole watchdog before it starts.
            fh = None
            last_exc: Exception | None = None
            for attempt in range(5):
                try:
                    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
                    break
                except OSError as exc:
                    last_exc = exc
                    time.sleep(2 * (attempt + 1))
            if fh is not None:
                fh.setFormatter(formatter)
                logger.addHandler(fh)

            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(formatter)
            logger.addHandler(sh)

            if fh is None:
                logger.warning("Could not open log file %s (%s) — logging to console only", LOG_FILE, last_exc)
        return logger

    def _run_command(self, command: list[str]) -> None:
        subprocess.run(command, check=True)

    def _parse_env_file(self) -> dict[str, str]:
        env = {}
        if not ENV_FILE.exists():
            return env
        for raw_line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
        return env

    def _python_is_healthy(self, python_exe: Path) -> bool:
        try:
            result = subprocess.run(
                [str(python_exe), "--version"],
                capture_output=True, text=True, timeout=15,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _repair_pyvenv_home(self, venv_dir: Path) -> None:
        # This project syncs across machines via OneDrive. pyvenv.cfg's "home"
        # is an absolute path to the base Python install baked in when the
        # venv was created on one specific machine — it breaks (venv's
        # python.exe fails with "No Python at ...") on any other machine, or
        # after a user profile rename. Repoint it at whatever interpreter is
        # currently running this watchdog, since that one is known-good here.
        cfg_path = venv_dir / "pyvenv.cfg"
        if not cfg_path.exists():
            return
        new_home = str(Path(sys.executable).resolve().parent)
        lines = cfg_path.read_text(encoding="utf-8").splitlines()
        rewritten = [
            f"home = {new_home}" if line.split("=", 1)[0].strip().lower() == "home" else line
            for line in lines
        ]
        cfg_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
        self.logger.warning("Repaired %s: home -> %s", cfg_path, new_home)

    def _ensure_virtualenv(self) -> Path:
        if not VENV_DIR.exists():
            self.logger.info("Creating virtualenv at %s", VENV_DIR)
            VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
            self._run_command([sys.executable, "-m", "venv", str(VENV_DIR)])

        python_exe = VENV_DIR / "Scripts" / "python.exe"
        if not python_exe.exists():
            raise RuntimeError(f"Unable to locate virtualenv Python at {python_exe}")

        if not self._python_is_healthy(python_exe):
            self.logger.warning(
                "Venv Python at %s failed health check — repairing pyvenv.cfg", python_exe
            )
            self._repair_pyvenv_home(VENV_DIR)
            if not self._python_is_healthy(python_exe):
                raise RuntimeError(f"Venv Python at {python_exe} still unhealthy after repair attempt")

        if not BOOTSTRAP_MARKER.exists():
            self.logger.info("Bootstrapping requirements into ApexTrader venv")
            self._run_command([str(python_exe), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
            if REQUIREMENTS_FILE.exists():
                self._run_command([str(python_exe), "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE)])
            BOOTSTRAP_MARKER.write_text("bootstrapped\n", encoding="utf-8")

        return python_exe

    def _write_pid(self) -> None:
        try:
            PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
            atexit.register(lambda: PID_FILE.unlink(missing_ok=True))
        except Exception as exc:
            self.logger.warning("Unable to write PID file: %s", exc)

    def kill_existing_project_processes(self) -> None:
        try:
            import psutil
        except ImportError:
            return

        me = os.getpid()
        for proc in psutil.process_iter(["pid", "cmdline"]):
            if proc.info["pid"] == me:
                continue
            try:
                cmdline = proc.info["cmdline"] or []
                joined = " ".join(str(x) for x in cmdline)
                if str(MAIN_SCRIPT) in joined or str(Path(__file__)) in joined:
                    self.logger.info("Killing duplicate process %s %s", proc.info["pid"], joined)
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def _drain_subprocess_output(self, process: subprocess.Popen) -> threading.Thread:
        def _drain() -> None:
            try:
                if process.stdout is None:
                    return
                for line in process.stdout:
                    self.logger.info("[main] %s", line.rstrip())
            except Exception:
                pass

        thread = threading.Thread(target=_drain, daemon=True)
        thread.start()
        return thread

    def get_current_trade_mode(self, env: dict[str, str]) -> str:
        return (env.get("TRADE_MODE", "paper") or "paper").strip().lower()

    # -- Stall Alerting ------------------------------------------------------
    # Deliberately stdlib-only (smtplib/email), reading SMTP settings straight
    # out of .env — this watchdog runs on the bare system Python, not the
    # managed venv, so it can't import engine.notifications (pulls in pandas,
    # alpaca-py, etc. via engine.utils, which may not be installed here).
    def _send_alert_email(self, env: dict[str, str], subject: str, text: str) -> None:
        if env.get("USE_EMAIL_NOTIFICATIONS", "false").strip().lower() not in ("1", "true", "yes"):
            self.logger.error("Email alerts disabled (USE_EMAIL_NOTIFICATIONS) — alert was: %s", subject)
            return
        to_addresses = [a.strip() for a in env.get("EMAIL_TO_ADDRESSES", "").split(",") if a.strip()]
        from_address = env.get("EMAIL_FROM_ADDRESS") or env.get("EMAIL_SMTP_USER")
        smtp_user = env.get("EMAIL_SMTP_USER")
        smtp_pass = env.get("EMAIL_SMTP_PASSWORD")
        if not (to_addresses and from_address and smtp_user and smtp_pass):
            self.logger.error("Email alert config incomplete (EMAIL_TO_ADDRESSES/EMAIL_SMTP_USER/EMAIL_SMTP_PASSWORD) — alert was: %s", subject)
            return
        smtp_server = env.get("EMAIL_SMTP_SERVER", "smtp.gmail.com") or "smtp.gmail.com"
        smtp_port = int(env.get("EMAIL_SMTP_PORT", "587") or "587")
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = from_address
            msg["To"] = ", ".join(to_addresses)
            msg.set_content(text)
            with smtplib.SMTP(smtp_server, smtp_port, timeout=15) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            self.logger.info("Alert email sent: %s", subject)
        except Exception as exc:
            self.logger.error("Failed to send alert email (%s): %s", subject, exc)

    def _heartbeat_age_seconds(self) -> float | None:
        """Seconds since engine/orchestrator.py last wrote HEARTBEAT_FILE, or
        None if it doesn't exist yet (e.g. still starting up, or crash-looping
        before ever reaching a completed cycle)."""
        if not HEARTBEAT_FILE.exists():
            return None
        try:
            last = datetime.datetime.fromisoformat(HEARTBEAT_FILE.read_text(encoding="utf-8").strip())
            if last.tzinfo is None:
                last = last.replace(tzinfo=datetime.timezone.utc)
            return (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds()
        except Exception as exc:
            self.logger.warning("heartbeat.txt unreadable: %s", exc)
            return None

    def _heartbeat_monitor(self) -> None:
        """Runs for the life of the watchdog, independent of any single main.py
        subprocess — covers both a crash-loop (heartbeat never written) and a
        hung-but-alive process (heartbeat stops advancing). Alerts once per
        stall, then goes quiet until the heartbeat actually recovers."""
        alerted = False
        started = time.monotonic()
        while True:
            time.sleep(HEARTBEAT_CHECK_INTERVAL)
            age = self._heartbeat_age_seconds()
            past_startup_grace = (time.monotonic() - started) > HEARTBEAT_STALE_SECONDS
            stale = (age is not None and age > HEARTBEAT_STALE_SECONDS) or (age is None and past_startup_grace)

            if stale and not alerted:
                age_desc = f"{age / 60:.0f} min ago" if age is not None else "never (no heartbeat file written since watchdog start)"
                text = (
                    f"AutoBot has not completed a scan cycle in over {HEARTBEAT_STALE_SECONDS // 60} minutes "
                    f"(last heartbeat: {age_desc}). It may be crash-looping or hung — check {LOG_FILE}."
                )
                self.logger.error(text)
                env = os.environ.copy()
                env.update(self._parse_env_file())
                self._send_alert_email(env, "ApexTrader ALERT: bot appears stalled", text)
                alerted = True
            elif not stale and alerted:
                self.logger.info("Heartbeat recovered — clearing stall alert")
                alerted = False

    def run(self) -> None:
        self.logger.info("Starting AutoBot watchdog")
        self._write_pid()
        last_env_mtime = ENV_FILE.stat().st_mtime if ENV_FILE.exists() else None
        restart_count = 0

        self.kill_existing_project_processes()
        threading.Thread(target=self._heartbeat_monitor, daemon=True).start()

        while True:
            # Re-checked every cycle, not just at startup: this folder syncs
            # via OneDrive across machines, so another machine's write can
            # clobber pyvenv.cfg's "home" mid-run and break the venv Python
            # after the loop is already going.
            python_exe = self._ensure_virtualenv()
            env = os.environ.copy()
            env.update(self._parse_env_file())
            env["TRADE_MODE"] = self.get_current_trade_mode(env)
            # Force UTF-8 stdio in the child. Without this, main.py's stdout is a
            # pipe (not a console), so Windows falls back to the system codepage
            # (cp1252) — any log message with a non-Latin-1 character (e.g. "≥")
            # raises UnicodeEncodeError mid-write and aborts whatever was logging.
            env["PYTHONUTF8"] = "1"

            mode = env["TRADE_MODE"]
            self.logger.info("Launching main.py in %s mode", mode.upper())

            process = subprocess.Popen(
                [str(python_exe), str(MAIN_SCRIPT), "--force"],
                cwd=str(BASE_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            start_time = time.monotonic()
            self._drain_subprocess_output(process)

            while process.poll() is None:
                time.sleep(2)
                current_mtime = ENV_FILE.stat().st_mtime if ENV_FILE.exists() else None
                if current_mtime != last_env_mtime:
                    self.logger.info("Detected .env change, restarting main.py")
                    last_env_mtime = current_mtime
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        self.logger.warning("main.py did not exit within 15s of terminate(), killing")
                        process.kill()
                        process.wait(timeout=15)
                    break

            runtime = time.monotonic() - start_time
            exit_code = process.poll()
            self.logger.warning("main.py exited with code %s after %.1f seconds", exit_code, runtime)

            if runtime < 30:
                restart_count += 1
            else:
                restart_count = 0

            backoff = RESTART_BACKOFF_SECONDS[min(restart_count, len(RESTART_BACKOFF_SECONDS) - 1)]
            self.logger.info("Restarting in %s seconds", backoff)
            time.sleep(backoff)

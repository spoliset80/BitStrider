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
# 2026-09-02: watchdog log + pid moved OUT of the OneDrive repo to the local
# ApexTrader dir. autobot.log in OneDrive threw OSError [Errno 22] on every
# write; each failed emit made logging print a full traceback to stderr,
# bloating autobot_scheduler.log to 375MB and choking the drain thread that
# reads main.py's stdout -- which filled the bot's stdout pipe and froze the
# bot's logging lock (see engine/utils/data.py setup_logging notes).
LOCAL_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ApexTrader"
LOG_DIR = LOCAL_DIR / "logs"
LOG_FILE = LOG_DIR / "autobot.log"
PID_FILE = LOG_DIR / "autobot.pid"
# 2026-09-02: agentic auto-deploy. The agent/user writes a flag file into the
# LOCAL (non-synced) ApexTrader state dir; this watchdog consumes it and
# restarts main.py on the new code. Restart is deferred to windows when the
# book is guaranteed flat (lunch 11:00-14:15 ET rule-enforced flat, and after
# the 15:44 EOD flat until next prep at 09:05 ET). Same LOCALAPPDATA rationale
# as VENV_DIR: coordination files must never sync via OneDrive.
STATE_DIR = LOCAL_DIR / "state"
DEPLOY_FLAG_FILE = STATE_DIR / "deploy_requested.flag"
# 2026-09-02: DEPLOY_RESTART_ENABLED kill switch. Read LIVE on every deploy
# check via AutoBotWatchdog._deploy_restart_enabled() (os.environ merged with
# .env) instead of a frozen module constant -- a module-level True made
# "DEPLOY_RESTART_ENABLED=false in .env" a no-op. Default stays enabled.
BOOTSTRAP_MARKER = VENV_DIR / ".bootstrapped"
RESTART_BACKOFF_SECONDS = [5, 10, 20, 30, 60]

# engine/orchestrator.py writes this file's contents fresh on every completed
# main-loop iteration. If it goes stale, the trading engine has stopped doing
# anything useful -- whether crash-looping or just hung -- regardless of whether
# this watchdog's own crash-restart logic thinks everything is fine. This is
# what the 2026-07-09 -> 2026-07-19 ten-day silent outage needed: the watchdog
# DID eventually repair the broken venv, but nothing ever surfaced that trading
# had actually stopped for over a week.
HEARTBEAT_FILE = BASE_DIR / "heartbeat.txt"
HEARTBEAT_STALE_SECONDS = 3600   # alert if no completed cycle in over an hour
HEARTBEAT_CHECK_INTERVAL = 300   # poll every 5 min -- no need for tighter granularity at an hour-scale threshold
# 2026-09-02: a hung-but-alive main.py never exits, so the crash loop below
# never fires -- only the (1h) stall alert does. Confirmed live: a yfinance/
# Alpaca bar fetch with no HTTP timeout black-holed the main loop ~20s into
# pre-market prewarm; log frozen, CPU 0, heartbeat never written, nothing
# auto-recovered. If the heartbeat stalls past STALL_RESTART_SECONDS AND the
# current time is in a guaranteed-flat window (11:00-14:15 / after 15:44 ET),
# the watchdog now terminates and relaunches main.py itself. Outside flat
# windows it deliberately does NOT restart (open positions may be mid-cycle)
# and the existing 1h stall alert carries the signal instead.
STALL_RESTART_SECONDS = int(os.getenv("STALL_RESTART_SECONDS", "900"))


class AutoBotWatchdog:
    def __init__(self) -> None:
        self.logger = self._setup_logging()

    def _setup_logging(self) -> logging.Logger:
        logger = logging.getLogger("autobot")
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")

            # 2026-09-02: LOG_FILE is now machine-local (LOCALAPPDATA) so the
            # OneDrive rehydration/sync lock class of failures is gone; the
            # retry stays as cheap insurance for any local I/O hiccup.
            fh = None
            last_exc: Exception | None = None
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                last_exc = exc
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
                logger.warning("Could not open log file %s (%s) -- logging to console only", LOG_FILE, last_exc)
        return logger

    def _run_command(self, command: list[str]) -> None:
        subprocess.run(command, check=True)

    @staticmethod
    def _env_hash() -> str | None:
        """SHA-256 of .env's raw bytes, or None when the file is missing.
        Content-hash (not mtime) so OneDrive sync touches that rewrite the
        file identically do NOT trigger a main.py restart."""
        import hashlib
        try:
            return hashlib.sha256(ENV_FILE.read_bytes()).hexdigest()
        except OSError:
            return None

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
        # venv was created on one specific machine -- it breaks (venv's
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
                "Venv Python at %s failed health check -- repairing pyvenv.cfg", python_exe
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

    def _another_instance_alive(self) -> bool:
        """Check PID_FILE for a live watchdog before touching anything.

        Task Scheduler's own IgnoreNew policy only tracks instances IT
        launched -- it has no visibility into a watchdog started manually in
        a foreground terminal (confirmed necessary 2026-08-05, running one
        of each at once as a reboot/on-demand-trigger workaround). Without
        this check, a scheduled backstop trigger would kill the foreground
        session's main.py via kill_existing_project_processes(), the
        foreground watchdog would then relaunch its own, and both processes
        would race to trade the same live account.
        """
        if not PID_FILE.exists():
            return False
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return False
        if pid == os.getpid():
            return False
        try:
            import psutil
            proc = psutil.Process(pid)
            cmdline = " ".join(proc.cmdline())
        except Exception:
            return False  # stale/unreadable PID file -- proceed, we'll take over
        return "autobot.py" in cmdline or str(Path(__file__)) in cmdline

    def kill_existing_project_processes(self) -> None:
        try:
            import psutil
        except ImportError:
            return

        me = os.getpid()
        try:
            procs = list(psutil.process_iter(["pid", "cmdline"]))
        except Exception as e:
            # Confirmed 2026-08-05: this whole function running at the top of
            # run(), uncaught, is a plausible cause of the watchdog dying
            # within ~1s of an on-demand restart with no error ever reaching
            # the log -- a day's worth of stray cross-session Edge/driver
            # processes (from unrelated browser-automation testing) sitting
            # around by then made a psutil failure on *some* process far more
            # likely than at a clean morning boot. Never let this function
            # take the whole watchdog down.
            self.logger.warning("kill_existing_project_processes: process_iter failed: %s", e)
            return

        for proc in procs:
            if proc.info["pid"] == me:
                continue
            try:
                cmdline = proc.info["cmdline"] or []
                joined = " ".join(str(x) for x in cmdline)
                if str(MAIN_SCRIPT) in joined or str(Path(__file__)) in joined:
                    self.logger.info("Killing duplicate process %s %s", proc.info["pid"], joined)
                    proc.kill()
            except Exception as e:
                # Was (psutil.NoSuchProcess, psutil.AccessDenied) only -- too
                # narrow. A single unreachable cross-session process (e.g. the
                # elevated/S4U Edge automation processes from a different
                # logon session) must never abort the whole scan.
                self.logger.debug("kill_existing_project_processes: skipping pid %s: %s", proc.info.get("pid"), e)
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
    # out of .env -- this watchdog runs on the bare system Python, not the
    # managed venv, so it can't import engine.notifications (pulls in pandas,
    # alpaca-py, etc. via engine.utils, which may not be installed here).
    def _send_alert_email(self, env: dict[str, str], subject: str, text: str) -> None:
        if env.get("USE_EMAIL_NOTIFICATIONS", "false").strip().lower() not in ("1", "true", "yes"):
            self.logger.error("Email alerts disabled (USE_EMAIL_NOTIFICATIONS) -- alert was: %s", subject)
            return
        to_addresses = [a.strip() for a in env.get("EMAIL_TO_ADDRESSES", "").split(",") if a.strip()]
        from_address = env.get("EMAIL_FROM_ADDRESS") or env.get("EMAIL_SMTP_USER")
        smtp_user = env.get("EMAIL_SMTP_USER")
        smtp_pass = env.get("EMAIL_SMTP_PASSWORD")
        if not (to_addresses and from_address and smtp_user and smtp_pass):
            self.logger.error("Email alert config incomplete (EMAIL_TO_ADDRESSES/EMAIL_SMTP_USER/EMAIL_SMTP_PASSWORD) -- alert was: %s", subject)
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
        subprocess -- covers both a crash-loop (heartbeat never written) and a
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
                    f"(last heartbeat: {age_desc}). It may be crash-looping or hung -- check {LOG_FILE}."
                )
                self.logger.error(text)
                env = os.environ.copy()
                env.update(self._parse_env_file())
                self._send_alert_email(env, "ApexTrader ALERT: bot appears stalled", text)
                alerted = True
            elif not stale and alerted:
                self.logger.info("Heartbeat recovered -- clearing stall alert")
                alerted = False

    @staticmethod
    def _now_et() -> datetime.datetime:
        """Current time in America/New_York using stdlib zoneinfo (this file
        must stay stdlib-only: it bootstraps the venv and runs under whatever
        interpreter Task Scheduler hands it)."""
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York"))

    def _deploy_window_allows(self, now_et: datetime.datetime) -> bool:
        """Restart-on-deploy only when the book is guaranteed flat:
          - lunch break 11:00-14:15 ET (rule-enforced flat, LUNCH_FLAT_TIME_ET),
          - after the 15:44 ET EOD flat through 09:05 ET next prep scan.
        Outside those, a mid-session deploy could race an open position, so the
        flag is left in place and the restart defers until a window opens."""
        hm = now_et.hour * 60 + now_et.minute
        lunch_flat = (11 * 60, 14 * 60 + 15)      # 11:00-14:15 ET (afternoon reopen moved 14:45->14:15, 2026-09-04)
        if lunch_flat[0] <= hm < lunch_flat[1]:
            return True
        eod_to_prep = (15 * 60 + 44, 9 * 60 + 5)  # after 15:44 ET (EOD close, 2026-09-03 (2nd): was 15:45) -> 09:05 ET next day
        if eod_to_prep[0] < hm or hm < eod_to_prep[1]:
            return True
        return False

    def _consume_deploy_flag(self) -> str | None:
        try:
            if not DEPLOY_FLAG_FILE.exists():
                return None
            content = DEPLOY_FLAG_FILE.read_text(encoding="utf-8").strip()
            DEPLOY_FLAG_FILE.unlink(missing_ok=True)
            return content or "(no reason)"
        except Exception as exc:
            self.logger.error("[DEPLOY] flag read/unlink failed: %s", exc)
            return None

    def _stop_child(self, process: subprocess.Popen) -> None:
        """Terminate main.py gracefully (15s grace) then hard-kill. Shared by
        the .env-change restart and the deploy-flag restart."""
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.logger.warning("main.py did not exit within 15s of terminate(), killing")
            process.kill()
            process.wait(timeout=15)

    _ENV_TRUE = {"1", "true", "yes", "on"}
    _ENV_FALSE = {"0", "false", "no", "off"}

    def _deploy_restart_enabled(self) -> bool:
        """Kill switch for the deploy-flag restart, read live on every check.

        Merges os.environ with .env (same precedence as the child-env merge in
        _run_loop: .env wins) and parses DEPLOY_RESTART_ENABLED. Documented
        semantics (asserted in scripts/test_guardian_and_deploy.py):
          - missing/blank value -> ENABLED (default, matching the module-level
            True constant this method replaces);
          - 0/false/no/off (case-insensitive) -> deploy restarts DISABLED;
          - 1/true/yes/on -> enabled;
          - any other value -> treated as enabled with a one-time warning:
            only an EXPLICIT false disables, so a typo can never silently
            switch off the auto-deploy path.
        """
        merged = dict(os.environ)
        try:
            merged.update(self._parse_env_file())
        except Exception:
            pass  # unreadable .env -> fall back to process env / default
        raw_value = merged.get("DEPLOY_RESTART_ENABLED")
        raw = str(raw_value or "").strip().strip('"').strip("'").lower()
        if raw in self._ENV_FALSE:
            return False
        if raw and raw not in self._ENV_TRUE:
            if not getattr(self, "_deploy_env_value_warned", False):
                self._deploy_env_value_warned = True
                self.logger.warning(
                    "DEPLOY_RESTART_ENABLED=%r is not a recognized boolean "
                    "(1/true/yes/on or 0/false/no/off) -- treating as enabled",
                    raw_value,
                )
        return True

    def _deploy_restart_requested(self, process: subprocess.Popen) -> bool:
        """Return True when a deploy restart was just performed; the caller must
        break its wait loop so _run_loop relaunches main.py on the new code.

        Kill switch (.env DEPLOY_RESTART_ENABLED, see _deploy_restart_enabled)
        off, or no flag present -> False.
        Flag present + flat window -> consume flag, stop child, restart.
        Flag present + NOT flat window -> log a deferral and LEAVE the flag so
        the next cycle (2s later) retries until a window opens."""
        if not self._deploy_restart_enabled() or not DEPLOY_FLAG_FILE.exists():
            return False
        now_et = self._now_et()
        if not self._deploy_window_allows(now_et):
            self.logger.info(
                "[DEPLOY] flag present at %s ET -- outside flat deploy windows (11:00-14:15 / after 15:44 ET); deferring",
                now_et.strftime("%H:%M"),
            )
            return False
        reason = self._consume_deploy_flag()
        self.logger.warning("[DEPLOY] deploy flag consumed (%s) -- restarting main.py on new code", reason)
        self._stop_child(process)
        return True

    def _stall_restart_requested(self, process: subprocess.Popen, start_time: float) -> bool:
        """Terminate + relaunch main.py when it has been alive past
        STALL_RESTART_SECONDS without writing a heartbeat -- but only inside a
        guaranteed-flat window (_deploy_window_allows). A hung-but-alive bot
        never trips the crash loop, so without this nothing would auto-recover
        (confirmed 2026-09-02: unbounded bar fetch wedged the main loop; only
        the 1h stall alert existed and it never restarts anything). Returns
        True when a restart was just performed; the caller breaks its wait
        loop so _run_loop relaunches main.py."""
        uptime = time.monotonic() - start_time
        if uptime <= STALL_RESTART_SECONDS:
            return False
        age = self._heartbeat_age_seconds()
        if age is not None and age <= STALL_RESTART_SECONDS:
            self._stall_not_flat_logged = False  # heartbeat recovered
            return False
        now_et = self._now_et()
        # 2026-09-02: a morning freeze with a FLAT book must also self-heal --
        # 09:14-09:30 ET is outside the flat deploy windows, but restarting a
        # bot with zero positions is always safe. Trust guardian_state.json's
        # position count only when it is FRESH (guardian writes it every
        # minute in its action band); a stale/missing file falls back to the
        # flat-window-only rule.
        positions = -1
        try:
            import json as _json
            gs = _json.loads(STATE_DIR.joinpath("guardian_state.json").read_text(encoding="utf-8"))
            positions = int(gs.get("positions", -1))
            last = datetime.datetime.fromisoformat(gs.get("last_run", ""))
            if last.tzinfo is None:
                last = last.replace(tzinfo=datetime.timezone.utc)
            if abs((datetime.datetime.now(datetime.timezone.utc) - last).total_seconds()) > 300:
                positions = -1  # stale guardian data -> don't trust the 0
        except Exception:
            positions = -1
        flat_book = positions == 0
        if not (self._deploy_window_allows(now_et) or flat_book):
            if not getattr(self, "_stall_not_flat_logged", False):
                self.logger.error(
                    "[STALL] heartbeat stale (%s) after %.0f min uptime at %s ET -- not in a flat window and "
                    "book not confirmed flat (positions=%s); not auto-restarting, the 1h stall alert stays armed",
                    (f"{age / 60:.0f} min" if age is not None else "never written"),
                    uptime / 60, now_et.strftime("%H:%M"), positions,
                )
                self._stall_not_flat_logged = True
            return False
        self._stall_not_flat_logged = False
        self.logger.warning(
            "[STALL] heartbeat stale (%s) after %.0f min uptime -- %s; restarting hung main.py",
            (f"{age / 60:.0f} min" if age is not None else "never written"), uptime / 60,
            "flat window" if self._deploy_window_allows(now_et) else f"flat book (positions={positions})",
        )
        self._stop_child(process)
        return True

    def run(self) -> None:
        """Thin outer supervisor around _run_loop(): if anything inside ever
        raises uncaught, log the full traceback (previously nothing did --
        confirmed 2026-08-05, the watchdog died twice with zero error output
        anywhere, only Task Scheduler's misleading "return code 0", so this
        was flying completely blind) and restart the loop from here rather
        than letting the whole process end. Task Scheduler's own triggers
        (boot / 8am weekday) are not a reliable safety net on their own --
        confirmed the same day, two of three on-demand restarts died within
        seconds even from an elevated session, for reasons never captured
        anywhere until this wrapper existed."""
        self.logger.info("Starting AutoBot watchdog")
        if self._another_instance_alive():
            self.logger.info("Another watchdog instance is already running -- exiting without touching it")
            return
        self._write_pid()
        while True:
            try:
                self._run_loop()
            except Exception:
                self.logger.error("Watchdog crashed -- restarting supervisor loop in 10s", exc_info=True)
                time.sleep(10)

    def _run_loop(self) -> None:
        # 2026-09-02: track .env by CONTENT HASH, not mtime. OneDrive sync
        # touches .env's mtime without changing its content (this repo syncs
        # across machines), which tripped the old mtime comparison and caused
        # a main.py restart storm (10+ restarts observed 2026-09-02 morning).
        last_env_hash = self._env_hash()
        restart_count = 0

        self.kill_existing_project_processes()
        if not getattr(self, "_heartbeat_thread_started", False):
            threading.Thread(target=self._heartbeat_monitor, daemon=True).start()
            self._heartbeat_thread_started = True

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
            # (cp1252) -- any log message with a non-Latin-1 character (e.g. ">=")
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
                # Content-hash comparison (see _env_hash): only a REAL .env
                # content change restarts the child; sync-touched mtimes don't.
                current_hash = self._env_hash()
                if current_hash != last_env_hash:
                    self.logger.info("Detected .env content change, restarting main.py")
                    last_env_hash = current_hash
                    self._stop_child(process)
                    break
                # 2026-09-02: agentic auto-deploy -- see _deploy_restart_requested.
                # Consumes deploy_requested.flag and restarts main.py on the new
                # code, but only inside flat deploy windows (else defers).
                if self._deploy_restart_requested(process):
                    break
                # 2026-09-02: hung-but-alive recovery -- see STALL_RESTART_SECONDS.
                if self._stall_restart_requested(process, start_time):
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

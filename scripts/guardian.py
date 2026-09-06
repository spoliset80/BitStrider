"""ApexTrader -- Loss Guardian (2026-09-02).

Independent, always-on daily-loss backstop that watches the LIVE account from
OUTSIDE the bot process. Deterministic (no LLM in the fast path). One-shot per
invocation; the Task Scheduler runs it every minute during market hours.

Flow per run (--once):
  1. Parse .env (self-contained -- never import engine.config first: this file
     must run even if the bot's config/universe files are mid-deploy).
  2. Baseline: engine/.daily_state.json daily_start_equity (the bot's own
     reset). If the bot hasn't reset for today ET yet, take no action.
  3. Poll Alpaca account equity -> day P&L % vs baseline.
  4. ALERT tier (<= -GUARDIAN_ALERT_PCT): email once/day + state file.
  5. HALT tier  (<= -GUARDIAN_HALT_PCT): write flat_request.flag for the bot
     (bot's 5s poll thread flattens + blocks entries until next daily reset).
     If the bot's heartbeat is stale (> GUARDIAN_STALE_HEARTBEAT_SEC) the
     guardian flat-sells every position DIRECTLY as a last resort.
  6. Never places buys. Never touches cash otherwise. Idempotent per day.

State/audit:
  %LOCALAPPDATA%\\ApexTrader\\state\\guardian_state.json  (machine-local snapshot)
  %LOCALAPPDATA%\\ApexTrader\\state\\flat_request.flag     (halt -> bot)
  %LOCALAPPDATA%\\ApexTrader\\state\\guardian.lock        (overlap guard)
  guardian.log                                           (repo root audit)

Usage:  python scripts/guardian.py --once
"""
import argparse
import datetime
import json
import os
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import pytz
import requests

ROOT = Path(__file__).resolve().parent.parent
ET = pytz.timezone("America/New_York")
LOCAL_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ApexTrader"
LOCAL_STATE = LOCAL_DIR / "state"
STATE_FILE = LOCAL_STATE / "guardian_state.json"
FLAT_FILE = LOCAL_STATE / "flat_request.flag"
LOCK_FILE = LOCAL_STATE / "guardian.lock"
# 2026-09-02: was ROOT/"guardian.log" (OneDrive repo). A blocking OneDrive
# append wedged the guardian mid-run at 15:37 ET (task stuck "Running", polls
# stopped 13 min before EOD). Log is now machine-local like the bot/watchdog.
LOG_FILE = LOCAL_DIR / "logs" / "guardian.log"
DAILY_STATE = ROOT / "engine" / ".daily_state.json"
HEARTBEAT = ROOT / "heartbeat.txt"

# Alpaca data endpoint (guardian guards whichever account TRADE_MODE names).
ALPACA_BASE = "https://api.alpaca.markets/v2"


def log(msg: str) -> None:
    line = f"{datetime.datetime.now(ET).isoformat()} | {msg}"
    print(line)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def load_env() -> dict:
    env = {}
    try:
        for ln in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, _, v = ln.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as exc:
        log(f"[ERROR] could not read .env: {exc}")
    return env


def http_json(url: str, key: str, secret: str) -> dict:
    r = requests.get(url, auth=(key, secret), timeout=30)
    r.raise_for_status()
    return r.json()


def send_alert_email(env: dict, subject: str, text: str) -> None:
    """Stdlib SMTP (mirror of the watchdog's alert sender). Best-effort."""
    try:
        if str(env.get("USE_EMAIL_NOTIFICATIONS", "false")).strip().lower() not in ("1", "true", "yes"):
            log(f"[ALERT-EMAIL] skipped (USE_EMAIL_NOTIFICATIONS not enabled): {subject}")
            return
        smtp_server = env.get("EMAIL_SMTP_SERVER", "smtp.gmail.com")
        smtp_port = int(env.get("EMAIL_SMTP_PORT", "587"))
        smtp_user = env.get("EMAIL_SMTP_USER", "")
        smtp_pass = env.get("EMAIL_SMTP_PASSWORD", "")
        from_addr = env.get("EMAIL_FROM_ADDRESS") or smtp_user
        to_list = [a.strip() for a in env.get("EMAIL_TO_ADDRESSES", "").split(",") if a.strip()]
        if not (smtp_user and smtp_pass and to_list):
            log("[ALERT-EMAIL] SMTP settings incomplete -- skipping")
            return
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = ", ".join(to_list)
        msg.set_content(text)
        with smtplib.SMTP(smtp_server, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        log(f"[ALERT-EMAIL] sent: {subject}")
    except Exception as exc:
        log(f"[ALERT-EMAIL] FAILED ({subject}): {exc}")
def acquire_lock() -> bool:
    """O_EXCL lock so a slow API call can't overlap the next 1-min task fire."""
    LOCAL_STATE.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        # Stale lock (dead process > 5 min)? Steal it.
        try:
            if time.time() - LOCK_FILE.stat().st_mtime > 300:
                LOCK_FILE.unlink(missing_ok=True)
                log("[LOCK] stole stale guardian lock")
                return acquire_lock()
        except OSError:
            pass
        return False


def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def now_et() -> datetime.datetime:
    return datetime.datetime.now(ET)


def read_baseline() -> tuple:
    """Return (baseline_equity, baseline_date_str_or_empty) from the bot's
    daily state. The bot rewrites engine/.daily_state.json on every day reset
    (session.reset_daily), so this is the same baseline the bot's own halt
    uses."""
    try:
        if DAILY_STATE.exists():
            st = json.loads(DAILY_STATE.read_text(encoding="utf-8"))
            return float(st.get("daily_start_equity", 0.0)), str(st.get("daily_reset") or "")
    except Exception as exc:
        log(f"[ERROR] baseline read failed: {exc}")
    return 0.0, ""


def heartbeat_age_seconds() -> float | None:
    try:
        if not HEARTBEAT.exists():
            return None
        last = datetime.datetime.fromisoformat(HEARTBEAT.read_text(encoding="utf-8").strip())
        if last.tzinfo is None:
            last = last.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds()
    except Exception:
        return None


def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_state(st: dict) -> None:
    try:
        LOCAL_STATE.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, indent=2), encoding="utf-8")
    except Exception as exc:
        log(f"[ERROR] state save failed: {exc}")


def in_guardian_band(et: datetime.datetime, start_hhmm: str, end_hhmm: str) -> bool:
    hm = et.hour * 60 + et.minute
    sh, sm = (int(x) for x in start_hhmm.split(":"))
    eh, em = (int(x) for x in end_hhmm.split(":"))
    return (sh * 60 + sm) <= hm < (eh * 60 + em)
def direct_flat_sell(env: dict, key: str, secret: str) -> tuple:
    """Last-resort flatten used when the bot is unresponsive (stale heartbeat).
    Market-sells every open equity position via the Alpaca REST API directly.
    Returns (closed_count, failed_count)."""
    try:
        raw = http_json(f"{ALPACA_BASE}/positions", key, secret)
    except Exception as exc:
        log(f"[DIRECT-FLAT] positions fetch failed: {exc}")
        return 0, 0
    closed = failed = 0
    if isinstance(raw, list):
        for pos in raw:
            sym = pos.get("symbol", "")
            try:
                fqty = abs(int(float(pos.get("qty", 0))))
            except Exception:
                continue
            if fqty == 0:
                continue
            try:
                body = {"symbol": sym, "qty": str(fqty), "side": "sell",
                        "type": "market", "time_in_force": "day"}
                r = requests.post(f"{ALPACA_BASE}/orders", auth=(key, secret), json=body, timeout=30)
                if r.status_code in (200, 201):
                    closed += 1
                    log(f"[DIRECT-FLAT] market-sell {fqty} {sym}")
                else:
                    failed += 1
                    log(f"[DIRECT-FLAT] FAILED {sym}: HTTP {r.status_code} {r.text[:200]}")
            except Exception as exc:
                failed += 1
                log(f"[DIRECT-FLAT] FAILED {sym}: {exc}")
    return closed, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one-shot run (scheduled task mode)")
    ap.add_argument("--force", action="store_true", help="act even outside the 09:35-15:50 ET band (manual test)")
    args = ap.parse_args()

    now = now_et()
    if now.weekday() >= 5:
        return 0  # weekends: no action

    env = load_env()
    mode = env.get("TRADE_MODE", "paper").strip().lower()
    key = env.get("LIVE_ALPACA_API_KEY" if mode == "live" else "PAPER_ALPACA_API_KEY", "")
    secret = env.get("LIVE_ALPACA_API_SECRET" if mode == "live" else "PAPER_ALPACA_API_SECRET", "")
    if not key or not secret:
        log(f"[ERROR] missing Alpaca credentials for TRADE_MODE={mode} -- aborting")
        return 1

    alert_pct = float(env.get("GUARDIAN_ALERT_PCT", "0.75"))
    halt_pct = float(env.get("GUARDIAN_HALT_PCT", "1.5"))
    stale_sec = int(env.get("GUARDIAN_STALE_HEARTBEAT_SEC", "300"))
    start_hhmm = env.get("GUARDIAN_POLL_START_ET", "09:35")
    end_hhmm = env.get("GUARDIAN_POLL_END_ET", "15:44")

    if not acquire_lock():
        log("[INFO] another guardian run is in progress -- skipping")
        return 0
    try:
        in_band = in_guardian_band(now, start_hhmm, end_hhmm)
        if not (in_band or args.force):
            log(f"[INFO] outside action band {start_hhmm}-{end_hhmm} ET (now {now.strftime('%H:%M')} ET) -- no-op")
            return 0

        baseline, baseline_date = read_baseline()
        today_et = now.date().isoformat()

        try:
            acct = http_json(f"{ALPACA_BASE}/account", key, secret)
            equity = float(acct.get("equity", 0.0))
        except Exception as exc:
            log(f"[ERROR] account poll failed: {exc}")
            return 0

        hb = heartbeat_age_seconds()
        state = load_state()
        try:
            raw = http_json(f"{ALPACA_BASE}/positions", key, secret)
            positions = raw if isinstance(raw, list) else []
        except Exception as exc:
            log(f"[WARNING] positions poll failed: {exc}")
            positions = []

        # Baseline handling: only act on a baseline the BOT set for TODAY.
        if baseline_date != today_et:
            log(f"[WARN] no today baseline yet (state date={baseline_date!r}) -- no action this run")
            state.update({"last_run": now.isoformat(), "baseline_date": baseline_date,
                          "today_et": today_et, "note": "no today baseline yet"})
            save_state(state)
            return 0

        if baseline <= 0:
            log("[ERROR] baseline equity <= 0 -- refusing to act")
            return 0

        pnl = equity - baseline
        pct = (pnl / baseline) * 100.0
        today_key = today_et
        hb_desc = f"{hb / 60:.1f} min ago" if hb is not None else "never/unknown"

        log(
            f"[POLL] equity=${equity:,.2f} baseline=${baseline:,.2f} "
            f"day_pnl=${pnl:+,.2f} ({pct:+.2f}%) | positions={len(positions)} | heartbeat={hb_desc} | "
            f"alert@-{alert_pct}% halt@-{halt_pct}%"
        )

        # -- ALERT tier (email once/day) -------------------------------------
        alerted_before = state.get("alerted_date") == today_key
        if pct <= -alert_pct and not alerted_before:
            state["alerted_date"] = today_key
            save_state(state)
            log(f"[ALERT] day P&L {pct:+.2f}% <= -{alert_pct}% -- emailing")
            send_alert_email(
                env,
                f"[APEXTRADER] Daily loss alert: {pct:+.2f}% (${pnl:+,.2f})",
                f"ApexTrader daily loss alert at {now.isoformat()}\n"
                f"Equity: ${equity:,.2f} | day start: ${baseline:,.2f}\n"
                f"Day P&L: ${pnl:+,.2f} ({pct:+.2f}%)\n"
                f"Open positions: {len(positions)}\n"
                f"Bot heartbeat: {hb_desc}\n"
                f"Hard-halt threshold: -{halt_pct}% (guardian will flatten + block entries)",
            )
        # -- HALT tier (flatten + block; once/day) ---------------------------
        halted_before = state.get("halted_date") == today_key
        if pct <= -halt_pct and not halted_before:
            state["halted_date"] = today_key
            state["halt_pct"] = pct
            state["halt_pnl"] = pnl
            save_state(state)
            log(f"[HALT] day P&L {pct:+.2f}% <= -{halt_pct}% -- hard flatten + block entries")

            # 1) Signal the bot: its 5s poll thread (orchestrator._tick) reads
            #    this flag, flattens via executor.guardian_halt_flatten, and
            #    blocks entries until the next daily reset.
            try:
                LOCAL_STATE.mkdir(parents=True, exist_ok=True)
                FLAT_FILE.write_text(
                    json.dumps({"date": today_key, "pnl": round(pnl, 2), "pct": round(pct, 3),
                                "equity": round(equity, 2), "reason": "guardian_daily_loss",
                                "ts": now.isoformat()}),
                    encoding="utf-8",
                )
                log("[HALT] flat_request.flag written -- bot should flatten within ~5s")
            except Exception as exc:
                log(f"[HALT] flag write FAILED: {exc}")

            # 2) If the bot is unresponsive, flatten directly (last resort).
            bot_down = hb is None or hb > stale_sec
            if bot_down and mode == "live":
                closed, failed = direct_flat_sell(env, key, secret)
                log(f"[HALT] bot unresponsive (heartbeat {hb_desc}) -- guardian direct flat-sell: {closed} ok, {failed} failed")
                send_alert_email(
                    env,
                    "[APEXTRADER] GUARDIAN EMERGENCY FLATTEN (bot unresponsive)",
                    f"Loss guardian hard-halt at {now.isoformat()} ({pct:+.2f}%, ${pnl:+,.2f}).\n"
                    f"Bot heartbeat was stale ({hb_desc}) so the guardian flat-sold directly.\n"
                    f"Closed {closed} position(s), {failed} failed.",
                )
            elif bot_down:
                log("[HALT] bot unresponsive but not live mode -- flag written, no direct sell in paper")
            else:
                log("[HALT] bot heartbeat fresh -- flag handed to the bot; guardian takes no direct action")

        # -- audit state every run -------------------------------------------
        state["last_run"] = now.isoformat()
        state["baseline_date"] = baseline_date
        state["equity"] = round(equity, 2)
        state["pnl"] = round(pnl, 2)
        state["pct"] = round(pct, 3)
        state["positions"] = len(positions)
        state["heartbeat_age_sec"] = hb
        state["mode"] = mode
        save_state(state)
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())

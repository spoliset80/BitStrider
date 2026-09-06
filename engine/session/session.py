"""
ApexTrader -- Session
Daily and quarterly P&L tracking state.
Extracted from main.py to keep the main entry point lean.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger("ApexTrader")

_QUARTERLY_STATE_FILE = Path(__file__).resolve().parent.parent / ".quarterly_state.json"
_quarterly_state_lock = threading.Lock()
_DAILY_STATE_FILE     = Path(__file__).resolve().parent.parent / ".daily_state.json"
_daily_state_lock     = threading.Lock()

# -- Daily state ------------------------------------------------------------
daily_pnl:          float               = 0.0
daily_start_equity: float               = 0.0
daily_reset:        Optional[datetime.date] = None
trades:             int                 = 0

# -- Quarterly state --------------------------------------------------------
quarterly_start_equity: float               = 0.0
quarterly_reset:        Optional[datetime.date] = None


# -- Helpers ----------------------------------------------------------------

def get_quarter_start(d: datetime.date) -> datetime.date:
    """Return the first date of the calendar quarter containing *d*."""
    quarter_month = ((d.month - 1) // 3) * 3 + 1
    return datetime.date(d.year, quarter_month, 1)


def load_quarterly_state() -> None:
    """Load persisted quarter-start equity from disk (survives restarts)."""
    global quarterly_start_equity, quarterly_reset
    try:
        if _QUARTERLY_STATE_FILE.exists():
            state              = json.loads(_QUARTERLY_STATE_FILE.read_text())
            quarterly_reset    = datetime.date.fromisoformat(state["quarterly_reset"])
            quarterly_start_equity = float(state["quarterly_start_equity"])
            log.info(
                f"Loaded quarterly state: start equity ${quarterly_start_equity:,.2f} "
                f"since {quarterly_reset}"
            )
    except Exception as e:
        log.warning(f"Could not load quarterly state: {e}")


def save_quarterly_state() -> None:
    """Persist current quarter-start equity to disk (thread-safe)."""
    try:
        payload = json.dumps({
            "quarterly_reset":        str(quarterly_reset),
            "quarterly_start_equity": quarterly_start_equity,
        })
        with _quarterly_state_lock:
            _QUARTERLY_STATE_FILE.write_text(payload)
    except Exception as e:
        log.warning(f"Could not save quarterly state: {e}")


def load_daily_state() -> None:
    """Load persisted daily-start equity from disk (survives restarts).

    2026-08-14: daily_start_equity/daily_reset were in-memory-only globals,
    unlike quarterly_start_equity (which already persists via
    _QUARTERLY_STATE_FILE). reset_daily()'s only guard against re-capturing
    was `daily_reset == today`, and daily_reset resets to None on every
    process restart -- so every restart was silently treated as a brand new
    trading day. Confirmed live: 12 separate 'NEW DAY: 2026-08-14' log
    lines fired on 2026-08-14 alone, one per restart, each one moving the
    daily-loss-limit baseline (DAILY_LOSS_LIMIT_BULL_PCT=1%) to whatever
    equity happened to be at that exact moment -- so the 1% halt was only
    ever protecting against loss since the LAST restart, never against loss
    since the true start of the day. Real day P&L (vs. Alpaca's own
    last_equity, i.e. prior close) was already -1.08% -- past the 1%
    threshold -- while the in-memory daily_pnl the halt actually checked
    was reset to ~$0 by the most recent restart and never came close.
    Call once at startup, before the first reset_daily()."""
    global daily_start_equity, daily_reset
    try:
        if _DAILY_STATE_FILE.exists():
            state = json.loads(_DAILY_STATE_FILE.read_text())
            saved_reset = datetime.date.fromisoformat(state["daily_reset"])
            if saved_reset == datetime.date.today():
                daily_reset        = saved_reset
                daily_start_equity = float(state["daily_start_equity"])
                log.info(
                    f"Loaded daily state: start equity ${daily_start_equity:,.2f} "
                    f"since {daily_reset} (survives this restart)"
                )
    except Exception as e:
        log.warning(f"Could not load daily state: {e}")


def save_daily_state() -> None:
    """Persist current daily-start equity to disk (thread-safe)."""
    try:
        payload = json.dumps({
            "daily_reset":        str(daily_reset),
            "daily_start_equity": daily_start_equity,
        })
        with _daily_state_lock:
            _DAILY_STATE_FILE.write_text(payload)
    except Exception as e:
        log.warning(f"Could not save daily state: {e}")


def reset_daily(client) -> None:
    """Reset daily counters for a new trading day and prune the universe."""
    global daily_pnl, daily_start_equity, daily_reset, trades

    today = datetime.date.today()
    if daily_reset == today:
        return

    try:
        _day_acct          = client.get_account()
        daily_start_equity = float(_day_acct.equity)
    except Exception as e:
        log.warning(f"Could not read start-of-day equity: {e}")
        daily_start_equity = 0.0

    daily_pnl   = 0.0
    trades      = 0
    daily_reset = today
    save_daily_state()

    # 2026-09-02: a new trading day clears yesterday's guardian flat flag (the
    # guardian rewrites it fresh, dated today, if the loss backstop trips again).
    try:
        from engine import config as _cfg
        _gf = Path(_cfg.GUARDIAN_FLAT_FILE)
        if _gf.exists():
            _gf.unlink(missing_ok=True)
            log.info("Cleared guardian flat flag for new trading day")
    except Exception as _e:
        log.warning(f"Could not clear guardian flat flag: {_e}")

    log.info("=" * 70)
    log.info(f"NEW DAY: {today} | Start equity: ${daily_start_equity:,.2f}")

    try:
        from engine.equity.universe import prune as _prune
        removed = _prune()
        if removed:
            log.info(
                f"Universe pruned: removed {len(removed)} expired ticker(s): "
                f"{removed[:10]}{'...' if len(removed) > 10 else ''}"
            )
        else:
            log.info("Universe pruned: no expired tickers")
    except Exception as _e:
        log.warning(f"Universe prune failed: {_e}")

    log.info("=" * 70)


def refresh_daily_pnl(client) -> float:
    """Re-read equity from broker and return current daily P&L."""
    global daily_pnl
    if daily_start_equity > 0:
        try:
            _acct     = client.get_account()
            daily_pnl = float(_acct.equity) - daily_start_equity
        except Exception as e:
            log.warning(f"Could not refresh daily P&L: {e}")
    return daily_pnl


def daily_loss_halted(client, regime: str = "bull", refresh: bool = True) -> bool:
    """True when the daily P&L is at/below the regime daily-loss limit.

    Single source of truth shared by the orchestrator scan gate and the
    executor's entry/re-entry funnel (EnhancedExecutor._submit_entry_order).

    2026-09-02: the orchestrator's own scan-gate checks this inline (lines
    ~522-528), but every re-entry path (_maybe_rearm_reentry,
    detect_stopped_out_positions, check_blocked_entries_ema,
    check_pending_entries_ema, staged tranches) skipped it entirely -- the
    single biggest loss driver in the 9/1 reconstruction. Those paths all
    submit through EnhancedExecutor._submit_entry_order, which now calls this
    helper. The orchestrator inline checks are left as-is (belt and braces).
    """
    if refresh:
        refresh_daily_pnl(client)
    from engine import config as _cfg
    loss_pct = _cfg.DAILY_LOSS_LIMIT_BEAR_PCT if regime == "bear" else _cfg.DAILY_LOSS_LIMIT_BULL_PCT
    limit = -(daily_start_equity * loss_pct / 100) if daily_start_equity > 0 else -999_999
    return daily_pnl <= limit


def check_quarterly(client, use_quarterly_target: bool, quarterly_profit_target_pct: float) -> None:
    """Check/initialise the quarterly profit target; logs progress each cycle."""
    global quarterly_start_equity, quarterly_reset

    if not use_quarterly_target:
        return

    today   = datetime.date.today()
    q_start = get_quarter_start(today)

    try:
        _acct   = client.get_account()
        _equity = float(_acct.equity)

        if quarterly_reset != q_start:
            quarterly_start_equity = _equity
            quarterly_reset        = q_start
            save_quarterly_state()
            log.info(f"New quarter {q_start} | Starting equity: ${quarterly_start_equity:,.2f}")

        if quarterly_start_equity > 0:
            q_gain_pct = ((_equity - quarterly_start_equity) / quarterly_start_equity) * 100
            log.info(f"Quarterly P&L: {q_gain_pct:+.1f}% (target >= {quarterly_profit_target_pct:.0f}%)")
            if q_gain_pct >= quarterly_profit_target_pct:
                log.info(
                    f"QUARTERLY TARGET HIT: +{q_gain_pct:.1f}% >= {quarterly_profit_target_pct:.0f}% | "
                    f"${quarterly_start_equity:,.2f} -> ${_equity:,.2f} | Target reached (continuing)"
                )
    except Exception as e:
        log.warning(f"Quarterly target check error: {e}")

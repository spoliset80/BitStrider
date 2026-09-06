"""Self-check for orchestrator._session being a LIVE reference (2026-08-18,
found while investigating daily P&L stuck at $0.00 all session).

`from . import session as _session` bound orchestrator.py's _session to the
engine.session PACKAGE, whose __init__.py does `from .session import
daily_pnl, daily_start_equity, ...` -- a one-time value copy, frozen at
import time. Every later refresh_daily_pnl()/reset_daily() call still only
mutates engine.session.session's own globals (that's where those functions
are defined, so that's where `global daily_pnl` resolves) -- invisible
through the package's already-frozen copy. Two real consequences, both
covered below: the STATUS log's "Daily P&L" never moved off $0.00, and the
daily loss-limit halt's `_session.daily_start_equity > 0` guard was always
False, so daily_loss_limit was always -999_999 -- the 1%/2% circuit breaker
could never trip regardless of real drawdown.

Run with:
  python scripts/test_session_alias_live.py
No network calls, no broker client -- pokes the module globals directly.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.orchestrator as orch
from engine.session import session as sess

# --- orchestrator._session must be the submodule itself, not a copy of it ---
assert orch._session is sess, (
    "orchestrator._session is not the live engine.session.session module -- "
    "P&L/loss-limit reads will silently freeze at their import-time values"
)

# --- a write to the real state (what refresh_daily_pnl/reset_daily do) must
#     be visible through the orchestrator alias immediately ---
sess.daily_start_equity = 2110.75
sess.daily_pnl = -25.0
sess.trades = 3
assert orch._session.daily_start_equity == 2110.75, "daily_start_equity not live through the alias"
assert orch._session.daily_pnl == -25.0, "daily_pnl not live through the alias"
assert orch._session.trades == 3, "trades not live through the alias"

# --- the daily loss-limit guard (orchestrator.py ~line 582) must now be able
#     to actually compute a real limit instead of always falling back to the
#     "disabled" -999_999 sentinel ---
daily_loss_limit = (
    -(orch._session.daily_start_equity * 1.0 / 100)
    if orch._session.daily_start_equity > 0 else -999_999
)
assert daily_loss_limit != -999_999, "daily loss-limit halt is still permanently disabled"
assert abs(daily_loss_limit - (-21.1075)) < 1e-6, f"unexpected daily_loss_limit: {daily_loss_limit}"

print("orchestrator._session alias: all checks passed")

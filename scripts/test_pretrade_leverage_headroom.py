"""Self-check for pre-trade gross-exposure headroom (2026-09-04, user request:
"increase alpaca margin total utilization to 2X the portfolio value").

MAX_PORTFOLIO_LEVERAGE is now 2.0x equity, and -- the actual fix -- it is
enforced BEFORE submission in _size_with_buying_power, not just after fills by
enforce_portfolio_leverage()'s 10-minute grid. Projected gross exposure:

    filled positions (abs market value, options excluded)
  + resting entry orders' notional (fresh/re-entry/staged adds)
  + this order's notional
    <= equity x MAX_PORTFOLIO_LEVERAGE

Run with:
  python scripts/test_pretrade_leverage_headroom.py
No network calls -- broker client / position cache are stubbed.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.execution.enhanced as ee
from engine.execution.enhanced import EnhancedExecutor, PositionInfo, OrderType
from engine.config import MAX_PORTFOLIO_LEVERAGE, MIN_BUYING_POWER_PCT

assert MAX_PORTFOLIO_LEVERAGE == 2.0

EQUITY = 2000.0
CAP = EQUITY * MAX_PORTFOLIO_LEVERAGE  # 4000


def _posinfo(exposure_by_sym):
    d = {}
    for sym, mv in exposure_by_sym.items():
        d[sym] = SimpleNamespace(market_value=mv)
    return PositionInfo(positions_dict=d, total_count=len(d))


def _executor(exposure_by_sym, pending=None, entry_pending=None):
    ex = EnhancedExecutor.__new__(EnhancedExecutor)
    ex._account_cache = SimpleNamespace(equity=EQUITY)
    ex._get_positions = lambda force_refresh=False: _posinfo(exposure_by_sym)
    ex._pending_entry_signals = pending or {}
    ex._entry_pending = entry_pending or {}
    return ex


def _size(ex, price, dollars):
    signal = SimpleNamespace(symbol="NEW", price=price)
    risk_info = {"dollar_amount": dollars, "allocation_pct": 10.0}
    shares, reason = ex._size_with_buying_power(8000.0, signal, risk_info, OrderType.LONG)
    return shares, reason


# 1. No exposure: the leverage bound is not binding -- desired size passes
#    (the per-symbol CONCENTRATION cap binds first here: int(2000*26.7%/10)=53).
ex = _executor({})
shares, _ = _size(ex, 10.0, 1000.0)
assert shares == 53, f"min(desired=100, bp, concentration=53, leverage=400) = 53, got {shares}"

# 2. Exposure 3500 -> headroom 500 -> exactly 50 shares @ $10 (clipped by leverage).
ex = _executor({"HELD": "3500.00"})
shares, _ = _size(ex, 10.0, 1000.0)
assert shares == 50, "gross headroom (4000-3500)/10 = 50 shares"

# 3. Exposure 4000 (at cap) -> zero headroom -> skip, never submit.
ex = _executor({"HELD": "4000.00"})
shares, reason = _size(ex, 10.0, 1000.0)
assert shares == 0 and reason, f"at the 2.0x cap a new entry must be skipped entirely: {shares} {reason}"

# 4. Over the cap -> clamped to zero headroom, not negative shares.
ex = _executor({"HELD": "4500.00"})
shares, _ = _size(ex, 10.0, 1000.0)
assert shares == 0, f"over-cap must clamp headroom at 0, not go negative: {shares}"

# 5. A resting entry order RESERVES headroom (its fill would consume it).
#    _pending_entry_signals stores {"signal": Signal-like, ...} per symbol.
ex = _executor(
    {"HELD": "3500.00"},
    pending={"PEND": {"signal": SimpleNamespace(symbol="PEND", price=10.0)}},
    entry_pending={"PEND": {"qty": 40}},
)
shares, _ = _size(ex, 10.0, 1000.0)
assert shares == 10, f"headroom (4000-3500-400)/10 = 10 shares after the pending 400 notional, got {shares}"

# 6. The symbol's OWN resting order does not double-reserve (it's being replaced/rechecked).
ex = _executor(
    {"HELD": "3500.00"},
    pending={"NEW": {"signal": SimpleNamespace(symbol="NEW", price=10.0)}},
    entry_pending={"NEW": {"qty": 40}},
)
shares, _ = _size(ex, 10.0, 1000.0)
assert shares == 50, "own resting order must not consume its own headroom"

# 7. Options legs are excluded from gross exposure (same as everywhere else).
ex = _executor({
    "HELD": "3500.00",
    "AAPL260116C00150000": "9999.00",  # OCC option -- must not count
})
shares, _ = _size(ex, 10.0, 1000.0)
assert shares == 50, "option market value must not eat equity headroom"

# 8. Short exposure counts by ABSOLUTE market value (gross, not net).
ex = _executor({"SHORTED": "-3500.00"})
shares, _ = _size(ex, 10.0, 1000.0)
assert shares == 50, "a 3500 short is 3500 gross exposure, not -3500"

print(f"OK: pre-trade gross headroom -- new orders bounded to equity x {MAX_PORTFOLIO_LEVERAGE} "
      f"minus filled exposure minus resting entry notional; at-cap/over-cap entries skip; "
      f"options excluded; shorts counted by absolute value")
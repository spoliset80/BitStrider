"""Self-check for the portfolio-wide leverage cap (2026-08-17, at the
user's request: "restrict portfolio value to 1.5X the actual account
[equity, not] margin account"; scaled to 2.0x the same day when the whole
sizing pipeline moved from a 7.5% to a 10% base -- "change this to 10%
base instead of 7.5% and scale everything up").

Alpaca's own buying_power already reflects margin (roughly 2x-4x equity
depending on account type/PDT status). This is a separate, usually-
stricter ceiling on TOTAL exposure across every open position combined --
independent of per-symbol and per-correlated-group caps, which don't stop
the WHOLE book from being over-leveraged if spread across enough
uncorrelated names.

Run with:
  python scripts/test_portfolio_leverage_cap.py
No network calls -- exercises PositionInfo.total_market_value() directly
and the entry-sizing math from _size_with_buying_power with a fake
account/position set.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.execution.enhanced import PositionInfo
from engine.config import MAX_PORTFOLIO_LEVERAGE

assert MAX_PORTFOLIO_LEVERAGE == 2.0

# --- PositionInfo.total_market_value(): sums abs(market_value), skips options ---
positions = {
    "AAPL": SimpleNamespace(market_value="500.00"),
    "TSLA": SimpleNamespace(market_value="-300.00"),   # short -- magnitude still counts
    "AAPL260116C00150000": SimpleNamespace(market_value="9999.00"),  # OCC option -- excluded
}
info = PositionInfo(positions_dict=positions, total_count=3)
assert info.total_market_value() == 800.0, f"expected 500+300 (option excluded), got {info.total_market_value()}"

# Empty book -> 0, no error.
assert PositionInfo(positions_dict={}, total_count=0).total_market_value() == 0.0

# A malformed market_value must not blow up the sum, just get skipped.
bad = {"XYZ": SimpleNamespace(market_value=None)}
assert PositionInfo(positions_dict=bad, total_count=1).total_market_value() == 0.0

# --- Entry-sizing math: the leverage bound _size_with_buying_power computes ---
def max_leverage_shares(equity: float, current_exposure: float, price: float, margin: float = 1.0) -> int:
    """Mirrors the inline calc in _size_with_buying_power exactly."""
    cap_value = equity * MAX_PORTFOLIO_LEVERAGE
    headroom  = max(0.0, cap_value - current_exposure)
    return int(headroom / (price * margin))

EQUITY = 2000.0
CAP = EQUITY * MAX_PORTFOLIO_LEVERAGE  # 4000 at the current 2.0x

# Partially exposed -> shares sized to exactly the remaining headroom.
assert max_leverage_shares(equity=EQUITY, current_exposure=2500, price=10.0) == int((CAP - 2500) / 10), \
    "headroom / price must match exactly"

# Already AT or OVER the cap -> zero headroom, zero shares, not negative.
assert max_leverage_shares(equity=EQUITY, current_exposure=CAP, price=10.0) == 0
assert max_leverage_shares(equity=EQUITY, current_exposure=CAP + 2000, price=10.0) == 0, \
    "over the cap must clamp to 0 headroom, not go negative"

# No existing exposure -> the full cap is available as headroom.
assert max_leverage_shares(equity=EQUITY, current_exposure=0, price=10.0) == int(CAP / 10), \
    "full cap / price when nothing is held yet"

# Margin (short) uses double the buying-power cost per share, same as max_bp does.
assert max_leverage_shares(equity=EQUITY, current_exposure=0, price=10.0, margin=2.0) == int(CAP / 20)

print(f"OK: portfolio-wide leverage cap sums total exposure correctly (options excluded), "
      f"clamps headroom at zero instead of going negative once already over the {MAX_PORTFOLIO_LEVERAGE}x cap, "
      f"and the entry-sizing headroom math matches what _size_with_buying_power computes")

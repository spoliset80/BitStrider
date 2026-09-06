"""Self-check for rounding the desired share count instead of truncating it
(2026-08-18, user request): "prioritize the full number than dollar value,
for example the 10% limit puts 1.8 stock then round to 2 stocks if there is
cash available".

_size_with_buying_power's `desired` used int() (floor) on dollar_amount /
price -- a 1.8-share target silently became 1, using only 56% of the
intended allocation. Now rounds to nearest; the existing min(desired, max_bp,
max_concentration, max_leverage) clamp is what enforces "if there is cash
available" -- rounding up only sticks when a real capacity cap doesn't pull
it back down.

Run with:
  python scripts/test_share_rounding.py
No network calls -- client/account/positions are stubbed.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.execution.enhanced import EnhancedExecutor, OrderType, PositionInfo


def _executor(equity, positions_value=0.0):
    ex = EnhancedExecutor.__new__(EnhancedExecutor)
    ex._account_cache = SimpleNamespace(equity=equity)
    ex._get_positions = lambda force_refresh=False: PositionInfo(
        positions_dict={"HELD": SimpleNamespace(market_value=str(positions_value))} if positions_value else {},
        total_count=1 if positions_value else 0,
    )
    return ex


signal = SimpleNamespace(symbol="TEST", price=10.0)

# --- 1.8-share target, plenty of cash -> rounds UP to 2, not down to 1. ---
ex = _executor(equity=100_000.0)  # huge equity/BP so no cap binds except desired itself
risk_info = {"dollar_amount": 18.0}  # 18 / 10.00 = 1.8 shares
shares, reason = ex._size_with_buying_power(buying_power=100_000.0, signal=signal, risk_info=risk_info, order_type=OrderType.LONG)
assert shares == 2, f"1.8 shares should round up to 2 when cash allows, got {shares} ({reason})"

# --- Same 1.8-share target, but buying power only covers 1 -> clamped back
#     down by max_bp, proving "if there is cash available" is still enforced. ---
ex = _executor(equity=100_000.0)
shares, reason = ex._size_with_buying_power(buying_power=15.0, signal=signal, risk_info=risk_info, order_type=OrderType.LONG)
assert shares == 1, f"rounding up must not override a real buying-power ceiling, got {shares}"

# --- 0.3-share target rounds DOWN to 0 (not up) -> correctly skipped. ---
ex = _executor(equity=100_000.0)
risk_info_small = {"dollar_amount": 3.0}  # 3 / 10.00 = 0.3 shares
shares, reason = ex._size_with_buying_power(buying_power=100_000.0, signal=signal, risk_info=risk_info_small, order_type=OrderType.LONG)
assert shares == 0, f"0.3 shares must round down to 0, got {shares}"
assert reason is not None

# --- Exactly on a whole number -> unaffected either way. ---
ex = _executor(equity=100_000.0)
risk_info_whole = {"dollar_amount": 30.0}  # exactly 3 shares
shares, _ = ex._size_with_buying_power(buying_power=100_000.0, signal=signal, risk_info=risk_info_whole, order_type=OrderType.LONG)
assert shares == 3

print("OK: desired share count rounds to nearest instead of always truncating down, still clamped by real buying-power/concentration/leverage caps")

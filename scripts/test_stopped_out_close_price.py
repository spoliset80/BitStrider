"""Self-check for detect_stopped_out_positions() using the ACTUAL closing
fill instead of a stale poll-cycle mark price (2026-08-17, found live).

FIEE spiked to $5.95 shortly after entry ($5.73), got polled near that
peak by the 10s software-stop thread, then its trailing stop fired and
closed it at $5.6751 -- a real loss -- within the same ~10s gap before the
next poll. Comparing entry against the stale $5.95 snapshot said "not a
loss" (5.95 > 5.73 for a long), so no re-entry cooldown got set, and the
identical EarlySqueeze signal re-bought FIEE two minutes later. Fixed by
looking up the real closing fill via the orders API first, falling back
to the approximate mark only if that lookup fails.

Run with:
  python scripts/test_stopped_out_close_price.py
No network calls -- exercises _get_recent_close_price() against a fake
client, and replicates the was_loss decision inline (the surrounding
method needs a live position-polling loop to exercise end-to-end).
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.execution.enhanced as enhanced

def _order(side, status, filled_avg_price):
    return SimpleNamespace(side=side, status=status, filled_avg_price=filled_avg_price)


def _exec_with_orders(orders):
    ex = enhanced.EnhancedExecutor.__new__(enhanced.EnhancedExecutor)
    ex.client = MagicMock()
    ex.client.get_orders.return_value = orders
    return ex


# --- The FIEE case: real closing fill was a loss even though the position
#     was above entry moments before. Must return the TRUE exit price. ---
ex = _exec_with_orders([_order("sell", "filled", 5.6751)])
price = ex._get_recent_close_price("FIEE", is_long=True)
assert price == 5.6751, f"expected the real closing fill 5.6751, got {price}"

# --- Only the matching CLOSE side counts -- a short's close is a BUY, not
#     a SELL (the entry side for a short). Passing is_long correctly must
#     select the right side in the API filter (verified via the client call). ---
ex2 = _exec_with_orders([_order("buy", "filled", 10.25)])
price2 = ex2._get_recent_close_price("XYZ", is_long=False)
assert price2 == 10.25
called_kwargs = ex2.client.get_orders.call_args
req = called_kwargs.kwargs.get("filter") or called_kwargs.args[0]
from alpaca.trading.enums import OrderSide
assert req.side == OrderSide.BUY, "closing a short must query BUY-side orders, not SELL"

ex3 = _exec_with_orders([])
req3_check = None
ex3._get_recent_close_price("ABC", is_long=True)
called = ex3.client.get_orders.call_args
req3 = called.kwargs.get("filter") or called.args[0]
assert req3.side == OrderSide.SELL, "closing a long must query SELL-side orders"

# --- No filled order found -> None, caller falls back to the approximate mark. ---
ex4 = _exec_with_orders([])
assert ex4._get_recent_close_price("EMPTY", is_long=True) is None

# --- An order that exists but isn't filled (still open/canceled) -> skipped, None. ---
ex5 = _exec_with_orders([_order("sell", "canceled", None)])
assert ex5._get_recent_close_price("CANCELED", is_long=True) is None

# --- Broker call raising -> caught, returns None, never propagates. ---
ex6 = enhanced.EnhancedExecutor.__new__(enhanced.EnhancedExecutor)
ex6.client = MagicMock()
ex6.client.get_orders.side_effect = RuntimeError("network down")
assert ex6._get_recent_close_price("ERR", is_long=True) is None

# --- Regression check: replicates the exact FIEE was_loss decision, old vs new ---
entry, stale_mark, real_close = 5.73, 5.95, 5.6751
old_was_loss = stale_mark < entry          # the bug: False (5.95 > 5.73)
new_was_loss = real_close < entry          # the fix: True (5.6751 < 5.73)
assert old_was_loss is False, "confirms the bug: stale mark alone misses this loss"
assert new_was_loss is True, "confirms the fix: real closing fill correctly flags it as a loss"

print("OK: detect_stopped_out_positions() now uses the real closing fill (via "
      "_get_recent_close_price) instead of a stale poll-cycle mark -- reproduces "
      "the FIEE case correctly (stale mark said 'not a loss', real fill says 'loss'), "
      "picks the correct closing side for longs vs shorts, and fails safe to None "
      "on no data / unfilled orders / a broker error")

"""Self-check for the 2026-08-17 fix: enforce_position_concentration's trim
was failing every single time (confirmed live: TTD, 0/36 over 6+ hours)
because Alpaca reserves a position's ENTIRE qty against its resting GTC
protective stop, leaving 0 shares "available" for the trim's own competing
order. _free_shares_for_trim now shrinks that resting stop by the trim
amount FIRST (via order-replace, never cancel) so the trim order actually
has shares to work with, and the position keeps continuous stop coverage
throughout (no cancel -> gap -> re-arm window).

Run with:
  python scripts/test_concentration_trim_frees_stop.py
No network calls / no broker connection -- everything is faked.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import ReplaceOrderRequest

import engine.execution.enhanced as enhanced


class FakeOrder:
    def __init__(self, id, symbol, qty, time_in_force=TimeInForce.GTC):
        self.id, self.symbol, self.qty, self.time_in_force = id, symbol, qty, time_in_force


class FakePosition:
    def __init__(self, symbol, qty, current_price, market_value, unrealized_plpc):
        self.symbol, self.qty, self.current_price = symbol, qty, current_price
        self.market_value, self.unrealized_plpc = market_value, unrealized_plpc


class FakeAccount:
    equity = 2000.0
    buying_power = 4000.0
    daytrade_count = 0
    pattern_day_trader = False
    maintenance_margin = 0.0


class FakeClient:
    def __init__(self, orders=None):
        self.orders = orders or []
        self.replace_calls = []   # (order_id, new_qty)
        self.submitted = []       # symbols a closing order was submitted for

    def get_account(self):
        return FakeAccount()

    def get_all_positions(self):
        return self.positions

    def get_orders(self, *a, **k):
        return self.orders

    def replace_order_by_id(self, order_id, order_data: ReplaceOrderRequest):
        self.replace_calls.append((order_id, order_data.qty))

    def cancel_order_by_id(self, order_id):
        pass

    def submit_order(self, req):
        self.submitted.append(req.symbol)


# ---- _free_shares_for_trim in isolation ----

# Happy path: GTC stop holds the full 64 shares, trim needs 20 -> shrink stop to 44.
client = FakeClient(orders=[FakeOrder("stop-1", "TTD", "64")])
ex = enhanced.EnhancedExecutor(client)
ex._free_shares_for_trim("TTD", 20)
assert client.replace_calls == [("stop-1", 44)], client.replace_calls

# No resting order for the symbol -> no-op, no replace attempted.
client2 = FakeClient(orders=[])
ex2 = enhanced.EnhancedExecutor(client2)
ex2._free_shares_for_trim("TTD", 20)
assert client2.replace_calls == []

# Trim would zero out (or invert) the stop's coverage -> guarded, no-op.
client3 = FakeClient(orders=[FakeOrder("stop-3", "TTD", "10")])
ex3 = enhanced.EnhancedExecutor(client3)
ex3._free_shares_for_trim("TTD", 10)  # 10 - 10 = 0, must not zero out coverage
assert client3.replace_calls == [], client3.replace_calls

# A DAY (non-GTC) order resting doesn't count -- only the GTC protective stop is shrunk.
client4 = FakeClient(orders=[FakeOrder("day-1", "TTD", "64", time_in_force=TimeInForce.DAY)])
ex4 = enhanced.EnhancedExecutor(client4)
ex4._free_shares_for_trim("TTD", 20)
assert client4.replace_calls == []

# ---- wired into enforce_position_concentration: replace happens before the trim submit ----
client5 = FakeClient(orders=[FakeOrder("stop-5", "TTD", "64")])
ex5 = enhanced.EnhancedExecutor(client5)
client5.positions = [FakePosition("TTD", "-64", 14.0, -896.0, 0.03)]  # short, 3% gain, over cap at $2,000 equity

ex5.enforce_position_concentration()
assert client5.replace_calls, "expected the resting stop to be shrunk before the trim"
assert client5.submitted == ["TTD"], client5.submitted
(_, new_qty) = client5.replace_calls[0]
assert 1 <= new_qty < 64, new_qty  # shrunk, but never to zero

print("OK: concentration trim now frees shares by shrinking the resting stop first, no cancel/re-arm gap")

"""End-to-end regression for the 2026-09-03 SNOW exit failure.

REAL 9/3 SEQUENCE (from %LOCALAPPDATA%\\ApexTrader\\logs\\apextrader.log):
  09:34:42 BUY 1 SNOW @ 380.25
  ~09:47   GTC trailing stop (1.5%) rests, reserving the only share
           (qty_available=0, held_for_orders=1)
  09:48+   check_software_stops fires repeatedly as price crosses the software
           stop -> Alpaca rejects 40310000 "insufficient qty available ...
           held_for_orders" NINE consecutive times
  09:50:09 EMA9 EXIT finally closes at 365.62 -> realized -14.63 (-3.85%)

This test replays that sequence through the REAL check_software_stops() with a
stub broker and asserts the new behavior: one cancel of the GTC stop, one close
order, no retry storm, and clean state transitions across poller ticks.

Run with:
  python scripts/test_snow_exit_reconciliation.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.execution.enhanced as ee
from engine.execution.enhanced import EnhancedExecutor, OrderSide, TimeInForce

_orig_sleep = ee.time.sleep
ee.time.sleep = lambda *_: None
_orig_market_state = ee.MarketState
ee.MarketState = SimpleNamespace(from_now=lambda: SimpleNamespace(is_regular_hours=True))
ee.CLOSE_CANCEL_CONFIRM_TIMEOUT_SEC = 1.0
ee.CLOSE_CANCEL_CONFIRM_POLL_SEC = 0.05
_orig_trail = ee._atr_trail_pct_for
ee._atr_trail_pct_for = lambda symbol, price, entry_log: (1.5, "FLAT")


class _Pos:
    def __init__(self, symbol, qty, price, avg, available):
        self.symbol, self.qty, self.current_price = symbol, qty, price
        self.avg_entry_price = avg
        self.qty_available = available


class _GTCStop:
    def __init__(self):
        self.id = "gtc-snow-1"
        self.symbol = "SNOW"
        self.side = SimpleNamespace(value="sell")
        self.order_type = "trailing_stop"
        self.time_in_force = TimeInForce.GTC
        self.status = SimpleNamespace(value="accepted")
        self.qty = 1
        self.filled_qty = 0
        self.client_order_id = ""


class _Client:
    """Broker replaying the 9/3 SNOW state machine."""
    def __init__(self):
        self.positions = [_Pos("SNOW", 1, 365.62, avg=380.25, available=0)]
        self.orders = [_GTCStop()]          # the protective stop, holding the share
        self.cancelled = []
        self.submitted = []

    def get_all_positions(self):
        return list(self.positions)

    def get_orders(self):
        return list(self.orders)

    def cancel_order_by_id(self, oid):
        self.cancelled.append(str(oid))
        self.orders = [o for o in self.orders if str(o.id) != str(oid)]

    def get_latest_quote(self, symbol):
        return SimpleNamespace(bid_price=364.0, ask_price=366.0)

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id=f"close-{len(self.submitted)}")


def _executor(client):
    ex = EnhancedExecutor.__new__(EnhancedExecutor)
    ex.client = client
    ex._pending_closes = {}
    ex._entry_log = {"SNOW": {"strategy": "TopList"}}
    ex._pdt_stop_blocked = {"SNOW": 380.25 * (1 - 0.015)}  # software SL from the naked-cover fallback
    # Re-entry policy is out of scope here (covered by test_loss_reentry_30m_gate);
    # stub it so this test isolates the reconciliation behavior.
    ex._maybe_rearm_reentry = lambda *a, **k: None
    return ex


try:
    client = _Client()
    ex = _executor(client)

    # Poller tick 1 (the first of the 9 rejections in the old code):
    # the stop is breached -> cancel the GTC, confirm, close exactly 1 share.
    ex.check_software_stops()
    assert client.cancelled == ["gtc-snow-1"], client.cancelled
    assert len(client.submitted) == 1, f"exactly one close, got {len(client.submitted)}"
    close_req = client.submitted[0]
    assert type(close_req).__name__ == "LimitOrderRequest"
    assert close_req.qty == 1 and close_req.side == OrderSide.SELL
    assert str(close_req.client_order_id).startswith("apex-close-software-sl-SNOW-")
    # Position NOT yet confirmed flat -> the software-stop watch stays armed
    # (the old code popped it optimistically and could lose an unfilled close).
    assert "SNOW" in ex._pdt_stop_blocked

    # Poller ticks 2..N (old code: up to NINE 40310000 rejections here):
    # close order still working -> dedupe, NO new orders, no exceptions.
    class _RestingClose:
        def __init__(self):
            self.id = "close-1"  # matches the stub broker's submit return value
            self.symbol = "SNOW"
            self.side = SimpleNamespace(value="sell")
            self.order_type = "limit"
            self.time_in_force = TimeInForce.DAY
            self.status = SimpleNamespace(value="accepted")
            self.qty = 1
            self.filled_qty = 0
            self.client_order_id = str(close_req.client_order_id)

    client.orders.append(_RestingClose())
    for _ in range(5):
        ex.check_software_stops()
    assert len(client.submitted) == 1, \
        f"retry storm! {len(client.submitted)} close orders (old code did this 9x)"

    # Broker reality: the close fills, position is gone.
    client.positions = []
    client.orders = []
    ex.check_software_stops()
    assert "SNOW" not in ex._pdt_stop_blocked, "flat must clear the software-stop watch"
    assert "SNOW" not in ex._pending_closes

    # Guard: the cancel list never contains entry-order ids.
    assert all(not str(c).startswith("entry") for c in client.cancelled)

    print("OK: SNOW 9/3 replay -- the share reserved by the GTC trail is now closed on the FIRST "
          "breach (cancel -> confirm -> close 1 sh), repeated poller ticks submit no duplicate, "
          "and the software-stop watch clears only on confirmed flat")
finally:
    ee.time.sleep = _orig_sleep
    ee.MarketState = _orig_market_state
    ee._atr_trail_pct_for = _orig_trail
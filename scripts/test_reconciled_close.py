"""Self-check for _request_reconciled_close() (2026-09-03).

Replaces the cancel-all -> sleep(0.4) -> close-with-stale-qty sequence that
raced the broker's own cancel processing. Guarantees verified here:

  1. flat position -> NO order at all;
  2. only classified protection is cancelled (never entry orders);
  3. close waits for CONFIRMED cancel (bounded poll), else defers
     (cancel_pending) with the position still protected;
  4. quantity is re-read after the cancel -- the cancelled stop may have
     filled, shrinking (or flattening) the position;
  5. at most ONE intentional close per symbol (dedupe against pending state
     and reconcile from broker reality);
  6. failed close re-arms GTC protection; failed re-arm -> critical state;
  7. PDT-blocked closes surface as blocked_pdt.

Run with:
  python scripts/test_reconciled_close.py
No network calls -- broker client is stubbed, MarketState/sleep patched.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.execution.enhanced as ee
from engine.execution.enhanced import EnhancedExecutor, OrderSide, TimeInForce

# Time-independent: no real sleeps during the cancel-confirmation poll.
_orig_sleep = ee.time.sleep
ee.time.sleep = lambda *_: None
# MarketState.from_now() is called by _submit_closing_order for extended-hours.
_orig_market_state = ee.MarketState
ee.MarketState = SimpleNamespace(from_now=lambda: SimpleNamespace(is_regular_hours=True))
# Fast, deterministic cancel-confirm window.
ee.CLOSE_CANCEL_CONFIRM_TIMEOUT_SEC = 1.0
ee.CLOSE_CANCEL_CONFIRM_POLL_SEC = 0.05
_orig_trail = ee._atr_trail_pct_for
ee._atr_trail_pct_for = lambda symbol, price, entry_log: (1.5, "FLAT")


class _Pos:
    def __init__(self, symbol, qty, price, avg=None, available=None):
        self.symbol = symbol
        self.qty = qty
        self.current_price = price
        self.avg_entry_price = avg if avg is not None else price
        self.qty_available = available if available is not None else qty


class _GTCStop:
    """The 9/3 SNOW protective order: sell GTC trailing stop reserving the share."""
    def __init__(self, order_id="gtc-1", symbol="SNOW", side="sell", qty=1):
        self.id = order_id
        self.symbol = symbol
        self.side = SimpleNamespace(value=side)
        self.order_type = "trailing_stop"
        self.time_in_force = TimeInForce.GTC
        self.status = SimpleNamespace(value="accepted")
        self.qty = qty
        self.filled_qty = 0
        self.client_order_id = ""


class _Client:
    def __init__(self, positions, orders, quote=(364.0, 366.0),
                 close_fails=False, rearm_fails=False):
        self._positions = list(positions)
        self._orders = list(orders)
        self._quote = quote
        self.cancelled = []
        self.submitted = []
        self.close_fails = close_fails
        self.rearm_fails = rearm_fails

    def get_all_positions(self):
        return list(self._positions)

    def get_orders(self):
        return list(self._orders)

    def cancel_order_by_id(self, oid):
        self.cancelled.append(str(oid))
        # Broker cancel is effective the moment it's acknowledged here, so the
        # bounded confirmation poll sees it gone on its first poll.
        self._orders = [o for o in self._orders if str(o.id) != str(oid)]

    def get_latest_quote(self, symbol):
        return SimpleNamespace(bid_price=self._quote[0], ask_price=self._quote[1])

    def submit_order(self, req):
        rtype = type(req).__name__
        if rtype == "LimitOrderRequest" and self.close_fails:
            raise RuntimeError("simulated close rejection")
        if rtype == "TrailingStopOrderRequest" and self.rearm_fails:
            raise RuntimeError("simulated re-arm rejection")
        self.submitted.append(req)
        return SimpleNamespace(id=f"new-{len(self.submitted)}")


def _executor(client):
    ex = EnhancedExecutor.__new__(EnhancedExecutor)
    ex.client = client
    ex._pending_closes = {}
    ex._entry_log = {}
    return ex


class _RestingClose:
    """A submitted close order as it rests at the broker (has a broker id --
    the pydantic REQUEST object returned by submit does not)."""
    def __init__(self, order_id, coid):
        self.id = order_id
        self.symbol = "SNOW"
        self.side = SimpleNamespace(value="sell")
        self.order_type = "limit"
        self.time_in_force = TimeInForce.DAY
        self.status = SimpleNamespace(value="accepted")
        self.qty = 1
        self.filled_qty = 0
        self.client_order_id = coid


try:
    # 1. Flat position -> no order, no crash.
    ex = _executor(_Client(positions=[], orders=[]))
    r = ex._request_reconciled_close("SNOW", "software-sl", 365.0)
    assert r.state == "flat" and ex.client.submitted == [], r

    # 2. THE SNOW CASE: 1-share long, GTC trail reserves it, stop breached.
    #    Cancel the trail, CONFIRM, then close exactly the remaining share.
    gtc = _GTCStop()
    pos = _Pos("SNOW", 1, 365.62, avg=380.25, available=0)
    ex = _executor(_Client(positions=[pos], orders=[gtc]))
    r = ex._request_reconciled_close("SNOW", "software-sl", 365.62)
    assert r.state == "submitted", r
    assert ex.client.cancelled == ["gtc-1"], ex.client.cancelled
    assert len(ex.client.submitted) == 1, "exactly ONE close order"
    close_req = ex.client.submitted[0]
    assert type(close_req).__name__ == "LimitOrderRequest"
    assert close_req.qty == 1 and close_req.side == OrderSide.SELL
    assert str(close_req.client_order_id).startswith("apex-close-software-sl-SNOW-"), close_req.client_order_id

    # 3. Second tick, close order still working -> already_pending, NO duplicate.
    resting = _RestingClose("close-1", str(close_req.client_order_id))
    ex.client._orders.append(resting)
    ex._pending_closes["SNOW"]["close_order_id"] = "close-1"
    r = ex._request_reconciled_close("SNOW", "software-sl", 365.0)
    assert r.state == "already_pending", r
    assert len(ex.client.submitted) == 1, "must never duplicate the close"

    # 4. Position goes flat -> pending state cleared.
    ex.client._positions = []
    r = ex._request_reconciled_close("SNOW", "software-sl", 365.0)
    assert r.state == "flat" and "SNOW" not in ex._pending_closes

    # 5. Cancel NOT confirmed within the timeout -> defer, still protected.
    class _StickyOrders(_Client):
        def cancel_order_by_id(self, oid):
            self.cancelled.append(str(oid))  # cancel never takes effect

    ex = _executor(_StickyOrders(positions=[_Pos("SNOW", 1, 365.0)], orders=[_GTCStop()]))
    r = ex._request_reconciled_close("SNOW", "software-sl", 365.0)
    assert r.state == "cancel_pending", r
    assert ex.client.submitted == [], "must NOT close while protection cancel is unconfirmed"

    # 6. Protection filled during cancellation -> position flattened, no close.
    class _FillOnCancel(_Client):
        def cancel_order_by_id(self, oid):
            self.cancelled.append(str(oid))
            self._orders = []      # stop filled, taking the position with it
            self._positions = []

    ex = _executor(_FillOnCancel(positions=[_Pos("SNOW", 1, 365.0)], orders=[_GTCStop()]))
    r = ex._request_reconciled_close("SNOW", "software-sl", 365.0)
    assert r.state == "flat" and ex.client.submitted == [], r

    # 7. Remaining qty re-read after cancel: 2-share position, 1 share's stop
    #    filled first -> close exactly the remaining 1.
    class _PartialFill(_Client):
        def cancel_order_by_id(self, oid):
            self.cancelled.append(str(oid))
            self._orders = []
            self._positions = [_Pos("SNOW", 1, 365.0)]  # was 2, stop filled 1

    ex = _executor(_PartialFill(positions=[_Pos("SNOW", 2, 365.0)], orders=[_GTCStop(qty=1)]))
    r = ex._request_reconciled_close("SNOW", "software-sl", 365.0)
    assert r.state == "submitted" and r.requested_qty == 1, r
    assert ex.client.submitted[0].qty == 1

    # 8. Close rejected -> GTC protection re-armed (fail-safe centralized).
    ex = _executor(_Client(positions=[_Pos("SNOW", 1, 365.0)], orders=[_GTCStop()], close_fails=True))
    r = ex._request_reconciled_close("SNOW", "software-sl", 365.0)
    assert r.state == "failed_reprotected", r
    rearm = [o for o in ex.client.submitted if type(o).__name__ == "TrailingStopOrderRequest"]
    assert len(rearm) == 1 and rearm[0].qty == 1 and rearm[0].time_in_force == TimeInForce.GTC

    # 9. Close AND re-arm both fail -> critical_unprotected (alerts fire).
    ex = _executor(_Client(positions=[_Pos("SNOW", 1, 365.0)], orders=[_GTCStop()],
                           close_fails=True, rearm_fails=True))
    r = ex._request_reconciled_close("SNOW", "software-sl", 365.0)
    assert r.state == "critical_unprotected", r

    # 10. PDT-blocked close (40310100) -> blocked_pdt, no re-arm loop.
    class _PdtClose(_Client):
        def submit_order(self, req):
            if type(req).__name__ == "LimitOrderRequest":
                raise RuntimeError('{"code":40310100,"message":"potential PDT violation"}')
            return super().submit_order(req)

    ex = _executor(_PdtClose(positions=[_Pos("SNOW", 1, 365.0)], orders=[_GTCStop()]))
    r = ex._request_reconciled_close("SNOW", "software-sl", 365.0)
    assert r.state == "blocked_pdt", r
    assert all(type(o).__name__ != "TrailingStopOrderRequest" for o in ex.client.submitted)

    # 11. A resting DAY trailing-buy ENTRY is never treated as protection and
    #     never cancelled by a close path.
    entry = _GTCStop(order_id="entry-1")
    entry.time_in_force = TimeInForce.DAY
    entry.order_type = "trailing_stop"
    entry.client_order_id = "apex-entry-TopList-SNOW-1"
    ex = _executor(_Client(positions=[_Pos("SNOW", 1, 365.0)], orders=[entry]))
    r = ex._request_reconciled_close("SNOW", "software-sl", 365.0)
    assert r.state == "submitted" and ex.client.cancelled == [], \
        "a DAY entry order must not be treated as protection and cancelled"

    # 12. Stale pending close (order vanished, position still open) -> resubmit once.
    ex = _executor(_Client(positions=[_Pos("SNOW", 1, 365.0)], orders=[]))
    ex._pending_closes["SNOW"] = {"state": "pending", "close_order_id": "dead-id"}
    r = ex._request_reconciled_close("SNOW", "software-sl", 365.0)
    assert r.state == "submitted" and len(ex.client.submitted) == 1, r

    print("OK: reconciled close -- cancels ONLY the GTC protective stop, waits for confirmed cancel, "
          "re-reads remaining qty, dedupes pending closes, defers when cancel is unconfirmed, "
          "re-arms on close failure, and surfaces PDT/critical states explicitly")
finally:
    ee.time.sleep = _orig_sleep
    ee.MarketState = _orig_market_state
    ee._atr_trail_pct_for = _orig_trail

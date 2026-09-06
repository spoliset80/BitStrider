"""Self-check for staged allocation (25% x 4), never adding while losing.

The requested implementation (2026-08-31): instead of one full-size entry
order, scale in over STAGED_ALLOCATION_TRANCHES equal tranches (default
4 x 25%). The first tranche is submitted at signal time; each subsequent
tranche is added by the periodic poller (PENDING_ENTRY_RECHECK_SEC cadence) ONLY while:

  - a position is actually open (first tranche filled);
  - the position is NOT losing (unrealized gain strictly above
    STAGED_ALLOCATION_MIN_GAIN_PCT) -- never adding while losing;
  - a FRESH EMA trend-alignment check passes immediately before the tranche;
  - tranches remain.

2026-09-02 hardening: tranches are submitted through the scale_in=True path
of _submit_entry_order, which bypasses the two FIRST-ENTRY-only local guards
(the 60s _recent_entry_submits debounce and the order_cache slot holding the
first tranche's order id) -- without that, tranche 2 could be blocked for the
life of the position. All broker-side guards (active same-symbol DAY order)
and local pending-entry guards (_entry_pending, _pending_entry_signals) still
apply, asserted below.

Run with:
  python scripts/test_staged_allocation.py
No network calls -- client/positions/bars are all stubbed.
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.execution.enhanced as enhanced
from engine.execution.enhanced import EnhancedExecutor, OrderSide, TimeInForce, AlpacaOrderType, TrailingStopOrderRequest

# Time-independent (2026-09-03): staged tranche submission paths gate on the
# 11:00-14:45 ET lunch break, so this test failed whenever it ran midday.
# Never-inside-the-break during the test, same shim the module's own
# self-test block uses.
enhanced.in_lunch_break = lambda *_: False

assert enhanced.STAGED_ALLOCATION_ENABLED is True
assert enhanced.STAGED_ALLOCATION_TRANCHES == 4
assert enhanced.STAGED_ALLOCATION_MIN_GAIN_PCT == 0.0


class _Pos:
    def __init__(self, symbol, qty, current_price):
        self.symbol, self.qty, self.current_price = symbol, qty, current_price


class _Order:
    def __init__(self, oid, symbol, side, tif, order_type="limit"):
        self.id, self.symbol, self.side = oid, symbol, side
        self.time_in_force = tif
        self.order_type = order_type


class _Client:
    def __init__(self, positions, orders=None):
        self._positions = positions
        self._orders = orders or []
        self.orders = []
        self.cancelled = []

    def get_all_positions(self):
        return self._positions

    def get_orders(self):
        return self._orders

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)

    def submit_order(self, req):
        self.orders.append(req)
        return SimpleNamespace(id=f"staged-{len(self.orders)}")


def _make_executor(current_price, entry_price):
    ex = EnhancedExecutor.__new__(EnhancedExecutor)
    ex.client = _Client([_Pos("STAGE", 10, current_price)])
    ex.order_cache = {}
    ex._entry_pending = {}
    ex._pending_entry_signals = {}
    ex._entries_today = {}
    ex._entries_today_date = None
    ex._no_history_cache = set()
    ex._entry_log = {}
    ex._staged_allocation = {
        "STAGE": {
            "tranches_done": 1,
            "tranche_qty": 5,
            "is_long": True,
            "entry_price": entry_price,
            "total_tranches": 4,
        }
    }
    ex._recent_entry_submits = {}
    ex._symbol_loss_counts_today = {}
    ex._loss_block_morning = set()
    ex._loss_block_day = set()
    ex._loss_block_date = None
    return ex


_orig_gate = enhanced._check_ema_trend_alignment

# --- Never adding while losing: gain <= 0 -> no tranche submitted ---
enhanced._check_ema_trend_alignment = lambda sig, is_long, force_fresh=False: (True, None)
try:
    ex = _make_executor(current_price=10.00, entry_price=10.00)  # flat, gain 0
    ex.maybe_add_staged_tranches()
    assert ex.client.orders == [], "flat position must NOT get a tranche (never adding while losing)"
    assert ex._staged_allocation["STAGE"]["tranches_done"] == 1, "state kept for later recovery"

    ex = _make_executor(current_price=9.50, entry_price=10.00)  # losing
    ex.maybe_add_staged_tranches()
    assert ex.client.orders == [], "losing position must NOT get a tranche"

    # --- Winning (gain > 0) -> next tranche added ---
    ex = _make_executor(current_price=10.20, entry_price=10.00)  # +2%
    ex.maybe_add_staged_tranches()
    assert len(ex.client.orders) == 1, "winning position should get one tranche"
    req = ex.client.orders[-1]
    assert isinstance(req, TrailingStopOrderRequest)
    assert req.qty == 5
    assert req.side == OrderSide.BUY
    assert req.time_in_force == TimeInForce.DAY
    assert ex._staged_allocation["STAGE"]["tranches_done"] == 2

    # --- Fresh EMA check immediately before each tranche ---
    ex = _make_executor(current_price=10.20, entry_price=10.00)
    enhanced._check_ema_trend_alignment = lambda sig, is_long, force_fresh=False: (False, "trend not aligned")
    ex.maybe_add_staged_tranches()
    assert ex.client.orders == [], "fresh EMA gate failing must block the tranche"
    assert ex._staged_allocation["STAGE"]["tranches_done"] == 1

    # --- No open position -> staged state dropped ---
    enhanced._check_ema_trend_alignment = lambda sig, is_long, force_fresh=False: (True, None)
    ex = _make_executor(current_price=10.20, entry_price=10.00)
    ex.client._positions = []  # position gone
    ex.maybe_add_staged_tranches()
    assert ex.client.orders == []
    assert "STAGE" not in ex._staged_allocation, "no open position must end staging"

    # --- All tranches done -> state dropped, nothing submitted ---
    ex = _make_executor(current_price=10.20, entry_price=10.00)
    ex._staged_allocation["STAGE"]["tranches_done"] = 4
    ex.maybe_add_staged_tranches()
    assert ex.client.orders == []
    assert "STAGE" not in ex._staged_allocation, "fully staged symbol must be cleaned up"
finally:
    enhanced._check_ema_trend_alignment = _orig_gate

# --- Scale-in path: first-entry bookkeeping must NOT block tranches ----------
# Real first-entry-to-scale-in sequence: the first entry stamps
# _recent_entry_submits["STAGE"] and order_cache["STAGE"]="first-entry-id";
# those are FIRST-ENTRY guards, so maybe_add_staged_tranches submits tranches
# via _submit_entry_order(scale_in=True), which bypasses exactly those two.
enhanced._check_ema_trend_alignment = lambda sig, is_long, force_fresh=False: (True, None)
try:
    # 1. first-entry debounce + order_cache present -> tranche 2 still added
    ex = _make_executor(current_price=10.20, entry_price=10.00)
    ex._recent_entry_submits["STAGE"] = time.monotonic()
    ex.order_cache["STAGE"] = "first-entry-id"
    ex.maybe_add_staged_tranches()
    assert len(ex.client.orders) == 1, \
        "first-entry debounce/order_cache must not block a scale-in tranche"
    assert ex._staged_allocation["STAGE"]["tranches_done"] == 2
    assert ex.order_cache["STAGE"] == "staged-1", \
        "scale-in tranche must take over the order_cache slot (active-order tracking)"
    assert ex._recent_entry_submits["STAGE"] is not None, \
        "scale-in submit still stamps the debounce (protects later fresh entries)"

    # 2. a still-open local pending entry signal STILL blocks a tranche --
    #    only the first-entry debounce/cache are bypassed, nothing else.
    ex = _make_executor(current_price=10.20, entry_price=10.00)
    ex._recent_entry_submits["STAGE"] = time.monotonic()
    ex.order_cache["STAGE"] = "first-entry-id"
    ex._pending_entry_signals["STAGE"] = {"signal": None, "order_type": None}
    ex.maybe_add_staged_tranches()
    assert ex.client.orders == [], \
        "a still-open local pending entry signal must block the tranche"
    assert ex._staged_allocation["STAGE"]["tranches_done"] == 1

    # 3. an active same-symbol DAY order at the broker STILL blocks a tranche
    #    (resting unfilled first tranche -> never a duplicate add).
    ex = _make_executor(current_price=10.20, entry_price=10.00)
    ex._recent_entry_submits["STAGE"] = time.monotonic()
    ex.order_cache["STAGE"] = "first-entry-id"

    class _Active:
        id, symbol, status = "first-entry-id", "STAGE", "new"
        side = enhanced.OrderSide.BUY
        time_in_force = enhanced.TimeInForce.DAY
        order_type = "trailing_stop"

    ex.client._orders = [_Active()]
    ex.maybe_add_staged_tranches()
    assert ex.client.orders == [], \
        "an active broker order for the symbol must block the tranche"
    assert ex._staged_allocation["STAGE"]["tranches_done"] == 1

    # 4. guard the guard: a FRESH entry (scale_in=False) is still debounced by
    #    _recent_entry_submits and blocked by order_cache.
    ex = _make_executor(current_price=10.20, entry_price=10.00)
    ex._entry_submission_lock = None
    ex._halt_until_eod = False
    ex._loss_halted_cache = False
    ex._loss_halted_cache_ts = time.time()
    ex.market_state = None
    ex._recent_entry_submits["STAGE"] = time.monotonic()
    ex.order_cache["STAGE"] = "first-entry-id"
    req = TrailingStopOrderRequest(
        symbol="STAGE", qty=5, side=OrderSide.BUY, type=AlpacaOrderType.TRAILING_STOP,
        time_in_force=TimeInForce.DAY, trail_percent=enhanced.REENTRY_TRAIL_PCT,
        client_order_id="apex-fresh-STAGE-test",
    )
    out = ex._submit_entry_order("STAGE", req)
    assert out is None, "fresh-entry path must remain debounced/blocked by first-entry state"
finally:
    enhanced._check_ema_trend_alignment = _orig_gate


# --- Cancel stale/opposite orders before entry ---
# A resting SELL DAY order must be cancelled before a fresh LONG entry; a GTC
# protective trailing stop must be left alone.
_ex = EnhancedExecutor.__new__(EnhancedExecutor)
_ex.client = _Client(
    positions=[],
    orders=[
        _Order("opp-sell", "OPP", enhanced.OrderSide.SELL, enhanced.TimeInForce.DAY),
        _Order("gtc-stop", "OPP", enhanced.OrderSide.SELL, enhanced.TimeInForce.GTC, order_type="trailing_stop"),
    ],
)
_ex.order_cache = {}
_ex._entry_pending = {}
_ex._pending_entry_signals = {}
_ex._recent_entry_submits = {}
_ex._cancel_opposite_orders_before_entry("OPP", is_long=True)
assert "opp-sell" in _ex.client.cancelled, "opposite-side DAY order must be cancelled before entry"
assert "gtc-stop" not in _ex.client.cancelled, "GTC protective stop must never be cancelled before entry"

# A resting BUY DAY order must be cancelled before a fresh SHORT entry.
_ex2 = EnhancedExecutor.__new__(EnhancedExecutor)
_ex2.client = _Client(
    positions=[],
    orders=[_Order("opp-buy", "OPP2", enhanced.OrderSide.BUY, enhanced.TimeInForce.DAY)],
)
_ex2.order_cache = {}
_ex2._entry_pending = {}
_ex2._pending_entry_signals = {}
_ex2._recent_entry_submits = {}
_ex2._cancel_opposite_orders_before_entry("OPP2", is_long=False)
assert "opp-buy" in _ex2.client.cancelled, "opposite-side BUY order must be cancelled before a short entry"

# Same-side DAY order is NOT cancelled (it's the entry itself, or a duplicate
# the _submit_entry_order guard will handle).
_ex3 = EnhancedExecutor.__new__(EnhancedExecutor)
_ex3.client = _Client(
    positions=[],
    orders=[_Order("same-buy", "SAME", enhanced.OrderSide.BUY, enhanced.TimeInForce.DAY)],
)
_ex3.order_cache = {}
_ex3._entry_pending = {}
_ex3._pending_entry_signals = {}
_ex3._recent_entry_submits = {}
_ex3._cancel_opposite_orders_before_entry("SAME", is_long=True)
assert "same-buy" not in _ex3.client.cancelled, "same-side order must not be cancelled before a long entry"

print("OK: staged allocation scales in 4 x 25%, never adds while losing, re-checks the EMA gate before each tranche, "
      "and cancels opposite-side stale orders before entry (GTC stops untouched)")

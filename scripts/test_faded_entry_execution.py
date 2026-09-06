"""Self-check for the faded/stale-entry execution fix (2026-08-17, CDTG:
bought $2.97 marketable into a signal already flagged "faded 9.8% off its
30-min high", price kept falling to $2.73 with ZERO stop coverage for
3+ minutes before protect_positions() finally armed a stop off the
already-fallen price -- realized -11.7% vs. the intended 4% trail).

Three independent pieces, all scoped to Signal.stale_entry specifically
(NOT the broader thin_liquidity flag, which also covers guardrail-floor
admits on fresh signals -- confirmed live 2026-08-17: CDTG trade 1 +$1.64
and both FIEE trades were thin_liquidity but NOT faded, and would have been
needlessly delayed/risked by a fix scoped to the wider flag):

1. _create_bracket_order: 2026-08-22, user request -- ALL entries (faded,
   thin, fresh) now submit the same TrailingStopOrderRequest at
   REENTRY_TRAIL_PCT regardless of stale_entry, and never touch
   _entry_pending at all. The passive-limit/price_ceiling behavior this
   point used to describe is gone from this function; still covered below
   for _sweep_pending_entries via the (untouched) SWAP path.
2. _sweep_pending_entries: a faded pending entry gets FADED_ENTRY_
   PASSIVE_WINDOW_SECONDS before any chase, then a chase capped at
   price_ceiling until FADED_ENTRY_CEILING_TIMEOUT_SECONDS, then uncapped.
   A partial fill only re-chases the REMAINING qty, not the original size.
3. _cover_naked_positions: registers a software stop for any open position
   without order coverage, off the REAL avg_entry_price -- pure bookkeeping,
   no broker order attempt. protect_positions() pops it once a real GTC
   stop lands.

Run with:
  python scripts/test_faded_entry_execution.py
No network calls -- broker client is stubbed throughout.
"""
import sys
import datetime
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.execution.enhanced as ee
from engine.execution.enhanced import EnhancedExecutor, OrderType, LimitOrderRequest, TrailingStopOrderRequest
from engine.equity.strategies import Signal
from engine.config import (
    FADED_ENTRY_PASSIVE_WINDOW_SECONDS, FADED_ENTRY_CEILING_TIMEOUT_SECONDS,
    AFTERHOURS_CHASE_STALE_SECONDS, MARKETABLE_LIMIT_BUFFER_PCT, REENTRY_TRAIL_PCT,
)

assert FADED_ENTRY_PASSIVE_WINDOW_SECONDS == 90
assert FADED_ENTRY_CEILING_TIMEOUT_SECONDS == 300

UTC = datetime.timezone.utc


def _sig(strategy="EarlySqueeze", thin=False, stale=False):
    return Signal("CDTG", "buy", 2.94, 0.90, "test", strategy, thin_liquidity=thin, stale_entry=stale)


# --- Signal.stale_entry defaults + _resolve_freshness_reject scoping ---

assert _sig().stale_entry is False, "must default False"

# 2026-08-26: config default flipped to False (hard reject). The faded-entry
# trade-through path this section exercises only exists when the toggle is on,
# so set it explicitly for these assertions and restore afterward.
_orig_toggle = ee.TRADE_STALE_MOMENTUM_REJECTS
ee.TRADE_STALE_MOMENTUM_REJECTS = True
try:
    sig = _sig()
    valid, reason = ee._resolve_freshness_reject(sig, fresh=False, fade_reason="faded 9.8% off its 30-min high")
    assert valid is True and reason is None
    assert sig.thin_liquidity is True and sig.stale_entry is True, "faded path sets BOTH flags"

    sig_fresh = _sig()
    valid, reason = ee._resolve_freshness_reject(sig_fresh, fresh=True, fade_reason=None)
    assert sig_fresh.thin_liquidity is False and sig_fresh.stale_entry is False, "fresh signal untouched"
finally:
    ee.TRADE_STALE_MOMENTUM_REJECTS = _orig_toggle


# --- _create_bracket_order: 2026-08-22, user request -- ALL entries (faded,
#     thin, fresh, long, short) now submit the same TrailingStopOrderRequest
#     at REENTRY_TRAIL_PCT; the passive-limit/ceiling path this section used
#     to check no longer exists in _create_bracket_order (still exercised by
#     _sweep_pending_entries below, via the SWAP path's _create_simple_order,
#     which is untouched). ---

class _Quote:
    def __init__(self, bid, ask):
        self.bid_price, self.ask_price = bid, ask


class _FakeClient:
    def __init__(self, quote):
        self._q = quote
        self.orders = []

    def get_latest_quote(self, symbol):
        return self._q

    def submit_order(self, req):
        self.orders.append(req)
        return SimpleNamespace(id=f"fake-{len(self.orders)}")


def _executor(quote):
    ex = EnhancedExecutor.__new__(EnhancedExecutor)
    ex.client = _FakeClient(quote)
    ex.order_cache = {}
    ex._entry_pending = {}
    ex._pending_entry_signals = {}
    ex._tp_targets = {}
    ex._entries_today = {}       # 2026-08-18: _create_bracket_order now checks
    ex._entries_today_date = None  # is_reentry -- empty here, these are all first entries
    ex._no_history_cache = set()
    ex._entry_log = {}
    return ex


risk_info = {"dollar_amount": 63.0, "allocation_pct": 4.0, "tier": "NORMAL", "stop_loss_pct": 4.0, "atr_pct": 0}
_orig_live = ee.LIVE
ee.LIVE = True  # matches production: defer the broker-side stop, no 2nd submit_order to stub
try:
    # Faded (stale_entry=True): same trailing-buy as any other entry now, no ceiling stored.
    ex = _executor(_Quote(2.93, 2.95))  # mid = 2.94 -- irrelevant to a trailing entry
    sig = _sig(stale=True)
    ok = ex._create_bracket_order(sig, 21, risk_info, OrderType.LONG)
    assert ok is True
    req = ex.client.orders[-1]
    assert isinstance(req, TrailingStopOrderRequest), f"expected a trailing-buy entry, got {type(req).__name__}"
    assert req.trail_percent == REENTRY_TRAIL_PCT
    assert req.side == ee.OrderSide.BUY
    assert ex._entry_pending == {}, "_create_bracket_order must never populate _entry_pending anymore"

    # Not faded, even thin_liquidity=True: identical trailing-buy, flags don't change the entry order.
    ex2 = _executor(_Quote(2.93, 2.95))
    sig2 = _sig(strategy="GapBreakout", thin=True, stale=False)
    ok = ex2._create_bracket_order(sig2, 21, risk_info, OrderType.LONG)
    assert ok is True
    req2 = ex2.client.orders[-1]
    assert isinstance(req2, TrailingStopOrderRequest) and req2.trail_percent == REENTRY_TRAIL_PCT
    assert ex2._entry_pending == {}

    # Short direction: same trailing mechanism, SELL side.
    ex3 = _executor(_Quote(9.98, 10.02))  # mid = 10.00 -- irrelevant to a trailing entry
    sig3 = _sig(stale=True)
    sig3.action = "short"
    ok = ex3._create_bracket_order(sig3, 5, risk_info, OrderType.SHORT)
    assert ok is True
    req3 = ex3.client.orders[-1]
    assert isinstance(req3, TrailingStopOrderRequest) and req3.side == ee.OrderSide.SELL
    assert req3.trail_percent == REENTRY_TRAIL_PCT
finally:
    ee.LIVE = _orig_live


# --- _sweep_pending_entries: window, cap, direction-aware cap, partial-fill ---

class _Order:
    def __init__(self, id, symbol, limit_price, submitted_at, filled_qty=0):
        self.id, self.symbol, self.limit_price = id, symbol, limit_price
        self.submitted_at = self.created_at = submitted_at
        self.filled_qty = filled_qty


class _SweepClient:
    def __init__(self, orders, quote):
        self._orders = orders
        self._q = quote
        self.cancelled = []
        self.submitted = []

    def get_orders(self):
        return self._orders

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)

    def get_latest_quote(self, symbol):
        return self._q

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id=f"resub-{len(self.submitted)}")


now = datetime.datetime.now(UTC)


def _sweep_ex(order, quote, entry_pending_extra=None):
    ex = EnhancedExecutor.__new__(EnhancedExecutor)
    ex.client = _SweepClient([order], quote)
    ex.order_cache = {}
    info = {"order_id": order.id, "qty": 21, "is_long": True, "chase_count": 0}
    if entry_pending_extra:
        info.update(entry_pending_extra)
    ex._entry_pending = {"CDTG": info}
    return ex


import time as _time
_orig_sleep = _time.sleep
ee.time.sleep = lambda *_: None  # skip the real 0.4s pause between cancel/resubmit
_orig_lunch_break = ee.in_lunch_break
ee.in_lunch_break = lambda *_: False  # never inside the midday break during this test run

try:
    # Faded, still within the 90s passive window -> untouched, no cancel/resubmit.
    order = _Order("o1", "CDTG", 2.91, now - datetime.timedelta(seconds=60))
    ex = _sweep_ex(order, _Quote(2.85, 2.87), {"price_ceiling": 2.97, "first_submitted_at": now - datetime.timedelta(seconds=60)})
    ex._sweep_pending_entries()
    assert ex.client.cancelled == [], "must not touch a faded entry before its passive window elapses"

    # Faded, non-faded default (AFTERHOURS_CHASE_STALE_SECONDS) does NOT yet apply -- 60s < 90s window.
    assert AFTERHOURS_CHASE_STALE_SECONDS < FADED_ENTRY_PASSIVE_WINDOW_SECONDS, \
        "test assumes the faded window is longer than the normal stale threshold"

    # Faded, past 90s, still unfilled -> cancelled and re-chased as the same
    # trailing entry every current path uses (REENTRY_TRAIL_PCT), remaining qty.
    order = _Order("o2", "CDTG", 2.91, now - datetime.timedelta(seconds=100))
    ex = _sweep_ex(order, _Quote(2.79, 2.81), {"price_ceiling": 2.97, "first_submitted_at": now - datetime.timedelta(seconds=100)})
    ex._sweep_pending_entries()
    assert ex.client.cancelled == ["o2"]
    req = ex.client.submitted[-1]
    assert isinstance(req, TrailingStopOrderRequest), f"re-chase must convert to a trailing entry, got {type(req).__name__}"
    assert req.trail_percent == REENTRY_TRAIL_PCT
    assert req.side == ee.OrderSide.BUY

    # Faded, past 90s, price REVERSED UP (mid now 3.10) -> still the same
    # trailing entry; the broker-side trailing order won't chase the reversal.
    order = _Order("o3", "CDTG", 2.91, now - datetime.timedelta(seconds=100))
    ex = _sweep_ex(order, _Quote(3.09, 3.11), {"price_ceiling": 2.97, "first_submitted_at": now - datetime.timedelta(seconds=100)})
    ex._sweep_pending_entries()
    req = ex.client.submitted[-1]
    assert isinstance(req, TrailingStopOrderRequest) and req.trail_percent == REENTRY_TRAIL_PCT

    # Faded, past the 5-min ceiling timeout, price still up -> same trailing
    # entry, no limit cap involved anymore.
    order = _Order("o4", "CDTG", 2.91, now - datetime.timedelta(seconds=310))
    ex = _sweep_ex(order, _Quote(3.09, 3.11), {"price_ceiling": 2.97, "first_submitted_at": now - datetime.timedelta(seconds=310)})
    ex._sweep_pending_entries()
    req = ex.client.submitted[-1]
    assert isinstance(req, TrailingStopOrderRequest) and req.trail_percent == REENTRY_TRAIL_PCT

    # Non-faded entry: normal AFTERHOURS_CHASE_STALE_SECONDS threshold, re-chased
    # as the same trailing entry.
    order = _Order("o5", "AAPL", 100.0, now - datetime.timedelta(seconds=AFTERHOURS_CHASE_STALE_SECONDS + 5))
    ex = _sweep_ex(order, _Quote(101.0, 101.2))
    ex._sweep_pending_entries()
    assert len(ex.client.submitted) == 1, "normal entries must still re-chase on the shorter threshold"
    assert isinstance(ex.client.submitted[-1], TrailingStopOrderRequest), "normal (non-faded) chase is the same trailing entry"

    # Partial fill: only the REMAINING qty gets re-chased, not the original full size.
    order = _Order("o6", "CDTG", 2.91, now - datetime.timedelta(seconds=100), filled_qty=15)
    ex = _sweep_ex(order, _Quote(2.85, 2.87), {"price_ceiling": 2.97, "first_submitted_at": now - datetime.timedelta(seconds=100)})
    ex._entry_pending["CDTG"]["qty"] = 21
    ex._sweep_pending_entries()
    req = ex.client.submitted[-1]
    assert req.qty == 6, f"expected only the 6 unfilled shares re-chased (21-15), got {req.qty}"

    # Fully filled between polls: popped, no phantom re-chase at all.
    order = _Order("o7", "CDTG", 2.91, now - datetime.timedelta(seconds=100), filled_qty=21)
    ex = _sweep_ex(order, _Quote(2.85, 2.87), {"price_ceiling": 2.97, "first_submitted_at": now - datetime.timedelta(seconds=100)})
    ex._entry_pending["CDTG"]["qty"] = 21
    ex._sweep_pending_entries()
    assert ex.client.submitted == [] and ex.client.cancelled == [], "a fully-filled resting order must not be cancelled/re-chased"
    assert "CDTG" not in ex._entry_pending
finally:
    ee.time.sleep = _orig_sleep
    ee.in_lunch_break = _orig_lunch_break


# --- _cover_naked_positions: registers off REAL avg_entry_price, skips covered/options ---

class _Pos:
    def __init__(self, symbol, qty, avg_entry_price, current_price):
        self.symbol, self.qty = symbol, qty
        self.avg_entry_price, self.current_price = avg_entry_price, current_price


class _CoverClient:
    def __init__(self, positions, open_orders):
        self._positions, self._orders = positions, open_orders

    def get_all_positions(self):
        return self._positions

    def get_orders(self):
        return self._orders


_orig_trail = ee._atr_trail_pct_for
ee._atr_trail_pct_for = lambda symbol, price, entry_log: (4.0, "NORMAL/THIN")  # stub out the ATR/bars lookup
try:
    # The "already covered" AAPL order is a realistic broker-side GTC trailing
    # stop (the coverage _cover_naked_positions looks for) -- side/side enum,
    # order_type "trailing_stop", GTC time_in_force, matching trail_percent.
    aapl_order = SimpleNamespace(
        id="AAPL-order", symbol="AAPL",
        order_type="trailing_stop",
        order_class="simple",
        side=ee.OrderSide.SELL,
        time_in_force=ee.TimeInForce.GTC,
        trail_percent=4.0,
    )
    ex = EnhancedExecutor.__new__(EnhancedExecutor)
    ex._pdt_stop_blocked = {}
    ex._entry_log = {}
    ex.client = _CoverClient(
        positions=[
            _Pos("CDTG", 21, 2.91, 2.85),                       # naked -- must get registered
            _Pos("TTD", -64, 13.76, 13.50),                     # naked short -- must get registered, correct direction
            _Pos("AAPL", 10, 100.0, 101.0),                     # already covered by an order -- skip
            _Pos("AAPL260116C00150000", 1, 5.0, 5.0),           # OCC option -- skip
        ],
        open_orders=[aapl_order],
    )
    ex._cover_naked_positions()
    assert ex._pdt_stop_blocked["CDTG"] == round(2.91 * 0.96, 2), f"long stop should be 4% below entry, got {ex._pdt_stop_blocked.get('CDTG')}"
    assert ex._pdt_stop_blocked["TTD"] == round(13.76 * 1.04, 2), f"short stop should be 4% above entry, got {ex._pdt_stop_blocked.get('TTD')}"
    assert "AAPL" not in ex._pdt_stop_blocked, "already-covered symbol must not get a redundant software stop"
    assert "AAPL260116C00150000" not in ex._pdt_stop_blocked, "options legs must be skipped"

    # Second tick: CDTG already tracked -> must not be re-registered (idempotent, no log spam).
    ex._pdt_stop_blocked["CDTG"] = 999.99  # sentinel: if overwritten, the guard failed
    ex._cover_naked_positions()
    assert ex._pdt_stop_blocked["CDTG"] == 999.99, "already-tracked symbol must be left alone, not re-registered"
finally:
    ee._atr_trail_pct_for = _orig_trail

print("OK: Signal.stale_entry scoping, flat trailing-buy entry (faded/thin/fresh, long + short), "
      "partial-fill-safe re-chase (SWAP path via _create_simple_order), and _cover_naked_positions all check out")

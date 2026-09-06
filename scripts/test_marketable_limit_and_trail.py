"""Self-check for the live-mid bounded-limit-price fix and the
thin-liquidity trailing-stop halving (2026-08-12).

Bounded limit price: entries/exits during regular hours used to be plain
MarketOrderRequest -- no price bound, so a wide bid-ask spread gets absorbed
in full (NBIL 2026-08-12: bought $28.76, the exact high tick of that 5-min
bar). Now _create_bracket_order/_create_simple_order/_submit_closing_order
all price off a LIVE bid/ask quote (_live_quote_mid) bounded within
MARKETABLE_LIMIT_BUFFER_PCT (1%) via _marketable_limit_price, instead of a
possibly-stale signal.price/pos.current_price -- the whole point of "delayed
entries" is that the scan-time reference can be seconds to minutes old by
the time the order reaches the broker.

Trailing-stop halving: a symbol admitted only via TRADE_THIN_LIQUIDITY_REJECTS
(signal.thin_liquidity=True) gets HALF the normal dynamic-tier trail% for its
whole life (entry, protect_positions, ratchet, after-hours virtual-stop, all
re-arm fallbacks) via the single _trail_pct_for() helper, not 6 separate
get_dynamic_tier() call sites that could drift out of sync.

Run with:
  python scripts/test_marketable_limit_and_trail.py
No network calls -- client / get_dynamic_tier / MarketState are stubbed.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import MARKETABLE_LIMIT_BUFFER_PCT, THIN_LIQUIDITY_TRAILING_STOP_MULT
import engine.execution.enhanced as ee
from engine.execution.enhanced import (
    _marketable_limit_price, _live_quote_mid, _trail_pct_for,
    _apply_thin_liquidity_override,
)
from engine.equity.strategies import Signal

assert MARKETABLE_LIMIT_BUFFER_PCT == 1.0, f"expected 1% buffer (user spec), config has {MARKETABLE_LIMIT_BUFFER_PCT}"
assert THIN_LIQUIDITY_TRAILING_STOP_MULT == 0.5, f"expected half, config has {THIN_LIQUIDITY_TRAILING_STOP_MULT}"


# --- _marketable_limit_price(): bounded, direction-aware ---
assert _marketable_limit_price(100.0, is_long=True) == 101.0, "buy/cover should round UP by the buffer"
assert _marketable_limit_price(100.0, is_long=False) == 99.0, "sell/short should round DOWN by the buffer"
assert _marketable_limit_price(100.0, is_long=True, buffer_pct=0.2) == 100.2, "custom buffer_pct honored"


# --- _live_quote_mid(): live quote wins, any failure/missing side falls back ---
class _Quote:
    def __init__(self, bid, ask):
        self.bid_price, self.ask_price = bid, ask


class _QuoteClient:
    def __init__(self, quote_or_exc):
        self._q = quote_or_exc

    def get_latest_quote(self, symbol):
        if isinstance(self._q, Exception):
            raise self._q
        return self._q


assert _live_quote_mid(_QuoteClient(_Quote(9.98, 10.02)), "TEST", fallback=999) == 10.0, "should average a healthy quote"
assert _live_quote_mid(_QuoteClient(RuntimeError("boom")), "TEST", fallback=42.0) == 42.0, "quote call failing falls back"
assert _live_quote_mid(_QuoteClient(_Quote(0, 10.02)), "TEST", fallback=42.0) == 42.0, "missing bid falls back"
assert _live_quote_mid(_QuoteClient(_Quote(9.98, 0)), "TEST", fallback=42.0) == 42.0, "missing ask falls back"


# --- _trail_pct_for(): 2026-08-22, user request -- tiers/thin-liquidity
#     halving removed, replaced by a flat floor that widens to
#     PROFIT_TRAIL_GIVEBACK_PCT of unrealized gain once that's wider ---
from engine.config import TRAIL_STOP_PCT, PROFIT_TRAIL_GIVEBACK_PCT

pct, label = _trail_pct_for("AAA", 10.0, entry_log={})
assert (pct, label) == (TRAIL_STOP_PCT, "FLAT"), f"no gain info -> flat floor, got {(pct, label)}"
pct, label = _trail_pct_for("BBB", 10.0, entry_log={}, gain_pct=-3.0)
assert (pct, label) == (TRAIL_STOP_PCT, "FLAT"), f"losing position -> flat floor, got {(pct, label)}"
pct, label = _trail_pct_for("CCC", 10.0, entry_log={}, gain_pct=10.0)
expected = round(10.0 * PROFIT_TRAIL_GIVEBACK_PCT / 100.0, 2)
assert (pct, label) == (expected, "PROFIT"), f"+10% gain -> {expected}% widened stop, got {(pct, label)}"


# --- _apply_thin_liquidity_override(): sizing AND (when present) stop_loss_pct both halve ---
def _sig(thin=False):
    return Signal("TEST", "buy", 10.0, 0.90, "test reason", "TestStrat", thin_liquidity=thin)


risk_info = {"dollar_amount": 150.0, "allocation_pct": 10.0, "tier": "NORMAL", "stop_loss_pct": 4.0}
out = _apply_thin_liquidity_override(risk_info, _sig(thin=True), equity=2000.0)
assert out["dollar_amount"] == 80.0, f"expected 4% of $2000 = $80, got {out['dollar_amount']}"
assert out["stop_loss_pct"] == 2.0, f"expected stop_loss_pct halved 4.0 -> 2.0, got {out['stop_loss_pct']}"
assert risk_info["stop_loss_pct"] == 4.0, "original dict must not be mutated in place"

risk_info_no_sl = {"dollar_amount": 150.0, "allocation_pct": 7.5, "tier": "NORMAL"}  # live-mode risk_info has no stop_loss_pct key
out = _apply_thin_liquidity_override(risk_info_no_sl, _sig(thin=True), equity=2000.0)
assert "stop_loss_pct" not in out, "must not invent a stop_loss_pct key that wasn't there"

out = _apply_thin_liquidity_override(risk_info, _sig(thin=False), equity=2000.0)
assert out is risk_info, "unflagged signal should return the exact same dict, not a copy"


# --- _create_simple_order(): regular hours now submits a bounded LimitOrderRequest
#     off the LIVE mid, never MarketOrderRequest, and ignores stale signal.price ---
class _FakeTradeClient:
    def __init__(self, quote):
        self._q = quote
        self.orders = []

    def get_latest_quote(self, symbol):
        return self._q

    def submit_order(self, req):
        self.orders.append(req)
        return SimpleNamespace(id="fake-order-id")


ex = ee.EnhancedExecutor.__new__(ee.EnhancedExecutor)  # skip __init__, no broker creds needed
ex.client = _FakeTradeClient(_Quote(9.99, 10.01))       # live mid = 10.00
ex.order_cache = {}
ex._entry_pending = {}
ex._pending_entry_signals = {}
ex._current_market_state = lambda: SimpleNamespace(is_regular_hours=True)
sig = Signal("ZZZ", "buy", 10.50, 0.8, "test", "TestStrat")  # signal.price deliberately stale vs. live mid
assert ex._create_simple_order(sig, 10, ee.OrderType.LONG) is True
req = ex.client.orders[-1]
assert isinstance(req, ee.MarketOrderRequest), f"fallback entry should use a bracket market order, not {type(req).__name__}"
assert req.order_class == ee.OrderClass.BRACKET
assert req.time_in_force == ee.TimeInForce.DAY
assert ex._pending_entry_signals["ZZZ"]["signal"] is sig

# 2026-08-18, user request: entries never use extended hours anymore, even
# outside regular hours and even with EXTENDED_HOURS=True (that flag now only
# governs exit paths) -- confirm the outside-regular-hours branch still
# submits a bounded limit, just never marked extended_hours.
_orig_extended = ee.EXTENDED_HOURS
ee.EXTENDED_HOURS = True
try:
    ex.client.orders.clear()
    ex._pending_entry_signals = {}
    ex._current_market_state = lambda: SimpleNamespace(is_regular_hours=False)
    sig_short = Signal("YYY", "short", 50.0, 0.8, "test", "TestStrat")
    assert ex._create_simple_order(sig_short, 5, ee.OrderType.SHORT) is True
    req = ex.client.orders[-1]
    assert isinstance(req, ee.MarketOrderRequest)
    assert req.order_class == ee.OrderClass.BRACKET
    assert req.side == ee.OrderSide.SELL
finally:
    ee.EXTENDED_HOURS = _orig_extended


# --- _submit_closing_order(): regular hours now bounded too, off the LIVE mid ---
_orig_market_state = ee.MarketState
ee.MarketState = SimpleNamespace(from_now=lambda: SimpleNamespace(is_regular_hours=True))
try:
    ex2 = ee.EnhancedExecutor.__new__(ee.EnhancedExecutor)
    ex2.client = _FakeTradeClient(_Quote(19.98, 20.02))  # live mid = 20.00
    ex2._submit_closing_order("ZZZ", 5, ee.OrderSide.SELL, current_price=999.0, slip_pct=0.5)
    req = ex2.client.orders[-1]
    assert isinstance(req, ee.LimitOrderRequest), f"regular-hours close must be a bounded limit, not {type(req).__name__}"
    assert req.limit_price == 19.90, f"expected 0.5% below the LIVE mid (20.00 -> 19.90), not stale current_price=999; got {req.limit_price}"
    assert req.extended_hours is False, "no override -> falls back to the is_regular_hours-derived flag"

    # force_extended_hours=True (EOD/guardrail force-closes): must survive past
    # the close even though the call itself happens during regular hours.
    ex2.client.orders.clear()
    ex2._submit_closing_order("ZZZ", 5, ee.OrderSide.SELL, current_price=999.0, force_extended_hours=True)
    req = ex2.client.orders[-1]
    assert req.extended_hours is True, "force_extended_hours=True must override the regular-hours default"
finally:
    ee.MarketState = _orig_market_state

print("OK: bounded-limit-price helpers (live mid, 1% bound), force_extended_hours override, and thin-liquidity trailing-stop halving all check out")

"""Self-check for the price-drift-stop age gate (2026-08-18, user request:
"check against the buyin price for the first 30mins, as the drift check is
w.r.t 30min before price").

_backfill_drift_reference (and the rolling in-memory deque, before it's had
30 minutes to fill) reconstruct "PRICE_DRIFT_LOOKBACK_MIN minutes ago" from
calendar time, not time-since-entry. For a position younger than that, the
reference lands BEFORE entry -- bars that have nothing to do with the trade
(a LiquiditySweep long entered right after a sweep-low routinely has a HIGHER
price somewhere in the 30 min before entry than at entry itself, so a
since-entry-flat position could "drift" >1% against a pre-entry high it was
never actually exposed to). check_price_drift_stop() must not use that
reference leg until the position itself has been held PRICE_DRIFT_LOOKBACK_MIN
minutes -- the entry-price leg alone covers it until then.

Run with:
  python scripts/test_price_drift_young_position.py
No network calls -- client/get_bars are stubbed.
"""
import sys
import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.execution.enhanced as enhanced
from engine.config import PRICE_DRIFT_LOOKBACK_MIN


class _Pos:
    def __init__(self, symbol, qty, current_price, avg_entry_price):
        self.symbol = symbol
        self.qty = qty
        self.current_price = current_price
        self.avg_entry_price = avg_entry_price


class _Client:
    def __init__(self, positions):
        self._positions = positions
    def get_all_positions(self):
        return self._positions


def _make_executor(entry_dt, current_price, entry_price=10.00):
    ex = enhanced.EnhancedExecutor.__new__(enhanced.EnhancedExecutor)
    ex.client = _Client([_Pos("TEST", 10, current_price, entry_price)])
    ex._entry_log = {"TEST": {"date": datetime.date.today(), "filled_at": entry_dt, "strategy": "Test", "confidence": 0.9}}
    ex._price_drift_history = {}
    return ex


_backfill_calls = {"n": 0}
def _spy_backfill(self, symbol):
    _backfill_calls["n"] += 1
    # A stale pre-entry HIGH the position was never actually exposed to --
    # if this leaks through for a young position it produces a false trigger.
    return 10.50


orig_backfill = enhanced.EnhancedExecutor._backfill_drift_reference
enhanced.EnhancedExecutor._backfill_drift_reference = _spy_backfill
# 2026-08-22, user request: PRICE_DRIFT_STOP_ENABLED now defaults False
# (replaced by the flat/profit-scaled trailing stop + the 1-min stagnant
# check) -- the age-gating logic under test still lives in
# check_price_drift_stop() and stays correct if it's ever re-enabled, so
# force it on for this test rather than deleting coverage for working code.
orig_enabled = enhanced.PRICE_DRIFT_STOP_ENABLED
enhanced.PRICE_DRIFT_STOP_ENABLED = True

try:
    now = datetime.datetime.now(datetime.timezone.utc)

    # --- Young position (10 min old): reference leg must be skipped, not
    #     backfilled -- current price is flat vs entry (no entry-leg trigger
    #     either), so nothing should fire, and backfill must never be called. ---
    _backfill_calls["n"] = 0
    ex = _make_executor(entry_dt=now - datetime.timedelta(minutes=10), current_price=10.02, entry_price=10.00)
    ex.check_price_drift_stop()
    assert _backfill_calls["n"] == 0, "a young position must not fall through to the pre-entry backfill reference"

    # --- Old position (45 min old, well past PRICE_DRIFT_LOOKBACK_MIN=30):
    #     no in-memory history yet either -> the backfill path is legitimate
    #     here (a real "30 min ago, since entry" reference) and must run. ---
    assert PRICE_DRIFT_LOOKBACK_MIN < 45
    _backfill_calls["n"] = 0
    ex2 = _make_executor(entry_dt=now - datetime.timedelta(minutes=45), current_price=10.02, entry_price=10.00)
    ex2.check_price_drift_stop()
    assert _backfill_calls["n"] == 1, "an old-enough position must still use the backfilled reference when in-memory history is empty"

finally:
    enhanced.EnhancedExecutor._backfill_drift_reference = orig_backfill
    enhanced.PRICE_DRIFT_STOP_ENABLED = orig_enabled

print("OK: price-drift-stop reference leg is skipped for a position younger than PRICE_DRIFT_LOOKBACK_MIN, still used once it's old enough")

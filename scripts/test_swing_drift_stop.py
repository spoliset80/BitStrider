"""Self-check for the swing/multi-day drift stop (2026-08-15, at the
user's request: idea #3 of six suggested improvements).

Built after TrendBreaker's multi-day losers surfaced (NWL -5.41% held 55h,
IMMR -2.71% held 24h, IMAX -1.85% held 21h) with nothing watching them
between entry and the wide trailing stop for days at a time --
check_price_drift_stop() only covers same-day entries. Only NWL would
actually have tripped a 3% cap; IMMR/IMAX stay under it, confirming this
targets tail losses, not every swing drawdown.

Run with:
  python scripts/test_swing_drift_stop.py
No network calls -- exercises the pure decision function directly.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.execution.enhanced import EnhancedExecutor
from engine.config import SWING_DRIFT_STOP_ENABLED, SWING_DRIFT_STOP_PCT, SWING_DRIFT_STOP_CHECK_INTERVAL_MIN

assert SWING_DRIFT_STOP_ENABLED is True
assert SWING_DRIFT_STOP_PCT == 3.0
assert SWING_DRIFT_STOP_CHECK_INTERVAL_MIN == 30
assert 60 % SWING_DRIFT_STOP_CHECK_INTERVAL_MIN == 0, "must land cleanly on the fixed clock grid"

f = EnhancedExecutor._swing_drift_stop_reason

# --- The three real TrendBreaker multi-day trades that motivated this: ---

# NWL: entry ~unknown exact $, but -5.41% adverse -- SHOULD trigger.
# (using round entry=$10.00 for a clean percentage match)
assert f(current=9.459, entry=10.00, is_long=True, stop_pct=3.0) is not None, \
    "NWL-shaped -5.41% move must trigger the 3% swing cap"

# IMMR: -2.71% adverse -- should NOT trigger, stays under the 3% threshold.
assert f(current=9.729, entry=10.00, is_long=True, stop_pct=3.0) is None, \
    "IMMR-shaped -2.71% move must stay under the cap -- targets tail losses only"

# IMAX: -1.85% adverse -- should NOT trigger either.
assert f(current=9.815, entry=10.00, is_long=True, stop_pct=3.0) is None, \
    "IMAX-shaped -1.85% move must stay under the cap"

# --- Mirrors correctly for shorts (adverse move is a RISE, not a drop) ---
assert f(current=10.541, entry=10.00, is_long=False, stop_pct=3.0) is not None
assert f(current=10.271, entry=10.00, is_long=False, stop_pct=3.0) is None

# --- No entry-price leg vs a "30-min-ago" reference -- swing holds don't
#     have a meaningful intraday reference point, entry alone is the anchor.
assert f(current=5.00, entry=None, is_long=True, stop_pct=3.0) is None
assert f(current=5.00, entry=0.0, is_long=True, stop_pct=3.0) is None

# --- Comfortably under threshold -> no trigger. ---
assert f(current=9.95, entry=10.00, is_long=True, stop_pct=3.0) is None

print("OK: swing drift stop would have caught NWL's -5.41% multi-day loss, left IMMR (-2.71%) and "
      "IMAX (-1.85%) alone, mirrors correctly for shorts, and is scheduled on a clean 30-min clock-grid interval")

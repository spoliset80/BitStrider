"""Self-check for the price drift stop (2026-08-13, refined 2026-08-14).
Same-day positions that drop (longs) or rise (shorts) more than
PRICE_DRIFT_STOP_PCT versus EITHER their own entry price OR their price
PRICE_DRIFT_LOOKBACK_MIN (30 min) ago get exited. Built after confirming
live that this morning's losers (DFSC, HLIT, EROC, JACK) were all bought
right at the open and all faded 4-8% before the normal, wider trailing stop
caught them.

2026-08-14 correction: the entry-price leg was dropped for one day (checked
only the 30-min-ago reference) -- confirmed live that TE dropped 2.69% off
its OWN entry price without ever triggering, because a slow bleed that
never shows a full move within any single 10-min-to-10-min window is
exactly what a 30-min-ago-only comparison misses. Restored the entry-price
leg (OR'd with the 30-min-ago one, same as the very first version). The
threshold itself stays at the original 1.0% per explicit follow-up
correction the same day.

Run with:
  python scripts/test_price_drift_stop.py
No network calls -- exercises the pure decision function _drift_stop_reason
directly.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.execution.enhanced import EnhancedExecutor
from engine.config import (
    PRICE_DRIFT_STOP_PCT, PRICE_DRIFT_CHECK_INTERVAL_MIN, PRICE_DRIFT_LOOKBACK_MIN,
)

f = EnhancedExecutor._drift_stop_reason

assert PRICE_DRIFT_STOP_PCT == 1.0
assert PRICE_DRIFT_CHECK_INTERVAL_MIN == 10
assert PRICE_DRIFT_LOOKBACK_MIN == 30
assert PRICE_DRIFT_LOOKBACK_MIN % PRICE_DRIFT_CHECK_INTERVAL_MIN == 0, "lookback should be a clean multiple of the poll interval"
assert PRICE_DRIFT_LOOKBACK_MIN // PRICE_DRIFT_CHECK_INTERVAL_MIN == 3, "3 ten-minute ticks = 30 minutes of lookback"

# --- Longs: adverse move is a DROP, checked against EITHER reference ---

# Flat/up on both references -> no trigger.
assert f(current=10.10, entry=10.00, reference=10.05, is_long=True, stop_pct=1.0) is None

# Down > 1.0% from entry, even with no 30-min-ago reference yet -> triggers
# (this is exactly the TE case: a slow bleed the 30-min-ago leg alone missed).
assert f(current=9.80, entry=10.00, reference=None, is_long=True, stop_pct=1.0) is not None

# Flat vs entry but down > 1.0% from the 30-min-ago reference -> triggers too.
reason = f(current=9.79, entry=9.80, reference=10.00, is_long=True, stop_pct=1.0)
assert reason is not None and "min" in reason, f"expected a 30min-drift reason, got {reason!r}"

# Comfortably under the threshold on both -> no trigger.
assert f(current=9.95, entry=10.00, reference=10.00, is_long=True, stop_pct=1.0) is None

# Neither reference available (fresh position, no history, entry unknown) -> no trigger, no crash.
assert f(current=5.00, entry=None, reference=None, is_long=True, stop_pct=1.0) is None
assert f(current=5.00, entry=0.0, reference=0.0, is_long=True, stop_pct=1.0) is None

# --- Shorts: adverse move is a RISE (mirrored), same OR-of-two-legs logic ---

assert f(current=9.90, entry=10.00, reference=9.95, is_long=False, stop_pct=1.0) is None
assert f(current=10.20, entry=10.00, reference=None, is_long=False, stop_pct=1.0) is not None
reason = f(current=10.19, entry=10.18, reference=10.00, is_long=False, stop_pct=1.0)
assert reason is not None and "min" in reason, f"expected a 30min-drift reason, got {reason!r}"

# --- Rolling-history mechanics: deque[0] becomes the reference only once full ---

from collections import deque
lookback_ticks = PRICE_DRIFT_LOOKBACK_MIN // PRICE_DRIFT_CHECK_INTERVAL_MIN
history = deque(maxlen=lookback_ticks)
prices = [10.00, 10.02, 10.01, 9.85]  # 4 ticks; window holds only the last 3
for i, p in enumerate(prices):
    ref = history[0] if len(history) == lookback_ticks else None
    if i < lookback_ticks:
        assert ref is None, f"tick {i}: not enough history yet, must not reference"
    else:
        assert ref == prices[i - lookback_ticks], f"tick {i}: reference should be exactly {lookback_ticks} ticks back"
    history.append(p)

print("OK: price drift stop triggers vs. EITHER entry or the 30-min-ago reference, mirrors correctly for shorts, rolling history is exactly 3 ticks deep")

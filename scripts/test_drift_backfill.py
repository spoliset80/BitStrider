"""Self-check for the price-drift-stop restart backfill (2026-08-14, at the
user's request: "why TE didn't exit... 30mins before to see the drop").

Confirmed live: TE entered 09:33, the bot restarted twice (10:07, 10:10)
before 30 clean minutes had elapsed -- _price_drift_history is in-memory
only, so each restart wiped it, and the rolling 3-tick history never had a
chance to rebuild. TE sat with zero drift protection past an hour while
down -2.8%. _backfill_drift_reference reconstructs an approximate
30-min-ago reference from real 1-min bar data instead of waiting.

Run with:
  python scripts/test_drift_backfill.py
No network calls -- engine.execution.enhanced.get_bars is monkeypatched to
return a synthetic DataFrame.
"""
import sys
from pathlib import Path
import pandas as pd
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.execution.enhanced as enhanced
from engine.config import PRICE_DRIFT_LOOKBACK_MIN

ex = enhanced.EnhancedExecutor.__new__(enhanced.EnhancedExecutor)  # skip __init__, no broker creds needed

_orig_get_bars = enhanced.get_bars

# 45 one-minute bars, close price = row index (0, 1, 2, ... 44) for an easy
# assertion: "PRICE_DRIFT_LOOKBACK_MIN minutes ago" should be the bar at
# position -1-PRICE_DRIFT_LOOKBACK_MIN, i.e. len-1-PRICE_DRIFT_LOOKBACK_MIN.
closes = list(range(45))
fake_bars = pd.DataFrame({"close": closes})

try:
    enhanced.get_bars = lambda *a, **k: fake_bars
    ref = ex._backfill_drift_reference("TEST")
    expected = closes[-1 - PRICE_DRIFT_LOOKBACK_MIN]
    assert ref == float(expected), f"expected the bar {PRICE_DRIFT_LOOKBACK_MIN} minutes back ({expected}), got {ref}"

    # Not enough bars yet (fewer rows than the lookback window) -> None, not
    # a wrong/short reference.
    enhanced.get_bars = lambda *a, **k: pd.DataFrame({"close": list(range(PRICE_DRIFT_LOOKBACK_MIN))})
    assert ex._backfill_drift_reference("TEST") is None, "too few bars must not produce a reference"

    # Empty / missing bars -> None, never raises.
    enhanced.get_bars = lambda *a, **k: pd.DataFrame()
    assert ex._backfill_drift_reference("TEST") is None

    # get_bars raising -> caught, returns None (fail-safe, doesn't crash the sweep).
    def _raise(*a, **k):
        raise RuntimeError("network down")
    enhanced.get_bars = _raise
    assert ex._backfill_drift_reference("TEST") is None
finally:
    enhanced.get_bars = _orig_get_bars

print("OK: drift-stop restart backfill reconstructs the right lookback bar, fails safe on missing/short/errored data")

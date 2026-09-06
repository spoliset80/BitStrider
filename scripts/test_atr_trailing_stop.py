"""Self-check for the ATR-based trailing stop (2026-09-01, user request:
"change the trail stop exit to atr based values").

The trailing-stop floor (TRAIL_STOP_PCT) is now widened per-symbol by ATR
when ATR is meaningfully wider than the floor: ATR(ATR_TRAIL_PERIOD) off the
1-min bars, scaled by ATR_TRAIL_MULTIPLIER, floored at TRAIL_STOP_PCT and
capped at ATR_TRAIL_MAX_PCT. The pure helper _trail_pct_for() takes the ATR
distance as a parameter; the network-touching wrapper _atr_trail_pct_for()
fetches it and must fail open to the flat floor on any fetch/parse failure or
insufficient bars. Profit giveback (PROFIT_TRAIL_GIVEBACK_PCT of gain) still
widens past the ATR value on a winner.

Run with:
  python scripts/test_atr_trailing_stop.py
No network calls -- get_bars / calculate_atr are monkeypatched.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as _pd

import engine.execution.enhanced as ee
from engine.execution.enhanced import _trail_pct_for, _atr_trail_pct_for
from engine.config import (
    TRAIL_STOP_PCT, PROFIT_TRAIL_GIVEBACK_PCT,
    ATR_TRAIL_ENABLED, ATR_TRAIL_PERIOD, ATR_TRAIL_MULTIPLIER, ATR_TRAIL_MAX_PCT,
)

assert ATR_TRAIL_ENABLED is True, "ATR-based trailing stop must be enabled by default"
assert ATR_TRAIL_PERIOD == 14, f"expected ATR period 14, got {ATR_TRAIL_PERIOD}"
assert ATR_TRAIL_MULTIPLIER == 1.5, f"expected 1.5 multiplier, got {ATR_TRAIL_MULTIPLIER}"
assert ATR_TRAIL_MAX_PCT == 4.0, f"expected 4.0 cap, got {ATR_TRAIL_MAX_PCT}"

_EMPTY = _pd.DataFrame()
_RICH = _pd.DataFrame({
    "open": [10.0] * 40, "high": [10.3] * 40, "low": [9.7] * 40, "close": [10.0] * 40,
})


def _make_bars_raiser(exc):
    def _raises(*a, **k):
        raise exc
    return _raises


def _patched_call(get_bars_impl, atr_impl, *args, **kwargs):
    _orig_bars, _orig_atr = ee.get_bars, ee.calculate_atr
    ee.get_bars, ee.calculate_atr = get_bars_impl, atr_impl
    try:
        return _atr_trail_pct_for(*args, **kwargs)
    finally:
        ee.get_bars, ee.calculate_atr = _orig_bars, _orig_atr


# 1. Pure helper: ATR widening, floor, cap, and profit-giveback precedence.
assert _trail_pct_for("X", 10.0, {}) == (TRAIL_STOP_PCT, "FLAT"), "no atr -> floor"
assert _trail_pct_for("X", 10.0, {}, atr=0.0) == (TRAIL_STOP_PCT, "FLAT"), "zero ATR -> floor"
assert _trail_pct_for("X", 10.0, {}, atr=0.05) == (TRAIL_STOP_PCT, "FLAT"), "ATR below floor -> floor"
atr_widen = round(0.20 / 10.0 * 100.0 * ATR_TRAIL_MULTIPLIER, 2)  # $0.20 ATR on $10 -> 3.0%
assert _trail_pct_for("X", 10.0, {}, atr=0.20) == (atr_widen, "ATR"), "ATR wider than floor -> ATR label"
assert _trail_pct_for("X", 10.0, {}, atr=1.00) == (ATR_TRAIL_MAX_PCT, "ATR"), "huge ATR capped at max"
r = _trail_pct_for("X", 10.0, {}, gain_pct=20.0, atr=0.20)  # ATR 3.0% vs profit 4.0%
assert r == (round(20.0 * PROFIT_TRAIL_GIVEBACK_PCT / 100.0, 2), "PROFIT"), r

# 2. Wrapper: bars fetch raising -> flat floor (fail open).
_orig_bars2, _orig_atr2 = ee.get_bars, ee.calculate_atr
ee.get_bars = _make_bars_raiser(RuntimeError("network down"))
ee.calculate_atr = lambda bars, period: (_ for _ in ()).throw(RuntimeError("unreachable"))
try:
    pct, label = _atr_trail_pct_for("X", 10.0, {})
    assert (pct, label) == (TRAIL_STOP_PCT, "FLAT"), "fetch failure must fall back to the floor"
finally:
    ee.get_bars, ee.calculate_atr = _orig_bars2, _orig_atr2

# 3. Wrapper: empty bars (calculate_atr -> 0) -> flat floor.
pct, label = _patched_call(lambda *a, **k: _EMPTY, lambda bars, period=14: 0.0, "X", 10.0, {})
assert (pct, label) == (TRAIL_STOP_PCT, "FLAT"), "empty bars -> floor"

# 4. Wrapper: ATR below the floor -> floor.
pct, label = _patched_call(lambda *a, **k: _RICH, lambda bars, period=14: 0.05, "X", 10.0, {})
assert (pct, label) == (TRAIL_STOP_PCT, "FLAT"), "sub-floor ATR -> floor"

# 5. Wrapper: ATR above the floor -> ATR value, capped at max.
pct, label = _patched_call(lambda *a, **k: _RICH, lambda bars, period=14: 0.20, "X", 10.0, {})
assert (pct, label) == (atr_widen, "ATR"), f"ATR widening through wrapper, got {(pct, label)}"
pct, label = _patched_call(lambda *a, **k: _RICH, lambda bars, period=14: 1.00, "X", 10.0, {})
assert (pct, label) == (ATR_TRAIL_MAX_PCT, "ATR"), "cap through wrapper"

# 6. Disabled toggle -> floor regardless of ATR.
_orig_enabled = ee.ATR_TRAIL_ENABLED
ee.ATR_TRAIL_ENABLED = False
pct, label = _patched_call(lambda *a, **k: _RICH, lambda bars, period=14: 1.00, "X", 10.0, {})
assert (pct, label) == (TRAIL_STOP_PCT, "FLAT"), "disabled -> floor"
ee.ATR_TRAIL_ENABLED = _orig_enabled

# 7. Wrapper still passes gain_pct through to profit giveback.
pct, label = _patched_call(lambda *a, **k: _RICH, lambda bars, period=14: 0.20, "X", 10.0, {}, 20.0)  # ATR 3.0% vs profit 4.0%
assert (pct, label) == (round(20.0 * PROFIT_TRAIL_GIVEBACK_PCT / 100.0, 2), "PROFIT"), (pct, label)

print("OK: ATR-based trailing stop -- floor preserved, ATR widening + cap, profit giveback precedence, fail-open on errors")

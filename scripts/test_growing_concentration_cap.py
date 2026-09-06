"""Self-check for the growing per-position concentration cap (2026-08-17,
at the user's request: "maximum holding as 20% of the portfolio value and
growing based on the continued positive returns"; base/ceiling scaled
again same day when the whole sizing pipeline moved from a 7.5% to a 10%
base -- "change this to 10% base instead of 7.5% and scale everything up").

A losing/flat position keeps the plain MAX_POSITION_CONCENTRATION_PCT
base cap. A winning position's cap grows with its unrealized gain --
POSITION_CAP_GROWTH_FACTOR points of extra room per point of gain -- up
to POSITION_CAP_ABSOLUTE_MAX_PCT, and never drops below the base
regardless of how large a loss is.

Run with:
  python scripts/test_growing_concentration_cap.py
No network calls -- exercises the pure cap function directly.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.execution.enhanced import EnhancedExecutor
from engine.config import (
    MAX_POSITION_CONCENTRATION_PCT, POSITION_CAP_GROWTH_FACTOR, POSITION_CAP_ABSOLUTE_MAX_PCT,
)

assert MAX_POSITION_CONCENTRATION_PCT == 26.7
assert POSITION_CAP_GROWTH_FACTOR == 0.25
assert POSITION_CAP_ABSOLUTE_MAX_PCT == 46.7

f = EnhancedExecutor._effective_concentration_cap_pct
base = MAX_POSITION_CONCENTRATION_PCT

# --- Losing/flat positions: plain base cap, unaffected by loss size ---
assert f(0.0) == base, "flat (0% gain) -> plain base cap"
assert f(-5.0) == base, "a loser must not get LESS room than the base cap"
assert f(-50.0) == base, "even a big loss stays at the base cap, never drops below it"

# --- Winning positions: cap grows linearly with gain, base + gain% x factor ---
assert round(f(20.0), 2) == round(base + 20.0 * POSITION_CAP_GROWTH_FACTOR, 2), "up 20% x factor -> base + 5"
assert round(f(40.0), 2) == round(base + 40.0 * POSITION_CAP_GROWTH_FACTOR, 2), "up 40% x factor -> base + 10"
assert round(f(4.0), 2) == round(base + 4.0 * POSITION_CAP_GROWTH_FACTOR, 2), "up 4% x factor -> base + 1"

# --- Absolute ceiling: never exceeds POSITION_CAP_ABSOLUTE_MAX_PCT no matter the gain ---
gain_at_ceiling = (POSITION_CAP_ABSOLUTE_MAX_PCT - base) / POSITION_CAP_GROWTH_FACTOR
assert round(f(gain_at_ceiling), 2) == POSITION_CAP_ABSOLUTE_MAX_PCT, "right at the ceiling"
assert f(gain_at_ceiling + 500.0) == POSITION_CAP_ABSOLUTE_MAX_PCT, "a huge gain must still clamp, not run away"

# --- Monotonic: cap never decreases as gain increases ---
prev = f(0.0)
for gain in range(0, 400, 5):
    cur = f(float(gain))
    assert cur >= prev, f"cap dropped from {prev} to {cur} as gain rose to {gain}%"
    assert cur <= POSITION_CAP_ABSOLUTE_MAX_PCT, f"cap {cur} exceeded the {POSITION_CAP_ABSOLUTE_MAX_PCT}% ceiling"
    prev = cur

print(f"OK: growing concentration cap stays at the {base}% base for losing/flat positions, "
      f"grows linearly with unrealized gain for winners, and clamps at the {POSITION_CAP_ABSOLUTE_MAX_PCT}% absolute ceiling")

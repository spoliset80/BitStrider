"""Self-check for the MFE give-back stop (2026-09-03).
Once a same-day position's unrealized gain has EVER reached
MFE_ARM_PROFIT_PCT, check_mfe_giveback_exit() closes it the moment the
current gain falls below max(peak_gain * MFE_GIVEBACK_FRACTION,
MFE_BREAKEVEN_FLOOR_PCT) -- a breakeven-plus ratchet on trades that already
showed profit.

Built from the 09:30-11:00 ET post-mortem the same day: 41 round trips
peaked at +$90.56 unrealized combined but realized only +$1.22 (1.3% MFE
capture); 34/41 went green and kept just 31.4% of what they showed. The
broker-side GTC trailing stop (1.5-4.0% wide) can't catch a green-then-fade
round trip that lives 2-11 minutes, and no software check tracked a trade's
best level. The test cases below are the REAL 9/3 morning trades that
motivated each rule.

Run with:
  python scripts/test_mfe_giveback.py
No network calls -- exercises the pure decision function
_mfe_giveback_reason directly.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.execution.enhanced import EnhancedExecutor
from engine.config import (
    MFE_GIVEBACK_ENABLED, MFE_ARM_PROFIT_PCT, MFE_GIVEBACK_FRACTION,
    MFE_BREAKEVEN_FLOOR_PCT,
)

f = EnhancedExecutor._mfe_giveback_reason

assert MFE_GIVEBACK_ENABLED is True
assert MFE_ARM_PROFIT_PCT == 0.5
assert MFE_GIVEBACK_FRACTION == 0.6
assert MFE_BREAKEVEN_FLOOR_PCT == 0.1

# --- Real 9/3 morning cases (longs) ---

# CONL 09:38 entry 6.12, peaked 6.24 (+1.96%), exited 6.14 (+0.33%):
# gave back 84% of its peak -> must fire. Floor = max(1.18%, 0.1%) = 1.18%.
reason = f(peak=6.24, entry=6.12, current=6.14, is_long=True)
assert reason is not None and "peaked at +1.96%" in reason, f"CONL case must fire, got {reason!r}"

# SMMT 09:31 entry 15.66, peaked 16.32 (+4.21%), exited 16.00 (+2.17%):
# gave back 49% -> fires at the 60%-of-peak floor (2.53%).
assert f(peak=16.32, entry=15.66, current=16.00, is_long=True) is not None

# HOOD 09:34 entry 117.24, peaked 122.19 (+4.22%), exited 119.18 (+1.66%):
# gave back 61% -> fires.
assert f(peak=122.19, entry=117.24, current=119.18, is_long=True) is not None

# PLTR 09:32 entry 174.35, peaked 185.74 (+6.53%), exited 184.57 (+5.86%):
# kept 90% of peak -- the runner we must NOT cut. Floor 3.92% << 5.86%.
assert f(peak=185.74, entry=174.35, current=184.57, is_long=True) is None, \
    "a trade holding >60% of its peak gain must never be exited"

# ASST 09:34 entry 26.20, peaked 26.34 (+0.53%, just armed), exited 25.95
# (-0.95%): a fresh arm still ratchets to breakeven-plus instead of
# round-tripping red. Floor = max(0.32%, 0.1%) = 0.32%.
assert f(peak=26.34, entry=26.20, current=25.95, is_long=True) is not None

# Breakeven-plus ratchet: armed at +0.6% then bleeding back through entry
# must fire on BOTH sides of entry (still-green +0.05% and red -0.10%).
assert f(peak=100.60, entry=100.00, current=100.05, is_long=True) is not None
assert f(peak=100.60, entry=100.00, current=99.90, is_long=True) is not None

# --- Arming gate ---

# Peak never reached the arm threshold -> never fires, no matter how bad
# the current price is (ordinary trailing stop owns un-armed trades).
assert f(peak=26.30, entry=26.20, current=25.00, is_long=True) is None, \
    "a trade that never armed must not be exited by the MFE rule"

# --- Shorts: mirrored (peak is the LOWEST price seen; gain = entry-current) ---

# Short entry 100.00, best (lowest) 99.00 (+1.0% armed), now 99.50 (+0.5%):
# floor = max(0.6%, 0.1%) = 0.6% -> 0.5% < 0.6% fires.
assert f(peak=99.00, entry=100.00, current=99.50, is_long=False) is not None

# Same short holding 0.65% of its 1.0% peak -> keep riding.
assert f(peak=99.00, entry=100.00, current=99.35, is_long=False) is None

# --- Fail-safe semantics (missing/bad inputs never trigger) ---

assert f(peak=None, entry=100.0, current=90.0, is_long=True) is None
assert f(peak=101.0, entry=None, current=90.0, is_long=True) is None
assert f(peak=101.0, entry=0.0, current=90.0, is_long=True) is None
assert f(peak=0.0, entry=100.0, current=90.0, is_long=True) is None

# --- Poller-side peak tracking mechanics (same shape as _update_ema9_peak) ---

peaks = {}
seq = [10.00, 10.40, 10.20, 10.60, 10.30]  # long: peak ratchets up, never down
for px in seq:
    prev = peaks.get("X")
    peaks["X"] = px if prev is None else max(prev, px)
assert peaks["X"] == 10.60, "long peak must ratchet to the highest sample"
short_peaks = {}
for px in [10.00, 9.70, 9.85, 9.60]:
    prev = short_peaks.get("Y")
    short_peaks["Y"] = px if prev is None else min(prev, px)
assert short_peaks["Y"] == 9.60, "short peak must ratchet to the lowest sample"

print("OK: MFE give-back stop fires on the real 9/3 green-then-fade cases (CONL/SMMT/HOOD/ASST), "
      "spares the >60%-held runner (PLTR), ratchets armed trades to breakeven-plus, "
      "mirrors for shorts, gates on the arm threshold, and fails safe on missing data")

"""Self-check for entry re-chasing (2026-08-14, at the user's request).

Confirmed live: MF's entry order rested unfilled all cycle (bid $12.95 /
ask $17.20, order resting at $15.21) because entries only ever submitted
once at a bounded limit and never retried, unlike every exit path
(_sweep_force_closes, check_afterhours_stops, close_no_gain_positions),
which all re-chase a stale order with escalating slip. _sweep_pending_entries
gives entries the same treatment.

Run with:
  python scripts/test_entry_rechase.py
No network calls -- exercises the pure slip-escalation function directly.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.execution.enhanced import _entry_rechase_slip_pct
from engine.config import MARKETABLE_LIMIT_BUFFER_PCT

assert MARKETABLE_LIMIT_BUFFER_PCT == 1.0

# Starts beyond the original 1% bound (chase_count=0 -> 2%), widens each
# retry, capped at 3% -- same shape as every other re-chase path.
assert _entry_rechase_slip_pct(0) == 2.0
assert _entry_rechase_slip_pct(1) == 3.0
assert _entry_rechase_slip_pct(2) == 3.0, "must cap at 3%, not keep growing"
assert _entry_rechase_slip_pct(10) == 3.0

# Monotonic (never narrows on a later attempt).
prev = 0.0
for n in range(6):
    cur = _entry_rechase_slip_pct(n)
    assert cur >= prev, f"slip must never decrease: attempt {n} gave {cur} after {prev}"
    prev = cur

print("OK: entry re-chase slip escalates from 2% and caps at 3%, same shape as the exit-side chase paths")

"""Self-check for _schedule_on_clock_grid (2026-08-14, found while
investigating why FFAI's drift-stop check missed a brief dip-and-recover).

schedule.every(N).minutes counts N minutes from whenever it's registered --
i.e. from process start. Confirmed live: on a day with many restarts, the
price-drift-stop checks landed 13-19 minutes apart instead of a clean 10,
because every restart re-registered its own independent countdown instead
of landing on a fixed wall-clock grid. _schedule_on_clock_grid fixes that:
every restart re-aligns to the same :00/:10/:20... marks instead.

2026-09-02, morning-readiness fix: registration now ALSO fires the job once
immediately (pre-grid warm-up) so a late-morning boot never leaves the
drift/concentration checks blind across the 09:30 open. This test pins that
contract too: exactly ONE immediate fire per registration, then the grid.

Run with:
  python scripts/test_clock_grid_schedule.py
No network calls -- inspects schedule.jobs() after registering.
"""
import sys
import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import schedule
from engine.orchestrator import _schedule_on_clock_grid

def _noop():
    pass

# --- interval=10 registers exactly 6 jobs, at :00/:10/.../:50 ---
calls = []
def _counting():
    calls.append(1)

schedule.clear()
_schedule_on_clock_grid(10, _counting)
at_times = sorted(j.at_time for j in schedule.jobs)
expected = [datetime.time(0, m, 0) for m in range(0, 60, 10)]
assert at_times == expected, f"expected {expected}, got {at_times}"
assert len(schedule.jobs) == 6
# 2026-09-02: the registration itself fires the job ONCE (pre-grid warm-up).
assert calls == [1], (
    f"registration must fire the job exactly once immediately (pre-grid warm-up), got {len(calls)} fires"
)

# --- args are passed through to the job (immediate fire + grid fire) ---
schedule.clear()
calls2 = []
_schedule_on_clock_grid(10, lambda x: calls2.append(x), "ctx-marker")
assert calls2 == ["ctx-marker"], (
    f"the registration-time first tick must forward args, got {calls2}"
)
schedule.jobs[0].run()
assert calls2 == ["ctx-marker", "ctx-marker"], (
    f"grid job must also forward args when it fires on the clock, got {calls2}"
)

# --- restart re-registration lands on the SAME grid, not a shifted one ---
schedule.clear()
_schedule_on_clock_grid(10, _noop)
first_run_grid = sorted(j.at_time for j in schedule.jobs)
schedule.clear()
_schedule_on_clock_grid(10, _noop)  # simulates a restart re-registering
second_run_grid = sorted(j.at_time for j in schedule.jobs)
assert first_run_grid == second_run_grid, "a restart must not shift the grid"

# --- an interval that doesn't evenly divide 60 must fail loudly, not silently misalign ---
schedule.clear()
raised = False
try:
    _schedule_on_clock_grid(7, _noop)
except AssertionError as e:
    raised = True
    assert "evenly divide" in str(e)
assert raised, "7 does not divide 60 -- must raise, not silently register a bad grid"

schedule.clear()
print("OK: clock-grid scheduling lands on fixed :00/:10/... marks, survives re-registration "
      "(restarts) unchanged, forwards args, fires ONCE immediately at registration (2026-09-02 "
      "pre-grid warm-up so late boots aren't blind across the open), and rejects intervals that don't divide 60")

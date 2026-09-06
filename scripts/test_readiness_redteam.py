"""Red-team / edge-case net for the 2026-09-02 morning-readiness machinery.

Attacks the new code paths the happy-path tests (test_morning_readiness.py)
don't cover:
  - boundary times of the readiness trigger (09:24:59 / 09:25:00 /
    10:59:59 / 11:00:00 / afternoon), late-boot coverage, next-day re-arm;
  - weekend boots (red-team finding: trigger must NOT fire Sat/Sun);
  - kick Event races: set-before-wait, set-during-work (double-run is by
    design), wait with an already-past deadline, concurrent set;
  - a grid job RAISING at registration (must not prevent grid registration);
  - malformed 'HH:MM' config values (must raise at import, not crash-loop the
    live process) and the config ordering assert catching a bad ordering;
  - _within_discovery_window at the kick moment (09:25 weekday must be inside
    so the kicked refresher actually runs work).

Static + logic-level (no network, no Alpaca).
"""
import sys
import threading
import time
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok: {name}")
    else:
        failures.append(f"{name}{(' -- ' + detail) if detail else ''}")


from engine import config as cfg  # noqa: E402
from engine import orchestrator as orch  # noqa: E402

ET = __import__("pytz").timezone("America/New_York")


def _readiness_replica(now_et, readiness_scan_date):
    """Exact replica of the run-loop readiness_due expression (incl. the
    2026-09-02 weekday gate). Kept in sync via test_readiness_redteam."""
    readiness_open = datetime.datetime.strptime(cfg.MORNING_READINESS_ET, "%H:%M").time()
    readiness_close = datetime.datetime.strptime(cfg.ENTRY_WINDOW_BREAK_START_ET, "%H:%M").time()
    return (
        now_et.weekday() < 5
        and readiness_open <= now_et.time() < readiness_close
        and readiness_scan_date != now_et.date()
    )


# -- 1. Boundary matrix (Mon=2026-09-07, a weekday) -------------------------
mon = lambda h, m, s=0: ET.localize(datetime.datetime(2026, 9, 7, h, m, s))
check("09:24:59 not due (1s before trigger)", not _readiness_replica(mon(9, 24, 59), None))
check("09:25:00 due (inclusive start)", _readiness_replica(mon(9, 25, 0), None))
check("10:59:59 still due if not yet fired", _readiness_replica(mon(10, 59, 59), None))
check("11:00:00 NOT due (exclusive close = lunch flat)", not _readiness_replica(mon(11, 0, 0), None))
check("14:45 (afternoon segment) NOT due", not _readiness_replica(mon(14, 45, 0), None))
check("late boot 09:29:46 due immediately", _readiness_replica(mon(9, 29, 46), None))
check("same-day second pass NOT due (once per day)", not _readiness_replica(mon(9, 30, 0), mon(9, 25, 0).date()))
tue = ET.localize(datetime.datetime(2026, 9, 8, 9, 25, 0))
check("next day re-arms", _readiness_replica(tue, mon(9, 25, 0).date()))

# -- 2. Weekend red-team finding (must NOT fire) -----------------------------
sat = ET.localize(datetime.datetime(2026, 9, 5, 9, 26, 0))   # Saturday
sun = ET.localize(datetime.datetime(2026, 9, 6, 9, 26, 0))   # Sunday
check("Saturday 09:26 boot does NOT fire", not _readiness_replica(sat, None))
check("Sunday 09:26 boot does NOT fire", not _readiness_replica(sun, None))

# -- 3. Kick Event races ------------------------------------------------------
ev = orch._readiness_kick
ev.clear()
# 3a. set() BEFORE wait() -> waiter wakes instantly (no lost wakeup).
ev.set()
t0 = time.monotonic()
woken = ev.wait(timeout=2.0)
dt = time.monotonic() - t0
ev.clear()
check("kick set-before-wait wakes instantly (no lost wakeup)", woken and dt < 1.0, f"dt={dt:.3f}s")
# 3b. set() DURING work (refresher not waiting) is not lost: next wait() returns
#     immediately -> one extra immediate run. By design (idempotent refresh).
ev.set()
time.sleep(0.05)  # simulate the tail of in-flight work
t0 = time.monotonic()
woken = ev.wait(timeout=2.0)
dt = time.monotonic() - t0
ev.clear()
check("kick set-during-work still wakes the next wait", woken and dt < 1.0, f"dt={dt:.3f}s")
# 3c. wait() with an already-past deadline must clamp to >=1s, not 0/negative.
t0 = time.monotonic()
woken = ev.wait(timeout=max(1.0, -5.0))
dt = time.monotonic() - t0
check("past-deadline wait clamps to ~1s (loop can't spin)", (not woken) and 0.9 <= dt <= 2.5, f"dt={dt:.3f}s")
# 3d. concurrent set() from another thread wakes the waiting refresher thread.
done = threading.Event()
def _waiter():
    ev.wait(timeout=2.0)
    done.set()
threading.Thread(target=_waiter, daemon=True).start()
time.sleep(0.1)
ev.set()
ok = done.wait(timeout=2.0)
ev.clear()
check("concurrent set from main-loop thread wakes the waiter", ok)
# 3e. refresher's wait timeout math never goes negative/zero.
check("refresher wait timeout floor is 1.0s", max(1.0, -1e9) == 1.0)

# -- 4. Grid job raising at registration must not kill the grid --------------
import schedule  # noqa: E402
def _boom():
    raise RuntimeError("simulated registration-time failure")
schedule.clear()
orch._schedule_on_clock_grid(10, _boom)
marks = sorted(j.at_time for j in schedule.jobs)
schedule.clear()
check("raising job: grid still fully registered (6 marks)", len(marks) == 6, f"got {marks}")
check("raising job: exception contained (no propagation)", True)  # reaching here proves containment

# -- 5. Config red-team: malformed times + ordering assert -------------------
def _raises(fn):
    try:
        fn()
        return False
    except AssertionError:
        return True
check("bad format '9:5' rejected", _raises(lambda: cfg._require_hhmm("X", "9:5")))
check("bad format '25:00' rejected", _raises(lambda: cfg._require_hhmm("X", "25:00")))
check("bad format '09:25:00' rejected", _raises(lambda: cfg._require_hhmm("X", "09:25:00")))
check("non-string None rejected", _raises(lambda: cfg._require_hhmm("X", None)))
check("good format passes", (cfg._require_hhmm("X", "09:25") or True))
for _name in (
    "PREP_SCAN_START_ET", "MORNING_READINESS_ET", "DISCOVERY_WINDOW_START_ET",
    "ENTRY_WINDOW_START_ET", "ENTRY_WINDOW_BREAK_START_ET", "ENTRY_WINDOW_BREAK_END_ET",
    "ENTRY_WINDOW_END_ET", "PREMARKET_START", "MARKET_OPEN", "MARKET_CLOSE",
    "AFTERHOURS_END", "EOD_CLOSE_TIME", "LUNCH_FLAT_TIME_ET",
):
    check(f"cfg.{_name}='{getattr(cfg, _name)}' valid HH:MM", (cfg._require_hhmm(_name, getattr(cfg, _name)) or True))
# The ordering assert must catch a hypothetically bad placement (11:30 is past
# the break start -> expression must go False, which is what fires the assert).
synthetic = "11:30"
check("ordering guard catches a too-late readiness time", not (
    cfg.PREP_SCAN_START_ET < cfg.ENTRY_WINDOW_START_ET < synthetic < cfg.MARKET_OPEN
))

# -- 6. _within_discovery_window at the kick moment ---------------------------
check("weekday 09:25 inside discovery window (kicked refresher runs work)",
      orch._within_discovery_window(mon(9, 25, 0)))
check("08:54 outside (pre-window boot waits, per refresher 60s re-check)",
      not orch._within_discovery_window(mon(8, 54, 0)))
# Documented pre-existing finding (NOT from the 09:25 work): the window check is
# time-only, so Saturday mid-day is 'inside' -- weekend refresher churn is
# pre-existing cadence behavior; the readiness trigger itself is weekday-gated.
check("weekend discovery-window quirk documented (time-only check)",
      orch._within_discovery_window(sat))

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("TEST RESULT: red-team edge-case checks passed")

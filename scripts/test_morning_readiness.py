"""Regression net for the 2026-09-02 morning-readiness trigger (09:25 ET).

2026-09-02 post-mortem: the bot restart-looped through the entire pre-open
(.env-change restarts 08:48-09:35 ET) and the ActiveListRefresher's fixed
10-min spacing pushed the last prewarm_entry_ema run to 09:28 -- EMA signals
were not ready by 09:29 and the first orders only fired at 09:35:43 ET.
Fixes verified here:
  1. cfg.MORNING_READINESS_ET = "09:25" ordered PREP(09:05) < ENTRY(09:14)
     < READINESS(09:25) < MARKET_OPEN(09:30), asserted at config import.
  2. Schedule-driven clock-grid jobs (price/swing drift, concentration) fire
     once immediately at registration, so a late boot never leaves them blind
     across the 09:30 open.
  3. The main loop's once-per-day 09:25 ET trigger forces a fresh scan AND
     sets _readiness_kick, and the ActiveListRefresher waits on that Event
     (instead of a bare sleep) so prewarm_entry_ema runs immediately.

Static + logic-level (no network, no Alpaca).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok: {name}")
    else:
        failures.append(f"{name}{(' -- ' + detail) if detail else ''}")


# 1. Config: readiness time + ordering (string "HH:MM" comparisons are the
#    same ones the entry-window asserts rely on).
from engine import config as cfg  # noqa: E402
check("MORNING_READINESS_ET == '09:25'", cfg.MORNING_READINESS_ET == "09:25", cfg.MORNING_READINESS_ET)
check(
    "PREP < ENTRY_START < READINESS < MARKET_OPEN",
    cfg.PREP_SCAN_START_ET < cfg.ENTRY_WINDOW_START_ET < cfg.MORNING_READINESS_ET < cfg.MARKET_OPEN,
    f"{cfg.PREP_SCAN_START_ET} < {cfg.ENTRY_WINDOW_START_ET} < {cfg.MORNING_READINESS_ET} < {cfg.MARKET_OPEN}",
)

# 2. Clock-grid jobs tick once at registration (pre-grid warm-up).
from engine import orchestrator as orch  # noqa: E402
_calls = []
orch._schedule_on_clock_grid(10, lambda: _calls.append(1))
check("clock-grid fires immediately at registration", len(_calls) == 1, f"calls={_calls}")

# 3. The readiness kick Event (the primitive the refresher loop waits on).
check("_readiness_kick is a module-level Event", hasattr(orch, "_readiness_kick"))
orch._readiness_kick.set()
_t0 = time.monotonic()
_woken = orch._readiness_kick.wait(timeout=2.0)
_dt = time.monotonic() - _t0
orch._readiness_kick.clear()
check("readiness kick wakes waiter instantly", _woken and _dt < 1.0, f"woken={_woken} dt={_dt:.3f}s")

# 4. Orchestrator wiring: main loop reads the config, tracks its once-per-day
#    state, sets the kick; refresher waits on the kick; grid warm-up logged.
_src = (Path(__file__).resolve().parents[1] / "engine" / "orchestrator.py").read_text(encoding="utf-8")
check("main loop reads MORNING_READINESS_ET", "cfg.MORNING_READINESS_ET" in _src)
check("main loop tracks readiness_scan_date", "readiness_scan_date" in _src)
check("readiness trigger sets the refresher kick", "_readiness_kick.set()" in _src)
check("refresher waits on the kick", "_readiness_kick.wait(" in _src)
check("clock-grid registration-time first fire wired", "first tick fired at registration" in _src)

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("TEST RESULT: morning-readiness checks passed")
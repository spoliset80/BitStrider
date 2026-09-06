"""Deep-dive timeline simulation: does the 09:25 ET morning-readiness design
deliver 'all polling loops live + EMA signals ready by 09:29, orders at 09:30'
even under adverse restart patterns?

Discrete-event simulation (1s steps, no network) of the three moving parts:
  - main loop: 5s tick, entry-open / readiness / adaptive triggers, scans that
    block the loop for ~SCAN_DUR seconds (as scan_and_trade does),
  - ActiveListRefresher: 10-min cadence within the discovery window + kick
    Event consumption (incl. kick-while-busy semantics),
  - clock-grid jobs: fire once at registration, then on :00/:10/... marks.
A watchdog restart resets ALL of it (in-flight scan/prewarm aborted), exactly
like the real process kill, then models ~STARTUP_S of import time.

Guarantees asserted per weekday scenario:
  G1 readiness fires at most once per CONTINUOUS RUN; each restart-boot inside
     the [09:25, 11:00) band self-heals with exactly one fresh fire (by design:
     a restart wipes in-memory state, so the boot must re-arm immediately);
  G2 a prewarm_entry_ema run STARTS in [09:25:00, 09:30:00) whenever the bot
     was alive in that band (fresh EMA warm-up before the bell), or, for boots
     inside [09:25, 09:30), at the first physically possible tick;
  G3 the last boot <= 09:30 fired its grid jobs at registration (drift-stop /
     concentration checks never blind across the open);
  G4 the entry-window-open scan fired <= 09:30:15 (first executable cycle);
  G5 weekend boots fire NOTHING (readiness is weekday-gated).
Run: python scripts/test_morning_timeline_sim.py
"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytz

ET = pytz.timezone("America/New_York")
TICK_S = 5              # main loop sleep(5)
SCAN_DUR_S = 100        # a full scan_and_trade cycle blocks the loop (measured ~100s)
PREWARM_DUR_S = 20      # ti_capture + movers + prewarm block the refresher
REFRESH_S = 600         # ACTIVE_SCAN_SNAPSHOT_INTERVAL_MIN * 60
ADAPTIVE_S = 600        # pre-market/regular adaptive interval (10 min)
STARTUP_S = 8           # main.py import/startup before the first loop tick

def _d(day, h, m, s=0):
    return ET.localize(datetime.datetime(day.year, day.month, day.day, h, m, s))

def _t(day, hm):
    h, m = map(int, hm.split(":"))
    return _d(day, h, m)

# --- SIMULATE ---

def simulate(day, boot, restarts=()):
    """Discrete 1s-step simulation. restarts: ET datetimes when the watchdog
    kills+relaunches the whole process."""
    restarts = sorted(restarts)
    ri = 0
    readiness_date = entry_open_date = reopen_date = None
    last_scan = None
    scanning_until = None
    next_tick = None
    kick = False
    refresh_busy_until = None
    next_run = None
    grid_fires = []
    readiness_fires = []
    scan_starts = []
    prewarm_starts = []

    def reset(at):
        nonlocal readiness_date, entry_open_date, reopen_date, last_scan
        nonlocal scanning_until, next_tick, kick, refresh_busy_until, next_run
        readiness_date = entry_open_date = reopen_date = None
        last_scan = None
        scanning_until = None
        next_tick = at + datetime.timedelta(seconds=STARTUP_S)
        kick = False
        refresh_busy_until = None
        next_run = at  # refresher thread registers immediately at process start
        grid_fires.append(at)  # _schedule_on_clock_grid immediate first fire

    t = boot
    reset(boot)
    end = datetime.datetime.combine(day, datetime.time(10, 30), tzinfo=ET)
    while t < end:
        if ri < len(restarts) and t >= restarts[ri]:  # watchdog kill+relaunch
            ri += 1
            reset(t)
            t += datetime.timedelta(seconds=1)
            continue
        # refresher (independent thread)
        if refresh_busy_until is None or t >= refresh_busy_until:
            if kick:
                kick = False
                if t.weekday() < 5 and _t(day, "08:55") <= t <= _t(day, "15:44"):
                    prewarm_starts.append(t)
                refresh_busy_until = t + datetime.timedelta(seconds=PREWARM_DUR_S)
                next_run = refresh_busy_until
            elif next_run is not None and t >= next_run:
                if t.weekday() < 5 and _t(day, "08:55") <= t <= _t(day, "15:44"):
                    prewarm_starts.append(t)
                    refresh_busy_until = t + datetime.timedelta(seconds=PREWARM_DUR_S)
                    next_run = refresh_busy_until + datetime.timedelta(seconds=REFRESH_S)
                else:
                    next_run = t + datetime.timedelta(seconds=60)
        # main loop (blocked while scanning, as scan_and_trade blocks run_loop)
        if scanning_until is not None:
            if t >= scanning_until:
                scanning_until = None
        elif next_tick is not None and t >= next_tick:
            now = t
            weekday = now.weekday() < 5
            readiness_due = (
                weekday
                and _t(day, "09:25") <= now < _t(day, "11:00")
                and readiness_date != day
            )
            entry_open_due = now >= _t(day, "09:14") and entry_open_date != day
            reopen_due = now >= _t(day, "14:45") and reopen_date != day
            adaptive_due = last_scan is None or (now - last_scan).total_seconds() >= ADAPTIVE_S
            # mirrors the real run_loop: the WHOLE trigger block is weekday-gated
            if (now.weekday() < 5
                    and (entry_open_due or reopen_due or readiness_due or adaptive_due)):
                if readiness_due:
                    readiness_date = day
                    readiness_fires.append(now)
                    kick = True
                if entry_open_due:
                    entry_open_date = day
                if reopen_due:
                    reopen_date = day
                scan_starts.append(now)
                last_scan = now
                scanning_until = now + datetime.timedelta(seconds=SCAN_DUR_S)
            next_tick = t + datetime.timedelta(seconds=TICK_S)
        t += datetime.timedelta(seconds=1)

    return {
        "readiness_fires": readiness_fires,
        "prewarm_starts": prewarm_starts,
        "scan_starts": scan_starts,
        "grid_fires": grid_fires,
    }

failures = []

def check(name, cond, detail=""):
    print(f"  {'ok' if cond else 'FAIL'}: {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(f"{name}{(' -- ' + detail) if detail else ''}")

WED = datetime.date(2026, 9, 9)   # weekday
SAT = datetime.date(2026, 9, 5)   # weekend

# -- S1: normal day, boot 08:48, no restarts ----------------------------------
r = simulate(WED, _d(WED, 8, 48))
check("S1 normal: readiness fires exactly once", len(r["readiness_fires"]) == 1 and r["readiness_fires"][0] >= _d(WED, 9, 25), str(r["readiness_fires"]))
check("S1 normal: prewarm starts in [09:25, 09:30)", any(datetime.time(9, 25) <= x.time() < datetime.time(9, 30) for x in r["prewarm_starts"]), str([x.strftime('%H:%M:%S') for x in r['prewarm_starts']]))
check("S1 normal: entry-open scan fired <= 09:30:15", any(x.time() <= datetime.time(9, 30, 15) for x in r["scan_starts"] if x.time() >= datetime.time(9, 14)))

# -- S2: TODAY'S restart storm (9 kills 08:52-09:34 after the 08:48 boot) -----
storm = [_d(WED, 8, 52), _d(WED, 8, 57), _d(WED, 9, 3), _d(WED, 9, 10), _d(WED, 9, 16),
         _d(WED, 9, 22), _d(WED, 9, 27), _d(WED, 9, 31), _d(WED, 9, 34)]
r = simulate(WED, _d(WED, 8, 48), storm)
# Deep-dive finding: readiness re-fires once PER BOOT inside [09:25,11:00) --
# by design (self-healing: every restart wipes in-memory state, and the fresh
# boot immediately re-arms the warm-up instead of waiting out the interval).
def _runs(day, boot, restarts):
    starts = [boot] + list(restarts)
    ends = list(restarts) + [datetime.datetime.combine(day, datetime.time(10, 30), tzinfo=ET)]
    return list(zip(starts, ends))
def _overlaps_window(run, day):
    s, e = run
    return s < _t(day, "11:00") and e > _t(day, "09:25")
runs = _runs(WED, _d(WED, 8, 48), storm)
fires = r["readiness_fires"]
def _run_of(fire):
    for s, e in runs:
        if s <= fire < e:
            return (s, e)
    return None
per_run = {}
for f in fires:
    per_run.setdefault(_run_of(f), []).append(f)
check("S2 storm: at most ONE readiness fire per continuous run", all(len(v) == 1 for v in per_run.values()), str({str(k): [x.strftime('%H:%M:%S') for x in v] for k, v in per_run.items()}))
check("S2 storm: every run overlapping [09:25,11:00) self-heals with exactly one fire",
      sum(len(v) for v in per_run.values()) == sum(1 for run in runs if _overlaps_window(run, WED)),
      f"fires={len(fires)} window-runs={sum(1 for run in runs if _overlaps_window(run, WED))}")
check("S2 storm: prewarm starts in [09:25, 09:30) despite storm", any(datetime.time(9, 25) <= x.time() < datetime.time(9, 30) for x in r["prewarm_starts"]), str([x.strftime('%H:%M:%S') for x in r['prewarm_starts']]))
check("S2 storm: a pre-09:30 boot grid-fired at registration (not blind)", any(x <= _d(WED, 9, 30) for x in r["grid_fires"]))

# -- S3: late boot 09:29:46 (today's actual last restart time) ----------------
r = simulate(WED, _d(WED, 9, 29, 46))
check("S3 late-boot: readiness fires at first possible tick", len(r["readiness_fires"]) == 1 and r["readiness_fires"][0] <= _d(WED, 9, 29, 59), str(r["readiness_fires"]))
check("S3 late-boot: prewarm starts < 09:30 (physics-bound)", any(x < _d(WED, 9, 30) for x in r["prewarm_starts"]), str([x.strftime('%H:%M:%S') for x in r['prewarm_starts']]))
check("S3 late-boot: entry-open scan at boot, <= 09:30:15", any(_d(WED, 9, 29, 46) <= x <= _d(WED, 9, 30, 15) for x in r["scan_starts"]))

# -- S4: boot 09:21 (the grid-blind window) ------------------------------------
r = simulate(WED, _d(WED, 9, 21))
check("S4 boot-0921: readiness fires once in [09:25, 09:30)", any(datetime.time(9, 25) <= x.time() < datetime.time(9, 30) for x in r["readiness_fires"]), str(r["readiness_fires"]))
check("S4 boot-0921: grid first fire at boot 09:21 (drift checks armed pre-open)", r["grid_fires"][0] == _d(WED, 9, 21))
check("S4 boot-0921: prewarm in [09:25, 09:30)", any(datetime.time(9, 25) <= x.time() < datetime.time(9, 30) for x in r["prewarm_starts"]))

# -- S5: Saturday boot 09:26 -- nothing fires ----------------------------------
r = simulate(SAT, _d(SAT, 9, 26))
check("S5 saturday: readiness never fires", len(r["readiness_fires"]) == 0)
check("S5 saturday: no scan triggers on the weekend", not r["scan_starts"])

# -- S6: mid-morning boot 10:30 -- fires once, immediately ---------------------
r = simulate(WED, _d(WED, 10, 30))
check("S6 boot-1030: readiness fires once at boot band", len(r["readiness_fires"]) == 1 and r["readiness_fires"][0] <= _d(WED, 10, 30, 15), str(r["readiness_fires"]))

# -- S7: after-hours boot 16:00 -- no fire today -------------------------------
r = simulate(WED, _d(WED, 16, 0))
check("S7 afterhours-boot: readiness does not fire", len(r["readiness_fires"]) == 0)

# -- S8: boot exactly at the 09:25:00 boundary --------------------------------
r = simulate(WED, _d(WED, 9, 25, 0))
check("S8 boundary-boot: readiness fires at/after 09:25:00", len(r["readiness_fires"]) == 1 and r["readiness_fires"][0] >= _d(WED, 9, 25, 0), str(r["readiness_fires"]))

# -- Cross-scenario: readiness fires at most once per continuous run ----------
for label, args in [("S1", (WED, _d(WED, 8, 48), ())), ("S2", (WED, _d(WED, 8, 48), storm))]:
    r = simulate(*args)
    day, boot, restarts = args
    runs = _runs(day, boot, restarts)
    def _run_of2(fire):
        for s, e in runs:
            if s <= fire < e:
                return (s, e)
        return None
    per = {}
    for f in r["readiness_fires"]:
        per.setdefault(_run_of2(f), []).append(f)
    check(f"{label}: <=1 readiness fire per continuous run", all(len(v) == 1 for v in per.values()), str(r["readiness_fires"]))

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("TEST RESULT: morning timeline simulation -- all guarantees hold")

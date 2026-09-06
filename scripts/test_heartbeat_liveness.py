"""Regression test for the 2026-09-02 watchdog stall-restart loop fix.

Root cause: heartbeat.txt was written only after each scan cycle, and the
off-hours adaptive interval (SCAN_INTERVAL_CALM_VOL = 20 min) exceeds the
watchdog's STALL_RESTART_SECONDS (900s = 15 min) -- so a healthy sleeping
bot was killed as "hung" every ~15 min, all night (observed live 19:23-
21:30 ET: 7+ consecutive stall restarts).

Fix: the main loop touches the heartbeat EVERY tick via _touch_heartbeat()
(rate-limited to one write per 60s; force=True after a completed cycle), so
the heartbeat measures MAIN-LOOP LIVENESS. A genuine hang still stops the
writes and trips the watchdog; a long adaptive sleep does not.

Run with:
  python scripts/test_heartbeat_liveness.py
No network calls -- writes go to a temp dir (REPO_ROOT monkeypatched).
"""
import datetime
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.orchestrator as orch
from engine import config as cfg
import engine.watchdog as wd

PASS = 0


def check(name, cond, detail=""):
    global PASS
    assert cond, f"{name}: FAILED {detail}"
    PASS += 1
    print(f"ok {name}")


with tempfile.TemporaryDirectory() as tmp:
    old_root = orch.REPO_ROOT
    orch.REPO_ROOT = Path(tmp)
    hb = Path(tmp) / "heartbeat.txt"
    try:
        # 1. force write -> parseable UTC ISO timestamp
        orch._last_hb_touch = 0.0
        orch._touch_heartbeat(force=True)
        check("force write creates heartbeat", hb.exists())
        ts = datetime.datetime.fromisoformat(hb.read_text(encoding="utf-8").strip())
        check("heartbeat content parses as ISO timestamp",
              ts.tzinfo is not None and abs((datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds()) < 60)

        # 2. rate limiter: non-forced call within 60s must NOT rewrite
        orch._last_hb_touch = time.monotonic()  # just "wrote"
        hb.write_text("untouched-marker", encoding="utf-8")
        orch._touch_heartbeat()  # within the 60s window -> no-op
        check("rate limiter: non-forced call within 60s is a no-op",
              hb.read_text(encoding="utf-8") == "untouched-marker")

        # 3. non-forced call AFTER the window does rewrite
        orch._last_hb_touch = time.monotonic() - 61.0
        orch._touch_heartbeat()
        check("non-forced call after 60s window rewrites",
              hb.read_text(encoding="utf-8") != "untouched-marker")

        # 4. force bypasses the limiter even inside the window
        orch._last_hb_touch = time.monotonic()
        hb.write_text("pre-force", encoding="utf-8")
        orch._touch_heartbeat(force=True)
        check("force=True bypasses the rate limiter",
              hb.read_text(encoding="utf-8") != "pre-force")
    finally:
        orch.REPO_ROOT = old_root

# 5. wiring: the main loop must call the liveness touch every iteration
src = (ROOT / "engine" / "orchestrator.py").read_text(encoding="utf-8")
check("main loop calls _touch_heartbeat() every tick",
      "schedule.run_pending()" in src and "_touch_heartbeat()" in src
      and src.index("_touch_heartbeat()") > src.index("schedule.run_pending()"))
check("post-scan write uses the forced variant",
      "_touch_heartbeat(force=True)" in src)
check("old inline heartbeat write exists only inside _touch_heartbeat",
      src.count('(REPO_ROOT / "heartbeat.txt").write_text(') == 1)

# 6. the invariant that makes the watchdog honest: stall threshold (900s)
#    must be far above the 60s touch cadence, and must be DOCUMENTED as
#    liveness-based now. If someone lowers the threshold below a few touch
#    intervals, or removes the touch, this fails loudly.
check("STALL_RESTART_SECONDS well above the 60s touch cadence",
      wd.STALL_RESTART_SECONDS >= 300, str(wd.STALL_RESTART_SECONDS))
# The old trap, documented: the longest adaptive sleep exceeded the threshold.
check("off-hours adaptive interval exceeds the old threshold "
      "(this is exactly why the heartbeat is liveness-based now)",
      cfg.SCAN_INTERVAL_CALM_VOL * 60 > wd.STALL_RESTART_SECONDS,
      f"calm={cfg.SCAN_INTERVAL_CALM_VOL}min stall={wd.STALL_RESTART_SECONDS}s")

print(f"\nTEST RESULT: {PASS} checks passed")

"""Regression test for the universe-health log rate limiter (2026-09-02).

Root cause: with TI_PRIMARY_TTL_MINUTES=125, the overnight TTL expiry of
data/ti_primary.json made the "[UNIVERSE HEALTH] ti_primary.json is empty!"
ERROR fire on every 5s scan cycle for hours (10k+ identical lines/day
observed in apextrader.log). The notice is now rate-limited to once per
5 minutes per deficiency episode, and re-arms as soon as the universe is
healthy again.

Run with:
  python scripts/test_universe_health_ratelimit.py
No network calls -- all data sources stubbed; asserts via a captured handler.
"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.equity.scan as scan
from engine import config as _cfg

PASS = 0


def check(name, cond, detail=""):
    global PASS
    assert cond, f"{name}: FAILED {detail}"
    PASS += 1
    print(f"ok {name}")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


saved = (scan._get_ti_primary, scan._get_alpaca_movers_queue,
         _cfg.get_dynamic_universe, scan._UNIVERSE_HEALTH_LAST_LOG)
cap = _Capture()
scan._log.addHandler(cap)
try:
    # --- deficient universe (empty ti_primary, static fallback present) ---
    scan._get_ti_primary = lambda: []
    scan._get_alpaca_movers_queue = lambda: []
    _cfg.get_dynamic_universe = lambda: (["AAA", "BBB"], ["CCC"], None)

    cap.records.clear()
    scan._UNIVERSE_HEALTH_LAST_LOG = 0.0
    scan.get_scan_targets()
    errs = [r for r in cap.records if "ti_primary.json is empty" in r]
    check("deficient universe logs the empty notice once", len(errs) == 1, str(cap.records))

    # immediate second call -> rate-limited, no new notice
    cap.records.clear()
    scan.get_scan_targets()
    errs = [r for r in cap.records if "ti_primary.json is empty" in r]
    check("second call within 5 min is rate-limited (no new notice)", len(errs) == 0, str(cap.records))

    # --- universe recovers -> limiter re-arms ---
    scan._get_ti_primary = lambda: ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    cap.records.clear()
    scan.get_scan_targets()
    check("healthy universe resets the rate limiter",
          scan._UNIVERSE_HEALTH_LAST_LOG == 0.0 and cap.records == [], str(cap.records))

    # --- deficient again -> logs immediately (limiter was re-armed) ---
    scan._get_ti_primary = lambda: []
    cap.records.clear()
    scan.get_scan_targets()
    errs = [r for r in cap.records if "ti_primary.json is empty" in r]
    check("re-armed limiter logs a fresh episode immediately", len(errs) == 1, str(cap.records))

    # --- 'too small' (1-4 tickers) uses the same limiter, warning level ---
    scan._get_ti_primary = lambda: ["AAA", "BBB"]
    cap.records.clear()
    scan.get_scan_targets()
    warns = [r for r in cap.records if "too small" in r and r.levelno == logging.WARNING]
    check("'too small' notice is a warning, still rate-limited", len(warns) == 0, str(cap.records))
finally:
    scan._get_ti_primary, scan._get_alpaca_movers_queue, _cfg.get_dynamic_universe, scan._UNIVERSE_HEALTH_LAST_LOG = saved
    scan._log.removeHandler(cap)

print(f"\nTEST RESULT: {PASS} checks passed")

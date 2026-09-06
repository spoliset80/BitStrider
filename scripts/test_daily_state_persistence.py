"""Self-check for daily-start-equity persistence across restarts (2026-08-14,
found while checking today's overall P&L).

Confirmed live: daily_start_equity/daily_reset were in-memory-only globals.
reset_daily()'s only guard against re-capturing was `daily_reset == today`,
which resets to None on every process restart -- so every restart was
silently treated as a brand new trading day, moving the 1%
daily-loss-limit baseline to whatever equity happened to be at that exact
restart. 12 separate 'NEW DAY: 2026-08-14' lines fired on 2026-08-14
alone. Real day P&L was already -1.08% (past the 1% bull-regime halt
threshold) while the in-memory daily_pnl the halt actually checked was
reset to ~$0 by the most recent restart and never came close to tripping.

Run with:
  python scripts/test_daily_state_persistence.py
No network calls -- writes/reads a throwaway state file, restores the
module's real state file path afterward either way.
"""
import sys
import json
import datetime
import tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.session.session as sess

today = datetime.date.today()
yesterday = today - datetime.timedelta(days=1)

_orig_file = sess._DAILY_STATE_FILE
tmp_dir = Path(tempfile.mkdtemp())

try:
    # --- A same-day persisted state must be restored exactly (this is the
    #     restart-survival case that was broken). ---
    sess._DAILY_STATE_FILE = tmp_dir / "same_day.json"
    sess._DAILY_STATE_FILE.write_text(json.dumps({
        "daily_reset": str(today), "daily_start_equity": 2139.06,
    }))
    sess.daily_reset = None
    sess.daily_start_equity = 0.0
    sess.load_daily_state()
    assert sess.daily_reset == today, "same-day state must be restored, not treated as a new day"
    assert sess.daily_start_equity == 2139.06, "start equity must come from the persisted file, not be re-captured"

    # --- A stale (prior-day) persisted state must NOT be restored -- a
    #     genuinely new trading day still needs reset_daily() to recapture. ---
    sess._DAILY_STATE_FILE = tmp_dir / "stale_day.json"
    sess._DAILY_STATE_FILE.write_text(json.dumps({
        "daily_reset": str(yesterday), "daily_start_equity": 1900.00,
    }))
    sess.daily_reset = None
    sess.daily_start_equity = 0.0
    sess.load_daily_state()
    assert sess.daily_reset is None, "a prior day's state must not carry over into today"
    assert sess.daily_start_equity == 0.0

    # --- Missing file -> no-op, never raises. ---
    sess._DAILY_STATE_FILE = tmp_dir / "does_not_exist.json"
    sess.daily_reset = None
    sess.daily_start_equity = 0.0
    sess.load_daily_state()
    assert sess.daily_reset is None
    assert sess.daily_start_equity == 0.0

    # --- save_daily_state() round-trips through load_daily_state(). ---
    sess._DAILY_STATE_FILE = tmp_dir / "roundtrip.json"
    sess.daily_reset = today
    sess.daily_start_equity = 2100.50
    sess.save_daily_state()
    sess.daily_reset = None
    sess.daily_start_equity = 0.0
    sess.load_daily_state()
    assert sess.daily_reset == today
    assert sess.daily_start_equity == 2100.50

finally:
    sess._DAILY_STATE_FILE = _orig_file
    sess.daily_reset = None
    sess.daily_start_equity = 0.0

print("OK: daily-start-equity survives a same-day restart, ignores a stale prior-day file, "
      "handles a missing file, and round-trips through save/load correctly")

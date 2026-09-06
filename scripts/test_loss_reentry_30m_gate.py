"""Focused self-check for the loss re-entry 30-minute gate.

Run:
  python scripts/test_loss_reentry_30m_gate.py
"""

import datetime as _dt
import os
import sys

import pandas as pd
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine.execution import enhanced as ee


EASTERN = pytz.timezone("America/New_York")
REAL_DATETIME = ee.datetime.datetime
REAL_GET_BARS = ee.get_bars


def _set_now(hour: int, minute: int) -> None:
    fixed = EASTERN.localize(_dt.datetime(2026, 8, 28, hour, minute))

    class FrozenDatetime(REAL_DATETIME):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    ee.datetime.datetime = FrozenDatetime


def _bars(first_30_move: float, open_to_current: float) -> pd.DataFrame:
    day_open = 100.0
    start = EASTERN.localize(_dt.datetime(2026, 8, 28, 9, 30))
    rows = []
    for i in range(35):
        close = day_open + (first_30_move * (i + 1) / 30.0) if i < 30 else day_open + open_to_current
        rows.append({
            "time": start + _dt.timedelta(minutes=i),
            "open": day_open,
            "close": close,
        })
    return pd.DataFrame(rows)


def _check(label: str, expected: bool, is_long: bool, frame: pd.DataFrame) -> str:
    ee.get_bars = lambda *args, **kwargs: frame
    ok, reason = ee.EnhancedExecutor._check_30m_reentry_performance("FOO", is_long)
    assert ok is expected, f"{label}: expected {expected}, got {ok}: {reason}"
    return reason


try:
    _set_now(9, 45)
    ee.get_bars = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("pre-10 gate should not fetch bars"))
    ok, reason = ee.EnhancedExecutor._check_30m_reentry_performance("FOO", True)
    assert ok is True and "skipping" in reason, reason

    _set_now(10, 5)
    long_reason = _check("long aligned", True, True, _bars(first_30_move=1.25, open_to_current=2.50))
    assert "30m gate passed" in long_reason and "first30 +1.25" in long_reason, long_reason

    _check("long wrong current direction", False, True, _bars(first_30_move=1.25, open_to_current=-0.20))

    short_reason = _check("short aligned", True, False, _bars(first_30_move=-1.25, open_to_current=-2.50))
    assert "30m gate passed" in short_reason and "open-to-current -2.50" in short_reason, short_reason

    _check("short wrong current direction", False, False, _bars(first_30_move=-1.25, open_to_current=0.20))
finally:
    ee.datetime.datetime = REAL_DATETIME
    ee.get_bars = REAL_GET_BARS

print("OK: loss re-entry 30m gate skips before 10:00 ET and enforces first30/open-to-current direction after 10:00 ET")

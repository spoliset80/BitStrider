"""Regression check: stale/empty data suppresses a symbol only after 10 consecutive pulls."""

import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.utils.bars as bars

bars._dead_ticker_hits.clear()
bars._dead_tickers.clear()
bars._DEAD_TICKER_THRESHOLD = 10
bars._DEAD_TICKER_RECHECK_SEC = 900

for _ in range(9):
    bars._record_empty_bars("STALE10")

assert not bars.is_dead_ticker("STALE10"), "9 stale pulls must not suppress yet"

bars._record_empty_bars("STALE10")
assert bars.is_dead_ticker("STALE10"), "10th consecutive stale pull must suppress"

bars._record_ok_bars("STALE10")
assert not bars.is_dead_ticker("STALE10"), "fresh data must clear stale suppression immediately"

for _ in range(10):
    bars._record_empty_bars("RECHECK10")

assert bars.is_dead_ticker("RECHECK10")
bars._dead_tickers["RECHECK10"] = time.time() - bars._DEAD_TICKER_RECHECK_SEC - 1
assert not bars.is_dead_ticker("RECHECK10"), "recheck window must allow one probe after suppression"

print("OK: stale data filter suppresses after exactly 10 consecutive stale/empty pulls and clears on fresh data")

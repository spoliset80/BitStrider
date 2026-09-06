"""Self-check for the 2026-08-17 fix: close_eod_positions and
close_guardrail_fail_positions now run every minute through their close
window (schedule.every(1).minutes) instead of once per day, so a position
opened AFTER the first post-close-time tick (ASST/NUAI, opened 15:57 ET,
12 min after both jobs had already run-and-parked for the day) still gets
caught. This checks the idempotency side of that change: a rerun must not
resubmit while the close order still rests at the broker, but must still
catch a symbol that shows up later -- and, 2026-09-04 (NFLX race-fill), must
RE-close a position that reappears after its earlier chain was closed and no
working close order remains.

Run with:
  python scripts/test_eod_close_rerun.py
No network calls / no broker connection -- everything (broker client,
volume/float/mcap lookups) is faked.
"""
import datetime
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytz

import engine.execution.enhanced as enhanced
from engine.config import MIN_AVG_DAILY_VOLUME_REGULAR_HOURS, MIN_FLOAT_SHARES, MIN_MARKET_CAP

ET = pytz.timezone("America/New_York")
FIXED_NOW = ET.localize(datetime.datetime(2026, 8, 17, 15, 50))  # inside both close windows
WEEKEND_NOW = ET.localize(datetime.datetime(2026, 8, 22, 15, 50))
EARLY_BEFORE_EOD = ET.localize(datetime.datetime(2026, 11, 27, 12, 45))
EARLY_EOD = ET.localize(datetime.datetime(2026, 11, 27, 12, 50))
EARLY_AFTER_CLOSE = ET.localize(datetime.datetime(2026, 11, 27, 13, 5))


class _FixedDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW


class FakePosition:
    def __init__(self, symbol, qty, current_price=5.0, unrealized_pl=1.0):
        self.symbol, self.qty, self.current_price, self.unrealized_pl = symbol, qty, current_price, unrealized_pl


class FakeClient:
    def __init__(self):
        self.positions = []
        self.submitted = []  # symbols a closing order was submitted for, in order
        self.orders = []     # orders resting at the broker (get_orders reflects these)
        self.calendar_close = None
        self.calendar_calls = 0

    def get_all_positions(self):
        return self.positions

    def get_orders(self, *args, **kwargs):
        return self.orders

    def cancel_order_by_id(self, order_id):
        pass

    def submit_order(self, req):
        self.submitted.append(req.symbol)

    def get_calendar(self, req):
        self.calendar_calls += 1
        if self.calendar_close is None:
            return []
        return [type("Cal", (), {"close": self.calendar_close})()]


today = datetime.date.today()

with patch("engine.execution.enhanced.datetime.datetime", _FixedDateTime):
    # ---- close_eod_positions: reruns must not double-submit, but must catch new arrivals ----
    client = FakeClient()
    ex = enhanced.EnhancedExecutor(client)
    client.positions = [FakePosition("FOO", 10)]
    ex._entry_log["FOO"] = {"date": today, "strategy": "VWAPReclaim"}  # in EOD_CLOSE_STRATEGIES

    s1 = ex.close_eod_positions()
    assert client.submitted == ["FOO"], client.submitted
    assert "FOO" not in ex._entry_log  # popped on close, same as before this change

    # 2026-09-04 (NFLX): the rerun contract is now "must not resubmit while a
    # close order still rests at the broker; MUST re-close a position that is
    # still open with NO working close order" -- a race-fill can reappear
    # after the first pass (NFLX refilled 15:44:37 and its done-set entry
    # from the earlier chain blocked the rerun, leaving it open overnight-
    # risk). Model the resting close order for the no-resubmit case:
    client.orders = [type("O", (), {"id": "close-1", "symbol": "FOO"})()]
    s2 = ex.close_eod_positions()  # FOO still open, close order resting -- must not resubmit
    assert client.submitted == ["FOO"], f"duplicate resubmit: {client.submitted}"

    client.orders = []  # the resting close is gone (cancelled/expired) but FOO is still open
    ex.close_eod_positions()  # reappeared with no working close -> MUST re-close
    assert client.submitted == ["FOO", "FOO"], client.submitted
    # The re-close now rests at the broker too -- pass 3 must not triple-submit.
    client.orders = [type("O", (), {"id": "close-2", "symbol": "FOO"})()]

    client.positions.append(FakePosition("BAR", 5))
    client.positions.append(FakePosition("BAZ", 3))  # no entry_log row: still must close at EOD
    ex._entry_log["BAR"] = {"date": today, "strategy": "VWAPReclaim"}
    ex.close_eod_positions()  # a symbol that shows up later must still get caught
    assert client.submitted == ["FOO", "FOO", "BAR", "BAZ"], client.submitted

    # ---- close_guardrail_fail_positions: same rerun contract, via the per-symbol _guardrail_eod_closed set ----
    # 2026-08-23, user request: GUARDRAIL_EOD_CLOSE_ENABLED now defaults False
    # (disabled, no longer relevant) -- the rerun-idempotency logic under
    # test still lives in the function and stays correct if it's ever
    # re-enabled, so force it on for this test rather than deleting coverage
    # for working code.
    client2 = FakeClient()
    ex2 = enhanced.EnhancedExecutor(client2)
    client2.positions = [FakePosition("THIN1", 10), FakePosition("GOOD1", 10)]

    def fake_daily_bars(sym):
        import pandas as pd
        vol = (MIN_AVG_DAILY_VOLUME_REGULAR_HOURS - 1) if sym.startswith("THIN") else 2_000_000
        return pd.DataFrame({"volume": [vol, vol]})

    with patch.object(enhanced, "get_daily_volume_bars", fake_daily_bars), \
         patch.object(enhanced, "_get_float_shares", lambda sym: 500_000_000), \
         patch.object(enhanced, "_get_market_cap", lambda sym: 500_000_000), \
         patch.object(enhanced, "GUARDRAIL_EOD_CLOSE_ENABLED", True):

        ex2.close_guardrail_fail_positions()
        assert client2.submitted == ["THIN1"], client2.submitted  # GOOD1 passes guardrails, left alone

        ex2.close_guardrail_fail_positions()  # THIN1 still "open" -- must not re-cancel/resubmit
        assert client2.submitted == ["THIN1"], f"duplicate resubmit: {client2.submitted}"

        client2.positions.append(FakePosition("THIN2", 10))
        ex2.close_guardrail_fail_positions()  # a symbol that shows up later must still get caught
        assert client2.submitted == ["THIN1", "THIN2"], client2.submitted

    class _WeekendDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return WEEKEND_NOW

    client3 = FakeClient()
    ex3 = enhanced.EnhancedExecutor(client3)
    client3.positions = [FakePosition("WEEKEND", 10)]
    with patch("engine.execution.enhanced.datetime.datetime", _WeekendDateTime):
        assert ex3.close_eod_positions() is None
        assert client3.submitted == [], client3.submitted

    class _EarlyBeforeDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return EARLY_BEFORE_EOD

    client4 = FakeClient()
    client4.calendar_close = datetime.time(13, 0)
    ex4 = enhanced.EnhancedExecutor(client4)
    client4.positions = [FakePosition("EARLY", 10)]
    with patch("engine.execution.enhanced.datetime.datetime", _EarlyBeforeDateTime):
        assert ex4.close_eod_positions() is None
        assert client4.submitted == [], client4.submitted
        assert client4.calendar_calls == 1, client4.calendar_calls

    class _EarlyEodDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return EARLY_EOD

    with patch("engine.execution.enhanced.datetime.datetime", _EarlyEodDateTime):
        ex4.close_eod_positions()
        assert client4.submitted == ["EARLY"], client4.submitted
        assert client4.calendar_calls == 1, "exchange close should be cached per day"

    class _EarlyAfterCloseDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return EARLY_AFTER_CLOSE

    client4.positions.append(FakePosition("TOO_LATE", 10))
    with patch("engine.execution.enhanced.datetime.datetime", _EarlyAfterCloseDateTime):
        assert ex4.close_eod_positions() is None
        assert client4.submitted == ["EARLY"], client4.submitted

print("OK: both EOD close jobs are safe to rerun every minute -- no duplicate closes, new arrivals still caught")

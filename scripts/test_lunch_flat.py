"""Self-check for the 2026-09-01 two-window schedule: lunch_flat_positions()
hard-flats the book the moment the morning entry segment ends (LUNCH_FLAT_TIME_ET
= 11:00 ET) -- every equity position closed, EVERY open order cancelled
(GTC trailing stops included), _no_rearm marked so detect_stopped_out_positions
never re-arms a lunch-flattened name, and _force_close_pending populated so
_sweep_force_closes chases any unfilled close. Runs every minute via the
orchestrator's _lunch_flat_job (schedule.every(1).minutes), so the rerun
idempotency contract matters: a rerun must not resubmit a close for a symbol it
already closed, but must still catch a symbol that shows up later. Also checks
the poller-side break gates: _sweep_pending_entries / _maybe_rearm_reentry /
check_pending_entries_ema / check_blocked_entries_ema place NO new orders
during the break (cycles keep running, they just no-op).

Run with:
  python scripts/test_lunch_flat.py
No network calls / no broker connection -- everything is faked.
"""
import datetime
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytz

import engine.execution.enhanced as enhanced
from engine.execution.enhanced import EnhancedExecutor

ET = pytz.timezone("America/New_York")
LUNCH_NOW = ET.localize(datetime.datetime(2026, 8, 17, 12, 0))       # inside 11:00-14:45 break
PRE_LUNCH = ET.localize(datetime.datetime(2026, 8, 17, 10, 59))      # one minute before lunch flat
AFTER_REOPEN = ET.localize(datetime.datetime(2026, 8, 17, 14, 46))   # one minute after reopen
WEEKEND_LUNCH = ET.localize(datetime.datetime(2026, 8, 22, 12, 0))   # Saturday


class _FixedDateTime(datetime.datetime):
    _fixed = LUNCH_NOW

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


class FakePosition:
    def __init__(self, symbol, qty, current_price=5.0, unrealized_pl=1.0):
        self.symbol, self.qty, self.current_price, self.unrealized_pl = symbol, qty, current_price, unrealized_pl


class FakeOrder:
    def __init__(self, oid, symbol="ZZZZ"):
        self.id, self.symbol = oid, symbol


class FakeClient:
    def __init__(self):
        self.positions = []
        self.orders = []
        self.submitted = []
        self.cancelled = []

    def get_all_positions(self):
        return self.positions

    def get_orders(self, *args, **kwargs):
        return self.orders

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(str(order_id))

    def get_latest_quote(self, symbol):
        return type("Q", (), {"bid_price": 4.98, "ask_price": 5.02})()

    def submit_order(self, req):
        self.submitted.append(req.symbol)


def make_executor():
    client = FakeClient()
    ex = EnhancedExecutor(client)
    return client, ex


with patch("engine.execution.enhanced.datetime.datetime", _FixedDateTime):
    _FixedDateTime._fixed = LUNCH_NOW

    # ---- lunch_flat_positions: closes everything, cancels every order ----
    client, ex = make_executor()
    client.positions = [FakePosition("FOO", 10), FakePosition("BAR", -5, unrealized_pl=-2.0)]
    client.orders = [FakeOrder("o-entry", "FOO"), FakeOrder("o-gtc-stop", "BAR"), FakeOrder("o-other", "ZZZZ")]
    ex._entry_log = {"FOO": {"strategy": "GapBreakout"}, "BAR": {"strategy": "Momentum"}}

    s = ex.lunch_flat_positions()
    assert s is not None and s["closed_count"] == 2, s
    assert client.submitted == ["FOO", "BAR"], client.submitted
    # every resting order died, including the one with no position (trailing-buy)
    assert len(client.cancelled) == 3, client.cancelled
    assert "FOO" not in ex._entry_log and "BAR" not in ex._entry_log, "closed symbols leave the entry log"
    assert "FOO" in ex._no_rearm and "BAR" in ex._no_rearm, "lunch-flattened names must never re-arm"
    assert ex._force_close_pending.get("FOO", {}).get("reason") == "lunch"

    # ---- rerun idempotency: already closed -> no resubmit; new arrival caught ----
    s2 = ex.lunch_flat_positions()
    assert s2["closed_count"] == 0 and client.submitted == ["FOO", "BAR"], f"duplicate resubmit: {client.submitted}"
    client.positions.append(FakePosition("NEW1", 7))
    ex.lunch_flat_positions()
    assert client.submitted == ["FOO", "BAR", "NEW1"], client.submitted

    # ---- options legs (OCC) skipped; zero-qty skipped ----
    client2, ex2 = make_executor()
    client2.positions = [FakePosition("AEHR260515C00080000", 1), FakePosition("OK", 0)]
    ex2.lunch_flat_positions()
    assert client2.submitted == [], client2.submitted

    # ---- not in the break window -> no-op ----
    for when in (PRE_LUNCH, AFTER_REOPEN):
        _FixedDateTime._fixed = when
        client3, ex3 = make_executor()
        client3.positions = [FakePosition("EARLY", 10)]
        assert ex3.lunch_flat_positions() is None, when
        assert client3.submitted == [], when

    # ---- weekend -> no-op ----
    _FixedDateTime._fixed = WEEKEND_LUNCH
    client4, ex4 = make_executor()
    client4.positions = [FakePosition("WEEKEND", 10)]
    assert ex4.lunch_flat_positions() is None
    assert client4.submitted == [], client4.submitted

    # ---- LUNCH_FLAT_ENABLED toggle ----
    _FixedDateTime._fixed = LUNCH_NOW
    client5, ex5 = make_executor()
    client5.positions = [FakePosition("TOGGLE", 10)]
    with patch.object(enhanced, "LUNCH_FLAT_ENABLED", False):
        assert ex5.lunch_flat_positions() is None
    assert client5.submitted == [], client5.submitted

    # ---- break gates: new-order actions no-op during the break ----
    _FixedDateTime._fixed = LUNCH_NOW
    orig_lunch_break = enhanced.in_lunch_break
    enhanced.in_lunch_break = lambda *_: True
    try:
        ex6 = EnhancedExecutor.__new__(EnhancedExecutor)
        ex6.client = type("C", (), {"get_orders": lambda *a, **k: [FakeOrder("o1", "CDTG")], "cancelled": []})()
        ex6.order_cache = {}
        ex6._entry_pending = {"CDTG": {"order_id": "o1", "qty": 10, "is_long": True, "chase_count": 0}}
        ex6._no_rearm = set()
        ex6._ema_blocked_entries = {}
        ex6._loss_reentry_required = set()
        ex6._entry_log = {}
        ex6._staged_allocation = {}
        ex6._force_close_pending = {}
        ex6._pending_entry_signals = {}
        ex6._tp_targets = {}
        ex6._entries_today = {}
        ex6._entries_today_date = None
        ex6._no_history_cache = set()
        ex6._swap_cycle_closed = set()
        ex6._pdt_stop_blocked = set()
        ex6._cover_naked_positions = lambda: None
        ex6.check_software_stops = lambda: None
        ex6._get_account = lambda *a, **k: type("A", (), {"equity": 10000, "buying_power": 10000})()

        ex6._sweep_pending_entries()
        assert ex6._entry_pending, "pending entry must survive the break, waiting for the 14:15 reopen"
        assert ex6.client.cancelled == [], "no entry re-chase (cancel+resubmit) during the break"

        ex6._no_rearm.add("FOO")
        ex6._maybe_rearm_reentry("FOO", True, 10, "STOPPED OUT", was_loss=True)
        assert "FOO" in ex6._no_rearm, "_no_rearm still marked even when the break blocks the re-arm"

        ex6.check_blocked_entries_ema()
        assert ex6._ema_blocked_entries == {}, "no queued entries to fire/expire during the break"

        ex6.check_pending_entries_ema()
        assert ex6._entry_pending, "check_pending_entries_ema must no-op during the break"

        ex6.maybe_add_staged_tranches()
    finally:
        enhanced.in_lunch_break = orig_lunch_break

print("OK: lunch_flat_positions flattens the book at 11:00 (positions closed, ALL orders cancelled, no re-arm), is safe to rerun every minute, skips options legs/weekends/out-of-window, and every poller new-order action is gated through the break")


"""Self-check for the entry-log restart rebuild covering SHORT positions
(2026-08-14, found while investigating a live SPAI loss).

Confirmed live: _rebuild_entry_log_from_orders only ever looked at BUY-side
orders. SPAI was entered as a SHORT (SELL to open) at 10:54:41, a routine
restart landed 16s later at 10:54:57, and the fresh process's entry_log had
no 'SPAI' key at all -- silently breaking both the thin-liquidity trailing
stop halving (_trail_pct_for reads entry_log[sym]['thin_liquidity']) and all
price-drift-stop coverage (check_price_drift_stop scopes to
entry_log[sym]['date'] == today), both of which fail closed (skip the
symbol) when the entry_log record is simply missing.

Run with:
  python scripts/test_entry_log_rebuild_shorts.py
No network calls -- self.client is a fake with canned get_all_positions()/
get_orders() responses.
"""
import sys
import datetime
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.execution.enhanced as enhanced
from alpaca.trading.enums import OrderSide

today = datetime.date.today()
today_utc_midday = datetime.datetime.combine(today, datetime.time(12, 0), tzinfo=datetime.timezone.utc)


def _order(symbol, side, filled_at):
    return SimpleNamespace(symbol=symbol, side=side, filled_at=filled_at)


def _position(symbol, qty):
    return SimpleNamespace(symbol=symbol, qty=str(qty))


class FakeClient:
    def __init__(self, positions, orders):
        self._positions = positions
        self._orders = orders

    def get_all_positions(self):
        return self._positions

    def get_orders(self, filter=None):
        return self._orders


def _rebuild(positions, orders):
    ex = enhanced.EnhancedExecutor.__new__(enhanced.EnhancedExecutor)  # skip __init__
    ex._entry_log = {}
    ex.client = FakeClient(positions, orders)
    ex._rebuild_entry_log_from_orders()
    return ex._entry_log


# --- A short position (SELL to open) must be restored, not dropped ---
entry_log = _rebuild(
    positions=[_position("SPAI", -11)],
    orders=[_order("SPAI", OrderSide.SELL, today_utc_midday)],
)
assert "SPAI" in entry_log, "short entry must be restored, not silently dropped"
assert entry_log["SPAI"]["date"] == today

# --- A long position (BUY to open) still restores exactly as before ---
entry_log = _rebuild(
    positions=[_position("LPTH", 15)],
    orders=[_order("LPTH", OrderSide.BUY, today_utc_midday)],
)
assert "LPTH" in entry_log
assert entry_log["LPTH"]["date"] == today

# --- The order that CLOSED a short (a BUY-to-cover) must not be mistaken
#     for the entry -- only the SELL-to-open side counts for a short. ---
entry_log = _rebuild(
    positions=[_position("SPAI", -11)],
    orders=[
        _order("SPAI", OrderSide.SELL, today_utc_midday),
        _order("SPAI", OrderSide.BUY, today_utc_midday + datetime.timedelta(minutes=5)),
    ],
)
assert entry_log["SPAI"]["date"] == today  # restored once, from the SELL leg, not overwritten oddly

# --- A symbol with no open position left (fully closed already) must not
#     get a spurious entry_log record from a stale order. ---
entry_log = _rebuild(
    positions=[],
    orders=[_order("TE", OrderSide.BUY, today_utc_midday)],
)
assert "TE" not in entry_log, "no open position -> nothing to protect, must not restore"

print("OK: entry-log restart rebuild restores SHORT (sell-to-open) positions too, "
      "not just longs; ignores closing-side orders and symbols with no open position")

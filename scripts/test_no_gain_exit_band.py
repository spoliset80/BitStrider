"""Self-check for the no-gain-exit band change (2026-08-11) and long+short
symmetry fix (2026-08-12).

Was (2026-08-10): exit only if held >= 24h AND gain <= 0% -- no downside
cutoff at all, so a stale position could sit arbitrarily negative and this
rule would never touch it (only the ~8% trailing stop eventually caught a
real decline).

2026-08-11: held >= 8h (was 24h), and exit on EITHER a positive gain (stop
waiting once it's decided which way it's going) OR a drop of
NO_GAIN_EXIT_MAX_LOSS_PCT (-1.5%) or worse (cut it well before the full
trailing stop would). Only a narrow flat/small-loss band -- (-1.5%, 0%] --
still survives the check and keeps holding.

2026-08-12: was long-only ("if qty <= 0: continue") -- found a live short
(ACHR) held well past NO_GAIN_EXIT_HOURS with no exit path at all. Now
applies to both directions: pos.unrealized_plpc is already sign-correct for
shorts (negative when a short is losing), so the gain_pct band logic is
unchanged; only the close side changes (SELL for longs, BUY/cover for
shorts).

Run with:
  python scripts/test_no_gain_exit_band.py
No network calls -- client is stubbed, _submit_closing_order records what
side it was called with instead of hitting the broker.
"""
import sys
import datetime
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.execution.enhanced import EnhancedExecutor, OrderSide
from engine.config import NO_GAIN_EXIT_HOURS, NO_GAIN_EXIT_MIN_PCT, NO_GAIN_EXIT_MAX_LOSS_PCT

assert NO_GAIN_EXIT_HOURS == 8, f"expected 8h threshold, config has {NO_GAIN_EXIT_HOURS}"
assert NO_GAIN_EXIT_MIN_PCT == 0.0, f"expected 0.0% ceiling, config has {NO_GAIN_EXIT_MIN_PCT}"
assert NO_GAIN_EXIT_MAX_LOSS_PCT == -1.5, f"expected -1.5% cutoff, config has {NO_GAIN_EXIT_MAX_LOSS_PCT}"


class _FakeClient:
    def __init__(self, pos):
        self._pos = pos
    def get_all_positions(self):
        return [self._pos]
    def get_orders(self, filter=None):
        return []  # no resting orders -- goes straight to the close attempt


def _run(held_hours: float, gain_pct: float, qty: int = 10):
    """Run the real close_no_gain_positions() against one fake position.
    Returns (closed, side_used) -- side_used is None if nothing was closed."""
    ex = EnhancedExecutor.__new__(EnhancedExecutor)  # skip __init__ (no broker creds needed)
    now = datetime.datetime.now(datetime.timezone.utc)
    entry_dt = now - datetime.timedelta(hours=held_hours)
    pos = SimpleNamespace(symbol="TEST", qty=qty, unrealized_plpc=gain_pct / 100,
                           unrealized_pl=1.23, current_price=10.0)
    ex.client = _FakeClient(pos)
    ex._entry_log = {"TEST": {"filled_at": entry_dt, "strategy": "TestStrat"}}
    ex._no_gain_chase_count = {}
    calls = []
    ex._submit_closing_order = lambda sym, q, side, price, **k: calls.append(side)
    result = ex.close_no_gain_positions()
    closed = result["closed_count"] == 1
    return closed, (calls[0] if calls else None)


def _closed(held_hours, gain_pct, qty=10):
    return _run(held_hours, gain_pct, qty)[0]


cases = [
    (7.9,  -5.0, False, "under the 8h threshold -- never checked, however bad the P&L"),
    (8.1,   0.0, False, "flat at 8h+ -- still in the (-1.5%, 0%] band, keeps holding"),
    (8.1,  -1.0, False, "small loss at 8h+ -- still in the band, keeps holding"),
    (8.1,  -1.5, True,  "exactly at the -1.5% cutoff -- exits"),
    (8.1,  -2.0, True,  "past the -1.5% cutoff -- exits"),
    (8.1,  +2.0, True,  "positive at 8h+ -- exits, doesn't wait for more"),
]

for held_hours, gain_pct, expect_closed, label in cases:
    got = _closed(held_hours, gain_pct, qty=10)
    assert got == expect_closed, (
        f"held={held_hours}h gain={gain_pct}% -> closed={got}, expected {expect_closed} ({label})"
    )

# Same band logic, short side (qty < 0) -- mirrors the long cases above.
# gain_pct is pre-computed from unrealized_plpc, already sign-correct for
# shorts, so the same numbers/expectations apply; only the order side differs.
for held_hours, gain_pct, expect_closed, label in cases:
    got = _closed(held_hours, gain_pct, qty=-10)
    assert got == expect_closed, (
        f"[short] held={held_hours}h gain={gain_pct}% -> closed={got}, expected {expect_closed} ({label})"
    )

# Close side must match direction: SELL closes a long, BUY covers a short.
closed, side = _run(8.1, +2.0, qty=10)
assert closed and side == OrderSide.SELL, "closing a long should SELL"

closed, side = _run(8.1, +2.0, qty=-10)
assert closed and side == OrderSide.BUY, "covering a short should BUY, not SELL -- this was the bug"

print("OK: no-gain-exit 8h/-1.5% band applies symmetrically to longs and shorts, with the correct close side")

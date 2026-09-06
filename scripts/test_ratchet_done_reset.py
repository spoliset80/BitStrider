"""Self-check for the _ratchet_done reset fix (2026-08-10).

Bug: _ratchet_done was symbol-keyed and add-only, so a symbol re-entered
after an earlier lot had already ratcheted got zero profit-lock protection
on the new position (ABCL ran +6.3% unrealized on a fresh entry and never
tightened -- enhanced.py:1132).

Fix: detect_stopped_out_positions() -- the generic "position closed via any
route" catch-all that already runs every 10s -- now discards the symbol from
_ratchet_done as soon as it disappears from the open-positions list, freeing
it up for the next entry.

Run with:
  python scripts/test_ratchet_done_reset.py
No network calls -- get_all_positions() is stubbed.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.execution.enhanced import EnhancedExecutor


class _FakeClient:
    def __init__(self, positions):
        self._positions = positions
    def get_all_positions(self):
        return self._positions


def _make_executor(open_positions):
    ex = EnhancedExecutor.__new__(EnhancedExecutor)  # skip __init__ (no broker creds needed)
    ex.client = _FakeClient(open_positions)
    ex._last_known_positions = {
        "ABCL": {"entry_price": 9.41, "last_price": 10.00, "is_long": True},
    }
    ex._ratchet_done = {"ABCL"}  # simulates lot 1 already ratcheted
    ex._entry_ema15_delta = {"ABCL": -0.5}  # simulates a captured entry-time delta
    ex._entry_ema15 = {"ABCL": 9.91}        # simulates the captured entry-time EMA15 itself
    ex._reclaimed_ema15 = {"ABCL"}          # simulates ABCL having reclaimed EMA15
    return ex


# Case 1: ABCL no longer in the broker's position list (i.e., it closed) ->
# _ratchet_done must be cleared so the next entry is ratchet-eligible again.
ex = _make_executor(open_positions=[])
ex.detect_stopped_out_positions()
assert "ABCL" not in ex._ratchet_done, "closed symbol should be evicted from _ratchet_done"
assert "ABCL" not in ex._entry_ema15_delta, "closed symbol's entry EMA15 delta should be evicted too"
assert "ABCL" not in ex._entry_ema15, "closed symbol's entry EMA15 itself should be evicted too"
assert "ABCL" not in ex._reclaimed_ema15, "closed symbol's reclaim flag should be evicted too"

# Case 2: ABCL still open -> untouched (still the position that was ratcheted;
# don't let it re-tighten mid-life).
_fake_pos = SimpleNamespace(symbol="ABCL", avg_entry_price=9.41, current_price=10.00, qty=9)
ex2 = _make_executor(open_positions=[_fake_pos])
ex2.detect_stopped_out_positions()
assert "ABCL" in ex2._ratchet_done, "still-open symbol must not be evicted"
assert "ABCL" in ex2._entry_ema15_delta, "still-open symbol's entry EMA15 delta must not be evicted"
assert "ABCL" in ex2._entry_ema15, "still-open symbol's entry EMA15 itself must not be evicted"
assert "ABCL" in ex2._reclaimed_ema15, "still-open symbol's reclaim flag must not be evicted"

print("OK: _ratchet_done / _entry_ema15_delta reset behaves correctly")

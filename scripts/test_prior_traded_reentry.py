"""Self-check for treating ANY prior-traded stock as a re-entry (2026-08-18,
user request): "the re entry to trail buy doesn't have to come from cool down
list only, even if the non cool down reentry to a prior traded stock is
entering put in a trail buy order".

Same-day count and post-loss cooldown don't cover a symbol that WON its last
trade (no cooldown ever set) or was traded days ago (cooldown long expired) --
neither catches it, so it fell through to the normal marketable chase. Fix:
_is_reentry_signal() also asks _get_entry_datetime() (broker-confirmed fill
history, survives restarts unlike _entry_log alone) whether this symbol has
ANY prior fill at all.

Run with:
  python scripts/test_prior_traded_reentry.py
No network calls -- client.get_orders is stubbed.
"""
import sys
import datetime
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.execution.enhanced as enhanced
from engine.execution.enhanced import EnhancedExecutor


class _Order:
    def __init__(self, filled_at):
        self.filled_at = filled_at


class _CountingClient:
    """get_orders returns `orders` and counts how many times it's called --
    proves the no-history result gets cached instead of re-querying forever."""
    def __init__(self, orders):
        self._orders = orders
        self.calls = 0
    def get_orders(self, filter=None):
        self.calls += 1
        return self._orders


def _make_executor(client_orders):
    ex = EnhancedExecutor.__new__(EnhancedExecutor)
    ex._entries_today = {}
    ex._entries_today_date = None
    ex._no_history_cache = set()
    ex._entry_log = {}
    ex.client = _CountingClient(client_orders)
    return ex

# --- A symbol we've genuinely never traded: not a re-entry, and the broker
#     lookup only happens once (cached after the first "no history" result). ---
ex = _make_executor(client_orders=[])
assert ex._is_reentry_signal("NEVER") is False, "a symbol with zero fill history must not be flagged"
assert ex.client.calls == 1
assert "NEVER" in ex._no_history_cache
assert ex._is_reentry_signal("NEVER") is False, "second check must reuse the cached no-history result"
assert ex.client.calls == 1, f"must not re-query the broker for an already-confirmed no-history symbol, got {ex.client.calls} calls"

# --- A symbol with broker fill history (won its last trade days ago, no
#     cooldown ever set, not traded again today) -- must be flagged, via the
#     broker fallback since _entry_log has nothing for it either. ---
ex = _make_executor(client_orders=[_Order(filled_at=datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc))])
assert ex._is_reentry_signal("SNDQ", is_long=True) is True, "a symbol with any prior fill history must be flagged for the trailing-buy path"

# --- _entry_log alone (no broker call needed) is enough too, e.g. still
#     warm in-memory from earlier this session. ---
ex = _make_executor(client_orders=[])
ex._entry_log["WARM"] = {"filled_at": datetime.datetime.now(datetime.timezone.utc)}
assert ex._is_reentry_signal("WARM") is True, "an in-memory entry_log record must count as prior history"
assert ex.client.calls == 0, "must not hit the broker when _entry_log already answers the question"

print("OK: any symbol with prior fill history (broker-confirmed or in-memory) is treated as a re-entry; a genuinely new symbol is checked once and cached")

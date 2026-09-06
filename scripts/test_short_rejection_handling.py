"""Self-check for shorting_blocked (live property) and the HTB/equity
conflation fix (2026-08-10).

Bug: "account is not allowed to short" (Alpaca 40310000) fires both for a
genuine per-symbol no-borrow condition AND for the account-wide Reg T
equity minimum (MIN_EQUITY_FOR_SHORT) -- same wording, different causes.
The old code always cached the rejecting symbol as hard-to-borrow, so a
rejection that was really just "equity dipped under $2,000 for an hour"
permanently blacklisted whatever ticker happened to trigger it (confirmed
2026-08-10: FIG and RIG both got stuck this way and stayed blocked for the
rest of the session even after equity recovered).

Fix: _handle_short_rejection() checks the live equity gate first
(shorting_blocked, now a live property instead of a sticky
self.shorting_blocked = True flag) and only caches the symbol when the
rejection is genuinely symbol-specific.

Run with:
  python scripts/test_short_rejection_handling.py
No network calls -- get_account() is stubbed.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.execution.enhanced import EnhancedExecutor
from engine.config import MIN_EQUITY_FOR_SHORT


class _FakeClient:
    def __init__(self, equity):
        self._equity = equity
    def get_account(self):
        return SimpleNamespace(equity=self._equity, buying_power=self._equity,
                                daytrade_count=0, pattern_day_trader=False)


def _make_executor(equity):
    ex = EnhancedExecutor.__new__(EnhancedExecutor)  # skip __init__ (no broker creds needed)
    ex.client = _FakeClient(equity)
    ex._account_cache = None
    ex._account_ttl = 2.0
    ex._htb_cache = set()
    return ex


_sig = SimpleNamespace(symbol="FIG")

# Case 1: equity below the Reg T floor -> shorting_blocked True, rejection
# treated as transient/account-wide, symbol NOT cached as hard-to-borrow.
ex = _make_executor(equity=MIN_EQUITY_FOR_SHORT - 50)
assert ex.shorting_blocked is True
ex._handle_short_rejection(_sig, Exception("account is not allowed to short"))
assert "FIG" not in ex._htb_cache, "equity-caused rejection must not poison the symbol HTB cache"

# Case 2: equity comfortably above the floor -> shorting_blocked False,
# rejection is genuinely symbol-specific, DOES get cached.
ex2 = _make_executor(equity=MIN_EQUITY_FOR_SHORT + 200)
assert ex2.shorting_blocked is False
ex2._handle_short_rejection(_sig, Exception("account is not allowed to short"))
assert "FIG" in ex2._htb_cache, "genuine per-symbol rejection should still be cached"

print("OK: shorting_blocked + short-rejection handling behave correctly")

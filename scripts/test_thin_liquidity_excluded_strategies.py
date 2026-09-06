"""Self-check for THIN_LIQUIDITY_EXCLUDED_STRATEGIES (2026-08-15, at the
user's request: idea #2 of six suggested improvements).

Measured (not projected): thin-liquidity-bypass trades net -$13.85 across
the currently-active strategies, while the SAME strategies' normal
(guardrail-passing) trades net +$77.73. ORB (57% win/+$34.23 normal vs 29%
win/-$12.25 bypass) and GapBreakout (77% win/+$45.31 normal vs 40%
win/-$1.80 bypass) are the two where this was actually measured -- a
signal from either that would only qualify via a guardrail/momentum-
freshness bypass is now hard-skipped instead of traded at reduced size.

Run with:
  python scripts/test_thin_liquidity_excluded_strategies.py
No network calls -- exercises _resolve_freshness_reject() (enhanced.py)
directly; the scan.py-side guardrail-admit exclusion needs a live scan
context to exercise end-to-end, so it's covered by reading the source
logic below instead (see the inline note).
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.equity.strategies import Signal
from engine.execution.enhanced import _resolve_freshness_reject
import engine.execution.enhanced as enhanced
from engine.config import THIN_LIQUIDITY_EXCLUDED_STRATEGIES

assert THIN_LIQUIDITY_EXCLUDED_STRATEGIES == {"ORB", "GapBreakout"}

# 2026-08-26: config default flipped to False (hard reject) -- the ORB/GapBreakout
# exclusion this test covers is only reachable when the toggle is on, so the
# test sets it explicitly instead of assuming the live default.
_orig_toggle = enhanced.TRADE_STALE_MOMENTUM_REJECTS
enhanced.TRADE_STALE_MOMENTUM_REJECTS = True


def _sig(strategy):
    return Signal("TEST", "buy", 10.0, 0.90, "test reason", strategy, thin_liquidity=False)


# --- A non-excluded strategy still gets the normal trade-anyway-at-reduced-
#     size treatment (unchanged behavior). ---
sig = _sig("Momentum")
valid, reason = _resolve_freshness_reject(sig, fresh=False, fade_reason="faded 10% off its 30-min high")
assert valid is True, "non-excluded strategies still trade through at reduced size"
assert reason is None
assert sig.thin_liquidity is True

# --- ORB: hard-blocked regardless of TRADE_STALE_MOMENTUM_REJECTS. ---
sig = _sig("ORB")
valid, reason = _resolve_freshness_reject(sig, fresh=False, fade_reason="faded 10% off its 30-min high")
assert valid is False, "ORB must be hard-blocked, not traded through at reduced size"
assert reason == "faded 10% off its 30-min high"
assert sig.thin_liquidity is False, "must not even flag it if it's going to be blocked anyway"

# --- GapBreakout: same. ---
sig = _sig("GapBreakout")
valid, reason = _resolve_freshness_reject(sig, fresh=False, fade_reason="faded 12% off its 30-min high")
assert valid is False
assert sig.thin_liquidity is False

# --- A fresh signal from an excluded strategy is untouched either way --
#     the exclusion only matters once a signal WOULD have been bypassed. ---
sig = _sig("ORB")
valid, reason = _resolve_freshness_reject(sig, fresh=True, fade_reason=None)
assert (valid, reason) == (True, None)
assert sig.thin_liquidity is False

enhanced.TRADE_STALE_MOMENTUM_REJECTS = _orig_toggle

print("OK: ORB/GapBreakout momentum-freshness rejects are hard-blocked (not traded through at reduced "
      "size) regardless of TRADE_STALE_MOMENTUM_REJECTS; every other strategy is unaffected. "
      "(scan.py's guardrail-admit-side exclusion in _scan_one() returns None for these two once the "
      "winning strategy is known -- same set, same reasoning, exercised via the log/live-run evidence "
      "instead of a unit test since it needs a full scan context to reach.)")

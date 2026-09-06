"""Self-check for _execute_bull_plan's ranked-until-cap-succeeds execution
(2026-08-14, at the user's request: "we should have seen multiple stock
picks").

Old behavior: sliced to the top signals_cap candidates by confidence BEFORE
attempting anything -- if those all failed (momentum freshness, hard-to-
borrow, insufficient buying power...), the cycle never even looked at the
next-ranked candidates. Confirmed live: 5 signals at 96-97% confidence,
cap=3, top 3 all failed, other 2 never tried. New behavior: keep walking
down the ranked list until signals_cap executions actually SUCCEED, or the
list runs out -- same risk cap, just doesn't give up early on failures that
were never going to fill.

Run with:
  python scripts/test_execute_bull_plan.py
No network calls -- ctx.executor.execute and _session are stubbed.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.orchestrator as orch

# Stub out the real delays and session/P&L side effects.
orch.time.sleep = lambda *_a, **_k: None
orch._session.refresh_daily_pnl = lambda *_a, **_k: None
orch._session.daily_pnl = 0.0
orch._session.trades = 0


def _sig(symbol, confidence):
    return SimpleNamespace(symbol=symbol, action="buy", price=10.0, confidence=confidence,
                            strategy="TestStrat", reason="test")


# A, B fail (e.g. momentum freshness / hard-to-borrow / insufficient BP);
# C, D succeed; E should never even be attempted once the cap (2) is hit.
FAIL_SYMS = {"A", "B"}
call_order = []


def _fake_execute(sig, swap_only=False):
    call_order.append(sig.symbol)
    return sig.symbol not in FAIL_SYMS


ctx = SimpleNamespace(executor=SimpleNamespace(execute=_fake_execute), client=None)
eligible = [_sig("E", 75), _sig("A", 95), _sig("C", 85), _sig("D", 80), _sig("B", 90)]  # deliberately unsorted

orch._execute_bull_plan(ctx, eligible, signals_cap=2, regime="bull", daily_loss_limit=-999_999, loss_pct=1.0)

assert call_order == ["A", "B", "C", "D"], (
    f"expected to walk past both failures (A, B) and stop right after the 2nd success (D), "
    f"never reaching E; got {call_order}"
)
assert orch._session.trades == 2, f"expected 2 successful trades counted, got {orch._session.trades}"

# --- Daily loss limit mid-cycle still halts, even with cap unmet ---
orch._session.trades = 0
call_order.clear()
orch._execute_bull_plan(ctx, eligible, signals_cap=2, regime="bull", daily_loss_limit=999_999, loss_pct=1.0)
assert call_order == [], "daily_pnl (0.0) <= daily_loss_limit (999999) must halt before the first attempt"
assert orch._session.trades == 0

print("OK: _execute_bull_plan walks past failures to reach the cap, stops once reached, still respects the daily loss limit")

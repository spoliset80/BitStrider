"""Self-check for the momentum-entry freshness guard (2026-08-11, widened
2026-08-12).

Reject a gap/momentum signal if price has already faded off its recent high
by the time the order is about to submit -- different axis from the
(disabled) GAP_CHASE guard, which gates on gap SIZE. Case studies from
2026-08-11 (ACHR, PLUG, CLSK, SOUN) all faded before or shortly after fill
despite gaps ranging 5.5%-27.9%: magnitude didn't predict the failure,
timing did. NBIL (2026-08-12, ORB) was the same shape but ORB wasn't in the
watch list yet -- widened to every LONG breakout/continuation strategy (see
engine/config.py MOMENTUM_FRESHNESS_STRATEGIES for the full list and why
VWAPFade/LiquiditySweep/PowerOf3 are deliberately excluded: they enter
*because* of a recent pullback, this check would break their own signal).

Run with:
  python scripts/test_momentum_freshness.py
No network calls -- get_bars is monkeypatched in engine.execution.enhanced.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import engine.execution.enhanced as enhanced
from engine.equity.strategies import Signal

enhanced.MOMENTUM_FRESHNESS_ENABLED = True  # ensure the master switch is on for this check


def _bars(highs, closes):
    return pd.DataFrame({"high": highs, "close": closes})


def _sig(strategy="PreMarketMomentum", price=10.0):
    return Signal("TEST", "buy", price, 0.90, "test reason", strategy)


# --- strategy not in the watch list -> always fresh, never even calls get_bars.
#     VWAPFade/LiquiditySweep/PowerOf3 are DELIBERATELY excluded from the
#     2026-08-12 widening -- they enter LONG because of a recent pullback, so
#     this check would false-reject their own signal. ---
enhanced.get_bars = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called"))
for _excluded in ("VWAPFade", "LiquiditySweep", "PowerOf3"):
    fresh, reason = enhanced._check_momentum_freshness(_sig(strategy=_excluded))
    assert fresh is True and reason is None, f"{_excluded} must stay excluded"

# --- NBIL-class case: ORB wasn't in the watch list before 2026-08-12, now is ---
enhanced.get_bars = lambda *a, **k: _bars([25.0, 27.0, 28.76, 27.5], [25.0, 27.0, 27.0, 26.5])
fresh, reason = enhanced._check_momentum_freshness(_sig(strategy="ORB"))
assert fresh is False, "ORB should now be covered by the freshness guard (NBIL case)"

# --- PLUG's actual 2nd entry, replayed: 30-min high $2.535, fill $2.38 (-6.1%) -> rejected ---
enhanced.get_bars = lambda *a, **k: _bars([2.40, 2.49, 2.535, 2.42, 2.38], [2.40, 2.49, 2.42, 2.36, 2.38])
fresh, reason = enhanced._check_momentum_freshness(_sig())
assert fresh is False, "PLUG's 2nd entry should be caught (6.1% off the 30-min high)"
assert "faded" in reason and "TEST" in reason

# --- still near the high (1% pullback) -> fresh, don't block normal noise ---
enhanced.get_bars = lambda *a, **k: _bars([10.0, 10.1, 10.2], [10.0, 10.1, 10.1])
fresh, reason = enhanced._check_momentum_freshness(_sig())
assert fresh is True and reason is None

# --- boundary: exactly at MOMENTUM_FRESHNESS_MAX_PULLBACK_PCT -> still fresh (strict >) ---
from engine.config import MOMENTUM_FRESHNESS_MAX_PULLBACK_PCT
high = 100.0
at_boundary_price = high * (1 - MOMENTUM_FRESHNESS_MAX_PULLBACK_PCT / 100)
enhanced.get_bars = lambda *a, **k: _bars([high], [at_boundary_price])
fresh, reason = enhanced._check_momentum_freshness(_sig())
assert fresh is True, "exactly at the pullback threshold should survive"

# --- no bar data -> fresh (fail-open, never block on missing data) ---
enhanced.get_bars = lambda *a, **k: pd.DataFrame()
fresh, reason = enhanced._check_momentum_freshness(_sig())
assert fresh is True and reason is None

# --- master switch off -> always fresh regardless of how bad the fade is ---
enhanced.MOMENTUM_FRESHNESS_ENABLED = False
enhanced.get_bars = lambda *a, **k: _bars([100.0], [50.0])  # 50% fade
fresh, reason = enhanced._check_momentum_freshness(_sig())
assert fresh is True and reason is None
enhanced.MOMENTUM_FRESHNESS_ENABLED = True

print("OK: momentum freshness guard blocks stale gap/momentum entries, leaves other strategies and fresh moves alone")

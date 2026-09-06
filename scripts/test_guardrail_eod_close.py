"""Self-check for close_guardrail_fail_positions (2026-08-12, at the user's
request): 5 min before close, force-close any open position that currently
fails the standard avg_volume/float/mcap guardrails so only guardrail-passing
names get held after-hours/overnight.

Run with:
  python scripts/test_guardrail_eod_close.py
No network calls / no broker connection -- exercises the pure decision
function _guardrail_fail_reason() directly.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.execution.enhanced import EnhancedExecutor
from engine.config import MIN_AVG_DAILY_VOLUME_REGULAR_HOURS, MIN_FLOAT_SHARES, MIN_MARKET_CAP

f = EnhancedExecutor._guardrail_fail_reason

# All metrics comfortably above every floor -> holds overnight.
assert f(2_000_000, 500_000_000, 500_000_000) is None

# Below the volume floor alone -> fails.
assert f(MIN_AVG_DAILY_VOLUME_REGULAR_HOURS - 1, 500_000_000, 500_000_000) is not None

# Below the float floor alone -> fails.
assert f(2_000_000, MIN_FLOAT_SHARES - 1, 500_000_000) is not None

# Below the market-cap floor alone -> fails.
assert f(2_000_000, 500_000_000, MIN_MARKET_CAP - 1) is not None

# Exactly at every floor -> passes (strict '<', not '<=').
assert f(MIN_AVG_DAILY_VOLUME_REGULAR_HOURS, MIN_FLOAT_SHARES, MIN_MARKET_CAP) is None

# All data unavailable (yfinance miss) -> never force-closes on missing data.
assert f(None, None, None) is None

# Partial data: only market cap known and it fails -> still fails.
assert f(None, None, MIN_MARKET_CAP - 1) is not None

print("OK: guardrail-fail overnight exit closes only on a known metric below its floor; missing data never forces a close")

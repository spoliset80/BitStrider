"""Self-check for _should_admit_thin_liquidity's EOD cutoff (2026-08-17, at
the user's request): thin-liquidity admits must stop at EOD_CLOSE_TIME
(now 15:44 ET), not just anywhere in regular hours -- close_guardrail_fail_
positions only sweeps once, gated on that same time, so an admit after it
has no same-day guardrail check left to catch it before an overnight hold
(confirmed same-day: ASST and NUAI both admitted at 15:57 ET, 12 min after
that sweep already ran and marked itself done).

Run with:
  python scripts/test_thin_liquidity_eod_cutoff.py
No network calls -- exercises the pure decision function directly with a
synthetic MarketState.
"""
import datetime
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytz

from engine.config import TRADE_THIN_LIQUIDITY_REJECTS, EOD_CLOSE_TIME
from engine.equity.scan import _should_admit_thin_liquidity
from engine.utils.market import MarketState

ET = pytz.timezone("America/New_York")


def _state(hhmm: str) -> MarketState:
    h, m = map(int, hhmm.split(":"))
    now = ET.localize(datetime.datetime(2026, 8, 17, h, m))  # a Monday
    return MarketState.from_now(now)


if TRADE_THIN_LIQUIDITY_REJECTS:
    # Before the EOD cutoff, a real guardrail reason is still admitted (existing behavior).
    assert _should_admit_thin_liquidity("rvol", _state("14:00")) is True

    # At/after EOD_CLOSE_TIME, no more admits -- this is the fix. Reads the
    # live config value (now 15:44, was 15:45/15:50) rather than a hardcoded
    # literal so this doesn't silently go stale the next time it moves --
    # see scripts/test_entry_window.py for the incident that taught this.
    assert _should_admit_thin_liquidity("rvol", _state(EOD_CLOSE_TIME)) is False
    assert _should_admit_thin_liquidity("rvol", _state("15:57")) is False  # the ASST/NUAI repro

# Non-guardrail reasons (min_price, catch-all "other") are never admitted, cutoff or not.
assert _should_admit_thin_liquidity("min_price", _state("14:00")) is False
assert _should_admit_thin_liquidity("other", _state("14:00")) is False

# No market_state passed -> fails closed.
assert _should_admit_thin_liquidity("rvol", None) is False

print("OK: thin-liquidity admits stop at EOD_CLOSE_TIME -- no more entries slipping past the same-day guardrail sweep")

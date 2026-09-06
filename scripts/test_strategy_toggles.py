"""Self-check for the strategy enable/disable toggles (2026-08-14, at the
user's request: "disable all that are below 37% win rate" -- then refined
same day: "don't disable if the number of trade[s] [is] under 10 ... too
early to judge"). Same backtest methodology as VWAP_FADE_ENABLED, across
all 16 strategies:

Below 37% AND n>=10 -> disabled (confidence gating doesn't rescue either,
no winning bucket even at their own ceiling):
  Momentum           n=25  20% win  -1.73% avg
  PreMarketMomentum  n=25  32% win  -1.60% avg

Below 37% but n<10 -> left enabled, too small a sample to judge:
  Sentiment          n=9   22% win
  LiquiditySweep     n=4   25% win
  PMHighBreakout     n=3   33% win
  Technical          n=3    0% win

2026-08-15: FloatRotation (n=41, 39% win, net -$33.31, second-worst
dollar loser after VWAPFade) disabled at the user's explicit request
despite clearing the 37% win-rate line -- a fuller loss-attribution pass
showed its worst trades (DFSC -27%, BNRG -8.6%) were the clearest
examples of "chasing an already-extended move" across the whole loser
list.

Run with:
  python scripts/test_strategy_toggles.py
No network calls -- just checks get_strategy_instances()'s composition.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import (
    VWAP_FADE_ENABLED, MOMENTUM_ENABLED, SENTIMENT_ENABLED, LIQUIDITY_SWEEP_ENABLED,
    PRE_MARKET_MOMENTUM_ENABLED, PM_HIGH_BREAKOUT_ENABLED, TECHNICAL_ENABLED,
    FLOAT_ROTATION_ENABLED,
)
from engine.equity.strategies import get_strategy_instances

# Disabled -- either n>=10 and below 37% win rate, or (FloatRotation) an
# explicit override despite clearing that line.
DISABLED = {
    "VWAPFadeStrategy":          VWAP_FADE_ENABLED,
    "MomentumStrategy":          MOMENTUM_ENABLED,
    "PreMarketMomentumStrategy": PRE_MARKET_MOMENTUM_ENABLED,
    "FloatRotationStrategy":     FLOAT_ROTATION_ENABLED,
}
for name, enabled in DISABLED.items():
    assert enabled is False, f"{name} expected disabled (False)"

# n<10 (too early to judge) -> stay enabled.
STILL_ENABLED_FLAGS = {
    "SentimentStrategy":      SENTIMENT_ENABLED,
    "LiquiditySweepStrategy": LIQUIDITY_SWEEP_ENABLED,
    "PMHighBreakoutStrategy": PM_HIGH_BREAKOUT_ENABLED,
    "TechnicalStrategy":      TECHNICAL_ENABLED,
}
for name, enabled in STILL_ENABLED_FLAGS.items():
    assert enabled is True, f"{name} expected enabled (True) -- sample too small (n<10) to judge"

STILL_ENABLED_UNCONDITIONAL = {"GapBreakoutStrategy", "ORBStrategy",
                                "TrendBreakerStrategy", "VWAPReclaimStrategy"}

for bull in (True, False):
    names = {type(s).__name__ for s in get_strategy_instances(bull_regime=bull)}
    for disabled_name in DISABLED:
        assert disabled_name not in names, f"{disabled_name} must be excluded (bull_regime={bull})"
    for kept_name in set(STILL_ENABLED_FLAGS) | STILL_ENABLED_UNCONDITIONAL:
        assert kept_name in names, f"{kept_name} must still be active (bull_regime={bull})"

print("OK: Momentum/PreMarketMomentum/VWAPFade/FloatRotation are excluded; "
      "Sentiment/LiquiditySweep/PMHighBreakout/Technical stay active (n<10, too early to judge); "
      "GapBreakout/ORB/TrendBreaker/VWAPReclaim stay active")

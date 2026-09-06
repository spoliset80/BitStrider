"""Self-check for the per-strategy Kelly-informed sizing multiplier
(2026-08-15, at the user's request: idea #1 of six suggested improvements).

Kelly % = W - (1-W)/R computed from each strategy's actual matched
entry/exit trades since inception:
  GapBreakout  (n=18): Kelly +44% -- real edge, sized up 2.0x
  ORB          (n=82): Kelly  +7% -- thin edge, left at 1.0x (unchanged)
  TrendBreaker (n=18): Kelly  -8% -- losers run bigger than winners despite
    a >50% win rate; shrunk to 0.25x rather than disabled outright

Run with:
  python scripts/test_strategy_kelly_mult.py
No network calls -- exercises the pure sizing function directly.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.execution.enhanced import _apply_strategy_kelly_mult
from engine.config import STRATEGY_KELLY_MULT, STRATEGY_KELLY_MULT_DEFAULT, MAX_POSITION_CONCENTRATION_PCT

assert STRATEGY_KELLY_MULT_DEFAULT == 1.0
assert STRATEGY_KELLY_MULT["GapBreakout"] == 2.0
assert STRATEGY_KELLY_MULT["TrendBreaker"] == 0.25

risk_info = {"dollar_amount": 150.0, "allocation_pct": 7.5, "tier": "NORMAL"}

# A strategy at the 1.0x default -> unchanged, same dict (no copy) --
# includes ORB, which measured a real but thin edge (Kelly +7%) close
# enough to today's sizing that no adjustment was warranted.
out = _apply_strategy_kelly_mult(risk_info, "ORB", equity=2000.0)
assert out is risk_info, "1.0x (default) multiplier must not touch risk_info"

# GapBreakout: 2.0x -- doubles allocation_pct and dollar_amount.
out = _apply_strategy_kelly_mult(risk_info, "GapBreakout", equity=2000.0)
assert out["allocation_pct"] == 15.0, f"expected 7.5 x 2.0 = 15.0, got {out['allocation_pct']}"
assert out["dollar_amount"] == 300.0, f"expected 15% of $2000 = $300, got {out['dollar_amount']}"
assert risk_info["allocation_pct"] == 7.5, "original dict must not be mutated in place"

# TrendBreaker: 0.25x -- shrinks to a quarter, not disabled outright.
out = _apply_strategy_kelly_mult(risk_info, "TrendBreaker", equity=2000.0)
assert out["allocation_pct"] == 1.875, f"expected 7.5 x 0.25 = 1.875, got {out['allocation_pct']}"
assert out["dollar_amount"] == 37.5, f"expected 1.875% of $2000 = $37.50, got {out['dollar_amount']}"

# An unlisted strategy (e.g. one of the n<10 ones still enabled) falls
# back to the 1.0x default, not an error.
out = _apply_strategy_kelly_mult(risk_info, "Sentiment", equity=2000.0)
assert out is risk_info

# 2026-08-15: found by running the full sizing pipeline against a real
# GapBreakout/95%-confidence combo -- the confidence ramp alone pushes
# allocation_pct to 12.5%, and the unclamped 2.0x would then push it to
# 25%, past MAX_POSITION_CONCENTRATION_PCT (20%). The FINAL executed
# share count was already correctly capped downstream in
# _size_with_buying_power either way, but risk_info/dollar_amount and the
# debug log built from them were overstating what would really execute.
# Clamped here too (defense-in-depth) so risk_info can never claim more
# than the real ceiling at any pipeline stage.
high_base = {"dollar_amount": 300.0, "allocation_pct": 15.0, "tier": "NORMAL"}
out = _apply_strategy_kelly_mult(high_base, "GapBreakout", equity=2000.0)
assert out["allocation_pct"] == MAX_POSITION_CONCENTRATION_PCT, (
    f"7.5 x 2.0 -> 15 x 2.0 = 30, must be clamped to the {MAX_POSITION_CONCENTRATION_PCT}% cap, "
    f"got {out['allocation_pct']}"
)
assert out["dollar_amount"] == 2000.0 * MAX_POSITION_CONCENTRATION_PCT / 100.0, (
    "dollar_amount must be recomputed from the CLAMPED pct, not the raw 30%"
)

print("OK: per-strategy Kelly multiplier sizes GapBreakout up (2.0x), TrendBreaker down (0.25x), "
      "leaves ORB and unlisted strategies unchanged (1.0x default), never mutates the input dict")

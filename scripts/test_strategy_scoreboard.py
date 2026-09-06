"""Self-check for the strategy scoreboard's pure math (2026-08-15, at the
user's request: idea #4 of six suggested improvements, "a recurring
strategy scoreboard instead of one-off manual reviews").

Run with:
  python scripts/test_strategy_scoreboard.py
No network calls -- exercises kelly_pct()/should_flag()/_summarize()
directly, same numbers as the manual TrendBreaker/GapBreakout/ORB
backtest earlier this session, to confirm the automated version
reproduces the manually-verified result.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.strategy_scoreboard import kelly_pct, should_flag, _summarize, MIN_TRADES_TO_JUDGE, KNOWN_STRATEGIES

assert MIN_TRADES_TO_JUDGE == 10

# --- kelly_pct(): reproduces the three manually-computed numbers exactly ---
# GapBreakout: W=12/18=0.667, avg_win=5.4517, avg_loss=3.6517 -> Kelly +44%
gb = kelly_pct(12 / 18, 65.42 / 12, 21.91 / 6)
assert abs(gb - 0.4434) < 0.001, f"GapBreakout Kelly: expected ~0.4434, got {gb}"

# ORB: W=42/82=0.512, avg_win=3.7043, avg_loss=3.34 -> Kelly +7%
orb = kelly_pct(42 / 82, 155.58 / 42, 133.60 / 40)
assert abs(orb - 0.0724) < 0.001, f"ORB Kelly: expected ~0.0724, got {orb}"

# TrendBreaker: W=10/18=0.556, avg_win=1.594, avg_loss=2.2875 -> Kelly -8%
tb = kelly_pct(10 / 18, 15.94 / 10, 18.30 / 8)
assert abs(tb - (-0.0820)) < 0.001, f"TrendBreaker Kelly: expected ~-0.0820, got {tb}"

# --- Edge cases ---
assert kelly_pct(1.0, 5.0, 0.0) == 1.0, "no losses observed -> treated as strong positive edge, not a ZeroDivisionError"
assert kelly_pct(0.0, 0.0, 5.0) == -1.0, "0% win rate with real losses -> R=0 -> -1.0, not an error"

# --- should_flag(): enabled + enough trades + negative Kelly + a real,
#     known (currently-controllable) strategy ---
assert should_flag(enabled=True, n=25, kelly=-0.08) is True
assert should_flag(enabled=False, n=25, kelly=-0.08) is False, "disabled strategies are never flagged (already off)"
assert should_flag(enabled=True, n=3, kelly=-0.08) is False, "n < MIN_TRADES_TO_JUDGE -> too early to judge"
assert should_flag(enabled=True, n=25, kelly=0.07) is False, "positive Kelly -> nothing to flag"
assert should_flag(enabled=True, n=10, kelly=-0.01) is True, "n exactly at the floor still counts"
assert should_flag(enabled=True, n=25, kelly=-0.08, known=False) is False, (
    "retired code / execution artifacts (e.g. 'Sweepea', 'rechase') must never be flagged -- "
    "there's no config flag to act on either"
)

# --- KNOWN_STRATEGIES: confirms the exact two live-run surprises are excluded ---
assert "Sweepea" not in KNOWN_STRATEGIES, "retired strategy, no longer producible by get_strategy_instances()"
assert "rechase" not in KNOWN_STRATEGIES, "_sweep_pending_entries' re-chase coid tag, not a strategy"
assert "TrendBreaker" in KNOWN_STRATEGIES and "ORB" in KNOWN_STRATEGIES, "sanity check on the real list"

# --- _summarize(): groups by strategy, computes the same fields consistently ---
trades = [
    {"strategy": "A", "pnl_usd": 10.0}, {"strategy": "A", "pnl_usd": -5.0},
    {"strategy": "A", "pnl_usd": 8.0}, {"strategy": "B", "pnl_usd": -3.0},
]
summary = _summarize(trades)
assert summary["A"][0] == 3, "n=3 for strategy A"
assert abs(summary["A"][1] - 2 / 3) < 1e-9, "win_rate = 2 wins / 3 trades"
assert summary["B"][0] == 1
assert summary["B"][1] == 0.0, "0% win rate -- its only trade lost"

print("OK: strategy scoreboard's Kelly math reproduces the manually-verified GapBreakout/ORB/TrendBreaker "
      "numbers exactly, should_flag() gates on enabled+n+negative-Kelly correctly, _summarize() groups right")

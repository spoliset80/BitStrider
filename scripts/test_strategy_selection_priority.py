"""Regression check for signal selection priority within one symbol."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.equity.scan import _strategy_selection_rank
from engine.equity.strategies import Signal


def _signal(strategy: str, confidence: float) -> Signal:
    return Signal("TEST", "buy", 10.0, confidence, "test", strategy)


signals = [_signal("Technical", 0.97), _signal("ORB", 0.74)]
assert max(signals, key=_strategy_selection_rank).strategy == "ORB"

signals = [_signal("Momentum", 0.96), _signal("GapBreakout", 0.73)]
assert max(signals, key=_strategy_selection_rank).strategy == "GapBreakout"

signals = [_signal("ORB", 0.78), _signal("GapBreakout", 0.84)]
assert max(signals, key=_strategy_selection_rank).strategy == "GapBreakout"

signals = [_signal("Technical", 0.75), _signal("Momentum", 0.82)]
assert max(signals, key=_strategy_selection_rank).strategy == "Momentum"

print("OK: GapBreakout/ORB outrank other strategies; confidence breaks ties within each tier")

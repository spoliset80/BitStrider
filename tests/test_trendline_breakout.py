"""Regression tests for the active equity trendline-breakout adapter."""

import unittest
from unittest.mock import patch

import pandas as pd

from engine.equity.strategies import SweepeaStrategy, TrendlineBreakoutStrategy, get_strategy_instances
from scripts.trendline_breakout import make_synthetic_ohlcv


class TrendlineBreakoutStrategyTests(unittest.TestCase):
    def test_strategy_converts_confirmed_breakout_to_equity_signal(self):
        with patch("engine.equity.strategies.get_bars", return_value=make_synthetic_ohlcv()):
            signal = TrendlineBreakoutStrategy().scan("AAPL")

        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "AAPL")
        self.assertEqual(signal.action, "buy")
        self.assertEqual(signal.strategy, "TrendlineBreakout")
        self.assertGreater(signal.atr_stop, 0.0)
        self.assertIn("target $", signal.reason)

    def test_strategy_is_in_active_registry(self):
        strategy_names = {strategy.__class__.__name__ for strategy in get_strategy_instances()}
        self.assertIn("TrendlineBreakoutStrategy", strategy_names)

    @patch("engine.equity.strategies._is_bull_regime", return_value=True)
    @patch("engine.equity.strategies.get_bars")
    def test_sweepea_rejects_low_price_and_low_liquidity_names(self, mocked_get_bars, _mock_regime):
        closes = [4.10 + i * 0.03 for i in range(90)]
        lows = [max(3.8, c - 0.25) for c in closes]
        highs = [c + 0.30 for c in closes]
        volumes = [200_000] * 89 + [220_000]
        daily = pd.DataFrame({
            "open": [c - 0.05 for c in closes],
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        })
        mocked_get_bars.return_value = daily

        signal = SweepeaStrategy().scan("PENNY")

        self.assertIsNone(signal)


if __name__ == "__main__":
    unittest.main()
import datetime
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from engine.execution import enhanced
from engine.execution.enhanced import EnhancedExecutor


class MockPosition:
    def __init__(self, symbol, qty, current_price):
        self.symbol = symbol
        self.qty = qty
        self.current_price = current_price
        self.unrealized_pl = 0.0


class MockClient:
    def __init__(self, positions):
        self.positions = positions
        self.orders = []

    def get_all_positions(self):
        return self.positions

    def submit_order(self, order):
        self.orders.append(order)

    def get_orders(self):
        return []

    def cancel_order_by_id(self, order_id):
        pass


def build_executor(client, state_path):
    executor = object.__new__(EnhancedExecutor)
    executor.client = client
    executor._entry_log = {}
    executor._tp_targets = {}
    executor._intermediate_targets = {}
    executor._tightened = set()
    executor._live_probe_scaled_in = set()
    executor._exit_state_path = state_path
    executor._exit_state_lock = threading.Lock()
    return executor


class EquityExitLifecycleTests(unittest.TestCase):
    def test_live_probe_uses_one_share_after_entry_checks(self):
        executor = object.__new__(EnhancedExecutor)
        executor.use_bracket_orders = True
        submitted_shares = []
        signal = SimpleNamespace(symbol="AAPL", price=100.0, confidence=0.9, strategy="Momentum")
        account = SimpleNamespace(equity=10_000.0, buying_power=40_000.0, daytrade_count=0)
        executor.pdt = SimpleNamespace(add=lambda _date: None, remaining=lambda *_args: 999)
        executor._validate_trade = lambda *_args, **_kwargs: (True, None)
        executor._can_submit_live_probe = lambda: True
        executor._validate_market_price = lambda *_args: (True, 100.0)
        executor._size_with_buying_power = lambda *_args: (50, None)
        executor._current_market_state = lambda: SimpleNamespace(is_regular_hours=True)
        executor._create_bracket_order = lambda _signal, shares, *_args: submitted_shares.append(shares) or True
        executor._record_entry = lambda *_args: None
        executor._get_positions = lambda **_kwargs: None
        executor._get_account = lambda **_kwargs: None

        with patch.object(enhanced, "LIVE", True), patch.object(
            enhanced, "LIVE_PROBE_MODE", True
        ), patch.object(enhanced, "LIVE_PROBE_SHARES", 1), patch.object(
            enhanced, "MARGIN_LEVERAGE", 1.0
        ), patch.object(
            enhanced, "calculate_risk_adjusted_size", return_value={"dollar_amount": 5_000.0}
        ):
            self.assertTrue(executor._execute_entry(signal, account, enhanced.OrderType.LONG))

        self.assertEqual(submitted_shares, [1])

    def test_live_probe_daily_cap_blocks_entry(self):
        executor = object.__new__(EnhancedExecutor)
        executor._live_probe_count_date = datetime.date.today()
        executor._live_probe_entries_today = 10

        with patch.object(enhanced, "LIVE", True), patch.object(
            enhanced, "LIVE_PROBE_MODE", True
        ), patch.object(enhanced, "LIVE_PROBE_MAX_ENTRIES_PER_DAY", 10):
            self.assertFalse(executor._can_submit_live_probe())

    def test_profitable_live_probe_scales_in_once_to_cap(self):
        client = MockClient([MockPosition("AAPL", "1", 101.0)])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_probe_state.json")
        executor._options_cost_reserve = 0.0
        executor._pdt_stop_blocked = {}
        executor._get_account = lambda **_kwargs: SimpleNamespace(equity=10_000.0, buying_power=10_000.0)
        executor._current_market_state = lambda: SimpleNamespace(
            is_regular_hours=True,
            now=datetime.datetime(2026, 8, 20, 10, 30),
            resolve_regime=lambda: True,
        )
        executor._entry_log["AAPL"] = {
            "entry_time": datetime.datetime.now() - datetime.timedelta(minutes=31),
            "entry_price": 100.0,
        }

        with patch.object(enhanced, "LIVE", True), patch.object(
            enhanced, "LIVE_PROBE_MODE", True
        ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_ENABLED", True), patch.object(
            enhanced, "LIVE_PROBE_SCALE_IN_MINUTES", 30
        ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT", 0.5), patch.object(
            enhanced, "LIVE_PROBE_SCALE_IN_MAX_POSITION_PCT", 25.0
        ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_CUTOFF_TIME", "15:15"):
            with patch.object(enhanced, "get_dynamic_tier", return_value={"ts": 6.0}):
                executor.check_live_probe_scale_ins()
                executor.check_live_probe_scale_ins()

        self.assertEqual(len(client.orders), 2)
        self.assertEqual(client.orders[0].qty, 23)
        self.assertEqual(client.orders[1].qty, 23)

    def test_exit_state_round_trip_restores_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "position_exit_state.json"
            client = MockClient([MockPosition("AAPL", "10", 106.0)])
            source = build_executor(client, state_path)
            source._entry_log["AAPL"] = {
                "strategy": "Momentum",
                "date": datetime.date.today(),
                "confidence": 0.9,
                "entry_time": datetime.datetime.now() - datetime.timedelta(minutes=10),
                "entry_price": 100.0,
            }
            source._intermediate_targets["AAPL"] = 106.0
            source._tp_targets["AAPL"] = 120.0
            source._tightened.add("AAPL")
            source._save_exit_state()

            restored = build_executor(client, state_path)
            restored._restore_exit_state()

            self.assertEqual(restored._entry_log["AAPL"]["entry_price"], 100.0)
            self.assertIn("AAPL", restored._tightened)
            self.assertEqual(restored._intermediate_targets["AAPL"], 106.0)
            self.assertEqual(restored._tp_targets["AAPL"], 120.0)

    def test_restored_losing_position_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "position_exit_state.json"
            client = MockClient([MockPosition("AAPL", "10", 98.5)])
            source = build_executor(client, state_path)
            source._entry_log["AAPL"] = {
                "strategy": "Momentum",
                "date": datetime.date.today(),
                "confidence": 0.9,
                "entry_time": datetime.datetime.now() - datetime.timedelta(minutes=46),
                "entry_price": 100.0,
                "atr_stop": 0.0,
            }
            source._tp_targets["AAPL"] = 120.0
            source._save_exit_state()

            restored = build_executor(client, state_path)
            restored._restore_exit_state()
            with patch.object(enhanced, "DEAD_MONEY_MINUTES", 45), patch.object(
                enhanced, "DEAD_MONEY_MAX_ADVERSE_DRIFT_PCT", 1.5
            ):
                restored.check_dead_money()

            self.assertEqual(len(client.orders), 1)
            self.assertFalse(state_path.exists())

    def test_restored_profitable_position_is_not_time_loss_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "position_exit_state.json"
            client = MockClient([MockPosition("AAPL", "10", 101.5)])
            executor = build_executor(client, state_path)
            executor._entry_log["AAPL"] = {
                "strategy": "Momentum",
                "date": datetime.date.today(),
                "confidence": 0.9,
                "entry_time": datetime.datetime.now() - datetime.timedelta(minutes=46),
                "entry_price": 100.0,
                "atr_stop": 0.0,
            }

            with patch.object(enhanced, "DEAD_MONEY_MINUTES", 45), patch.object(
                enhanced, "DEAD_MONEY_MAX_ADVERSE_DRIFT_PCT", 1.5
            ):
                executor.check_dead_money()

            self.assertEqual(client.orders, [])

    def test_atr_time_loss_allows_normal_high_volatility_pullback(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MockClient([MockPosition("AAPL", "10", 98.5)])
            executor = build_executor(client, Path(directory) / "position_exit_state.json")
            executor._entry_log["AAPL"] = {
                "strategy": "Momentum",
                "date": datetime.date.today(),
                "confidence": 0.9,
                "entry_time": datetime.datetime.now() - datetime.timedelta(minutes=46),
                "entry_price": 100.0,
                "atr_stop": 9.0,
            }

            with patch.object(enhanced, "DEAD_MONEY_MINUTES", 45), patch.object(
                enhanced, "ATR_STOP_MULTIPLIER", 1.5
            ), patch.object(enhanced, "TIME_LOSS_ATR_MULTIPLIER", 0.35), patch.object(
                enhanced, "TIME_LOSS_ATR_MIN_PCT", 1.0
            ), patch.object(enhanced, "TIME_LOSS_ATR_MAX_PCT", 2.5):
                executor.check_dead_money()

            self.assertEqual(client.orders, [])

    def test_eod_close_releases_trailing_order_for_untracked_position(self):
        class EodClient(MockClient):
            def __init__(self):
                super().__init__([MockPosition("AAPL", "10", 100.0)])
                self.cancelled_orders = []

            def get_orders(self):
                return [SimpleNamespace(symbol="AAPL", id="trail-1")]

            def cancel_order_by_id(self, order_id):
                self.cancelled_orders.append(order_id)

        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 19, 15, 56, tzinfo=tz)

        with tempfile.TemporaryDirectory() as directory:
            client = EodClient()
            executor = build_executor(client, Path(directory) / "position_exit_state.json")
            executor._eod_close_done = None

            with patch.object(enhanced, "EOD_CLOSE_ENABLED", True), patch.object(
                enhanced, "EOD_CLOSE_ALL", True
            ), patch.object(enhanced, "EOD_CLOSE_TIME", "15:55"), patch.object(
                enhanced.datetime, "datetime", FixedDateTime
            ):
                summary = executor.close_eod_positions()

            self.assertEqual(client.cancelled_orders, ["trail-1"])
            self.assertEqual(len(client.orders), 1)
            self.assertEqual(summary["closed_count"], 1)


if __name__ == "__main__":
    unittest.main()
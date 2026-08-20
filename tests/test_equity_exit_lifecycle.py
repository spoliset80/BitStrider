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
    executor._exit_state_path = state_path
    executor._exit_state_lock = threading.Lock()
    return executor


class EquityExitLifecycleTests(unittest.TestCase):
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
            }

            with patch.object(enhanced, "DEAD_MONEY_MINUTES", 45), patch.object(
                enhanced, "DEAD_MONEY_MAX_ADVERSE_DRIFT_PCT", 1.5
            ):
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
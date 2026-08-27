import datetime
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from engine.execution import enhanced
from engine.execution.enhanced import EnhancedExecutor


class MockPosition:
    def __init__(self, symbol, qty, current_price, market_value=None, avg_entry_price=None):
        self.symbol = symbol
        self.qty = qty
        self.current_price = current_price
        self.avg_entry_price = current_price if avg_entry_price is None else avg_entry_price
        self.unrealized_pl = 0.0
        self.market_value = market_value if market_value is not None else float(qty) * current_price


class MockClient:
    def __init__(self, positions):
        self.positions = positions
        self.orders = []

    def get_all_positions(self):
        return self.positions

    def get_account(self):
        return SimpleNamespace(cash=1_000_000.0)

    def submit_order(self, order):
        self.orders.append(order)

    def get_orders(self):
        return []

    def cancel_order_by_id(self, order_id):
        pass


class FlattenClient(MockClient):
    def __init__(self, positions, close_errors=None):
        super().__init__(positions)
        self.close_errors = close_errors or {}
        self.close_attempts = []

    def close_position(self, symbol):
        self.close_attempts.append(symbol)
        error = self.close_errors.get(symbol)
        if error:
            raise error


def build_executor(client, state_path):
    executor = object.__new__(EnhancedExecutor)
    executor.client = client
    executor._entry_log = {}
    executor._tp_targets = {}
    executor._intermediate_targets = {}
    executor._tightened = set()
    executor._live_probe_scaled_in = set()
    executor._live_probe_scale_in_pending = {}
    executor._exit_state_path = state_path
    executor._exit_state_lock = threading.Lock()
    executor._probe_journal_path = state_path.with_name("probe_journal.jsonl")
    executor._probe_journal_lock = threading.Lock()
    return executor


class EquityExitLifecycleTests(unittest.TestCase):
    def test_scan_and_trade_keeps_live_probe_scale_checks_during_cutoff(self):
        executor = object.__new__(EnhancedExecutor)
        executor.check_live_probe_scale_ins = Mock()
        ctx = SimpleNamespace(client=None, executor=executor, options_executor=None, crypto_trader=None)

        with patch("engine.orchestrator._manage_intraday_window", return_value=False), patch(
            "engine.orchestrator.log"
        ) as mock_log:
            from engine.orchestrator import scan_and_trade

            scan_and_trade(ctx)

        executor.check_live_probe_scale_ins.assert_called_once()
        mock_log.info.assert_any_call("[SYSTEM] Outside an active intraday window or waiting for portfolio flatten")

    def test_flatten_ignores_inactive_assets(self):
        client = FlattenClient(
            [MockPosition("AVNS", "10", 5.0)],
            {"AVNS": RuntimeError('{"code":40010001,"message":"asset AVNS is not active"}')},
        )
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_flatten_state.json")

        self.assertTrue(executor.flatten_portfolio("INTRADAY FINAL RESET"))
        self.assertEqual(client.close_attempts, ["AVNS"])
        self.assertEqual(executor._flatten_in_progress, set())
        self.assertEqual(executor._flatten_failed, set())
        self.assertTrue(executor.flatten_portfolio("INTRADAY FINAL RESET"))
        self.assertEqual(client.close_attempts, ["AVNS"])

    def test_flatten_proceeds_after_close_requests_are_submitted(self):
        client = FlattenClient([MockPosition("AAPL", "10", 100.0)])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_flatten_state.json")

        self.assertTrue(executor.flatten_portfolio("INTRADAY FINAL RESET"))
        self.assertEqual(client.close_attempts, ["AAPL"])
        self.assertEqual(executor._flatten_in_progress, {"AAPL"})
        self.assertEqual(executor._flatten_failed, set())

    def test_flatten_still_waits_for_active_close_failure(self):
        client = FlattenClient(
            [MockPosition("AAPL", "10", 100.0)],
            {"AAPL": RuntimeError("temporary broker failure")},
        )
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_flatten_state.json")

        self.assertFalse(executor.flatten_portfolio("INTRADAY FINAL RESET"))
        self.assertEqual(executor._flatten_failed, {"AAPL"})

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

    def test_probe_uses_one_share_in_paper_mode(self):
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

        with patch.object(enhanced, "LIVE", False), patch.object(
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
        client = MockClient([MockPosition("AAPL", "1", 101.0, avg_entry_price=100.0)])
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
            enhanced, "LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT", 0.5
        ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_BUYING_POWER_PCT", 25.0):
            with patch.object(enhanced, "get_dynamic_tier", return_value={"ts": 6.0}):
                executor.check_live_probe_scale_ins()
                executor.check_live_probe_scale_ins()

        self.assertEqual(len(client.orders), 2)
        self.assertEqual(client.orders[0].qty, 23)
        self.assertEqual(client.orders[1].qty, 24)

    def test_live_probe_scale_in_replaces_existing_protective_order(self):
        class WashTradeClient(MockClient):
            def __init__(self):
                super().__init__([MockPosition("AAPL", "1", 101.0, avg_entry_price=100.0)])
                self.open_orders = [SimpleNamespace(symbol="AAPL", id="existing-trailing-stop")]
                self.cancelled_orders = []

            def get_orders(self):
                return self.open_orders

            def cancel_order_by_id(self, order_id):
                self.cancelled_orders.append(order_id)
                self.open_orders = [order for order in self.open_orders if order.id != order_id]

            def submit_order(self, order):
                if self.open_orders and order.side == enhanced.OrderSide.SELL:
                    raise RuntimeError("potential wash trade detected")
                self.orders.append(order)

        client = WashTradeClient()
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_probe_state.json")
        executor._options_cost_reserve = 0.0
        executor._get_account = lambda **_kwargs: SimpleNamespace(equity=10_000.0, buying_power=10_000.0)
        executor._current_market_state = lambda: SimpleNamespace(
            is_regular_hours=True,
            now=datetime.datetime(2026, 8, 20, 10, 30),
            resolve_regime=lambda: True,
        )
        executor._entry_log["AAPL"] = {"entry_price": 100.0}

        with patch.object(enhanced, "LIVE_PROBE_MODE", True), patch.object(
            enhanced, "LIVE_PROBE_SCALE_IN_ENABLED", True
        ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT", 0.5), patch.object(
            enhanced, "get_dynamic_tier", return_value={"ts": 6.0}
        ), patch.object(enhanced.time, "sleep"):
            executor.check_live_probe_scale_ins()

        self.assertEqual(client.cancelled_orders, ["existing-trailing-stop"])
        self.assertEqual(len(client.orders), 2)
        self.assertEqual(client.orders[1].qty, 24)
        self.assertIn("AAPL", executor._live_probe_scaled_in)

    def test_live_probe_scale_in_uses_limit_order_premarket(self):
        client = MockClient([MockPosition("AAPL", "1", 101.0, avg_entry_price=100.0)])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_probe_state.json")
        executor._options_cost_reserve = 0.0
        executor._get_account = lambda **_kwargs: SimpleNamespace(equity=10_000.0, buying_power=10_000.0)
        executor._current_market_state = lambda: SimpleNamespace(
            is_regular_hours=False,
            resolve_regime=lambda: True,
        )
        executor._after_hours_limit_price = lambda *_args: (101.1, None)
        executor._entry_log["AAPL"] = {"entry_price": 100.0}

        with patch.object(enhanced, "LIVE_PROBE_MODE", True), patch.object(
            enhanced, "LIVE_PROBE_SCALE_IN_ENABLED", True
        ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT", 0.5
        ):
            executor.check_live_probe_scale_ins()

        self.assertEqual(len(client.orders), 1)
        self.assertIsInstance(client.orders[0], enhanced.LimitOrderRequest)
        self.assertTrue(client.orders[0].extended_hours)
        self.assertIn("AAPL", executor._live_probe_scale_in_pending)

    def test_close_long_uses_limit_order_premarket(self):
        client = MockClient([MockPosition("AAPL", "10", 100.0)])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_exit_state.json")
        executor._get_positions = lambda **_kwargs: SimpleNamespace(
            has_position=lambda symbol: symbol == "AAPL",
            positions_dict={"AAPL": SimpleNamespace(qty="10")},
        )
        executor._current_market_state = lambda: SimpleNamespace(is_regular_hours=False)
        executor._after_hours_limit_price = lambda *_args: (99.5, None)
        signal = SimpleNamespace(symbol="AAPL", price=100.0, strategy="Momentum")

        self.assertTrue(executor._close_long_position(signal, 10_000.0))

        self.assertEqual(len(client.orders), 1)
        self.assertIsInstance(client.orders[0], enhanced.LimitOrderRequest)
        self.assertEqual(client.orders[0].limit_price, 99.5)
        self.assertTrue(client.orders[0].extended_hours)

    def test_short_probe_does_not_scale_in(self):
        client = MockClient([MockPosition("AAPL", "-1", 99.0)])
        with tempfile.TemporaryDirectory() as directory:
            executor = build_executor(client, Path(directory) / "exit_state.json")
            executor._options_cost_reserve = 0.0
            executor._pdt_stop_blocked = {}
            executor._get_account = lambda **_kwargs: SimpleNamespace(equity=10_000.0, buying_power=10_000.0)
            executor._current_market_state = lambda: SimpleNamespace(
                is_regular_hours=True,
                now=datetime.datetime(2026, 8, 20, 10, 30),
                resolve_regime=lambda: False,
            )
            executor._entry_log["AAPL"] = {
                "entry_time": datetime.datetime.now() - datetime.timedelta(minutes=31),
                "entry_price": 100.0,
            }

            with patch.object(enhanced, "LIVE", True), patch.object(
                enhanced, "LIVE_PROBE_MODE", True
            ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_ENABLED", True):
                executor.check_live_probe_scale_ins()

            self.assertEqual(client.orders, [])

    def test_probe_outcome_journal_records_managed_exit(self):
        client = MockClient([])
        with tempfile.TemporaryDirectory() as directory:
            executor = build_executor(client, Path(directory) / "exit_state.json")
            executor._entry_log["AAPL"] = {
                "strategy": "Momentum",
                "regime_at_entry": "bull",
                "entry_time": datetime.datetime(2026, 8, 20, 10, 0),
                "entry_price": 100.0,
            }
            position = MockPosition("AAPL", "2", 105.0)
            with patch.object(enhanced, "LIVE", True), patch.object(enhanced, "LIVE_PROBE_MODE", True):
                executor._record_probe_outcome("AAPL", position, "TP_CLOSE")

            record = json.loads(executor._probe_journal_path.read_text(encoding="utf-8"))
            self.assertEqual(record["strategy"], "Momentum")
            self.assertEqual(record["regime_at_entry"], "bull")
            self.assertEqual(record["estimated_pnl_pct"], 5.0)
            self.assertEqual(record["exit_reason"], "TP_CLOSE")

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

    def test_margin_eod_force_closes_position_ignoring_strategy_filter(self):
        class MarginClient(MockClient):
            def get_account(self):
                return SimpleNamespace(cash=500.0)

        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 19, 15, 56, tzinfo=tz)

        with tempfile.TemporaryDirectory() as directory:
            client = MarginClient([MockPosition("AAPL", "10", 100.0)])  # $1,000 market value > $500 cash
            executor = build_executor(client, Path(directory) / "position_exit_state.json")
            executor._eod_close_done = None
            executor._entry_log["AAPL"] = {"strategy": "Momentum", "date": datetime.date(2026, 8, 19)}

            with patch.object(enhanced, "EOD_CLOSE_ENABLED", True), patch.object(
                enhanced, "EOD_CLOSE_ALL", False
            ), patch.object(enhanced, "EOD_CLOSE_TIME", "15:55"), patch.object(
                enhanced, "MARGIN_EOD_FORCE_CLOSE", True
            ), patch.object(enhanced.datetime, "datetime", FixedDateTime):
                summary = executor.close_eod_positions()

            self.assertEqual(len(client.orders), 1)
            self.assertEqual(summary["closed_count"], 1)

    def test_no_margin_no_force_close_when_strategy_not_eligible(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 19, 15, 56, tzinfo=tz)

        with tempfile.TemporaryDirectory() as directory:
            client = MockClient([MockPosition("AAPL", "10", 100.0)])  # cash covers exposure — no margin
            executor = build_executor(client, Path(directory) / "position_exit_state.json")
            executor._eod_close_done = None
            executor._entry_log["AAPL"] = {"strategy": "Momentum", "date": datetime.date(2026, 8, 19)}

            with patch.object(enhanced, "EOD_CLOSE_ENABLED", True), patch.object(
                enhanced, "EOD_CLOSE_ALL", False
            ), patch.object(enhanced, "EOD_CLOSE_TIME", "15:55"), patch.object(
                enhanced, "MARGIN_EOD_FORCE_CLOSE", True
            ), patch.object(enhanced.datetime, "datetime", FixedDateTime):
                summary = executor.close_eod_positions()

            self.assertEqual(client.orders, [])
            self.assertEqual(summary["closed_count"], 0)

    def test_after_hours_entry_uses_live_quote_for_limit(self):
        class RecordingClient:
            def __init__(self):
                self.last_order = None
            def get_latest_quote(self, symbol):
                return SimpleNamespace(bid_price=98.8, ask_price=99.2)
            def submit_order(self, order):
                self.last_order = order
                return SimpleNamespace(id="order-1")

        client = RecordingClient()
        executor = object.__new__(EnhancedExecutor)
        executor.client = client
        executor.order_cache = {}
        executor._submitted_entry_orders = {}
        executor.market_state = None
        executor._current_market_state = lambda: SimpleNamespace(is_regular_hours=False)

        signal = SimpleNamespace(symbol="AAPL", price=100.0, strategy="Momentum")
        self.assertTrue(executor._create_simple_order(signal, 10, enhanced.OrderType.LONG))
        self.assertLessEqual(client.last_order.limit_price, 99.2)
        self.assertLess(client.last_order.limit_price, 100.0)

    def test_after_hours_eod_close_uses_executable_limit_order(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 20, 16, 30, tzinfo=tz)

        with tempfile.TemporaryDirectory() as directory:
            client = MockClient([MockPosition("AAPL", "10", 100.0)])
            executor = build_executor(client, Path(directory) / "exit_state.json")
            executor._eod_close_done = None
            executor._record_probe_outcome = lambda *_args: None

            with patch.object(enhanced, "EOD_CLOSE_ENABLED", True), patch.object(
                enhanced, "EOD_CLOSE_ALL", True
            ), patch.object(enhanced, "EOD_CLOSE_TIME", "15:55"), patch.object(
                enhanced.datetime, "datetime", FixedDateTime
            ):
                summary = executor.close_eod_positions()

            request = client.orders[0]
            self.assertEqual(request.limit_price, 99.0)
            self.assertTrue(request.extended_hours)
            self.assertEqual(summary["failed_count"], 0)

    def test_after_hours_eod_close_retries_when_quote_is_unavailable(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 20, 16, 30, tzinfo=tz)

        with tempfile.TemporaryDirectory() as directory:
            client = MockClient([MockPosition("AAPL", "10", 0.0)])
            executor = build_executor(client, Path(directory) / "exit_state.json")
            executor._eod_close_done = None
            executor._record_probe_outcome = lambda *_args: None

            with patch.object(enhanced, "EOD_CLOSE_ENABLED", True), patch.object(
                enhanced, "EOD_CLOSE_ALL", True
            ), patch.object(enhanced, "EOD_CLOSE_TIME", "15:55"), patch.object(
                enhanced.datetime, "datetime", FixedDateTime
            ):
                summary = executor.close_eod_positions()

            self.assertEqual(client.orders, [])
            self.assertEqual(summary["failed_count"], 1)
            self.assertIsNone(executor._eod_close_done)


if __name__ == "__main__":
    unittest.main()
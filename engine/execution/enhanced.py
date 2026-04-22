"""
ApexTrader - Enhanced Executor
Optimized trade executor with consolidated logic:
  - Reduced API calls through caching
  - Unified buy/short entry paths
  - Bracket orders with tiered SL/TP
  - PDT compliance
"""

import logging
import datetime
import re
import time
from typing import Optional, Dict, Tuple
from dataclasses import dataclass, field
from enum import Enum

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
    ReplaceOrderRequest,
    TrailingStopOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.trading.enums import OrderType as AlpacaOrderType

from engine.config import (
    PDT_ACCOUNT_MIN, PDT_MAX_TRADES,
    MAX_POSITIONS,
    SWAP_ON_FULL,
    SWAP_MIN_CONFIDENCE,
    EXTENDED_HOURS,
    USE_DYNAMIC_TIERS,
    USE_RISK_EQUALIZED_SIZING,
    USE_VIX_ROC_FILTER,
    MIN_BUYING_POWER_PCT, MIN_POSITION_DOLLARS, PDT_WARN_AT_REMAINING,
    TAKE_PROFIT_NORMAL, TAKE_PROFIT_HIGH, STOP_LOSS_PCT,
    ATR_TP_RATIO, MAX_SHORT_FLOAT_PCT, HIGH_SHORT_FLOAT_STOCKS, is_high_short_float,
    EOD_CLOSE_ENABLED, EOD_CLOSE_TIME, EOD_CLOSE_STRATEGIES,
    LONG_ONLY_MODE,
    STALE_ORDER_MINUTES, STALE_ORDER_MINUTES_INTRADAY,
    KILL_MODE_TRAIL_PCT,
    SMALL_ACCOUNT_EQUITY_THRESHOLD, SMALL_ACCOUNT_MAX_POSITIONS,
    SMALL_ACCOUNT_MIN_POSITION_DOLLARS,
    POSITION_SIZE_PCT, SMALL_ACCOUNT_POSITION_SIZE_PCT,
    CONF_SCALE_MIN_MULT, CONF_SCALE_FULL_CONF,
    LIVE,
)
from engine.equity.strategies import Signal
from engine.utils import MarketState, calculate_risk_adjusted_size, check_vix_roc_filter, get_dynamic_tier
from engine.notifications.notifications import send_email

log = logging.getLogger("ApexTrader")


# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Helpers
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
class OrderType(Enum):
    LONG  = "long"
    SHORT = "short"


@dataclass
class PDTTracker:
    """Pattern Day Trader tracking — syncs with live Alpaca daytrade_count."""
    trades: list = field(default_factory=list)

    def add(self, date: datetime.date) -> None:
        self.trades.append(date)
        cutoff = date - datetime.timedelta(days=7)
        self.trades = [d for d in self.trades if d > cutoff]

    def remaining(self, equity: float, live_count: int, pdt_flagged: bool = False) -> int:
        """Returns day trades remaining. 999 = exempt if account is PDT-exempt or equity >= $25k."""
        if equity >= PDT_ACCOUNT_MIN or not pdt_flagged:
            return 999
        used = max(live_count, len(self.trades))
        return max(0, PDT_MAX_TRADES - used)

    def can_trade(self, equity: float, live_count: int = 0, pdt_flagged: bool = False) -> bool:
        return self.remaining(equity, live_count, pdt_flagged) > 0


@dataclass
class PositionInfo:
    """Cached snapshot of open positions."""
    positions_dict: Dict[str, any]
    total_count:    int

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions_dict

    def is_long(self, symbol: str) -> bool:
        return self.has_position(symbol) and float(self.positions_dict[symbol].qty) > 0

    def is_short(self, symbol: str) -> bool:
        return self.has_position(symbol) and float(self.positions_dict[symbol].qty) < 0


@dataclass
class AccountSnapshot:
    """Cached Alpaca account state — equity, buying power, live PDT count."""
    equity:              float
    buying_power:        float
    daytrade_count:      int
    pattern_day_trader:  bool = False
    timestamp:           float = field(default=0.0)


# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Executor
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
class EnhancedExecutor:
    """Optimized trade executor with consolidated long/short logic."""

    def __init__(self, client: TradingClient, use_bracket_orders: bool = True):
        self.client              = client
        self.use_bracket_orders  = use_bracket_orders
        self.pdt                 = PDTTracker()
        self.order_cache:  Dict[str, str] = {}
        self._position_cache: Optional[PositionInfo]    = None
        self._cache_timestamp: float = 0
        self._cache_ttl:       float = 5.0
        self._account_cache:  Optional[AccountSnapshot] = None
        self._account_ttl:    float = 2.0   # tight TTL — buying power must be fresh between orders
        self._htb_cache:      set   = set()   # hard-to-borrow symbols — skip shorts this session
        self._entry_log:   Dict[str, dict] = {}  # {symbol: {"strategy": str, "date": date}}
        self._swap_cycle_closed: set = set()     # positions already swapped this scan cycle
        self._tp_targets: Dict[str, float] = {} # {symbol: take-profit price} for ATR-based TP tracking
        self.shorting_blocked: bool = False  # set true when broker rejects all short attempts for account
        self._pdt_stop_blocked: Dict[str, float] = {}  # {symbol: stop_price} — broker-rejected stops; monitored in software
        self._pdt_overnight_forced: set = set()  # symbols where PDT also blocks close — forced overnight, no retries
        self._pdt_violation_alerted: bool = False  # tracks whether the PDT violation email has been sent this session
        self._eod_close_done: object = None  # date of last completed EOD close (prevents duplicate runs)
        self.market_state: Optional[MarketState] = None
        self._rebuild_entry_log_from_orders()

    def update_market_state(self, market_state: MarketState) -> None:
        """Store the active market snapshot for per-cycle execution decisions."""
        self.market_state = market_state

    # -- Entry Log Rebuild (survive restarts) ----------------------------
    def _rebuild_entry_log_from_orders(self) -> None:
        """On startup, reconstruct today's entry log from Alpaca filled buy orders.
        Prevents swap-closes of same-day positions after a bot restart, which would
        trigger Alpaca PDT protection (error 40310100)."""
        try:
            today = datetime.date.today()
            import pytz
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            et       = pytz.timezone("America/New_York")
            # Filter to today only — avoids fetching the full account order history
            # on accounts with months of activity (can be thousands of orders).
            today_start = datetime.datetime.combine(today, datetime.time.min).replace(tzinfo=pytz.UTC)
            req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=today_start)
            filled_orders = self.client.get_orders(filter=req)
            for order in filled_orders:
                filled_at = getattr(order, "filled_at", None)
                if filled_at is None:
                    continue
                if hasattr(filled_at, "astimezone"):
                    order_date = filled_at.astimezone(et).date()
                else:
                    order_date = today  # conservative fallback
                if order_date != today:
                    continue
                side = str(getattr(order, "side", "")).lower()
                if side != "buy":
                    continue
                sym = order.symbol
                if sym not in self._entry_log:
                    self._entry_log[sym] = {
                        "strategy": "restored",
                        "date": today,
                        "confidence": 0.0,
                    }
            if self._entry_log:
                log.info(
                    f"Entry log rebuilt from today's orders: "
                    f"{', '.join(self._entry_log.keys())}"
                )
        except Exception as e:
            log.warning(f"_rebuild_entry_log_from_orders failed (non-fatal): {e}")

    def _current_market_state(self) -> MarketState:
        if self.market_state is not None:
            return self.market_state
        return MarketState.from_now()

    # -- Position Cache ----------------------------------------------------
    def _find_weakest_position(self) -> Optional[str]:
        """Return the symbol of the open long position with the worst unrealized P&L %.
        Only considers longs with no shares held for pending orders (closable immediately).
        Skips positions entered today (protected for full day) and those already closed this cycle.
        Returns None if no closable position found."""
        try:
            today = datetime.date.today()
            entered_today = {
                sym for sym, info in self._entry_log.items()
                if info.get("date") == today
            }
            positions = self.client.get_all_positions()
            longs = [
                p for p in positions
                if float(p.qty) > 0
                and float(getattr(p, "qty_available", p.qty)) > 0
                and p.symbol not in self._swap_cycle_closed
                and p.symbol not in entered_today
                and not re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', p.symbol)  # skip OCC option symbols
            ]
            if not longs:
                return None
            worst = min(longs, key=lambda p: float(p.unrealized_plpc))
            return worst.symbol
        except Exception as e:
            log.warning(f"_find_weakest_position error: {e}")
            return None

    def _find_least_confident_position(self, min_new_conf: float = 0.0) -> tuple:
        """Return (symbol, entry_confidence) of the held long position with the lowest
        entry confidence that is strictly below min_new_conf.
        Skips positions entered today (give them a full day) and those already swapped.
        Returns (None, 1.0) if no suitable candidate found."""
        try:
            today = datetime.date.today()
            entered_today = {
                sym for sym, info in self._entry_log.items()
                if info.get("date") == today
            }
            positions = self.client.get_all_positions()
            candidates = [
                p for p in positions
                if float(p.qty) > 0
                and float(getattr(p, "qty_available", p.qty)) > 0
                and p.symbol not in self._swap_cycle_closed
                and p.symbol not in entered_today
                and not re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', p.symbol)  # skip OCC option symbols
            ]
            if not candidates:
                return None, 1.0

            def _entry_conf(p):
                return self._entry_log.get(p.symbol, {}).get("confidence", 0.0)

            worst = min(candidates, key=_entry_conf)
            worst_conf = _entry_conf(worst)
            # Only swap if new signal is meaningfully more confident (>5% gap)
            if worst_conf >= min_new_conf - 0.05:
                return None, worst_conf
            return worst.symbol, worst_conf
        except Exception as e:
            log.warning(f"_find_least_confident_position error: {e}")
            return None, 1.0

    def _get_positions(self, force_refresh: bool = False) -> PositionInfo:
        now = time.time()
        if force_refresh or self._position_cache is None or (now - self._cache_timestamp) > self._cache_ttl:
            raw = self.client.get_all_positions()
            self._position_cache = PositionInfo(
                positions_dict={p.symbol: p for p in raw},
                total_count=len(raw),
            )
            self._cache_timestamp = now
        return self._position_cache

    # -- Account Cache -----------------------------------------------------
    def _get_account(self, force_refresh: bool = False) -> AccountSnapshot:
        now = time.time()
        if force_refresh or self._account_cache is None or (now - self._account_cache.timestamp) > self._account_ttl:
            raw = self.client.get_account()
            self._account_cache = AccountSnapshot(
                equity=float(raw.equity),
                buying_power=float(raw.buying_power),
                daytrade_count=int(raw.daytrade_count),
                pattern_day_trader=str(getattr(raw, "pattern_day_trader", False)).lower() in ("1", "true", "yes"),
                timestamp=now,
            )
        return self._account_cache

    # -- Validation --------------------------------------------------------
    def _validate_trade(self, signal: Signal, acct: AccountSnapshot, order_type: OrderType, swap_only: bool = False) -> Tuple[bool, Optional[str]]:
        if USE_VIX_ROC_FILTER:
            allow, roc = check_vix_roc_filter()
            if not allow:
                return False, f"VIX spike filter: {roc:.1f}% increase"

        # PDT — use live broker count (survives restarts).
        # Block only when the count EXCEEDS the limit (4+) — an actual PDT violation.
        # At exactly 3/3: new buys are allowed because they are held overnight (not same-day
        # round-trips) and therefore do NOT count as additional day trades.
        if acct.pattern_day_trader and acct.equity < PDT_ACCOUNT_MIN and acct.daytrade_count > PDT_MAX_TRADES:
            msg = (
                f"PDT VIOLATION: {acct.daytrade_count} day trades used "
                f"(limit {PDT_MAX_TRADES}, equity ${acct.equity:,.0f}) — "
                f"account may be flagged as Pattern Day Trader. Review immediately!"
            )
            log.error(msg)
            if not getattr(self, "_pdt_violation_alerted", False):
                send_email("[APEXTRADER] PDT VIOLATION ALERT", msg)
                self._pdt_violation_alerted = True
            return False, f"PDT violation: {acct.daytrade_count}/{PDT_MAX_TRADES} day trades exceeded"
        dt_left = self.pdt.remaining(acct.equity, acct.daytrade_count, acct.pattern_day_trader)
        if acct.pattern_day_trader and dt_left <= PDT_WARN_AT_REMAINING and acct.equity < PDT_ACCOUNT_MIN:
            log.warning(f"PDT WARNING: only {dt_left} day trade(s) remaining (equity ${acct.equity:,.0f})")

        # Skip hard-to-borrow shorts cached from previous failures this session
        if order_type == OrderType.SHORT and signal.symbol in self._htb_cache:
            return False, f"{signal.symbol} hard-to-borrow (cached)"

        # Asset tradability check: skip halted or suspended symbols
        try:
            asset = self.client.get_asset(signal.symbol)
            raw_status = getattr(asset, "status", "active")
            status = str(getattr(raw_status, "value", raw_status)).lower()
            if status != "active":
                return False, f"{signal.symbol} not tradable: asset status={raw_status}"
            if not getattr(asset, "tradable", True):
                return False, f"{signal.symbol} not tradable: asset.tradable=False"
        except Exception as e:
            log.warning(f"{signal.symbol}: asset status check failed ({e}) — proceeding cautiously")

        # Pending order guard: don't submit a second order if one is already live/filling
        if signal.symbol in self.order_cache:
            cached_id = self.order_cache[signal.symbol]
            try:
                cached_order = self.client.get_order_by_id(cached_id)
                active_statuses = {"new", "partially_filled", "pending_new", "accepted", "held"}
                if str(getattr(cached_order, "status", "")).lower() in active_statuses:
                    return False, f"Pending order already active for {signal.symbol} (id={cached_id})"
                else:
                    # Order is filled/cancelled — remove stale cache entry
                    del self.order_cache[signal.symbol]
            except Exception:
                # Can't verify — keep cache entry intact to avoid double-submit risk
                return False, f"Could not verify order status for {signal.symbol} (id={cached_id}) — skipping to be safe"

        positions = self._get_positions()

        # Dynamic max positions: use equity-based strategic capacity (not raw buying_power).
        # buying_power can be artificially depressed by leveraged/inverse ETF margin requirements,
        # causing the bot to permanently block new entries even when capital is available.
        # We compute effective_max from equity × position_size_pct, then separately gate each
        # execution on whether buying_power is sufficient for one position.
        _pos_size_pct = (
            SMALL_ACCOUNT_POSITION_SIZE_PCT
            if acct.equity < SMALL_ACCOUNT_EQUITY_THRESHOLD
            else POSITION_SIZE_PCT
        )
        _pos_size_dollars = max(MIN_POSITION_DOLLARS, acct.equity * _pos_size_pct / 100.0)
        # Strategic max: how many positions our equity allocation strategy supports
        equity_capacity = max(1, int(acct.equity * 0.95 / _pos_size_dollars))
        effective_max = min(MAX_POSITIONS, equity_capacity)
        log.debug(
            f"[DBG] effective_max={effective_max} equity={acct.equity:.0f} bp={acct.buying_power:.0f} "
            f"pos_size=${_pos_size_dollars:.0f} ({_pos_size_pct:.0f}%) equity_cap={equity_capacity}"
        )

        # ── Buying power gate (must come first) ───────────────────────────
        # Check if sufficient buying power for this trade (primary constraint).
        # This allows entry even when at max positions if capital is available.
        margin = 2.0 if order_type == OrderType.SHORT else 1.0
        min_usable = (SMALL_ACCOUNT_MIN_POSITION_DOLLARS 
                      if acct.equity < SMALL_ACCOUNT_EQUITY_THRESHOLD 
                      else MIN_POSITION_DOLLARS)
        min_bp_needed = min_usable * margin
        
        if acct.buying_power < min_bp_needed:
            return False, (
                f"Insufficient buying power: ${acct.buying_power:,.0f} "
                f"(need ${min_bp_needed:,.0f} for minimum position)"
            )
        
        # ── Max positions gate (secondary; optional swap if at limit) ─────
        if positions.total_count >= effective_max:
            if not (SWAP_ON_FULL and signal.confidence >= SWAP_MIN_CONFIDENCE):
                # At max but BP available — allow entry (no swap needed)
                log.debug(
                    f"At max positions {positions.total_count}/{effective_max} but allowing entry "
                    f"due to available BP ${acct.buying_power:,.0f}"
                )
            else:
                # Strong confidence signal + at max: prefer swap to maintain position count
                label = "SWAP (bear)" if swap_only else "SWAP"
                weakest = self._find_weakest_position()
                if weakest:
                    log.info(
                        f"{label}: closing {weakest} (weakest) to make room for "
                        f"{signal.symbol} (conf={signal.confidence:.0%})"
                    )
                    try:
                        self.client.close_position(weakest)
                        self._swap_cycle_closed.add(weakest)
                        # Closing a prior-day position is NOT a day trade — do not count against PDT
                        positions = self._get_positions(force_refresh=True)
                    except Exception as e:
                        err_str = str(e)
                        if "40310100" in err_str:
                            # Alpaca PDT protection: position was entered today — can't close same day.
                            # Mark as today's entry so it's never selected as swap candidate again.
                            self._entry_log[weakest] = {
                                "strategy": "restored",
                                "date": datetime.date.today(),
                                "confidence": 0.0,
                            }
                            log.warning(
                                f"SWAP skip {weakest}: PDT same-day protection (40310100) — "
                                f"marked as today entry, will not retry this session"
                            )
                            # Don't block the new signal — allow entry without the swap
                        else:
                            log.warning(f"SWAP close failed for {weakest}: {e}")
                            return False, f"Swap close failed: {e}"
                else:
                    # No position to swap, but BP available — allow entry anyway
                    log.debug(
                        f"No swappable position found, but allowing entry due to available BP ${acct.buying_power:,.0f}"
                    )

        if positions.has_position(signal.symbol):
            if order_type == OrderType.LONG  and positions.is_long(signal.symbol):
                return False, f"Already long {signal.symbol}"
            if order_type == OrderType.SHORT and positions.is_short(signal.symbol):
                return False, f"Already short {signal.symbol}"

        return True, None

    # -- Buying Power Sizing -----------------------------------------------
    def _size_with_buying_power(
        self, buying_power: float, signal: Signal,
        risk_info: Dict, order_type: OrderType
    ) -> Tuple[int, Optional[str]]:
        """Returns (shares, skip_reason). Downsizes if BP constrained, skips if below min."""
        margin  = 2.0 if order_type == OrderType.SHORT else 1.0
        usable  = buying_power * (1.0 - MIN_BUYING_POWER_PCT / 100.0)
        desired = int(risk_info["dollar_amount"] / signal.price)
        max_bp  = int(usable / (signal.price * margin))
        shares  = min(desired, max_bp)

        account_snapshot = self._account_cache or self._get_account()  # use cached if available
        min_position = SMALL_ACCOUNT_MIN_POSITION_DOLLARS if account_snapshot.equity < SMALL_ACCOUNT_EQUITY_THRESHOLD else MIN_POSITION_DOLLARS

        if shares < 1:
            return 0, (
                f"Insufficient BP: ${buying_power:,.0f} usable ${usable:,.0f} "
                f"for {signal.symbol} @ ${signal.price:.2f} (x{margin:.0f} margin)"
            )

        cost = shares * signal.price

        # Debug trace for min position handling.
        log.debug(
            f"size check {signal.symbol}: equity={account_snapshot.equity:.2f}, "
            f"min_position=${min_position:.2f}, shares={shares}, cost=${cost:.2f}, desired={desired}, max_bp={max_bp}, usable=${usable:.2f}"
        )

        if cost < min_position:
            return 0, f"{signal.symbol} too small after downsize: ${cost:.0f} < min ${min_position:.0f}"

        if shares < desired:
            log.info(
                f"  BP downsize {signal.symbol}: {desired} -> {shares} shares "
                f"(BP ${buying_power:,.0f}, usable ${usable:,.0f}, cost ${cost:,.0f})"
            )
        return shares, None

    # ── Bracket Prices ──────────────────────────────────────────────────────────
    def _calculate_bracket_prices(self, signal: Signal, risk_info: Dict, order_type: OrderType) -> tuple:
        if signal.atr_stop and signal.atr_stop > 0:
            # ATR-based 2:1 R:R — stop at 1.5×ATR, target at 2× the risk
            risk_dist = signal.atr_stop
            if order_type == OrderType.LONG:
                sl = round(signal.price - risk_dist, 2)
                tp = round(signal.price + ATR_TP_RATIO * risk_dist, 2)
            else:
                sl = round(signal.price + risk_dist, 2)
                tp = round(signal.price - ATR_TP_RATIO * risk_dist, 2)
        else:
            # Percentage-based fallback
            if order_type == OrderType.LONG:
                sl = round(signal.price * (1 - risk_info["stop_loss_pct"] / 100), 2)
                tp = round(signal.price * (1 + risk_info["tp"]            / 100), 2)
            else:
                sl = round(signal.price * (1 + risk_info["stop_loss_pct"] / 100), 2)
                tp = round(signal.price * (1 - risk_info["tp"]            / 100), 2)
        return sl, tp

    # ── Entry + Trailing Stop Order ──────────────────────────────────────────
    def _create_bracket_order(self, signal: Signal, shares: int, risk_info: Dict, order_type: OrderType) -> bool:
        """Submit market entry then a GTC trailing stop at risk_info['stop_loss_pct']%.
        TP bracket leg is intentionally dropped — the trailing stop locks in gains
        automatically; swap logic and EOD close handle opportunity exits."""
        side      = OrderSide.BUY  if order_type == OrderType.LONG else OrderSide.SELL
        stop_side = OrderSide.SELL if order_type == OrderType.LONG else OrderSide.BUY
        trail_pct = risk_info["stop_loss_pct"]  # tiered: NORMAL=3%, MEDIUM=4%, HIGH=5%, EXTREME=7%

        # ── Step 1: Entry order (failure aborts the whole bracket) ──────────
        try:
            entry_req = MarketOrderRequest(
                symbol          = signal.symbol,
                qty             = shares,
                side            = side,
                time_in_force   = TimeInForce.DAY,
                client_order_id = f"apex-{signal.strategy}-{signal.symbol}-{int(time.time())}",
            )
            order = self.client.submit_order(entry_req)
            self.order_cache[signal.symbol] = order.id

            # Store ATR-based TP target — checked each scan cycle by check_tp_targets()
            if signal.atr_stop and signal.atr_stop > 0:
                _sl, _tp = self._calculate_bracket_prices(signal, risk_info, order_type)
                self._tp_targets[signal.symbol] = _tp
                log.info(f"TP target set {signal.symbol}: ${_tp:.2f} (ATR R:R {ATR_TP_RATIO}:1)")

        except Exception as e:
            err = str(e).lower()
            if order_type == OrderType.SHORT and ("cannot be sold short" in err or "40310000" in err or "account is not allowed to short" in err):
                # Symbol-level HTB: block only this ticker for the session
                self._htb_cache.add(signal.symbol)
                if "account is not allowed to short" in err:
                    # Account-level: no short permission at all — disable all shorts
                    self.shorting_blocked = True
                    log.warning(
                        f"Short entry blocked for {signal.symbol} (account permission). "
                        "Disabling shorts for this session."
                    )
                else:
                    log.warning(f"Short blocked {signal.symbol} (HTB/insufficient BP): {e}")
            elif order_type != OrderType.SHORT and ("cannot be sold short" in err or "40310000" in err):
                # Inverse ETF or other buy rejected by broker — do not poison short flag
                log.warning(f"Buy rejected for {signal.symbol} (broker): {e}")
            elif "insufficient buying power" in err:
                log.warning(f"Bracket skip {signal.symbol}: insufficient buying power")
            else:
                log.error(f"Bracket order failed {signal.symbol}: {e}")
            return False

        # ── Step 2: Trailing stop — best-effort; entry already filled ────────
        # On live accounts, skip the same-day trailing stop — PDT rules block GTC
        # SELL legs on shares entered today.  protect_positions() re-places it next
        # session when the position is no longer same-day restricted.
        # Inverse ETFs (SOXS, DUST, UVXY …) may also reject a GTC trailing stop
        # with 40310000.  This must NOT cancel the entry or disable shorting.
        if LIVE:
            log.info(
                f"Trailing stop deferred {signal.symbol} (live same-day entry) — "
                "protect_positions() will place it next session"
            )
        else:
            try:
                ts_req = TrailingStopOrderRequest(
                    symbol        = signal.symbol,
                    qty           = shares,
                    side          = stop_side,
                    type          = AlpacaOrderType.TRAILING_STOP,
                    time_in_force = TimeInForce.GTC,
                    trail_percent = trail_pct,
                )
                self.client.submit_order(ts_req)
            except Exception as e:
                log.warning(
                    f"Trailing stop skipped {signal.symbol} (entry filled): {e} — "
                    "protect_positions() will re-place next cycle"
                )

        self._log_bracket(signal, shares, risk_info, trail_pct, None, order_type)
        return True

    def _log_bracket(self, signal, shares, risk_info, trail_pct, _tp_unused, order_type):
        action    = "BUY"  if order_type == OrderType.LONG else "SHORT"
        tier      = risk_info["tier"]
        atr_pct   = risk_info.get("atr_pct", 0)
        alloc_pct = risk_info["allocation_pct"]

        if USE_DYNAMIC_TIERS and atr_pct > 0 and USE_RISK_EQUALIZED_SIZING:
            log.info(f"{action} {signal.symbol}: {shares} @ ${signal.price:.2f} "
                     f"({alloc_pct:.1f}% pos) | TRAILING SL {trail_pct:.1f}% "
                     f"| Tier: {tier} (ATR {atr_pct:.1f}%) | {signal.strategy}")
        else:
            log.info(f"{action} {signal.symbol}: {shares} @ ${signal.price:.2f} "
                     f"| TRAILING SL {trail_pct:.1f}% | Tier: {tier} | {signal.strategy}")

    # ΓöÇΓöÇ Simple Order ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
    def _create_simple_order(self, signal: Signal, shares: int, order_type: OrderType) -> bool:
        side   = OrderSide.BUY if order_type == OrderType.LONG else OrderSide.SELL
        action = "BUY"         if order_type == OrderType.LONG else "SHORT"

        try:
            coid = f"apex-{signal.strategy}-{signal.symbol}-{int(time.time())}"
            if EXTENDED_HOURS and not self._current_market_state().is_regular_hours:
                adj   = 1.002 if order_type == OrderType.LONG else 0.998
                limit = round(signal.price * adj, 2)
                req   = LimitOrderRequest(
                    symbol          = signal.symbol,
                    qty             = shares,
                    side            = side,
                    time_in_force   = TimeInForce.DAY,
                    limit_price     = limit,
                    extended_hours  = True,
                    client_order_id = coid,
                )
                order = self.client.submit_order(req)
                self.order_cache[signal.symbol] = order.id
                log.info(f"{action} LIMIT {signal.symbol}: {shares} @ ${limit:.2f} (ext-hours) | {signal.strategy}")
                return True
            else:
                req = MarketOrderRequest(
                    symbol          = signal.symbol,
                    qty             = shares,
                    side            = side,
                    time_in_force   = TimeInForce.DAY,
                    client_order_id = coid,
                )
                order = self.client.submit_order(req)
                self.order_cache[signal.symbol] = order.id
                log.info(f"{action} {signal.symbol}: {shares} @ ${signal.price:.2f} | {signal.strategy}")
                return True

        except Exception as e:
            err = str(e).lower()
            if order_type == OrderType.SHORT and ("cannot be sold short" in err or "40310000" in err or "account is not allowed to short" in err):
                # Symbol-level HTB: block only this ticker for the session
                self._htb_cache.add(signal.symbol)
                if "account is not allowed to short" in err:
                    # Account-level: no short permission at all — disable all shorts
                    self.shorting_blocked = True
                    log.warning(
                        f"Short entry blocked for {signal.symbol} (account permission). "
                        "Disabling shorts for this session."
                    )
                else:
                    log.warning(f"Short blocked {signal.symbol} (HTB/insufficient BP): {e}")
            elif order_type != OrderType.SHORT and ("cannot be sold short" in err or "40310000" in err):
                # Inverse ETF or other buy rejected by broker — do not poison short flag
                log.warning(f"Buy rejected for {signal.symbol} (broker): {e}")
            elif "insufficient buying power" in err:
                log.warning(f"Skip {signal.symbol}: insufficient buying power")
            else:
                log.error(f"{action} order error {signal.symbol}: {e}")
            return False

    # -- Entry (unified) ---------------------------------------------------
    def _execute_entry(self, signal: Signal, acct: AccountSnapshot, order_type: OrderType, swap_only: bool = False) -> bool:
        valid, reason = self._validate_trade(signal, acct, order_type, swap_only=swap_only)
        if not valid:
            if reason:
                log.info(f"Skip {signal.symbol}: {reason}")
            return False

        risk_info = calculate_risk_adjusted_size(acct.equity, signal.symbol, signal.price)

        # Scale dollar_amount by confidence: 0.50× at floor (MIN_SIGNAL_CONFIDENCE) → 1.0× at 0.85+
        from engine.config import MIN_SIGNAL_CONFIDENCE
        _conf_floor = MIN_SIGNAL_CONFIDENCE
        _conf_mult = CONF_SCALE_MIN_MULT + (1.0 - CONF_SCALE_MIN_MULT) * min(
            1.0, max(0.0, (signal.confidence - _conf_floor) / (CONF_SCALE_FULL_CONF - _conf_floor))
        )
        risk_info = dict(risk_info, dollar_amount=round(risk_info["dollar_amount"] * _conf_mult, 2))
        log.debug(
            f"[SIZE] {signal.symbol} conf={signal.confidence:.0%} → "
            f"scale={_conf_mult:.2f}× → ${risk_info['dollar_amount']:,.0f}"
        )

        shares, skip_reason = self._size_with_buying_power(acct.buying_power, signal, risk_info, order_type)
        if shares < 1:
            # Confidence-swap: if a held position has lower entry confidence, rotate into the new signal.
            # Skip entirely when PDT = 0 — closing a same-day position would itself be a day trade.
            _dt_left_swap = self.pdt.remaining(acct.equity, acct.daytrade_count)
            if order_type == OrderType.LONG and _dt_left_swap > 0:
                victim, victim_conf = self._find_least_confident_position(signal.confidence)
                if victim:
                    log.info(
                        f"CONF-SWAP: closing {victim} (conf={victim_conf:.0%}) "
                        f"to make room for {signal.symbol} (conf={signal.confidence:.0%})"
                    )
                    try:
                        self.client.close_position(victim)
                        self._swap_cycle_closed.add(victim)
                        # Do not count the close as a day trade (exits are always allowed)
                        acct = self._get_account(force_refresh=True)
                        shares, skip_reason = self._size_with_buying_power(acct.buying_power, signal, risk_info, order_type)
                    except Exception as e:
                        log.warning(f"Conf-swap close failed for {victim}: {e}")
            if shares < 1:
                log.info(f"Skip {signal.symbol}: {skip_reason}")
                return False

        # Short-float position cap: never exceed 20% of equity in a single squeeze ticker
        if is_high_short_float(signal.symbol):
            cap_shares = max(0, int(acct.equity * (MAX_SHORT_FLOAT_PCT / 100) / signal.price))
            if shares > cap_shares:
                log.info(
                    f"Short-float cap {signal.symbol}: {shares}→{cap_shares} shares "
                    f"({MAX_SHORT_FLOAT_PCT:.0f}% equity max, equity ${acct.equity:,.0f})"
                )
                shares = cap_shares
            if shares < 1:
                log.info(f"Skip {signal.symbol}: too small after short-float cap")
                return False

        if order_type == OrderType.SHORT and LONG_ONLY_MODE:
            log.info(f"Skipping {signal.symbol} SHORT because LONG_ONLY_MODE is active")
            return False

        if self.use_bracket_orders and self._current_market_state().is_regular_hours:
            if self._create_bracket_order(signal, shares, risk_info, order_type):
                self.pdt.add(datetime.date.today())
                self._entry_log[signal.symbol] = {"strategy": signal.strategy, "date": datetime.date.today(), "confidence": signal.confidence}
                self._swap_cycle_closed.add(signal.symbol)  # protect from same-cycle swap-out
                self._get_positions(force_refresh=True)
                self._get_account(force_refresh=True)
                return True

        if self._create_simple_order(signal, shares, order_type):
            self.pdt.add(datetime.date.today())
            self._entry_log[signal.symbol] = {"strategy": signal.strategy, "date": datetime.date.today(), "confidence": signal.confidence}
            self._swap_cycle_closed.add(signal.symbol)  # protect from same-cycle swap-out
            self._get_positions(force_refresh=True)
            self._get_account(force_refresh=True)
            return True

        return False

    # -- Public: Execute ---------------------------------------------------
    def execute(self, signal: Signal, swap_only: bool = False) -> bool:
        try:
            acct      = self._get_account()
            positions = self._get_positions()

            if signal.action == "buy":
                if positions.has_position(signal.symbol) and positions.is_short(signal.symbol):
                    return self._close_short_position(signal, acct.equity)
                return self._execute_entry(signal, acct, OrderType.LONG, swap_only=swap_only)

            elif signal.action in ("sell", "short"):
                if LONG_ONLY_MODE:
                    log.info(
                        f"Skipping {signal.symbol} {signal.action.upper()} because LONG_ONLY_MODE is enabled"
                    )
                    return False
                if self.shorting_blocked:
                    log.info(
                        f"Skipping {signal.symbol} {signal.action.upper()} because shorting is blocked for this account/session"
                    )
                    return False

                if positions.has_position(signal.symbol) and positions.is_long(signal.symbol):
                    return self._close_long_position(signal, acct.equity)
                return self._execute_entry(signal, acct, OrderType.SHORT, swap_only=swap_only)

        except Exception as e:
            log.error(f"Execute error {signal.symbol}: {e}")
        return False

    # ΓöÇΓöÇ Close Short ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
    def _close_short_position(self, signal: Signal, equity: float) -> bool:
        positions = self._get_positions()
        if not positions.has_position(signal.symbol):
            log.info(f"No short position in {signal.symbol}")
            return False
        try:
            qty = abs(int(positions.positions_dict[signal.symbol].qty))
            if EXTENDED_HOURS and not self._current_market_state().is_regular_hours:
                req = LimitOrderRequest(
                    symbol=signal.symbol, qty=qty, side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(signal.price * 1.002, 2), extended_hours=True,
                )
            else:
                req = MarketOrderRequest(
                    symbol=signal.symbol, qty=qty, side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
            self.client.submit_order(req)
            # Closing a short that was opened today is a day trade round-trip
            self.pdt.add(datetime.date.today())
            log.info(f"COVER {signal.symbol}: {qty} @ ${signal.price:.2f} | {signal.strategy}")
            return True
        except Exception as e:
            log.error(f"Cover error {signal.symbol}: {e}")
            return False

    # ΓöÇΓöÇ Close Long ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
    def _close_long_position(self, signal: Signal, equity: float) -> bool:
        positions = self._get_positions()
        if not positions.has_position(signal.symbol):
            log.info(f"No position in {signal.symbol}")
            return False
        # Closes are ALWAYS allowed regardless of PDT — never block an exit

        qty = abs(int(float(positions.positions_dict[signal.symbol].qty)))
        try:
            req = MarketOrderRequest(
                symbol=signal.symbol, qty=qty,
                side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
            )
            self.client.submit_order(req)
            # NOTE: closing an existing position is NOT a new day trade.
            # Alpaca counts the round-trip (open+close same day) as one trade;
            # pdt.add() is intentionally omitted here — it was already counted at entry.
            self._get_positions(force_refresh=True)
            log.info(f"SELL {signal.symbol}: {qty} shares | {signal.strategy}")
            return True
        except Exception as e:
            log.error(f"Sell error {signal.symbol}: {e}")
            return False

    # ─── Protect Open Positions ──────────────────────────────────────────────
    def protect_positions(self) -> None:
        """
        For every open position whose shares are fully free (qty_available > 0
        AND no existing sell/buy-to-cover order on that symbol), place a GTC
        trailing stop.  Skips any position already covered by an active order.

        Covers today's entries too — if the bracket-order step-2 trailing stop
        was rejected by the broker (common for inverse ETFs), this re-places it
        so the position is never left naked intraday.  A GTC trailing stop that
        fills same-day will count as a day trade; the PDT violation alert in
        _validate_trade fires if the count exceeds PDT_MAX_TRADES.
        """
        positions = []
        covered = set()

        # Resist transient connection drops by retrying fetch operations.
        for attempt in range(1, 4):
            try:
                positions = self.client.get_all_positions()
                open_orders = self.client.get_orders()
                covered = {o.symbol for o in open_orders}
                break
            except Exception as e:
                log.warning(
                    f"protect_positions: data fetch attempt {attempt}/3 failed: {e}"
                )
                if attempt < 3:
                    time.sleep(2)
                else:
                    log.error("protect_positions: all fetch retries failed; skipping this cycle")
                    return

        for pos in positions:
            sym = pos.symbol

            # Skip options legs — OCC symbols (e.g. AEHR260515C00080000) are managed
            # by OptionsExecutor.monitor_positions(); trailing stops are invalid for options
            # (Alpaca error 42210000).  OCC symbols always match <ticker><YYMMDD><C|P><8digits>.
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue

            # Primary guard: don't add orders if symbol already has any active order
            if sym in covered:
                continue

            # Skip positions confirmed as forced overnight holds (PDT blocks close too)
            if sym in self._pdt_overnight_forced:
                continue

            # Secondary guard: skip if broker reports zero available qty
            try:
                qty_available = int(float(pos.qty_available))
            except (AttributeError, TypeError, ValueError):
                qty_available = 0
            if qty_available <= 0:
                continue

            try:
                qty         = int(float(pos.qty))
                avail       = abs(qty_available)
                current     = float(pos.current_price)
                is_long_pos = qty > 0

                tier_info  = get_dynamic_tier(sym, current)
                trail_pct  = tier_info["ts"]
                tier_label = tier_info["tier"]

                stop_side = OrderSide.SELL if is_long_pos else OrderSide.BUY
                self.client.submit_order(TrailingStopOrderRequest(
                    symbol        = sym,
                    qty           = avail,
                    side          = stop_side,
                    type          = AlpacaOrderType.TRAILING_STOP,
                    time_in_force = TimeInForce.GTC,
                    trail_percent = trail_pct,
                ))
                direction = "LONG" if is_long_pos else "SHORT"
                log.info(f"PROTECT {direction} {sym} [{tier_label}]: trailing stop {trail_pct:.1f}% GTC")
            except Exception as e:
                err_str = str(e)
                if "40310100" in err_str:
                    # Broker PDT protection rejects the stop for today's entry.
                    # Fall back to software stop monitoring via check_software_stops().
                    if sym not in self._pdt_stop_blocked:
                        try:
                            entry_price = float(pos.avg_entry_price or pos.current_price)
                            tier_info   = get_dynamic_tier(sym, float(pos.current_price))
                            stop_pct    = tier_info["ts"]
                            stop_price  = round(
                                entry_price * (1 - stop_pct / 100) if qty > 0
                                else entry_price * (1 + stop_pct / 100),
                                2,
                            )
                            self._pdt_stop_blocked[sym] = stop_price
                            log.warning(
                                f"protect_positions {sym}: broker PDT stop rejected — "
                                f"software SL set at ${stop_price:.2f} ({stop_pct:.1f}% from ${entry_price:.2f})"
                            )
                        except Exception:
                            log.warning(f"protect_positions {sym}: PDT stop rejected (software SL unavailable)")
                    else:
                        log.debug(f"protect_positions {sym}: PDT stop still rejected (software SL active @ ${self._pdt_stop_blocked[sym]:.2f})")
                else:
                    log.error(f"protect_positions {sym}: {e}")

    def check_software_stops(self) -> None:
        """Market-close any position whose broker-rejected PDT stop has been breached.
        Called every scan cycle for positions in _pdt_stop_blocked."""
        if not self._pdt_stop_blocked:
            return
        try:
            positions = {p.symbol: p for p in self.client.get_all_positions()}
        except Exception as e:
            log.warning(f"check_software_stops: fetch failed: {e}")
            return
        for sym, stop_price in list(self._pdt_stop_blocked.items()):
            pos = positions.get(sym)
            if pos is None:
                # Position already closed (stop filled or manual)
                self._pdt_stop_blocked.pop(sym, None)
                continue
            try:
                current = float(pos.current_price)
                qty     = int(float(pos.qty))
                is_long = qty > 0
                hit     = (is_long and current <= stop_price) or (not is_long and current >= stop_price)
                if hit:
                    side = OrderSide.SELL if is_long else OrderSide.BUY
                    try:
                        self.client.submit_order(MarketOrderRequest(
                            symbol        = sym,
                            qty           = abs(qty),
                            side          = side,
                            time_in_force = TimeInForce.DAY,
                        ))
                        self._pdt_stop_blocked.pop(sym, None)
                        log.warning(
                            f"SOFTWARE SL HIT {sym}: price ${current:.2f} crossed stop ${stop_price:.2f} — "
                            f"market {'SELL' if is_long else 'BUY-TO-COVER'} submitted"
                        )
                    except Exception as close_err:
                        if "40310100" in str(close_err):
                            # Broker PDT also blocks same-day close — position is a forced
                            # overnight hold.  Stop retrying; it will carry to next session.
                            self._pdt_stop_blocked.pop(sym, None)
                            self._pdt_overnight_forced.add(sym)
                            log.warning(
                                f"SOFTWARE SL {sym}: stop breached at ${current:.2f} but PDT blocks "
                                f"same-day close — holding overnight (stop was ${stop_price:.2f})"
                            )
                        else:
                            log.error(f"check_software_stops {sym}: {close_err}")
                else:
                    log.debug(f"SOFTWARE SL {sym}: current ${current:.2f} | stop ${stop_price:.2f} | margin ${current - stop_price:+.2f}")
            except Exception as e:
                log.error(f"check_software_stops {sym}: {e}")

    # ── EOD Close ─────────────────────────────────────────────────────────────
    def close_eod_positions(self) -> Optional[dict]:
        """Close all intraday-strategy positions at EOD_CLOSE_TIME.
        Targets FloatRotation, GapBreakout, ORB, VWAPReclaim opened today."""
        if not EOD_CLOSE_ENABLED:
            return None

        import pytz
        now_et = datetime.datetime.now(pytz.timezone("America/New_York"))
        close_h, close_m = map(int, EOD_CLOSE_TIME.split(":"))
        if now_et.hour < close_h or (now_et.hour == close_h and now_et.minute < close_m):
            return None  # Not yet EOD close time
        if now_et.hour >= 16:
            return None  # Market already closed

        today = datetime.date.today()
        if getattr(self, "_eod_close_done", None) == today:
            return None  # EOD close already processed for today

        try:
            positions = self.client.get_all_positions()
        except Exception as e:
            log.error(f"close_eod_positions: fetch failed: {e}")
            return None

        closed_items = []
        failed_items = []

        for pos in positions:
            sym = pos.symbol
            qty = int(float(pos.qty))
            if qty == 0:
                continue

            entry_info = self._entry_log.get(sym)
            if not entry_info:
                continue
            if entry_info.get("date") != today:
                continue
            if entry_info.get("strategy") not in EOD_CLOSE_STRATEGIES:
                continue

            try:
                # Cancel only DAY-TIF orders holding shares for this symbol before
                # submitting the market close ("insufficient qty available" error).
                # GTC trailing stops are intentionally preserved — they protect the
                # position until the close fill settles and should not be cancelled.
                try:
                    sym_orders = [
                        o for o in (self.client.get_orders() or [])
                        if o.symbol == sym
                        and str(getattr(o, "time_in_force", "")).upper() != "GTC"
                    ]
                    for _o in sym_orders:
                        try:
                            self.client.cancel_order_by_id(str(_o.id))
                        except Exception:
                            pass
                    if sym_orders:
                        time.sleep(0.4)
                except Exception:
                    pass

                side = OrderSide.SELL if qty > 0 else OrderSide.BUY
                req = MarketOrderRequest(
                    symbol=sym, qty=abs(qty),
                    side=side, time_in_force=TimeInForce.DAY,
                )
                self.client.submit_order(req)
                self._entry_log.pop(sym, None)

                pnl = float(pos.unrealized_pl)
                closed_items.append({
                    "symbol": sym,
                    "qty": abs(qty),
                    "strategy": entry_info.get("strategy", "unknown"),
                    "pnl": pnl,
                })

                log.info(
                    f"EOD CLOSE {sym}: {abs(qty)} shares | "
                    f"strategy={entry_info['strategy']} | P&L ${pnl:.2f}"
                )
            except Exception as e:
                failed_items.append({"symbol": sym, "error": str(e)})
                log.error(f"EOD close failed {sym}: {e}")

        self._eod_close_done = today

        summary = {
            "date": today.isoformat(),
            "closed_count": len(closed_items),
            "failed_count": len(failed_items),
            "closed_items": closed_items,
            "failed_items": failed_items,
            "asof": now_et.isoformat(),
        }
        return summary

    # ── Kill Mode: Emergency Close All ───────────────────────────────────────
    def emergency_close_all(self, equity: float) -> None:
        """
        Kill mode emergency exit. Closes every open position as safely as possible.

        PDT rules (equity < $25k):
          - Positions opened on a PRIOR day → cancel any open orders then market-close.
            These are NOT day trades so no PDT count is consumed.
          - Positions opened TODAY → cannot close without a day-trade violation.
            Instead, a hairpin trailing stop of KILL_MODE_TRAIL_PCT (0.5%) is placed
            so the position exits automatically within minutes via the stop engine.

        PDT-exempt (equity >= $25k): cancel all open orders + market-close everything.
        """
        import time as _t

        pdt_exempt = equity >= PDT_ACCOUNT_MIN
        today      = datetime.date.today()

        try:
            positions   = self.client.get_all_positions()
            open_orders = self.client.get_orders()
        except Exception as e:
            log.error(f"KILL MODE: failed to fetch data: {e}")
            return

        orders_by_sym: dict = {}
        for o in open_orders:
            orders_by_sym.setdefault(o.symbol, []).append(o)

        closed: list    = []
        protected: list = []

        for pos in positions:
            sym = pos.symbol
            qty = int(float(pos.qty))
            if qty == 0:
                continue

            entry_date = self._entry_log.get(sym, {}).get("date")
            is_today   = entry_date == today

            if not pdt_exempt and is_today:
                # Today's position — tighten trailing stop to hairpin; do NOT market-close
                for o in orders_by_sym.get(sym, []):
                    try:
                        self.client.cancel_order_by_id(str(o.id))
                    except Exception:
                        pass
                _t.sleep(0.3)
                try:
                    stop_side = OrderSide.SELL if qty > 0 else OrderSide.BUY
                    self.client.submit_order(TrailingStopOrderRequest(
                        symbol        = sym,
                        qty           = abs(qty),
                        side          = stop_side,
                        type          = AlpacaOrderType.TRAILING_STOP,
                        time_in_force = TimeInForce.GTC,
                        trail_percent = KILL_MODE_TRAIL_PCT,
                    ))
                    cur = float(pos.current_price or 0)
                    log.warning(
                        f"KILL MODE [PDT-SAFE] {sym}: hairpin trailing stop "
                        f"{KILL_MODE_TRAIL_PCT}% @ ${cur:.2f} "
                        f"(opened today — closing via stop to avoid PDT violation)"
                    )
                    protected.append(sym)
                except Exception as e:
                    log.error(f"KILL MODE: hairpin stop failed {sym}: {e}")
                continue

            # Prior-day position (or PDT-exempt): cancel standing orders, then market-close
            for o in orders_by_sym.get(sym, []):
                try:
                    self.client.cancel_order_by_id(str(o.id))
                except Exception:
                    pass
            _t.sleep(0.3)

            try:
                side = OrderSide.SELL if qty > 0 else OrderSide.BUY
                self.client.submit_order(MarketOrderRequest(
                    symbol        = sym,
                    qty           = abs(qty),
                    side          = side,
                    time_in_force = TimeInForce.DAY,
                ))
                pnl = float(pos.unrealized_pl or 0)
                log.warning(
                    f"KILL MODE CLOSE {sym}: {abs(qty)} shares "
                    f"{'SELL' if qty > 0 else 'BUY-TO-COVER'} | unrealized ${pnl:+.2f}"
                )
                closed.append(sym)
            except Exception as e:
                log.error(f"KILL MODE: close failed {sym}: {e}")

        log.warning(
            f"KILL MODE COMPLETE — "
            f"market-closed: {len(closed)} {closed} | "
            f"hairpin stops (PDT-safe): {len(protected)} {protected}"
        )

    # ── Stale Order Updater ───────────────────────────────────────────────────
    def update_stale_orders(self) -> None:
        """
        Find open orders older than STALE_ORDER_MINUTES and re-submit them:
          - Regular hours   → cancel + market order (instant fill)
          - Extended hours  → cancel + limit order at current price (IOC)
        Only applies to entry/exit orders (buy/sell), not bracket legs (stop/limit TP-SL).
        Also resets _swap_cycle_closed so each scan cycle starts fresh.
        """
        import time
        self._swap_cycle_closed.clear()  # reset per-cycle swap dedup
        try:
            open_orders = self.client.get_orders()
        except Exception as e:
            log.warning(f"update_stale_orders: fetch failed: {e}")
            return

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        regular = self._current_market_state().is_regular_hours

        for order in open_orders:
            # Only handle plain entry/exit orders, not bracket legs or protective stops
            order_type = getattr(order, "order_type", "") or ""
            order_class = str(getattr(order, "order_class", "") or "")
            if order_class in ("bracket", "oco"):
                continue
            # Never cancel GTC trailing stop orders — they are protective stops,
            # not stale entry orders.  Killing them leaves positions unprotected.
            if "trailing_stop" in str(order_type).lower():
                continue

            created_at = getattr(order, "created_at", None)
            if created_at is None:
                continue

            # Pick timeout: intraday strategies use short cutoff to avoid lunchtime fills
            coid = str(getattr(order, "client_order_id", "") or "")
            is_intraday = False
            if coid.startswith("apex-"):
                parts = coid.split("-", 2)   # ["apex", strategy, symbol]
                if len(parts) >= 2 and parts[1] in EOD_CLOSE_STRATEGIES:
                    is_intraday = True
            cutoff_secs = (STALE_ORDER_MINUTES_INTRADAY if is_intraday else STALE_ORDER_MINUTES) * 60

            age_secs = (now_utc - created_at).total_seconds()
            if age_secs < cutoff_secs:
                continue

            sym = order.symbol
            qty = int(float(order.qty))
            side = order.side  # OrderSide enum
            order_id = str(order.id)

            log.info(
                f"STALE ORDER: {sym} {side} {qty} — age {age_secs/60:.1f}m "
                f"(cutoff {'intraday 30m' if is_intraday else '6h'}) "
                f"→ {'market' if regular else 'limit @ current price'}"
            )

            try:
                self.client.cancel_order_by_id(order_id)
                time.sleep(0.3)

                if regular:
                    # If the original was a limit buy and the limit was more than 1%
                    # below the current ask, the order was defensive/passive — don't
                    # blast it to market (bad fill); just cancel and let the next
                    # scan cycle re-evaluate.
                    orig_limit = float(getattr(order, "limit_price", None) or 0)
                    if orig_limit > 0 and str(order_type).lower() == "limit":
                        try:
                            quote = self.client.get_latest_quote(sym)
                            cur_ask = float(getattr(quote, "ask_price", orig_limit))
                        except Exception:
                            cur_ask = orig_limit
                        if cur_ask > 0 and orig_limit < cur_ask * 0.99:
                            log.info(
                                f"STALE ORDER {sym}: limit ${orig_limit:.2f} is defensive "
                                f"(ask=${cur_ask:.2f}) — cancelling without re-entry"
                            )
                            continue  # skip re-submit; cancelled above

                    req = MarketOrderRequest(
                        symbol=sym, qty=qty, side=side,
                        time_in_force=TimeInForce.DAY,
                    )
                else:
                    # Best-effort limit at current price for extended hours
                    try:
                        bar = self.client.get_latest_quote(sym)
                        cur_price = round(
                            (float(bar.ask_price) + float(bar.bid_price)) / 2, 2
                        )
                    except Exception:
                        cur_price = float(getattr(order, "limit_price", None) or 0)
                    if cur_price <= 0:
                        log.warning(f"STALE ORDER {sym}: can't determine price, skipping")
                        continue
                    req = LimitOrderRequest(
                        symbol=sym, qty=qty, side=side,
                        limit_price=cur_price,
                        time_in_force=TimeInForce.DAY,
                        extended_hours=True,
                    )

                self.client.submit_order(req)
                log.info(f"STALE ORDER {sym}: replaced successfully")
            except Exception as e:
                log.warning(f"STALE ORDER {sym}: replace failed: {e}")

    # ── ATR Take-Profit Checker ────────────────────────────────────────────────
    def check_tp_targets(self) -> None:
        """Scan open positions against stored ATR-based TP targets.
        Submits a market close (sell/buy-to-cover) when current price reaches TP.
        Called once per scan cycle alongside update_stale_orders().
        """
        if not self._tp_targets:
            return
        try:
            positions = {p.symbol: p for p in self.client.get_all_positions()}
        except Exception as e:
            log.warning(f"check_tp_targets: fetch failed: {e}")
            return

        triggered = []
        for sym, tp_price in list(self._tp_targets.items()):
            pos = positions.get(sym)
            if pos is None:
                triggered.append(sym)  # position already closed, clean up
                continue
            qty = int(float(pos.qty))
            if qty == 0:
                triggered.append(sym)
                continue
            cur_price = float(getattr(pos, "current_price", 0) or 0)
            if cur_price <= 0:
                continue
            is_long = qty > 0
            hit = (is_long and cur_price >= tp_price) or (not is_long and cur_price <= tp_price)
            if hit:
                try:
                    side = OrderSide.SELL if is_long else OrderSide.BUY
                    req  = MarketOrderRequest(
                        symbol        = sym,
                        qty           = abs(qty),
                        side          = side,
                        time_in_force = TimeInForce.DAY,
                    )
                    self.client.submit_order(req)
                    log.info(
                        f"TP HIT {sym}: ${cur_price:.2f} {'>=  ' if is_long else '<= '}"
                        f"${tp_price:.2f} → market {'sell' if is_long else 'buy-to-cover'}"
                    )
                    triggered.append(sym)
                except Exception as e:
                    log.warning(f"TP close failed {sym}: {e}")

        for sym in triggered:
            self._tp_targets.pop(sym, None)

    # ── Health ─────────────────────────────────────────────────────────────────
    def get_health(self) -> Dict:
        try:
            acct = self._get_account(force_refresh=True)
            dt_left = self.pdt.remaining(acct.equity, acct.daytrade_count)
            return {
                "equity":           acct.equity,
                "cash":             acct.buying_power,
                "buying_power":     acct.buying_power,
                "pdt_protected":    acct.equity >= PDT_ACCOUNT_MIN,
                "day_trade_count":  acct.daytrade_count,
                "day_trades_left":  dt_left,
            }
        except Exception as e:
            log.error(f"Health check error: {e}")
            return {}

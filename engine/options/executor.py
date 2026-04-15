"""
ApexTrader - Options Executor (Level 3 Account / Alpaca)
Manages opening, monitoring, and closing options positions via the
Alpaca trading API.

Responsibilities:
  - Enforce 15% portfolio allocation cap across all open options
  - Size each trade (number of contracts) within allocation budget
  - Place buy_to_open (calls/puts) and sell_to_open (covered calls) orders
  - Monitor open options P&L, close at profit target (+50%) or stop (-40%)
  - Cancel expired or near-expiry contracts (DTE <= 1)
"""

import logging
import datetime
import time
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

from engine.config import (
    OPTIONS_ENABLED,
    OPTIONS_ALLOCATION_PCT,
    OPTIONS_MAX_POSITIONS,
    OPTIONS_PROFIT_TARGET_PCT,
    OPTIONS_STOP_LOSS_PCT,
    OPTIONS_DTE_MIN,
    PDT_ACCOUNT_MIN, PDT_MAX_TRADES, PDT_OPTIONS_DAY_TRADE_RESERVE,
    API_KEY, API_SECRET, PAPER,
)
from .strategies import OptionSignal, CONTRACT_SIZE, record_stop_cooldown

log = logging.getLogger("ApexTrader.Options")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _alpaca_option_symbol(symbol: str, expiry: datetime.date, option_type: str, strike: float) -> str:
    """Build the OCC option symbol used by Alpaca.
    Format: <underlying><YYMMDD><C|P><8-digit-strike-in-thousandths>
    e.g. AAPL260418C00185000
    """
    exp_str    = expiry.strftime("%y%m%d")
    cp         = "C" if option_type.lower() == "call" else "P"
    strike_int = int(round(strike * 1000))
    return f"{symbol}{exp_str}{cp}{strike_int:08d}"


@dataclass
class OptionsPosition:
    """Tracked open options position."""
    occ_symbol:  str
    symbol:      str
    option_type: str
    action:      str          # 'buy_to_open' or 'sell_to_open'
    strike:      float
    expiry:      datetime.date
    contracts:   int
    entry_price: float        # per-share premium paid/received (net debit for spreads)
    strategy:    str
    entered_at:  datetime.date = field(default_factory=datetime.date.today)
    peak_pnl_pct: float = 0.0   # highest observed P&L % (for trailing stop)
    # Debit spread: short leg fields (None for single-leg positions)
    short_occ_symbol:  Optional[str]   = None
    short_strike:      Optional[float] = None
    short_entry_price: Optional[float] = None   # credit received per share


class OptionsExecutor:
    """Manages options positions within a 15% portfolio allocation."""

    def __init__(self, client: TradingClient):
        self.client = client
        self._positions: Dict[str, OptionsPosition] = {}   # occ_symbol -> OptionsPosition
        self._last_monitor_ts: float = 0.0
        self._MONITOR_INTERVAL = 60   # seconds between P&L checks

    # ── Allocation / Budget ────────────────────────────────────────────────────

    def _get_options_budget(self) -> Tuple[float, float]:
        """Returns (total_options_budget $, remaining_budget $) based on current equity and Alpaca's options_buying_power.
        
        Uses the stricter of:
        - Configured allocation (15% of equity)
        - Alpaca's actual options_buying_power (accounts for margin, positions, etc.)
        """
        try:
            acct          = self.client.get_account()
            equity        = float(acct.equity)
            # Get Alpaca's actual options buying power (most important constraint)
            alpaca_options_bp = float(getattr(acct, "options_buying_power", 0.0))
            
            # Configured allocation as secondary constraint
            configured_budget = equity * (OPTIONS_ALLOCATION_PCT / 100.0)
            
            # Deduct current open option premium cost
            used = self._current_options_cost()
            
            # Use the stricter of the two
            total_budget = min(configured_budget, alpaca_options_bp)
            remaining = max(0.0, total_budget - used)
            
            # Debug: log when Alpaca constraint is binding
            if alpaca_options_bp < configured_budget:
                log.debug(
                    f"[OPTIONS] Alpaca OBP ${alpaca_options_bp:.2f} < configured ${configured_budget:.2f} "
                    f"(limiting budget to ${total_budget:.2f})"
                )
            
            return total_budget, remaining
        except Exception as e:
            log.warning(f"[OPTIONS] Could not fetch account budget: {e}", exc_info=True)
            return 0.0, 0.0

    def _current_options_cost(self) -> float:
        """Estimate total capital deployed in open options positions."""
        total = 0.0
        for pos in self._positions.values():
            if pos.action == "buy_to_open":
                total += pos.entry_price * CONTRACT_SIZE * pos.contracts
        return total

    def _count_open_options(self) -> int:
        return len(self._positions)

    # ── Position Sizing ────────────────────────────────────────────────────────

    def _calc_contracts(self, signal: OptionSignal, remaining_budget: float) -> int:
        """Calculate how many contracts to buy within the remaining budget.
        Each contract costs: mid_price × CONTRACT_SIZE dollars.
        We size to use ~33% of remaining budget (split across 3 max positions).
        """
        if signal.mid_price <= 0:
            return 0
        per_contract_cost = signal.mid_price * CONTRACT_SIZE
        # Use up to 1/3 of remaining budget per position
        position_budget = remaining_budget / max(1, OPTIONS_MAX_POSITIONS - self._count_open_options())
        contracts = int(position_budget // per_contract_cost)
        return max(0, min(contracts, 10))  # hard cap: never more than 10 contracts

    # ── Order Placement ────────────────────────────────────────────────────────

    def place_option_order(self, signal: OptionSignal) -> bool:
        """Place a limit order for the options signal.
        Returns True if order was submitted successfully.
        """
        if not OPTIONS_ENABLED:
            return False

        # ── Skip multi-leg strategies not yet supported ─────────────────────────
        # Butterflies are now supported as MLEG orders! Iron condors still need work.
        if "condor" in signal.strategy.lower():
            log.info(
                f"[OPTIONS] Skipping {signal.symbol} {signal.strategy} — iron condor multi-leg orders not yet supported"
            )
            return True

        # ── PDT & small-account guard ──────────────────────────────────────────
        try:
            acct        = self.client.get_account()
            equity      = float(acct.equity)
            dt_used     = int(acct.daytrade_count)
            pdt_flagged = str(getattr(acct, "pattern_day_trader", False)).lower() in ("1", "true", "yes")
        except Exception as e:
            log.warning(f"[OPTIONS] Could not check account for PDT: {e}", exc_info=True)
            return False

        is_small = equity < PDT_ACCOUNT_MIN
        if is_small:
            if pdt_flagged:
                dt_left = max(0, PDT_MAX_TRADES - dt_used)
                # Reserve at least PDT_OPTIONS_DAY_TRADE_RESERVE DTs for stock exits
                if dt_left <= PDT_OPTIONS_DAY_TRADE_RESERVE:
                    log.info(
                        f"[OPTIONS] Skipping {signal.symbol} — PDT day trades remaining={dt_left} "
                        f"(reserving {PDT_OPTIONS_DAY_TRADE_RESERVE} for stock exits, equity=${equity:,.0f})"
                    )
                    return True
            # Small account: cap to 1 open options position at a time
            if self._count_open_options() >= 1:
                log.info(
                    f"[OPTIONS] Small account (${equity:,.0f}) already has 1 open position — skipping {signal.symbol}"
                )
                return True
        else:
            if self._count_open_options() >= OPTIONS_MAX_POSITIONS:
                log.info(f"[OPTIONS] At max positions ({OPTIONS_MAX_POSITIONS}), skipping {signal.symbol}")
                return True

        _, remaining = self._get_options_budget()
        if remaining <= 0:
            log.info(f"[OPTIONS] No budget remaining (allocation exhausted), skipping {signal.symbol}")
            return False

        contracts = self._calc_contracts(signal, remaining)
        if contracts <= 0:
            log.info(f"[OPTIONS] {signal.symbol} — not enough budget for 1 contract (need ${signal.mid_price * CONTRACT_SIZE:.2f})")
            return False

        # ── Handle butterfly as MLEG order ────────────────────────────────────────
        is_butterfly = "butterfly" in signal.option_type.lower()
        if is_butterfly:
            if not all([signal.butterfly_low_strike, signal.butterfly_high_strike, 
                       signal.butterfly_low_mid, signal.butterfly_high_mid]):
                log.error(f"[OPTIONS] Butterfly {signal.symbol} missing leg data, skipping")
                return False
            
            try:
                # Extract call or put from option_type (e.g., "call_butterfly" → "call")
                cp_type = "call" if "call" in signal.option_type.lower() else "put"
                
                # Build 4-leg butterfly (buy low, sell 2 mid, buy high)
                legs = [
                    {
                        "symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.butterfly_low_strike),
                        "side": "buy",
                        "ratio_qty": 1
                    },
                    {
                        "symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.strike),
                        "side": "sell",
                        "ratio_qty": 2
                    },
                    {
                        "symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.butterfly_high_strike),
                        "side": "buy",
                        "ratio_qty": 1
                    }
                ]
                
                # net_debit (positive for butterfly debit; limit_price is what we're willing to pay)
                net_debit = signal.mid_price
                limit_price = round(net_debit * 1.02, 2)  # 2% buffer
                
                # Build MLEG payload
                mleg_payload = {
                    "order_class": "mleg",
                    "type": "limit",
                    "limit_price": str(limit_price),
                    "time_in_force": "day",
                    "legs": legs
                }
                
                # Submit as raw order (Alpaca SDK may need direct HTTP for MLEG)
                order = self.client.submit_order(mleg_payload)
                
                pdt_note = f" [PDT {dt_left}DT left]" if is_small else ""
                log.info(
                    f"[OPTIONS] MLEG BUTTERFLY: {signal.symbol} {signal.reason} | "
                    f"net_debit=${net_debit:.2f} limit=${limit_price:.2f} | conf={signal.confidence:.0%}{pdt_note}"
                )
                
                # Track as single position (composite of 3 legs)
                occ_sym = _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.strike)
                self._positions[occ_sym] = OptionsPosition(
                    occ_symbol=occ_sym,
                    symbol=signal.symbol,
                    option_type=signal.option_type,
                    action=signal.action,
                    strike=signal.strike,
                    expiry=signal.expiry,
                    contracts=contracts,
                    entry_price=signal.mid_price,
                    strategy=signal.strategy,
                    short_occ_symbol=None,  # For butterfly, all legs are tracked in strategy
                    short_strike=None,
                    short_entry_price=None,
                )
                return True
                
            except Exception as e:
                log.error(f"[OPTIONS] Butterfly MLEG order failed for {signal.symbol}: {e}")
                return False

        # ── Handle simple and 2-leg spreads ───────────────────────────────────────
        occ_sym = _alpaca_option_symbol(
            signal.symbol, signal.expiry, signal.option_type, signal.strike
        )

        # Check for duplicate
        if occ_sym in self._positions:
            log.info(f"[OPTIONS] Already have position in {occ_sym}, skipping")
            return False

        side = OrderSide.BUY if signal.action == "buy_to_open" else OrderSide.SELL

        # Use limit at mid_price (+ small buffer for fills)
        limit_price = round(signal.mid_price * (1.02 if side == OrderSide.BUY else 0.98), 2)

        # For debit spreads the mid_price is the net debit; reconstruct the long-leg limit
        is_spread = signal.spread_sell_strike is not None and signal.spread_sell_mid is not None
        if is_spread:
            long_mid    = signal.mid_price + (signal.spread_sell_mid or 0)
            limit_price = round(long_mid * 1.02, 2)

        try:
            order_req = LimitOrderRequest(
                symbol=occ_sym,
                qty=contracts,
                side=side,
                type="limit",
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )
            self.client.submit_order(order_req)
            pdt_note = f" [PDT {dt_left}DT left]" if is_small else ""
            log.info(
                f"[OPTIONS] ORDER: {signal.action.upper()} {contracts}x {occ_sym} "
                f"@ ${limit_price:.2f} | {signal.reason} | conf={signal.confidence:.0%}{pdt_note}"
            )

            short_occ = None
            short_entry = None
            if is_spread and signal.spread_sell_strike is not None:
                # Place the short leg (sell the OTM call)
                short_occ  = _alpaca_option_symbol(
                    signal.symbol, signal.expiry, "call", signal.spread_sell_strike
                )
                short_limit = round((signal.spread_sell_mid or 0) * 0.98, 2)
                try:
                    short_req = LimitOrderRequest(
                        symbol=short_occ,
                        qty=contracts,
                        side=OrderSide.SELL,
                        type="limit",
                        time_in_force=TimeInForce.DAY,
                        limit_price=short_limit,
                    )
                    self.client.submit_order(short_req)
                    short_entry = signal.spread_sell_mid
                    log.info(
                        f"OPTIONS SPREAD SHORT LEG: SELL {contracts}x {short_occ} "
                        f"@ ${short_limit:.2f} (credit leg)"
                    )
                except Exception as e:
                    log.warning(
                        f"Spread short-leg order failed for {short_occ}: {e} "
                        f"— long leg already placed, monitoring as naked call"
                    )
                    short_occ   = None
                    short_entry = None

            self._positions[occ_sym] = OptionsPosition(
                occ_symbol=occ_sym,
                symbol=signal.symbol,
                option_type=signal.option_type,
                action=signal.action,
                strike=signal.strike,
                expiry=signal.expiry,
                contracts=contracts,
                entry_price=signal.mid_price,
                strategy=signal.strategy,
                short_occ_symbol=short_occ,
                short_strike=signal.spread_sell_strike if is_spread else None,
                short_entry_price=short_entry,
            )
            return True

        except Exception as e:
            log.error(f"Options order failed for {occ_sym}: {e}")
            return False

    # ── Position Monitoring ────────────────────────────────────────────────────

    def monitor_positions(self) -> None:
        """Check open options positions and close at profit target or stop loss.
        Also closes positions with DTE <= 1 to avoid expiry risk.
        Run every MONITOR_INTERVAL seconds.
        """
        now = time.monotonic()
        if now - self._last_monitor_ts < self._MONITOR_INTERVAL:
            return
        self._last_monitor_ts = now

        if not self._positions:
            return

        try:
            all_positions = {p.symbol: p for p in self.client.get_all_positions()}
        except Exception as e:
            log.warning(f"Options monitor: could not fetch positions: {e}")
            return

        to_close: List[str] = []
        stop_symbols: List[str] = []   # underlying symbols closed due to stop loss
        today = datetime.date.today()

        # Check if we're on a small account with limited PDT headroom
        pdt_small_account = False
        dt_left_today = 999
        try:
            acct = self.client.get_account()
            pdt_flagged = str(getattr(acct, "pattern_day_trader", False)).lower() in ("1", "true", "yes")
            if float(acct.equity) < PDT_ACCOUNT_MIN and pdt_flagged:
                pdt_small_account = True
                dt_left_today = max(0, PDT_MAX_TRADES - int(acct.daytrade_count))
        except Exception:
            pass

        for occ_sym, pos in list(self._positions.items()):
            dte = (pos.expiry - today).days

            # 1. Expiry risk: close day-before or day-of expiry
            if dte <= 1:
                # On small account, closing same-day entry at expiry = day trade.
                # If PDT headroom is tight, log a warning but still close (expiry loss is worse).
                if pdt_small_account and pos.entered_at == today and dt_left_today <= PDT_OPTIONS_DAY_TRADE_RESERVE:
                    log.warning(
                        f"OPTIONS: {occ_sym} expiring DTE={dte} but entered today — "
                        f"closing anyway (expiry risk > PDT risk, {dt_left_today} DT left)"
                    )
                else:
                    log.warning(f"OPTIONS: {occ_sym} expiring in {dte}d — closing to avoid expiry")
                to_close.append(occ_sym)
                continue

            # 2. Fetch current market value from Alpaca positions
            ap = all_positions.get(occ_sym)
            if ap is None:
                # Position no longer exists (filled/closed externally)
                log.info(f"OPTIONS: {occ_sym} no longer in positions, removing from tracker")
                del self._positions[occ_sym]
                continue

            try:
                current_price = float(ap.current_price)
                entry_price   = pos.entry_price
                if entry_price <= 0:
                    continue

                # PDT guard: never close a buy_to_open position on the same day it was entered
                # when the account is small — that's a day trade. Let it ride overnight instead.
                same_day_entry = (pos.entered_at == today)
                pdt_block_today = pdt_small_account and same_day_entry and dt_left_today <= PDT_OPTIONS_DAY_TRADE_RESERVE

                if pos.action == "buy_to_open":
                    # For debit spreads: net P&L = long_gain - short_gain
                    # entry_price is the net debit (long_mid - short_mid at entry)
                    if pos.short_occ_symbol and pos.short_entry_price:
                        short_ap = all_positions.get(pos.short_occ_symbol)
                        if short_ap is not None:
                            short_price = float(short_ap.current_price)
                            net_current = current_price - short_price
                            # Compare net current value vs net debit (entry cost)
                            pnl_pct = (net_current - pos.entry_price) / pos.entry_price * 100
                        else:
                            pnl_pct = (current_price - entry_price) / entry_price * 100
                    else:
                        pnl_pct = (current_price - entry_price) / entry_price * 100

                    # Track peak P&L for trailing stop
                    if pnl_pct > pos.peak_pnl_pct:
                        pos.peak_pnl_pct = pnl_pct

                    # Spread: profit target 50–70% of max gain; use 60% of entry as proxy
                    profit_target = OPTIONS_PROFIT_TARGET_PCT if not pos.short_occ_symbol else 60.0
                    if pnl_pct >= profit_target:
                        if pdt_block_today:
                            log.info(
                                f"OPTIONS: {occ_sym} +{pnl_pct:.1f}% (profit) but entered today — "
                                f"holding overnight to avoid PDT day trade ({dt_left_today} DT left)"
                            )
                        else:
                            log.info(f"OPTIONS: {occ_sym} hit profit target +{pnl_pct:.1f}% — closing")
                            to_close.append(occ_sym)
                    elif pnl_pct <= -OPTIONS_STOP_LOSS_PCT:
                        if pdt_block_today:
                            log.warning(
                                f"OPTIONS: {occ_sym} {pnl_pct:.1f}% (stop) but entered today — "
                                f"holding overnight to avoid PDT day trade ({dt_left_today} DT left)"
                            )
                        else:
                            log.warning(f"OPTIONS: {occ_sym} hit stop loss {pnl_pct:.1f}% — closing")
                            to_close.append(occ_sym)
                            stop_symbols.append(pos.symbol)
                    elif pos.peak_pnl_pct >= 20.0 and pnl_pct <= pos.peak_pnl_pct - 20.0:
                        # Trailing stop: if position reached +20%+ then gave back 20pp, close
                        if pdt_block_today:
                            log.info(
                                f"OPTIONS: {occ_sym} trailing stop peak={pos.peak_pnl_pct:.1f}% now={pnl_pct:.1f}% — "
                                f"holding overnight (PDT)"
                            )
                        else:
                            log.info(
                                f"OPTIONS: {occ_sym} trailing stop peak={pos.peak_pnl_pct:.1f}% now={pnl_pct:.1f}% — closing"
                            )
                            to_close.append(occ_sym)
                else:
                    # sell_to_open (covered call) — monitor for buy-to-close
                    # Close when premium decays 75%+ (retain most income) or 3 DTE
                    decay_pct = (entry_price - current_price) / entry_price * 100
                    if decay_pct >= 75 or dte <= 3:
                        log.info(
                            f"OPTIONS: covered call {occ_sym} decay={decay_pct:.0f}% DTE={dte} — closing"
                        )
                        to_close.append(occ_sym)

            except Exception as e:
                log.debug(f"Options monitor error for {occ_sym}: {e}")

        for occ_sym in to_close:
            self._close_option(occ_sym)

        # Record cooldown for any symbols closed due to stop loss
        for underlying in stop_symbols:
            record_stop_cooldown(underlying)

    def _close_option(self, occ_sym: str) -> None:
        """Market close an options position (and its spread short leg if applicable)."""
        pos = self._positions.get(occ_sym)
        if pos is None:
            return

        side = OrderSide.SELL if pos.action == "buy_to_open" else OrderSide.BUY

        try:
            order_req = MarketOrderRequest(
                symbol=occ_sym,
                qty=pos.contracts,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
            self.client.submit_order(order_req)
            log.info(f"OPTIONS CLOSE: {side.value.upper()} {pos.contracts}x {occ_sym}")
        except Exception as e:
            log.error(f"Options close failed for {occ_sym}: {e}")

        # Close the short leg of a debit spread (buy-to-close the sold OTM call)
        if pos.short_occ_symbol:
            try:
                short_req = MarketOrderRequest(
                    symbol=pos.short_occ_symbol,
                    qty=pos.contracts,
                    side=OrderSide.BUY,   # buy-to-close the short leg
                    time_in_force=TimeInForce.DAY,
                )
                self.client.submit_order(short_req)
                log.info(f"OPTIONS SPREAD CLOSE SHORT: BUY {pos.contracts}x {pos.short_occ_symbol}")
            except Exception as e:
                log.error(f"Spread short-leg close failed for {pos.short_occ_symbol}: {e}")

        del self._positions[occ_sym]

    def close_all(self) -> None:
        """Emergency: close all open options positions."""
        for occ_sym in list(self._positions.keys()):
            self._close_option(occ_sym)

    # ── Status ─────────────────────────────────────────────────────────────────

    def status_summary(self) -> str:
        if not self._positions:
            return "Options: no open positions"
        lines = [f"Options: {len(self._positions)} position(s)"]
        today = datetime.date.today()
        for occ_sym, pos in self._positions.items():
            dte = (pos.expiry - today).days
            lines.append(
                f"  {occ_sym} | {pos.contracts}x {pos.strategy} "
                f"entry=${pos.entry_price:.2f} DTE={dte}"
            )
        return "\n".join(lines)

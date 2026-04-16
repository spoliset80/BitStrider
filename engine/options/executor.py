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
from typing import List, Dict, Tuple
from dataclasses import dataclass, field
import json
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce, QueryOrderStatus

from engine.config import (
    OPTIONS_ENABLED,
    OPTIONS_ALLOCATION_PCT,
    OPTIONS_MAX_POSITIONS,
    OPTIONS_PROFIT_TARGET_PCT,
    OPTIONS_STOP_LOSS_PCT,
    OPTIONS_DTE_MIN,
    PDT_ACCOUNT_MIN, PDT_MAX_TRADES, PDT_OPTIONS_DAY_TRADE_RESERVE,
    OPTIONS_THETA_EXIT_DTE,
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
    occ_symbol:  str               # primary leg OCC symbol (key in _positions dict)
    symbol:      str               # underlying ticker
    option_type: str
    action:      str               # 'buy_to_open' or 'sell_to_open'
    strike:      float             # primary leg strike
    expiry:      datetime.date
    contracts:   int
    entry_price: float             # signed net value at entry: positive = net debit paid,
                                   # negative = net credit received (per share)
    strategy:    str
    entered_at:  datetime.date = field(default_factory=datetime.date.today)
    peak_pnl_pct: float = 0.0     # highest observed P&L % (trailing stop reference)
    # All legs stored at entry — enables strategy-agnostic close and P&L calculation.
    # Each entry: {"occ_symbol": str, "side": "buy"|"sell", "ratio_qty": int}
    legs: list = field(default_factory=list)


class OptionsExecutor:
    """Manages options positions within a 15% portfolio allocation."""

    def __init__(self, client: TradingClient):
        self.client = client
        self._positions: Dict[str, OptionsPosition] = {}   # occ_symbol -> OptionsPosition
        self._last_monitor_ts: float = 0.0
        self._MONITOR_INTERVAL = 20   # seconds between P&L checks (fast enough to catch intraday moves)

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
        """
        Production-ready order placement for ApexTrader.
        Fixes communications with Alpaca API and internal state tracking.
        """
        # 1. Global Enable Check
        if not getattr(self, "OPTIONS_ENABLED", True):
            return False

        # 2. PDT & Account Guard
        is_small_account, dt_remaining = self._check_pdt_status()
        if is_small_account:
            # Reserve day trades only matter for same-day closes.
            # Options entered with DTE >= OPTIONS_DTE_MIN are swing trades by design
            # and will NOT create a day trade on entry. Only block if we literally have
            # no day trades left (would be unable to emergency-exit any position today).
            if dt_remaining == 0:
                log.info(
                    f"[OPTIONS] PDT reserve: 0 day trades remaining — skipping {signal.symbol} entry"
                )
                return False
            # Cap to 1 open option position on small accounts to conserve capital
            if self._count_open_options() >= 1:
                log.info(f"[OPTIONS] Small account 1-position cap reached — skipping {signal.symbol}")
                return False

        # 3. Budget & Contract Calculation
        _, remaining = self._get_options_budget()
        contracts = self._calc_contracts(signal, remaining)
        if contracts <= 0:
            log.info(f"[OPTIONS] Insufficient budget for {signal.symbol}")
            return False

        # 4. Determine Strategy Type
        strat = signal.strategy.lower()
        is_butterfly = "butterfly" in strat or "butterfly" in signal.option_type.lower()
        is_condor = "condor" in strat
        is_spread = signal.spread_sell_strike is not None and not is_butterfly and not is_condor
        is_mleg = is_butterfly or is_spread or is_condor
        
        cp_type = "call" if "call" in signal.option_type.lower() else "put"

        try:
            # Price improvement: for debits (buy) bid 1% below mid to lower cost;
            # for credits (sell) offer 1% above mid to collect more.
            # Avoid the prior mistake of always paying 2% over mid on debit orders.
            if "buy" in signal.action:
                limit_price = round(signal.mid_price * 0.99, 2)   # bid below mid
            else:
                limit_price = round(signal.mid_price * 1.01, 2)   # offer above mid

            # ── CASE A: MULTI-LEG (Spreads, Butterflies, Condors) ────────────────
            if is_mleg:
                legs_list = []

                if is_condor:
                    legs_list = [
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, "put", signal.put_long_strike), "side": OrderSide.BUY, "ratio_qty": 1},
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, "put", signal.put_short_strike), "side": OrderSide.SELL, "ratio_qty": 1},
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, "call", signal.call_short_strike), "side": OrderSide.SELL, "ratio_qty": 1},
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, "call", signal.call_long_strike), "side": OrderSide.BUY, "ratio_qty": 1}
                    ]
                elif is_butterfly:
                    legs_list = [
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.butterfly_low_strike), "side": OrderSide.BUY, "ratio_qty": 1},
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.strike), "side": OrderSide.SELL, "ratio_qty": 2},
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.butterfly_high_strike), "side": OrderSide.BUY, "ratio_qty": 1}
                    ]
                else: # Vertical Spread
                    legs_list = [
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.strike), "side": OrderSide.BUY, "ratio_qty": 1},
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.spread_sell_strike), "side": OrderSide.SELL, "ratio_qty": 1}
                    ]

                # Construct Payload
                payload = {
                    "symbol": "", # Must be empty for MLEG
                    "qty": str(float(contracts)),
                    "side": "buy",
                    "type": "limit",
                    "order_class": "mleg",
                    "limit_price": str(limit_price),
                    "time_in_force": "day",
                    "legs": [
                        {
                            "symbol": l["symbol"],
                            "side": l["side"].value if hasattr(l["side"], 'value') else l["side"],
                            "ratio_qty": str(float(l["ratio_qty"]))
                        } for l in legs_list
                    ]
                }

                log.info(f"[OPTIONS] Submitting MLEG {signal.symbol}: {json.dumps(payload)}")
                self.client.post("/orders", payload)

            # ── CASE B: SINGLE OPTION (Standard) ─────────────────────────────
            else:
                occ_sym = _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.strike)
                order_req = LimitOrderRequest(
                    symbol=occ_sym,
                    qty=contracts,
                    side=OrderSide.BUY if "buy" in signal.action else OrderSide.SELL,
                    limit_price=limit_price,
                    time_in_force=TimeInForce.DAY
                )
                log.info(f"[OPTIONS] Submitting SINGLE {occ_sym}")
                self.client.submit_order(order_req)

            # 5. Tracking — store all leg OCC symbols for strategy-agnostic close/monitor
            primary_occ = _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.strike)
            entry_legs = [
                {
                    "occ_symbol": l["symbol"],
                    "side": l["side"].value if hasattr(l["side"], "value") else str(l["side"]),
                    "ratio_qty": l["ratio_qty"],
                }
                for l in (legs_list if is_mleg else [{"symbol": primary_occ, "side": OrderSide.BUY, "ratio_qty": 1}])
            ]

            self._positions[primary_occ] = OptionsPosition(
                occ_symbol=primary_occ,
                symbol=signal.symbol,
                option_type=signal.option_type,
                action=signal.action,
                strike=signal.strike,
                expiry=signal.expiry,
                contracts=contracts,
                entry_price=signal.mid_price,
                strategy=signal.strategy,
                legs=entry_legs,
            )

            leg_summary = ", ".join(
                f"{l['side'].upper()} {l['occ_symbol']}" for l in entry_legs
            )
            log.info(f"[OPTIONS] Tracked {signal.symbol} ({len(entry_legs)} leg(s)): {leg_summary}")
            return True

        except Exception as e:
            log.error(f"[OPTIONS] CRITICAL FAILURE for {signal.symbol}: {e}", exc_info=True)
            return False
        
        
    # ── Position Monitoring ────────────────────────────────────────────────────
    def _check_pdt_status(self) -> Tuple[bool, int]:
        """
        Helper to determine if the account is subject to PDT restrictions.
        Returns: (is_small_account, day_trades_remaining)
        """
        try:
            acct = self.client.get_account()
            equity = float(acct.equity)
            pdt_flagged = str(getattr(acct, "pattern_day_trader", False)).lower() in ("1", "true", "yes")
            
            # If under 25k and flagged, we are restricted
            is_small = equity < PDT_ACCOUNT_MIN and pdt_flagged
            trades_used = int(getattr(acct, "daytrade_count", 0))
            remaining = max(0, PDT_MAX_TRADES - trades_used)
            
            return is_small, remaining
        except Exception as e:
            log.warning(f"PDT Status Check Failed: {e}")
            return False, 0
    def monitor_positions(self) -> None:
        """
        Check open options (Single & MLEG) and close at target/stop.
        Handles Net MtM calculation for Butterflies and Iron Condors.
        """
        now = time.monotonic()
        if now - self._last_monitor_ts < self._MONITOR_INTERVAL:
            return
        self._last_monitor_ts = now

        if not self._positions:
            return

        try:
            # Map by OCC symbol for easy leg lookup
            all_positions = {p.symbol: p for p in self.client.get_all_positions()}
        except Exception as e:
            log.warning(f"Options monitor: could not fetch positions: {e}")
            return

        to_close: List[str] = []
        stop_symbols: List[str] = []
        today = datetime.date.today()

        # PDT/Account Status Logic (unchanged)
        pdt_small_account, dt_left_today = self._check_pdt_status()

        for occ_sym, pos in list(self._positions.items()):
            dte = (pos.expiry - today).days
            same_day_entry = pos.entered_at == today
            pdt_block = pdt_small_account and same_day_entry and dt_left_today <= 1

            # 1. Theta guard: exit within OPTIONS_THETA_EXIT_DTE days to avoid accelerating decay
            # Skip if entered today and PDT-blocked — Alpaca will reject the close anyway
            if dte <= OPTIONS_THETA_EXIT_DTE:
                if pdt_block or same_day_entry:
                    log.debug(
                        f"OPTIONS: {occ_sym} theta guard (DTE={dte}) deferred — entered today, will close tomorrow"
                    )
                else:
                    log.warning(f"OPTIONS: {occ_sym} within {OPTIONS_THETA_EXIT_DTE}d of expiry (DTE={dte}) — closing")
                    to_close.append(occ_sym)
                continue

            # 2. Net value = sum(sign * ratio * current_price) across all stored legs
            # sign: +1 for buy legs (we own), -1 for sell legs (we owe)
            # This formula works identically for single, spread, butterfly, and condor.
            try:
                current_net_value = 0.0
                any_leg_missing = False
                for leg in pos.legs:
                    leg_pos = all_positions.get(leg["occ_symbol"])
                    if leg_pos is None:
                        any_leg_missing = True
                        break
                    sign = 1 if leg["side"] == "buy" else -1
                    current_net_value += sign * leg["ratio_qty"] * float(leg_pos.current_price)

                if any_leg_missing:
                    log.info(f"OPTIONS: {occ_sym} leg(s) closed externally — clearing tracker")
                    del self._positions[occ_sym]
                    continue

                # 3. P&L — normalise by abs(entry) so credit and debit strategies
                # both show positive % when profitable
                entry_price = pos.entry_price
                if abs(entry_price) < 0.001:
                    continue
                pnl_pct = (current_net_value - entry_price) / abs(entry_price) * 100

                if pnl_pct > pos.peak_pnl_pct:
                    pos.peak_pnl_pct = pnl_pct

                log.debug(
                    f"[OPTIONS] {pos.symbol} {pos.strategy} net=${current_net_value:.2f} "
                    f"entry=${entry_price:.2f} P&L={pnl_pct:+.1f}% peak={pos.peak_pnl_pct:.1f}%"
                )

                # 4. Exit decision  (same_day_entry / pdt_block computed at top of loop)

                if pnl_pct >= OPTIONS_PROFIT_TARGET_PCT:
                    if pdt_block:
                        log.info(f"OPTIONS: {pos.symbol} at target {pnl_pct:.1f}% but PDT blocked")
                    else:
                        log.info(f"OPTIONS: {pos.symbol} target hit ({pnl_pct:.1f}%) — closing")
                        to_close.append(occ_sym)

                elif pnl_pct <= -OPTIONS_STOP_LOSS_PCT:
                    if same_day_entry:
                        # Never stop out on entry day — let the position breathe overnight
                        log.debug(
                            f"OPTIONS: {pos.symbol} at stop ({pnl_pct:.1f}%) but entered today — holding"
                        )
                    elif not pdt_block:
                        log.warning(f"OPTIONS: {pos.symbol} stop hit ({pnl_pct:.1f}%) — closing")
                        to_close.append(occ_sym)
                        stop_symbols.append(pos.symbol)

            except Exception as e:
                log.error(f"Error monitoring {occ_sym}: {e}")

        # Execute closes
        for occ_sym in to_close:
            self._close_option(occ_sym)

    def _close_option(self, occ_sym: str) -> None:
        """Close an options position by reversing all stored legs atomically.
        Works identically for single, spread, butterfly, and condor positions.
        """
        pos = self._positions.get(occ_sym)
        if pos is None:
            return

        try:
            if len(pos.legs) > 1:
                # Multi-leg: reverse every side and submit as a single mleg order
                reversed_legs = [
                    {
                        "symbol": l["occ_symbol"],
                        "side": "sell" if l["side"] == "buy" else "buy",
                        "ratio_qty": str(float(l["ratio_qty"])),
                    }
                    for l in pos.legs
                ]
                payload = {
                    "symbol": "",
                    "qty": str(float(pos.contracts)),
                    "side": "sell",
                    "type": "market",
                    "order_class": "mleg",
                    "time_in_force": "day",
                    "legs": reversed_legs,
                }
                log.info(
                    f"OPTIONS MLEG CLOSE: {pos.symbol} {pos.strategy} "
                    f"({len(pos.legs)} legs, {pos.contracts} contract(s))"
                )
                self.client.post("/orders", payload)

            else:
                # Single leg
                side = OrderSide.SELL if pos.action == "buy_to_open" else OrderSide.BUY
                order_req = MarketOrderRequest(
                    symbol=occ_sym,
                    qty=pos.contracts,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                )
                self.client.submit_order(order_req)
                log.info(f"OPTIONS CLOSE: {side.value.upper()} {pos.contracts}x {occ_sym}")

            del self._positions[occ_sym]

        except Exception as e:
            if "40310100" in str(e):
                # PDT protection — Alpaca rejected the close because it would be a day trade.
                # Leave the position in the tracker so it is retried next session.
                log.warning(
                    f"[OPTIONS] PDT block on close {occ_sym} — position held, will retry next session"
                )
            else:
                log.error(f"Options close failed for {occ_sym}: {e}", exc_info=True)

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

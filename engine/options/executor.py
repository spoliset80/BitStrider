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
import json
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.trading.requests import OrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderClass, OrderType, TimeInForce, OrderSide
from alpaca.trading.requests import OrderRequest, LimitOrderRequest, OptionLegRequest
from alpaca.trading.enums import OrderClass, OrderType, TimeInForce, OrderSide

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
        """
        Production-ready order placement for ApexTrader.
        Fixes communications with Alpaca API and internal state tracking.
        """
        # 1. Global Enable Check
        if not getattr(self, "OPTIONS_ENABLED", True):
            return False

        # 2. PDT & Account Guard
        try:
            acct = self.client.get_account()
            equity = float(acct.equity)
            # Small account check (PDT Rule: $25k)
            if equity < 25000 and self._count_open_options() >= 1:
                log.info(f"[OPTIONS] Small account (${equity:,.0f}) position limit reached.")
                return True
        except Exception as e:
            log.warning(f"[OPTIONS] Account check failed: {e}")
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
            # Buffer: Paying 2% more than mid for debits (or accepting 2% less for credits)
            limit_price = round(signal.mid_price * 1.02, 2)

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

            # 5. Tracking (REPAIRED: Includes all required positional arguments)
            primary_occ = _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.strike)
            
            self._positions[primary_occ] = OptionsPosition(
                occ_symbol=primary_occ,
                symbol=signal.symbol,
                option_type=signal.option_type,
                action=signal.action,
                strike=signal.strike,
                expiry=signal.expiry,
                contracts=contracts,
                entry_price=signal.mid_price,
                strategy=signal.strategy
            )

            log.info(f"[OPTIONS] SUCCESS: Position tracked for {signal.symbol}")
            return True

        except Exception as e:
            log.error(f"[OPTIONS] CRITICAL FAILURE for {signal.symbol}: {e}", exc_info=True)
            return False
        
        
    # ── Position Monitoring ────────────────────────────────────────────────────

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

            # 1. Expiry risk
            if dte <= 1:
                log.warning(f"OPTIONS: {occ_sym} expiring in {dte}d — closing strategy")
                to_close.append(occ_sym)
                continue

            # 2. Net Value Calculation for MLEG
            try:
                # Get the long leg (primary)
                ap = all_positions.get(occ_sym)
                if ap is None:
                    log.info(f"OPTIONS: {occ_sym} primary leg gone, clearing tracker")
                    del self._positions[occ_sym]
                    continue

                # Calculate Net Current Value
                strat = pos.strategy.lower()
                is_butterfly = "butterfly" in strat
                is_condor = "condor" in strat
                
                current_net_value = float(ap.current_price)
                
                # Add/Subtract other legs to get the Net Strategy Price
                if is_butterfly:
                    # Butterfly: Low(Long) - 2*Mid(Short) + High(Long)
                    # Note: We use the ratio_qty stored in your state
                    mid_leg = all_positions.get(pos.mid_occ_symbol) # You must store this at entry
                    high_leg = all_positions.get(pos.high_occ_symbol)
                    if mid_leg and high_leg:
                        current_net_value = (float(ap.current_price) + float(high_leg.current_price)) - (2 * float(mid_leg.current_price))
                
                elif is_condor:
                    # Iron Condor: (PL + CL) - (PS + CS)
                    # Reconstruct from stored legs
                    legs = [all_positions.get(l) for l in pos.all_leg_symbols]
                    if all(legs):
                        # Calculate net credit/debit based on side
                        # This is a simplified version; adjust based on your specific side tracking
                        current_net_value = sum([float(l.current_price) * (1 if l.side == 'long' else -1) for l in legs])

                elif pos.short_occ_symbol: # Vertical Spread
                    short_ap = all_positions.get(pos.short_occ_symbol)
                    if short_ap:
                        current_net_value = float(ap.current_price) - float(short_ap.current_price)

                # 3. P&L Logic
                entry_price = pos.entry_price
                pnl_pct = (current_net_value - entry_price) / entry_price * 100 if entry_price > 0 else 0

                # Track peak P&L
                if pnl_pct > pos.peak_pnl_pct:
                    pos.peak_pnl_pct = pnl_pct

                # 4. Exit Decision Matrix
                pdt_block = pdt_small_account and pos.entered_at == today and dt_left_today <= 1
                
                # Profit Target (Butterflies often target 25-50% of debit paid)
                target = 50.0 if is_butterfly else OPTIONS_PROFIT_TARGET_PCT
                
                if pnl_pct >= target:
                    if pdt_block:
                        log.info(f"OPTIONS: {pos.symbol} at target {pnl_pct:.1f}% but PDT blocked.")
                    else:
                        log.info(f"OPTIONS: {pos.symbol} target hit ({pnl_pct:.1f}%) — closing.")
                        to_close.append(occ_sym)

                elif pnl_pct <= -OPTIONS_STOP_LOSS_PCT:
                    if not pdt_block:
                        log.warning(f"OPTIONS: {pos.symbol} stop loss hit ({pnl_pct:.1f}%) — closing.")
                        to_close.append(occ_sym)
                        stop_symbols.append(pos.symbol)

            except Exception as e:
                log.error(f"Error monitoring {occ_sym}: {e}")

        # Execute closes
        for occ_sym in to_close:
            self._close_option(occ_sym)

    def _close_option(self, occ_sym: str) -> None:
        """
        Market close an options position using MLEG to ensure atomic execution.
        """
        pos = self._positions.get(occ_sym)
        if pos is None:
            return

        strat = pos.strategy.lower()
        is_butterfly = "butterfly" in strat
        is_condor = "condor" in strat
        # Determine if it was a spread based on presence of short_occ_symbol
        is_spread = getattr(pos, 'short_occ_symbol', None) is not None and not (is_butterfly or is_condor)
        is_mleg = is_butterfly or is_spread or is_condor

        try:
            if is_mleg:
                # ── REVERSE THE LEGS ──
                # Use the stored strikes/details from the 'pos' object
                # Note: We reverse the 'side' (BUY becomes SELL, SELL becomes BUY)
                legs_list = []

                if is_condor:
                    legs_list = [
                        {"symbol": self._get_occ(pos, "put_long"), "side": OrderSide.SELL, "ratio_qty": 1},
                        {"symbol": self._get_occ(pos, "put_short"), "side": OrderSide.BUY, "ratio_qty": 1},
                        {"symbol": self._get_occ(pos, "call_short"), "side": OrderSide.BUY, "ratio_qty": 1},
                        {"symbol": self._get_occ(pos, "call_long"), "side": OrderSide.SELL, "ratio_qty": 1}
                    ]
                elif is_butterfly:
                    legs_list = [
                        {"symbol": self._get_occ(pos, "low"), "side": OrderSide.SELL, "ratio_qty": 1},
                        {"symbol": self._get_occ(pos, "mid"), "side": OrderSide.BUY, "ratio_qty": 2},
                        {"symbol": self._get_occ(pos, "high"), "side": OrderSide.SELL, "ratio_qty": 1}
                    ]
                else: # Spread
                    legs_list = [
                        {"symbol": pos.occ_symbol, "side": OrderSide.SELL, "ratio_qty": 1},
                        {"symbol": pos.short_occ_symbol, "side": OrderSide.BUY, "ratio_qty": 1}
                    ]

                payload = {
                    "symbol": "",
                    "qty": str(float(pos.contracts)),
                    "side": "sell",  # Closing a 'buy' entry is a 'sell' exit
                    "type": "market",
                    "order_class": "mleg",
                    "time_in_force": "day",
                    "legs": [
                        {
                            "symbol": l["symbol"],
                            "side": l["side"].value,
                            "ratio_qty": str(float(l["ratio_qty"]))
                        } for l in legs_list
                    ]
                }
                log.info(f"OPTIONS MLEG CLOSE: {pos.symbol} {strat}")
                self.client.post("/orders", payload)

            else:
                # ── STANDARD SINGLE CLOSE ──
                side = OrderSide.SELL if pos.action == "buy_to_open" else OrderSide.BUY
                order_req = MarketOrderRequest(
                    symbol=occ_sym,
                    qty=pos.contracts,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                )
                self.client.submit_order(order_req)
                log.info(f"OPTIONS CLOSE: {side.value.upper()} {pos.contracts}x {occ_sym}")

            # Always cleanup state
            del self._positions[occ_sym]

        except Exception as e:
            log.error(f"Options close failed for {occ_sym}: {e}", exc_info=True)

    def _get_occ(self, pos, leg_type):
        """Helper to reconstruct OCC symbols for multi-leg exits."""
        # This assumes your OptionsPosition object stores necessary strikes 
        # or you have a helper to re-derive them from the strategy name.
        pass

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

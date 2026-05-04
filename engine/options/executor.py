import threading
import time
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
from typing import List, Dict, Tuple, Optional
from collections import defaultdict
from dataclasses import dataclass, field
import json
import re
from alpaca.trading.client import TradingClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce, QueryOrderStatus

# Set to True when Alpaca returns 40310000 (liquidation-only restriction).
# Prevents repeated failed order attempts until the bot is restarted or the
# restriction is cleared (checked fresh each cycle via _is_account_tradeable).
_ACCOUNT_RESTRICTED = False

from engine.config import (
    OPTIONS_ENABLED,
    OPTIONS_ALLOCATION_PCT,
    OPTIONS_MAX_POSITIONS,
    OPTIONS_PROFIT_TARGET_PCT,
    OPTIONS_STOP_LOSS_PCT,
    OPTIONS_DTE_MIN,
    PDT_ACCOUNT_MIN, PDT_MAX_TRADES, PDT_OPTIONS_DAY_TRADE_RESERVE,
    OPTIONS_THETA_EXIT_DTE,
    OPTIONS_TRAIL_ACTIVATE_PCT,
    OPTIONS_TRAIL_DRAWDOWN_PCT,
    API_KEY, API_SECRET, PAPER,
)
from engine.utils import MarketState
from .strategies import OptionSignal, CONTRACT_SIZE, record_stop_cooldown

log = logging.getLogger("ApexTrader.Options")


# ── Helpers ───────────────────────────────────────────────────────────────────

import math as _math

def _bs_option_price(spot: float, strike: float, dte: int, iv: float, call: bool = True) -> float:
    """Thin Black-Scholes pricer used to estimate short-leg credit on auto-derived spreads."""
    T = dte / 365.0
    if T <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return max(0.0, (spot - strike) if call else (strike - spot))
    try:
        d1 = (_math.log(spot / strike) + (0.05 + 0.5 * iv * iv) * T) / (iv * _math.sqrt(T))
        d2 = d1 - iv * _math.sqrt(T)
        def _n(x):
            a = abs(x)
            t = 1.0 / (1.0 + 0.2316419 * a)
            p = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
            r = 1.0 - (1 / _math.sqrt(2 * _math.pi)) * _math.exp(-0.5 * a * a) * p
            return r if x >= 0 else 1.0 - r
        if call:
            return max(0.0, spot * _n(d1) - strike * _math.exp(-0.05 * T) * _n(d2))
        else:
            return max(0.0, strike * _math.exp(-0.05 * T) * _n(-d2) - spot * _n(-d1))
    except Exception:
        return 0.0


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
    # Open-window / IV-gate fields
    entry_iv:      float = 0.0   # IV% at entry (0-1 scale from yfinance/Alpaca)
    is_naked:      bool  = False  # True = single naked leg, eligible for spread conversion
    open_stop_pct: float = 0.0   # Per-position hard stop override (25% open window, 0 = use global)
    # Butterfly break-even mode: once mark goes negative, lower exit target to 0%
    # (recover original debit) instead of waiting for +45%. Latches True, never resets.
    breakeven_mode: bool = False




class OptionsExecutor:
    """Manages options positions within a 15% portfolio allocation."""

    # Adaptive limit order retry config
    _ORDER_RETRY_TIMEOUT = 180  # seconds (3 min)
    _ORDER_MAX_RETRIES = 2
    _ORDER_RETRY_STEP = 0.01    # 1% more aggressive each retry

    def _adaptive_limit_retry(self, order_id, side, orig_limit, symbol, contracts, occ_sym, is_mleg, payload, retry_count=0):
        """After timeout, cancel and resubmit limit order at more aggressive price, up to max retries."""
        log.info(f"[OPTIONS][RETRY] Started retry thread for {symbol} order {order_id} (retry {retry_count})")
        if retry_count >= self._ORDER_MAX_RETRIES:
            log.info(f"[OPTIONS][RETRY] Max retries reached for {symbol} order {order_id}, giving up.")
            return
        log.info(f"[OPTIONS][RETRY] Waiting {self._ORDER_RETRY_TIMEOUT}s before checking order {order_id} for {symbol}")
        time.sleep(self._ORDER_RETRY_TIMEOUT)
        # Check if order is still open
        open_orders = self.client.get_orders(status="OPEN")
        log.debug(f"[OPTIONS][RETRY] Open orders after timeout: {[getattr(o, 'id', None) for o in open_orders]}")
        order = next((o for o in open_orders if getattr(o, "id", None) == order_id), None)
        if order is None:
            log.info(f"[OPTIONS][RETRY] Order {order_id} for {symbol} not found (likely filled or canceled externally), no retry needed.")
            return
        filled_qty = getattr(order, "filled_qty", 0)
        log.info(f"[OPTIONS][RETRY] Order {order_id} for {symbol} still open after timeout. Filled qty: {filled_qty}, Contracts: {contracts}")
        if filled_qty >= contracts:
            log.info(f"[OPTIONS][RETRY] Order {order_id} for {symbol} fully filled, no retry needed.")
            return
        # Cancel and resubmit at more aggressive price
        log.info(f"[OPTIONS][RETRY] Cancelling order {order_id} for {symbol} (unfilled qty: {contracts - filled_qty})")
        self.client.cancel_order_by_id(order_id)
        if side == "buy":
            new_limit = round(orig_limit * (1 + self._ORDER_RETRY_STEP * (retry_count + 1)), 2)
        else:
            new_limit = round(orig_limit * (1 - self._ORDER_RETRY_STEP * (retry_count + 1)), 2)
        log.info(f"[OPTIONS][RETRY] Retrying {symbol} order at more aggressive limit: {new_limit} (retry {retry_count+1})")
        if is_mleg:
            payload["limit_price"] = str(new_limit)
            resp = self.client.post("/orders", payload)
            new_order_id = getattr(resp, "id", None)
            log.info(f"[OPTIONS][RETRY] Submitted new MLEG order for {symbol}: {new_order_id}")
            if new_order_id:
                threading.Thread(target=self._adaptive_limit_retry, args=(new_order_id, side, new_limit, symbol, contracts, occ_sym, is_mleg, payload, retry_count+1), daemon=True).start()
        else:
            order_req = LimitOrderRequest(
                symbol=occ_sym,
                qty=contracts,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                limit_price=new_limit,
                time_in_force=TimeInForce.DAY
            )
            resp = self.client.submit_order(order_req)
            new_order_id = getattr(resp, "id", None)
            log.info(f"[OPTIONS][RETRY] Submitted new SINGLE order for {symbol}: {new_order_id}")
            if new_order_id:
                threading.Thread(target=self._adaptive_limit_retry, args=(new_order_id, side, new_limit, symbol, contracts, occ_sym, is_mleg, payload, retry_count+1), daemon=True).start()

    def _calculate_gross_notional(self, extra: Optional[dict] = None) -> float:
        """
        Calculate the gross notional value of all open option positions, plus an optional extra position.
        Each leg: abs(contracts * strike * 100)
        """
        total = 0.0
        # Existing positions
        for pos in self._positions.values():
            for leg in (pos.legs if pos.legs else [{"occ_symbol": pos.occ_symbol, "side": "buy", "ratio_qty": 1}]):
                contracts = abs(pos.contracts) * abs(leg.get("ratio_qty", 1))
                # Extract strike from OCC symbol (format: SYMBOLYYMMDDC|P########)
                occ = leg["occ_symbol"]
                m = re.match(r"^[A-Z]+\d{6}[CP](\d{8})$", occ)
                if m:
                    strike = int(m.group(1)) / 1000.0
                    total += abs(contracts * strike * 100)
        # Add extra (pending order) if provided
        if extra:
            for leg in extra.get("legs", []):
                contracts = abs(extra["contracts"]) * abs(leg.get("ratio_qty", 1))
                strike = leg["strike"]
                total += abs(contracts * strike * 100)
        return total

    def __init__(self, client: TradingClient):
        self.client = client
        self.data_client = OptionHistoricalDataClient(API_KEY, API_SECRET)
        self._positions: Dict[str, OptionsPosition] = {}   # occ_symbol -> OptionsPosition
        self._last_monitor_ts: float = 0.0
        self._MONITOR_INTERVAL = 20   # seconds between P&L checks (fast enough to catch intraday moves)
        self._last_iv_convert_ts: float = 0.0
        self._IV_CONVERT_INTERVAL = 600.0  # check spread-conversion every 10 min max
        self._reconcile_positions()

    def _reconcile_positions(self) -> None:
        """On startup, re-hydrate _positions from any open options already held in the account.
        Uses avg_entry_price from Alpaca and conservative defaults (global stop/target, no
        open-window flag).  This allows monitor_positions() to manage positions entered in a
        previous session or in paper mode without having to track them from entry.

        Multi-leg detection: groups legs by (ticker, expiry) and reconstructs butterfly
        and iron-condor structures so that P&L and close orders are computed correctly
        as a net position rather than per individual leg.
        """
        try:
            open_pos = [p for p in self.client.get_all_positions()
                        if getattr(p, "asset_class", "") == "us_option"]
        except Exception as e:
            log.warning(f"[OPTIONS] Reconcile: could not fetch positions: {e}")
            return

        if not open_pos:
            return

        today = datetime.date.today()

        # ── Step 1: parse every option position into a plain dict ────────────
        parsed: List[dict] = []
        for p in open_pos:
            occ = p.symbol
            if occ in self._positions:
                continue  # already tracked (entered this session before restart)
            m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", occ)
            if not m:
                continue
            ticker, exp_str, cp_char, strike_str = m.groups()
            try:
                expiry   = datetime.datetime.strptime(exp_str, "%y%m%d").date()
                strike   = int(strike_str) / 1000.0
                opt_type = "call" if cp_char == "C" else "put"
                signed_qty = int(round(float(p.qty)))   # + = long, - = short
                entry_px   = float(p.avg_entry_price)   # always positive (Alpaca convention)
                lastday    = float(getattr(p, "lastday_price", 0) or 0)
                entered_at = today if lastday == 0 else today - datetime.timedelta(days=1)
            except Exception:
                continue
            parsed.append(dict(
                occ=occ, ticker=ticker, expiry=expiry,
                strike=strike, opt_type=opt_type,
                qty=signed_qty, entry_px=entry_px, entered_at=entered_at,
            ))

        if not parsed:
            return

        # ── Step 2: group by (ticker, expiry) for multi-leg detection ────────
        groups: Dict[tuple, list] = defaultdict(list)
        for item in parsed:
            groups[(item["ticker"], item["expiry"])].append(item)

        registered: set = set()   # OCCs already absorbed into a multi-leg position

        for (ticker, expiry), items in groups.items():
            calls = sorted([i for i in items if i["opt_type"] == "call"], key=lambda x: x["strike"])
            puts  = sorted([i for i in items if i["opt_type"] == "put"],  key=lambda x: x["strike"])

            # ── Detect CALL butterfly: [+N, -2N, +N] by ascending strike ─────
            if len(calls) == 3 and all(c["occ"] not in registered for c in calls):
                q0, q1, q2 = calls[0]["qty"], calls[1]["qty"], calls[2]["qty"]
                if q0 > 0 and q2 > 0 and q1 < 0 and q0 == q2 and abs(q1) == 2 * q0:
                    # net_debit = what was paid for both long wings minus credit from short body
                    net_debit = round(
                        calls[0]["entry_px"] + calls[2]["entry_px"] - 2 * calls[1]["entry_px"], 3
                    )
                    primary_occ = calls[0]["occ"]
                    self._positions[primary_occ] = OptionsPosition(
                        occ_symbol  = primary_occ,
                        symbol      = ticker,
                        option_type = "call_butterfly",
                        action      = "buy_to_open",
                        strike      = calls[1]["strike"],   # mid (short body) strike
                        expiry      = expiry,
                        contracts   = q0,
                        entry_price = net_debit,            # positive debit; formula works correctly
                        strategy    = "reconciled_butterfly",
                        legs        = [
                            {"occ_symbol": calls[0]["occ"], "side": "buy",  "ratio_qty": 1},
                            {"occ_symbol": calls[1]["occ"], "side": "sell", "ratio_qty": 2},
                            {"occ_symbol": calls[2]["occ"], "side": "buy",  "ratio_qty": 1},
                        ],
                        entered_at  = calls[0]["entered_at"],
                    )
                    for c in calls:
                        registered.add(c["occ"])
                    log.info(
                        f"[OPTIONS] Reconciled CALL BUTTERFLY: {ticker} "
                        f"{calls[0]['strike']:.2f}/{calls[1]['strike']:.2f}/{calls[2]['strike']:.2f}C "
                        f"net_debit=${net_debit:.3f} ×{q0}c"
                    )
                    continue

            # ── Detect PUT butterfly: [+N, -2N, +N] by ascending strike ──────
            if len(puts) == 3 and all(p_["occ"] not in registered for p_ in puts):
                q0, q1, q2 = puts[0]["qty"], puts[1]["qty"], puts[2]["qty"]
                if q0 > 0 and q2 > 0 and q1 < 0 and q0 == q2 and abs(q1) == 2 * q0:
                    net_debit = round(
                        puts[0]["entry_px"] + puts[2]["entry_px"] - 2 * puts[1]["entry_px"], 3
                    )
                    primary_occ = puts[0]["occ"]
                    self._positions[primary_occ] = OptionsPosition(
                        occ_symbol  = primary_occ,
                        symbol      = ticker,
                        option_type = "put_butterfly",
                        action      = "buy_to_open",
                        strike      = puts[1]["strike"],
                        expiry      = expiry,
                        contracts   = q0,
                        entry_price = net_debit,
                        strategy    = "reconciled_butterfly",
                        legs        = [
                            {"occ_symbol": puts[0]["occ"], "side": "buy",  "ratio_qty": 1},
                            {"occ_symbol": puts[1]["occ"], "side": "sell", "ratio_qty": 2},
                            {"occ_symbol": puts[2]["occ"], "side": "buy",  "ratio_qty": 1},
                        ],
                        entered_at  = puts[0]["entered_at"],
                    )
                    for p_ in puts:
                        registered.add(p_["occ"])
                    log.info(
                        f"[OPTIONS] Reconciled PUT BUTTERFLY: {ticker} "
                        f"{puts[0]['strike']:.2f}/{puts[1]['strike']:.2f}/{puts[2]['strike']:.2f}P "
                        f"net_debit=${net_debit:.3f} ×{q0}c"
                    )
                    continue

            # ── Detect IRON CONDOR: 2 puts + 2 calls, pattern [+,-,-,+] ──────
            # puts sorted asc: [long OTM put (+), short near-ATM put (-)]
            # calls sorted asc: [short near-ATM call (-), long OTM call (+)]
            if (len(puts) == 2 and len(calls) == 2
                    and all(i["occ"] not in registered for i in items)):
                qp0, qp1 = puts[0]["qty"], puts[1]["qty"]
                qc0, qc1 = calls[0]["qty"], calls[1]["qty"]
                base = abs(qp0)
                if (qp0 > 0 and qp1 < 0 and qc0 < 0 and qc1 > 0
                        and abs(qp0) == abs(qp1) == abs(qc0) == abs(qc1)):
                    # net_credit = premiums received (shorts) minus premiums paid (long wings)
                    net_credit = round(
                        puts[1]["entry_px"] + calls[0]["entry_px"]
                        - puts[0]["entry_px"] - calls[1]["entry_px"], 3
                    )
                    primary_occ = calls[0]["occ"]   # short call as position key
                    # For sell_to_open: store entry_price as NEGATIVE so the P&L formula
                    # (current_net_value - entry_price) / abs(entry_price) shows +% when profitable.
                    self._positions[primary_occ] = OptionsPosition(
                        occ_symbol  = primary_occ,
                        symbol      = ticker,
                        option_type = "iron_condor",
                        action      = "sell_to_open",
                        strike      = calls[0]["strike"],
                        expiry      = expiry,
                        contracts   = base,
                        entry_price = -abs(net_credit),     # negative for credit strategy
                        strategy    = "reconciled_condor",
                        legs        = [
                            {"occ_symbol": puts[0]["occ"],  "side": "buy",  "ratio_qty": 1},
                            {"occ_symbol": puts[1]["occ"],  "side": "sell", "ratio_qty": 1},
                            {"occ_symbol": calls[0]["occ"], "side": "sell", "ratio_qty": 1},
                            {"occ_symbol": calls[1]["occ"], "side": "buy",  "ratio_qty": 1},
                        ],
                        entered_at  = calls[0]["entered_at"],
                    )
                    for i in items:
                        registered.add(i["occ"])
                    log.info(
                        f"[OPTIONS] Reconciled IRON CONDOR: {ticker} "
                        f"{puts[0]['strike']:.2f}/{puts[1]['strike']:.2f}P "
                        f"{calls[0]['strike']:.2f}/{calls[1]['strike']:.2f}C "
                        f"net_credit=${net_credit:.3f} ×{base}c"
                    )
                    continue

            # ── Detect CALL/PUT SPREAD: 2 same-type legs [+N, -N] ───────────
            for opt_legs in (calls, puts):
                if len(opt_legs) == 2 and all(l["occ"] not in registered for l in opt_legs):
                    l0, l1 = opt_legs[0], opt_legs[1]   # sorted asc by strike
                    if l0["qty"] > 0 and l1["qty"] < 0 and l0["qty"] == abs(l1["qty"]):
                        # Debit spread: buy lower strike, sell higher strike
                        buy_leg, sell_leg = l0, l1
                    elif l0["qty"] < 0 and l1["qty"] > 0 and abs(l0["qty"]) == l1["qty"]:
                        # Credit spread: sell lower strike, buy higher strike
                        buy_leg, sell_leg = l1, l0
                    else:
                        continue

                    n         = abs(buy_leg["qty"])
                    opt_type  = buy_leg["opt_type"]
                    # net_debit: positive for debit spreads, negative for credit spreads
                    net_debit = round(buy_leg["entry_px"] - sell_leg["entry_px"], 3)
                    primary_occ = buy_leg["occ"]
                    spread_name = f"{opt_type}_spread"
                    self._positions[primary_occ] = OptionsPosition(
                        occ_symbol  = primary_occ,
                        symbol      = ticker,
                        option_type = spread_name,
                        action      = "buy_to_open" if net_debit > 0 else "sell_to_open",
                        strike      = buy_leg["strike"],
                        expiry      = expiry,
                        contracts   = n,
                        entry_price = net_debit,
                        strategy    = "reconciled_spread",
                        legs        = [
                            {"occ_symbol": buy_leg["occ"],  "side": "buy",  "ratio_qty": 1},
                            {"occ_symbol": sell_leg["occ"], "side": "sell", "ratio_qty": 1},
                        ],
                        entered_at  = buy_leg["entered_at"],
                    )
                    registered.add(buy_leg["occ"])
                    registered.add(sell_leg["occ"])
                    direction = "DEBIT" if net_debit > 0 else "CREDIT"
                    log.info(
                        f"[OPTIONS] Reconciled {opt_type.upper()} {direction} SPREAD: {ticker} "
                        f"{buy_leg['strike']:.2f}/{sell_leg['strike']:.2f} "
                        f"net={'${:.3f}'.format(net_debit)} ×{n}c"
                    )

            # ── Fall through: register remaining legs as individual positions ─
            for item in items:
                if item["occ"] in self._positions or item["occ"] in registered:
                    continue
                side   = "buy" if item["qty"] > 0 else "sell"
                action = "buy_to_open" if side == "buy" else "sell_to_open"
                # For single short legs: negate entry_price so P&L formula is correct.
                # Formula: (current_net_value - entry_price) / abs(entry_price)
                # Short: current_net_value = -current_price; with negative entry_price
                # this shows +% when current_price falls below what was received.
                stored_entry = item["entry_px"] if side == "buy" else -item["entry_px"]
                self._positions[item["occ"]] = OptionsPosition(
                    occ_symbol  = item["occ"],
                    symbol      = item["ticker"],
                    option_type = item["opt_type"],
                    action      = action,
                    strike      = item["strike"],
                    expiry      = item["expiry"],
                    contracts   = abs(item["qty"]),
                    entry_price = stored_entry,
                    strategy    = "reconciled",
                    legs        = [{"occ_symbol": item["occ"], "side": side, "ratio_qty": 1}],
                    entered_at  = item["entered_at"],
                )
                log.info(
                    f"[OPTIONS] Reconciled {side.upper()} position: {item['occ']} "
                    f"{abs(item['qty'])}x {item['opt_type']} @{item['strike']} "
                    f"entry=${item['entry_px']:.2f}"
                )

    # ── Allocation / Budget ────────────────────────────────────────────────────

    def _get_options_budget(self) -> Tuple[float, float]:
        """Returns (total_options_budget $, remaining_budget $) using real-time API values.

        Priority order (all sourced from /v2/account — real-time, not dashboard):
          1. options_buying_power  — options-specific BP (may be None on some account tiers)
          2. buying_power          — general real-time BP (always present; used as fallback)
          3. equity * pct          — configured allocation cap (always applied as an upper bound)

        Note: Alpaca dashboard charts show 15-min delayed data. The API fields used here
        (equity, buying_power, options_buying_power) are all real-time SIP data regardless
        of whether the account is live or paper.
        """
        try:
            acct              = self.client.get_account()
            equity            = float(acct.equity)
            configured_budget = equity * (OPTIONS_ALLOCATION_PCT / 100.0)

            # --- Determine the actual available buying power from the API ---
            # options_buying_power: options-specific; may be None on some live tiers.
            _raw_obp = getattr(acct, "options_buying_power", None)
            if _raw_obp is not None and float(_raw_obp) > 0:
                available_bp = float(_raw_obp)
                log.debug(f"[OPTIONS] options_buying_power=${available_bp:.2f} (real-time)")
            else:
                # Fall back to general buying_power — always present and real-time.
                available_bp = float(acct.buying_power)
                log.debug(
                    f"[OPTIONS] options_buying_power unavailable — "
                    f"using buying_power=${available_bp:.2f} (real-time)"
                )

            # Apply configured allocation as an upper cap so we never over-deploy.
            total_budget = min(configured_budget, available_bp)
            if available_bp < configured_budget:
                log.debug(
                    f"[OPTIONS] Available BP ${available_bp:.2f} < configured allocation "
                    f"${configured_budget:.2f} — budget capped at ${total_budget:.2f}"
                )

            used      = self.current_options_cost()
            remaining = max(0.0, total_budget - used)
            return total_budget, remaining

        except Exception as e:
            log.warning(f"[OPTIONS] Could not fetch account budget: {e}", exc_info=True)
            return 0.0, 0.0

    def current_options_cost(self) -> float:
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

    def _get_premarket_gap(self, symbol: str) -> float:
        """Returns today's open vs yesterday's close as a signed fraction.
        e.g. +0.04 = gapped up 4%.  Uses Alpaca daily bars (already cached by get_bars).
        """
        try:
            from engine.utils import get_bars
            bars = get_bars(symbol, "2d", "1d")
            if len(bars) < 2:
                return 0.0
            prev_close = float(bars["close"].iloc[-2])
            today_open = float(bars["open"].iloc[-1])
            return (today_open - prev_close) / prev_close
        except Exception:
            return 0.0

    def _fetch_current_iv(self, pos: "OptionsPosition") -> Optional[float]:
        """Fetch current implied volatility for a tracked naked position via yfinance.
        Returns 0-1 float (e.g. 0.65 = 65% IV) or None on failure.
        Only called for open naked positions (max 3), every 10 minutes.
        """
        try:
            import yfinance as yf
            ticker = yf.Ticker(pos.symbol)
            exp_str = pos.expiry.strftime("%Y-%m-%d")
            chain = ticker.option_chain(exp_str)
            df = chain.calls if "call" in pos.option_type.lower() else chain.puts
            closest = (df["strike"] - pos.strike).abs()
            if closest.empty:
                return None
            row = df.loc[[closest.idxmin()]]
            iv = float(row["impliedVolatility"].iloc[0])
            return iv if iv > 0 else None
        except Exception:
            return None

    def _maybe_convert_to_spread(self) -> None:
        """For each open naked position: if IV has dropped >=30% from entry AND
        the position is profitable, sell a further-OTM leg to convert to a spread.

        This is called from monitor_positions() but rate-limited to every
        _IV_CONVERT_INTERVAL seconds to avoid excess API calls.
        """
        now_t = time.monotonic()
        if now_t - self._last_iv_convert_ts < self._IV_CONVERT_INTERVAL:
            return
        self._last_iv_convert_ts = now_t

        try:
            all_pos = {p.symbol: p for p in self.client.get_all_positions()}
        except Exception as e:
            log.warning(f"[OPTIONS] IV convert: could not fetch positions: {e}")
            return

        for occ_sym, pos in list(self._positions.items()):
            if not pos.is_naked or pos.entry_iv <= 0:
                continue
            try:
                # Gate 1: current IV must be >=30% below entry IV
                cur_iv = self._fetch_current_iv(pos)
                if cur_iv is None:
                    continue
                drop_pct = (pos.entry_iv - cur_iv) / pos.entry_iv
                if drop_pct < 0.30:
                    log.debug(
                        f"[OPTIONS] IV convert {pos.symbol}: IV {pos.entry_iv:.2f}→{cur_iv:.2f} "
                        f"(drop={drop_pct:.0%} < 30%) — waiting"
                    )
                    continue

                # Gate 2: position must be profitable
                leg_pos = all_pos.get(occ_sym)
                if leg_pos is None:
                    continue
                cur_price = float(leg_pos.current_price)
                pnl_pct = (cur_price - pos.entry_price) / abs(pos.entry_price) * 100
                if pnl_pct <= 0:
                    log.debug(
                        f"[OPTIONS] IV convert {pos.symbol}: not profitable ({pnl_pct:+.1f}%) — holding naked"
                    )
                    continue

                # Compute short leg: 10% further OTM, rounded to nearest $0.50
                cp_type = "call" if "call" in pos.option_type.lower() else "put"
                if cp_type == "put":
                    short_strike = round(pos.strike * 0.90 / 0.5) * 0.5   # 10% below
                else:
                    short_strike = round(pos.strike * 1.10 / 0.5) * 0.5   # 10% above

                short_occ = _alpaca_option_symbol(pos.symbol, pos.expiry, cp_type, short_strike)

                # Submit sell_to_open for the short leg (market order — speed matters)
                order_req = MarketOrderRequest(
                    symbol=short_occ,
                    qty=pos.contracts,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
                self.client.submit_order(order_req)
                log.info(
                    f"[OPTIONS] IV CONVERT {pos.symbol}: added short leg {short_occ} "
                    f"(IV drop={drop_pct:.0%}, P&L={pnl_pct:+.1f}%) — position is now a spread"
                )

                # Update tracking: add short leg, mark as no longer naked
                pos.legs.append({"occ_symbol": short_occ, "side": "sell", "ratio_qty": 1})
                pos.is_naked = False

            except Exception as e:
                log.error(f"[OPTIONS] IV convert failed for {occ_sym}: {e}", exc_info=True)

    def _is_account_tradeable(self) -> bool:
        """Return False if the account is in liquidation-only / trading-blocked state."""
        global _ACCOUNT_RESTRICTED
        # Short-circuit: if we already caught a 40310000 this session, don't retry.
        # The restriction is a regulatory/PDT hold — it won't clear mid-session.
        if _ACCOUNT_RESTRICTED:
            log.warning(
                "[OPTIONS] Account restricted (40310000 / liquidation-only) — "
                "all new entries blocked. Restart bot or resolve via Alpaca dashboard."
            )
            return False
        try:
            acct = self.client.get_account()
            blocked = (
                getattr(acct, "trading_blocked", False)
                or getattr(acct, "account_blocked", False)
            )
            if blocked:
                log.error(
                    "[OPTIONS] Account is trading-blocked (liquidation-only) — "
                    "halting all new option entries until restriction is cleared"
                )
                _ACCOUNT_RESTRICTED = True
                return False
            return True
        except Exception as e:
            log.warning(f"[OPTIONS] Could not verify account tradeable status: {e}")
            return True  # allow attempt; the order itself will catch any restriction

    def place_option_order(self, signal: OptionSignal, market_state: MarketState) -> bool:
        """
        Production-ready order placement for ApexTrader.
        Fixes communications with Alpaca API and internal state tracking.
        """
        # Fetch budget once — reused by the gross-notional pre-check (section 0) and
        # contract sizing (section 3) to avoid two account API calls per order.
        total_budget, remaining = self._get_options_budget()

        # 0. Gross Notional Check (LIVE accounts only)
        if not PAPER:
            # Prepare legs for the pending order
            strat = signal.strategy.lower()
            is_butterfly = "butterfly" in strat or "butterfly" in signal.option_type.lower()
            is_condor = "condor" in strat
            is_spread = signal.spread_sell_strike is not None and not is_butterfly and not is_condor
            is_mleg = is_butterfly or is_spread or is_condor
            cp_type = "call" if "call" in signal.option_type.lower() else "put"
            raw_contracts = self._calc_contracts(signal, remaining)
            from engine.config import MIN_SIGNAL_CONFIDENCE, CONF_SCALE_MIN_MULT, CONF_SCALE_FULL_CONF
            _conf_mult = CONF_SCALE_MIN_MULT + (1.0 - CONF_SCALE_MIN_MULT) * min(
                1.0, max(0.0, (signal.confidence - MIN_SIGNAL_CONFIDENCE) / (CONF_SCALE_FULL_CONF - MIN_SIGNAL_CONFIDENCE))
            )
            contracts = max(1, int(round(raw_contracts * _conf_mult)))
            # Build legs for the pending order
            if is_condor:
                legs = [
                    {"strike": signal.put_long_strike, "ratio_qty": 1},
                    {"strike": signal.put_short_strike, "ratio_qty": 1},
                    {"strike": signal.call_short_strike, "ratio_qty": 1},
                    {"strike": signal.call_long_strike, "ratio_qty": 1},
                ]
            elif is_butterfly:
                legs = [
                    {"strike": signal.butterfly_low_strike, "ratio_qty": 1},
                    {"strike": signal.strike, "ratio_qty": 2},
                    {"strike": signal.butterfly_high_strike, "ratio_qty": 1},
                ]
            elif is_spread:
                _eff_spread_sell_strike = signal.spread_sell_strike
                legs = [
                    {"strike": signal.strike, "ratio_qty": 1},
                    {"strike": _eff_spread_sell_strike, "ratio_qty": 1},
                ]
            else:
                legs = [{"strike": signal.strike, "ratio_qty": 1}]

            # Dynamically reduce contracts to fit under 6x equity notional cap
            try:
                acct = self.client.get_account()
                equity = float(acct.equity)
            except Exception as e:
                log.warning(f"[OPTIONS] Could not fetch account equity for notional check: {e}")
                equity = 0.0
            min_contracts = 1
            order_accepted = False
            while contracts >= min_contracts:
                extra = {"contracts": contracts, "legs": legs}
                gross_notional = self._calculate_gross_notional(extra=extra)
                if equity > 0 and gross_notional > 6 * equity:
                    contracts -= 1
                    continue
                order_accepted = True
                break
            if not order_accepted:
                log.warning(
                    f"[OPTIONS] Skipping {signal.symbol} order: even 1 contract would exceed gross notional cap (${gross_notional:,.2f} > 6x equity ${equity:,.2f})"
                )
                return False
        # 1. Global Enable Check
        if not getattr(self, "OPTIONS_ENABLED", True):
            return False

        # 1b. Account restriction check (liquidation-only = code 40310000)
        if not self._is_account_tradeable():
            return False

        # 1c. Corporate actions guard — block entry if a reverse split or merger is
        #     pending for the underlying within the next 14 calendar days.
        #     Uses the Alpaca corporate actions endpoint (same auth credentials).
        try:
            import requests as _req
            import datetime as _dt
            _today = _dt.date.today()
            _ca_resp = _req.get(
                "https://data.alpaca.markets/v1beta1/corporate_actions",
                params={
                    "symbols": signal.symbol,
                    "types": "reverse_split,cash_merger,stock_merger,stock_and_cash_merger",
                    "start": _today.isoformat(),
                    "end": (_today + _dt.timedelta(days=14)).isoformat(),
                    "limit": 10,
                },
                headers={
                    "APCA-API-KEY-ID": API_KEY,
                    "APCA-API-SECRET-KEY": API_SECRET,
                },
                timeout=5,
            )
            _ca_resp.raise_for_status()
            _ca_data = _ca_resp.json()
            _ca_events = (
                _ca_data.get("reverse_splits", [])
                + _ca_data.get("cash_mergers", [])
                + _ca_data.get("stock_mergers", [])
                + _ca_data.get("stock_and_cash_mergers", [])
            )
            if _ca_events:
                log.info(
                    f"[OPTIONS] Corporate action pending for {signal.symbol} within 14 days "
                    f"({len(_ca_events)} event(s)) — skipping entry"
                )
                return False
        except Exception:
            pass  # network/parse failure — allow entry rather than block all trades

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
        raw_contracts = self._calc_contracts(signal, remaining)
        if raw_contracts <= 0:
            per_contract = signal.mid_price * CONTRACT_SIZE
            log.info(
                f"[OPTIONS] Insufficient budget for {signal.symbol} "
                f"(remaining=${remaining:.2f}, total=${total_budget:.2f}, "
                f"per_contract=${per_contract:.2f})"
            )
            return False
        # Scale contracts by signal confidence (same curve as equity: 0.50× at floor → 1.0× at 0.85+)
        from engine.config import MIN_SIGNAL_CONFIDENCE, CONF_SCALE_MIN_MULT, CONF_SCALE_FULL_CONF
        _conf_mult = CONF_SCALE_MIN_MULT + (1.0 - CONF_SCALE_MIN_MULT) * min(
            1.0, max(0.0, (signal.confidence - MIN_SIGNAL_CONFIDENCE) / (CONF_SCALE_FULL_CONF - MIN_SIGNAL_CONFIDENCE))
        )
        contracts = max(1, int(round(raw_contracts * _conf_mult)))
        log.debug(f"[OPTIONS] {signal.symbol} conf={signal.confidence:.0%} → scale={_conf_mult:.2f}× → {contracts}c")

        # 4. Determine Strategy Type
        strat = signal.strategy.lower()
        is_butterfly = "butterfly" in strat or "butterfly" in signal.option_type.lower()
        is_condor = "condor" in strat
        is_spread = signal.spread_sell_strike is not None and not is_butterfly and not is_condor
        is_mleg = is_butterfly or is_spread or is_condor

        cp_type = "call" if "call" in signal.option_type.lower() else "put"

        # ── 4b. Open Window / IV Gate ─────────────────────────────────────────
        # Decides naked vs spread AFTER type detection so butterflies/condors are untouched.
        _in_open_window = market_state.is_open_window
        _eff_spread_sell_strike = signal.spread_sell_strike  # may be overridden below
        _entry_iv      = signal.iv_pct   # IV% at scan time (stored on position)
        _is_naked_entry = False
        _open_stop_pct  = 0.0            # 0 = use global OPTIONS_STOP_LOSS_PCT
        _net_entry_price = signal.mid_price   # overridden below if auto-spread net debit is computed

        if not is_butterfly and not is_condor:
            if _in_open_window:
                # Higher confidence bar — only cleanest signals at the open
                if signal.confidence < 0.85:
                    log.debug(
                        f"[OPTIONS] Open window: {signal.symbol} conf={signal.confidence:.0%} < 85% — skip"
                    )
                    return False
                # 1 naked position max during the open window
                if self._count_open_options() >= 1:
                    log.debug(
                        f"[OPTIONS] Open window: already 1 open position — skip {signal.symbol}"
                    )
                    return False
                # 50% contract count (premium is peak at open)
                contracts = max(1, contracts // 2)
                _open_stop_pct = 25.0
                # Open window: default to spread (IV is peak at open).
                # Only go naked if IV is very low AND gap is small — options are cheap.
                gap = self._get_premarket_gap(signal.symbol)
                _allow_naked = signal.iv_rank < 20 and abs(gap) <= 0.02
                _force_spread = not _allow_naked
                if _allow_naked:
                    log.debug(
                        f"[OPTIONS] Open window: {signal.symbol} IV rank={signal.iv_rank:.0f} < 20 "
                        f"gap={gap:+.1%} — low IV, allowing naked"
                    )
                else:
                    log.debug(
                        f"[OPTIONS] Open window: {signal.symbol} IV rank={signal.iv_rank:.0f} "
                        f"gap={gap:+.1%} → spread (default at open)"
                    )
            else:
                # Normal session: IV rank decides naked vs spread
                _force_spread = signal.iv_rank > 35

            if not _force_spread:
                # Trade as pure naked single-leg — ignore any spread_sell_strike from strategy
                is_spread  = False
                is_mleg    = False
                _is_naked_entry = True
                if _in_open_window:
                    log.debug(
                        f"[OPTIONS] Open window NAKED: {signal.symbol} {cp_type} "
                        f"@{signal.strike} ×{contracts}c | IV={_entry_iv:.0%} | stop=25%"
                    )
            else:
                # Need a spread.  If strategy didn't provide a short leg, derive one.
                if _eff_spread_sell_strike is None:
                    if cp_type == "put":
                        _eff_spread_sell_strike = round(signal.strike * 0.90 / 0.5) * 0.5
                    else:
                        _eff_spread_sell_strike = round(signal.strike * 1.10 / 0.5) * 0.5
                    log.debug(
                        f"[OPTIONS] IV gate forced spread for {signal.symbol}: "
                        f"derived short leg @{_eff_spread_sell_strike}"
                    )
                is_spread = True
                is_mleg   = True
        # ── End 4b ───────────────────────────────────────────────────────────

        try:
            # Price improvement: for debits (buy) bid 1% below mid to lower cost;
            # for credits (sell) offer 1% above mid to collect more.
            # For auto-derived spreads (IV gate): signal.mid_price is the long-leg premium
            # only, so we must subtract the estimated short-leg credit to get the true
            # net debit — otherwise Alpaca reserves 2× the buying power needed.
            _spread_mid_price = signal.mid_price
            if is_mleg and signal.spread_sell_mid is None and _eff_spread_sell_strike is not None:
                # Estimate short-leg credit via Black-Scholes
                _dte = max(1, (signal.expiry - datetime.date.today()).days)
                _iv  = signal.iv_pct if signal.iv_pct > 0 else 0.30
                # Spot ≈ signal.strike (entry is at-the-money by convention)
                _short_credit = _bs_option_price(
                    spot=signal.strike,
                    strike=_eff_spread_sell_strike,
                    dte=_dte,
                    iv=_iv,
                    call=(cp_type == "call"),
                )
                _spread_mid_price = max(0.01, signal.mid_price - _short_credit)
                log.debug(
                    f"[OPTIONS] Auto-spread net debit: long=${signal.mid_price:.2f} "
                    f"- short_est=${_short_credit:.2f} = net=${_spread_mid_price:.2f} "
                    f"(was sending ${signal.mid_price:.2f} -- overstating BP)"
                )
                _net_entry_price = _spread_mid_price  # use net debit as position entry_price

            if "buy" in signal.action:
                limit_price = round(_spread_mid_price * 0.99, 2)   # debit: bid below mid (pay less)
            else:
                # sell_to_open (credit strategy)
                if is_mleg:
                    # Alpaca mleg convention: positive = debit, NEGATIVE = credit received
                    limit_price = -round(_spread_mid_price * 1.01, 2)  # offer to receive above mid
                else:
                    limit_price = round(_spread_mid_price * 1.01, 2)   # single-leg sell: offer above mid

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
                    # For debit spreads (buy_to_open): buy primary, sell secondary
                    # For credit spreads (sell_to_open): sell primary, buy secondary
                    is_credit = "sell" in signal.action
                    primary_side   = OrderSide.SELL if is_credit else OrderSide.BUY
                    secondary_side = OrderSide.BUY  if is_credit else OrderSide.SELL
                    legs_list = [
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.strike), "side": primary_side, "ratio_qty": 1},
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, _eff_spread_sell_strike), "side": secondary_side, "ratio_qty": 1}
                    ]

                # Construct Payload
                def _leg_side_str(leg_side) -> str:
                    return leg_side.value if hasattr(leg_side, "value") else str(leg_side)

                payload = {
                    "symbol": "", # Must be empty for MLEG
                    "qty": str(int(round(contracts))),
                    # "side" intentionally omitted — not required for mleg per Alpaca docs;
                    # direction is conveyed by each leg's own side field.
                    "type": "limit",
                    "order_class": "mleg",
                    "limit_price": str(limit_price),  # positive=debit, negative=credit
                    "time_in_force": "day",
                    "legs": [
                        {
                            "symbol": l["symbol"],
                            "side": _leg_side_str(l["side"]),
                            "ratio_qty": str(int(l["ratio_qty"])),
                            "position_intent": (
                                "buy_to_open" if _leg_side_str(l["side"]) == "buy" else "sell_to_open"
                            ),
                        } for l in legs_list
                    ]
                }

                log.debug(f"[OPTIONS] Submitting MLEG {signal.symbol}: {json.dumps(payload)}")
                resp = self.client.post("/orders", payload)
                order_id = getattr(resp, "id", None)
                if order_id:
                    threading.Thread(target=self._adaptive_limit_retry, args=(order_id, "buy" if "buy" in signal.action else "sell", float(payload["limit_price"]), signal.symbol, contracts, "", True, payload, 0), daemon=True).start()

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
                log.debug(f"[OPTIONS] Submitting SINGLE {occ_sym}")
                resp = self.client.submit_order(order_req)
                order_id = getattr(resp, "id", None)
                if order_id:
                    threading.Thread(target=self._adaptive_limit_retry, args=(order_id, "buy" if "buy" in signal.action else "sell", float(limit_price), signal.symbol, contracts, occ_sym, False, None, 0), daemon=True).start()

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
                # For sell_to_open (credit strategies): store as negative so the P&L formula
                # (current_net_value - entry_price) / abs(entry_price) shows +% when profitable.
                # At entry, current_net_value ≈ -net_credit; with entry_price = -net_credit → 0%.
                # At max profit (all legs expire worthless), current_net_value → 0 → +100%.
                entry_price=(-abs(_net_entry_price) if "sell" in signal.action else _net_entry_price),
                strategy=signal.strategy,
                legs=entry_legs,
                entry_iv=_entry_iv,
                is_naked=_is_naked_entry,
                open_stop_pct=_open_stop_pct,
            )

            leg_summary = ", ".join(
                f"{l['side'].upper()} {l['occ_symbol']}" for l in entry_legs
            )
            log.info(
                f"[OPTIONS] EXECUTED {signal.symbol} {signal.option_type} "
                f"{signal.strategy} {contracts}c conf={signal.confidence:.0%} "
                f"legs={len(entry_legs)} open_window={_in_open_window}"
            )
            log.debug(f"[OPTIONS] Tracked {signal.symbol} ({len(entry_legs)} leg(s)): {leg_summary}")
            return True

        except Exception as e:
            # Detect account restriction (40310000) and suppress further order attempts
            if re.search(r'40310000', str(e)):
                global _ACCOUNT_RESTRICTED
                _ACCOUNT_RESTRICTED = True
                log.error(
                    f"[OPTIONS] Account restricted to liquidation-only (40310000) — "
                    f"halting new entries. Resolve via Alpaca dashboard."
                )
            else:
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

            # 2. P&L — fetch live bid/ask quotes for every leg, compute net mark.
            # mid = (bid + ask) / 2 per leg, sign = +1 long / -1 short.
            # Applies to all position types: naked, spread, butterfly, condor.
            # current_mark = net spread price you'd receive (or pay) right now.
            # pnl_pct = (current_mark - entry_mark) / entry_mark × 100
            try:
                # Check all legs still exist in the account first
                any_leg_missing = any(
                    all_positions.get(l["occ_symbol"]) is None for l in pos.legs
                )
                if any_leg_missing:
                    log.info(f"OPTIONS: {occ_sym} leg(s) closed externally — clearing tracker")
                    del self._positions[occ_sym]
                    continue

                # Fetch live snapshots for all legs in one API call.
                # OptionsSnapshot includes bid/ask (latest_quote) + greeks + IV.
                _leg_syms = [l["occ_symbol"] for l in pos.legs]
                _snaps = self.data_client.get_option_snapshot(
                    OptionSnapshotRequest(symbol_or_symbols=_leg_syms)
                )
                current_mark = 0.0
                for leg in pos.legs:
                    _s = _snaps.get(leg["occ_symbol"])
                    if _s is None or _s.latest_quote is None:
                        raise ValueError(f"no snapshot/quote for {leg['occ_symbol']}")
                    _mid  = (float(_s.latest_quote.bid_price) + float(_s.latest_quote.ask_price)) / 2.0
                    _sign = 1 if leg["side"] == "buy" else -1
                    current_mark += _sign * _mid

                entry_mark = abs(pos.entry_price)   # net debit paid (or credit received) per share
                entry_cost_dollars = entry_mark * pos.contracts * CONTRACT_SIZE
                if entry_cost_dollars < 0.01:
                    continue

                # pnl_pct: positive = profitable, negative = losing.
                # For debit spreads: current_mark > entry_mark → profit.
                # For credit spreads: entry_price stored negative, entry_mark = abs → same formula.
                pnl_pct = (current_mark - entry_mark) / entry_mark * 100

                if pnl_pct > pos.peak_pnl_pct:
                    pos.peak_pnl_pct = pnl_pct

                log.debug(
                    f"[OPTIONS] {pos.symbol} {pos.strategy} "
                    f"mark=${current_mark:.2f} entry=${entry_mark:.2f} "
                    f"pnl={pnl_pct:+.1f}% peak={pos.peak_pnl_pct:.1f}%"
                )

                # 4. Exit decision  (same_day_entry / pdt_block computed at top of loop)

                # Per-position stop: tighter for open-window entries (25%) vs global (30%)
                # Must be computed BEFORE the outer check so tighter stops are reachable.
                _eff_stop = pos.open_stop_pct if pos.open_stop_pct > 0 else OPTIONS_STOP_LOSS_PCT
                # DTE-tightened stop: as expiry nears, tighten stop to preserve remaining value.
                # Only applies to standard spreads/naked (butterfly/condor use separate logic below).
                if dte <= 13:
                    _eff_stop = min(_eff_stop, 15.0)
                elif dte <= 20:
                    _eff_stop = min(_eff_stop, 22.0)

                # ── Butterfly-specific exit logic ─────────────────────────────
                # % P&L stop is meaningless for butterflies: max loss is already
                # ── Butterfly / Iron Condor exit logic ───────────────────────
                # current_mark and entry_mark already computed above from live quotes.
                # Strategy: HOLD and wait for recovery. Emergency exit only at DTE ≤ 3.
                _is_butterfly = "butterfly" in pos.option_type.lower() or "butterfly" in pos.strategy.lower()
                _is_condor    = "condor"    in pos.option_type.lower() or "condor"    in pos.strategy.lower()
                if _is_butterfly or _is_condor:
                    _mleg_type = "butterfly" if _is_butterfly else "iron condor"

                    # Break-even mode: once mark drops below entry price, latch.
                    # Lowers profit target from +45% to 0% (just recover what was paid).
                    if current_mark < entry_mark and not pos.breakeven_mode:
                        pos.breakeven_mode = True
                        log.info(
                            f"OPTIONS: {pos.symbol} {_mleg_type} mark ${current_mark:.2f} "
                            f"< entry ${entry_mark:.2f} — switching to break-even exit"
                        )

                    # Profit target: current mark recovered to entry (break-even mode)
                    #                or +45% above entry (normal mode).
                    # Only close if mark is positive (we receive money on close).
                    _target_mark = entry_mark if pos.breakeven_mode else entry_mark * (1 + OPTIONS_PROFIT_TARGET_PCT / 100)
                    if occ_sym not in to_close and current_mark > 0 and current_mark >= _target_mark:
                        if not pdt_block:
                            _reason = "break-even" if pos.breakeven_mode else f"target +{OPTIONS_PROFIT_TARGET_PCT:.0f}%"
                            log.info(
                                f"OPTIONS: {pos.symbol} {_mleg_type} {_reason} hit "
                                f"(mark=${current_mark:.2f} >= target=${_target_mark:.2f}) — closing"
                            )
                            to_close.append(occ_sym)

                    # Emergency exit: DTE ≤ 3 with remaining positive value below 55% of entry.
                    # Close to recover what's left rather than let theta eat it to zero.
                    # If mark is inverted (≤ 0): clear tracker and let expire — no close cost.
                    elif occ_sym not in to_close and dte <= 3:
                        if current_mark <= 0:
                            if not same_day_entry:
                                log.info(
                                    f"OPTIONS: {pos.symbol} {_mleg_type} DTE={dte} "
                                    f"mark=${current_mark:.2f} (inverted) — letting expire"
                                )
                                del self._positions[occ_sym]
                        elif current_mark < entry_mark * 0.55:
                            if same_day_entry:
                                log.debug(
                                    f"OPTIONS: {pos.symbol} {_mleg_type} DTE={dte} emergency exit deferred — entered today"
                                )
                            elif not pdt_block:
                                log.warning(
                                    f"OPTIONS: {pos.symbol} {_mleg_type} DTE={dte} emergency exit "
                                    f"— mark=${current_mark:.2f} < 55% of entry=${entry_mark:.2f} — closing"
                                )
                                to_close.append(occ_sym)
                                stop_symbols.append(pos.symbol)


                    continue   # skip standard % stop / trailing stop for mleg structures
                # ── End butterfly/condor logic ────────────────────────────────

                if pnl_pct >= OPTIONS_PROFIT_TARGET_PCT:
                    if pdt_block:
                        log.info(f"OPTIONS: {pos.symbol} at target {pnl_pct:.1f}% but PDT blocked")
                    else:
                        log.info(f"OPTIONS: {pos.symbol} target hit ({pnl_pct:.1f}%) -- closing")
                        to_close.append(occ_sym)

                elif pnl_pct <= -_eff_stop:
                    if same_day_entry:
                        # Never stop out on entry day — let the position breathe overnight
                        # Exception: open-window entries have tighter 25% same-day stop
                        if pos.open_stop_pct > 0:
                            log.warning(
                                f"OPTIONS: {pos.symbol} open-window stop {_eff_stop:.0f}% hit "
                                f"({pnl_pct:.1f}%) same-day — closing"
                            )
                            to_close.append(occ_sym)
                            stop_symbols.append(pos.symbol)
                        else:
                            log.debug(
                                f"OPTIONS: {pos.symbol} at stop ({pnl_pct:.1f}%) but entered today — holding"
                            )
                    elif not pdt_block:
                        log.warning(f"OPTIONS: {pos.symbol} stop hit ({pnl_pct:.1f}%) — closing")
                        to_close.append(occ_sym)
                        stop_symbols.append(pos.symbol)

                # 5. Trailing stop — arms once peak >= OPTIONS_TRAIL_ACTIVATE_PCT
                # Fires when pnl drops OPTIONS_TRAIL_DRAWDOWN_PCT pp below the peak.
                # Only evaluated when the fixed stop and target haven't already triggered.
                # Skipped on entry day (same logic as fixed stop) to avoid noise.
                elif (
                    not same_day_entry
                    and not pdt_block
                    and pos.peak_pnl_pct >= OPTIONS_TRAIL_ACTIVATE_PCT
                    and pnl_pct <= pos.peak_pnl_pct - OPTIONS_TRAIL_DRAWDOWN_PCT
                ):
                    log.info(
                        f"OPTIONS: {pos.symbol} trailing stop — peak={pos.peak_pnl_pct:.1f}% "
                        f"current={pnl_pct:.1f}% (drawdown {pos.peak_pnl_pct - pnl_pct:.1f}pp "
                        f"> {OPTIONS_TRAIL_DRAWDOWN_PCT:.0f}pp threshold) — closing"
                    )
                    to_close.append(occ_sym)

            except Exception as e:
                log.error(f"Error monitoring {occ_sym}: {e}")

        # Execute closes — pass all_positions so _close_option can compute limit price
        for occ_sym in to_close:
            self._close_option(occ_sym, all_positions=all_positions)

        # Record stop cooldowns so stopped symbols can't re-enter within OPTIONS_STOP_COOLDOWN_DAYS
        for sym in stop_symbols:
            record_stop_cooldown(sym)

        # IV conversion check (rate-limited to every 10 min, touches only open naked positions)
        self._maybe_convert_to_spread()

    def _close_option(self, occ_sym: str, all_positions: Optional[dict] = None) -> None:
        """Close an options position by reversing all stored legs atomically.
        Works identically for single, spread, butterfly, and condor positions.

        For multi-leg positions uses a limit order at current_net_mid * 0.97 to avoid
        market-maker slippage on wide bid/ask spreads (AEHR/EOSE/SOUN style names).
        Falls back to market if current mark cannot be computed.
        """
        pos = self._positions.get(occ_sym)
        if pos is None:
            return

        try:
            if len(pos.legs) > 1:
                # Multi-leg: reverse every side and submit as a single mleg limit order
                reversed_legs = [
                    {
                        "symbol": l["occ_symbol"],
                        "side": "sell" if l["side"] == "buy" else "buy",
                        "ratio_qty": str(int(round(l["ratio_qty"]))),
                        "position_intent": (
                            "buy_to_close" if l["side"] == "sell" else "sell_to_close"
                        ),
                    }
                    for l in pos.legs
                ]

                # Compute net mark price to set a sensible limit.
                # For a closing order the net direction is reversed vs entry:
                #   debit entry  → closing is a credit (we receive) → limit < 0 in Alpaca convention
                #   credit entry → closing is a debit (we pay)      → limit > 0
                # We target 97% of the current mid to guarantee a fill without full-spread
                # slippage on all legs simultaneously (critical for low-liquidity names).
                _close_limit_price = None
                if all_positions:
                    try:
                        net_mid = 0.0
                        for l in pos.legs:
                            lp = all_positions.get(l["occ_symbol"])
                            if lp is None:
                                raise ValueError("leg missing")
                            sign = 1 if l["side"] == "buy" else -1
                            net_mid += sign * l["ratio_qty"] * float(lp.current_price)
                        # For closing: reverse the sign (we're doing the opposite trade)
                        close_mid = -net_mid

                        # Safety: for long-debit mleg positions (butterfly, debit spread)
                        # NEVER pay to close when mark has gone negative — that would cost
                        # more than the original debit (exceeding defined max loss).
                        # Correct action: let it expire worthless. Max loss = debit paid.
                        _is_long_debit_mleg = (
                            pos.action == "buy_to_open"
                            and ("butterfly" in pos.option_type.lower()
                                 or "butterfly" in pos.strategy.lower()
                                 or "spread" in pos.strategy.lower())
                        )
                        if close_mid > 0 and _is_long_debit_mleg:
                            log.info(
                                f"[OPTIONS] {pos.symbol} mleg close would cost "
                                f"${close_mid:.2f} (mark negative) — letting expire worthless. "
                                f"Closing would exceed defined max loss."
                            )
                            del self._positions[occ_sym]
                            return

                        if abs(close_mid) > 0.01:
                            # Debit close (we pay): limit slightly above mid to ensure fill
                            # Credit close (we receive): limit at 97% of mid (accept slightly less)
                            if close_mid > 0:
                                _close_limit_price = round(close_mid * 1.03, 2)   # paying — bid up 3%
                            else:
                                _close_limit_price = round(close_mid * 0.97, 2)   # receiving — 3% haircut
                    except Exception as _e:
                        log.debug(f"[OPTIONS] Could not compute mleg close limit for {occ_sym}: {_e}")

                payload = {
                    "symbol": "",
                    "qty": str(int(round(pos.contracts))),
                    # "side" intentionally omitted — not required for mleg per Alpaca docs;
                    # each reversed leg carries its own side.
                    "order_class": "mleg",
                    "time_in_force": "day",
                    "legs": reversed_legs,
                }
                if _close_limit_price is not None:
                    payload["type"] = "limit"
                    payload["limit_price"] = str(_close_limit_price)
                    log.info(
                        f"OPTIONS MLEG CLOSE (limit @ {_close_limit_price:+.2f}): "
                        f"{pos.symbol} {pos.strategy} ({len(pos.legs)} legs, {pos.contracts} contract(s))"
                    )
                else:
                    payload["type"] = "market"
                    log.info(
                        f"OPTIONS MLEG CLOSE (market fallback): {pos.symbol} {pos.strategy} "
                        f"({len(pos.legs)} legs, {pos.contracts} contract(s))"
                    )
                self.client.post("/orders", payload)

            else:
                # Single leg — always use a limit order (Alpaca rejects market orders
                # for options when no quote is available, error 40310000).
                side = OrderSide.SELL if pos.action == "buy_to_open" else OrderSide.BUY
                _close_limit = None

                # First try: use passed-in all_positions for the mark price
                if all_positions:
                    _lp = all_positions.get(occ_sym)
                    if _lp is not None:
                        _cur = float(_lp.current_price)
                        if _cur > 0.01:
                            # Selling to close: accept 97% of mid to guarantee a fill
                            # Buying to close: pay up 3% to get out quickly
                            _close_limit = round(_cur * 0.97, 2) if side == OrderSide.SELL else round(_cur * 1.03, 2)

                # Second try: fetch a fresh broker quote if still no limit price
                if _close_limit is None:
                    try:
                        _fresh = {p.symbol: p for p in self.client.get_all_positions()}
                        _lp = _fresh.get(occ_sym)
                        if _lp is not None:
                            _cur = float(_lp.current_price)
                            if _cur > 0.01:
                                _close_limit = round(_cur * 0.97, 2) if side == OrderSide.SELL else round(_cur * 1.03, 2)
                    except Exception as _fe:
                        log.debug(f"[OPTIONS] Could not fetch fresh quote for {occ_sym}: {_fe}")

                # Final fallback: near-worthless or unquoted contract — use floor limit
                # Alpaca requires a minimum limit price of $0.05 for options.
                if _close_limit is None:
                    _close_limit = 0.05 if side == OrderSide.SELL else 0.50
                    log.warning(
                        f"[OPTIONS] No quote for {occ_sym} — limit fallback @{_close_limit:.2f} "
                        f"({'floor sell' if side == OrderSide.SELL else 'ceiling buy'})"
                    )

                order_req = LimitOrderRequest(
                    symbol=occ_sym,
                    qty=pos.contracts,
                    side=side,
                    limit_price=_close_limit,
                    time_in_force=TimeInForce.DAY,
                )
                log.info(f"OPTIONS CLOSE (limit @{_close_limit:.2f}): {side.value.upper()} {pos.contracts}x {occ_sym}")
                self.client.submit_order(order_req)

            del self._positions[occ_sym]

        except Exception as e:
            if "40310100" in str(e):
                # PDT protection — Alpaca rejected the close because it would be a day trade.
                # Leave the position in the tracker so it is retried next session.
                log.warning(
                    f"[OPTIONS] PDT block on close {occ_sym} — position held, will retry next session"
                )
            elif "40310000" in str(e):
                # No available quote — contract is expired, halted, or worthless.
                # There is no market to close into; remove from tracker (max loss already realized).
                log.warning(
                    f"[OPTIONS] No quote available to close {occ_sym} — contract likely expired/worthless. "
                    f"Removing from tracker (max loss realized)."
                )
                self._positions.pop(occ_sym, None)
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

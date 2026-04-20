"""
ApexTrader - Options Executor (Level 3 Account / Alpaca)
Manages opening, monitoring, and closing options positions.

Responsibilities:
  - Enforce portfolio allocation cap across all open options
  - Kelly-based contract sizing (confidence × R/R weighted)
  - Place buy_to_open / sell_to_open limit orders
  - Monitor open options P&L every 15 seconds via dedicated thread
  - Partial profit taking at 50% gain (free-ride the remainder)
  - IV-scaled profit targets (cheap IV → let winners run)
  - Time-based exit for flat positions at 50% DTE elapsed
  - Scaled trailing stop that tightens as peak P&L grows
  - Regime-adaptive allocation (10% bear / 15% bull)
  - Per-strategy win-rate tracker for adaptive sizing
  - Aggressive limit close (never market on options)
"""

import logging
import datetime
import threading
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

# ── Regime-adaptive allocation ────────────────────────────────────────────────
_ALLOCATION_BULL = OPTIONS_ALLOCATION_PCT          # e.g. 15% in bull
_ALLOCATION_BEAR = max(OPTIONS_ALLOCATION_PCT * 0.67, 5.0)  # 10% in bear

# ── Profit target scaled by IV rank at entry ──────────────────────────────────
def _iv_profit_target(iv_rank: float) -> float:
    """Return profit target % based on IV rank at entry.
    Cheap IV → larger expected move, let winners run.
    Expensive IV → take profit sooner before IV crush.
    """
    if iv_rank < 20:  return 80.0
    if iv_rank < 30:  return 60.0
    if iv_rank < 40:  return 45.0
    return 35.0

# ── Scaled trailing stop ──────────────────────────────────────────────────────
# (peak_pnl_threshold, trail_drawdown_pp)
_TRAIL_TIERS = [(20, 15), (50, 20), (80, 30), (100, 40)]

def _trail_drawdown(peak_pnl: float) -> float:
    """Return how many pp below peak triggers the trailing stop."""
    for threshold, drawdown in reversed(_TRAIL_TIERS):
        if peak_pnl >= threshold:
            return float(drawdown)
    return 999.0  # not activated yet

# ── Per-strategy win-rate tracker ─────────────────────────────────────────────
_strategy_results: Dict[str, List[bool]] = {}

def _record_result(strategy: str, profit: bool) -> None:
    _strategy_results.setdefault(strategy, []).append(profit)

def _size_multiplier(strategy: str) -> float:
    """Return sizing multiplier [0.5, 1.25] based on recent win rate."""
    results = _strategy_results.get(strategy, [])
    if len(results) < 10:
        return 1.0
    win_rate = sum(results[-20:]) / len(results[-20:])
    if win_rate < 0.40: return 0.50
    if win_rate > 0.65: return 1.25
    return 1.0

# ── OCC symbol helpers ────────────────────────────────────────────────────────

def _alpaca_option_symbol(symbol: str, expiry: datetime.date, option_type: str, strike: float) -> str:
    exp_str    = expiry.strftime("%y%m%d")
    cp         = "C" if option_type.lower() == "call" else "P"
    strike_int = int(round(strike * 1000))
    return f"{symbol}{exp_str}{cp}{strike_int:08d}"


# ── Position dataclass ────────────────────────────────────────────────────────

# ── Position types ───────────────────────────────────────────────────────────
PTYPE_SINGLE    = "single"
PTYPE_SPREAD    = "spread"
PTYPE_CONDOR    = "condor"
PTYPE_BUTTERFLY = "butterfly"
PTYPE_COVERED   = "covered"


@dataclass
class OptionsPosition:
    """Tracked open options position — supports single-leg, spreads, condors, butterflies."""
    occ_symbol:       str
    symbol:           str
    option_type:      str
    action:           str           # 'buy_to_open' or 'sell_to_open'
    strike:           float
    expiry:           datetime.date
    contracts:        int
    entry_price:      float         # per-share net debit paid / net credit received
    strategy:         str
    position_type:    str  = PTYPE_SINGLE  # one of the PTYPE_* constants above
    entered_at:       datetime.date = field(default_factory=datetime.date.today)
    peak_pnl_pct:     float  = 0.0
    iv_rank_at_entry: float  = 0.0
    partial_taken:    bool   = False
    # ── All legs as a list: [{occ, side, qty_ratio}] ─────────────────────────
    # Empty for single-leg. For multi-leg, every non-primary leg is listed here.
    # side: "buy" = long leg / buy-to-close; "sell" = short leg / sell-to-close
    legs: list = field(default_factory=list)
    # ── Debit spread (PTYPE_SPREAD) ───────────────────────────────────────────
    short_occ_symbol:  Optional[str]   = None
    short_strike:      Optional[float] = None
    short_entry_price: Optional[float] = None
    # ── Iron condor (PTYPE_CONDOR) ────────────────────────────────────────────
    ic_put_long_occ:    Optional[str]   = None
    ic_put_short_occ:   Optional[str]   = None
    ic_call_short_occ:  Optional[str]   = None
    ic_call_long_occ:   Optional[str]   = None
    ic_put_credit:      Optional[float] = None  # credit from put  spread at entry
    ic_call_credit:     Optional[float] = None  # credit from call spread at entry
    # ── Butterfly (PTYPE_BUTTERFLY) ───────────────────────────────────────────
    bf_low_occ:    Optional[str]   = None   # long ITM call (buy 1)
    bf_high_occ:   Optional[str]   = None   # long OTM call (buy 1)
    bf_sell_occ:   Optional[str]   = None   # short ATM call (sell 2)
    bf_max_profit: Optional[float] = None   # spread_width - net_debit


# ── Executor ──────────────────────────────────────────────────────────────────

class OptionsExecutor:
    """Manages options positions within the configured portfolio allocation."""

    def __init__(self, client: TradingClient):
        self.client     = client
        self._positions: Dict[str, OptionsPosition] = {}
        self._monitor_interval = 15   # seconds between P&L checks
        self._last_monitor_ts: float  = 0.0
        # Reconcile positions from previous session so monitor covers them immediately
        self._reconcile_positions()
        # Start dedicated monitor thread (15s fixed interval, regime-independent)
        self._start_monitor_thread()

    # ── Startup reconciliation ────────────────────────────────────────────────

    def _reconcile_positions(self) -> None:
        """Rebuild _positions from any open options already held in the account.
        Allows the monitor to manage positions entered in a prior session."""
        try:
            open_pos = [p for p in self.client.get_all_positions()
                        if getattr(p, "asset_class", "") == "us_option"]
        except Exception as e:
            log.warning(f"[OPTIONS] Reconcile: could not fetch positions: {e}")
            return
        if not open_pos:
            return
        for p in open_pos:
            occ = p.symbol
            if occ in self._positions:
                continue
            try:
                entry = float(p.avg_entry_price or 0)
                qty   = abs(int(float(p.qty)))
                if qty == 0 or entry <= 0:
                    continue
                # Infer basic fields from OCC symbol
                from .strategies import _parse_occ_symbol
                parsed = _parse_occ_symbol(occ)
                if parsed is None:
                    continue
                underlying, expiry, opt_type, strike = parsed
                action = "buy_to_open" if float(p.qty) > 0 else "sell_to_open"
                self._positions[occ] = OptionsPosition(
                    occ_symbol=occ, symbol=underlying,
                    option_type=opt_type, action=action,
                    strike=strike, expiry=expiry,
                    contracts=qty, entry_price=entry,
                    strategy="restored",
                    entered_at=datetime.date.today() - datetime.timedelta(days=1),
                )
                log.info(f"[OPTIONS] Reconciled {action} {qty}x {occ} @ ${entry:.2f}")
            except Exception as e:
                log.debug(f"[OPTIONS] Reconcile skip {occ}: {e}")

    # ── Dedicated monitor thread ──────────────────────────────────────────────

    def _start_monitor_thread(self) -> None:
        """Spawn a daemon thread that calls monitor_positions() every 15 seconds.
        Only makes API calls when positions are open — zero overhead otherwise."""
        def _loop():
            while True:
                try:
                    if self._positions:
                        self.monitor_positions()
                except Exception as e:
                    log.error(f"[OPTIONS] Monitor thread error: {e}", exc_info=True)
                time.sleep(self._monitor_interval)

        t = threading.Thread(target=_loop, name="OptionsMonitor", daemon=True)
        t.start()
        log.info(f"[OPTIONS] Monitor thread started ({self._monitor_interval}s interval)")

    # ── Allocation / budget ───────────────────────────────────────────────────

    def _get_options_budget(self) -> Tuple[float, float]:
        """Return (total_budget $, remaining_budget $) based on equity and regime."""
        try:
            from engine.utils.market import is_bull_regime
            acct    = self.client.get_account()
            equity  = float(acct.equity)
            alloc   = _ALLOCATION_BULL if is_bull_regime() else _ALLOCATION_BEAR
            total   = equity * (alloc / 100.0)
            used    = sum(
                p.entry_price * CONTRACT_SIZE * p.contracts
                for p in self._positions.values()
                if p.action == "buy_to_open"
            )
            return total, max(0.0, total - used)
        except Exception as e:
            log.warning(f"[OPTIONS] Budget fetch failed: {e}")
            return 0.0, 0.0

    def _count_open_options(self) -> int:
        return len(self._positions)

    # ── Kelly-based contract sizing ───────────────────────────────────────────

    def _calc_contracts(self, signal: OptionSignal, remaining_budget: float) -> int:
        """Kelly-fraction contract sizing weighted by confidence and R/R.

        Uses half-Kelly to avoid overbetting. Applies per-strategy win-rate
        multiplier to reduce size on underperforming strategies.
        Hard cap: 5 contracts per position (keeps risk manageable).
        """
        if signal.mid_price <= 0:
            return 0

        per_contract = signal.mid_price * CONTRACT_SIZE

        # Half-Kelly based on confidence × R/R
        edge  = float(signal.confidence)
        rr    = min(float(signal.rr_ratio) if signal.rr_ratio > 0 else 1.0, 3.0)
        kelly = (edge * rr - (1 - edge)) / rr
        half_kelly = max(kelly * 0.5, 0.05)   # floor at 5% of remaining budget

        # Apply strategy win-rate multiplier
        mult = _size_multiplier(signal.strategy)
        raw  = int((remaining_budget * half_kelly * mult) // per_contract)

        return max(0, min(raw, 5))   # hard cap 5 contracts

    # ── Live quote for limit pricing ──────────────────────────────────────────

    def _get_mid(self, occ_sym: str) -> float:
        """Fetch current mid price for an option. Returns 0.0 on failure."""
        try:
            from alpaca.data.historical.option import OptionHistoricalDataClient
            from alpaca.data.requests import OptionLatestQuoteRequest
            dc    = OptionHistoricalDataClient(API_KEY, API_SECRET)
            quote = dc.get_option_latest_quote(OptionLatestQuoteRequest(symbol_or_symbols=occ_sym))
            q     = quote.get(occ_sym)
            if q:
                bid = float(q.bid_price or 0)
                ask = float(q.ask_price or 0)
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2.0
        except Exception:
            pass
        return 0.0

    # ── Order placement ───────────────────────────────────────────────────────

    def place_option_order(self, signal: OptionSignal) -> bool:
        """Place a limit order for the options signal.
        Returns True if order was submitted successfully."""
        if not OPTIONS_ENABLED:
            return False

        # PDT & small-account guard
        try:
            acct        = self.client.get_account()
            equity      = float(acct.equity)
            dt_used     = int(acct.daytrade_count)
            pdt_flagged = str(getattr(acct, "pattern_day_trader", False)).lower() in ("1", "true", "yes")
        except Exception as e:
            log.warning(f"[OPTIONS] Account check failed: {e}")
            return False

        is_small = equity < PDT_ACCOUNT_MIN
        if is_small and pdt_flagged:
            dt_left = max(0, PDT_MAX_TRADES - dt_used)
            if dt_left <= PDT_OPTIONS_DAY_TRADE_RESERVE:
                log.info(f"[OPTIONS] Skip {signal.symbol} — PDT {dt_left} DT left")
                return False
        if is_small and self._count_open_options() >= 1:
            log.info(f"[OPTIONS] Small account — already 1 open position, skip {signal.symbol}")
            return False
        if not is_small and self._count_open_options() >= OPTIONS_MAX_POSITIONS:
            log.info(f"[OPTIONS] At max {OPTIONS_MAX_POSITIONS} positions, skip {signal.symbol}")
            return False

        # Never hold 2 positions in the same underlying (same-symbol check)
        existing_underlying = {p.symbol for p in self._positions.values()
                               if p.action == "buy_to_open"}
        if signal.symbol in existing_underlying and signal.action == "buy_to_open":
            log.info(f"[OPTIONS] Already hold {signal.symbol} directional — skip duplicate")
            return False

        _, remaining = self._get_options_budget()
        if remaining <= 0:
            log.info("[OPTIONS] Budget exhausted")
            return False

        contracts = self._calc_contracts(signal, remaining)
        if contracts <= 0:
            log.info(f"[OPTIONS] {signal.symbol} — not enough budget for 1 contract")
            return False

        # Route to correct placement method by option type
        opt_type = signal.option_type
        if opt_type == "iron_condor":
            return self._place_condor(signal, contracts)
        elif opt_type == "call_butterfly":
            return self._place_butterfly(signal, contracts)
        else:
            return self._place_single_or_spread(signal, contracts)

    # ── _place_single_or_spread ───────────────────────────────────────────────

    def _place_single_or_spread(self, signal, contracts: int) -> bool:
        """Place a single-leg or 2-leg debit spread order."""
        from .strategies import _alpaca_option_symbol as _occ
        opt_type = signal.option_type
        occ_sym  = _alpaca_option_symbol(
            signal.symbol, signal.expiry, opt_type, signal.strike
        )
        if occ_sym in self._positions:
            log.info(f"[OPTIONS] Already have {occ_sym}")
            return False

        side        = OrderSide.BUY if signal.action == "buy_to_open" else OrderSide.SELL
        is_spread   = signal.spread_sell_strike is not None and signal.spread_sell_mid is not None
        long_mid    = signal.mid_price + (signal.spread_sell_mid or 0) if is_spread else signal.mid_price
        limit_price = round(long_mid * (1.02 if side == OrderSide.BUY else 0.98), 2)

        try:
            order_req  = LimitOrderRequest(
                symbol=occ_sym, qty=contracts, side=side,
                type="limit", time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )
            long_order = self.client.submit_order(order_req)
            log.info(
                f"[OPTIONS] ORDER: {signal.action.upper()} {contracts}x {occ_sym} "
                f"@ ${limit_price:.2f} | {signal.reason} | conf={signal.confidence:.0%}"
            )

            short_occ = short_entry = None
            if is_spread:
                # Debit spread — short leg is always a call for call spreads,
                # always a put for put spreads; infer from option_type
                spread_type = "put" if opt_type == "put" else "call"
                short_occ   = _alpaca_option_symbol(
                    signal.symbol, signal.expiry, spread_type, signal.spread_sell_strike
                )
                short_limit = round((signal.spread_sell_mid or 0) * 0.98, 2)
                try:
                    short_order = self.client.submit_order(LimitOrderRequest(
                        symbol=short_occ, qty=contracts, side=OrderSide.SELL,
                        type="limit", time_in_force=TimeInForce.DAY,
                        limit_price=short_limit,
                    ))
                    log.info(f"[OPTIONS] SPREAD SHORT: SELL {contracts}x {short_occ} @ ${short_limit:.2f}")
                    # Verify within 30s
                    time.sleep(30)
                    open_ids = {str(o.id) for o in (self.client.get_orders() or [])}
                    if str(short_order.id) in open_ids:
                        log.error(f"[OPTIONS] Spread short {short_occ} unfilled — cancelling")
                        for oid in (str(long_order.id), str(short_order.id)):
                            try: self.client.cancel_order_by_id(oid)
                            except Exception: pass
                        return False
                    short_entry = signal.spread_sell_mid
                except Exception as e:
                    log.warning(f"[OPTIONS] Spread short failed {short_occ}: {e} — cancelling long")
                    try: self.client.cancel_order_by_id(str(long_order.id))
                    except Exception: pass
                    return False

            ptype = PTYPE_SPREAD if is_spread else (PTYPE_COVERED if signal.action == "sell_to_open" else PTYPE_SINGLE)
            self._positions[occ_sym] = OptionsPosition(
                occ_symbol=occ_sym, symbol=signal.symbol,
                option_type=opt_type, action=signal.action,
                strike=signal.strike, expiry=signal.expiry,
                contracts=contracts, entry_price=signal.mid_price,
                strategy=signal.strategy, position_type=ptype,
                iv_rank_at_entry=float(signal.iv_rank),
                short_occ_symbol=short_occ,
                short_strike=signal.spread_sell_strike if is_spread else None,
                short_entry_price=short_entry,
            )
            return True

        except Exception as e:
            log.error(f"[OPTIONS] Order failed {occ_sym}: {e}")
            return False

    # ── _place_condor ─────────────────────────────────────────────────────────

    def _place_condor(self, signal, contracts: int) -> bool:
        """Submit all 4 iron condor legs; cancel all if any fail within 30s."""
        sym    = signal.symbol
        expiry = signal.expiry
        occ_pl = _alpaca_option_symbol(sym, expiry, "put",  signal.ic_put_long_strike)
        occ_ps = _alpaca_option_symbol(sym, expiry, "put",  signal.ic_put_short_strike)
        occ_cs = _alpaca_option_symbol(sym, expiry, "call", signal.ic_call_short_strike)
        occ_cl = _alpaca_option_symbol(sym, expiry, "call", signal.ic_call_long_strike)

        if occ_cs in self._positions or occ_ps in self._positions:
            log.info(f"[OPTIONS] Condor legs already held for {sym}")
            return False

        # Use short-call OCC as the primary key (it's the defining short leg)
        primary_occ = occ_cs

        leg_orders = {}
        # (occ, side, limit_price)
        leg_defs = [
            (occ_pl, OrderSide.BUY,  round(signal.ic_put_long_mid  * 1.04, 2)),
            (occ_ps, OrderSide.SELL, round(signal.ic_put_short_mid * 0.96, 2)),
            (occ_cs, OrderSide.SELL, round(signal.ic_call_short_mid* 0.96, 2)),
            (occ_cl, OrderSide.BUY,  round(signal.ic_call_long_mid * 1.04, 2)),
        ]
        try:
            for occ, side, lp in leg_defs:
                o = self.client.submit_order(LimitOrderRequest(
                    symbol=occ, qty=contracts, side=side,
                    type="limit", time_in_force=TimeInForce.DAY,
                    limit_price=lp,
                ))
                leg_orders[occ] = str(o.id)
                log.info(f"[OPTIONS] CONDOR LEG: {side.value.upper()} {contracts}x {occ} @ ${lp:.2f}")
        except Exception as e:
            log.error(f"[OPTIONS] Condor leg submission failed {sym}: {e} — cancelling all")
            for oid in leg_orders.values():
                try: self.client.cancel_order_by_id(oid)
                except Exception: pass
            return False

        # Verify all 4 filled within 30s
        time.sleep(30)
        open_ids = {str(o.id) for o in (self.client.get_orders() or [])}
        unfilled = [occ for occ, oid in leg_orders.items() if oid in open_ids]
        if unfilled:
            log.error(f"[OPTIONS] Condor {unfilled} unfilled after 30s — cancelling all legs")
            for oid in leg_orders.values():
                try: self.client.cancel_order_by_id(oid)
                except Exception: pass
            return False

        self._positions[primary_occ] = OptionsPosition(
            occ_symbol=primary_occ, symbol=sym,
            option_type="iron_condor", action="sell_to_open",
            strike=float(signal.ic_call_short_strike), expiry=expiry,
            contracts=contracts, entry_price=signal.mid_price,
            strategy=signal.strategy, position_type=PTYPE_CONDOR,
            iv_rank_at_entry=float(signal.iv_rank),
            legs=[
                {"occ": occ_pl, "side": "buy",  "ratio": 1},
                {"occ": occ_ps, "side": "sell", "ratio": 1},
                {"occ": occ_cs, "side": "sell", "ratio": 1},
                {"occ": occ_cl, "side": "buy",  "ratio": 1},
            ],
            ic_put_long_occ=occ_pl, ic_put_short_occ=occ_ps,
            ic_call_short_occ=occ_cs, ic_call_long_occ=occ_cl,
            ic_put_credit=signal.ic_put_credit,
            ic_call_credit=signal.ic_call_credit,
        )
        log.info(
            f"[OPTIONS] CONDOR OPEN: {sym} "
            f"{signal.ic_put_long_strike:.0f}/{signal.ic_put_short_strike:.0f}P "
            f"{signal.ic_call_short_strike:.0f}/{signal.ic_call_long_strike:.0f}C "
            f"net=${signal.mid_price:.2f} | conf={signal.confidence:.0%}"
        )
        return True

    # ── _place_butterfly ──────────────────────────────────────────────────────

    def _place_butterfly(self, signal, contracts: int) -> bool:
        """Submit all 3 butterfly legs (buy low, sell 2× mid, buy high)."""
        sym    = signal.symbol
        expiry = signal.expiry
        occ_low  = _alpaca_option_symbol(sym, expiry, "call", signal.bf_low_strike)
        occ_mid  = _alpaca_option_symbol(sym, expiry, "call", signal.strike)
        occ_high = _alpaca_option_symbol(sym, expiry, "call", signal.bf_high_strike)

        if occ_mid in self._positions:
            log.info(f"[OPTIONS] Butterfly mid leg {occ_mid} already held")
            return False

        leg_defs = [
            (occ_low,  OrderSide.BUY,  round(signal.bf_low_mid  * 1.04, 2), 1),
            (occ_mid,  OrderSide.SELL, round(signal.spread_sell_mid * 0.96, 2), 2),
            (occ_high, OrderSide.BUY,  round(signal.bf_high_mid * 1.04, 2), 1),
        ]
        leg_orders = {}
        try:
            for occ, side, lp, qty_ratio in leg_defs:
                o = self.client.submit_order(LimitOrderRequest(
                    symbol=occ, qty=contracts * qty_ratio, side=side,
                    type="limit", time_in_force=TimeInForce.DAY,
                    limit_price=lp,
                ))
                leg_orders[occ] = str(o.id)
                log.info(f"[OPTIONS] BUTTERFLY LEG: {side.value.upper()} {contracts*qty_ratio}x {occ} @ ${lp:.2f}")
        except Exception as e:
            log.error(f"[OPTIONS] Butterfly leg failed {sym}: {e} — cancelling all")
            for oid in leg_orders.values():
                try: self.client.cancel_order_by_id(oid)
                except Exception: pass
            return False

        # Verify all filled within 30s
        time.sleep(30)
        open_ids = {str(o.id) for o in (self.client.get_orders() or [])}
        if any(oid in open_ids for oid in leg_orders.values()):
            log.error(f"[OPTIONS] Butterfly legs unfilled — cancelling")
            for oid in leg_orders.values():
                try: self.client.cancel_order_by_id(oid)
                except Exception: pass
            return False

        self._positions[occ_mid] = OptionsPosition(
            occ_symbol=occ_mid, symbol=sym,
            option_type="call_butterfly", action="buy_to_open",
            strike=float(signal.strike), expiry=expiry,
            contracts=contracts, entry_price=signal.mid_price,
            strategy=signal.strategy, position_type=PTYPE_BUTTERFLY,
            iv_rank_at_entry=float(signal.iv_rank),
            legs=[
                {"occ": occ_low,  "side": "buy",  "ratio": 1},
                {"occ": occ_mid,  "side": "sell", "ratio": 2},
                {"occ": occ_high, "side": "buy",  "ratio": 1},
            ],
            bf_low_occ=occ_low, bf_high_occ=occ_high, bf_sell_occ=occ_mid,
            bf_max_profit=signal.bf_max_profit,
        )
        log.info(
            f"[OPTIONS] BUTTERFLY OPEN: {sym} "
            f"{signal.bf_low_strike:.0f}/{signal.strike:.0f}/{signal.bf_high_strike:.0f}C "
            f"debit=${signal.mid_price:.2f} max=${signal.bf_max_profit:.2f} | conf={signal.confidence:.0%}"
        )
        return True

    # ── _close_all_legs ───────────────────────────────────────────────────────

    def _close_all_legs(self, pos: 'OptionsPosition', urgency: str = "normal") -> None:
        """Close all legs of a multi-leg position by reversing each leg."""
        reverse = {"buy": OrderSide.SELL, "sell": OrderSide.BUY}
        for leg in pos.legs:
            occ  = leg["occ"]
            side = reverse[leg["side"]]
            qty  = pos.contracts * leg["ratio"]
            mid  = self._get_mid(occ)
            if mid > 0:
                mult = (0.90 if urgency == "stop" else 0.96) if side == OrderSide.SELL                   else (1.10 if urgency == "stop" else 1.04)
                req = LimitOrderRequest(
                    symbol=occ, qty=qty, side=side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(mid * mult, 2),
                )
            else:
                req = MarketOrderRequest(symbol=occ, qty=qty, side=side, time_in_force=TimeInForce.DAY)
            try:
                self.client.submit_order(req)
                log.info(f"[OPTIONS] LEG CLOSE {side.value.upper()} {qty}x {occ}")
            except Exception as e:
                log.error(f"[OPTIONS] Leg close failed {occ}: {e}")

    # ── Condor per-side close ─────────────────────────────────────────────────

    def _close_condor_side(self, pos: 'OptionsPosition', side: str, urgency: str) -> None:
        """Close one side (put or call) of an iron condor."""
        if side == "put":
            legs = [
                {"occ": pos.ic_put_short_occ, "side": "sell"},  # buy-to-close short put
                {"occ": pos.ic_put_long_occ,  "side": "buy"},   # sell-to-close long put
            ]
        else:
            legs = [
                {"occ": pos.ic_call_short_occ, "side": "sell"},
                {"occ": pos.ic_call_long_occ,  "side": "buy"},
            ]
        reverse = {"buy": OrderSide.SELL, "sell": OrderSide.BUY}
        for leg in legs:
            if not leg["occ"]:
                continue
            close_side = reverse[leg["side"]]
            mid = self._get_mid(leg["occ"])
            mult = (0.90 if urgency == "stop" else 0.96) if close_side == OrderSide.SELL               else (1.10 if urgency == "stop" else 1.04)
            req = LimitOrderRequest(
                symbol=leg["occ"], qty=pos.contracts, side=close_side,
                time_in_force=TimeInForce.DAY,
                limit_price=round(mid * mult, 2),
            ) if mid > 0 else MarketOrderRequest(
                symbol=leg["occ"], qty=pos.contracts,
                side=close_side, time_in_force=TimeInForce.DAY,
            )
            try:
                self.client.submit_order(req)
                log.info(f"[OPTIONS] CONDOR {side.upper()} SIDE CLOSE {close_side.value.upper()} {pos.contracts}x {leg['occ']}")
            except Exception as e:
                log.error(f"[OPTIONS] Condor side close failed {leg['occ']}: {e}")

    # ── Aggressive limit close ────────────────────────────────────────────────

    def _close_option(self, occ_sym: str, qty: Optional[int] = None, urgency: str = "normal") -> None:
        # Route multi-leg positions to their proper close method
        pos = self._positions.get(occ_sym)
        if pos and pos.position_type in (PTYPE_CONDOR, PTYPE_BUTTERFLY):
            self._close_all_legs(pos, urgency)
            del self._positions[occ_sym]
            return
        """Close an options position using an aggressive limit order.

        Never uses market orders — options spreads make market fills catastrophic.
        Prices limit at mid × multiplier:
          - normal: bid × 0.96 for sells, ask × 1.04 for buys
          - stop:   bid × 0.90 for sells, ask × 1.10 for buys (wider to ensure fill)
        Falls back to market ONLY if mid cannot be determined.
        """
        pos = self._positions.get(occ_sym)
        if pos is None:
            return

        close_qty = qty if qty is not None else pos.contracts
        side      = OrderSide.SELL if pos.action == "buy_to_open" else OrderSide.BUY

        mid = self._get_mid(occ_sym)
        if mid > 0:
            mult        = (0.90 if urgency == "stop" else 0.96) if side == OrderSide.SELL \
                     else (1.10 if urgency == "stop" else 1.04)
            limit_price = round(mid * mult, 2)
            order_req   = LimitOrderRequest(
                symbol=occ_sym, qty=close_qty, side=side,
                time_in_force=TimeInForce.DAY, limit_price=limit_price,
            )
            log.info(f"[OPTIONS] CLOSE {side.value.upper()} {close_qty}x {occ_sym} limit=${limit_price:.2f} ({urgency})")
        else:
            # No quote — market as last resort
            order_req = MarketOrderRequest(
                symbol=occ_sym, qty=close_qty, side=side,
                time_in_force=TimeInForce.DAY,
            )
            log.warning(f"[OPTIONS] CLOSE MARKET {occ_sym} — no quote available")

        try:
            self.client.submit_order(order_req)
        except Exception as e:
            log.error(f"[OPTIONS] Close failed {occ_sym}: {e}")
            return

        # Full close: remove from tracker and close spread short leg
        if qty is None or close_qty >= pos.contracts:
            if pos.short_occ_symbol:
                self._close_short_leg(pos.short_occ_symbol, pos.contracts, urgency)
            del self._positions[occ_sym]
        else:
            # Partial close: reduce contract count
            pos.contracts -= close_qty
            pos.partial_taken = True
            pos.entry_price   = 0.0   # cost basis zeroed — remainder is a free ride
            log.info(f"[OPTIONS] Partial close {close_qty}x {occ_sym} — {pos.contracts} contracts remain (free ride)")

    def _close_short_leg(self, short_occ: str, qty: int, urgency: str = "normal") -> None:
        """Buy-to-close the short leg of a spread."""
        mid = self._get_mid(short_occ)
        if mid > 0:
            mult  = 1.10 if urgency == "stop" else 1.04
            req   = LimitOrderRequest(
                symbol=short_occ, qty=qty, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY, limit_price=round(mid * mult, 2),
            )
        else:
            req = MarketOrderRequest(symbol=short_occ, qty=qty,
                                     side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        try:
            self.client.submit_order(req)
            log.info(f"[OPTIONS] SPREAD CLOSE SHORT BUY {qty}x {short_occ}")
        except Exception as e:
            log.error(f"[OPTIONS] Spread short-leg close failed {short_occ}: {e}")

    # ── P&L monitoring ────────────────────────────────────────────────────────

    # ── Per-type monitor helpers ──────────────────────────────────────────────

    def _monitor_condor(
        self, occ_sym: str, pos: "OptionsPosition",
        all_pos: dict, today: datetime.date,
    ) -> Optional[Tuple[str, str]]:
        """Iron condor: per-side stop at 2× credit, full close at 50% profit.
        Returns (action, urgency) or None. action: 'close_all'|'close_put'|'close_call'
        """
        dte = (pos.expiry - today).days
        if dte <= 5:
            log.info(f"[OPTIONS] Condor {occ_sym} DTE={dte} — theta risk, closing all")
            return ("close_all", "normal")

        # Fetch all 4 leg prices
        leg_prices = {}
        for leg in pos.legs:
            lp = all_pos.get(leg["occ"])
            leg_prices[leg["occ"]] = float(lp.current_price) if lp else 0.0

        put_short_price  = leg_prices.get(pos.ic_put_short_occ,  0.0)
        put_long_price   = leg_prices.get(pos.ic_put_long_occ,   0.0)
        call_short_price = leg_prices.get(pos.ic_call_short_occ, 0.0)
        call_long_price  = leg_prices.get(pos.ic_call_long_occ,  0.0)

        # Cost-to-close each spread (positive = debit to close)
        put_close_cost  = put_short_price  - put_long_price   # buy short, sell long
        call_close_cost = call_short_price - call_long_price

        put_credit  = pos.ic_put_credit  or 0.001
        call_credit = pos.ic_call_credit or 0.001

        # Per-side stop: if cost to close > 2× original credit, the spread is tested
        put_tested  = put_close_cost  >= 2.0 * put_credit
        call_tested = call_close_cost >= 2.0 * call_credit

        if put_tested and call_tested:
            log.warning(f"[OPTIONS] Condor {occ_sym} both sides tested — closing all (double breach)")
            return ("close_all", "stop")
        if call_tested:
            log.warning(f"[OPTIONS] Condor {occ_sym} call side tested (close=${call_close_cost:.2f} vs credit=${call_credit:.2f}) — closing call spread")
            return ("close_call", "stop")
        if put_tested:
            log.warning(f"[OPTIONS] Condor {occ_sym} put side tested (close=${put_close_cost:.2f} vs credit=${put_credit:.2f}) — closing put spread")
            return ("close_put", "stop")

        # Profit target: close whole condor when buy-back costs 50% of original credit
        total_credit     = put_credit + call_credit
        total_close_cost = put_close_cost + call_close_cost
        if total_close_cost <= total_credit * 0.50:
            log.info(f"[OPTIONS] Condor {occ_sym} 50% profit target reached — closing")
            return ("close_all", "normal")

        return None

    def _monitor_butterfly(
        self, occ_sym: str, pos: "OptionsPosition",
        all_pos: dict, today: datetime.date,
    ) -> Optional[str]:
        """Butterfly: close at 60% of max profit or DTE ≤ 7. No time-based flat exit.
        Returns urgency if should close, else None.
        """
        dte = (pos.expiry - today).days
        if dte <= 7:
            log.info(f"[OPTIONS] Butterfly {occ_sym} DTE={dte} — take what we have, closing")
            return "normal"

        # Compute net current value: low + high legs - 2× mid
        low_p  = float(all_pos[pos.bf_low_occ].current_price)  if pos.bf_low_occ  and all_pos.get(pos.bf_low_occ)  else 0.0
        mid_p  = float(all_pos[pos.bf_sell_occ].current_price) if pos.bf_sell_occ and all_pos.get(pos.bf_sell_occ) else 0.0
        high_p = float(all_pos[pos.bf_high_occ].current_price) if pos.bf_high_occ and all_pos.get(pos.bf_high_occ) else 0.0
        net_val = low_p + high_p - 2 * mid_p

        max_profit = pos.bf_max_profit or 1.0
        net_debit  = pos.entry_price

        # Near-total loss stop: close if current value < 20% of net debit paid
        # (use time-weighted: only fire after 40% of DTE elapsed)
        days_held = (today - pos.entered_at).days
        total_dte = max((pos.expiry - pos.entered_at).days, 1)
        if days_held >= total_dte * 0.4 and net_val < net_debit * 0.20:
            log.warning(f"[OPTIONS] Butterfly {occ_sym} near-total loss (val=${net_val:.2f} vs debit=${net_debit:.2f}) — closing")
            return "stop"

        # Profit target: 60% of max profit
        profit_of_max = (net_val - net_debit) / max_profit * 100
        if profit_of_max >= 60.0:
            log.info(f"[OPTIONS] Butterfly {occ_sym} {profit_of_max:.0f}% of max profit — closing")
            return "normal"

        return None

    def monitor_positions(self) -> None:
        """Evaluate all open positions and apply per-type SL/TP logic.

        Dispatches by position_type:
          single / spread → directional P&L with IV-scaled TP and scaled trail
          condor          → per-side 2× credit stop, 50% premium decay target
          butterfly       → time-weighted stop, 60% of max profit target
          covered         → 50% premium decay, DTE ≤ 5
        """
        if not self._positions:
            return

        try:
            all_positions = {p.symbol: p for p in self.client.get_all_positions()}
        except Exception as e:
            log.warning(f"[OPTIONS] Monitor: position fetch failed: {e}")
            return

        to_close_full:    List[Tuple[str, str]]  = []   # (occ_sym, urgency)
        to_close_partial: List[str]              = []
        stop_symbols:     List[str]              = []
        today = datetime.date.today()

        for occ_sym, pos in list(self._positions.items()):
            dte = (pos.expiry - today).days

            # ── 1. Expiry risk ────────────────────────────────────────────────
            if dte <= 1:
                log.warning(f"[OPTIONS] {occ_sym} DTE={dte} — closing to avoid expiry")
                to_close_full.append((occ_sym, "normal"))
                continue

            # ── Per-type dispatch ─────────────────────────────────────────────
            if pos.position_type == PTYPE_CONDOR:
                result = self._monitor_condor(occ_sym, pos, all_positions, today)
                if result:
                    action, urgency = result
                    if action == "close_all":
                        to_close_full.append((occ_sym, urgency))
                    elif action == "close_call":
                        self._close_condor_side(pos, "call", urgency)
                    elif action == "close_put":
                        self._close_condor_side(pos, "put", urgency)
                continue

            if pos.position_type == PTYPE_BUTTERFLY:
                urgency = self._monitor_butterfly(occ_sym, pos, all_positions, today)
                if urgency:
                    to_close_full.append((occ_sym, urgency))
                    _record_result(pos.strategy, urgency == "normal")
                continue

            # ── Fetch current price (single/spread/covered) ───────────────────
            ap = all_positions.get(occ_sym)
            if ap is None:
                log.info(f"[OPTIONS] {occ_sym} no longer in positions — removing")
                del self._positions[occ_sym]
                continue

            try:
                current_price = float(ap.current_price)
                entry_price   = pos.entry_price
                if entry_price <= 0:
                    continue

                # ── P&L calculation ───────────────────────────────────────────
                if pos.action == "buy_to_open":
                    if pos.short_occ_symbol and pos.short_entry_price:
                        short_ap = all_positions.get(pos.short_occ_symbol)
                        if short_ap is not None:
                            net_current = current_price - float(short_ap.current_price)
                            pnl_pct     = (net_current - entry_price) / entry_price * 100
                        else:
                            # Short leg gone — recalculate as naked long vs full original cost
                            long_cost = entry_price + (pos.short_entry_price or 0)
                            pnl_pct   = (current_price - long_cost) / long_cost * 100
                            log.warning(
                                f"[OPTIONS] {occ_sym}: short leg {pos.short_occ_symbol} missing — "
                                f"recalculating as naked long (cost=${long_cost:.2f})"
                            )
                    else:
                        pnl_pct = (current_price - entry_price) / entry_price * 100

                    # Update peak
                    if pnl_pct > pos.peak_pnl_pct:
                        pos.peak_pnl_pct = pnl_pct

                    # ── 2. Stop loss ──────────────────────────────────────────
                    stop_thresh = -OPTIONS_STOP_LOSS_PCT
                    if pnl_pct <= stop_thresh:
                        log.warning(f"[OPTIONS] {occ_sym} stop loss {pnl_pct:.1f}% — closing")
                        to_close_full.append((occ_sym, "stop"))
                        stop_symbols.append(pos.symbol)
                        _record_result(pos.strategy, False)
                        continue

                    # ── 3. Partial profit at +50% ─────────────────────────────
                    if (not pos.partial_taken and pos.contracts > 1
                            and pnl_pct >= 50.0):
                        log.info(f"[OPTIONS] {occ_sym} +{pnl_pct:.1f}% — partial close (half)")
                        to_close_partial.append(occ_sym)

                    # ── 4. IV-scaled profit target ────────────────────────────
                    profit_target = _iv_profit_target(pos.iv_rank_at_entry)
                    if pos.short_occ_symbol:
                        profit_target = min(profit_target, 60.0)  # spread: cap at 60%
                    if pnl_pct >= profit_target:
                        log.info(f"[OPTIONS] {occ_sym} profit target +{pnl_pct:.1f}% (target={profit_target:.0f}%) — closing")
                        to_close_full.append((occ_sym, "normal"))
                        _record_result(pos.strategy, True)
                        continue

                    # ── 5. IV spike exit ──────────────────────────────────────
                    # Close calls when IV rank surges 30+ points post-entry and we're in profit
                    if pos.option_type == "call" and pnl_pct >= 15.0:
                        try:
                            from .strategies import _get_options_chain
                            chain = _get_options_chain(pos.symbol)
                            if chain and chain.iv_rank >= pos.iv_rank_at_entry + 30:
                                log.info(
                                    f"[OPTIONS] {occ_sym} IV spike exit: rank {pos.iv_rank_at_entry:.0f} → "
                                    f"{chain.iv_rank:.0f} at +{pnl_pct:.1f}%"
                                )
                                to_close_full.append((occ_sym, "normal"))
                                _record_result(pos.strategy, True)
                                continue
                        except Exception:
                            pass

                    # ── 6. Scaled trailing stop ───────────────────────────────
                    trail = _trail_drawdown(pos.peak_pnl_pct)
                    if pos.peak_pnl_pct >= 20.0 and pnl_pct <= pos.peak_pnl_pct - trail:
                        log.info(
                            f"[OPTIONS] {occ_sym} trailing stop: peak={pos.peak_pnl_pct:.1f}% "
                            f"now={pnl_pct:.1f}% trail={trail:.0f}pp — closing"
                        )
                        to_close_full.append((occ_sym, "normal"))
                        _record_result(pos.strategy, pnl_pct > 0)
                        continue

                    # ── 7. Time-based exit for flat positions ─────────────────
                    days_held = (today - pos.entered_at).days
                    total_dte = max((pos.expiry - pos.entered_at).days, 1)
                    if days_held >= total_dte // 2 and -10.0 <= pnl_pct <= 10.0:
                        log.info(
                            f"[OPTIONS] {occ_sym} time exit: flat {pnl_pct:.1f}% "
                            f"after {days_held}/{total_dte} days"
                        )
                        to_close_full.append((occ_sym, "normal"))
                        _record_result(pos.strategy, False)
                        continue

                else:
                    # ── sell_to_open (covered call) ───────────────────────────
                    decay_pct = (entry_price - current_price) / entry_price * 100
                    if decay_pct >= 50.0 or dte <= 5:
                        log.info(
                            f"[OPTIONS] Covered call {occ_sym} decay={decay_pct:.0f}% DTE={dte} — closing"
                        )
                        to_close_full.append((occ_sym, "normal"))
                        _record_result(pos.strategy, True)

            except Exception as e:
                log.debug(f"[OPTIONS] Monitor error {occ_sym}: {e}")

        # Execute partial closes first (before full closes change the position state)
        for occ_sym in to_close_partial:
            if occ_sym not in {s for s, _ in to_close_full}:
                pos = self._positions.get(occ_sym)
                if pos:
                    self._close_option(occ_sym, qty=pos.contracts // 2, urgency="normal")

        # Execute full closes
        closed = set()
        for occ_sym, urgency in to_close_full:
            if occ_sym not in closed:
                self._close_option(occ_sym, urgency=urgency)
                closed.add(occ_sym)

        # Record stop cooldowns
        for underlying in stop_symbols:
            record_stop_cooldown(underlying)

    # ── Emergency close ───────────────────────────────────────────────────────

    def close_all(self) -> None:
        """Emergency: close all open options positions immediately."""
        for occ_sym in list(self._positions.keys()):
            self._close_option(occ_sym, urgency="stop")

    # ── Status ────────────────────────────────────────────────────────────────

    def status_summary(self) -> str:
        if not self._positions:
            return "Options: no open positions"
        today = datetime.date.today()
        lines = [f"Options: {len(self._positions)} position(s)"]
        for occ_sym, pos in self._positions.items():
            dte  = (pos.expiry - today).days
            flag = " [partial]" if pos.partial_taken else ""
            lines.append(
                f"  {occ_sym} | {pos.contracts}x {pos.strategy} "
                f"entry=${pos.entry_price:.2f} DTE={dte} peak={pos.peak_pnl_pct:.0f}%{flag}"
            )
        return "\n".join(lines)

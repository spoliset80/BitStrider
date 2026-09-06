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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from types import SimpleNamespace
from typing import Any, Optional, Dict, Tuple, Deque
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
    PDT_ACCOUNT_MIN, PDT_MAX_TRADES, MIN_EQUITY_FOR_SHORT,
    MAX_POSITIONS,
    SWAP_ON_FULL,
    SWAP_MIN_CONFIDENCE,
    POLLER_CHECK_WORKERS, PROTECTION_LIMIT_REPLACE_AFTER_SEC,
    EXTENDED_HOURS,
    USE_DYNAMIC_TIERS,
    USE_RISK_EQUALIZED_SIZING,
    USE_VIX_ROC_FILTER,
    MIN_BUYING_POWER_PCT, MIN_POSITION_DOLLARS, PDT_WARN_AT_REMAINING,
    TAKE_PROFIT_NORMAL, TAKE_PROFIT_HIGH, STOP_LOSS_PCT,
    ATR_TP_RATIO, MAX_SHORT_FLOAT_PCT, HIGH_SHORT_FLOAT_STOCKS, is_high_short_float,
    EOD_CLOSE_ENABLED, EOD_CLOSE_TIME, EOD_CLOSE_STRATEGIES, MARKET_CLOSE,
    GUARDRAIL_EOD_CLOSE_ENABLED, GUARDRAIL_EOD_CLOSE_TIME,
    PRICE_DRIFT_STOP_ENABLED, PRICE_DRIFT_STOP_PCT,
    PRICE_DRIFT_CHECK_INTERVAL_MIN, PRICE_DRIFT_LOOKBACK_MIN,
    TRAIL_STOP_PCT, PROFIT_TRAIL_GIVEBACK_PCT,
    ATR_TRAIL_ENABLED, ATR_TRAIL_PERIOD, ATR_TRAIL_MULTIPLIER, ATR_TRAIL_MAX_PCT,
    STAGNANT_STOP_ENABLED, STAGNANT_STOP_CHECK_INTERVAL_MIN, ENTRY_WINDOW_END_ET,
    ENTRY_WINDOW_BREAK_START_ET, ENTRY_WINDOW_BREAK_END_ET,
    LUNCH_FLAT_ENABLED, LUNCH_FLAT_TIME_ET,
    EMA_TREND_FILTER_ENABLED, EMA_TREND_MIN_BARS, EMA9_TRAIL_PCT,
    EMA_ENTRY_CONFIRM_SEC, EMA_ENTRY_CONFIRM_CHECKS,
    EMA_ENTRY_MIN_SPREAD_PCT, EMA_ENTRY_MIN_TRAILING_30M_RETURN_PCT,
    REENTRY_SIZE_REDUCTION_PCT, LOSS_BLOCK_MORNING_END_ET, SYMBOL_DAILY_LOSS_BLOCK_COUNT,
    SWING_DRIFT_STOP_ENABLED, SWING_DRIFT_STOP_PCT,
    MIN_AVG_DAILY_VOLUME_REGULAR_HOURS, MIN_FLOAT_SHARES, MIN_MARKET_CAP,
    SWING_STALE_EXIT_ENABLED, SWING_STALE_DAYS, SWING_STALE_MIN_GAIN_PCT,
    NO_GAIN_EXIT_ENABLED, NO_GAIN_EXIT_HOURS, NO_GAIN_EXIT_MIN_PCT, NO_GAIN_EXIT_MAX_LOSS_PCT,
    MFE_GIVEBACK_ENABLED, MFE_ARM_PROFIT_PCT, MFE_GIVEBACK_FRACTION, MFE_BREAKEVEN_FLOOR_PCT,
    AFTERHOURS_STOP_CHECK_ENABLED, AFTERHOURS_CHASE_STALE_SECONDS,
    CLOSE_RECONCILIATION_ENABLED, CLOSE_CANCEL_CONFIRM_TIMEOUT_SEC, CLOSE_CANCEL_CONFIRM_POLL_SEC,
    MAX_POSITION_CONCENTRATION_PCT, CORRELATION_GROUPS, MAX_PORTFOLIO_LEVERAGE,
    POSITION_CAP_GROWTH_FACTOR, POSITION_CAP_ABSOLUTE_MAX_PCT,
    LONG_ONLY_MODE,
    STALE_ORDER_MINUTES, STALE_ORDER_MINUTES_INTRADAY,
    KILL_MODE_TRAIL_PCT,
    SMALL_ACCOUNT_EQUITY_THRESHOLD, SMALL_ACCOUNT_MAX_POSITIONS,
    SMALL_ACCOUNT_MIN_POSITION_DOLLARS,
    POSITION_SIZE_PCT, SMALL_ACCOUNT_POSITION_SIZE_PCT,
    CONF_SCALE_MIN_MULT, CONF_SCALE_FULL_CONF,
    MAX_POSITION_SIZE_PCT,
    STRATEGY_KELLY_MULT, STRATEGY_KELLY_MULT_DEFAULT,
    THIN_LIQUIDITY_EXCLUDED_STRATEGIES,
    CONF_RATCHET_ENABLED, CONF_RATCHET_TRIGGER_GAIN_PCT, CONF_RATCHET_MAX_TIGHTEN,
    MOMENTUM_FRESHNESS_ENABLED, MOMENTUM_FRESHNESS_STRATEGIES,
    TRADE_STALE_MOMENTUM_REJECTS,
    MOMENTUM_FRESHNESS_LOOKBACK_MIN, MOMENTUM_FRESHNESS_MAX_PULLBACK_PCT,
    THIN_LIQUIDITY_POSITION_SIZE_PCT,
    THIN_LIQUIDITY_TRAILING_STOP_MULT,
    MARKETABLE_LIMIT_BUFFER_PCT,
    FADED_ENTRY_PASSIVE_WINDOW_SECONDS, FADED_ENTRY_CEILING_TIMEOUT_SECONDS,
    REENTRY_TRAIL_PCT, DUPLICATE_ENTRY_BLOCK_SECONDS,
    STAGED_ALLOCATION_ENABLED, STAGED_ALLOCATION_TRANCHES, STAGED_ALLOCATION_MIN_GAIN_PCT, STAGED_ALLOCATION_MAX_ADD_PCT,
    LIVE,
)
from engine.equity.strategies import Signal, _get_float_shares, _get_market_cap
from engine.equity.scan import get_scan_targets as _get_scan_targets
from engine.utils import MarketState, calculate_risk_adjusted_size, check_vix_roc_filter, get_dynamic_tier, calculate_atr, in_lunch_break
from engine.utils.bars import get_bars, get_daily_volume_bars, get_premarket_bars, is_dead_ticker
from engine.never_trade import is_never_trade
from engine.notifications.notifications import send_email
# Submodule object (not the package alias -- see orchestrator.py's 2026-08-18
# comment on why `from . import session` freezes a value copy and breaks the
# daily-loss halt). Used by _entry_halt_active so re-entry paths honor the
# daily-loss limit (2026-09-02: previously skipped entirely on re-entries).
from engine.session import session as _session
from engine.telemetry import log_event as _telemetry_log

log = logging.getLogger("ApexTrader")


# ----------------------------------------------------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------------------------------------------------
def ratchet_scale(confidence: float) -> float:
    """Pure math for the confidence-ratchet trailing-stop multiplier.
    confidence <= SWAP_MIN_CONFIDENCE (0.75) -> 1.0 (no tightening).
    confidence == 1.0                        -> CONF_RATCHET_MAX_TIGHTEN (max tightening).
    Linear in between. See ratchet_confident_winners() for where this is used
    and CONFIG.md / config.py for the constants' rationale."""
    if confidence <= SWAP_MIN_CONFIDENCE:
        return 1.0
    span = max(1e-6, 1.0 - SWAP_MIN_CONFIDENCE)
    frac = min(1.0, (confidence - SWAP_MIN_CONFIDENCE) / span)
    return 1.0 - frac * (1.0 - CONF_RATCHET_MAX_TIGHTEN)


def _check_momentum_freshness(signal: Signal) -> Tuple[bool, Optional[str]]:
    """Reject a gap/momentum signal (MOMENTUM_FRESHNESS_STRATEGIES) if price
    has already faded MOMENTUM_FRESHNESS_MAX_PULLBACK_PCT+ off its high over
    the last MOMENTUM_FRESHNESS_LOOKBACK_MIN minutes -- the move may already
    be rolling over by the time the order is about to submit, seconds to
    minutes after the strategy detected it. See engine/config.py for the
    full reasoning and known limitations (sharp reversals only, not gradual
    multi-hour fades).

    Returns (fresh, reject_reason). fresh=True with no reason for any
    strategy not in MOMENTUM_FRESHNESS_STRATEGIES, or when there isn't
    enough recent bar data to judge -- never blocks on missing data.
    """
    if not MOMENTUM_FRESHNESS_ENABLED or signal.strategy not in MOMENTUM_FRESHNESS_STRATEGIES:
        return True, None
    bars = get_bars(signal.symbol, period="1d", interval="1m")
    if bars.empty or "high" not in bars.columns or "close" not in bars.columns:
        return True, None
    recent = bars.tail(MOMENTUM_FRESHNESS_LOOKBACK_MIN)
    recent_high = float(recent["high"].max())
    current_price = float(bars["close"].iloc[-1])
    if recent_high <= 0:
        return True, None
    pullback_pct = (recent_high - current_price) / recent_high * 100
    if pullback_pct > MOMENTUM_FRESHNESS_MAX_PULLBACK_PCT:
        return False, (
            f"{signal.symbol}: faded {pullback_pct:.1f}% off its {MOMENTUM_FRESHNESS_LOOKBACK_MIN}-min "
            f"high (${recent_high:.2f} -> ${current_price:.2f}) -- {signal.strategy} entry not fresh"
        )
    return True, None


def _entry_gate_bars(symbol: str, force_fresh: bool = False):
    """Return premarket-inclusive 1m bars when needed so 09:30 entries can validate."""
    bars = get_bars(symbol, period="1d", interval="1m", bypass_cache=force_fresh)
    if bars.empty or "close" not in bars.columns or len(bars) < 62:
        premarket = get_premarket_bars(symbol)
        if not premarket.empty and "close" in premarket.columns and len(premarket) > len(bars):
            bars = premarket
    if not bars.empty and "time" in bars.columns:
        try:
            bars = bars.sort_values("time")
        except Exception:
            pass
    return bars


def _closed_1m_bars(bars):
    """Keep only closed 1-minute candles; fall back to provider rows as closed if no time column."""
    if bars.empty or "time" not in bars.columns:
        return bars
    try:
        eastern = datetime.datetime.now(datetime.timezone.utc).astimezone().tzinfo
        import pytz as _pytz
        eastern = _pytz.timezone("America/New_York")
        now_et = datetime.datetime.now(eastern)
        current_minute = now_et.replace(second=0, microsecond=0)

        def _to_et(value):
            ts = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
            if isinstance(ts, str):
                ts = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if getattr(ts, "tzinfo", None) is None:
                ts = eastern.localize(ts)
            return ts.astimezone(eastern)

        et_times = bars["time"].apply(_to_et)
        closed = bars[et_times < current_minute]
        return closed if not closed.empty else bars.iloc[:0]
    except Exception:
        # Better to use the provider's completed rows than fail-open. If they are
        # stale/empty, the checks below still block.
        return bars


def _entry_trend_snapshot(signal: Signal, is_long: bool, force_fresh: bool = False) -> Tuple[bool, str]:
    """Full hard entry trend gate using two closed candles and recent 30m momentum."""
    bars = _closed_1m_bars(_entry_gate_bars(signal.symbol, force_fresh=force_fresh))
    high_col = "high" if "high" in bars.columns else "close"
    if bars.empty or "close" not in bars.columns or high_col not in bars.columns or len(bars) < 62:
        return False, f"{signal.symbol}: EMA/momentum trend unavailable (need 62 closed 1-min bars)"

    closes = bars["close"].astype(float).reset_index(drop=True)
    highs = bars[high_col].astype(float).reset_index(drop=True)
    ema7 = closes.ewm(span=7, adjust=False).mean()
    ema15 = closes.ewm(span=15, adjust=False).mean()

    details = []
    for idx in (len(closes) - 2, len(closes) - 1):
        close_now = float(closes.iloc[idx])
        ema7_now = float(ema7.iloc[idx])
        ema15_now = float(ema15.iloc[idx])
        ema7_delta = float(ema7.iloc[idx] - ema7.iloc[idx - 1])
        spread_pct = ((ema7_now - ema15_now) / ema15_now * 100.0) if ema15_now else 0.0
        ret30_pct = ((close_now - float(closes.iloc[idx - 30])) / float(closes.iloc[idx - 30]) * 100.0) if float(closes.iloc[idx - 30]) else 0.0
        recent_high = float(highs.iloc[idx - 29:idx + 1].max())
        previous_high = float(highs.iloc[idx - 59:idx - 29].max())

        if is_long:
            ok = (
                ema7_now > ema15_now
                and spread_pct >= EMA_ENTRY_MIN_SPREAD_PCT
                and ema7_delta > 0
                and close_now >= ema7_now
                and ret30_pct > EMA_ENTRY_MIN_TRAILING_30M_RETURN_PCT
                and recent_high > previous_high
            )
            need = (
                f"need long EMA7>EMA15 spread>={EMA_ENTRY_MIN_SPREAD_PCT:.2f}%, "
                f"EMA7 delta>0, price>=EMA7, trailing30>{EMA_ENTRY_MIN_TRAILING_30M_RETURN_PCT:.2f}%, "
                "recent30 high > prior30 high"
            )
        else:
            ok = (
                ema7_now < ema15_now
                and spread_pct <= -EMA_ENTRY_MIN_SPREAD_PCT
                and ema7_delta < 0
                and close_now <= ema7_now
                and ret30_pct < -EMA_ENTRY_MIN_TRAILING_30M_RETURN_PCT
                and recent_high < previous_high
            )
            need = (
                f"need short EMA7<EMA15 spread<=-{EMA_ENTRY_MIN_SPREAD_PCT:.2f}%, "
                f"EMA7 delta<0, price<=EMA7, trailing30<-{EMA_ENTRY_MIN_TRAILING_30M_RETURN_PCT:.2f}%, "
                "recent30 high < prior30 high"
            )

        detail = (
            f"candle {idx - len(closes) + 1}/0 close ${close_now:.2f}, "
            f"EMA7 ${ema7_now:.2f}, EMA15 ${ema15_now:.2f}, spread {spread_pct:+.3f}%, "
            f"EMA7 delta {ema7_delta:+.4f}, trailing30 {ret30_pct:+.3f}%, "
            f"recent30 high ${recent_high:.2f} vs prior30 high ${previous_high:.2f}"
        )
        details.append(detail)
        if not ok:
            return False, f"{signal.symbol}: {detail} ({need}) -- trend not aligned with the {'long' if is_long else 'short'} entry"

    return True, f"{signal.symbol}: EMA ENTRY PASS -- " + " | ".join(details)


def _check_ema_trend_alignment(signal: Signal, is_long: bool, force_fresh: bool = False) -> Tuple[bool, Optional[str]]:
    """EMA7 slope + EMA7-vs-EMA15 crossover alignment gate for a fresh entry.

    This is the per-minute recheck gate used by check_pending_entries_ema /
    check_blocked_entries_ema / _maybe_rearm_reentry. It deliberately stays on
    the lighter slope+crossover reading (works from EMA_TREND_MIN_BARS bars)
    rather than the full 62-bar _entry_trend_snapshot: the signal-time entry
    gate in _validate_trade already applied the strict snapshot, and these
    per-minute paths just need to notice a trend turn, fast, from whatever
    bars are currently available.

    Missing/insufficient bar data (fewer than EMA_TREND_MIN_BARS of 1-min
    history) blocks the recheck. EMA alignment is required, so an unavailable
    reading must not be treated as approval.

    force_fresh=True bypasses get_bars()'s per-scan-cycle cache (see its
    2026-08-27 docstring update). Pass it from any per-minute recheck running
    on the SoftwareStopPoller thread -- that thread's whole job is noticing a
    trend change since the last check, on a different cadence than the equity
    scan that owns this cache; reading the scan's stale snapshot there defeats
    the recheck.
    """
    bars = get_bars(signal.symbol, period="1d", interval="1m", bypass_cache=force_fresh)
    if bars.empty or "close" not in bars.columns or len(bars) < EMA_TREND_MIN_BARS:
        return False, (
            f"{signal.symbol}: EMA trend unavailable "
            f"(need {EMA_TREND_MIN_BARS} 1-min bars)"
        )
    closes     = bars["close"]
    ema7       = closes.ewm(span=7, adjust=False).mean()
    ema7_delta = float(ema7.diff().iloc[-1])
    ema7_now   = float(ema7.iloc[-1])
    ema15_now  = float(closes.ewm(span=15, adjust=False).mean().iloc[-1])
    slope_aligned = (ema7_delta > 0) if is_long else (ema7_delta < 0)
    crossover_aligned = (ema7_now > ema15_now) if is_long else (ema7_now < ema15_now)
    if not (slope_aligned and crossover_aligned):
        return False, (
            f"{signal.symbol}: EMA7 delta {ema7_delta:+.4f} / "
            f"EMA7 ${ema7_now:.2f} vs EMA15 ${ema15_now:.2f} "
            f"(need {'rising delta + EMA7 above EMA15' if is_long else 'falling delta + EMA7 below EMA15'}) "
            f"-- trend not aligned with the {'long' if is_long else 'short'} entry"
        )
    return True, None

def _resolve_freshness_reject(signal: Signal, fresh: bool, fade_reason: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Decide what _validate_trade does with a _check_momentum_freshness
    result: (valid, block_reason). fresh=True -> always valid, signal
    untouched. fresh=False -> hard-blocked (valid=False, block_reason=
    fade_reason) unless TRADE_STALE_MOMENTUM_REJECTS, in which case the
    signal is flagged thin_liquidity=True (same reduced sizing as a
    guardrail admit, see _apply_thin_liquidity_override) and treated as
    valid so it still trades -- UNLESS signal.strategy is in
    THIN_LIQUIDITY_EXCLUDED_STRATEGIES (2026-08-15: ORB/GapBreakout,
    measured net-negative specifically on their bypass trades), in which
    case it's hard-blocked regardless of the toggle. Split out for
    unit-testability without a broker/bars connection -- mutates signal in
    place same as the inline version would, callers pass their own Signal
    instance."""
    if fresh:
        return True, None
    if not TRADE_STALE_MOMENTUM_REJECTS or signal.strategy in THIN_LIQUIDITY_EXCLUDED_STRATEGIES:
        return False, fade_reason
    signal.thin_liquidity = True
    signal.stale_entry = True  # narrower flag -- see Signal.stale_entry docstring in
                                # strategies.py. Only THIS path sets it; the guardrail-
                                # floor admit in scan.py sets thin_liquidity alone.
    return True, None


def _entry_rechase_slip_pct(chase_count: int) -> float:
    """Next slip% for an entry re-chase attempt (_sweep_pending_entries) --
    starts beyond the original MARKETABLE_LIMIT_BUFFER_PCT bound and widens
    each retry, capped at 3% same as every other re-chase path in this file
    (_sweep_force_closes, check_afterhours_stops, close_no_gain_positions)."""
    return min(MARKETABLE_LIMIT_BUFFER_PCT * (chase_count + 2), 3.0)


def _marketable_limit_price(price: float, is_long: bool, buffer_pct: float = MARKETABLE_LIMIT_BUFFER_PCT) -> float:
    """A limit price just past the reference price -- fills like a market
    order under normal conditions, but caps the worst case at buffer_pct
    instead of a plain market order absorbing an unbounded bid-ask spread.
    is_long=True (buying, or covering a short) rounds UP by buffer_pct;
    False (selling, or opening a short) rounds DOWN."""
    adj = 1 + buffer_pct / 100 if is_long else 1 - buffer_pct / 100
    return round(price * adj, 2)


def _live_quote_mid(client, symbol: str, fallback: float) -> float:
    """Live bid/ask midpoint -- the reference _marketable_limit_price should
    bound against, instead of the scan-time signal.price or a possibly-stale
    pos.current_price. By the time an order reaches the broker, the scan
    that produced the reference price can be seconds to minutes old (scan
    cadence, MAX_SIGNALS_PER_CYCLE throttling); bounding "within 1%" of a
    stale number defeats the point. Falls back to `fallback` if the quote
    call fails or either side is missing/non-positive -- same defensive
    pattern as the stale-order requote path in detect_stopped_out_positions."""
    try:
        q = client.get_latest_quote(symbol)
        bid = float(getattr(q, "bid_price", 0) or 0)
        ask = float(getattr(q, "ask_price", 0) or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
    except Exception:
        pass
    return fallback


# ----------------------------------------------------------------------------------------------------------------------------
# Order-state normalization & classification (2026-09-03, SNOW post-mortem).
# A symbol having "some open order" is NOT proof of protection: SNOW's 1-share
# long sat behind 9 rejected software-stop closes (Alpaca 40310000
# "held_for_orders") because a GTC trailing stop reserved the only share, and the
# exit paths blind-cancelled + slept 0.4s + closed, racing the broker's cancel
# processing. These helpers let close paths see WHAT each order is before acting.
# ----------------------------------------------------------------------------------------------------------------------------

# Broker statuses under which an order can still act (fill or block quantity).
# "pending_cancel" stays in here deliberately: until the broker confirms the
# cancel, the quantity is still reserved.
_ACTIVE_ORDER_STATUSES = {
    "new", "accepted", "pending_new", "pending_replace", "accepted_for_bidding",
    "partially_filled", "held", "pending_cancel", "done_for_day_pending_cancel",
}


@dataclass(frozen=True)
class ActiveOrderView:
    """Normalized, failure-tolerant snapshot of one broker order."""
    order_id: str
    symbol: str
    side: str            # "buy" / "sell" (lowercased, enum-tolerant)
    order_type: str      # e.g. "trailing_stop", "limit", "market"
    time_in_force: str   # "gtc" / "day" / ...
    status: str          # lowercase broker status
    qty: float
    filled_qty: float
    client_order_id: str

    @property
    def remaining_qty(self) -> float:
        return max(0.0, self.qty - self.filled_qty)

    @property
    def is_active(self) -> bool:
        return self.status in _ACTIVE_ORDER_STATUSES


def _normalize_order_view(order: Any) -> Optional[ActiveOrderView]:
    """Tolerate alpaca models AND the minimal SimpleNamespace stubs used by the
    test suite: any missing/unparsable field degrades to a safe default rather
    than raising. Returns None only when there's no usable id/symbol."""
    def _low(v: Any) -> str:
        return str(getattr(v, "value", v) or "").lower()

    try:
        oid = str(getattr(order, "id", "") or "")
        sym = str(getattr(order, "symbol", "") or "")
        if not oid or not sym:
            return None
        return ActiveOrderView(
            order_id=oid,
            symbol=sym,
            side=_low(getattr(order, "side", "")),
            order_type=_low(getattr(order, "order_type", "")),
            time_in_force=_low(getattr(order, "time_in_force", "")),
            status=_low(getattr(order, "status", "")),
            qty=float(getattr(order, "qty", 0) or 0),
            filled_qty=float(getattr(order, "filled_qty", 0) or 0),
            client_order_id=str(getattr(order, "client_order_id", "") or ""),
        )
    except Exception:
        return None


def classify_symbol_order(view: Optional[ActiveOrderView], position_qty: float) -> str:
    """Pure decision: what is this order FOR, relative to the live position?

    Returns one of: entry | staged_entry | reentry | pending_close |
    pending_close_legacy | valid_protection | partial_protection | wrong_side |
    unknown. Client-order-id prefixes (apex-entry-/apex-staged-/apex-reentry-trail-/
    apex-close-) are authoritative where present -- they survive every code path
    that creates them. Otherwise protection requires GTC + trailing_stop + the
    position-closing side; anything else on the closing side is treated as closing
    intent (legacy limit closes submitted before this scheme existed), and an
    order on the WRONG side is never, ever protection.
    """
    if view is None:
        return "unknown"
    coid = view.client_order_id or ""
    if coid.startswith("apex-close-"):
        return "pending_close"
    if coid.startswith("apex-staged-"):
        return "staged_entry"
    if coid.startswith("apex-reentry-trail-"):
        return "reentry"
    if coid.startswith("apex-entry-"):
        return "entry"
    if not view.is_active:
        return "unknown"
    if position_qty == 0:
        return "unknown"  # no position: nothing here is protection or a close
    closing_side = "sell" if position_qty > 0 else "buy"
    if view.side == closing_side:
        if view.order_type == "trailing_stop" and view.time_in_force == "gtc":
            # Protection -- but a partially-filled stop protecting less than the
            # full position leaves the rest uncovered.
            if 0 < view.remaining_qty < abs(position_qty):
                return "partial_protection"
            return "valid_protection"
        return "pending_close_legacy"
    return "wrong_side"


def _pending_close_client_id(symbol: str, reason: str) -> str:
    """Stable, broker-safe client order id for deliberate closes -- the marker
    classify_symbol_order() uses to recognise our own closing orders after a
    restart or across poller ticks. Reason is sanitized to a short token; no
    free-form exception text goes into an order id."""
    token = re.sub(r"[^a-z0-9]+", "-", str(reason or "exit").lower()).strip("-")[:20]
    return f"apex-close-{token}-{symbol}-{int(time.time())}"


@dataclass(frozen=True)
class CloseResult:
    """Explicit outcome of _request_reconciled_close() -- callers must never have
    to guess whether a close actually went out."""
    state: str                 # flat | already_pending | submitted | cancel_pending
                               # | failed_reprotected | critical_unprotected
                               # | blocked_pdt | failed
    symbol: str
    order_id: Optional[str]
    requested_qty: int
    remaining_position_qty: int
    detail: str


def _apply_thin_liquidity_override(risk_info: Dict, signal: Signal, equity: float) -> Dict:
    """If signal.thin_liquidity is set, replace dollar_amount with a flat
    THIN_LIQUIDITY_POSITION_SIZE_PCT of equity, overriding confidence-scaling
    entirely rather than stacking on top of it -- a predictable cap on the
    downside regardless of how confident the firing strategy was. Two
    independent reasons set this flag, same sizing either way: a rejected-
    list symbol admitted anyway (TRADE_THIN_LIQUIDITY_REJECTS, engine/
    equity/scan.py _scan_one) or a momentum-freshness reject traded anyway
    (TRADE_STALE_MOMENTUM_REJECTS, _validate_trade below, 2026-08-14).
    Returns risk_info unchanged if the signal isn't flagged.
    """
    if not getattr(signal, "thin_liquidity", False):
        return risk_info
    thin_dollars = round(equity * THIN_LIQUIDITY_POSITION_SIZE_PCT / 100, 2)
    out = dict(risk_info, dollar_amount=thin_dollars, allocation_pct=THIN_LIQUIDITY_POSITION_SIZE_PCT)
    log_extra = ""
    # stop_loss_pct only exists on the non-LIVE bracket path (_create_bracket_order's
    # inline trailing stop) -- the live path's protect_positions()/etc. don't read
    # risk_info at all, they get the same halving from _trail_pct_for() instead.
    if "stop_loss_pct" in risk_info:
        halved = round(risk_info["stop_loss_pct"] * THIN_LIQUIDITY_TRAILING_STOP_MULT, 2)
        out["stop_loss_pct"] = halved
        log_extra = f" | stop {risk_info['stop_loss_pct']:.1f}% -> {halved:.1f}%"
    log.info(
        f"[SIZE] {signal.symbol}: thin-liquidity admit -- "
        f"${risk_info['dollar_amount']:,.0f} -> ${thin_dollars:,.0f} "
        f"({THIN_LIQUIDITY_POSITION_SIZE_PCT:.0f}% flat){log_extra}"
    )
    return out


def _apply_confidence_size_ramp(risk_info: Dict, confidence: float, equity: float) -> Dict:
    """2026-08-13, user request: confidence-scaling (_execute_entry's
    CONF_SCALE_MIN_MULT..CONF_SCALE_FULL_CONF ramp) plateaus at 1.0x for any
    confidence >= 85% -- 85% and 99% get sized identically. Originally
    patched with a flat step above 92% confidence.

    2026-08-15, user request: "increase the percentage progressively
    maximum to 15% maximum per ticker" -- replaced the flat step with a
    continuous linear ramp: allocation_pct rises from the base %
    (risk_info['allocation_pct'], i.e. POSITION_SIZE_PCT/SMALL_ACCOUNT_
    POSITION_SIZE_PCT) at CONF_SCALE_FULL_CONF (85%) up to
    MAX_POSITION_SIZE_PCT (15%) at 100% confidence -- every confidence
    level above 85% now gets its own size instead of just two tiers.
    Returns risk_info unchanged at or below CONF_SCALE_FULL_CONF. Applied
    before _apply_thin_liquidity_override in the caller, which fully
    overrides -- not stacks with -- either scaling step."""
    if confidence <= CONF_SCALE_FULL_CONF:
        return risk_info
    base_pct = risk_info["allocation_pct"]
    span     = max(1e-6, 1.0 - CONF_SCALE_FULL_CONF)
    frac     = min(1.0, (confidence - CONF_SCALE_FULL_CONF) / span)
    ramp_pct = base_pct + (MAX_POSITION_SIZE_PCT - base_pct) * frac
    return dict(risk_info, allocation_pct=ramp_pct, dollar_amount=round(equity * ramp_pct / 100.0, 2))


def _apply_strategy_kelly_mult(risk_info: Dict, strategy: str, equity: float) -> Dict:
    """2026-08-15, user request: per-strategy sizing informed by each
    strategy's own Kelly % (STRATEGY_KELLY_MULT in config.py -- GapBreakout
    2.0x, TrendBreaker 0.25x, everything else unchanged at 1.0x). Straight
    multiplier on whatever allocation_pct the confidence ramp already
    produced, clamped to MAX_POSITION_CONCENTRATION_PCT (the hard
    per-symbol cap, also enforced independently and more precisely at
    order-sizing time via signal.price/buying power in
    _size_with_buying_power -- this clamp is defense-in-depth so risk_info
    itself never CLAIMS more than the real ceiling allows).

    2026-08-15: found by running the full sizing pipeline against real
    symbols/confidences -- GapBreakout at 95% confidence ramps to 12.5%
    BEFORE this multiplier runs, so the unclamped 2.0x pushed
    allocation_pct/dollar_amount to 25%, past the 20% cap, even though
    the final executed share count was already correctly capped
    downstream. Harmless to the actual trade, but risk_info and the debug
    log line built from it were overstating what would really execute --
    clamped here so they can't diverge from reality at any pipeline stage.
    Returns risk_info unchanged for a 1.0x (default) strategy."""
    mult = STRATEGY_KELLY_MULT.get(strategy, STRATEGY_KELLY_MULT_DEFAULT)
    if mult == 1.0:
        return risk_info
    new_pct = min(risk_info["allocation_pct"] * mult, MAX_POSITION_CONCENTRATION_PCT)
    return dict(risk_info, allocation_pct=new_pct, dollar_amount=round(equity * new_pct / 100.0, 2))


def _trail_pct_for(symbol: str, price: float, entry_log: Dict, gain_pct: float = None, atr: Optional[float] = None) -> Tuple[float, str]:
    """Trailing-stop % for `symbol`. 2026-08-22, user request: replaced the
    tiered/thin-liquidity system (get_dynamic_tier + THIN_LIQUIDITY_
    TRAILING_STOP_MULT) with one flat floor, TRAIL_STOP_PCT, for every
    position -- no more per-tier or per-liquidity variability.

    2026-09-01, user request ("change the trail stop exit to atr based
    values"): an optional `atr` distance can now widen the floor per-symbol
    -- ATR scaled by ATR_TRAIL_MULTIPLIER, floored at TRAIL_STOP_PCT and
    capped at ATR_TRAIL_MAX_PCT, so a volatile name gets a volatility-scaled
    stop instead of the same fixed leash as a quiet one. This function stays
    PURE (no network) -- the caller fetches ATR and passes it in, via the
    _atr_trail_pct_for() wrapper below. `atr` omitted/None/<=0 falls back to
    the flat floor, exactly the pre-ATR behavior (keeps every existing unit
    test on this helper valid).

    If `gain_pct` (current unrealized %) is given and positive, widen past
    the floor to PROFIT_TRAIL_GIVEBACK_PCT of that gain once it computes
    wider than the floor -- a winning trade earns more room instead of
    riding the same fixed leash as a fresh entry. Losing/flat positions
    (gain_pct <= 0 or omitted) just get the flat floor.

    Single source of truth for every trailing-stop placement/re-place/
    tighten in this file (protect_positions, ratchet, after-hours
    virtual-stop, all re-arm fallbacks) instead of separate call sites
    drifting out of sync with each other."""
    trail_pct = TRAIL_STOP_PCT
    label = "FLAT"
    if atr is not None and atr > 0 and price > 0 and ATR_TRAIL_ENABLED:
        atr_pct = atr / price * 100.0 * ATR_TRAIL_MULTIPLIER
        if atr_pct > ATR_TRAIL_MAX_PCT:
            atr_pct = ATR_TRAIL_MAX_PCT
        if atr_pct > trail_pct:
            trail_pct, label = round(atr_pct, 2), "ATR"
    if gain_pct is not None and gain_pct > 0:
        profit_trail = round(gain_pct * (PROFIT_TRAIL_GIVEBACK_PCT / 100.0), 2)
        if profit_trail > trail_pct:
            trail_pct, label = profit_trail, "PROFIT"
    return trail_pct, label


def _atr_trail_pct_for(symbol: str, price: float, entry_log: Dict, gain_pct: float = None) -> Tuple[float, str]:
    """ATR-aware trailing-stop % -- the wrapper every real call site uses.
    Computes ATR(ATR_TRAIL_PERIOD) off the cached 1-min bars and passes it
    into _trail_pct_for(); any fetch/compute failure, empty/insufficient
    bars, or ATR below the floor falls back to the flat TRAIL_STOP_PCT
    (fail-open -- a missing reading must never loosen a stop)."""
    atr = 0.0
    if ATR_TRAIL_ENABLED:
        try:
            _bars = get_bars(symbol, period="1d", interval="1m")
            atr = calculate_atr(_bars, period=ATR_TRAIL_PERIOD)
        except Exception:
            atr = 0.0
    return _trail_pct_for(symbol, price, entry_log, gain_pct=gain_pct, atr=atr)


def _demo() -> None:
    """python -m engine.execution.enhanced -- asserts the ratchet math holds
    at its key points before it's trusted against a live account."""
    global _get_scan_targets, _check_ema_trend_alignment, get_bars, ENTRY_WINDOW_END_ET
    assert ratchet_scale(0.0) == 1.0, "below floor -> no tightening"
    assert ratchet_scale(0.75) == 1.0, "at floor -> no tightening"
    assert abs(ratchet_scale(1.0) - CONF_RATCHET_MAX_TIGHTEN) < 1e-9, "at 1.0 -> max tightening"
    mid = ratchet_scale(0.875)  # halfway between 0.75 and 1.0
    expected_mid = 1.0 - 0.5 * (1.0 - CONF_RATCHET_MAX_TIGHTEN)
    assert abs(mid - expected_mid) < 1e-9, f"halfway point off: {mid} != {expected_mid}"
    assert ratchet_scale(0.90) < ratchet_scale(0.80), "higher confidence must tighten more"
    print("ratchet_scale: all checks passed")

    # _trail_pct_for: flat floor, widens to PROFIT_TRAIL_GIVEBACK_PCT of gain
    # only once that's wider than the floor. 2026-08-22, user request.
    assert _trail_pct_for("X", 10.0, {}) == (TRAIL_STOP_PCT, "FLAT")
    assert _trail_pct_for("X", 10.0, {}, gain_pct=-2.0) == (TRAIL_STOP_PCT, "FLAT"), "losing position must use the floor"
    assert _trail_pct_for("X", 10.0, {}, gain_pct=0.0) == (TRAIL_STOP_PCT, "FLAT")
    crossover = TRAIL_STOP_PCT / (PROFIT_TRAIL_GIVEBACK_PCT / 100.0)  # gain% where widening starts beating the floor
    below = crossover - 1.0  # just under the crossover
    assert _trail_pct_for("X", 10.0, {}, gain_pct=below)[1] == "FLAT", "still under the floor -> no widening yet"
    above = crossover + 10.0  # comfortably past the crossover
    r = _trail_pct_for("X", 10.0, {}, gain_pct=above)
    assert r == (round(above * PROFIT_TRAIL_GIVEBACK_PCT / 100.0, 2), "PROFIT"), r
    print("_trail_pct_for: all checks passed")

    # _trail_pct_for ATR widening: 2026-09-01, user request ("change the
    # trail stop exit to atr based values"). `atr` is a caller-provided
    # distance (the wrapper fetches it) -- this helper stays pure.
    assert _trail_pct_for("X", 10.0, {}, atr=0.0) == (TRAIL_STOP_PCT, "FLAT"), "zero ATR -> flat floor"
    assert _trail_pct_for("X", 10.0, {}, atr=0.05) == (TRAIL_STOP_PCT, "FLAT"), "ATR below floor -> floor"
    atr_widen = round(0.20 / 10.0 * 100.0 * ATR_TRAIL_MULTIPLIER, 2)  # $0.20 ATR on $10
    assert _trail_pct_for("X", 10.0, {}, atr=0.20) == (atr_widen, "ATR"), "ATR wider than floor -> ATR label"
    assert _trail_pct_for("X", 10.0, {}, atr=1.00) == (ATR_TRAIL_MAX_PCT, "ATR"), "huge ATR capped at ATR_TRAIL_MAX_PCT"
    # Profit giveback must still widen past the ATR value once gain is large.
    r_atr = _trail_pct_for("X", 10.0, {}, gain_pct=20.0, atr=0.20)  # ATR -> 3.0%, profit -> 4.0%
    assert r_atr == (round(20.0 * PROFIT_TRAIL_GIVEBACK_PCT / 100.0, 2), "PROFIT"), r_atr
    print("_trail_pct_for ATR widening: all checks passed")

    # _update_ema9_peak: tracks the running peak (long) / trough (short) of
    # EMA9 since entry -- the reference check_ema9_exit trails against
    # (2026-08-25, user request: "get a ema 9 trail stop of 0.3%").
    update_peak = EnhancedExecutor._update_ema9_peak
    p: Dict[str, float] = {}
    assert update_peak(p, "X", 10.0, True) == 10.0, "first observation becomes the initial peak"
    assert update_peak(p, "X", 10.5, True) == 10.5, "new high -> peak ratchets up"
    assert update_peak(p, "X", 10.2, True) == 10.5, "pullback -> peak holds, doesn't fall back"
    assert update_peak(p, "X", 10.8, True) == 10.8, "new high again -> peak ratchets further"
    # short: trough moves DOWN, never back up
    assert update_peak(p, "Y", 20.0, False) == 20.0, "first observation becomes the initial trough (short)"
    assert update_peak(p, "Y", 19.5, False) == 19.5, "new low -> trough ratchets down (short)"
    assert update_peak(p, "Y", 19.8, False) == 19.5, "bounce -> trough holds (short)"
    print("_update_ema9_peak: all checks passed")

    # _ema9_trail_exit_reason: exit once EMA9 has pulled back
    # EMA9_TRAIL_PCT% from its own peak/trough since entry -- a trailing
    # stop on EMA9, not a snapshot vs. the previous minute.
    trail_reason = EnhancedExecutor._ema9_trail_exit_reason
    peak = 10.00
    trail_threshold = EMA9_TRAIL_PCT / 100.0 * peak
    assert trail_reason(peak, peak, True) is None, "at the peak -> no exit"
    assert trail_reason(peak - trail_threshold / 2, peak, True) is None, "pulled back but under the trail% -> no exit"
    assert trail_reason(peak - trail_threshold - 0.0001, peak, True) is not None, "pulled back past the trail% -> exit"
    assert trail_reason(peak + 1.0, peak, True) is None, "new high (favorable) -> no exit"
    # short: adverse direction is EMA9 rising back past trough + trail%
    trough = 20.00
    trough_threshold = EMA9_TRAIL_PCT / 100.0 * trough
    assert trail_reason(trough, trough, False) is None, "at the trough (short) -> no exit"
    assert trail_reason(trough + trough_threshold + 0.0001, trough, False) is not None, "rose past the trail% off the trough (short) -> exit"
    assert trail_reason(trough - 1.0, trough, False) is None, "new low (favorable, short) -> no exit"
    print("_ema9_trail_exit_reason: all checks passed")

    # _check_ema_trend_alignment: EMA7 delta must confirm the trade
    # direction AND EMA7 must sit on the right side of EMA15. Missing or
    # insufficient data blocks the entry because EMA alignment is required.
    # 2026-08-22, user request; EMA9 -> EMA7
    # 2026-08-24; EMA3 added alongside EMA7 2026-08-25, then removed
    # 2026-08-25 ("remove ema3 delta positive"); EMA7-vs-EMA15 crossover
    # added 2026-08-25 (see the function's own docstring).
    import pandas as _pd
    _orig_get_bars = get_bars
    _sig_stub = Signal("TEST", "buy", 10.0, 0.9, "test", "TestStrat")
    globals()["get_bars"] = lambda symbol, period, interval, bypass_cache=False: _pd.DataFrame({"close": list(range(1, 40))})  # rising -> EMA7 delta positive, EMA7 above EMA15
    ok, reason = _check_ema_trend_alignment(_sig_stub, is_long=True)
    assert ok is True and reason is None, "rising EMA7 + EMA7>EMA15 must align with a long"
    ok, reason = _check_ema_trend_alignment(_sig_stub, is_long=False)
    assert ok is False and reason is not None, "rising EMA7 must reject a short"
    globals()["get_bars"] = lambda symbol, period, interval, bypass_cache=False: _pd.DataFrame({"close": list(range(40, 1, -1))})  # falling -> EMA7 delta negative, EMA7 below EMA15
    ok, reason = _check_ema_trend_alignment(_sig_stub, is_long=False)
    assert ok is True and reason is None, "falling EMA7 + EMA7<EMA15 must align with a short"
    ok, reason = _check_ema_trend_alignment(_sig_stub, is_long=True)
    assert ok is False and reason is not None, "falling EMA7 must reject a long"
    # crossover: a long decline that just turned up -- EMA7's delta is
    # freshly positive, but EMA7 hasn't caught up past the still-elevated,
    # slower-reacting EMA15 yet. Proves the crossover condition does real
    # work beyond the slope check alone (which would pass this on its own).
    globals()["get_bars"] = lambda symbol, period, interval, bypass_cache=False: _pd.DataFrame({"close": list(range(50, 10, -1)) + [11, 12, 13, 14, 15]})
    ok, reason = _check_ema_trend_alignment(_sig_stub, is_long=True)
    assert ok is False and reason is not None, "rising EMA7 delta but EMA7 still below EMA15 must reject a long"
    # mirror: a long rally that just turned down -- EMA7's delta freshly
    # negative, EMA7 hasn't dropped below the still-elevated EMA15 for a short.
    globals()["get_bars"] = lambda symbol, period, interval, bypass_cache=False: _pd.DataFrame({"close": list(range(10, 50)) + [49, 48, 47, 46, 45]})
    ok, reason = _check_ema_trend_alignment(_sig_stub, is_long=False)
    assert ok is False and reason is not None, "falling EMA7 delta but EMA7 still above EMA15 must reject a short"
    globals()["get_bars"] = lambda symbol, period, interval, bypass_cache=False: _pd.DataFrame()
    ok, reason = _check_ema_trend_alignment(_sig_stub, is_long=True)
    assert ok is False and reason is not None, "empty bars must block because EMA alignment is required"
    globals()["get_bars"] = _orig_get_bars
    print("_check_ema_trend_alignment: all checks passed")

    # _blocked_entry_action: check_blocked_entries_ema's per-poll decision
    # -- fire once the gate agrees, expire once the entry window's closed
    # or the symbol's fallen out of the TI universe, otherwise keep
    # waiting indefinitely ("no expire" -- no staleness timer anymore).
    # 2026-08-25, user request chain (see the function's own docstring).
    action = EnhancedExecutor._blocked_entry_action
    assert action(gate_ok=False, past_window=False, in_universe=True) == "wait", "just queued, gate still failing -> wait"
    assert action(gate_ok=False, past_window=False, in_universe=True) == "wait", "still waiting an hour later is fine too -- no staleness timer"
    assert action(gate_ok=True, past_window=False, in_universe=True) == "fire", "gate agrees -> fire"
    assert action(gate_ok=False, past_window=True, in_universe=True) == "expire", "past the entry window -> expire regardless of the gate"
    assert action(gate_ok=True, past_window=True, in_universe=True) == "expire", "past the entry window -> expire even if the gate happens to agree (window is absolute)"
    assert action(gate_ok=False, past_window=False, in_universe=False) == "expire", "dropped out of the TI universe -> expire even with time left in the window"
    assert action(gate_ok=True, past_window=False, in_universe=False) == "expire", "dropped out of the TI universe -> expire even if the gate happens to agree"
    print("_blocked_entry_action: all checks passed")

    # _entries_today_count: needs only the two bare attrs, no live client --
    # build a stub rather than a full EnhancedExecutor().
    class _Stub:
        _entries_today: Dict[str, int] = {}
        _entries_today_date = None
    stub = _Stub()
    count = EnhancedExecutor._entries_today_count
    assert count(stub, "PFSA") == 0, "first entry today must not look like a re-entry"
    stub._entries_today["PFSA"] = 1  # what _create_bracket_order does after that first entry fills
    assert count(stub, "PFSA") == 1, "second same-day entry must be flagged a re-entry"
    assert count(stub, "OTHER") == 0, "a different symbol is unaffected"
    stub._entries_today_date = datetime.date(2000, 1, 1)  # force a date rollover
    assert count(stub, "PFSA") == 0, "a new day must reset the count"
    print("_entries_today_count: all checks passed")

    # _maybe_rearm_reentry / detect_stopped_out_positions' _no_rearm gate:
    # 2026-08-26, user request ("the reentry is key to success... ensure it
    # goes to work for sure") -- mocked end-to-end rather than re-reading
    # the code again, since these two need a live client/broker to exercise
    # for real. Monkeypatches the module globals _maybe_rearm_reentry itself
    # calls (_get_scan_targets, _check_ema_trend_alignment, get_bars,
    # ENTRY_WINDOW_END_ET) and restores them in a finally so this can't leak
    # into any other test or a real run.
    _orig_scan_targets, _orig_gate, _orig_bars, _orig_window, _orig_reentry_perf, _orig_lunch_break = (
        _get_scan_targets, _check_ema_trend_alignment, get_bars, ENTRY_WINDOW_END_ET,
        EnhancedExecutor._check_30m_reentry_performance, in_lunch_break,
    )
    try:
        import pandas as _pd

        class _FakeOrder:
            id = "fake-order-id"

        class _FakeClient:
            def __init__(self):
                self.submitted = []
                self.positions = []
            def submit_order(self, req):
                self.submitted.append(req)
                return _FakeOrder()
            def get_all_positions(self):
                return self.positions

        class _ReentryStub:
            # detect_stopped_out_positions calls self._maybe_rearm_reentry(...)
            # internally -- needs the real bound method, not just the stub's
            # own attributes.
            _maybe_rearm_reentry = EnhancedExecutor._maybe_rearm_reentry

            def __init__(self):
                self.client = _FakeClient()
                self.order_cache = {}
                self._no_rearm = set()
                self._ema_blocked_entries = {}
                self._entry_log = {}
                self._ratchet_done = set()
                self._ema9_trail_peak = {}
                self._last_known_positions = {}
                self._loss_reentry_required = set()
                self._loss_block_morning = set()
                self._loss_block_day = set()
                self._symbol_loss_counts_today = {}
                self._loss_block_date = None

            def _record_symbol_loss(self, sym, tag):
                # bare bookkeeping stub -- no date-sensitive morning/day block
                # logic is needed for the re-entry-arming assertions below.
                self._symbol_loss_counts_today[sym] = self._symbol_loss_counts_today.get(sym, 0) + 1

            def _get_account(self, force_refresh=False):
                return SimpleNamespace(equity=10000.0, buying_power=10000.0)

            def _execute_entry(self, signal, acct, order_type, bypass_pdt=False):
                order = self.client.submit_order(SimpleNamespace(symbol=signal.symbol))
                self.order_cache[signal.symbol] = order.id
                return True

        ENTRY_WINDOW_END_ET = "23:59"  # never "past window" during this test run
        in_lunch_break = lambda *_: False  # never inside the midday break during this test run
        _get_scan_targets = lambda: ["FOO", "BAR"]  # FOO in top-30, BAZ is not
        get_bars = lambda *a, **k: _pd.DataFrame({"close": [1.20, 1.22, 1.24]})
        EnhancedExecutor._check_30m_reentry_performance = staticmethod(lambda symbol, is_long: (True, ""))

        rearm = EnhancedExecutor._maybe_rearm_reentry
        stopped_out = EnhancedExecutor.detect_stopped_out_positions

        # Case 1: gate agrees -> submits a real re-entry order.
        _check_ema_trend_alignment = lambda sig, is_long, force_fresh=False: (True, None)
        s = _ReentryStub()
        rearm(s, "FOO", True, 10, "TEST", was_loss=True)
        assert len(s.client.submitted) == 1, "gate-ok must submit exactly one order"
        assert s.order_cache.get("FOO") == "fake-order-id", "must register the order in order_cache"
        assert "FOO" in s._no_rearm, "must self-mark _no_rearm so detect_stopped_out_positions doesn't double-process this close"
        assert "FOO" not in s._ema_blocked_entries, "a successful arm must not also queue a retry"

        # Case 2: gate disagrees -> queues for per-minute retry instead of giving up.
        _check_ema_trend_alignment = lambda sig, is_long, force_fresh=False: (False, "trend not aligned")
        s = _ReentryStub()
        rearm(s, "FOO", True, 10, "TEST", was_loss=True)
        assert len(s.client.submitted) == 0, "gate-fail must not submit an order"
        assert "FOO" in s._no_rearm, "must still self-mark even on a failed immediate check"
        assert "FOO" in s._ema_blocked_entries, "gate-fail must queue into _ema_blocked_entries for per-minute retry"
        q = s._ema_blocked_entries["FOO"]
        assert q["signal"].symbol == "FOO" and q["order_type"] == OrderType.LONG, "queued signal must carry the right symbol/direction"

        # Case 3: symbol not in the top-30 scan universe -> no order, no queue.
        _check_ema_trend_alignment = lambda sig, is_long, force_fresh=False: (True, None)  # would pass if it got there
        s = _ReentryStub()
        rearm(s, "BAZ", True, 10, "TEST")
        assert len(s.client.submitted) == 0, "outside top-30 must not submit"
        assert "BAZ" not in s._ema_blocked_entries, "outside top-30 must not queue either"
        assert "BAZ" in s._no_rearm, "self-mark still happens even when the top-30 check is what blocked it"

        # Case 4: detect_stopped_out_positions respects _no_rearm (intentional close) --
        # must NOT re-arm, and must clear the mark.
        s = _ReentryStub()
        s._last_known_positions = {"FOO": {"entry_price": 1.0, "last_price": 1.0, "is_long": True, "qty": 10}}
        s._no_rearm.add("FOO")
        stopped_out(s)
        assert len(s.client.submitted) == 0, "a _no_rearm-marked close must not re-arm"
        assert "FOO" not in s._no_rearm, "the mark must be consumed (discarded) once seen, not left dangling"

        # Case 5: detect_stopped_out_positions re-arms an UNMARKED close (a
        # genuine broker-side stop firing on its own, with no explicit
        # _no_rearm from any closing path).
        _check_ema_trend_alignment = lambda sig, is_long, force_fresh=False: (True, None)
        s = _ReentryStub()
        s._last_known_positions = {"FOO": {"entry_price": 1.0, "last_price": 0.9, "is_long": True, "qty": 10}}
        stopped_out(s)
        assert len(s.client.submitted) == 1, "an unmarked disappeared position must be treated as a genuine stop and re-armed"
        assert s.order_cache.get("FOO") == "fake-order-id"
    finally:
        _get_scan_targets, _check_ema_trend_alignment, get_bars, ENTRY_WINDOW_END_ET, in_lunch_break = (
            _orig_scan_targets, _orig_gate, _orig_bars, _orig_window, _orig_lunch_break
        )
        EnhancedExecutor._check_30m_reentry_performance = staticmethod(_orig_reentry_perf)
    print("_maybe_rearm_reentry / detect_stopped_out_positions: all checks passed")

    # check_pending_entries_ema: 2026-08-27, user request ("ensure 1min
    # checks are robust to cancel unfilled order if conditions change").
    # Rewritten to query the broker directly (see the method's own
    # docstring for why order_cache alone missed a real duplicate-order
    # case live) -- these checks target that rewrite directly: multiple
    # resting entries per symbol both get rechecked, a GTC protective stop
    # is never touched even on a universal gate-fail, side determines
    # long/short correctly, and stale cache references get swept.
    _orig_gate2 = _check_ema_trend_alignment
    try:
        class _FakeOrderObj:
            def __init__(self, symbol, order_id, side, otype, tif):
                self.symbol, self.id, self.side, self.order_type, self.time_in_force = (
                    symbol, order_id, side, otype, tif
                )

        class _PendingClient:
            def __init__(self, orders):
                self._orders = orders
                self.cancelled = []
                self.list_calls = 0
            def get_orders(self, filter=None):
                self.list_calls += 1
                return self._orders
            def cancel_order_by_id(self, order_id):
                self.cancelled.append(order_id)

        class _PendingStub:
            check_pending_entries_ema = EnhancedExecutor.check_pending_entries_ema
            def __init__(self, orders):
                self.client = _PendingClient(orders)
                self.order_cache = {}
                self._pending_entry_signals = {}
                self._ema_blocked_entries = {}

        # Case 1: two separate DAY trailing-buy orders resting on the SAME
        # symbol (the live scenario that exposed this) -- both must be
        # independently rechecked and, on a gate-fail, both cancelled.
        # order_cache only ever knew about one of them (the classic
        # single-slot-overwrite gap), confirmed by leaving it pointed at
        # a THIRD, unrelated fake id below.
        orders = [
            _FakeOrderObj("DUP", "id-1", OrderSide.BUY, AlpacaOrderType.TRAILING_STOP, TimeInForce.DAY),
            _FakeOrderObj("DUP", "id-2", OrderSide.BUY, AlpacaOrderType.TRAILING_STOP, TimeInForce.DAY),
        ]
        s = _PendingStub(orders)
        s.order_cache["DUP"] = "id-1"  # stale single-slot reference, deliberately
        _check_ema_trend_alignment = lambda sig, is_long, force_fresh=False: (False, "trend not aligned")
        s.check_pending_entries_ema()
        assert set(s.client.cancelled) == {"id-1", "id-2"}, \
            f"both resting entries for the same symbol must be cancelled independently, got {s.client.cancelled}"
        assert "DUP" not in s.order_cache, "order_cache must be cleared once its tracked order resolves"

        # Case 2: a GTC trailing_stop (protective exit stop) must NEVER be
        # touched here, even with a gate check that fails everything --
        # this is the safety-critical case (a held short's buy-to-cover
        # protective stop looks identical except for time_in_force).
        orders = [
            _FakeOrderObj("SHRT", "protect-1", OrderSide.BUY, AlpacaOrderType.TRAILING_STOP, TimeInForce.GTC),
        ]
        s = _PendingStub(orders)
        _check_ema_trend_alignment = lambda sig, is_long, force_fresh=False: (False, "trend not aligned")
        s.check_pending_entries_ema()
        assert s.client.cancelled == [], \
            f"a GTC trailing stop must never be cancelled by this check (it's a protective exit, not an entry), got {s.client.cancelled}"

        # Case 3: a short entry (side=sell) must be evaluated with
        # is_long=False, not assumed long.
        captured = []
        def _capture_gate(sig, is_long, force_fresh=False):
            captured.append(is_long)
            return (True, None)  # gate passes -> no cancel, just checking the arg
        orders = [
            _FakeOrderObj("SHORTENT", "id-s", OrderSide.SELL, AlpacaOrderType.TRAILING_STOP, TimeInForce.DAY),
        ]
        s = _PendingStub(orders)
        _check_ema_trend_alignment = _capture_gate
        s.check_pending_entries_ema()
        assert captured == [False], f"a sell-side entry order must be checked as a short (is_long=False), got {captured}"
        assert s.client.cancelled == [], "gate-pass must not cancel"

        # Case 4: order_cache/_pending_entry_signals references for a
        # symbol with no resting entry order left at the broker (filled
        # or cancelled elsewhere) are stale -- must be swept even though
        # nothing was cancelled THIS poll.
        s = _PendingStub(orders=[])  # broker reports nothing resting at all
        s.order_cache["GONE"] = "stale-id"
        s._pending_entry_signals["GONE"] = {"signal": None, "order_type": None}
        _check_ema_trend_alignment = lambda sig, is_long, force_fresh=False: (True, None)
        s.check_pending_entries_ema()
        assert "GONE" not in s.order_cache, "a stale order_cache entry with nothing resting at the broker must be swept"
        assert "GONE" not in s._pending_entry_signals, "a stale pending-signal entry must be swept the same way"

        # Case 5: broker listing itself fails -- must not raise, must not
        # touch any state (fail closed / no-op, not fail open).
        class _FailingClient(_PendingClient):
            def get_orders(self, filter=None):
                raise RuntimeError("simulated broker outage")
        s = _PendingStub(orders=[])
        s.client = _FailingClient([])
        s.order_cache["SAFE"] = "id-safe"
        s.check_pending_entries_ema()  # must not raise
        assert s.order_cache.get("SAFE") == "id-safe", "a broker-listing failure must leave existing state untouched, not wipe it"

        print("check_pending_entries_ema: all checks passed")
    finally:
        _check_ema_trend_alignment = _orig_gate2

    # check_blocked_entries_ema: same 2026-08-27 parallelization request,
    # applied to the fire/wait/expire loop. Verifies multiple queued
    # symbols are each gate-checked (now concurrently) and correctly
    # fire/wait/expire, that one symbol's gate check raising doesn't take
    # down the rest, and that a raised gate check leaves that symbol
    # queued (not silently dropped) rather than treated as a pass or fail.
    _orig_gate3, _orig_scan3, _orig_window3, _orig_lunch_break3 = (
        _check_ema_trend_alignment, _get_scan_targets, ENTRY_WINDOW_END_ET, in_lunch_break,
    )
    try:
        class _BlockedClient:
            def __init__(self):
                self.submitted = []
            def submit_order(self, req):
                self.submitted.append(req)
                return SimpleNamespace(id="fake-id")

        def _fake_signal(sym):
            return SimpleNamespace(symbol=sym)

        class _BlockedStub:
            check_blocked_entries_ema = EnhancedExecutor.check_blocked_entries_ema
            _blocked_entry_action = staticmethod(EnhancedExecutor._blocked_entry_action)
            def __init__(self):
                self.client = _BlockedClient()
                self._ema_blocked_entries = {}
                self.fired = []
            def _get_account(self, force_refresh=False):
                return SimpleNamespace(equity=10000, buying_power=10000)
            def _is_reentry_signal(self, sym, is_long):
                return False
            def _execute_entry(self, signal, acct, order_type, bypass_pdt=False):
                self.fired.append(signal.symbol)
                return True

        ENTRY_WINDOW_END_ET = "23:59"
        _get_scan_targets = lambda: ["GOOD1", "GOOD2", "BAD", "RAISES"]
        in_lunch_break = lambda *_: False  # never inside the midday break during this test run

        def _gate_by_symbol(sig, is_long, force_fresh=False):
            if sig.symbol == "RAISES":
                raise RuntimeError("simulated fetch failure")
            return (sig.symbol != "BAD", None)
        _check_ema_trend_alignment = _gate_by_symbol

        s = _BlockedStub()
        for sym in ["GOOD1", "BAD", "RAISES"]:
            s._ema_blocked_entries[sym] = {
                "signal": _fake_signal(sym), "order_type": OrderType.LONG,
                "queued_at": datetime.datetime.now(datetime.timezone.utc),
            }
        s.check_blocked_entries_ema()
        assert s.fired == ["GOOD1"], f"only GOOD1 must fire, got {s.fired}"
        assert "BAD" in s._ema_blocked_entries, "gate-fail (BAD) with time left in the window must stay queued, not be dropped"
        assert "RAISES" in s._ema_blocked_entries, \
            "a symbol whose gate check itself raised must stay queued for retry, not be dropped or treated as pass/fail"
        assert "GOOD1" not in s._ema_blocked_entries, "a fired symbol must leave the queue"

        # Past the entry window -> everything still queued expires, gate result irrelevant.
        s2 = _BlockedStub()
        s2._ema_blocked_entries["GOOD1"] = {
            "signal": _fake_signal("GOOD1"), "order_type": OrderType.LONG,
            "queued_at": datetime.datetime.now(datetime.timezone.utc),
        }
        ENTRY_WINDOW_END_ET = "00:00"  # already past for any real now_et
        s2.check_blocked_entries_ema()
        assert s2.fired == [], "past the entry window, nothing may fire even if the gate agrees"
        assert "GOOD1" not in s2._ema_blocked_entries, "past the entry window, the queued entry must expire (be removed)"

        print("check_blocked_entries_ema: all checks passed")
    finally:
        _check_ema_trend_alignment, _get_scan_targets, ENTRY_WINDOW_END_ET, in_lunch_break = (
            _orig_gate3, _orig_scan3, _orig_window3, _orig_lunch_break3,
        )

    # check_ema9_exit: same 2026-08-27 parallelization request, applied to
    # the fetch/decide phase (fresh bar fetch per same-day position).
    # _update_ema9_peak/_ema9_trail_exit_reason are already covered by
    # their own dedicated tests above -- this only needs to prove multiple
    # positions get independently fetched/decided in the new parallel
    # phase, the one that should exit actually reaches the sequential
    # close path, and one symbol's fetch failing doesn't take down the
    # rest. The exit decision is driven by a distinguishable close price
    # per symbol from the mocked get_bars, run through the REAL
    # _ema9_trail_exit_reason (not stubbed) -- exercises the actual logic,
    # not just the wiring.
    _orig_bars2 = get_bars
    try:
        class _Ema9Client:
            def __init__(self, positions):
                self._positions = positions
                self.cancelled = []
                self.orders = []
            def get_all_positions(self):
                return self._positions
            def get_orders(self):
                return self.orders
            def cancel_order_by_id(self, order_id):
                self.cancelled.append(order_id)

        class _FakePos:
            def __init__(self, symbol, qty, price):
                self.symbol, self.qty, self.current_price = symbol, qty, price

        class _Ema9Stub:
            check_ema9_exit = EnhancedExecutor.check_ema9_exit
            _update_ema9_peak = staticmethod(EnhancedExecutor._update_ema9_peak)
            _ema9_trail_exit_reason = staticmethod(EnhancedExecutor._ema9_trail_exit_reason)
            def __init__(self, positions):
                self.client = _Ema9Client(positions)
                self._entry_log = {p.symbol: {"date": datetime.date.today()} for p in positions}
                # Pre-seed a peak well above the mocked EXIT1 reading below,
                # so its very first check this call already shows a genuine
                # pullback -- _update_ema9_peak's real "first observation
                # becomes the peak" behavior would otherwise mean nothing
                # can trigger on a symbol's first-ever check.
                self._ema9_trail_peak = {"EXIT1": 100.0, "HOLD1": 1.0}
                self.closed = []
            def _submit_closing_order(self, sym, qty, side, current):
                self.closed.append(sym)
            def _request_reconciled_close(self, symbol, reason, current_price, **k):
                # Stub the reconciliation entry point itself: the EMA9 close path
                # now routes through it (2026-09-03), so the self-test exercises
                # the decision logic, not broker reconciliation.
                self.closed.append(symbol)
                return CloseResult("submitted", symbol, "stub-close-id", 5, 5, "stub")
            def _maybe_rearm_reentry(self, sym, is_long, qty, tag, was_loss=False):
                pass

        import pandas as _pd
        def _bars_for(sym, *a, **k):
            if sym == "FAILS":
                raise RuntimeError("simulated fetch failure")
            # EXIT1: was at peak 100, now way down -> real trailing-stop
            # logic must flag it. HOLD1: right at its own peak -> must not.
            close = 50.0 if sym == "EXIT1" else 1.0
            return _pd.DataFrame({"close": [close] * (EMA_TREND_MIN_BARS + 1)})
        get_bars = _bars_for

        positions = [_FakePos("EXIT1", 5, 10.0), _FakePos("HOLD1", 5, 10.0), _FakePos("FAILS", 5, 10.0)]
        s = _Ema9Stub(positions)
        s.check_ema9_exit()
        assert s.closed == ["EXIT1"], \
            f"only the position that genuinely pulled back from its peak must close, got {s.closed}"

        print("check_ema9_exit: all checks passed")
    finally:
        get_bars = _orig_bars2


class OrderType(Enum):
    LONG  = "long"
    SHORT = "short"


@dataclass
class PDTTracker:
    """Pattern Day Trader tracking -- syncs with live Alpaca daytrade_count."""
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

    def total_market_value(self) -> float:
        """Sum of abs(market_value) across every open equity position
        (options legs excluded -- they're sized/margined separately, same
        exclusion used throughout this file for concentration checks)."""
        total = 0.0
        for sym, pos in self.positions_dict.items():
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue
            try:
                total += abs(float(pos.market_value))
            except (TypeError, ValueError, AttributeError):
                continue
        return total


@dataclass
class AccountSnapshot:
    """Cached Alpaca account state -- equity, buying power, live PDT count."""
    equity:              float
    buying_power:        float
    daytrade_count:      int
    pattern_day_trader:  bool = False
    maintenance_margin:  float = 0.0
    timestamp:           float = field(default=0.0)


# ----------------------------------------------------------------------------------------------------------------------------
# Executor
# ----------------------------------------------------------------------------------------------------------------------------
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
        self._account_ttl:    float = 2.0   # tight TTL -- buying power must be fresh between orders
        self._entry_submission_lock = threading.RLock()
        self._ema_entry_confirmations: Dict[str, Tuple[int, float]] = {}
        self._htb_cache:      set   = set()   # hard-to-borrow symbols -- skip shorts this session
        self._entry_log:   Dict[str, dict] = {}  # {symbol: {"strategy": str, "date": date}}
        self._swap_cycle_closed: set = set()     # positions already swapped this scan cycle
        self._ratchet_done: set = set()          # symbols whose stop was already confidence-tightened
        self._tp_targets: Dict[str, float] = {} # {symbol: take-profit price} for ATR-based TP tracking
        # {symbol: running peak (long) / trough (short) of EMA9 since
        # entry} -- 2026-08-25, user request ("get a ema 9 trail stop of
        # 0.3%"): the reference check_ema9_exit trails against. See
        # _update_ema9_peak. Cleared on close in
        # detect_stopped_out_positions.
        self._ema9_trail_peak: Dict[str, float] = {}
        self._pdt_stop_blocked: Dict[str, float] = {}  # {symbol: stop_price} -- broker-rejected stops; monitored in software
        self._last_known_positions: Dict[str, dict] = {}  # {symbol: {entry_price, last_price, is_long, qty}} -- snapshot used to notice a position disappearing between polls
        # 2026-08-26, user request ("I have put in 1% on the hope the new
        # orders will be placed immediately after the exit with conditions
        # check every minute, but it doesn't seem to work"): every
        # deliberate/intentional closing path (EOD, guardrail-fail, stale-
        # swing, no-gain, portfolio-rebalance/concentration/leverage trims,
        # emergency, take-profit, a contradicting strategy signal) marks its
        # symbol here right before submitting the close. detect_stopped_out_
        # positions() checks this set when a position disappears: marked ->
        # respect the intentional close, don't re-arm; unmarked -> the most
        # likely explanation is the resting broker-side GTC trailing stop
        # filled entirely on its own (confirmed the dominant exit route
        # 2026-08-26: ~51 of 73 trades, vs. only 9 through check_ema9_exit,
        # the only path that already had re-entry logic) -- genuinely a stop,
        # so it's eligible for the same re-entry check as any other stop.
        self._no_rearm: set = set()
        self._afterhours_chase_count: Dict[str, int] = {}  # {symbol: consecutive re-chase attempts} -- widens slip each retry so a fast-falling after-hours book actually fills
        self._no_gain_chase_count: Dict[str, int] = {}  # same, for close_no_gain_positions's re-chase
        self._pdt_overnight_forced: set = set()  # symbols where PDT also blocks close -- forced overnight, no retries
        self._pdt_violation_alerted: bool = False  # tracks whether the PDT violation email has been sent this session
        self._force_close_pending: Dict[str, dict] = {}  # {symbol: {"reason": str, "chase_count": int}} -- EOD/guardrail closes not yet confirmed flat; swept by _sweep_force_closes until filled
        self._eod_closed: Dict[object, set] = {}  # {date: {symbol, ...}} -- EOD close orders already submitted today, including positions missing an entry-log row
        self._exchange_close_cache: Dict[object, Tuple[datetime.datetime, datetime.datetime, str]] = {}  # {date: (exchange_close_et, eod_at_et, source)}
        self._guardrail_eod_closed: Dict[object, set] = {}  # {date: {symbol, ...}} -- symbols already force-closed today by close_guardrail_fail_positions, so its per-minute reruns don't re-cancel/resubmit an order already in flight
        # {symbol: deque of the last N check_price_drift_stop prices, maxlen = PRICE_DRIFT_LOOKBACK_MIN / PRICE_DRIFT_CHECK_INTERVAL_MIN}
        # deque[0] is the oldest sample kept -- the ~PRICE_DRIFT_LOOKBACK_MIN-minutes-ago reference once full.
        self._price_drift_history: Dict[str, Deque[float]] = {}
        # {symbol: best price seen since entry (high for longs, low for shorts)}
        # -- the MFE give-back stop's reference, updated every poller tick by
        # check_mfe_giveback_exit(). First sighting seeds from the current
        # price (fail-open after a restart: we may miss the pre-restart peak
        # but never invent one). Cleaned up when the position is gone.
        self._mfe_peaks: Dict[str, float] = {}
        # {symbol: close-state dict} -- deliberate software closes in flight
        # (software SL / EMA9 / MFE), tracked by _request_reconciled_close().
        # Guarantees at most ONE intentional close per symbol no matter how many
        # exit rules fire on the same 5s tick (SNOW 9/3: three exit paths kept
        # cancelling each other's protection and resubmitting against a GTC-
        # reserved share -- 9 consecutive 40310000 rejections). Cleared when the
        # position confirms flat; reconciled from broker state after a restart.
        self._pending_closes: Dict[str, dict] = {}
        # {symbol: {"order_id": str, "qty": int, "is_long": bool, "chase_count": int}}
        # -- resting entry orders not yet confirmed filled; swept by _sweep_pending_entries
        self._entry_pending: Dict[str, dict] = {}
        # {symbol: {"signal": Signal, "order_type": OrderType, "queued_at": datetime}}
        # -- a signal that was blocked ONLY by _check_ema_trend_alignment,
        # held for a fresh retry instead of being discarded. 2026-08-25,
        # user request: "each blocked trade should wait for next minute
        # recheck not to completely discard the order." Swept every minute
        # by check_blocked_entries_ema; one slot per symbol, latest signal
        # wins if a symbol gets blocked again before the first one resolves.
        self._ema_blocked_entries: Dict[str, dict] = {}
        # {symbol: {"signal": Signal, "order_type": OrderType}} -- the
        # signal/order_type behind a still-resting order in order_cache,
        # kept so check_pending_entries_ema can requeue it into
        # _ema_blocked_entries (instead of just discarding it) if it has
        # to cancel the order because the gate turned against it. Same
        # 2026-08-25 request as above -- "so every minute order is
        # cancelled to place a new in next minute" once conditions realign.
        self._pending_entry_signals: Dict[str, dict] = {}
        self._stale_exit_done: object = None  # date of last completed swing stale-exit check
        # {symbol: count} -- how many times a symbol has been entered today, reset
        # on a date rollover (_entries_today_date). 2026-08-18, user request: a
        # 2nd+ same-day entry uses a trailing BUY (see REENTRY_TRAIL_PCT) instead
        # of chasing a marketable limit -- PFSA that day, 2nd EarlySqueeze entry
        # chased in at $13.52 while fading 15% off its high, filled $12.50,
        # stopped $11.72 eight minutes later. Deliberately independent of
        # win/loss -- PFSA's first exit was a ratcheted win, not a loss, and
        # still deserved the trailing-buy treatment on the 2nd entry.
        self._entries_today: Dict[str, int] = {}
        self._entries_today_date: Optional[datetime.date] = None
        # Symbols whose most recent completed trade was a loss.  This flag is
        # consumed only after the next entry succeeds, so every re-entry route
        # (including the five-second scan retry) shares the same 30m gate.
        self._loss_reentry_required: set = set()
        # {symbol: monotonic timestamp} -- short same-symbol submit debounce.
        # Broker/order-cache visibility can lag the 5s poller; this catches
        # same-process double-fires before they become duplicate live orders.
        self._recent_entry_submits: Dict[str, float] = {}
        # {symbol: {"tranches_done": int, "tranche_qty": int, "is_long": bool,
        #          "entry_price": float}} -- staged-allocation bookkeeping.
        # First tranche is submitted at signal time (_execute_entry); remaining
        # tranches are added by maybe_add_staged_tranches() only while the
        # position is not losing and the fresh EMA gate still aligns.
        self._staged_allocation: Dict[str, dict] = {}
        # symbols confirmed to have zero prior broker fill history -- lets
        # _is_reentry_signal's broker fallback (_get_entry_datetime) skip the
        # round-trip on every future entry attempt for a genuinely new name.
        self._no_history_cache: set = set()
        self._symbol_loss_counts_today: Dict[str, int] = {}
        self._loss_block_morning: set = set()
        self._loss_block_day: set = set()
        self._loss_block_date: Optional[datetime.date] = None
        self.market_state: Optional[MarketState] = None
        # 2026-09-02: guardian daily-loss halt state. _halt_until_eod blocks
        # EVERY entry/re-entry path (checked in _submit_entry_order, the single
        # order-submission funnel) once a guardian flat fires; cleared at the
        # next daily reset. _guardian_halt_closed is a per-day dedupe so the
        # orchestrator's poll tick only ever flattens once per day.
        self._halt_until_eod: bool = False
        self._guardian_halt_closed: Optional[datetime.date] = None
        # Cached result of session.daily_loss_halted (15s TTL -- avoids hammering
        # get_account on the 5s poller while keeping the halt fresh enough).
        self._loss_halted_cache: Optional[bool] = None
        self._loss_halted_cache_ts: float = 0.0
        self._rebuild_entry_log_from_orders()
        self._rebuild_order_cache_from_broker()

    def update_market_state(self, market_state: MarketState) -> None:
        """Store the active market snapshot for per-cycle execution decisions."""
        self.market_state = market_state

    @staticmethod
    def _now_et() -> datetime.datetime:
        import pytz as _pytz
        return datetime.datetime.now(_pytz.timezone("America/New_York"))

    def prewarm_entry_ema(self, symbols) -> None:
        """Fetch premarket-inclusive 1-minute bars and evaluate EMA gates before 09:30."""
        warm_symbols = [str(s).upper() for s in list(dict.fromkeys(symbols or []))[:60] if str(s).strip()]
        ready = long_ready = short_ready = 0
        for symbol in warm_symbols:
            try:
                bars = _entry_gate_bars(symbol, force_fresh=True)
                closed = _closed_1m_bars(bars)
                if len(closed) < 62:
                    log.debug(f"[PREMARKET EMA] {symbol} not ready: need 62 closed 1-min bars, have {len(closed)}")
                    continue

                ready += 1
                stub = SimpleNamespace(symbol=symbol)
                long_ok, long_reason = _entry_trend_snapshot(stub, True, force_fresh=False)
                short_ok, short_reason = _entry_trend_snapshot(stub, False, force_fresh=False)
                if long_ok:
                    long_ready += 1
                    log.info(f"[PREMARKET EMA] {symbol} LONG READY: {long_reason}")
                if short_ok:
                    short_ready += 1
                    log.info(f"[PREMARKET EMA] {symbol} SHORT READY: {short_reason}")
                if not long_ok and not short_ok:
                    log.debug(f"[PREMARKET EMA] {symbol} not aligned: long=({long_reason}); short=({short_reason})")
            except Exception as exc:
                log.debug(f"PREMARKET EMA warmup {symbol} failed: {exc}")
        log.info(
            f"[PREMARKET EMA] evaluated {ready}/{len(warm_symbols)} symbols before entry window; "
            f"long_ready={long_ready}, short_ready={short_ready}"
        )

    def _reset_symbol_loss_blocks_if_needed(self) -> None:
        today = self._now_et().date()
        if getattr(self, "_loss_block_date", None) == today:
            return
        self._loss_block_date = today
        self._symbol_loss_counts_today = {}
        self._loss_block_morning = set()
        self._loss_block_day = set()

    def _record_symbol_loss(self, symbol: str, tag: str) -> None:
        self._reset_symbol_loss_blocks_if_needed()
        count = self._symbol_loss_counts_today.get(symbol, 0) + 1
        self._symbol_loss_counts_today[symbol] = count
        now_et = self._now_et()
        session_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        first_30_done = session_open + datetime.timedelta(minutes=30)
        if session_open <= now_et < first_30_done:
            self._loss_block_morning.add(symbol)
            log.warning(f"SYMBOL BLOCK {symbol}: loss during first 30 minutes via {tag}; blocked until {LOSS_BLOCK_MORNING_END_ET} ET")
        if count >= SYMBOL_DAILY_LOSS_BLOCK_COUNT:
            self._loss_block_day.add(symbol)
            log.warning(f"SYMBOL BLOCK {symbol}: {count} losses today; blocked for rest of day")

    def _symbol_entry_block_reason(self, symbol: str) -> Optional[str]:
        self._reset_symbol_loss_blocks_if_needed()
        if is_dead_ticker(symbol):
            return f"{symbol}: blocked due to 10+ consecutive stale/empty data fetches"
        if symbol in self._loss_block_day:
            return f"{symbol}: blocked for rest of day after {self._symbol_loss_counts_today.get(symbol, 0)} losses"
        now_et = self._now_et()
        if symbol in self._loss_block_morning and now_et.strftime("%H:%M") < LOSS_BLOCK_MORNING_END_ET:
            return f"{symbol}: blocked for morning after first-30-minute loss until {LOSS_BLOCK_MORNING_END_ET} ET"
        return None
    def _entry_halt_active(self) -> bool:
        """True when no new entry orders may be submitted.

        Two independent halts feed it:
          1. Guardian halt -- _halt_until_eod was set by guardian_halt_flatten()
             (the loss guardian's flat_request.flag fired); entries stay blocked
             until the next daily reset.
          2. In-bot daily-loss limit -- session.daily_loss_halted() tripped.
             The orchestrator's scan gate has always checked this inline, but
             every RE-ENTRY path (_maybe_rearm_reentry, detect_stopped_out_
             positions, check_blocked_entries_ema, check_pending_entries_ema,
             staged tranches) skipped it -- the single biggest loss driver in
             the 9/1 reconstruction. All of those submit through
             _submit_entry_order, so gating here closes the hole by
             construction. 15s cache: the 5s poller would otherwise hammer
             get_account.
        """
        if getattr(self, "_halt_until_eod", False):
            return True
        now = time.time()
        cached = getattr(self, "_loss_halted_cache", None)
        cached_ts = getattr(self, "_loss_halted_cache_ts", 0.0)
        if cached is not None and now - cached_ts < 15:
            return cached
        regime = "bull"
        ms = getattr(self, "market_state", None)
        if ms is not None and getattr(ms, "bull_regime", None) is False:
            regime = "bear"
        try:
            halted = _session.daily_loss_halted(self.client, regime=regime)
        except Exception as e:
            log.warning(f"[LOSS-HALT] daily-loss check failed (fail-open): {e}")
            halted = False
        self._loss_halted_cache, self._loss_halted_cache_ts = halted, now
        return halted

    def _submit_entry_order(self, symbol: str, request, allow_existing_position: bool = False,
                            scale_in: bool = False) -> Optional[object]:
        """Submit at most one active DAY entry per symbol.

        2026-08-31: add an in-process same-symbol debounce. Live RBLX showed
        two accepted BUY orders inside ~9 seconds; broker order lists and the
        one-slot order_cache can lag fast retry threads, so guard immediately
        before the broker call and stamp only after Alpaca accepts.

        allow_existing_position=True is used by staged-allocation adds
        (maybe_add_staged_tranches): the whole point is to scale INTO an
        already-open position, so the "position already open" duplicate guard
        is skipped while every other guard (debounce, order_cache,
        _entry_pending, _pending_entry_signals, resting broker order) still
        applies.

        scale_in=True (staged-allocation tranches only) additionally bypasses
        the two FIRST-ENTRY-only local guards -- the 60s _recent_entry_submits
        debounce and the order_cache slot holding the first tranche's order id.
        Without this, tranche 2 could be blocked for the life of the position:
        the first entry stamps both, and order_cache for a filled order is only
        cleared opportunistically by check_pending_entries_ema's stale-cleanup.
        This is safe because the staged path itself has already verified, via
        the broker, that (a) a position is actually open (first tranche FILLED
        -- an unfilled resting order has no position to scale into) and (b) the
        fresh EMA gate passed. Every broker-side guard (active same-symbol DAY
        order) and the local pending-entry guards (_entry_pending,
        _pending_entry_signals) still apply, so a resting unfilled first
        tranche can never produce a duplicate.

        Broker-side duplicate checks (open position / resting active order)
        are best-effort: a minimal or mocked client without the position/order
        listing methods still submits (the in-process order_cache /
        _entry_pending / _pending_entry_signals guards above remain the hard
        ones). Real Alpaca clients expose both, so production keeps the
        one-position-per-symbol enforcement.
        """
        lock = getattr(self, "_entry_submission_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._entry_submission_lock = lock
        with lock:
            try:
                if self._entry_halt_active():
                    log.warning(
                        f"[LOSS-HALT] Entry blocked {symbol}: guardian/daily-loss halt active -- "
                        f"no new entries until the next daily reset"
                    )
                    return None
                now_mono = time.monotonic()
                recent = getattr(self, "_recent_entry_submits", None)
                if recent is None:
                    recent = {}
                    self._recent_entry_submits = recent
                if not scale_in:
                    # First-entry debounce + local cache slot: only meaningful
                    # for a NEW position. A scale-in tranche has already proven
                    # (broker position check in the staged path) that the first
                    # entry filled, so these two are bypassed -- see docstring.
                    last_submit = recent.get(symbol)
                    if last_submit is not None:
                        age = now_mono - last_submit
                        if age < DUPLICATE_ENTRY_BLOCK_SECONDS:
                            log.warning(
                                f"Duplicate entry blocked {symbol}: recent submit {age:.1f}s ago "
                                f"(< {DUPLICATE_ENTRY_BLOCK_SECONDS}s)"
                            )
                            return None
                        recent.pop(symbol, None)

                    if symbol in getattr(self, "order_cache", {}):
                        log.warning(f"Duplicate entry blocked {symbol}: local order_cache already has {self.order_cache[symbol]}")
                        return None
                if symbol in getattr(self, "_entry_pending", {}):
                    log.warning(f"Duplicate entry blocked {symbol}: local pending entry exists")
                    return None
                if symbol in getattr(self, "_pending_entry_signals", {}):
                    log.warning(f"Duplicate entry blocked {symbol}: local pending entry signal exists")
                    return None

                if getattr(self.client, "get_all_positions", None) is not None:
                    try:
                        positions = self._get_positions(force_refresh=True)
                        if positions.has_position(symbol) and not allow_existing_position:
                            log.warning(f"Duplicate entry blocked {symbol}: position already open")
                            return None
                    except Exception as pos_err:
                        log.warning(f"Entry position duplicate check failed for {symbol}: {pos_err} -- refusing submission")
                        return None

                active_statuses = {"new", "partially_filled", "pending_new", "accepted", "held"}
                get_orders = getattr(self.client, "get_orders", None)
                if get_orders is not None:
                    for order in get_orders() or []:
                        raw_status = getattr(order, "status", "")
                        status = str(getattr(raw_status, "value", raw_status)).lower()
                        if getattr(order, "symbol", None) == symbol and status in active_statuses:
                            log.warning(f"Duplicate entry blocked {symbol}: active broker order {order.id} status={status}")
                            return None
            except Exception as e:
                log.warning(f"Entry duplicate check failed for {symbol}: {e} -- refusing submission")
                return None

            order = self.client.submit_order(request)
            getattr(self, "_recent_entry_submits", {})[symbol] = time.monotonic()
            return order

    def _cancel_opposite_orders_before_entry(self, symbol: str, is_long: bool) -> None:
        """Cancel stale/opposite resting orders for `symbol` before entering.

        User request (staged/allocation hardening): before submitting a fresh
        entry, cancel any resting DAY order for the same symbol on the OPPOSITE
        side (e.g. a leftover SELL when entering LONG, or a leftover BUY when
        entering SHORT) so the new entry can't conflict with a stale opposite
        book. GTC trailing stops (protective exits on a held position) are
        deliberately untouched. Best-effort: any failure just logs and lets the
        entry proceed (the duplicate guards in _submit_entry_order still block
        a genuinely conflicting active order).
        """
        try:
            expected_side = OrderSide.BUY if is_long else OrderSide.SELL
            for order in (self.client.get_orders() or []):
                if getattr(order, "symbol", None) != symbol:
                    continue
                if getattr(order, "time_in_force", None) == TimeInForce.GTC:
                    continue  # protective stop -- never cancel before entry
                raw_side = getattr(order, "side", "")
                side = str(getattr(raw_side, "value", raw_side)).lower()
                expected = str(getattr(expected_side, "value", expected_side)).lower()
                if side and side != expected:
                    try:
                        self.client.cancel_order_by_id(str(order.id))
                        time.sleep(0.2)
                        log.warning(
                            f"PRE-ENTRY CANCEL {symbol}: cancelled opposite-side resting "
                            f"{side.upper()} order {order.id} before {expected.upper()} entry"
                        )
                    except Exception as cancel_err:
                        log.warning(f"PRE-ENTRY CANCEL {symbol}: opposite order {order.id} cancel failed: {cancel_err}")
        except Exception as e:
            log.debug(f"PRE-ENTRY CANCEL {symbol}: order scan failed (non-fatal): {e}")
    # -- Entry Log Rebuild (survive restarts) ----------------------------
    def _rebuild_entry_log_from_orders(self) -> None:
        """On startup, reconstruct today's entry log from Alpaca filled orders.
        Prevents swap-closes of same-day positions after a bot restart, which would
        trigger Alpaca PDT protection (error 40310100).

        2026-08-14: was BUY-only, so a SHORT position (opened via a SELL) never
        got an entry_log record after a restart. Confirmed live: SPAI entered
        10:54:41, a routine restart landed 16s later at 10:54:57, and the fresh
        process's entry_log had no 'SPAI' key at all -- which silently broke TWO
        things at once, both scoped by entry_log lookups: _trail_pct_for()
        couldn't see thin_liquidity=True anymore so protect_positions() armed a
        full 8.0% trailing stop instead of the intended 4.0% thin-liquidity half,
        and check_price_drift_stop()'s same-day scope (entry_log[sym]['date'] ==
        today) skipped SPAI entirely, leaving it with zero drift-stop coverage
        too. Now derives the correct entry side per symbol from the live
        position (BUY opened a long, SELL opened a short) instead of assuming
        BUY. thin_liquidity itself still can't be recovered this way (not
        derivable from broker order data) -- same known gap as the 0.0
        confidence / 'restored' strategy placeholder below."""
        try:
            today = datetime.date.today()
            import pytz
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            et       = pytz.timezone("America/New_York")
            try:
                positions = self.client.get_all_positions()
            except Exception:
                positions = []
            is_long_by_sym = {p.symbol: float(p.qty) > 0 for p in positions}
            # Filter to today only -- avoids fetching the full account order history
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
                sym = order.symbol
                if sym not in is_long_by_sym:
                    continue  # no open position left for this symbol -- nothing to protect
                # order.side is an OrderSide enum; str(enum) is "OrderSide.SELL", not
                # "sell" -- comparing that against a bare "buy"/"sell" literal never
                # matches. .value gives the plain string; getattr falls back to the
                # raw attribute so a plain string (e.g. from a test double) still
                # works. 2026-08-14: this was the actual root cause of the rebuild
                # being a total no-op -- the "Entry log rebuilt from today's orders"
                # log line had never once fired in the whole log history, for ANY
                # symbol, long or short.
                raw_side = getattr(order, "side", "")
                side = str(getattr(raw_side, "value", raw_side)).lower()
                entry_side = "buy" if is_long_by_sym[sym] else "sell"
                if side != entry_side:
                    continue  # this order closed/trimmed the position, not opened it
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

    def _rebuild_order_cache_from_broker(self) -> None:
        """On startup, reconstruct self.order_cache from any BUY trailing-stop
        orders still open at the broker.

        2026-08-27, user request ("make the 1 min checks more robust"):
        self.order_cache starts empty every process restart, with nothing to
        repopulate it -- check_pending_entries_ema only ever looks at
        `for sym, order_id in self.order_cache.items()`, so any resting
        re-entry order placed by a PRIOR run (check_ema9_exit's re-arm,
        _maybe_rearm_reentry, a fresh entry's own trailing-buy) silently
        drops out of the per-minute EMA recheck the instant the bot
        restarts -- it just sits there, completely unmonitored, until it
        either fills on its own or someone notices. Confirmed live:
        SAIL/MARA/ASAN were all resting BUY trailing-stop orders from
        earlier today, invisible to a freshly-restarted process, on a day
        this bot restarted more than half a dozen times. Same fix shape as
        _rebuild_entry_log_from_orders right above -- reconstruct from
        broker truth instead of assuming in-memory state survived.

        Can't recover self._pending_entry_signals (the original Signal
        that would let a cancelled order requeue into _ema_blocked_entries
        for retry) -- that context doesn't exist in the order itself. A
        recovered order that later gets cancelled by check_pending_entries_ema
        just won't requeue, same graceful degradation that already applies
        to every check_ema9_exit-armed re-entry (which never had a stored
        signal either -- see that method's docstring).

        2026-08-27, found while hardening check_pending_entries_ema
        ("ensure 1min checks are robust to cancel unfilled order if
        conditions change"): side=="buy" + trailing_stop alone is NOT
        enough to identify an entry order -- a SHORT position's protective
        buy-to-cover stop is submitted with the exact same side and order
        type (see _create_bracket_order/protect_positions), differing only
        in time_in_force (GTC for the protective stop, DAY for a real
        entry). LONG_ONLY_MODE=False on this account, so shorts are live:
        without this check, a held short's protective stop could get
        registered here as if it were a stale pending entry, and later
        genuinely CANCELLED by check_pending_entries_ema on an EMA-gate
        miss -- leaving a live short position completely unprotected.
        time_in_force==DAY is the actual distinguishing signal, not side."""
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            open_orders = self.client.get_orders(filter=req)
            recovered = []
            for order in open_orders:
                raw_type = getattr(order, "order_type", "")
                otype = str(getattr(raw_type, "value", raw_type)).lower()
                if "trailing_stop" not in otype:
                    continue
                if getattr(order, "time_in_force", None) != TimeInForce.DAY:
                    continue  # GTC trailing stop == protective exit order, never an entry
                self.order_cache[order.symbol] = str(order.id)
                recovered.append(order.symbol)
            if recovered:
                log.info(f"order_cache rebuilt from broker: {', '.join(recovered)}")
        except Exception as e:
            log.warning(f"_rebuild_order_cache_from_broker failed (non-fatal): {e}")

    def _current_market_state(self) -> MarketState:
        if self.market_state is not None:
            return self.market_state
        raise RuntimeError("EnhancedExecutor requires market_state to be set before execution")

    # -- Position Cache ----------------------------------------------------
    def _has_pending_close(self, symbol: str) -> bool:
        """True if *symbol* already has a resting non-GTC order (i.e. something
        other than its routine protective trailing stop) -- meaning a swap-close
        was already submitted for it on an earlier cycle and just hasn't filled
        yet (routine in pre/after-hours illiquidity). Candidate-finders use this
        to avoid re-selecting the same position for a second close order before
        the first one clears -- confirmed in production 2026-08-05: without this,
        RRC and GCT each got a duplicate close submitted 10 minutes apart, and
        both swaps were for nothing since the intended new entry (PLTR/ONDS)
        still got skipped on insufficient buying power either time (freed cash
        from an unfilled close doesn't settle same-cycle)."""
        try:
            for o in (self.client.get_orders() or []):
                if o.symbol != symbol:
                    continue
                if getattr(o, "time_in_force", None) != TimeInForce.GTC:
                    return True
            return False
        except Exception:
            return False

    def _find_weakest_position(self) -> Optional[str]:
        """Return the symbol of the open long position with the worst unrealized P&L %.
        Skips positions entered today (protected for full day), those already
        closed this cycle, and those already mid-close from a prior cycle.
        Returns None if no closable position found.

        Does NOT require qty_available > 0: every position here normally carries
        a full-size GTC trailing stop (qty_available is always 0 as a result),
        and _attempt_swap already cancels that resting order before closing --
        requiring qty_available > 0 here meant this never found a candidate in
        practice, silently defeating the whole swap-on-high-confidence feature.
        """
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
                and p.symbol not in self._swap_cycle_closed
                and p.symbol not in entered_today
                and not re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', p.symbol)  # skip OCC option symbols
                and not self._has_pending_close(p.symbol)
            ]
            if not longs:
                return None
            worst = min(longs, key=lambda p: float(p.unrealized_plpc))
            return worst.symbol
        except Exception as e:
            log.warning(f"_find_weakest_position error: {e}")
            return None

    def _find_stalest_position(self, min_hours: float = NO_GAIN_EXIT_HOURS) -> Optional[str]:
        """Return the symbol of the oldest closable long position held >= min_hours
        (default: same 24h bar as NO_GAIN_EXIT_HOURS), for swap-out when a new
        high-confidence signal arrives and the book is full. Age takes priority
        over P&L here -- a day-old idea makes room for a stronger new one whether
        it's currently green or red. This is on top of (not instead of)
        close_no_gain_positions, which separately force-exits anything stale
        AND non-positive every cycle regardless of new signals."""
        try:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            positions = self.client.get_all_positions()
            candidates = []
            for p in positions:
                sym = p.symbol
                if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                    continue  # options legs -- managed separately
                if float(p.qty) <= 0:
                    continue
                if sym in self._swap_cycle_closed:
                    continue
                if self._has_pending_close(sym):
                    continue
                entry_dt = self._get_entry_datetime(sym)
                if entry_dt is None:
                    continue
                held_hours = (now_utc - entry_dt).total_seconds() / 3600
                if held_hours < min_hours:
                    continue
                candidates.append((held_hours, sym))
            if not candidates:
                return None
            candidates.sort(reverse=True)  # oldest first
            return candidates[0][1]
        except Exception as e:
            log.warning(f"_find_stalest_position error: {e}")
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
                and p.symbol not in self._swap_cycle_closed
                and p.symbol not in entered_today
                and not re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', p.symbol)  # skip OCC option symbols
                and not self._has_pending_close(p.symbol)
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
                daytrade_count=int(raw.daytrade_count or 0),
                pattern_day_trader=str(getattr(raw, "pattern_day_trader", False)).lower() in ("1", "true", "yes"),
                maintenance_margin=float(getattr(raw, "maintenance_margin", None) or 0.0),
                timestamp=now,
            )
        return self._account_cache

    @property
    def shorting_blocked(self) -> bool:
        """Live account-wide short-selling gate -- Alpaca's own Reg T equity
        minimum (MIN_EQUITY_FOR_SHORT), read fresh off the 2s-TTL account
        cache every time so it self-corrects the moment equity crosses back
        above the floor. Replaces an old sticky `self.shorting_blocked = True`
        flag that a single misclassified broker rejection could leave stuck
        for the rest of the session with no way back -- confirmed 2026-08-07:
        one INDI no-borrow rejection, misread as account-wide, disabled every
        short for hours despite the account's Shorting Enabled setting being
        on the whole time."""
        return self._get_account().equity < MIN_EQUITY_FOR_SHORT

    # -- Swap -----------------------------------------------------------
    def _attempt_swap(self, signal: Signal, swap_only: bool) -> Tuple[bool, Optional[str]]:
        """Try to close the stalest (24h+, falling back to weakest P&L) position
        to make room / free cash for *signal*. Shared by the buying-power gate
        (cash-starved even below max positions) and the max-positions gate.

        Returns (closed, block_reason):
          closed=True        a position was closed -- caller should refresh
                              account/position state before re-checking gates.
          block_reason=str   the close attempt itself failed and entry should
                              be denied (position may be left unprotected).
          Otherwise (False, None): no candidate to swap -- caller proceeds
          without a swap (matches the pre-existing "allow entry anyway" path).
        """
        label = "SWAP (bear)" if swap_only else "SWAP"
        stale_candidate = self._find_stalest_position()
        if stale_candidate:
            weakest, swap_reason = stale_candidate, "stale 24h+"
        else:
            weakest, swap_reason = self._find_weakest_position(), "weakest"
        if not weakest:
            log.debug(f"No swappable position found for {signal.symbol}")
            return False, None

        log.info(
            f"{label}: closing {weakest} ({swap_reason}) to make room for "
            f"{signal.symbol} (conf={signal.confidence:.0%})"
        )
        # Any resting order for this symbol -- the GTC trailing stop, or a
        # leftover DAY close from a prior NO-GAIN/stale-exit attempt -- reserves
        # qty and makes Alpaca reject close_position() as a wash trade (confirmed
        # in production: 40310000, "opposite side market/stop order exists").
        # Cancel ALL of them first, not just the GTC, so the swap-close actually
        # goes through (GTC-only cancel here previously had a 0% success rate).
        weakest_gtc_id = None
        try:
            for o in (self.client.get_orders() or []):
                if o.symbol != weakest:
                    continue
                if getattr(o, "time_in_force", None) == TimeInForce.GTC:
                    weakest_gtc_id = o.id
                self.client.cancel_order_by_id(str(o.id))
                time.sleep(0.4)
        except Exception as cancel_err:
            log.warning(f"SWAP {weakest}: order cancel failed, close may reject: {cancel_err}")

        try:
            self._no_rearm.add(weakest)  # portfolio rebalance, not a verdict on this symbol
            self.client.close_position(weakest)
            self._swap_cycle_closed.add(weakest)
            # Closing a prior-day position is NOT a day trade -- do not count against PDT
            return True, None
        except Exception as e:
            err_str = str(e)
            if "40310100" in err_str:
                # Alpaca PDT protection: position was entered today -- can't close same day.
                # Mark as today's entry so it's never selected as swap candidate again.
                self._entry_log[weakest] = {
                    "strategy": "restored",
                    "date": datetime.date.today(),
                    "confidence": 0.0,
                }
                log.warning(
                    f"SWAP skip {weakest}: PDT same-day protection (40310100) -- "
                    f"marked as today entry, will not retry this session"
                )
                # Don't block the new signal -- allow entry without the swap
                return False, None
            log.warning(f"SWAP close failed for {weakest}: {e}")
            if weakest_gtc_id:
                # We cancelled its GTC stop to attempt the close, and the
                # close itself failed -- re-arm protection immediately
                # rather than leave the position naked.
                try:
                    weakest_pos = next(
                        (p for p in self.client.get_all_positions() if p.symbol == weakest), None
                    )
                    if weakest_pos is not None:
                        w_qty     = int(float(weakest_pos.qty))
                        w_current = float(weakest_pos.current_price)
                        w_trail   = _atr_trail_pct_for(weakest, w_current, self._entry_log)[0]
                        self.client.submit_order(TrailingStopOrderRequest(
                            symbol        = weakest,
                            qty           = abs(w_qty),
                            side          = OrderSide.SELL if w_qty > 0 else OrderSide.BUY,
                            type          = AlpacaOrderType.TRAILING_STOP,
                            time_in_force = TimeInForce.GTC,
                            trail_percent = w_trail,
                        ))
                        log.warning(f"SWAP {weakest}: re-armed GTC trailing stop after failed close")
                except Exception as rearm_err:
                    log.error(f"SWAP {weakest}: close failed AND GTC re-arm failed -- position may be UNPROTECTED: {rearm_err}")
            return False, f"Swap close failed: {e}"

    # -- Validation --------------------------------------------------------
    def _validate_trade(self, signal: Signal, acct: AccountSnapshot, order_type: OrderType, swap_only: bool = False, bypass_pdt: bool = False) -> Tuple[bool, Optional[str]]:
        if USE_VIX_ROC_FILTER:
            allow, roc = check_vix_roc_filter()
            if not allow:
                return False, f"VIX spike filter: {roc:.1f}% increase"

        # PDT -- use live broker count (survives restarts).
        # Block only when the count EXCEEDS the limit (4+) -- an actual PDT violation.
        # At exactly 3/3: new buys are allowed because they are held overnight (not same-day
        # round-trips) and therefore do NOT count as additional day trades.
        #
        # 2026-08-26, user request ("remove cool off or any other software
        # blocks for stock reentries such as pdt or other blocks if any"):
        # bypass_pdt (set by check_blocked_entries_ema only when
        # _is_reentry_signal says this really is a re-entry, not a fresh
        # first-time signal) skips the BLOCK below but still logs/alerts --
        # this is the bot's own, extra-cautious ceiling on top of what the
        # broker itself enforces; the trailing-buy re-entry path
        # (_maybe_rearm_reentry) already never had this check at all, so
        # this just makes check_blocked_entries_ema's retry path consistent
        # with it. Alpaca's actual PDT enforcement is untouched -- a real
        # violation still gets rejected broker-side regardless of this flag.
        if acct.pattern_day_trader and acct.equity < PDT_ACCOUNT_MIN and acct.daytrade_count > PDT_MAX_TRADES:
            msg = (
                f"PDT VIOLATION: {acct.daytrade_count} day trades used "
                f"(limit {PDT_MAX_TRADES}, equity ${acct.equity:,.0f}) -- "
                f"account may be flagged as Pattern Day Trader. Review immediately!"
            )
            log.error(msg)
            if not getattr(self, "_pdt_violation_alerted", False):
                send_email("[APEXTRADER] PDT VIOLATION ALERT", msg)
                self._pdt_violation_alerted = True
            if not bypass_pdt:
                return False, f"PDT violation: {acct.daytrade_count}/{PDT_MAX_TRADES} day trades exceeded"
            log.warning(f"{signal.symbol}: PDT ceiling exceeded but this is a re-entry -- bypassing the bot's own limit, letting the broker decide")
        dt_left = self.pdt.remaining(acct.equity, acct.daytrade_count, acct.pattern_day_trader)
        if acct.pattern_day_trader and dt_left <= PDT_WARN_AT_REMAINING and acct.equity < PDT_ACCOUNT_MIN:
            log.warning(f"PDT WARNING: only {dt_left} day trade(s) remaining (equity ${acct.equity:,.0f})")

        # Alpaca's own Reg T minimum to short at all -- checked live every time
        # (not cached/session-flag) so shorting resumes automatically the
        # moment equity crosses back above the floor, no restart needed.
        if order_type == OrderType.SHORT and acct.equity < MIN_EQUITY_FOR_SHORT:
            return False, f"equity ${acct.equity:,.0f} < ${MIN_EQUITY_FOR_SHORT:,.0f} minimum required to short"

        # Skip hard-to-borrow shorts cached from previous failures this session
        if order_type == OrderType.SHORT and signal.symbol in self._htb_cache:
            return False, f"{signal.symbol} hard-to-borrow (cached)"

        # 2026-08-24, user request: no post-loss re-entry cooldown at all --
        # every entry (cooldown or not) already goes through the trailing-buy
        # path (_create_bracket_order), which can't fill mid-fall the way a
        # marketable chase could. Protection against a re-firing signal is
        # the exit stack alone now: the trailing stop, check_ema9_exit
        # (per-minute), and the standalone software stop-loss. See SOXS
        # (2026-08-05, 22 trades/-$605 net re-firing the same losing signal)
        # for why that stack matters if this gets revisited.

        # Momentum entry freshness (long only -- a short entry isn't chasing a
        # gap up) -- reject a gap/momentum signal that's already faded off its
        # recent high by the time we're about to submit. See engine/config.py
        # MOMENTUM_FRESHNESS_* for the reasoning and known limitations.
        if order_type == OrderType.LONG:
            fresh, fade_reason = _check_momentum_freshness(signal)
            valid, block_reason = _resolve_freshness_reject(signal, fresh, fade_reason)
            if not valid:
                return False, block_reason
            if fade_reason:
                log.info(f"[SIZE] {fade_reason} -- trading anyway at reduced size")

        # EMA7 slope trend alignment (both directions) -- see
        # _check_ema_trend_alignment for the reasoning.
        trend_ok, trend_reason = _entry_trend_snapshot(signal, order_type == OrderType.LONG)
        if not trend_ok:
            self._ema_entry_confirmations.pop(signal.symbol, None)
            # 2026-08-25, user request: "each blocked trade should wait for
            # next minute recheck not to completely discard the order" ->
            # "the trade idea should check for every minute conditions to
            # see when the new condition is met to reenter than completely
            # discard a trade signal" -- a signal blocked ONLY by this EMA
            # gate (not PDT/buying-power/short-restriction/etc., which
            # aren't "wait for the trend" situations) gets queued instead
            # of just discarded. check_blocked_entries_ema re-checks it
            # every minute and fires _execute_entry fresh the moment the
            # gate agrees, same as any other entry -- see that method.
            self._ema_blocked_entries[signal.symbol] = {
                "signal": signal, "order_type": order_type,
                "queued_at": datetime.datetime.now(datetime.timezone.utc),
            }
            return False, trend_reason


        log.info(trend_reason)

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
            log.warning(f"{signal.symbol}: asset status check failed ({e}) -- proceeding cautiously")

        # Pending order guard: don't submit a second order if one is already live/filling
        if signal.symbol in self.order_cache:
            cached_id = self.order_cache[signal.symbol]
            try:
                cached_order = self.client.get_order_by_id(cached_id)
                active_statuses = {"new", "partially_filled", "pending_new", "accepted", "held"}
                if str(getattr(cached_order, "status", "")).lower() in active_statuses:
                    return False, f"Pending order already active for {signal.symbol} (id={cached_id})"
                else:
                    # Order is filled/cancelled -- remove stale cache entry
                    del self.order_cache[signal.symbol]
            except Exception:
                # Can't verify -- keep cache entry intact to avoid double-submit risk
                return False, f"Could not verify order status for {signal.symbol} (id={cached_id}) -- skipping to be safe"

        positions = self._get_positions()

        # Dynamic max positions: use equity-based strategic capacity (not raw buying_power).
        # buying_power can be artificially depressed by leveraged/inverse ETF margin requirements,
        # causing the bot to permanently block new entries even when capital is available.
        # We compute effective_max from equity x position_size_pct, then separately gate each
        # execution on whether buying_power is sufficient for one position.
        #
        # 2026-08-13, user request ("max position increase to 24"): SMALL_ACCOUNT_MAX_POSITIONS
        # (24) had been defined in config.py since before this file existed but was dead --
        # imported here and never referenced, so every account, small or not, was silently
        # capped at plain MAX_POSITIONS (12). Wired it in as the ceiling for accounts under
        # SMALL_ACCOUNT_EQUITY_THRESHOLD. Also switched the equity_capacity estimate to use
        # THIN_LIQUIDITY_POSITION_SIZE_PCT (3%) when the signal itself is a thin-liquidity
        # admit, not the flat 7.5% small-account rate -- otherwise the affordability math
        # still silently caps out around 12 (7.5% x 12 ~ 90% of equity) regardless of the new
        # 24 ceiling, since today's guardrail widening means most newly-eligible signals will
        # actually execute at the smaller 3% size, not 7.5%.
        _pos_size_pct = (
            THIN_LIQUIDITY_POSITION_SIZE_PCT if getattr(signal, "thin_liquidity", False)
            else SMALL_ACCOUNT_POSITION_SIZE_PCT if acct.equity < SMALL_ACCOUNT_EQUITY_THRESHOLD
            else POSITION_SIZE_PCT
        )
        _pos_size_dollars = max(MIN_POSITION_DOLLARS, acct.equity * _pos_size_pct / 100.0)
        # Strategic max: how many positions our equity allocation strategy supports.
        # 2026-09-04: leverage-aware -- the deployable gross budget is
        # equity x MAX_PORTFOLIO_LEVERAGE (with a 5% gross reserve), not just
        # 95% of unleveraged equity. At the 2.0x cap and the 10% base size
        # this yields ~19 slots (the SMALL_ACCOUNT_MAX_POSITIONS=24 count cap
        # still applies on top), instead of ~9 that silently kept the book
        # near 0.9x even when the leverage ceiling allowed 2x.
        _gross_budget = acct.equity * MAX_PORTFOLIO_LEVERAGE * 0.95
        equity_capacity = max(1, int(_gross_budget / _pos_size_dollars))
        _max_positions_cap = (
            SMALL_ACCOUNT_MAX_POSITIONS
            if acct.equity < SMALL_ACCOUNT_EQUITY_THRESHOLD
            else MAX_POSITIONS
        )
        effective_max = min(_max_positions_cap, equity_capacity)
        log.debug(
            f"[DBG] effective_max={effective_max} equity={acct.equity:.0f} bp={acct.buying_power:.0f} "
            f"pos_size=${_pos_size_dollars:.0f} ({_pos_size_pct:.0f}%) equity_cap={equity_capacity} "
            f"max_cap={_max_positions_cap}"
        )

        # -- Buying power gate (must come first) ---------------------------
        # Check if sufficient buying power for this trade (primary constraint).
        # This allows entry even when at max positions if capital is available.
        margin = 2.0 if order_type == OrderType.SHORT else 1.0
        min_usable = (SMALL_ACCOUNT_MIN_POSITION_DOLLARS
                      if acct.equity < SMALL_ACCOUNT_EQUITY_THRESHOLD
                      else MIN_POSITION_DOLLARS)
        min_bp_needed = min_usable * margin

        if acct.buying_power < min_bp_needed:
            # Cash-starved even below max positions (e.g. margin tied up by
            # leveraged/inverse ETFs) -- a high-confidence signal should still
            # be able to bump a stale/weak position for the cash rather than
            # just being skipped every cycle until something exits on its own.
            if SWAP_ON_FULL and signal.confidence >= SWAP_MIN_CONFIDENCE and positions.total_count > 0:
                closed, block_reason = self._attempt_swap(signal, swap_only)
                if block_reason:
                    return False, block_reason
                if closed:
                    acct = self._get_account(force_refresh=True)
                    positions = self._get_positions(force_refresh=True)
            if acct.buying_power < min_bp_needed:
                return False, (
                    f"Insufficient buying power: ${acct.buying_power:,.0f} "
                    f"(need ${min_bp_needed:,.0f} for minimum position)"
                )

        # -- Max positions gate (secondary; optional swap if at limit) -----
        if positions.total_count >= effective_max:
            if not (SWAP_ON_FULL and signal.confidence >= SWAP_MIN_CONFIDENCE):
                # At max but BP available -- allow entry (no swap needed)
                log.debug(
                    f"At max positions {positions.total_count}/{effective_max} but allowing entry "
                    f"due to available BP ${acct.buying_power:,.0f}"
                )
            else:
                # Strong confidence signal + at max: prefer swap to maintain position count.
                closed, block_reason = self._attempt_swap(signal, swap_only)
                if block_reason:
                    return False, block_reason
                if closed:
                    positions = self._get_positions(force_refresh=True)

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
        """Returns (shares, skip_reason). Downsizes if BP constrained, skips if below min.

        2026-08-18, user request: "prioritize the full number than dollar
        value... 10% limit puts 1.8 stock then round to 2 stocks if there is
        cash available" -- `desired` rounds to the NEAREST share instead of
        always truncating down, so a 1.8-share target becomes 2 rather than
        1 (silently using only 56% of the intended allocation). The caps
        below (max_bp, max_concentration) stay floored with
        int() -- those are hard capacity ceilings, not targets, so "if
        there is cash available" is enforced by the min() below: rounding
        desired up only sticks when a cap doesn't clamp it back down."""
        margin  = 2.0 if order_type == OrderType.SHORT else 1.0
        usable  = buying_power * (1.0 - MIN_BUYING_POWER_PCT / 100.0)
        account_snapshot = self._account_cache or self._get_account()  # use cached if available
        # New orders use broker buying power. The portfolio leverage cap is
        # ALSO enforced pre-trade in this function (gross-headroom bound
        # below, 2026-09-04) and after fills by enforce_portfolio_leverage().
        desired = round(risk_info["dollar_amount"] / signal.price)
        max_bp  = int(usable / (signal.price * margin))
        max_concentration = int(account_snapshot.equity * MAX_POSITION_CONCENTRATION_PCT / 100.0 / signal.price)

        # 2026-09-04, user request ("increase alpaca margin total utilization
        # to 2X the portfolio value"): pre-trade gross-exposure headroom.
        # Previously this cap was enforced ONLY after fills by
        # enforce_portfolio_leverage()'s 10-minute grid -- the book could sit
        # over the cap for up to 10 minutes and then get trimmed (pure
        # turnover + spread cost). Bound the NEW order here so projected
        # gross exposure can never exceed equity x MAX_PORTFOLIO_LEVERAGE at
        # submission time:
        #   filled positions (abs market value, options excluded)
        # + resting entry orders' notional (fresh/re-entry/staged)
        # + this order's notional
        #   <= equity x MAX_PORTFOLIO_LEVERAGE
        try:
            gross_now = self._get_positions().total_market_value()
        except Exception:
            gross_now = 0.0  # fail open to the other caps, never crash sizing
        pending_notional = 0.0
        for p_sym, p_info in getattr(self, "_pending_entry_signals", {}).items():
            if p_sym == signal.symbol:
                continue  # this symbol's own resting order is being replaced/rechecked
            try:
                p_price = float(getattr(p_info.get("signal"), "price", 0.0) or 0.0)
                p_qty = float((getattr(self, "_entry_pending", {}).get(p_sym, {}) or {}).get("qty", 0) or 0)
                if p_price > 0 and p_qty > 0:
                    pending_notional += p_price * p_qty
            except Exception:
                continue
        cap_value = account_snapshot.equity * MAX_PORTFOLIO_LEVERAGE
        gross_headroom = max(0.0, cap_value - gross_now - pending_notional)
        max_leverage = int(gross_headroom / (signal.price * margin))

        # Broker buying power, the single-symbol concentration cap, and the
        # whole-book leverage cap (pre-trade) all limit placement; the
        # post-fill enforce_portfolio_leverage() grid remains a backstop for
        # price appreciation.
        shares  = min(desired, max_bp, max_concentration, max_leverage)

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

    # -- Bracket Prices ----------------------------------------------------------
    def _calculate_bracket_prices(self, signal: Signal, risk_info: Dict, order_type: OrderType) -> tuple:
        if signal.atr_stop and signal.atr_stop > 0:
            # ATR-based 2:1 R:R -- stop at 1.5xATR, target at 2x the risk
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

    # -- Entry + Trailing Stop Order ------------------------------------------
    def _handle_short_rejection(self, signal: Signal, e: Exception) -> None:
        """Broker rejected a short with "cannot be sold short" / 40310000 /
        "account is not allowed to short". Alpaca reuses that same wording for
        two different causes that need different handling: a genuine
        per-symbol no-borrow-available condition (should stick for the
        session) versus the account-wide Reg T equity minimum,
        MIN_EQUITY_FOR_SHORT (transient -- must NOT poison one ticker's cache).
        Confirmed 2026-08-10: FIG and RIG both got cached as hard-to-borrow
        from rejections that fired while equity was under $2,000, then stayed
        stuck "not shortable" for the rest of the session even after equity
        recovered -- checking equity here first is what `shorting_blocked`
        already does live, so re-check it rather than caching the symbol."""
        if self.shorting_blocked:
            log.warning(
                f"Short blocked {signal.symbol}: account equity below "
                f"${MIN_EQUITY_FOR_SHORT:,.0f} minimum -- not caching as HTB, "
                "will retry once equity recovers"
            )
            return
        self._htb_cache.add(signal.symbol)
        log.warning(f"Short blocked {signal.symbol} (not shortable/insufficient BP): {e}")

    def _entries_today_count(self, symbol: str) -> int:
        """How many times `symbol` has already been entered today -- resets
        on a date rollover. Read BEFORE submitting a new entry (0 = first
        entry today, so a return value > 0 means the one about to be
        submitted is a re-entry). See _entries_today in __init__."""
        today = datetime.date.today()
        if self._entries_today_date != today:
            self._entries_today.clear()
            self._entries_today_date = today
        return self._entries_today.get(symbol, 0)

    def _is_reentry_signal(self, symbol: str, is_long: bool = True) -> bool:
        """True if `symbol` should use the trailing-buy entry path instead of
        the normal marketable chase: a 2nd+ same-day entry, OR any symbol with
        SOME prior fill history at all -- broker-confirmed via
        _get_entry_datetime, since _entry_log alone doesn't survive this
        bot's frequent restarts.

        2026-08-18, user request: "the re entry to trail buy doesn't have to
        come from cool down list only... even if the non cool down reentry to
        a prior traded stock is entering put in a trail buy order" -- SNDQ
        that day: stopped 09:02 (no cooldown block issue, still same-day
        entry), but the general case is a symbol that WON its last trade or
        was traded days ago -- same-day count alone catches neither, and it
        deserves the same falling-knife protection on re-entry.

        2026-08-24, user request: dropped the post-loss cooldown branch that
        used to live here -- there's no cooldown window left to check (see
        _validate_trade), everything else about this function is unchanged.

        Broker lookup only runs once per symbol per process lifetime -- a
        confirmed "never traded" result is cached in _no_history_cache so a
        genuinely new symbol doesn't pay a broker round-trip on every single
        entry attempt forever."""
        if self._entries_today_count(symbol) > 0:
            return True
        if symbol in self._no_history_cache:
            return False
        has_history = self._get_entry_datetime(symbol, is_long) is not None
        if not has_history:
            self._no_history_cache.add(symbol)
        return has_history

    def _create_bracket_order(self, signal: Signal, shares: int, risk_info: Dict, order_type: OrderType) -> bool:
        """Submit a DAY trailing-stop entry. Protective 1.5% GTC trailing stop is attached after fill."""
        side          = OrderSide.BUY  if order_type == OrderType.LONG else OrderSide.SELL
        trail_pct     = TRAIL_STOP_PCT
        is_long_entry = order_type == OrderType.LONG
        is_reentry    = self._is_reentry_signal(signal.symbol, is_long_entry)

        try:
            entry_req = TrailingStopOrderRequest(
                symbol          = signal.symbol,
                qty             = shares,
                side            = side,
                type            = AlpacaOrderType.TRAILING_STOP,
                time_in_force   = TimeInForce.DAY,
                trail_percent   = REENTRY_TRAIL_PCT,
                client_order_id = f"apex-entry-{signal.strategy}-{signal.symbol}-{int(time.time())}",
            )
            order = self._submit_entry_order(signal.symbol, entry_req)
            if order is None:
                return False
            self.order_cache[signal.symbol] = order.id
            self._pending_entry_signals[signal.symbol] = {"signal": signal, "order_type": order_type}
            self._entries_today[signal.symbol] = self._entries_today.get(signal.symbol, 0) + 1
            log.info(
                f"{signal.symbol}: {'re-entry' if is_reentry else 'entry'} -- {REENTRY_TRAIL_PCT:.2f}% trailing-stop "
                f"{'BUY' if is_long_entry else 'SELL'} entry; exit protection {TRAIL_STOP_PCT:.1f}% after fill"
            )
        except Exception as e:
            err = str(e).lower()
            if order_type == OrderType.SHORT and ("cannot be sold short" in err or "40310000" in err or "account is not allowed to short" in err):
                self._handle_short_rejection(signal, e)
            elif order_type != OrderType.SHORT and ("cannot be sold short" in err or "40310000" in err):
                log.warning(f"Buy rejected for {signal.symbol} (broker): {e}")
            elif "insufficient buying power" in err:
                log.warning(f"Entry skip {signal.symbol}: insufficient buying power")
            else:
                log.error(f"Entry order failed {signal.symbol}: {e}")
            return False

        try:
            self._cover_naked_positions()
        except Exception as e:
            log.debug(f"{signal.symbol}: immediate post-entry protection check failed (5s thread will retry): {e}")

        self._log_bracket(signal, shares, risk_info, trail_pct, None, order_type)
        return True

    def _log_bracket(self, signal, shares, risk_info, trail_pct, _tp_unused, order_type):
        action    = "BUY"  if order_type == OrderType.LONG else "SHORT"
        tier      = risk_info["tier"]
        atr_pct   = risk_info.get("atr_pct", 0)
        alloc_pct = risk_info["allocation_pct"]

        if USE_DYNAMIC_TIERS and atr_pct > 0 and USE_RISK_EQUALIZED_SIZING:
            log.info(f"{action} {signal.symbol}: {shares} @ ${signal.price:.2f} submitted "
                     f"({alloc_pct:.1f}% pos) | TRAILING SL {trail_pct:.1f}% "
                     f"| Tier: {tier} (ATR {atr_pct:.1f}%) | {signal.strategy}")
        else:
            log.info(f"{action} {signal.symbol}: {shares} @ ${signal.price:.2f} submitted "
                     f"| TRAILING SL {trail_pct:.1f}% | Tier: {tier} | {signal.strategy}")

    # ---- Simple Order --------------------------------------------------------------------------------------
    def _create_simple_order(self, signal: Signal, shares: int, order_type: OrderType) -> bool:
        """Non-bracket entry path -- fires when _create_bracket_order's own
        TrailingStopOrderRequest attempt raised (see its except block) or
        outside regular hours. Submits a bracket MarketOrderRequest (market
        entry leg plus a take_profit and a stop_loss leg) so the fallback
        path still carries a hard maximum-loss limit at fill, priced off
        signal.price (no re-chase loop: a bracket either fills or cancels at
        end of day on its own, so nothing for _sweep_pending_entries to do).

        2026-08-18, user request: entries never use extended hours -- EXTENDED_HOURS
        now only governs exit paths (a stop-loss must be able to fire outside
        regular hours; a new position never needs to open outside them). In
        practice this is already unreachable -- ENTRY_WINDOW_START/END_ET now
        match regular hours -- but hardcoded here too rather than relying
        solely on that window (FORCE_SCAN bypasses it)."""
        side   = OrderSide.BUY if order_type == OrderType.LONG else OrderSide.SELL
        action = "BUY"         if order_type == OrderType.LONG else "SHORT"

        try:
            coid = f"apex-{signal.strategy}-{signal.symbol}-{int(time.time())}"
            is_long_entry = order_type == OrderType.LONG
            stop_price = round(
                signal.price * (1 - TRAIL_STOP_PCT / 100) if is_long_entry
                else signal.price * (1 + TRAIL_STOP_PCT / 100),
                2,
            )
            take_profit = round(
                signal.price * (1 + ATR_TP_RATIO * TRAIL_STOP_PCT / 100) if is_long_entry
                else signal.price * (1 - ATR_TP_RATIO * TRAIL_STOP_PCT / 100),
                2,
            )
            req = MarketOrderRequest(
                symbol          = signal.symbol,
                qty             = shares,
                side            = side,
                time_in_force   = TimeInForce.DAY,
                order_class     = OrderClass.BRACKET,
                take_profit     = TakeProfitRequest(limit_price=take_profit),
                stop_loss       = StopLossRequest(stop_price=stop_price),
                client_order_id = coid,
            )
            order = self._submit_entry_order(signal.symbol, req)
            if order is None:
                return False
            self.order_cache[signal.symbol] = order.id
            if not hasattr(self, "_pending_entry_signals"):
                self._pending_entry_signals = {}
            self._pending_entry_signals[signal.symbol] = {"signal": signal, "order_type": order_type}
            log.info(
                f"{action} {signal.symbol}: {shares} @ ${signal.price:.2f} -- trailing "
                f"market bracket with fixed {TRAIL_STOP_PCT:.1f}% protection | {signal.strategy}"
            )
            return True

        except Exception as e:
            err = str(e).lower()
            if order_type == OrderType.SHORT and ("cannot be sold short" in err or "40310000" in err or "account is not allowed to short" in err):
                self._handle_short_rejection(signal, e)
            elif order_type != OrderType.SHORT and ("cannot be sold short" in err or "40310000" in err):
                # Inverse ETF or other buy rejected by broker -- do not poison short flag
                log.warning(f"Buy rejected for {signal.symbol} (broker): {e}")
            elif "insufficient buying power" in err:
                log.warning(f"Skip {signal.symbol}: insufficient buying power")
            else:
                log.error(f"{action} order error {signal.symbol}: {e}")
            return False

    # -- Entry (unified) ---------------------------------------------------
    def _execute_entry(self, signal: Signal, acct: AccountSnapshot, order_type: OrderType, swap_only: bool = False, bypass_pdt: bool = False) -> bool:
        symbol_block = self._symbol_entry_block_reason(signal.symbol)
        if symbol_block:
            log.info(f"Skip {signal.symbol}: {symbol_block}")
            self._ema_blocked_entries.pop(signal.symbol, None)
            return False
        valid, reason = self._validate_trade(signal, acct, order_type, swap_only=swap_only, bypass_pdt=bypass_pdt)
        if not valid:
            if reason:
                log.info(f"Skip {signal.symbol}: {reason}")
            return False

        risk_info = calculate_risk_adjusted_size(acct.equity, signal.symbol, signal.price)
        if signal.symbol in getattr(self, "_loss_reentry_required", set()):
            factor = max(0.0, 1.0 - REENTRY_SIZE_REDUCTION_PCT / 100.0)
            old_amt = risk_info.get("dollar_amount", 0.0)
            old_pct = risk_info.get("allocation_pct", 0.0)
            risk_info["dollar_amount"] = old_amt * factor
            risk_info["allocation_pct"] = old_pct * factor
            log.info(
                f"REENTRY SIZE {signal.symbol}: prior-loss re-entry reduced by "
                f"{REENTRY_SIZE_REDUCTION_PCT:.0f}% (${old_amt:,.0f} -> ${risk_info['dollar_amount']:,.0f})"
            )

        # Every signal reaching this point already passed the hard confidence
        # and EMA gates. Do not halve its allocation at the confidence floor;
        # use the full configured position target and let buying power,
        # concentration, and actual-position leverage caps control exposure.
        log.debug(
            f"[SIZE] {signal.symbol} conf={signal.confidence:.0%} -> "
            f"scale=1.00x -> ${risk_info['dollar_amount']:,.0f}"
        )

        _pre_bonus_pct = risk_info["allocation_pct"]
        risk_info = _apply_confidence_size_ramp(risk_info, signal.confidence, acct.equity)
        if risk_info["allocation_pct"] != _pre_bonus_pct:
            log.debug(
                f"[SIZE] {signal.symbol} conf={signal.confidence:.0%} > {CONF_SCALE_FULL_CONF:.0%} "
                f"-- confidence size ramp: allocation {_pre_bonus_pct:.1f}% -> {risk_info['allocation_pct']:.1f}% "
                f"(${risk_info['dollar_amount']:,.0f})"
            )

        _pre_kelly_pct = risk_info["allocation_pct"]
        risk_info = _apply_strategy_kelly_mult(risk_info, signal.strategy, acct.equity)
        if risk_info["allocation_pct"] != _pre_kelly_pct:
            log.debug(
                f"[SIZE] {signal.symbol} [{signal.strategy}] Kelly mult "
                f"{STRATEGY_KELLY_MULT.get(signal.strategy, STRATEGY_KELLY_MULT_DEFAULT):.2f}x: "
                f"allocation {_pre_kelly_pct:.1f}% -> {risk_info['allocation_pct']:.1f}% "
                f"(${risk_info['dollar_amount']:,.0f})"
            )

        risk_info = _apply_thin_liquidity_override(risk_info, signal, acct.equity)

        shares, skip_reason = self._size_with_buying_power(acct.buying_power, signal, risk_info, order_type)
        if shares < 1:
            # Confidence-swap: if a held position has lower entry confidence, rotate into the new signal.
            # Skip entirely when PDT = 0 -- closing a same-day position would itself be a day trade.
            _dt_left_swap = self.pdt.remaining(acct.equity, acct.daytrade_count)
            if order_type == OrderType.LONG and _dt_left_swap > 0:
                victim, victim_conf = self._find_least_confident_position(signal.confidence)
                if victim:
                    log.info(
                        f"CONF-SWAP: closing {victim} (conf={victim_conf:.0%}) "
                        f"to make room for {signal.symbol} (conf={signal.confidence:.0%})"
                    )
                    try:
                        # Same as _attempt_swap: victim's full qty is normally
                        # reserved by its own GTC trailing stop, so close_position()
                        # rejects with "insufficient qty available" unless that
                        # resting order is cancelled first (confirmed failing on
                        # every cycle in production before this -- AMLX, 40310000).
                        try:
                            for o in (self.client.get_orders() or []):
                                if o.symbol == victim:
                                    self.client.cancel_order_by_id(str(o.id))
                                    time.sleep(0.4)
                        except Exception as cancel_err:
                            log.warning(f"CONF-SWAP {victim}: order cancel failed, close may reject: {cancel_err}")
                        self._no_rearm.add(victim)  # portfolio rebalance, not a verdict on this symbol
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
                    f"Short-float cap {signal.symbol}: {shares}->{cap_shares} shares "
                    f"({MAX_SHORT_FLOAT_PCT:.0f}% equity max, equity ${acct.equity:,.0f})"
                )
                shares = cap_shares
            if shares < 1:
                log.info(f"Skip {signal.symbol}: too small after short-float cap")
                return False

        if order_type == OrderType.SHORT and LONG_ONLY_MODE:
            log.info(f"Skipping {signal.symbol} SHORT because LONG_ONLY_MODE is active")
            return False

        # Cancel stale/opposite resting DAY orders before entry (requested
        # hardening): a leftover opposite-side order must not conflict with the
        # fresh entry we're about to place.
        self._cancel_opposite_orders_before_entry(signal.symbol, order_type == OrderType.LONG)

        # Staged allocation (25% x 4), never adding while losing. A FRESH
        # (first-time) entry is split into STAGED_ALLOCATION_TRANCHES equal
        # tranches: only the first tranche is submitted now, and the remaining
        # tranches are added by maybe_add_staged_tranches() -- each add
        # requires (a) the position to be NOT losing (gain strictly above
        # STAGED_ALLOCATION_MIN_GAIN_PCT) and (b) a fresh EMA trend-alignment
        # check immediately before the tranche. Re-entries (2nd+ same day) and
        # symbols already being staged are not re-staged.
        staged_state = getattr(self, "_staged_allocation", {})
        if (
            STAGED_ALLOCATION_ENABLED
            and signal.symbol not in staged_state
            and not self._is_reentry_signal(signal.symbol, order_type == OrderType.LONG)
        ):
            total_tranches = max(1, STAGED_ALLOCATION_TRANCHES)
            tranche_qty    = max(1, shares // total_tranches)
            if STAGED_ALLOCATION_MAX_ADD_PCT > 0:
                max_add = max(1, int(shares * STAGED_ALLOCATION_MAX_ADD_PCT / 100.0))
                tranche_qty = min(tranche_qty, max_add)
            if tranche_qty < shares:
                staged_state[signal.symbol] = {
                    "tranches_done": 1,
                    "tranche_qty": tranche_qty,
                    "is_long": order_type == OrderType.LONG,
                    "entry_price": signal.price,
                    "total_tranches": total_tranches,
                }
                log.info(
                    f"STAGED ENTRY {signal.symbol}: full {shares} sh split into "
                    f"{total_tranches} tranches -- submitting first {tranche_qty} sh now; "
                    f"remaining tranches only while NOT losing"
                )
                shares = tranche_qty

        if self.use_bracket_orders and self._current_market_state().is_regular_hours:
            if self._create_bracket_order(signal, shares, risk_info, order_type):
                self.pdt.add(datetime.date.today())
                self._entry_log[signal.symbol] = {"strategy": signal.strategy, "date": datetime.date.today(), "filled_at": datetime.datetime.now(datetime.timezone.utc), "confidence": signal.confidence, "thin_liquidity": signal.thin_liquidity}
                self._swap_cycle_closed.add(signal.symbol)  # protect from same-cycle swap-out
                self._get_positions(force_refresh=True)
                self._get_account(force_refresh=True)
                return True

        if self._create_simple_order(signal, shares, order_type):
            self.pdt.add(datetime.date.today())
            self._entry_log[signal.symbol] = {"strategy": signal.strategy, "date": datetime.date.today(), "confidence": signal.confidence, "thin_liquidity": signal.thin_liquidity}
            self._swap_cycle_closed.add(signal.symbol)  # protect from same-cycle swap-out
            self._get_positions(force_refresh=True)
            self._get_account(force_refresh=True)
            return True

        return False

    def maybe_add_staged_tranches(self) -> None:
        """Periodic poller (PENDING_ENTRY_RECHECK_SEC cadence) for staged
        allocation (25% x 4), never adding while losing. For each symbol
        currently being staged (first tranche already submitted), add the next
        tranche ONLY while:

          - the entry actually filled and a position is open (if the first
            order never filled or the position was closed, staging is done);
          - the position is NOT losing -- unrealized gain strictly above
            STAGED_ALLOCATION_MIN_GAIN_PCT ("never adding while losing");
          - a FRESH EMA trend-alignment check still passes immediately
            before this tranche (fresh EMA check before each tranche) --
            same gate a fresh entry would need;
          - tranches remain.

        Each add submits a DAY trailing-stop entry for one tranche_qty via
        _submit_entry_order(allow_existing_position=True, scale_in=True): the
        broker-side one-active-entry-per-symbol guards still apply (a resting
        unfilled first tranche blocks the add), but the FIRST-ENTRY-only local
        guards (60s submit debounce, order_cache slot holding the first
        tranche's order id) are bypassed -- they would otherwise block tranche
        2 for the life of the position -- while _entry_pending /
        _pending_entry_signals remain hard blocks.
        """
        staged_state = getattr(self, "_staged_allocation", {})
        if not staged_state:
            return
        # 2026-09-01, two-window schedule: no scale-in tranche orders during
        # the midday break -- the lunch flat closed the positions this would
        # add to at 11:00 ET. Staged state survives the break (any position
        # re-entered at 14:45 is a fresh signal/state anyway).
        import pytz as _pytz
        if in_lunch_break(datetime.datetime.now(_pytz.timezone("America/New_York"))):
            return
        try:
            positions = self._get_positions(force_refresh=True)
        except Exception as e:
            log.warning(f"maybe_add_staged_tranches: position fetch failed: {e}")
            return

        for sym, state in list(staged_state.items()):
            try:
                total  = int(state.get("total_tranches", STAGED_ALLOCATION_TRANCHES))
                done   = int(state.get("tranches_done", 1))
                if done >= total:
                    staged_state.pop(sym, None)
                    continue

                if not positions.has_position(sym):
                    # First tranche never filled, or the position has since been
                    # closed (stop-out/EOD/swap) -- nothing left to scale into.
                    log.info(f"STAGED {sym}: no open position -- dropping staged state")
                    staged_state.pop(sym, None)
                    continue

                is_long = bool(state.get("is_long", True))
                pos = positions.positions_dict[sym]
                current = float(getattr(pos, "current_price", 0) or 0)
                entry   = float(state.get("entry_price", 0) or 0)
                if current <= 0 or entry <= 0:
                    continue
                gain_pct = (current - entry) / entry * 100.0 if is_long else (entry - current) / entry * 100.0
                if gain_pct <= STAGED_ALLOCATION_MIN_GAIN_PCT:
                    # Never adding while losing. Keep staging state so a later
                    # recovery can still add -- this is the whole point of
                    # scaling into a winner.
                    log.info(
                        f"STAGED {sym}: not adding while losing (gain {gain_pct:+.2f}% <= "
                        f"{STAGED_ALLOCATION_MIN_GAIN_PCT:+.2f}%) -- tranche {done + 1}/{total} held"
                    )
                    continue

                # Fresh EMA check immediately before each tranche.
                stub = SimpleNamespace(symbol=sym)
                gate_ok, gate_reason = _check_ema_trend_alignment(stub, is_long, force_fresh=True)
                if not gate_ok:
                    log.info(f"STAGED {sym}: fresh EMA gate failed -- tranche {done + 1}/{total} held ({gate_reason})")
                    continue

                qty = int(state.get("tranche_qty", 0) or 0)
                if qty < 1:
                    staged_state.pop(sym, None)
                    continue

                side = OrderSide.BUY if is_long else OrderSide.SELL
                req = TrailingStopOrderRequest(
                    symbol=sym, qty=qty, side=side,
                    type=AlpacaOrderType.TRAILING_STOP,
                    time_in_force=TimeInForce.DAY,
                    trail_percent=REENTRY_TRAIL_PCT,
                    client_order_id=f"apex-staged-{sym}-{int(time.time())}",
                )
                order = self._submit_entry_order(sym, req, allow_existing_position=True, scale_in=True)
                if order is None:
                    continue
                self.order_cache[sym] = order.id
                state["tranches_done"] = done + 1
                log.warning(
                    f"STAGED ADD {sym}: tranche {done + 1}/{total} ({qty} sh) "
                    f"@ gain {gain_pct:+.2f}% -- trailing {REENTRY_TRAIL_PCT:.2f}% DAY entry"
                )
            except Exception as e:
                log.warning(f"maybe_add_staged_tranches {sym}: {e}")

    # -- Public: Execute ---------------------------------------------------
    def execute(self, signal: Signal, swap_only: bool = False) -> bool:
        if is_never_trade(signal.symbol):
            log.info(f"Skipping {signal.symbol}: listed in data/never_trade.txt")
            return False
        try:
            acct      = self._get_account()
            positions = self._get_positions()

            if signal.action == "buy":
                if positions.has_position(signal.symbol) and positions.is_short(signal.symbol):
                    return self._close_short_position(signal, acct.equity)
                order_type = OrderType.LONG
                if signal.symbol in getattr(self, "_loss_reentry_required", set()):
                    ok, reason = EnhancedExecutor._check_30m_reentry_performance(signal.symbol, True)
                    if not ok:
                        log.info(f"Skip {signal.symbol}: {reason} -- loss re-entry 30m gate")
                        return False
                    if reason:
                        log.info(f"LOSS REENTRY {signal.symbol}: {reason}")
                result = self._execute_entry(signal, acct, order_type, swap_only=swap_only)
                if result:
                    getattr(self, "_loss_reentry_required", set()).discard(signal.symbol)
                return result

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
                order_type = OrderType.SHORT
                if signal.symbol in getattr(self, "_loss_reentry_required", set()):
                    ok, reason = EnhancedExecutor._check_30m_reentry_performance(signal.symbol, False)
                    if not ok:
                        log.info(f"Skip {signal.symbol}: {reason} -- loss re-entry 30m gate")
                        return False
                    if reason:
                        log.info(f"LOSS REENTRY {signal.symbol}: {reason}")
                result = self._execute_entry(signal, acct, order_type, swap_only=swap_only)
                if result:
                    getattr(self, "_loss_reentry_required", set()).discard(signal.symbol)
                return result

        except Exception as e:
            log.error(f"Execute error {signal.symbol}: {e}")
        return False

    # ---- Close Short ----------------------------------------------------------------------------------------
    def _close_short_position(self, signal: Signal, equity: float) -> bool:
        positions = self._get_positions()
        if not positions.has_position(signal.symbol):
            log.info(f"No short position in {signal.symbol}")
            return False
        try:
            qty = abs(int(positions.positions_dict[signal.symbol].qty))
            # 2026-08-25, user request ("you f-ed up again" -- rightly):
            # every other close path in this file (close_eod_positions,
            # check_afterhours_stops, close_no_gain_positions, the
            # weakest-swap path, check_tp_targets, close_stale_swing_positions,
            # close_guardrail_fail_positions) already cancels resting orders
            # -- GTC trailing stop included -- before submitting its close,
            # because that stop reserves the qty and the close order gets
            # rejected "insufficient qty available" otherwise. This
            # strategy-driven cover path never got that same fix. Same
            # pattern here.
            try:
                sym_orders = [o for o in (self.client.get_orders() or []) if o.symbol == signal.symbol]
                for _o in sym_orders:
                    try:
                        self.client.cancel_order_by_id(str(_o.id))
                    except Exception:
                        pass
                if sym_orders:
                    time.sleep(0.4)
            except Exception:
                pass
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

    # ---- Close Long ------------------------------------------------------------------------------------------
    def _close_long_position(self, signal: Signal, equity: float) -> bool:
        positions = self._get_positions()
        if not positions.has_position(signal.symbol):
            log.info(f"No position in {signal.symbol}")
            return False
        # Closes are ALWAYS allowed regardless of PDT -- never block an exit

        qty = abs(int(float(positions.positions_dict[signal.symbol].qty)))
        try:
            # 2026-08-25, user request ("you f-ed up again" -- rightly): same
            # cancel-resting-orders-first fix as _close_short_position right
            # above (and every other close path in this file) -- the GTC
            # trailing stop protect_positions() places reserves the qty, so
            # a close submitted while it's still resting gets rejected
            # "insufficient qty available." This strategy-driven sell path
            # never had that fix either.
            try:
                sym_orders = [o for o in (self.client.get_orders() or []) if o.symbol == signal.symbol]
                for _o in sym_orders:
                    try:
                        self.client.cancel_order_by_id(str(_o.id))
                    except Exception:
                        pass
                if sym_orders:
                    time.sleep(0.4)
            except Exception:
                pass
            # A plain MarketOrderRequest gets rejected outside regular hours -- this
            # path (a strategy-driven "sell" signal on a held long) is reachable
            # any time scan_and_trade runs, which spans the full 07:00-20:00
            # is_market_open window, not just 09:30-16:00. Its sibling
            # _close_short_position already branches on regular-hours a few lines
            # above; this one didn't. _submit_closing_order handles both cases.
            self._submit_closing_order(signal.symbol, qty, OrderSide.SELL, signal.price)
            # NOTE: closing an existing position is NOT a new day trade.
            # Alpaca counts the round-trip (open+close same day) as one trade;
            # pdt.add() is intentionally omitted here -- it was already counted at entry.
            self._get_positions(force_refresh=True)
            log.info(f"SELL {signal.symbol}: {qty} shares | {signal.strategy}")
            return True
        except Exception as e:
            log.error(f"Sell error {signal.symbol}: {e}")
            return False

    # --- Protect Open Positions ----------------------------------------------
    def protect_positions(self) -> None:
        """
        For every open position whose shares are fully free (qty_available > 0
        AND no existing sell/buy-to-cover order on that symbol), place a GTC
        trailing stop.  Skips any position already covered by an active order.

        Covers today's entries too -- if the bracket-order step-2 trailing stop
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

            # Skip options legs -- OCC symbols (e.g. AEHR260515C00080000) can
            # still exist in the account from before options trading was removed
            # (2026-09-01); trailing stops are invalid for them (Alpaca error
            # 42210000). OCC symbols always match <ticker><YYMMDD><C|P><8digits>.
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue

            # Primary guard: don't add orders if symbol already has any active order
            if sym in covered:
                continue

            # Skip positions confirmed as forced overnight holds (PDT blocks close too)
            if sym in self._pdt_overnight_forced:
                continue

            # Secondary guard: skip if broker reports zero available qty. Alpaca
            # mirrors qty_available's sign to qty for shorts (a fully-free -26
            # share short reports qty_available=-26, not +26) -- checking <= 0
            # is only correct for longs. Confirmed live 2026-08-12: every open
            # short (ACHR, CORZ, IREN, MARA, ONON, SE, WULF) has a negative
            # qty_available and was being skipped here every single cycle,
            # leaving the entire short book with zero trailing-stop protection.
            # 0 (not sign) is what actually means "fully reserved by another
            # order" on both sides, so that's the only case to skip.
            try:
                qty_available = int(float(pos.qty_available))
            except (AttributeError, TypeError, ValueError):
                qty_available = 0
            if qty_available == 0:
                continue

            try:
                qty         = int(float(pos.qty))
                avail       = abs(qty_available)
                current     = float(pos.current_price)
                is_long_pos = qty > 0
                try:
                    gain_pct = float(pos.unrealized_plpc) * 100.0
                except (TypeError, ValueError, AttributeError):
                    gain_pct = None

                trail_pct, tier_label = _atr_trail_pct_for(sym, current, self._entry_log, gain_pct)

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
                # A real broker-side GTC now covers this symbol -- drop any
                # software-stop fallback (from _cover_naked_positions or an
                # earlier 40310100 rejection) so check_software_stops() stops
                # watching a position that's already covered for real.
                self._pdt_stop_blocked.pop(sym, None)
            except Exception as e:
                err_str = str(e)
                if "40310100" in err_str:
                    # Broker PDT protection rejects the stop for today's entry.
                    # Fall back to software stop monitoring via check_software_stops().
                    if sym not in self._pdt_stop_blocked:
                        try:
                            entry_price = float(pos.avg_entry_price or pos.current_price)
                            stop_pct    = _atr_trail_pct_for(sym, float(pos.current_price), self._entry_log)[0]
                            stop_price  = round(
                                entry_price * (1 - stop_pct / 100) if qty > 0
                                else entry_price * (1 + stop_pct / 100),
                                2,
                            )
                            self._pdt_stop_blocked[sym] = stop_price
                            log.warning(
                                f"protect_positions {sym}: broker PDT stop rejected -- "
                                f"software SL set at ${stop_price:.2f} ({stop_pct:.1f}% from ${entry_price:.2f})"
                            )
                        except Exception:
                            log.warning(f"protect_positions {sym}: PDT stop rejected (software SL unavailable)")
                    else:
                        log.debug(f"protect_positions {sym}: PDT stop still rejected (software SL active @ ${self._pdt_stop_blocked[sym]:.2f})")
                else:
                    log.error(f"protect_positions {sym}: {e}")

    def ratchet_confident_winners(self) -> None:
        """Tighten the trailing stop on a position once it's up
        CONF_RATCHET_TRIGGER_GAIN_PCT or more, scaled by how confident the
        original entry signal was -- a trade we were more sure about locks in
        its gain sooner instead of riding the full tier-width stop like every
        other trade. Runs once per position for its whole life (tracked via
        _ratchet_done); protect_positions() never revisits a symbol once it
        has a resting order, so this is the only place a stop gets replaced
        after the fact.

        Skips: positions still at/under their entry price, confidence at or
        below SWAP_MIN_CONFIDENCE (includes the 0.0 placeholder that
        _rebuild_entry_log_from_orders uses for positions restored after a
        bot restart -- we don't actually know those were high-confidence, so
        never tighten them), and anything already ratcheted.
        """
        if not CONF_RATCHET_ENABLED:
            return
        try:
            positions = self.client.get_all_positions()
        except Exception as e:
            log.warning(f"ratchet_confident_winners: fetch failed: {e}")
            return

        for pos in positions:
            sym = pos.symbol
            if sym in self._ratchet_done:
                continue
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue  # options legs -- managed separately
            confidence = self._entry_log.get(sym, {}).get("confidence", 0.0)
            if confidence <= SWAP_MIN_CONFIDENCE:
                continue
            try:
                qty      = int(float(pos.qty))
                gain_pct = float(pos.unrealized_plpc) * 100.0
            except (TypeError, ValueError):
                continue
            if qty == 0 or gain_pct < CONF_RATCHET_TRIGGER_GAIN_PCT:
                continue

            try:
                current  = float(pos.current_price)
                base_pct = _atr_trail_pct_for(sym, current, self._entry_log)[0]
                tightened_pct = round(base_pct * ratchet_scale(confidence), 2)
                if tightened_pct >= base_pct:
                    self._ratchet_done.add(sym)  # nothing to tighten to; don't recheck every cycle
                    continue

                for o in (self.client.get_orders() or []):
                    if o.symbol == sym:
                        self.client.cancel_order_by_id(str(o.id))
                        time.sleep(0.4)

                stop_side = OrderSide.SELL if qty > 0 else OrderSide.BUY
                self.client.submit_order(TrailingStopOrderRequest(
                    symbol        = sym,
                    qty           = abs(qty),
                    side          = stop_side,
                    type          = AlpacaOrderType.TRAILING_STOP,
                    time_in_force = TimeInForce.GTC,
                    trail_percent = tightened_pct,
                ))
                self._ratchet_done.add(sym)
                log.info(
                    f"RATCHET {sym}: +{gain_pct:.1f}% unrealized, entry conf={confidence:.0%} -- "
                    f"trailing stop {base_pct:.1f}% -> {tightened_pct:.1f}%"
                )
            except Exception as e:
                log.warning(f"ratchet_confident_winners {sym}: {e}")

    def _submit_closing_order(
        self, symbol: str, qty: int, side: OrderSide, current_price: float,
        slip_pct: float = 0.5, force_extended_hours: bool = False,
        no_extended_hours: bool = False, client_order_id: Optional[str] = None,
    ) -> Optional[object]:
        """Submit a position-closing order as a marketable limit crossing the
        spread by slip_pct off the LIVE bid/ask mid (see _live_quote_mid) --
        never a naked MarketOrderRequest during regular hours either
        anymore: same unbounded-spread risk as the entry side (NBIL,
        MARKETABLE_LIMIT_BUFFER_PCT), just on the way out instead of in.
        extended_hours is set whenever we're actually outside regular hours,
        since Alpaca rejects market orders (and non-extended limits) then --
        force_extended_hours=True overrides that for callers submitted DURING
        regular hours that still need to survive past the close if unfilled.
        no_extended_hours=True is the opposite override -- 2026-08-18, user
        request: EOD/guardrail force-closes (_sweep_force_closes' regular-
        hours branch) must NEVER be extended_hours even if this call happens
        to land right at the regular/extended boundary and MarketState.from_now()
        has already flipped to after-hours by the time this fires; those two
        reasons aren't "price moved against the position" and don't get to
        trade in extended hours at all anymore (see _sweep_force_closes).
        Callers that keep missing the fill (fast-moving book) should widen
        slip_pct on retry rather than resubmitting at the same price forever."""
        mid  = _live_quote_mid(self.client, symbol, current_price)
        slip = (1.0 - slip_pct / 100.0) if side == OrderSide.SELL else (1.0 + slip_pct / 100.0)
        extended = False if no_extended_hours else (force_extended_hours or not MarketState.from_now().is_regular_hours)
        req = LimitOrderRequest(
            symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY,
            limit_price=round(mid * slip, 2),
            extended_hours=extended,
            **({"client_order_id": client_order_id} if client_order_id else {}),
        )
        # Return the accepted order so close paths can track it by broker id
        # (previously None -- callers couldn't reconcile an in-flight close).
        return self.client.submit_order(req)

    def _request_reconciled_close(
        self, symbol: str, reason: str, current_price: float, *,
        slip_pct: float = 0.5, force_extended_hours: bool = False,
        no_extended_hours: bool = False,
    ) -> CloseResult:
        """The ONE entry point for intentional software closes (software SL,
        EMA9 exit, MFE give-back). Replaces the old cancel-all -> sleep(0.4) ->
        close-with-stale-qty sequence that raced the broker's own cancel
        processing: SNOW 9/3 took NINE consecutive 40310000 "insufficient qty
        available ... held_for_orders" rejections while a GTC trailing stop
        reserved the only share, and the position bled -3.85% before any exit
        landed.

        Sequence (CLOSE_RECONCILIATION_ENABLED=True):
          1. refresh the live position -- flat means done, no order at all;
          2. dedupe: an already-pending close is reconciled against broker
             state (still working / gone-and-resubmitted), never duplicated;
          3. cancel ONLY classified protection (GTC trailing stops on the
             closing side) -- entry/staged orders are never touched;
          4. POLL broker state until the cancel is confirmed, bounded by
             CLOSE_CANCEL_CONFIRM_TIMEOUT_SEC -- no blind fixed sleep;
          5. re-read the position (the cancelled stop may have filled on its
             way out) and close exactly the remaining quantity;
          6. on a failed close, re-arm GTC protection (fail-safe centralized
             here so all exit paths share it).

        CLOSE_RECONCILIATION_ENABLED=False restores the legacy cancel+sleep
        behavior through this same method, so callers are identical either way.
        """
        pending = getattr(self, "_pending_closes", None)
        if pending is None:
            pending = self._pending_closes = {}

        # -- 1. live position ------------------------------------------------
        try:
            positions = {p.symbol: p for p in (self.client.get_all_positions() or [])}
        except Exception as e:
            return CloseResult("failed", symbol, None, 0, 0, f"position fetch failed: {e}")
        pos = positions.get(symbol)
        if pos is None:
            pending.pop(symbol, None)
            return CloseResult("flat", symbol, None, 0, 0, "no position")
        try:
            qty = int(float(pos.qty))
        except (TypeError, ValueError):
            return CloseResult("failed", symbol, None, 0, 0, "unparsable position qty")
        if qty == 0:
            pending.pop(symbol, None)
            return CloseResult("flat", symbol, None, 0, 0, "position qty is 0")
        pos_qty = abs(qty)

        # -- 2. already-pending close: reconcile, never duplicate -------------
        st = pending.get(symbol)
        if st and st.get("state") in ("submitting", "pending", "canceling_protection"):
            close_id = st.get("close_order_id")
            still_active = False
            if close_id:
                try:
                    still_active = any(
                        str(getattr(o, "id", "")) == close_id
                        for o in (self.client.get_orders() or [])
                    )
                except Exception:
                    still_active = True  # unknown -- never risk a duplicate
            if still_active:
                return CloseResult("already_pending", symbol, close_id, pos_qty, pos_qty,
                                   f"close {close_id} still working")
            log.warning(f"[CLOSE-RECON] {symbol}: pending close {close_id} no longer active "
                        f"but position ({pos_qty} sh) remains -- resubmitting")
            pending.pop(symbol, None)

        # -- 3. cancel classified protection (not entries, not our closes) ----
        canceled_any = False
        if CLOSE_RECONCILIATION_ENABLED:
            try:
                open_orders = self.client.get_orders() or []
            except Exception as e:
                return CloseResult("failed", symbol, None, pos_qty, pos_qty, f"order fetch failed: {e}")
            protective: list = []
            for o in open_orders:
                if getattr(o, "symbol", None) != symbol:
                    continue
                v = _normalize_order_view(o)
                if classify_symbol_order(v, qty) in ("valid_protection", "partial_protection"):
                    protective.append(v)
            for v in protective:
                try:
                    self.client.cancel_order_by_id(v.order_id)
                    canceled_any = True
                    _telemetry_log("protection_cancel_requested", symbol=symbol,
                                   order_id=v.order_id, reason=reason)
                except Exception as ce:
                    log.warning(f"[CLOSE-RECON] {symbol}: cancel of protection {v.order_id} failed: {ce}")
            if protective:
                # -- 4. bounded cancel-confirmation poll (replaces sleep(0.4)) --
                deadline = time.monotonic() + max(0.0, CLOSE_CANCEL_CONFIRM_TIMEOUT_SEC)
                poll_sec = max(0.05, CLOSE_CANCEL_CONFIRM_POLL_SEC)
                protective_ids = {v.order_id for v in protective}
                confirmed = False
                while time.monotonic() < deadline:
                    time.sleep(poll_sec)
                    try:
                        sym_orders = [o for o in (self.client.get_orders() or [])
                                      if getattr(o, "symbol", None) == symbol]
                    except Exception:
                        return CloseResult("failed", symbol, None, pos_qty, pos_qty,
                                           "order fetch failed during cancel confirmation")
                    if not any(str(getattr(o, "id", "")) in protective_ids for o in sym_orders):
                        confirmed = True
                        break
                if not confirmed:
                    # Protection is still resting -- the position is NOT naked.
                    # Defer the close; the next 5s tick re-runs this method.
                    log.info(f"[CLOSE-RECON] {symbol}: {reason} -- protection cancel not confirmed "
                             f"within {CLOSE_CANCEL_CONFIRM_TIMEOUT_SEC:.2f}s, deferring close "
                             f"(position remains protected by the resting stop)")
                    return CloseResult("cancel_pending", symbol, None, pos_qty, pos_qty,
                                       "protection cancel not confirmed")
                # -- 5. the cancelled stop may have filled on its way out ------
                try:
                    positions = {p.symbol: p for p in (self.client.get_all_positions() or [])}
                except Exception as e:
                    return CloseResult("failed", symbol, None, pos_qty, pos_qty,
                                       f"position re-fetch failed: {e}")
                pos = positions.get(symbol)
                if pos is None:
                    pending.pop(symbol, None)
                    return CloseResult("flat", symbol, None, 0, 0,
                                       "flat after protection cancel/fill")
                qty = int(float(pos.qty))
                if qty == 0:
                    pending.pop(symbol, None)
                    return CloseResult("flat", symbol, None, 0, 0,
                                       "position closed during reconciliation")
                pos_qty = abs(qty)
        else:
            # Legacy behavior: cancel everything on the symbol, sleep, proceed.
            try:
                sym_orders = [o for o in (self.client.get_orders() or [])
                              if getattr(o, "symbol", None) == symbol]
                for _o in sym_orders:
                    try:
                        self.client.cancel_order_by_id(str(_o.id))
                    except Exception:
                        pass
                if sym_orders:
                    time.sleep(0.4)
            except Exception as e:
                log.warning(f"[CLOSE-RECON] {symbol}: legacy cancel failed, deferring: {e}")
                return CloseResult("cancel_pending", symbol, None, pos_qty, pos_qty,
                                   f"legacy cancel failed: {e}")

        return self._submit_reconciled_close_order(
            symbol, reason, qty, pos_qty, current_price, pending, canceled_any,
            slip_pct=slip_pct, force_extended_hours=force_extended_hours,
            no_extended_hours=no_extended_hours,
        )

    def _submit_reconciled_close_order(
        self, symbol: str, reason: str, qty: int, pos_qty: int,
        current_price: float, pending: Dict[str, dict], canceled_any: bool, *,
        slip_pct: float = 0.5, force_extended_hours: bool = False,
        no_extended_hours: bool = False,
    ) -> CloseResult:
        """Submit tail of _request_reconciled_close(): exactly one bounded-limit
        close for the remaining quantity, pending-state bookkeeping keyed on a
        stable apex-close-* client order id, and the centralized
        re-arm-GTC-on-failure fail-safe (previously copy-pasted across three
        exit paths with slightly diverging behavior)."""
        side = OrderSide.SELL if qty > 0 else OrderSide.BUY
        coid = _pending_close_client_id(symbol, reason)
        pending[symbol] = {"reason": reason, "state": "submitting", "close_order_id": None,
                           "client_order_id": coid, "requested_qty": pos_qty,
                           "triggered_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        try:
            order = self._submit_closing_order(
                symbol, pos_qty, side, float(current_price), slip_pct=slip_pct,
                force_extended_hours=force_extended_hours,
                no_extended_hours=no_extended_hours, client_order_id=coid,
            )
        except Exception as e:
            err = str(e)
            pending.pop(symbol, None)
            _telemetry_log("close_rejected", symbol=symbol, reason=reason,
                           qty=pos_qty, error=err[:300], canceled_protection=canceled_any)
            if "40310100" in err:
                # Broker PDT blocks the same-day close itself -- caller decides
                # whether that means forced-overnight (software SL) or retry.
                return CloseResult("blocked_pdt", symbol, None, pos_qty, pos_qty, err[:200])
            # -- 6. close failed: re-arm GTC protection (fail-safe) -----------
            rearmed = False
            try:
                trail_pct, _ = _atr_trail_pct_for(symbol, float(current_price), getattr(self, "_entry_log", {}))
                self.client.submit_order(TrailingStopOrderRequest(
                    symbol=symbol, qty=pos_qty, side=side,
                    type=AlpacaOrderType.TRAILING_STOP, time_in_force=TimeInForce.GTC,
                    trail_percent=trail_pct,
                ))
                rearmed = True
                log.warning(f"[CLOSE-RECON] {symbol}: {reason} close failed ({err[:120]}) -- "
                            f"re-armed GTC trailing stop {trail_pct:.2f}%")
            except Exception as rearm_err:
                log.error(f"[CLOSE-RECON] {symbol}: {reason} close failed AND GTC re-arm failed "
                          f"-- position may be UNPROTECTED: {rearm_err}")
                _telemetry_log("critical_unprotected", symbol=symbol, reason=reason,
                               qty=pos_qty, error=str(rearm_err)[:300])
            return CloseResult("failed_reprotected" if rearmed else "critical_unprotected",
                               symbol, None, pos_qty, pos_qty, err[:200])

        order_id = str(getattr(order, "id", "") or "") or None
        pending[symbol] = {"reason": reason, "state": "pending", "close_order_id": order_id,
                           "client_order_id": coid, "requested_qty": pos_qty,
                           "triggered_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        _telemetry_log("close_submitted", symbol=symbol, reason=reason, qty=pos_qty,
                       order_id=order_id, canceled_protection=canceled_any)
        log.warning(f"[CLOSE-RECON] {symbol}: {reason} close submitted ({pos_qty} sh, "
                    f"order {order_id}) after cancelling {'protection' if canceled_any else 'nothing'}")
        return CloseResult("submitted", symbol, order_id, pos_qty, pos_qty, "submitted")

    def _cover_naked_positions(self) -> None:
        """Fast-thread companion to protect_positions(): the moment a
        position exists with no resting order and no software-stop coverage
        yet, attempt the REAL broker-side GTC trailing stop right here on
        the 5s cycle instead of waiting for protect_positions()'s slower
        adaptive-scan cadence. Falls back to software-stop bookkeeping only
        if the broker actually rejects the order (e.g. real 40310100).

        2026-08-17, CDTG: the bracket-order trailing stop used to be
        deliberately deferred for every live same-day entry on an assumed
        PDT block that was never real (removed 2026-08-28 -- see enhanced.py
        entry-order step 2), and protect_positions() only ran on the
        adaptive scan cadence -- CDTG sat with literally zero stop coverage
        for 3+ minutes while it fell from $2.97 to $2.73, and by the time a
        stop was finally armed it anchored to the already-fallen price
        instead of the entry. Now that the broker attempt itself happens
        here every 5s, that gap closes to one tick instead of one scan
        cycle. Called from the same thread as check_software_stops(), which
        watches/closes positions that land in the fallback path below."""
        try:
            positions   = self.client.get_all_positions()
            open_orders = self.client.get_orders()
        except Exception as e:
            log.warning(f"_cover_naked_positions: fetch failed: {e}")
            return
        for pos in positions:
            sym = pos.symbol
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue  # options legs -- managed separately
            if sym in self._pdt_stop_blocked:
                continue
            try:
                qty = int(float(pos.qty))
                if qty == 0:
                    continue
                is_long_pos = qty > 0
                stop_pct    = _atr_trail_pct_for(sym, float(pos.current_price or 0), self._entry_log)[0]
                position_side = OrderSide.SELL if is_long_pos else OrderSide.BUY
                symbol_orders = [o for o in open_orders if o.symbol == sym]
                trailing_stops = []
                has_day_protection = False
                for order in symbol_orders:
                    raw_type = getattr(order, "order_type", "")
                    order_type = str(getattr(raw_type, "value", raw_type)).lower()
                    order_class = str(getattr(getattr(order, "order_class", ""), "value", getattr(order, "order_class", ""))).lower()
                    raw_side = getattr(order, "side", "")
                    order_side = str(getattr(raw_side, "value", raw_side)).lower()
                    tif = getattr(order, "time_in_force", None)
                    expected_side = "sell" if is_long_pos else "buy"
                    is_day_exit = (
                        tif == TimeInForce.DAY
                        and order_side == expected_side
                        and (
                            order_class in ("bracket", "oco")
                            or any(kind in order_type for kind in ("limit", "stop"))
                        )
                    )
                    if is_day_exit:
                        submitted_at = getattr(order, "submitted_at", None) or getattr(order, "created_at", None)
                        if submitted_at is not None:
                            if submitted_at.tzinfo is None:
                                submitted_at = submitted_at.replace(tzinfo=datetime.timezone.utc)
                            age_seconds = (datetime.datetime.now(datetime.timezone.utc) - submitted_at).total_seconds()
                        else:
                            age_seconds = 0.0
                        if age_seconds >= PROTECTION_LIMIT_REPLACE_AFTER_SEC:
                            try:
                                self.client.cancel_order_by_id(str(order.id))
                                time.sleep(0.4)
                                log.warning(
                                    f"PROTECT {sym}: converted DAY bracket exit {order.id} "
                                    f"to fixed {stop_pct:.1f}% GTC trailing stop after {age_seconds:.0f}s"
                                )
                            except Exception as cancel_err:
                                has_day_protection = True
                                log.warning(f"PROTECT {sym}: bracket-exit conversion failed for {order.id}: {cancel_err}")
                        else:
                            has_day_protection = True
                        continue
                    if tif != TimeInForce.GTC:
                        continue
                    if order_side != expected_side:
                        continue
                    if "trailing_stop" in order_type:
                        current_trail = getattr(order, "trail_percent", None)
                        try:
                            current_trail = float(current_trail) if current_trail is not None else None
                        except (TypeError, ValueError):
                            current_trail = None
                        if current_trail is not None and abs(current_trail - stop_pct) < 1e-9:
                            trailing_stops.append(order)
                        else:
                            try:
                                self.client.cancel_order_by_id(str(order.id))
                                time.sleep(0.2)
                                log.warning(
                                    f"PROTECT {sym}: replaced trailing exit {order.id} "
                                    f"({current_trail}%) with fixed {stop_pct:.1f}%"
                                )
                            except Exception as cancel_err:
                                log.warning(f"PROTECT {sym}: wrong-trail cancel failed for {order.id}: {cancel_err}")
                    else:
                        try:
                            self.client.cancel_order_by_id(str(order.id))
                            time.sleep(0.2)
                            log.warning(f"PROTECT {sym}: cancelled stale {order_type} exit order {order.id}")
                        except Exception as cancel_err:
                            log.warning(f"PROTECT {sym}: could not cancel stale exit order {order.id}: {cancel_err}")

                if trailing_stops:
                    # Keep one fixed 1.5% protection order and remove duplicates.
                    for duplicate in trailing_stops[1:]:
                        try:
                            self.client.cancel_order_by_id(str(duplicate.id))
                        except Exception as cancel_err:
                            log.warning(f"PROTECT {sym}: duplicate trailing-stop cancel failed: {cancel_err}")
                    continue
                if has_day_protection:
                    continue

                try:
                    self.client.submit_order(TrailingStopOrderRequest(
                        symbol        = sym,
                        qty           = abs(qty),
                        side          = position_side,
                        type          = AlpacaOrderType.TRAILING_STOP,
                        time_in_force = TimeInForce.GTC,
                        trail_percent = stop_pct,
                    ))
                    log.info(f"PROTECT {sym} [fast-thread]: trailing stop {stop_pct:.1f}% GTC")
                    continue
                except Exception as broker_e:
                    log.warning(f"_cover_naked_positions {sym}: broker stop rejected ({broker_e}) -- falling back to software SL")

                entry_price = float(pos.avg_entry_price or pos.current_price)
                stop_price  = round(
                    entry_price * (1 - stop_pct / 100) if is_long_pos
                    else entry_price * (1 + stop_pct / 100),
                    2,
                )
                self._pdt_stop_blocked[sym] = stop_price
                log.warning(
                    f"_cover_naked_positions {sym}: no order coverage yet -- "
                    f"software SL set at ${stop_price:.2f} ({stop_pct:.1f}% from ${entry_price:.2f})"
                )
            except Exception:
                continue  # best-effort -- next 5s tick tries again

    def check_software_stops(self) -> None:
        """Close any position whose broker-rejected PDT stop has been breached.
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
                # Position already closed (stop filled or manual) -- clear BOTH
                # the software-stop watch AND any pending close state, so a
                # dangling close record can never leak onto a future position.
                self._pdt_stop_blocked.pop(sym, None)
                getattr(self, "_pending_closes", {}).pop(sym, None)
                continue
            try:
                current = float(pos.current_price)
                qty     = int(float(pos.qty))
                is_long = qty > 0
                hit     = (is_long and current <= stop_price) or (not is_long and current >= stop_price)
                if hit:
                    result = self._request_reconciled_close(sym, "software-sl", current)
                    if result.state == "submitted":
                        log.warning(
                            f"SOFTWARE SL HIT {sym}: price ${current:.2f} crossed stop ${stop_price:.2f} -- "
                            f"close submitted ({result.requested_qty} sh)"
                        )
                        self._maybe_rearm_reentry(
                            sym, is_long, result.requested_qty, "SOFTWARE SL",
                            was_loss=(current - float(pos.avg_entry_price)) * qty < 0,
                        )
                        # Deliberately KEEP the _pdt_stop_blocked entry: the next
                        # 5s tick re-runs this path, sees the close already
                        # pending (or flat) and clears it then. The old
                        # optimistic pop is what let an unfilled close sit
                        # unwatched.
                    elif result.state == "flat":
                        self._pdt_stop_blocked.pop(sym, None)
                    elif result.state == "blocked_pdt":
                        # Broker PDT blocks the same-day close -- forced
                        # overnight hold, stop retrying.
                        self._pdt_stop_blocked.pop(sym, None)
                        self._pdt_overnight_forced.add(sym)
                        log.warning(
                            f"SOFTWARE SL {sym}: stop breached at ${current:.2f} but PDT blocks "
                            f"same-day close -- holding overnight (stop was ${stop_price:.2f})"
                        )
                    elif result.state in ("cancel_pending", "already_pending"):
                        log.debug(f"SOFTWARE SL {sym}: close {result.state} -- re-checking next tick")
                    else:
                        log.error(f"check_software_stops {sym}: {result.state}: {result.detail}")
                else:
                    log.debug(f"SOFTWARE SL {sym}: current ${current:.2f} | stop ${stop_price:.2f} | margin ${current - stop_price:+.2f}")
            except Exception as e:
                log.error(f"check_software_stops {sym}: {e}")

    def detect_stopped_out_positions(self) -> None:
        """Catch a position closing via ANY route -- most commonly a normal
        broker-side GTC trailing stop filling on its own -- and reset its
        ratchet-tightening state so a later re-entry gets fresh confidence-
        ratchet protection instead of none (see the _ratchet_done.discard
        call below).

        2026-08-24, user request: this used to also arm a post-loss re-entry
        cooldown here (and in check_afterhours_stops()'s own close path) --
        removed. No cooldown left anywhere; the exit stack (trailing stop,
        per-minute check_ema9_exit, standalone stop-loss) is the only
        protection now.

        2026-08-26, user request ("I have put in 1% on the hope the new
        orders will be placed immediately after the exit with conditions
        check every minute, but it doesn't seem to work"): this is where
        that gap actually lived. check_ema9_exit, check_software_stops,
        check_afterhours_stops, check_price_drift_stop all now call
        _maybe_rearm_reentry() directly, synchronously, right after their
        own close -- but a genuine broker-side GTC trailing stop filling
        entirely on its own never goes through any of those; this poll is
        the only place that ever notices it happened at all. Confirmed
        2026-08-26: this was the dominant exit route (~51 of 73 trades), and
        it had zero re-entry logic.

        Same day, follow-up request ("irrespective of exit type reentry
        should happen for the top 30 list... catch the missed gains after
        the dips"): widened further -- now checks self._no_rearm, which
        only FOUR closing paths still mark (see _maybe_rearm_reentry's
        docstring for the full reasoning): close_guardrail_fail_positions
        (structurally unsafe), the portfolio-rebalance paths
        (enforce_position_concentration/enforce_correlation_concentration/
        enforce_portfolio_leverage/_attempt_swap/_execute_entry's weakest-
        position swap), emergency_close_all (kill-switch), and
        close_eod_positions (explicit follow-up request: "don't reenter
        after end of day exit"). Every other close in the file -- stale-
        swing, no-gain, swing-drift, take-profit, a contradicting strategy
        signal -- is unmarked on purpose, so it falls through here and gets
        the same re-entry check as a genuine stop.
        """
        try:
            positions = self.client.get_all_positions()
        except Exception as e:
            log.warning(f"detect_stopped_out_positions: fetch failed: {e}")
            return

        current: Dict[str, dict] = {}
        for p in positions:
            sym = p.symbol
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue  # options legs -- managed separately
            try:
                current[sym] = {
                    "entry_price": float(p.avg_entry_price),
                    "last_price": float(p.current_price),
                    "is_long": float(p.qty) > 0,
                    "qty": abs(int(float(p.qty))),
                }
            except (TypeError, ValueError):
                continue

        for sym, last in self._last_known_positions.items():
            if sym in current:
                continue  # still open
            # Closed via any route -- eligible for confidence-ratchet protection
            # again next time it's re-entered (was symbol-keyed and add-only,
            # so a re-entry after an earlier ratcheted win got none -- 2026-08-10:
            # ABCL ran +6.3% unrealized on a fresh entry after an earlier lot had
            # already ratcheted, and never got tightened).
            self._ratchet_done.discard(sym)
            # A stale peak from the lot that just closed must never carry
            # over and get trailed against by a later, differently-priced
            # re-entry.
            if hasattr(self, "_ema9_trail_peak"):
                self._ema9_trail_peak.pop(sym, None)
            for attr in ("_entry_ema15_delta", "_entry_ema15", "_reclaimed_ema15"):
                state = getattr(self, attr, None)
                if state is not None:
                    state.discard(sym) if hasattr(state, "discard") else state.pop(sym, None)

            no_rearm = getattr(self, "_no_rearm", set())
            if sym in no_rearm:
                no_rearm.discard(sym)
                continue  # closed on purpose -- respect it, don't re-arm
            qty = last.get("qty")
            if not qty:
                continue  # no qty on record (pre-upgrade snapshot) -- skip rather than guess
            close_lookup = getattr(self, "_get_recent_close_price", lambda *_args, **_kwargs: None)
            close_price = close_lookup(sym, is_long=last["is_long"]) or last["last_price"]
            signed_pnl = (close_price - last["entry_price"]) * qty * (1 if last["is_long"] else -1)
            self._maybe_rearm_reentry(
                sym, last["is_long"], qty, "STOPPED OUT", was_loss=signed_pnl < 0,
            )

        self._last_known_positions = current

    def check_afterhours_stops(self) -> None:
        """Actively watch every open position's loss while the market is NOT in
        regular hours -- the broker-side GTC trailing stop from protect_positions()
        sits inert outside 09:30-16:00 ET, so a position can free-fall pre-market
        or after-hours with no protection until regular hours resume. Uses a flat
        stop from avg_entry_price at the same trail % as the resting trailing
        stop (not a true trailing high-water-mark -- good enough for a software
        backstop). Skips symbols already handled by check_software_stops to
        avoid double-submitting a close. Meant to be polled frequently (the
        10s software-stop thread) since after-hours moves can be sharp.

        The resting GTC trailing stop reserves the position's qty, so Alpaca
        won't accept a replacement close order while it's still open -- it's
        cancelled up front, deterministically, rather than waiting to see if
        the close gets rejected. If the close then fails for any reason, a
        fresh GTC trailing stop is immediately re-armed as a fallback so the
        position is never left with zero protection. If a submitted close
        sits unfilled past AFTERHOURS_CHASE_STALE_SECONDS, it's cancelled and
        re-submitted at a fresh marketable price to make sure it actually
        executes."""
        if not AFTERHOURS_STOP_CHECK_ENABLED:
            return
        if MarketState.from_now().is_regular_hours:
            return

        now_utc = datetime.datetime.now(datetime.timezone.utc)

        try:
            positions   = self.client.get_all_positions()
            open_orders = self.client.get_orders()
        except Exception as e:
            log.warning(f"check_afterhours_stops: fetch failed: {e}")
            return

        # Position closed since the last poll -- its re-chase count is stale, drop it
        # so a future breach of the same symbol starts back at the base slip.
        _live_syms = {p.symbol for p in positions}
        for _sym in [s for s in self._afterhours_chase_count if s not in _live_syms]:
            self._afterhours_chase_count.pop(_sym, None)

        pending_by_sym: Dict[str, object] = {}  # symbol -> resting non-GTC order (a close already in flight)
        gtc_orders: Dict[str, str] = {}          # symbol -> GTC trailing-stop order id
        for o in open_orders:
            if getattr(o, "time_in_force", None) == TimeInForce.GTC:
                gtc_orders[o.symbol] = o.id
            else:
                pending_by_sym[o.symbol] = o

        for pos in positions:
            sym = pos.symbol
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue  # options legs -- managed separately
            if sym in self._pdt_stop_blocked:
                continue
            try:
                qty = int(float(pos.qty))
                if qty == 0:
                    continue
                is_long = qty > 0
                current = float(pos.current_price)
                entry   = float(pos.avg_entry_price)
                trail_pct  = _atr_trail_pct_for(sym, current, self._entry_log)[0]
                stop_price = entry * (1 - trail_pct / 100) if is_long else entry * (1 + trail_pct / 100)
                hit = (is_long and current <= stop_price) or (not is_long and current >= stop_price)
                if not hit:
                    continue
                side = OrderSide.SELL if is_long else OrderSide.BUY

                existing = pending_by_sym.get(sym)
                if existing is not None:
                    submitted_at = getattr(existing, "submitted_at", None) or getattr(existing, "created_at", None)
                    age_s = (now_utc - submitted_at).total_seconds() if submitted_at else 0.0
                    if age_s < AFTERHOURS_CHASE_STALE_SECONDS:
                        continue  # close already in flight -- give it time to fill
                    try:
                        self.client.cancel_order_by_id(str(existing.id))
                        time.sleep(0.4)
                    except Exception as e:
                        log.warning(f"check_afterhours_stops {sym}: stale-close cancel failed, will retry next poll: {e}")
                        continue
                    log.warning(f"AFTER-HOURS SL {sym}: prior close unfilled after {age_s:.0f}s -- re-chasing at fresh price")
                else:
                    # First attempt for this breach: the resting GTC trailing
                    # stop reserves the qty, so it must go before Alpaca will
                    # accept a replacement close order after-hours.
                    gtc_id = gtc_orders.get(sym)
                    if gtc_id:
                        try:
                            self.client.cancel_order_by_id(str(gtc_id))
                            time.sleep(0.4)
                        except Exception as cancel_err:
                            log.warning(f"check_afterhours_stops {sym}: GTC cancel failed, will retry next poll: {cancel_err}")
                            continue

                try:
                    chase_n  = self._afterhours_chase_count.get(sym, 0)
                    slip_pct = min(0.5 * (chase_n + 1), 3.0)  # widen 0.5% -> 1.0% -> ... capped at 3% so a fast-falling book still fills
                    self._submit_closing_order(sym, abs(qty), side, current, slip_pct=slip_pct)
                    self._afterhours_chase_count[sym] = chase_n + 1
                    _strategy = self._entry_log.get(sym, {}).get("strategy", "unknown")
                    _pnl = (current - entry) * qty
                    log.warning(
                        f"AFTER-HOURS SL HIT {sym} [{_strategy}]: price ${current:.2f} crossed stop ${stop_price:.2f} "
                        f"({trail_pct:.1f}% from entry ${entry:.2f}) | P&L ${_pnl:+,.2f} -- extended-hours "
                        f"{'SELL' if is_long else 'BUY-TO-COVER'} submitted @ {slip_pct:.1f}% slip "
                        f"(attempt {chase_n + 1})"
                    )
                    if chase_n == 0:
                        # Only on the first attempt for this breach -- a re-chase
                        # (chase_n > 0) is retrying the SAME close, not a new one.
                        self._maybe_rearm_reentry(
                            sym, is_long, abs(qty), "AFTER-HOURS SL",
                            was_loss=_pnl < 0,
                        )
                except Exception as close_err:
                    log.error(f"AFTER-HOURS SL {sym}: close order failed after GTC cancel: {close_err}")
                    # GTC is gone and the replacement didn't go through -- without
                    # a fallback the position would sit fully unprotected until
                    # the next protect_positions() cycle. Re-arm one now.
                    try:
                        self.client.submit_order(TrailingStopOrderRequest(
                            symbol        = sym,
                            qty           = abs(qty),
                            side          = OrderSide.SELL if is_long else OrderSide.BUY,
                            type          = AlpacaOrderType.TRAILING_STOP,
                            time_in_force = TimeInForce.GTC,
                            trail_percent = trail_pct,
                        ))
                        log.warning(f"AFTER-HOURS SL {sym}: re-armed GTC trailing stop as fallback after failed close")
                    except Exception as rearm_err:
                        log.error(f"AFTER-HOURS SL {sym}: close failed AND GTC re-arm failed -- position may be UNPROTECTED: {rearm_err}")
            except Exception as e:
                log.error(f"check_afterhours_stops {sym}: {e}")

    # -- Position Concentration Cap -------------------------------------------
    @staticmethod
    def _effective_concentration_cap_pct(gain_pct: float) -> float:
        """2026-08-17, user request: "maximum holding as 20% of the
        portfolio value and growing based on the continued positive
        returns". A losing/flat position (gain_pct <= 0) keeps the plain
        MAX_POSITION_CONCENTRATION_PCT (20%) cap. A winning position's cap
        grows with its gain -- POSITION_CAP_GROWTH_FACTOR points of extra
        room per point of unrealized gain -- up to
        POSITION_CAP_ABSOLUTE_MAX_PCT (35%), never below the 20% base."""
        bonus = max(0.0, gain_pct) * POSITION_CAP_GROWTH_FACTOR
        return min(MAX_POSITION_CONCENTRATION_PCT + bonus, POSITION_CAP_ABSOLUTE_MAX_PCT)

    def enforce_position_concentration(self) -> None:
        """Trim any position whose market value exceeds its effective
        concentration cap (see _effective_concentration_cap_pct -- the base
        MAX_POSITION_CONCENTRATION_PCT for a losing/flat position, growing
        room for a winner). Entry sizing caps new buys at the plain base
        cap (see _size_with_buying_power -- a brand-new position has no
        gain yet to grow from), but an existing winner can drift past its
        (possibly wider) cap through further price appreciation -- this is
        the backstop for that case."""
        try:
            acct = self._get_account()
            positions = self.client.get_all_positions()
        except Exception as e:
            log.warning(f"enforce_position_concentration: fetch failed: {e}")
            return
        if acct.equity <= 0:
            return
        for pos in positions:
            sym = pos.symbol
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue  # options legs -- sized/managed separately
            qty = int(float(pos.qty))
            if qty == 0:
                continue
            try:
                gain_pct = float(pos.unrealized_plpc) * 100.0
            except (TypeError, ValueError, AttributeError):
                gain_pct = 0.0
            cap_pct   = self._effective_concentration_cap_pct(gain_pct)
            cap_value = acct.equity * cap_pct / 100.0
            market_value = abs(float(pos.market_value))
            if market_value <= cap_value:
                continue
            current = float(pos.current_price)
            trim_qty = int((market_value - cap_value) / current)
            if trim_qty < 1:
                continue
            side = OrderSide.SELL if qty > 0 else OrderSide.BUY  # BUY-to-cover trims a short
            try:
                self._no_rearm.add(sym)  # portfolio-level risk control, not a verdict on this symbol
                self._free_shares_for_trim(sym, trim_qty)
                self._submit_closing_order(sym, trim_qty, side, current)
                log.warning(
                    f"CONCENTRATION TRIM {sym}: {trim_qty} shares -- ${market_value:,.0f} was "
                    f"{market_value / acct.equity:.0%} of equity, cap {cap_pct:.0f}% "
                    f"(gain {gain_pct:+.1f}%)"
                )
            except Exception as e:
                log.error(f"enforce_position_concentration {sym}: trim failed: {e}")

    def _free_shares_for_trim(self, symbol: str, trim_qty: int) -> None:
        """Shrink symbol's resting GTC protective stop by trim_qty shares
        BEFORE a trim/close order for that qty gets submitted.

        Alpaca reserves a position's ENTIRE quantity against any open order
        on it, so a second, competing order for even part of the position
        always fails with "insufficient qty available" while a full-qty
        stop rests -- confirmed live, 2026-08-17: TTD's concentration trim
        failed exactly this way every ~10 min for 6+ hours straight (0/36
        attempts succeeded, see enforce_position_concentration's ERROR log),
        because its 8% GTC trailing stop held all 64 shares.

        Resizing the stop down first, instead of cancelling and re-arming
        it, means the position is never left without stop coverage for even
        an instant -- there's no gap where a fast move has nothing resting
        to catch it. Deliberately conservative: no-op (the caller's trim is
        then left to fail on its own, same as before this fix existed) if
        no matching resting order is found, or if shrinking it would leave
        less than 1 share of coverage. Never touches price/trail, never
        cancels -- qty only."""
        try:
            orders = self.client.get_orders() or []
        except Exception as e:
            log.warning(f"_free_shares_for_trim {symbol}: order fetch failed: {e}")
            return
        stop_order = next(
            (o for o in orders if o.symbol == symbol and getattr(o, "time_in_force", None) == TimeInForce.GTC),
            None,
        )
        if stop_order is None:
            return  # nothing resting to shrink -- let the trim attempt itself surface any issue
        stop_qty = int(float(stop_order.qty))
        new_qty = stop_qty - trim_qty
        if new_qty < 1:
            return  # would zero out (or invert) the stop's coverage -- leave it alone
        self.client.replace_order_by_id(str(stop_order.id), ReplaceOrderRequest(qty=new_qty))
        time.sleep(0.4)  # let the reduced hold register before the trim order competes for the freed shares

    def enforce_correlation_concentration(self) -> None:
        """Trim a correlated basket (e.g. leveraged inverse-market ETFs) whose
        COMBINED market value exceeds that group's cap. enforce_position_concentration
        can't catch this: several different tickers that move together can each
        stay under MAX_POSITION_CONCENTRATION_PCT individually while adding up to
        one oversized directional bet combined (confirmed in production:
        SQQQ+SOXS+TZA+LABD held simultaneously on 2026-07-30)."""
        if not CORRELATION_GROUPS:
            return
        try:
            acct = self._get_account()
            positions = self.client.get_all_positions()
        except Exception as e:
            log.warning(f"enforce_correlation_concentration: fetch failed: {e}")
            return
        if acct.equity <= 0:
            return

        for group_name, group in CORRELATION_GROUPS.items():
            members = group["symbols"]
            group_positions = [
                p for p in positions
                if p.symbol in members and int(float(p.qty)) != 0
            ]
            if not group_positions:
                continue

            total_value = sum(abs(float(p.market_value)) for p in group_positions)
            cap_value = acct.equity * group["max_pct"] / 100.0
            if total_value <= cap_value:
                continue

            excess = total_value - cap_value
            # Trim largest positions first -- fewer orders, and it's the biggest
            # single contributor to the breach.
            for pos in sorted(group_positions, key=lambda p: abs(float(p.market_value)), reverse=True):
                if excess <= 0:
                    break
                sym = pos.symbol
                qty = int(float(pos.qty))
                current = float(pos.current_price)
                pos_value = abs(float(pos.market_value))
                trim_qty = int(min(excess, pos_value) / current)
                if trim_qty < 1:
                    continue
                side = OrderSide.SELL if qty > 0 else OrderSide.BUY  # BUY-to-cover trims a short
                try:
                    self._no_rearm.add(sym)  # portfolio-level risk control, not a verdict on this symbol
                    self._submit_closing_order(sym, trim_qty, side, current)
                    log.warning(
                        f"CORRELATION TRIM [{group_name}] {sym}: {trim_qty} shares -- group was "
                        f"${total_value:,.0f} ({total_value / acct.equity:.0%} of equity), cap {group['max_pct']:.0f}%"
                    )
                    excess -= trim_qty * current
                except Exception as e:
                    log.error(f"enforce_correlation_concentration {sym}: trim failed: {e}")

    def enforce_portfolio_leverage(self) -> None:
        """Trim the largest position(s) if TOTAL market value across every
        open position exceeds MAX_PORTFOLIO_LEVERAGE x equity. 2026-08-17,
        user request: cap total exposure independent of whatever margin the
        broker's buying_power would otherwise allow -- _size_with_buying_power
        already blocks a NEW entry from pushing total exposure past this cap;
        this is the backstop for the book drifting over it through price
        appreciation alone on positions already held (same relationship
        enforce_position_concentration has to per-symbol sizing)."""
        try:
            acct = self._get_account()
            positions = self.client.get_all_positions()
        except Exception as e:
            log.warning(f"enforce_portfolio_leverage: fetch failed: {e}")
            return
        if acct.equity <= 0:
            return

        equity_positions = [
            p for p in positions
            if int(float(p.qty)) != 0 and not re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', p.symbol)
        ]
        if not equity_positions:
            return

        total_value = sum(abs(float(p.market_value)) for p in equity_positions)
        cap_value = acct.equity * MAX_PORTFOLIO_LEVERAGE
        # 2026-09-04: utilization telemetry -- required to judge whether the
        # 2.0x ceiling actually improves portfolio returns (P&L per dollar of
        # average exposure) or just magnifies churn. One event per 10-min grid
        # tick, machine-local JSONL, never blocks trading.
        try:
            _telemetry_log(
                "leverage_snapshot",
                equity=round(float(acct.equity), 2),
                gross_exposure=round(total_value, 2),
                gross_leverage=round(total_value / float(acct.equity), 3),
                cap=MAX_PORTFOLIO_LEVERAGE,
                positions=len(equity_positions),
                over_cap=total_value > cap_value,
            )
        except Exception:
            pass
        if total_value <= cap_value:
            return

        excess = total_value - cap_value
        def _exit_priority(pos):
            """Prefer losing positions, then older flat positions."""
            gain_pct = float(getattr(pos, "unrealized_plpc", 0.0))
            entry_date = self._entry_log.get(pos.symbol, {}).get("date")
            age_days = (datetime.date.today() - entry_date).days if entry_date else 0
            return (gain_pct >= 0, gain_pct if gain_pct < 0 else 0.0, -age_days, -abs(float(pos.market_value)))

        # Reduce exposure in order: losers first, then older non-gainers,
        # then profitable positions only if the breach remains.
        for pos in sorted(equity_positions, key=_exit_priority):
            if excess <= 0:
                break
            sym = pos.symbol
            qty = int(float(pos.qty))
            current = float(pos.current_price)
            pos_value = abs(float(pos.market_value))
            trim_qty = int(min(excess, pos_value) / current)
            if trim_qty < 1:
                continue
            side = OrderSide.SELL if qty > 0 else OrderSide.BUY  # BUY-to-cover trims a short
            try:
                self._no_rearm.add(sym)  # portfolio-level risk control, not a verdict on this symbol
                self._submit_closing_order(sym, trim_qty, side, current)
                log.warning(
                    f"PORTFOLIO LEVERAGE TRIM {sym}: {trim_qty} shares -- book was "
                    f"${total_value:,.0f} ({total_value / acct.equity:.1f}x equity), cap {MAX_PORTFOLIO_LEVERAGE:.1f}x"
                )
                excess -= trim_qty * current
            except Exception as e:
                log.error(f"enforce_portfolio_leverage {sym}: trim failed: {e}")

        # 2026-08-28, user request ("everytime the actual positions reach the
        # 1.5X margin then exit the outstanding entry orders to minimize the
        # actual margin utilization"): trimming filled positions above only
        # addresses exposure that already landed -- any still-resting NEW-
        # entry order (tracked in _pending_entry_signals) would add MORE
        # exposure the instant it fills, undoing the trim just made. Cancel
        # those too, every time this breach fires. Only touches symbols with
        # NO existing position -- a symbol already held has its resting
        # order checked against held_symbols and skipped, since that's a
        # protective stop (see protect_positions/_cover_naked_positions),
        # not an entry; cancelling that here would strip protection exactly
        # when leverage is tightest.
        held_symbols = {p.symbol for p in equity_positions}
        for sym in list(self._pending_entry_signals.keys()):
            if sym in held_symbols:
                continue
            try:
                sym_orders = [o for o in (self.client.get_orders() or []) if o.symbol == sym]
                for _o in sym_orders:
                    try:
                        self.client.cancel_order_by_id(str(_o.id))
                    except Exception:
                        pass
                if sym_orders:
                    log.warning(
                        f"PORTFOLIO LEVERAGE: cancelled outstanding entry order(s) for {sym} -- "
                        f"book over {MAX_PORTFOLIO_LEVERAGE:.1f}x cap, not adding more exposure"
                    )
                self._pending_entry_signals.pop(sym, None)
            except Exception as e:
                log.warning(f"enforce_portfolio_leverage: cancel pending entry {sym} failed: {e}")

    # -- EOD Close -------------------------------------------------------------
    @staticmethod
    def _guardrail_fail_reason(
        avg_daily_vol: Optional[float], shares_float: Optional[float], market_cap: Optional[float]
    ) -> Optional[str]:
        """Pure decision logic for close_guardrail_fail_positions: return the
        reason string if any known metric is below its guardrail, else None
        (passes, or all three are unavailable -- missing data never forces a
        close). Split out from the method below so it's unit-testable without
        a broker connection."""
        if avg_daily_vol is not None and avg_daily_vol < MIN_AVG_DAILY_VOLUME_REGULAR_HOURS:
            return f"avg_volume {avg_daily_vol:.0f} < {MIN_AVG_DAILY_VOLUME_REGULAR_HOURS:.0f}"
        if shares_float is not None and shares_float < MIN_FLOAT_SHARES:
            return f"float {shares_float/1e6:.1f}M < {MIN_FLOAT_SHARES/1e6:.0f}M"
        if market_cap is not None and market_cap < MIN_MARKET_CAP:
            return f"mcap ${market_cap/1e6:.0f}M < ${MIN_MARKET_CAP/1e6:.0f}M"
        return None

    def _exchange_close_for_today(self, now_et: datetime.datetime) -> Tuple[datetime.datetime, datetime.datetime, str]:
        """Return (official close ET, EOD close ET, source), cached per day.

        Uses Alpaca's exchange calendar so early-close sessions flatten in
        time even when their close is before the configured EOD time. On a
        regular session the CONFIGURED EOD_CLOSE_TIME governs (2026-09-03:
        user set 15:44 -- the old close-10min override was why the log showed
        eod_exit=15:50 while config said 15:45). We take the EARLIER of the
        two so both the user's cutoff AND early-close protection always hold.
        Falls back to configured MARKET_CLOSE/EOD_CLOSE_TIME if the calendar
        is unavailable.
        """
        import pytz

        et = pytz.timezone("America/New_York")
        today = now_et.date()
        cached = getattr(self, "_exchange_close_cache", {}).get(today)
        if cached:
            return cached

        close_at = None
        source = "config"
        try:
            from alpaca.trading.requests import GetCalendarRequest
            today_s = today.isoformat()
            cal = self.client.get_calendar(GetCalendarRequest(start=today_s, end=today_s)) or []
            if cal:
                close_value = getattr(cal[0], "close", None)
                close_dt = close_value.to_pydatetime() if hasattr(close_value, "to_pydatetime") else close_value
                if isinstance(close_dt, str):
                    close_dt = datetime.datetime.fromisoformat(close_dt.replace("Z", "+00:00"))
                if isinstance(close_dt, datetime.time):
                    close_dt = datetime.datetime.combine(today, close_dt)
                if getattr(close_dt, "tzinfo", None) is None:
                    close_dt = et.localize(close_dt)
                close_at = close_dt.astimezone(et)
                source = "exchange-calendar"
        except Exception as e:
            log.debug(f"exchange calendar close lookup failed, using configured close times: {e}")

        if close_at is None:
            close_h, close_m = map(int, MARKET_CLOSE.split(":"))
            close_at = now_et.replace(hour=close_h, minute=close_m, second=0, microsecond=0)

        # EARLIER of the configured cutoff and calendar-close-minus-10min:
        # the user's EOD_CLOSE_TIME governs regular sessions (15:44), while an
        # early close (e.g. 13:00 holiday session -> 12:50 flatten) still wins
        # whenever it would land before the configured time.
        configured_h, configured_m = map(int, EOD_CLOSE_TIME.split(":"))
        configured_eod = now_et.replace(hour=configured_h, minute=configured_m, second=0, microsecond=0)
        eod_at = min(configured_eod, close_at - datetime.timedelta(minutes=10))
        source = f"{source}+config-min" if source == "exchange-calendar" else "config"

        if not hasattr(self, "_exchange_close_cache"):
            self._exchange_close_cache = {}
        self._exchange_close_cache = {today: (close_at, eod_at, source)}
        log.info(
            f"EOD schedule for {today}: exchange_close={close_at.strftime('%H:%M')} ET, "
            f"eod_exit={eod_at.strftime('%H:%M')} ET ({source})"
        )
        return close_at, eod_at, source

    def _cancel_pending_entry_orders(self, reason: str) -> None:
        """Cancel every still-resting DAY ENTRY order (fresh / re-entry /
        staged add) and clear the local pending-entry bookkeeping that
        described them.

        2026-09-04 (NFLX): the EOD sweep only cancelled orders belonging to
        symbols it was CLOSING -- a trailing-buy resting for a symbol with no
        position kept sitting at the broker and filled at 15:44:37, after the
        flatten had begun (and the earlier NFLX chain's done-set entry then
        blocked the rerun sweep from closing the new fill). Called at the
        lunch (11:00) and EOD (15:44) boundaries so NOTHING new can fill past
        a session boundary.

        GTC trailing stops are NEVER touched: they are protective exits on
        held positions, not entries (classify_symbol_order is the authority).
        Idempotent -- every minute's rerun finds nothing to cancel and just
        keeps local state clean.
        """
        cancelled = 0
        try:
            positions = {p.symbol: p for p in (self.client.get_all_positions() or [])}
        except Exception:
            positions = {}
        try:
            open_orders = self.client.get_orders() or []
        except Exception as e:
            log.warning(f"_cancel_pending_entry_orders ({reason}): order fetch failed: {e}")
            return
        for order in open_orders:
            sym = getattr(order, "symbol", None)
            view = _normalize_order_view(order)
            kind = classify_symbol_order(view, int(float(positions.get(sym, SimpleNamespace(qty=0)).qty) or 0))
            # Entry-side orders only. On a symbol with NO position,
            # position_qty is 0 so classify returns id-based kinds
            # (entry/staged_entry/reentry) from the client_order_id alone.
            if kind not in ("entry", "staged_entry", "reentry"):
                continue
            try:
                self.client.cancel_order_by_id(str(order.id))
                cancelled += 1
            except Exception:
                pass
        if cancelled:
            log.warning(f"[BOUNDARY] {reason}: cancelled {cancelled} pending entry order(s) -- nothing may fill past the boundary")
            _telemetry_log("boundary_pending_entries_cancelled", reason=reason, cancelled=cancelled)
        # Local bookkeeping for the dead orders must go too -- a stale
        # _entry_pending/_pending_entry_signals entry makes the 5s sweeps
        # treat a cancelled order as live (and _staged_allocation would keep
        # trying to add tranches for it).
        self.order_cache.clear()
        self._entry_pending.clear()
        self._pending_entry_signals.clear()
        self._staged_allocation.clear()

    def close_eod_positions(self) -> Optional[dict]:
        """Close every same-day position at EOD_CLOSE_TIME, regardless of
        strategy.

        2026-08-24, user request ("I wouldn't expect any positions to stay
        active at 3:50pm ET" / "don't leave it for trail order"): dropped
        the EOD_CLOSE_STRATEGIES allow-list gate. Confirmed live: SPXU
        (Technical) and WULF (LiquiditySweep) both sat open past 15:50 ET
        because neither strategy was on that list -- close_eod_positions
        logged "EOD email skipped" every minute without ever attempting
        either close, and both only closed ~15 min after the 16:00 ET
        market close via the passive after-hours stop instead. The
        strategy list dates to a narrower 2026-08-22 version of this
        function and was never kept in sync as new strategies (Technical,
        LiquiditySweep, TrendBreaker, ...) were added -- rather than keep
        patching that list, EOD close now just means every same-day
        position, no allow-list to fall out of date again. A multi-day
        swing hold is still out of scope (gated by the same-day-entry
        check below, unchanged). close_guardrail_fail_positions is the
        other half of the overnight picture but is itself disabled (see
        GUARDRAIL_EOD_CLOSE_ENABLED in config.py).

        2026-08-17, user request: runs every minute through the window
        (schedule.every(1).minutes) rather than once per day -- the old
        once-per-day flag meant a position opened AFTER the first post-
        15:45 tick (ASST/NUAI, opened 15:57 ET) had already missed its
        only chance to be flattened. Safe to call repeatedly: a symbol
        already closed has its _entry_log entry popped below, so the next
        tick's `if not entry_info: continue` is a no-op for it -- only
        newly-opened, not-yet-processed positions actually submit an order."""
        if not EOD_CLOSE_ENABLED:
            return None

        import pytz
        et = pytz.timezone("America/New_York")
        now_et = datetime.datetime.now(et)
        market_state = MarketState.from_now(now_et)
        if not market_state.weekday or not market_state.is_regular_hours:
            return None  # Only submit EOD closes during regular hours on market weekdays

        exchange_close_at, eod_at, _source = self._exchange_close_for_today(now_et)
        if now_et < eod_at:
            return None  # Not yet EOD close time
        if now_et >= exchange_close_at:
            return None  # Official exchange close has passed

        try:
            positions = self.client.get_all_positions()
        except Exception as e:
            log.error(f"close_eod_positions: fetch failed: {e}")
            return None

        # 2026-09-04 (NFLX): cancel every still-resting DAY ENTRY order the
        # moment the EOD window opens -- not just per-position. The EOD sweep
        # used to only cancel orders belonging to symbols it was closing, so
        # a trailing-buy resting for a symbol with NO position kept sitting
        # at the broker and could fill at 15:44:37 -- after the flatten had
        # begun. GTC protective stops are not touched here (they belong to
        # the per-position close path below); DAY entry/re-entry/staged
        # orders all die so nothing new can fill past the boundary.
        self._cancel_pending_entry_orders("EOD close window open")

        today = now_et.date()
        already_closed = getattr(self, "_eod_closed", {}).setdefault(today, set())
        self._eod_closed = {today: already_closed}
        closed_items = []
        failed_items = []

        for pos in positions:
            sym = pos.symbol
            qty = int(float(pos.qty))
            if qty == 0:
                continue
            if sym in already_closed:
                # 2026-09-04 (NFLX): a symbol whose EARLIER chain was closed
                # today can REAPPEAR when a race-fill lands after this sweep
                # first ran (NFLX refilled at 15:44:37; its done-set entry
                # from the morning close silently blocked the rerun). If no
                # active close order is resting for it, clear the done mark
                # so this pass re-closes it; if one IS resting, keep waiting.
                try:
                    active_close = any(
                        getattr(o, "symbol", None) == sym
                        for o in (self.client.get_orders() or [])
                    )
                except Exception:
                    active_close = True  # unknown -- don't double-submit
                if active_close:
                    continue
                already_closed.discard(sym)
                log.warning(f"EOD CLOSE {sym}: reappeared after an earlier close (race-fill) -- re-closing")

            entry_info = self._entry_log.get(sym) or {}
            strategy = entry_info.get("strategy", "unknown")

            side = OrderSide.SELL if qty > 0 else OrderSide.BUY
            try:
                # Cancel EVERY resting order for this symbol, GTC trailing
                # stops included, before submitting the market close.
                #
                # 2026-08-25: previously only DAY-TIF orders were cancelled
                # here, on the reasoning that the GTC trailing stop
                # "protects the position until the close fill settles."
                # That reasoning was backwards and caused a live bug:
                # the GTC stop reserves the shares (held_for_orders), so
                # the close order the bot submits right after is REJECTED
                # by the broker every single cycle with "insufficient qty
                # available for order" -- the stop can't protect a sell
                # that can never execute because the stop itself is
                # blocking it. Confirmed live 2026-08-25: ABCL/EH/FUTG/
                # INR/PRME/WLFC/WTI all failed to close every ~1 min from
                # 14:51 ET onward and were still open hours into
                # after-hours, unrealized gains never booked. Now cancels
                # the GTC stop too -- if the close itself then fails for a
                # different reason (see except below), a fresh GTC stop is
                # re-armed so the position is never left fully naked.
                try:
                    sym_orders = [o for o in (self.client.get_orders() or []) if o.symbol == sym]
                    for _o in sym_orders:
                        try:
                            self.client.cancel_order_by_id(str(_o.id))
                        except Exception:
                            pass
                    if sym_orders:
                        time.sleep(0.4)
                except Exception:
                    pass

                # Submit the first EOD close as a regular-session DAY limit.
                # If it misses the bell, _sweep_force_closes keeps chasing
                # with extended-hours limits because Alpaca equity trailing
                # stops do not execute after-hours.
                # 2026-08-26, user request ("irrespective of exit type
                # reentry should happen for the top 30 list") briefly left
                # this unmarked on the reasoning that ENTRY_WINDOW_END_ET
                # already blocks re-arming this late anyway -- user then
                # explicitly asked to exclude EOD regardless ("don't
                # reenter after end of day exit"), so this marks it directly
                # rather than relying on that time-window side effect.
                self._no_rearm.add(sym)
                self._submit_closing_order(sym, abs(qty), side, float(pos.current_price), no_extended_hours=True)
                already_closed.add(sym)
                self._entry_log.pop(sym, None)
                self._force_close_pending[sym] = {"reason": f"eod:{strategy}", "chase_count": 0}

                pnl = float(pos.unrealized_pl)
                closed_items.append({
                    "symbol": sym,
                    "qty": abs(qty),
                    "strategy": strategy,
                    "pnl": pnl,
                })

                log.info(
                    f"EOD CLOSE {sym}: {abs(qty)} shares | "
                    f"strategy={strategy} | P&L ${pnl:.2f}"
                )
            except Exception as e:
                failed_items.append({"symbol": sym, "error": str(e)})
                log.error(f"EOD close failed {sym}: {e}")
                # The GTC stop was just cancelled above -- re-arm a fallback
                # so a close failure for some other reason doesn't leave the
                # position unprotected overnight.
                try:
                    trail_pct, _ = _atr_trail_pct_for(sym, float(pos.current_price), self._entry_log)
                    self.client.submit_order(TrailingStopOrderRequest(
                        symbol=sym, qty=abs(qty), side=side,
                        type=AlpacaOrderType.TRAILING_STOP, time_in_force=TimeInForce.GTC,
                        trail_percent=trail_pct,
                    ))
                    log.warning(f"close_eod_positions {sym}: re-armed GTC trailing stop after failed close")
                except Exception as rearm_err:
                    log.error(f"close_eod_positions {sym}: close failed AND GTC re-arm failed -- position may be UNPROTECTED: {rearm_err}")

        summary = {
            "date": today.isoformat(),
            "closed_count": len(closed_items),
            "failed_count": len(failed_items),
            "closed_items": closed_items,
            "failed_items": failed_items,
            "asof": now_et.isoformat(),
        }
        return summary

    def close_guardrail_fail_positions(self) -> Optional[dict]:
        """5 min before close, force-close any open position (any strategy --
        unlike close_eod_positions, not limited to EOD_CLOSE_STRATEGIES) that
        currently fails the standard liquidity/quality guardrails: avg daily
        volume, float shares, or market cap. Only guardrail-passing names get
        held after-hours/overnight.

        2026-08-17, user request: runs every minute through the window
        (schedule.every(1).minutes) instead of once per day -- the old
        once-per-day flag meant a position opened AFTER the first post-
        close-time tick (ASST/NUAI, opened 15:57 ET) had already missed its
        only chance to be checked. Unlike close_eod_positions, this function
        has no natural per-symbol idempotency marker (nothing it pops on
        success), so _guardrail_eod_closed tracks which symbols already had
        a close attempted today -- without it, a reruns-every-minute version
        would re-cancel and resubmit a still-unfilled close order on the
        same illiquid symbol every single minute instead of leaving it to
        _sweep_force_closes's own re-chase cadence."""
        if not GUARDRAIL_EOD_CLOSE_ENABLED:
            return None

        import pytz
        now_et = datetime.datetime.now(pytz.timezone("America/New_York"))
        close_h, close_m = map(int, GUARDRAIL_EOD_CLOSE_TIME.split(":"))
        if now_et.hour < close_h or (now_et.hour == close_h and now_et.minute < close_m):
            return None  # Not yet the guardrail close time
        if now_et.hour >= 16:
            return None  # Market already closed

        today = datetime.date.today()
        already_closed = self._guardrail_eod_closed.setdefault(today, set())
        self._guardrail_eod_closed = {today: already_closed}  # drop any stale prior-day entries

        try:
            positions = self.client.get_all_positions()
        except Exception as e:
            log.error(f"close_guardrail_fail_positions: fetch failed: {e}")
            return None

        closed_items = []
        failed_items = []

        for pos in positions:
            sym = pos.symbol
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue  # options legs -- managed separately

            qty = int(float(pos.qty))
            if qty == 0:
                continue
            if sym in already_closed:
                continue  # close already attempted today -- _sweep_force_closes chases it if still unfilled

            try:
                daily = get_daily_volume_bars(sym)
                avg_daily_vol = (
                    float(daily["volume"].iloc[:-1].mean())
                    if not daily.empty and len(daily) >= 2 else None
                )
            except Exception as e:
                log.warning(f"close_guardrail_fail_positions {sym}: volume lookup failed: {e}")
                avg_daily_vol = None
            shares_float = _get_float_shares(sym)
            market_cap   = _get_market_cap(sym)

            fail_reason = self._guardrail_fail_reason(avg_daily_vol, shares_float, market_cap)
            if fail_reason is None:
                continue  # passes guardrails (or data unavailable) -- fine to hold overnight

            try:
                sym_orders = [o for o in (self.client.get_orders() or []) if o.symbol == sym]
                for _o in sym_orders:
                    try:
                        self.client.cancel_order_by_id(str(_o.id))
                    except Exception:
                        pass
                if sym_orders:
                    time.sleep(0.4)
            except Exception as e:
                log.warning(f"close_guardrail_fail_positions {sym}: order fetch/cancel failed, will retry next cycle: {e}")
                continue

            close_side = OrderSide.SELL if qty > 0 else OrderSide.BUY
            try:
                # 2026-08-18, user request: same as close_eod_positions -- no
                # force_extended_hours, no after-hours chase. Carries overnight
                # under its GTC trailing stop if unfilled by the 16:00 close.
                self._no_rearm.add(sym)  # structurally unsafe -- re-entering defeats the guardrail
                self._submit_closing_order(sym, abs(qty), close_side, float(pos.current_price), no_extended_hours=True)
                self._force_close_pending[sym] = {"reason": f"guardrail:{fail_reason}", "chase_count": 0}
                already_closed.add(sym)
                pnl = float(pos.unrealized_pl)
                closed_items.append({"symbol": sym, "qty": abs(qty), "reason": fail_reason, "pnl": pnl})
                log.info(f"GUARDRAIL EOD CLOSE {sym}: {abs(qty)} shares | {fail_reason} | P&L ${pnl:.2f}")
            except Exception as e:
                failed_items.append({"symbol": sym, "error": str(e)})
                log.error(f"GUARDRAIL EOD CLOSE failed {sym}: {e}")

        summary = {
            "date": today.isoformat(),
            "closed_count": len(closed_items),
            "failed_count": len(failed_items),
            "closed_items": closed_items,
            "failed_items": failed_items,
            "asof": now_et.isoformat(),
        }
        return summary


    def lunch_flat_positions(self) -> Optional[dict]:
        """Close EVERY position and cancel EVERY open order when the midday
        break begins (LUNCH_FLAT_TIME_ET = 11:00 ET), then keep the book flat
        through the 11:00-14:45 break.

        2026-09-01, user request ("at 11AM close all positions and open
        orders, and reenter only at 2:45PM and again exist all at 3:50"): the
        entry window is now two disjoint segments (09:14-11:00 and 14:45-15:50)
        and the book must be FULLY flat in between -- positions do not carry
        across the break. Shares the close_eod_positions machinery: per-symbol
        cancel-every-resting-order first (GTC stops included -- a resting stop
        reserves the shares and would reject the close), _no_rearm marking so
        detect_stopped_out_positions never re-arms a lunch-flattened name,
        _force_close_pending population so _sweep_force_closes chases any
        unfilled close on its poll until actually flat, and a per-day
        per-symbol done set so it's safe to call every minute (the
        orchestrator's _lunch_flat_job runs it on schedule.every(1).minutes).
        On top of that it also cancels EVERY open order sweep-wide (not just
        per-position), so a resting entry for a symbol with no position --
        e.g. a trailing-buy that never filled -- can't fill mid-break either.
        Options legs (OCC symbols) are skipped: options trading was removed
        2026-09-01 and any legacy legs are left untouched."""
        if not LUNCH_FLAT_ENABLED:
            return None

        import pytz
        et = pytz.timezone("America/New_York")
        now_et = datetime.datetime.now(et)
        market_state = MarketState.from_now(now_et)
        if not market_state.weekday or not market_state.is_regular_hours:
            return None

        lunch_start = datetime.datetime.strptime(LUNCH_FLAT_TIME_ET, "%H:%M").time()
        break_end = datetime.datetime.strptime(ENTRY_WINDOW_BREAK_END_ET, "%H:%M").time()
        if not (lunch_start <= now_et.time() < break_end):
            return None  # not inside the break window

        today = now_et.date()
        already_closed = getattr(self, "_lunch_closed", {}).setdefault(today, set())
        self._lunch_closed = {today: already_closed}

        # 1) Cancel EVERY open order sweep-wide. Re-runs each minute so an
        #    order that raced the previous pass (placed 10:59:50, resting as
        #    this fires) still dies this pass instead of filling mid-break.
        cancelled_ids: set = set()
        cancelled_orders = 0
        try:
            open_orders = self.client.get_orders() or []
            for _o in open_orders:
                try:
                    self.client.cancel_order_by_id(str(_o.id))
                    cancelled_ids.add(str(_o.id))
                    cancelled_orders += 1
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"lunch_flat_positions: open-order cancel pass failed: {e}")

        # 2026-09-04: the broker-side sweep above kills every resting order,
        # but the LOCAL pending-entry bookkeeping (_entry_pending /
        # _pending_entry_signals / order_cache / staged-add state) still
        # described those dead orders. Clear it so the 14:15 reopen starts
        # from fresh state instead of "reviving" morning orders whose broker
        # legs were just cancelled (a stale _entry_pending entry made the
        # pending-entry sweep treat a dead order as live through the break).
        self.order_cache.clear()
        self._entry_pending.clear()
        self._pending_entry_signals.clear()
        self._staged_allocation.clear()

        try:
            positions = self.client.get_all_positions()
        except Exception as e:
            log.error(f"lunch_flat_positions: fetch failed: {e}")
            return None

        closed_items = []
        failed_items = []
        for pos in positions:
            sym = pos.symbol
            qty = int(float(pos.qty))
            if qty == 0:
                continue
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue  # legacy options leg -- untouched (options removed 2026-09-01)
            if sym in already_closed:
                continue

            entry_info = self._entry_log.get(sym) or {}
            strategy = entry_info.get("strategy", "unknown")
            side = OrderSide.SELL if qty > 0 else OrderSide.BUY
            try:
                try:
                    sym_orders = [o for o in (self.client.get_orders() or []) if o.symbol == sym and str(o.id) not in cancelled_ids]
                    for _o in sym_orders:
                        try:
                            self.client.cancel_order_by_id(str(_o.id))
                        except Exception:
                            pass
                    if sym_orders:
                        time.sleep(0.4)
                except Exception:
                    pass

                # No re-entry after the lunch flat: directly mark the symbol so
                # detect_stopped_out_positions/_maybe_rearm_reentry never
                # re-arm a lunch-flattened name (same reasoning as EOD).
                self._no_rearm.add(sym)
                self._submit_closing_order(sym, abs(qty), side, float(pos.current_price), no_extended_hours=True)
                already_closed.add(sym)
                self._entry_log.pop(sym, None)
                self._force_close_pending[sym] = {"reason": "lunch", "chase_count": 0}

                pnl = float(pos.unrealized_pl)
                closed_items.append({"symbol": sym, "qty": abs(qty), "strategy": strategy, "pnl": pnl})
                log.info(f"LUNCH-FLAT CLOSE {sym}: {abs(qty)} shares | {strategy} | P&L ${pnl:.2f}")
            except Exception as e:
                failed_items.append({"symbol": sym, "error": str(e)})
                log.error(f"LUNCH-FLAT CLOSE failed {sym}: {e}")

        summary = {
            "date": today.isoformat(),
            "closed_count": len(closed_items),
            "cancelled_orders": cancelled_orders,
            "failed_count": len(failed_items),
            "closed_items": closed_items,
            "failed_items": failed_items,
            "asof": now_et.isoformat(),
        }
        if closed_items or cancelled_orders:
            log.warning(
                f"[LUNCH-FLAT] {now_et.strftime('%H:%M')} ET -- closed {len(closed_items)} position(s), "
                f"cancelled {cancelled_orders} open order(s), {len(failed_items)} failed"
            )
        return summary

    def guardian_halt_flatten(self, reason: str = "guardian") -> Optional[dict]:
        """Emergency flatten for the loss-guardian daily-loss backstop
        (scripts/guardian.py -> flat_request.flag -> orchestrator poll tick).

        Same mechanics as lunch_flat_positions -- cancel EVERY resting order
        (GTC stops included: a resting stop reserves the shares and would
        reject the close), _no_rearm so nothing ever re-arms the flattened
        name, _force_close_pending so _sweep_force_closes chases unfilled
        closes -- but deliberately NOT time-gated: it fires the moment the
        guardian flag is seen, regular hours or not. Once fired it also sets
        _halt_until_eod so every entry/re-entry path (gated in
        _submit_entry_order) stays blocked until the next daily reset.
        Per-day deduped (_guardian_halt_closed) -- safe to call on every
        5s poll tick.
        """
        import pytz
        now_et = datetime.datetime.now(pytz.timezone("America/New_York"))
        today = now_et.date()
        if getattr(self, "_guardian_halt_closed", None) == today:
            # Already flattened today -- keep entries blocked, do not re-close.
            self._halt_until_eod = True
            return None

        log.warning("=" * 70)
        log.warning(f"[GUARDIAN-HALT] {reason} -- closing ALL positions and cancelling ALL orders")
        log.warning("=" * 70)

        # 1) Cancel EVERY open order sweep-wide (a racing order must not fill
        #    after the halt decision).
        cancelled_orders = 0
        try:
            for _o in (self.client.get_orders() or []):
                try:
                    self.client.cancel_order_by_id(str(_o.id))
                    cancelled_orders += 1
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"guardian_halt_flatten: open-order cancel pass failed: {e}")

        # 2) Market-close every non-option position.
        try:
            positions = self.client.get_all_positions() or []
        except Exception as e:
            log.error(f"guardian_halt_flatten: positions fetch failed: {e}")
            positions = []

        closed = failed = 0
        for pos in positions:
            sym = pos.symbol
            qty = int(float(pos.qty))
            if qty == 0 or re.match(r"^[A-Z]+\d{6}[CP]\d{8}$", sym):
                continue  # legacy options leg -- untouched (options removed 2026-09-01)
            side = OrderSide.SELL if qty > 0 else OrderSide.BUY
            try:
                try:
                    for _o in (self.client.get_orders() or []):
                        if _o.symbol == sym:
                            try:
                                self.client.cancel_order_by_id(str(_o.id))
                            except Exception:
                                pass
                    time.sleep(0.4)
                except Exception:
                    pass
                self._no_rearm.add(sym)
                self._submit_closing_order(sym, abs(qty), side, float(pos.current_price), no_extended_hours=True)
                self._force_close_pending[sym] = {"reason": "guardian", "chase_count": 0}
                self._entry_log.pop(sym, None)
                closed += 1
                log.warning(f"[GUARDIAN-HALT] CLOSE {sym}: {abs(qty)} shares @ ${pos.current_price}")
            except Exception as e:
                failed += 1
                log.error(f"[GUARDIAN-HALT] CLOSE failed {sym}: {e}")

        self._guardian_halt_closed = today
        self._halt_until_eod = True
        log.warning(
            f"[GUARDIAN-HALT] Flat sweep done -- {closed} closed, {failed} failed, "
            f"{cancelled_orders} orders cancelled. Entries blocked until the next daily reset."
        )
        try:
            send_email(
                "[APEXTRADER] GUARDIAN HALT -- positions flattened",
                f"ApexTrader was hard-flattened by the loss guardian at {now_et.isoformat()}.\n\n"
                f"Reason: {reason}\n"
                f"Positions closed: {closed} | failed: {failed} | open orders cancelled: {cancelled_orders}\n\n"
                f"All new entries are blocked until the next daily reset.",
            )
        except Exception as e:
            log.error(f"[GUARDIAN-HALT] alert email failed: {e}")

        return {
            "closed": closed,
            "failed": failed,
            "cancelled_orders": cancelled_orders,
            "date": today.isoformat(),
        }


    def _sweep_force_closes(self) -> None:
        """Poll every symbol close_eod_positions / close_guardrail_fail_positions
        submitted a close for but hasn't confirmed flat yet (self._force_close_pending).
        A single limit order can miss its fill -- price drifted past the limit, or
        it was still resting when the regular/extended session boundary hit --
        without this the position would just sit open, silently surviving the
        force-close it was supposed to get. Re-chases with a fresh live-bid/ask
        limit at escalating slip (same shape as check_afterhours_stops) until
        it's actually flat. Meant to be polled frequently (the 10s software-stop
        thread) so it catches a stale order quickly.
        ponytail: no cap on total re-chase attempts within regular hours (only
        slip% is capped, at 3%) -- a genuinely halted/no-bid symbol would retry
        indefinitely. Add a max-attempts giveup (with an alert) if that's ever
        observed live.

        If the regular-session close misses, keep chasing into extended hours
        with extended-hours limits. Alpaca equity trailing stops do not execute
        after-hours, so a resting GTC trail is not considered sufficient EOD
        protection."""
        if not self._force_close_pending:
            return
        try:
            positions   = {p.symbol: p for p in self.client.get_all_positions()}
            open_orders = self.client.get_orders() or []
        except Exception as e:
            log.warning(f"_sweep_force_closes: fetch failed: {e}")
            return

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        orders_by_sym: Dict[str, list] = {}
        for o in open_orders:
            orders_by_sym.setdefault(o.symbol, []).append(o)

        for sym, info in list(self._force_close_pending.items()):
            pos = positions.get(sym)
            qty = int(float(pos.qty)) if pos is not None else 0
            if pos is None or qty == 0:
                self._force_close_pending.pop(sym, None)  # confirmed flat
                continue

            sym_orders = orders_by_sym.get(sym, [])

            if not self._current_market_state().is_regular_hours:
                # Alpaca equity trailing stops do not execute in extended
                # hours. If an EOD/force close missed the bell, keep chasing
                # with extended-hours marketable limits instead of assuming a
                # resting GTC trail is meaningful protection overnight.
                pending = next((o for o in sym_orders if getattr(o, "time_in_force", None) != TimeInForce.GTC), None)
                if pending is not None:
                    submitted_at = getattr(pending, "submitted_at", None) or getattr(pending, "created_at", None)
                    age_s = (now_utc - submitted_at).total_seconds() if submitted_at else 0.0
                    if age_s < AFTERHOURS_CHASE_STALE_SECONDS:
                        continue
                cancel_failed = False
                for order in sym_orders:
                    try:
                        self.client.cancel_order_by_id(str(order.id))
                    except Exception as e:
                        cancel_failed = True
                        log.warning(f"_sweep_force_closes {sym} [{info.get('reason')}]: after-hours order cancel failed, will retry next poll: {e}")
                        break
                if cancel_failed:
                    continue
                if sym_orders:
                    time.sleep(0.4)

                side = OrderSide.SELL if qty > 0 else OrderSide.BUY
                try:
                    chase_n = info.get("chase_count", 0)
                    slip_pct = min(0.5 * (chase_n + 1), 3.0)
                    self._submit_closing_order(sym, abs(qty), side, float(pos.current_price), slip_pct=slip_pct, force_extended_hours=True)
                    info["chase_count"] = chase_n + 1
                    log.warning(
                        f"AFTER-HOURS FORCE-CLOSE {sym} [{info.get('reason')}]: GTC trails inactive after-hours -- "
                        f"submitted extended-hours close @ {slip_pct:.1f}% slip (attempt {chase_n + 1})"
                    )
                except Exception as e:
                    log.error(f"_sweep_force_closes {sym} [{info.get('reason')}]: after-hours close failed, will retry next poll: {e}")
                continue

            pending = next((o for o in sym_orders if getattr(o, "time_in_force", None) != TimeInForce.GTC), None)
            if pending is not None:
                submitted_at = getattr(pending, "submitted_at", None) or getattr(pending, "created_at", None)
                age_s = (now_utc - submitted_at).total_seconds() if submitted_at else 0.0
                if age_s < AFTERHOURS_CHASE_STALE_SECONDS:
                    continue  # still fresh -- give it time to fill
                try:
                    self.client.cancel_order_by_id(str(pending.id))
                    time.sleep(0.4)
                except Exception as e:
                    log.warning(f"_sweep_force_closes {sym}: stale-close cancel failed, will retry next poll: {e}")
                    continue

            # A resting GTC (re-armed as a fallback by another path, or never
            # cancelled) reserves the qty and would reject the replacement close.
            gtc_cancelled = False
            gtc = next((o for o in sym_orders if getattr(o, "time_in_force", None) == TimeInForce.GTC), None)
            if gtc:
                try:
                    self.client.cancel_order_by_id(str(gtc.id))
                    time.sleep(0.4)
                    gtc_cancelled = True
                except Exception as e:
                    log.warning(f"_sweep_force_closes {sym}: GTC cancel failed, will retry next poll: {e}")
                    continue

            side = OrderSide.SELL if qty > 0 else OrderSide.BUY
            try:
                chase_n  = info.get("chase_count", 0)
                slip_pct = min(0.5 * (chase_n + 1), 3.0)
                # no_extended_hours=True -- still regular hours here (checked above),
                # and this reason (eod/guardrail) must never spill into extended
                # hours, even if MarketState.from_now() has already flipped by the
                # time this fires (right at the 16:00 boundary).
                self._submit_closing_order(sym, abs(qty), side, float(pos.current_price), slip_pct=slip_pct, no_extended_hours=True)
                info["chase_count"] = chase_n + 1
                log.warning(
                    f"FORCE-CLOSE RE-CHASE {sym} [{info.get('reason')}]: unfilled after prior attempt "
                    f"-- resubmitted @ {slip_pct:.1f}% slip (attempt {chase_n + 1})"
                )
            except Exception as e:
                log.error(f"_sweep_force_closes {sym}: re-chase failed: {e}")
                # GTC is gone and the replacement didn't go through -- without a
                # fallback the position would sit fully unprotected until the next
                # poll. Re-arm one now, same as check_afterhours_stops.
                if gtc_cancelled:
                    try:
                        trail_pct, _ = _atr_trail_pct_for(sym, float(pos.current_price), self._entry_log)
                        self.client.submit_order(TrailingStopOrderRequest(
                            symbol        = sym,
                            qty           = abs(qty),
                            side          = side,
                            type          = AlpacaOrderType.TRAILING_STOP,
                            time_in_force = TimeInForce.GTC,
                            trail_percent = trail_pct,
                        ))
                        log.warning(f"_sweep_force_closes {sym}: re-armed GTC trailing stop as fallback after failed re-chase")
                    except Exception as rearm_err:
                        log.error(f"_sweep_force_closes {sym}: re-chase failed AND GTC re-arm failed -- position may be UNPROTECTED: {rearm_err}")

    def _sweep_pending_entries(self) -> None:
        """Re-chase a resting ENTRY order that hasn't filled within
        AFTERHOURS_CHASE_STALE_SECONDS -- cancel and resubmit at a fresh
        live-mid-bounded limit with escalating slip, same shape as
        _sweep_force_closes on the exit side. 2026-08-14, confirmed live:
        without this, an entry that misses its initial 1%-bounded limit (a
        fast-moving or wide-spread name -- MF: bid $12.95/ask $17.20,
        order resting unfilled at $15.21) just sits until end of day and
        is silently never entered, no matter how good the signal was --
        every close path already re-chases, entries never did.
        ponytail: no cap on total re-chase attempts (only slip% is capped,
        at 3%) -- same known ceiling as _sweep_force_closes.

        2026-08-17, faded/stale entries (info["price_ceiling"] present, see
        _create_bracket_order): the FIRST wait uses
        FADED_ENTRY_PASSIVE_WINDOW_SECONDS instead of the normal (shorter)
        AFTERHOURS_CHASE_STALE_SECONDS -- give the passive limit its full
        window before touching it at all. Every chase after that is capped
        at price_ceiling (today's baseline price) until
        FADED_ENTRY_CEILING_TIMEOUT_SECONDS have passed since the ORIGINAL
        submission, so a reversal makes the fix wait, never chase upward
        into it -- only after that timeout does it fall through to the
        normal uncapped escalation, as a last resort so the trade isn't
        lost entirely (2026-08-14 "trade it anyway" rule).

        2026-08-17: also only ever re-chases the QUANTITY STILL UNFILLED,
        not the original full size -- a partial fill (routine on the thin/
        illiquid names this mostly applies to, and far more likely now that
        faded entries can rest for minutes instead of filling near-
        instantly) used to get topped up with a second full-size order on
        cancel+resubmit, silently over-buying the position."""
        if not self._entry_pending:
            return
        # 2026-09-01, two-window schedule: no entry re-chase during the midday
        # break -- the lunch flat (lunch_flat_positions, 11:00 ET) cancels
        # every resting entry order anyway, and re-chasing here would just
        # re-place a morning entry into the break. Pending entries WAIT for
        # the 14:45 afternoon segment. The poller cycle itself keeps running.
        import pytz as _pytz
        if in_lunch_break(datetime.datetime.now(_pytz.timezone("America/New_York"))):
            return
        try:
            open_orders = self.client.get_orders() or []
        except Exception as e:
            log.warning(f"_sweep_pending_entries: fetch failed: {e}")
            return

        orders_by_id = {str(o.id): o for o in open_orders}
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        for sym, info in list(self._entry_pending.items()):
            pending = orders_by_id.get(info.get("order_id"))
            if pending is None:
                # No longer resting under that order id -- filled, cancelled
                # elsewhere, or expired. Either way, nothing left to chase.
                self._entry_pending.pop(sym, None)
                continue

            is_faded = "price_ceiling" in info
            submitted_at = getattr(pending, "submitted_at", None) or getattr(pending, "created_at", None)
            age_s = (now_utc - submitted_at).total_seconds() if submitted_at else 0.0
            stale_threshold = (
                FADED_ENTRY_PASSIVE_WINDOW_SECONDS
                if is_faded and info.get("chase_count", 0) == 0
                else AFTERHOURS_CHASE_STALE_SECONDS
            )
            if age_s < stale_threshold:
                continue  # still fresh -- give it time to fill

            filled_so_far = int(float(getattr(pending, "filled_qty", 0) or 0))
            remaining = info["qty"] - filled_so_far
            if remaining <= 0:
                self._entry_pending.pop(sym, None)  # fully filled already, nothing left to chase
                continue

            try:
                self.client.cancel_order_by_id(str(pending.id))
                time.sleep(0.4)
            except Exception as e:
                log.warning(f"_sweep_pending_entries {sym}: stale-order cancel failed, will retry next poll: {e}")
                continue

            is_long = info["is_long"]
            side = OrderSide.BUY if is_long else OrderSide.SELL
            try:
                chase_n  = info.get("chase_count", 0)
                slip_pct = _entry_rechase_slip_pct(chase_n)
                mid = _live_quote_mid(self.client, sym, float(pending.limit_price or 0) or 0.01)
                fresh_limit = _marketable_limit_price(mid, is_long=is_long, buffer_pct=slip_pct)

                ceiling = info.get("price_ceiling")
                if ceiling is not None:
                    first_at = info.get("first_submitted_at")
                    ceiling_age_s = (now_utc - first_at).total_seconds() if first_at else float("inf")
                    if ceiling_age_s < FADED_ENTRY_CEILING_TIMEOUT_SECONDS:
                        fresh_limit = min(fresh_limit, ceiling) if is_long else max(fresh_limit, ceiling)

                # Legacy pending-limit entries are converted to the same
                # trailing entry used by every current entry path. This keeps
                # the poller from recreating a limit order after cancellation.
                req = TrailingStopOrderRequest(
                    symbol=sym, qty=remaining, side=side,
                    type=AlpacaOrderType.TRAILING_STOP,
                    time_in_force=TimeInForce.DAY,
                    trail_percent=REENTRY_TRAIL_PCT,
                    client_order_id=f"apex-reentry-trail-{sym}-{int(time.time())}",
                )
                # Temporarily drop our own tracking slot so _submit_entry_order's
                # one-pending-entry-per-symbol guard doesn't block the
                # replacement -- this is an intentional re-chase of the SAME
                # slot (the old order was just cancelled above), not a
                # duplicate. Restored with the fresh order id below.
                self._entry_pending.pop(sym, None)
                new_order = self._submit_entry_order(sym, req)
                if new_order is None:
                    raise RuntimeError(f"duplicate entry blocked for {sym}")
                self.order_cache[sym] = new_order.id
                info["order_id"] = str(new_order.id)
                info["qty"] = remaining
                info["chase_count"] = chase_n + 1
                self._entry_pending[sym] = info
                log.warning(
                    f"ENTRY RE-CHASE {sym}: unfilled after prior attempt "
                    f"-- converted to {remaining}-share {REENTRY_TRAIL_PCT:.2f}% trailing entry "
                    f"(attempt {chase_n + 1})"
                )
            except Exception as e:
                log.error(f"_sweep_pending_entries {sym}: re-chase failed: {e}")
                self._entry_pending.pop(sym, None)  # give up tracking rather than loop on a hard failure

    # -- Stale Swing Exit -----------------------------------------------------
    def _get_entry_date(self, symbol: str) -> Optional[datetime.date]:
        """Return the date a position was opened.

        Checks the in-memory entry log first, then falls back to the broker's
        MOST RECENT filled BUY order for the symbol -- covers positions opened
        on a prior day whose entry_log record was lost to a bot restart (the
        startup rebuild in _rebuild_entry_log_from_orders only restores today's
        orders).

        2026-08-14, confirmed live: this used Sort.ASC (oldest first) with no
        date bound, so a symbol bought, sold, and re-bought weeks apart (SNXX:
        2026-07-21 and again 2026-08-14) always returned the ANCIENT fill, not
        the one that actually opened the currently-open lot -- close_stale_
        swing_positions then saw "held 24d" for a position that was 52 minutes
        old and force-closed it. The most recent matching BUY is always the
        right one for a position that's currently open (if it had been closed
        after an earlier buy, the position wouldn't be open now) -- Sort.DESC."""
        info = self._entry_log.get(symbol)
        if info and info.get("date"):
            return info["date"]
        try:
            import pytz
            from alpaca.common.enums import Sort
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            et  = pytz.timezone("America/New_York")
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, symbols=[symbol],
                side=OrderSide.BUY, direction=Sort.DESC, limit=50,
            )
            orders = self.client.get_orders(filter=req) or []
            for order in orders:
                filled_at = getattr(order, "filled_at", None)
                if filled_at is None:
                    continue
                entry_date = filled_at.astimezone(et).date() if hasattr(filled_at, "astimezone") else filled_at
                self._entry_log.setdefault(symbol, {"strategy": "restored", "confidence": 0.0})["date"] = entry_date
                return entry_date
        except Exception as e:
            log.warning(f"_get_entry_date {symbol}: lookup failed: {e}")
        return None

    def _get_entry_datetime(self, symbol: str, is_long: bool = True) -> Optional[datetime.datetime]:
        """Return the UTC fill timestamp a position was opened -- hour-precision
        counterpart to _get_entry_date, needed for the NO_GAIN_EXIT_HOURS check.
        Same broker fallback for positions opened before a bot restart.

        2026-08-14: two bugs fixed here together, same root pattern as
        _get_entry_date (see its docstring for the SNXX case that surfaced
        this) --
          1. Sort.ASC with no date bound returned the OLDEST matching fill
             ever, not the one that opened the currently-open lot. Sort.DESC
             (most recent first) is always correct for a position that's
             still open. Same fix already confirmed necessary for QNT
             2026-08-13, where held_hours came back inflated after a restart.
          2. side was hardcoded to BUY regardless of the position's actual
             direction -- a SHORT is opened via a SELL, so this fallback
             could never find the right order for a short at all (silently
             fell through to a wrong, unrelated BUY or None). is_long now
             selects the correct side; callers must pass their own qty sign."""
        info = self._entry_log.get(symbol)
        if info and info.get("filled_at"):
            return info["filled_at"]
        try:
            from alpaca.common.enums import Sort
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, symbols=[symbol],
                side=(OrderSide.BUY if is_long else OrderSide.SELL), direction=Sort.DESC, limit=50,
            )
            orders = self.client.get_orders(filter=req) or []
            for order in orders:
                filled_at = getattr(order, "filled_at", None)
                if filled_at is None:
                    continue
                self._entry_log.setdefault(symbol, {"strategy": "restored", "confidence": 0.0})["filled_at"] = filled_at
                return filled_at
        except Exception as e:
            log.warning(f"_get_entry_datetime {symbol}: lookup failed: {e}")
        return None

    def _get_recent_close_price(self, symbol: str, is_long: bool = True) -> Optional[float]:
        """Return the most recent filled close price for a just-closed equity lot.

        detect_stopped_out_positions() runs from a polling loop, so its last
        cached mark can be several seconds older than the actual broker fill.
        Use the broker's latest filled close-side order first, and let callers
        fall back to the poll mark if the lookup is unavailable.
        """
        try:
            from alpaca.common.enums import Sort
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            side = OrderSide.SELL if is_long else OrderSide.BUY
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                symbols=[symbol],
                side=side,
                direction=Sort.DESC,
                limit=50,
            )
            orders = self.client.get_orders(filter=req) or []
            for order in orders:
                status = str(getattr(getattr(order, "status", ""), "value", getattr(order, "status", ""))).lower()
                if status and status != "filled":
                    continue
                price = getattr(order, "filled_avg_price", None)
                if price is None:
                    continue
                price = float(price)
                if price > 0:
                    return price
        except Exception as e:
            log.debug(f"_get_recent_close_price {symbol}: lookup failed: {e}")
        return None

    def close_stale_swing_positions(self) -> Optional[dict]:
        """Close swing-strategy positions (i.e. any long NOT opened by a strategy
        in EOD_CLOSE_STRATEGIES, since those already close same-day) that have
        been held SWING_STALE_DAYS+ calendar days without reaching
        SWING_STALE_MIN_GAIN_PCT% unrealized gain. Runs once per calendar day.

        These positions otherwise ride only the GTC trailing stop, which only
        protects against a reversal from the peak -- it never exits a position
        that just goes nowhere. This is the "cut dead capital loose" check."""
        if not SWING_STALE_EXIT_ENABLED:
            return None

        today = datetime.date.today()
        if getattr(self, "_stale_exit_done", None) == today:
            return None

        try:
            positions = self.client.get_all_positions()
        except Exception as e:
            log.error(f"close_stale_swing_positions: fetch failed: {e}")
            return None

        closed_items = []
        failed_items = []

        for pos in positions:
            sym = pos.symbol
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue  # options legs -- managed separately

            qty = int(float(pos.qty))
            if qty <= 0:
                continue  # only long swing positions are subject to this policy

            strategy = self._entry_log.get(sym, {}).get("strategy")
            if strategy in EOD_CLOSE_STRATEGIES:
                continue  # already force-closed same-day by close_eod_positions

            entry_date = self._get_entry_date(sym)
            if entry_date is None:
                log.warning(f"close_stale_swing_positions {sym}: can't determine entry date, skipping")
                continue

            held_days = (today - entry_date).days
            if held_days < SWING_STALE_DAYS:
                continue

            try:
                gain_pct = float(pos.unrealized_plpc) * 100
            except (AttributeError, TypeError, ValueError):
                continue

            if gain_pct >= SWING_STALE_MIN_GAIN_PCT:
                continue  # performing fine -- leave it to the trailing stop

            try:
                # Cancel ALL resting orders first, including GTC -- this method has no
                # regular-hours gate (only "once per calendar day"), so it can run
                # after-hours too, and a resting GTC trailing stop reserves qty and
                # gets a close rejected as a wash trade regardless of time of day
                # (same root cause already fixed for check_afterhours_stops,
                # close_no_gain_positions, the weakest-swap path, and check_tp_targets --
                # confirmed in production via BHC's repeated "insufficient qty
                # available" TP-close rejections on 2026-07-31).
                try:
                    sym_orders = [o for o in (self.client.get_orders() or []) if o.symbol == sym]
                    for _o in sym_orders:
                        try:
                            self.client.cancel_order_by_id(str(_o.id))
                        except Exception:
                            pass
                    if sym_orders:
                        time.sleep(0.4)
                except Exception:
                    pass

                # _submit_closing_order handles the after-hours case (plain
                # MarketOrderRequest gets rejected outside regular hours).
                self._submit_closing_order(sym, abs(qty), OrderSide.SELL, float(pos.current_price))
                _strategy = self._entry_log.get(sym, {}).get("strategy", "unknown")
                try:
                    _pnl = float(pos.unrealized_pl)
                except (AttributeError, TypeError, ValueError):
                    _pnl = 0.0
                self._entry_log.pop(sym, None)

                closed_items.append({
                    "symbol": sym, "qty": abs(qty),
                    "held_days": held_days, "gain_pct": round(gain_pct, 2),
                })
                log.info(
                    f"STALE EXIT {sym} [{_strategy}]: {qty} shares | held {held_days}d | "
                    f"gain {gain_pct:+.1f}% < {SWING_STALE_MIN_GAIN_PCT:.1f}% threshold | P&L ${_pnl:+,.2f}"
                )
            except Exception as e:
                failed_items.append({"symbol": sym, "error": str(e)})
                log.error(f"STALE EXIT failed {sym}: {e}")

        self._stale_exit_done = today

        return {
            "date": today.isoformat(),
            "closed_count": len(closed_items),
            "failed_count": len(failed_items),
            "closed_items": closed_items,
            "failed_items": failed_items,
        }

    def close_no_gain_positions(self) -> Optional[dict]:
        """Close any position (long or short) that hasn't settled into a clear
        positive trend within NO_GAIN_EXIT_HOURS of entry: exit on ANY
        positive gain (stop waiting once it's decided), or on a
        NO_GAIN_EXIT_MAX_LOSS_PCT drop (cut it early rather than riding the
        full trailing stop down). Only a narrow flat/small-loss band survives
        the check and keeps holding. Checked every scan cycle (unlike
        close_stale_swing_positions, which only runs once/day) since the
        N-hour mark can land mid-session, not just at EOD.

        Was long-only ("if qty <= 0: continue") until 2026-08-12, at the
        user's request after finding a live short (ACHR) that had been open
        well past NO_GAIN_EXIT_HOURS with no exit path at all -- this rule
        skipped it by direction, same blind spot as the qty_available sign
        bug in protect_positions() found the same day. pos.unrealized_plpc is
        already sign-correct for shorts (negative when a short is losing, i.e.
        price rose) so the gain_pct band check below needs no changes for
        direction -- only the close side does: SELL for longs, BUY (cover)
        for shorts.
        """
        if not NO_GAIN_EXIT_ENABLED:
            return None

        now_utc = datetime.datetime.now(datetime.timezone.utc)

        try:
            positions = self.client.get_all_positions()
        except Exception as e:
            log.error(f"close_no_gain_positions: fetch failed: {e}")
            return None

        _live_syms = {p.symbol for p in positions}
        for _sym in [s for s in self._no_gain_chase_count if s not in _live_syms]:
            self._no_gain_chase_count.pop(_sym, None)

        closed_items = []
        failed_items = []

        for pos in positions:
            sym = pos.symbol
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue  # options legs -- managed separately

            qty = int(float(pos.qty))
            if qty == 0:
                continue  # no position

            strategy = self._entry_log.get(sym, {}).get("strategy")
            if strategy in EOD_CLOSE_STRATEGIES:
                continue  # already force-closed same-day by close_eod_positions

            entry_dt = self._get_entry_datetime(sym, is_long=qty > 0)
            if entry_dt is None:
                log.warning(f"close_no_gain_positions {sym}: can't determine entry time, skipping")
                continue

            held_hours = (now_utc - entry_dt).total_seconds() / 3600
            if held_hours < NO_GAIN_EXIT_HOURS:
                continue

            try:
                gain_pct = float(pos.unrealized_plpc) * 100
            except (AttributeError, TypeError, ValueError):
                continue

            if NO_GAIN_EXIT_MAX_LOSS_PCT < gain_pct <= NO_GAIN_EXIT_MIN_PCT:
                continue  # still flat / a small loss -- give it more time
            # Otherwise exit: either gain_pct > NO_GAIN_EXIT_MIN_PCT (positive --
            # stop waiting once it's decided) or gain_pct <= NO_GAIN_EXIT_MAX_LOSS_PCT
            # (dropped enough to cut early rather than ride the full trailing stop).

            # A close already in flight for this symbol? Don't blindly cancel-and-resubmit
            # every cycle (that's what spammed FRMI 186x and NG 38x -- the old version
            # re-issued an identical close order every scan cycle with no fill check).
            # Give a fresh close AFTERHOURS_CHASE_STALE_SECONDS to fill; only re-chase,
            # with escalating slip, once it's actually stale.
            try:
                sym_orders = self.client.get_orders() or []
                sym_orders = [o for o in sym_orders if o.symbol == sym]
            except Exception as e:
                log.warning(f"close_no_gain_positions {sym}: order fetch failed, will retry next cycle: {e}")
                continue

            pending = next((o for o in sym_orders if getattr(o, "time_in_force", None) != TimeInForce.GTC), None)
            if pending is not None:
                submitted_at = getattr(pending, "submitted_at", None) or getattr(pending, "created_at", None)
                age_s = (now_utc - submitted_at).total_seconds() if submitted_at else 0.0
                if age_s < AFTERHOURS_CHASE_STALE_SECONDS:
                    continue  # close already in flight -- give it time to fill
                try:
                    self.client.cancel_order_by_id(str(pending.id))
                    time.sleep(0.4)
                except Exception as e:
                    log.warning(f"close_no_gain_positions {sym}: stale-close cancel failed, will retry next cycle: {e}")
                    continue

            # The resting GTC trailing stop reserves this position's qty and can cause
            # the close to be rejected as a wash trade -- cancel it first, same fix as
            # check_afterhours_stops. Re-armed below as a fallback if the close fails.
            gtc_order = next((o for o in sym_orders if getattr(o, "time_in_force", None) == TimeInForce.GTC), None)
            if gtc_order:
                try:
                    self.client.cancel_order_by_id(str(gtc_order.id))
                    time.sleep(0.4)
                except Exception as e:
                    log.warning(f"close_no_gain_positions {sym}: GTC cancel failed, will retry next cycle: {e}")
                    continue

            close_side = OrderSide.SELL if qty > 0 else OrderSide.BUY  # SELL to close a long, BUY to cover a short
            try:
                chase_n  = self._no_gain_chase_count.get(sym, 0)
                slip_pct = min(0.5 * (chase_n + 1), 3.0)
                self._submit_closing_order(sym, abs(qty), close_side, float(pos.current_price), slip_pct=slip_pct)
                self._no_gain_chase_count[sym] = chase_n + 1
                _strategy = self._entry_log.get(sym, {}).get("strategy", "unknown")
                try:
                    _pnl = float(pos.unrealized_pl)
                except (AttributeError, TypeError, ValueError):
                    _pnl = 0.0
                self._entry_log.pop(sym, None)

                closed_items.append({
                    "symbol": sym, "qty": abs(qty),
                    "held_hours": round(held_hours, 1), "gain_pct": round(gain_pct, 2),
                })
                _why = "positive gain" if gain_pct > NO_GAIN_EXIT_MIN_PCT else f"<= {NO_GAIN_EXIT_MAX_LOSS_PCT:.1f}% loss"
                log.info(
                    f"NO-GAIN EXIT {sym} [{_strategy}]: {qty} shares | held {held_hours:.1f}h | "
                    f"gain {gain_pct:+.1f}% ({_why}) | P&L ${_pnl:+,.2f} "
                    f"@ {slip_pct:.1f}% slip (attempt {chase_n + 1})"
                )
            except Exception as e:
                failed_items.append({"symbol": sym, "error": str(e)})
                log.error(f"NO-GAIN EXIT failed {sym}: {e}")
                if gtc_order:
                    # GTC is gone and the replacement didn't go through -- re-arm one now
                    # rather than leave the position unprotected until the next cycle.
                    try:
                        trail_pct = _atr_trail_pct_for(sym, float(pos.current_price), self._entry_log)[0]
                        self.client.submit_order(TrailingStopOrderRequest(
                            symbol=sym, qty=abs(qty), side=close_side,
                            type=AlpacaOrderType.TRAILING_STOP,
                            time_in_force=TimeInForce.GTC, trail_percent=trail_pct,
                        ))
                        log.warning(f"NO-GAIN EXIT {sym}: re-armed GTC trailing stop after failed close")
                    except Exception as rearm_err:
                        log.error(f"NO-GAIN EXIT {sym}: close failed AND GTC re-arm failed -- position may be UNPROTECTED: {rearm_err}")

        return {
            "closed_count": len(closed_items),
            "failed_count": len(failed_items),
            "closed_items": closed_items,
            "failed_items": failed_items,
        }

    # -- Price Drift Stop (10-min poll, 30-min lookback, same-day entries) --
    @staticmethod
    def _drift_stop_reason(
        current: float, entry: Optional[float], reference: Optional[float], is_long: bool, stop_pct: float,
    ) -> Optional[str]:
        """Pure decision logic for check_price_drift_stop: return a reason
        string if the adverse move versus EITHER `entry` (the position's own
        entry price) OR `reference` (the price PRICE_DRIFT_LOOKBACK_MIN ago)
        exceeds stop_pct, else None. Split out for unit-testability without a
        broker connection. Either reference being None/<=0 (missing data, or
        not enough rolling history yet) just drops that leg of the check --
        never a false trigger, and the other leg still applies independently.

        2026-08-14, user correction: entry-price leg restored after being
        dropped 2026-08-13 -- confirmed live TE dropped 2.69% off its OWN
        entry price with the 30-min-ago-only version never even looking at
        entry, so a slow bleed that never shows a full move within any
        single 10-min window went completely uncaught. Both legs checked
        again, OR'd together."""
        def _adverse_pct(ref: Optional[float]) -> Optional[float]:
            if ref is None or ref <= 0:
                return None
            return ((ref - current) / ref * 100) if is_long else ((current - ref) / ref * 100)

        drift_entry = _adverse_pct(entry)
        if drift_entry is not None and drift_entry > stop_pct:
            return f"entry ${entry:.2f}->${current:.2f} ({drift_entry:+.1f}%)"
        drift_ref = _adverse_pct(reference)
        if drift_ref is not None and drift_ref > stop_pct:
            return f"{PRICE_DRIFT_LOOKBACK_MIN}min ${reference:.2f}->${current:.2f} ({drift_ref:+.1f}%)"
        return None

    def _backfill_drift_reference(self, symbol: str) -> Optional[float]:
        """When _price_drift_history has no rolling history yet for symbol
        (a fresh position, or a restart wiped it), reconstruct an
        approximate PRICE_DRIFT_LOOKBACK_MIN-minutes-ago reference from real
        1-min bar data instead of leaving the position with zero drift
        protection until PRICE_DRIFT_LOOKBACK_MIN more minutes of in-memory
        history rebuilds on its own. Same "row N back ~= N minutes ago"
        approximation _check_momentum_freshness already uses. Returns None
        if bars are unavailable -- missing data never forces a decision,
        same fail-safe as everywhere else in this file."""
        try:
            bars = get_bars(symbol, period="1d", interval="1m")
            if bars.empty or "close" not in bars.columns or len(bars) <= PRICE_DRIFT_LOOKBACK_MIN:
                return None
            return float(bars["close"].iloc[-1 - PRICE_DRIFT_LOOKBACK_MIN])
        except Exception as e:
            log.warning(f"_backfill_drift_reference {symbol}: failed: {e}")
            return None

    def check_price_drift_stop(self) -> None:
        """Every PRICE_DRIFT_CHECK_INTERVAL_MIN (10 min), exit any same-day
        position that's moved against it by more than PRICE_DRIFT_STOP_PCT
        versus EITHER its own entry price OR its price PRICE_DRIFT_LOOKBACK_MIN
        (30 min) ago (2026-08-14: restored the entry-price leg -- a
        30-min-ago-only check misses a slow bleed that never shows a full
        move within any single 10-min window; see _drift_stop_reason).
        Tighter and faster than the normal trailing stop -- see the
        PRICE_DRIFT_STOP block in config.py for why (2026-08-13, confirmed
        live: DFSC/HLIT/EROC/JACK all bought right at the open, all faded
        4-8% before the wider trailing stop caught them; polling every 10
        min instead of 30 gives a fast 10-15 min collapse a real chance of
        being caught by the very next check). Longs: drop > PRICE_DRIFT_STOP_PCT%.
        Shorts: rise > PRICE_DRIFT_STOP_PCT% (mirrored). Scoped to same-day
        entries only (self._entry_log date), not by strategy -- survives the strategy-name
        loss a restart causes.

        No re-entry cooldown (2026-08-24, user request) -- this drift stop,
        the trailing stop, and check_ema9_exit are the whole protection
        stack; nothing here throttles how soon a symbol re-enters."""
        if not PRICE_DRIFT_STOP_ENABLED:
            return

        try:
            positions = self.client.get_all_positions()
        except Exception as e:
            log.warning(f"check_price_drift_stop: fetch failed: {e}")
            return

        today = datetime.date.today()
        live_syms = set()
        lookback_ticks = max(1, PRICE_DRIFT_LOOKBACK_MIN // PRICE_DRIFT_CHECK_INTERVAL_MIN)

        for pos in positions:
            sym = pos.symbol
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue  # options legs -- managed separately
            qty = int(float(pos.qty))
            if qty == 0:
                continue

            entry_info = self._entry_log.get(sym)
            if not entry_info or entry_info.get("date") != today:
                continue  # not a same-day entry -- out of scope, leave on its normal trailing stop

            live_syms.add(sym)
            try:
                current = float(pos.current_price)
                entry   = float(pos.avg_entry_price)
            except (TypeError, ValueError):
                continue
            if current <= 0:
                continue

            is_long  = qty > 0
            history  = self._price_drift_history.setdefault(sym, deque(maxlen=lookback_ticks))
            # deque[0] is the oldest sample once full -- exactly lookback_ticks
            # checks back, i.e. ~PRICE_DRIFT_LOOKBACK_MIN minutes ago at this
            # check's own cadence. Not enough history yet (a fresh position, OR
            # a restart wiped it -- confirmed live 2026-08-14: TE entered
            # 09:33, the bot restarted twice before 30 clean minutes had
            # elapsed, so the in-memory history never rebuilt and TE sat with
            # zero drift protection past an hour while down -2.8%) -- backfill
            # an approximate reference from real 1-min bar data instead of
            # leaving the position unwatched until history rebuilds on its own.
            #
            # 2026-08-18, user request: that backfill is only valid once the
            # POSITION ITSELF is at least PRICE_DRIFT_LOOKBACK_MIN old -- for
            # anything younger, "PRICE_DRIFT_LOOKBACK_MIN minutes ago" lands
            # BEFORE entry, in bars that have nothing to do with this trade
            # (e.g. a LiquiditySweep long entered right after a sweep-low: the
            # 30 min before entry routinely include a HIGH above the entry
            # price, so a since-entry-flat position could "drift" >1% against
            # that stale pre-entry high and get stopped for a move that never
            # happened after we were even in the trade). Force reference=None
            # (drops that leg, per _drift_stop_reason) until the position has
            # actually been held that long -- the entry-price leg alone still
            # covers it in the meantime.
            entry_dt = self._get_entry_datetime(sym, is_long)
            age_min  = (
                (datetime.datetime.now(datetime.timezone.utc) - entry_dt).total_seconds() / 60
                if entry_dt else None
            )
            if age_min is not None and age_min < PRICE_DRIFT_LOOKBACK_MIN:
                reference = None
            else:
                reference = history[0] if len(history) == lookback_ticks else self._backfill_drift_reference(sym)
            reason = self._drift_stop_reason(current, entry, reference, is_long, PRICE_DRIFT_STOP_PCT)

            # Record this check's price regardless of outcome -- the deque
            # naturally evicts the oldest sample once full, keeping the
            # lookback window rolling forward.
            history.append(current)

            if reason is None:
                continue

            try:
                sym_orders = [o for o in (self.client.get_orders() or []) if o.symbol == sym]
                for _o in sym_orders:
                    try:
                        self.client.cancel_order_by_id(str(_o.id))
                    except Exception:
                        pass
                if sym_orders:
                    time.sleep(0.4)
            except Exception as e:
                log.warning(f"check_price_drift_stop {sym}: order fetch/cancel failed, will retry next cycle: {e}")
                continue

            side = OrderSide.SELL if is_long else OrderSide.BUY
            try:
                self._submit_closing_order(sym, abs(qty), side, current)
                log.warning(f"PRICE DRIFT STOP {sym}: {abs(qty)} shares | {reason}")
                self._maybe_rearm_reentry(
                    sym, is_long, abs(qty), "PRICE DRIFT STOP",
                    was_loss=(current - entry) * qty < 0,
                )
            except Exception as e:
                log.error(f"PRICE DRIFT STOP {sym}: close failed: {e}")
                # The resting GTC was just cancelled above -- re-arm a fallback
                # so the position isn't left fully unprotected.
                try:
                    trail_pct, _ = _atr_trail_pct_for(sym, current, self._entry_log)
                    self.client.submit_order(TrailingStopOrderRequest(
                        symbol=sym, qty=abs(qty), side=side,
                        type=AlpacaOrderType.TRAILING_STOP, time_in_force=TimeInForce.GTC,
                        trail_percent=trail_pct,
                    ))
                    log.warning(f"check_price_drift_stop {sym}: re-armed GTC trailing stop after failed close")
                except Exception as rearm_err:
                    log.error(f"check_price_drift_stop {sym}: close failed AND GTC re-arm failed -- position may be UNPROTECTED: {rearm_err}")

        # Drop history for symbols no longer held or no longer in scope
        for sym in [s for s in self._price_drift_history if s not in live_syms]:
            self._price_drift_history.pop(sym, None)

    @staticmethod
    def _mfe_giveback_reason(peak: Optional[float], entry: Optional[float],
                             current: float, is_long: bool) -> Optional[str]:
        """Pure decision function for check_mfe_giveback_exit(): once a
        position's unrealized gain has ever reached MFE_ARM_PROFIT_PCT
        (measured against `peak`, the best price seen since entry), exit the
        moment the CURRENT gain falls below max(peak_gain *
        MFE_GIVEBACK_FRACTION, MFE_BREAKEVEN_FLOOR_PCT). Returns a reason
        string when the stop fires, else None.

        2026-09-03, from the morning post-mortem: 41 round trips peaked at
        +$90.56 unrealized combined and realized +$1.22 -- a 1.3% capture
        rate. The broker-side GTC trailing stop trails from its own HWM but
        at 1.5-4.0% width, so a green-then-fade round trip inside a few
        minutes never touches it; and no software check tracked what a trade
        had ALREADY shown. This closes that gap: arming at +0.5% and giving
        back at most 40% of the peak (floor: entry +0.1%) would have locked
        most of SMMT/HOOD/CRCL/CONL's morning peaks and kept the ASST-class
        trades (peak +0.5%, exited -1.96%) from ever going red.

        Fail-safe semantics, matching the rest of this file: peak/entry
        missing or <=0 drops that leg of the decision (never a trigger), and
        a peak that never armed returns None. Shorts are mirrored (peak is
        the lowest price seen; gain is entry-minus-current)."""
        if peak is None or entry is None or entry <= 0 or peak <= 0:
            return None
        if is_long:
            peak_gain_pct = (peak - entry) / entry * 100.0
            cur_gain_pct = (current - entry) / entry * 100.0
        else:
            peak_gain_pct = (entry - peak) / entry * 100.0
            cur_gain_pct = (entry - current) / entry * 100.0
        if peak_gain_pct < MFE_ARM_PROFIT_PCT:
            return None  # never armed -- ordinary trailing stop still owns this trade
        floor_pct = max(peak_gain_pct * MFE_GIVEBACK_FRACTION, MFE_BREAKEVEN_FLOOR_PCT)
        if cur_gain_pct > floor_pct:
            return None
        return (
            f"peaked at {peak_gain_pct:+.2f}% (${peak:.2f}) but now {cur_gain_pct:+.2f}% "
            f"(${current:.2f}) -- below the {floor_pct:.2f}% give-back floor "
            f"(arm {MFE_ARM_PROFIT_PCT:.2f}%, keep {MFE_GIVEBACK_FRACTION:.0%} of peak)"
        )

    def check_mfe_giveback_exit(self) -> None:
        """Exit any same-day position that armed the MFE give-back watch and
        has since surrendered too much of its best gain (see
        _mfe_giveback_reason for the decision rule and the 2026-09-03
        post-mortem that motivated it). Runs on the SoftwareStopPoller
        thread, same lifecycle as check_price_drift_stop: cancel any resting
        orders on the symbol, submit a marketable-limit close, re-arm the
        GTC trailing stop if the close itself fails so the position is never
        left unprotected, and offer the closed leg to the re-entry machinery.

        The per-symbol peak is the best price seen across this process's
        polls (long: highest; short: lowest), seeded from the first sighting
        and dropped once the position is gone. Scoped to same-day entries
        only -- overnight/swing positions keep their normal trailing stop."""
        if not MFE_GIVEBACK_ENABLED:
            return

        try:
            positions = self.client.get_all_positions()
        except Exception as e:
            log.warning(f"check_mfe_giveback_exit: fetch failed: {e}")
            return

        today = datetime.date.today()
        live_syms = set()

        for pos in positions:
            sym = pos.symbol
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue  # options legs -- managed separately
            try:
                qty = int(float(pos.qty))
            except (TypeError, ValueError):
                continue
            if qty == 0:
                continue

            entry_info = self._entry_log.get(sym)
            if not entry_info or entry_info.get("date") != today:
                continue  # not a same-day entry -- out of scope, leave on its normal trailing stop

            live_syms.add(sym)
            try:
                current = float(pos.current_price)
                entry = float(pos.avg_entry_price)
            except (TypeError, ValueError):
                continue
            if current <= 0:
                continue

            is_long = qty > 0
            prev_peak = self._mfe_peaks.get(sym)
            new_peak = current if prev_peak is None else (
                max(prev_peak, current) if is_long else min(prev_peak, current)
            )
            self._mfe_peaks[sym] = new_peak

            reason = self._mfe_giveback_reason(new_peak, entry, current, is_long)
            if reason is None:
                continue

            try:
                sym_orders = [o for o in (self.client.get_orders() or []) if o.symbol == sym]
                for _o in sym_orders:
                    try:
                        self.client.cancel_order_by_id(str(_o.id))
                    except Exception:
                        pass
                if sym_orders:
                    time.sleep(0.4)
            except Exception as e:
                log.warning(f"check_mfe_giveback_exit {sym}: order fetch/cancel failed, will retry next cycle: {e}")
                continue

            result = self._request_reconciled_close(sym, "mfe", current)
            if result.state == "submitted":
                log.warning(f"MFE GIVEBACK STOP {sym}: {result.requested_qty} shares | {reason}")
                self._maybe_rearm_reentry(
                    sym, is_long, result.requested_qty, "MFE GIVEBACK STOP",
                    was_loss=(current - entry) * qty < 0,
                )
            elif result.state in ("cancel_pending", "already_pending"):
                log.debug(f"check_mfe_giveback_exit {sym}: close {result.state} -- re-checking next tick")
            elif result.state != "flat":
                log.error(f"MFE GIVEBACK STOP {sym}: close not submitted ({result.state}): {result.detail}")

        # Drop peaks for symbols no longer held or no longer in scope
        for sym in [s for s in self._mfe_peaks if s not in live_syms]:
            self._mfe_peaks.pop(sym, None)

    @staticmethod
    def _update_ema9_peak(peak_map: Dict[str, float], sym: str, ema9_now: float, is_long: bool) -> float:
        """Track the running peak of EMA9 since entry for a long (trough
        for a short) -- the reference _ema9_trail_exit_reason trails
        against, same role _trail_pct_for's peak-tracking plays for the
        price-based TRAIL_STOP_PCT stop, just on EMA9 instead of raw
        price. First observation for a symbol becomes the initial peak
        (no guessing needed, same fail-open-to-first-reading philosophy
        used everywhere else in this file -- a restart or a position with
        no captured peak yet just starts fresh from here). Returns the
        (possibly updated) peak so the caller doesn't need a second
        lookup."""
        prev = peak_map.get(sym)
        new_peak = ema9_now if prev is None else (max(prev, ema9_now) if is_long else min(prev, ema9_now))
        peak_map[sym] = new_peak
        return new_peak

    @staticmethod
    def _ema9_trail_exit_reason(ema9_now: float, peak: float, is_long: bool) -> Optional[str]:
        """Pure decision function for check_ema9_exit(): exit once EMA9 has
        pulled back EMA9_TRAIL_PCT% from its own peak (long) / trough
        (short) since entry -- a trailing stop ON EMA9, not a delta
        snapshot against the previous minute.

        2026-08-25, user request chain: "add exit condition ema 7 and ema 3
        both negative for exit" -> EMA7 alone -> a delta-vs-0.3%-of-price
        threshold -> "should only trigger is the stock trending down"
        (price-vs-EMA9 added) -> "try options 2" (2-bar persistence) ->
        switched from EMA7 to EMA9 -> "get a ema 9 trail stop of 0.3%
        instead of just delta with 0.3% of price". Every version before
        this one was a flat, memoryless snapshot -- no matter how it was
        thresholded or persisted, it judged a trade only against its last
        1-2 bars, never against how far it had already run. That's the
        exact gap the JEM/SDOT/RZLV backtests kept surfacing: a real
        pullback inside a real uptrend reads identically to a real top,
        so a memoryless check cuts both the same way. Tracking EMA9's own
        peak fixes that directly -- a trade that's run up 10% gets 0.3%
        of real room to breathe measured from where it's BEEN, not from
        one bar to the next."""
        threshold = EMA9_TRAIL_PCT / 100.0 * peak
        against = (ema9_now <= peak - threshold) if is_long else (ema9_now >= peak + threshold)
        if not against:
            return None
        return (
            f"EMA9 ${ema9_now:.2f} pulled back {EMA9_TRAIL_PCT:.1f}% from its "
            f"{'peak' if is_long else 'trough'} ${peak:.2f} since entry -- trailing EMA9 stop hit"
        )

    @staticmethod
    def _ema7_ema15_reversal_reason(ema7_now: float, ema15_now: float, is_long: bool) -> Optional[str]:
        """Exit when EMA7 moves to the wrong side of EMA15."""
        reversed_trend = ema7_now < ema15_now if is_long else ema7_now > ema15_now
        if not reversed_trend:
            return None
        return (
            f"EMA7 ${ema7_now:.2f} {'below' if is_long else 'above'} "
            f"EMA15 ${ema15_now:.2f} -- EMA trend reversed against the "
            f"{'long' if is_long else 'short'} position"
        )

    @staticmethod
    def _check_30m_reentry_performance(symbol: str, is_long: bool) -> Tuple[bool, str]:
        """For loss re-entry, require first-30 direction and recent 30m momentum."""
        try:
            import pytz as _pytz

            eastern = _pytz.timezone("America/New_York")
            now_et = datetime.datetime.now(eastern)
            session_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            first_30_done = session_open + datetime.timedelta(minutes=30)
            if now_et < first_30_done:
                # Before 10:00 the first-30 window isn't complete, so the gate
                # has nothing to evaluate. Skip it (pass) rather than block --
                # the morning-loss block (LOSS_BLOCK_MORNING_END_ET) is the
                # cooldown that applies this early, not this performance gate.
                return True, f"{symbol}: first 30 minutes not complete; skipping loss re-entry 30m gate until 10:00 ET"

            bars = _closed_1m_bars(_entry_gate_bars(symbol, force_fresh=True))
            high_col = "high" if "high" in bars.columns else "close"
            needed = {"open", "close", high_col}
            if bars.empty or not needed.issubset(bars.columns):
                return False, f"{symbol}: today's 1m bars unavailable for loss re-entry"

            regular = bars
            if "time" in bars.columns:
                def _to_et(value):
                    ts = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
                    if isinstance(ts, str):
                        ts = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if getattr(ts, "tzinfo", None) is None:
                        ts = eastern.localize(ts)
                    return ts.astimezone(eastern)

                et_times = bars["time"].apply(_to_et)
                regular = bars[
                    (et_times >= session_open)
                    & (et_times < now_et)
                    & et_times.apply(lambda ts: ts.date() == now_et.date())
                ]

            if len(regular) < 31:
                return False, f"{symbol}: fewer than 31 regular-session 1m bars for 30m re-entry check"

            day_open = float(regular["open"].iloc[0])
            first_30_close = float(regular["close"].iloc[29])
            current = float(regular["close"].iloc[-1])
            trailing_ref = float(regular["close"].iloc[-31])
            first_30_move = first_30_close - day_open
            open_to_current = current - day_open
            trailing30_pct = ((current - trailing_ref) / trailing_ref * 100.0) if trailing_ref else 0.0
            recent_high = float(regular[high_col].iloc[-30:].max())
            previous_high = float(regular[high_col].iloc[-60:-30].max())

            if is_long:
                aligned = (
                    first_30_move > 0
                    and open_to_current > 0
                    and trailing30_pct > EMA_ENTRY_MIN_TRAILING_30M_RETURN_PCT
                    and recent_high > previous_high
                )
            else:
                aligned = (
                    first_30_move < 0
                    and open_to_current < 0
                    and trailing30_pct < -EMA_ENTRY_MIN_TRAILING_30M_RETURN_PCT
                    and recent_high < previous_high
                )
            if not aligned:
                return False, (
                    f"{symbol}: first30 {first_30_move:+.2f}, open-to-current {open_to_current:+.2f}, "
                    f"trailing30 {trailing30_pct:+.2f}%, recent30 high ${recent_high:.2f} vs prior30 high ${previous_high:.2f} "
                    f"not aligned for {'long' if is_long else 'short'} loss re-entry"
                )
            return True, (
                f"30m gate passed for {'long' if is_long else 'short'} loss re-entry -- "
                f"first30 {first_30_move:+.2f}, open-to-current {open_to_current:+.2f}, "
                f"trailing30 {trailing30_pct:+.2f}%, recent30 high ${recent_high:.2f} > prior30 high ${previous_high:.2f}"
            )
        except Exception as exc:
            return False, f"{symbol}: 30m performance check failed: {exc}"

    def _maybe_rearm_reentry(
        self, sym: str, is_long: bool, qty: int, tag: str, was_loss: bool = False,
    ) -> None:
        """After a STOP-LOSS-type close, re-check the same entry gate a fresh
        signal would need and re-arm a trailing re-entry if it currently
        passes. Extracted 2026-08-26 (was inline in check_ema9_exit only,
        2026-08-25 origin -- user request "every exit will look for rentery
        as soon as the exit but with conditions to entry met") so every
        stop-loss-type exit path gets the same treatment, not just the rare
        EMA9 trail. Root cause this fixes: user request 2026-08-26 ("I have
        put in 1% on the hope the new orders will be placed immediately
        after the exit with conditions check every minute, but it doesn't
        seem to work") -- confirmed live, of 73 trades on 2026-08-26 only 9
        exited via check_ema9_exit (the only path with this logic); the
        other 64 (broker-side GTC trailing-stop fills caught by
        detect_stopped_out_positions, and PDT-forced software stops via
        check_software_stops) had zero re-entry path regardless of whether
        conditions still held afterward.

        2026-08-26, user request ("irrespective of exit type reentry should
        happen for the top 30 list during the every minute check after
        exit... this should catch the missed gains after the dips"):
        widened from "stop-loss-type closes only" to every close EXCEPT
        three categories where re-entering would undo a different, real
        protection built earlier the same day/session -- user explicitly
        confirmed keeping these three excluded when asked:
          - close_guardrail_fail_positions: closes because the stock just
            failed a structural safety check (dollar_vol/avg_volume/
            low_float/low_mcap) -- the RPGL fix from earlier 2026-08-26.
            Re-entering the same symbol immediately would undo it.
          - enforce_position_concentration/enforce_correlation_concentration/
            enforce_portfolio_leverage, and the weakest-position swap in
            _attempt_swap/_execute_entry: portfolio-level capital
            reallocation, not a verdict on the symbol -- re-entering fights
            the very reason for the trim (risks a trim-reenter-trim thrash
            loop).
          - emergency_close_all: the market-wide kill switch (VIX spike/SPY
            crash). Re-entering right after defeats its entire purpose.
        Immediate follow-up ("exclude eod don't reenter after end of day
        exit"): close_eod_positions added back as a fourth exclusion --
        briefly left unmarked on the reasoning that ENTRY_WINDOW_END_ET
        already blocks re-arming this late anyway, but the user wants it
        excluded directly rather than relying on that time-window side
        effect.
        Everything else -- close_stale_swing_positions, close_no_gain_positions,
        check_swing_drift_stop, check_tp_targets, _close_long_position/
        _close_short_position -- re-arms the same as any stop-loss close,
        via the generic catch in detect_stopped_out_positions() (below):
        NOT marking self._no_rearm for these is what lets that generic path
        pick them up.

        Registers in self.order_cache so check_pending_entries_ema's
        per-minute recheck covers the new order like any other entry.
        Deliberately conditional (unlike the old, removed check_ema15_exit,
        which re-armed unconditionally on every exit -- confirmed live:
        that blind re-arm whipsawed RZLV three times in 28 minutes on
        2026-08-25). Best-effort: any failure here must never be treated as
        the close itself failing -- caller has already succeeded by the
        time this runs.

        Marks self._no_rearm for *sym* unconditionally, first thing --
        detect_stopped_out_positions() will notice this same close later
        (the position disappearing between its polls) and must not
        double-process it (a second _maybe_rearm_reentry call for the same
        exit would submit a second, orphaned trailing-buy re-entry order).
        """
        self._no_rearm.add(sym)
        if was_loss:
            self._record_symbol_loss(sym, tag)
            getattr(self, "_loss_reentry_required", set()).add(sym)
        else:
            getattr(self, "_loss_reentry_required", set()).discard(sym)
        try:
            import pytz as _pytz
            _now_et = datetime.datetime.now(_pytz.timezone("America/New_York"))
            if in_lunch_break(_now_et):
                # 2026-09-01, two-window schedule: no re-arming during the
                # midday break -- the symbol stays _no_rearm'd (already set
                # above) so detect_stopped_out_positions won't double-process
                # this close either; re-entry only happens after the 14:45
                # afternoon segment opens, via a fresh scan/re-arm.
                log.info(f"{tag} {sym}: midday break ({ENTRY_WINDOW_BREAK_START_ET}-{ENTRY_WINDOW_BREAK_END_ET} ET) -- not re-arming a re-entry until the afternoon segment opens")
                return
            if _now_et.strftime("%H:%M") >= ENTRY_WINDOW_END_ET:
                log.info(f"{tag} {sym}: past entry window ({ENTRY_WINDOW_END_ET} ET) -- not re-arming a re-entry")
                return
            # 2026-08-26, user request ("reentry should happen for the top 30
            # list" / "trade only top 30 stocks, but update the top 30 based
            # on the new scans included"): check against the ACTUAL scan
            # universe (Alpaca-movers + TI, capped/deduped at
            # TI_PRIMARY_SCAN_BATCH_LIMIT=30 -- see get_scan_targets(),
            # engine/equity/scan.py), not the raw, uncapped get_ti_primary()
            # (routinely 90-100+ tickers) -- a symbol could be "somewhere in
            # TI" without being one of the 30 the equity scan is actually
            # trading right now.
            if sym not in _get_scan_targets():
                log.info(f"{tag} {sym}: no longer in the top-30 scan universe -- not re-arming a re-entry")
                return
            if was_loss:
                performance_ok, performance_reason = EnhancedExecutor._check_30m_reentry_performance(sym, is_long)
                if not performance_ok:
                    log.info(f"{tag} {sym}: {performance_reason} -- not re-arming after loss/reversal")
                    return
                if performance_reason:
                    log.info(f"{tag} {sym}: {performance_reason}")
            sig_stub = SimpleNamespace(symbol=sym)
            gate_ok, gate_reason = _check_ema_trend_alignment(sig_stub, is_long, force_fresh=True)
            if not gate_ok:
                # 2026-08-26, user request ("will the 1 minute check will
                # reenter the exited runners" -- confirmed this needed
                # fixing): this used to just give up here. The gate not
                # being aligned at the EXACT instant of exit doesn't mean it
                # won't align a few minutes later -- backtested against
                # today's actual exits: 58 of 73 would have re-aligned
                # within 30 min, but 36 of those 58 only aligned AFTER the
                # instant of exit, so the old one-shot check would have
                # missed them entirely. Queue into the same
                # _ema_blocked_entries structure a fresh blocked signal
                # uses (see _validate_trade) so check_blocked_entries_ema's
                # existing per-minute retry loop keeps checking until it
                # aligns, the entry window closes, or the symbol drops out
                # of the top-30 -- genuinely "every minute check after
                # exit," not just once.
                log.info(f"{tag} {sym}: entry conditions not currently met ({gate_reason}) -- queuing for per-minute recheck")
                if sym not in self._ema_blocked_entries:
                    try:
                        _bars = get_bars(sym, period="1d", interval="1m")
                        _px = float(_bars["close"].iloc[-1]) if not _bars.empty else 0.0
                    except Exception:
                        _px = 0.0
                    if _px > 0:
                        _strategy = self._entry_log.get(sym, {}).get("strategy", "reentry")
                        _reentry_signal = Signal(
                            symbol=sym, action="buy" if is_long else "short", price=_px,
                            confidence=0.70, reason=f"re-entry after {tag}", strategy=_strategy,
                        )
                        self._ema_blocked_entries[sym] = {
                            "signal": _reentry_signal,
                            "order_type": OrderType.LONG if is_long else OrderType.SHORT,
                            "queued_at": datetime.datetime.now(datetime.timezone.utc),
                        }
                return
            _bars = get_bars(sym, period="1d", interval="1m")
            _px = float(_bars["close"].iloc[-1]) if not _bars.empty else 0.0
            if _px <= 0:
                log.info(f"{tag} {sym}: no current price data -- not re-arming")
                return
            _strategy = self._entry_log.get(sym, {}).get("strategy", "reentry")
            _reentry_signal = Signal(
                symbol=sym,
                action="buy" if is_long else "short",
                price=_px,
                confidence=0.70,
                reason=f"re-entry after {tag}",
                strategy=_strategy,
            )
            _acct = self._get_account(force_refresh=True)
            if self._execute_entry(
                _reentry_signal,
                _acct,
                OrderType.LONG if is_long else OrderType.SHORT,
                bypass_pdt=True,
            ):
                log.info(f"{tag} {sym}: entry conditions still met -- re-entry passed all hard checks")
        except Exception as e:
            log.warning(f"{tag} {sym}: re-entry watch order failed (exit itself still succeeded): {e}")

    def check_ema9_exit(self) -> None:
        """Every STAGNANT_STOP_CHECK_INTERVAL_MIN (1 min), exit any same-day
        position where EMA9 has pulled back EMA9_TRAIL_PCT% from its own
        peak (long) / trough (short) since entry -- a trailing stop on
        EMA9 itself. See _ema9_trail_exit_reason / _update_ema9_peak for
        the full "why," including the request chain that got here (delta
        snapshot -> thresholded -> price-vs-EMA9 -> 2-bar persistence ->
        finally a proper trailing stop, 2026-08-25). Only entry
        (_check_ema_trend_alignment) still looks at EMA7's delta and the
        EMA7-vs-EMA15 crossover -- this exit is EMA9-only. This is now
        the ONLY per-minute exit check -- check_ema15_exit (its slower,
        entry-anchored EMA15 rules) was removed 2026-08-25, user request:
        "remove the ema15 delta check, only keep the ema3 and ema7
        positive slope."

        2026-08-25, user request: "every exit will look for rentery as
        soon as the exit but with conditions to entry met" -- on a
        successful close, immediately re-checks the same
        _check_ema_trend_alignment gate a fresh signal would need (plus
        still-in-TI-universe and the entry-window cutoff); only re-arms a
        trailing re-entry if it currently passes. Deliberately NOT the
        same as check_ema15_exit's old re-arm, which fired unconditionally
        on every exit with no entry-condition check at all -- confirmed
        live: that blind re-arm whipsawed RZLV three times in 28 minutes
        on 2026-08-25 (exit -> re-arm -> re-enter -> exit again, repeat).
        Also registers the re-entry order in self.order_cache, which the
        old re-arm never did either -- that meant check_pending_entries_ema
        could never see or cancel a stale re-armed order once conditions
        turned against it; this one is covered by that same per-minute
        recheck like any other entry.

        Fail-open on missing/insufficient bar data. Scoped to same-day
        entries only.

        2026-08-27, user request ("the 1 min check algo has to work in
        parallel if needed to avoid overload issue"): the fresh bar fetch
        (bypass_cache=True) is a network call per open same-day position --
        I/O-bound, previously one position at a time. Fetched/decided in
        parallel via a small pool (POLLER_CHECK_WORKERS): each position's
        peak tracking (_update_ema9_peak) only ever touches ITS OWN key in
        self._ema9_trail_peak, so concurrent updates across different
        symbols don't collide. Actually closing a position (cancel resting
        order, submit the close, re-arm a re-entry) stays strictly
        sequential in a second pass -- unrelated to any capital/PDT
        concern here (each close only affects its own symbol), but there's
        no benefit to parallelizing broker mutations that don't share a
        bottleneck, and it keeps this exit path exactly as easy to reason
        about as it was before."""
        if not STAGNANT_STOP_ENABLED:
            return

        try:
            positions = self.client.get_all_positions()
        except Exception as e:
            log.warning(f"check_ema9_exit: fetch failed: {e}")
            return

        today = datetime.date.today()

        eligible = []  # (sym, qty, current, is_long)
        for pos in positions:
            sym = pos.symbol
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue  # options legs -- managed separately
            qty = int(float(pos.qty))
            if qty == 0:
                continue

            entry_info = self._entry_log.get(sym)
            if not entry_info or entry_info.get("date") != today:
                continue  # not a same-day entry -- out of scope

            try:
                current = float(pos.current_price)
            except (TypeError, ValueError):
                continue
            if current <= 0:
                continue
            try:
                entry = float(getattr(pos, "avg_entry_price", current))
            except (TypeError, ValueError):
                continue
            eligible.append((sym, qty, current, qty > 0, entry))

        def _fetch_and_decide(item):
            sym, qty, current, is_long, entry = item
            # 2026-08-27, user request ("it should have canceled the order"):
            # bypass_cache=True -- this is the actual per-minute stop-loss
            # trigger, not just a gate check; it must never read a snapshot
            # the equity scan cached possibly several minutes ago. See
            # get_bars()'s 2026-08-27 docstring update / _check_ema_trend_
            # alignment's force_fresh for the full reasoning (found via the
            # same BTDR case that exposed this in the re-entry gate).
            bars = get_bars(sym, period="1d", interval="1m", bypass_cache=True)
            if bars.empty or "close" not in bars.columns or len(bars) < EMA_TREND_MIN_BARS:
                return item, None  # not enough data -- never force a decision on it
            closes = bars["close"]
            ema7_now = float(closes.ewm(span=7, adjust=False).mean().iloc[-1])
            ema15_now = float(closes.ewm(span=15, adjust=False).mean().iloc[-1])
            reversal_reason = EnhancedExecutor._ema7_ema15_reversal_reason(ema7_now, ema15_now, is_long)
            if reversal_reason is not None:
                return item, reversal_reason
            ema9_now = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])
            peak = EnhancedExecutor._update_ema9_peak(self._ema9_trail_peak, sym, ema9_now, is_long)
            reason = EnhancedExecutor._ema9_trail_exit_reason(ema9_now, peak, is_long)
            return item, reason

        to_exit = []  # (item, reason)
        if eligible:
            with ThreadPoolExecutor(max_workers=min(POLLER_CHECK_WORKERS, len(eligible))) as pool:
                futures = [pool.submit(_fetch_and_decide, item) for item in eligible]
                for fut in as_completed(futures):
                    try:
                        item, reason = fut.result()
                        if reason is not None:
                            to_exit.append((item, reason))
                    except Exception as e:
                        log.warning(f"check_ema9_exit: fetch/decide failed for one position, skipping this poll: {e}")

        for (sym, qty, current, is_long, entry), reason in to_exit:
            # Cancel any resting order (the deferred GTC trailing stop, most
            # commonly) before closing -- the broker won't accept a second
            # order against qty that's already reserved by one.
            try:
                sym_orders = [o for o in (self.client.get_orders() or []) if o.symbol == sym]
                for _o in sym_orders:
                    try:
                        self.client.cancel_order_by_id(str(_o.id))
                    except Exception:
                        pass
                if sym_orders:
                    time.sleep(0.4)
            except Exception as e:
                log.warning(f"check_ema9_exit {sym}: order fetch/cancel failed, will retry next cycle: {e}")
                continue

            result = self._request_reconciled_close(sym, "ema9", current)
            if result.state == "submitted":
                log.warning(f"EMA9 EXIT {sym}: {result.requested_qty} shares | {reason}")
                self._maybe_rearm_reentry(
                    sym, is_long, result.requested_qty, "EMA9 EXIT",
                    was_loss=(current - entry) * qty < 0,
                )
            elif result.state in ("cancel_pending", "already_pending"):
                log.debug(f"check_ema9_exit {sym}: close {result.state} -- re-checking next tick")
            elif result.state != "flat":
                log.error(f"EMA9 EXIT {sym}: close not submitted ({result.state}): {result.detail}")

    def check_pending_entries_ema(self) -> None:
        """Every STAGNANT_STOP_CHECK_INTERVAL_MIN (1 min), re-check the
        EMA7 trend-alignment gate (_check_ema_trend_alignment) for
        every entry order still resting unfilled (tracked in
        self.order_cache), and cancel it if the condition no longer holds.

        2026-08-24, user request: "every minute check for the placed orders
        again for ema condition if the orders are not place already, often
        when the order is place the ema delta is met, but next minute the
        order doesn't execute but the ema delta condition is not met
        anymore." The entry gate is normally checked ONCE, at signal time,
        right before the trailing-buy order is submitted -- but that order
        is a resting TrailingStopOrderRequest that only fills once price
        reverses REENTRY_TRAIL_PCT% off its extreme (see
        _create_bracket_order), so it can sit unfilled for a while. By the
        time it would fill, the trend that justified placing it may have
        already turned back over. Rather than let it fill anyway on a
        setup that's no longer valid, this cancels it.

        2026-08-25, user request: "so every minute order is cancelled to
        place a new in next minute" [once conditions realign] -- a
        cancelled order isn't just discarded anymore, same as a signal
        blocked before ever placing one (see _validate_trade /
        check_blocked_entries_ema): if self._pending_entry_signals has the
        original signal for this symbol, it moves into
        self._ema_blocked_entries for the same per-minute retry treatment,
        instead of waiting on a fresh scan cycle to re-signal it. No
        stored signal (e.g. check_ema9_exit's re-entry re-arm, which has
        no real Signal to requeue with) -- just cancels, same as before.

        2026-08-27, user request ("ensure 1min checks are robust to cancel
        unfilled order if conditions change"): used to iterate
        self.order_cache (Dict[str, str], one order id per symbol) --
        confirmed live that two genuinely separate entry paths (a fresh
        scan-cycle signal and a check_blocked_entries_ema re-fire, ~45s
        apart) can each submit their own trailing-buy for the SAME symbol;
        the second write silently overwrote the first in order_cache, so
        only the newer order was ever rechecked here -- the older one, if
        still resting, was invisible to this whole function and would
        fill unconditionally regardless of what fresh EMA data said.

        Queries the broker directly instead -- one open-orders list call
        covers every resting entry order regardless of how many exist per
        symbol or whether order_cache's bookkeeping is complete. Filters
        on trailing_stop + time_in_force==DAY specifically: that combination
        is what every entry order in this file submits (_create_bracket_order,
        its re-chase path, check_ema9_exit's re-arm, _maybe_rearm_reentry).
        A GTC trailing_stop is a protective exit stop, not an entry --
        see _rebuild_order_cache_from_broker's docstring for the live risk
        of conflating the two (a held short's buy-to-cover protective stop
        looks identical to a long entry's buy order except for this field).
        side determines is_long per order (buy=long entry, sell=short
        entry) rather than assuming buy, so short entries get the same
        coverage as long ones.

        2026-08-27, user request ("the 1 min check algo has to work in
        parallel if needed to avoid overload issue"): the gate check per
        order is a fresh (force_fresh=True) network bar fetch -- I/O-bound,
        one per resting order, previously sequential. On a day with many
        resting entries that stacks up real wall-clock time on the
        10s-tick poller thread. Fetched/decided in parallel via a small
        pool (POLLER_CHECK_WORKERS); the actual mutations (cancel, dict
        writes) stay strictly sequential afterward in the main thread --
        no shared mutable state is touched from worker threads, only the
        read-only gate check runs concurrently."""
        # 2026-09-01, two-window schedule: no entry-order recheck/requeue
        # during the midday break -- the lunch flat already cancelled every
        # resting entry order at 11:00 ET, and requeueing a morning signal
        # into _ema_blocked_entries here would fire it on the 14:45 reopen.
        # check_blocked_entries_ema's own break gate holds those queues intact
        # until then; this function just skips the broker round-trip.
        import pytz as _pytz
        if in_lunch_break(datetime.datetime.now(_pytz.timezone("America/New_York"))):
            return
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            open_orders = self.client.get_orders(filter=req)
        except Exception as e:
            log.warning(f"check_pending_entries_ema: could not list open orders, skipping this recheck: {e}")
            return

        tracked_entry_orders = {str(v): k for k, v in getattr(self, "order_cache", {}).items()}
        candidates = []  # (order, order_id, sym, is_long)
        pending_syms_seen = set()
        for order in open_orders:
            order_id = str(getattr(order, "id", ""))
            raw_type = getattr(order, "order_type", "")
            otype = str(getattr(raw_type, "value", raw_type)).lower()
            is_tracked_entry = order_id in tracked_entry_orders
            if "trailing_stop" not in otype and not is_tracked_entry:
                continue
            if getattr(order, "time_in_force", None) != TimeInForce.DAY:
                continue  # GTC == protective exit stop, never touch it here
            sym = order.symbol
            pending_syms_seen.add(sym)
            raw_side = getattr(order, "side", "")
            side = str(getattr(raw_side, "value", raw_side)).lower()
            candidates.append((order, order_id, sym, side == "buy"))

        def _gate(item):
            _order, order_id, sym, is_long = item
            sig_stub = SimpleNamespace(symbol=sym)
            ok, reason = _check_ema_trend_alignment(sig_stub, is_long, force_fresh=True)
            return item, ok, reason

        results = []
        if candidates:
            with ThreadPoolExecutor(max_workers=min(POLLER_CHECK_WORKERS, len(candidates))) as pool:
                futures = [pool.submit(_gate, item) for item in candidates]
                for fut in as_completed(futures):
                    try:
                        results.append(fut.result())
                    except Exception as e:
                        log.warning(f"check_pending_entries_ema: gate check failed, skipping this order this poll: {e}")

        for (_order, order_id, sym, _is_long), ok, reason in results:
            if ok:
                continue
            try:
                self.client.cancel_order_by_id(order_id)
                if self.order_cache.get(sym) == order_id:
                    self.order_cache.pop(sym, None)
                pending = self._pending_entry_signals.pop(sym, None)
                if pending is not None:
                    self._ema_blocked_entries[sym] = {
                        "signal": pending["signal"], "order_type": pending["order_type"],
                        "queued_at": datetime.datetime.now(datetime.timezone.utc),
                    }
                    log.warning(f"PENDING ENTRY CANCELLED {sym}: still unfilled and {reason} -- requeued for retry")
                else:
                    log.warning(f"PENDING ENTRY CANCELLED {sym}: still unfilled and {reason}")
            except Exception as e:
                log.warning(f"check_pending_entries_ema {sym}: cancel failed: {e}")

        # order_cache/_pending_entry_signals entries for symbols with no
        # resting entry order left at the broker are stale references
        # (filled, or cancelled elsewhere) -- drop them so a future
        # _validate_trade pending-order-guard check doesn't block on a
        # dead order id.
        for sym in list(self.order_cache.keys()):
            if sym not in pending_syms_seen:
                self.order_cache.pop(sym, None)
        for sym in list(self._pending_entry_signals.keys()):
            if sym not in pending_syms_seen:
                self._pending_entry_signals.pop(sym, None)

    @staticmethod
    def _blocked_entry_action(gate_ok: bool, past_window: bool, in_universe: bool) -> str:
        """Pure decision function for check_blocked_entries_ema(): what to
        do with one queued blocked-entry this poll. Returns "fire" (gate
        now agrees -- submit it), "expire" (the entry window's closed, or
        the symbol's fallen out of the top-15 TI universe -- drop it,
        nothing left to wait for), or "wait" (keep it queued, check again
        next minute).

        2026-08-25, user request chain: "each blocked trade should wait for
        next minute recheck not to completely discard the order" -> ... ->
        "no expire" (dropped the arbitrary ENTRY_RETRY_MAX_MIN staleness
        timer -- a queued entry now waits as long as the entry window
        itself allows, not a fixed number of minutes) -> "the signal
        should cancel out only due to price conditions and not part of
        the top 15 list of the universe" -- clarifies what "waiting" is
        actually for: price (the gate) realigning, scoped to names still
        worth watching (still in the top-15 universe). A symbol that's
        dropped out of the universe isn't coming back on its own, so
        there's nothing left to wait for even before the window closes.
        past_window and not in_universe both take priority over gate_ok --
        same as every other re-entry-arm path in this file (e.g.
        check_ema9_exit's): both are absolute give-up conditions, checked
        first, no exception even if the gate happens to agree that exact
        minute."""
        if past_window or not in_universe:
            return "expire"
        if gate_ok:
            return "fire"
        return "wait"

    def check_blocked_entries_ema(self) -> None:
        """Every STAGNANT_STOP_CHECK_INTERVAL_MIN (1 min), re-check every
        signal queued in self._ema_blocked_entries (blocked by
        _check_ema_trend_alignment at signal time, or cancelled out of
        self.order_cache by check_pending_entries_ema once conditions
        turned, or -- 2026-08-26 -- a just-exited symbol whose immediate
        re-entry gate check in _maybe_rearm_reentry failed) -- the moment
        the gate agrees, fires _execute_entry fresh with a current account
        snapshot (re-validates and re-sizes from scratch; nothing about the
        original signal's price/sizing is assumed still current). Drops a
        queued entry once the entry window closes, or once the symbol
        falls out of the top-30 scan universe -- no separate staleness
        timer, see _blocked_entry_action.

        2026-08-25, user request chain: "each blocked trade should wait
        for next minute recheck not to completely discard the order" ->
        "the trade idea should check for every minute conditions to see
        when the new condition is met to reenter than completely discard
        a trade signal" -> "so every minute order is cancelled to place a
        new in next minute" [after conditions realign] -> "that why the
        trade enters as soon as the condition are met than stale orders"
        -> "no expire" -> "the signal should cancel out only due to price
        conditions and not part of the top 15 list of the universe" (the
        TI-universe check added here).

        Every signal reaching this queue already came from the normal
        scan (which draws from the top-30 scan universe), so under normal
        conditions this is already scoped to whatever the scan actually
        flagged -- the explicit universe check here is what keeps a queued
        entry from waiting indefinitely on a name that's since dropped off
        that list entirely.

        2026-08-26, user request ("trade only top 30 stocks, but update the
        top 30 based on the new scans included"): checks the actual scan
        universe (get_scan_targets(), same as _maybe_rearm_reentry) rather
        than the raw, uncapped get_ti_primary() -- was checking membership
        in a ~90-100-ticker list when the equity scan itself only ever
        trades 30 of them.

        2026-08-27, user request ("the 1 min check algo has to work in
        parallel if needed to avoid overload issue"): the gate check
        (_check_ema_trend_alignment, force_fresh=True) is a fresh network
        bar fetch per queued symbol -- I/O-bound, previously done one
        symbol at a time. Fetched in parallel via a small pool
        (POLLER_CHECK_WORKERS) as its own first pass; firing an entry
        (_execute_entry, real capital/order-submission side effects) stays
        strictly sequential in a second pass, same order and same logic as
        before -- two blocked entries racing to spend the same buying
        power in parallel would be a genuinely different (and worse) class
        of bug than the one being fixed here, so only the read-only gate
        check runs concurrently."""
        import pytz as _pytz
        _now_et = datetime.datetime.now(_pytz.timezone("America/New_York"))
        past_window = _now_et.strftime("%H:%M") >= ENTRY_WINDOW_END_ET
        # 2026-09-01, two-window schedule: during the midday break the queued
        # entries neither fire nor expire -- they WAIT (intact) for the 14:45
        # afternoon segment, per "reenter only at 2:45PM". The past_window
        # expiry below still applies at the 15:50 final cutoff.
        if in_lunch_break(_now_et):
            return
        ti_universe = set(_get_scan_targets())

        queued = list(self._ema_blocked_entries.items())

        def _gate(item):
            sym, info = item
            gate_ok, _ = _check_ema_trend_alignment(info["signal"], info["order_type"] == OrderType.LONG, force_fresh=True)
            return sym, gate_ok

        gate_results: Dict[str, bool] = {}
        if queued:
            with ThreadPoolExecutor(max_workers=min(POLLER_CHECK_WORKERS, len(queued))) as pool:
                futures = [pool.submit(_gate, item) for item in queued]
                for fut in as_completed(futures):
                    try:
                        sym, gate_ok = fut.result()
                        gate_results[sym] = gate_ok
                    except Exception as e:
                        log.warning(f"check_blocked_entries_ema: gate check failed, skipping this symbol this poll: {e}")

        for sym, info in queued:
            if sym not in gate_results:
                continue  # this symbol's gate check itself failed above -- leave it queued, retry next poll
            signal, order_type = info["signal"], info["order_type"]
            gate_ok = gate_results[sym]
            action = self._blocked_entry_action(gate_ok, past_window, sym in ti_universe)
            if action == "wait":
                continue
            del self._ema_blocked_entries[sym]
            if action == "expire":
                log.info(f"BLOCKED ENTRY EXPIRED {sym}: entry window closed or no longer in TI universe")
                continue
            try:
                acct = self._get_account(force_refresh=True)
                # 2026-08-26, user request ("remove cool off or any other
                # software blocks for stock reentries such as pdt"): only
                # bypass the bot's own PDT ceiling when this genuinely IS a
                # re-entry (already traded sym today, or has broker fill
                # history at all -- same definition _create_bracket_order
                # uses to pick the trailing-buy path) -- a fresh first-time
                # signal blocked purely on the EMA gate still gets the
                # normal PDT check.
                is_long = order_type == OrderType.LONG
                if sym in getattr(self, "_loss_reentry_required", set()):
                    performance_ok, performance_reason = EnhancedExecutor._check_30m_reentry_performance(sym, is_long)
                    if not performance_ok:
                        log.info(f"BLOCKED ENTRY {sym}: {performance_reason} -- loss re-entry 30m gate")
                        self._ema_blocked_entries[sym] = info
                        continue
                    if performance_reason:
                        log.info(f"BLOCKED ENTRY {sym}: {performance_reason}")
                bypass_pdt = self._is_reentry_signal(sym, is_long)
                if self._execute_entry(signal, acct, order_type, bypass_pdt=bypass_pdt):
                    getattr(self, "_loss_reentry_required", set()).discard(sym)
                    log.info(f"BLOCKED ENTRY RE-FIRED {sym}: EMA condition realigned")
            except Exception as e:
                log.warning(f"check_blocked_entries_ema {sym}: retry failed: {e}")

    @staticmethod
    def _swing_drift_stop_reason(current: float, entry: Optional[float], is_long: bool, stop_pct: float) -> Optional[str]:
        """Pure decision function for check_swing_drift_stop() -- entry price
        only (no 30-min-ago leg; doesn't map across multiple days the way it
        does intraday). Longs: current below entry by more than stop_pct%.
        Shorts: mirrored (current above entry)."""
        if entry is None or entry <= 0:
            return None
        adverse_pct = ((entry - current) / entry * 100) if is_long else ((current - entry) / entry * 100)
        if adverse_pct > stop_pct:
            return f"${entry:.2f}->${current:.2f} ({adverse_pct:+.1f}%)"
        return None

    def check_swing_drift_stop(self) -> None:
        """Wider-threshold sibling of check_price_drift_stop() for positions
        it doesn't cover -- anything NOT a same-day entry (multi-day swing
        holds). 2026-08-15, user request: idea #3 of six suggested
        improvements, built after TrendBreaker's multi-day losers (NWL
        -5.41% held 55h) sat unwatched between entry and its normal, much
        wider trailing stop for days at a time. See SWING_DRIFT_STOP_PCT in
        config.py for the reasoning and which trades this would/wouldn't
        have caught."""
        if not SWING_DRIFT_STOP_ENABLED:
            return

        try:
            positions = self.client.get_all_positions()
        except Exception as e:
            log.warning(f"check_swing_drift_stop: fetch failed: {e}")
            return

        today = datetime.date.today()
        for pos in positions:
            sym = pos.symbol
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue  # options legs -- managed separately
            qty = int(float(pos.qty))
            if qty == 0:
                continue

            entry_info = self._entry_log.get(sym)
            if entry_info and entry_info.get("date") == today:
                continue  # same-day -- covered by check_price_drift_stop() already

            try:
                current = float(pos.current_price)
                entry   = float(pos.avg_entry_price)
            except (TypeError, ValueError):
                continue
            if current <= 0:
                continue

            is_long = qty > 0
            reason = self._swing_drift_stop_reason(current, entry, is_long, SWING_DRIFT_STOP_PCT)
            if reason is None:
                continue

            try:
                sym_orders = [o for o in (self.client.get_orders() or []) if o.symbol == sym]
                for _o in sym_orders:
                    try:
                        self.client.cancel_order_by_id(str(_o.id))
                    except Exception:
                        pass
                if sym_orders:
                    time.sleep(0.4)
            except Exception as e:
                log.warning(f"check_swing_drift_stop {sym}: order fetch/cancel failed, will retry next cycle: {e}")
                continue

            side = OrderSide.SELL if is_long else OrderSide.BUY
            try:
                self._submit_closing_order(sym, abs(qty), side, current)
                log.warning(f"SWING DRIFT STOP {sym}: {abs(qty)} shares | {reason}")
            except Exception as e:
                log.error(f"SWING DRIFT STOP {sym}: close failed: {e}")
                try:
                    trail_pct, _ = _atr_trail_pct_for(sym, current, self._entry_log)
                    self.client.submit_order(TrailingStopOrderRequest(
                        symbol=sym, qty=abs(qty), side=side,
                        type=AlpacaOrderType.TRAILING_STOP, time_in_force=TimeInForce.GTC,
                        trail_percent=trail_pct,
                    ))
                    log.warning(f"check_swing_drift_stop {sym}: re-armed GTC trailing stop after failed close")
                except Exception as rearm_err:
                    log.error(f"check_swing_drift_stop {sym}: close failed AND GTC re-arm failed -- position may be UNPROTECTED: {rearm_err}")

    # -- Kill Mode: Emergency Close All ---------------------------------------
    def emergency_close_all(self, equity: float) -> None:
        """
        Kill mode emergency exit. Closes every open position as safely as possible.

        PDT rules (equity < $25k):
          - Positions opened on a PRIOR day -> cancel any open orders then market-close.
            These are NOT day trades so no PDT count is consumed.
          - Positions opened TODAY -> cannot close without a day-trade violation.
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
                # Today's position -- tighten trailing stop to hairpin; do NOT market-close
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
                        f"(opened today -- closing via stop to avoid PDT violation)"
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
                # A plain MarketOrderRequest gets rejected outside regular hours --
                # kill mode is only reachable while is_market_open (07:00-20:00 ET),
                # not just regular hours, and every crash this account has actually
                # hit (BIOA, FIRY, SQQQ) happened after-hours. This is the emergency
                # exit; it can't be the one path that silently no-ops exactly when
                # it's needed most. _submit_closing_order handles the extended-hours
                # limit-order fallback the same as every other close path.
                self._no_rearm.add(sym)  # kill-switch -- get flat for safety, never re-enter immediately
                self._submit_closing_order(sym, abs(qty), side, float(pos.current_price or 0))
                pnl = float(pos.unrealized_pl or 0)
                log.warning(
                    f"KILL MODE CLOSE {sym}: {abs(qty)} shares "
                    f"{'SELL' if qty > 0 else 'BUY-TO-COVER'} | unrealized ${pnl:+.2f}"
                )
                closed.append(sym)
            except Exception as e:
                log.error(f"KILL MODE: close failed {sym}: {e}")

        log.warning(
            f"KILL MODE COMPLETE -- "
            f"market-closed: {len(closed)} {closed} | "
            f"hairpin stops (PDT-safe): {len(protected)} {protected}"
        )

    # -- Stale Order Updater ---------------------------------------------------
    def update_stale_orders(self) -> None:
        """
        Find open orders older than STALE_ORDER_MINUTES and re-submit them:
          - Regular hours   -> cancel + market order (instant fill)
          - Extended hours  -> cancel + limit order at current price (IOC)
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
            # Never cancel GTC trailing stop orders -- they are protective stops,
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
                f"STALE ORDER: {sym} {side} {qty} -- age {age_secs/60:.1f}m "
                f"(cutoff {'intraday 30m' if is_intraday else '6h'}) "
                f"-> {'market' if regular else 'limit @ current price'}"
            )

            try:
                self.client.cancel_order_by_id(order_id)
                time.sleep(0.3)

                if regular:
                    # If the original was a limit buy and the limit was more than 1%
                    # below the current ask, the order was defensive/passive -- don't
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
                                f"(ask=${cur_ask:.2f}) -- cancelling without re-entry"
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

    # -- ATR Take-Profit Checker ------------------------------------------------
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
                    # Cancel ALL resting orders for this symbol first -- a GTC trailing
                    # stop (or leftover DAY order) reserves qty and gets this rejected
                    # as "insufficient qty available" (confirmed in production: BHC
                    # rejected 13+ times over an hour on 2026-07-31, same root cause
                    # already fixed for check_afterhours_stops/close_no_gain_positions/
                    # the weakest-swap path -- this one just never got it).
                    try:
                        for o in (self.client.get_orders() or []):
                            if o.symbol == sym:
                                self.client.cancel_order_by_id(str(o.id))
                                time.sleep(0.4)
                    except Exception as cancel_err:
                        log.warning(f"TP close {sym}: order cancel failed, close may reject: {cancel_err}")

                    side = OrderSide.SELL if is_long else OrderSide.BUY
                    # A plain MarketOrderRequest also gets rejected outside regular
                    # hours (07:00-20:00 is_market_open spans well past 09:30-16:00,
                    # and this method runs on every cycle in that whole window) --
                    # _submit_closing_order already handles the extended-hours case.
                    # 2026-08-26, user request ("irrespective of exit type"):
                    # NOT marked _no_rearm -- a take-profit hit followed by a
                    # re-arm is exactly "catch the continuation" if the trend
                    # gate still agrees.
                    self._submit_closing_order(sym, abs(qty), side, cur_price)
                    _strategy = self._entry_log.get(sym, {}).get("strategy", "unknown")
                    try:
                        _pnl = float(getattr(pos, "unrealized_pl", 0) or 0)
                    except (TypeError, ValueError):
                        _pnl = 0.0
                    log.info(
                        f"TP HIT {sym} [{_strategy}]: ${cur_price:.2f} {'>=  ' if is_long else '<= '}"
                        f"${tp_price:.2f} | P&L ${_pnl:+,.2f} -> {'sell' if is_long else 'buy-to-cover'} submitted"
                    )
                    triggered.append(sym)
                except Exception as e:
                    log.warning(f"TP close failed {sym}: {e}")

        for sym in triggered:
            self._tp_targets.pop(sym, None)


# 2026-08-24, user request: this guard used to sit right after _demo()'s own
# definition (line ~424), well before EnhancedExecutor exists below it --
# _demo() references EnhancedExecutor._ema15_exit_reason, so running this
# file directly always raised NameError before ever reaching that class's
# checks, silently since 2026-08-22. Moved to the actual end of the file so
# `python engine/execution/enhanced.py` runs every check it's meant to.
if __name__ == "__main__":
    _demo()

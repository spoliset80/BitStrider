"""ApexTrader scan nucleus.

Contains reusable scanning functions for main loop and run_top3 tools.
"""

import datetime
import json
import logging
import threading
import time
from pathlib import Path
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Tuple, Set, Optional, Union

from engine import config as _cfg
from engine.config import (
    SCAN_WORKERS,
    SCAN_SYMBOL_TIMEOUT,
    MIN_DOLLAR_VOLUME,
    MIN_FLOAT_SHARES_REGULAR_HOURS,
    MIN_AVG_DAILY_VOLUME_REGULAR_HOURS,
    MIN_MARKET_CAP,
    MIN_STOCK_PRICE,
    LONG_ONLY_MODE,
    MIN_SIGNAL_CONFIDENCE,
    MAX_SIGNALS_PER_CYCLE,
    RVOL_MIN,
    MAX_GAP_CHASE_PCT,
    GAP_CHASE_CONSOL_BARS,
    GAP_CHASE_GUARD_ENABLED,
    HMM_REGIME_LOOKBACK_DAYS,
    HMM_REGIME_CONFIDENCE_BOOST,
    TRADE_THIN_LIQUIDITY_REJECTS,
    THIN_LIQUIDITY_EXCLUDED_STRATEGIES,
    EOD_CLOSE_TIME,
)
from engine.utils import MarketState, clear_bar_cache, get_bars, get_daily_volume_bars, is_dead_ticker, get_hmm_regime
from engine.utils.bars import get_data_client as _get_data_client
from alpaca.data import StockSnapshotRequest as _StockSnapshotRequest
from .universe import get_tier as _get_tier_live, get_latest_batch as _get_latest_batch, get_ti_primary as _get_ti_primary
from .discovery import get_alpaca_movers_queue as _get_alpaca_movers_queue
from .strategies import (
    Signal,
    get_strategy_instances,
    _get_float_shares,
    _get_market_cap,
)

_ET  = pytz.timezone("America/New_York")
_log = logging.getLogger("ApexTrader")
_ACTIVE_SCAN_FILE = Path(__file__).resolve().parents[2] / "data" / "ti_primary_active.json"
_ACTIVE_LONGS_FILE = Path(__file__).resolve().parents[2] / "data" / "ti_primary_active_longs.json"
_ACTIVE_SHORTS_FILE = Path(__file__).resolve().parents[2] / "data" / "ti_primary_active_shorts.json"
_DEFAULT_SCAN_SYMBOLS = set(_cfg.LOW_PRIORITY_SCAN_SYMBOLS)
_active_scan_write_lock = threading.Lock()
_last_active_scan_write = 0.0
# 2026-09-02: total wall-clock budget for one scan_universe() worker-collection
# loop. SCAN_WORKERS=16 workers at SCAN_SYMBOL_TIMEOUT=15s each can legitimately
# take ~90-120s on a slow-data day (every symbol timing out); anything beyond
# that means the pool itself is wedged and the main loop must move on.
_SCAN_TOTAL_BUDGET_SEC = 120

# 2026-09-02: restored module-level init for the adaptive-relax block at the
# bottom of scan_universe() (lines ~948-964) -- the references survived but the
# init dict + _ADAPTIVE_* constants had been dropped, crashing every scan with
# `NameError: name '_adaptive_state' is not defined` (only reachable once the
# scan actually completes, which the freeze/blocked mornings never allowed).
# Purely informational today: nothing reads the relaxed values back into
# enforcement (the regime-based RVOL at _passes_guardrails is the live gate);
# restored so the block logs as designed instead of aborting the scan cycle.
_ADAPTIVE_MAX_EMPTY  = 3
_ADAPTIVE_MIN_RVOL   = 1.0
_ADAPTIVE_STEP_RVOL  = 0.05
_ADAPTIVE_MIN_CONF   = 0.60
_ADAPTIVE_STEP_CONF  = 0.01
_adaptive_state = {
    "empty_scans": 0,
    "rvol_min": RVOL_MIN,
    "min_conf": MIN_SIGNAL_CONFIDENCE,
}


def _write_active_scan_list(tickers: List[str]) -> bool:
    """Persist the guardrail-approved top scan list at the configured cadence."""
    global _last_active_scan_write
    now = time.monotonic()
    with _active_scan_write_lock:
        if now - _last_active_scan_write < _cfg.ACTIVE_SCAN_SNAPSHOT_INTERVAL_MIN * 60:
            return False
        _last_active_scan_write = now
    try:
        _ACTIVE_SCAN_FILE.write_text(
            json.dumps({
                "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "count": len(tickers),
                "tickers": tickers,
                "longs": [],
                "shorts": [],
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_active_directional_lists(tickers)
        _log.info(f"[UNIVERSE] active top-{len(tickers)} list snapshot updated: {_ACTIVE_SCAN_FILE.name}")
        return True
    except Exception as e:
        _log.warning(f"[UNIVERSE] could not write active scan list: {e}")
        return False


def _write_active_directional_lists(tickers: List[str]) -> None:
    """Write direction lists, switching sides on the 09:30 open after 10 ET."""
    try:
        from engine.ti.yahoo_universe import fetch_long_short_candidates
        gainers, losers = fetch_long_short_candidates()
        long_symbols = {symbol for symbol, _ in gainers}
        short_symbols = {symbol for symbol, _ in losers}
        if not long_symbols and not short_symbols:
            raise RuntimeError("Yahoo returned no directional candidates")
    except Exception as e:
        _log.warning(f"[SIGNALS] live Yahoo direction fetch failed, using universe tiers: {e}")
        long_symbols = set(_get_tier_live(1))
        short_symbols = set(_get_tier_live(2))
    mover_symbols = set(_get_alpaca_movers_queue())
    now_et = datetime.datetime.now(_ET)
    after_open_confirmation = (now_et.hour > 10) or (now_et.hour == 10 and now_et.minute >= 0)

    def _day_direction(symbol: str) -> Optional[bool]:
        try:
            bars = get_bars(symbol, "1d", "1m")
            if bars.empty or "open" not in bars.columns or "close" not in bars.columns:
                return None
            if not after_open_confirmation:
                return None
            current_price = float(bars["close"].iloc[-1])
            if current_price <= 0:
                return None
            open_price = None
            if "time" in bars.columns:
                session_open = bars[bars["time"].dt.strftime("%H:%M") >= "09:30"]
                if not session_open.empty:
                    open_price = float(session_open["open"].iloc[0])
            if open_price is None or open_price <= 0:
                return None
            if current_price > open_price:
                return True
            if current_price < open_price:
                return False
            return None
        except Exception as e:
            _log.debug(f"[SIGNALS] {symbol}: day-direction check failed: {e}")
            return None

    if after_open_confirmation:
        directions = {symbol: _day_direction(symbol) for symbol in tickers}
        longs = [s for s in tickers if directions[s] is True or s in _DEFAULT_SCAN_SYMBOLS]
        shorts = [s for s in tickers if directions[s] is False or s in _DEFAULT_SCAN_SYMBOLS]
    else:
        longs = [s for s in tickers if s in long_symbols or s in mover_symbols or s in _DEFAULT_SCAN_SYMBOLS]
        shorts = [s for s in tickers if s in short_symbols or s in mover_symbols or s in _DEFAULT_SCAN_SYMBOLS]

    longs = list(dict.fromkeys(longs))
    shorts = list(dict.fromkeys(shorts))

    # Keep each side populated with at least 30 candidates when enough names
    # exist: ranked Yahoo/Alpaca movers first, then deterministic defaults.
    default_longs = list(dict.fromkeys(_cfg.PRIORITY_1_MOMENTUM + sorted(_DEFAULT_SCAN_SYMBOLS)))
    default_shorts = list(dict.fromkeys(_cfg.PRIORITY_2_ESTABLISHED + sorted(_DEFAULT_SCAN_SYMBOLS)))
    long_backfill = list(dict.fromkeys(
        [s for s in tickers if s in long_symbols or s in mover_symbols]
        + [s for s in tickers if s not in short_symbols]
        + default_longs
    ))
    short_backfill = list(dict.fromkeys(
        [s for s in tickers if s in short_symbols]
        + [s for s in tickers if s in mover_symbols and s not in long_symbols]
        + default_shorts
    ))

    for symbol in long_backfill:
        if len(longs) >= 30:
            break
        if symbol not in longs:
            longs.append(symbol)
    for symbol in short_backfill:
        if len(shorts) >= 30:
            break
        if symbol not in shorts:
            shorts.append(symbol)

    longs = longs[:30]
    shorts = shorts[:30]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        directional = {
            "longs": [{"symbol": symbol, "action": "buy"} for symbol in longs],
            "shorts": [{"symbol": symbol, "action": "short"} for symbol in shorts],
        }
        for path, selected, action in (
            (_ACTIVE_LONGS_FILE, longs, "buy"),
            (_ACTIVE_SHORTS_FILE, shorts, "short"),
        ):
            path.write_text(json.dumps({
                "updated": now,
                "count": len(selected),
                "signals": [{"symbol": symbol, "action": action} for symbol in selected],
            }, indent=2) + "\n", encoding="utf-8")
        active = json.loads(_ACTIVE_SCAN_FILE.read_text(encoding="utf-8"))
        active.update(directional)
        active["updated"] = now
        _ACTIVE_SCAN_FILE.write_text(json.dumps(active, indent=2) + "\n", encoding="utf-8")
    except Exception as e:
        _log.warning(f"[SIGNALS] could not write active Yahoo directional lists: {e}")


_PRIORITY_STRATEGIES = {"GapBreakout", "ORB"}


def _strategy_selection_rank(signal: Signal) -> tuple:
    """Prefer Gap Breakout/ORB, then use confidence within each tier."""
    return (signal.strategy in _PRIORITY_STRATEGIES, signal.confidence)


def _top_list_signal(symbol: str) -> Optional[Signal]:
    """Fallback candidate for a guardrail-approved active-list symbol."""
    bars = get_bars(symbol, "1d", "1m")
    if bars.empty or "close" not in bars.columns:
        return None
    try:
        price = float(bars["close"].iloc[-1])
        if price <= 0:
            return None
    except Exception:
        return None

    try:
        long_side = set(_get_tier_live(1))
        short_side = set(_get_tier_live(2))
    except Exception:
        long_side, short_side = set(), set()

    if symbol in short_side and symbol not in long_side and not LONG_ONLY_MODE:
        return Signal(symbol, "short", price, MIN_SIGNAL_CONFIDENCE, "Top-list loser after guardrails; waiting on EMA entry conditions", "TopList")
    return Signal(symbol, "buy", price, MIN_SIGNAL_CONFIDENCE, "Top-list candidate after guardrails; waiting on EMA entry conditions", "TopList")

# -- Batch snapshot cache ------------------------------------------------------
# Populated once at the start of each scan_universe() call via a single
# batch request.  _passes_guardrails() reads from this cache to avoid
# per-symbol 1-minute bars requests (390 bars x N symbols = dominant I/O cost).
_snapshot_cache: Dict = {}
_SNAPSHOT_STALE_SECONDS = 300  # snapshot's latest_trade older than this -> fall back to fresh intraday bars


def _prefetch_snapshots(symbols: List[str]) -> None:
    """Batch-fetch stock snapshots for *symbols* and store in _snapshot_cache.

    A single API call replaces N individual get_bars("1d","1m") requests in
    _passes_guardrails(), reducing scan latency significantly for large universes.
    Failures are silently swallowed -- _passes_guardrails() falls back to bars.
    """
    global _snapshot_cache
    _snapshot_cache = {}
    if not symbols:
        return
    try:
        client = _get_data_client()
        snaps = client.get_stock_snapshot(
            _StockSnapshotRequest(symbol_or_symbols=symbols)
        )
        if isinstance(snaps, dict):
            _snapshot_cache = snaps
    except Exception:
        pass  # fall back to per-symbol get_bars in _passes_guardrails


# Every guardrail-rejection reason _passes_guardrails() can return that this
# path is allowed to rescue. Deliberately excludes:
#   - 'other': not a guardrail at all, the catch-all for non-guardrail skips
#     (stale data, symbol errors, etc.) this path was never meant to override.
#   - 'min_price': penny stocks stay hard-blocked even intraday, 2026-08-13
#     user request ("only avoid penny stocks") -- the one guardrail reason
#     that's about instrument quality (poor fill quality, wide spreads on
#     sub-MIN_STOCK_PRICE names) rather than liquidity/volume/momentum
#     thresholds this widening is meant to relax.
#   - 'dollar_vol', 'avg_volume', 'low_float', 'low_mcap' -- 2026-08-26, user
#     request ("shouldn't meet the non-negotiable limits of volume"):
#     removed. These four describe the security's actual tradability --
#     how much of it exists and how much really changes hands -- not a
#     noisy momentary reading, so admitting a signal that fails one of
#     them isn't "the setup looked thin for a second," it's "this stock
#     structurally doesn't have the volume to trade safely." Confirmed
#     live: RPGL admitted via dollar_vol (12x under the $900K floor,
#     0.0M float) and round-tripped for a real loss. rvol and gap_chase
#     stay admittable -- both are momentum-shape reads that can
#     legitimately wobble under/over their threshold minute to minute on
#     an otherwise normal-liquidity name.
_ALL_GUARDRAIL_REASONS = frozenset({
    'rvol', 'gap_chase',
})


def _should_admit_thin_liquidity(reason: Optional[str], market_state: Optional[MarketState] = None) -> bool:
    """True if a _passes_guardrails() rejection reason should be re-admitted
    (sized down via THIN_LIQUIDITY_POSITION_SIZE_PCT) instead of discarded.

    2026-08-12, user request, off by default (TRADE_THIN_LIQUIDITY_REJECTS).
    Originally only avg_volume/low_float qualified. Split out as its own
    function so this decision is unit-testable without driving the rest of
    scan_universe()'s threaded scan machinery.

    2026-08-13, user request ("no stocks to be held or traded overnight
    which fail guards"): regular-hours only. NRGV got admitted via this path
    at 16:02 ET (2 min after close, ext-hours) and sat failing the overnight
    guardrail all night until the no-gain-exit rule caught it at 06:37 the
    next morning -- an entry opened outside regular hours IS an overnight
    hold from the moment it fills, with no same-day close_guardrail_fail_
    positions run left to catch it before the close it already missed.

    2026-08-13, user request ("no guard rails for ANY scanner during intra
    day... check before closing end of day if the tickers pass guardrail,
    keep them overnight" -- refined same day to "only avoid penny stocks"):
    widened from avg_volume/low_float to every real guardrail reason except
    min_price -- RVOL, dollar_vol, gap_chase, avg_volume, and market cap were
    no longer hard-blocked at entry; penny stocks still are. The overnight
    side already enforces the real safety boundary regardless of which
    reasons got waived at entry: close_guardrail_fail_positions checks every
    open position, any strategy, against avg_volume/float/mcap at 15:45 ET
    and force-closes anything still failing -- that's the "check before
    closing end of day" the user asked for, already built (2026-08-12
    guardrail-fail overnight exit feature). This just stops the entry-side
    gate from being stricter than the exit-side one for intraday trades that
    are getting flattened by the close regardless.
    market_state=None (caller didn't pass one) fails closed -- no admit.

    2026-08-26, user request ("shouldn't meet the non-negotiable limits of
    volume"): narrowed back down. dollar_vol/avg_volume/low_float/low_mcap
    removed from _ALL_GUARDRAIL_REASONS -- those four describe actual
    tradability, not a momentary reading, and admitting a signal that fails
    one of them isn't the "thin for a second" case this path exists for.
    Only rvol and gap_chase remain admittable. See _ALL_GUARDRAIL_REASONS'
    own comment for the RPGL case this was measured against.

    2026-08-17, user request: also cut off at EOD_CLOSE_TIME (15:45 ET).
    is_regular_hours alone wasn't enough -- ASST and NUAI both got admitted
    at 15:57 ET, 12 min after close_guardrail_fail_positions' own once-per-
    day sweep already ran (gated on the same EOD_CLOSE_TIME) and marked
    itself done for the day. An admit past that point has no same-day
    guardrail check left to catch it before an overnight hold.
    """
    if not (TRADE_THIN_LIQUIDITY_REJECTS and reason in _ALL_GUARDRAIL_REASONS):
        return False
    if not (market_state and market_state.is_regular_hours):
        return False
    # EOD cutoff: 2026-08-17. Guard against a bare/mocked market_state that
    # carries is_regular_hours but no .now (unit tests / defensive callers) --
    # treat "time unknown" as inside the window rather than raising.
    now = getattr(market_state, "now", None)
    if now is None:
        return True
    return now.strftime("%H:%M") < EOD_CLOSE_TIME


def _passes_guardrails(
    symbol: str,
    bull_regime: Optional[bool] = None,
    market_state: Optional[MarketState] = None,
    return_reason: bool = False,
) -> Union[bool, Tuple[bool, Optional[str]]]:
    """Pre-scan gates: dollar-volume, RVOL, and gap-chase guard.
    Returns False to skip the symbol; never raises.

    bull_regime: pass the pre-computed regime from scan_universe() to avoid
    a concurrent re-fetch of _is_bull_regime() inside each worker thread.
    If None, falls back to calling _is_bull_regime() directly.

    market_state: shared MarketState for the current scan cycle. If None,
    it will be created lazily.
    """
    # return_reason is now an explicit argument
    try:
        # -- Fast path: use batch-prefetched snapshot (no per-symbol HTTP call) -
        # Only trusted if latest_trade itself is recent -- an unbounded-age snapshot
        # (thin after-hours book, dead feed for this symbol) previously fed straight
        # into every guardrail with no check at all.
        _snap = _snapshot_cache.get(symbol)
        _snap_fresh = False
        latest_trade = None
        daily_bar = None
        previous_daily_bar = None
        if _snap is not None:
            latest_trade = getattr(_snap, "latest_trade", None)
            daily_bar = getattr(_snap, "daily_bar", None)
            previous_daily_bar = getattr(_snap, "previous_daily_bar", None)
        if (
            _snap is not None
            and daily_bar is not None
            and latest_trade is not None
        ):
            _trade_ts = getattr(latest_trade, "timestamp", None)
            if _trade_ts is None:
                _snap_fresh = True  # no timestamp to check -- trust it, same as before
            else:
                if _trade_ts.tzinfo is None:
                    _trade_ts = _trade_ts.replace(tzinfo=datetime.timezone.utc)
                _snap_age = (datetime.datetime.now(datetime.timezone.utc) - _trade_ts).total_seconds()
                _snap_fresh = _snap_age <= _SNAPSHOT_STALE_SECONDS
                if not _snap_fresh:
                    _log.debug(f"[GUARDRAIL] {symbol}: snapshot stale ({_snap_age:.0f}s) -- falling back to fresh intraday bars")

        if _snap_fresh and latest_trade is not None and daily_bar is not None:
            price   = float(latest_trade.price)
            day_vol = float(daily_bar.volume)
            open_px = float(daily_bar.open)
            prev_close = float(previous_daily_bar.close) if previous_daily_bar is not None else 0.0
            intraday = None
        else:
            # -- Fallback: fetch 1-min intraday bars ---------------------------
            intraday = get_bars(symbol, "1d", "1m")
            if intraday.empty or len(intraday) < 5:
                if return_reason:
                    return False, 'other'
                return False
            price   = float(intraday["close"].iloc[-1])
            day_vol = float(intraday["volume"].sum())
            open_px = float(intraday["open"].iloc[0])
            prev_close = 0.0  # not available without an extra daily-bars call

        # true_day_vol: day_vol above is Alpaca/IEX-sourced -- typically just a
        # few percent of real market volume (confirmed 2026-08-05, see
        # get_daily_volume_bars). avg_daily_vol below comes from yfinance's
        # full consolidated volume, so comparing raw day_vol against it -- as
        # both RVOL gates below used to -- compares apples to oranges and
        # crushes RVOL toward ~0 for everything (confirmed 2026-08-06: AAPL/
        # MSFT/NVDA all showing 0.01-0.07 RVOL mid-afternoon, blocking nearly
        # every candidate all day). Use yfinance's own running total for
        # today when its last bar actually is today; otherwise keep day_vol
        # rather than risk a stale number.
        true_day_vol = day_vol
        _vol_daily = get_daily_volume_bars(symbol)
        if not _vol_daily.empty and "time" in _vol_daily.columns:
            _last_bar_date = _vol_daily["time"].iloc[-1].date()
            if _last_bar_date == datetime.datetime.now(_ET).date():
                true_day_vol = float(_vol_daily["volume"].iloc[-1])

        # Resolve regime and VIX before adaptive gates
        # Stock entries are regime-neutral. Use the stock's own data and the
        # fixed guardrail thresholds; SPY/VIX must not decide direction or
        # whether an individual stock is eligible.
        vix = None
        bull = True

        # Adaptive MIN_STOCK_PRICE: more flexible for current market
        base_min_price = MIN_STOCK_PRICE
        base_dollar_vol = MIN_DOLLAR_VOLUME
        base_rvol = RVOL_MIN

        if bull:
            if vix and vix > 25:
                adaptive_min_price = base_min_price + 0.5
                adaptive_dollar_vol = base_dollar_vol * 1.2
                adaptive_rvol = base_rvol + 0.3
            elif vix and vix >= 18:
                adaptive_min_price = base_min_price
                adaptive_dollar_vol = base_dollar_vol
                adaptive_rvol = max(1.2, base_rvol - 0.3)
            elif vix and vix >= 15:
                adaptive_min_price = base_min_price
                adaptive_dollar_vol = base_dollar_vol * 0.9
                adaptive_rvol = max(1.0, base_rvol - 0.5)
            else:
                adaptive_min_price = max(1.0, base_min_price - 0.5)
                adaptive_dollar_vol = base_dollar_vol * 0.8
                adaptive_rvol = max(0.9, base_rvol - 0.6)
        else:
            if vix and vix < 18:
                adaptive_min_price = max(1.0, base_min_price - 0.7)
                adaptive_dollar_vol = base_dollar_vol * 0.6
                adaptive_rvol = max(0.8, base_rvol - 0.7)
            else:
                adaptive_min_price = max(1.0, base_min_price - 0.5)
                adaptive_dollar_vol = base_dollar_vol * 0.75
                adaptive_rvol = max(1.0, base_rvol - 0.4)

        if price < adaptive_min_price:
            _log.warning(f"[GUARDRAIL] {symbol} blocked: price {price:.2f} < adaptive_min_price {adaptive_min_price}")
            if return_reason:
                return False, 'min_price'
            return False

        # Adaptive RVOL_MIN: higher in bull/high VIX, lower in calm or bear conditions
        # Use regular market hours only so extended-hours volume does not distort the pace.
        if market_state is not None and market_state.is_regular_hours and bull:
            daily = get_daily_volume_bars(symbol)
            if not daily.empty and len(daily) >= 2:
                avg_daily_vol = float(daily["volume"].iloc[:-1].mean())
                if avg_daily_vol > 0:
                    now_et       = datetime.datetime.now(_ET)
                    mkt_open     = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
                    elapsed_min  = max((now_et - mkt_open).total_seconds() / 60, 1.0)
                    elapsed_frac = min(elapsed_min / 390.0, 1.0)
                    rvol = (true_day_vol / max(elapsed_frac, 0.02)) / avg_daily_vol
                    if rvol < adaptive_rvol:
                        _log.warning(f"[GUARDRAIL] {symbol} blocked: RVOL {rvol:.2f} < adaptive_rvol {adaptive_rvol:.2f} | day_vol={true_day_vol:.0f} | avg_daily_vol={avg_daily_vol:.0f}")
                        if return_reason:
                            return False, 'rvol'
                        return False

        # Adaptive MIN_DOLLAR_VOLUME: more flexible for current market
        dollar_vol = price * true_day_vol
        if dollar_vol < adaptive_dollar_vol:
            _log.warning(f"[GUARDRAIL] {symbol} blocked: dollar volume {dollar_vol:.0f} < adaptive_dollar_vol {adaptive_dollar_vol:.0f} | price={price:.2f} | day_vol={true_day_vol:.0f}")
            if return_reason:
                return False, 'dollar_vol'
            return False

        # Liquidity / quality floor -- skip thin, low-float, micro-cap names prone to
        # violent, illiquid moves. 2026-08-23, user request: combined the old
        # two-layer system (an absolute hard floor plus a separate, session-
        # gated regular/pre-after-hours floor) into one flat set, applied the
        # same regardless of time of day or session: float > 10M, avg daily
        # volume >= 700K. See MIN_FLOAT_SHARES_REGULAR_HOURS/
        # MIN_AVG_DAILY_VOLUME_REGULAR_HOURS in config.py (names kept for the
        # overnight guardrail-fail check in enhanced.py, which still uses
        # them independently).
        daily = get_daily_volume_bars(symbol)
        if not daily.empty and len(daily) >= 2:
            avg_daily_vol = float(daily["volume"].iloc[:-1].mean())
            if avg_daily_vol < MIN_AVG_DAILY_VOLUME_REGULAR_HOURS:
                _log.warning(f"[GUARDRAIL] {symbol} blocked: avg daily volume {avg_daily_vol:.0f} < {MIN_AVG_DAILY_VOLUME_REGULAR_HOURS:.0f}")
                if return_reason:
                    return False, 'avg_volume'
                return False

        shares_float = _get_float_shares(symbol)
        if shares_float is not None and shares_float <= MIN_FLOAT_SHARES_REGULAR_HOURS:
            _log.warning(f"[GUARDRAIL] {symbol} blocked: float {shares_float/1e6:.1f}M <= {MIN_FLOAT_SHARES_REGULAR_HOURS/1e6:.0f}M")
            if return_reason:
                return False, 'low_float'
            return False

        market_cap = _get_market_cap(symbol)
        if market_cap is not None and market_cap < MIN_MARKET_CAP:
            _log.warning(f"[GUARDRAIL] {symbol} blocked: market cap ${market_cap/1e6:.0f}M < ${MIN_MARKET_CAP/1e6:.0f}M")
            if return_reason:
                return False, 'low_mcap'
            return False

        # RVOL gate (adaptive)
        if market_state is not None and market_state.is_market_open and bull:
            daily = get_daily_volume_bars(symbol)
            if not daily.empty and len(daily) >= 2:
                avg_daily_vol = float(daily["volume"].iloc[:-1].mean())
                if avg_daily_vol > 0:
                    now_et       = datetime.datetime.now(_ET)
                    mkt_open     = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
                    elapsed_min  = max((now_et - mkt_open).total_seconds() / 60, 1.0)
                    elapsed_frac = min(elapsed_min / 390.0, 1.0)
                    rvol = (true_day_vol / max(elapsed_frac, 0.02)) / avg_daily_vol
                    if rvol < adaptive_rvol:
                        _log.warning(f"[GUARDRAIL] {symbol} blocked: RVOL {rvol:.2f} < adaptive_rvol {adaptive_rvol:.2f} | day_vol={true_day_vol:.0f} | avg_daily_vol={avg_daily_vol:.0f}")
                        if return_reason:
                            return False, 'rvol'
                        return False

        # Stock-level fixed gap-chase threshold; broad-market regime and VIX
        # do not decide individual stock eligibility.
        adaptive_gap = MAX_GAP_CHASE_PCT

        # Gap-chase guard: skip if up >adaptive_gap% without a tight consolidation base.
        # Checked against BOTH today's tracked open (intraday chase) and the prior
        # close (overnight/pre-market gap) -- a stock that already gapped huge before
        # its first tracked bar of the day would show ~0% day_gain and slip through
        # the open_px check alone, since that check resets its baseline to the
        # already-elevated open.
        gap_vs_open  = ((price - open_px) / open_px) * 100 if open_px > 0 else 0.0
        gap_vs_prev  = ((price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
        if GAP_CHASE_GUARD_ENABLED and max(gap_vs_open, gap_vs_prev) > adaptive_gap:
            # Always check for consolidation on gapped-up stocks, even on snapshot path.
            # This is a critical risk-management gate to avoid chasing.
            # If intraday bars weren't fetched before, get them now.
            if intraday is None:
                intraday = get_bars(symbol, "1d", "1m")

            if intraday is not None and not intraday.empty and len(intraday) >= GAP_CHASE_CONSOL_BARS:
                last_n = intraday.iloc[-GAP_CHASE_CONSOL_BARS:]
                bar_range = float(last_n["high"].max() - last_n["low"].min())
                # If the range of the last few bars is > 2% of the price, it's not consolidating.
                if bar_range > price * 0.02:
                    _log.debug(f"[GUARDRAIL] {symbol} blocked: gap chase, bar range {bar_range:.2f} > 2% of price {price:.2f}")
                    if return_reason:
                        return False, 'gap_chase'
                    return False

        if return_reason:
            return True, None
        return True
    except Exception as e:
        _log.warning(f"Guardrail check failed for {symbol}: {e} -- skipping symbol")
        if return_reason:
            return False, 'other'
        return False  # fail-safe: block on error, never bypass guardrails


# 2026-08-26, user request ("thinly traded stocks should be removed from
# universe to avoid too much fetchload to alpaca" / "ensure thinly traded
# stocks removed from list"): a symbol used to sit in the scan target list
# with genuinely thin liquidity for its whole time there -- it got fully
# scanned (Alpaca intraday bars, snapshot prefetch, strategy evaluation)
# every cycle, and only got rejected AFTER all that work, at the
# _passes_guardrails() avg-daily-volume check. This pre-filters it out of
# get_scan_targets() entirely, using the same threshold/data source as that
# guardrail (get_daily_volume_bars, MIN_AVG_DAILY_VOLUME_REGULAR_HOURS) so
# it's not a new liquidity bar, just enforced earlier.
#
# Cached separately from get_daily_volume_bars()'s own cache, which
# clear_bar_cache() wipes every single scan cycle (1-3 min) -- 3-month
# average daily volume doesn't meaningfully change within an hour, so a
# 1-hour TTL here avoids re-fetching the same yfinance data every cycle.
_thin_check_cache: Dict[str, Tuple[float, bool]] = {}
_THIN_CHECK_TTL_SEC = 3600


def _is_thinly_traded(symbol: str) -> bool:
    """True if symbol's 3mo avg daily volume is below MIN_AVG_DAILY_VOLUME_REGULAR_HOURS.
    Fails OPEN (not thin) on missing/errored data -- same as the guardrail's
    own behavior, so a data hiccup doesn't wrongly prune a real symbol.
    """
    now = time.time()
    cached = _thin_check_cache.get(symbol)
    if cached is not None and (now - cached[0]) < _THIN_CHECK_TTL_SEC:
        return cached[1]
    is_thin = False
    try:
        daily = get_daily_volume_bars(symbol)
        if not daily.empty and len(daily) >= 2:
            avg_daily_vol = float(daily["volume"].iloc[:-1].mean())
            is_thin = avg_daily_vol < MIN_AVG_DAILY_VOLUME_REGULAR_HOURS
    except Exception as e:
        _log.debug(f"{symbol}: thin-liquidity pre-check failed, failing open: {e}")
    _thin_check_cache[symbol] = (now, is_thin)
    return is_thin


_UNIVERSE_HEALTH_LAST_LOG = 0.0  # monotonic ts of last universe-health notice (rate limiter)


def get_scan_targets(excluded: Optional[Set[str]] = None, market_state: Optional[MarketState] = None) -> List[str]:
    # 2026-09-02: rate-limit the universe-health notices (module-level state):
    # with TI_PRIMARY_TTL_MINUTES=125 the overnight TTL expiry made the
    # "ti_primary.json is empty" error fire on EVERY 5s scan cycle for hours
    # (10k+ identical log lines/day observed). Same deficiency logged at most
    # once per 5 minutes; the timer re-arms as soon as the universe recovers.
    global _UNIVERSE_HEALTH_LAST_LOG
    """Equity scan universe = Alpaca-movers queue + top TI_PRIMARY_SCAN_BATCH_LIMIT
    tickers from the latest Trade Ideas capture (data/ti_primary.json), TI in
    its own rank order.

    2026-08-27, user request ("the top 30 list should only keep after all
    the guardrails also passed not just ti scrapper and alpaca movers"):
    when market_state is provided, candidates also have to clear
    _passes_guardrails() (price/RVOL/dollar-volume/avg-volume/float/
    market-cap/gap-chase -- the same checks a signal has to pass to
    execute) BEFORE the [:TI_PRIMARY_SCAN_BATCH_LIMIT] cap, not just the
    existing dead/thin/excluded/delisted filter. Confirmed live: roughly
    half a 24-symbol roster was a guaranteed reject every single cycle
    (RVOL: FSLY/GTM/CLSK/UMAC/OMER/ZENA, price floor: GCTK/WKSP/UPXI, float
    floor: BTCT/YYGH/BIRD) -- those permanently occupied scan slots a real
    candidate further down TI's ranking never got a chance to fill.
    market_state is optional and defaults to the old dead/thin/excluded/
    delisted-only filter: two callers (enhanced.py's expiry/membership
    checks) ask "is this symbol still on the list" many times a minute and
    shouldn't pay for a full guardrail pass (snapshot prefetch, daily-volume/
    float/mcap lookups) just for that.

    2026-08-26, user request ("reduce the number of signals to what TI
    provides... top 20... the universe of stocks should limit to the latest
    trade ideas scrapping"): replaced the old multi-source assembly (EDGAR/
    sympathy/Alpaca-movers/watchlist priority queue, an inverse-ETF/
    BEAR_SHORT_UNIVERSE bear-regime seed, and an ~80-symbol rotating fallback
    universe -- routinely 90-150+ symbols/cycle, confirmed live) with just TI
    top-N. ti_primary.json is refreshed in place by the ApexTraderTICapture
    scheduled task (3 min 8:25-9:30 ET, 10 min 9:30-14:50 ET), so this
    naturally tracks TI's latest read all day -- no separate freeze/snapshot
    needed; whatever's newest in the file each cycle IS the universe.

    Same day, follow-up request ("remove edgar and sympathy but keep alpaca
    movers along with trade ideas.com"): Alpaca-movers added back in via its
    own dedicated queue (_alpaca_movers_queue / get_alpaca_movers_queue(),
    engine/equity/discovery.py) -- separate from the EDGAR/sympathy/watchlist
    queue, which stays excluded from equity scan entirely (the priority
    queue that once fed the options scan was removed 2026-09-01 with options
    trading). Backtest evidence for keeping movers: 2026-08-26 fills showed
    Alpaca-movers-sourced trades at 58.8% win rate / +$15.10 net vs.
    TI/other's 34.7% / -$17.82 (small sample, one outlier trade drove most of
    the movers P&L -- not a confident signal, just the reason this wasn't
    reverted with EDGAR/sympathy).

    Falls back to the static config lists (get_dynamic_universe) only when
    ti_primary.json is critically thin/empty (_MIN_TI) -- a TI-outage safety
    net, not a routine noise source. is_dead_ticker() (engine/utils/bars.py)
    still strips out names with persistent stale/empty data.
    """
    if excluded is None:
        excluded = set()

    delisted = set(_cfg.DELISTED_STOCKS)

    def _live(s: str) -> bool:
        return s not in excluded and s not in delisted and not is_dead_ticker(s) and not _is_thinly_traded(s)

    ti_primary = [s for s in _get_ti_primary() if s not in delisted]

    # Universe health check
    _MIN_TI = 5
    if len(ti_primary) < _MIN_TI:
        now_mono = time.monotonic()
        if now_mono - _UNIVERSE_HEALTH_LAST_LOG > 300.0:
            _UNIVERSE_HEALTH_LAST_LOG = now_mono
            if len(ti_primary) == 0:
                _log.error("[UNIVERSE HEALTH] ti_primary.json is empty! No tickers to scan. Check data pipeline.")
            else:
                _log.warning(f"[UNIVERSE HEALTH] ti_primary.json too small ({len(ti_primary)}). Falling back to static config lists.")
        p1, p2, _ = _cfg.get_dynamic_universe()
        ti_pool = list(dict.fromkeys(p2 + p1))
        if len(ti_pool) == 0:
            _log.error("[UNIVERSE HEALTH] Static universe lists are empty! No tickers to scan. Check config/universe sources.")
    else:
        _UNIVERSE_HEALTH_LAST_LOG = 0.0  # healthy again -> next deficiency logs immediately
        ti_pool = list(dict.fromkeys(ti_primary))

    movers = [s for s in _get_alpaca_movers_queue() if s not in delisted]
    # 2026-08-27, user request ("the top 30 list seems to be not aligned with
    # the morning runners"): filter dead/thin/excluded BEFORE capping to 30,
    # not after -- with the movers queue no longer TTL-evicted mid-day (see
    # scan_alpaca_movers), it can hold more names for longer, so a handful of
    # names that went dead earlier in the session could otherwise fill scan
    # slots the cap should have handed to live TI candidates instead, with no
    # backfill. is_dead_ticker/_is_thinly_traded/excluded/delisted are now
    # applied here, per-source, before either the merge or the cap.
    movers = [s for s in movers if _live(s)]
    ti_pool = [s for s in ti_pool if _live(s)]

    if market_state is not None:
        pool = list(dict.fromkeys(movers + ti_pool))
        try:
            _prefetch_snapshots(pool)
        except Exception as e:
            _log.debug(f"[GUARDRAIL] pre-filter snapshot prefetch failed, guardrail checks fail open: {e}")
        def _quality_ok(s: str) -> bool:
            try:
                result = _passes_guardrails(s, market_state=market_state, return_reason=True)
                if isinstance(result, tuple):
                    return bool(result[0])
                return bool(result)
            except Exception as e:
                _log.warning(f"{s}: guardrail pre-filter failed, excluding from top-{_cfg.TI_PRIMARY_SCAN_BATCH_LIMIT}: {e}")
                return False

        # Guardrail reads are independent network/data lookups. Run them in
        # parallel so a long raw TI list cannot delay the active snapshot
        # write past the scan timeout; source order is preserved below.
        # 2026-09-02: explicit executor + per-item and total budgets instead of
        # `with ... pool.map(...)` -- map() has no per-item timeout and the
        # context manager's shutdown(wait=True) froze the whole main loop
        # when a guardrail worker hung on a provider lock (seen live: log
        # silent, CPU 0, no rescue possible). A wedged symbol now costs at
        # most its own timeout; a wedged pool costs at most the total budget.
        def _filter_quality(items: List[str]) -> List[str]:
            index = {s: i for i, s in enumerate(items)}
            keep: Dict[int, str] = {}
            pool = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
            try:
                futs = {pool.submit(_quality_ok, s): s for s in items}
                try:
                    for fut in as_completed(futs, timeout=_SCAN_TOTAL_BUDGET_SEC):
                        s = futs[fut]
                        try:
                            if fut.result(timeout=SCAN_SYMBOL_TIMEOUT):
                                keep[index[s]] = s
                        except Exception as e:
                            _log.warning(f"{s}: guardrail pre-filter failed, excluding from top-{_cfg.TI_PRIMARY_SCAN_BATCH_LIMIT}: {e}")
                except TimeoutError:
                    _log.warning("[SCAN] guardrail pre-filter exceeded total budget -- proceeding with completed checks")
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            return [s for _, s in sorted(keep.items())]

        movers = _filter_quality(movers)
        ti_pool = _filter_quality(ti_pool)

    ti_slice = ti_pool[:_cfg.TI_PRIMARY_SCAN_BATCH_LIMIT]

    # 2026-08-28: preserve a healthy merged movers + Yahoo universe instead of
    # collapsing to a tiny final list. Keep the active scan footprint broad
    # enough that both a long and short book can stay populated.
    combined = list(dict.fromkeys(movers + ti_slice))
    reserved = sorted(_DEFAULT_SCAN_SYMBOLS)
    ranked = [s for s in combined if s not in _DEFAULT_SCAN_SYMBOLS]
    # use the same merged source order for both directional books; cap per side
    # at 30 while still letting the shared scan pool remain broad enough to trade.
    target_cap = max(30, _cfg.TI_PRIMARY_SCAN_BATCH_LIMIT)
    targets = ranked[:max(0, target_cap - len(reserved))] + reserved
    # If the merged pool is still above the cap, prefer the healthiest-ranked names
    # already present in the merged list rather than leaving the long/short books sparse.
    targets = list(dict.fromkeys(targets))[:max(30, target_cap)]
    if market_state is not None:
        _write_active_scan_list(targets)
    return targets


def scan_universe(scan_targets: List[str], sentiment: str, market_state: MarketState) -> Tuple[List, Dict[str, int], int]:
    clear_bar_cache()

    # Batch-prefetch stock snapshots for all scan targets in one API call.
    # Populates _snapshot_cache so _passes_guardrails() avoids per-symbol
    # get_bars("1d","1m") requests -- the dominant I/O cost of each scan cycle.
    _prefetch_snapshots(scan_targets)

    # Direction is determined by each stock's own strategy conditions.
    regime_str = "stock"
    strats = get_strategy_instances()


    signals = []
    hit_counts = {}
    scan_errors = 0
    guardrail_rejections = {
        'dollar_vol': 0,
        'rvol': 0,
        'gap_chase': 0,
        'min_price': 0,
        'avg_volume': 0,
        'low_float': 0,
        'low_mcap': 0,
        'other': 0
    }
    thin_liquidity_stats = {'admitted': 0}  # rejected-list symbols scanned anyway; see TRADE_THIN_LIQUIDITY_REJECTS

    def _scan_one(symbol: str):
        # Dead-ticker check already done in get_scan_targets() -- skip here.
        # Pass pre-computed regime into guardrails to avoid re-calling _is_bull_regime()
        # Custom: get rejection reason from _passes_guardrails
        guardrail_result = _passes_guardrails(symbol, market_state=market_state, return_reason=True)
        if isinstance(guardrail_result, tuple):
            passed, reason = guardrail_result
        else:
            passed = bool(guardrail_result)
            reason = 'other'
        thin_liquidity = False
        if not passed:
            if reason in guardrail_rejections:
                guardrail_rejections[reason] += 1
            else:
                guardrail_rejections['other'] += 1
            # Rejection itself is unchanged and still counted above -- this is a
            # separate, toggleable path on top of it: a symbol rejected for ONLY
            # thin float/volume still gets scanned, just flagged so _execute_entry
            # sizes it at THIN_LIQUIDITY_POSITION_SIZE_PCT instead of skipping it
            # outright. min_price/RVOL/dollar_vol/mcap/gap_chase are never rescued.
            if _should_admit_thin_liquidity(reason, market_state):
                thin_liquidity = True
                thin_liquidity_stats['admitted'] += 1
            else:
                return None

        candidates = []
        for s in strats:
            try:
                if isinstance(s, TechnicalStrategy):
                    sig = s.scan(symbol, sentiment)
                elif isinstance(s, SentimentStrategy):
                    sig = s.scan(symbol, sentiment)
                elif isinstance(s, MomentumStrategy):
                    sig = s.scan(symbol, regime_str)
                else:
                    sig = s.scan(symbol)
                if sig:
                    candidates.append(sig)
            except Exception as _ex:
                _log.debug(f"[SCAN] {symbol} {type(s).__name__}: {_ex}")

        if not candidates:
            best = _top_list_signal(symbol)
            if best is None:
                return None
        else:
            # User-requested order: GapBreakout/ORB first, then every other
            # real strategy. Confidence remains the tie-breaker within a tier.
            # TopList stays fallback-only in the branch above.
            best = max(candidates, key=_strategy_selection_rank)
        if thin_liquidity:
            # 2026-08-15: the guardrail-admit decision above happens before we
            # know which strategy will actually fire (it's a symbol-level gate,
            # strategies get scanned after). ORB/GapBreakout measured net-
            # negative specifically on their bypass trades (see
            # THIN_LIQUIDITY_EXCLUDED_STRATEGIES in config.py) -- now that
            # `best` tells us the winning strategy, drop the signal entirely
            # for those two instead of admitting it at reduced size.
            if best.strategy in THIN_LIQUIDITY_EXCLUDED_STRATEGIES:
                return None
            best.thin_liquidity = True

        # Per-symbol HMM regime alignment: confidence bonus only, never a gate.
        # Buys get a boost when the symbol's own 2-state HMM regime is bullish;
        # shorts/sells get it when that regime is bearish.
        hmm_bull = get_hmm_regime(symbol, HMM_REGIME_LOOKBACK_DAYS)
        if hmm_bull is not None:
            aligned = (best.action == "buy" and hmm_bull) or (best.action in ("sell", "short") and not hmm_bull)
            if aligned:
                best.confidence = round(min(best.confidence + HMM_REGIME_CONFIDENCE_BOOST, 0.97), 3)

        return best


    # 2026-09-02: explicit executor instead of `with` -- the context manager's
    # shutdown(wait=True) freezes the whole main loop if a worker hangs (seen
    # live: log silent, CPU 0, heartbeat not written, worker stuck on a
    # provider lock). futures already time out individually below; when the
    # loop ends, abandon any still-running worker and move on -- the watchdog's
    # stall-restart handles a genuinely wedged scan, but a slow symbol must
    # never hold the market-cycle hostage.
    pool = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
    try:
        future_map = {pool.submit(_scan_one, sym): sym for sym in scan_targets}
        # Total-cycle budget: as_completed() alone blocks until the FIRST
        # future completes, so an all-workers-hung scan would never reach the
        # finally below. Bound the whole collection loop instead.
        try:
            for future in as_completed(future_map, timeout=_SCAN_TOTAL_BUDGET_SEC):
                sym = future_map[future]
                try:
                    sig = future.result(timeout=SCAN_SYMBOL_TIMEOUT)
                    if sig:
                        signals.append(sig)
                        hit_counts[sig.strategy] = hit_counts.get(sig.strategy, 0) + 1
                except Exception as e:
                    scan_errors += 1
                    _log.error(f"[SCAN ERROR] {sym}: {e}")
        except TimeoutError:
            _log.warning("[SCAN] total scan budget exceeded -- abandoning slow workers and proceeding")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # Log guardrail rejection summary
    total_rejected = sum(guardrail_rejections.values())
    if total_rejected > 0:
        _log.info(
            f"[GUARDRAIL SUMMARY] Rejected: {total_rejected} | DollarVol: {guardrail_rejections['dollar_vol']} | "
            f"RVOL: {guardrail_rejections['rvol']} | GapChase: {guardrail_rejections['gap_chase']} | "
            f"MinPrice: {guardrail_rejections['min_price']} | AvgVolume: {guardrail_rejections['avg_volume']} | "
            f"LowFloat: {guardrail_rejections['low_float']} | "
            f"LowMcap: {guardrail_rejections['low_mcap']} | "
            f"Other: {guardrail_rejections['other']}"
            + (f" | ThinLiquidityAdmitted: {thin_liquidity_stats['admitted']}" if thin_liquidity_stats['admitted'] else "")
        )

    signals.sort(key=lambda x: x.confidence, reverse=True)
    # Adaptive confidence filter using pre-intelligence (market regime, VIX)
    base_conf = MIN_SIGNAL_CONFIDENCE
    adaptive_conf = base_conf
    signals = [s for s in signals if s.confidence >= adaptive_conf]

    # Dynamic sector/industry weighting cap
    # Limit to max 3 signals per sector (can be tuned)
    from collections import defaultdict
    sector_cap = 3
    sector_counts = defaultdict(int)
    filtered_signals = []
    for sig in signals:
        sector = getattr(sig, 'sector', None)
        if sector is None:
            filtered_signals.append(sig)  # If no sector info, allow
            continue
        if sector_counts[sector] < sector_cap:
            filtered_signals.append(sig)
            sector_counts[sector] += 1
    signals = filtered_signals
    if LONG_ONLY_MODE:
        # Long-only enforcement: drop sell/short signals only when LONG_ONLY_MODE is active
        pre_len = len(signals)
        signals = [s for s in signals if s.action == "buy"]
        if len(signals) != pre_len:
            _log.info(f"Long-only enforced in scan_universe: dropping {pre_len-len(signals)} short signals")

    # Adaptive filter logic: relax after N empty scans, reset after success
    if len(signals) == 0:
        _adaptive_state["empty_scans"] += 1
        if _adaptive_state["empty_scans"] >= _ADAPTIVE_MAX_EMPTY:
            # Relax RVOL and confidence stepwise
            if _adaptive_state["rvol_min"] > _ADAPTIVE_MIN_RVOL:
                _adaptive_state["rvol_min"] = max(_ADAPTIVE_MIN_RVOL, _adaptive_state["rvol_min"] - _ADAPTIVE_STEP_RVOL)
                _log.info(f"[ADAPTIVE] Lowered RVOL_MIN to {_adaptive_state['rvol_min']:.2f}")
            if _adaptive_state["min_conf"] > _ADAPTIVE_MIN_CONF:
                _adaptive_state["min_conf"] = max(_ADAPTIVE_MIN_CONF, _adaptive_state["min_conf"] - _ADAPTIVE_STEP_CONF)
                _log.info(f"[ADAPTIVE] Lowered MIN_SIGNAL_CONFIDENCE to {_adaptive_state['min_conf']:.2f}")
    else:
        if _adaptive_state["empty_scans"] > 0:
            _log.info(f"[ADAPTIVE] Resetting adaptive filters after successful scan.")
        _adaptive_state["empty_scans"] = 0
        _adaptive_state["rvol_min"] = RVOL_MIN
        _adaptive_state["min_conf"] = MIN_SIGNAL_CONFIDENCE
    return signals, hit_counts, scan_errors


def _demo() -> None:
    """Self-check for get_scan_targets()'s market_state-gated guardrail
    pre-filter (2026-08-27, user request: "the top 30 list should only keep
    after all the guardrails also passed not just ti scrapper and alpaca
    movers"). Monkeypatches the module's own network-touching globals so
    this runs with no live calls; restored in a finally."""
    global _get_ti_primary, _get_alpaca_movers_queue, is_dead_ticker, _is_thinly_traded
    global _prefetch_snapshots, _passes_guardrails, _DEFAULT_SCAN_SYMBOLS, _write_active_scan_list
    _orig = (_get_ti_primary, _get_alpaca_movers_queue, is_dead_ticker, _is_thinly_traded,
               _prefetch_snapshots, _passes_guardrails, _DEFAULT_SCAN_SYMBOLS,
               _write_active_scan_list)
    try:
        _DEFAULT_SCAN_SYMBOLS = set()
        _write_active_scan_list = lambda tickers: False
        # GOOD passes every check; RVOLBAD/PRICEBAD/FLOATBAD each fail one
        # of the structural guardrails -- exactly the pattern confirmed live
        # 2026-08-27 (FSLY/GTM/etc on RVOL, GCTK/WKSP/UPXI on price,
        # BTCT/YYGH/BIRD on float).
        _get_ti_primary = lambda: ["RVOLBAD", "GOOD1", "PRICEBAD", "GOOD2", "FLOATBAD", "GOOD3"]
        _get_alpaca_movers_queue = lambda: []
        is_dead_ticker = lambda s: False
        _is_thinly_traded = lambda symbol: False
        _prefetch_snapshots = lambda symbols: None
        _passes_guardrails = lambda symbol, bull_regime=None, market_state=None, return_reason=False: not symbol.endswith("BAD")

        # market_state=None (existing callers that don't have one) -> old
        # behavior, guardrails not applied at all.
        targets = get_scan_targets(market_state=None)
        assert targets == ["RVOLBAD", "GOOD1", "PRICEBAD", "GOOD2", "FLOATBAD", "GOOD3"], \
            f"market_state=None must skip the guardrail pre-filter entirely, got {targets}"

        # market_state provided -> the *BAD candidates must be filtered out
        # before the cap, not just left in place.
        class _FakeMarketState(MarketState):
            def __init__(self):
                super().__init__(
                    now=datetime.datetime.now(_ET),
                    hour=float(datetime.datetime.now(_ET).hour),
                    weekday=True,
                    is_market_open=True,
                    is_regular_hours=True,
                    is_open_window=False,
                )

            def resolve_regime(self): return True

        targets = get_scan_targets(market_state=_FakeMarketState())
        assert targets == ["GOOD1", "GOOD2", "GOOD3"], \
            f"guardrail-failing candidates must be dropped before the cap when market_state is given, got {targets}"

        # A doomed candidate ranked ABOVE good ones must not consume a scan
        # slot that a lower-ranked-but-tradeable candidate could fill --
        # the whole point of filtering before the cap, not after.
        orig_limit = _cfg.TI_PRIMARY_SCAN_BATCH_LIMIT
        _cfg.TI_PRIMARY_SCAN_BATCH_LIMIT = 2
        try:
            targets = get_scan_targets(market_state=_FakeMarketState())
            assert targets == ["GOOD1", "GOOD2"], \
                f"a doomed top-ranked candidate must not occupy a capped slot, got {targets}"
        finally:
            _cfg.TI_PRIMARY_SCAN_BATCH_LIMIT = orig_limit

        print("scan._demo: all assertions passed")
    finally:
        (_get_ti_primary, _get_alpaca_movers_queue, is_dead_ticker, _is_thinly_traded,
         _prefetch_snapshots, _passes_guardrails, _DEFAULT_SCAN_SYMBOLS,
         _write_active_scan_list) = _orig


if __name__ == "__main__":
    _demo()

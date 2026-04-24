# Adaptive equity allocation based on pre-intelligence (market regime, signal quality, or pre-market indicators)
def get_adaptive_equity_allocation(market_state: MarketState, avg_signal_conf: float = None, premarket_strength: float = None) -> float:
    """
    Returns adaptive position size percentage for equities based on pre-intelligence.
    - In strong bull regime or high signal confidence, increase allocation.
    - In bear regime or weak signals, decrease allocation.
    - Optionally, use premarket_strength (0-1) if available.
    """
    from engine.config import POSITION_SIZE_PCT
    from engine.utils.market import get_allocation_split
    base = POSITION_SIZE_PCT
    equity_pct, _ = get_allocation_split(market_state)
    base *= equity_pct
    # Example logic: scale up in bull, down in bear
    if hasattr(market_state, 'resolve_regime'):
        bull = market_state.resolve_regime()
        if bull:
            base *= 1.2  # 20% more aggressive in bull
        else:
            base *= 0.8  # 20% more conservative in bear
    # If average signal confidence is provided, scale further
    if avg_signal_conf is not None:
        if avg_signal_conf > 0.85:
            base *= 1.15
        elif avg_signal_conf < 0.75:
            base *= 0.85
    # If premarket_strength is provided (0-1), scale linearly between 0.8x and 1.2x
    if premarket_strength is not None:
        base *= (0.8 + 0.4 * premarket_strength)
    # Clamp to reasonable bounds (e.g., 3% to 15%)
    return max(3.0, min(base, 15.0))
"""ApexTrader scan nucleus.

Contains reusable scanning functions for main loop and run_top3 tools.
"""

import datetime
import logging
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Tuple, Set, Optional

from engine import config as _cfg
from engine.config import (
    SCAN_MAX_SYMBOLS,
    SCAN_WORKERS,
    SCAN_SYMBOL_TIMEOUT,
    MIN_DOLLAR_VOLUME,
    MIN_STOCK_PRICE,
    LONG_ONLY_MODE,
    MIN_SIGNAL_CONFIDENCE,
    MAX_SIGNALS_PER_CYCLE,
    RVOL_MIN,
    MAX_GAP_CHASE_PCT,
    GAP_CHASE_CONSOL_BARS,
    BEAR_SHORT_UNIVERSE,
)
from engine.utils import MarketState, clear_bar_cache, get_bars, is_dead_ticker
from engine.utils.bars import get_data_client as _get_data_client
from alpaca.data import StockSnapshotRequest as _StockSnapshotRequest
from .universe import get_tier as _get_tier_live, get_latest_batch as _get_latest_batch, get_ti_primary as _get_ti_primary
from .discovery import get_priority_scan_queue as _get_priority_scan_queue

_ET  = pytz.timezone("America/New_York")
_log = logging.getLogger("ApexTrader")

# ── Adaptive Filter State ──
_adaptive_state = {
    "empty_scans": 0,
    "rvol_min": RVOL_MIN,
    "min_conf": MIN_SIGNAL_CONFIDENCE,
}
_ADAPTIVE_MAX_EMPTY = 3  # Number of empty scans before relaxing
_ADAPTIVE_MIN_RVOL = 1.2
_ADAPTIVE_MIN_CONF = 0.60
_ADAPTIVE_STEP_RVOL = 0.2
_ADAPTIVE_STEP_CONF = 0.03
from .strategies import get_strategy_instances, MomentumStrategy, TechnicalStrategy, SentimentStrategy
from engine.utils.market import _is_bull_regime, _INVERSE_ETFS

# Rotating scan offset — advances by SCAN_MAX_SYMBOLS each call so different
# slices of the universe are covered across consecutive cycles.
_scan_offset: int = 0

# ── Batch snapshot cache ──────────────────────────────────────────────────────
# Populated once at the start of each scan_universe() call via a single
# batch request.  _passes_guardrails() reads from this cache to avoid
# per-symbol 1-minute bars requests (390 bars × N symbols = dominant I/O cost).
_snapshot_cache: Dict = {}


def _prefetch_snapshots(symbols: List[str]) -> None:
    """Batch-fetch stock snapshots for *symbols* and store in _snapshot_cache.

    A single API call replaces N individual get_bars("1d","1m") requests in
    _passes_guardrails(), reducing scan latency significantly for large universes.
    Failures are silently swallowed — _passes_guardrails() falls back to bars.
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


def _passes_guardrails(symbol: str, bull_regime: bool = None, market_state: Optional[MarketState] = None) -> bool:
    """Pre-scan gates: dollar-volume, RVOL, and gap-chase guard.
    Returns False to skip the symbol; never raises.

    bull_regime: pass the pre-computed regime from scan_universe() to avoid
    a concurrent re-fetch of _is_bull_regime() inside each worker thread.
    If None, falls back to calling _is_bull_regime() directly.

    market_state: shared MarketState for the current scan cycle. If None,
    it will be created lazily.
    """
    try:
        # ── Fast path: use batch-prefetched snapshot (no per-symbol HTTP call) ─
        _snap = _snapshot_cache.get(symbol)
        if (
            _snap is not None
            and _snap.daily_bar is not None
            and _snap.latest_trade is not None
        ):
            price   = float(_snap.latest_trade.price)
            day_vol = float(_snap.daily_bar.volume)
            open_px = float(_snap.daily_bar.open)
        else:
            # ── Fallback: fetch 1-min intraday bars ───────────────────────────
            intraday = get_bars(symbol, "1d", "1m")
            if intraday.empty or len(intraday) < 5:
                return True  # not enough data — let strategies decide
            price   = float(intraday["close"].iloc[-1])
            day_vol = float(intraday["volume"].sum())
            open_px = float(intraday["open"].iloc[0])

        # Minimum price gate — skip penny stocks (poor fills, wide spreads)
        if price < MIN_STOCK_PRICE:
            _log.debug(f"[GUARDRAIL] {symbol} blocked: price {price:.2f} < MIN_STOCK_PRICE {MIN_STOCK_PRICE}")
            return False

        # Dollar-volume gate
        dollar_vol = price * day_vol
        if dollar_vol < MIN_DOLLAR_VOLUME:
            _log.debug(f"[GUARDRAIL] {symbol} blocked: dollar volume {dollar_vol:.0f} < MIN_DOLLAR_VOLUME {MIN_DOLLAR_VOLUME}")
            return False

        # RVOL gate: only meaningful during regular market hours
        # In bear regime, skip RVOL gate — breakdown volume is often distributed,
        # not the spike pattern seen in squeeze/momentum setups.
        _bull = bull_regime if bull_regime is not None else market_state.resolve_regime()
        adaptive_rvol = _adaptive_state.get("rvol_min", RVOL_MIN)
        if market_state.is_market_open and _bull:
            daily = get_bars(symbol, "5d", "1d")
            if not daily.empty and len(daily) >= 2:
                avg_daily_vol = float(daily["volume"].iloc[:-1].mean())
                if avg_daily_vol > 0:
                    now_et       = datetime.datetime.now(_ET)
                    mkt_open     = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
                    elapsed_min  = max((now_et - mkt_open).total_seconds() / 60, 1.0)
                    elapsed_frac = min(elapsed_min / 390.0, 1.0)
                    rvol = (day_vol / max(elapsed_frac, 0.02)) / avg_daily_vol
                    if rvol < adaptive_rvol:
                        _log.debug(f"[GUARDRAIL] {symbol} blocked: RVOL {rvol:.2f} < adaptive_rvol {adaptive_rvol}")
                        return False

        # Gap-chase guard: skip if up >MAX_GAP_CHASE_PCT% without a tight consolidation base
        if open_px > 0:
            day_gain = ((price - open_px) / open_px) * 100
            if day_gain > MAX_GAP_CHASE_PCT:
                # When 1-min bars are available, require tight recent consolidation.
                # With snapshot-only data, skip the consolidation check (conservative:
                # allows the signal — strategy-level filters apply next).
                _snap_fast_path = _snapshot_cache.get(symbol) is not None and \
                    _snapshot_cache[symbol].daily_bar is not None and \
                    _snapshot_cache[symbol].latest_trade is not None
                if not _snap_fast_path:
                    last_n    = intraday.iloc[-GAP_CHASE_CONSOL_BARS:]
                    bar_range = float(last_n["high"].max() - last_n["low"].min())
                    if bar_range > price * 0.02:  # range > 2% = no consolidation
                        _log.debug(f"[GUARDRAIL] {symbol} blocked: gap chase, bar range {bar_range:.2f} > 2% of price {price:.2f}")
                        return False

        return True
    except Exception as e:
        _log.warning(f"Guardrail check failed for {symbol}: {e} — skipping symbol")
        return False  # fail-safe: block on error, never bypass guardrails


def get_scan_targets(excluded: Set[str] = None) -> List[str]:
    global _scan_offset

    if excluded is None:
        excluded = set()

    delisted = set(_cfg.DELISTED_STOCKS)

    # PRIMARY: latest captured TI tickers from ti_primary.json.
    # FALLBACK: active TI tickers from universe.json tiers 1+2.
    ti_primary = [s for s in _get_ti_primary() if s not in delisted]

    # FALLBACK: static config lists — used only when TI universe is empty.
    _MIN_TI = 5
    if len(ti_primary) < _MIN_TI:
        _log.warning(
            f"TI primary universe too small ({len(ti_primary)}) — falling back to static config lists"
        )
        p1, p2, _ = _cfg.get_dynamic_universe()
    else:
        p1, p2 = ti_primary, []

    # live_p1/p2 = same as p1/p2 (TI-primary); kept for TI_FRONT push below.
    live_p1 = p1
    live_p2 = p2

    # Limit how many fresh TI primary tickers are guaranteed into each cycle.
    # This keeps the scan set tight and avoids scanning excessively large TI batches.
    max_fresh = min(_cfg.TI_PRIMARY_SCAN_BATCH_LIMIT, SCAN_MAX_SYMBOLS)
    latest_batch = ti_primary[:max_fresh] if ti_primary else [
        s for s in _get_latest_batch(window_minutes=5)
        if s not in delisted
    ]
    latest_batch = list(dict.fromkeys(latest_batch))[:max_fresh]

    # Rotate through the combined universe so every cycle scans a different slice.
    # TI-promoted tickers sit at the front and always make it in regardless of offset.
    combined_base = p2 + p1   # tier-2 first in bear, then tier-1
    slice_size = max(SCAN_MAX_SYMBOLS * 2, len(combined_base))   # rotation window
    if len(combined_base) > 0:
        off = _scan_offset % len(combined_base)
        rotated_base = combined_base[off:] + combined_base[:off]
    else:
        rotated_base = combined_base
    _scan_offset = (_scan_offset + SCAN_MAX_SYMBOLS) % max(len(combined_base), 1)

    # Default slice: top 50% from each list (marketscope360 + highshortfloat)
    p1_slice = p1[:max(1, len(p1) // 2)]
    p2_slice = p2[:max(1, len(p2) // 2)]

    in_bear = not _is_bull_regime()
    targets = []
    seen = set()

    # Inverse ETFs guaranteed first in bear — they profit from market decline
    # and are valid LONG buys with LONG_ONLY_MODE=True.

    def _push(symbols: List[str], limit: int = None) -> None:
        for s in symbols:
            if limit is not None and len(targets) >= limit:
                break
            if s in seen or s in excluded or s in delisted:
                continue
            if is_dead_ticker(s):
                continue
            seen.add(s)
            targets.append(s)

    # TI-latest-batch guarantee: push a capped slice of TI primary tickers so
    # they appear in every cycle regardless of universe size.
    # NOTE: do NOT override the max_fresh cap here — the original assignment above
    # already limits to TI_PRIMARY_SCAN_BATCH_LIMIT so BEAR_SHORT_UNIVERSE has room.
    if not latest_batch:
        # Fallback: guarantee top-N newest from each tier
        TI_FRONT = 10
        latest_batch = list(dict.fromkeys(live_p1[:TI_FRONT] + live_p2[:TI_FRONT]))

    if in_bear:
        # Always seed with inverse ETFs first — they are valid longs in bear regime
        _push(_INVERSE_ETFS)
        # Sympathy + EDGAR tickers queued this cycle — push before TI batch
        _push(_get_priority_scan_queue())
        # Push capped TI batch (respects max_fresh so bear-universe gets slots)
        _push(latest_batch)
        # Guarantee bear short universe symbols get into every bear cycle scan.
        # These large/mid-cap names are what BearBreakdownStrategy fires on.
        # Use SCAN_MAX_SYMBOLS as the ceiling so all slots up to the max are filled.
        _push(list(BEAR_SHORT_UNIVERSE), limit=SCAN_MAX_SYMBOLS)
        # Fill any remaining capacity from the rotating universe
        _push(rotated_base, limit=SCAN_MAX_SYMBOLS)
    else:
        # Bull/neutral: sympathy/EDGAR tickers first, then latest batch + rotating universe
        _push(_get_priority_scan_queue())
        _push(latest_batch)
        _push(rotated_base, limit=SCAN_MAX_SYMBOLS)
        if len(targets) < SCAN_MAX_SYMBOLS:
            _push(p1_slice + p2_slice, limit=SCAN_MAX_SYMBOLS)

    return targets


def scan_universe(scan_targets: List[str], sentiment: str, market_state: MarketState) -> Tuple[List, Dict[str, int], int]:
    clear_bar_cache()

    # Batch-prefetch stock snapshots for all scan targets in one API call.
    # Populates _snapshot_cache so _passes_guardrails() avoids per-symbol
    # get_bars("1d","1m") requests — the dominant I/O cost of each scan cycle.
    _prefetch_snapshots(scan_targets)

    # Compute regime ONCE here before spawning workers — avoids a thread race where
    # multiple workers concurrently hit the 15-min TTL expiry and each make a
    # separate get_bars("SPY") call to refresh the shared _regime_cache dict.
    bull_regime = market_state.resolve_regime()
    regime_str  = "bull" if bull_regime else "bear"
    strats = get_strategy_instances(bull_regime)

    signals = []
    hit_counts = {}
    scan_errors = 0

    def _scan_one(symbol: str):
        # Dead-ticker check already done in get_scan_targets() — skip here.
        # Pass pre-computed regime into guardrails to avoid re-calling _is_bull_regime()
        if not _passes_guardrails(symbol, bull_regime=bull_regime, market_state=market_state):
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
            return None
        return max(candidates, key=lambda s: s.confidence)

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        future_map = {pool.submit(_scan_one, sym): sym for sym in scan_targets}
        for future in as_completed(future_map):
            sym = future_map[future]
            try:
                sig = future.result(timeout=SCAN_SYMBOL_TIMEOUT)
                if sig:
                    signals.append(sig)
                    hit_counts[sig.strategy] = hit_counts.get(sig.strategy, 0) + 1
            except Exception:
                scan_errors += 1

    signals.sort(key=lambda x: x.confidence, reverse=True)
    # Adaptive confidence filter
    adaptive_conf = _adaptive_state.get("min_conf", MIN_SIGNAL_CONFIDENCE)
    signals = [s for s in signals if s.confidence >= adaptive_conf]
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


def filter_signals(signals, long_only: bool = False, min_conf: float = 0.0, cap: int = None):
    if long_only:
        signals = [s for s in signals if s.action == "buy"]

    signals = [s for s in signals if s.confidence >= min_conf]

    if cap is not None:
        signals = signals[:cap]
    return signals

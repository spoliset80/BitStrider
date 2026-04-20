"""ApexTrader scan nucleus.

Contains reusable scanning functions for main loop and run_top3 tools.
"""

import datetime
import logging
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Tuple, Set

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
from engine.utils import clear_bar_cache, get_bars, is_market_open, is_dead_ticker
from .universe import get_tier as _get_tier_live, get_latest_batch as _get_latest_batch, get_ti_primary as _get_ti_primary
from .discovery import get_priority_scan_queue as _get_priority_scan_queue

_ET  = pytz.timezone("America/New_York")
_log = logging.getLogger("ApexTrader")
from .strategies import get_strategy_instances, MomentumStrategy, TechnicalStrategy, SentimentStrategy, _is_bull_regime, _INVERSE_ETFS

# Rotating scan offset — advances by SCAN_MAX_SYMBOLS each call so different
# slices of the universe are covered across consecutive cycles.
_scan_offset: int = 0


def _passes_guardrails(symbol: str, bull_regime: bool = None) -> bool:
    """Pre-scan gates: dollar-volume, RVOL, and gap-chase guard.
    Returns False to skip the symbol; never raises.

    bull_regime: pass the pre-computed regime from scan_universe() to avoid
    a concurrent re-fetch of _is_bull_regime() inside each worker thread.
    If None, falls back to calling _is_bull_regime() directly.
    """
    try:
        intraday = get_bars(symbol, "1d", "1m")
        if intraday.empty or len(intraday) < 5:
            return True  # not enough data — let strategies decide

        price   = float(intraday["close"].iloc[-1])
        day_vol = float(intraday["volume"].sum())

        # Minimum price gate — skip penny stocks (poor fills, wide spreads)
        if price < MIN_STOCK_PRICE:
            return False

        # Dollar-volume gate
        if price * day_vol < MIN_DOLLAR_VOLUME:
            return False

        # RVOL gate: only meaningful during regular market hours
        # In bear regime, skip RVOL gate — breakdown volume is often distributed,
        # not the spike pattern seen in squeeze/momentum setups.
        _bull = bull_regime if bull_regime is not None else _is_bull_regime()
        if is_market_open() and _bull:
            daily = get_bars(symbol, "5d", "1d")
            if not daily.empty and len(daily) >= 2:
                avg_daily_vol = float(daily["volume"].iloc[:-1].mean())
                if avg_daily_vol > 0:
                    now_et       = datetime.datetime.now(_ET)
                    mkt_open     = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
                    elapsed_min  = max((now_et - mkt_open).total_seconds() / 60, 1.0)
                    elapsed_frac = min(elapsed_min / 390.0, 1.0)
                    rvol = (day_vol / max(elapsed_frac, 0.02)) / avg_daily_vol
                    if rvol < RVOL_MIN:
                        return False

        # Gap-chase guard: skip if up >MAX_GAP_CHASE_PCT% without a tight consolidation base
        open_px = float(intraday["open"].iloc[0])
        if open_px > 0:
            day_gain = ((price - open_px) / open_px) * 100
            if day_gain > MAX_GAP_CHASE_PCT:
                last_n    = intraday.iloc[-GAP_CHASE_CONSOL_BARS:]
                bar_range = float(last_n["high"].max() - last_n["low"].min())
                if bar_range > price * 0.02:  # range > 2% = no consolidation
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


def scan_universe(scan_targets: List[str], sentiment: str) -> Tuple[List, Dict[str, int], int]:
    clear_bar_cache()

    # Compute regime ONCE here before spawning workers — avoids a thread race where
    # multiple workers concurrently hit the 15-min TTL expiry and each make a
    # separate get_bars("SPY") call to refresh the shared _regime_cache dict.
    bull_regime = _is_bull_regime()
    regime_str  = "bull" if bull_regime else "bear"
    strats = get_strategy_instances(bull_regime)

    signals = []
    hit_counts = {}
    scan_errors = 0

    def _scan_one(symbol: str):
        # Dead-ticker check already done in get_scan_targets() — skip here.
        # Pass pre-computed regime into guardrails to avoid re-calling _is_bull_regime()
        if not _passes_guardrails(symbol, bull_regime=bull_regime):
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
            except Exception:
                pass

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
    if LONG_ONLY_MODE:
        # Long-only enforcement: drop sell/short signals only when LONG_ONLY_MODE is active
        pre_len = len(signals)
        signals = [s for s in signals if s.action == "buy"]
        if len(signals) != pre_len:
            _log.info(f"Long-only enforced in scan_universe: dropping {pre_len-len(signals)} short signals")
    return signals, hit_counts, scan_errors


def filter_signals(signals, long_only: bool = False, min_conf: float = 0.0, cap: int = None):
    if long_only:
        signals = [s for s in signals if s.action == "buy"]

    signals = [s for s in signals if s.confidence >= min_conf]

    if cap is not None:
        signals = signals[:cap]
    return signals

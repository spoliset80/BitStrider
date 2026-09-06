"""
ApexTrader -- Discovery
Manages live trending-stock scans, EDGAR 8-K and Alpaca-movers universe
refresh. Extracted from main.py to keep the main entry point lean.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from engine.utils import (
    MarketState,
    get_bars,
    get_premarket_bars,
    get_trending_tickers,
    filter_trending_momentum,
    get_finnhub_trending_tickers,
    check_sentiment_gate,
)
from engine.config import PRIORITY_1_MOMENTUM as _P1, PRIORITY_2_ESTABLISHED as _P2
from engine.ti.yahoo_universe import _is_valid_ti_ticker as is_valid_ti_ticker

log = logging.getLogger("ApexTrader")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # engine/equity/discovery.py -> equity -> engine -> repo root

# -- Module-level state -----------------------------------------------------
trending_stocks:           List[Dict] = []
last_trending_scan:        float      = 0.0
# Tickers discovered via trending feeds -- kept separately so we never mutate
# the imported config list (PRIORITY_1_MOMENTUM is a module-level constant).
_discovered_trending:      List[str]  = []
last_edgar_scan:           float      = 0.0
# 2026-09-01: the sympathy/EDGAR/watchlist _priority_scan_queue was removed
# with options trading -- its only consumer was the options scanner
# (engine/options/strategies.py via get_priority_scan_queue). Equity scan
# has used TI top-N + _alpaca_movers_queue only since 2026-08-26.
# 2026-08-26, user request: Alpaca-movers tickers get their OWN queue so the
# equity scan can include movers while still excluding EDGAR/sympathy/
# watchlist (which used to share a single priority queue -- removed 2026-09-01,
# see the note above).
# 2026-08-27, user request ("the top 30 list seems to be not aligned with
# the morning runners"): originally shared the old priority queue's 60-min
# TTL -- confirmed live this evicted genuinely still-active
# movers, not just dead ones (WKSP/WNW/BTCT/CRM: added 08:35 ET, aged out
# 09:35, RE-QUALIFIED as movers again at 09:55 -- proving they were still
# real, just absent from the scan universe for a ~20-min gap in between --
# and NOWL's re-entry got explicitly blocked at 10:27 ET because the TTL had
# dropped it from the top-30 at 10:14). Alpaca-movers entries are already
# activity-confirmed at add time (trade_count >= 10K, real price/move bands)
# unlike a one-off EDGAR/sympathy news trigger, and is_dead_ticker() (engine/
# utils/bars.py, immediate-suppression as of last night) already prunes
# genuinely inactive names on the very next fetch -- the TTL added no real
# safety on top of that, just an unwanted eviction. Now reset once per
# trading day (see _alpaca_movers_day below) instead of rolling-windowed.
_alpaca_movers_queue:      Dict[str, float] = {}
_alpaca_movers_day: Optional[datetime.date] = None  # date of the last reset -- see scan_alpaca_movers

# 2026-08-27, user request ("I have lost most of my gains due to these
# bugs"): _alpaca_movers_queue was pure in-memory with zero disk
# persistence -- every one of today's several restarts silently wiped the
# ENTIRE queue back to empty, independent of the TTL bug fixed just above.
# CRM/CRWD and 13 other names confirmed as real movers earlier today
# (DAIC, NCPL, OKTA, CELU, YJ, BRNX, NVDX, NVDL, NMTC, MERC, NOWL, AZIO,
# WNW, PURR) were lost this way, not just by the TTL. Persisted the same
# way universe.json/ti_primary.json already are (engine/equity/universe.py)
# -- load once at import, save on every mutation (add + daily reset) --
# so a restart mid-session no longer costs the day's confirmed movers.
_MOVERS_QUEUE_FILE = REPO_ROOT / "data" / "alpaca_movers_queue.json"


def _load_movers_queue_from_disk() -> None:
    """Populate _alpaca_movers_queue/_alpaca_movers_day from disk at import
    time. A file from a PRIOR trading day is intentionally ignored here
    (left for scan_alpaca_movers's own date check to clear on its next
    call) rather than special-cased twice -- same "only today's date
    counts" rule, one place."""
    global _alpaca_movers_queue, _alpaca_movers_day
    if not _MOVERS_QUEUE_FILE.exists():
        return
    try:
        raw = json.loads(_MOVERS_QUEUE_FILE.read_text(encoding="utf-8"))
        saved_date = datetime.date.fromisoformat(raw["date"])
        if saved_date != datetime.date.today():
            return  # stale (yesterday or older) -- leave empty, don't restore
        _alpaca_movers_queue = {str(k): float(v) for k, v in raw.get("tickers", {}).items()}
        _alpaca_movers_day = saved_date
        if _alpaca_movers_queue:
            log.info(f"[ALPACA-MOVERS-QUEUE] Restored {len(_alpaca_movers_queue)} ticker(s) from disk after restart: {list(_alpaca_movers_queue.keys())}")
    except Exception as e:
        log.warning(f"[ALPACA-MOVERS-QUEUE] Failed to load {_MOVERS_QUEUE_FILE.name} (non-fatal, starting empty): {e}")


def _save_movers_queue_to_disk() -> None:
    """Write the current queue to disk. Best-effort -- a save failure must
    never block the caller (an add or a daily reset) from completing."""
    try:
        _MOVERS_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MOVERS_QUEUE_FILE.write_text(
            json.dumps({"date": str(datetime.date.today()), "tickers": _alpaca_movers_queue}, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning(f"[ALPACA-MOVERS-QUEUE] Failed to save {_MOVERS_QUEUE_FILE.name} (non-fatal): {e}")

_load_movers_queue_from_disk()

last_alpaca_mover_scan:    float      = 0.0


def get_alpaca_movers_queue() -> List[str]:
    """Return current Alpaca-movers tickers (read-only peek).

    2026-08-27: no longer TTL-pruned here -- see the module comment above
    _alpaca_movers_queue's declaration. Reset once per trading day by
    scan_alpaca_movers instead; is_dead_ticker (engine/utils/bars.py) prunes
    genuinely inactive names.
    """
    return list(_alpaca_movers_queue.keys())


@dataclass


class PreopenSignalProvider:
    name: str
    apply: Callable[["PreopenIntelligenceScanner", dict[str, dict], MarketState, list, list, int, "PreopenSignalProvider"], None]
    weight: float = 1.0
    active: bool = True
    description: str = ""


class PreopenIntelligenceScanner:
    def __init__(self) -> None:
        self.last_scan: float = 0.0
        self.watchlist: List[dict] = []
        self.providers: List[PreopenSignalProvider] = []
        self.use_regime_gating: bool = True
        self.use_sentiment_gating: bool = True
        self.signal_performance: Dict[str, Dict[str, float]] = {}
        self._register_default_providers()

    def get_watchlist(self) -> List[str]:
        return [item["symbol"] for item in self.watchlist]

    def _get_provider_weight(self, name: str) -> float:
        for provider in self.providers:
            if provider.name == name:
                return provider.weight
        return 1.0

    def scan(
        self,
        *,
        enabled: bool,
        interval_min: float,
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int = 20,
        use_regime_gating: bool = True,
        use_sentiment_gating: bool = True,
    ) -> None:
        """Build a scored pre-open watchlist and inject high-priority tickers.

        This is meant for closed-market / pre-open cycles where the bot can
        proactively gather event-driven ideas and churn signals before the next
        live scan.
        """
        if not enabled:
            return

        self.use_regime_gating = use_regime_gating
        self.use_sentiment_gating = use_sentiment_gating

        now = time.time()
        if (now - self.last_scan) < (interval_min * 60):
            return
        self.last_scan = now

        if market_state.is_market_open:
            return

        scores: dict[str, dict] = {}
        self._run_providers(scores, market_state, priority_1, priority_2, max_watchlist)
        self.watchlist = self._build_watchlist(scores, max_watchlist)

        self._log_watchlist_summary(market_state)

    def _register_default_providers(self) -> None:
        self.providers = [
            PreopenSignalProvider(
                name="trending",
                apply=self._provider_trending,
                description="Live momentum feed from external trending sources",
            ),
            PreopenSignalProvider(
                name="finnhub",
                apply=self._provider_finnhub,
                description="Finnhub trending and stock momentum candidates",
            ),
            PreopenSignalProvider(
                name="universe_seed",
                apply=self._provider_universe_seed,
                description="Fallback universe seeds for robustness and coverage",
            ),
            PreopenSignalProvider(
                name="sentiment",
                apply=self._provider_sentiment,
                description="News sentiment gate and regime-aligned signal boost",
            ),
            PreopenSignalProvider(
                name="premarket",
                apply=self._provider_premarket,
                description="Pre-market gap and volume scoring for overnight churn",
            ),
        ]

    def _run_providers(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
    ) -> None:
        for provider in self.providers:
            if not provider.active:
                continue
            try:
                provider.apply(scores, market_state, priority_1, priority_2, max_watchlist, provider)
            except Exception as exc:
                log.warning(f"[PREOPEN] provider failed: {provider.name} -> {exc}")

    def _add_candidate(
        self,
        scores: dict[str, dict],
        symbol: str,
        weight: float,
        reason: str,
        provider_name: Optional[str] = None,
    ) -> None:
        if not is_valid_ti_ticker(symbol):
            return
        if symbol not in scores:
            scores[symbol] = {
                "symbol": symbol,
                "score": 0.0,
                "reasons": set(),
                "source_weight": 0.0,
            }
        scores[symbol]["score"] += weight
        scores[symbol]["reasons"].add(reason)
        if provider_name is not None:
            perf = self.signal_performance.setdefault(
                provider_name,
                {"runs": 0.0, "contributions": 0.0, "hits": 0.0},
            )
            perf["contributions"] += abs(weight)

    def _provider_trending(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
        provider: PreopenSignalProvider,
    ) -> None:
        try:
            trending = get_trending_tickers(15)
        except Exception:
            trending = []
        for sym in trending:
            self._add_candidate(scores, sym, 1.0 * provider.weight, "trending", provider_name=provider.name)

    def _provider_finnhub(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
        provider: PreopenSignalProvider,
    ) -> None:
        try:
            finn_tickers = get_finnhub_trending_tickers()
        except Exception:
            finn_tickers = []
        for sym in finn_tickers:
            self._add_candidate(scores, sym, 0.8 * provider.weight, "finnhub", provider_name=provider.name)

    def _provider_universe_seed(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
        provider: PreopenSignalProvider,
    ) -> None:
        for sym in priority_1 + priority_2:
            if sym in scores:
                continue
            if len(scores) >= max_watchlist * 3:
                break
            self._add_candidate(scores, sym, 0.1 * provider.weight, "universe-seed", provider_name=provider.name)

    def _provider_sentiment(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
        provider: PreopenSignalProvider,
    ) -> None:
        # check_sentiment_gate() is a per-symbol network call; fetch all of
        # them concurrently (same pattern as scan_universe's ThreadPoolExecutor)
        # instead of one-by-one, which was the dominant cost in this provider.
        from engine.config import SCAN_WORKERS
        symbols = list(scores.keys())
        sentiment_cache: dict[str, tuple[bool, float]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            future_map = {pool.submit(check_sentiment_gate, s): s for s in symbols}
            for future in concurrent.futures.as_completed(future_map):
                s = future_map[future]
                try:
                    sentiment_cache[s] = future.result()
                except Exception:
                    sentiment_cache[s] = (True, 0.5)

        for symbol in symbols:
            allow, bullish_pct = sentiment_cache[symbol]

            if allow:
                self._add_candidate(scores, symbol, 0.25 * provider.weight, "news-bullish", provider_name=provider.name)
                if market_state.resolve_sentiment() == "bull":
                    self._add_candidate(scores, symbol, 0.1 * provider.weight, "sentiment-aligned", provider_name=provider.name)
            else:
                penalty = 0.4 if self.use_sentiment_gating else 0.15
                self._add_candidate(
                    scores,
                    symbol,
                    -penalty * provider.weight,
                    "sentiment-block" if self.use_sentiment_gating else "news-bearish",
                    provider_name=provider.name,
                )
                if self.use_sentiment_gating and market_state.resolve_sentiment() == "bear":
                    self._add_candidate(scores, symbol, -0.1 * provider.weight, "bearish-regime-penalty", provider_name=provider.name)

    def _provider_premarket(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
        provider: PreopenSignalProvider,
    ) -> None:
        from engine.config import SCAN_WORKERS
        ranked = sorted(scores.values(), key=lambda d: d["score"], reverse=True)
        premkt_candidates = [item["symbol"] for item in ranked[: max_watchlist * 2]]

        # Pre-warm the bar cache concurrently -- _score_premarket's own
        # get_premarket_bars()/get_bars() calls then hit cache instead of
        # each doing its two network calls one-by-one in a serial loop.
        def _prefetch(symbol: str) -> None:
            get_premarket_bars(symbol)
            get_bars(symbol, "2d", "1d")

        with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            futures = [pool.submit(_prefetch, s) for s in premkt_candidates]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception:
                    pass  # _score_premarket has its own try/except and handles empty data

        for symbol in premkt_candidates:
            self._score_premarket(symbol, scores, provider)

    def _score_premarket(self, symbol: str, scores: dict[str, dict], provider: PreopenSignalProvider) -> None:
        try:
            pm = get_premarket_bars(symbol)
            if pm.empty:
                return
            t = pm["time"]
            if t.dt.tz is None:
                t = t.dt.tz_localize(datetime.timezone.utc).dt.tz_convert("America/New_York")
            else:
                t = t.dt.tz_convert("America/New_York")
            premkt = pm[(t.dt.hour < 9) | ((t.dt.hour == 9) & (t.dt.minute < 30))]
            if premkt.empty:
                return

            daily = get_bars(symbol, "2d", "1d")
            if daily.empty or len(daily) < 2:
                return

            prev_close = float(daily["close"].iloc[-2])
            last_price = float(premkt["close"].iloc[-1])
            gap_pct = ((last_price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
            pm_volume = float(premkt["volume"].sum())
            avg_daily_vol = float(daily["volume"].iloc[-2:-1].mean()) if len(daily) >= 2 else 0.0
            pm_vol_pct = (pm_volume / max(avg_daily_vol, 1.0)) * 100

            if gap_pct >= 1.0 and pm_vol_pct >= 10.0:
                self._add_candidate(scores, symbol, 0.5 * provider.weight, f"pre-market gap +{gap_pct:.1f}%", provider_name=provider.name)
            elif gap_pct >= 0.5 and pm_vol_pct >= 5.0:
                self._add_candidate(scores, symbol, 0.25 * provider.weight, f"pre-market gap +{gap_pct:.1f}%", provider_name=provider.name)
            if pm_vol_pct >= 15.0:
                self._add_candidate(scores, symbol, 0.2 * provider.weight, f"PM vol {pm_vol_pct:.0f}%", provider_name=provider.name)
        except Exception:
            return

    def _build_watchlist(self, scores: dict[str, dict], max_watchlist: int) -> List[dict]:
        ranked = sorted(scores.values(), key=lambda d: d["score"], reverse=True)
        return ranked[:max_watchlist]

    def _log_watchlist_summary(self, market_state: MarketState) -> None:
        sentiment_label = "bull" if market_state.resolve_sentiment() == "bull" else "bear"
        if not self.watchlist:
            log.info("[PREOPEN] Intelligence watchlist produced no candidates")
            return

        top_list = []
        for item in self.watchlist[:10]:
            reasons = ", ".join(sorted(item["reasons"]))
            top_list.append(f"{item['symbol']}({item['score']:.2f}:{reasons})")

        log.info(
            f"[PREOPEN] Intelligence watchlist ({sentiment_label}, "
            f"regime={market_state.regime}): top {len(self.watchlist)} "
            f"tickers -> {', '.join(top_list)}"
        )

        if self.signal_performance:
            provider_stats = []
            for name, metrics in self.signal_performance.items():
                provider_stats.append(
                    f"{name}=w{self._get_provider_weight(name):.2f}/"
                    f"{int(metrics.get('hits', 0))}/{int(metrics.get('contributions', 0))}"
                )
            log.info(f"[PREOPEN] Provider performance -> {', '.join(provider_stats)}")

_preopen_intelligence_scanner = PreopenIntelligenceScanner()


def scan_preopen_intelligence(
    *,
    enabled: bool,
    interval_min: float,
    market_state: MarketState,
    priority_1: list,
    priority_2: list,
    max_watchlist: int = 20,
    use_regime_gating: bool = True,
    use_sentiment_gating: bool = True,
) -> None:
    return _preopen_intelligence_scanner.scan(
        enabled=enabled,
        interval_min=interval_min,
        market_state=market_state,
        priority_1=priority_1,
        priority_2=priority_2,
        max_watchlist=max_watchlist,
        use_regime_gating=use_regime_gating,
        use_sentiment_gating=use_sentiment_gating,
    )


def scan_trending_stocks(
    *,
    use_live_trending: bool,
    use_finnhub: bool,
    use_sentiment_gate: bool,
    trending_max: int,
    trending_interval_min: float,
    trending_min_momentum: float,
    priority_1: list,
) -> None:
    """Refresh ``trending_stocks`` from live feeds (Finnhub, etc.).

    New tickers are stored in the module-level ``_discovered_trending`` list
    rather than mutating the caller-supplied ``priority_1`` config list.
    Callers can read discovered tickers via ``get_discovered_trending()``.
    """
    global trending_stocks, last_trending_scan

    if not use_live_trending and not use_finnhub:
        return

    now = time.time()
    if now - last_trending_scan < (trending_interval_min * 60):
        return

    from engine.utils import (
        get_trending_tickers, filter_trending_momentum,
        get_finnhub_trending_tickers, check_sentiment_gate,
    )

    try:
        log.info("[SCAN] Scanning for live trending stocks...")
        all_tickers: List[str] = []

        if use_live_trending:
            tickers = get_trending_tickers(trending_max)
            if tickers:
                all_tickers.extend(tickers)

        if use_finnhub:
            tickers = get_finnhub_trending_tickers()
            if tickers:
                all_tickers.extend(tickers)

        unique = list(set(all_tickers))

        if not unique:
            log.info("[SCAN] No trending tickers found -- using existing universe")
            trending_stocks    = [{"symbol": s, "momentum_pct": 0, "current_price": 0}
                                   for s in priority_1[:trending_max]]
            last_trending_scan = now
            return

        momentum_stocks = filter_trending_momentum(unique, trending_min_momentum)

        if not momentum_stocks:
            log.info(f"[SCAN] No trending stocks with >{trending_min_momentum}% momentum -- using universe")
            trending_stocks    = [{"symbol": s, "momentum_pct": 0, "current_price": 0}
                                   for s in priority_1[:trending_max]]
            last_trending_scan = now
            return

        if use_sentiment_gate:
            filtered = []
            for stock in momentum_stocks:
                allow, bullish_pct = check_sentiment_gate(stock["symbol"])
                if allow:
                    stock["sentiment"] = bullish_pct
                    filtered.append(stock)
            momentum_stocks = filtered
            log.info(f"[SCAN] Sentiment filter: {len(filtered)} passed")

        new_stocks = [s for s in momentum_stocks if s["symbol"] not in priority_1]
        if new_stocks:
            log.info(f"[SCAN] Found {len(new_stocks)} new trending stocks: " + ", ".join(f"{s['symbol']} (+{s['momentum_pct']:.1f}% @ ${s['current_price']:.2f})" for s in new_stocks[:5]))
            # Store in module-level set -- never mutate the config list
            _discovered_syms = {s for s in _discovered_trending}
            for s in new_stocks:
                if s["symbol"] not in _discovered_syms:
                    _discovered_trending.append(s["symbol"])
            log.info(f"[SCAN] Discovered trending: {len(_discovered_trending)} tickers this session")

        trending_stocks    = momentum_stocks
        last_trending_scan = now

    except Exception as e:
        log.error(f"[SCAN] Trending scan failed: {e}", exc_info=True)
        trending_stocks = [{"symbol": s, "momentum_pct": 0, "current_price": 0}
                           for s in priority_1[:trending_max]]


def scan_edgar(
    *,
    edgar_enabled: bool,
    edgar_interval_min: float,
    priority_1: list,
    priority_2: list,
) -> None:
    """
    EDGAR 8-K feed scanner: polls the SEC 8-K ATOM feed for material
    filings (supply agreements, contract awards, acquisitions) and
    injects matched tickers into priority_2 for follow-on monitoring.
    """
    global last_edgar_scan

    now = time.time()
    delisted: set = set()
    try:
        from engine.config import DELISTED_STOCKS
        delisted = set(DELISTED_STOCKS)
    except Exception:
        pass

    # -- EDGAR 8-K feed ---------------------------------------------------
    if edgar_enabled and (now - last_edgar_scan) >= (edgar_interval_min * 60):
        try:
            from engine.data.edgar_scraper import get_edgar_triggered_tickers
            edgar_tickers = get_edgar_triggered_tickers()
            if edgar_tickers:
                p2_set = set(priority_2)
                p1_set = set(priority_1)
                now = time.time()
                for sym in edgar_tickers:
                    if sym not in delisted and sym not in p2_set and sym not in p1_set:
                        log.info(f"[EDGAR] Adding {sym} to P2 for monitoring")
                        priority_2.append(sym)
        except Exception as exc:
            log.debug(f"[EDGAR] Scan error: {exc}")
        finally:
            last_edgar_scan = now


def scan_alpaca_movers(*, interval_min: float = 10.0, market_state: MarketState) -> None:
    """Fetch Alpaca Most Actives + Market Movers and inject qualifying symbols
    into the dedicated _alpaca_movers_queue (equity scan) -- the movers-only
    queue is what lets equity scan include movers while keeping EDGAR/sympathy
    out (see the module comment above _alpaca_movers_queue's declaration).

    The endpoint resets at market open -- data before 09:30 ET is from the
    previous session, so we only run during regular market hours.
    """
    global last_alpaca_mover_scan, _alpaca_movers_queue, _alpaca_movers_day

    if not market_state.is_market_open:
        return

    # 2026-08-27, user request ("the top 30 list seems to be not aligned
    # with the morning runners"): reset the movers-only queue once per
    # trading day, matching the endpoint's own "resets at market open"
    # behavior -- replaces the old rolling 60-min TTL, which was evicting
    # genuinely still-active movers mid-session (see the module comment
    # above _alpaca_movers_queue's declaration for the live evidence).
    today = market_state.now.date()
    if _alpaca_movers_day != today:
        if _alpaca_movers_queue:
            log.info(f"[ALPACA-MOVERS-QUEUE] New trading day -- clearing {len(_alpaca_movers_queue)} ticker(s) from the previous session: {list(_alpaca_movers_queue.keys())}")
        _alpaca_movers_queue = {}
        _alpaca_movers_day = today
        _save_movers_queue_to_disk()

    now = time.time()
    if now - last_alpaca_mover_scan < interval_min * 60:
        return

    try:
        import engine.config as _cfg
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout
        from alpaca.data.historical.screener import ScreenerClient
        from alpaca.data.requests import MostActivesRequest, MarketMoversRequest
        from alpaca.data.enums import MostActivesBy

        _SCREENER_TIMEOUT = 15  # seconds per request

        sc = ScreenerClient(_cfg.API_KEY, _cfg.API_SECRET)

        with ThreadPoolExecutor(max_workers=1) as _pool:
            try:
                # top=100 (API max), not 30: market_movers.gainers (top-%-by-day, market-wide)
                # and most_actives (top-volume, market-wide) are close to disjoint populations --
                # wild % movers skew thin/small-cap, top raw-volume skews mega-cap/ETF. At
                # top=30 the AND of both lists almost never has a member (confirmed: zero
                # matches across 65 cycles on 2026-08-04, including a 20%+ PLTR day). Same
                # single API call either way -- just asking for the widest slice the endpoint allows.
                actives_resp = _pool.submit(
                    sc.get_most_actives, MostActivesRequest(by=MostActivesBy.VOLUME, top=100)
                ).result(timeout=_SCREENER_TIMEOUT)
            except _FuturesTimeout:
                log.warning("[ALPACA-MOVERS] most_actives timed out -- skipping cycle")
                return

        # Build a set of symbols that cleared the trade-count floor (real participation)
        active_syms = {
            a.symbol
            for a in actives_resp.most_actives
            if int(a.trade_count) >= 10_000
        }

        with ThreadPoolExecutor(max_workers=1) as _pool:
            try:
                # top=50 (API max), not 20 -- same reasoning as most_actives above.
                movers_resp = _pool.submit(
                    sc.get_market_movers, MarketMoversRequest(market_type="stocks", top=50)
                ).result(timeout=_SCREENER_TIMEOUT)
            except _FuturesTimeout:
                log.warning("[ALPACA-MOVERS] market_movers timed out -- skipping cycle")
                return

        injected: List[str] = []
        delisted  = set(_cfg.DELISTED_STOCKS)

        for m in movers_resp.gainers:
            sym = m.symbol
            # Structural filter: warrants/rights have > 5 chars (e.g. BZAIW, GFAIW)
            if len(sym) > 5:
                continue
            # Price band: wide enough for gap runners, not so high position-sizing breaks
            if not (0.50 <= float(m.price) <= 500.0):
                continue
            # Move band: meaningful but not a halt/binary-news situation
            if not (3.0 <= float(m.percent_change) <= 40.0):
                continue
            # Volume confirmation: must also appear in most actives
            if sym not in active_syms:
                continue
            if sym in delisted:
                continue
            is_new = sym not in _alpaca_movers_queue
            now_ts = time.time()
            _alpaca_movers_queue.setdefault(sym, now_ts)
            if not is_new:
                continue  # already tracked -- nothing new to log
            _save_movers_queue_to_disk()
            injected.append(sym)
            log.info(f"[ALPACA-MOVERS] Adding {sym} to the movers scan queue")

        last_alpaca_mover_scan = now
        if injected:
            log.info(f"[ALPACA-MOVERS] {len(injected)} gainers queued for scan: {injected}")
        else:
            log.info("[ALPACA-MOVERS] No gainers passed filters this cycle")

    except Exception as exc:
        log.warning(f"[ALPACA-MOVERS] Screener fetch failed: {exc}")


def _demo() -> None:
    """Self-check for the alpaca-movers-queue day-reset + disk persistence."""
    _alpaca_movers_queue.clear()

    now = time.time()
    _alpaca_movers_queue["MOVR"] = now
    movers = get_alpaca_movers_queue()
    assert movers == ["MOVR"], f"expected only MOVR in the movers queue, got {movers}"

    # 2026-08-27, user request ("the top 30 list seems to be not aligned
    # with the morning runners"): the movers queue must NOT time-prune --
    # confirmed live this evicted still-active movers (CRM/OKTA/CRWD/NVDX:
    # added 08:35 ET, evicted 09:35 ET, still active). A very old timestamp
    # must survive get_alpaca_movers_queue() -- only a new trading day
    # (scan_alpaca_movers) or is_dead_ticker (engine/utils/bars.py, applied
    # downstream in get_scan_targets) may remove it.
    _alpaca_movers_queue["OLDMOVR"] = 1.0  # 1970 -- as old as a timestamp gets
    assert "OLDMOVR" in get_alpaca_movers_queue(), "movers queue must not age out entries by elapsed time"

    # 2026-08-27, disk-persistence fix ("I have lost most of my gains due to
    # these bugs"): the movers queue was pure in-memory -- every restart
    # wiped it, stranding legitimately-active movers. Round-trip through a
    # temp file (never the real data/alpaca_movers_queue.json) to verify
    # save -> load survives a process restart, and that a stale (yesterday's)
    # file is correctly ignored rather than resurrected.
    import tempfile
    global _MOVERS_QUEUE_FILE
    real_file = _MOVERS_QUEUE_FILE
    tmpdir = tempfile.mkdtemp()
    try:
        _MOVERS_QUEUE_FILE = Path(tmpdir) / "movers_test.json"

        _alpaca_movers_queue.clear()
        _alpaca_movers_queue["PERSIST"] = now
        _save_movers_queue_to_disk()
        assert _MOVERS_QUEUE_FILE.exists(), "save must write the file"

        _alpaca_movers_queue.clear()
        _load_movers_queue_from_disk()
        assert _alpaca_movers_queue == {"PERSIST": now}, \
            f"load must restore exactly what was saved, got {_alpaca_movers_queue}"

        # A file from a prior trading day must be ignored (daily reset owns
        # clearing it, not the loader silently resurrecting stale movers).
        stale = {"date": "2020-01-01", "tickers": {"STALE": now}}
        _MOVERS_QUEUE_FILE.write_text(json.dumps(stale), encoding="utf-8")
        _alpaca_movers_queue.clear()
        _load_movers_queue_from_disk()
        assert _alpaca_movers_queue == {}, \
            f"a stale-dated file must not be loaded, got {_alpaca_movers_queue}"

        # A missing/corrupt file must not raise -- restart must still boot.
        _MOVERS_QUEUE_FILE.write_text("not json", encoding="utf-8")
        _alpaca_movers_queue.clear()
        _load_movers_queue_from_disk()  # must not raise
        assert _alpaca_movers_queue == {}, "corrupt file must load as empty, not raise"
    finally:
        _MOVERS_QUEUE_FILE = real_file
        _alpaca_movers_queue.clear()

    print("discovery._demo: all assertions passed")


if __name__ == "__main__":
    _demo()

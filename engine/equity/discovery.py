"""
ApexTrader — Discovery
Manages live trending-stock scans and Trade Ideas universe refresh.
Extracted from main.py to keep the main entry point lean.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from pathlib import Path
from typing import List, Dict

from engine.utils import get_trending_tickers, filter_trending_momentum, get_finnhub_trending_tickers, check_sentiment_gate
from engine.config import PRIORITY_1_MOMENTUM as _P1, PRIORITY_2_ESTABLISHED as _P2
from engine.ti.ti import get_scans, is_valid_ti_ticker, scrape_tradeideas

log = logging.getLogger("ApexTrader")

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Module-level state ─────────────────────────────────────────────────────
trending_stocks:           List[Dict] = []
last_trending_scan:        float      = 0.0
# Tickers discovered via trending feeds — kept separately so we never mutate
# the imported config list (PRIORITY_1_MOMENTUM is a module-level constant).
_discovered_trending:      List[str]  = []
last_ti_scan:              float      = 0.0
last_ti_options_scan:      float      = 0.0
last_ti_toplists_scan:     float      = 0.0
last_sympathy_scan:        float      = 0.0
last_edgar_scan:           float      = 0.0
# Sympathy + EDGAR tickers queued for the NEXT scan cycle.
# get_scan_targets() pops these to the front so they are guaranteed to be scanned.
_priority_scan_queue:      List[str]  = []
last_alpaca_mover_scan:    float      = 0.0
_ti_future                            = None
_ti_started_at:            float      = 0.0
_ti_warned_running:        bool       = False
_ti_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)


def get_priority_scan_queue() -> List[str]:
    """Return the current sympathy/EDGAR/screener tickers (read-only peek).

    Does NOT drain the queue — symbols remain until the next screener/discovery
    refresh replaces them, so both equity and options scans see the same set.
    """
    return list(_priority_scan_queue)


# ── Trending scan ──────────────────────────────────────────────────────────

def get_discovered_trending() -> List[str]:
    """Return tickers found by trending scans this session (read-only copy)."""
    return list(_discovered_trending)


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
        log.info("[SCAN] Scanning for live trending stocks…")
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
            log.info("[SCAN] No trending tickers found — using existing universe")
            trending_stocks    = [{"symbol": s, "momentum_pct": 0, "current_price": 0}
                                   for s in priority_1[:trending_max]]
            last_trending_scan = now
            return

        momentum_stocks = filter_trending_momentum(unique, trending_min_momentum)

        if not momentum_stocks:
            log.info(f"[SCAN] No trending stocks with >{trending_min_momentum}% momentum — using universe")
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
            # Store in module-level set — never mutate the config list
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


# ── Trade Ideas universe refresh ───────────────────────────────────────────

def _apply_tradeideas_results(results: dict, scans: dict, priority_1: list, priority_2: list) -> None:
    """Merge TI scrape results into *priority_1* / *priority_2* lists in-place."""
    from engine.config import PRIORITY_1_MOMENTUM as _P1, PRIORITY_2_ESTABLISHED as _P2

    by_dest: dict = {
        "PRIORITY_1_MOMENTUM":   [],
        "PRIORITY_2_ESTABLISHED": [],
    }

    for scan_key, tickers in results.items():
        if scan_key in scans:
            target_list_name = scans[scan_key]["target"]
            label            = scans[scan_key]["label"]
            if target_list_name == "BOTH":
                continue
        elif scan_key.endswith("_leaders"):
            target_list_name = "PRIORITY_1_MOMENTUM"
            label            = "stock_race_central_leaders"
        elif scan_key.endswith("_laggards"):
            target_list_name = "PRIORITY_2_ESTABLISHED"
            label            = "stock_race_central_laggards"
        else:
            continue

        valid = [t for t in tickers if is_valid_ti_ticker(t)]
        if len(valid) < 5:
            log.warning(
                f"Trade Ideas {label}: only {len(valid)} valid ticker(s) after filtering "
                f"(need ≥5) — likely a login-page scrape or empty scan; "
                f"skipping to preserve {target_list_name}"
            )
            continue
        by_dest[target_list_name].append((label, valid))

    PRIMARY_SLOTS   = 35
    SECONDARY_SLOTS = 50 - PRIMARY_SLOTS

    for target_list_name, sources in by_dest.items():
        if not sources:
            continue

        dest        = priority_1 if target_list_name == "PRIORITY_1_MOMENTUM" else priority_2
        existing_set = set(dest)

        if len(sources) == 1:
            merged = sources[0][1][:]
        else:
            seen: set = set()
            primary_part: List[str] = []
            for t in sources[0][1]:
                if len(primary_part) >= PRIMARY_SLOTS:
                    break
                if t not in seen:
                    primary_part.append(t)
                    seen.add(t)

            secondary_part: List[str] = []
            for _, src in sources[1:]:
                for t in src:
                    if len(secondary_part) >= SECONDARY_SLOTS:
                        break
                    if t not in seen:
                        secondary_part.append(t)
                        seen.add(t)
            merged = primary_part + secondary_part

        merged_set = set(merged)
        new_tickers = [t for t in merged if t not in existing_set]
        fresh       = [t for t in merged if t in existing_set]
        demote      = [t for t in dest  if t not in merged_set]

        dest.clear()
        dest.extend(merged[:50])
        for t in demote:
            if t not in merged_set and t not in dest:
                dest.append(t)

        labels = " + ".join(f"{s[0]}({len(s[1])})" for s in sources)
        if new_tickers:
            log.info(
                f"Trade Ideas [{target_list_name}] {labels}: "
                f"+{len(new_tickers)} new, {len(fresh)} existing → top-10: {merged[:10]}"
            )
        else:
            log.info(
                f"Trade Ideas [{target_list_name}] {labels}: "
                f"{len(fresh)} merged → top-10: {merged[:10]}"
            )
        log.info(
            f"── TI top-20 [{target_list_name}] "
            f"(primary={len(sources[0][1]) if sources else 0} "
            f"secondary={sum(len(s[1]) for s in sources[1:]) if len(sources) > 1 else 0}): "
            + ", ".join(dest[:20])
        )


def scan_tradeideas_universe(
    *,
    enabled: bool,
    scan_interval_min: float,
    headless: bool,
    chrome_profile,
    update_config: bool,
    priority_1: list,
    priority_2: list,
    browser: str = "edge",
    remote_debug_port: int = 9222,
) -> None:
    """Submit or check a background TI scrape for core Trade Ideas pages."""
    global last_ti_scan, _ti_future, _ti_started_at, _ti_warned_running

    if not enabled:
        return

    try:
        SCANS = get_scans()
    except ImportError as e:
        log.warning(f"Trade Ideas scraper unavailable (selenium not installed?): {e}")
        last_ti_scan = time.time()
        return

    now = time.time()

    # 1) Apply finished results.
    if _ti_future is not None and _ti_future.done():
        try:
            results = _ti_future.result()
            _apply_tradeideas_results(results, SCANS, priority_1, priority_2)
        except Exception as e:
            log.error(f"Trade Ideas scan failed: {e}")
        finally:
            _ti_future         = None
            _ti_warned_running = False
            last_ti_scan       = now

    # 2) If still running, back off with escalating timeouts.
    if _ti_future is not None:
        elapsed = now - _ti_started_at
        if elapsed > 180:
            log.error(
                f"Trade Ideas scrape hard-timeout ({elapsed:.0f}s) — "
                "killing Chrome/chromedriver and resetting future"
            )
            import subprocess as _hk
            for _exe in ("chromedriver.exe", "chrome.exe"):
                try:
                    _hk.run(["taskkill", "/F", "/IM", _exe, "/T"],
                            capture_output=True, timeout=5)
                except Exception:
                    pass
            _ti_future         = None
            _ti_warned_running = False
            last_ti_scan       = now
            return
        if elapsed > 90 and not _ti_warned_running:
            log.warning(f"Trade Ideas scan still running ({elapsed:.0f}s) — trading loop continues")
            _ti_warned_running = True
        return

    # 3) Schedule next scrape when due.
    if (now - last_ti_scan) < (scan_interval_min * 60):
        return

    ti_profile  = (chrome_profile or "").strip() or None

    log.info(
        f"Scanning Trade Ideas core pages in background (browser=edge, profile={ti_profile or 'none'}) …"
    )
    _ti_started_at     = now
    _ti_warned_running = False
    _ti_future         = _ti_executor.submit(
        scrape_tradeideas,
        update_config=update_config,
        chrome_profile=ti_profile,
        select_30min=True,
        scan_keys=["marketscope360", "highshortfloat"],
        remote_debug_port=remote_debug_port,
    )


def scan_tradeideas_unusual_options(
    *,
    enabled: bool,
    scan_interval_min: float,
    headless: bool,
    chrome_profile,
    update_config: bool,
    priority_1: list,
    priority_2: list,
    browser: str = "edge",
    remote_debug_port: int = 9222,
) -> None:
    """Submit or check a background Trade Ideas unusual options scrape."""
    global last_ti_options_scan, _ti_future, _ti_started_at, _ti_warned_running

    if not enabled:
        return

    try:
        SCANS = get_scans()
    except ImportError as e:
        log.warning(f"Trade Ideas scraper unavailable (selenium not installed?): {e}")
        last_ti_options_scan = time.time()
        return

    now = time.time()

    if _ti_future is not None and _ti_future.done():
        try:
            results = _ti_future.result()
            _apply_tradeideas_results(results, SCANS, priority_1, priority_2)
        except Exception as e:
            log.error(f"Trade Ideas unusual options scan failed: {e}")
        finally:
            _ti_future         = None
            _ti_warned_running = False
            last_ti_options_scan = now

    if _ti_future is not None:
        elapsed = now - _ti_started_at
        if elapsed > 180:
            log.error(
                f"Trade Ideas unusual options scrape hard-timeout ({elapsed:.0f}s) — "
                "killing Chrome/chromedriver and resetting future"
            )
            import subprocess as _hk
            for _exe in ("chromedriver.exe", "chrome.exe"):
                try:
                    _hk.run(["taskkill", "/F", "/IM", _exe, "/T"],
                            capture_output=True, timeout=5)
                except Exception:
                    pass
            _ti_future         = None
            _ti_warned_running = False
            last_ti_options_scan = now
            return
        if elapsed > 90 and not _ti_warned_running:
            log.warning(f"Trade Ideas unusual options scan still running ({elapsed:.0f}s) — trading loop continues")
            _ti_warned_running = True
        return

    if (now - last_ti_options_scan) < (scan_interval_min * 60):
        return

    ti_profile  = (chrome_profile or "").strip() or None

    log.info(
        f"Scanning Trade Ideas unusual options in background (browser=edge, profile={ti_profile or 'none'}) …"
    )
    _ti_started_at     = now
    _ti_warned_running = False
    _ti_future         = _ti_executor.submit(
        scrape_tradeideas,
        update_config=update_config,
        chrome_profile=ti_profile,
        select_30min=True,
        scan_keys=["unusualoptionsvolume"],
        remote_debug_port=remote_debug_port,
    )


def scan_sympathy_and_edgar(
    *,
    sympathy_enabled: bool,
    edgar_enabled: bool,
    sympathy_interval_min: float,
    edgar_interval_min: float,
    priority_1: list,
    priority_2: list,
) -> None:
    """
    Sector sympathy + EDGAR 8-K scanner.

    Sympathy: checks leader stocks (via Finnhub quote) for gap/momentum moves
    that historically pull related peers. Triggered sympathies are injected
    into priority_1 for immediate scanning.

    EDGAR: polls the SEC 8-K ATOM feed for material filings (supply agreements,
    contract awards, acquisitions) and injects matched tickers into priority_2
    for follow-on monitoring.
    """
    global last_sympathy_scan, last_edgar_scan

    now = time.time()
    delisted: set = set()
    try:
        from engine.config import DELISTED_STOCKS
        delisted = set(DELISTED_STOCKS)
    except Exception:
        pass

    # ── Sector sympathy ───────────────────────────────────────────────────
    if sympathy_enabled and (now - last_sympathy_scan) >= (sympathy_interval_min * 60):
        try:
            from engine.data.sector_sympathy import get_active_sympathies
            sympathies = get_active_sympathies()
            if sympathies:
                p1_set = set(priority_1)
                queue_set = set(_priority_scan_queue)
                new_syms = [s for s in sympathies if s not in p1_set and s not in delisted]
                if new_syms:
                    log.info(f"[SYMPATHY] Injecting {len(new_syms)} sympathy tickers into P1 + scan queue: {new_syms}")
                    priority_1.extend(new_syms)
                    for s in new_syms:
                        if s not in queue_set:
                            _priority_scan_queue.append(s)
                            queue_set.add(s)
        except Exception as exc:
            log.debug(f"[SYMPATHY] Scan error: {exc}")
        finally:
            last_sympathy_scan = now

    # ── EDGAR 8-K feed ───────────────────────────────────────────────────
    if edgar_enabled and (now - last_edgar_scan) >= (edgar_interval_min * 60):
        try:
            from engine.data.edgar_scraper import get_edgar_triggered_tickers
            edgar_tickers = get_edgar_triggered_tickers()
            if edgar_tickers:
                p2_set = set(priority_2)
                p1_set = set(priority_1)
                queue_set = set(_priority_scan_queue)
                for sym in edgar_tickers:
                    if sym not in delisted and sym not in p2_set and sym not in p1_set:
                        log.info(f"[EDGAR] Adding {sym} to P2 + scan queue for monitoring")
                        priority_2.append(sym)
                    if sym not in delisted and sym not in queue_set:
                        _priority_scan_queue.append(sym)
                        queue_set.add(sym)
        except Exception as exc:
            log.debug(f"[EDGAR] Scan error: {exc}")
        finally:
            last_edgar_scan = now


def scan_alpaca_movers(*, interval_min: float = 10.0) -> None:
    """Fetch Alpaca Most Actives + Market Movers and inject qualifying symbols
    into the priority scan queue.

    The endpoint resets at market open — data before 09:30 ET is from the
    previous session, so we only run during regular market hours.
    """
    global last_alpaca_mover_scan, _priority_scan_queue

    from engine.utils import is_market_open

    if not is_market_open():
        return

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
                actives_resp = _pool.submit(
                    sc.get_most_actives, MostActivesRequest(by=MostActivesBy.VOLUME, top=30)
                ).result(timeout=_SCREENER_TIMEOUT)
            except _FuturesTimeout:
                log.warning("[ALPACA-MOVERS] most_actives timed out — skipping cycle")
                return

        # Build a set of symbols that cleared the trade-count floor (real participation)
        active_syms = {
            a.symbol
            for a in actives_resp.most_actives
            if int(a.trade_count) >= 10_000
        }

        with ThreadPoolExecutor(max_workers=1) as _pool:
            try:
                movers_resp = _pool.submit(
                    sc.get_market_movers, MarketMoversRequest(market_type="stocks", top=20)
                ).result(timeout=_SCREENER_TIMEOUT)
            except _FuturesTimeout:
                log.warning("[ALPACA-MOVERS] market_movers timed out — skipping cycle")
                return

        injected: List[str] = []
        queue_set = set(_priority_scan_queue)
        delisted  = set(_cfg.DELISTED_STOCKS)

        for m in movers_resp.gainers:
            sym = m.symbol
            # Structural filter: warrants/rights have > 5 chars (e.g. BZAIW, GFAIW)
            if len(sym) > 5:
                continue
            # Price band: cheap enough for options chains, not so high position-sizing breaks
            if not (0.50 <= float(m.price) <= 500.0):
                continue
            # Move band: meaningful but not a halt/binary-news situation
            if not (3.0 <= float(m.percent_change) <= 40.0):
                continue
            # Volume confirmation: must also appear in most actives
            if sym not in active_syms:
                continue
            if sym in queue_set or sym in delisted:
                continue
            _priority_scan_queue.append(sym)
            queue_set.add(sym)
            injected.append(sym)

        last_alpaca_mover_scan = now
        if injected:
            log.info(f"[ALPACA-MOVERS] {len(injected)} gainers queued for scan: {injected}")
        else:
            log.debug("[ALPACA-MOVERS] No gainers passed filters this cycle")

    except Exception as exc:
        log.warning(f"[ALPACA-MOVERS] Screener fetch failed: {exc}")


def scan_tradeideas_toplists(
    *,
    enabled: bool,
    scan_interval_min: float,
    headless: bool,
    chrome_profile,
    update_config: bool,
    priority_1: list,
    priority_2: list,
    browser: str = "edge",
    remote_debug_port: int = 9222,
) -> None:
    """Submit or check a background TI toplist scrape."""
    global last_ti_toplists_scan, _ti_future, _ti_started_at, _ti_warned_running

    if not enabled:
        return

    try:
        SCANS = get_scans()
    except ImportError as e:
        log.warning(f"Trade Ideas scraper unavailable (selenium not installed?): {e}")
        last_ti_toplists_scan = time.time()
        return

    now = time.time()

    if _ti_future is not None and _ti_future.done():
        try:
            results = _ti_future.result()
            _apply_tradeideas_results(results, SCANS, priority_1, priority_2)
        except Exception as e:
            log.error(f"Trade Ideas toplists scan failed: {e}")
        finally:
            _ti_future         = None
            _ti_warned_running = False
            last_ti_toplists_scan = now

    if _ti_future is not None:
        elapsed = now - _ti_started_at
        if elapsed > 180:
            log.error(
                f"Trade Ideas toplists scrape hard-timeout ({elapsed:.0f}s) — "
                "killing Chrome/chromedriver and resetting future"
            )
            import subprocess as _hk
            for _exe in ("chromedriver.exe", "chrome.exe"):
                try:
                    _hk.run(["taskkill", "/F", "/IM", _exe, "/T"],
                            capture_output=True, timeout=5)
                except Exception:
                    pass
            _ti_future         = None
            _ti_warned_running = False
            last_ti_toplists_scan = now
            return
        if elapsed > 90 and not _ti_warned_running:
            log.warning(f"Trade Ideas toplists scan still running ({elapsed:.0f}s) — trading loop continues")
            _ti_warned_running = True
        return

    if (now - last_ti_toplists_scan) < (scan_interval_min * 60):
        return

    ti_profile  = (chrome_profile or "").strip() or None

    log.info(
        f"Scanning Trade Ideas toplists in background (browser=edge, profile={ti_profile or 'none'}) …"
    )
    _ti_started_at     = now
    _ti_warned_running = False
    _ti_future         = _ti_executor.submit(
        scrape_tradeideas,
        update_config=update_config,
        chrome_profile=ti_profile,
        select_minutes=15,
        include_toplists=True,
        scan_keys=["toplists"],
        remote_debug_port=remote_debug_port,
    )

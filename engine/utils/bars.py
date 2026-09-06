"""
engine.utils.bars
-----------------
Bar data fetching, per-cycle cache, technical indicators (RSI, MACD, ATR).

All public functions here are re-exported from engine.utils for backward compat.
"""

from __future__ import annotations


import datetime
import logging
import os
import socket
import threading
import time
from typing import Dict, Tuple

# Add tenacity for retry logic
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type, retry_if_not_exception_type

import pandas as pd
import pytz

ET = pytz.timezone("America/New_York")

# -- Global socket-level bound -------------------------------------------------
# 2026-09-02: several yfinance/Alpaca paths (Ticker.info in guardrails, some
# SDK batch/premarket calls) can still create sockets with no timeout of their
# own, black-holing the caller for minutes despite the per-fetch wrapper above.
# socket.setdefaulttimeout() applies to every socket created without an
# explicit timeout, so even an unwrapped call cannot wedge the process. This
# engine is pure REST polling (no websockets/live streams in the codebase),
# and any future streaming client sets its own timeouts and is unaffected.
try:
    socket.setdefaulttimeout(float(os.getenv("SOCKET_TIMEOUT_SEC", "15")))
except Exception:
    pass  # never block engine startup over a defensive timeout

# ---- Alpaca SDK availability ------------------------------------------------------------------------------------------------------
try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False

# ---- Per-cycle bar cache --------------------------------------------------------------------------------------------------------------
# Keyed by (symbol, period, interval). Thread-safe via lock.
_bar_cache: Dict[Tuple[str, str, str], pd.DataFrame] = {}
_bar_cache_lock = threading.Lock()

# get_daily_volume_bars() keeps its own cache rather than sharing _bar_cache:
# it's a symbol-only key (no period/interval), and more importantly it must
# never accidentally read back an Alpaca/IEX-sourced entry that some other
# caller left in _bar_cache under the same (symbol, "5d", "1d") key -- that
# would silently reintroduce the undercounted-volume bug this function exists
# to avoid. Kept simple: yfinance data is good enough for all callers, so
# there's no equivalent risk in the other direction.
_volume_bar_cache: Dict[str, pd.DataFrame] = {}
_volume_bar_cache_lock = threading.Lock()

_ALPACA_MIN_INTERVAL = 0.35   # per-symbol throttle to reduce 429s
_last_alpaca_bar_ts: float = 0.0

# Interval-aware staleness thresholds. Previously only minute bars were checked
# (`interval.endswith("m")`) -- daily-bar fetches (RVOL, dollar-volume, the
# avg-daily-volume guardrail) and the batch snapshot path returned data no
# matter how old it was. A trading decision built on a genuinely dead feed
# for a symbol should never look identical to one built on fresh data.
_STALE_THRESHOLD_SECONDS = {
    "m": 120,               # minute bars: >2 min old means the feed isn't live
    "h": 2 * 3600,          # hour bars
    "d": 4 * 24 * 3600,     # daily bars: generous enough for a weekend/holiday gap,
                            # still catches a feed that's been dead for days
}


def _staleness_threshold(interval: str) -> float:
    """2026-08-24, user request ("stale data skipping is happening every
    time"): this used to be interval[-1] alone -- "1m"/"5m"/"15m"/"30m" all
    end in "m", so every minute-family interval got the SAME flat 120s
    threshold. A 15m bar's own timestamp is naturally 0-15 min old at any
    given check (median ~7.5 min = 450s) -- that's how a 15m candle works,
    not the feed being dead, so it failed a 120s check almost by
    definition. Now scales with the bar's own period: 1.5x the period + a
    60s fetch/processing buffer, floored at the original flat 120s so a 1m
    bar's check is effectively unchanged (150s vs 120s)."""
    suffix = interval[-1] if interval else "d"
    if suffix == "m":
        try:
            n = int(interval[:-1])
        except ValueError:
            n = 1
        return max(120.0, n * 60 * 1.5 + 60)
    return _STALE_THRESHOLD_SECONDS.get(suffix, _STALE_THRESHOLD_SECONDS["d"])

_data_client = None

# ---- Dead ticker suppression --------------------------------------------------------------------------------------------------
# A symbol that comes back stale/empty this many *consecutive* fetches (across
# both the Alpaca and yfinance paths, whichever runs) gets suppressed from
# further scans for a while instead of getting hit every cycle for nothing.
# Root cause this addresses: names like SAJ get added to the scan universe on
# a one-off signal (EDGAR 8-K match, sympathy, mover) and, with no data-driven
# filter, keep getting queried every cycle for the rest of the session even
# once IEX/yfinance have shown zero fresh prints for an hour+ -- pure wasted
# Alpaca calls on structurally thin names (confirmed 2026-08-26: SAJ staleness
# climbing every cycle with no reset after ~10:50 ET).
# 2026-08-31, user request: suppress only after 10 consecutive stale/empty
# pulls. This keeps a buffer against transient provider glitches while still
# dropping symbols that repeatedly return unusable data.
_DEAD_TICKER_THRESHOLD = int(os.getenv("DEAD_TICKER_THRESHOLD", "10"))
# Once suppressed, let one real fetch back through every N seconds so a name
# that starts trading again recovers on its own instead of being dead for the
# rest of the process's life.
_DEAD_TICKER_RECHECK_SEC = int(os.getenv("DEAD_TICKER_RECHECK_SEC", "900"))
_dead_ticker_hits: Dict[str, int] = {}
_dead_tickers: Dict[str, float] = {}  # symbol -> epoch time last marked dead
_dead_ticker_lock = threading.Lock()


def _record_empty_bars(symbol: str) -> None:
    """Count a stale/empty fetch; suppress after _DEAD_TICKER_THRESHOLD in a row."""
    with _dead_ticker_lock:
        hits = _dead_ticker_hits.get(symbol, 0) + 1
        _dead_ticker_hits[symbol] = hits
        if hits >= _DEAD_TICKER_THRESHOLD:
            newly = symbol not in _dead_tickers
            _dead_tickers[symbol] = time.time()
    if hits >= _DEAD_TICKER_THRESHOLD and newly:
        logging.getLogger("ApexTrader").warning(
            f"{symbol}: {hits} consecutive stale/empty bar fetches -- suppressing from scans"
        )


def _record_ok_bars(symbol: str) -> None:
    """A usable, fresh bar came back -- clear any suppression immediately."""
    with _dead_ticker_lock:
        _dead_ticker_hits.pop(symbol, None)
        _dead_tickers.pop(symbol, None)


def is_dead_ticker(symbol: str) -> bool:
    """True if symbol is currently suppressed for persistent stale/empty data."""
    with _dead_ticker_lock:
        marked_at = _dead_tickers.get(symbol)
        if marked_at is None:
            return False
        if time.time() - marked_at >= _DEAD_TICKER_RECHECK_SEC:
            return False  # let one probe through; _record_* will re-mark or clear it
        return True


def clear_bar_cache() -> None:
    """Flush the per-cycle bar cache. Call once at the start of each scan cycle."""
    global _bar_cache, _volume_bar_cache
    with _volume_bar_cache_lock:
        _volume_bar_cache = {}
    with _bar_cache_lock:
        _bar_cache = {}


# ---- Alpaca client singletons ----------------------------------------------------------------------------------------------------

def mount_wide_pool(client) -> None:
    """alpaca-py builds its internal requests.Session with the urllib3
    default pool_maxsize=10, no constructor param to raise it. Concurrent
    scan workers (equity scan: 8 workers) share one client, so simultaneous
    requests routinely exceed 10 and urllib3 tears
    down + rebuilds connections instead of reusing them (confirmed
    2026-08-07 on the stock data client: "Connection pool is full,
    discarding connection: data.alpaca.markets" every cycle) pure
    wasted latency, not a correctness bug. Call once right after
    constructing any StockHistoricalDataClient (the option data client was
    removed 2026-09-01 with options trading).
    """
    try:
        from requests.adapters import HTTPAdapter
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
        client._session.mount("https://", adapter)
        client._session.mount("http://", adapter)
    except Exception:
        pass  # cosmetic perf fix -- never block client creation over it


def get_data_client() -> "StockHistoricalDataClient":
    global _data_client
    if _data_client is None:
        from engine.config import API_KEY, API_SECRET
        if not API_KEY or not API_SECRET:
            raise ValueError("Alpaca API credentials not found in environment")
        _data_client = StockHistoricalDataClient(API_KEY, API_SECRET)
        mount_wide_pool(_data_client)
    return _data_client



# ---- TimeFrame helper --------------------------------------------------------------------------------------------------------------------

def _parse_timeframe(interval: str) -> "TimeFrame":
    if interval.endswith("m"):
        return TimeFrame(int(interval[:-1]), TimeFrameUnit.Minute)
    if interval.endswith("h"):
        return TimeFrame(int(interval[:-1]), TimeFrameUnit.Hour)
    if interval.endswith("d"):
        return TimeFrame(int(interval[:-1]), TimeFrameUnit.Day)
    return TimeFrame(15, TimeFrameUnit.Minute)


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Standardize column names and convert 'time' to ET-aware timestamps."""
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    for alias in ("timestamp", "datetime", "date"):
        if alias in df.columns:
            df = df.rename(columns={alias: "time"})
    if "time" in df.columns:
        col = pd.to_datetime(df["time"])
        try:
            col = col.dt.tz_convert(ET) if col.dt.tz is not None else col.dt.tz_localize("UTC").dt.tz_convert(ET)
        except Exception:
            pass
        df["time"] = col
    return df


# ---- Core bar fetch ------------------------------------------------------------------------------------------------------------------------

# -- Hard wall-clock bound on single-symbol network fetches --------------------
# 2026-09-02: originally added a daemon-thread wrapper here, but abandoned
# threads can hold yfinance/Alpaca session locks and deadlock the scan's
# worker pool -- REMOVED. The socket-level default set at import (below) is
# the single robust bound: any socket created without an explicit timeout
# raises socket.timeout after SOCKET_TIMEOUT_SEC, so unwrapped calls fail
# cleanly in their own thread (no orphan threads, no lock leaks).

@retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3),
       retry=(retry_if_exception_type(Exception) & retry_if_not_exception_type(socket.timeout)))
def _get_bars_alpaca(symbol: str, period: str, interval: str, log) -> pd.DataFrame:
    """Fetch OHLCV bars via Alpaca only, with retry."""
    client = get_data_client()
    tf     = _parse_timeframe(interval)
    days   = int(period[:-1]) if period.endswith("d") else 5
    end_dt = datetime.datetime.now(ET)
    start_dt = end_dt - datetime.timedelta(days=days)
    start_iso = start_dt.astimezone(pytz.UTC).isoformat().replace("+00:00", "Z")
    end_iso   = end_dt.astimezone(pytz.UTC).isoformat().replace("+00:00", "Z")

    global _last_alpaca_bar_ts
    elapsed = time.time() - _last_alpaca_bar_ts
    if elapsed < _ALPACA_MIN_INTERVAL:
        time.sleep(_ALPACA_MIN_INTERVAL - elapsed)

    bars = client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=tf,
        start=start_iso,
        end=end_iso,
        feed="iex",
    ))
    _last_alpaca_bar_ts = time.time()
    if symbol in bars.data:
        data = _normalize_df(bars.df.reset_index())
        if "time" in data.columns:
            latest    = pd.to_datetime(data["time"].iloc[-1])
            if latest.tzinfo is None:
                latest = ET.localize(latest)
            staleness = (datetime.datetime.now(ET) - latest).total_seconds()
            threshold = _staleness_threshold(interval)
            if staleness > threshold:
                log.warning(f"{symbol}: Alpaca data stale ({staleness:.0f}s > {threshold:.0f}s for {interval}) -- skipping")
            else:
                _record_ok_bars(symbol)
                with _bar_cache_lock:
                    _bar_cache[(symbol, period, interval)] = data
                return data
    return pd.DataFrame()

@retry(wait=wait_exponential(multiplier=1, min=1, max=3), stop=stop_after_attempt(2),
       retry=(retry_if_exception_type(Exception) & retry_if_not_exception_type(socket.timeout)))
def _get_bars_yfinance(symbol: str, period: str, interval: str, log) -> pd.DataFrame:
    import yfinance as yf
    yf_interval_map = {
        "1m": "1m", "2m": "2m", "5m": "5m", "15m": "15m",
        "30m": "30m", "60m": "60m", "90m": "90m", "1h": "1h",
        "1d": "1d", "5d": "5d", "1wk": "1wk", "1mo": "1mo",
    }
    yf_interval = yf_interval_map.get(interval, "1d")
    yf_period   = period if period.endswith(("d", "mo", "y", "wk")) else "5d"
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=yf_period, interval=yf_interval, auto_adjust=False)
    if not df.empty:
        df = _normalize_df(df.reset_index())
        _record_ok_bars(symbol)
        with _bar_cache_lock:
            _bar_cache[(symbol, period, interval)] = df
        return df
    return pd.DataFrame()


def get_bars(symbol: str, period: str = "5d", interval: str = "15m", bypass_cache: bool = False) -> pd.DataFrame:
    """Fetch OHLCV bars via Alpaca, then Yahoo Finance fallback.

    Results are cached per (symbol, period, interval) for the current scan
    cycle. Call clear_bar_cache() to reset at cycle start.

    2026-08-27, user request ("but the ema7 isn't above ema 15" / "it should
    have canceled the order"): this cache is scoped to the EQUITY SCAN's own
    cycle (cleared by clear_bar_cache() at scan_universe() start) -- but the
    SoftwareStopPoller thread's per-minute EMA-gate rechecks
    (check_pending_entries_ema, check_ema9_exit, _maybe_rearm_reentry, etc.)
    run on a totally separate cadence/thread whose entire job is detecting a
    trend CHANGE since the last check. Reading the scan's cache there is a
    scope mismatch: if a symbol wasn't refetched by a recent scan pass (e.g.
    briefly excluded from scan targets while a position is open), the poller
    could keep reading a stale snapshot for as long as that symbol goes
    unrefreshed -- confirmed live: BTDR's resting re-entry order sat
    unfilled 2026-08-27 11:05-11:18 ET while EMA7 was demonstrably below
    EMA15 the entire time (verified independently via both Alpaca's own IEX
    bars and yfinance), yet check_pending_entries_ema never cancelled it.
    bypass_cache=True skips the cache READ (still populates the cache after
    a fresh fetch, so scan-cycle consumers still benefit) -- used by every
    correctness-critical per-minute recheck in the poller thread.
    """
    symbol = symbol.strip().upper().lstrip("$")
    log = logging.getLogger("ApexTrader")


    # Always use yfinance for ^VIX (Alpaca does not support index symbols)
    if symbol == "^VIX":
        try:
            data = _get_bars_yfinance(symbol, period, interval, log)
            if not data.empty:
                return data
        except ImportError:
            log.warning("yfinance not installed -- cannot use fallback for ^VIX")
        except Exception as e:
            log.warning(f"^VIX: yfinance fetch failed: {e}")
        _record_empty_bars(symbol)
        return pd.DataFrame()
    if is_dead_ticker(symbol):
        return pd.DataFrame()

    cache_key = (symbol, period, interval)
    if not bypass_cache:
        with _bar_cache_lock:
            if cache_key in _bar_cache:
                log.debug(f"{symbol}: bar cache hit ({period}/{interval})")
                return _bar_cache[cache_key]

    # ---- Alpaca path with retry ----
    if ALPACA_AVAILABLE:
        try:
            data = _get_bars_alpaca(symbol, period, interval, log)
            if not data.empty:
                return data
        except Exception as e:
            log.warning(f"{symbol}: Alpaca fetch failed: {e}")

    # ---- yfinance fallback with retry ----
    try:
        data = _get_bars_yfinance(symbol, period, interval, log)
        if not data.empty:
            # _get_bars_alpaca applies a staleness check before returning --
            # this fallback path never did, so when Alpaca's feed went stale
            # (routine overnight/thin-liquidity) and fell through to here,
            # whatever yfinance had -- even hours old -- was returned as if it
            # were live, with no warning. Confirmed 2026-08-05: this let
            # VWAPFade compute an identical signal off frozen SOXS bars for
            # hours, driving repeated same-symbol re-entries. Same guard,
            # same threshold, same behavior as the Alpaca path now.
            if "time" in data.columns:
                latest = pd.to_datetime(data["time"].iloc[-1])
                if latest.tzinfo is None:
                    latest = ET.localize(latest)
                staleness = (datetime.datetime.now(ET) - latest).total_seconds()
                threshold = _staleness_threshold(interval)
                if staleness > threshold:
                    log.warning(f"{symbol}: yfinance data stale ({staleness:.0f}s > {threshold:.0f}s for {interval}) -- skipping")
                    _record_empty_bars(symbol)
                    return pd.DataFrame()
            return data
    except ImportError:
        log.warning("yfinance not installed -- cannot use fallback")
    except Exception as e:
        log.warning(f"{symbol}: yfinance fetch failed: {e}")


    _record_empty_bars(symbol)
    return pd.DataFrame()


def get_daily_volume_bars(symbol: str) -> pd.DataFrame:
    """3-month daily bars via yfinance specifically, for volume-based liquidity
    checks (avg daily volume / dollar volume guardrails).

    Was 5d (2026-08-06: widened per trader request) -- a 4-day trailing window
    (today's still-in-progress bar is dropped by the caller) meant a handful
    of unusually quiet days could block a symbol that normally trades well
    above the threshold, even though nothing about its real liquidity
    changed. 3mo reflects "does this stock normally trade above the
    threshold" rather than "was it quiet this specific week."

    Confirmed 2026-08-05: this account's Alpaca market data subscription is
    IEX-only ("subscription does not permit querying recent SIP data") -- IEX
    is a real exchange but typically only a few percent of a liquid stock's
    true total volume (SHOP showed ~700K/day on IEX vs its real tens of
    millions), so Alpaca's volume is unusable against thresholds calibrated
    for real market volume. yfinance reports full consolidated volume for
    free. Price/OHLC elsewhere (ATR tiers, execution) still uses Alpaca/IEX
    fine -- it's specifically volume that's this skewed, not price.
    """
    symbol = symbol.strip().upper().lstrip("$")
    log = logging.getLogger("ApexTrader")

    with _volume_bar_cache_lock:
        if symbol in _volume_bar_cache:
            return _volume_bar_cache[symbol]

    try:
        data = _get_bars_yfinance(symbol, "3mo", "1d", log)
        if not data.empty:
            with _volume_bar_cache_lock:
                _volume_bar_cache[symbol] = data
            return data
    except ImportError:
        log.warning("yfinance not installed -- volume guardrail checks unavailable")
    except Exception as e:
        log.warning(f"{symbol}: yfinance volume fetch failed: {e}")
    return pd.DataFrame()


def get_bars_batch(symbols, period: str = "5d", interval: str = "15m") -> Dict[str, pd.DataFrame]:
    """Fetch OHLCV bars for multiple symbols via Alpaca batch endpoint.

    Cache-backed: already-fetched symbols in the current cycle are returned
    immediately without a network call. Uncached symbols are batched in groups
    of 5 with 400 ms throttle between batches.
    """
    log     = logging.getLogger("ApexTrader")
    symbols = [s.strip().upper().lstrip("$") for s in symbols]
    results: Dict[str, pd.DataFrame] = {}
    uncached = []

    with _bar_cache_lock:
        for s in symbols:
            key = (s, period, interval)
            if key in _bar_cache:
                log.debug(f"{s}: bar cache hit ({period}/{interval}) [batch]")
                results[s] = _bar_cache[key]
            else:
                uncached.append(s)

    BATCH_SIZE   = 5
    THROTTLE_SEC = 0.4

    if ALPACA_AVAILABLE and uncached:
        try:
            client = get_data_client()
            tf     = _parse_timeframe(interval)
            days   = int(period[:-1]) if period.endswith("d") else 5
            start  = datetime.datetime.now(ET) - datetime.timedelta(days=days)

            for i in range(0, len(uncached), BATCH_SIZE):
                batch = uncached[i : i + BATCH_SIZE]
                try:
                    bars = client.get_stock_bars(StockBarsRequest(
                        symbol_or_symbols=batch, timeframe=tf, start=start,
                    ))
                except Exception as e:
                    log.debug(f"Alpaca batch failed for {batch}: {e}")
                    bars = {}

                for s in batch:
                    if isinstance(bars, dict) or s not in bars.data:
                        log.debug(f"{s}: Alpaca missing/stale [batch]")
                        continue
                    sym_df = bars.df[bars.df.index.get_level_values("symbol") == s]
                    if sym_df.empty:
                        log.debug(f"{s}: Alpaca missing/stale [batch]")
                        continue
                    data = _normalize_df(sym_df.reset_index())
                    if "time" in data.columns and not data.empty:
                        latest    = pd.to_datetime(data["time"].iloc[-1])
                        if latest.tzinfo is None:
                            latest = ET.localize(latest)
                        staleness = (datetime.datetime.now(ET) - latest).total_seconds()
                        threshold = _staleness_threshold(interval)
                        if staleness > threshold:
                            log.warning(f"{s}: Alpaca data stale ({staleness:.0f}s > {threshold:.0f}s for {interval}) [batch]")
                            continue
                    _record_ok_bars(s)
                    results[s] = data
                    with _bar_cache_lock:
                        _bar_cache[(s, period, interval)] = data
                time.sleep(THROTTLE_SEC)
        except Exception as e:
            log.debug(f"Alpaca batch outer failure: {e}")

    # Fill missing entries with empty DataFrame
    for s in symbols:
        if s not in results:
            _record_empty_bars(s)
            results[s] = pd.DataFrame()
    return results


def get_price(symbol: str) -> float:
    """Return the latest close price for symbol, or 0.0 on failure."""
    try:
        data = get_bars(symbol, "1d", "1m")
        return float(data["close"].iloc[-1]) if not data.empty else 0.0
    except Exception:
        return 0.0


def get_premarket_bars(symbol: str) -> pd.DataFrame:
    """Fetch today's 1-min bars from 4:00 AM ET (pre-market included).

    Cached under a '_prepost' period key -- invalidated by clear_bar_cache().
    """
    log       = logging.getLogger("ApexTrader")
    cache_key = (symbol, "1d_prepost", "1m")
    with _bar_cache_lock:
        if cache_key in _bar_cache:
            return _bar_cache[cache_key]

    result = pd.DataFrame()
    if ALPACA_AVAILABLE:
        try:
            client = get_data_client()
            now_et = datetime.datetime.now(ET)
            start  = now_et.replace(hour=4, minute=0, second=0, microsecond=0)
            bars   = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=start,
            ))
            if symbol in bars.data:
                result = _normalize_df(bars.df.reset_index())
                log.debug(f"get_premarket_bars({symbol}): {len(result)} bars")
        except Exception as e:
            log.debug(f"get_premarket_bars({symbol}): failed: {e}")

    if result.empty:
        _record_empty_bars(symbol)
    else:
        _record_ok_bars(symbol)

    with _bar_cache_lock:
        _bar_cache[cache_key] = result
    return result


# ---- Finnhub bar source ----------------------------------------------------------------------------------------------------------------

def get_finnhub_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
    """Fetch OHLCV bars from Finnhub (alternative data source)."""
    from engine.config import FINNHUB_API_KEY
    if not FINNHUB_API_KEY:
        raise ValueError("FINNHUB_API_KEY not configured")

    try:
        import finnhub
    except ImportError:
        raise ImportError("finnhub-python is not installed")

    resolution_map = {
        "1m": "1", "5m": "5", "15m": "15", "30m": "30",
        "60m": "60", "1h": "60", "1d": "D",
    }
    resolution = resolution_map.get(interval.lower())
    if resolution is None:
        raise ValueError(f"Unsupported Finnhub interval: {interval}")

    now_utc  = datetime.datetime.now(datetime.timezone.utc)
    days     = int(period[:-1]) if period.endswith("d") else 5
    start    = now_utc - datetime.timedelta(days=days)
    client   = finnhub.Client(api_key=FINNHUB_API_KEY)
    data     = client.stock_candles(symbol, resolution, int(start.timestamp()), int(now_utc.timestamp()))
    if data.get("s") != "ok":
        raise RuntimeError(f"Finnhub error for {symbol}: {data.get('s')}")

    df = pd.DataFrame({
        "time":   data.get("t", []),
        "open":   data.get("o", []),
        "high":   data.get("h", []),
        "low":    data.get("l", []),
        "close":  data.get("c", []),
        "volume": data.get("v", []),
    })
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_convert(ET)
    return df


# ---- Technical indicators ------------------------------------------------------------------------------------------------------------

def calc_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    delta = prices.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = -delta.clip(upper=0).rolling(period).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))


def calc_macd(prices: pd.Series) -> Dict:
    exp1   = prices.ewm(span=12, adjust=False).mean()
    exp2   = prices.ewm(span=26, adjust=False).mean()
    macd   = exp1 - exp2
    signal = macd.ewm(span=9, adjust=False).mean()
    return {"macd": macd, "signal": signal, "hist": macd - signal}


def calculate_atr(bars: pd.DataFrame, period: int = 14) -> float:
    """Compute Average True Range over the last `period` bars. Returns 0.0 on failure."""
    if bars.empty or len(bars) < period:
        return 0.0
    try:
        hl  = bars["high"] - bars["low"]
        hc  = (bars["high"] - bars["close"].shift()).abs()
        lc  = (bars["low"]  - bars["close"].shift()).abs()
        tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean().iloc[-1]
        return float(atr) if not pd.isna(atr) else 0.0
    except Exception:
        return 0.0


def _demo() -> None:
    """Self-check for the dead-ticker suppression state machine."""
    global _DEAD_TICKER_THRESHOLD, _DEAD_TICKER_RECHECK_SEC
    _dead_ticker_hits.clear()
    _dead_tickers.clear()
    _DEAD_TICKER_THRESHOLD = 3
    _DEAD_TICKER_RECHECK_SEC = 900

    assert not is_dead_ticker("XYZ"), "fresh symbol must not start suppressed"
    for _ in range(2):
        _record_empty_bars("XYZ")
    assert not is_dead_ticker("XYZ"), "below threshold must not suppress yet"
    _record_empty_bars("XYZ")  # 3rd consecutive miss
    assert is_dead_ticker("XYZ"), "threshold hits must suppress"

    _record_ok_bars("XYZ")
    assert not is_dead_ticker("XYZ"), "a fresh bar must clear suppression immediately"

    for _ in range(3):
        _record_empty_bars("ABC")
    assert is_dead_ticker("ABC")
    _dead_tickers["ABC"] = time.time() - _DEAD_TICKER_RECHECK_SEC - 1
    assert not is_dead_ticker("ABC"), "recheck window must let a probe back through"

    print("bars._demo: all assertions passed")


if __name__ == "__main__":
    _demo()

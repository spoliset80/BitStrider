"""
engine.utils.bars
-----------------
Bar data fetching, per-cycle cache, technical indicators (RSI, MACD, ATR).

All public functions here are re-exported from engine.utils for backward compat.
"""

from __future__ import annotations


import datetime
import logging
import threading
import time
from typing import Dict, Tuple

# Add tenacity for retry logic
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

import pandas as pd
import pytz

ET = pytz.timezone("America/New_York")

# ── Alpaca SDK availability ───────────────────────────────────────────────────
try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.historical import OptionHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False

# ── Market Data App availability ────────────────────────────────────────────
import os as _os
_MDA_API_KEY: str | None = _os.environ.get("MARKETDATA_API_KEY") or None
_MDA_AVAILABLE: bool = bool(_MDA_API_KEY)

# ── Per-cycle bar cache ───────────────────────────────────────────────────────
# Keyed by (symbol, period, interval). Thread-safe via lock.
_bar_cache: Dict[Tuple[str, str, str], pd.DataFrame] = {}
_bar_cache_lock = threading.Lock()

_ALPACA_MIN_INTERVAL = 0.35   # per-symbol throttle to reduce 429s
_last_alpaca_bar_ts: float = 0.0

_data_client = None
_option_data_client = None

# ── Feed tracking ────────────────────────────────────────────────────────────
# _last_feed_used: records which feed was used per symbol in this cycle.
# Cleared by clear_bar_cache() at the start of each scan cycle.
_last_feed_used: Dict[str, str] = {}

# ── Dead ticker suppression (disabled — all tickers eligible) ─────────────────
_DEAD_TICKER_THRESHOLD = 999_999
_dead_ticker_hits: Dict[str, int] = {}
_dead_tickers: set = set()
_dead_ticker_lock = threading.Lock()


def _record_empty_bars(symbol: str) -> None:
    """No-op stub — suppression disabled."""
    return


def _record_ok_bars(symbol: str) -> None:
    """No-op stub."""
    return


def is_dead_ticker(symbol: str) -> bool:
    """Always False — dead-ticker suppression is disabled."""
    return False


def clear_bar_cache() -> None:
    """Flush the per-cycle bar cache. Call once at the start of each scan cycle."""
    global _bar_cache, _last_feed_used
    with _bar_cache_lock:
        _bar_cache = {}
        _last_feed_used = {}


def get_feed_used(symbol: str) -> str:
    """Return the data feed used for the last bar fetch of *symbol* this cycle.

    Returns 'sip' or 'iex'. Defaults to 'iex' when unknown (conservative —
    IEX-adjusted thresholds are applied if the feed is uncertain).
    """
    return _last_feed_used.get(symbol.strip().upper(), "iex")


# ── Alpaca client singletons ──────────────────────────────────────────────────

def get_data_client() -> "StockHistoricalDataClient":
    global _data_client
    if _data_client is None:
        from engine.config import API_KEY, API_SECRET
        if not API_KEY or not API_SECRET:
            raise ValueError("Alpaca API credentials not found in environment")
        _data_client = StockHistoricalDataClient(API_KEY, API_SECRET)
        # Increase connection pool to match the 24-worker parallel prefetch
        # (urllib3 default is 10 — causes 'pool full, discarding connection' at 24 workers)
        try:
            from requests.adapters import HTTPAdapter
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=30)
            _data_client._session.mount("https://", adapter)
            _data_client._session.mount("http://", adapter)
        except Exception:
            pass
    return _data_client


def get_option_data_client() -> "OptionHistoricalDataClient":
    global _option_data_client
    if _option_data_client is None:
        from engine.config import API_KEY, API_SECRET
        if not API_KEY or not API_SECRET:
            raise ValueError("Alpaca API credentials not found in environment")
        _option_data_client = OptionHistoricalDataClient(API_KEY, API_SECRET)
        try:
            from requests.adapters import HTTPAdapter
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=30)
            _option_data_client._session.mount("https://", adapter)
            _option_data_client._session.mount("http://", adapter)
        except Exception:
            pass
    return _option_data_client


# ── TimeFrame helper ──────────────────────────────────────────────────────────

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


# ── Core bar fetch ────────────────────────────────────────────────────────────

def _parse_mda_resolution(interval: str) -> str | None:
    """Map bot interval strings to Market Data App resolution codes."""
    _map = {
        "1m": "1", "2m": "2", "3m": "3", "5m": "5",
        "15m": "15", "30m": "30", "60m": "60", "1h": "60",
        "1d": "D", "1D": "D", "D": "D",
        "1wk": "W", "1mo": "M",
    }
    return _map.get(interval)


def _get_bars_marketdata(symbol: str, period: str, interval: str, log) -> pd.DataFrame:
    """Fetch OHLCV bars from Market Data App API."""
    try:
        import requests as _req
    except ImportError:
        return pd.DataFrame()

    resolution = _parse_mda_resolution(interval)
    if resolution is None:
        return pd.DataFrame()  # unsupported interval — fall through to next source

    days = int(period[:-1]) if period.endswith("d") else 5
    now_et = datetime.datetime.now(ET)
    from_dt = (now_et - datetime.timedelta(days=days)).date()
    to_dt   = now_et.date()

    # Reload key from env at call time so hot-reload works
    key = _os.environ.get("MARKETDATA_API_KEY") or _MDA_API_KEY
    if not key:
        return pd.DataFrame()

    url = f"https://api.marketdata.app/v1/stocks/candles/{resolution}/{symbol}/"
    try:
        r = _req.get(url,
            params={"from": str(from_dt), "to": str(to_dt)},
            headers={"Authorization": f"Bearer {key}"},
            timeout=12)
    except Exception as e:
        log.debug(f"{symbol}: MDA bars request error: {e}")
        return pd.DataFrame()

    if r.status_code not in (200, 203):
        log.debug(f"{symbol}: MDA bars status {r.status_code}")
        return pd.DataFrame()

    data = r.json()
    if data.get("s") != "ok" or not data.get("t"):
        log.debug(f"{symbol}: MDA bars empty response")
        return pd.DataFrame()

    df = pd.DataFrame({
        "time":   pd.to_datetime(data["t"], unit="s", utc=True).tz_convert(ET),
        "open":   data["o"],
        "high":   data["h"],
        "low":    data["l"],
        "close":  data["c"],
        "volume": data.get("v", [0] * len(data["t"])),
    })
    _last_feed_used[symbol] = "mda"
    with _bar_cache_lock:
        _bar_cache[(symbol, period, interval)] = df
    log.debug(f"{symbol}: MDA bars OK — {len(df)} rows ({period}/{interval})")
    return df


@retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3),
       retry=retry_if_exception_type(Exception))
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

    from engine.config import ALPACA_DATA_FEED
    feed_used = ALPACA_DATA_FEED  # "sip" for paid subscribers, "iex" for free tier
    bars = client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=tf,
        start=start_iso,
        end=end_iso,
        feed=feed_used,
    ))
    _last_alpaca_bar_ts = time.time()
    _last_feed_used[symbol] = feed_used
    if symbol in bars:
        data = _normalize_df(bars[symbol].df.reset_index())
        if not data.empty and "time" in data.columns:
            # Forward-fill missing bars (close, volume, etc.)
            data = data.ffill()
            latest = pd.to_datetime(data["time"].iloc[-1])
            if latest.tzinfo is None:
                latest = ET.localize(latest)
            staleness = (datetime.datetime.now(ET) - latest).total_seconds()
            if interval.endswith("m") and staleness > 120:
                log.warning(f"{symbol}: Alpaca data stale ({staleness:.0f}s) — skipping")
            else:
                _record_ok_bars(symbol)
                with _bar_cache_lock:
                    _bar_cache[(symbol, period, interval)] = data
                return data
        else:
            log.warning(f"{symbol}: Alpaca returned empty or malformed data.")
    return pd.DataFrame()

@retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3),
       retry=retry_if_exception_type(Exception))
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

def get_bars(symbol: str, period: str = "5d", interval: str = "15m") -> pd.DataFrame:
    """Fetch OHLCV bars via Alpaca (yfinance fallback).

    Results are cached per (symbol, period, interval) for the current scan
    cycle. Call clear_bar_cache() to reset at cycle start.
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
            log.warning("yfinance not installed — cannot use fallback for ^VIX")
        except Exception as e:
            log.warning(f"^VIX: yfinance fetch failed: {e}")
        _record_empty_bars(symbol)
        return pd.DataFrame()
    if is_dead_ticker(symbol):
        return pd.DataFrame()

    cache_key = (symbol, period, interval)
    with _bar_cache_lock:
        if cache_key in _bar_cache:
            log.debug(f"{symbol}: bar cache hit ({period}/{interval})")
            return _bar_cache[cache_key]

    # ── Market Data App path (primary) ──
    if _MDA_AVAILABLE and symbol != "^VIX":
        try:
            data = _get_bars_marketdata(symbol, period, interval, log)
            if not data.empty:
                return data
        except Exception as e:
            log.debug(f"{symbol}: MDA bars failed: {e}")

    # ── Alpaca path with retry ──
    if ALPACA_AVAILABLE:
        try:
            data = _get_bars_alpaca(symbol, period, interval, log)
            if not data.empty:
                return data
        except Exception as e:
            log.warning(f"{symbol}: Alpaca fetch failed: {e}")

    # ── yfinance fallback with retry ──
    try:
        data = _get_bars_yfinance(symbol, period, interval, log)
        if not data.empty:
            return data
    except ImportError:
        log.warning("yfinance not installed — cannot use fallback")
    except Exception as e:
        log.warning(f"{symbol}: yfinance fetch failed: {e}")

    _record_empty_bars(symbol)
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

    # ── Market Data App batch path (primary) ──
    if _MDA_AVAILABLE and uncached:
        mda_failed = []
        for s in uncached:
            try:
                data = _get_bars_marketdata(s, period, interval, log)
                if not data.empty:
                    results[s] = data
                else:
                    mda_failed.append(s)
            except Exception as e:
                log.debug(f"{s}: MDA batch bars failed: {e}")
                mda_failed.append(s)
        uncached = mda_failed

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
                    if s not in bars:
                        log.debug(f"{s}: Alpaca missing/stale [batch]")
                        continue
                    data = _normalize_df(bars[s].df.reset_index())
                    if "time" in data.columns and not data.empty:
                        latest    = pd.to_datetime(data["time"].iloc[-1])
                        if latest.tzinfo is None:
                            latest = ET.localize(latest)
                        staleness = (datetime.datetime.now(ET) - latest).total_seconds()
                        if interval.endswith("m") and staleness > 120:
                            log.warning(f"{s}: Alpaca data stale ({staleness:.0f}s) [batch]")
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

    Cached under a '_prepost' period key — invalidated by clear_bar_cache().
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
            if symbol in bars:
                result = _normalize_df(bars[symbol].df.reset_index())
                log.debug(f"get_premarket_bars({symbol}): {len(result)} bars")
        except Exception as e:
            log.debug(f"get_premarket_bars({symbol}): failed: {e}")

    with _bar_cache_lock:
        _bar_cache[cache_key] = result
    return result


# ── Finnhub bar source ────────────────────────────────────────────────────────

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


# ── Technical indicators ──────────────────────────────────────────────────────

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

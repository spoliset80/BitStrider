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

# ── Market Data App availability ────────────────────────────────────────────
import os as _os
_MDA_API_KEY: str | None = _os.environ.get("MARKETDATA_API_KEY") or None
_MDA_AVAILABLE: bool = bool(_MDA_API_KEY)

# ── Per-cycle bar cache ───────────────────────────────────────────────────────
# Keyed by (symbol, period, interval). Thread-safe via lock.
_bar_cache: Dict[Tuple[str, str, str], pd.DataFrame] = {}
_bar_cache_lock = threading.Lock()

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


# ── TimeFrame helper (for other sources) ─────────────────────────────────────


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




def _get_bars_schwab(symbol: str, period: str, interval: str, log) -> pd.DataFrame:
    """Fetch OHLCV bars via Schwab API."""
    try:
        from engine.broker.schwab_client import get_schwab_market_data_client
    except ImportError:
        return pd.DataFrame()
    
    try:
        client = get_schwab_market_data_client()
        
        # Map our period/interval to Schwab parameters
        # period: "5d", "20d", "65d" etc
        # interval: "1m", "5m", "15m", "30m", "60m", "1h", "1d"
        
        days_match = int(period[:-1]) if period.endswith("d") else 5
        
        # Schwab frequencyType options: minute, daily, weekly, monthly
        if interval.endswith("m"):
            frequency_type = "minute"
            frequency = int(interval[:-1])
            period_type = "day"
            period_val = days_match if days_match <= 10 else 10  # Max 10 days for intraday
        elif interval in ("1h", "60m"):
            frequency_type = "minute"
            frequency = 60
            period_type = "day"
            period_val = days_match if days_match <= 10 else 10
        else:  # daily or longer
            frequency_type = "daily"
            frequency = 1
            period_type = "month"
            period_val = (days_match + 19) // 20  # ~20 days per month
        
        response = client.get_candles(
            symbol,
            period_type=period_type,
            period=period_val,
            frequency_type=frequency_type,
            frequency=frequency
        )
        
        if not response or "candles" not in response or not response["candles"]:
            return pd.DataFrame()
        
        candles = response["candles"]
        
        # Normalize Schwab candle format to our standard format
        df = pd.DataFrame({
            "time": pd.to_datetime([c.get("datetime") for c in candles], unit="ms", utc=True).tz_convert(ET),
            "open": [c.get("open", 0) for c in candles],
            "high": [c.get("high", 0) for c in candles],
            "low": [c.get("low", 0) for c in candles],
            "close": [c.get("close", 0) for c in candles],
            "volume": [c.get("volume", 0) for c in candles],
        })
        
        if not df.empty:
            _last_feed_used[symbol] = "schwab"
            _record_ok_bars(symbol)
            with _bar_cache_lock:
                _bar_cache[(symbol, period, interval)] = df
            log.debug(f"{symbol}: Schwab bars OK — {len(df)} rows ({period}/{interval})")
            return df
        
        return pd.DataFrame()
    
    except Exception as e:
        log.debug(f"{symbol}: Schwab bars failed: {e}")
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

    # ── Schwab path (primary Alpaca replacement) ──
    try:
        data = _get_bars_schwab(symbol, period, interval, log)
        if not data.empty:
            return data
    except Exception as e:
        log.debug(f"{symbol}: Schwab fetch failed: {e}")

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
    Currently uses Schwab via get_bars() as the fallback.
    """
    log       = logging.getLogger("ApexTrader")
    cache_key = (symbol, "1d_prepost", "1m")
    with _bar_cache_lock:
        if cache_key in _bar_cache:
            return _bar_cache[cache_key]

    # Try Schwab/MDA via the standard get_bars() path
    result = get_bars(symbol, period="1d", interval="1m")

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

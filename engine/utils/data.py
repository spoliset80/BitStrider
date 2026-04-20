"""
engine.utils.data
-----------------
External data integrations: Finnhub trending discovery, sentiment gate,
logging setup, and small env/format helpers.

All public functions here are re-exported from engine.utils for backward compat.
"""

from __future__ import annotations

import logging
import os
from typing import List, Tuple

# ── Finnhub SDK availability ──────────────────────────────────────────────────
try:
    import finnhub as _finnhub_mod
    FINNHUB_SDK_AVAILABLE = True
except ImportError:
    FINNHUB_SDK_AVAILABLE = False


# ── Env / format helpers ──────────────────────────────────────────────────────

def bool_env(name: str, default: str = "false") -> bool:
    """Parse a boolean environment variable. Truthy: '1', 'true', 'yes'."""
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


def get_env(name: str, default: str = "") -> str:
    """Return stripped env var value or *default*."""
    return os.getenv(name, default).strip()


def format_currency(value: float) -> str:
    return f"${value:,.2f}"


# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    """Configure root logger with console + rotating file handlers.

    Level is driven by APEXTRADER_LOG_LEVEL env var (default INFO).
    Safe to call multiple times — duplicate handlers are removed first.
    """
    from logging.handlers import TimedRotatingFileHandler

    fmt       = "%(asctime)s [%(levelname)s] %(message)s"
    formatter = logging.Formatter(fmt)
    root      = logging.getLogger()

    level_name = os.getenv("APEXTRADER_LOG_LEVEL", "INFO").upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))

    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    file_h = TimedRotatingFileHandler(
        filename="apextrader.log",
        when="midnight", interval=1, backupCount=14,
        encoding="utf-8", delay=True, utc=False,
    )
    file_h.setFormatter(formatter)
    file_h.suffix = "%Y-%m-%d"

    root.addHandler(console)
    root.addHandler(file_h)

    for noisy in ("urllib3", "selenium", "webdriver_manager", "WDM"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("ApexTrader")


# ── Finnhub client ────────────────────────────────────────────────────────────

def get_finnhub_client():
    from engine.config import FINNHUB_API_KEY
    if not FINNHUB_API_KEY:
        raise ValueError("FINNHUB_API_KEY not configured")
    if not FINNHUB_SDK_AVAILABLE:
        raise ImportError("finnhub-python is not installed")
    return _finnhub_mod.Client(api_key=FINNHUB_API_KEY)


# ── Trending discovery ────────────────────────────────────────────────────────

def get_trending_tickers(max_results: int = 20) -> List[str]:
    """Trending ticker discovery. Returns empty list — external screener removed."""
    return []


def filter_trending_momentum(
    trending_tickers: list, min_momentum_pct: float = 3.0
) -> list:
    """Filter a list of ticker dicts to those with >= min_momentum_pct 5-day move."""
    from engine.utils.bars import get_bars
    filtered = []
    for symbol in trending_tickers:
        try:
            bars = get_bars(symbol, "5d", "1d")
            if bars.empty or len(bars) < 2:
                continue
            cur = float(bars["close"].iloc[-1])
            old = float(bars["close"].iloc[0])
            if old <= 0:
                continue
            mom = ((cur - old) / old) * 100
            if mom >= min_momentum_pct:
                filtered.append({"symbol": symbol, "momentum_pct": mom, "current_price": cur})
        except Exception:
            continue
    filtered.sort(key=lambda x: x["momentum_pct"], reverse=True)
    return filtered


def get_finnhub_trending_tickers() -> List[str]:
    """Parse Finnhub general news for mentioned ticker symbols."""
    from engine.config import FINNHUB_API_KEY
    log = logging.getLogger("ApexTrader")
    if not FINNHUB_API_KEY:
        log.warning("FINNHUB_API_KEY not set — skipping Finnhub trending")
        return []
    try:
        import requests
        resp = requests.get(
            f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_API_KEY}",
            timeout=10,
        )
        resp.raise_for_status()
        symbols: set = set()
        for item in resp.json()[:50]:
            for s in item.get("related", "").split(","):
                s = s.strip().upper()
                if s and s.isalpha() and 1 <= len(s) <= 5:
                    symbols.add(s)
        return list(symbols)
    except Exception as e:
        log.error(f"get_finnhub_trending_tickers failed: {e}")
        return []


def check_sentiment_gate(ticker: str) -> Tuple[bool, float]:
    """Return (passes_gate, bullish_pct) from Alpaca News headline sentiment.

    Scores the last 10 news headlines for *ticker* using keyword matching.
    Returns (True, 0.5) when credentials are absent or the call fails —
    defaulting to allow so a news outage never blocks all trades.
    """
    from engine import config as _cfg
    from engine.config import SENTIMENT_BULLISH_THRESHOLD
    if not _cfg.API_KEY or not _cfg.API_SECRET:
        return True, 0.5
    try:
        import requests as _req
        resp = _req.get(
            "https://data.alpaca.markets/v1beta1/news",
            params={"symbols": ticker, "limit": 10, "sort": "desc"},
            headers={
                "APCA-API-KEY-ID": _cfg.API_KEY,
                "APCA-API-SECRET-KEY": _cfg.API_SECRET,
            },
            timeout=5,
        )
        resp.raise_for_status()
        articles = resp.json().get("news", [])
        if not articles:
            return True, 0.5

        _BULLISH = {"upgrade", "beat", "surge", "raises", "record", "strong", "buy", "outperform"}
        _BEARISH = {"downgrade", "miss", "decline", "cut", "weak", "sell", "underperform",
                    "loss", "warning", "recall"}
        bullish = bearish = 0
        for art in articles:
            headline = art.get("headline", "").lower()
            if any(w in headline for w in _BULLISH):
                bullish += 1
            elif any(w in headline for w in _BEARISH):
                bearish += 1

        total = bullish + bearish
        bullish_pct = bullish / max(total, 1)
        if total == 0:
            return True, 0.5
        return bullish_pct >= SENTIMENT_BULLISH_THRESHOLD, bullish_pct
    except Exception:
        return True, 0.5

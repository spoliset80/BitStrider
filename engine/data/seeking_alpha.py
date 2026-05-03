"""
engine.data.seeking_alpha
-------------------------
Seeking Alpha Finance API (via RapidAPI) client.

Provides:
  get_sa_quant_rating(ticker)       → float | None   (1=StrongSell … 5=StrongBuy)
  get_sa_news_sentiment(ticker)     → (bullish_pct, n_articles)
  get_sa_trending_tickers(size)     → List[str]

All results are cached to minimise RapidAPI quota usage:
  • Ratings     : 4-hour cache  (changes infrequently)
  • News        : 15-minute cache
  • Trending    : 10-minute cache

When SEEKING_ALPHA_API_KEY is absent or a call fails the function returns None
(ratings) or an empty/neutral result — callers must handle the fallback.
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("ApexTrader")

# ── Cache stores ──────────────────────────────────────────────────────────────
_rating_cache:   Dict[str, Tuple[float, float]]              = {}  # ticker → (ts, rating)
_news_cache:     Dict[str, Tuple[float, float, int]]         = {}  # ticker → (ts, bullish_pct, n)
_trending_cache: Tuple[float, List[str]]                     = (0.0, [])

_RATING_TTL   = 4 * 3600   # 4 h
_NEWS_TTL     = 900        # 15 min
_TRENDING_TTL = 600        # 10 min

# ── SA rating scale ───────────────────────────────────────────────────────────
# SA quant ratings use a 1–5 scale:
#   5.0 = Strong Buy   4.0 = Buy   3.0 = Hold   2.0 = Sell   1.0 = Strong Sell
# We normalise to [0, 1] bullish_pct: (rating - 1) / 4
#   → 5 = 1.00, 4 = 0.75, 3 = 0.50, 2 = 0.25, 1 = 0.00

_BULLISH_KW = frozenset({
    "upgrade", "beat", "beats", "surge", "surges", "raised", "raises", "record",
    "strong", "buy", "outperform", "growth", "bullish", "upside", "overweight",
    "positive", "higher", "gains", "rally", "momentum", "exceeds",
})
_BEARISH_KW = frozenset({
    "downgrade", "miss", "misses", "decline", "cut", "weak", "sell",
    "underperform", "loss", "warning", "recall", "bearish", "downside",
    "underweight", "negative", "lower", "drops", "slump", "layoffs", "worse",
})


def _headers() -> Optional[dict]:
    """Return RapidAPI headers, or None when key is absent."""
    from engine.config import SEEKING_ALPHA_API_KEY, SEEKING_ALPHA_HOST
    if not SEEKING_ALPHA_API_KEY:
        return None
    return {
        "x-rapidapi-key":  SEEKING_ALPHA_API_KEY,
        "x-rapidapi-host": SEEKING_ALPHA_HOST,
    }


def _base() -> str:
    from engine.config import SEEKING_ALPHA_HOST
    # strip any accidental whitespace; default host is seeking-alpha.p.rapidapi.com
    return f"https://{SEEKING_ALPHA_HOST.strip()}"


# ── Public API ────────────────────────────────────────────────────────────────

def get_sa_quant_rating(ticker: str) -> Optional[float]:
    """Return SA quant rating (1–5) for *ticker*, or None on failure.

    Uses `/v2/symbols/get-ratings` endpoint.
    Rating meaning: 5=StrongBuy 4=Buy 3=Hold 2=Sell 1=StrongSell
    """
    hdrs = _headers()
    if hdrs is None:
        return None

    sym = ticker.upper()
    now = time.monotonic()
    cached = _rating_cache.get(sym)
    if cached and (now - cached[0]) < _RATING_TTL:
        return cached[1]

    try:
        import requests as _req
        resp = _req.get(
            f"{_base()}/v2/symbols/get-ratings",
            params={"symbols": sym},
            headers=hdrs,
            timeout=6,
        )
        resp.raise_for_status()
        data = resp.json()
        # Response: {"data": [{"id": "AAPL", "attributes": {"quantRating": 3.52, ...}}]}
        items = data.get("data", [])
        if not items:
            return None
        attrs = items[0].get("attributes", {})
        rating = attrs.get("quantRating")
        if rating is None:
            return None
        rating = float(rating)
        _rating_cache[sym] = (now, rating)
        log.debug(f"[SA] {sym} quant_rating={rating:.2f}")
        return rating
    except Exception as e:
        log.debug(f"[SA] get_sa_quant_rating({sym}) failed: {e}")
        return None


def get_sa_news_sentiment(ticker: str, size: int = 20) -> Tuple[float, int]:
    """Return (bullish_pct, n_articles) from SA news headlines for *ticker*.

    Falls back to (0.5, 0) on any error — neutral, no block.
    Uses `/news/v2/list` endpoint.
    """
    hdrs = _headers()
    if hdrs is None:
        return 0.5, 0

    sym  = ticker.upper()
    slug = sym.lower()   # SA uses lowercase slugs
    now  = time.monotonic()
    cached = _news_cache.get(sym)
    if cached and (now - cached[0]) < _NEWS_TTL:
        return cached[1], cached[2]

    try:
        import requests as _req
        resp = _req.get(
            f"{_base()}/news/v2/list",
            params={"id": slug, "size": size, "until": 0},
            headers=hdrs,
            timeout=6,
        )
        resp.raise_for_status()
        articles = resp.json().get("data", [])
        if not articles:
            _news_cache[sym] = (now, 0.5, 0)
            return 0.5, 0

        bullish = bearish = 0
        for art in articles:
            headline = art.get("attributes", {}).get("title", "").lower()
            if any(w in headline for w in _BULLISH_KW):
                bullish += 1
            elif any(w in headline for w in _BEARISH_KW):
                bearish += 1

        total     = bullish + bearish
        bpct      = bullish / max(total, 1) if total else 0.5
        _news_cache[sym] = (now, bpct, total)
        log.debug(f"[SA] {sym} news: bullish={bullish} bearish={bearish} pct={bpct:.2f}")
        return bpct, total
    except Exception as e:
        log.debug(f"[SA] get_sa_news_sentiment({sym}) failed: {e}")
        return 0.5, 0


def get_sa_trending_tickers(size: int = 40) -> List[str]:
    """Return a deduplicated list of tickers from SA trending news.

    Uses `/news/v2/list-trending` endpoint.
    Falls back to [] on any failure.
    """
    hdrs = _headers()
    if hdrs is None:
        return []

    now = time.monotonic()
    global _trending_cache
    if (now - _trending_cache[0]) < _TRENDING_TTL:
        return list(_trending_cache[1])

    try:
        import requests as _req
        resp = _req.get(
            f"{_base()}/news/v2/list-trending",
            params={"size": size, "since": 0},
            headers=hdrs,
            timeout=6,
        )
        resp.raise_for_status()
        articles = resp.json().get("data", [])
        tickers: List[str] = []
        for art in articles:
            # primary: tickers field in attributes
            attrs = art.get("attributes", {})
            for t in attrs.get("tickers", []):
                sym = str(t.get("symbol") or t).upper().strip()
                if sym and sym.isalpha() and 1 <= len(sym) <= 5:
                    tickers.append(sym)
            # secondary: parse from related field if present
            for s in str(attrs.get("related", "")).split(","):
                s = s.strip().upper()
                if s and s.isalpha() and 1 <= len(s) <= 5:
                    tickers.append(s)

        # deduplicate preserving order
        seen: set = set()
        unique = [t for t in tickers if not (t in seen or seen.add(t))]
        _trending_cache = (now, unique)
        log.debug(f"[SA] Trending tickers ({len(unique)}): {unique[:10]}")
        return unique
    except Exception as e:
        log.debug(f"[SA] get_sa_trending_tickers failed: {e}")
        return []


def sa_sentiment_gate(ticker: str) -> Tuple[bool, float]:
    """High-level sentiment gate using SA quant ratings then SA news as fallback.

    Returns (passes_gate, bullish_pct).
    - Primary  : SA quant rating (most authoritative — analyst consensus)
    - Secondary: SA news headline sentiment
    - Always returns (True, 0.5) on complete failure so a key outage never
      blocks all trades.
    """
    from engine.config import SENTIMENT_BULLISH_THRESHOLD

    # 1. Try quant rating
    rating = get_sa_quant_rating(ticker)
    if rating is not None:
        bullish_pct = (rating - 1.0) / 4.0   # normalise [1,5] → [0,1]
        passes = bullish_pct >= SENTIMENT_BULLISH_THRESHOLD
        log.debug(f"[SA] {ticker} quant_gate: rating={rating:.2f} bullish_pct={bullish_pct:.2f} passes={passes}")
        return passes, bullish_pct

    # 2. Fall back to SA news headlines
    bpct, n = get_sa_news_sentiment(ticker)
    if n > 0:
        passes = bpct >= SENTIMENT_BULLISH_THRESHOLD
        log.debug(f"[SA] {ticker} news_gate: bullish_pct={bpct:.2f} n={n} passes={passes}")
        return passes, bpct

    # 3. No SA data available — allow by default
    return True, 0.5

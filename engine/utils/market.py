"""
engine.utils.market
-------------------
Market-hours detection, VIX, adaptive interval calculations, and market sentiment.

All public functions here are re-exported from engine.utils for backward compat.
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import Optional, Tuple

import pytz

ET = pytz.timezone("America/New_York")

# ── Market hours ──────────────────────────────────────────────────────────────

def is_market_open() -> bool:
    """Extended hours: 7:00 AM – 8:00 PM ET, weekdays only."""
    now = datetime.datetime.now(ET)
    if now.weekday() >= 5:
        return False
    t = now.strftime("%H:%M")
    return "07:00" <= t <= "20:00"


def is_regular_hours() -> bool:
    """Regular session: 9:30 AM – 4:00 PM ET, weekdays only."""
    now = datetime.datetime.now(ET)
    if now.weekday() >= 5:
        return False
    t = now.strftime("%H:%M")
    return "09:30" <= t <= "16:00"


def is_options_lull_hours() -> bool:
    """True during low-liquidity windows where options spreads are typically wide.

    Blocks new option *entries* during:
      - Open auction (9:30–9:35 ET): price discovery unstable, spreads widest
      - Midday lull  (11:30–13:45 ET): low volume, inflated spreads

    Monitoring and exits are never blocked. Paper mode always returns False.
    """
    from engine.config import PAPER
    if PAPER:
        return False
    now = datetime.datetime.now(ET)
    if now.weekday() >= 5:
        return False
    h = now.hour + now.minute / 60.0
    return (9.5 <= h < (9.5 + 5 / 60.0)) or (11.5 <= h < 13.75)


def is_open_window() -> bool:
    """True during 9:35–9:45 ET — post-auction, pre-trend-lock options entry window.

    Live mode only. Paper mode always returns False.
    """
    from engine.config import PAPER
    if PAPER:
        return False
    now = datetime.datetime.now(ET)
    if now.weekday() >= 5:
        return False
    h = now.hour + now.minute / 60.0
    return (9.5 + 5 / 60.0) <= h < (9.5 + 15 / 60.0)


# ── VIX ───────────────────────────────────────────────────────────────────────

def get_vix() -> float:
    """Return the latest VIX daily close. Defaults to 15.0 on failure."""
    try:
        from engine.utils.bars import get_bars
        data = get_bars("^VIX", "5d", "1d")
        return float(data["close"].iloc[-1]) if not data.empty else 15.0
    except Exception:
        return 15.0


def check_vix_roc_filter() -> Tuple[bool, float]:
    """Return (allow_entry, vix_roc_pct).

    Blocks new entries when VIX has risen sharply over the configured period,
    indicating an accelerating fear spike that precedes rapid drawdowns.
    Returns (True, 0.0) when the filter is disabled or data is unavailable.
    """
    from engine.config import USE_VIX_ROC_FILTER, VIX_ROC_THRESHOLD, VIX_ROC_PERIOD
    if not USE_VIX_ROC_FILTER:
        return True, 0.0
    try:
        from engine.utils.bars import get_bars
        vix_bars = get_bars("^VIX", "5d", "1h")
        if vix_bars.empty or len(vix_bars) < VIX_ROC_PERIOD:
            return True, 0.0
        current_vix = float(vix_bars["close"].iloc[-1])
        past_vix    = float(vix_bars["close"].iloc[-VIX_ROC_PERIOD])
        if past_vix <= 0:
            return True, 0.0
        roc = ((current_vix - past_vix) / past_vix) * 100
        return roc < VIX_ROC_THRESHOLD, roc
    except Exception:
        return True, 0.0


# ── Adaptive interval helpers ─────────────────────────────────────────────────

def get_vix_interval(vix: float, config: dict) -> Tuple[int, str]:
    """Map VIX level to scan interval (minutes) and volatility label."""
    thresholds = [
        (30, config.get("SCAN_INTERVAL_EXTREME_VOL", 1),  "EXTREME"),
        (26, config.get("SCAN_INTERVAL_HIGH_VOL",     2), "HIGH"),
        (22, config.get("SCAN_INTERVAL_MODERATE_VOL", 3), "MODERATE"),
        (18, config.get("SCAN_INTERVAL_NORMAL_VOL",   5), "NORMAL"),
        (15, config.get("SCAN_INTERVAL_CALM_VOL",     7), "CALM"),
    ]
    for threshold, interval, label in thresholds:
        if vix >= threshold:
            return interval, label
    return config.get("SCAN_INTERVAL_LOW_VOL", 10), "LOW"


def get_market_hours_interval(hour: float, config: dict) -> Tuple[Optional[int], str]:
    """Map current hour (decimal, ET) to scan interval and phase label."""
    if 7 <= hour < 9.5:
        return config.get("PREMARKET_SCAN_INTERVAL",     5), "PRE-MARKET"
    if 9.5 <= hour < 16:
        return config.get("REGULAR_HOURS_SCAN_INTERVAL", 3), "REGULAR HOURS"
    if 16 <= hour < 20:
        return config.get("AFTERHOURS_SCAN_INTERVAL",    7), "AFTER-HOURS"
    return None, "OFF-HOURS"


def get_position_tuning_interval(pos_count: int, config: dict) -> Tuple[Optional[int], str]:
    """Map open-position count to scan interval and position-status label."""
    if pos_count >= 8:
        return config.get("HIGH_POSITION_INTERVAL",   10), f"HIGH POS ({pos_count})"
    if 3 <= pos_count <= 7:
        return config.get("NORMAL_POSITION_INTERVAL",  5), f"NORMAL POS ({pos_count})"
    if pos_count < 3:
        return config.get("LOW_POSITION_INTERVAL",     3), f"LOW POS ({pos_count})"
    return None, "DISABLED"


# ── Market sentiment ──────────────────────────────────────────────────────────
# Cached for 15 min — SPY momentum + VIX threshold composite.

_sentiment_cache: dict = {"ts": 0.0, "value": "neutral"}
_SENTIMENT_TTL   = 900  # seconds


def get_market_sentiment() -> str:
    """Return 'bullish', 'bearish', or 'neutral' based on 5-day SPY momentum + VIX.

    Designed to be cheap — result is cached for 15 minutes.
    """
    now = time.monotonic()
    if now - _sentiment_cache["ts"] < _SENTIMENT_TTL:
        return _sentiment_cache["value"]

    try:
        from engine.utils.bars import get_bars
        spy = get_bars("SPY",  "5d", "1h")
        vix = get_bars("^VIX", "5d", "1h")
        if spy.empty:
            result = "neutral"
        else:
            spy_mom = ((spy["close"].iloc[-1] / spy["close"].iloc[0]) - 1) * 100
            vix_val = float(vix["close"].iloc[-1]) if not vix.empty else 20.0
            if spy_mom > 1.0 and vix_val < 20:
                result = "bullish"
            elif spy_mom < -1.0 or vix_val > 30:
                result = "bearish"
            else:
                result = "neutral"
    except Exception:
        result = "neutral"

    _sentiment_cache.update({"ts": now, "value": result})
    return result


# ── Live holdings ─────────────────────────────────────────────────────────────

def get_live_holdings(client) -> Tuple[set, set, set]:
    """Return (positions, buy_orders, combined) symbol sets from the broker.

    Only buy-side orders are included — stop/TP sell legs are already covered by
    the positions set and must not block re-investment in the same ticker.
    """
    log = logging.getLogger("ApexTrader")
    try:
        positions = {p.symbol for p in client.get_all_positions()}
        orders    = {
            o.symbol for o in client.get_orders()
            if str(getattr(o, "side", "")).lower() == "buy"
        }
        return positions, orders, positions | orders
    except Exception as e:
        log.warning(f"get_live_holdings failed: {e}")
        return set(), set(), set()

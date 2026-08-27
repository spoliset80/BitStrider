"""Technical scoring — grades an alert on market data, not on how it was written.

Produces a 0-100 conviction score from six independent checks. Each check returns
points and a human-readable reason so every decision can be audited in the log.
"""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ── Indicators ────────────────────────────────────────────────────────────────

def sma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes[-period - 1:-1], closes[-period:]):
        change = cur - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(bars: list, period: int = 14) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-period - 1:-1], bars[-period:]):
        trs.append(max(cur.high - cur.low,
                       abs(cur.high - prev.close),
                       abs(cur.low - prev.close)))
    return sum(trs) / period


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_signal(trade, broker, bullish: bool = True, bars: list | None = None) -> tuple[int, list[str]]:
    """
    Grade a trade signal 0-100 on price action. Returns (score, reasons).

    Checks: trend, momentum, volume conviction, entry chase, risk/reward,
    and proximity to recent range extremes. Missing data scores neutral rather
    than failing the trade outright.

    `bars` may be supplied to score against a point-in-time history (backtests);
    otherwise the latest daily bars are fetched.
    """
    reasons: list[str] = []
    if bars is None:
        bars = broker.get_daily_bars(trade.ticker)
    if len(bars) < 25:
        reasons.append(f"insufficient history ({len(bars)} bars) — neutral 50")
        return 50, reasons

    closes  = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    spot    = closes[-1]
    score   = 50

    # 1. Trend — price vs 20/50 SMA
    s20, s50 = sma(closes, 20), sma(closes, 50)
    if s20 and s50:
        if spot > s20 > s50:
            score += 15; reasons.append(f"strong uptrend (px {spot:.2f} > SMA20 {s20:.2f} > SMA50 {s50:.2f}) +15")
        elif spot > s20:
            score += 8;  reasons.append(f"above SMA20 {s20:.2f} +8")
        elif spot < s20 < s50:
            score -= 15; reasons.append(f"downtrend (px {spot:.2f} < SMA20 {s20:.2f} < SMA50 {s50:.2f}) -15")
        else:
            score -= 5;  reasons.append(f"below SMA20 {s20:.2f} -5")

    # 2. Momentum — RSI sweet spot is trending-but-not-exhausted
    r = rsi(closes)
    if r is not None:
        if 50 <= r <= 70:
            score += 12; reasons.append(f"RSI {r:.0f} in momentum zone +12")
        elif r > 78:
            score -= 12; reasons.append(f"RSI {r:.0f} overbought -12")
        elif r < 35:
            score -= 8;  reasons.append(f"RSI {r:.0f} weak -8")
        else:
            score += 3;  reasons.append(f"RSI {r:.0f} neutral +3")

    # 3. Volume conviction — today vs 20-day average
    v20 = sma([float(v) for v in volumes], 20)
    if v20 and v20 > 0:
        ratio = volumes[-1] / v20
        if ratio >= 1.5:
            score += 10; reasons.append(f"volume {ratio:.1f}x avg +10")
        elif ratio >= 1.0:
            score += 5;  reasons.append(f"volume {ratio:.1f}x avg +5")
        elif ratio < 0.6:
            score -= 8;  reasons.append(f"volume {ratio:.1f}x avg (thin) -8")

    # 4. Chase guard — how far price has run past the alerted entry
    if trade.entry_price and trade.entry_price > 0 and not trade.occ:
        drift = (spot - trade.entry_price) / trade.entry_price * 100
        if drift > 3:
            score -= 15; reasons.append(f"chasing: {drift:+.1f}% past entry -15")
        elif drift > 1.5:
            score -= 7;  reasons.append(f"extended {drift:+.1f}% past entry -7")
        elif drift < -3:
            score -= 5;  reasons.append(f"{drift:+.1f}% below entry, setup may be broken -5")
        else:
            score += 8;  reasons.append(f"near entry ({drift:+.1f}%) +8")

    # 5. Risk/reward from the alert's own stop and target
    if trade.stop and trade.targets and trade.entry_price:
        risk   = abs(trade.entry_price - trade.stop)
        reward = abs(max(trade.targets) - trade.entry_price)
        if risk > 0:
            rr = reward / risk
            if rr >= 2.5:
                score += 12; reasons.append(f"R:R {rr:.1f} +12")
            elif rr >= 1.5:
                score += 6;  reasons.append(f"R:R {rr:.1f} +6")
            else:
                score -= 10; reasons.append(f"R:R {rr:.1f} poor -10")

    # 6. Range position — near highs confirms strength, but not at the very top
    window = closes[-60:] if len(closes) >= 60 else closes
    lo, hi = min(window), max(window)
    if hi > lo:
        pos = (spot - lo) / (hi - lo) * 100
        if 60 <= pos <= 92:
            score += 8; reasons.append(f"{pos:.0f}% of 60d range (strong) +8")
        elif pos > 97:
            score -= 6; reasons.append(f"{pos:.0f}% of 60d range (extended) -6")
        elif pos < 25:
            score -= 6; reasons.append(f"{pos:.0f}% of 60d range (weak) -6")

    score = max(0, min(100, score))
    return score, reasons

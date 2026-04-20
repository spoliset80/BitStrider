"""
engine.utils.risk
-----------------
ATR-based tier assignment (with 15-min cache) and risk-adjusted position sizing.

Design notes:
  - get_dynamic_tier() is the single source of truth for TS/TP parameters.
    The 15-min TTL means the tier is stable for a full scan cycle — no per-signal
    Alpaca bar fetches on the hot execution path.
  - calculate_risk_adjusted_size() uses local effective_* variables instead of
    mutating imported config names, which was a silent source of confusion.

All public functions here are re-exported from engine.utils for backward compat.
"""

from __future__ import annotations

import logging
import time
from typing import Dict

_log = logging.getLogger("ApexTrader")

# ── ATR tier cache ────────────────────────────────────────────────────────────
# Keyed by symbol. Each entry: {"result": dict, "ts": float (monotonic)}.
# TTL of 900s (15 min) — ATR tiers are stable across a trading session and
# only need refreshing after a major intraday vol regime shift.
_tier_cache: Dict[str, dict] = {}
_TIER_CACHE_TTL = 900


def get_dynamic_tier(symbol: str, price: float = None) -> dict:
    """Return ATR-based TP/TS tier info for *symbol*.

    Result is cached per symbol for _TIER_CACHE_TTL seconds to avoid repeated
    Alpaca bar fetches during scan cycles (previously 8–15 calls per cycle).

    Returns a dict with keys: tier, tp, ts[, atr_pct]
    """
    from engine.config import (
        USE_DYNAMIC_TIERS,
        ATR_TIER_EXTREME, ATR_TIER_HIGH, ATR_TIER_MEDIUM,
        TAKE_PROFIT_EXTREME,  TAKE_PROFIT_HIGH,  TAKE_PROFIT_MEDIUM,  TAKE_PROFIT_NORMAL,
        TRAILING_STOP_EXTREME, TRAILING_STOP_HIGH, TRAILING_STOP_MEDIUM, TRAILING_STOP_NORMAL,
        EXTREME_MOMENTUM_STOCKS, HIGH_MOMENTUM_STOCKS,
    )

    # ── Static tier lists (when dynamic tiers disabled) ───────────────────────
    if not USE_DYNAMIC_TIERS:
        if symbol in EXTREME_MOMENTUM_STOCKS:
            return {"tier": "EXTREME", "tp": TAKE_PROFIT_EXTREME, "ts": TRAILING_STOP_EXTREME}
        if symbol in HIGH_MOMENTUM_STOCKS:
            return {"tier": "HIGH", "tp": TAKE_PROFIT_HIGH, "ts": TRAILING_STOP_HIGH}
        return {"tier": "MEDIUM", "tp": TAKE_PROFIT_MEDIUM, "ts": TRAILING_STOP_MEDIUM}

    # ── Cache hit ─────────────────────────────────────────────────────────────
    now = time.monotonic()
    cached = _tier_cache.get(symbol)
    if cached and (now - cached["ts"]) < _TIER_CACHE_TTL:
        return cached["result"]

    # ── ATR calculation ───────────────────────────────────────────────────────
    _NORMAL = {"tier": "NORMAL", "tp": TAKE_PROFIT_NORMAL, "ts": TRAILING_STOP_NORMAL}

    try:
        from engine.utils.bars import get_bars, calculate_atr
        bars = get_bars(symbol, "10d", "1d")
        if bars.empty:
            _tier_cache[symbol] = {"result": _NORMAL, "ts": now}
            return _NORMAL

        atr           = calculate_atr(bars, period=14)
        current_price = price if price else float(bars["close"].iloc[-1])

        if current_price <= 0 or atr <= 0:
            _tier_cache[symbol] = {"result": _NORMAL, "ts": now}
            return _NORMAL

        atr_pct = (atr / current_price) * 100

        if atr_pct >= ATR_TIER_EXTREME:
            result = {"tier": "EXTREME", "tp": TAKE_PROFIT_EXTREME, "ts": TRAILING_STOP_EXTREME, "atr_pct": atr_pct}
        elif atr_pct >= ATR_TIER_HIGH:
            result = {"tier": "HIGH",    "tp": TAKE_PROFIT_HIGH,    "ts": TRAILING_STOP_HIGH,    "atr_pct": atr_pct}
        elif atr_pct >= ATR_TIER_MEDIUM:
            result = {"tier": "MEDIUM",  "tp": TAKE_PROFIT_MEDIUM,  "ts": TRAILING_STOP_MEDIUM,  "atr_pct": atr_pct}
        else:
            result = _NORMAL | {"atr_pct": atr_pct}

        _tier_cache[symbol] = {"result": result, "ts": now}
        return result

    except Exception as e:
        _log.debug(f"get_dynamic_tier({symbol}): ATR calc failed: {e}")
        _tier_cache[symbol] = {"result": _NORMAL, "ts": now}
        return _NORMAL


def calculate_risk_adjusted_size(account_balance: float, symbol: str, price: float) -> dict:
    """Return position-sizing metadata for an entry.

    Uses local effective_* variables rather than reassigning imported config
    constants — the original code's implicit local rebind was misleading.

    Returns a dict with keys: tier, allocation_pct, dollar_amount,
                               stop_loss_pct, tp, atr_pct
    """
    from engine.config import (
        USE_RISK_EQUALIZED_SIZING,
        RISK_PER_TRADE_PCT,      POSITION_SIZE_PCT,
        SMALL_ACCOUNT_EQUITY_THRESHOLD,
        SMALL_ACCOUNT_POSITION_SIZE_PCT,
        SMALL_ACCOUNT_RISK_PER_TRADE_PCT,
    )

    tier_info     = get_dynamic_tier(symbol, price)
    stop_loss_pct = tier_info["ts"]

    small = account_balance < SMALL_ACCOUNT_EQUITY_THRESHOLD
    effective_pos_pct  = SMALL_ACCOUNT_POSITION_SIZE_PCT  if small else POSITION_SIZE_PCT
    effective_risk_pct = SMALL_ACCOUNT_RISK_PER_TRADE_PCT if small else RISK_PER_TRADE_PCT

    if not USE_RISK_EQUALIZED_SIZING:
        dollar_amount = account_balance * (effective_pos_pct / 100)
        return {
            "tier":           tier_info["tier"],
            "allocation_pct": effective_pos_pct,
            "dollar_amount":  round(dollar_amount, 2),
            "stop_loss_pct":  stop_loss_pct,
            "tp":             tier_info["tp"],
            "atr_pct":        tier_info.get("atr_pct", 0),
        }

    # Risk-equalised sizing: scale position so that a 1× ATR move costs
    # exactly effective_risk_pct of account equity, capped at effective_pos_pct.
    calc_pos_size_pct  = (effective_risk_pct / stop_loss_pct) * 100
    final_pos_size_pct = min(calc_pos_size_pct, effective_pos_pct)
    dollar_amount      = account_balance * (final_pos_size_pct / 100)

    return {
        "tier":           tier_info["tier"],
        "allocation_pct": round(final_pos_size_pct, 2),
        "dollar_amount":  round(dollar_amount, 2),
        "stop_loss_pct":  stop_loss_pct,
        "tp":             tier_info["tp"],
        "atr_pct":        tier_info.get("atr_pct", 0),
    }

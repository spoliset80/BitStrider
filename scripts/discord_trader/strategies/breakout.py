"""Breakout strategy — execute swing CALL options on breakout alerts.

Breakout alerts are bullish (52-week highs, resistance breaks), so we buy an
ATM-ish call ~`breakout_dte` days out. Underlying stop/targets are logged for
manual management (option-leg brackets are not placed automatically).
"""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def handle_breakout(signal, broker, risk, config) -> Optional[dict]:
    """
    signal : BreakoutSignal
    broker : Broker
    risk   : RiskManager
    config : Config
    """
    ticker = signal.ticker

    # Dedupe / position / daily-cap checks (reuse equity-style risk gate)
    notional = config.breakout_notional
    try:
        open_count = broker.open_position_count()
    except RuntimeError as e:
        logger.error(f"  [CRITICAL] {e} — skipping for safety")
        return None

    allowed, reason = risk.check_buy(ticker, notional, open_count, conf=100)
    if not allowed:
        logger.warning(f"  [SKIP] {ticker} breakout: {reason}")
        return {"status": "skip", "reason": reason}

    # Find the swing call contract
    contract = broker.find_swing_call(
        ticker, near_price=signal.entry,
        target_dte=config.breakout_dte, min_dte=config.breakout_min_dte,
    )
    if not contract:
        return {"status": "skip", "reason": "no swing call contract found"}

    occ = contract["symbol"]

    # Price off the last trade (fallback to live mid, then last close) + configured buffer
    last_price = broker.get_option_last_trade_price(occ)
    if last_price:
        est_prem = last_price
        logger.info(f"  [BREAKOUT] {occ} last trade={last_price}")
    else:
        quote = broker.get_option_quote(occ)
        if quote:
            est_prem = quote["mid"]
            logger.info(f"  [BREAKOUT] {occ}: last trade unavailable, using mid "
                        f"bid={quote['bid']} ask={quote['ask']}")
        else:
            try:
                est_prem = float(contract["close_price"]) if contract.get("close_price") else None
            except (TypeError, ValueError):
                est_prem = None

    # Size by notional using premium estimate (fallback qty=1)
    if est_prem and est_prem > 0:
        qty = max(1, int(notional // (est_prem * 100)))
        limit_px = round(est_prem * (1 + config.price_above_last_pct / 100), 2)
    else:
        qty = 1
        limit_px = None

    logger.info(f"  [BREAKOUT] {ticker} swing CALL {occ} qty={qty} "
                f"entry≈${signal.entry} stop=${signal.stop} "
                f"T1=${signal.t1} T2=${signal.t2} T3=${signal.t3}")

    result = broker.buy_option(occ, qty, limit_price=limit_px)
    if result.get("status") == "submitted":
        risk.record_buy(ticker, notional)
        # Immediately place a GTC trailing stop to protect the position
        stop_result = broker.place_option_trailing_stop(
            occ, qty, trail_pct=config.breakout_trail_pct
        )
        if stop_result.get("status") == "submitted":
            logger.info(f"  [BREAKOUT] Trailing stop {config.breakout_trail_pct}% GTC "
                        f"placed for {occ}")
        else:
            logger.warning(f"  [BREAKOUT] *** NO STOP on {occ}: {stop_result.get('error','?')} "
                           "— close manually ***")
    return result

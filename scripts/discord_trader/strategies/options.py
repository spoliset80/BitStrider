"""Options strategy — handle options BUY/SELL/close trades."""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def handle_options(trade, broker, risk, buying_power: Optional[float]) -> Optional[dict]:
    """
    Process an options or equity Trade object from the options channel.
    Returns order result dict or None if skipped.
    """
    ticker = trade.ticker
    is_buy = trade.action == "BUY"

    if is_buy:
        notional = risk.notional_for(trade.confidence, buying_power)
        try:
            open_count = broker.open_position_count()
        except RuntimeError as e:
            logger.error(f"  [CRITICAL] {e} — skipping for safety")
            return None

        allowed, reason = risk.check_buy(ticker, notional, open_count, trade.confidence)
        if not allowed:
            logger.warning(f"  [SKIP] {ticker}: {reason}")
            return None

        if trade.occ:
            if not trade.entry_price or trade.entry_price <= 0:
                logger.warning(f"  [SKIP] {trade.occ}: no entry price")
                return None
            qty = max(1, int(notional // (trade.entry_price * 100)))
            # During market hours use market order; outside hours use limit at entry price
            result = broker.buy_option(trade.occ, qty, limit_price=trade.entry_price)
        else:
            result = broker.buy_equity(ticker, notional)

        if result.get("status") == "submitted":
            risk.record_buy(ticker, notional)
        return result

    else:
        # SELL — find position
        if not trade.occ:
            # Try to resolve underlying → open OCC
            occ, pos = broker.find_option_position_for_ticker(ticker)
            if occ and pos:
                logger.info(f"  [CLOSE] Resolved {ticker} → OCC {occ}")
                qty = max(1, int(float(pos.qty)))
                return broker.sell_option(occ, qty)
        if trade.occ:
            pos = broker.get_position(trade.occ)
            if not pos:
                logger.info(f"  [SKIP SELL] No position in {trade.occ}")
                return None
            qty = max(1, int(float(pos.qty)))
            return broker.sell_option(trade.occ, qty)

        # plain equity close
        return broker.sell_equity(ticker)

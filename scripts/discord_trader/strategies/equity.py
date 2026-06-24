"""Equity strategy — handle equity BUY/SELL trades."""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def handle_equity(trade, broker, risk, buying_power: Optional[float]) -> Optional[dict]:
    """
    Process an equity Trade object.
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

        result = broker.buy_equity(ticker, notional)
        if result.get("status") == "submitted":
            risk.record_buy(ticker, notional)
        return result
    else:
        # SELL — resolve OCC first if it's an option
        if trade.occ:
            pos = broker.get_position(trade.occ)
            if not pos:
                logger.info(f"  [SKIP SELL] No position in {trade.occ}")
                return None
            qty = max(1, int(float(pos.qty)))
            return broker.sell_option(trade.occ, qty)
        else:
            # Check if underlying has an open option position to close
            occ, pos = broker.find_option_position_for_ticker(ticker)
            if occ and pos:
                logger.info(f"  [CLOSE] Resolved {ticker} equity close → OCC {occ}")
                qty = max(1, int(float(pos.qty)))
                return broker.sell_option(occ, qty)
            return broker.sell_equity(ticker)

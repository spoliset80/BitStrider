"""Options strategy — handle options BUY/SELL/close trades."""
from __future__ import annotations
import logging
from typing import Optional

from .equity import buy_as_option

logger = logging.getLogger(__name__)


def handle_options(trade, broker, risk, buying_power: Optional[float],
                    price_above_last_pct: float = 2.0, config=None) -> Optional[dict]:
    """
    Process an options or equity Trade object from the options channel.
    Returns order result dict or None if skipped.
    """
    ticker = trade.ticker
    is_buy = trade.action == "BUY"

    if is_buy:
        if buying_power is None or buying_power <= 0:
            reason = "buying power unavailable"
            logger.warning(f"  [SKIP] {ticker}: {reason}")
            return {"status": "skip", "reason": reason}

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
            limit_price = None
            last_price = broker.get_option_last_trade_price(trade.occ)
            if last_price:
                limit_price = round(last_price * (1 + price_above_last_pct / 100), 2)
                logger.info(f"  [PRICE] {trade.occ} last trade={last_price} → "
                            f"limit={limit_price} (chat alerted {trade.entry_price})")
            else:
                quote = broker.get_option_quote(trade.occ)
                if quote:
                    mid = quote["mid"]
                    limit_price = round(mid * (1 + price_above_last_pct / 100), 2)
                    logger.info(f"  [PRICE] {trade.occ}: last trade unavailable, using mid "
                                f"bid={quote['bid']} ask={quote['ask']} mid={mid} → "
                                f"limit={limit_price} (chat alerted {trade.entry_price})")
                elif trade.entry_price and trade.entry_price > 0:
                    limit_price = trade.entry_price
                    logger.warning(f"  [PRICE] {trade.occ}: quote/last trade unavailable, "
                                   f"falling back to chat price {limit_price}")
            if not limit_price or limit_price <= 0:
                logger.warning(f"  [SKIP] {trade.occ}: no usable price")
                return None
            qty = max(1, int(notional // (limit_price * 100)))
            # During market hours use market order; outside hours use limit at entry price
            result = broker.buy_option(trade.occ, qty, limit_price=limit_price)
        else:
            result = None
            if config and getattr(config, "equity_as_options", False):
                result = buy_as_option(trade, broker, config, notional)
                if not result:
                    logger.warning(f"  [{ticker}] option conversion failed — falling back to shares")
            if not result:
                result = broker.buy_equity(ticker, notional)

        if result.get("status") == "submitted":
            risk.record_buy(ticker, notional)
        return result

    else:
        # SELL — find position
        if trade.occ:
            pos = broker.get_position(trade.occ)
            if not pos:
                logger.info(f"  [SKIP SELL] No position in {trade.occ}")
                return None
            qty = max(1, int(float(pos.qty)))
            return broker.sell_option(trade.occ, qty)

        # No explicit contract — "Out $TICKER" means exit every option leg we hold
        positions = broker.find_option_positions_for_ticker(ticker)
        if positions:
            results = []
            for p in positions:
                qty = max(1, int(float(p.qty)))
                logger.info(f"  [CLOSE] Resolved {ticker} → OCC {p.symbol} qty={qty}")
                results.append(broker.sell_option(p.symbol, qty))
            submitted = [r for r in results if r.get("status") == "submitted"]
            return {"status": "submitted" if submitted else "error",
                    "type": "option", "ticker": ticker,
                    "legs": results}

        # plain equity close
        return broker.sell_equity(ticker)

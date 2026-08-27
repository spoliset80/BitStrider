"""Equity strategy — handle equity BUY/SELL trades."""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def buy_as_option(trade, broker, config, notional: float) -> Optional[dict]:
    """Buy a call in place of shares. Returns None if no affordable contract could be resolved."""
    ticker = trade.ticker
    spot = trade.entry_price or broker.get_latest_price(ticker)
    if not spot or spot <= 0:
        logger.warning(f"  [OPT] {ticker}: no spot price to pick a strike")
        return None

    # Walk further OTM until one contract fits the budget.
    attempts = [(config.equity_opt_moneyness, config.equity_opt_moneyness_pct)]
    step = max(config.equity_opt_moneyness_pct, 5.0)
    attempts += [("OTM", step * n) for n in range(1, 5)]

    target_date = None
    if getattr(config, "equity_opt_expiry_mode", "week") == "week":
        from scripts.discord_parser import this_week_friday
        target_date = this_week_friday()

    for moneyness, pct in attempts:
        contract = broker.find_option_contract(
            ticker, near_price=spot, opt_type="call",
            target_dte=config.equity_opt_dte, min_dte=config.equity_opt_min_dte,
            moneyness=moneyness, moneyness_pct=pct, target_date=target_date,
        )
        if not contract:
            continue

        occ = contract["symbol"]
        prem = broker.get_option_last_trade_price(occ)
        if not prem:
            quote = broker.get_option_quote(occ)
            prem = quote["mid"] if quote else None
        if not prem or prem <= 0:
            continue

        limit_px = round(prem * (1 + config.price_above_last_pct / 100), 2)
        contract_cost = limit_px * 100
        if contract_cost > notional:
            logger.info(f"  [OPT] {occ} ({moneyness} {pct}%) costs ${contract_cost:,.0f} "
                        f"> ${notional:,.0f} budget — trying further OTM")
            continue

        qty = int(notional // contract_cost)
        logger.info(f"  [OPT] {ticker} equity alert → {moneyness} call {occ} "
                    f"prem={prem} limit={limit_px} qty={qty} "
                    f"cost=${contract_cost * qty:,.0f} of ${notional:,.0f} budget")
        return broker.buy_option(occ, qty, limit_price=limit_px)

    logger.warning(f"  [OPT] {ticker}: no call contract fits ${notional:,.0f} budget — skipping")
    return {"status": "skip", "reason": f"no affordable option under ${notional:,.0f}"}


def handle_equity(trade, broker, risk, buying_power: Optional[float],
                  config=None) -> Optional[dict]:
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

        if config and getattr(config, "equity_as_options", False):
            result = buy_as_option(trade, broker, config, notional)
            if result:
                if result.get("status") == "submitted":
                    risk.record_buy(ticker, notional)
                return result
            logger.warning(f"  [{ticker}] option conversion failed — falling back to shares")

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

        # No explicit contract — exit every option leg we hold on this underlying
        positions = broker.find_option_positions_for_ticker(ticker)
        if positions:
            results = []
            for p in positions:
                qty = max(1, int(float(p.qty)))
                logger.info(f"  [CLOSE] Resolved {ticker} equity close → OCC {p.symbol} qty={qty}")
                results.append(broker.sell_option(p.symbol, qty))
            submitted = [r for r in results if r.get("status") == "submitted"]
            return {"status": "submitted" if submitted else "error",
                    "type": "option", "ticker": ticker, "legs": results}
        return broker.sell_equity(ticker)

"""Alpaca broker wrapper — all order placement and account queries here."""
from __future__ import annotations
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_BP_CACHE: dict = {"bp": None, "ts": 0.0}


class Broker:
    """Thin wrapper around alpaca-py TradingClient."""

    def __init__(self, key: str, secret: str, paper: bool = True):
        from alpaca.trading.client import TradingClient
        self._paper  = paper
        self._client = TradingClient(key, secret, paper=paper)
        self.trading_enabled = True   # gated by poller based on market hours
        mode = "PAPER" if paper else "LIVE"
        logger.info(f"Alpaca connected [{mode}]")

    def set_trading_enabled(self, enabled: bool):
        """Enable/disable actual order submission. Parsing/state still runs when disabled."""
        self.trading_enabled = enabled

    def _gate(self, label: str) -> Optional[dict]:
        """Return a skip result if trading is disabled, else None."""
        if not self.trading_enabled:
            logger.info(f"  [GATED] market closed — not placing {label}")
            return {"status": "skip", "reason": "market closed"}
        return None

    # ── Account ───────────────────────────────────────────────────────────────

    def buying_power(self, ttl: float = 60.0) -> Optional[float]:
        """Options buying power, cached for ttl seconds."""
        now = time.time()
        if _BP_CACHE["bp"] is not None and now - _BP_CACHE["ts"] < ttl:
            return _BP_CACHE["bp"]
        try:
            acct = self._client.get_account()
            bp = float(getattr(acct, "options_buying_power", None) or acct.buying_power or 0)
            _BP_CACHE["bp"] = bp
            _BP_CACHE["ts"] = now
            logger.info(f"  [BP] options_buying_power=${bp:,.2f}")
            return bp
        except Exception as e:
            logger.warning(f"  [BP] fetch failed: {e}")
            return None

    def open_position_count(self) -> int:
        try:
            return len(self._client.get_all_positions())
        except Exception as e:
            raise RuntimeError(f"Position count failed: {e}") from e

    def get_all_positions(self) -> list:
        return self._client.get_all_positions()

    def get_position(self, symbol: str):
        """Returns position or None."""
        try:
            return self._client.get_open_position(symbol)
        except Exception:
            return None

    def find_option_position_for_ticker(self, ticker: str):
        """Scan open positions for any OCC whose root matches ticker."""
        try:
            for p in self._client.get_all_positions():
                sym = p.symbol
                if sym.upper().startswith(ticker.upper()) and len(sym) > 6:
                    return sym, p
        except Exception:
            pass
        return None, None

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """Get latest trade price via Alpaca market data."""
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestTradeRequest
            # re-use broker keys stored on client
            c = self._client
            data_client = StockHistoricalDataClient(
                c._api_key, c._secret_key  # type: ignore[attr-defined]
            )
            req = StockLatestTradeRequest(symbol_or_symbols=symbol)
            resp = data_client.get_stock_latest_trade(req)
            return float(resp[symbol].price)
        except Exception as e:
            logger.warning(f"  [PRICE] {symbol} fetch failed: {e}")
            return None

    # ── Order placement ───────────────────────────────────────────────────────

    def buy_equity(self, ticker: str, notional: float) -> dict:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums    import OrderSide, TimeInForce
        gated = self._gate(f"EQUITY BUY {ticker}")
        if gated:
            return gated
        try:
            o = self._client.submit_order(MarketOrderRequest(
                symbol=ticker, notional=notional,
                side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            ))
            logger.info(f"  SUBMITTED EQUITY BUY ${notional} {ticker} id={o.id}")
            return {"status": "submitted", "type": "equity", "id": str(o.id), "ticker": ticker}
        except Exception as e:
            err = str(e)
            if "not fractionable" in err:
                return self._buy_equity_qty1(ticker)
            logger.error(f"  ORDER FAILED {ticker}: {e}")
            return {"status": "error", "ticker": ticker, "error": err}

    def _buy_equity_qty1(self, ticker: str) -> dict:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums    import OrderSide, TimeInForce
        try:
            o = self._client.submit_order(MarketOrderRequest(
                symbol=ticker, qty=1,
                side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            ))
            logger.info(f"  SUBMITTED EQUITY BUY 1 share {ticker} (non-frac) id={o.id}")
            return {"status": "submitted", "type": "equity", "id": str(o.id), "ticker": ticker}
        except Exception as e:
            logger.error(f"  ORDER FAILED {ticker} qty=1: {e}")
            return {"status": "error", "ticker": ticker, "error": str(e)}

    def sell_equity(self, ticker: str) -> dict:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums    import OrderSide, TimeInForce
        gated = self._gate(f"EQUITY SELL {ticker}")
        if gated:
            return gated
        pos = self.get_position(ticker)
        if not pos:
            return {"status": "skip", "reason": f"no position in {ticker}"}
        try:
            qty = float(pos.qty)
            o = self._client.submit_order(MarketOrderRequest(
                symbol=ticker, qty=qty,
                side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
            ))
            logger.info(f"  SUBMITTED EQUITY SELL {qty} {ticker} id={o.id}")
            return {"status": "submitted", "type": "equity", "id": str(o.id), "ticker": ticker}
        except Exception as e:
            logger.error(f"  ORDER FAILED SELL {ticker}: {e}")
            return {"status": "error", "ticker": ticker, "error": str(e)}

    def buy_option(self, occ: str, qty: int, limit_price: Optional[float] = None) -> dict:
        from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
        from alpaca.trading.enums    import OrderSide, TimeInForce
        gated = self._gate(f"OPTION BUY {occ}")
        if gated:
            return gated
        try:
            if limit_price:
                req = LimitOrderRequest(symbol=occ, qty=qty, limit_price=limit_price,
                                        side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
            else:
                req = MarketOrderRequest(symbol=occ, qty=qty,
                                         side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
            o = self._client.submit_order(req)
            label = f"LIMIT @{limit_price}" if limit_price else "MARKET"
            logger.info(f"  SUBMITTED OPTION BUY {label} {qty}x {occ} id={o.id}")
            return {"status": "submitted", "type": "option", "id": str(o.id), "occ": occ, "qty": qty}
        except Exception as e:
            logger.error(f"  ORDER FAILED {occ}: {e}")
            return {"status": "error", "occ": occ, "error": str(e)}

    def sell_option(self, occ: str, qty: int) -> dict:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums    import OrderSide, TimeInForce
        gated = self._gate(f"OPTION SELL {occ}")
        if gated:
            return gated
        try:
            o = self._client.submit_order(MarketOrderRequest(
                symbol=occ, qty=qty,
                side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
            ))
            logger.info(f"  SUBMITTED OPTION SELL {qty}x {occ} id={o.id}")
            return {"status": "submitted", "type": "option", "id": str(o.id), "occ": occ, "qty": qty}
        except Exception as e:
            logger.error(f"  ORDER FAILED SELL {occ}: {e}")
            return {"status": "error", "occ": occ, "error": str(e)}

    def place_option_trailing_stop(self, occ: str, qty: int,
                                   trail_pct: float = 40.0) -> dict:
        """
        Place a GTC trailing-stop SELL on an option position.

        `trail_pct` is the % drawdown from the option's intra-day high before
        the stop fires (e.g. 40 → sell if premium drops 40 % from its high).

        Returns a result dict identical to buy_option / sell_option.
        """
        from alpaca.trading.requests import TrailingStopOrderRequest
        from alpaca.trading.enums    import OrderSide, TimeInForce
        gated = self._gate(f"TRAILING STOP {occ}")
        if gated:
            return gated
        try:
            req = TrailingStopOrderRequest(
                symbol          = occ,
                qty             = qty,
                side            = OrderSide.SELL,
                time_in_force   = TimeInForce.GTC,
                trail_percent   = trail_pct,
            )
            o = self._client.submit_order(req)
            logger.info(f"  TRAILING STOP placed {trail_pct}% GTC {qty}x {occ} id={o.id}")
            return {"status": "submitted", "type": "trailing_stop", "id": str(o.id),
                    "occ": occ, "qty": qty, "trail_pct": trail_pct}
        except Exception as e:
            # Trailing stops may not be supported for options in some account types
            logger.warning(f"  [STOP] Trailing stop failed for {occ} ({e}) — "
                           "position is UNPROTECTED, close manually or check account permissions")
            return {"status": "error", "occ": occ, "error": str(e)}

    def find_spy_0dte_contract(self, direction: str) -> Optional[str]:
        """Find ATM SPY 0DTE option symbol for today."""
        import requests as req_lib
        import os
        from datetime import date
        today = date.today().strftime("%Y-%m-%d")
        opt_type = "call" if direction == "LONG" else "put"

        # Get SPY price first
        spy_price = self.get_latest_price("SPY")
        if not spy_price:
            return None
        atm_strike = round(spy_price)

        # Query Alpaca options contracts
        c = self._client
        headers = {
            "APCA-API-KEY-ID":     c._api_key,     # type: ignore
            "APCA-API-SECRET-KEY": c._secret_key,  # type: ignore
        }
        base = "https://paper-api.alpaca.markets" if self._paper else "https://api.alpaca.markets"
        try:
            r = req_lib.get(
                f"{base}/v2/options/contracts",
                headers=headers,
                params={
                    "underlying_symbols": "SPY",
                    "expiration_date":    today,
                    "type":               opt_type,
                    "limit":             100,
                },
                timeout=10,
            )
            if not r.ok:
                logger.warning(f"  [SPX] Contract search failed {r.status_code}")
                return None
            contracts = r.json().get("option_contracts", [])
            if not contracts:
                logger.warning(f"  [SPX] No SPY 0DTE {opt_type} contracts found for {today}")
                return None
            # Pick closest to ATM
            best = min(contracts, key=lambda c: abs(float(c["strike_price"]) - atm_strike))
            logger.info(f"  [SPX] Selected SPY contract: {best['symbol']} strike={best['strike_price']}")
            return best["symbol"]
        except Exception as e:
            logger.error(f"  [SPX] Contract lookup error: {e}")
            return None

    def find_swing_call(self, ticker: str, near_price: float,
                        target_dte: int = 45, min_dte: int = 30) -> Optional[dict]:
        """
        Find an ATM-ish call for a swing trade.

        Picks the expiration closest to `target_dte` (but >= `min_dte` out),
        then the strike closest to `near_price`. Returns the contract dict
        (with 'symbol', 'strike_price', 'expiration_date', 'close_price') or None.
        """
        import requests as req_lib
        from datetime import date, timedelta

        today    = date.today()
        gte_date = (today + timedelta(days=min_dte)).strftime("%Y-%m-%d")
        lte_date = (today + timedelta(days=max(target_dte * 2, min_dte + 30))).strftime("%Y-%m-%d")

        c = self._client
        headers = {
            "APCA-API-KEY-ID":     c._api_key,     # type: ignore
            "APCA-API-SECRET-KEY": c._secret_key,  # type: ignore
        }
        base = "https://paper-api.alpaca.markets" if self._paper else "https://api.alpaca.markets"
        try:
            r = req_lib.get(
                f"{base}/v2/options/contracts",
                headers=headers,
                params={
                    "underlying_symbols":   ticker.upper(),
                    "type":                 "call",
                    "expiration_date_gte":  gte_date,
                    "expiration_date_lte":  lte_date,
                    "limit":                1000,
                },
                timeout=10,
            )
            if not r.ok:
                logger.warning(f"  [BREAKOUT] Contract search failed {r.status_code} for {ticker}")
                return None
            contracts = [c for c in r.json().get("option_contracts", []) if c.get("tradable")]
            if not contracts:
                logger.warning(f"  [BREAKOUT] No tradable {ticker} calls {gte_date}..{lte_date}")
                return None

            # Choose expiration closest to target_dte
            target_exp = today + timedelta(days=target_dte)
            def exp_key(c):
                ed = date.fromisoformat(c["expiration_date"])
                return abs((ed - target_exp).days)
            best_exp = min(contracts, key=exp_key)["expiration_date"]
            same_exp = [c for c in contracts if c["expiration_date"] == best_exp]

            # Among that expiry, strike closest to near_price (ATM)
            best = min(same_exp, key=lambda c: abs(float(c["strike_price"]) - near_price))
            logger.info(f"  [BREAKOUT] Selected {ticker} call {best['symbol']} "
                        f"strike={best['strike_price']} exp={best['expiration_date']}")
            return best
        except Exception as e:
            logger.error(f"  [BREAKOUT] Contract lookup error for {ticker}: {e}")
            return None

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
        self._client = TradingClient(key, secret, paper=paper)
        mode = "PAPER" if paper else "LIVE"
        logger.info(f"Alpaca connected [{mode}]")

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
        base = "https://paper-api.alpaca.markets" if c._base_url and "paper" in str(c._base_url) else "https://api.alpaca.markets"  # type: ignore
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

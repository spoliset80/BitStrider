"""ApexTrader - Broker Factory
Selects the appropriate broker client.
Only Alpaca is used (stocks only -- options trading removed 2026-09-01);
E*TRADE support was removed 2026-09-01 (see git history for the old OAuth
adapter).
"""

import logging

from engine.config import PAPER, API_KEY, API_SECRET

log = logging.getLogger("ApexTrader")


class BrokerFactory:
    """Factory for creating broker clients."""

    @staticmethod
    def create_stock_client(broker: str = "alpaca"):
        """
        Create a stock trading client.

        Args:
            broker: only 'alpaca' is supported.
        """
        broker = broker.lower()

        if broker != "alpaca":
            raise ValueError(f"Unknown broker: {broker}")

        from alpaca.trading.client import TradingClient

        api_key    = API_KEY
        api_secret = API_SECRET
        paper      = PAPER

        if not api_key or not api_secret:
            raise ValueError("Alpaca credentials not found in environment")

        # SDK picks the correct endpoint automatically via paper=True/False.
        log.debug(f"Using Alpaca for stock trading (paper={paper})")
        return TradingClient(api_key, api_secret, paper=paper)

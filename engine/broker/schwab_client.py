"""Schwab OAuth token manager and market data API client."""

import os
import json
import time
import base64
import requests
import logging
from typing import Dict, Optional
from datetime import datetime, timedelta

log = logging.getLogger("ApexTrader")

# OAuth endpoints
SCHWAB_OAUTH_URL = "https://api.schwabapi.com/v1/oauth/token"
SCHWAB_MARKET_DATA_URL = "https://api.schwabapi.com/marketdata/v1"

# Token cache file
TOKEN_CACHE_FILE = ".schwab_token_cache.json"


class SchwabOAuthClient:
    """Manages Schwab OAuth token refresh and API authentication."""
    
    def __init__(self, client_id: Optional[str] = None, client_secret: Optional[str] = None):
        self.client_id = client_id or os.environ.get("SCHWAB_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("SCHWAB_CLIENT_SECRET")
        
        if not self.client_id or not self.client_secret:
            raise ValueError("Schwab SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET required in .env")
        
        self.access_token: Optional[str] = None
        self.token_expiry: Optional[float] = None
        self._load_cached_token()
    
    def _load_cached_token(self) -> None:
        """Load token from cache file if it exists and is not expired."""
        if os.path.exists(TOKEN_CACHE_FILE):
            try:
                with open(TOKEN_CACHE_FILE, "r") as f:
                    cache = json.load(f)
                    self.access_token = cache.get("access_token")
                    self.token_expiry = cache.get("token_expiry")
                    
                    # Check if token is still valid
                    if self.token_expiry and time.time() < self.token_expiry:
                        log.debug("Schwab: Loaded cached access token")
                        return
            except Exception as e:
                log.debug(f"Schwab: Failed to load cached token: {e}")
        
        self.access_token = None
        self.token_expiry = None
    
    def _save_cached_token(self) -> None:
        """Save token to cache file."""
        try:
            with open(TOKEN_CACHE_FILE, "w") as f:
                json.dump({
                    "access_token": self.access_token,
                    "token_expiry": self.token_expiry
                }, f)
        except Exception as e:
            log.debug(f"Schwab: Failed to cache token: {e}")
    
    def get_access_token(self) -> str:
        """Get valid access token, refreshing if needed."""
        # Check if current token is valid
        if self.access_token and self.token_expiry and time.time() < self.token_expiry - 60:
            return self.access_token
        
        # Need to refresh
        log.info("Schwab: Refreshing access token...")
        self._refresh_token()
        return self.access_token
    
    def _refresh_token(self) -> None:
        """Request new access token using client credentials (OAuth2 Client Credentials flow)."""
        try:
            # Base64 encode credentials
            credentials = f"{self.client_id}:{self.client_secret}"
            encoded = base64.b64encode(credentials.encode()).decode()
            
            response = requests.post(
                SCHWAB_OAUTH_URL,
                headers={
                    "Authorization": f"Basic {encoded}",
                    "Content-Type": "application/x-www-form-urlencoded"
                },
                data={"grant_type": "client_credentials"},
                timeout=10
            )
            response.raise_for_status()
            
            data = response.json()
            self.access_token = data.get("access_token")
            expires_in = int(data.get("expires_in", 1800))  # Default 30 minutes, ensure int
            self.token_expiry = time.time() + expires_in
            
            self._save_cached_token()
            log.info(f"Schwab: Access token refreshed (expires in {expires_in}s)")
        
        except requests.exceptions.RequestException as e:
            log.error(f"Schwab: OAuth token refresh failed: {e}")
            raise
    
    def get_headers(self) -> Dict[str, str]:
        """Get headers for API requests with authorization."""
        token = self.get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }


class SchwabMarketDataClient:
    """Schwab market data API client for quotes, candles, options chains."""
    
    def __init__(self, oauth_client: Optional[SchwabOAuthClient] = None):
        if oauth_client is None:
            oauth_client = SchwabOAuthClient()
        self.oauth = oauth_client
        self.session = requests.Session()
    
    def get_quote(self, symbol: str) -> Optional[Dict]:
        """Get current quote for symbol."""
        try:
            url = f"{SCHWAB_MARKET_DATA_URL}/quotes/{symbol}"
            response = self.session.get(
                url,
                headers=self.oauth.get_headers(),
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            log.warning(f"Schwab: Failed to get quote for {symbol}: {e}")
            return None
    
    def get_candles(self, symbol: str, period_type: str = "day", period: int = 5, 
                   frequency_type: str = "minute", frequency: int = 15) -> Optional[Dict]:
        """
        Get candles (OHLCV bars) for symbol.
        
        Args:
            symbol: Stock symbol
            period_type: "day", "month", "year", "ytd"
            period: Number of periods to fetch
            frequency_type: "minute", "daily", "weekly", "monthly"
            frequency: 1, 5, 10, 15, 30 for minute; 1 for daily/weekly/monthly
        """
        try:
            params = {
                "periodType": period_type,
                "period": period,
                "frequencyType": frequency_type,
                "frequency": frequency
            }
            url = f"{SCHWAB_MARKET_DATA_URL}/pricehistory"
            response = self.session.get(
                url,
                headers=self.oauth.get_headers(),
                params={**params, "symbol": symbol},
                timeout=15
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            log.warning(f"Schwab: Failed to get candles for {symbol}: {e}")
            return None
    
    def get_option_chains(self, symbol: str, contract_type: str = "ALL") -> Optional[Dict]:
        """
        Get options chains for symbol.
        
        Args:
            symbol: Stock symbol
            contract_type: "CALL", "PUT", "ALL"
        """
        try:
            params = {
                "symbol": symbol,
                "contractType": contract_type
            }
            url = f"{SCHWAB_MARKET_DATA_URL}/chains"
            response = self.session.get(
                url,
                headers=self.oauth.get_headers(),
                params=params,
                timeout=15
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            log.warning(f"Schwab: Failed to get options chains for {symbol}: {e}")
            return None


# Singleton instances
_oauth_client: Optional[SchwabOAuthClient] = None
_market_data_client: Optional[SchwabMarketDataClient] = None


def get_schwab_oauth_client() -> SchwabOAuthClient:
    """Get or create Schwab OAuth client singleton."""
    global _oauth_client
    if _oauth_client is None:
        _oauth_client = SchwabOAuthClient()
    return _oauth_client


def get_schwab_market_data_client() -> SchwabMarketDataClient:
    """Get or create Schwab market data client singleton."""
    global _market_data_client
    if _market_data_client is None:
        oauth = get_schwab_oauth_client()
        _market_data_client = SchwabMarketDataClient(oauth)
    return _market_data_client

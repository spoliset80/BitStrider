"""Centralised configuration — all env vars in one place, no logic."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class Config:
    # Runtime mode
    mode: str                      # "paper" | "live"

    # Credentials
    user_token: str
    paper_key: str
    paper_secret: str
    live_key: str
    live_secret: str

    # Channels  {channel_id: handler_type}
    # handler types: "options" | "equity" | "spx" | "breakout"
    channel_types: Dict[str, str] = field(default_factory=dict)

    # Risk / allocation
    confidence_min: int   = 70
    order_notional: float = 500.0
    max_positions:  int   = 70
    max_daily_spend: float = 5000.0
    dedupe_ticker:  bool  = True
    alloc_low_pct:  float = 1.0    # conf 70-79 %
    alloc_med_pct:  float = 2.0    # conf 80-89 %
    alloc_high_pct: float = 3.0    # conf 90+  %

    # Polling
    poll_secs:     int = 60
    history_limit: int = 50

    # Market hours (orders only placed inside this window; polling continues 24/7)
    market_hours_only: bool = True
    market_open:  str = "09:30"   # ET
    market_close: str = "16:00"   # ET
    market_tz:    str = "America/New_York"

    # SPX / SPY settings
    spx_notional:  float = 300.0   # $ per SPY 0DTE trade
    spx_stop_pct:  float = 50.0    # stop-loss at 50 % of premium paid
    spx_target_pct: float = 100.0  # profit target at 2x premium paid

    # Breakout swing-option settings
    breakout_notional: float = 400.0   # $ per swing call
    breakout_dte:      int   = 45      # target days-to-expiry for swing calls
    breakout_min_dte:  int   = 30      # never pick an expiry sooner than this

    @property
    def alpaca_key(self) -> str:
        return self.live_key if self.mode == "live" else self.paper_key

    @property
    def alpaca_secret(self) -> str:
        return self.live_secret if self.mode == "live" else self.paper_secret

    @property
    def alpaca_paper(self) -> bool:
        return self.mode != "live"


def load_config() -> Config:
    """Build Config from environment variables."""
    raw_ids   = os.getenv("DISCORD_CHANNEL_IDS",
                          "753377655532945558,752750381918060589,"
                          "769046364738289734,744643208973254726")
    raw_types = os.getenv("DISCORD_CHANNEL_TYPES", "")

    # Parse explicit type overrides:  "id1:spx,id2:equity"
    channel_types: Dict[str, str] = {}
    if raw_types:
        for part in raw_types.split(","):
            part = part.strip()
            if ":" in part:
                cid, ctype = part.split(":", 1)
                channel_types[cid.strip()] = ctype.strip().lower()

    # Fill remaining channels from DISCORD_CHANNEL_IDS (default = "options")
    for cid in raw_ids.split(","):
        cid = cid.strip()
        if cid and cid not in channel_types:
            channel_types[cid] = "options"

    return Config(
        mode         = os.getenv("DISCORD_OPTIONS_MODE", "paper"),
        user_token   = os.getenv("DISCORD_USER_TOKEN", ""),
        paper_key    = os.getenv("PAPER_ALPACA_API_KEY", ""),
        paper_secret = os.getenv("PAPER_ALPACA_API_SECRET", ""),
        live_key     = os.getenv("LIVE_ALPACA_API_KEY", ""),
        live_secret  = os.getenv("LIVE_ALPACA_API_SECRET", ""),
        channel_types = channel_types,
        confidence_min  = int(os.getenv("DISCORD_CONFIDENCE_MIN", "70")),
        order_notional  = float(os.getenv("DISCORD_ORDER_NOTIONAL", "500")),
        max_positions   = int(os.getenv("DISCORD_MAX_POSITIONS", "70")),
        max_daily_spend = float(os.getenv("DISCORD_MAX_DAILY_SPEND", "5000")),
        dedupe_ticker   = os.getenv("DISCORD_DEDUPE_TICKER", "true").lower() == "true",
        alloc_low_pct   = float(os.getenv("DISCORD_ALLOC_LOW_PCT", "1.0")),
        alloc_med_pct   = float(os.getenv("DISCORD_ALLOC_MED_PCT", "2.0")),
        alloc_high_pct  = float(os.getenv("DISCORD_ALLOC_HIGH_PCT", "3.0")),
        spx_notional    = float(os.getenv("DISCORD_SPX_NOTIONAL", "300")),
        spx_stop_pct    = float(os.getenv("DISCORD_SPX_STOP_PCT", "50")),
        spx_target_pct  = float(os.getenv("DISCORD_SPX_TARGET_PCT", "100")),
        breakout_notional = float(os.getenv("DISCORD_BREAKOUT_NOTIONAL", "400")),
        breakout_dte      = int(os.getenv("DISCORD_BREAKOUT_DTE", "45")),
        breakout_min_dte  = int(os.getenv("DISCORD_BREAKOUT_MIN_DTE", "30")),
        market_hours_only = os.getenv("DISCORD_MARKET_HOURS_ONLY", "true").lower() == "true",
        market_open       = os.getenv("DISCORD_MARKET_OPEN", "09:30"),
        market_close      = os.getenv("DISCORD_MARKET_CLOSE", "16:00"),
        market_tz         = os.getenv("DISCORD_MARKET_TZ", "America/New_York"),
    )

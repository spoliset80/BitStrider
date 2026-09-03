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
    alloc_min_notional: float = 250.0  # never size a trade below this (small accounts)
    alloc_max_bp_pct: float = 25.0     # hard ceiling: % of BP a single trade may use
    price_above_last_pct: float = 2.0  # limit price = last trade (or mid) * (1 + this/100), overrides chat-alerted price
    use_technical_score: bool = True   # backtested as no better than random — see notes before relying on it
    max_hold_days: int = 1             # force-close positions older than this (0 = wait for 'Out' only)

    # Equity alerts → options conversion (cheaper than buying shares outright)
    equity_as_options:      bool  = True
    equity_opt_moneyness:   str   = "ATM"   # ITM | ATM | OTM
    equity_opt_moneyness_pct: float = 5.0   # % offset from spot for ITM/OTM
    equity_opt_expiry_mode: str   = "week"  # "week" = this week's Friday | "dte" = use equity_opt_dte
    equity_opt_dte:         int   = 7
    equity_opt_min_dte:     int   = 1

    # Polling
    poll_secs:     int = 60
    history_limit: int = 50

    # Market hours (orders only placed inside this window; polling continues 24/7)
    market_hours_only: bool = True
    market_open:  str = "09:30"   # ET
    market_close: str = "16:00"   # ET
    market_tz:    str = "America/New_York"

    # SPX / SPY settings
    spx_notional:  float = 300.0   # $ per SPY 0DTE trade (fallback when BP unavailable)
    spx_stop_pct:  float = 50.0    # stop-loss at 50 % of premium paid
    spx_target_pct: float = 100.0  # profit target at 2x premium paid
    spx_bp_pct:    float = 80.0    # % of available options BP to use per SPX trade

    # Breakout swing-option settings
    breakout_notional: float = 400.0   # $ per swing call
    breakout_dte:      int   = 45      # target days-to-expiry for swing calls
    breakout_min_dte:  int   = 30      # never pick an expiry sooner than this
    breakout_trail_pct: float = 40.0  # trailing stop % below option premium high

    @property
    def alpaca_key(self) -> str:
        return self.live_key if self.mode == "live" else self.paper_key

    @property
    def alpaca_secret(self) -> str:
        return self.live_secret if self.mode == "live" else self.paper_secret

    @property
    def alpaca_paper(self) -> bool:
        return self.mode != "live"


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in ("true", "1", "yes", "on")


# env var -> (Config field, converter). Defaults live on the dataclass only.
_ENV_MAP = {
    "DISCORD_CONFIDENCE_MIN":           ("confidence_min", int),
    "DISCORD_ORDER_NOTIONAL":           ("order_notional", float),
    "DISCORD_MAX_POSITIONS":            ("max_positions", int),
    "DISCORD_MAX_DAILY_SPEND":          ("max_daily_spend", float),
    "DISCORD_DEDUPE_TICKER":            ("dedupe_ticker", _as_bool),
    "DISCORD_ALLOC_LOW_PCT":            ("alloc_low_pct", float),
    "DISCORD_ALLOC_MED_PCT":            ("alloc_med_pct", float),
    "DISCORD_ALLOC_HIGH_PCT":           ("alloc_high_pct", float),
    "DISCORD_ALLOC_MIN_NOTIONAL":       ("alloc_min_notional", float),
    "DISCORD_ALLOC_MAX_BP_PCT":         ("alloc_max_bp_pct", float),
    "DISCORD_PRICE_ABOVE_LAST_PCT":     ("price_above_last_pct", float),
    "DISCORD_USE_TECHNICAL_SCORE":      ("use_technical_score", _as_bool),
    "DISCORD_MAX_HOLD_DAYS":            ("max_hold_days", int),
    "DISCORD_EQUITY_AS_OPTIONS":        ("equity_as_options", _as_bool),
    "DISCORD_EQUITY_OPT_MONEYNESS":     ("equity_opt_moneyness", lambda s: s.strip().upper()),
    "DISCORD_EQUITY_OPT_MONEYNESS_PCT": ("equity_opt_moneyness_pct", float),
    "DISCORD_EQUITY_OPT_EXPIRY_MODE":   ("equity_opt_expiry_mode", lambda s: s.strip().lower()),
    "DISCORD_EQUITY_OPT_DTE":           ("equity_opt_dte", int),
    "DISCORD_EQUITY_OPT_MIN_DTE":       ("equity_opt_min_dte", int),
    "DISCORD_SPX_NOTIONAL":             ("spx_notional", float),
    "DISCORD_SPX_STOP_PCT":             ("spx_stop_pct", float),
    "DISCORD_SPX_TARGET_PCT":           ("spx_target_pct", float),
    "DISCORD_SPX_BP_PCT":               ("spx_bp_pct", float),
    "DISCORD_BREAKOUT_NOTIONAL":        ("breakout_notional", float),
    "DISCORD_BREAKOUT_DTE":             ("breakout_dte", int),
    "DISCORD_BREAKOUT_MIN_DTE":         ("breakout_min_dte", int),
    "DISCORD_BREAKOUT_TRAIL_PCT":       ("breakout_trail_pct", float),
    "DISCORD_MARKET_HOURS_ONLY":        ("market_hours_only", _as_bool),
    "DISCORD_MARKET_OPEN":              ("market_open", str),
    "DISCORD_MARKET_CLOSE":             ("market_close", str),
    "DISCORD_MARKET_TZ":                ("market_tz", str),
}


def load_config() -> Config:
    """Build Config from environment variables.

    Only env vars that are actually set override the dataclass defaults, so a
    default is never defined in two places.
    """
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

    overrides = {}
    for env_key, (field_name, convert) in _ENV_MAP.items():
        raw = os.getenv(env_key)
        if raw is None or raw.strip() == "":
            continue
        try:
            overrides[field_name] = convert(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{env_key}={raw!r} is not a valid {field_name}") from None

    return Config(
        mode         = os.getenv("DISCORD_OPTIONS_MODE", "paper"),
        user_token   = os.getenv("DISCORD_USER_TOKEN", ""),
        paper_key    = os.getenv("PAPER_ALPACA_API_KEY", ""),
        paper_secret = os.getenv("PAPER_ALPACA_API_SECRET", ""),
        live_key     = os.getenv("LIVE_ALPACA_API_KEY", ""),
        live_secret  = os.getenv("LIVE_ALPACA_API_SECRET", ""),
        channel_types = channel_types,
        **overrides,
    )

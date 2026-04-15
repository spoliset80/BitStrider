"""
ApexTrader - Configuration
Professional Automated Trading System
Modular architecture with multiple strategies and PDT compliance
"""


import os
# --- .env support: load user/environment profile if present ---
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
    if os.path.exists(_env_path):
        load_dotenv(_env_path, override=False)
except ImportError:
    pass  # python-dotenv not installed; skip .env loading

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Broker Selection
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
STOCKS_BROKER = os.getenv("STOCKS_BROKER", "alpaca")   # 'alpaca' or 'etrade'
OPTIONS_BROKER = "alpaca"                               # Only Alpaca supports options

# ─────────────────────────────────────────────────────────────────
# Options Trading Configuration (Level 3 account)
#
# MASTER KILL-SWITCH: set OPTIONS_ENABLED=false in .env to disable
# all options trading system-wide (scanner, executor, main loop).
# Default: true (enabled). Safe to flip live without restart via env.
#
# Allocation: 15% of portfolio. Strategies: momentum calls, bear puts,
# covered calls on held positions. Expiry: 7–21 DTE (near-term).
# ─────────────────────────────────────────────────────────────────
OPTIONS_ENABLED             = os.getenv("OPTIONS_ENABLED", "true").lower() in ("1", "true", "yes")
OPTIONS_ALLOCATION_PCT      = float(os.getenv("OPTIONS_ALLOCATION_PCT", "28.0"))  # % of equity for all options (increased for sniper focus)
OPTIONS_MAX_POSITIONS       = int(os.getenv("OPTIONS_MAX_POSITIONS", "5"))        # max open options contracts (5 max for sniper + momentum)
EXTENDED_HOURS_EQUITY_TRADING = os.getenv("EXTENDED_HOURS_EQUITY_TRADING", "false").lower() in ("1", "true", "yes")  # Allow equity trades pre/post market
OPTIONS_DTE_MIN             = int(os.getenv("OPTIONS_DTE_MIN", "14"))             # min days-to-expiry at entry (14 avoids forced same-day close = PDT hit)
OPTIONS_DTE_MAX             = int(os.getenv("OPTIONS_DTE_MAX", "40"))             # max days-to-expiry at entry
OPTIONS_DELTA_TARGET        = float(os.getenv("OPTIONS_DELTA_TARGET", "0.40"))    # target delta (0.30-0.50)
OPTIONS_MIN_OPEN_INTEREST   = int(os.getenv("OPTIONS_MIN_OPEN_INTEREST", "100"))  # keep at 100 for broadest universe
OPTIONS_MAX_SPREAD_PCT      = float(os.getenv("OPTIONS_MAX_SPREAD_PCT", "10.0"))  # max bid/ask spread % of mid
OPTIONS_MAX_IV_PCT          = float(os.getenv("OPTIONS_MAX_IV_PCT", "150.0"))     # skip when IV is extreme
OPTIONS_MIN_IV_PCT          = float(os.getenv("OPTIONS_MIN_IV_PCT", "15.0"))      # skip when IV is too flat
OPTIONS_PROFIT_TARGET_PCT   = float(os.getenv("OPTIONS_PROFIT_TARGET_PCT", "50.0"))  # close at +50% gain
OPTIONS_STOP_LOSS_PCT       = float(os.getenv("OPTIONS_STOP_LOSS_PCT", "30.0"))      # close at -30% loss

# ─────────────────────────────────────────────────────────────────
# SNIPER OPTIONS MODE — High-Confidence, Precision Entry
# ─────────────────────────────────────────────────────────────────
OPTIONS_SNIPER_MODE_ENABLED = os.getenv("OPTIONS_SNIPER_MODE", "true").lower() in ("1", "true", "yes")
OPTIONS_SNIPER_CONFIDENCE_MIN = float(os.getenv("OPTIONS_SNIPER_CONFIDENCE_MIN", "0.85"))  # Only 85%+ confidence
OPTIONS_SNIPER_UNUSUAL_VOL_MIN = float(os.getenv("OPTIONS_SNIPER_UNUSUAL_VOL_MIN", "3.0"))  # 3x IV rank or better
OPTIONS_SNIPER_IV_RANK_MAX_CALL = float(os.getenv("OPTIONS_SNIPER_IV_RANK_MAX_CALL", "35.0"))  # Buy cheap calls
OPTIONS_SNIPER_IV_RANK_MAX_PUT = float(os.getenv("OPTIONS_SNIPER_IV_RANK_MAX_PUT", "50.0"))   # Buy cheap puts
OPTIONS_SNIPER_OI_MIN = int(os.getenv("OPTIONS_SNIPER_OI_MIN", "1000"))  # Tight OI requirement
OPTIONS_SNIPER_MAX_SPREAD_PCT = float(os.getenv("OPTIONS_SNIPER_MAX_SPREAD_PCT", "8.0"))  # <8% bid/ask
OPTIONS_SNIPER_RR_MIN = float(os.getenv("OPTIONS_SNIPER_RR_MIN", "2.0"))  # 2:1 min risk/reward
OPTIONS_SNIPER_ALLOCATION_PCT = float(os.getenv("OPTIONS_SNIPER_ALLOCATION_PCT", "12.0"))  # 12% of total equity to sniper
OPTIONS_MOMENTUM_ALLOCATION_PCT = float(os.getenv("OPTIONS_MOMENTUM_ALLOCATION_PCT", "10.0"))  # 10% to momentum
OPTIONS_INCOME_ALLOCATION_PCT = float(os.getenv("OPTIONS_INCOME_ALLOCATION_PCT", "6.0"))   # 6% to covered calls
OPTIONS_COVERED_CALL_DELTA  = float(os.getenv("OPTIONS_COVERED_CALL_DELTA", "0.25")) # sell OTM calls ~0.25 delta
OPTIONS_MIN_SIGNAL_CONFIDENCE = float(os.getenv("OPTIONS_MIN_SIGNAL_CONFIDENCE", "0.70"))  # relaxed from 0.80 to 0.70
OPTIONS_MIN_STOCK_PRICE     = float(os.getenv("OPTIONS_MIN_STOCK_PRICE", "4.0"))  # tighter low-price gate to avoid noisy penny names
OPTIONS_MIN_MOVE_PCT        = float(os.getenv("OPTIONS_MIN_MOVE_PCT", "1.0"))     # min % daily move to qualify (was 2.0)
OPTIONS_MIN_RVOL            = float(os.getenv("OPTIONS_MIN_RVOL", "1.0"))         # min relative volume for MomentumCall entry (was 1.5)
OPTIONS_MIN_ADV             = float(os.getenv("OPTIONS_MIN_ADV", "250_000"))    # relaxed volume gate for more signals
OPTIONS_UNIVERSE_OVERRIDE   = os.getenv("OPTIONS_UNIVERSE_OVERRIDE", "").strip()  # comma-separated tickers to force a smaller options universe
OPTIONS_STOP_COOLDOWN_DAYS  = int(os.getenv("OPTIONS_STOP_COOLDOWN_DAYS", "2"))   # no re-entry within N days after a stop on same symbol
OPTIONS_EARNINGS_AVOID_DAYS = int(os.getenv("OPTIONS_EARNINGS_AVOID_DAYS", "15")) # skip entries if earnings within N calendar days

# ─────────────────────────────────────────────────────────────────
# SMART UNIVERSE — Tier-Based Ticker Segmentation
# ─────────────────────────────────────────────────────────────────
SMART_UNIVERSE_ENABLED      = os.getenv("SMART_UNIVERSE_ENABLED", "true").lower() in ("1", "true", "yes")

# TIER A — Mega-Liquid Segment (Always scan, 60% of options capital if signal fires)
TIER_A_ALLOCATION_PCT       = float(os.getenv("TIER_A_ALLOCATION_PCT", "18.0"))    # 18% of equity to mega-caps
TIER_A_MAX_POSITIONS        = int(os.getenv("TIER_A_MAX_POSITIONS", "2"))         # Max 2 concurrent mega-cap positions
TIER_A_CONFIDENCE_MIN       = float(os.getenv("TIER_A_CONFIDENCE_MIN", "0.85"))   # Strict: 85%+ only
TIER_A_SCAN_EVERY_MIN       = int(os.getenv("TIER_A_SCAN_EVERY_MIN", "5"))       # Re-scan every 5 minutes

# TIER B — Unusual Volume/Opportunity Segment (Dynamic, 30% capital, higher turnover)
TIER_B_ALLOCATION_PCT       = float(os.getenv("TIER_B_ALLOCATION_PCT", "8.0"))    # 8% of equity to unusual vol plays
TIER_B_MAX_POSITIONS        = int(os.getenv("TIER_B_MAX_POSITIONS", "3"))         # Max 3 concurrent positions
TIER_B_CONFIDENCE_MIN       = float(os.getenv("TIER_B_CONFIDENCE_MIN", "0.70"))   # Relaxed: 70%+ OK (faster rotation)
TIER_B_IV_RANK_MAX          = float(os.getenv("TIER_B_IV_RANK_MAX", "30.0"))     # Hunt for IV Rank <30% (cheap vol)
TIER_B_HOLD_DAYS            = int(os.getenv("TIER_B_HOLD_DAYS", "5"))            # Max 5 days per position (rotate out)
TIER_B_SCAN_EVERY_MIN       = int(os.getenv("TIER_B_SCAN_EVERY_MIN", "30"))      # Re-scan TI every 30 minutes
TIER_B_LIQUID_HOURS_ONLY    = os.getenv("TIER_B_LIQUID_HOURS_ONLY", "true").lower() in ("1", "true", "yes")  # 9:30-11am + 2-3pm only

# TIER C — Equity Breakout Bridge (10% capital, hedge + correlation play)
TIER_C_ALLOCATION_PCT       = float(os.getenv("TIER_C_ALLOCATION_PCT", "2.0"))    # 2% of equity capital
TIER_C_MAX_POSITIONS        = int(os.getenv("TIER_C_MAX_POSITIONS", "2"))         # Max 2 positions
TIER_C_CONFIDENCE_MIN       = float(os.getenv("TIER_C_CONFIDENCE_MIN", "0.65"))   # Relaxed: 65%+ (supporting equities)
TIER_C_SELL_PUT_BUFFER_PCT  = float(os.getenv("TIER_C_SELL_PUT_BUFFER_PCT", "5.0"))  # Sell puts 5% below support
TIER_C_BUY_CALL_BUFFER_PCT  = float(os.getenv("TIER_C_BUY_CALL_BUFFER_PCT", "2.0"))  # Buy calls 2% above resistance

# Tier A: Mega-Liquid Tickers (Static, Core Scanning Universe)
TIER_A_TICKERS = os.getenv("TIER_A_TICKERS", "SPY,QQQ,AAPL,MSFT,NVDA,TSLA,AMD,GOOGL,META,AMZN,NFLX").split(",")
TIER_A_TICKERS = [t.strip().upper() for t in TIER_A_TICKERS if t.strip()]

# ─────────────────────────────────────────────────────────────────
# ADAPTIVE INTELLIGENCE — Dynamic Filtering & Sizing
# ─────────────────────────────────────────────────────────────────
ADAPTIVE_SIGNAL_FILTERING = os.getenv("ADAPTIVE_SIGNAL_FILTERING", "true").lower() in ("1", "true", "yes")
ADAPTIVE_POSITION_SIZING = os.getenv("ADAPTIVE_POSITION_SIZING", "true").lower() in ("1", "true", "yes")
ADAPTIVE_VOLATILITY_GATES = os.getenv("ADAPTIVE_VOLATILITY_GATES", "true").lower() in ("1", "true", "yes")

# Signal validation: Confidence gates adapt to regime
SIGNAL_CONFIDENCE_BULL_RELAX_PCT = float(os.getenv("SIGNAL_CONFIDENCE_BULL_RELAX_PCT", "5.0"))  # Relax 5% in bull
SIGNAL_CONFIDENCE_BEAR_TIGHTEN_PCT = float(os.getenv("SIGNAL_CONFIDENCE_BEAR_TIGHTEN_PCT", "5.0"))  # Tighten 5% in bear

# Position sizing: Adjust multipliers based on strategy win rate (from daily analytics)
POSITION_SIZE_DYNAMIC = os.getenv("POSITION_SIZE_DYNAMIC", "true").lower() in ("1", "true", "yes")
POSITION_SIZE_WIN_RATE_BOOST = float(os.getenv("POSITION_SIZE_WIN_RATE_BOOST", "1.3"))  # 30% boost at 70%+ win rate
POSITION_SIZE_WIN_RATE_THRESHOLD = float(os.getenv("POSITION_SIZE_WIN_RATE_THRESHOLD", "0.65"))  # Boost threshold

# Smart position rotation (score-based SWAP instead of age-based)
POSITION_ROTATION_ENABLED = os.getenv("POSITION_ROTATION_ENABLED", "true").lower() in ("1", "true", "yes")
POSITION_ROTATION_MIN_SCORE = float(os.getenv("POSITION_ROTATION_MIN_SCORE", "50.0"))  # Only rotate if <50

# Risk monitoring: Circuit breakers
RISK_MONITOR_ENABLED = os.getenv("RISK_MONITOR_ENABLED", "true").lower() in ("1", "true", "yes")
DRAWDOWN_WARNING_PCT = float(os.getenv("DRAWDOWN_WARNING_PCT", "-5.0"))  # Warning at -5%
DRAWDOWN_CAUTION_PCT = float(os.getenv("DRAWDOWN_CAUTION_PCT", "-8.0"))  # Caution at -8%
DRAWDOWN_CIRCUIT_BREAKER_PCT = float(os.getenv("DRAWDOWN_CIRCUIT_BREAKER_PCT", "-10.0"))  # Halt at -10%
DRAWDOWN_EMERGENCY_PCT = float(os.getenv("DRAWDOWN_EMERGENCY_PCT", "-15.0"))  # Full halt at -15%

# Performance analytics: Track metrics for continuous improvement
PERFORMANCE_ANALYTICS_ENABLED = os.getenv("PERFORMANCE_ANALYTICS_ENABLED", "true").lower() in ("1", "true", "yes")
ANALYTICS_HOURLY_REPORTING = os.getenv("ANALYTICS_HOURLY_REPORTING", "true").lower() in ("1", "true", "yes")
ANALYTICS_STRATEGY_BREAKDOWN = os.getenv("ANALYTICS_STRATEGY_BREAKDOWN", "true").lower() in ("1", "true", "yes")
ANALYTICS_REGIME_METRICS = os.getenv("ANALYTICS_REGIME_METRICS", "true").lower() in ("1", "true", "yes")

# ─────────────────────────────────────────────────────────────────
# Tickers that actively trade liquid options.
# Loaded dynamically from data/ti_unusual_options.json (written by capture_tradeideas.py
# every time the TI unusualoptionsvolume scan is scraped).  Falls back to the
# hardcoded list below if the file doesn't exist or is empty.
_OPTIONS_FALLBACK_UNIVERSE = [
    # Mega-cap tech — always liquid options
    "AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "META", "TSLA", "AMZN", "NFLX",
    # High-beta momentum favourites
    "MARA", "COIN", "PLTR", "SMCI", "CRWD", "NET", "SNOW",
    # ETFs with liquid options chains
    "SPY", "QQQ", "IWM", "SQQQ", "SPXU", "UVXY", "VIX",
    # Biotech / speculative with options
    "MRNA", "BCRX",
]

# ────────────────────────────────────────────────────────────────────────────────
# TRADE IDEAS EQUITY TRADING (Primary Signal Source for TI Tickers)
# ────────────────────────────────────────────────────────────────────────────────
# When enabled, the TradeIdeasEquityStrategy prioritizes tickers from TI primary
# universe (data/ti_primary.json) during equity scans. Enables immediate equity trading
# during extended hours (4AM-9:30AM pre-market) without waiting for market open.
#
# Configuration:
#   USE_TI_PRIMARY_EQUITY_TRADING: Enable TI tickers as primary equity signal source (default: true)
#   TI_EQUITY_BASE_CONFIDENCE: Base confidence for TI tickers (default: 0.85, range: 0.80-0.95)
#
USE_TI_PRIMARY_EQUITY_TRADING = os.getenv('USE_TI_PRIMARY_EQUITY_TRADING', 'true').lower() in ('1', 'true', 'yes')
TI_EQUITY_BASE_CONFIDENCE = float(os.getenv('TI_EQUITY_BASE_CONFIDENCE', '0.85'))

# ────────────────────────────────────────────────────────────────────────────────
# EQUITY ENSEMBLE CONFIGURATION
# ────────────────────────────────────────────────────────────────────────────────
# 4-Layer Equity Ensemble with TI + Sweepea dual confirmation
USE_EQUITY_ENSEMBLE = os.getenv('USE_EQUITY_ENSEMBLE', 'true').lower() in ('1', 'true', 'yes')
EQUITY_ENSEMBLE_TI_SWEEPEA_DUAL_CONFIDENCE = 0.92  # TI + Sweepea both fire
EQUITY_ENSEMBLE_TI_ALONE_CONFIDENCE = 0.85         # TI only
EQUITY_ENSEMBLE_SWEEPEA_ALONE_CONFIDENCE = 0.75    # Sweepea without TI

# ────────────────────────────────────────────────────────────────────────────────
# OPTIONS ENSEMBLE CONFIGURATION
# ────────────────────────────────────────────────────────────────────────────────
# 4-Layer Options Ensemble with cross-asset linking
USE_OPTIONS_ENSEMBLE = os.getenv('USE_OPTIONS_ENSEMBLE', 'true').lower() in ('1', 'true', 'yes')
OPTIONS_UNUSUAL_VOLUME_CONFIDENCE = 0.82            # TI unusual options
OPTIONS_IV_RANK_CONFIDENCE_HIGH = 0.78              # High IV rank
OPTIONS_IV_RANK_CONFIDENCE_LOW = 0.72               # Low IV rank
OPTIONS_DIRECTIONAL_CROSSOVER_BONUS = 0.05          # +5% when equity+options align
OPTIONS_GREEKS_DELTA_MIN = 0.25                     # Minimum delta (avoid edge cases)
OPTIONS_GREEKS_DELTA_MAX = 0.75                     # Maximum delta (sweet spot)

def _load_options_universe() -> list:
    """Load live TI unusual-options-volume tickers.

    Returns the scraped unusual options universe, falling back to a hardcoded list
    only when the TI file is unavailable or empty.
    """
    import json as _json
    import re as _re
    _VALID_TICKER = _re.compile(r'^[A-Z]{1,5}$')
    _ti_file = os.path.join(os.path.dirname(__file__), "..", "data", "ti_unusual_options.json")
    try:
        with open(_ti_file, encoding="utf-8") as _f:
            _d = _json.load(_f)
        _tickers = [
            str(t).upper().strip()
            for t in _d.get("tickers", [])
            if t and _VALID_TICKER.match(str(t).upper().strip())
        ]
        if _tickers:
            return _tickers
    except Exception:
        pass
    return _OPTIONS_FALLBACK_UNIVERSE


def get_options_universe(require_ti_file: bool = False) -> list:
    """Return the live options universe, applying override rules.

    Primary source: latest TI capture tickers from data/ti_primary.json.
    Fallback source: active TI tickers from data/universe.json (tier 1 + tier 2).
    If OPTIONS_UNIVERSE_OVERRIDE is set, those tickers take precedence.
    If no primary or fallback universe is available, return the static fallback universe.
    """
    if OPTIONS_UNIVERSE_OVERRIDE:
        import re as _re
        _VALID_TICKER_OVERRIDE = _re.compile(r'^[A-Z]{1,5}$')
        _override_symbols = [
            t.strip().upper()
            for t in OPTIONS_UNIVERSE_OVERRIDE.split(",")
            if t and _VALID_TICKER_OVERRIDE.match(t.strip().upper())
        ]
        if _override_symbols:
            return list(dict.fromkeys(_override_symbols))

    universe = []
    try:
        from engine.equity.universe import get_ti_primary as _get_ti_primary
        universe = list(dict.fromkeys(_get_ti_primary()))
    except Exception:
        universe = []

    if universe:
        return universe

    try:
        from engine.equity.universe import get_tier as _get_tier
        universe = list(dict.fromkeys(_get_tier(1) + _get_tier(2)))
    except Exception:
        universe = []

    if universe:
        return universe

    if require_ti_file:
        raise FileNotFoundError("Primary TI universe (data/ti_primary.json or data/universe.json tiers 1+2) is missing or empty")

    return _OPTIONS_FALLBACK_UNIVERSE


OPTIONS_ELIGIBLE_UNIVERSE = get_options_universe()

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Alpaca API Configuration
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# PAPER mode is strongly recommended for development/testing.
# Set environment variable TRADE_MODE=paper or TRADE_MODE=live.
TRADE_MODE = os.getenv("TRADE_MODE", "paper").lower()
PAPER      = TRADE_MODE == "paper"
LIVE       = not PAPER
_MODE      = "PAPER" if PAPER else "LIVE"

API_KEY    = os.getenv(f"{_MODE}_ALPACA_API_KEY", "")
API_SECRET = os.getenv(f"{_MODE}_ALPACA_API_SECRET", "")
# SDK picks the correct endpoint automatically via paper=True/False — no URL override needed
ALPACA_BASE_URL = "https://paper-api.alpaca.markets" if PAPER else "https://api.alpaca.markets"

# Convenience for switching: override per branch by env var if needed.
MIN_POSITION_DOLLARS = float(os.getenv("MIN_POSITION_DOLLARS", "500"))
MIN_BUYING_POWER_PCT = float(os.getenv("MIN_BUYING_POWER_PCT", "10.0"))

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# E*TRADE API Configuration
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
ETRADE_CONSUMER_KEY    = os.getenv("ETRADE_CONSUMER_KEY", "")
ETRADE_CONSUMER_SECRET = os.getenv("ETRADE_CONSUMER_SECRET", "")
ETRADE_ACCOUNT_ID      = os.getenv("ETRADE_ACCOUNT_ID", "")
ETRADE_SANDBOX         = os.getenv("ETRADE_SANDBOX", "false").lower() == "true"

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Stock Universe
# Priority 1: Momentum stocks (scanned FIRST, highest allocation)
# Priority 2: Established tech and high short-float stocks
# Priority 3: Market ETFs for context
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
PRIORITY_1_MOMENTUM = [
    # ── Permanent core (never expire, always scanned) ──────────────
    # Crypto-leveraged / popular momentum plays
    "MARA", "WULF", "CORZ", "HUT", "IREN",
    # Biotech / speculative momentum
    "MRNA", "BCRX", "SNDX", "IMVT",
    # Energy / commodities momentum
    "RIG", "NOG", "CNX", "BTU", "DK",
    # ── Bear-market long plays (inverse ETFs — go UP when market falls) ──
    # Valid LONG buys in bear regime as LONG_ONLY_MODE=True
    "SQQQ", "SPXU", "UVXY", "TZA", "FAZ", "SOXS", "LABD", "DUST",
]

PRIORITY_2_ESTABLISHED = [
    # ── Permanent core (never expire) ─────────────────────────────
    # Tech giants — liquid at all times
    "AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "META", "TSLA", "AMZN",
    # High short-float perennials
    "LCID", "MVIS", "WKHS", "SNDX", "FUBO", "INDO", "SOXS", "UCO",
]

PRIORITY_3_MARKET = ["SPY", "QQQ", "IWM", "^VIX"]

# Delisted or broken tickers — filtered out at runtime
DELISTED_STOCKS = [
    # Truly delisted
    "IMV", "EKV", "AMTK", "SUNE",
    "CGV", "CHAC", "CIFG", "CNVS",
    # Index tickers (not tradeable)
    "DJI", "$DJI",
    # Broken / no-data tickers seen in live scans
    "ADR", "BF", "AMEX", "ADVB",
]

# Remove delisted from core lists
PRIORITY_1_MOMENTUM = [s for s in PRIORITY_1_MOMENTUM if s not in DELISTED_STOCKS]
PRIORITY_2_ESTABLISHED = [s for s in PRIORITY_2_ESTABLISHED if s not in DELISTED_STOCKS]

# ─── Dynamic universe: load TTL-managed tickers from data/universe.json ───────
# Trade Ideas updates and prediction picks live there, NOT in this file.
# Universe TTL values are configurable via env vars:
#   UNIVERSE_TTL_TIER1, UNIVERSE_TTL_TIER2, UNIVERSE_TTL_TIER3
# Defaults are currently 15 minutes per tier for live scan freshness.
# get_dynamic_universe() is called live each scan cycle so newly scraped TI
# tickers are picked up without restarting the bot.
from engine.equity.universe import get_tier as _get_tier, merge_live as _merge_live


def get_dynamic_universe() -> tuple:
    """Return (p1, p2, p3) merged lists, re-reading universe.json on every call."""
    _ex = set(DELISTED_STOCKS)
    p1 = _merge_live(_get_tier(1), PRIORITY_1_MOMENTUM,    _ex)
    p2 = _merge_live(_get_tier(2), PRIORITY_2_ESTABLISHED, _ex)
    p3 = _merge_live(_get_tier(3), [],                     _ex)
    return p1, p2, p3


# Module-level lists: populated once at startup as fallback / for any code that
# imports them directly.  get_scan_targets() always calls get_dynamic_universe()
# so the running bot never relies on these being fresh.
_dyn1, _dyn2, _dyn3 = get_dynamic_universe()
PRIORITY_1_MOMENTUM    = _dyn1
PRIORITY_2_ESTABLISHED = _dyn2
PRIORITY_FOLLOWING     = _dyn3
del _dyn1, _dyn2, _dyn3

STOCKS = {
    "priority_1": PRIORITY_1_MOMENTUM,
    "priority_2": PRIORITY_2_ESTABLISHED,
    "priority_3": PRIORITY_3_MARKET,
    "following":  PRIORITY_FOLLOWING,
}

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Trading Parameters ΓÇö Swing Trading Optimized
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
MAX_POSITIONS        = 25     # Temporarily increased to match current portfolio (was 12); will reduce once positions manage down
# When full, close the weakest position to make room if new signal conf > this threshold
SWAP_ON_FULL         = True   # enabled — close weakest position for a better signal when full
SWAP_MIN_CONFIDENCE  = 0.75   # Swap out weakest when new signal >= this confidence (was 0.85)
POSITION_SIZE_PCT    = 7.5    # 7.5% per position → up to 12 positions within 90% BP utilization
USE_RISK_EQUALIZED_SIZING = False  # use fixed position sizing instead of risk-scaled
RISK_PER_TRADE_PCT   = 0.8    # Risk 0.8% of account per trade (unused with fixed sizing)

# Small account reduction caps (sub-$5k equity)
SMALL_ACCOUNT_POSITION_SIZE_PCT = 7.5   # same 7.5% allocation for small accounts
SMALL_ACCOUNT_RISK_PER_TRADE_PCT = 0.5 # lower risk per trade for small accounts
SMALL_ACCOUNT_MIN_POSITION_DOLLARS = 5.0  # lowered to allow ~$5 entry for cheap tickers

# Tiered Profit Targets — aggressive: book profits faster
TAKE_PROFIT_EXTREME  = 35.0   # was 50
TAKE_PROFIT_HIGH     = 25.0   # was 40
TAKE_PROFIT_MEDIUM   = 18.0   # was 35
TAKE_PROFIT_NORMAL   = 12.0   # was 25

# Tiered Trailing Stops — tighter: lock in gains quickly
TRAILING_STOP_EXTREME = 12.0  # more room for extreme movers
TRAILING_STOP_HIGH    = 10.0  # high momentum
TRAILING_STOP_MEDIUM  =  8.0  # medium momentum
TRAILING_STOP_NORMAL  =  8.0  # default trailing stop

# Legacy (backward compat)
STOP_LOSS_PCT   = 3.0
TAKE_PROFIT_PCT = 18.0

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Dynamic ATR-Based Tier Assignment
# Lower thresholds = more stocks classified as high-volatility = tighter TP/SL
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
USE_DYNAMIC_TIERS  = True
ATR_TIER_EXTREME   = 5.0   # was 7.0
ATR_TIER_HIGH      = 3.0   # was 5.0
ATR_TIER_MEDIUM    = 1.5   # was 3.0

# Legacy static lists (used only if USE_DYNAMIC_TIERS=False)
EXTREME_MOMENTUM_STOCKS = ["UGRO", "VCX", "PTLE", "BIAF", "SATL", "ELAB"]
HIGH_MOMENTUM_STOCKS    = ["QNTM", "MRLN", "DMRA", "RCAX", "ALDX", "NAMM", "PAYP", "SER", "NAUT", "CGV"]

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Adaptive Scan Intervals (VIX-Based)
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
ADAPTIVE_INTERVALS          = True
SCAN_INTERVAL_EXTREME_VOL   = 3    # VIX > 30
SCAN_INTERVAL_HIGH_VOL      = 5    # VIX 26-30
SCAN_INTERVAL_MODERATE_VOL  = 10   # VIX 22-26
SCAN_INTERVAL_NORMAL_VOL    = 15   # VIX 18-22
SCAN_INTERVAL_CALM_VOL      = 20   # VIX 15-18
SCAN_INTERVAL_LOW_VOL       = 30   # VIX < 15
SCAN_INTERVAL_MIN            = 10  # Default fallback

# ─────────────────────────────────────────────────────────────────
# Kill Mode — Emergency Capital Protection
# Triggers a full portfolio close when extreme bear conditions hit.
# ─────────────────────────────────────────────────────────────────
KILL_MODE_VIX_LEVEL    = 40.0   # Absolute VIX level that triggers kill mode (2008/2020: 80+, crash: 40+)
KILL_MODE_SPY_DROP_PCT =  3.0   # SPY intraday drop from open (%) triggers kill mode
KILL_MODE_VIX_ROC_PCT  = 50.0   # VIX spike: up >50% in last 5 hours triggers kill mode
KILL_MODE_TRAIL_PCT    =  0.5   # PDT-safe hairpin trailing stop % placed on today's positions

# Market Hours Tuning
USE_MARKET_HOURS_TUNING    = True
PREMARKET_SCAN_INTERVAL    = 10
REGULAR_HOURS_SCAN_INTERVAL = 3
AFTERHOURS_SCAN_INTERVAL   = 10

# Position-Based Adaptive Scanning
USE_POSITION_TUNING      = True
HIGH_POSITION_INTERVAL   = 5    # was 10 — check more frequently when holding many positions
NORMAL_POSITION_INTERVAL = 3    # was 5
LOW_POSITION_INTERVAL    = 2    # was 3

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# VIX Rate-of-Change Filter
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
USE_VIX_ROC_FILTER  = True
VIX_ROC_THRESHOLD   = 20.0   # Block entries if VIX up >20% in last hour
VIX_ROC_PERIOD      = 5

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Live Trending Discovery
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
USE_LIVE_TRENDING       = False
TRENDING_SCAN_INTERVAL  = 60
TRENDING_MAX_RESULTS    = 20
TRENDING_MIN_MOMENTUM   = 3.0

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Finnhub Integration
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
USE_FINNHUB_DISCOVERY      = False
FINNHUB_API_KEY            = os.getenv("FINNHUB_API_KEY", "")
PRICE_DATA_SOURCE          = os.getenv("PRICE_DATA_SOURCE", "alpaca").strip().lower()
USE_FINNHUB_HISTORICAL     = PRICE_DATA_SOURCE == "finnhub" or os.getenv("USE_FINNHUB_HISTORICAL", "false").strip().lower() in ("1", "true", "yes")
USE_SENTIMENT_GATE         = False
SENTIMENT_BULLISH_THRESHOLD = 0.6

# Trade Ideas Discovery
# Scrapes TIPro highshortfloat + marketscope360 with Selenium.
# Requires a logged-in Chrome profile (TRADEIDEAS_CHROME_PROFILE) to get real data.
# Disabled by default — without a profile the scraper only hits the TI login page.
# To enable: set USE_TRADEIDEAS_DISCOVERY=true and TRADEIDEAS_CHROME_PROFILE=<profile>
# TRADEIDEAS_BROWSER: "edge" (pre-installed on Windows) or "chrome"
USE_TRADEIDEAS_DISCOVERY                          = __import__('os').getenv('USE_TRADEIDEAS_DISCOVERY', 'false').lower() == 'true'
USE_TRADEIDEAS_UNUSUAL_OPTIONS_DISCOVERY         = __import__('os').getenv('USE_TRADEIDEAS_UNUSUAL_OPTIONS_DISCOVERY', 'true').lower() == 'true'
USE_TRADEIDEAS_TOPLISTS_DISCOVERY                = __import__('os').getenv('USE_TRADEIDEAS_TOPLISTS_DISCOVERY', 'false').lower() == 'true'
TRADEIDEAS_SCAN_INTERVAL_MIN                     = 15
TRADEIDEAS_UNUSUAL_OPTIONS_SCAN_INTERVAL_MIN     = 30
TRADEIDEAS_TOPLISTS_SCAN_INTERVAL_MIN            = 180
TRADEIDEAS_HEADLESS                              = __import__('os').getenv('TRADEIDEAS_HEADLESS', 'false').lower() == 'true'
TRADEIDEAS_CHROME_PROFILE                        = __import__('os').getenv('TRADEIDEAS_CHROME_PROFILE', '')
TRADEIDEAS_BROWSER                                = __import__('os').getenv('TRADEIDEAS_BROWSER', 'edge')
TRADEIDEAS_UPDATE_CONFIG_FILE                     = True
TI_PRIMARY_SCAN_BATCH_LIMIT                       = int(__import__('os').getenv('TI_PRIMARY_SCAN_BATCH_LIMIT', '50'))

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Daily Limits
# ─────────────────────────────────────────────────────────────────
POSITION_CHECK_MIN       = 5
DAILY_LOSS_LIMIT_BULL_PCT = 1.0   # Halt if down >1% of start equity in bull regime
DAILY_LOSS_LIMIT_BEAR_PCT = 2.0   # Halt if down >2% of start equity in bear regime (wider room)
DAILY_PROFIT_TARGET       = 3500.0

# Quarterly Profit Target
USE_QUARTERLY_TARGET        = True
QUARTERLY_PROFIT_TARGET_PCT = 50.0   # Halt new entries once +50% equity this quarter

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Extended Hours Trading
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
EXTENDED_HOURS   = True
PREMARKET_START  = "07:00"
MARKET_OPEN      = "09:30"
MARKET_CLOSE     = "16:00"
AFTERHOURS_END   = "20:00"

# Set FORCE_SCAN=1 (env var) or pass --force CLI flag to bypass the
# market-hours gate when a high-confidence opportunity is spotted.
FORCE_SCAN = os.getenv("FORCE_SCAN", "false").lower() in ("1", "true", "yes")

# ─────────────────────────────────────────────────────────────────
# EOD (End-of-Day) Position Close
# Intraday strategies should never be held overnight — close by EOD_CLOSE_TIME
# ─────────────────────────────────────────────────────────────────
EOD_CLOSE_ENABLED    = True
EOD_CLOSE_TIME       = "15:50"   # Close intraday positions 10 min before market close
EOD_CLOSE_STRATEGIES = {         # Strategy names that must be closed same day
    "FloatRotation",
    "GapBreakout",
    "ORB",
    "VWAPReclaim",
    "PreMarketMomentum",
    "OpeningBellSurge",
    "PMHighBreakout",
    "EarlySqueeze",
}

# Stale order upgrade: unfilled orders older than this get re-submitted as market/limit
STALE_ORDER_MINUTES          = 360  # minutes before an unfilled order is considered stale
STALE_ORDER_MINUTES_INTRADAY =  30  # intraday strategies (ORB, surge, etc.) — cancel if unfilled after 30 min

# ─────────────────────────────────────────────────────────────────
# PDT Rules
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
PDT_ACCOUNT_MIN = 25000.0
PDT_MAX_TRADES  = 3
PDT_OPTIONS_DAY_TRADE_RESERVE = int(os.getenv("PDT_OPTIONS_DAY_TRADE_RESERVE", "1"))  # keep at least N day trades free for stock exits

# ─────────────────────────────────────────────────────────────────
# Email Notifications
# ─────────────────────────────────────────────────────────────────
USE_EMAIL_NOTIFICATIONS = os.getenv("USE_EMAIL_NOTIFICATIONS", "false").lower() in ("1", "true", "yes")
EMAIL_SMTP_SERVER       = os.getenv("EMAIL_SMTP_SERVER", "smtp.gmail.com")
EMAIL_SMTP_PORT         = int(os.getenv("EMAIL_SMTP_PORT", "587"))
EMAIL_SMTP_USER         = os.getenv("EMAIL_SMTP_USER", "")
EMAIL_SMTP_PASSWORD     = os.getenv("EMAIL_SMTP_PASSWORD", "")
EMAIL_FROM_ADDRESS      = os.getenv("EMAIL_FROM_ADDRESS", "apextrader_bot@gmail.com")
EMAIL_TO_ADDRESSES      = [a.strip() for a in os.getenv("EMAIL_TO_ADDRESSES", "spolisetti.archive@gmail.com,alerts@apextrader.example.com").split(",") if a.strip()]
EMAIL_SUBJECT_PREFIX    = os.getenv("EMAIL_SUBJECT_PREFIX", "ApexTrader EOD Report")
EMAIL_SCAN_MIN_INTERVAL_SEC = int(os.getenv("EMAIL_SCAN_MIN_INTERVAL_SEC", "600"))
EMAIL_SCAN_SEND_ON_CHANGE   = os.getenv("EMAIL_SCAN_SEND_ON_CHANGE", "true").lower() in ("1", "true", "yes")

# Enterprise Risk Controls (environment-overridable)
MIN_BUYING_POWER_PCT  = float(os.getenv("MIN_BUYING_POWER_PCT", "5.0"))   # Reserve this % of equity as free buffer (never spend it)
MIN_POSITION_DOLLARS  = float(os.getenv("MIN_POSITION_DOLLARS", "5"))   # Minimum trade size in $ — skip if downsized below this
PDT_WARN_AT_REMAINING = int(os.getenv("PDT_WARN_AT_REMAINING", "1"))      # Warn log when PDT trades remaining falls to this level

# Small account smart sizing (for ~$1k buying power)
SMALL_ACCOUNT_EQUITY_THRESHOLD = float(os.getenv("SMALL_ACCOUNT_EQUITY_THRESHOLD", "5000"))
SMALL_ACCOUNT_MAX_POSITIONS     = int(os.getenv("SMALL_ACCOUNT_MAX_POSITIONS", "24"))

# Sniper Mode Controls
# Set to False to allow both long and short (recommended for non-restricted paper trading).
LONG_ONLY_MODE        = False  # False = allow shorts (paper); True = long-only (live restricted accounts)
MIN_SIGNAL_CONFIDENCE = 0.72   # Execute signals with confidence >= this (lowered from 0.78 for bear regime coverage)
MIN_SHORT_CONFIDENCE_BEAR = 0.65  # In bear regime, allow Technical short setups at current confidence scale
SHORT_FAIL_COOLDOWN_MIN = 5    # Re-try failed short symbols immediately
MAX_SIGNALS_PER_CYCLE = 5      # Execute at most this many signals per scan cycle

# Parallel Scanning
SCAN_WORKERS        = 8    # Threads scanning symbols concurrently (kept below Alpaca pool defaults)
SCAN_SYMBOL_TIMEOUT = 15   # Max seconds per symbol before it is skipped
SCAN_MAX_SYMBOLS    = 75   # Max symbols to scan per cycle (increased for better bear regime coverage)
BEAR_SHORT_TARGET_RESERVE = 30  # In bear regime, reserve more scan slots for short universe backups

# ─────────────────────────────────────────────────────────────────
# ADAPTIVE / INTELLIGENT EXECUTION FRAMEWORK
# ─────────────────────────────────────────────────────────────────
USE_MULTI_REGIME = os.getenv("USE_MULTI_REGIME", "true").lower() in ("1", "true", "yes")  # Enable multi-axis regime
USE_STRATEGY_FEEDBACK = os.getenv("USE_STRATEGY_FEEDBACK", "true").lower() in ("1", "true", "yes")  # Adaptive allocation
USE_TIMING_AWARE_ENTRY = os.getenv("USE_TIMING_AWARE_ENTRY", "true").lower() in ("1", "true", "yes")  # Market phase sizing
USE_ADAPTIVE_PDT_RESERVE = os.getenv("USE_ADAPTIVE_PDT_RESERVE", "true").lower() in ("1", "true", "yes")  # Dynamic PDT buffer
USE_REGIME_STOPS = os.getenv("USE_REGIME_STOPS", "true").lower() in ("1", "true", "yes")  # Tighten stops in volatility

# Adaptive Stop Loss Tightening (when volatility spikes)
STOP_LOSS_MULTIPLIER_EXTREME = 0.65  # Tighten stops by 35% in extreme VIX (>=40)
STOP_LOSS_MULTIPLIER_HIGH = 0.85     # Tighten by 15% in high vol (VIX 25-40)
STOP_LOSS_MULTIPLIER_NORMAL = 1.0    # Normal stops
STOP_LOSS_MULTIPLIER_CALM = 1.15     # Widen by 15% in calm markets

# Strategy Performance Tracking
STRATEGY_METRICS_DB = os.path.join(os.path.dirname(__file__), "..", "..", "strategy_metrics.db")
STRATEGY_STATS_LOOKBACK_DAYS = int(os.getenv("STRATEGY_STATS_LOOKBACK_DAYS", "30"))
MIN_TRADES_FOR_FEEDBACK = int(os.getenv("MIN_TRADES_FOR_FEEDBACK", "10"))  # Require 10 trades before using feedback

# Regime-Adaptive Strategy Configuration
STRATEGY_REGIME_CONFIG = {
    # Format: (trend, volatility) -> config
    ("uptrend", "normal"): {
        "active_strategies": ["TrendBreaker", "Momentum", "Sweepea", "FloatRotation", "GapBreakout"],
        "confidence_min": 0.68,
        "rvol_min_override": None,  # Use default
        "max_positions_override": None,
        "position_size_mult": 1.0,
        "description": "Bull+Normal: All strategies active, normal sizing",
    },
    ("uptrend", "high"): {
        "active_strategies": ["TrendBreaker", "Momentum", "Sweepea"],
        "confidence_min": 0.75,
        "rvol_min_override": 1.8,
        "max_positions_override": 8,
        "position_size_mult": 0.85,
        "description": "Bull+HighVol: Tighter filters, reduced sizing",
    },
    ("uptrend", "extreme"): {
        "active_strategies": ["TrendBreaker"],  # Only most robust
        "confidence_min": 0.82,
        "rvol_min_override": 2.5,
        "max_positions_override": 4,
        "position_size_mult": 0.5,
        "description": "Bull+ExtremeVol: Conservative, size down 50%",
    },
    ("range", "normal"): {
        "active_strategies": ["Sweepea", "ORB", "VWAP Reclaim"],
        "confidence_min": 0.70,
        "rvol_min_override": 1.2,
        "max_positions_override": 10,
        "position_size_mult": 1.0,
        "description": "Range+Normal: Range strategies preferred",
    },
    ("range", "high"): {
        "active_strategies": ["Sweepea"],  # Range best in ranges
        "confidence_min": 0.78,
        "rvol_min_override": 1.5,
        "max_positions_override": 6,
        "position_size_mult": 0.75,
        "description": "Range+HighVol: Sweepea + tight filters",
    },
    ("downtrend", "normal"): {
        "active_strategies": ["Momentum", "ORB", "VWAP Reclaim"],
        "confidence_min": 0.80,  # Tighter in bear
        "rvol_min_override": 2.0,
        "max_positions_override": 6,
        "position_size_mult": 0.8,
        "description": "Bear+Normal: Long entries swap-only or closed, short emphasis",
    },
    ("downtrend", "high"): {
        "active_strategies": ["Momentum"],  # Only strong signals
        "confidence_min": 0.85,
        "rvol_min_override": 2.5,
        "max_positions_override": 3,
        "position_size_mult": 0.5,
        "description": "Bear+HighVol: Extreme caution, minimal sizing",
    },
}

# ─────────────────────────────────────────────────────────────────
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
SWEEPEA = {
    "timeframe":        15,
    "pinbar_threshold": 80.0,
    "sweep_bars":       1,
    "min_sweep":        0.10,
    "use_ma":           True,
    "ma_fast":          20,
    "ma_slow":          50,
    "use_bb":           True,
    "bb_period":        20,
    "bb_std":           2.0,
}

TECHNICAL = {
    "rsi_oversold":   30,
    "rsi_overbought": 70,
    "volume_surge":   2.0,   # was 1.5 — stronger volume required
}

MOMENTUM = {
    "min_momentum": 4.0,   # 4%+ move required (was 5 — too tight)
    "volume_surge": 2.5,   # 2.5x volume confirmation (was 3 — too tight)
}

SENTIMENT_STRATEGY = {
    "enabled": True,
    "min_sentiment_score": 0.6,
    "min_sentiment_confidence": 0.55,
    "volume_surge": 2.0,
}

# ─────────────────────────────────────────────────────────────────
# Gap Breakout Strategy
# ─────────────────────────────────────────────────────────────────
GAP_BREAKOUT = {
    "min_gap_pct":       5.0,   # Minimum gap-up % from prior close
    "volume_multiplier": 2.5,   # Recent vol must be > X * session avg (raised from 1.5 — x1.5 was noise-level)
    "entry_window_min":  90,    # Only enter within first 90 min of open
}

# ─────────────────────────────────────────────────────────────────
# Opening Range Breakout (ORB) Strategy
# ─────────────────────────────────────────────────────────────────
ORB = {
    "range_minutes":       15,   # ORB formed in first 15 min (9:30-9:45)
    "entry_start_min":     15,   # Start looking for breakouts after ORB forms
    "entry_end_min":       120,  # Stop entering after 2 hrs into session
    "breakout_buffer_pct": 0.1,  # Require 0.1% above ORB high to confirm
    "volume_surge":        2.0,  # Post-ORB vol must be > 2.0x ORB avg (raised from 1.5)
}

# ─────────────────────────────────────────────────────────────────
# VWAP Reclaim Strategy
# ─────────────────────────────────────────────────────────────────
VWAP_RECLAIM = {
    "volume_surge": 2.0,   # Volume in last 3 bars vs session avg
    "rsi_max":      72,    # Don't enter if already overbought
}

# ─────────────────────────────────────────────────────────────────
# Float Rotation Strategy
# ─────────────────────────────────────────────────────────────────
FLOAT_ROTATION = {
    "max_float_shares":   15_000_000,  # Only stocks with float < 15M shares
    "volume_float_ratio": 0.25,        # Today's volume already > 25% of float
    "min_price_up_pct":   5.0,         # Price must be up >5% on the day
}

# ─────────────────────────────────────────────────────────────────
# Early Momentum / Opening Strategies
# ─────────────────────────────────────────────────────────────────
PRE_MARKET_MOMENTUM = {
    "min_gap_pct":       3.0,   # Gap from prior close must be >= 3%
    "pm_vol_pct_of_avg": 15.0,  # Pre-market volume must be >= 15% of avg daily vol
    "pm_trend_bars":     5,     # Last N pre-market bars must trend up
    "entry_window_end":  10.0,  # Stop firing after 10:00 AM ET (hour decimal)
}

OPENING_BELL_SURGE = {
    "surge_bars":      5,     # Number of first 1-min bars after open to measure
    "vol_multiplier":  4.0,   # First N bars total vol vs baseline (N * avg_1min)
    "min_price_up_pct": 2.0,  # Price must be up >= 2% from open after N bars
    "window_min":      15,    # Only valid for first 15 min after open
}

PM_HIGH_BREAKOUT = {
    "breakout_buffer_pct": 0.2,  # Must clear PM high by 0.2%
    "volume_surge":        1.5,  # Volume in last 3 bars vs session avg
    "entry_window_min":    60,   # Only valid for first 60 min after open
}

EARLY_SQUEEZE = {
    "max_float_shares":  20_000_000,  # Low-float stocks only
    "min_gap_pct":        3.0,         # Gap from prior close >= 3%
    "rvol_multiplier":    4.0,         # Projected full-day RVOL must exceed 4x
    "entry_window_min":  45,           # Only valid for first 45 min after open
    "rsi_max":           75,           # Not yet overbought
}

# ─────────────────────────────────────────────────────────────────
# Bear Breakdown Strategy (short-selling)
# Fires only in bear regime (SPY < 200SMA). Inverse of TrendBreaker.
# ─────────────────────────────────────────────────────────────────
BEAR_BREAKDOWN = {
    "volume_multiplier":  1.5,   # Volume today vs 20-day avg (raised from 1.2 — filters x1.3/x1.4 noise)
    "rsi_max":           65,    # Allow earlier distribution entries before full trend extension
    "rsi_min":           30,    # Raised from 20 — avoid shorting deeply oversold stocks (bounce risk)
    "above_sma_min_days": 1,    # Loosen freshness requirement in fast bear tapes
    "breakdown_buffer_pct": 0.30,  # Allow entry if within 0.30% above 10-day low
}

# ─────────────────────────────────────────────────────────────────
# Golden Ratio Scanner Guardrails
# ─────────────────────────────────────────────────────────────────
RVOL_MIN                 = 2.0         # Require relative volume ≥ 2x before entering
MIN_STOCK_PRICE          = 3.0         # Skip penny stocks below $3 (poor fill quality, high spread)
MIN_DOLLAR_VOLUME        = 20_000_000  # Skip illiquid setups: price × day_vol < $20M
MAX_GAP_CHASE_PCT        = 15.0       # Skip if already up >15% without consolidation
GAP_CHASE_CONSOL_BARS    = 5          # Number of 1-min bars to check for tight base
USE_MARKET_REGIME_FILTER = True       # SPY below 200-day MA → cut signals to 1
MARKET_REGIME_SIGNALS_CAP  = 5        # Max LONG entries per cycle in bear regime (swap-only); tries until one succeeds
BEAR_SHORT_SIGNALS_CAP     = 3        # Max SHORT entries per cycle in bear regime
ATR_STOP_MULTIPLIER      = 1.5        # Stop loss = entry − ATR × 1.5
ATR_TP_RATIO             = 2.0        # Take-profit at 2:1 R:R (risk × 2)
MAX_SHORT_FLOAT_PCT      = 20.0       # Never exceed this % of equity per squeeze ticker

# Bear short scan supplement — liquid large/mid caps with clean SMA structure that
# BearBreakdownStrategy and TechnicalStrategy can fire on during a bear regime.
# These stocks have stable 20/50 SMA patterns and meaningful distribution moves.
BEAR_SHORT_UNIVERSE = [
    "NVDA", "AMD", "TSLA", "META", "AMZN", "AAPL", "MSFT", "NFLX",
    "PLTR", "MSTR", "COIN", "SMCI", "SNOW", "CRM", "CRWD", "NET",
    "ARKK", "SOXS", "LABD",   # sector ETFs (can be shorted directly)
    "MARA", "WULF", "CLSK",   # crypto miners — high-beta bear breakdowns
    "IONQ", "RGTI", "QUBT",   # quantum/AI overhyped names
]
HIGH_SHORT_FLOAT_STOCKS  = {
    "AAP", "ABTS", "ACHC", "ACXP", "ADMA", "AESI",
    "AEVA", "AGQ", "AGX", "AI", "AIFF", "AIRS",
    "AISP", "ALBT", "ALMU", "AMC", "AMPG", "ANAB",
    "ANNA", "ANNX", "ANTX", "APGE", "APLD", "APP",
    "APPX", "ARCT", "ARMG", "ARTL", "ARWR", "ASAN",
    "ASPI", "ASST", "ASTI", "ASTS", "ATAI", "ATPC",
    "AVBP", "AVTX", "AVXL", "AXTI", "AZ", "AZN",
    "BABX", "BAIG", "BAK", "BATL", "BBNX", "BBW",
    "BCRX", "BEAM", "BETR", "BF", "BFLY", "BHVN",
    "BIAF", "BIRD", "BITU", "BKD", "BKKT", "BKSY",
    "BLSH", "BMEA", "BMNZ", "BNAI", "BNRG", "BOIL",
    "BOXL", "BTBD", "BTBT", "BTDR", "BTGO", "BTU",
    "BUR", "BWET", "BZUN", "CABA", "CAR", "CBIO",
    "CBUS", "CDIO", "CELC", "CGEM", "CGON", "CHAC",
    "CHPT", "CHRS", "CIFG", "CIFR", "CISS", "CNVS",
    "CNXC", "COIG", "CONI", "CONL", "CORZ", "CPB",
    "CRCA", "CRCG", "CRDF", "CRK", "CRSR", "CRVS",
    "CRWD", "CRWG", "CRWL", "CRWV", "CSIQ", "CTXR",
    "CV", "CVI", "CVV", "CYN", "DAMD", "DBGI",
    "DBI", "DBVT", "DERM", "DIN", "DNA",
    "DNTH", "DNUT", "DOCN", "DRVN", "DTCX", "DUOG",
    "DUOL", "DUST", "DVLT", "DWSN", "DXST", "DXYZ",
    "EAF", "EBS", "EDSA", "EEIQ", "ELVN", "ENLT",
    "EOSE", "ERAS", "ETHD", "ETHT", "ETR", "EUDA",
    "EVH", "EVMN", "EVTV", "EWTX", "EYE", "FATN",
    "FBGL", "FBIO", "FBYD", "FCHL", "FEED", "FFAI",
    "FGL", "FLNC", "FOSL", "FOUR", "FROG", "GDXD",
    "GDXU", "GEF", "GLND", "GLSI", "GLUE", "GLWG",
    "GNPX", "GOGO", "GPRE", "GRND", "GRPN", "HCTI",
    "HNRG", "HOOG", "HOOZ", "HPK", "HRTX", "HTCO",
    "HTZ", "HUBC", "HUMA", "HUT", "HYPD", "IBG",
    "IBRX", "IBTA", "ICU", "IDYA", "IEP", "IMAX",
    "IMTE", "INDI", "INDO", "IONZ", "IRE", "IREG",
    "ISSC", "IXHL", "JACK", "JBLU", "JDZG", "JNUG",
    "KALV", "KIDZ", "KLRS", "KOD", "KOLD", "KOPN",
    "KORU", "KPTI", "KRRO", "KRUS", "KSCP", "KULR",
    "KVYO", "LAR", "LASE", "LBGJ", "LCID", "LE",
    "LENZ", "LEU", "LGN", "LGVN", "LICN", "LMND",
    "LMRI", "LOVE", "LUD", "LUNR", "LVWR", "MARA",
    "MDCX", "MDGL", "MED", "MEOH", "METC", "METU",
    "MGTX", "MKDW", "MKT", "MLKN", "MNPR", "MNTS",
    "MRAL", "MRLN", "MRNO", "MSS", "MSTX", "MULL",
    "MUU", "MUX", "MVIS", "MVO", "NAMM", "NAUT",
    "NAVN", "NBIG", "NBIL", "NBIS", "NCI", "NDRA",
    "NEXT", "NFE", "NGNE", "NMAX", "NOAH", "NOTE",
    "NSRX", "NTLA", "NUGT", "NVTS", "OGEN", "OKLL",
    "OKLO", "OKLS", "OKTA", "OKUR", "OLPX", "ONCO",
    "ONDG", "ONDS", "ONEG", "OPTX", "ORGN", "ORGO",
    "ORIC", "ORIS", "OXM", "PALI", "PANW", "PAR",
    "PCRX", "PGEN", "PGY", "PHAT", "PHGE", "PL",
    "PLCE", "PLTZ", "POLA", "PONY", "PRME", "PROF",
    "PROP", "PSIX", "QBTZ", "QLYS", "QNCX", "QNRX",
    "QNTM", "QTTB", "QVCGA", "RBNE", "RCAT", "RCAX",
    "RCKT", "RDTL", "REED", "RENX", "REPL", "RETO",
    "RGTZ", "RILY", "RIME", "RIOX", "RKLX", "RKLZ",
    "RLYB", "RNAC", "ROMA", "RR", "RUM", "RVI",
    "RXT", "RZLT", "SAIL", "SATL", "SATS", "SBIT",
    "SCVL", "SER", "SGML", "SHMD", "SHNY", "SIGA",
    "SION", "SKIL", "SKIN", "SKLZ", "SLNH", "SLON",
    "SLS", "SMCX", "SMCZ", "SMST", "SMX", "SNBR",
    "SND", "SNSE", "SOC", "SOLT", "SOWG", "SOXS",
    "SPCE", "SPIR", "SPRC", "SPRY", "SQM", "SRFM",
    "SRPT", "STIM", "SUNE", "SWMR", "TASK", "TBCH",
    "TDUP", "TEAD", "TECX", "TENB", "TERN", "TMDE",
    "TNGX", "TONX", "TPET", "TRIP", "TRON", "TROX",
    "TSSI", "TTEC", "TURB", "TWST", "UAMY", "UGRO",
    "UNG", "UPB", "UPXI", "UUUG", "UWMC", "VCIC",
    "VCX", "VERI", "VIVO", "VNET", "VOR", "VRCA",
    "VSA", "VSTM", "VTAK", "VTIX", "VTS", "VWAV",
    "WATT", "WKHS", "WOLF", "WRAP", "WS", "WT",
    "WTI", "WULF", "WVE", "WYFI", "XRX", "XTIA",
    "XYF", "YANG", "YDDL", "YINN", "ZBIO", "ZNTL",
    "ZS", "ZSL",
}

# Remove any DELISTED_STOCKS entries that crept into HIGH_SHORT_FLOAT_STOCKS
HIGH_SHORT_FLOAT_STOCKS = {s for s in HIGH_SHORT_FLOAT_STOCKS if s not in DELISTED_STOCKS}

# Live HSF lookup — merges the static set above with tier-2 universe.json entries
# so newly TI-scraped tickers are recognised as HSF without restarting the bot.
_hsf_tier2_cache: dict = {"ts": 0.0, "symbols": frozenset()}
_HSF_CACHE_TTL = 300  # 5 minutes — re-read universe.json at most every 5 min

def is_high_short_float(symbol: str) -> bool:
    """Return True if symbol is in the static HSF set OR in the live tier-2 universe."""
    if symbol in HIGH_SHORT_FLOAT_STOCKS:
        return True
    import time as _time
    now = _time.monotonic()
    if now - _hsf_tier2_cache["ts"] > _HSF_CACHE_TTL:
        try:
            from engine.equity.universe import get_tier as _gt
            _hsf_tier2_cache["symbols"] = frozenset(_gt(2))
        except Exception:
            _hsf_tier2_cache["symbols"] = frozenset()
        _hsf_tier2_cache["ts"] = now
    return symbol in _hsf_tier2_cache["symbols"]

# OOM and cache management
OPTIONS_CHAIN_CACHE_MAX = int(os.getenv("OPTIONS_CHAIN_CACHE_MAX", "300"))  # max symbols in options chain cache

# Global memory warning threshold (in MB)
MEMORY_WARN_MB = int(os.getenv("MEMORY_WARN_MB", "1500"))

"""
engine.utils
------------
Public facade — re-exports every symbol that callers already import from
engine.utils directly so no call-site changes are needed during Stage 2.

Submodules:
  utils.bars   — bar fetching, cache, RSI/MACD/ATR
  utils.market — market hours, VIX, sentiment, adaptive intervals
  utils.risk   — ATR tier assignment (cached), position sizing
  utils.data   — Finnhub discovery, sentiment gate, logging, env helpers
"""

from engine.utils.bars import (
    ALPACA_AVAILABLE,
    clear_bar_cache,
    is_dead_ticker,
    get_data_client,
    get_option_data_client,
    get_bars,
    get_bars_batch,
    get_price,
    get_premarket_bars,
    get_finnhub_bars,
    calc_rsi,
    calc_macd,
    calculate_atr,
)

from engine.utils.market import (
    is_market_open,
    is_regular_hours,
    is_options_lull_hours,
    is_open_window,
    get_vix,
    check_vix_roc_filter,
    get_vix_interval,
    get_market_hours_interval,
    get_position_tuning_interval,
    get_market_sentiment,
    get_live_holdings,
)

from engine.utils.risk import (
    get_dynamic_tier,
    calculate_risk_adjusted_size,
)

from engine.utils.data import (
    bool_env,
    get_env,
    format_currency,
    setup_logging,
    get_finnhub_client,
    get_trending_tickers,
    filter_trending_momentum,
    get_finnhub_trending_tickers,
    check_sentiment_gate,
)

__all__ = [
    "ALPACA_AVAILABLE",
    "clear_bar_cache", "is_dead_ticker",
    "get_data_client", "get_option_data_client",
    "get_bars", "get_bars_batch", "get_price", "get_premarket_bars", "get_finnhub_bars",
    "calc_rsi", "calc_macd", "calculate_atr",
    "is_market_open", "is_regular_hours", "is_options_lull_hours", "is_open_window",
    "get_vix", "check_vix_roc_filter",
    "get_vix_interval", "get_market_hours_interval", "get_position_tuning_interval",
    "get_market_sentiment", "get_live_holdings",
    "get_dynamic_tier", "calculate_risk_adjusted_size",
    "bool_env", "get_env", "format_currency",
    "setup_logging", "get_finnhub_client",
    "get_trending_tickers", "filter_trending_momentum",
    "get_finnhub_trending_tickers", "check_sentiment_gate",
]

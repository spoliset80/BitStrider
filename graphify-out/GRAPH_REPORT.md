# Graph Report - .  (2026-08-04)

## Corpus Check
- 89 files · ~87,179 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 967 nodes · 2052 edges · 69 communities (55 shown, 14 thin omitted)
- Extraction: 95% EXTRACTED · 5% INFERRED · 0% AMBIGUOUS · INFERRED: 95 edges (avg confidence: 0.73)
- Token cost: 209,050 input · 0 output

## Community Hubs (Navigation)
- Scan Universe Assembly
- Options Universe Resolution
- Broker Abstraction Layer
- Options Order Pricing/Execution
- Equity Breakout Strategies
- Bear Options Spreads
- Bar Data Fetching
- Regime-Adaptive Position Sizing
- Multi-Source Backtest Data
- Options Chain & OI Data
- Broker Client Factory
- Watchdog Venv Bootstrap
- EOD Notifications/Reporting
- AutoBot Process Watchdog
- Shared Utility Helpers
- Momentum/VWAP Entry Strategies
- Static Priority Ticker Lists
- Trade Ideas Discovery Manager
- Pre-Open Intelligence Providers
- Executor Health & Stop Management
- Pre-Open Watchlist Builder
- TI Ticker Validation & Never-Trade
- No-Gain Position Exit Logic
- Dynamic Universe TTL Store
- Scan Guardrail Pipeline
- Trailing Stop & TP Enforcement
- Technical Indicator Primitives
- Short-Squeeze Fundamentals
- TI Scraper Browser Automation
- EDGAR 8-K Ticker Extraction
- TI Primary/Unusual-Options Files
- EOD Position Closing
- Position Close Execution
- Float Rotation & Squeeze Detection
- Predictions & Day-Picks Output
- Correlation & Confidence Gating
- TI Primary File Pruning
- Sector Sympathy Scanner
- Project Config Docs & CLI
- Bear-Market Kill Mode
- Options Data Cross-Check Script
- Discovery Injection Sources
- Position Swap-on-Full Logic
- Bracket Order Construction
- Edge WebDriver Session Mgmt
- PowerShell Bot Monitor Script
- Live TI Squeeze Screener
- Technical Strategy Indicators
- Options Position Tracking
- Modularization Refactor Plan
- Stale Universe Snapshot Files
- Quarterly State Files
- Squeeze Screener Scripts
- Equity Package Public API
- Inverse ETF Basket
- Options Adaptive Limit Retry
- Options Contract Sizing
- Options Budget Calculation
- Options Spread Conversion
- Options Position Reconciliation
- Options Package Public API
- Gap-Run Watchlist File
- Scripts Package Marker

## God Nodes (most connected - your core abstractions)
1. `get_bars` - 65 edges
2. `EnhancedExecutor` - 56 edges
3. `MarketState` - 45 edges
4. `engine.utils facade` - 43 edges
5. `Signal` - 34 edges
6. `PreopenIntelligenceScanner` - 27 edges
7. `OptionsExecutor` - 27 edges
8. `ETradeClient` - 26 edges
9. `_get_options_chain` - 25 edges
10. `_fetch_bar_context` - 25 edges

## Surprising Connections (you probably didn't know these)
- `Heartbeat Liveness Timestamp` --semantically_similar_to--> `ApexTraderAutoRun scheduled task installer`  [INFERRED] [semantically similar]
  heartbeat.txt → windows_schedule_apextrader.ps1
- `day_picks.json (daily prediction output)` --shares_data_with--> `is_bull_regime`  [INFERRED]
  predictions/day_picks.json → engine/utils/market.py
- `ApexTraderAutoRun scheduled task installer` --references--> `main()`  [AMBIGUOUS]
  windows_schedule_apextrader.ps1 → main.py
- `data/never_trade.txt (permanent ticker exclusion list)` --conceptually_related_to--> `EnhancedExecutor`  [INFERRED]
  data/never_trade.txt → engine/execution/__init__.py
- `_place_trailing_stops()` --calls--> `TrailingStopOrderRequest`  [INFERRED]
  scripts/predict_tomorrow.py → engine/broker/etrade_client.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Broker Abstraction Layer (Alpaca/E*TRADE interface parity)** — engine_broker_broker_factory, engine_broker_etrade_client, execution_enhancedexecutor [INFERRED 0.85]
- **Kill-Mode Emergency Liquidation** — engine_risk_kill_mode_check, engine_execution_enhanced_enhancedexecutor, engine_options_executor_optionsexecutor [EXTRACTED 1.00]
- **Fail-open trading gate design pattern** — engine_utils_data_check_sentiment_gate, engine_utils_earnings_no_earnings_soon, engine_utils_market_check_vix_roc_filter, engine_utils_market_is_bull_regime [INFERRED 0.85]
- **ShortSqueeze gate-check duplication across standalone scripts** — scripts__squeeze_screen, scripts__squeeze_screen_ti, scripts__squeeze_scan_ti, scripts__squeeze_deep, scripts__debug_squeeze, scripts__squeeze_timing [INFERRED 0.80]
- **Cross-checking market data across Alpaca / yfinance / Finnhub** — scripts_backtest_finnhub, scripts_check_alpaca_data, scripts_check_options_data [INFERRED 0.80]
- **Options backtesting via shared Black-Scholes simulation and OPTIONS_* config** — scripts_backtest_options, scripts_backtest_open_window, scripts_backtest_ti_primary, config [INFERRED 0.80]
- **Trade Ideas Data Capture and Universe Persistence Pipeline** — engine_ti_capture_tradeideas_scrape_tradeideas, data_ti_primary_tickers, data_ti_unusual_options_tickers, data_universe_tickers, engine_equity_universe_add_tickers [INFERRED 0.85]
- **TI Capture Task Scheduler Chain** — windows_schedule_ti_capture_task, scripts_run_ti_capture_task_launcher, engine_ti_capture_tradeideas_scrape_tradeideas [EXTRACTED 1.00]
- **Options Eligible Universe Multi-Source Resolution** — engine_config_get_options_universe, engine_config_load_options_universe, engine_equity_universe_get_ti_primary, engine_equity_universe_get_tier, data_ti_unusual_options_tickers [INFERRED 0.85]

## Communities (69 total, 14 thin omitted)

### Community 0 - "Scan Universe Assembly"
Cohesion: 0.05
Nodes (65): BEAR_SHORT_UNIVERSE, get_scan_targets(), filter_universe_by_positions(), Filter out symbols already held or with unfilled buy orders from the scan…, OptionsExecutor.monitor_positions, AppContext, _build_context(), _build_scan_targets() (+57 more)

### Community 1 - "Options Universe Resolution"
Cohesion: 0.06
Nodes (53): get_options_universe(), OPTIONS_ELIGIBLE_UNIVERSE, _OPTIONS_FALLBACK_UNIVERSE, Return the live options universe, applying override rules. Core liquid names…, _calc_iv_rank, Series, Options IV rank helper utilities for ApexTrader., Calculate IV rank as current IV versus trailing historical volatility range.… (+45 more)

### Community 2 - "Broker Abstraction Layer"
Cohesion: 0.06
Nodes (32): data/never_trade.txt (permanent ticker exclusion list), BrokerFactory, BrokerFactory.create_stock_client, BrokerFactory.get_broker_type, ApexTrader - Broker Factory Selects the appropriate broker client. Supports…, ETradeClient (Alpaca-compatible E*TRADE adapter), _Account, _Asset (+24 more)

### Community 3 - "Options Order Pricing/Execution"
Cohesion: 0.07
Nodes (23): LimitOrderRequest, _alpaca_option_symbol, _bs_option_price, OptionsExecutor, date, TradingClient, Helper to determine if the account is subject to PDT restrictions. Returns:…, Check open options (Single & MLEG) and close at target/stop. Handles Net MtM… (+15 more)

### Community 4 - "Equity Breakout Strategies"
Cohesion: 0.07
Nodes (28): BearBreakdownStrategy, _calc_atr14(), GapBreakoutStrategy, get_strategy_instances, LiquiditySweepStrategy, MomentumStrategy, OpeningBellSurgeStrategy, ORBStrategy (+20 more)

### Community 5 - "Bear Options Spreads"
Cohesion: 0.16
Nodes (23): BearCallSpreadStrategy, BearPutStrategy, _calc_rr, CoveredCallStrategy, _fetch_bar_context, get_dynamic_option_filters, _get_filters, _get_options_chain (+15 more)

### Community 6 - "Bar Data Fetching"
Cohesion: 0.12
Nodes (30): _calc_atr14(), calculate_atr, _get_bars_alpaca, get_bars_batch, _get_bars_yfinance, get_data_client, get_finnhub_bars, get_premarket_bars (+22 more)

### Community 7 - "Regime-Adaptive Position Sizing"
Cohesion: 0.11
Nodes (21): get_adaptive_equity_allocation(), Returns adaptive position size percentage for equities based on pre-…, Store the active market snapshot for per-cycle execution decisions., is_bull_regime as single canonical regime source, get_allocation_split, get_market_sentiment, get_vix, get_vix_interval (+13 more)

### Community 8 - "Multi-Source Backtest Data"
Cohesion: 0.15
Nodes (27): alpaca_client(), compare_series(), fetch_alpaca_bars(), fetch_finnhub_bars(), fetch_yfinance_bars(), format_return(), main(), normalize_times() (+19 more)

### Community 9 - "Options Chain & OI Data"
Cohesion: 0.11
Nodes (25): _apply_oi_to_df(), _check_memory(), _fetch_oi_from_contracts(), _get_chain_alpaca, _get_options_universe(), _is_bullish_reversal(), OptionsChainInfo, _parse_occ_symbol() (+17 more)

### Community 10 - "Broker Client Factory"
Cohesion: 0.09
Nodes (22): BrokerFactory, Factory for creating broker clients., Create a stock trading client. Args: broker: 'alpaca' or 'etrade', Create an options trading client. Currently only Alpaca supports options., Determine broker type from client instance., get_priority_scan_queue(), Return the current sympathy/EDGAR/screener tickers (read-only peek). Does NOT…, _BarCtx (+14 more)

### Community 11 - "Watchdog Venv Bootstrap"
Cohesion: 0.09
Nodes (23): _drain_subprocess_output, _ensure_virtualenv, _heartbeat_age_seconds, _heartbeat_monitor, _parse_env_file, _python_is_healthy, _repair_pyvenv_home, _run_command (+15 more)

### Community 12 - "EOD Notifications/Reporting"
Cohesion: 0.14
Nodes (20): notifications package API, _bool_env(), build_eod_report, _build_html_section(), build_top5_report, _format_currency(), _format_signal_text(), _get_env() (+12 more)

### Community 13 - "AutoBot Process Watchdog"
Cohesion: 0.16
Nodes (7): AutoBotWatchdog, Logger, Path, Seconds since engine/orchestrator.py last wrote HEARTBEAT_FILE, or None if it…, Runs for the life of the watchdog, independent of any single main.py subprocess…, Popen, Thread

### Community 14 - "Shared Utility Helpers"
Cohesion: 0.10
Nodes (23): bool_env, filter_trending_momentum, format_currency, get_env, Logger, engine.utils.data ----------------- External data integrations: Finnhub…, Filter a list of ticker dicts to those with >= min_momentum_pct 5-day move., Parse a boolean environment variable. Truthy: '1', 'true', 'yes'. (+15 more)

### Community 15 - "Momentum/VWAP Entry Strategies"
Cohesion: 0.14
Nodes (20): Price reclaims VWAP from below with accelerating volume — second-leg setup.…, Detects upside trend breaks on high short float stocks. Pattern (short-squeeze…, TrendBreakerStrategy, TrendBreakerStrategy.scan, VWAPReclaimStrategy, calc_rsi, get_bars, get_price (+12 more)

### Community 16 - "Static Priority Ticker Lists"
Cohesion: 0.11
Nodes (15): get_dynamic_universe(), is_high_short_float(), PRIORITY_1_MOMENTUM, PRIORITY_2_ESTABLISHED, PRIORITY_3_MARKET, ApexTrader - Configuration Professional Automated Trading System Modular…, Return (p1, p2, p3) merged lists, re-reading universe.json on every call., Return True if symbol is in the static HSF set OR in the live tier-2 universe. (+7 more)

### Community 17 - "Trade Ideas Discovery Manager"
Cohesion: 0.19
Nodes (19): _apply_tradeideas_results(), get_discovered_trending(), ApexTrader — Discovery Manages live trending-stock scans and Trade Ideas…, Submit or check a background TI toplist scrape., Return tickers found by trending scans this session (read-only copy)., Merge TI scrape results into *priority_1* / *priority_2* lists in-place., Submit or check a background TI scrape for core Trade Ideas pages., Submit or check a background Trade Ideas unusual options scrape. (+11 more)

### Community 18 - "Pre-Open Intelligence Providers"
Cohesion: 0.15
Nodes (10): PreopenIntelligenceScanner._add_candidate(), PreopenSignalProvider, Refresh ``trending_stocks`` from live feeds (Finnhub, etc.). New tickers are…, scan_trending_stocks(), check_sentiment_gate, get_finnhub_trending_tickers, get_trending_tickers, Parse Finnhub general news for mentioned ticker symbols. (+2 more)

### Community 19 - "Executor Health & Stop Management"
Cohesion: 0.12
Nodes (11): EnhancedExecutor.check_software_stops(), PDTTracker, date, TradingClient, Close any position whose broker-rejected PDT stop has been breached. Called…, Return the date a position was opened. Checks the in-memory entry log first,…, Close swing-strategy positions (i.e. any long NOT opened by a strategy in…, On startup, reconstruct today's entry log from Alpaca filled buy orders.… (+3 more)

### Community 20 - "Pre-Open Watchlist Builder"
Cohesion: 0.16
Nodes (5): get_preopen_watchlist(), PreopenIntelligenceScanner, Build a scored pre-open watchlist and inject high-priority tickers. This is…, Return the current pre-open intelligence watchlist., scan_preopen_intelligence()

### Community 21 - "TI Ticker Validation & Never-Trade"
Cohesion: 0.16
Nodes (17): is_never_trade(), OptionsExecutor.place_option_order, _is_valid_ti_ticker(), _patch_config(), _patch_high_short_float(), Path, Trade Ideas — Screenshot + Universe Updater…, Return False for obvious scraper garbage: too short, too long, non-alpha, or… (+9 more)

### Community 22 - "No-Gain Position Exit Logic"
Cohesion: 0.14
Nodes (13): datetime, ApexTrader - Enhanced Executor Optimized trade executor with consolidated…, Return the UTC fill timestamp a position was opened — hour-precision…, Close any long position that has shown zero positive unrealized gain within…, # NOTE: closing an existing position is NOT a new day trade., engine.execution package public API, calculate_risk_adjusted_size, Local effective_* vars instead of mutating imported config (+5 more)

### Community 23 - "Dynamic Universe TTL Store"
Cohesion: 0.25
Nodes (14): Dynamic Universe Tickers (universe.json), add_tickers(), get_latest_batch(), get_tier(), _is_expired(), _load_raw(), prune(), engine/universe.py — Dynamic universe manager… (+6 more)

### Community 24 - "Scan Guardrail Pipeline"
Cohesion: 0.18
Nodes (12): _passes_guardrails(), _prefetch_snapshots(), Batch-fetch stock snapshots for *symbols* and store in _snapshot_cache. A…, Pre-scan gates: dollar-volume, RVOL, and gap-chase guard. Returns False to skip…, scan_universe(), _get_market_cap, Cached market cap sourced from yfinance. Returns None when unavailable. Same…, clear_bar_cache (+4 more)

### Community 25 - "Trailing Stop & TP Enforcement"
Cohesion: 0.15
Nodes (9): TrailingStopOrderRequest, EnhancedExecutor.protect_positions(), Submit a position-closing order. During regular hours this is a plain market…, Actively watch every open position's loss while the market is NOT in regular…, Trim any position whose market value exceeds MAX_POSITION_CONCENTRATION_PCT of…, Kill mode emergency exit. Closes every open position as safely as possible. PDT…, Scan open positions against stored ATR-based TP targets. Submits a market close…, For every open position whose shares are fully free (qty_available > 0 AND no… (+1 more)

### Community 26 - "Technical Indicator Primitives"
Cohesion: 0.14
Nodes (14): _at_ema20_pullback(), _calc_hv30(), _calc_rsi_scalar(), _ema50_above(), _lower_bollinger_touch(), Series, True if the last close is at or almost exactly at the lower Bollinger Band., True if the last close is above the 50-day EMA. (+6 more)

### Community 27 - "Short-Squeeze Fundamentals"
Cohesion: 0.16
Nodes (9): _fetch_squeeze_fundamentals(), _fetch_squeeze_rs(), Fetch short float %, gross margins, and revenue growth via yfinance (daily-…, Fetch 13-week price return relative to S&P 500 via Finnhub (daily-cached)., Directional call / bull-call-spread on high short-float stocks with confirmed…, ShortSqueezeStrategy, Gate-by-gate debug for ShortSqueezeStrategy on a single symbol., Deep-dive ShortSqueeze gate check for a single symbol. Usage: python -m… (+1 more)

### Community 28 - "TI Scraper Browser Automation"
Cohesion: 0.18
Nodes (13): _extract_race_sides(), _extract_tickers(), _get_driver(), _is_driver_alive(), main(), Return True if the Edge WebDriver session is still responsive., Return the persistent Edge driver. 1. If already alive in-process → reuse it.…, Extract ticker symbols from the loaded Trade Ideas heatmap page. Primary:… (+5 more)

### Community 29 - "EDGAR 8-K Ticker Extraction"
Cohesion: 0.22
Nodes (11): _load_cik_map (engine.data.edgar_scraper), _ticker_from_cik (engine.data.edgar_scraper), get_edgar_triggered_tickers (EDGAR 8-K scraper), _load_cik_map(), EDGAR 8-K RSS Scraper ===================== Polls the SEC's free public 8-K…, Fetch the latest EDGAR 8-K ATOM feed and return tickers for companies that…, Fetch SEC company_tickers.json and build CIK → ticker lookup (once per session)., Resolve a raw CIK string (any length) to a ticker via the CIK map. (+3 more)

### Community 30 - "TI Primary/Unusual-Options Files"
Cohesion: 0.18
Nodes (12): TI Primary Tickers (ti_primary.json), TI Unusual Options Tickers (ti_unusual_options.json), _load_options_universe(), Load live TI unusual-options-volume tickers. Returns the scraped unusual…, get_ti_primary(), _is_ti_primary_stale(), Return True if the ti_primary.json data is too old to be used., Return the latest TI capture tickers from ti_primary.json, or [] if stale. (+4 more)

### Community 31 - "EOD Position Closing"
Cohesion: 0.21
Nodes (9): MarketOrderRequest, EOD_CLOSE_STRATEGIES, EnhancedExecutor, EnhancedExecutor._create_simple_order(), Symbols currently blocked from re-entry after an after-hours stop-loss exit., Optimized trade executor with consolidated long/short logic., Close all intraday-strategy positions at EOD_CLOSE_TIME. Targets FloatRotation,…, Find open orders older than STALE_ORDER_MINUTES and re-submit them: - Regular… (+1 more)

### Community 32 - "Position Close Execution"
Cohesion: 0.47
Nodes (5): Signal, EnhancedExecutor.execute(), EnhancedExecutor._validate_trade(), PositionInfo, Cached snapshot of open positions.

### Community 33 - "Float Rotation & Squeeze Detection"
Cohesion: 0.24
Nodes (6): EarlySqueezeDetector, FloatRotationStrategy, _get_float_shares, Cached float share count sourced from yfinance. Returns None when float data is…, Low-float stock with volume > X% of float = stock is 'in play'. Logic: - Fetch…, Fires 9:30–10:15 AM ET for low-float stocks showing gap + projected RVOL >4× +…

### Community 34 - "Predictions & Day-Picks Output"
Cohesion: 0.31
Nodes (9): Return a summary dict for logging/display., stats(), main(), _place_trailing_stops(), DataFrame, 1. Write top_n tickers to predictions/watchlist.json (for reference). 2. Add…, Connect to Alpaca (live or paper, per TRADE_MODE env var) and place a GTC…, _save_and_inject() (+1 more)

### Community 35 - "Correlation & Confidence Gating"
Cohesion: 0.24
Nodes (7): AccountSnapshot, EnhancedExecutor._execute_entry(), EnhancedExecutor._size_with_buying_power(), Cached Alpaca account state — equity, buying power, live PDT count., Trim a correlated basket (e.g. leveraged inverse-market ETFs) whose COMBINED…, Return (symbol, entry_confidence) of the held long position with the lowest…, Returns (shares, skip_reason). Downsizes if BP constrained, skips if below min.

### Community 36 - "TI Primary File Pruning"
Cohesion: 0.28
Nodes (8): _load_ti_primary_raw(), prune_ti_primary(), Read the TI primary file from disk and return raw JSON data., Remove stale TI primary tickers when the source file is too old., requirements.txt — Python dependency manifest, _check_memory(), main(), scripts/prune_universe.py — Universe maintenance tool…

### Community 37 - "Sector Sympathy Scanner"
Cohesion: 0.32
Nodes (7): CORRELATION_GROUPS, _fetch_quotes(), get_active_sympathies (sector sympathy scanner), Sector Sympathy Scanner ======================= Monitors "leader" stocks for…, Check all leaders and return a deduplicated list of sympathy tickers for any…, Fetch Finnhub quotes for *symbols*; returns {sym: {"c", "pc", "dp"}}., get_finnhub_client

### Community 38 - "Project Config Docs & CLI"
Cohesion: 0.29
Nodes (3): CONFIG.md — ApexTrader Configuration Reference, README.md — ApexTrader Overview, CLI utility to print and validate ApexTrader config. Run with: python…

### Community 39 - "Bear-Market Kill Mode"
Cohesion: 0.38
Nodes (6): risk package API, kill_mode.check, kill_mode.is_active, ApexTrader — Kill Mode Extreme bear-market circuit breaker. Extracted from…, Return True when kill mode is engaged for today., Check extreme bear conditions and trigger emergency close if needed. Returns…

### Community 40 - "Options Data Cross-Check Script"
Cohesion: 0.38
Nodes (6): check_alpaca_options(), check_yfinance(), main(), check_options_data.py -------------------- Cross-checks options data…, Check which symbols have optionable contracts on Alpaca., Check yfinance options availability for a symbol.

### Community 41 - "Discovery Injection Sources"
Cohesion: 0.33
Nodes (6): DELISTED_STOCKS, HIGH_SHORT_FLOAT_STOCKS, Sector sympathy + EDGAR 8-K scanner. Sympathy: checks leader stocks (via…, Fetch Alpaca Most Actives + Market Movers and inject qualifying symbols into…, scan_alpaca_movers(), scan_sympathy_and_edgar()

### Community 42 - "Position Swap-on-Full Logic"
Cohesion: 0.33
Nodes (4): EnhancedExecutor._attempt_swap(), Return the symbol of the open long position with the worst unrealized P&L %.…, Return the symbol of the oldest closable long position held >= min_hours…, Try to close the stalest (24h+, falling back to weakest P&L) position to make…

### Community 43 - "Bracket Order Construction"
Cohesion: 0.40
Nodes (4): EnhancedExecutor._create_bracket_order(), OrderType, Submit market entry then a GTC trailing stop at risk_info['stop_loss_pct']%. TP…, Enum

### Community 44 - "Edge WebDriver Session Mgmt"
Cohesion: 0.33
Nodes (6): _create_edge_driver(), _find_existing_edgedriver(), Locate msedgedriver.exe — checks repo .drivers/ first, then ~/.wdm cache., Try to attach to an already-running Edge instance that was started with…, Spawn a new visible Edge window and return the driver (never headless)., _try_attach_edge()

### Community 45 - "PowerShell Bot Monitor Script"
Cohesion: 0.67
Nodes (5): Get-ActiveKeyPrefix(), Get-ActiveMode(), Get-WatchdogAlive(), main polling loop, Start-BotWatchdog()

### Community 47 - "Technical Strategy Indicators"
Cohesion: 0.40
Nodes (4): Multi-indicator technical analysis (RSI, MACD, MA, Volume)., TechnicalStrategy, calc_macd, Series

### Community 48 - "Options Position Tracking"
Cohesion: 0.40
Nodes (4): OptionsPosition, Tracked open options position., Call this from OptionsExecutor after a stop/loss close on an option position.…, record_stop_cooldown

### Community 49 - "Modularization Refactor Plan"
Cohesion: 0.50
Nodes (4): MODULARIZATION_PLAN.md — ApexTrader Refactor Plan, Centralize shared utilities in engine/utils/ (Stage 3), Flatten/remove unnecessary wrapper modules (Stage 2), Remove all `import *` usage (Stage 1)

## Ambiguous Edges - Review These
- `PreopenIntelligenceScanner._add_candidate()` → `_is_valid_ti_ticker()`  [AMBIGUOUS]
  engine/equity/discovery.py · relation: references
- `main()` → `ApexTraderAutoRun scheduled task installer`  [AMBIGUOUS]
  windows_schedule_apextrader.ps1 · relation: references

## Knowledge Gaps
- **41 isolated node(s):** `engine.equity package public API`, `options package API`, `run_local_sh.sh script`, `TRADE_MODE`, `_S` (+36 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **14 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `PreopenIntelligenceScanner._add_candidate()` and `_is_valid_ti_ticker()`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **What is the exact relationship between `main()` and `ApexTraderAutoRun scheduled task installer`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **Why does `get_bars` connect `Momentum/VWAP Entry Strategies` to `Float Rotation & Squeeze Detection`, `Options Universe Resolution`, `Options Order Pricing/Execution`, `Equity Breakout Strategies`, `Bear Options Spreads`, `Bar Data Fetching`, `Bear-Market Kill Mode`, `Regime-Adaptive Position Sizing`, `Options Chain & OI Data`, `Predictions & Day-Picks Output`, `Shared Utility Helpers`, `Technical Strategy Indicators`, `Options Position Tracking`, `Trade Ideas Discovery Manager`, `Pre-Open Intelligence Providers`, `No-Gain Position Exit Logic`, `Scan Guardrail Pipeline`, `Short-Squeeze Fundamentals`?**
  _High betweenness centrality (0.151) - this node is a cross-community bridge._
- **Why does `EnhancedExecutor` connect `EOD Position Closing` to `Position Close Execution`, `Scan Universe Assembly`, `Correlation & Confidence Gating`, `Options Order Pricing/Execution`, `Sector Sympathy Scanner`, `Regime-Adaptive Position Sizing`, `Bear-Market Kill Mode`, `Discovery Injection Sources`, `Position Swap-on-Full Logic`, `Bracket Order Construction`, `Executor Health & Stop Management`, `No-Gain Position Exit Logic`, `Trailing Stop & TP Enforcement`?**
  _High betweenness centrality (0.099) - this node is a cross-community bridge._
- **Why does `MarketState` connect `Regime-Adaptive Position Sizing` to `Scan Universe Assembly`, `Options Order Pricing/Execution`, `Discovery Injection Sources`, `Options Chain & OI Data`, `Broker Client Factory`, `Shared Utility Helpers`, `Options Position Tracking`, `Trade Ideas Discovery Manager`, `Pre-Open Intelligence Providers`, `Pre-Open Watchlist Builder`, `No-Gain Position Exit Logic`, `Scan Guardrail Pipeline`, `EOD Position Closing`?**
  _High betweenness centrality (0.077) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `EnhancedExecutor` (e.g. with `Signal` and `OptionsExecutor`) actually correct?**
  _`EnhancedExecutor` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `Signal` (e.g. with `AccountSnapshot` and `EnhancedExecutor`) actually correct?**
  _`Signal` has 5 INFERRED edges - model-reasoned connections that need verification._
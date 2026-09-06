# Graph Report - .  (2026-09-03)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 1124 nodes · 2209 edges · 91 communities (82 shown, 9 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 73 edges (avg confidence: 0.53)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `4cc9faac`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- MarketState
- Signal
- AutoBotWatchdog
- ._submit_closing_order
- EnhancedExecutor
- test_guardian_and_deploy.py
- orchestrator.py
- universe.py
- strategies.py
- bars.py
- scan.py
- .execute
- session.py
- notifications.py
- test_eod_close_rerun.py
- guardian.py
- market.py
- _atr_trail_pct_for
- yahoo_universe.py
- utils/__init__.py
- enhanced.py
- scan_and_trade
- predict_tomorrow.py
- FakeClient
- FakeClient
- config.py
- get_bars
- _calc_atr14
- strategy_scoreboard.py
- test_marketable_limit_and_trail.py
- test_readiness_redteam.py
- test_staged_allocation.py
- .check_price_drift_stop
- calc_rsi
- .check_blocked_entries_ema
- kill_mode.py
- check_alpaca_data.py
- FakeClient
- test_thin_liquidity_admit.py
- test_morning_timeline_sim.py
- test_pending_entry_ema_recheck.py
- _get_float_shares
- ._maybe_rearm_reentry
- ._create_bracket_order
- run
- refresh_daily_pnl
- get_dynamic_universe
- test_entry_window.py
- _review_30d.py
- test_entry_log_rebuild_shorts.py
- test_no_gain_exit_band.py
- test_price_drift_young_position.py
- test_prior_traded_reentry.py
- test_protect_positions_shorts.py
- deploy.py
- _build_context
- _filter_eligible
- get_adaptive_interval
- test_clock_grid_schedule.py
- test_loss_reentry_30m_gate.py
- test_ratchet_done_reset.py
- test_short_rejection_handling.py
- test_universe_health_ratelimit.py
- test_strategy_selection_priority.py
- _analyze_entry_times.py
- monitor_bot_10min.ps1
- _simulate_day.py
- _margin_cushion_ok
- is_dead_ticker
- earnings.py
- print_config.py
- test_execute_bull_plan.py
- test_momentum_freshness.py
- test_portfolio_leverage_cap.py
- test_stopped_out_close_price.py
- run_local_sh.sh
- status.py
- test_scan_smoke.py
- _audit_trades.py

## God Nodes (most connected - your core abstractions)
1. `EnhancedExecutor` - 125 edges
2. `Signal` - 67 edges
3. `get_bars()` - 53 edges
4. `MarketState` - 33 edges
5. `AutoBotWatchdog` - 30 edges
6. `AppContext` - 26 edges
7. `scan_and_trade()` - 23 edges
8. `start()` - 23 edges
9. `get_strategy_instances()` - 21 edges
10. `_atr_trail_pct_for()` - 18 edges

## Surprising Connections (you probably didn't know these)
- `_Active` --uses--> `EnhancedExecutor`  [INFERRED]
  scripts/test_staged_allocation.py → engine/execution/enhanced.py
- `_demo()` --indirect_call--> `_raise()`  [INFERRED]
  engine/orchestrator.py → scripts/test_drift_backfill.py
- `_FakeTradeClient` --uses--> `Signal`  [INFERRED]
  scripts/test_marketable_limit_and_trail.py → engine/equity/strategies.py
- `_Quote` --uses--> `Signal`  [INFERRED]
  scripts/test_marketable_limit_and_trail.py → engine/equity/strategies.py
- `_QuoteClient` --uses--> `Signal`  [INFERRED]
  scripts/test_marketable_limit_and_trail.py → engine/equity/strategies.py

## Import Cycles
- None detected.

## Communities (91 total, 9 thin omitted)

### Community 0 - "MarketState"
Cohesion: 0.07
Nodes (29): _demo(), _load_movers_queue_from_disk(), PreopenIntelligenceScanner, PreopenSignalProvider, ApexTrader -- Discovery Manages live trending-stock scans, EDGAR 8-K and…, Write the current queue to disk. Best-effort -- a save failure must never block…, Build a scored pre-open watchlist and inject high-priority tickers. This is…, Refresh ``trending_stocks`` from live feeds (Finnhub, etc.). New tickers are… (+21 more)

### Community 1 - "Signal"
Cohesion: 0.07
Nodes (26): Signal, AccountSnapshot, _apply_thin_liquidity_override(), _check_momentum_freshness(), _entry_trend_snapshot(), OrderType, PDTTracker, Pattern Day Trader tracking -- syncs with live Alpaca daytrade_count. (+18 more)

### Community 2 - "AutoBotWatchdog"
Cohesion: 0.08
Nodes (18): AutoBotWatchdog, datetime, Logger, Path, SHA-256 of .env's raw bytes, or None when the file is missing. Content-hash…, Check PID_FILE for a live watchdog before touching anything. Task Scheduler's…, Seconds since engine/orchestrator.py last wrote HEARTBEAT_FILE, or None if it…, Runs for the life of the watchdog, independent of any single main.py subprocess… (+10 more)

### Community 3 - "._submit_closing_order"
Cohesion: 0.08
Nodes (19): _demo(), date, Submit a position-closing order as a marketable limit crossing the spread by…, Close any position whose broker-rejected PDT stop has been breached. Called…, Trim a correlated basket (e.g. leveraged inverse-market ETFs) whose COMBINED…, Trim the largest position(s) if TOTAL market value across every open position…, Pure decision logic for close_guardrail_fail_positions: return the reason…, Close every same-day position at EOD_CLOSE_TIME, regardless of strategy.… (+11 more)

### Community 4 - "EnhancedExecutor"
Cohesion: 0.08
Nodes (16): EnhancedExecutor, Optimized trade executor with consolidated long/short logic., Cancel stale/opposite resting orders for `symbol` before entering. User request…, On startup, reconstruct today's entry log from Alpaca filled orders. Prevents…, On startup, reconstruct self.order_cache from any BUY trailing-stop orders…, True if *symbol* already has a resting non-GTC order (i.e. something other than…, Return the symbol of the open long position with the worst unrealized P&L %.…, Return the symbol of the oldest closable long position held >= min_hours… (+8 more)

### Community 5 - "test_guardian_and_deploy.py"
Cohesion: 0.12
Nodes (21): check(), FakeClient, _kill_switch_env_checks(), _kill_switch_gate_checks(), _make_executor(), _NullLogger, pos(), Path (+13 more)

### Community 6 - "orchestrator.py"
Cohesion: 0.13
Nodes (26): AppContext, _build_short_queue(), _concentration_check_job(), _eod_close_job(), _fetch_account_and_positions(), _guardrail_close_job(), log_status(), _lunch_flat_job() (+18 more)

### Community 7 - "universe.py"
Cohesion: 0.12
Nodes (26): Fallback candidate for a guardrail-approved active-list symbol., _top_list_signal(), add_tickers(), get_latest_batch(), get_tier(), _is_expired(), _is_ti_primary_stale(), _load_raw() (+18 more)

### Community 8 - "strategies.py"
Cohesion: 0.10
Nodes (20): _get_market_cap(), get_strategy_instances(), LiquiditySweepStrategy, MomentumStrategy, ORBStrategy, PMHighBreakoutStrategy, PreMarketMomentumStrategy, ApexTrader - Strategies Trading strategy implementations: - TechnicalStrategy :… (+12 more)

### Community 9 - "bars.py"
Cohesion: 0.14
Nodes (22): _get_bars_alpaca(), get_bars_batch(), get_data_client(), mount_wide_pool(), _normalize_df(), _parse_timeframe(), engine.utils.bars ----------------- Bar data fetching, per-cycle cache,…, A usable, fresh bar came back -- clear any suppression immediately. (+14 more)

### Community 10 - "scan.py"
Cohesion: 0.14
Nodes (20): get_alpaca_movers_queue(), Return current Alpaca-movers tickers (read-only peek). 2026-08-27: no longer…, _demo(), get_scan_targets(), _is_thinly_traded(), _prefetch_snapshots(), ApexTrader scan nucleus. Contains reusable scanning functions for main loop and…, Self-check for get_scan_targets()'s market_state-gated guardrail pre-filter… (+12 more)

### Community 11 - ".execute"
Cohesion: 0.13
Nodes (11): _live_quote_mid(), PositionInfo, Cached snapshot of open positions., Sum of abs(market_value) across every open equity position (options legs…, True when no new entry orders may be submitted. Two independent halts feed it:…, Submit at most one active DAY entry per symbol. 2026-08-31: add an in-process…, Periodic poller (PENDING_ENTRY_RECHECK_SEC cadence) for staged allocation (25%…, Live bid/ask midpoint -- the reference _marketable_limit_price should bound… (+3 more)

### Community 12 - "session.py"
Cohesion: 0.12
Nodes (20): check_quarterly(), daily_loss_halted(), get_quarter_start(), load_daily_state(), load_quarterly_state(), date, ApexTrader -- Session Daily and quarterly P&L tracking state. Extracted from…, Persist current daily-start equity to disk (thread-safe). (+12 more)

### Community 13 - "notifications.py"
Cohesion: 0.17
Nodes (19): _bool_env(), build_eod_report(), _build_html_section(), build_top5_report(), _format_currency(), _format_signal_text(), _get_env(), _has_fresh_ticker() (+11 more)

### Community 14 - "test_eod_close_rerun.py"
Cohesion: 0.09
Nodes (8): _EarlyAfterCloseDateTime, _EarlyBeforeDateTime, _EarlyEodDateTime, FakeClient, FakePosition, _FixedDateTime, Self-check for the 2026-08-17 fix: close_eod_positions and…, _WeekendDateTime

### Community 15 - "guardian.py"
Cohesion: 0.20
Nodes (20): acquire_lock(), direct_flat_sell(), heartbeat_age_seconds(), http_json(), in_guardian_band(), load_env(), load_state(), log() (+12 more)

### Community 16 - "market.py"
Cohesion: 0.11
Nodes (17): check_vix_roc_filter(), get_market_sentiment(), in_lunch_break(), is_bull_regime(), is_market_open(), is_open_window(), is_regular_hours(), datetime (+9 more)

### Community 17 - "_atr_trail_pct_for"
Cohesion: 0.11
Nodes (11): _atr_trail_pct_for(), ratchet_scale(), Pure math for the confidence-ratchet trailing-stop multiplier. confidence <=…, For every open position whose shares are fully free (qty_available > 0 AND no…, Tighten the trailing stop on a position once it's up…, Fast-thread companion to protect_positions(): the moment a position exists with…, Poll every symbol close_eod_positions / close_guardrail_fail_positions…, ATR-aware trailing-stop % -- the wrapper every real call site uses. Computes… (+3 more)

### Community 18 - "yahoo_universe.py"
Cohesion: 0.20
Nodes (17): demo(), fetch_long_short_candidates(), fetch_yahoo_universe(), _is_valid_ti_ticker(), Yahoo Finance equity universe -- the source for data/ti_primary.json.…, Return (gainers, losers), each [(symbol, pct_change_today), ...]. Gainers =…, Gainers + losers + trending, deduped (order preserved, first-seen wins),…, Write gainers -> universe.json tier 1 (long candidates), losers -> tier 2… (+9 more)

### Community 19 - "utils/__init__.py"
Cohesion: 0.14
Nodes (17): get_finnhub_bars(), get_price(), Return the latest close price for symbol, or 0.0 on failure., Fetch OHLCV bars from Finnhub (alternative data source)., bool_env(), filter_trending_momentum(), format_currency(), get_env() (+9 more)

### Community 20 - "enhanced.py"
Cohesion: 0.13
Nodes (12): is_high_short_float(), Return True if symbol is in the static HSF set OR in the live tier-2 universe., _apply_strategy_kelly_mult(), _entry_rechase_slip_pct(), ApexTrader - Enhanced Executor Optimized trade executor with consolidated…, # NOTE: closing an existing position is NOT a new day trade., Next slip% for an entry re-chase attempt (_sweep_pending_entries) -- starts…, 2026-08-15, user request: per-strategy sizing informed by each strategy's own… (+4 more)

### Community 21 - "scan_and_trade"
Cohesion: 0.14
Nodes (16): filter_universe_by_positions(), Filter out symbols already held or with unfilled buy orders from the scan…, _build_scan_targets(), _check_kill_mode(), Call fn(*args, **kwargs) and log its wall time under [TIMING] <label>., Fire all configured universe refresh sources (each throttled internally)., Return the stock-only execution capacity; broad-market regime is ignored., Return (scan_targets, excluded) after universe assembly and position filtering. (+8 more)

### Community 22 - "predict_tomorrow.py"
Cohesion: 0.18
Nodes (14): calculate_atr(), Compute Average True Range over the last `period` bars. Returns 0.0 on failure., calculate_risk_adjusted_size(), get_dynamic_tier(), engine.utils.risk ----------------- ATR-based tier assignment (with 15-min…, Return position-sizing metadata for an entry. Uses local effective_* variables…, Return ATR-based TP/TS tier info for *symbol*. Result is cached per symbol for…, main() (+6 more)

### Community 23 - "FakeClient"
Cohesion: 0.13
Nodes (6): ReplaceOrderRequest, FakeAccount, FakeClient, FakeOrder, FakePosition, Self-check for the 2026-08-17 fix: enforce_position_concentration's trim was…

### Community 24 - "FakeClient"
Cohesion: 0.13
Nodes (6): FakeClient, FakeOrder, FakePosition, _FixedDateTime, make_executor(), Self-check for the 2026-09-01 two-window schedule: lunch_flat_positions() hard-…

### Community 25 - "config.py"
Cohesion: 0.13
Nodes (7): ApexTrader - Configuration Professional Automated Trading System Modular…, _raise(), Self-check for the price-drift-stop restart backfill (2026-08-14, at the user's…, Self-check for the growing per-position concentration cap (2026-08-17, at the…, Self-check for close_guardrail_fail_positions (2026-08-12, at the user's…, Regression net for the 2026-09-02 morning-readiness trigger (09:25 ET).…, Self-check for the swing/multi-day drift stop (2026-08-15, at the user's…

### Community 26 - "get_bars"
Cohesion: 0.19
Nodes (13): _passes_guardrails(), Pre-scan gates: dollar-volume, RVOL, and gap-chase guard. Returns False to skip…, get_bars(), _get_bars_yfinance(), get_daily_volume_bars(), get_premarket_bars(), DataFrame, Count a stale/empty fetch; suppress after _DEAD_TICKER_THRESHOLD in a row. (+5 more)

### Community 27 - "_calc_atr14"
Cohesion: 0.13
Nodes (11): _calc_atr14(), GapBreakoutStrategy, OpeningBellSurgeStrategy, PowerOf3Strategy, DataFrame, ICT Power of 3: tight morning accumulation -> sweep below the range low…, Gap-up continuation: stock opens significantly above prior close. Logic: - Load…, Calculate Average True Range over the last `period` bars. (+3 more)

### Community 28 - "strategy_scoreboard.py"
Cohesion: 0.21
Nodes (13): kelly_pct(), _pull_matched_trades(), Strategy scoreboard -- recurring Kelly/win-rate health check across every…, {strategy: (n, win_rate, avg_win, avg_loss, kelly)} from a trade list., Pull data, compute the scoreboard, log + print it, return flagged strategies., Kelly fraction: W - (1-W)/R, R = avg_win/avg_loss (both positive $ amounts).…, True if a strategy is worth surfacing: currently enabled, enough trades to…, Network call: pull every apex-tagged entry, match to its exit, pull confidence… (+5 more)

### Community 29 - "test_marketable_limit_and_trail.py"
Cohesion: 0.14
Nodes (7): _marketable_limit_price(), A limit price just past the reference price -- fills like a market order under…, _FakeTradeClient, _Quote, _QuoteClient, Self-check for the live-mid bounded-limit-price fix and the thin-liquidity…, _sig()

### Community 30 - "test_readiness_redteam.py"
Cohesion: 0.14
Nodes (9): Trailing-stop % for `symbol`. 2026-08-22, user request: replaced the…, _trail_pct_for(), _make_bars_raiser(), _patched_call(), Self-check for the ATR-based trailing stop (2026-09-01, user request: "change…, _raises(), Red-team / edge-case net for the 2026-09-02 morning-readiness machinery.…, Exact replica of the run-loop readiness_due expression (incl. the 2026-09-02… (+1 more)

### Community 31 - "test_staged_allocation.py"
Cohesion: 0.16
Nodes (6): _Active, _Client, _make_executor(), _Order, _Pos, Self-check for staged allocation (25% x 4), never adding while losing. The…

### Community 32 - ".check_price_drift_stop"
Cohesion: 0.15
Nodes (7): datetime, Return (official close ET, EOD close ET, source), cached per day. Uses Alpaca's…, Return the UTC fill timestamp a position was opened -- hour-precision…, Close any position (long or short) that hasn't settled into a clear positive…, Pure decision logic for check_price_drift_stop: return a reason string if the…, When _price_drift_history has no rolling history yet for symbol (a fresh…, Every PRICE_DRIFT_CHECK_INTERVAL_MIN (10 min), exit any same-day position…

### Community 33 - "calc_rsi"
Cohesion: 0.18
Nodes (9): BearBreakdownStrategy, Short-entry: daily breakdown below 20-SMA + 10-day low with volume spike. Only…, Multi-indicator technical analysis (RSI, MACD, MA, Volume)., Price reclaims VWAP from below with accelerating volume -- second-leg setup.…, TechnicalStrategy, VWAPReclaimStrategy, calc_macd(), calc_rsi() (+1 more)

### Community 34 - ".check_blocked_entries_ema"
Cohesion: 0.18
Nodes (8): _closed_1m_bars(), _entry_gate_bars(), Fetch premarket-inclusive 1-minute bars and evaluate EMA gates before 09:30., Return premarket-inclusive 1m bars when needed so 09:30 entries can validate., Keep only closed 1-minute candles; fall back to provider rows as closed if no…, For loss re-entry, require first-30 direction and recent 30m momentum., Pure decision function for check_blocked_entries_ema(): what to do with one…, Every STAGNANT_STOP_CHECK_INTERVAL_MIN (1 min), re-check every signal queued in…

### Community 35 - "kill_mode.py"
Cohesion: 0.21
Nodes (9): check(), is_active(), ApexTrader -- Kill Mode Extreme bear-market circuit breaker. Extracted from…, Return True when kill mode is engaged for today., Check extreme bear conditions and trigger emergency close if needed. Returns…, get_vix(), get_vix_interval(), Return the latest VIX daily close. Defaults to 15.0 on failure. (+1 more)

### Community 36 - "check_alpaca_data.py"
Cohesion: 0.29
Nodes (11): alpaca_client(), compare_symbol(), fetch_alpaca_bars(), fetch_finnhub_quote(), fetch_yfinance_bars(), main(), parse_period_days(), parse_timeframe() (+3 more)

### Community 37 - "FakeClient"
Cohesion: 0.17
Nodes (4): FakeClient, FakeOrder, FakePosition, Self-check for force-close after-hours chasing. Alpaca equity trailing stops do…

### Community 38 - "test_thin_liquidity_admit.py"
Cohesion: 0.18
Nodes (9): _apply_confidence_size_ramp(), Decide what _validate_trade does with a _check_momentum_freshness result:…, 2026-08-13, user request: confidence-scaling (_execute_entry's…, _resolve_freshness_reject(), Self-check for the thin-liquidity rejected-list trading path (2026-08-12,…, # NOTE: TRADE_THIN_LIQUIDITY_REJECTS's live value is a deployment decision, _sig(), Self-check for THIN_LIQUIDITY_EXCLUDED_STRATEGIES (2026-08-15, at the user's… (+1 more)

### Community 39 - "test_morning_timeline_sim.py"
Cohesion: 0.24
Nodes (6): _d(), _overlaps_window(), Deep-dive timeline simulation: does the 09:25 ET morning-readiness design…, Discrete 1s-step simulation. restarts: ET datetimes when the watchdog…, simulate(), _t()

### Community 40 - "test_pending_entry_ema_recheck.py"
Cohesion: 0.20
Nodes (4): _FakeClient, _FakeOrder, _make_executor(), Self-check for check_pending_entries_ema (2026-08-24, user request): "every…

### Community 41 - "_get_float_shares"
Cohesion: 0.24
Nodes (6): EarlySqueezeDetector, FloatRotationStrategy, _get_float_shares(), Fires 9:30-10:15 AM ET for low-float stocks showing gap + projected RVOL >4x +…, Cached float share count sourced from yfinance. Returns None when float data is…, Low-float stock with volume > X% of float = stock is 'in play'. Logic: - Fetch…

### Community 42 - "._maybe_rearm_reentry"
Cohesion: 0.20
Nodes (6): _check_ema_trend_alignment(), EMA7 slope + EMA7-vs-EMA15 crossover alignment gate for a fresh entry. This is…, Catch a position closing via ANY route -- most commonly a normal broker-side…, Actively watch every open position's loss while the market is NOT in regular…, Return the most recent filled close price for a just-closed equity lot.…, After a STOP-LOSS-type close, re-check the same entry gate a fresh signal would…

### Community 43 - "._create_bracket_order"
Cohesion: 0.20
Nodes (5): Broker rejected a short with "cannot be sold short" / 40310000 / "account is…, How many times `symbol` has already been entered today -- resets on a date…, True if `symbol` should use the trailing-buy entry path instead of the normal…, Submit a DAY trailing-stop entry. Protective 1.5% GTC trailing stop is attached…, Exception

### Community 44 - "run"
Cohesion: 0.53
Nodes (5): run(), handle_lock(), _is_pid_running(), main(), Checks for an existing lock file and creates one if it doesn't exist.

### Community 45 - "refresh_daily_pnl"
Cohesion: 0.14
Nodes (14): _execute_bull_plan(), _execution_rank(), _guardian_flat_requested(), _maybe_guardian_halt(), One iteration's worth of _start_software_stop_thread's checks. Returns the…, Bull (or neutral) regime: try eligible signals ranked by confidence, highest…, Keep default basket signals behind ordinary day-scan signals., Retry the latest top eligible signals on the five-second poller. Every attempt… (+6 more)

### Community 46 - "get_dynamic_universe"
Cohesion: 0.25
Nodes (8): get_dynamic_universe(), Return (p1, p2, p3) merged lists, re-reading universe.json on every call., merge_live(), Merge dynamic (TTL-managed) tickers with core static list, deduplicating and…, is_never_trade(), load_never_trade(), Permanent ticker exclusion list -- data/never_trade.txt. Shared by universe…, Return the set of permanently excluded tickers. Re-reads the file when its…

### Community 47 - "test_entry_window.py"
Cohesion: 0.15
Nodes (16): _demo(), _poller_staleness_job(), datetime, Scheduled every minute (see run()) -- alerts if SoftwareStopPoller hasn't…, Return the configured interval (minutes) for now_et's tier, or None if outside…, Scheduled every minute (see run()) -- refreshes data/ti_primary.json from Yahoo…, python -m engine.orchestrator -- asserts _poller_staleness_job's alert state…, True if now_et (ET, tz-aware) falls within either entry segment:… (+8 more)

### Community 48 - "_review_30d.py"
Cohesion: 0.39
Nodes (8): et_hhmm(), fetch(), in_entry_window(), in_lunch(), load_env(), main(), Review the last N calendar days of real Alpaca fills against the new two-window…, to_et_minutes()

### Community 49 - "test_entry_log_rebuild_shorts.py"
Cohesion: 0.25
Nodes (3): FakeClient, Self-check for the entry-log restart rebuild covering SHORT positions…, _rebuild()

### Community 50 - "test_no_gain_exit_band.py"
Cohesion: 0.28
Nodes (5): _closed(), _FakeClient, Self-check for the no-gain-exit band change (2026-08-11) and long+short…, Run the real close_no_gain_positions() against one fake position. Returns…, _run()

### Community 51 - "test_price_drift_young_position.py"
Cohesion: 0.28
Nodes (4): _Client, _make_executor(), _Pos, Self-check for the price-drift-stop age gate (2026-08-18, user request: "check…

### Community 52 - "test_prior_traded_reentry.py"
Cohesion: 0.25
Nodes (5): _CountingClient, _make_executor(), _Order, Self-check for treating ANY prior-traded stock as a re-entry (2026-08-18, user…, get_orders returns `orders` and counts how many times it's called -- proves the…

### Community 53 - "test_protect_positions_shorts.py"
Cohesion: 0.25
Nodes (3): _FakeClient, _make_executor(), Self-check for the protect_positions() qty_available sign bug (2026-08-12).…

### Community 54 - "deploy.py"
Cohesion: 0.39
Nodes (7): CompletedProcess, git_head(), main(), ApexTrader -- auto-deploy gate (2026-09-02). Runs the full offline test gate…, Every scripts/test_*.py must pass; engine/scripts must compile., run(), test_gate()

### Community 55 - "_build_context"
Cohesion: 0.18
Nodes (8): BrokerFactory, ApexTrader - Broker Factory Selects the appropriate broker client. Only Alpaca…, Factory for creating broker clients., Create a stock trading client. Args: broker: only 'alpaca' is supported., _build_context(), Create and wire all runtime singletons. Called once at startup., Read-only dry run: which currently-open positions WOULD close_eod_positions and…, TradingClient

### Community 56 - "_filter_eligible"
Cohesion: 0.33
Nodes (6): leveraged_underlying(), Best-effort underlying key for the same-underlying entry guard. Exact group…, _filter_eligible(), _log_skipped(), Apply confidence gate, position cross-ref, and long-only enforcement. Returns…, Log skip reason for each top-10 raw signal that did not make it to eligible.

### Community 57 - "get_adaptive_interval"
Cohesion: 0.33
Nodes (6): get_adaptive_interval(), Return next scan interval in minutes based on VIX, market phase, and position…, get_market_hours_interval(), get_position_tuning_interval(), Map current hour (decimal, ET) to scan interval and phase label., Map open-position count to scan interval and position-status label.

### Community 60 - "test_clock_grid_schedule.py"
Cohesion: 0.33
Nodes (3): Register `job` to run at fixed wall-clock marks (:00, :10, :20, ... for…, _schedule_on_clock_grid(), Self-check for _schedule_on_clock_grid (2026-08-14, found while investigating…

### Community 61 - "test_loss_reentry_30m_gate.py"
Cohesion: 0.40
Nodes (4): _bars(), _check(), DataFrame, Focused self-check for the loss re-entry 30-minute gate. Run: python…

### Community 62 - "test_ratchet_done_reset.py"
Cohesion: 0.40
Nodes (3): _FakeClient, _make_executor(), Self-check for the _ratchet_done reset fix (2026-08-10). Bug: _ratchet_done was…

### Community 63 - "test_short_rejection_handling.py"
Cohesion: 0.40
Nodes (3): _FakeClient, _make_executor(), Self-check for shorting_blocked (live property) and the HTB/equity conflation…

### Community 65 - "test_strategy_selection_priority.py"
Cohesion: 0.40
Nodes (4): Prefer Gap Breakout/ORB, then use confidence within each tier., _strategy_selection_rank(), Regression check for signal selection priority within one symbol., _signal()

### Community 66 - "_analyze_entry_times.py"
Cohesion: 0.60
Nodes (4): et_hhmm(), main(), Analyze apextrader.log: entry time -> outcome, to test the hypothesis that…, to_minutes()

### Community 67 - "monitor_bot_10min.ps1"
Cohesion: 0.60
Nodes (3): Get-ActiveKeyPrefix(), Get-ActiveMode(), Start-BotWatchdog()

### Community 68 - "_simulate_day.py"
Cohesion: 0.60
Nodes (4): fetch(), load_env(), main(), Simulate 2026-09-01's trades under the proposed rule changes. Data source:…

### Community 69 - "_margin_cushion_ok"
Cohesion: 0.50
Nodes (3): _margin_cushion_ok(), True if equity is still >= min_ratio x maintenance_margin (safe cushion against…, Self-check for the margin-cushion safeguard (2026-08-12, at the user's…

### Community 70 - "is_dead_ticker"
Cohesion: 0.50
Nodes (4): _demo(), is_dead_ticker(), True if symbol is currently suppressed for persistent stale/empty data., Self-check for the dead-ticker suppression state machine.

### Community 71 - "earnings.py"
Cohesion: 0.50
Nodes (3): no_earnings_soon(), Shared earnings-date lookup, used by equity strategies to avoid entering right…, Return True if no earnings are expected within *days* calendar days. Data…

### Community 75 - "test_portfolio_leverage_cap.py"
Cohesion: 0.50
Nodes (3): max_leverage_shares(), Self-check for the portfolio-wide leverage cap (2026-08-17, at the user's…, Mirrors the inline calc in _size_with_buying_power exactly.

## Knowledge Gaps
- **3 isolated node(s):** `run_local_sh.sh script`, `TRADE_MODE`, `_S`
  These have ≤1 connection - possible missing edges or undocumented components.
- **9 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `EnhancedExecutor` connect `EnhancedExecutor` to `MarketState`, `Signal`, `._submit_closing_order`, `test_guardian_and_deploy.py`, `orchestrator.py`, `.execute`, `_atr_trail_pct_for`, `enhanced.py`, `FakeClient`, `config.py`, `test_staged_allocation.py`, `.check_price_drift_stop`, `.check_blocked_entries_ema`, `test_pending_entry_ema_recheck.py`, `._maybe_rearm_reentry`, `._create_bracket_order`, `test_no_gain_exit_band.py`, `test_prior_traded_reentry.py`, `test_protect_positions_shorts.py`, `_build_context`, `_filter_eligible`, `test_ratchet_done_reset.py`, `test_short_rejection_handling.py`?**
  _High betweenness centrality (0.271) - this node is a cross-community bridge._
- **Why does `Signal` connect `Signal` to `._submit_closing_order`, `EnhancedExecutor`, `orchestrator.py`, `universe.py`, `strategies.py`, `scan.py`, `.execute`, `enhanced.py`, `get_bars`, `_calc_atr14`, `test_marketable_limit_and_trail.py`, `calc_rsi`, `test_thin_liquidity_admit.py`, `_get_float_shares`, `._maybe_rearm_reentry`, `._create_bracket_order`, `refresh_daily_pnl`, `test_strategy_selection_priority.py`, `test_momentum_freshness.py`?**
  _High betweenness centrality (0.080) - this node is a cross-community bridge._
- **Why does `AutoBotWatchdog` connect `AutoBotWatchdog` to `test_guardian_and_deploy.py`?**
  _High betweenness centrality (0.059) - this node is a cross-community bridge._
- **Are the 26 inferred relationships involving `EnhancedExecutor` (e.g. with `Signal` and `AppContext`) actually correct?**
  _`EnhancedExecutor` has 26 INFERRED edges - model-reasoned connections that need verification._
- **Are the 14 inferred relationships involving `Signal` (e.g. with `AccountSnapshot` and `EnhancedExecutor`) actually correct?**
  _`Signal` has 14 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `AutoBotWatchdog` (e.g. with `FakeClient` and `_NullLogger`) actually correct?**
  _`AutoBotWatchdog` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `run_local_sh.sh script`, `TRADE_MODE`, `_S` to the rest of the system?**
  _3 weakly-connected nodes found - possible documentation gaps or missing edges._
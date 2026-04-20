"""
ApexTrader orchestrator.

Encapsulates startup, scan loop, status logging, and execution orchestration.
"""

from __future__ import annotations

import datetime
import logging
import os
import sys
import threading
import time
import concurrent.futures
from pathlib import Path

import schedule
import pytz
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

from . import config as cfg
from .utils import (
    setup_logging,
    is_market_open,
    is_regular_hours,
    is_options_lull_hours,
    get_vix,
    clear_bar_cache,
    get_finnhub_trending_tickers,
    get_market_sentiment,
    get_market_hours_interval,
    get_position_tuning_interval,
    get_trending_tickers,
    filter_trending_momentum,
    get_vix_interval,
    get_bars,
    get_live_holdings,
)
from .equity.strategies import _is_bull_regime
from engine.execution.enhanced import EnhancedExecutor
from .notifications import notify_scan_results, notify_eod
from .equity.scan import get_scan_targets, scan_universe, filter_signals
from .equity.universe import filter_universe_by_positions
from engine.broker.broker_factory import BrokerFactory
from .predictions import save_day_picks
from engine.options.executor import OptionsExecutor
from engine.options.strategies import scan_options_universe
from .equity import discovery as _discovery
from . import session as _session
from engine.risk import kill_mode as _kill_mode

log = setup_logging()
log.info(f"Trade mode: {cfg.TRADE_MODE} (PAPER={cfg.PAPER}, LIVE={cfg.LIVE})")
if not cfg.LONG_ONLY_MODE:
    log.info("Shorting enabled (LONG_ONLY_MODE=False).")

import logging as _logging
_logging.getLogger("WDM").setLevel(_logging.ERROR)
_logging.getLogger("webdriver_manager").setLevel(_logging.ERROR)

client = BrokerFactory.create_stock_client(cfg.STOCKS_BROKER)
executor = EnhancedExecutor(client, use_bracket_orders=True)
options_executor = OptionsExecutor(client) if cfg.OPTIONS_ENABLED else None
if cfg.OPTIONS_ENABLED:
    log.info(
        f"Options trading ENABLED ({int(cfg.OPTIONS_ALLOCATION_PCT)}% allocation, "
        f"{cfg.OPTIONS_DTE_MIN}-{cfg.OPTIONS_DTE_MAX} DTE)"
    )

_last_market_regime: str = "bull"
_short_fail_cooldown: dict = {}


def scan_trending_stocks() -> None:
    _discovery.scan_trending_stocks(
        use_live_trending=cfg.USE_LIVE_TRENDING,
        use_finnhub=cfg.USE_FINNHUB_DISCOVERY,
        use_sentiment_gate=cfg.USE_SENTIMENT_GATE,
        trending_max=cfg.TRENDING_MAX_RESULTS,
        trending_interval_min=cfg.TRENDING_SCAN_INTERVAL,
        trending_min_momentum=cfg.TRENDING_MIN_MOMENTUM,
        priority_1=cfg.PRIORITY_1_MOMENTUM,
    )


def scan_tradeideas_universe() -> None:
    _discovery.scan_tradeideas_universe(
        enabled=cfg.USE_TRADEIDEAS_DISCOVERY,
        scan_interval_min=cfg.TRADEIDEAS_SCAN_INTERVAL_MIN,
        headless=cfg.TRADEIDEAS_HEADLESS,
        chrome_profile=cfg.TRADEIDEAS_CHROME_PROFILE,
        update_config=cfg.TRADEIDEAS_UPDATE_CONFIG_FILE,
        priority_1=cfg.PRIORITY_1_MOMENTUM,
        priority_2=cfg.PRIORITY_2_ESTABLISHED,
        browser=cfg.TRADEIDEAS_BROWSER,
        remote_debug_port=9222,
    )


def scan_tradeideas_unusual_options() -> None:
    _discovery.scan_tradeideas_unusual_options(
        enabled=cfg.USE_TRADEIDEAS_UNUSUAL_OPTIONS_DISCOVERY,
        scan_interval_min=cfg.TRADEIDEAS_UNUSUAL_OPTIONS_SCAN_INTERVAL_MIN,
        headless=cfg.TRADEIDEAS_HEADLESS,
        chrome_profile=cfg.TRADEIDEAS_CHROME_PROFILE,
        update_config=cfg.TRADEIDEAS_UPDATE_CONFIG_FILE,
        priority_1=cfg.PRIORITY_1_MOMENTUM,
        priority_2=cfg.PRIORITY_2_ESTABLISHED,
        browser=cfg.TRADEIDEAS_BROWSER,
        remote_debug_port=9222,
    )


def scan_tradeideas_toplists() -> None:
    _discovery.scan_tradeideas_toplists(
        enabled=cfg.USE_TRADEIDEAS_TOPLISTS_DISCOVERY,
        scan_interval_min=cfg.TRADEIDEAS_TOPLISTS_SCAN_INTERVAL_MIN,
        headless=cfg.TRADEIDEAS_HEADLESS,
        chrome_profile=cfg.TRADEIDEAS_CHROME_PROFILE,
        update_config=cfg.TRADEIDEAS_UPDATE_CONFIG_FILE,
        priority_1=cfg.PRIORITY_1_MOMENTUM,
        priority_2=cfg.PRIORITY_2_ESTABLISHED,
        browser=cfg.TRADEIDEAS_BROWSER,
        remote_debug_port=9222,
    )


def scan_sympathy_and_edgar() -> None:
    _discovery.scan_sympathy_and_edgar(
        sympathy_enabled=cfg.USE_SECTOR_SYMPATHY,
        edgar_enabled=cfg.USE_EDGAR_SCANNER,
        sympathy_interval_min=cfg.SECTOR_SYMPATHY_INTERVAL_MIN,
        edgar_interval_min=cfg.EDGAR_SCANNER_INTERVAL_MIN,
        priority_1=cfg.PRIORITY_1_MOMENTUM,
        priority_2=cfg.PRIORITY_2_ESTABLISHED,
    )

def scan_top3_only() -> None:
    sentiment = get_market_sentiment()
    log.info(f"Market sentiment: {sentiment}")

    scan_trending_stocks()
    scan_tradeideas_universe()
    scan_tradeideas_unusual_options()
    scan_tradeideas_toplists()
    scan_sympathy_and_edgar()

    _positions, _orders, _excluded = get_live_holdings(client)
    scan_targets = get_scan_targets(_excluded)
    log.info(f"Top3 mode: scanning {len(scan_targets)} symbols ({len(_excluded)} pre-excluded)")

    signals, hit_counts, scan_errors = scan_universe(scan_targets, sentiment)
    log.info(f"Scan errors: {scan_errors} | Signals: {len(signals)}")

    if signals:
        _, _, _fresh_held = get_live_holdings(client)
        _fresh_held = _fresh_held or _excluded
        top5 = [s for s in signals if s.symbol not in _fresh_held][:5]
        if not top5:
            log.info("No signals found in Top5 mode (all candidates already held)")
            return
        log.info("TOP 5 SCAN PICKS:")
        for idx, s in enumerate(top5, start=1):
            log.info(f"#{idx}: {s.symbol} {s.action.upper()} ${s.price:.2f} conf={s.confidence:.0%} [{s.strategy}] - {s.reason}")
        notify_scan_results(top5, datetime.date.today(), sentiment, _last_market_regime)
    else:
        log.info("No signals found in Top5 mode")


def check_kill_mode() -> bool:
    return _kill_mode.check(
        client, executor, options_executor,
        vix_level=cfg.KILL_MODE_VIX_LEVEL,
        spy_drop_pct=cfg.KILL_MODE_SPY_DROP_PCT,
        vix_roc_pct=cfg.KILL_MODE_VIX_ROC_PCT,
    )


def scan_and_trade() -> None:
    global _last_market_regime

    _session.reset_daily(client)

    if options_executor is not None and is_regular_hours():
        try:
            options_executor.monitor_positions()

            if is_options_lull_hours():
                log.info("[OPTIONS] Lull period (open auction or midday) — monitoring only, no new entries")
            else:
                _all_positions = client.get_all_positions()
                _held_map = {
                    p.symbol: int(float(p.qty))
                    for p in _all_positions
                    if float(p.qty) > 0
                }
                _existing_opt_syms = {
                    pos.occ_symbol for pos in options_executor._positions.values()
                }

                opt_signals = scan_options_universe(_held_map, _existing_opt_syms)
                if opt_signals:
                    top_opt_signals = opt_signals[:10]
                    log.info(
                        f"[OPTIONS] {len(opt_signals)} signals found — passing top {len(top_opt_signals)} to executor; "
                        f"top candidate: {top_opt_signals[0].symbol} {top_opt_signals[0].option_type} conf={top_opt_signals[0].confidence:.0%}"
                    )
                    for opt_sig in top_opt_signals:
                        if options_executor.place_option_order(opt_sig):
                            break
                else:
                    log.info("[OPTIONS] No qualifying signals this cycle")

            log.info(f"[OPTIONS] {options_executor.status_summary()}")
        except Exception as _opt_err:
            log.error(f"[OPTIONS] Options cycle error: {_opt_err}", exc_info=True)
            
    if not is_market_open():
        if not cfg.FORCE_SCAN:
            log.info("[SYSTEM] Market closed - skipping scan")
            return
        log.warning("[SYSTEM] FORCE_SCAN active — bypassing market-hours gate")

    if check_kill_mode():
        log.info("[SYSTEM] Kill mode active, exiting scan_and_trade.")
        return

    _session.refresh_daily_pnl(client)

    _loss_pct = cfg.DAILY_LOSS_LIMIT_BEAR_PCT if _last_market_regime == "bear" else cfg.DAILY_LOSS_LIMIT_BULL_PCT
    _daily_loss_limit = -(_session.daily_start_equity * _loss_pct / 100) if _session.daily_start_equity > 0 else -999_999

    if _session.daily_pnl <= _daily_loss_limit:
        log.warning(
            f"[SYSTEM] Daily loss limit hit ({_loss_pct:.0f}% {_last_market_regime}): "
            f"${_session.daily_pnl:.2f} <= ${_daily_loss_limit:.2f} — halting trades"
        )
        return

    if _session.daily_pnl >= cfg.DAILY_PROFIT_TARGET:
        log.info(f"[SYSTEM] Daily profit target reached: ${_session.daily_pnl:.2f} (started at ${_session.daily_start_equity:,.2f})")
        return

    _session.check_quarterly(client, cfg.USE_QUARTERLY_TARGET, cfg.QUARTERLY_PROFIT_TARGET_PCT)

    sentiment = get_market_sentiment()
    log.info(f"[SCAN] Market sentiment: {sentiment}")

    executor.update_stale_orders()
    executor.check_tp_targets()

    scan_trending_stocks()
    scan_tradeideas_universe()
    scan_sympathy_and_edgar()

    _open_positions, _open_orders, _excluded = get_live_holdings(client)
    scan_targets = filter_universe_by_positions(get_scan_targets(), _excluded)
    log.info(
        f"[SCAN] Scanning {len(scan_targets)} symbols (filtered by held/ordered), {cfg.SCAN_WORKERS} workers: "
        f"{', '.join(scan_targets)}"
    )

    if not scan_targets:
        log.info("[SCAN] No scan targets after filtering — skipping scan")
        return

    executor._swap_cycle_closed.clear()

    signals_cap = cfg.MAX_SIGNALS_PER_CYCLE
    market_regime = _last_market_regime
    if cfg.USE_MARKET_REGIME_FILTER:
        try:
            is_bull = _is_bull_regime()
            market_regime = "bull" if is_bull else "bear"
            _last_market_regime = market_regime
            signals_cap = cfg.MAX_SIGNALS_PER_CYCLE if is_bull else cfg.MARKET_REGIME_SIGNALS_CAP
            if is_bull:
                log.info(f"[SCAN] BULL REGIME — signals capped at {signals_cap}/cycle")
            else:
                effective_short_cap = 0 if (cfg.LONG_ONLY_MODE or executor.shorting_blocked) else cfg.BEAR_SHORT_SIGNALS_CAP
                log.info(
                    f"[SCAN] BEAR REGIME — long cap {cfg.MARKET_REGIME_SIGNALS_CAP}/cycle, "
                    f"short cap {effective_short_cap}/cycle"
                )
        except Exception as e:
            log.error(f"[SYSTEM] Market regime check FAILED — retaining '{_last_market_regime}' regime: {e}", exc_info=True)

    signals, hit_counts, scan_errors = scan_universe(scan_targets, sentiment)

    if cfg.LONG_ONLY_MODE:
        pre_len = len(signals)
        signals = [s for s in signals if s.action == "buy"]
        log.warning(
            f"LONG_ONLY_MODE is enabled: filtered {pre_len} -> {len(signals)} signals (buy-only)"
        )

    breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(hit_counts.items()))
    log.info(f"[SCAN] Signal breakdown — {breakdown or 'none'} | Errors: {scan_errors}")
    if not hit_counts:
        log.info("[SCAN] No signals: market likely in downtrend — waiting for setups")

    log.info(f"[SCAN] Total raw signals: {len(signals)}")

    if signals:
        top5_raw = sorted(signals, key=lambda s: s.confidence, reverse=True)[:5]
        for idx, s in enumerate(top5_raw, start=1):
            log.info(
                f"[SCAN] TOP5_RAW #{idx}: {s.symbol} {s.action.upper()} ${s.price:.2f} "
                f"conf={s.confidence:.0%} [{s.strategy}] — {s.reason}"
            )
    else:
        log.info("[SCAN] TOP5_RAW: none this cycle")

    if signals:
        _live_positions, _live_orders, _fresh_held_new = get_live_holdings(client)
        _fresh_held = _fresh_held_new or _excluded

        log.info(
            f"Live holdings: {len(_live_positions)} positions, "
            f"{len(_live_orders)} active orders | {len(_fresh_held)} total excluded"
        )

        short_min_conf = cfg.MIN_SHORT_CONFIDENCE_BEAR if market_regime == "bear" else cfg.MIN_SIGNAL_CONFIDENCE
        eligible = []
        log.debug(f"[DBG] LONG_ONLY_MODE={cfg.LONG_ONLY_MODE} shorting_blocked={executor.shorting_blocked} short_min={short_min_conf} regime={market_regime}")
        for s in signals:
            if s.symbol in _fresh_held:
                continue
            conf = round(float(s.confidence), 2)
            log.debug(f"[DBG] signal {s.symbol} action={s.action} conf={conf:.2f} held={s.symbol in _fresh_held}")
            if s.action == "buy" and conf >= cfg.MIN_SIGNAL_CONFIDENCE:
                eligible.append(s)
            elif (
                s.action in ("sell", "short")
                and not cfg.LONG_ONLY_MODE
                and not executor.shorting_blocked
                and conf >= short_min_conf
            ):
                eligible.append(s)

        if executor.shorting_blocked and not cfg.LONG_ONLY_MODE:
            log.warning("Shorting blocked by broker permissions (40310000). Continuing in effective long-only mode this session.")

        log.info(
            f"Confidence gate (long>={cfg.MIN_SIGNAL_CONFIDENCE:.0%}, "
            f"short>={short_min_conf:.0%}) + position cross-ref: {len(eligible)} signal(s) qualify"
        )

        long_only_hit = cfg.LONG_ONLY_MODE or executor.shorting_blocked
        if cfg.LONG_ONLY_MODE and any(s.action in ("sell", "short") for s in eligible):
            log.warning("LONG_ONLY_MODE is active - removing short candidates from eligible list")
            eligible = [s for s in eligible if s.action == "buy"]

        if long_only_hit and not eligible:
            fallback = next(
                (s for s in signals
                 if s.action == "buy" and s.symbol not in _fresh_held and round(float(s.confidence), 2) >= cfg.MIN_SIGNAL_CONFIDENCE),
                None
            )
            if fallback:
                log.warning(
                    f"Long-only fallback: no eligible signals, forcing {fallback.symbol} buy @ ${fallback.price:.2f} conf={fallback.confidence:.0%}"
                )
                eligible = [fallback]

        eligible_syms = {s.symbol for s in eligible}
        top10_raw = sorted(signals, key=lambda s: s.confidence, reverse=True)[:10]
        not_qualified = [s for s in top10_raw if s.symbol not in eligible_syms]
        if not_qualified:
            for s in not_qualified:
                conf = round(float(s.confidence), 2)
                if s.symbol in _fresh_held:
                    reason_str = "already held/ordered"
                elif s.action == "buy" and conf < cfg.MIN_SIGNAL_CONFIDENCE:
                    reason_str = f"conf {conf:.0%} < long min {cfg.MIN_SIGNAL_CONFIDENCE:.0%}"
                elif s.action in ("sell", "short") and conf < short_min_conf:
                    reason_str = f"conf {conf:.0%} < short min {short_min_conf:.0%}"
                elif executor.shorting_blocked and s.action in ("sell", "short"):
                    reason_str = "shorting blocked by broker"
                elif cfg.LONG_ONLY_MODE and s.action != "buy":
                    reason_str = "long-only mode"
                else:
                    reason_str = "filtered"
                log.info(
                    f"[SCAN] SKIP {s.symbol} {s.action.upper()} ${s.price:.2f} "
                    f"conf={s.confidence:.0%} [{s.strategy}] — {reason_str}"
                )

        if cfg.LONG_ONLY_MODE or executor.shorting_blocked:
            eligible = [s for s in eligible if s.action == "buy"]

        for idx, s in enumerate(eligible[:5], start=1):
            log.info(
                f"[TRADE] TOP5_ELIGIBLE #{idx}: {s.symbol} {s.action.upper()} ${s.price:.2f} "
                f"conf={s.confidence:.0%} [{s.strategy}] - {s.reason}"
            )

        save_day_picks(eligible[:5], market_regime)
        notify_scan_results(eligible[:5], datetime.date.today(), sentiment, market_regime)

        if market_regime == "bear":
            long_sigs = [s for s in eligible if s.action == "buy"][:cfg.MARKET_REGIME_SIGNALS_CAP]
            short_candidates = [s for s in eligible if s.action in ("sell", "short")]
            if cfg.LONG_ONLY_MODE:
                if short_candidates:
                    log.warning(f"LONG_ONLY_MODE active — dropping {len(short_candidates)} short candidate(s)")
                short_candidates = []
            if executor.shorting_blocked:
                if short_candidates:
                    log.warning(f"Shorting blocked — dropping {len(short_candidates)} short candidate(s)")
                short_candidates = []
            short_queue = []
            now_ts = time.monotonic()
            expired = [sym for sym, ts in _short_fail_cooldown.items() if ts <= now_ts]
            for sym in expired:
                _short_fail_cooldown.pop(sym, None)
            for s in short_candidates:
                cool_until = _short_fail_cooldown.get(s.symbol, 0.0)
                if cool_until > now_ts:
                    mins_left = (cool_until - now_ts) / 60.0
                    log.info(f"Pre-skip {s.symbol} SHORT: cooldown {mins_left:.1f}m remaining")
                    continue
                try:
                    asset = client.get_asset(s.symbol)
                    raw_status = getattr(asset, "status", "active")
                    status = str(getattr(raw_status, "value", raw_status)).lower()
                    tradable = bool(getattr(asset, "tradable", True))
                    shortable = bool(getattr(asset, "shortable", True))
                    if status != "active" or not tradable or not shortable:
                        log.info(
                            f"Pre-skip {s.symbol} SHORT: "
                            f"status={status}, tradable={tradable}, shortable={shortable}"
                        )
                        _short_fail_cooldown[s.symbol] = max(
                            _short_fail_cooldown.get(s.symbol, 0.0),
                            time.monotonic() + (cfg.SHORT_FAIL_COOLDOWN_MIN * 60),
                        )
                        continue
                except Exception as e:
                    log.warning(f"Pre-check asset failed for {s.symbol}: {e} — keeping candidate")
                short_queue.append(s)

            short_target = 0 if (cfg.LONG_ONLY_MODE or executor.shorting_blocked) else cfg.BEAR_SHORT_SIGNALS_CAP
            log.info(
                f"[TRADE] BEAR execution plan: {len(long_sigs)} long(s) swap-only, "
                f"target {short_target} short(s) from queue {len(short_queue)}"
            )

            for sig in long_sigs:
                _session.refresh_daily_pnl(client)
                if _session.daily_pnl <= _daily_loss_limit:
                    log.warning(
                        f"Daily loss limit hit mid-cycle ({_loss_pct:.0f}% {market_regime}): "
                        f"${_session.daily_pnl:.2f} — halting remaining signals"
                    )
                    break
                log.info(f"[TRADE] EXECUTE: {sig.action.upper()} {sig.symbol} @ ${sig.price:.2f} | {sig.strategy} | {sig.reason}")
                if executor.execute(sig, swap_only=True):
                    _session.trades += 1
                    break
                time.sleep(1)

            short_success = 0
            for sig in short_queue:
                if short_target <= 0 or short_success >= short_target:
                    break
                _session.refresh_daily_pnl(client)
                if _session.daily_pnl <= _daily_loss_limit:
                    log.warning(
                        f"Daily loss limit hit mid-cycle ({_loss_pct:.0f}% {market_regime}): "
                        f"${_session.daily_pnl:.2f} — halting remaining signals"
                    )
                    break
                log.info(f"[TRADE] EXECUTE: {sig.action.upper()} {sig.symbol} @ ${sig.price:.2f} | {sig.strategy} | {sig.reason}")
                if executor.execute(sig, swap_only=False):
                    _session.trades += 1
                    short_success += 1
                    _short_fail_cooldown.pop(sig.symbol, None)
                else:
                    _short_fail_cooldown[sig.symbol] = time.monotonic() + (cfg.SHORT_FAIL_COOLDOWN_MIN * 60)
                    log.info(
                        f"SHORT attempt failed for {sig.symbol} — cooldown {cfg.SHORT_FAIL_COOLDOWN_MIN}m; "
                        "trying next qualified candidate"
                    )
                time.sleep(1)
        else:
            top_signals = sorted(eligible, key=lambda s: s.confidence, reverse=True)[:signals_cap]
            log.info(f"Executing top {len(top_signals)} signal(s) (cap={signals_cap})")
            for sig in top_signals:
                is_short_signal = sig.action in ("sell", "short")
                effective_swap_only = (market_regime == "bear") and not is_short_signal
                _session.refresh_daily_pnl(client)
                if _session.daily_pnl <= _daily_loss_limit:
                    log.warning(
                        f"Daily loss limit hit mid-cycle ({_loss_pct:.0f}% {market_regime}): "
                        f"${_session.daily_pnl:.2f} — halting remaining signals"
                    )
                    break
                log.info(f"EXECUTE: {sig.action.upper()} {sig.symbol} @ ${sig.price:.2f} | {sig.strategy} | {sig.reason}")
                if executor.execute(sig, swap_only=effective_swap_only):
                    _session.trades += 1
                time.sleep(1)
    else:
        log.info("[SCAN] No signals found this cycle")

    


def _fetch_account_and_positions(timeout_seconds: int = 30):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor_thread:
        future = executor_thread.submit(lambda: (client.get_account(), client.get_all_positions()))
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"Account status call timed out after {timeout_seconds}s")


def log_status() -> None:
    try:
        account, positions = _fetch_account_and_positions(timeout_seconds=20)
        log.info("=" * 70)
        log.info("STATUS")
        log.info(f"Equity:     ${float(account.equity):,.2f}")
        log.info(f"Daily P&L:  ${_session.daily_pnl:.2f}  |  Trades: {_session.trades}")
        if cfg.USE_QUARTERLY_TARGET and _session.quarterly_start_equity > 0:
            q_gain = ((float(account.equity) - _session.quarterly_start_equity) / _session.quarterly_start_equity) * 100
            log.info(f"Quarterly:  {q_gain:+.1f}% (target >= {cfg.QUARTERLY_PROFIT_TARGET_PCT:.0f}%")
        log.info(f"Positions:  {len(positions)}")

        if positions:
            total_pnl = sum(float(p.unrealized_pl) for p in positions)
            log.info(f"Unrealized: ${total_pnl:.2f}")
            for p in positions:
                pct = float(p.unrealized_plpc) * 100
                log.info(f"  {p.symbol}: {p.qty} @ ${float(p.avg_entry_price):.2f} "
                         f"| ${float(p.unrealized_pl):.2f} ({pct:+.2f}%)")
        log.info("=" * 70)
    except Exception as e:
        log.error(f"Status error: {e}")


def get_adaptive_interval() -> int:
    if not cfg.ADAPTIVE_INTERVALS:
        return cfg.SCAN_INTERVAL_MIN

    vix = get_vix()
    vix_config = {
        "SCAN_INTERVAL_EXTREME_VOL": cfg.SCAN_INTERVAL_EXTREME_VOL,
        "SCAN_INTERVAL_HIGH_VOL": cfg.SCAN_INTERVAL_HIGH_VOL,
        "SCAN_INTERVAL_MODERATE_VOL": cfg.SCAN_INTERVAL_MODERATE_VOL,
        "SCAN_INTERVAL_NORMAL_VOL": cfg.SCAN_INTERVAL_NORMAL_VOL,
        "SCAN_INTERVAL_CALM_VOL": cfg.SCAN_INTERVAL_CALM_VOL,
        "SCAN_INTERVAL_LOW_VOL": cfg.SCAN_INTERVAL_LOW_VOL,
    }
    vix_interval, vol = get_vix_interval(vix, vix_config)
    interval = vix_interval
    market_phase = "ALL DAY"

    if cfg.USE_MARKET_HOURS_TUNING:
        h = datetime.datetime.now().hour + datetime.datetime.now().minute / 60
        mkt_config = {
            "PREMARKET_SCAN_INTERVAL": cfg.PREMARKET_SCAN_INTERVAL,
            "REGULAR_HOURS_SCAN_INTERVAL": cfg.REGULAR_HOURS_SCAN_INTERVAL,
            "AFTERHOURS_SCAN_INTERVAL": cfg.AFTERHOURS_SCAN_INTERVAL,
        }
        mkt_interval, market_phase = get_market_hours_interval(h, mkt_config)
        if mkt_interval is not None:
            interval = mkt_interval
        else:
            interval = vix_interval

    pos_status = "DISABLED"
    if cfg.USE_POSITION_TUNING:
        try:
            pos_count = len(client.get_all_positions())
            pos_config = {
                "HIGH_POSITION_INTERVAL": cfg.HIGH_POSITION_INTERVAL,
                "NORMAL_POSITION_INTERVAL": cfg.NORMAL_POSITION_INTERVAL,
                "LOW_POSITION_INTERVAL": cfg.LOW_POSITION_INTERVAL,
            }
            pos_interval, pos_status = get_position_tuning_interval(pos_count, pos_config)
            if pos_interval is not None:
                interval = max(interval, pos_interval)
        except Exception as e:
            log.debug(f"Position tuning check failed: {e}")
            pos_status = "POS CHECK ERROR"

    log.info(f"VIX: {vix:.2f} ({vol}) | {market_phase} | {pos_status} | Scan: {interval} min")
    return interval


def _prune_universe_job() -> None:
    """Remove expired universe tickers. Scheduled every 30 min by start()."""
    try:
        from .equity.universe import prune as _prune
        removed = _prune()
        if removed:
            log.info(
                f"Universe pruned: removed {len(removed)} expired ticker(s): "
                f"{removed[:10]}{'\u2026' if len(removed) > 10 else ''}"
            )
        else:
            log.info("Universe pruned: no expired tickers")
    except Exception as e:
        log.warning(f"Universe prune job failed: {e}")


def start() -> None:
    _session.load_quarterly_state()
    log.info("=" * 70)
    log.info("APEXTRADER - Priority-Based Momentum Trading")
    log.info("=" * 70)
    log.info("Strategies: Sweepea | Technical | Momentum")
    log.info(f"Priority 1 (Momentum): {len(cfg.PRIORITY_1_MOMENTUM)} stocks")
    log.info(f"Priority 2 (Established): {len(cfg.PRIORITY_2_ESTABLISHED)} stocks")
    log.info(f"Total Universe: {sum(len(v) for v in cfg.STOCKS.values())} stocks")
    log.info(f"Scan: {'ADAPTIVE (VIX-based)' if cfg.ADAPTIVE_INTERVALS else f'{cfg.SCAN_INTERVAL_MIN} min fixed'}")
    log.info("=" * 70)

    try:
        account = client.get_account()
        log.info(f"Equity:          ${float(account.equity):,.2f}")
        log.info(f"Buying Power:    ${float(account.buying_power):,.2f}")
        log.info(f"PDT Status:      {'Yes' if account.pattern_day_trader else 'No'}")
        log.info(f"Day Trade Count: {account.daytrade_count}")
    except Exception as e:
        log.error(f"Account info error: {e}")

    log.info("=" * 70)
    log.info("Starting… Press Ctrl+C to stop")
    log.info("=" * 70)

    try:
        executor.protect_positions()
    except Exception as e:
        log.error(f"protect_positions initial load error: {e}", exc_info=True)

    if cfg.USE_TRADEIDEAS_DISCOVERY:
        try:
            log.info("Startup TI capture — scheduling universe refresh before first scan (background) …")
            scan_tradeideas_universe()
            if getattr(_discovery, "_ti_future", None) is not None:
                log.info("Waiting up to 90s for startup TI capture to complete before first scan …")
                try:
                    _discovery._ti_future.result(timeout=90)
                except concurrent.futures.TimeoutError:
                    log.warning(
                        "Startup TI capture did not finish within 90s — proceeding with current universe"
                    )
                except Exception as _e:
                    log.warning(f"Startup TI capture failed: {_e}")

            else:
                log.info("Startup TI capture complete — proceeding with current universe")
        except Exception as _e:
            log.warning(f"Startup TI capture failed ({_e}) — proceeding with existing universe")

    try:
        scan_and_trade()
    except Exception as e:
        log.error(f"Initial scan error: {e}", exc_info=True)

    last_vix_check = time.time()
    current_interval = get_adaptive_interval()
    last_scan = time.time()


    schedule.every(30).minutes.do(log_status)
    schedule.every(30).minutes.do(_prune_universe_job)

    try:
        while True:
            try:
                if cfg.ADAPTIVE_INTERVALS and (time.time() - last_vix_check) >= 900:
                    new_interval = get_adaptive_interval()
                    if new_interval != current_interval:
                        log.info(f"Scan interval: {current_interval} → {new_interval} min")
                        current_interval = new_interval
                    last_vix_check = time.time()

                if (time.time() - last_scan) >= (current_interval * 60):
                    try:
                        executor.protect_positions()
                    except Exception as e:
                        log.error(f"protect_positions loop error: {e}", exc_info=True)

                    try:
                        executor.check_software_stops()
                    except Exception as e:
                        log.error(f"check_software_stops loop error: {e}", exc_info=True)

                    try:
                        eod_summary = executor.close_eod_positions()
                    except Exception as e:
                        log.error(f"close_eod_positions loop error: {e}", exc_info=True)
                        eod_summary = None

                    if eod_summary:
                        try:
                            account = client.get_account()
                            positions = client.get_all_positions()
                            notify_eod(eod_summary, account, positions, _session.daily_pnl, _session.trades, _discovery.trending_stocks)
                        except Exception as e:
                            log.error(f"EOD account fetch error: {e}", exc_info=True)

                    try:
                        scan_and_trade()
                    except Exception as e:
                        log.error(f"Scan cycle error: {e}", exc_info=True)

                    last_scan = time.time()
                    log.info(f"Heartbeat: scan cycle completed at {datetime.datetime.now().isoformat()}")

                schedule.run_pending()
                time.sleep(5)

            except KeyboardInterrupt:
                log.info("Stopped by user")
                log_status()
                break
            except Exception as e:
                log.error(f"Unexpected main loop error: {e}", exc_info=True)
                time.sleep(10)
    except KeyboardInterrupt:
        log.info("Stopped by user")
        log_status()


def run(*, force: bool = False, once: bool = False, top3_only: bool = False) -> None:
    if force:
        cfg.FORCE_SCAN = True
    if top3_only:
        log.info("APEXTRADER — Top3 scan mode")
        scan_top3_only()
        log_status()
        return
    if once:
        log.info("=" * 70)
        log.info("APEXTRADER — Single Scan Cycle (GitHub Actions)")
        log.info("=" * 70)
        scan_and_trade()
        log_status()
        return
    start()

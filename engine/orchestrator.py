"""
ApexTrader orchestrator -- Stage 3 refactor.

scan_and_trade() decomposed into focused private functions:
  _run_discovery()         -- all universe refresh sources
  _resolve_market_regime() -- regime detection with safe fallback
  _build_scan_targets()    -- universe assembly + position filtering
  _filter_eligible()       -- confidence gate + long-only enforcement
  _log_skipped()           -- skip diagnostics for top-10 non-qualifiers
  _execute_bear_plan()     -- bear regime: 1 swap-long + N shorts with cooldown
  _execute_bull_plan()     -- bull regime: top-N by confidence
  _build_short_queue()     -- pre-screen shorts (tradability + cooldown)

AppContext dataclass holds all runtime singletons so they are never
instantiated at import time -- importing this module no longer opens a
broker connection.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import schedule
import pytz
REPO_ROOT = Path(__file__).resolve().parent.parent

def _execution_rank(signal: Signal) -> tuple:
    """Keep default basket signals behind ordinary day-scan signals."""
    return (signal.symbol in cfg.LOW_PRIORITY_SCAN_SYMBOLS, -signal.confidence)

from . import config as cfg
from .utils import (
    setup_logging,
    MarketState,
    within_entry_window,
    get_finnhub_trending_tickers,
    get_market_hours_interval,
    get_position_tuning_interval,
    get_vix_interval,
    get_live_holdings,
)
from .equity.strategies import Signal
from .equity.scan import get_scan_targets, scan_universe
from .equity.universe import filter_universe_by_positions
from .equity import discovery as _discovery
from .notifications import notify_scan_results, notify_eod, send_email
from .predictions import save_day_picks

# 2026-08-18, user request (daily P&L stuck at $0.00 all session despite real
# trades/losses): `from . import session as _session` bound _session to the
# PACKAGE (engine/session/__init__.py), which does `from .session import
# daily_pnl, daily_start_equity, ...` -- a one-time VALUE COPY at first
# import, frozen at whatever the submodule's globals held at that instant
# (0.0/0.0/None, before load_daily_state()/reset_daily() ever ran). Every
# later refresh_daily_pnl()/reset_daily() call still only mutates the
# SUBMODULE's own globals (that's where `global daily_pnl` resolves, since
# that's where the function is defined) -- invisible through the package's
# already-frozen copy. Reproduced: mutating engine.session.session.daily_pnl
# left engine.session.daily_pnl at 0.0. Importing the submodule object
# itself instead of the package makes every _session.X read/write live.
#
# Same bug silently disabled the daily loss-limit halt: daily_loss_limit at
# line ~558 is `-(_session.daily_start_equity * loss_pct/100) if
# _session.daily_start_equity > 0 else -999_999` -- daily_start_equity was
# always 0.0 through this alias, so the guard always fell to -999_999 and
# could never trip regardless of real drawdown. Also silently suppressed
# log_status()'s "Quarterly:" line (same `_session.quarterly_start_equity >
# 0` pattern) even though session.py's OWN check_quarterly() printed a
# correct "Quarterly P&L:" line right next to it all along, using its local
# global instead of this alias.
from .session import session as _session
from engine.broker.broker_factory import BrokerFactory
from engine.execution.enhanced import EnhancedExecutor
from engine.risk import kill_mode as _kill_mode

log = setup_logging()

import logging as _logging
_logging.getLogger("WDM").setLevel(_logging.ERROR)
_logging.getLogger("webdriver_manager").setLevel(_logging.ERROR)

# 2026-08-27, user request ("improve the 1min checks to have better
# reliability as the whole logic is dependent on it"): liveness tracking
# for the SoftwareStopPoller thread -- see _start_software_stop_thread and
# _poller_staleness_job.
#
# Same date, separate issue ("why the 3mins web scrapping is not
# happening"): TI capture liveness/trigger state -- see _ti_capture_job.
# 2026-08-28: source switched from TI's Selenium/Edge scrape to Yahoo
# Finance (plain HTTP, engine/ti/yahoo_universe.py) -- no more subprocess/
# wedge tracking needed, just the interval gate.
_last_ti_capture_ts: float = 0.0
_last_poller_tick: float = 0.0
_poller_stale_alerted: bool = False


# -- AppContext ----------------------------------------------------------------
# Holds all runtime singletons. Instantiated once inside start()/run() so
# importing this module never opens a broker connection.

@dataclass
class AppContext:
    client:           object
    executor:         EnhancedExecutor
    # Per-session state
    last_market_regime:   str  = "bull"
    market_state:         Optional[MarketState] = None
    # 2026-09-02: date (ET) for which the guardian flat flag has already been
    # acted on (guardian_halt_flatten already ran) so the 5s poll tick only
    # flattens once per guardian trip. Date-scoped rather than a bool: the
    # watchdog keeps main.py alive across midnight (EOD flat -> 09:05 prep),
    # so a process-lifetime bool would silently block the NEXT day's
    # legitimate guardian flatten until a manual restart.
    guardian_halt_acted_date: Optional[datetime.date] = None
    # Latest ranked signals eligible for five-second capital-utilization retries.
    top_entry_signals:    List[Signal] = field(default_factory=list)
    # Short-fail cooldown: {symbol: monotonic_ts_until_retry}
    # Merged here from the old module-level global so it survives restarts
    # via executor._htb_cache and is accessible to the bear plan.
    short_fail_cooldown: dict = field(default_factory=dict)


def _build_context() -> AppContext:
    """Create and wire all runtime singletons. Called once at startup."""
    client   = BrokerFactory.create_stock_client(cfg.STOCKS_BROKER)
    executor = EnhancedExecutor(client, use_bracket_orders=True)
    log.info(f"Trade mode: {cfg.TRADE_MODE} (PAPER={cfg.PAPER}, LIVE={cfg.LIVE})")
    if not cfg.LONG_ONLY_MODE:
        log.info("Shorting enabled (LONG_ONLY_MODE=False)")
    return AppContext(client=client, executor=executor)


# -- Discovery wrappers --------------------------------------------------------
# Thin wrappers that forward config into discovery -- keeps scan_and_trade lean.

def _timed(label: str, fn, *args, **kwargs) -> None:
    """Call fn(*args, **kwargs) and log its wall time under [TIMING] <label>."""
    t0 = time.monotonic()
    try:
        fn(*args, **kwargs)
    finally:
        log.info(f"[TIMING]   {label}: {time.monotonic() - t0:.1f}s")


def _run_discovery(ctx: AppContext, market_state: MarketState) -> None:
    """Fire all configured universe refresh sources (each throttled internally)."""
    _timed("yahoo_universe", _ti_capture_job, market_state.now)
    _timed("trending_stocks", _discovery.scan_trending_stocks,
        use_live_trending=cfg.USE_LIVE_TRENDING,
        use_finnhub=cfg.USE_FINNHUB_DISCOVERY,
        use_sentiment_gate=cfg.USE_SENTIMENT_GATE,
        trending_max=cfg.TRENDING_MAX_RESULTS,
        trending_interval_min=cfg.TRENDING_SCAN_INTERVAL,
        trending_min_momentum=cfg.TRENDING_MIN_MOMENTUM,
        priority_1=cfg.PRIORITY_1_MOMENTUM,
    )
    _timed("edgar", _discovery.scan_edgar,
        edgar_enabled=cfg.USE_EDGAR_SCANNER,
        edgar_interval_min=cfg.EDGAR_SCANNER_INTERVAL_MIN,
        priority_1=cfg.PRIORITY_1_MOMENTUM,
        priority_2=cfg.PRIORITY_2_ESTABLISHED,
    )
    _timed("alpaca_movers", _discovery.scan_alpaca_movers,
        interval_min=cfg.ALPACA_MOVER_SCAN_INTERVAL_MIN,
        market_state=market_state,
    )
    _timed("preopen_intelligence", _discovery.scan_preopen_intelligence,
        enabled=cfg.USE_PREOPEN_INTELLIGENCE,
        interval_min=cfg.PREOPEN_INTELLIGENCE_SCAN_INTERVAL_MIN,
        market_state=market_state,
        priority_1=cfg.PRIORITY_1_MOMENTUM,
        priority_2=cfg.PRIORITY_2_ESTABLISHED,
        max_watchlist=cfg.PREOPEN_INTELLIGENCE_MAX_TICKERS,
        use_regime_gating=cfg.PREOPEN_USE_REGIME_GATING,
        use_sentiment_gating=cfg.PREOPEN_USE_SENTIMENT_GATING,
    )


# -- Market regime -------------------------------------------------------------

def _resolve_market_regime(ctx: AppContext, market_state: MarketState) -> Tuple[str, int]:
    """Return the stock-only execution capacity; broad-market regime is ignored."""
    return "stock", cfg.MAX_LONG_ENTRIES_PER_CYCLE + cfg.MAX_SHORT_ENTRIES_PER_CYCLE


# -- Universe assembly ---------------------------------------------------------

def _build_scan_targets(ctx: AppContext) -> Tuple[List[str], set]:
    """Return (scan_targets, excluded) after universe assembly and position filtering."""
    _, _, excluded = get_live_holdings(ctx.client)
    # 2026-08-18, user request: a post-loss cooldown symbol is no longer kept
    # out of the scan entirely -- _create_bracket_order now routes any signal
    # for it through a trailing buy (see is_reentry) instead of the normal
    # marketable chase, so it's safe to let it be re-scanned/re-signaled.
    # _validate_trade's cooldown check was removed the same way -- see there
    # for the SOXS precedent (22 rapid re-entries, -$605) this replaces.
    targets = filter_universe_by_positions(get_scan_targets(market_state=ctx.market_state), excluded)
    log.info(
        f"[SCAN] {len(targets)} symbols (filtered, {cfg.SCAN_WORKERS} workers): "
        f"{', '.join(targets)}"
    )
    return targets, excluded


# -- Signal filtering ----------------------------------------------------------

def _filter_eligible(
    ctx: AppContext,
    signals: list,
    fresh_held: set,
    regime: str,
) -> list:
    """Apply confidence gate, position cross-ref, and long-only enforcement.

    Returns the eligible signal list ready for execution.
    """
    short_min_conf = cfg.MIN_SIGNAL_CONFIDENCE
    long_only      = cfg.LONG_ONLY_MODE or ctx.executor.shorting_blocked

    if ctx.executor.shorting_blocked and not cfg.LONG_ONLY_MODE:
        log.warning("Shorting blocked by broker (40310000) -- effective long-only this session")

    # Same-underlying guard: don't buy two leveraged siblings of the same
    # commodity/index/stock in one cycle (e.g. BOIL+KOLD, or AAPU alongside
    # held AAPL) -- see leveraged_underlying() in config.py.
    picked_underlyings = {cfg.leveraged_underlying(sym) for sym in fresh_held}

    eligible = []
    for s in signals:
        if s.symbol in fresh_held:
            continue
        underlying = cfg.leveraged_underlying(s.symbol)
        if underlying in picked_underlyings:
            continue
        conf = round(float(s.confidence), 2)
        if s.action == "buy" and conf >= cfg.MIN_SIGNAL_CONFIDENCE:
            eligible.append(s)
            picked_underlyings.add(underlying)
        elif (
            s.action in ("sell", "short")
            and not long_only
            and conf >= short_min_conf
        ):
            eligible.append(s)
            picked_underlyings.add(underlying)

    # Strip shorts when effectively long-only
    if long_only:
        eligible = [s for s in eligible if s.action == "buy"]

    # Long-only fallback: if nothing qualifies, pick the best buy above min conf
    if long_only and not eligible:
        fallback = next(
            (s for s in signals
             if s.action == "buy"
             and s.symbol not in fresh_held
             and cfg.leveraged_underlying(s.symbol) not in picked_underlyings
             and round(float(s.confidence), 2) >= cfg.MIN_SIGNAL_CONFIDENCE),
            None,
        )
        if fallback:
            log.warning(
                f"Long-only fallback: {fallback.symbol} buy @ ${fallback.price:.2f} "
                f"conf={fallback.confidence:.0%}"
            )
            eligible = [fallback]

    log.info(
        f"Confidence gate (long>={cfg.MIN_SIGNAL_CONFIDENCE:.0%}, "
        f"short>={short_min_conf:.0%}) + cross-ref: {len(eligible)} eligible"
    )
    return eligible


def _log_skipped(signals: list, eligible: list, fresh_held: set, regime: str, executor: EnhancedExecutor) -> None:
    """Log skip reason for each top-10 raw signal that did not make it to eligible."""
    short_min_conf = cfg.MIN_SIGNAL_CONFIDENCE
    eligible_syms  = {s.symbol for s in eligible}
    eligible_underlyings = {cfg.leveraged_underlying(sym) for sym in fresh_held} | \
                            {cfg.leveraged_underlying(s.symbol) for s in eligible}
    top10          = sorted(signals, key=lambda s: s.confidence, reverse=True)[:10]
    for s in top10:
        if s.symbol in eligible_syms:
            continue
        conf = round(float(s.confidence), 2)
        if s.symbol in fresh_held:
            reason = "already held/ordered"
        elif cfg.leveraged_underlying(s.symbol) in eligible_underlyings:
            reason = f"same underlying ({cfg.leveraged_underlying(s.symbol)}) already held/picked"
        elif s.action == "buy" and conf < cfg.MIN_SIGNAL_CONFIDENCE:
            reason = f"conf {conf:.0%} < long min {cfg.MIN_SIGNAL_CONFIDENCE:.0%}"
        elif s.action in ("sell", "short") and conf < short_min_conf:
            reason = f"conf {conf:.0%} < short min {short_min_conf:.0%}"
        elif executor.shorting_blocked and s.action in ("sell", "short"):
            reason = "shorting blocked by broker"
        elif cfg.LONG_ONLY_MODE and s.action != "buy":
            reason = "long-only mode"
        else:
            reason = "filtered"
        log.info(f"[SCAN] SKIP {s.symbol} {s.action.upper()} ${s.price:.2f} conf={s.confidence:.0%} [{s.strategy}] -- {reason}")


# -- Short pre-screening -------------------------------------------------------

def _build_short_queue(ctx: AppContext, short_candidates: list) -> list:
    """Pre-screen short candidates: remove cooldown hits and non-shortable assets.

    Returns the filtered short_queue ready for bear execution.
    """
    now_ts = time.monotonic()
    # Prune expired cooldowns
    expired = [sym for sym, ts in ctx.short_fail_cooldown.items() if ts <= now_ts]
    for sym in expired:
        ctx.short_fail_cooldown.pop(sym, None)

    queue = []
    for s in short_candidates:
        cool_until = ctx.short_fail_cooldown.get(s.symbol, 0.0)
        if cool_until > now_ts:
            log.info(f"Pre-skip {s.symbol} SHORT: cooldown {(cool_until - now_ts) / 60:.1f}m remaining")
            continue
        try:
            asset     = ctx.client.get_asset(s.symbol)
            status    = str(getattr(getattr(asset, "status", "active"), "value", getattr(asset, "status", "active"))).lower()
            tradable  = bool(getattr(asset, "tradable",  True))
            shortable = bool(getattr(asset, "shortable", True))
            if status != "active" or not tradable or not shortable:
                log.info(f"Pre-skip {s.symbol} SHORT: status={status} tradable={tradable} shortable={shortable}")
                ctx.short_fail_cooldown[s.symbol] = now_ts + cfg.SHORT_FAIL_COOLDOWN_MIN * 60
                continue
        except Exception as e:
            log.warning(f"Pre-check asset failed {s.symbol}: {e} -- keeping candidate")
        queue.append(s)
    return queue


# -- Execution plans -----------------------------------------------------------

def _execute_bull_plan(
    ctx: AppContext,
    eligible: list,
    signals_cap: int,
    regime: str,
    daily_loss_limit: float,
    loss_pct: float,
) -> None:
    """Bull (or neutral) regime: try eligible signals ranked by confidence,
    highest first, until signals_cap of them actually SUCCEED (or the list
    runs out) -- not just attempt the top signals_cap once each.

    2026-08-14, user request ("we should have seen multiple stock picks"):
    the old version sliced to the top signals_cap candidates BEFORE
    attempting anything, so if the top-ranked ones all failed for any reason
    (momentum freshness, hard-to-borrow, insufficient buying power...) the
    cycle wasted its whole budget on failures and never even looked at the
    next-ranked candidates. Confirmed live: 5 signals at 96-97% confidence,
    cap=3, the top 3 all failed and the other 2 (BRUN, LFS) were never
    tried. Same risk cap as before (still at most signals_cap new
    positions) -- this only stops giving up early on failures that were
    never going to fill anyway. Mirrors the pattern _execute_bear_plan's
    short queue already used correctly (short_success counter, not a
    pre-slice)."""
    ranked = sorted(eligible, key=_execution_rank)
    log.info(f"Executing up to {signals_cap} signal(s) from {len(ranked)} eligible (cap={signals_cap})")
    executed = 0
    long_executed = 0
    short_executed = 0
    for sig in ranked:
        if executed >= min(signals_cap, cfg.MAX_LONG_ENTRIES_PER_CYCLE + cfg.MAX_SHORT_ENTRIES_PER_CYCLE):
            break
        if sig.action == "buy" and long_executed >= cfg.MAX_LONG_ENTRIES_PER_CYCLE:
            continue
        if sig.action in ("sell", "short") and short_executed >= cfg.MAX_SHORT_ENTRIES_PER_CYCLE:
            continue
        swap_only = (regime == "bear") and sig.action not in ("sell", "short")
        _session.refresh_daily_pnl(ctx.client)
        if _session.daily_pnl <= daily_loss_limit:
            log.warning(f"Daily loss limit mid-cycle ({loss_pct:.0f}%): ${_session.daily_pnl:.2f} -- halting")
            break
        log.info(f"EXECUTE: {sig.action.upper()} {sig.symbol} @ ${sig.price:.2f} | {sig.strategy} | {sig.reason}")
        if ctx.executor.execute(sig, swap_only=swap_only):
            _session.trades += 1
            executed += 1
            if sig.action == "buy":
                long_executed += 1
            elif sig.action in ("sell", "short"):
                short_executed += 1
        time.sleep(1)


def _retry_top_entries(ctx: AppContext) -> None:
    """Retry the latest top eligible signals on the five-second poller.

    Every attempt goes through EnhancedExecutor.execute(), so the normal hard
    entry validation, including fresh EMA alignment and duplicate-order guard,
    remains authoritative. This only retries candidates that failed or were
    temporarily unaffordable during the last scan; it does not rescan prices.
    """
    top_entry_signals = getattr(ctx, "top_entry_signals", [])
    if not top_entry_signals:
        return

    now_et = datetime.datetime.now(pytz.timezone("America/New_York"))
    if not _within_entry_window(now_et):
        return

    _session.refresh_daily_pnl(ctx.client)
    last_market_regime = getattr(ctx, "last_market_regime", "bull")
    loss_pct = cfg.DAILY_LOSS_LIMIT_BEAR_PCT if last_market_regime == "bear" else cfg.DAILY_LOSS_LIMIT_BULL_PCT
    daily_loss_limit = -(_session.daily_start_equity * loss_pct / 100) if _session.daily_start_equity > 0 else -999_999
    if _session.daily_pnl <= daily_loss_limit or _session.daily_pnl >= cfg.DAILY_PROFIT_TARGET:
        return

    for signal in list(top_entry_signals):
        try:
            if ctx.executor.execute(signal):
                _session.trades += 1
                log.info(f"[5S RETRY] EXECUTED {signal.action.upper()} {signal.symbol} after hard checks")
        except Exception as e:
            log.warning(f"[5S RETRY] {signal.symbol} failed: {e}")


# -- Core scan cycle -----------------------------------------------------------

def _check_kill_mode(ctx: AppContext) -> bool:
    return _kill_mode.check(
        ctx.client, ctx.executor,
        vix_level=cfg.KILL_MODE_VIX_LEVEL,
        spy_drop_pct=cfg.KILL_MODE_SPY_DROP_PCT,
        vix_roc_pct=cfg.KILL_MODE_VIX_ROC_PCT,
    )


def _guardian_flat_requested() -> Optional[dict]:
    """Read the loss guardian's flat_request.flag (written by scripts/
    guardian.py when the hard daily-loss backstop trips).

    Returns the payload ONLY when it is dated TODAY (ET). A stale flag from a
    prior day must never flatten the current day -- the guardian rewrites the
    file fresh (dated today) if it trips again.
    """
    try:
        import json as _json
        f = Path(cfg.GUARDIAN_FLAT_FILE)
        if not f.exists():
            return None
        payload = _json.loads(f.read_text(encoding="utf-8"))
        today_et = datetime.datetime.now(pytz.timezone("America/New_York")).date()
        if str(payload.get("date")) != today_et.isoformat():
            return None
        return payload
    except Exception as e:
        log.warning(f"guardian flat flag read failed: {e}")
        return None


_last_hb_touch = 0.0


def _touch_heartbeat(force: bool = False) -> None:
    """Keep heartbeat.txt meaning MAIN-LOOP LIVENESS, not cycle completion.

    2026-09-02 red-team fix for the watchdog stall-restart loop (19:23-21:30
    ET): heartbeat was written only after each scan cycle, and off-hours the
    adaptive interval stretches to 20 min (SCAN_INTERVAL_CALM_VOL) -- longer
    than the watchdog's 900s STALL_RESTART_SECONDS -- so a perfectly healthy
    sleeping bot was killed as "hung" every ~15 min, all night. The main loop
    now touches the heartbeat on every tick (rate-limited to one write per
    60s; force=True bypasses the limiter after a completed scan cycle), so:
      - a genuine hang (e.g. a black-holed bar fetch) still stops the writes
        and is caught by the watchdog within <= 15 min;
      - legitimate long adaptive sleeps no longer trip it.
    Content stays a plain UTC ISO timestamp (watchdog + guardian read it).
    """
    global _last_hb_touch
    now_mono = time.monotonic()
    if not force and (now_mono - _last_hb_touch) < 60.0:
        return
    try:
        (REPO_ROOT / "heartbeat.txt").write_text(
            datetime.datetime.now(datetime.timezone.utc).isoformat(), encoding="utf-8"
        )
        _last_hb_touch = now_mono
    except Exception as e:
        log.warning(f"heartbeat.txt write failed: {e}")


def _maybe_guardian_halt(ctx) -> Optional[str]:
    """Act on the guardian flat flag at most once per flag date (ET).

    Called first on every _tick (the 5s software-stop poll thread) so a halt
    flattens the book even while scan_and_trade is mid-cycle. Dedupe is
    DATE-scoped on the flag payload's own "date" (which _guardian_flat_requested
    already guarantees is today ET): repeated same-day polls are a no-op, while
    a fresh next-day flag flattens again without a process restart -- a
    process-lifetime bool would stay stuck across the overnight session.
    If the payload date is missing/unparsable, fall back to today's ET date so
    dedupe still holds. Module-level (not inlined in _tick) so it is directly
    testable with a mock ctx. Returns the flatten reason when a flatten was
    just triggered, else None.
    """
    try:
        _gf = _guardian_flat_requested()
        if not _gf:
            return None
        try:
            flag_date = datetime.date.fromisoformat(str(_gf.get("date", "")))
        except Exception:
            flag_date = datetime.datetime.now(pytz.timezone("America/New_York")).date()
        if getattr(ctx, "guardian_halt_acted_date", None) == flag_date:
            return None  # already flattened for this guardian trip
        ctx.guardian_halt_acted_date = flag_date
        log.warning(
            f"[GUARDIAN-HALT] flat flag fired (pnl ${_gf.get('pnl')}, "
            f"{_gf.get('pct')}%, reason={_gf.get('reason')}) -- flattening"
        )
        ctx.executor.guardian_halt_flatten(
            f"guardian flat @ {_gf.get('pct')}% (${_gf.get('pnl')}) {_gf.get('reason')}"
        )
        return str(_gf.get("reason") or "guardian")
    except Exception as e:
        log.error(f"[STOP-THREAD] guardian-halt check error: {e}", exc_info=True)
        return None


def _within_entry_window(now_et: datetime.datetime) -> bool:
    """True if now_et (ET, tz-aware) falls within either entry segment:
    [ENTRY_WINDOW_START_ET, ENTRY_WINDOW_BREAK_START_ET] (09:14-11:00) or
    [ENTRY_WINDOW_BREAK_END_ET, ENTRY_WINDOW_END_ET] (14:45-15:50) -- the
    two-segment window added 2026-09-01 (user request: "time for entry 9:14AM
    to 11:00AM and 2:45 PM to 3:50PM ET"), separated by a midday break when
    the book is hard-flatted. Pure string-time comparison, same pattern
    MarketState.from_now() already uses for is_market_open/is_regular_hours.
    Delegates to engine.utils.market.within_entry_window (single source of
    truth -- enhanced.py's re-entry guards use the same helper); this thin
    wrapper keeps the historical name for existing callers/tests."""
    return within_entry_window(now_et)


def _within_discovery_window(now_et: datetime.datetime) -> bool:
    """True if now_et falls within [DISCOVERY_WINDOW_START_ET,
    ENTRY_WINDOW_END_ET] -- the wider band universe discovery (TI-capture
    trigger + scan_alpaca_movers, both inside _run_discovery) is allowed to
    run in. Strictly wider than _within_entry_window on the early side only
    (DISCOVERY_WINDOW_START_ET < ENTRY_WINDOW_START_ET): discovery gets a
    pre-market head start so the scan universe is already warm by the time
    ENTRY_WINDOW_START_ET opens order submission, per scan_and_trade()'s
    two-stage gate. Since 2026-09-01 it is ALSO wider through the midday
    break (11:00-14:45) -- discovery keeps refreshing through the lunch flat
    so the afternoon entry segment (14:45-15:50) trades on a warm universe.
    Never wider on the late side -- discovery has no reason to outlive the
    entry window itself."""
    t = now_et.strftime("%H:%M")
    return cfg.DISCOVERY_WINDOW_START_ET <= t <= cfg.ENTRY_WINDOW_END_ET


def _margin_cushion_ok(equity: float, maintenance_margin: float, min_ratio: float) -> bool:
    """True if equity is still >= min_ratio x maintenance_margin (safe cushion
    against an Alpaca maintenance margin call). No margin exposure at all
    (maintenance_margin <= 0) is always safe -- nothing to protect against."""
    if maintenance_margin <= 0:
        return True
    return equity >= min_ratio * maintenance_margin


def scan_and_trade(ctx: AppContext) -> None:
    """One complete scan-and-trade cycle.

    Sequence:
      1. Session reset / daily guards
      2. Market-hours + kill-mode gates
      3. Session P&L guards
      4. Discovery refresh
      5. Universe assembly + scan
      6. Signal filtering
      7. Execution (bear or bull plan)
    """
    _cycle_start = time.monotonic()
    ctx.top_entry_signals = []
    _session.reset_daily(ctx.client)

    ctx.market_state = MarketState.from_now()
    ctx.market_state.resolve_regime()
    ctx.executor.update_market_state(ctx.market_state)
    # 2026-09-01: the options scan cycle was removed entirely (see git history
    # for _run_options_cycle / _start_options_scan_thread) -- once step 2 of
    # this function, later a dedicated thread, and before that a minutes-long
    # sequential options scan (160 tickers) that routinely blew past a minute
    # and starved equity re-entries.

    market_state = ctx.market_state
    if not market_state.is_market_open:
        if not cfg.FORCE_SCAN:
            log.info("[SYSTEM] Market closed -- skipping scan")
            return
        log.warning("[SYSTEM] FORCE_SCAN active -- bypassing market-hours gate")

    # Kill mode has real protective side effects (emergency close on an
    # extreme-bear trigger) beyond just gating entries, so it must run
    # unconditionally here -- the entry-window check below only ever blocks
    # new entries and must not stand in front of it.
    if _check_kill_mode(ctx):
        log.info("[SYSTEM] Kill mode active -- aborting cycle")
        return

    # 2026-09-02: guardian hard daily-loss halt (flat_request.flag). Read every
    # cycle -- cheap local file stat -- so entries stop the moment the guardian
    # trips, even if the 5s poll thread's flatten is still mid-sweep.
    if _guardian_flat_requested():
        log.warning("[SYSTEM] Guardian daily-loss halt active -- skipping discovery/scan/entries this cycle")
        return

    if not _within_discovery_window(market_state.now):
        log.info(
            f"[SYSTEM] Outside discovery window ({cfg.DISCOVERY_WINDOW_START_ET}-{cfg.ENTRY_WINDOW_END_ET} ET) "
            f"-- skipping discovery/scan this cycle (concentration/correlation checks run on their own "
            f"schedule now, unaffected by this gate -- see _concentration_check_job)"
        )
        return

    _session.refresh_daily_pnl(ctx.client)
    loss_pct          = cfg.DAILY_LOSS_LIMIT_BEAR_PCT if ctx.last_market_regime == "bear" else cfg.DAILY_LOSS_LIMIT_BULL_PCT
    daily_loss_limit  = -(_session.daily_start_equity * loss_pct / 100) if _session.daily_start_equity > 0 else -999_999

    if _session.daily_pnl <= daily_loss_limit:
        log.warning(f"[SYSTEM] Daily loss limit ({loss_pct:.0f}% {ctx.last_market_regime}): ${_session.daily_pnl:.2f} -- halting")
        return
    if _session.daily_pnl >= cfg.DAILY_PROFIT_TARGET:
        log.info(f"[SYSTEM] Daily profit target reached: ${_session.daily_pnl:.2f}")
        return

    _session.check_quarterly(ctx.client, cfg.USE_QUARTERLY_TARGET, cfg.QUARTERLY_PROFIT_TARGET_PCT)

    sentiment = market_state.resolve_sentiment()
    log.info(f"[SCAN] Market sentiment: {sentiment}")

    ctx.executor.update_stale_orders()
    ctx.executor.check_tp_targets()

    acct = ctx.executor._get_account()
    min_needed = (
        cfg.SMALL_ACCOUNT_MIN_POSITION_DOLLARS if acct.equity < cfg.SMALL_ACCOUNT_EQUITY_THRESHOLD
        else cfg.MIN_POSITION_DOLLARS
    )
    if acct.buying_power < min_needed:
        log.info(
            f"[SYSTEM] Buying power ${acct.buying_power:,.0f} < minimum position ${min_needed:,.0f} "
            f"-- skipping discovery/scan this cycle (existing stops/TP/concentration checks still ran above)"
        )
        return

    if cfg.MARGIN_SAFEGUARD_ENABLED and not _margin_cushion_ok(acct.equity, acct.maintenance_margin, cfg.MARGIN_CUSHION_MIN_RATIO):
        cushion_ratio = (acct.equity / acct.maintenance_margin) if acct.maintenance_margin > 0 else float("inf")
        log.warning(
            f"[SYSTEM] Margin cushion {cushion_ratio:.2f}x < {cfg.MARGIN_CUSHION_MIN_RATIO}x minimum "
            f"(equity ${acct.equity:,.0f} vs maintenance ${acct.maintenance_margin:,.0f}) "
            f"-- skipping discovery/scan this cycle (existing stops/TP/concentration checks still ran above)"
        )
        return

    _t_discovery = time.monotonic()
    _run_discovery(ctx, market_state)
    log.info(f"[TIMING] discovery: {time.monotonic() - _t_discovery:.1f}s")

    prep_start = datetime.datetime.strptime(cfg.PREP_SCAN_START_ET, "%H:%M").time()
    in_prep_window = prep_start <= market_state.now.time() < datetime.datetime.strptime(cfg.ENTRY_WINDOW_START_ET, "%H:%M").time()
    if not _within_entry_window(market_state.now) and not in_prep_window:
        log.info(
            f"[SYSTEM] Pre-market discovery only (entry window opens {cfg.ENTRY_WINDOW_START_ET} ET) "
            f"-- universe refreshed, no scan/execute this cycle"
        )
        return

    scan_targets, excluded = _build_scan_targets(ctx)
    if not scan_targets:
        log.info("[SCAN] No targets after filtering -- skipping scan")
        return

    # A full strategy/EMA preparation scan is intentionally performed once per
    # day. Repeating a ~100-second scan every minute would overlap the 09:30
    # entry boundary and delay the first executable cycle.
    if in_prep_window and getattr(ctx, "_prep_scan_date", None) == market_state.now.date():
        log.info("[SYSTEM] Preparation already complete; waiting for entry window")
        return

    ctx.executor._swap_cycle_closed.clear()
    regime, signals_cap = _resolve_market_regime(ctx, market_state)

    _t_scan = time.monotonic()
    signals, hit_counts, scan_errors = scan_universe(scan_targets, sentiment, market_state)
    _scan_elapsed = time.monotonic() - _t_scan
    log.info(f"[TIMING] scan_universe: {_scan_elapsed:.1f}s for {len(scan_targets)} symbols ({_scan_elapsed/max(len(scan_targets),1):.2f}s/symbol)")

    if cfg.LONG_ONLY_MODE:
        pre = len(signals)
        signals = [s for s in signals if s.action == "buy"]
        log.warning(f"LONG_ONLY_MODE: filtered {pre} -> {len(signals)} (buy-only)")

    breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(hit_counts.items()))
    log.info(f"[SCAN] Breakdown -- {breakdown or 'none'} | Errors: {scan_errors} | Total: {len(signals)}")
    if not hit_counts:
        if not market_state.is_market_open:
            log.info("[SCAN] No signals -- after hours (stale daily bars, intraday gates not met)")
        else:
            log.info("[SCAN] No signals -- market likely in downtrend or momentum gates not met")

    for idx, s in enumerate(sorted(signals, key=lambda s: s.confidence, reverse=True)[:cfg.TOP_N_SIGNALS], 1):
        log.info(f"[SCAN] TOP{cfg.TOP_N_SIGNALS}_RAW #{idx}: {s.symbol} {s.action.upper()} ${s.price:.2f} conf={s.confidence:.0%} [{s.strategy}] -- {s.reason}")

    if not signals:
        log.info("[SCAN] No signals this cycle")
        return

    _, _, fresh_held = get_live_holdings(ctx.client)
    fresh_held = fresh_held or excluded
    log.info(f"Live holdings: {len(fresh_held)} excluded")

    eligible = _filter_eligible(ctx, signals, fresh_held, regime)
    _log_skipped(signals, eligible, fresh_held, regime, ctx.executor)

    for idx, s in enumerate(eligible[:cfg.TOP_N_SIGNALS], 1):
        log.info(f"[TRADE] TOP{cfg.TOP_N_SIGNALS}_ELIGIBLE #{idx}: {s.symbol} {s.action.upper()} ${s.price:.2f} conf={s.confidence:.0%} [{s.strategy}] -- {s.reason}")

    save_day_picks(eligible[:cfg.TOP_N_SIGNALS], regime)
    notify_scan_results(eligible[:cfg.TOP_N_SIGNALS], datetime.date.today(), sentiment, regime)

    if not eligible:
        log.info("[SCAN] No eligible signals after filtering")
        return

    ctx.top_entry_signals = list(eligible[:cfg.TOP_N_SIGNALS])

    _t_exec = time.monotonic()
    if in_prep_window:
        ctx._prep_scan_date = market_state.now.date()
        log.info(f"[SYSTEM] Preparation scan complete ({len(eligible)} eligible); orders remain blocked until {cfg.ENTRY_WINDOW_START_ET} ET")
        return
    _execute_bull_plan(ctx, eligible, signals_cap, regime, daily_loss_limit, loss_pct)
    log.info(
        f"[TIMING] signal->order: {time.monotonic() - _t_exec:.1f}s | "
        f"total cycle: {time.monotonic() - _cycle_start:.1f}s"
    )


# -- Status + interval helpers -------------------------------------------------

def _fetch_account_and_positions(ctx: AppContext, timeout_seconds: int = 30):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: (ctx.client.get_account(), ctx.client.get_all_positions()))
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"Account status call timed out after {timeout_seconds}s")


def log_status(ctx: AppContext) -> None:
    try:
        account, positions = _fetch_account_and_positions(ctx, timeout_seconds=20)
        log.info("=" * 70)
        log.info("STATUS")
        log.info(f"Equity:     ${float(account.equity):,.2f}")
        log.info(f"Daily P&L:  ${_session.daily_pnl:.2f}  |  Trades: {_session.trades}")
        if cfg.USE_QUARTERLY_TARGET and _session.quarterly_start_equity > 0:
            q_gain = ((float(account.equity) - _session.quarterly_start_equity) / _session.quarterly_start_equity) * 100
            log.info(f"Quarterly:  {q_gain:+.1f}% (target >= {cfg.QUARTERLY_PROFIT_TARGET_PCT:.0f}%)")
        log.info(f"Positions:  {len(positions)}")
        if positions:
            total_pnl = sum(float(p.unrealized_pl) for p in positions)
            log.info(f"Unrealized: ${total_pnl:.2f}")
            for p in positions:
                pct = float(p.unrealized_plpc) * 100
                log.info(
                    f"  {p.symbol}: {p.qty} @ ${float(p.avg_entry_price):.2f} "
                    f"| ${float(p.unrealized_pl):.2f} ({pct:+.2f}%)"
                )
        log.info("=" * 70)
    except Exception as e:
        log.error(f"Status error: {e}")


def get_adaptive_interval(ctx: AppContext) -> int:
    """Return next scan interval in minutes based on VIX, market phase, and position count."""
    if not cfg.ADAPTIVE_INTERVALS:
        return cfg.SCAN_INTERVAL_MIN

    market_state = ctx.market_state or MarketState.from_now()
    vix, vix_interval, vol = market_state.resolve_vix()
    interval     = vix_interval
    market_phase = "ALL DAY"

    if cfg.USE_MARKET_HOURS_TUNING:
        mkt_interval, market_phase = get_market_hours_interval(market_state.hour, {
            "PREMARKET_SCAN_INTERVAL":     cfg.PREMARKET_SCAN_INTERVAL,
            "REGULAR_HOURS_SCAN_INTERVAL": cfg.REGULAR_HOURS_SCAN_INTERVAL,
            "AFTERHOURS_SCAN_INTERVAL":    cfg.AFTERHOURS_SCAN_INTERVAL,
        })
        if mkt_interval is not None:
            interval = mkt_interval

    # Tighten cadence for the pre-open preparation window so EMA and strategy
    # signals are refreshed every minute before/at the morning entry segment.
    # 2026-09-01: the entry window now opens at ENTRY_WINDOW_START_ET (09:14),
    # but get_market_hours_interval() still labels everything before 09:30 as
    # PRE-MARKET (10 min) -- without this override, the first 16 minutes of the
    # morning segment would scan on a 10-min cadence (same failure mode the
    # main-loop phase comment documents: a stale premarket interval rode up to
    # 15 min past the open before recomputing).
    now_et = market_state.now
    prep_start = datetime.datetime.strptime(cfg.PREP_SCAN_START_ET, "%H:%M").time()
    market_open = datetime.datetime.strptime(cfg.MARKET_OPEN, "%H:%M").time()
    if prep_start <= now_et.time() < market_open:
        interval = 1

    pos_status = "DISABLED"
    if cfg.USE_POSITION_TUNING:
        try:
            pos_count  = len(ctx.client.get_all_positions())
            pos_interval, pos_status = get_position_tuning_interval(pos_count, {
                "HIGH_POSITION_INTERVAL":   cfg.HIGH_POSITION_INTERVAL,
                "NORMAL_POSITION_INTERVAL": cfg.NORMAL_POSITION_INTERVAL,
                "LOW_POSITION_INTERVAL":    cfg.LOW_POSITION_INTERVAL,
            })
            if pos_interval is not None:
                interval = max(interval, pos_interval)
        except Exception as e:
            log.debug(f"Position tuning check failed: {e}")
            pos_status = "POS CHECK ERROR"

    log.info(f"VIX: {vix:.2f} ({vol}) | {market_phase} | {pos_status} | Scan: {interval} min")
    return interval


def _eod_close_job(ctx: AppContext) -> None:
    """schedule-driven wrapper for close_eod_positions, same reasoning as
    _guardrail_close_job below: was scan-cadence-gated with a narrow window
    (EOD_CLOSE_TIME through the hard 16:00 ET cutoff) that could fall
    between two cycles. 2026-08-12: retimed 15:50->15:45 (15 min before
    close, was 10) and decoupled the same way."""
    eod_summary = None
    try:
        eod_summary = ctx.executor.close_eod_positions()
    except Exception as e:
        log.error(f"close_eod_positions error: {e}", exc_info=True)

    if eod_summary:
        try:
            account   = ctx.client.get_account()
            positions = ctx.client.get_all_positions()
            notify_eod(eod_summary, account, positions, _session.daily_pnl, _session.trades, _discovery.trending_stocks)
        except Exception as e:
            log.error(f"EOD notify error: {e}", exc_info=True)


def _lunch_flat_job(ctx: AppContext) -> None:
    """schedule-driven wrapper for lunch_flat_positions -- 2026-09-01, user
    request ("at 11AM close all positions and open orders, and reenter only
    at 2:45PM and again exist all at 3:50"): hard-flat the whole equity book
    the moment the morning entry segment ends. Same decoupling reasoning as
    _eod_close_job/_guardrail_close_job: this must land inside the 11:00-14:45
    window no matter what the adaptive scan cadence is doing. The function's
    own time-of-day gate + per-day per-symbol done set do the real work, so
    calling it every minute is safe."""
    try:
        summary = ctx.executor.lunch_flat_positions()
        if summary and (summary.get("closed_count") or summary.get("cancelled_orders")):
            log.warning(
                f"[LUNCH-FLAT] {summary.get('closed_count')} position(s) closed, "
                f"{summary.get('cancelled_orders')} open order(s) cancelled at the midday break"
            )
    except Exception as e:
        log.error(f"lunch_flat_positions error: {e}", exc_info=True)


def _guardrail_close_job(ctx: AppContext) -> None:
    """schedule-driven wrapper for close_guardrail_fail_positions.

    2026-08-12: this used to run only inside the main scan-cadence block
    below, gated on the same variable interval (5-60 min depending on
    VIX/position count) as everything else there. That let the function's
    own internal 5-min close window (GUARDRAIL_EOD_CLOSE_TIME through
    16:00 ET) fall entirely between two cycles -- confirmed same-day: one
    cycle started 15:54:16 ET (too early), the next didn't start until
    ~16:01 ET (already past the hard 16:00 cutoff), so the window never
    got checked at all. Running it as its own schedule.every(1).minutes
    job decouples it from scan cadence -- schedule.run_pending() ticks
    every 5s in the main loop regardless, so a 1-min job is guaranteed to
    land inside any 5-min window. The function's own internal gating
    (time-of-day check + once-per-day done flag) still does the real work;
    this just guarantees it's actually asked every minute."""
    try:
        ctx.executor.close_guardrail_fail_positions()
    except Exception as e:
        log.error(f"close_guardrail_fail_positions error: {e}", exc_info=True)


def _price_drift_stop_job(ctx: AppContext) -> None:
    """schedule-driven wrapper for check_price_drift_stop -- runs on its own
    PRICE_DRIFT_CHECK_INTERVAL_MIN cadence (10 min, matching the TI-scrape
    cadence) rather than the variable scan-cadence block, same decoupling
    reasoning as _guardrail_close_job."""
    try:
        ctx.executor.check_price_drift_stop()
    except Exception as e:
        log.error(f"check_price_drift_stop error: {e}", exc_info=True)


def _swing_drift_stop_job(ctx: AppContext) -> None:
    """schedule-driven wrapper for check_swing_drift_stop -- wider-threshold
    sibling of _price_drift_stop_job for multi-day positions, its own
    SWING_DRIFT_STOP_CHECK_INTERVAL_MIN cadence (30 min)."""
    try:
        ctx.executor.check_swing_drift_stop()
    except Exception as e:
        log.error(f"check_swing_drift_stop error: {e}", exc_info=True)


def _concentration_check_job(ctx: AppContext) -> None:
    """schedule-driven wrapper for enforce_position_concentration/
    enforce_correlation_concentration -- 2026-08-15, user request: idea #6
    of six suggested improvements. These used to run inline inside
    scan_and_trade(), which meant they were gated behind FOUR separate
    early-returns above them (market-closed, kill-mode, entry-window,
    daily-loss-limit/profit-target) despite being risk-REDUCTION actions on
    existing positions, not new entries -- on a day the daily-loss-limit
    trips, concentration trimming stopped right when it mattered most. Own
    fixed clock-grid schedule now (CONCENTRATION_CHECK_INTERVAL_MIN), same
    decoupling reasoning as _guardrail_close_job/_price_drift_stop_job,
    runs regardless of any of those four gates.

    2026-08-17: also runs enforce_portfolio_leverage() (caps TOTAL exposure
    at MAX_PORTFOLIO_LEVERAGE x equity, independent of per-symbol/per-group
    caps) on the same schedule."""
    try:
        ctx.executor.enforce_position_concentration()
        ctx.executor.enforce_correlation_concentration()
        ctx.executor.enforce_portfolio_leverage()
    except Exception as e:
        log.error(f"concentration check error: {e}", exc_info=True)


def _schedule_on_clock_grid(interval_min: int, job, *args) -> None:
    """Register `job` to run at fixed wall-clock marks (:00, :10, :20, ... for
    interval_min=10) instead of schedule.every(N).minutes, which counts N
    minutes from whenever this line executes -- i.e. from process start.

    2026-08-14, found while investigating why FFAI's drift-stop check missed
    a brief dip-and-recover: on a day with this many restarts, each restart
    re-registered schedule.every(10).minutes fresh, so the first fire landed
    10 min after THAT restart, not on any fixed grid -- confirmed live,
    checks landed 11:06, 11:25, 11:41, 11:54 (13-19 min gaps, not a clean
    10), widening the blind spot between checks. A drift stop is a
    point-in-time poll, not a continuous high/low tracker, so wider gaps
    mean more brief moves slip through entirely. This doesn't fix that
    inherent polling gap, but it does stop restarts from making it worse --
    every restart now re-aligns to the same clock marks instead of resetting
    its own independent countdown.

    interval_min must evenly divide 60 (10, 12, 15, 20, 30 ... -- 10 is what
    every caller here actually uses)."""
    assert 60 % interval_min == 0, f"{interval_min} must evenly divide 60 to land on a fixed grid"
    # 2026-09-02, user request ("check all the polling loops start at 9.25AM
    # ET to avoid delays"): the grid alignment below only guarantees the first
    # fire at the NEXT clock mark -- up to interval_min (10-30) minutes after
    # boot, which on a late-morning restart left the drift-stop/concentration
    # checks blind straight across the 09:30 open. Fire once immediately at
    # registration so every schedule-driven loop has ticked at least once
    # before the open regardless of boot time. The wrappers have their own
    # try/excepts; this one keeps an unexpected boot-time failure from
    # killing registration of the recurring grid jobs.
    try:
        job(*args)
        log.info(f"[SCHEDULE] {getattr(job, '__name__', job)} first tick fired at registration (pre-grid warm-up)")
    except Exception as e:
        log.error(f"[SCHEDULE] {getattr(job, '__name__', job)} registration-time first tick failed: {e}", exc_info=True)
    for minute in range(0, 60, interval_min):
        schedule.every().hour.at(f":{minute:02d}").do(job, *args)


def _prune_universe_job() -> None:
    try:
        from .equity.universe import prune as _prune
        removed = _prune()
        if removed:
            log.info(f"Universe pruned: {len(removed)} expired ticker(s): {removed[:10]}{'...' if len(removed) > 10 else ''}")
        else:
            log.info("Universe pruned: no expired tickers")
    except Exception as e:
        log.warning(f"Universe prune failed: {e}")


# -- Top3-only (dry-run) mode --------------------------------------------------

def scan_top3_only(ctx: AppContext) -> None:
    market_state = ctx.market_state or MarketState.from_now()
    ctx.market_state = market_state
    sentiment = market_state.resolve_sentiment()
    log.info(f"Market sentiment: {sentiment}")
    _run_discovery(ctx, market_state)
    _, _, excluded = get_live_holdings(ctx.client)
    scan_targets   = get_scan_targets(excluded, market_state=market_state)
    log.info(f"Top3 mode: scanning {len(scan_targets)} symbols ({len(excluded)} pre-excluded)")
    signals, _, scan_errors = scan_universe(scan_targets, sentiment, market_state)
    log.info(f"Scan errors: {scan_errors} | Signals: {len(signals)}")
    if not signals:
        log.info("No signals found in Top3 mode")
        return
    _, _, fresh_held = get_live_holdings(ctx.client)
    fresh_held = fresh_held or excluded
    top5 = [s for s in signals if s.symbol not in fresh_held][:5]
    if not top5:
        log.info("No signals (all candidates already held)")
        return
    log.info("TOP 5 SCAN PICKS:")
    for idx, s in enumerate(top5, 1):
        log.info(f"#{idx}: {s.symbol} {s.action.upper()} ${s.price:.2f} conf={s.confidence:.0%} [{s.strategy}] -- {s.reason}")
    notify_scan_results(top5, datetime.date.today(), sentiment, ctx.last_market_regime)


# -- Main loop -----------------------------------------------------------------

# -- Software-stop fast-poll thread -------------------------------------------
# PDT-blocked stops need frequent polling regardless of the adaptive scan
# interval (which can stretch to 20 min in calm markets).
# This thread runs independently at a fixed 10-second cadence and only
# makes a broker call when _pdt_stop_blocked is non-empty.

def _tick(ctx: AppContext, last_ema15: float, last_pending: float) -> Tuple[float, float]:
    """One iteration's worth of _start_software_stop_thread's checks.
    Returns the (possibly updated) (last_ema15, last_pending) timestamps.
    Each check keeps its own try/except so one failing check doesn't block
    the rest running this same tick. Module-level (not a closure inside
    _start_software_stop_thread) specifically so it's directly testable
    with a mock ctx -- see _demo() below.

    2026-08-27, user request ("in the next 18secs before order executed
    the code should have cancelled the order"): check_pending_entries_ema
    now runs on its own PENDING_ENTRY_RECHECK_SEC (5s) timer, separate
    check_ema9_exit/check_blocked_entries_ema every poll as well as
    check_pending_entries_ema. All entry/exit condition checks therefore use
    the same ten-second cadence.
    Deliberately still the SAME thread/sequential execution as everything
    else here, not a second thread -- check_pending_entries_ema mutates
    order_cache/_pending_entry_signals/_ema_blocked_entries, the same
    dicts check_ema9_exit and check_blocked_entries_ema touch; keeping
    all of it single-threaded avoids introducing a new cross-thread race
    on that shared state for the sake of speed."""
    # 2026-09-02: guardian hard daily-loss backstop. Runs FIRST on this tick so
    # a halt flattens the book even while scan_and_trade is mid-cycle (this
    # thread is genuine concurrency, independent of the single-threaded main
    # loop -- same reasoning as the 2026-08-24 comment above). Idempotent:
    # per-day dedupe HERE (ctx.guardian_halt_acted_date, date-scoped so a
    # next-day flag re-arms without a restart) AND inside
    # executor.guardian_halt_flatten (_guardian_halt_closed).
    _maybe_guardian_halt(ctx)

    if time.time() - last_pending >= cfg.PENDING_ENTRY_RECHECK_SEC:
        try:
            ctx.executor.check_pending_entries_ema()
        except Exception as e:
            log.error(f"[STOP-THREAD] check_pending_entries_ema error: {e}", exc_info=True)
        try:
            ctx.executor.maybe_add_staged_tranches()
        except Exception as e:
            log.error(f"[STOP-THREAD] maybe_add_staged_tranches error: {e}", exc_info=True)
        last_pending = time.time()

    try:
        _retry_top_entries(ctx)
    except Exception as e:
        log.error(f"[STOP-THREAD] top-entry retry error: {e}", exc_info=True)

    try:
        ctx.executor._cover_naked_positions()
    except Exception as e:
        log.error(f"[STOP-THREAD] _cover_naked_positions error: {e}", exc_info=True)
    try:
        if ctx.executor._pdt_stop_blocked:
            ctx.executor.check_software_stops()
    except Exception as e:
        log.error(f"[STOP-THREAD] check_software_stops error: {e}", exc_info=True)
    try:
        ctx.executor.check_afterhours_stops()
    except Exception as e:
        log.error(f"[STOP-THREAD] check_afterhours_stops error: {e}", exc_info=True)
    try:
        ctx.executor._sweep_force_closes()
    except Exception as e:
        log.error(f"[STOP-THREAD] _sweep_force_closes error: {e}", exc_info=True)
    try:
        ctx.executor._sweep_pending_entries()
    except Exception as e:
        log.error(f"[STOP-THREAD] _sweep_pending_entries error: {e}", exc_info=True)
    try:
        ctx.executor.detect_stopped_out_positions()
    except Exception as e:
        log.error(f"[STOP-THREAD] detect_stopped_out_positions error: {e}", exc_info=True)
    try:
        ctx.executor.check_ema9_exit()
    except Exception as e:
        log.error(f"[STOP-THREAD] check_ema9_exit error: {e}", exc_info=True)
    try:
        ctx.executor.check_mfe_giveback_exit()
    except Exception as e:
        log.error(f"[STOP-THREAD] check_mfe_giveback_exit error: {e}", exc_info=True)
    try:
        ctx.executor.check_blocked_entries_ema()
    except Exception as e:
        log.error(f"[STOP-THREAD] check_blocked_entries_ema error: {e}", exc_info=True)
    last_ema15 = time.time()
    return last_ema15, last_pending


def _start_software_stop_thread(ctx: AppContext) -> None:
    """Spawn a daemon thread that polls _cover_naked_positions(),
    check_software_stops(), check_afterhours_stops(), _sweep_force_closes(),
    _sweep_pending_entries(), detect_stopped_out_positions(),
    check_pending_entries_ema(), check_ema9_exit(), and
    check_blocked_entries_ema() every PENDING_ENTRY_RECHECK_SEC seconds.

    2026-08-24, user request ("why do you say the cycle time increase" --
    it wasn't supposed to touch the EMA check at all): the per-minute EMA
    exit check used to run via schedule.every() in the main loop, with
    comments claiming that decouples it from scan cadence because
    "schedule.run_pending() ticks every 5s in the main loop regardless."
    That's false once scan_and_trade() itself runs long -- the main loop is
    single-threaded, so schedule.run_pending() (and every job registered on
    it) simply doesn't get called until scan_and_trade() returns. Confirmed
    live: cycles were landing 4-10 min apart despite a 1-min config, which
    silently starved the EMA exit check the same way. This thread is
    genuine concurrency (a real Thread, checked every 10s independent of
    what the main loop is doing) -- moving the EMA check here is what
    schedule.every() was supposed to give it and didn't. Still the only
    trigger for the per-minute exit check, now check_ema9_exit
    (2026-08-25, user request: the original EMA15-based check_ema15_exit
    this reasoning was built for is removed -- see check_ema9_exit's
    docstring for the current logic)."""
    import threading

    def _loop() -> None:
        global _last_poller_tick
        last_ema15 = 0.0
        last_pending = 0.0
        while True:
            loop_started = time.monotonic()
            # 2026-08-27, user request ("improve the 1min checks to have
            # better reliability as the whole logic is dependent on it"):
            # outer catch-all around the whole tick, on top of _tick()'s own
            # per-call try/excepts. Those already stop one check's failure
            # from blocking the rest this tick, but nothing previously
            # caught a failure in the glue code around them (the timing
            # logic, a future edit adding an unwrapped line, etc.) -- any of
            # that would have silently killed this daemon thread forever,
            # with no sign anything was wrong until positions went
            # unmanaged. Now even an unanticipated failure just logs and the
            # loop keeps going next tick.
            try:
                last_ema15, last_pending = _tick(ctx, last_ema15, last_pending)
            except Exception as e:
                log.error(f"[STOP-THREAD] unhandled tick error (loop continues): {e}", exc_info=True)
            # Liveness marker read by _poller_staleness_job (below) -- proves
            # this thread is actually still ticking, not just presumed alive
            # because the process's own heartbeat.txt (written by the main
            # loop, a different thread) is unaffected by this one dying.
            _last_poller_tick = time.time()
            # Keep the poller on a fixed cadence. Sleep only for the remaining
            # interval after network/work completes to avoid compounding delays.
            time.sleep(max(0.0, cfg.PENDING_ENTRY_RECHECK_SEC - (time.monotonic() - loop_started)))

    t = threading.Thread(target=_loop, name="SoftwareStopPoller", daemon=True)
    t.start()
    log.info(
        f"[STOP-THREAD] Software-stop poll thread started ({cfg.PENDING_ENTRY_RECHECK_SEC}s interval, "
        f"pending-entry/EMA exit/blocked-entry checks every {cfg.PENDING_ENTRY_RECHECK_SEC}s)"
    )


def _poller_staleness_job() -> None:
    """Scheduled every minute (see run()) -- alerts if SoftwareStopPoller
    hasn't ticked in a while, since a silently-dead poller thread otherwise
    has zero observable symptom until positions go unmanaged: the main
    loop's own heartbeat.txt keeps updating fine (different thread), and
    the exit-stack functions this thread is the only trigger for
    (check_ema9_exit, check_pending_entries_ema, check_blocked_entries_ema,
    detect_stopped_out_positions, ...) just quietly stop running. Threshold
    (3 min) is generous versus the thread's own 10s tick / 1min EMA cadence
    -- only fires on a genuine stall, not routine scheduling jitter."""
    global _last_poller_tick, _poller_stale_alerted
    if _last_poller_tick == 0.0:
        return  # thread hasn't started yet
    age = time.time() - _last_poller_tick
    if age < 180:
        _poller_stale_alerted = False
        return
    if not _poller_stale_alerted:
        msg = (
            f"[STOP-THREAD] SoftwareStopPoller has not ticked in {age:.0f}s "
            f"(expected every ~5s) -- re-entry/exit checks (check_ema9_exit, "
            f"check_pending_entries_ema, check_blocked_entries_ema, "
            f"detect_stopped_out_positions) have stopped running. Restart required."
        )
        log.error(msg)
        try:
            send_email("[APEXTRADER] SoftwareStopPoller stalled", msg)
        except Exception as e:
            log.error(f"[STOP-THREAD] stall alert email failed: {e}")
        _poller_stale_alerted = True


# (start_ET, end_ET, interval_minutes) as (hour, minute) pairs: fast 3-min
# refreshes in the morning and before the close, 10-min refreshes otherwise.
# 2026-08-27, user request ("fix the stock universe check from ti web
# scrapping ... starting 8:55 ET and perform the 3 min check till 10:30
# ET, but don't trade until 9:25 ET"): tier 1 starts 30 min before
# entries, matching DISCOVERY_WINDOW_START_ET in config.py
# opens, so the universe is warm well before trading is allowed to start.
_TI_CAPTURE_TIERS = [
    ((8, 55),  (10, 30), 3),
    ((10, 30), (14, 50), 10),
    ((14, 50), (15, 50), 3),
]

def _ti_capture_interval_min(now_et: datetime.datetime) -> Optional[int]:
    """Return the configured interval (minutes) for now_et's tier, or None if
    outside all tiers (no capture window right now)."""
    hm = (now_et.hour, now_et.minute)
    for start, end, interval in _TI_CAPTURE_TIERS:
        if start <= hm < end:
            return interval
    return None


def _ti_capture_job(now_et: Optional[datetime.datetime] = None) -> None:
    """Scheduled every minute (see run()) -- refreshes data/ti_primary.json
    from Yahoo Finance on the same tiered interval TI's scrape used to run on.

    2026-08-28, user request ("stop the webscrapping from Trade ideas. instead
    use yahoo finance trending now, top gainer and top looser list"): TI's
    Selenium/Edge subprocess was replaced with a direct, in-process call to
    engine/ti/yahoo_universe.py: plain HTTP GETs (yfinance's day_gainers/
    day_losers screeners + Yahoo's trending endpoint), no browser, no login,
    no session to expire. That also means no more crash-prone child process
    to wedge-detect/taskkill -- this job is now just the interval gate; a
    fetch failure logs and retries next tick like any other best-effort job
    in this loop. The TI scraper itself (capture_tradeideas.py) and its
    Task-Scheduler launcher were deleted 2026-09-01.
    """
    global _last_ti_capture_ts

    if now_et is None:
        now_et = datetime.datetime.now(pytz.timezone("America/New_York"))
    interval_min = _ti_capture_interval_min(now_et)
    if interval_min is None:
        return  # outside all capture windows today

    if time.time() - _last_ti_capture_ts < interval_min * 60:
        return

    _last_ti_capture_ts = time.time()
    try:
        from engine.ti.yahoo_universe import write_ti_primary
        n = write_ti_primary()
        log.info(f"[YAHOO-UNIVERSE] refreshed (tier interval={interval_min}min): {n} tickers")
    except Exception as e:
        log.error(f"[YAHOO-UNIVERSE] refresh failed: {e}")


# 2026-09-02: set by the main loop's once-per-day 09:25 ET morning-readiness
# trigger (see the run-loop readiness_due block). Wakes the ActiveListRefresher
# below instantly instead of letting it sleep up to
# ACTIVE_SCAN_SNAPSHOT_INTERVAL_MIN (10) minutes past the trigger -- confirmed
# live 2026-09-02: a boot at 08:48 spaced the prewarm runs at 08:48/58, 09:08,
# 09:18, 09:28, i.e. the last EMA warm-up landed AFTER the 09:30 open.
_readiness_kick = threading.Event()


def _start_active_list_thread(ctx: AppContext) -> None:
    """Refresh the filtered active stock lists independently of scan duration."""
    def _loop() -> None:
        next_run = time.monotonic()
        while True:
            now_et = datetime.datetime.now(pytz.timezone("America/New_York"))
            if _within_discovery_window(now_et):
                try:
                    market_state = MarketState.from_now(now_et)
                    _ti_capture_job(now_et)
                    _discovery.scan_alpaca_movers(
                        interval_min=cfg.ALPACA_MOVER_SCAN_INTERVAL_MIN,
                        market_state=market_state,
                    )
                    warm_symbols = get_scan_targets(market_state=market_state)
                    ctx.executor.prewarm_entry_ema(warm_symbols)
                    log.info("[ACTIVE-LISTS] refreshed Yahoo+Alpaca filtered combined/long/short snapshots")
                except Exception as e:
                    log.error(f"[ACTIVE-LISTS] refresh failed: {e}", exc_info=True)
                next_run += cfg.ACTIVE_SCAN_SNAPSHOT_INTERVAL_MIN * 60
            else:
                # If the bot is already running before 08:55 ET, don't sleep
                # through the discovery-window open on the full snapshot interval.
                next_run += 60
            # 2026-09-02: wait on the readiness kick instead of sleeping
            # straight through, so the main loop's 09:25 ET morning-readiness
            # trigger can force ti_capture + Alpaca movers + prewarm_entry_ema
            # to run NOW. The work itself runs unconditionally at the top of
            # this loop, so resetting next_run to "now" after a kick simply
            # gives the kicked run a full fresh snapshot interval before the
            # next scheduled one.
            if _readiness_kick.wait(timeout=max(1.0, next_run - time.monotonic())):
                _readiness_kick.clear()
                next_run = time.monotonic()

    t = threading.Thread(target=_loop, name="ActiveListRefresher", daemon=True)
    t.start()
    log.info(
        f"[ACTIVE-LISTS] refresher started ({cfg.ACTIVE_SCAN_SNAPSHOT_INTERVAL_MIN} min interval)"
    )


def start() -> None:
    ctx = _build_context()

    _session.load_quarterly_state()
    _session.load_daily_state()
    # 2026-09-02: clear a stale (prior-day) guardian flat flag at startup so a
    # yesterday halt can never bleed into today. Today's flag is left intact --
    # the 5s poll tick will flatten on it.
    try:
        _gf = Path(cfg.GUARDIAN_FLAT_FILE)
        if _gf.exists():
            import json as _json
            today_et = datetime.datetime.now(pytz.timezone("America/New_York")).date().isoformat()
            try:
                stale = str(_json.loads(_gf.read_text(encoding="utf-8")).get("date")) != today_et
            except Exception:
                stale = True
            if stale:
                _gf.unlink(missing_ok=True)
                log.info("Cleared stale guardian flat flag at startup")
    except Exception as _e:
        log.warning(f"startup guardian-flag cleanup failed: {_e}")
    log.info("=" * 70)
    log.info("APEXTRADER - Priority-Based Momentum Trading")
    log.info("=" * 70)
    log.info(f"Priority 1 (Momentum): {len(cfg.PRIORITY_1_MOMENTUM)} stocks")
    log.info(f"Priority 2 (Established): {len(cfg.PRIORITY_2_ESTABLISHED)} stocks")
    log.info(f"Total Universe: {sum(len(v) for v in cfg.STOCKS.values())} stocks")
    log.info(f"Scan: {'ADAPTIVE (VIX-based)' if cfg.ADAPTIVE_INTERVALS else f'{cfg.SCAN_INTERVAL_MIN} min fixed'}")
    log.info("=" * 70)

    try:
        account = ctx.client.get_account()
        log.info(f"Equity:          ${float(account.equity):,.2f}")
        log.info(f"Buying Power:    ${float(account.buying_power):,.2f}")
        log.info(f"PDT Status:      {'Yes' if account.pattern_day_trader else 'No'}")
        log.info(f"Day Trade Count: {account.daytrade_count}")
    except Exception as e:
        log.error(f"Account info error: {e}")

    log.info("=" * 70)
    log.info("Starting... Press Ctrl+C to stop")
    log.info("=" * 70)

    try:
        ctx.executor.protect_positions()
    except Exception as e:
        log.error(f"protect_positions startup error: {e}", exc_info=True)

    # Start the dedicated software-stop monitor thread
    _start_software_stop_thread(ctx)
    _start_active_list_thread(ctx)

    try:
        scan_and_trade(ctx)
    except Exception as e:
        log.error(f"Initial scan error: {e}", exc_info=True)

    last_vix_check    = time.time()
    current_interval  = get_adaptive_interval(ctx)
    last_scan         = time.time()
    entry_open_scan_date = None
    entry_reopen_scan_date = None
    readiness_scan_date = None  # 2026-09-02: once-per-day 09:25 ET morning-readiness trigger
    _, last_market_phase = get_market_hours_interval(MarketState.from_now().hour, {})

    schedule.every(30).minutes.do(log_status, ctx)
    schedule.every(30).minutes.do(_prune_universe_job)
    schedule.every(1).minutes.do(_guardrail_close_job, ctx)
    schedule.every(1).minutes.do(_eod_close_job, ctx)
    schedule.every(1).minutes.do(_lunch_flat_job, ctx)
    schedule.every(1).minutes.do(_poller_staleness_job)
    schedule.every(1).minutes.do(_ti_capture_job)
    _schedule_on_clock_grid(cfg.PRICE_DRIFT_CHECK_INTERVAL_MIN, _price_drift_stop_job, ctx)
    _schedule_on_clock_grid(cfg.SWING_DRIFT_STOP_CHECK_INTERVAL_MIN, _swing_drift_stop_job, ctx)
    # Per-minute EMA exit check (check_ema9_exit) runs on the
    # SoftwareStopPoller thread (see _start_software_stop_thread), not
    # registered here.
    _schedule_on_clock_grid(cfg.CONCENTRATION_CHECK_INTERVAL_MIN, _concentration_check_job, ctx)

    try:
        while True:
            try:
                # Refresh interval every 15 min, OR immediately on a market-phase
                # transition (PRE-MARKET/REGULAR HOURS/AFTER-HOURS/OFF-HOURS).
                # The 15-min timer alone let a stale premarket interval (10 min)
                # ride up to 15 min past the 9:30 ET open before recomputing --
                # confirmed 2026-08-26: one scan at 9:27:44 ET, next not until
                # 9:38:12 ET, an ~8 min dead zone spanning the open. This phase
                # check is a local hour lookup (get_market_hours_interval), no
                # API call, so it's cheap to run every loop tick.
                _, market_phase_now = get_market_hours_interval(MarketState.from_now().hour, {})
                phase_changed = cfg.USE_MARKET_HOURS_TUNING and market_phase_now != last_market_phase
                if cfg.ADAPTIVE_INTERVALS and (phase_changed or (time.time() - last_vix_check) >= 900):
                    new_interval = get_adaptive_interval(ctx)
                    if new_interval != current_interval:
                        log.info(f"Scan interval: {current_interval} -> {new_interval} min"
                                 + (f" (phase: {last_market_phase} -> {market_phase_now})" if phase_changed else ""))
                        current_interval = new_interval
                    last_vix_check = time.time()
                last_market_phase = market_phase_now

                now_et = datetime.datetime.now(pytz.timezone("America/New_York"))
                entry_start = datetime.datetime.strptime(
                    cfg.ENTRY_WINDOW_START_ET, "%H:%M"
                ).time()
                entry_open_due = (
                    now_et.time() >= entry_start
                    and entry_open_scan_date != now_et.date()
                )
                # 2026-09-01, two-window schedule: the afternoon entry segment
                # (now 14:15-15:44, 2026-09-04) needs its own once-per-day
                # kick, same shape as the morning one -- without it the first
                # afternoon cycle waits out the adaptive interval before it
                # can re-enter at all.
                entry_reopen = datetime.datetime.strptime(
                    cfg.ENTRY_WINDOW_BREAK_END_ET, "%H:%M"
                ).time()
                entry_reopen_due = (
                    now_et.time() >= entry_reopen
                    and entry_reopen_scan_date != now_et.date()
                )
                # 2026-09-02, user request ("check all the polling loops start
                # at 9.25AM ET to avoid delays"): once-per-day 09:25 ET
                # morning-readiness trigger -- forces a fresh scan cycle and
                # kicks the ActiveListRefresher (immediate ti_capture + Alpaca
                # movers + prewarm_entry_ema) so every polling loop has freshly
                # ticked before the 09:30 open: EMA signals ready by 09:29,
                # first orders at 09:30. Also covers a LATE boot (this
                # morning's 09:29:46 restart): on boot after 09:25 this fires
                # immediately instead of waiting out the adaptive interval.
                # Scoped to the morning segment (ends at the 11:00 lunch flat);
                # the afternoon segment has its own entry_reopen (14:15).
                readiness_open = datetime.datetime.strptime(
                    cfg.MORNING_READINESS_ET, "%H:%M"
                ).time()
                readiness_close = datetime.datetime.strptime(
                    cfg.ENTRY_WINDOW_BREAK_START_ET, "%H:%M"
                ).time()
                readiness_due = (
                    now_et.weekday() < 5  # 2026-09-02 red-team: Mon-Fri only -- a Saturday 09:25 boot must not force scans/kicks on a non-trading day
                    and readiness_open <= now_et.time() < readiness_close
                    and readiness_scan_date != now_et.date()
                )
                # 2026-09-02 deep-dive (timeline sim S5): no scan triggers on
                # weekends -- the discovery-window check is time-only, so a
                # Saturday 09:14/adaptive trigger would otherwise run a full
                # scan against closed markets. Protective + poller jobs on the
                # schedule are unaffected (self-gated by their own windows).
                if (now_et.weekday() < 5
                        and (entry_open_due or entry_reopen_due or readiness_due
                        or (time.time() - last_scan) >= (current_interval * 60))):
                    if readiness_due:
                        readiness_scan_date = now_et.date()
                        log.info(
                            "Morning-readiness scan trigger (%s ET): forcing fresh scan + ActiveListRefresher prewarm kick "
                            "so EMA signals are ready by 09:29 for the 09:30 open",
                            cfg.MORNING_READINESS_ET,
                        )
                        _readiness_kick.set()
                    if entry_open_due:
                        entry_open_scan_date = now_et.date()
                        log.info("Entry-window-open scan trigger: forcing first executable scan")
                    if entry_reopen_due:
                        entry_reopen_scan_date = now_et.date()
                        log.info("Afternoon-entry-open scan trigger: forcing first executable scan")
                    try:
                        ctx.executor.protect_positions()
                    except Exception as e:
                        log.error(f"protect_positions error: {e}", exc_info=True)
                    # check_software_stops() runs in its dedicated 10s thread -- not here

                    try:
                        ctx.executor.ratchet_confident_winners()
                    except Exception as e:
                        log.error(f"ratchet_confident_winners error: {e}", exc_info=True)

                    try:
                        ctx.executor.close_stale_swing_positions()
                    except Exception as e:
                        log.error(f"close_stale_swing_positions error: {e}", exc_info=True)

                    try:
                        ctx.executor.close_no_gain_positions()
                    except Exception as e:
                        log.error(f"close_no_gain_positions error: {e}", exc_info=True)

                    try:
                        scan_and_trade(ctx)
                    except Exception as e:
                        log.error(f"Scan cycle error: {e}", exc_info=True)

                    last_scan = time.time()
                    log.info(f"Heartbeat: {datetime.datetime.now().isoformat()}")
                    # Completed scan cycle -> unconditional heartbeat refresh
                    # (bypasses the 60s rate limiter). The EVERY-TICK liveness
                    # touch below is what keeps the stall watchdog honest
                    # during long off-hours adaptive sleeps.
                    _touch_heartbeat(force=True)

                schedule.run_pending()
                # 2026-09-02 red-team fix: touch the heartbeat EVERY tick
                # (rate-limited to 60s inside _touch_heartbeat) so the
                # watchdog's 15-min stall detector measures main-loop
                # liveness, not time-since-last-scan -- a 20-min off-hours
                # adaptive sleep must never read as "hung".
                _touch_heartbeat()
                time.sleep(5)

            except KeyboardInterrupt:
                log.info("Stopped by user")
                log_status(ctx)
                break
            except Exception as e:
                log.error(f"Main loop error: {e}", exc_info=True)
                time.sleep(10)

    except KeyboardInterrupt:
        log.info("Stopped by user")
        log_status(ctx)


# -- Public entry point --------------------------------------------------------

def run(*, force: bool = False, once: bool = False, top3_only: bool = False) -> None:
    if force:
        cfg.FORCE_SCAN = True

    if top3_only:
        ctx = _build_context()
        log.info("APEXTRADER -- Top3 scan mode")
        scan_top3_only(ctx)
        log_status(ctx)
        return

    if once:
        ctx = _build_context()
        log.info("=" * 70)
        log.info("APEXTRADER -- Single Scan Cycle")
        log.info("=" * 70)
        scan_and_trade(ctx)
        log_status(ctx)
        return

    start()


def _demo() -> None:
    """python -m engine.orchestrator -- asserts _poller_staleness_job's
    alert state machine holds before it's trusted to actually catch a
    stalled SoftwareStopPoller thread. Monkeypatches send_email (real
    network I/O otherwise) and the module's own liveness globals, restored
    in a finally."""
    global _last_poller_tick, _poller_stale_alerted, send_email
    _orig_tick, _orig_alerted, _orig_email = _last_poller_tick, _poller_stale_alerted, send_email
    sent = []
    try:
        send_email = lambda subject, text, html=None: sent.append((subject, text))

        # No tick yet (thread hasn't started) -> never alert.
        _last_poller_tick, _poller_stale_alerted = 0.0, False
        _poller_staleness_job()
        assert sent == [], "must not alert before the poller thread has ever ticked"

        # Fresh tick -> no alert.
        _last_poller_tick, _poller_stale_alerted = time.time(), False
        _poller_staleness_job()
        assert sent == [], "a fresh tick must not alert"

        # Stale tick -> alerts exactly once.
        _last_poller_tick, _poller_stale_alerted = time.time() - 200, False
        _poller_staleness_job()
        assert len(sent) == 1, f"a stale tick (200s > 180s threshold) must alert, got {len(sent)}"
        assert _poller_stale_alerted is True, "must latch alerted=True so it doesn't re-alert every minute"

        # Still stale next check -> does NOT re-alert (already latched).
        _poller_staleness_job()
        assert len(sent) == 1, "must not re-alert every check while still stale -- one alert per stall, not spam"

        # Recovers (tick becomes fresh again) -> clears the latch.
        _last_poller_tick = time.time()
        _poller_staleness_job()
        assert _poller_stale_alerted is False, "recovering must clear the latch so a FUTURE stall alerts again"

        # Stalls a second time after recovering -> alerts again (proves the
        # latch-clear above actually re-arms it, not just resets a flag that's
        # never read again).
        _last_poller_tick = time.time() - 200
        _poller_staleness_job()
        assert len(sent) == 2, "a second, separate stall after recovery must alert again"

        print("_poller_staleness_job: all checks passed")
    finally:
        _last_poller_tick, _poller_stale_alerted, send_email = _orig_tick, _orig_alerted, _orig_email

    # -- _ti_capture_job / _ti_capture_interval_min --------------------------
    global _last_ti_capture_ts
    _orig_ts = _last_ti_capture_ts
    ET = pytz.timezone("America/New_York")

    try:
        # Tier lookup: inside each tier, and the gaps between/around them.
        assert _ti_capture_interval_min(ET.localize(datetime.datetime(2026, 8, 27, 8, 54))) is None, "before first tier"
        assert _ti_capture_interval_min(ET.localize(datetime.datetime(2026, 8, 27, 8, 55))) == 3, "tier 1 start (inclusive)"
        assert _ti_capture_interval_min(ET.localize(datetime.datetime(2026, 8, 27, 10, 29))) == 3, "tier 1 end (exclusive upper)"
        assert _ti_capture_interval_min(ET.localize(datetime.datetime(2026, 8, 27, 10, 30))) == 10, "tier 2 start"
        assert _ti_capture_interval_min(ET.localize(datetime.datetime(2026, 8, 27, 11, 45))) == 10, "mid tier 2"
        assert _ti_capture_interval_min(ET.localize(datetime.datetime(2026, 8, 27, 12, 30))) == 10, "mid tier 2"
        assert _ti_capture_interval_min(ET.localize(datetime.datetime(2026, 8, 27, 14, 49))) == 10, "tier 2 end"
        assert _ti_capture_interval_min(ET.localize(datetime.datetime(2026, 8, 27, 14, 50))) == 3, "tier 3 start"
        assert _ti_capture_interval_min(ET.localize(datetime.datetime(2026, 8, 27, 15, 49))) == 3, "tier 4 end"
        assert _ti_capture_interval_min(ET.localize(datetime.datetime(2026, 8, 27, 15, 50))) is None, "after last tier"
        assert _ti_capture_interval_min(ET.localize(datetime.datetime(2026, 8, 27, 3, 0))) is None, "middle of the night"

        write_calls = []
        import engine.ti.yahoo_universe as _yu
        _orig_write = _yu.write_ti_primary
        _yu.write_ti_primary = lambda: (write_calls.append(1), 7)[1]

        t9_25 = ET.localize(datetime.datetime(2026, 8, 27, 9, 25))

        # Outside any tier -> never refreshes, regardless of state.
        _last_ti_capture_ts = 0.0
        _ti_capture_job(now_et=ET.localize(datetime.datetime(2026, 8, 27, 3, 0)))
        assert write_calls == [], "must not refresh outside a capture window"

        # Due (never run before) -> refreshes.
        _last_ti_capture_ts = 0.0
        _ti_capture_job(now_et=t9_25)
        assert len(write_calls) == 1, "first-ever call inside a tier must refresh"

        # Not due yet (last run 1 min ago, tier interval 3 min) -> no refresh.
        _last_ti_capture_ts = time.time() - 60
        _ti_capture_job(now_et=t9_25)
        assert len(write_calls) == 1, "must not refresh again before the tier interval elapses"

        # Due again (last run past the tier interval) -> refreshes.
        _last_ti_capture_ts = time.time() - 200  # > 3min tier
        _ti_capture_job(now_et=t9_25)
        assert len(write_calls) == 2, "must refresh once the tier interval has elapsed"

        # write_ti_primary raising must not propagate (best-effort job).
        _yu.write_ti_primary = lambda: (_ for _ in ()).throw(RuntimeError("network down"))
        _last_ti_capture_ts = time.time() - 200
        _ti_capture_job(now_et=t9_25)  # must not raise

        print("_ti_capture_job: all checks passed")
    finally:
        _last_ti_capture_ts = _orig_ts
        _yu.write_ti_primary = _orig_write

    # _tick: 2026-08-27, user request ("in the next 18secs before order
    # executed the code should have cancelled the order"): asserts
    # check_pending_entries_ema fires on its own PENDING_ENTRY_RECHECK_SEC
    # timer independent of check_ema9_exit/check_blocked_entries_ema's
    # separate STAGNANT_STOP_CHECK_INTERVAL_MIN timer -- the actual bug
    # (both sharing one 60s gate) this whole change fixes. Real _tick
    # (module-level, not reimplemented here), a mock ctx.executor.
    class _TickExecutor:
        def __init__(self):
            self.calls = []
            self._pdt_stop_blocked = False
        def _cover_naked_positions(self): self.calls.append("cover")
        def check_software_stops(self): self.calls.append("software_stops")
        def check_afterhours_stops(self): self.calls.append("afterhours")
        def _sweep_force_closes(self): self.calls.append("force_closes")
        def _sweep_pending_entries(self): self.calls.append("sweep_pending")
        def detect_stopped_out_positions(self): self.calls.append("stopped_out")
        def check_pending_entries_ema(self): self.calls.append("pending_ema")
        def check_ema9_exit(self): self.calls.append("ema9_exit")
        def check_blocked_entries_ema(self): self.calls.append("blocked_ema")

    class _TickCtx:
        def __init__(self):
            self.executor = _TickExecutor()

    # The first five-second poll runs every monitoring check.
    tctx = _TickCtx()
    last_ema15, last_pending = _tick(tctx, 0.0, 0.0)
    assert "pending_ema" in tctx.executor.calls, "pending-entry check must fire on a fresh/never-ticked timer"
    assert "ema9_exit" in tctx.executor.calls and "blocked_ema" in tctx.executor.calls, \
        "the 1-min checks must also fire on a fresh/never-ticked timer"
    assert set(tctx.executor.calls) >= {"cover", "afterhours", "force_closes", "sweep_pending", "stopped_out"}, \
        "every-tick checks must always run regardless of either timer"

    # Every subsequent five-second poll runs the same monitoring checks.
    tctx.executor.calls.clear()
    last_ema15_2, last_pending_2 = _tick(tctx, last_ema15, time.time() - (cfg.PENDING_ENTRY_RECHECK_SEC + 1))
    assert "pending_ema" in tctx.executor.calls, "pending-entry check must fire again once its own 5s timer elapses"
    assert "ema9_exit" in tctx.executor.calls and "blocked_ema" in tctx.executor.calls, \
        "EMA exit and blocked-entry checks must run on every five-second poll"

    # The monitoring checks remain active even when the pending timer is not due.
    tctx.executor.calls.clear()
    last_ema15_3, last_pending_3 = _tick(tctx, time.time() - (cfg.STAGNANT_STOP_CHECK_INTERVAL_MIN * 60 + 1), time.time())
    assert "pending_ema" not in tctx.executor.calls, "pending-entry check must respect its five-second timer"
    assert "ema9_exit" in tctx.executor.calls and "blocked_ema" in tctx.executor.calls, \
        "the 1-min checks must fire once their own timer elapses, independent of pending's timer"

    # One check raising must not block the rest of the same tick.
    tctx2 = _TickCtx()
    def _raise(): raise RuntimeError("simulated failure")
    tctx2.executor.check_pending_entries_ema = _raise
    _tick(tctx2, 0.0, 0.0)  # must not raise
    assert "ema9_exit" in tctx2.executor.calls, "one check raising must not block the other checks in the same tick"

    print("_tick: all checks passed")


if __name__ == "__main__":
    _demo()

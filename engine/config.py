"""
ApexTrader - Configuration
Professional Automated Trading System
Modular architecture with multiple strategies and PDT compliance
"""


import datetime
import os
import re
# .env is loaded once by main.py at process start.
# config.py reads env vars directly via os.getenv -- no dotenv call here.

# ------------------------------------------------------------------------------------------
# Broker Selection
# ------------------------------------------------------------------------------------------
STOCKS_BROKER = os.getenv("STOCKS_BROKER", "alpaca")   # alpaca only (E*TRADE removed 2026-09-01)

# -----------------------------------------------------------------
# (2026-09-01: options trading removed -- the whole Options Trading
# Configuration + options universe sections were deleted. See git history.)
# -----------------------------------------------------------------
# Alpaca API Configuration
# ------------------------------------------------------------------------------------------
# Alpaca API Configuration
# ------------------------------------------------------------------------------------------
# PAPER mode is strongly recommended for development/testing.
# Set environment variable TRADE_MODE=paper or TRADE_MODE=live.
TRADE_MODE = os.getenv("TRADE_MODE", "paper").strip().lower()
if TRADE_MODE not in ("paper", "live"):
    raise ValueError(f"Invalid TRADE_MODE={TRADE_MODE!r}; expected 'paper' or 'live'")
PAPER      = TRADE_MODE == "paper"
LIVE       = not PAPER
_MODE      = "PAPER" if PAPER else "LIVE"

API_KEY    = os.getenv(f"{_MODE}_ALPACA_API_KEY", "")
API_SECRET = os.getenv(f"{_MODE}_ALPACA_API_SECRET", "")
# SDK picks the correct endpoint automatically via paper=True/False -- no URL override needed
ALPACA_BASE_URL = "https://paper-api.alpaca.markets" if PAPER else "https://api.alpaca.markets"


# ------------------------------------------------------------------------------------------
# Stock Universe
# Priority 1: Momentum stocks (scanned FIRST, highest allocation)
# Priority 2: Established tech and high short-float stocks
# Priority 3: Market ETFs for context
# ------------------------------------------------------------------------------------------
PRIORITY_1_MOMENTUM = [
    # -- Permanent core (never expire, always scanned) --------------
    # Crypto-leveraged / popular momentum plays
    "MARA", "WULF", "CORZ", "HUT", "IREN",
    # Biotech / speculative momentum
    "MRNA", "BCRX", "SNDX", "IMVT",
    # Energy / commodities momentum
    "RIG", "NOG", "CNX", "BTU", "DK",
    # -- Bear-market long plays (inverse ETFs -- go UP when market falls) --
    # Valid LONG buys in bear regime as LONG_ONLY_MODE=True
    "SQQQ", "SPXU", "UVXY", "TZA", "FAZ", "SOXS", "LABD", "DUST",
]

PRIORITY_2_ESTABLISHED = [
    # -- Permanent core (never expire) -----------------------------
    # Tech giants -- liquid at all times
    "AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "META", "TSLA", "AMZN",
    # High short-float perennials
    "LCID", "MVIS", "WKHS", "SNDX", "FUBO", "INDO", "SOXS", "UCO",
]

PRIORITY_3_MARKET = ["SPY", "QQQ", "IWM", "^VIX"]

# Delisted or broken tickers -- filtered out at runtime
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

# --- Dynamic universe: load TTL-managed tickers from data/universe.json -------
# Trade Ideas updates and prediction picks live there, NOT in this file.
# Universe TTL values are configurable via env vars:
#   UNIVERSE_TTL_TIER1, UNIVERSE_TTL_TIER2, UNIVERSE_TTL_TIER3
# Defaults are currently 15 minutes per tier for live scan freshness.
# get_dynamic_universe() is called live each scan cycle so newly scraped TI
# tickers are picked up without restarting the bot.
from engine.equity.universe import get_tier as _get_tier, merge_live as _merge_live
from engine.never_trade import load_never_trade as _load_never_trade


def get_dynamic_universe() -> tuple:
    """Return (p1, p2, p3) merged lists, re-reading universe.json on every call."""
    _ex = set(DELISTED_STOCKS) | _load_never_trade()
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

# ------------------------------------------------------------------------------------------
# Trading Parameters -- Swing Trading Optimized
# ------------------------------------------------------------------------------------------
MAX_POSITIONS        = 12     # 7.5% -- 12 = 90% of usable equity (within 10% BP reserve) -- reverted 2026-08-11 (was briefly 9 @ 10%)
# 2026-08-22, user request ("top 15 will be picked"): how many ranked
# candidates the scan cycle logs/tracks per cycle (TOP5_RAW/TOP5_ELIGIBLE
# log lines, day_picks.json, the scan-results notification) -- was a
# hardcoded 5. This is a watchlist/visibility breadth, not an execution cap:
# _execute_bull_plan already ranks and tries every eligible signal, not just
# the top 5/15, until MAX_POSITIONS-driven capacity fills.
TOP_N_SIGNALS        = 60
# When full, close the weakest position to make room if new signal conf > this threshold
SWAP_ON_FULL         = True   # enabled -- close weakest position for a better signal when full
SWAP_MIN_CONFIDENCE  = 0.75   # Swap out weakest when new signal >= this confidence (was 0.85)

# 2026-08-17, user request: "change this to 10% base instead of 7.5% and
# scale everything up" -- every % below that was originally set as a
# fixed multiple of the 7.5% base scales by the same 10/7.5 = 4/3 factor,
# so the whole sizing pipeline's proportions stay exactly as they were,
# just anchored to a bigger base:
#   POSITION_SIZE_PCT               7.5  -> 10.0   (the base itself)
#   MAX_POSITION_SIZE_PCT          15.0  -> 20.0   (2x base, confidence-ramp ceiling)
#   MAX_POSITION_CONCENTRATION_PCT 20.0  -> 26.7   (2.67x base, hard per-symbol cap)
#   POSITION_CAP_ABSOLUTE_MAX_PCT  35.0  -> 46.7   (4.67x base, growing-winner ceiling)
#   THIN_LIQUIDITY_POSITION_SIZE_PCT 3.0 -> 4.0    (0.4x base, guardrail-bypass flat size)
#   CORRELATION_GROUPS max_pct     25.0  -> 33.3   (correlated-basket cap)
#   MAX_PORTFOLIO_LEVERAGE          1.5x -> 2.0x   (whole-book cap)
# STRATEGY_KELLY_MULT (2.0x/0.25x) and POSITION_CAP_GROWTH_FACTOR are
# rates/multipliers, not base %s -- left as-is, they already scale
# correctly since they multiply whatever the (now bigger) base produces.
POSITION_SIZE_PCT    = 10.0
MAX_POSITION_CONCENTRATION_PCT = 26.7  # Hard cap: no single symbol's market value may exceed this % of equity,
                                        # enforced both at entry sizing and by trimming winners that ran past it

# 2026-08-17, user request: "maximum holding as 20% of the portfolio value
# and growing based on the continued positive returns" -- the base
# concentration cap above still applies to entry sizing (a brand-new
# position has no gain yet to grow from) and to any losing/flat HELD
# position, but a position currently showing an unrealized gain gets a
# wider personal cap instead of being trimmed straight back to base:
# effective_cap = MAX_POSITION_CONCENTRATION_PCT + gain% x
# POSITION_CAP_GROWTH_FACTOR, capped at POSITION_CAP_ABSOLUTE_MAX_PCT.
# Never drops BELOW the base (max(0, gain) in the formula) -- this only
# ever grows room for winners, it doesn't shrink room for anyone. See
# _effective_concentration_cap_pct() in enhanced.py.
POSITION_CAP_GROWTH_FACTOR      = 0.25  # cap grows by this many points per point of unrealized gain
POSITION_CAP_ABSOLUTE_MAX_PCT   = 46.7  # ceiling the growing cap can never exceed, regardless of gain

# Correlated-exposure cap: several DIFFERENT symbols that move together (e.g. the
# leveraged inverse-market ETF basket) can each stay under MAX_POSITION_CONCENTRATION_PCT
# individually while still adding up to one oversized directional bet combined --
# confirmed in production: SQQQ+SOXS+TZA+LABD held simultaneously on 2026-07-30.
# Symbol list mirrors engine.utils.market.INVERSE_ETFS (duplicated intentionally --
# config.py stays dependency-free; engine.equity.strategies._INVERSE_ETFS already
# duplicates the same list for the same reason).
CORRELATION_GROUPS = {
    "leveraged_inverse": {
        "symbols": {"SQQQ", "SPXU", "UVXY", "TZA", "FAZ", "SOXS", "LABD", "DUST"},
        "max_pct": 33.3,   # combined cap -- above the single-symbol cap, since it's a basket (scaled 25.0 -> 33.3 2026-08-17)
    },
}

# 2026-08-15, user request: idea #6 of six suggested improvements.
# enforce_position_concentration()/enforce_correlation_concentration() used
# to run inline inside scan_and_trade(), gated behind four separate early-
# returns above them (market-closed, kill-mode, entry-window, daily-loss-
# limit/profit-target) despite being risk-REDUCTION actions on existing
# positions, not new entries. Own fixed clock-grid schedule now (see
# _concentration_check_job/_schedule_on_clock_grid in orchestrator.py),
# runs regardless of those gates.
CONCENTRATION_CHECK_INTERVAL_MIN = 10

# -----------------------------------------------------------------
# Portfolio-Wide Leverage Cap
# 2026-08-17, user request: "restrict portfolio value to 1.5x the actual
# account [equity], [not] the margin account" -- Alpaca's own buying_power
# already reflects margin (roughly 2x-4x equity depending on account type/
# PDT status); this is a SEPARATE, stricter ceiling on TOTAL exposure
# across every open position combined, independent of whatever margin the
# broker would otherwise extend. MAX_POSITION_CONCENTRATION_PCT (26.7%)
# caps one symbol; CORRELATION_GROUPS (33.3%) caps one correlated basket;
# this caps the WHOLE book at once. It is enforced BOTH before submission
# (2026-09-04: _size_with_buying_power bounds every new order to the
# remaining gross headroom -- filled positions + resting entry notional
# + this order <= equity x cap) AND after fills by
# enforce_portfolio_leverage (10-min grid backstop for price appreciation).
# 2026-09-04, user request: raise the ceiling to 2.0x portfolio value.
# A ceiling, NOT a target -- utilization still requires qualified signals;
# nothing sizes up to "use" the extra room.
# -----------------------------------------------------------------
MAX_PORTFOLIO_LEVERAGE = float(os.getenv("MAX_PORTFOLIO_LEVERAGE", "2.0"))
# Import-time guard: a typo'd .env value must never let the book run at
# Alpaca's full 4x order-placement margin. 1.0 floor = no leverage below
# cash-equivalent makes sense for this strategy; 2.0 is the user's ceiling.
assert 1.0 <= MAX_PORTFOLIO_LEVERAGE <= 2.0, (
    f"MAX_PORTFOLIO_LEVERAGE ({MAX_PORTFOLIO_LEVERAGE}) must be within [1.0, 2.0] "
    f"(2026-09-04: user ceiling 2.0x portfolio value; Alpaca's ~4x buying_power "
    f"is order-placement capacity, not a target)"
)

# Same-underlying leveraged-ETF pairs (bull+bear on one commodity/index, e.g.
# BOIL/KOLD both on nat gas -- arbitrary product names, no ticker pattern to
# exploit, must be hand-maintained) -- confirmed in production 2026-08-10:
# BOIL+KOLD and UCO+SCO both held simultaneously via the TI-scraped universe.
# Unlike CORRELATION_GROUPS (a %-of-equity trim), this is a same-cycle/
# same-symbol entry block: _filter_eligible() skips a signal whose underlying
# key is already held or already picked earlier in the same cycle.
LEVERAGED_UNDERLYING_GROUPS = {
    "OIL":         {"UCO", "SCO"},
    "NATGAS":      {"BOIL", "KOLD"},
    "GOLD_MINERS": {"NUGT", "DUST", "JNUG", "JDST", "GDXU", "GDXD"},
    "SILVER":      {"AGQ", "ZSL"},
    "ETH":         {"ETHU", "ETHD", "ETHT"},
    "SPX":         {"SPXL", "SPXS", "SPXU", "UPRO"},
    "NDX":         {"TQQQ", "SQQQ"},
    "RUSSELL2000": {"TNA", "TZA", "URTY", "SRTY"},
    "SEMIS":       {"SOXL", "SOXS"},
    "FINANCIALS":  {"FAS", "FAZ"},
    "BIOTECH":     {"LABU", "LABD"},
    "DOW":         {"UDOW", "SDOW"},
    "20Y_TREASURY": {"TMF", "TMV"},
}
LEVERAGED_UNDERLYING = {
    sym: key for key, syms in LEVERAGED_UNDERLYING_GROUPS.items() for sym in syms
}

# Single-stock leveraged ETFs (AAPU on AAPL, NVDL on NVDA, SMCL/SMCX/SMCZ on
# SMCI, ...) follow a ticker pattern instead: root + one suffix letter from a
# small fixed set. Rather than hand-list every provider's product for every
# hot mega/large-cap (new ones list constantly), leveraged_underlying() below
# pattern-matches any candidate against this whitelist of tickers popular
# enough to attract 3rd-party leveraged products. Whitelisted (not applied to
# the whole universe) so two unrelated small-caps sharing a prefix are never
# coincidentally blocked -- e.g. "AAP" (Advance Auto Parts) and "METC" (Ramaco
# Resources) are real, unrelated tickers that must NOT collide with AAPL/META.
SINGLE_STOCK_LEVERAGE_TARGETS = {
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "META", "TSLA", "AMZN",
    "AVGO", "NFLX", "ORCL", "MSTR", "COIN", "SMCI", "PLTR", "IONQ", "RKLB", "MARA",
    "SPCX",  # SpaceX -- confirmed live in scan universe alongside SPCF (2026-07-06 log)
}
_LEVERAGE_SUFFIXES = set("LSUDXZQBTF")  # F added for SPCF (SpaceX leveraged sibling)


def leveraged_underlying(symbol: str) -> str:
    """Best-effort underlying key for the same-underlying entry guard.

    Exact group lookup first (LEVERAGED_UNDERLYING); then a same-length,
    same-root, known-suffix match against SINGLE_STOCK_LEVERAGE_TARGETS
    (catches new single-stock products before anyone remembers to add them
    to a list). Anything else maps to itself.
    """
    if symbol in LEVERAGED_UNDERLYING:
        return LEVERAGED_UNDERLYING[symbol]
    if len(symbol) >= 4 and symbol.isalpha():
        for base in SINGLE_STOCK_LEVERAGE_TARGETS:
            if (len(symbol) == len(base) and symbol != base
                    and symbol[:-1] == base[:-1]
                    and symbol[-1] in _LEVERAGE_SUFFIXES):
                return base
    return symbol
USE_RISK_EQUALIZED_SIZING = False  # use fixed position sizing instead of risk-scaled
RISK_PER_TRADE_PCT   = 0.8    # Risk 0.8% of account per trade (unused with fixed sizing)

# Confidence-based position scaling: low-confidence signals get smaller allocations.
# Multiplier scales linearly from CONF_SCALE_MIN_MULT at MIN_SIGNAL_CONFIDENCE
# up to 1.0-- at CONF_SCALE_FULL_CONF. Above that threshold: always full size.
CONF_SCALE_MIN_MULT  = 0.50   # 50% of normal size at the confidence floor (0.72)
CONF_SCALE_FULL_CONF = 0.85   # 100% of normal size at this confidence and above

# 2026-08-13, user request: the scaling above plateaus at 1.0x (full
# POSITION_SIZE_PCT) for anything >= CONF_SCALE_FULL_CONF (85%) -- 85% and
# 99% confidence get sized identically. Originally patched with a flat
# 1.5x step above a 92% threshold (7.5% -> 11.25%, nothing in between).
#
# 2026-08-15, user request: "increase the percentage progressively maximum
# to 15% maximum per ticker" -- replaced the flat step with a continuous
# ramp instead: allocation_pct rises linearly from the base % (at
# CONF_SCALE_FULL_CONF, 85%) up to MAX_POSITION_SIZE_PCT (100% confidence),
# so every confidence level above 85% gets its own size, not just two
# tiers. See _apply_confidence_size_ramp() in enhanced.py. Applied before
# the thin-liquidity override (still trumps everything with its own flat
# size, unaffected by confidence). MAX_POSITION_SIZE_PCT sits safely
# under MAX_POSITION_CONCENTRATION_PCT (the hard per-symbol cap).
# 2026-08-17: scaled 15.0 -> 20.0 along with the 7.5% -> 10% base (see
# POSITION_SIZE_PCT's comment for the full scaling rationale).
MAX_POSITION_SIZE_PCT = 20.0

# 2026-08-15, user request: "implement everything except 5" (a set of six
# suggested improvements; #1 was per-strategy Kelly-informed sizing).
# Kelly % = W - (1-W)/R computed from each strategy's actual matched
# entry/exit trades since inception (n in parens):
#   GapBreakout  (n=18): W=67%, R=1.49 -> Kelly +44% -- real edge, size up
#   ORB          (n=82): W=51%, R=1.11 -> Kelly  +7% -- thin edge, ~unchanged
#   TrendBreaker (n=18): W=56%, R=0.70 -> Kelly  -8% -- losers run bigger
#     than winners despite the >50% win rate; shrink instead of disabling
#     outright (its multi-day trades still get SWING_DRIFT_STOP_PCT
#     protection, its same-day trades still get PRICE_DRIFT_STOP_PCT).
# Applied as a straight multiplier on allocation_pct, after the confidence
# ramp and before the thin-liquidity override (which still fully
# overrides, unchanged) -- see _apply_strategy_kelly_mult() in enhanced.py.
# Every strategy not listed defaults to 1.0 (unchanged) -- most have too
# few trades (n<10) for a Kelly estimate to mean anything yet. Kept
# conservative (not full/half-Kelly) given the small samples behind these
# numbers; MAX_POSITION_CONCENTRATION_PCT (20%) is still the hard ceiling
# regardless of this multiplier.
STRATEGY_KELLY_MULT = {
    "GapBreakout":  2.0,
    "TrendBreaker": 0.25,
}
STRATEGY_KELLY_MULT_DEFAULT = 1.0

# Small account reduction caps (sub-$5k equity)
SMALL_ACCOUNT_POSITION_SIZE_PCT = 10.0  # same allocation as POSITION_SIZE_PCT for small accounts -- reverted 2026-08-11, scaled 2026-08-17
SMALL_ACCOUNT_RISK_PER_TRADE_PCT = 0.5 # lower risk per trade for small accounts
SMALL_ACCOUNT_MIN_POSITION_DOLLARS = 5.0  # lowered to allow ~$5 entry for cheap tickers

# Tiered Profit Targets -- aggressive: book profits faster
TAKE_PROFIT_EXTREME  = 50.0   # was 35 (+15pp, 2026-08-06)
TAKE_PROFIT_HIGH     = 40.0   # was 25 (+15pp, 2026-08-06)
TAKE_PROFIT_MEDIUM   = 33.0   # was 18 (+15pp, 2026-08-06)
TAKE_PROFIT_NORMAL   = 27.0   # was 12 (+15pp, 2026-08-06)

# Tiered Trailing Stops -- tighter: lock in gains quickly
# 2026-08-22, user request: replaced by a single flat TRAIL_STOP_PCT below
# (tiers + THIN_LIQUIDITY_TRAILING_STOP_MULT halving were the "variable trail
# stop protections" removed). Constants kept, now unused by _trail_pct_for(),
# in case a future tier-based feature wants them back.
TRAILING_STOP_EXTREME = 12.0  # more room for extreme movers
TRAILING_STOP_HIGH    = 10.0  # high momentum
TRAILING_STOP_MEDIUM  =  8.0  # medium momentum
TRAILING_STOP_NORMAL  =  8.0  # default trailing stop

# 2026-08-22, user request: flat trailing-stop floor for every position,
# replacing the tiered/thin-liquidity system above. See _trail_pct_for()
# in enhanced.py -- single source of truth for every trailing-stop placement.
# 2026-08-24, user request: 1.5% -> 4%, then settled at 2.5% -- the EMA15
# delta/trend-drop checks (EMA15_EXIT_DELTA_PCT, EMA15_TREND_DROP_PCT) are
# the primary per-minute defense against a genuinely deteriorating
# position; this flat stop is the worst-case backstop for a move between
# two 1-min checks, not the front line, so it carries more room than the
# original 1.5% floor without going as wide as the first 4% pass.
# 2026-08-25, user request: 2.0% -> 1.0% -- tighter backstop now that entry
# (_check_ema_trend_alignment, EMA7 delta + EMA7-vs-EMA15) and exit
# (check_ema9_exit, an EMA9 trailing stop) both also gate on the trend.
# 2026-08-26, user request: 1.0% -> 1.5%. The 1% setting whipsawed a
# meaningful share of today's trades (several stopped out in single-digit
# seconds -- BZ's worst loss of the day was a 36-second, -$4.48 stop-out).
# A precise backtest to justify a specific replacement value wasn't
# possible -- attempted a 2.5% comparison and a full percentage sweep, but
# the sweep's own 1.0% baseline didn't reproduce today's actual results
# even at the same setting (yfinance's free 1-min bar Low is noisier than
# what a real resting stop order actually saw, so simulated fills triggered
# well before the real ones did -- e.g. WOLF: real hold 40 min/+$4.75 vs.
# simulated 5 min/-$1.48 on the identical trade). 1.5% is a live-tested
# choice made without that data, not a backtested-optimal one -- judge it
# from live results, not this comment.
TRAIL_STOP_PCT = 1.5

# Once a position is profitable, widen the trailing stop to give back only
# this fraction of the unrealized gain -- e.g. +10% unrealized -> stop at
# 10 * 0.20 = 2.0% (wider than the 1.5% floor, so the trade gets more room
# as it proves itself). Only takes effect when it computes wider than
# TRAIL_STOP_PCT; a small or negative gain still uses the flat floor.
PROFIT_TRAIL_GIVEBACK_PCT = 20.0

# 2026-09-01, user request ("change the trail stop exit to atr based values"):
# the trailing-stop floor is now widened per-symbol by ATR when ATR is
# meaningfully wider than the floor -- a volatile name gets a volatility-scaled
# stop instead of the same fixed leash as a quiet one. ATR is computed from the
# intraday 1-min bars (the same data every other entry/exit gate uses), then
# scaled by ATR_TRAIL_MULTIPLIER. TRAIL_STOP_PCT stays the floor and
# ATR_TRAIL_MAX_PCT is the ceiling, so no single wild ATR reading can blow the
# stop out to double digits; profit giveback (above) still widens past both on
# a winner. ATR data unavailable/insufficient -> fail open to the flat floor.
ATR_TRAIL_ENABLED    = os.getenv("ATR_TRAIL_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
ATR_TRAIL_PERIOD     = int(os.getenv("ATR_TRAIL_PERIOD", "14"))
ATR_TRAIL_MULTIPLIER = float(os.getenv("ATR_TRAIL_MULTIPLIER", "1.5"))
ATR_TRAIL_MAX_PCT    = float(os.getenv("ATR_TRAIL_MAX_PCT", "4.0"))

# Confidence ratchet: once a position is up CONF_RATCHET_TRIGGER_GAIN_PCT or
# more, tighten its trailing stop to lock in the gain sooner -- scaled by how
# confident the original entry signal was, so a trade we were more sure about
# protects its profit faster. Never tightens a position still underwater, and
# never applies below SWAP_MIN_CONFIDENCE (0.75) -- same "high confidence" bar
# used for swaps elsewhere. Full scale (2x tighter) at confidence 1.0.
# Positions restored after a bot restart carry no real recorded confidence
# (placeholder 0.0) and are naturally excluded -- the formula only kicks in
# above 0.75, so an unknown/restored entry is correctly never ratcheted.
CONF_RATCHET_ENABLED          = True
CONF_RATCHET_TRIGGER_GAIN_PCT = 4.0   # unrealized gain % required before tightening kicks in
CONF_RATCHET_MAX_TIGHTEN      = 0.5   # at confidence=1.0, tightened stop = tier stop * this

# Legacy (backward compat)
STOP_LOSS_PCT   = 3.0
TAKE_PROFIT_PCT = 18.0

# ------------------------------------------------------------------------------------------
# Dynamic ATR-Based Tier Assignment
# Lower thresholds = more stocks classified as high-volatility = tighter TP/SL
# ------------------------------------------------------------------------------------------
USE_DYNAMIC_TIERS  = True
ATR_TIER_EXTREME   = 5.0   # was 7.0
ATR_TIER_HIGH      = 3.0   # was 5.0
ATR_TIER_MEDIUM    = 1.5   # was 3.0

# Legacy static lists (used only if USE_DYNAMIC_TIERS=False)
EXTREME_MOMENTUM_STOCKS = ["UGRO", "VCX", "PTLE", "BIAF", "SATL", "ELAB"]
HIGH_MOMENTUM_STOCKS    = ["QNTM", "MRLN", "DMRA", "RCAX", "ALDX", "NAMM", "PAYP", "SER", "NAUT", "CGV"]

# ------------------------------------------------------------------------------------------
# Adaptive Scan Intervals (VIX-Based)
# ------------------------------------------------------------------------------------------
ADAPTIVE_INTERVALS          = True
SCAN_INTERVAL_EXTREME_VOL   = 3    # VIX > 30
SCAN_INTERVAL_HIGH_VOL      = 5    # VIX 26-30
SCAN_INTERVAL_MODERATE_VOL  = 10   # VIX 22-26
SCAN_INTERVAL_NORMAL_VOL    = 15   # VIX 18-22
SCAN_INTERVAL_CALM_VOL      = 20   # VIX 15-18
SCAN_INTERVAL_LOW_VOL       = 30   # VIX < 15
SCAN_INTERVAL_MIN            = 10  # Default fallback

# -----------------------------------------------------------------
# Kill Mode -- Emergency Capital Protection
# Triggers a full portfolio close when extreme bear conditions hit.
# -----------------------------------------------------------------
KILL_MODE_VIX_LEVEL    = 40.0   # Absolute VIX level that triggers kill mode (2008/2020: 80+, crash: 40+)
KILL_MODE_SPY_DROP_PCT =  3.0   # SPY intraday drop from open (%) triggers kill mode
KILL_MODE_VIX_ROC_PCT  = 50.0   # VIX spike: up >50% in last 5 hours triggers kill mode
KILL_MODE_TRAIL_PCT    =  0.5   # PDT-safe hairpin trailing stop % placed on today's positions

# Market Hours Tuning
USE_MARKET_HOURS_TUNING    = True
PREMARKET_SCAN_INTERVAL    = 10
# 2026-09-01: entry window opens at 09:14 ET now (ENTRY_WINDOW_START_ET) --
# prep scans (EMA/strategy warm-up, orders still blocked) run 09:05-09:14 so
# the first executable cycle at 09:14 isn't trading blind.
PREP_SCAN_START_ET         = "09:05"
# 2026-09-02, user request ("check all the polling loops start at 9.25AM ET
# to avoid delays"): once-per-day morning-readiness trigger at 09:25 ET. The
# main loop forces a fresh scan cycle and kicks the ActiveListRefresher
# (ti_capture + Alpaca movers + prewarm_entry_ema run immediately) so every
# polling loop has freshly ticked before the 09:30 open -- EMA signals ready
# by 09:29, first orders at 09:30. Sits between ENTRY_WINDOW_START_ET (09:14,
# first executable scan) and MARKET_OPEN (09:30), and is scoped to the morning
# segment only (the afternoon segment has its own 14:45 reopen trigger).
MORNING_READINESS_ET = "09:25"
# 2026-08-24, user request: regular-hours discovery scan every 1 min (was 3) --
# so a position that exits gets a re-entry shot within the same minute
# instead of waiting up to 3-5 min, to actually catch a swing back in.
REGULAR_HOURS_SCAN_INTERVAL = 1
AFTERHOURS_SCAN_INTERVAL   = 10

# Position-Based Adaptive Scanning
USE_POSITION_TUNING      = True
HIGH_POSITION_INTERVAL   = 1    # was 5 (2026-08-24, user request: every-minute rescan regardless of position count)
NORMAL_POSITION_INTERVAL = 1    # was 3
LOW_POSITION_INTERVAL    = 1    # was 2

# ------------------------------------------------------------------------------------------
# VIX Rate-of-Change Filter
# ------------------------------------------------------------------------------------------
USE_VIX_ROC_FILTER  = True
VIX_ROC_THRESHOLD   = 20.0   # Block entries if VIX up >20% in last hour
VIX_ROC_PERIOD      = 5

# ------------------------------------------------------------------------------------------
# Live Trending Discovery
# ------------------------------------------------------------------------------------------
USE_LIVE_TRENDING       = False
TRENDING_SCAN_INTERVAL  = 60
TRENDING_MAX_RESULTS    = 20
TRENDING_MIN_MOMENTUM   = 3.0

# ------------------------------------------------------------------------------------------
# Finnhub Integration
# ------------------------------------------------------------------------------------------
USE_FINNHUB_DISCOVERY      = False
FINNHUB_API_KEY            = os.getenv("FINNHUB_API_KEY", "")
PRICE_DATA_SOURCE          = os.getenv("PRICE_DATA_SOURCE", "alpaca").strip().lower()
USE_FINNHUB_HISTORICAL     = PRICE_DATA_SOURCE == "finnhub" or os.getenv("USE_FINNHUB_HISTORICAL", "false").strip().lower() in ("1", "true", "yes")
USE_SENTIMENT_GATE         = False
SENTIMENT_BULLISH_THRESHOLD = 0.6

# 2026-08-26, user request ("top 20... universe should limit to the latest
# trade ideas scrapping", then same-day follow-up "keep alpaca movers... top
# 30 signals together"): this is now the COMBINED cap on (Alpaca-movers queue
# + Yahoo primary), not the scraper alone -- see get_scan_targets() in
# equity/scan.py. Movers get priority; the Yahoo pool fills whatever is left,
# up to this shared ceiling. Keep the active long/short universe broad enough
TI_PRIMARY_SCAN_BATCH_LIMIT                       = int(__import__('os').getenv('TI_PRIMARY_SCAN_BATCH_LIMIT', '30'))
ACTIVE_SCAN_SNAPSHOT_INTERVAL_MIN                 = int(__import__('os').getenv('ACTIVE_SCAN_SNAPSHOT_INTERVAL_MIN', '10'))

# Sector sympathy scanner -- injects peer tickers when a leader stock fires

# EDGAR 8-K feed scanner -- injects tickers from material event filings (free, no auth)
USE_EDGAR_SCANNER            = os.getenv("USE_EDGAR_SCANNER",    "true").lower() in ("1", "true", "yes")
EDGAR_SCANNER_INTERVAL_MIN   = int(os.getenv("EDGAR_SCANNER_INTERVAL_MIN",   "10"))
USE_PREOPEN_INTELLIGENCE     = os.getenv("USE_PREOPEN_INTELLIGENCE", "true").lower() in ("1", "true", "yes")
PREOPEN_INTELLIGENCE_SCAN_INTERVAL_MIN = int(os.getenv("PREOPEN_INTELLIGENCE_SCAN_INTERVAL_MIN", "15"))
PREOPEN_INTELLIGENCE_MAX_TICKERS = int(os.getenv("PREOPEN_INTELLIGENCE_MAX_TICKERS", "20"))
PREOPEN_USE_REGIME_GATING    = os.getenv("PREOPEN_USE_REGIME_GATING", "true").lower() in ("1", "true", "yes")
PREOPEN_USE_SENTIMENT_GATING = os.getenv("PREOPEN_USE_SENTIMENT_GATING", "true").lower() in ("1", "true", "yes")

# ------------------------------------------------------------------------------------------
# Daily Limits
# -----------------------------------------------------------------
POSITION_CHECK_MIN       = 5
# 2026-08-28, user request ("don't stop the trading today I want to fix all
# the issues today"): env-overridable so today's debugging session can raise
# the halt threshold via .env without a code edit -- defaults unchanged.
DAILY_LOSS_LIMIT_BULL_PCT = float(os.getenv("DAILY_LOSS_LIMIT_BULL_PCT", "1.0"))   # Halt if down >1% of start equity in bull regime
DAILY_LOSS_LIMIT_BEAR_PCT = float(os.getenv("DAILY_LOSS_LIMIT_BEAR_PCT", "2.0"))   # Halt if down >2% of start equity in bear regime (wider room)
DAILY_PROFIT_TARGET       = 3500.0

# 2026-08-18, user request: restrict new entries to regular hours only
# (09:30-16:00 ET, was 07:30-20:00) -- pre/post-market LiquiditySweep fires
# were walking the entry re-chase price 20-30% in thin extended-hours books
# (KEEL/TTD, 2026-08-17) before ever getting filled. New entries (either
# direction) only submitted within this ET window. Existing positions are
# completely unaffected: protect_positions, every close_*/check_*_stop path,
# and detect_stopped_out_positions all run from the orchestrator main loop
# regardless of this window, same as they already ignore is_market_open.
# Deliberately a separate, narrower gate from MarketState.is_market_open
# (07:00-20:00) rather than tightening that shared flag in place --
# is_market_open also drives allocation-split logic that isn't part of this
# ask (options-lull-hours logic was removed 2026-09-01 with options trading).
# 2026-08-22, user request ("Trading hours 9.25am ET to 3.50PM ET"): was
# "09:30" -- moved 5 min earlier so fresh universe data (now the in-process
# Yahoo refresh, see DISCOVERY_WINDOW_START_ET below) is already available
# the moment the entry window opens instead of trading blind at first.
# 2026-08-27, user request ("fix the stock universe check from ti web
# scrapping or alpaca movers starting 8:55 ET and perform the 3 min check
# till 10:30 ET, but don't trade until 9:25 ET"): keep the requested 09:25
# entry start while allowing discovery to warm up earlier. Universe discovery
# (Yahoo/TI-primary capture + Alpaca movers, see DISCOVERY_WINDOW_START_ET /
# _run_discovery's gating in scan_and_trade()) starts at 08:55, giving at
# least 30 minutes of warm-up before entries are allowed at 09:25.
# 2026-09-01, user request ("time for entry 9:14AM to 11:00AM and 2:45 PM to
# 3:50PM ET"): the entry window is now TWO disjoint segments -- 09:14-11:00
# and 14:45-15:50 -- separated by a midday break (ENTRY_WINDOW_BREAK_START_ET
# / ENTRY_WINDOW_BREAK_END_ET below) during which the book is hard-flatted
# (LUNCH_FLAT_TIME_ET) and no entry/re-entry orders are allowed. This is the
# morning segment's start; PREP_SCAN_START_ET above shifted to 09:05 so the
# first executable cycle at 09:14 isn't trading blind.
ENTRY_WINDOW_START_ET = "09:14"

# 2026-08-27, user request (see ENTRY_WINDOW_START_ET above): universe
# discovery (TI-capture trigger + scan_alpaca_movers, both inside
# _run_discovery) is allowed to run starting this early -- well before
# ENTRY_WINDOW_START_ET -- so the scan universe is already warm the moment
# trading opens. Nothing gated by ENTRY_WINDOW_START_ET (order submission)
# is affected; see scan_and_trade()'s two-stage gate.
DISCOVERY_WINDOW_START_ET = "08:55"
# 2026-09-01, user request ("time for entry 9:14AM to 11:00AM and 2:45 PM to
# 3:50PM ET"): the entry window is now TWO disjoint segments. These two
# literals are the midday break between them -- the morning segment runs
# [ENTRY_WINDOW_START_ET, ENTRY_WINDOW_BREAK_START_ET] (09:14-11:00) and the
# afternoon segment runs [ENTRY_WINDOW_BREAK_END_ET, ENTRY_WINDOW_END_ET]
# (14:15-15:44). During the break (11:00-14:15) order submission is fully
# blocked AND the book is hard-flatted (LUNCH_FLAT_TIME_ET below): every
# position closed and every open order cancelled, per the user's "at 11AM
# close all positions and open orders, and reenter only at 2:45PM" request.
# Universe discovery (_within_discovery_window) deliberately keeps running
# through the break so the afternoon segment trades on a warm universe.
# Ordering is enforced at import time (see the asserts near EOD_CLOSE_TIME).
ENTRY_WINDOW_BREAK_START_ET = "11:00"   # morning entry segment ends / lunch flat begins
# 2026-09-04, user request: afternoon reopen 2:15PM ET (was 2:45PM) -- 30
# more minutes of runway before the 15:44 EOD flat, so afternoon trades can
# arm MFE and develop instead of entering in the final hour. End stays 15:44.
ENTRY_WINDOW_BREAK_END_ET   = "14:15"   # afternoon entry segment opens
# 2026-08-18, user request ("no new buys after 2:45" -- 2:45 PM CDT = 15:45
# ET -- then same day, refined to "change the eod close time and no trades
# time to 3:50pm ET... after this no new entry positions only keep the
# existing positions overnight if they meet guardrails, and exit only"):
# was "16:00", which left a window AFTER EOD_CLOSE_TIME started flattening
# positions where entries still fired anyway -- confirmed live, AXTI got
# shorted fresh at 15:55 ET while close_guardrail_fail_positions was
# actively closing OTHER names down for the same session, opening brand-new
# risk in the exact window the bot should only be winding down. Must never
# be later than EOD_CLOSE_TIME again -- see the assert next to
# EOD_CLOSE_TIME below, which enforces this at import time instead of
# trusting the two literals to stay in sync by hand.
# 2026-09-01: this is now the AFTERNOON entry segment's end (14:45-15:44),
# still the absolute last cutoff for any entry/re-entry order placement --
# the enhanced.py re-arm and blocked-entry-expiry gates key on it unchanged.
# 2026-09-03, user request: afternoon session ends 3:45PM ET (was 3:50PM).
# 2026-09-03, user request (2nd): afternoon ends 3:44PM ET -- MARA/MSTX entered
# 15:43-15:44 and were flattened at 15:50 with ~4 min of runway; every minute
# earlier the book must be flat is a minute less forced-exit slippage risk.
ENTRY_WINDOW_END_ET   = "15:44"

# Quarterly Profit Target
USE_QUARTERLY_TARGET        = True
QUARTERLY_PROFIT_TARGET_PCT = 50.0   # Halt new entries once +50% equity this quarter

# 2026-08-12, user request: protect against an Alpaca maintenance margin call.
# Nothing previously tracked maintenance_margin vs equity at all --
# MIN_BUYING_POWER_PCT only reserves spending buffer for new entries, and
# MAX_POSITION_CONCENTRATION_PCT/CORRELATION_GROUPS cap individual position/
# group size, neither watches the account's aggregate margin cushion. Trips
# when equity falls below MARGIN_CUSHION_MIN_RATIO x maintenance_margin (1.5x
# = equity has to drop another 33% from here before an actual call at 1.0x).
# Blocks new entries only (like the daily-loss-limit halt) -- existing
# positions keep their normal stops/exits, doesn't force a sale at a bad
# price. See orchestrator._margin_cushion_ok / scan_and_trade.
MARGIN_SAFEGUARD_ENABLED   = True
MARGIN_CUSHION_MIN_RATIO   = 1.5

# ------------------------------------------------------------------------------------------
# Extended Hours Trading
# ------------------------------------------------------------------------------------------
EXTENDED_HOURS   = True
PREMARKET_START  = "07:00"
MARKET_OPEN      = "09:30"
MARKET_CLOSE     = "16:00"
AFTERHOURS_END   = "20:00"

# Set FORCE_SCAN=1 (env var) or pass --force CLI flag to bypass the
# market-hours gate when a high-confidence opportunity is spotted.
FORCE_SCAN = os.getenv("FORCE_SCAN", "false").lower() in ("1", "true", "yes")

# -----------------------------------------------------------------
# Midday Lunch Flat
# -----------------------------------------------------------------
# 2026-09-01, user request ("at 11AM close all positions and open orders,
# and reenter only at 2:45PM and again exist all at 3:50"): with the entry
# window now two disjoint segments (09:14-11:00 + 14:45-15:50, see
# ENTRY_WINDOW_BREAK_START_ET / ENTRY_WINDOW_BREAK_END_ET above), the book
# must be FULLY flat through the midday break -- every equity position
# closed, every open order cancelled, and no re-entry until the afternoon
# segment opens. Runs from the orchestrator's schedule.every(1).minutes
# job (_lunch_flat_job); the sweep's own time-of-day gate does the real
# work. LUNCH_FLAT_TIME_ET is bound to ENTRY_WINDOW_BREAK_START_ET so the
# flat can never fire before entries stop (assert below).
LUNCH_FLAT_ENABLED   = True
LUNCH_FLAT_TIME_ET   = ENTRY_WINDOW_BREAK_START_ET

# -----------------------------------------------------------------
# EOD (End-of-Day) Position Close
# Intraday strategies should never be held overnight -- close by EOD_CLOSE_TIME
# -----------------------------------------------------------------
EOD_CLOSE_ENABLED    = True
# 2026-08-18, user request: "change the eod close time and no trades time to
# 3:50pm ET" -- was 15:45 (10 min/15:50 before that, widened 2026-08-12,
# tightened back same day as this ask). 2026-09-03, user request: back to
# 3:45PM ET (afternoon session ends 15:45, not 15:50). 2026-09-03 (2nd):
# 3:44PM ET -- same late-runway reasoning as ENTRY_WINDOW_END_ET above.
# NOTE: _exchange_close_for_today() takes the EARLIER of this time and the
# exchange-calendar close minus 10 min, so an early-close session still
# flattens in time even when its close is before 15:54.
EOD_CLOSE_TIME       = "15:44"
# 2026-09-01: the lunch flat must fire exactly when the morning entry segment
# ends (otherwise the book could stay open into the break) and the two entry
# segments must not overlap and must both stay inside the final 15:50 cutoff.
assert LUNCH_FLAT_TIME_ET == ENTRY_WINDOW_BREAK_START_ET, (
    f"LUNCH_FLAT_TIME_ET ({LUNCH_FLAT_TIME_ET}) must equal ENTRY_WINDOW_BREAK_START_ET ({ENTRY_WINDOW_BREAK_START_ET})"
)
assert (
    ENTRY_WINDOW_START_ET < ENTRY_WINDOW_BREAK_START_ET
    and ENTRY_WINDOW_BREAK_START_ET <= ENTRY_WINDOW_BREAK_END_ET
    and ENTRY_WINDOW_BREAK_END_ET <= ENTRY_WINDOW_END_ET
), (
    f"entry segments must order START < BREAK_START <= BREAK_END <= END, got "
    f"START={ENTRY_WINDOW_START_ET} BREAK_START={ENTRY_WINDOW_BREAK_START_ET} "
    f"BREAK_END={ENTRY_WINDOW_BREAK_END_ET} END={ENTRY_WINDOW_END_ET}"
)
# 2026-09-02: the morning-readiness trigger must land inside the morning
# segment and before the open -- after the first executable scan (09:14),
# before MARKET_OPEN (09:30), so the forced refresh produces signals that are
# still fresh at the bell. Fails loudly at import time instead of silently
# drifting out of the readiness band.
assert (
    PREP_SCAN_START_ET < ENTRY_WINDOW_START_ET < MORNING_READINESS_ET < MARKET_OPEN
), (
    f"morning readiness must order PREP < ENTRY_START < READINESS < MARKET_OPEN, got "
    f"PREP={PREP_SCAN_START_ET} ENTRY_START={ENTRY_WINDOW_START_ET} "
    f"READINESS={MORNING_READINESS_ET} MARKET_OPEN={MARKET_OPEN}"
)
# 2026-09-02 red-team pass: every "HH:MM" constant below is strptime'd inside
# the hot run loop (and _within_discovery_window compares some as raw strings).
# A single malformed value (typo, OneDrive merge artifact) would NOT crash the
# process -- the main loop's broad except logs "Main loop error" every tick and
# starves heartbeats until the watchdog stall-restarts into the same error,
# i.e. a config-typo restart storm. Validate the format once here, at import
# time, where a bad value fails loudly with the offending constant's name.
def _require_hhmm(name: str, value: str) -> None:
    # Zero-padding is REQUIRED, not stylistic: _within_discovery_window compares
    # these constants as raw strings, where "9:5" > "10:00" lexicographically
    # would silently break every window. strptime alone is NOT strict enough
    # here (it happily parses "9:5").
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise AssertionError(
            f"{name}={value!r} must be a zero-padded 24-hour 'HH:MM' time string "
            f"(zero-padding matters: these are also compared as raw strings)"
        )
    try:
        datetime.datetime.strptime(value, "%H:%M")
    except ValueError:
        raise AssertionError(f"{name}={value!r} is not a valid 24-hour time (e.g. '25:00')")

for _time_const_name in (
    "PREP_SCAN_START_ET", "MORNING_READINESS_ET", "DISCOVERY_WINDOW_START_ET",
    "ENTRY_WINDOW_START_ET", "ENTRY_WINDOW_BREAK_START_ET", "ENTRY_WINDOW_BREAK_END_ET",
    "ENTRY_WINDOW_END_ET", "PREMARKET_START", "MARKET_OPEN", "MARKET_CLOSE",
    "AFTERHOURS_END", "EOD_CLOSE_TIME", "LUNCH_FLAT_TIME_ET",
):
    _require_hhmm(_time_const_name, globals()[_time_const_name])
# 2026-08-18: entries must never still be allowed once the EOD close sweep has
# started -- see ENTRY_WINDOW_END_ET above (the AXTI-at-15:55-ET incident this
# guards against). Fails loudly at import time instead of silently drifting.
assert ENTRY_WINDOW_END_ET <= EOD_CLOSE_TIME, (
    f"ENTRY_WINDOW_END_ET ({ENTRY_WINDOW_END_ET}) must be <= EOD_CLOSE_TIME ({EOD_CLOSE_TIME})"
)
_market_close_h, _market_close_m = map(int, MARKET_CLOSE.split(":"))
_eod_close_h, _eod_close_m = map(int, EOD_CLOSE_TIME.split(":"))
_eod_gap_min = (_market_close_h * 60 + _market_close_m) - (_eod_close_h * 60 + _eod_close_m)
assert 10 <= _eod_gap_min <= 16, (
    f"EOD_CLOSE_TIME ({EOD_CLOSE_TIME}) must be 10-16 minutes before MARKET_CLOSE ({MARKET_CLOSE}), "
    f"got {_eod_gap_min} min (2026-09-03 (2nd): 15:44 ET close = 16 min gap, was 15:45 = 15 min)"
)

# -----------------------------------------------------------------
# Local coordination state + Loss Guardian + Auto-Deploy (2026-09-02)
# -----------------------------------------------------------------
# All cross-process coordination files live OUTSIDE the OneDrive-synced repo,
# under %LOCALAPPDATA%\ApexTrader\state, so the watchdog (elevated), the bot,
# the loss guardian, and the agent can hand each other flags without OneDrive
# sync races between processes at different integrity levels (the
# .mainbot.lock double-runner incident, 2026-09-02, was exactly that).
#
#   flat_request.flag   guardian -> bot: hard daily-loss halt (flatten + block)
#   deploy_requested.flag  agent/user -> watchdog: restart main.py on new code
_local_base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
STATE_DIR       = os.path.join(_local_base, "ApexTrader", "state")
GUARDIAN_FLAT_FILE = os.path.join(STATE_DIR, "flat_request.flag")
DEPLOY_FLAG_FILE   = os.path.join(STATE_DIR, "deploy_requested.flag")

# Loss Guardian (scripts/guardian.py) -- an independent, always-on backstop on
# top of the in-bot DAILY_LOSS_LIMIT_* halt. Deliberately NOT tied to the .env
# DAILY_LOSS_LIMIT_BULL/BEAR_PCT values (which are loose today -- 5%/8% in
# .env vs the 1%/2% code defaults). ALERT = email + state file; HALT = write
# flat_request.flag (bot flattens) + guardian flat-sells directly if the bot's
# heartbeat is stale. Defaults: alert at -0.75%, hard halt at -1.5%.
GUARDIAN_ALERT_PCT = float(os.getenv("GUARDIAN_ALERT_PCT", "0.75"))
GUARDIAN_HALT_PCT  = float(os.getenv("GUARDIAN_HALT_PCT", "1.5"))
GUARDIAN_STALE_HEARTBEAT_SEC = int(os.getenv("GUARDIAN_STALE_HEARTBEAT_SEC", "300"))
# Guardian only takes flatten action inside this ET band (the bot has reset its
# daily baseline by 09:35 ET and the day is over after 15:44 -- matches
# EOD_CLOSE_TIME; env can override but the fallback tracks the config default).
GUARDIAN_POLL_START_ET = os.getenv("GUARDIAN_POLL_START_ET", "09:35")
GUARDIAN_POLL_END_ET   = os.getenv("GUARDIAN_POLL_END_ET",   "15:44")
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

# -----------------------------------------------------------------
# Guardrail-Fail Overnight Exit
# 2026-08-12, user request: force-close ANY open position (any strategy, not
# just EOD_CLOSE_STRATEGIES) that currently fails the standard liquidity/
# quality guardrails -- avg daily volume, float shares, market cap (same
# thresholds the live scanner uses: MIN_AVG_DAILY_VOLUME_REGULAR_HOURS,
# MIN_FLOAT_SHARES, MIN_MARKET_CAP). Only names that still pass those
# guardrails get held after-hours/overnight. Originally 15:55 (5 min before
# close); aligned to EOD_CLOSE_TIME same day at the user's request so both
# EOD closes fire together.
# -----------------------------------------------------------------
GUARDRAIL_EOD_CLOSE_ENABLED = False  # 2026-08-23, user request: disabled, no longer relevant
GUARDRAIL_EOD_CLOSE_TIME    = EOD_CLOSE_TIME   # fires alongside close_eod_positions

# -----------------------------------------------------------------
# Price Drift Stop (30-min check, same-day entries only)
# 2026-08-13, user request: this morning's losses shared a common pattern --
# bought right after the open on a gap/momentum signal, then faded back as
# the pop unwound, giving back 4-8% before the normal trailing stop (set by
# protect_positions() at the strategy's dynamic tier) finally caught it.
# Confirmed live: DFSC, HLIT, EROC, JACK all bought within the first 20 min
# of the 2026-08-13 open, all gave back most of their gain before the wider
# stop fired. This is a tighter, faster check layered on top: exit if price
# has moved against the position by more than PRICE_DRIFT_STOP_PCT versus
# EITHER entry price OR where it was PRICE_DRIFT_LOOKBACK_MIN ago (longs:
# drop > 1%; shorts: rise > 1%, mirrored). Checked every
# PRICE_DRIFT_CHECK_INTERVAL_MIN (10 min), in step with the TI screener run.
#
# 2026-08-13, refined same day: originally checked every 30 min against both
# references -- narrowed same day to a single reference (30-min-ago only,
# entry-price leg dropped) with poll frequency raised to every
# PRICE_DRIFT_CHECK_INTERVAL_MIN (10 min) so the comparison stays accurate
# without waiting a full 30 min between looks.
#
# 2026-08-14, user correction: entry-price leg restored (briefly raised to
# 1.5% same day, then reverted back to 1.0% -- see below). Confirmed live
# why the entry-price leg mattered: TE dropped 2.69% off its OWN entry price
# and the (at the time) 30-min-ago-only check never looked at entry at all,
# so it never triggered no matter how far TE fell from where it was bought
# -- a slow, steady bleed from entry that never shows a full move within any
# single 10-min-to-10-min window is exactly the gap a 30-min-ago-only
# comparison misses. Both references checked again, OR'd together, same as
# the very first version, still backfilling the 30-min-ago leg from real bar
# data after a restart (see _backfill_drift_reference in enhanced.py).
# Threshold kept at the original 1.0% per explicit follow-up correction the
# same day ("keep 1% for price move against from 30mins or the purchased
# price").
#
# Scoped to same-day entries only (user's choice) -- a multi-day swing hold
# is expected to tolerate more than 1% noise on the way to a bigger target,
# and scoping by entry date, not strategy, survives the strategy-name loss a
# process restart causes (see _rebuild_entry_log_from_orders).
# -----------------------------------------------------------------
PRICE_DRIFT_STOP_ENABLED       = False  # 2026-08-22, user request: disabled -- replaced by the flat/profit-scaled trailing stop (TRAIL_STOP_PCT) and a separate 1-min "isn't moving" check
PRICE_DRIFT_STOP_PCT           = 1.0    # % adverse move vs. EITHER entry OR the price PRICE_DRIFT_LOOKBACK_MIN ago that triggers an exit
PRICE_DRIFT_CHECK_INTERVAL_MIN = 10     # how often the check runs, in step with the TI screener run
PRICE_DRIFT_LOOKBACK_MIN       = 30     # how far back the comparison price is from ("price from 30 min ago")

# -----------------------------------------------------------------
# Stagnant Position Stop -- independent fast-loop exit check, polled every
# STAGNANT_STOP_CHECK_INTERVAL_MIN (1 min). 2026-08-22 through 2026-08-25
# this ran an EMA15 close-cross rule (check_ema15_exit, entry-anchored
# delta/trend-drop/breakdown variants) -- removed 2026-08-25, user request:
# "remove the ema15 delta check, only keep the ema3 and ema7 positive
# slope." check_ema9_exit (an EMA9_TRAIL_PCT% trailing stop on EMA9, see
# below) is the only per-minute exit check now; these two flags still gate it.
STAGNANT_STOP_ENABLED             = True
STAGNANT_STOP_CHECK_INTERVAL_MIN  = 1

# 2026-08-27, user request ("in the next 18secs before order executed the
# code should have cancelled the order"): check_pending_entries_ema's own
# gate recheck (cancel a resting entry order once the EMA trend that
# justified it no longer holds) now runs on this separate, much faster
# timer instead of sharing STAGNANT_STOP_CHECK_INTERVAL_MIN (1 min) with
# check_ema9_exit/check_blocked_entries_ema -- confirmed live 2026-08-27:
# an OKTA trailing-buy was armed on a fresh gate-pass, then filled just 18
# seconds later after price reversed in between, with zero chance for a
# 60s-interval recheck to ever catch it. A 5s recheck can't GUARANTEE
# catching every fill that fast either (the order is broker-side and can
# fill on any tick; a cancel request in flight can still lose that race --
# no polling interval closes that to zero), but it turns "60s vs an
# 18-second fill = never" into "5s vs an 18-second fill = ~3-4 real
# chances." Only this specific check moved -- check_ema9_exit and
# check_blocked_entries_ema stay on the original 1-min cadence, since
# neither has this same "resting broker order could fill any second" risk
# profile (see engine/orchestrator.py's _start_software_stop_thread).
PENDING_ENTRY_RECHECK_SEC = int(os.getenv("PENDING_ENTRY_RECHECK_SEC", "5"))
PROTECTION_LIMIT_REPLACE_AFTER_SEC = 10

# -----------------------------------------------------------------
# EMA Trend Alignment Filter -- 2026-08-22, user request: "ensure the trend
# is in the way trade is intended" before entering, checked alongside the
# trail-buy entry. Simplified to an EMA's own slope on 1-min bars: this
# minute's EMA minus last minute's EMA must be positive for a long,
# negative for a short -- rejects the entry outright otherwise. Missing or
# insufficient bar data also blocks because EMA alignment is a hard condition.
# 2026-08-24, user request: EMA9 -> EMA7 -- faster/more responsive slope,
# allows an earlier entry read; paired with the entry-anchored EMA15 delta
# exit above so a still-below-EMA15 entry isn't rejected outright, it's
# instead watched for whether it keeps getting worse.
# 2026-08-25, user request: EMA3 delta required alongside EMA7's for a
# while (both had to agree), then dropped ("remove ema3 delta positive")
# once paired with a structural condition -- EMA7 above EMA15 for a long,
# below it for a short ("add one more condition of ema 7 above ema 15 for
# entry long order and vice versa for short"). See
# _check_ema_trend_alignment's own docstring for both current conditions.
EMA_TREND_FILTER_ENABLED = True
EMA_TREND_MIN_BARS       = 10  # need at least this many 1-min bars before trusting EMA7's slope
EMA_ENTRY_CONFIRM_SEC    = 60  # legacy; entry confirmation now uses closed 1-min candles, not 10s polling
EMA_ENTRY_CONFIRM_CHECKS = 2
EMA_ENTRY_MIN_SPREAD_PCT = float(os.getenv("EMA_ENTRY_MIN_SPREAD_PCT", "0.10"))
EMA_ENTRY_MIN_TRAILING_30M_RETURN_PCT = float(os.getenv("EMA_ENTRY_MIN_TRAILING_30M_RETURN_PCT", "0.20"))
REENTRY_SIZE_REDUCTION_PCT = float(os.getenv("REENTRY_SIZE_REDUCTION_PCT", "30.0"))
LOSS_BLOCK_MORNING_END_ET = os.getenv("LOSS_BLOCK_MORNING_END_ET", "10:30")
SYMBOL_DAILY_LOSS_BLOCK_COUNT = int(os.getenv("SYMBOL_DAILY_LOSS_BLOCK_COUNT", "2"))

# check_blocked_entries_ema -- 2026-08-25, user request: "each blocked
# trade should wait for next minute recheck not to completely discard the
# order" -> "the trade idea should check for every minute conditions to
# see when the new condition is met to reenter than completely discard a
# trade signal." A signal blocked only by the EMA gate above gets queued
# and re-checked every STAGNANT_STOP_CHECK_INTERVAL_MIN instead of just
# vanishing -- fires the moment the gate agrees. Originally also dropped
# after a fixed number of minutes without realigning; removed same day
# ("no expire") -- ENTRY_WINDOW_END_ET closing is now the only reason a
# queued entry ever gets dropped instead of retried.

# check_ema9_exit -- 2026-08-25, user request chain: "add exit condition
# ema 7 and ema 3 both negative for exit" -> narrowed to EMA7 alone -> a
# delta-vs-0.3%-of-price threshold -> "should only trigger is the stock
# trending down" (added price-vs-EMA9) -> "try options 2" (require the
# combined condition to persist 2 consecutive polls) -> switched from EMA7
# to EMA9 -> finally "get a ema 9 trail stop of 0.3% instead of just delta
# with 0.3% of price" -- replaced the whole delta/threshold/persistence
# stack with a proper trailing stop ON EMA9 ITSELF: track the running peak
# of EMA9 since entry (trough for a short), exit once EMA9 has pulled back
# EMA9_TRAIL_PCT% from that peak. Same shape as the existing price-based
# TRAIL_STOP_PCT trailing stop, just computed on the smoothed EMA9 line
# instead of raw price -- and unlike every version before this one, it has
# actual memory of how far the trade has run (a peak that only ratchets
# forward), the exact gap flagged by the JEM/SDOT/RZLV backtests (a flat
# threshold cuts a real winner short on its first pullback with no memory
# of the run-up). See _ema9_trail_exit_reason / _update_ema9_peak.
# 2026-08-25, user request: 0.3% -> 0.5% -- a bit more room on the trail
# itself, on top of the peak-tracking already giving the trade room to run.
EMA9_TRAIL_PCT = 0.5

# -----------------------------------------------------------------
# Swing/Multi-Day Drift Stop -- wider sibling of the price drift stop above,
# for positions NOT covered by it. check_price_drift_stop() only watches
# same-day entries (entry_log[sym]['date'] == today); anything older is
# left on its normal (much wider, dynamic-tier) trailing stop alone.
#
# 2026-08-15, user request: idea #3 of six suggested improvements, built
# after TrendBreaker's multi-day losers surfaced (NWL -5.41% held 55h,
# IMMR -2.71% held 24h, IMAX -1.85% held 21h) with nothing between entry
# and the wide trailing stop watching them for the days in between. Only
# NWL would actually have tripped a 3% cap -- IMMR/IMAX stay under it, so
# this targets tail losses, not every swing drawdown. No 30-min-ago leg
# here (doesn't map across multiple days the way it does intraday) --
# entry price alone is the reference. Checked every
# SWING_DRIFT_STOP_CHECK_INTERVAL_MIN, on the same fixed clock-grid
# pattern as the price drift stop (see _schedule_on_clock_grid in
# orchestrator.py) -- 30 min is plenty for a multi-day thesis, no need for
# 10-min granularity here.
# -----------------------------------------------------------------
SWING_DRIFT_STOP_ENABLED             = True
SWING_DRIFT_STOP_PCT                 = 3.0   # % adverse move vs. entry price alone that trims a MULTI-DAY position
SWING_DRIFT_STOP_CHECK_INTERVAL_MIN  = 30

# -----------------------------------------------------------------
# Swing Position Staleness Exit
# Positions held by strategies NOT in EOD_CLOSE_STRATEGIES (Momentum,
# Technical, etc.) are meant to ride a trend via the GTC trailing stop rather
# than close same-day. But a position that just grinds sideways/down for days
# without reaching SWING_STALE_MIN_GAIN_PCT% is dead capital -- close it out.
# -----------------------------------------------------------------
SWING_STALE_EXIT_ENABLED  = True
SWING_STALE_DAYS          = 5     # calendar days held before the check applies
SWING_STALE_MIN_GAIN_PCT  = 3.0   # required unrealized gain % by SWING_STALE_DAYS

# -----------------------------------------------------------------
# No-Gain 24h Exit
# If a position hasn't decided which way it's going within N hours of entry,
# stop waiting: exit on ANY positive gain (don't hold out for more once it's
# 8h+ old), or on a NO_GAIN_EXIT_MAX_LOSS_PCT drop (cut it before the full
# trailing stop would). Only a narrow flat band survives the check. Checked
# every scan cycle (not once/day) since the N-hour mark can fall mid-session.
# Was 24h / no downside cutoff (positive-only exit) until 2026-08-11, tightened
# to 8h with a -1.5% loss cutoff at the user's request.
# -----------------------------------------------------------------
NO_GAIN_EXIT_ENABLED     = True
NO_GAIN_EXIT_HOURS       = 8      # hours held before the check applies (was 24)
NO_GAIN_EXIT_MIN_PCT     = 0.0    # gain above this exits (must be <= this, and > max-loss, to survive)
NO_GAIN_EXIT_MAX_LOSS_PCT = -1.5  # loss at or below this also exits (new -- was no downside cutoff)

# -----------------------------------------------------------------
# MFE Give-back Stop -- protects gains a trade has ALREADY shown.
# 2026-09-03, from the 09:30-11:00 ET post-mortem: 41 morning round
# trips peaked at +$90.56 unrealized combined but realized only
# +$1.22 (1.3% MFE capture). 34/41 trips went green at some point and
# their +$94.88 of peak profit was only 31.4% captured. Typical
# pattern: green within 2-10 min of entry, exit flat/negative 2-11
# min later (CONL x4, CRCL x4, MSTX x3, SMMT, HOOD, BTDR...).
#
# Rule (check_mfe_giveback_exit(), runs on the SoftwareStopPoller
# thread): once a position's unrealized gain has EVER reached
# MFE_ARM_PROFIT_PCT this session, exit the moment the CURRENT gain
# falls below max(peak_gain * MFE_GIVEBACK_FRACTION,
# MFE_BREAKEVEN_FLOOR_PCT). The floor doubles as a breakeven-plus
# ratchet -- an armed trade can never round-trip through its entry
# again. Scoped to same-day entries only (self._entry_log date),
# same restart-survivable scoping as PRICE_DRIFT_STOP/NO_GAIN_EXIT.
# The peak is tracked in-memory from the poller's own samples; a
# restart re-seeds the peak from the current price (fail-open: we may
# miss the pre-restart peak but never invent one). Tuned from the
# 9/3 data: typical armed give-backs were 35-80% of peak, so 0.6
# locks most of a green trade's best level while leaving the PLTR
# case (peak +6.5%, held 90% of it) almost untouched.
# -----------------------------------------------------------------
MFE_GIVEBACK_ENABLED      = True
MFE_ARM_PROFIT_PCT        = 0.5   # peak unrealized gain % needed to arm the give-back watch
MFE_GIVEBACK_FRACTION     = 0.6   # exit once current gain falls below this fraction of the peak gain
MFE_BREAKEVEN_FLOOR_PCT   = 0.1   # floor: an armed trade exits before falling under entry + this %

# -----------------------------------------------------------------
# After-Hours Software Stop-Loss
# Alpaca's broker-side GTC trailing stop is only evaluated during regular
# hours (09:30-16:00 ET) -- a position can free-fall pre-market/after-hours
# with the resting stop order sitting inert. This actively checks every open
# position's loss against its stop % (same tier as the trailing stop) while
# the market isn't in regular hours, and force-closes via an extended-hours
# marketable limit order (plain market orders are rejected outside regular
# hours too).
# -----------------------------------------------------------------
AFTERHOURS_STOP_CHECK_ENABLED = True
AFTERHOURS_STOP_CHECK_ENABLED = True
AFTERHOURS_CHASE_STALE_SECONDS = 45  # re-chase (cancel + resubmit at fresh price) if the close sits unfilled this long

# ---------------------------------------------------------------------------------
# Close/Protection Order Reconciliation -- 2026-09-03, SNOW post-mortem.
# Live SNOW (9/3): a 1-share long's GTC trailing stop reserved the only share, so
# the software-stop close was rejected NINE times with Alpaca 40310000
# "insufficient qty available ... held_for_orders=1" while the position bled from
# $380.25 to $365.62 (-3.85%) before the EMA9 exit finally caught it. The exit
# paths used to cancel-then-blindly-sleep(0.4)-then-close, which races the broker's
# own cancel processing.
#
# Fix: every intentional software close (software SL, EMA9 exit, MFE give-back)
# goes through _request_reconciled_close(), which
#   1. dedupes against an already-pending close for the symbol,
#   2. cancels ONLY valid GTC protective trailing stops (never entry orders),
#   3. POLLS broker order state until the cancel is confirmed (bounded timeout),
#   4. re-reads the live position and closes exactly the remaining quantity,
#   5. re-arms GTC protection on a failed close (existing behavior, centralized).
# Disable to restore the legacy cancel+sleep+close behavior.
CLOSE_RECONCILIATION_ENABLED        = True
CLOSE_CANCEL_CONFIRM_TIMEOUT_SEC    = float(os.getenv("CLOSE_CANCEL_CONFIRM_TIMEOUT_SEC", "2.0"))
CLOSE_CANCEL_CONFIRM_POLL_SEC       = float(os.getenv("CLOSE_CANCEL_CONFIRM_POLL_SEC", "0.25"))
PENDING_CLOSE_RETRY_SEC             = float(os.getenv("PENDING_CLOSE_RETRY_SEC", "10"))

# ---------------------------------------------------------------------------------
# Execution telemetry -- non-blocking JSONL event log under
# %LOCALAPPDATA%\ApexTrader\analytics\ (machine-local, git-ignored by location,
# never in the OneDrive repo). Observability only: a telemetry failure must NEVER
# delay or break a trading decision -- see engine/telemetry.py (bounded queue,
# daemon writer, drops-on-full).
EXECUTION_TELEMETRY_ENABLED = True
TELEMETRY_QUEUE_MAX         = int(os.getenv("TELEMETRY_QUEUE_MAX", "2000"))
TELEMETRY_FLUSH_INTERVAL_SEC = float(os.getenv("TELEMETRY_FLUSH_INTERVAL_SEC", "2.0"))
AFTERHOURS_CHASE_STALE_SECONDS = 45  # re-chase (cancel + resubmit at fresh price) if the close sits unfilled this long
# 2026-08-24, user request: no post-loss re-entry cooldown anymore (was 1440min /
# 24h here). Protection is the exit stack alone -- trailing stop, per-minute
# check_ema9_exit, standalone stop-loss.

# Stale order upgrade: unfilled orders older than this get re-submitted as market/limit
STALE_ORDER_MINUTES          = 360  # minutes before an unfilled order is considered stale
STALE_ORDER_MINUTES_INTRADAY =  30  # intraday strategies (ORB, surge, etc.) -- cancel if unfilled after 30 min

# -----------------------------------------------------------------
# PDT Rules
# ------------------------------------------------------------------------------------------
PDT_ACCOUNT_MIN = 25000.0
PDT_MAX_TRADES  = 3

# Alpaca (Reg T) hard minimum equity to short at all -- confirmed 2026-08-07:
# every short order was rejected with "account is not allowed to short"
# (code 40310000) regardless of symbol, despite the account's Shorting
# Enabled toggle being on; the account's equity (~$1,000, per the quarterly
# baseline) is simply under this floor. Not configurable per-account by us --
# it's the broker's own regulatory minimum.
MIN_EQUITY_FOR_SHORT = 2000.0

# -----------------------------------------------------------------
# Email Notifications
# -----------------------------------------------------------------
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
MIN_POSITION_DOLLARS  = float(os.getenv("MIN_POSITION_DOLLARS", "5"))   # Minimum trade size in $ -- skip if downsized below this
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
# 2026-08-24, user request ("top 15 list not top 4/5"): was 3 -- matches
# TOP_N_SIGNALS now so the execution cap isn't quietly narrower than the
# ranked watchlist it's drawn from. Still bounded above by MAX_POSITIONS (12
# total open at once) and each attempt still has to individually pass
# _validate_trade -- this only raises how many the cycle is willing to TRY.
MAX_SIGNALS_PER_CYCLE = 60     # Execute at most this many signals per scan cycle

# Per-symbol HMM regime alignment: confidence bonus (not a gate) when a
# signal's direction agrees with the symbol's own 2-state Gaussian HMM regime,
# fit on 1-min bars over the trailing lookback window.
HMM_REGIME_LOOKBACK_DAYS        = 2     # days of 1-min bars used to fit the HMM
HMM_REGIME_CONFIDENCE_BOOST     = 0.03  # added to confidence when aligned

# Parallel Scanning
SCAN_WORKERS        = 16   # Threads scanning symbols concurrently. Was capped at 8 to stay below
                            # alpaca-py's default urllib3 pool_maxsize=10 -- obsolete now that
                            # mount_wide_pool() (engine/utils/bars.py) raises the pool to 20 on
                            # both the stock and option data clients (2026-08-11). Faster full-
                            # universe scan completion = less time between a mover appearing and
                            # us actually checking it -- pure throughput, doesn't change what gets
                            # traded, so it's not another variable in reading tomorrow's results.
SCAN_SYMBOL_TIMEOUT = 15   # Max seconds per symbol before it is skipped

# 2026-08-27, user request ("the 1 min check algo has to work in parallel
# if needed to avoid overload issue"): check_ema9_exit,
# check_pending_entries_ema, check_blocked_entries_ema all run on the
# SoftwareStopPoller thread's tight 10s-tick budget and each does one
# fresh (bypass_cache/force_fresh) bar fetch per symbol -- network I/O,
# sequential, one symbol at a time before this. A busy day with many
# open positions/pending entries/blocked entries could stack up enough
# sequential fetches to run the tick past its budget. Lower than
# SCAN_WORKERS (16) on purpose: these run 6x more often than a scan
# cycle and typically cover far fewer symbols (positions/pending/blocked
# counts, not the full 30-symbol universe) -- no need for the same
# thread count, and the bar-fetch client pool is shared with the scan
# threads too.
POLLER_CHECK_WORKERS = 8
# SCAN_MAX_SYMBOLS: unused as of 2026-08-26. Used to cap a much larger
# multi-source combined universe (EDGAR/sympathy/movers/watchlist + an
# ~80-symbol rotating fallback list on top of the TI batch) that
# get_scan_targets() (engine/equity/scan.py) no longer assembles -- the scan
# universe is just the top TI_PRIMARY_SCAN_BATCH_LIMIT tickers from
# ti_primary.json now, so every cycle scans all of them, no rotation/cap
# needed. Left defined (not deleted) in case that assembly comes back.
SCAN_MAX_SYMBOLS    = 150
BEAR_SHORT_TARGET_RESERVE = 30  # In bear regime, reserve more scan slots for short universe backups

# ------------------------------------------------------------------------------------------
# Strategy Parameters
# ------------------------------------------------------------------------------------------
# -----------------------------------------------------------------
# Strategy enable/disable toggles -- 2026-08-14, at the user's request:
# backtested every strategy's matched entry/exit trades since each went
# live, bucketed by confidence (same methodology as VWAP_FADE_ENABLED
# below). User's rule: disable anything with an overall win rate below
# VWAPFade's own 37% (already disabled) -- EXCEPT don't judge a strategy
# on fewer than 10 completed trades, too early to call. Confidence
# gating doesn't rescue either of the two that clear that bar -- same
# finding as VWAPFade, no winning bucket even at their own ceiling:
#   Momentum           n=25  20% win  -1.73% avg  (worst bucket, 90%+: -2.19%)
#   PreMarketMomentum  n=25  32% win  -1.60% avg  (90%+: -1.47%)
# Below the n=10 floor -- win rate not trustworthy yet, left enabled:
#   Sentiment          n=9   22% win  +0.54% avg
#   LiquiditySweep     n=4   25% win  -0.80% avg
#   PMHighBreakout     n=3   33% win  -2.82% avg
#   Technical          n=3    0% win  -3.33% avg
#
# 2026-08-15 update: a fuller loss-attribution pass (371 matched trades,
# all strategies, since inception) put FloatRotation at n=41, 39% win --
# clear of the 37% line on win rate alone, but still net -$33.31, the
# second-worst dollar loser after VWAPFade, and its worst trades (DFSC
# -27%, BNRG -8.6%) were the clearest examples of the "chasing an
# already-extended move" pattern across the whole loser list. Disabled
# at the user's explicit request despite clearing the win-rate line.
# Strategy code and tuning params below are untouched for all of these;
# flip a flag to True to re-enable / False to disable. See
# scripts/test_strategy_toggles.py.
# -----------------------------------------------------------------
MOMENTUM_ENABLED            = False
SENTIMENT_ENABLED           = True
LIQUIDITY_SWEEP_ENABLED     = True
PRE_MARKET_MOMENTUM_ENABLED = False
PM_HIGH_BREAKOUT_ENABLED    = True
TECHNICAL_ENABLED           = True
FLOAT_ROTATION_ENABLED      = False

TECHNICAL = {
    "rsi_oversold":   30,
    "rsi_overbought": 70,
    "volume_surge":   2.0,   # was 1.5 -- stronger volume required
}

MOMENTUM = {
    "min_momentum": 4.0,   # 4%+ move required (was 5 -- too tight)
    "volume_surge": 2.5,   # 2.5x volume confirmation (was 3 -- too tight)
}

SENTIMENT_STRATEGY = {
    "enabled": True,
    "min_sentiment_score": 0.6,
    "min_sentiment_confidence": 0.55,
    "volume_surge": 2.0,
}

# -----------------------------------------------------------------
# Gap Breakout Strategy
# -----------------------------------------------------------------
GAP_BREAKOUT = {
    "min_gap_pct":       5.0,   # Minimum gap-up % from prior close
    "volume_multiplier": 2.5,   # Recent vol must be > X * session avg (raised from 1.5 -- x1.5 was noise-level)
    "entry_window_min":  90,    # Only enter within first 90 min of open
}

# -----------------------------------------------------------------
# Opening Range Breakout (ORB) Strategy
# -----------------------------------------------------------------
ORB = {
    "range_minutes":       15,   # ORB formed in first 15 min (9:30-9:45)
    "entry_start_min":     15,   # Start looking for breakouts after ORB forms
    "entry_end_min":       120,  # Stop entering after 2 hrs into session
    "breakout_buffer_pct": 0.1,  # Require 0.1% above ORB high to confirm
    "volume_surge":        2.0,  # Post-ORB vol must be > 2.0x ORB avg (raised from 1.5)
}

# -----------------------------------------------------------------
# VWAP Reclaim Strategy
# -----------------------------------------------------------------
VWAP_RECLAIM = {
    "volume_surge": 2.0,   # Volume in last 3 bars vs session avg
    "rsi_max":      72,    # Don't enter if already overbought
}

# -----------------------------------------------------------------
# VWAP Fade Strategy (mean reversion -- counter-play to VWAP Reclaim's
# continuation bet, for range/chop days where continuation entries just
# grind into stops)
#
# 2026-08-14, disabled at the user's request after a backtest of every
# VWAPFade trade since it went live (89 matched entry/exit pairs,
# 2026-08-03 -> today, joined against each signal's logged confidence):
# net-negative at EVERY confidence bucket tested (37% win rate, -0.85%
# avg P&L overall), and the 90%+ bucket -- its own ceiling -- was the
# WORST bucket (22% win rate), not the best. Confidence has no predictive
# value for this strategy's outcomes, so gating it higher (the user's
# original ask was ">=95%, and it's never logged above 90% anyway) can't
# fix it. See scripts/test_vwap_fade_disabled.py.
# -----------------------------------------------------------------
VWAP_FADE_ENABLED = False
VWAP_FADE = {
    "zscore_threshold": 1.5,   # price must be this many session std-devs from VWAP
    "min_stretch_pct":  1.5,   # ...and at least this far away in raw % terms
    "reversal_bars":    5,     # bars looked back for a "already turning" tick
}

# -----------------------------------------------------------------
# Liquidity Sweep Continuation Strategy ("stop hunt" reversal)
# A recent swing high/low gets briefly violated (the stops resting there get
# taken out), price closes back on the original side, then confirms with a
# Break of Structure back in the original trend direction. Distinct from
# ORB/TrendBreaker (which trade the breakout itself) -- this trades the
# fakeout-then-reversal.
# -----------------------------------------------------------------
LIQUIDITY_SWEEP = {
    "swing_lookback_bars": 30,    # bars used to establish the prior swing high/low ("liquidity pool")
    "swing_exclude_bars":  5,     # most-recent bars excluded from swing calc, then searched for the sweep + BOS
    "sweep_buffer_pct":    0.05,  # min % beyond the swing level to count as a genuine sweep, not noise
    "bos_buffer_pct":      0.05,  # min % beyond the confirming level to count as a genuine break of structure
    "volume_surge":        1.5,  # the BOS move needs above-average volume -- real participation, not drift
}

# -----------------------------------------------------------------
# Float Rotation Strategy
# -----------------------------------------------------------------
FLOAT_ROTATION = {
    "max_float_shares":   15_000_000,  # Only stocks with float < 15M shares
    "volume_float_ratio": 0.25,        # Today's volume already > 25% of float
    "min_price_up_pct":   5.0,         # Price must be up >5% on the day
}

# -----------------------------------------------------------------
# Early Momentum / Opening Strategies
# -----------------------------------------------------------------
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

# -----------------------------------------------------------------
# Bear Breakdown Strategy (short-selling)
# Fires only in bear regime (SPY < 200SMA). Inverse of TrendBreaker.
# -----------------------------------------------------------------
BEAR_BREAKDOWN = {
    "volume_multiplier":  1.5,   # Volume today vs 20-day avg (raised from 1.2 -- filters x1.3/x1.4 noise)
    "rsi_max":           65,    # Allow earlier distribution entries before full trend extension
    "rsi_min":           30,    # Raised from 20 -- avoid shorting deeply oversold stocks (bounce risk)
    "above_sma_min_days": 1,    # Loosen freshness requirement in fast bear tapes
    "breakdown_buffer_pct": 0.30,  # Allow entry if within 0.30% above 10-day low
}

# -----------------------------------------------------------------
# Golden Ratio Scanner Guardrails
# -----------------------------------------------------------------
RVOL_MIN                 = 1.5         # Require relative volume >= 1.5x before entering
MIN_STOCK_PRICE          = 2.0         # Skip penny stocks below $2 (poor fill quality, high spread)
ALPACA_MOVER_SCAN_INTERVAL_MIN = 10   # Re-poll Alpaca screener every 10 min (resets at market open)
MIN_DOLLAR_VOLUME        = 1_300_000   # 30% tighter 2026-08-31: skip price * day_vol < $1.3M
# Low-float / thin-volume floors for scan-time entry eligibility.
# 2026-08-23, user request: collapsed the old two-layer system (a separate
# absolute "hard floor" plus a session-gated regular/pre-after-hours floor,
# with a first-hour delay on top) into one flat rule applied the same
# regardless of time of day: float > 13M, avg daily volume >= 910K. See
# _passes_guardrails in scan.py.
MIN_FLOAT_SHARES_REGULAR_HOURS = 13_000_000  # 30% tighter 2026-08-31: single flat float floor, no session/time gating
MIN_AVG_DAILY_VOLUME_REGULAR_HOURS = 910_000  # 30% tighter 2026-08-31: single flat volume floor, no session/time gating

# 2026-08-18, user request: 200M (the BIOA-driven pre-open/after-hours floor)
# was inconsistent with the two OTHER floors already gating the same names --
# MIN_MARKET_CAP ($100M) and MIN_STOCK_PRICE ($2). At the boundary of both
# (mcap exactly $100M, price exactly $2), shares outstanding = $100M / $2 =
# 50M -- so 200M float demanded a name be ~4x bigger than the mcap floor
# alone would require at that price. Lowered to 50M, consistent with the
# floors already in place. Not read by the entry-side scan-time guardrails
# any more (see MIN_FLOAT_SHARES_REGULAR_HOURS above) -- close_guardrail_
# fail_positions' overnight-hold check is this constant's only remaining
# consumer, and that function itself is disabled (GUARDRAIL_EOD_CLOSE_ENABLED
# = False) as of the same 2026-08-23 request. Kept defined since the
# function body still references it.
MIN_FLOAT_SHARES         = 50_000_000  # close_guardrail_fail_positions' overnight-hold bar (function currently disabled)

# 2026-08-12, user request: the guardrails above are UNCHANGED and still
# reject these symbols exactly as before (counted in [GUARDRAIL SUMMARY] same
# as always) -- this is a separate, toggleable path that re-admits a rejected
# symbol, sized at THIN_LIQUIDITY_POSITION_SIZE_PCT instead of the normal
# POSITION_SIZE_PCT. Off by default -- flip TRADE_THIN_LIQUIDITY_REJECTS to
# switch it on. Gives exposure to below-the-floor names that are genuinely
# liquid (HTZ/RUM/NN-style) without betting full size on the ones that turn
# out thin (PLAG-style: passes on paper, 154 shares traded in a 30-min bar
# at the highs) -- a flat 3% caps the downside on the latter either way.
#
# 2026-08-13, user request ("no guard rails for ANY scanner during intra
# day... check before closing end of day if the tickers pass guardrail, keep
# them overnight" -- refined same day to "only avoid penny stocks, everything
# else allow during intraday trading"): widened from avg_volume/low_float
# only to every guardrail reason except min_price (RVOL, dollar_vol,
# gap_chase, market cap included -- penny stocks stay hard-blocked; see
# _ALL_GUARDRAIL_REASONS in scan.py). The overnight boundary is still fully
# enforced regardless of what got waived at entry: close_guardrail_fail_
# positions checks every open position against avg_volume/float/mcap at
# 15:45 ET and force-closes anything still failing, same as before this
# widening -- that's the "check before closing end of day" half of the ask,
# already built. This just stops the entry-side gate from being stricter
# than the exit-side one when the position is getting flattened by the
# close regardless of how it got in.
TRADE_THIN_LIQUIDITY_REJECTS     = True   # master switch for this path -- enabled 2026-08-12 at the user's request
THIN_LIQUIDITY_POSITION_SIZE_PCT = 4.0    # scaled 3.0 -> 4.0 2026-08-17 with the 7.5->10% base

# 2026-08-15, user request: idea #2 of six suggested improvements. Measured
# (not projected) from actual historical trades, split by guardrail status,
# for the strategies still active: thin-liquidity-bypass trades net -$13.85
# combined, while the SAME strategies' normal (guardrail-passing) trades
# net +$77.73 -- the bypass is a pure drag specifically for these two:
#   ORB:         normal 57% win/+$34.23 (n=65)  vs bypass 29% win/-$12.25 (n=17)
#   GapBreakout: normal 77% win/+$45.31 (n=13)  vs bypass 40% win/-$1.80  (n=5)
# Other still-active strategies (TrendBreaker, LiquiditySweep, PMHighBreakout,
# Sentiment, Technical, VWAPReclaim) either had too few bypass trades to
# judge or didn't show the same pattern, so left alone -- this is scoped to
# the two strategies where the effect was actually measured, not applied
# blanket. A signal from either strategy that would only qualify via
# thin-liquidity admit (guardrail rescue or stale-momentum trade-through)
# is now hard-skipped instead of traded at reduced size -- same as before
# TRADE_THIN_LIQUIDITY_REJECTS/TRADE_STALE_MOMENTUM_REJECTS existed, but
# only for these two strategies. See _scan_one() in scan.py and
# _resolve_freshness_reject() in enhanced.py.
THIN_LIQUIDITY_EXCLUDED_STRATEGIES = {"ORB", "GapBreakout"}

# 2026-08-18, user request: a 2nd+ same-day entry into a symbol already traded
# today submits a trailing-stop BUY/SELL (trails the adverse move, fires only
# once price reverses REENTRY_TRAIL_PCT% off the extreme) instead of chasing a
# marketable limit straight into a still-moving price. PFSA that day: 2nd
# EarlySqueeze entry chased in at $13.52 while fading 15% off its 30-min high,
# filled $12.50 (still falling), stopped out $11.72 eight minutes later -- a
# trailing buy would never have filled while it kept dropping. Costs giving up
# the first REENTRY_TRAIL_PCT% of any real reversal in exchange for not
# catching the falling knife. See _entries_today in enhanced.py.
# 2026-08-22, user request: "every entry trail orders percentage is 0.5%
# along with the ema slope instead of 1%" -- tightened 1.0 -> 0.5. On
# 2026-08-28, tightened again to 0.25% so an aligned reversal can trigger
# sooner, while still requiring the hard EMA entry gate. The entry trigger
# leans harder on
# _check_ema_trend_alignment (EMA9 slope must already be moving in the
# trade's direction) to keep this from just catching noise -- the two
# conditions apply together, not as alternatives: EMA slope gates entry
# eligibility at signal time, this trail % gates the actual fill price.
REENTRY_TRAIL_PCT = 0.25

# 2026-08-31: hard duplicate-entry debounce. Live RBLX submitted twice within
# ~9 seconds, which means broker/order-cache state can lag the fast poller.
# Block same-symbol entry submits briefly after Alpaca accepts one.
DUPLICATE_ENTRY_BLOCK_SECONDS = 60

# -----------------------------------------------------------------
# Staged allocation (25% x 4), never adding while losing.
# 2026-08-31, user request: instead of one full-size entry order, scale in
# over STAGED_ALLOCATION_TRANCHES equal tranches (default 4 x 25%). The first
# tranche is submitted at signal time; each subsequent tranche is added by the
# periodic poller (PENDING_ENTRY_RECHECK_SEC cadence) ONLY while the position
# is not losing (unrealized gain
# strictly above STAGED_ALLOCATION_MIN_GAIN_PCT) and the same fresh EMA entry
# gate still aligns -- "never adding while losing" is enforced by the gain
# requirement, and the fresh-EMA-check-immediately-before-each-tranche rule is
# enforced by re-running _check_ema_trend_alignment right before each add.
# -----------------------------------------------------------------
STAGED_ALLOCATION_ENABLED      = os.getenv("STAGED_ALLOCATION_ENABLED", "true").lower() in ("1", "true", "yes")
STAGED_ALLOCATION_TRANCHES     = int(os.getenv("STAGED_ALLOCATION_TRANCHES", "4"))
STAGED_ALLOCATION_MIN_GAIN_PCT = float(os.getenv("STAGED_ALLOCATION_MIN_GAIN_PCT", "0.0"))  # never add while losing: require gain > this
STAGED_ALLOCATION_MAX_ADD_PCT  = float(os.getenv("STAGED_ALLOCATION_MAX_ADD_PCT", "25.0"))  # each add is this % of the ORIGINAL full size

# 2026-08-14, user request: "I told ones which fail guard will be traded too
# but with lower portfolio limit" -- extending the same trade-anyway-at-
# reduced-size treatment to momentum-freshness rejects (_check_momentum_
# freshness), a DIFFERENT mechanism from the guardrails above. Confirmed
# live same day: a cycle found 5 strong candidates (96-97% confidence),
# but the top 3 by rank all got hard-skipped as "not fresh" (already faded
# 5-16% off their 30-min high) and the other 2 never even got tried
# (MAX_SIGNALS_PER_CYCLE slices to the top 3 before attempting anything) --
# zero fills from a cycle with 5 real candidates. Rather than a hard skip,
# a stale-momentum signal traded anyway at THIN_LIQUIDITY_POSITION_
# SIZE_PCT (reuses signal.thin_liquidity -- same flag, same sizing, same
# halved trailing stop -- a chased/faded entry deserves the same tighter
# leash as a liquidity-guardrail admit, not a separate mechanism).
#
# 2026-08-25, user request ("any other recommendations to reduce losses" ->
# "fix 4"): flipped back to a hard reject. Today's log still showed faded
# entries trading anyway at reduced size (JEM: "faded 8.9%/9.0%/11.1% off
# its 30-min high -- entry not fresh -- trading anyway") and losing --
# with _check_ema_trend_alignment (EMA7 delta + EMA7-vs-EMA15) now gating
# every entry too, a stale-momentum signal is doubly suspect, not a case
# that just needs a smaller position. Re-accepts the original 2026-08-14
# risk this flag exists to avoid -- a cycle's top-ranked candidates can
# all be stale and get hard-skipped before a fresher lower-ranked one is
# even tried -- watch for that pattern in the logs (a cycle with 0 fills
# despite several signals) if this needs revisiting.
TRADE_STALE_MOMENTUM_REJECTS = False

# 2026-08-12, user request: these names already failed a guardrail, so hold
# them on a shorter leash for their whole life too, not just a smaller size
# at entry -- every trailing-stop placement/re-place/tighten for a
# thin_liquidity=True symbol (entry bracket, protect_positions, ratchet
# tightening, after-hours virtual-stop, all re-arm fallbacks) uses HALF the
# normal dynamic-tier trail% instead of the tier's own value. See
# _trail_pct_for() in enhanced.py -- one shared helper, not 6 special cases.
# Scope tracks TRADE_THIN_LIQUIDITY_REJECTS above automatically (both keyed
# off signal.thin_liquidity) -- as that admit path widened 2026-08-13, this
# tighter-stop treatment widened with it, no separate change needed here.
THIN_LIQUIDITY_TRAILING_STOP_MULT = 0.5
MIN_MARKET_CAP           = 100_000_000 # Skip micro-caps below $100M

# 2026-08-12, user request: entries during regular hours were plain
# MarketOrderRequest -- no price bound at all, so a wide bid-ask spread (thin
# name, fast-moving book) gets absorbed in full at whatever the ask happens
# to be (see NBIL 2026-08-12: bought $28.76, the exact high tick of that
# 5-min bar). Applies to entries (_create_bracket_order, _create_simple_order)
# and every software-triggered exit (_submit_closing_order) alike -- all
# three now price off a LIVE bid/ask quote fetched at submit time (see
# _live_quote_mid in enhanced.py), not the scan-time signal.price/
# pos.current_price, which can be seconds to minutes stale by the time the
# order actually reaches the broker (scan cadence, MAX_SIGNALS_PER_CYCLE
# throttling) -- bounding against a stale reference defeats the point.
# User's spec: stay within 1% of that live bid/ask midpoint. Still fills like
# a market order in normal conditions; caps the worst case instead of
# absorbing an unbounded spread. If it doesn't fill same-day (DAY
# time-in-force), that itself is a signal the spread was genuinely too wide
# to trade safely -- no active re-chase added here, that's a separate ask.
MARKETABLE_LIMIT_BUFFER_PCT = 1.0

# Faded/stale-entry passive limit (2026-08-17, CDTG: bought $2.97 marketable
# into a signal already flagged "faded 9.8% off its 30-min high", price kept
# falling to $2.73 before any stop existed at all, eventual stop armed off
# the already-fallen price -> -11.7% realized vs. the intended 4% trail).
# ONLY applies to Signal.stale_entry (the freshness-reject path specifically,
# see strategies.py) -- never to a guardrail-floor thin_liquidity admit,
# which has no "fade" to wait out (confirmed live: would have delayed/risked
# missing CDTG trade 1 +$1.64 and both FIEE trades, none of which faded).
# Design (3 tiers, all in _create_bracket_order/_sweep_pending_entries):
#   1. Submit a PASSIVE limit at the opposite side of the spread from
#      today's marketable price (mid -1% for a long) instead of chasing in
#      -- let the confirmed fade come to the order instead of paying up.
#   2. If unfilled after FADED_ENTRY_PASSIVE_WINDOW_SECONDS, start chasing,
#      but capped at what today's code would have paid at decision time
#      (mid+1% at signal time) -- never worse than today's baseline. If
#      price reversed and is now above that cap, the order rests and waits
#      rather than chasing upward into the reversal.
#   3. If STILL unfilled after FADED_ENTRY_CEILING_TIMEOUT_SECONDS, drop the
#      cap and fall through to the normal uncapped escalating chase so the
#      trade doesn't get lost entirely (matches the 2026-08-14 "trade it
#      anyway, just smaller" rule) -- a rare last resort, not the default.
FADED_ENTRY_PASSIVE_WINDOW_SECONDS  = 90
FADED_ENTRY_CEILING_TIMEOUT_SECONDS = 300

MAX_GAP_CHASE_PCT        = 15.0       # Skip if already up >15% without consolidation
GAP_CHASE_CONSOL_BARS    = 5          # Number of 1-min bars to check for tight base
# ponytail: suppressed 2026-08-03 to observe a month of live impact (log data showed
# it never logged a single block, and the 21 float blocks / 0 mcap blocks this past
# month suggest it's costing more legit movers than it's protecting). Flip to True
# to reactivate if the no-guard month looks worse.
GAP_CHASE_GUARD_ENABLED  = False

# Momentum entry freshness -- reject a gap/momentum signal if price has already
# faded off its recent high by the time the order is about to be submitted.
# Different axis from GAP_CHASE (which gates on gap SIZE, still disabled
# above): the 2026-08-11 case studies (ACHR, PLUG, CLSK, SOUN) all faded
# before or shortly after fill despite gaps ranging 5.5%-27.9% -- magnitude
# didn't predict it, timing did. Scan cadence (3-5min) plus limit-order fill
# lag means the move can already be rolling over by the time execution
# happens, seconds to minutes after the signal fired.
# ponytail: MOMENTUM_FRESHNESS_MAX_PULLBACK_PCT and _LOOKBACK_MIN below are a
# first-pass estimate sized against those 4 case studies, not a backtest --
# PLUG's costlier 2nd entry (-$8.49, 6.1% off its 30-min high at fill) would
# have been caught at these settings; CLSK's slow multi-hour bleed (which
# never showed a sharp pullback right at entry) would not -- this targets a
# sharp reversal, not a gradual one. Revisit with real win/loss data once
# it's run for a while, same experiment shape as GAP_CHASE_GUARD_ENABLED.
MOMENTUM_FRESHNESS_ENABLED          = True
# 2026-08-12, user request ("delayed entries... every signal picked
# yesterday"): was just the 2 strategies above -- NBIL (ORB) faded before
# fill same as PLUG/ACHR/CLSK/SOUN did, and the check itself
# (_check_momentum_freshness) has no strategy-specific logic, it only ever
# looks at bars + the strategies set, so widening this set is the entire
# fix. Added every other LONG strategy whose own signal reasoning is
# "breaking out / continuing a move" (vulnerable to buying after it's
# already rolled over): ORB, Momentum, TrendBreaker, VWAPReclaim,
# FloatRotation, OpeningBellSurge, PMHighBreakout, EarlySqueeze.
# Deliberately NOT added: VWAPFade and LiquiditySweep enter LONG *because*
# price already pulled back off a recent high (that pullback is the signal,
# not staleness) -- this check would false-reject most of their real
# signals. PowerOf3 is the same shape (enters after its own "sweep" leg).
# Sentiment/Technical are composite/ambiguous, left alone rather than
# guessed at. BearBreakdown is short-only, moot either way (freshness only
# gates OrderType.LONG). Revisit if PowerOf3/VWAPFade/LiquiditySweep show
# the same late-entry pattern in the data.
MOMENTUM_FRESHNESS_STRATEGIES       = {
    "PreMarketMomentum", "GapBreakout", "ORB", "Momentum", "TrendBreaker",
    "VWAPReclaim", "FloatRotation", "OpeningBellSurge", "PMHighBreakout",
    "EarlySqueeze",
}
MOMENTUM_FRESHNESS_LOOKBACK_MIN     = 30   # minutes of recent bars to find the high against
MOMENTUM_FRESHNESS_MAX_PULLBACK_PCT = 5.0  # reject if price has faded more than this % off that high

USE_MARKET_REGIME_FILTER = True       # SPY below 200-day MA -> cut signals to 1
MAX_LONG_ENTRIES_PER_CYCLE  = 12      # Maximum successful long entries attempted per scan cycle
MAX_SHORT_ENTRIES_PER_CYCLE = 12      # Maximum successful short entries attempted per scan cycle
MARKET_REGIME_SIGNALS_CAP   = MAX_LONG_ENTRIES_PER_CYCLE  # Bear-regime long cap (swap-only)
BEAR_SHORT_SIGNALS_CAP      = MAX_SHORT_ENTRIES_PER_CYCLE
LOW_PRIORITY_SCAN_SYMBOLS   = {
    "QLD", "META", "AAPL", "AMZN", "NFLX", "GOOGL", "NVDA", "AMD",
    "AVGO", "PLTR", "ORCL", "MSFT", "ARM", "SMCI", "MU", "TSM", "MRVL", "AI", "SPCX",
}
ATR_STOP_MULTIPLIER      = 1.5        # Stop loss = entry - ATR -- 1.5
ATR_TP_RATIO             = 2.0        # Take-profit at 2:1 R:R (risk -- 2)
MAX_SHORT_FLOAT_PCT      = 20.0       # Never exceed this % of equity per squeeze ticker

# Bear short scan supplement -- liquid large/mid caps with clean SMA structure that
# BearBreakdownStrategy and TechnicalStrategy can fire on during a bear regime.
# These stocks have stable 20/50 SMA patterns and meaningful distribution moves.
BEAR_SHORT_UNIVERSE = [
    "NVDA", "AMD", "TSLA", "META", "AMZN", "AAPL", "MSFT", "NFLX",
    "PLTR", "MSTR", "COIN", "SMCI", "SNOW", "CRM", "CRWD", "NET",
    "ARKK", "SOXS", "LABD",   # sector ETFs (can be shorted directly)
    "MARA", "WULF", "CLSK",   # crypto miners -- high-beta bear breakdowns
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

# Live HSF lookup -- merges the static set above with tier-2 universe.json entries
# so newly TI-scraped tickers are recognised as HSF without restarting the bot.
_hsf_tier2_cache: dict = {"ts": 0.0, "symbols": frozenset()}
_HSF_CACHE_TTL = 300  # 5 minutes -- re-read universe.json at most every 5 min

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

# Global memory warning threshold (in MB)
MEMORY_WARN_MB = int(os.getenv("MEMORY_WARN_MB", "1500"))

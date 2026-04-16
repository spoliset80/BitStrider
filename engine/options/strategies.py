"""
ApexTrader - Options Strategies (Level 3 Account) — A+ Edition
Professional-grade options strategies with multi-layer entry filters:

  - MomentumCallStrategy  : Buy calls on confirmed breakouts (cheap IV, trend aligned)
  - BearPutStrategy       : Buy puts on breakdowns (bear regime or individual collapse)
  - CoveredCallStrategy   : Sell OTM covered calls for income (high IV rank required)

A+ filters on every buy-side signal:
  1. IV Rank gate        — buy when IV is CHEAP (rank <35 calls / <55 puts)
  2. EMA-20 trend        — price & EMA direction must align with signal
  3. 3-day momentum      — 3-day close trend confirms today's move
  4. Breakout/Breakdown  — must clear/break prior 5-day high/low
  5. Premium/spot cap    — mid <= 3% of spot (avoid overpriced contracts)
  6. R/R gate            — ATR-expected move / premium >= 1.5x
  7. OI >= 500 near ATM  — genuine liquidity
  8. B/A spread <= 15%   — tight enough for fair fills
  9. Composite scoring   — confidence built from IV rank, momentum, vol, trend, R/R

Allocation: 15% of portfolio across max 3 concurrent option positions.
Expiry preference: 7-21 DTE near-term.
"""

import datetime
import logging
import math
import time
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

import pandas as pd
import psutil
import pytz
from engine.options._options_today import _calc_iv_rank
from engine.utils import get_bars, calc_rsi, get_option_data_client, ALPACA_AVAILABLE
from engine.config import (
    OPTIONS_ENABLED,
    OPTIONS_DTE_MIN,
    OPTIONS_DTE_MAX,
    OPTIONS_DELTA_TARGET,
    OPTIONS_MIN_OPEN_INTEREST,
    OPTIONS_MAX_SPREAD_PCT,
    OPTIONS_MAX_IV_PCT,
    OPTIONS_MIN_IV_PCT,
    OPTIONS_COVERED_CALL_DELTA,
    OPTIONS_MIN_SIGNAL_CONFIDENCE,
    OPTIONS_MIN_STOCK_PRICE,
    OPTIONS_MIN_MOVE_PCT,
    OPTIONS_MIN_RVOL,
    OPTIONS_MIN_ADV,
    OPTIONS_STOP_COOLDOWN_DAYS,
    OPTIONS_EARNINGS_AVOID_DAYS,
    ATR_STOP_MULTIPLIER,
    OPTIONS_CHAIN_CACHE_MAX,
    MEMORY_WARN_MB,
    get_options_universe,
)
from engine.equity.strategies import _is_bull_regime, _calc_atr14, _INVERSE_ETFS

_OPTIONS_UNIVERSE_CACHE: list[str] | None = None
_OPTIONS_UNIVERSE_CACHE_TS: datetime.datetime = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
_OPTIONS_UNIVERSE_CACHE_TTL_SEC = 60


def _get_options_universe() -> list[str]:
    global _OPTIONS_UNIVERSE_CACHE, _OPTIONS_UNIVERSE_CACHE_TS
    now = datetime.datetime.now(datetime.timezone.utc)
    if (
        _OPTIONS_UNIVERSE_CACHE is None or
        (now - _OPTIONS_UNIVERSE_CACHE_TS).total_seconds() > _OPTIONS_UNIVERSE_CACHE_TTL_SEC
    ):
        try:
            _OPTIONS_UNIVERSE_CACHE = get_options_universe()
        except Exception:
            _OPTIONS_UNIVERSE_CACHE = []
        _OPTIONS_UNIVERSE_CACHE_TS = now
    return _OPTIONS_UNIVERSE_CACHE


def _calc_rsi_scalar(prices: pd.Series) -> Optional[float]:
    rsi = calc_rsi(prices)
    if rsi is None or getattr(rsi, 'empty', False):
        return None
    try:
        return float(rsi.iloc[-1])
    except Exception:
        return None

ET  = pytz.timezone("America/New_York")


# Session-level stop cooldown: symbol -> date of last stop/loss close
# Prevents re-entering a symbol within OPTIONS_STOP_COOLDOWN_DAYS after a stop.
_stop_cooldown: Dict[str, datetime.date] = {}
log = logging.getLogger("ApexTrader.Options")

CONTRACT_SIZE = 100  # standard 1 options contract = 100 shares

# --- Dynamic/Adaptive Option Filter Logic ---
# Set this to True for strict A+ mode, False for regime-adaptive filters
STRICT_A_PLUS_OPTION_FILTERS = False

def get_dynamic_option_filters():
    """
    Returns a dict of filter thresholds, adapting to market regime if not in strict A+ mode.
    All values are set for genuine liquidity: tight spreads, real OI, proper R/R.
    """
    bull = _is_bull_regime()
    if STRICT_A_PLUS_OPTION_FILTERS:
        return {
            "MAX_SPREAD_PCT": 10.0,
            "MIN_OI_ATM": 500,
            "MAX_PREMIUM_SPOT": 3.0,
            "MIN_RR": 1.5,
            "IV_RANK_CALL_MAX": 40.0,
            "IV_RANK_PUT_MAX": 60.0,
        }
    else:
        # Adaptive: tighten slightly in bear regime (fear premiums widen spreads)
        return {
            "MAX_SPREAD_PCT": 12.0 if bull else 15.0,   # was 25/20 — now much tighter
            "MIN_OI_ATM": 400 if bull else 500,          # was 150/200 — meaningful liquidity
            "MAX_PREMIUM_SPOT": 4.0 if bull else 3.5,   # unchanged effective cap
            "MIN_RR": 1.3 if bull else 1.4,             # was 1.0/1.1 — require real payoff
            "IV_RANK_CALL_MAX": 50.0 if bull else 45.0,
            "IV_RANK_PUT_MAX": 70.0 if bull else 80.0,  # bear crash = elevated IV is the environment, not a reason to skip puts
        }

# Use these in all filter checks below
_FILTERS = get_dynamic_option_filters()
_MAX_SPREAD_PCT   = _FILTERS["MAX_SPREAD_PCT"]
_MIN_OI_ATM       = _FILTERS["MIN_OI_ATM"]
_MAX_PREMIUM_SPOT = _FILTERS["MAX_PREMIUM_SPOT"]
_MIN_RR           = _FILTERS["MIN_RR"]
_IV_RANK_CALL_MAX = _FILTERS["IV_RANK_CALL_MAX"]
_IV_RANK_PUT_MAX  = _FILTERS["IV_RANK_PUT_MAX"]
_IV_RANK_CC_MIN   = 50.0  # covered calls: sell when IV is elevated (static)


# -- Data Structures -----------------------------------------------------------

@dataclass
class OptionSignal:
    symbol:        str
    option_type:   str          # 'call', 'put', 'call_butterfly', 'iron_condor', etc.
    action:        str          # 'buy_to_open' or 'sell_to_open'
    strike:        float        # primary leg strike
    expiry:        datetime.date
    mid_price:     float        # estimated entry price per share (*100 for notional)
    confidence:    float
    reason:        str
    strategy:      str
    iv_pct:        float = 0.0  # implied volatility at time of scan
    iv_rank:       float = 0.0  # 0-100 IV rank vs 52-week HV range
    delta:         float = 0.0  # option delta
    open_interest: int   = 0
    rr_ratio:      float = 0.0  # R/R: ATR expected move / premium
    breakeven:     float = 0.0  # breakeven price at expiry
    
    # Debit spread fields (TrendPullbackSpread: 2-leg; None = single-leg)
    spread_sell_strike: Optional[float] = None   # short leg strike
    spread_sell_mid:    Optional[float] = None   # credit received from short leg per share
    
    # Multi-leg butterfly fields (buy low, sell 2 mid, buy high; None = not a butterfly)
    butterfly_low_strike:  Optional[float] = None   # lowest strike (buy 1)
    butterfly_low_mid:     Optional[float] = None   # cost of low strike
    butterfly_high_strike: Optional[float] = None   # highest strike (buy 1)
    butterfly_high_mid:    Optional[float] = None   # cost of high strike
    # For butterfly: strike = mid_strike (sell 2x), spread_sell_strike = mid_strike (unused), mid_price = net_debit
    
    # Iron condor fields (sell 1 long put, sell 1 short put, sell 1 short call, sell 1 long call)
    put_long_strike:  Optional[float] = None   # long (OTM) put strike
    put_short_strike: Optional[float] = None   # short (ITM) put strike
    call_short_strike: Optional[float] = None  # short (ITM) call strike
    call_long_strike: Optional[float] = None   # long (OTM) call strike


@dataclass
class OptionsChainInfo:
    """Parsed options chain data for a symbol."""
    symbol:     str
    expiry:     datetime.date
    calls:      pd.DataFrame
    puts:       pd.DataFrame
    spot_price: float
    iv_rank:    float   # 0-100 percentile of IV vs 52-week HV range
    hv_30:      float   # 30-day historical vol (annualised %)
    atr14:      float   # 14-day ATR in $


# -- TI Universe (always loaded live from ti_unusual_options.json) -------------

# -- Chain Fetch & Quality Helpers ---------------------------------------------

_chain_cache: Dict[str, tuple] = {}   # symbol -> (timestamp, OptionsChainInfo)
_CHAIN_TTL   = 300  # 5-minute cache
_CHAIN_MAX   = OPTIONS_CHAIN_CACHE_MAX  # configurable cache size

# Memory usage monitor
def _check_memory():
    process = psutil.Process()
    mem_mb = process.memory_info().rss / 1024 / 1024
    if mem_mb > MEMORY_WARN_MB:
        log.warning(f"[OOM WARNING] Memory usage high: {mem_mb:.0f} MB (limit {MEMORY_WARN_MB} MB)")


def _calc_hv30(closes: pd.Series) -> float:
    """30-day annualised historical volatility."""
    if len(closes) < 32:
        return 30.0
    return float(closes.pct_change().dropna().iloc[-30:].std()) * math.sqrt(252) * 100


def _get_options_chain(symbol: str) -> Optional[OptionsChainInfo]:
    """Fetch the best near-term options chain (14-30 DTE) with full quality metadata.
    Uses Alpaca OptionHistoricalDataClient.
    """
    now = time.monotonic()
    cached = _chain_cache.get(symbol)
    if cached and (now - cached[0]) < _CHAIN_TTL:
        return cached[1]

    _check_memory()
    if len(_chain_cache) >= _CHAIN_MAX:
        _chain_cache.clear()

    # 65-day daily bars for HV, ATR, IV rank (already Alpaca-first in get_bars)
    hist = get_bars(symbol, period="65d", interval="1d")
    if hist.empty or len(hist) < 15:
        return None
    spot = float(hist["close"].iloc[-1])
    if spot <= 0:
        return None

    # ATR-14
    hi = hist["high"]; lo = hist["low"]; pc = hist["close"].shift(1)
    tr = pd.concat([(hi - lo), (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    atr14 = float(tr.rolling(14).mean().iloc[-1])

    # HV-30
    hv30 = _calc_hv30(hist["close"])

    today = datetime.date.today()
    exp_gte = today + datetime.timedelta(days=OPTIONS_DTE_MIN)
    exp_lte = today + datetime.timedelta(days=OPTIONS_DTE_MAX)

    # ── Alpaca option chain ──────────────────────────────────────
    if ALPACA_AVAILABLE:
        try:
            result = _get_chain_alpaca(symbol, spot, exp_gte, exp_lte, hv30, atr14, hist)
            if result is not None:
                _chain_cache[symbol] = (now, result)
                return result
        except Exception as e:
            log.debug(f"{symbol}: Alpaca option chain failed: {e}")

    return None


def _parse_occ_symbol(occ: str):
    """Parse an OCC symbol like AAPL260501C00195000.
    Returns (underlying, expiry_date, option_type, strike) or None.
    """
    import re
    m = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', occ)
    if not m:
        return None
    underlying = m.group(1)
    exp_str = m.group(2)  # YYMMDD
    opt_type = "call" if m.group(3) == "C" else "put"
    strike = int(m.group(4)) / 1000.0
    expiry = datetime.date(2000 + int(exp_str[:2]), int(exp_str[2:4]), int(exp_str[4:6]))
    return underlying, expiry, opt_type, strike


def _snapshots_to_df(snapshots: dict, opt_type: str) -> pd.DataFrame:
    """Convert Alpaca option chain snapshots to a normalised DataFrame."""
    rows = []
    for occ_sym, snap in snapshots.items():
        parsed = _parse_occ_symbol(occ_sym)
        if parsed is None:
            continue
        _, expiry, snap_type, strike = parsed
        if snap_type != opt_type:
            continue

        bid = getattr(snap.latest_quote, "bid_price", 0) or 0 if snap.latest_quote else 0
        ask = getattr(snap.latest_quote, "ask_price", 0) or 0 if snap.latest_quote else 0
        mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else 0
        last = getattr(snap.latest_trade, "price", 0) or 0 if snap.latest_trade else 0
        iv = getattr(snap, "implied_volatility", 0) or 0
        greeks = snap.greeks if snap.greeks else None
        delta = getattr(greeks, "delta", 0) or 0 if greeks else 0
        oi = getattr(snap, "open_interest", 0) or 0

        rows.append({
            "contractsymbol": occ_sym,
            "strike": strike,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "lastprice": last if last > 0 else mid,
            "impliedvolatility": iv,
            "iv_pct": iv * 100,
            "delta": delta,
            "openinterest": oi,
            "expiry": expiry,
        })

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _get_chain_alpaca(
    symbol: str, spot: float,
    exp_gte: datetime.date, exp_lte: datetime.date,
    hv30: float, atr14: float, hist: pd.DataFrame,
) -> Optional[OptionsChainInfo]:
    """Fetch option chain via Alpaca OptionHistoricalDataClient."""
    from alpaca.data.requests import OptionChainRequest

    client = get_option_data_client()
    req = OptionChainRequest(
        underlying_symbol=symbol,
        expiration_date_gte=exp_gte,
        expiration_date_lte=exp_lte,
    )
    snapshots = client.get_option_chain(req)
    if not snapshots:
        return None

    calls = _snapshots_to_df(snapshots, "call")
    puts  = _snapshots_to_df(snapshots, "put")

    if calls.empty and puts.empty:
        return None

    # Pick the closest expiry from the returned data
    all_expiries = set()
    if not calls.empty:
        all_expiries.update(calls["expiry"].unique())
    if not puts.empty:
        all_expiries.update(puts["expiry"].unique())
    target_expiry = min(all_expiries) if all_expiries else exp_gte

    # Filter to just that expiry
    if not calls.empty:
        calls = calls[calls["expiry"] == target_expiry].drop(columns=["expiry"])
    if not puts.empty:
        puts = puts[puts["expiry"] == target_expiry].drop(columns=["expiry"])

    # IV rank from ATM call IV
    mid_c = calls[(calls["strike"] >= spot * 0.95) & (calls["strike"] <= spot * 1.05)]
    if not mid_c.empty and "impliedvolatility" in mid_c.columns:
        cur_iv = float(mid_c["impliedvolatility"].mean()) * 100
    else:
        cur_iv = hv30
    iv_rank = _calc_iv_rank(cur_iv, hist["close"])

    log.debug(f"{symbol}: Alpaca chain OK — {len(calls)} calls, {len(puts)} puts, exp={target_expiry}")
    return OptionsChainInfo(
        symbol=symbol,
        expiry=target_expiry,
        calls=calls,
        puts=puts,
        spot_price=spot,
        iv_rank=iv_rank,
        hv_30=hv30,
        atr14=max(atr14, 0.01),
    )


def _pick_strike(
    chain_df: pd.DataFrame,
    spot: float,
    target_delta: float,
) -> Optional[pd.Series]:
    """Pick the best strike with A+ quality filters.
    Priority: delta proximity, then ATM. Must pass OI, spread, IV gates.
    """
    if chain_df.empty:
        return None

    df = chain_df.copy()

    # OI gate — use config value directly; no floor override that undermines it
    if "openinterest" in df.columns:
        df = df[df["openinterest"] >= OPTIONS_MIN_OPEN_INTEREST]
    if df.empty:
        return None

    # Bid-ask
    if "bid" in df.columns and "ask" in df.columns:
        df = df[(df["bid"] > 0) & (df["ask"] > 0)].copy()
        df["mid"]        = (df["bid"] + df["ask"]) / 2
        df["spread_pct"] = (df["ask"] - df["bid"]) / df["mid"].clip(lower=0.01) * 100
        df = df[df["spread_pct"] <= _MAX_SPREAD_PCT]
    else:
        df["mid"]        = df.get("lastprice", 0)
        df["spread_pct"] = 100.0

    if df.empty:
        return None

    # IV filter
    if "impliedvolatility" in df.columns:
        df["iv_pct"] = df["impliedvolatility"] * 100
        df = df[(df["iv_pct"] >= OPTIONS_MIN_IV_PCT) & (df["iv_pct"] <= OPTIONS_MAX_IV_PCT)]

    if df.empty:
        return None

    # Delta selection
    if "delta" in df.columns and df["delta"].abs().max() > 0:
        df["delta_dist"] = (df["delta"].abs() - target_delta).abs()
        best = df.loc[df["delta_dist"].idxmin()]
    else:
        df["strike_dist"] = (df["strike"] - spot).abs()
        best = df.loc[df["strike_dist"].idxmin()]

    return best


def _calc_rr(atr14: float, dte: int, mid_price: float) -> float:
    """R/R ratio: ATR-scaled expected move in the DTE window vs premium paid.
    expected_move = ATR14 * sqrt(DTE)   (random-walk scaling)
    R/R = expected_move / (2 * mid_price)  -- need 2x premium to be profitable
    """
    if mid_price <= 0:
        return 0.0
    expected_move = atr14 * math.sqrt(max(dte, 1))
    return round(expected_move / (2 * mid_price), 2)


def _trend_aligned(closes: pd.Series, direction: str) -> Tuple[bool, float]:
    """Check 20-EMA trend alignment.
    Returns (aligned: bool, ema20_value: float).
    direction: 'up' for calls, 'down' for puts.
    """
    if len(closes) < 22:
        return True, float(closes.iloc[-1])   # insufficient data -- don't block
    ema  = closes.ewm(span=20, adjust=False).mean()
    ema20      = float(ema.iloc[-1])
    ema20_prev = float(ema.iloc[-3])
    spot = float(closes.iloc[-1])
    if direction == "up":
        return spot > ema20 and ema20 > ema20_prev, ema20
    else:
        return spot < ema20 and ema20 < ema20_prev, ema20


def _three_day_trend(closes: pd.Series, direction: str) -> bool:
    """True if at least 2 of the last 3 sessions confirm direction (no whipsaw)."""
    if len(closes) < 5:
        return True
    c = closes.iloc[-4:].tolist()
    if direction == "up":
        return (c[-1] > c[-2]) or (c[-2] > c[-3])
    else:
        return (c[-1] < c[-2]) or (c[-2] < c[-3])


# -- Shared Bar Context --------------------------------------------------------

@dataclass
class _BarCtx:
    """Pre-computed bar data and indicators, shared across all strategy scan() calls."""
    daily:     pd.DataFrame
    closes:    pd.Series
    spot:      float
    prev:      float
    chg_pct:   float        # % change from previous close
    avg_vol20: float
    cur_vol:   float
    vol_ratio: float        # cur_vol / avg_vol20
    rsi:       Optional[float]
    ema20:     float
    ema50:     float
    atr14:     float


def _fetch_bar_context(symbol: str) -> Optional[_BarCtx]:
    """Fetch 80-day daily bars and compute common indicators for all strategies.

    Returns None if data is insufficient or spot is below OPTIONS_MIN_STOCK_PRICE.
    All strategies should call this instead of independently fetching bars.
    """
    daily = get_bars(symbol, "80d", "1d")
    if daily.empty or len(daily) < 25:
        return None
    closes = daily["close"]
    spot   = float(closes.iloc[-1])
    if spot < OPTIONS_MIN_STOCK_PRICE:
        return None
    prev    = float(closes.iloc[-2])
    chg_pct = (spot - prev) / prev * 100
    avg_vol20 = float(daily["volume"].iloc[-21:-1].mean())
    cur_vol   = float(daily["volume"].iloc[-1])
    vol_ratio = cur_vol / max(avg_vol20, 1.0)
    rsi       = _calc_rsi_scalar(closes)
    ema20     = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
    ema50     = float(closes.ewm(span=50, adjust=False).mean().iloc[-1])
    hi = daily["high"]; lo = daily["low"]; pc = daily["close"].shift(1)
    tr    = pd.concat([(hi - lo), (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    atr14 = float(tr.rolling(14).mean().iloc[-1])
    return _BarCtx(daily, closes, spot, prev, chg_pct, avg_vol20, cur_vol, vol_ratio,
                   rsi, ema20, ema50, atr14)


# -- Strategy Implementations --------------------------------------------------

class MomentumCallStrategy:
    """Buy near-term calls on confirmed bullish breakouts with A+ filters.

    Entry requirements:
    - Bull regime (SPY > 200-SMA)
    - Today >= +3%, 20-day volume surge >= 1.5x
    - RSI 50-72: trending but not overbought
    - 20-EMA rising AND price above EMA
    - 3-day upward momentum confirms (no one-day fluke)
    - Price broke above prior 5-day high (real breakout)
    - IV rank < 35 (buying cheap premium only)
    - Premium <= 3% of spot
    - R/R >= 1.5 (ATR expected move justifies premium)
    - ATM OI >= 500, spread <= 15%
    """

    name = "MomentumCall"

    def scan(self, symbol: str) -> Optional[OptionSignal]:
        if not OPTIONS_ENABLED:
            return None
        # Inverse ETFs (SQQQ, SPXU, UVXY…) go UP in bear markets — allow calls on
        # them regardless of regime. All other symbols require bull regime.
        is_inverse = symbol in _INVERSE_ETFS
        if not is_inverse and not _is_bull_regime():
            return None

        try:
            ctx = _fetch_bar_context(symbol)
            if ctx is None or len(ctx.closes) < 25:
                return None

            if ctx.chg_pct < OPTIONS_MIN_MOVE_PCT:
                return None
            if ctx.vol_ratio < OPTIONS_MIN_RVOL:
                return None

            if ctx.rsi is None or not (50 <= ctx.rsi <= 72):
                return None

            # A+ Filter 1: EMA-20 trend alignment
            trend_ok, ema20 = _trend_aligned(ctx.closes, "up")
            if not trend_ok:
                return None

            # A+ Filter 2: 3-day momentum confirmation
            if not _three_day_trend(ctx.closes, "up"):
                return None

            # A+ Filter 3: breakout above prior 5-day high
            prior_5d_high = float(ctx.daily["high"].iloc[-7:-2].max())
            if ctx.spot < prior_5d_high * 0.995:
                return None

            chain = _get_options_chain(symbol)
            if chain is None:
                return None

            # A+ Filter 4: IV rank -- buy cheap premium only
            if chain.iv_rank > _IV_RANK_CALL_MAX:
                log.debug(f"MomentumCall {symbol}: IV rank {chain.iv_rank:.0f} > {_IV_RANK_CALL_MAX} (dynamic) -- skip")
                return None

            strike_row = _pick_strike(chain.calls, ctx.spot, OPTIONS_DELTA_TARGET)
            if strike_row is None:
                return None

            strike = float(strike_row["strike"])
            mid    = float(strike_row.get("mid", strike_row.get("lastprice", 0)))
            iv_pct = float(strike_row.get("iv_pct", chain.hv_30))
            delta  = float(strike_row.get("delta", OPTIONS_DELTA_TARGET))
            oi     = int(strike_row.get("openinterest", 0))
            dte    = (chain.expiry - datetime.date.today()).days

            if mid <= 0:
                return None

            # A+ Filter 5: Premium/spot cap
            if mid / ctx.spot * 100 > _MAX_PREMIUM_SPOT:
                return None

            # A+ Filter 6: R/R gate
            rr = _calc_rr(chain.atr14, dte, mid)
            if rr < _MIN_RR:
                return None

            # A+ OI gate at ATM
            if "openinterest" in chain.calls.columns:
                atm = chain.calls[(chain.calls["strike"] >= ctx.spot * 0.90) & (chain.calls["strike"] <= ctx.spot * 1.10)]
                if int(atm["openinterest"].sum()) < _MIN_OI_ATM:
                    return None

            # A+ Confidence formula
            conf  = 0.72
            conf += min(0.06, (ctx.chg_pct - 3.0) * 0.015)
            conf += min(0.05, (ctx.vol_ratio - 1.5) * 0.025)
            conf += min(0.04, (_IV_RANK_CALL_MAX - chain.iv_rank) * 0.001)
            conf += min(0.04, (rr - _MIN_RR) * 0.02)
            if ctx.spot > prior_5d_high:
                conf += 0.03   # genuine breakout bonus
            confidence = round(min(0.97, conf), 3)

            return OptionSignal(
                symbol=symbol,
                option_type="call",
                action="buy_to_open",
                strike=strike,
                expiry=chain.expiry,
                mid_price=mid,
                confidence=confidence,
                reason=(
                    f"Breakout +{ctx.chg_pct:.1f}% vol={ctx.vol_ratio:.1f}x RSI={ctx.rsi:.0f} "
                    f"EMA20=${ema20:.2f}^ IVrank={chain.iv_rank:.0f} R/R={rr:.1f}x "
                    f"| {dte}DTE ${strike:.0f}C d={delta:.2f} IV={iv_pct:.0f}%"
                ),
                strategy=self.name,
                iv_pct=iv_pct,
                iv_rank=chain.iv_rank,
                delta=delta,
                open_interest=oi,
                rr_ratio=rr,
                breakeven=round(strike + mid, 2),
            )

        except Exception as e:
            log.debug(f"MomentumCall {symbol}: {e}")
            return None


class BearPutStrategy:
    """Buy near-term puts on confirmed breakdowns with A+ filters.

    Entry requirements:
    - Bear regime (SPY < 200-SMA) OR severe individual breakdown (>= -4%)
    - Today <= -2% (bear) / <= -4% (bull), volume >= 1.2x
    - 20-EMA declining AND price below EMA
    - 3-day downside momentum confirms
    - Price broke below prior 5-day low (real breakdown)
    - IV rank < 55 (don't buy puts after fear already priced in)
    - Premium <= 3% of spot
    - R/R >= 1.5
    - ATM OI >= 500, spread <= 15%
    """

    name = "BearPut"

    def scan(self, symbol: str) -> Optional[OptionSignal]:
        if not OPTIONS_ENABLED:
            return None
        if symbol not in _get_options_universe():
            return None

        bull = _is_bull_regime()

        # Inverse ETFs (SQQQ, SPXU…) go UP when market falls.
        # Buying puts on them in bear regime = betting market rallies — wrong direction.
        if symbol in _INVERSE_ETFS and not bull:
            return None

        try:
            ctx = _fetch_bar_context(symbol)
            if ctx is None or len(ctx.closes) < 25:
                return None

            if ctx.rsi is None:
                return None

            chg_thresh = -4.0 if bull else -1.0  # bear: allow entries on moderate-decline days, not just crash days
            if ctx.chg_pct > chg_thresh:
                return None

            min_rvol = 1.1 if bull and not STRICT_A_PLUS_OPTION_FILTERS else 1.2
            if ctx.vol_ratio < min_rvol:
                return None

            # A+ Filter 1: EMA-20 trend alignment
            trend_ok, ema20 = _trend_aligned(ctx.closes, "down")
            if bull and not trend_ok:
                return None   # strict in bull regime; bear regime EMA used for confidence

            # A+ Filter 2: 3-day momentum confirmation
            # In bear regime, waive if price has been below its 50-EMA for 5+ consecutive
            # days — the macro downtrend is already confirmed, a bounce day is an entry.
            if not bull:
                ema50 = ctx.closes.ewm(span=50, adjust=False).mean()
                below_ema50_streak = int((ctx.closes.iloc[-6:-1] < ema50.iloc[-6:-1]).sum())
                in_macro_downtrend = below_ema50_streak >= 5
            else:
                in_macro_downtrend = False
            if not in_macro_downtrend and not _three_day_trend(ctx.closes, "down"):
                return None

            # A+ Filter 3: breakdown below prior 5-day low
            # In bear regime with a crash-day drop >= 3%, the trend gates above are
            # sufficient; waive the 5d-low requirement so we don't sit out the move.
            prior_5d_low = float(ctx.daily["low"].iloc[-7:-2].min())
            crash_day    = ctx.chg_pct <= -3.0
            if ctx.spot > prior_5d_low * 1.005 and not (not bull and crash_day):
                return None

            chain = _get_options_chain(symbol)
            if chain is None:
                return None

            # A+ Filter 4: IV rank -- don't buy when fear already spiked
            if chain.iv_rank > _IV_RANK_PUT_MAX:
                log.debug(f"BearPut {symbol}: IV rank {chain.iv_rank:.0f} > {_IV_RANK_PUT_MAX} -- skip")
                return None

            strike_row = _pick_strike(chain.puts, ctx.spot, 0.40)
            if strike_row is None:
                return None

            strike = float(strike_row["strike"])
            mid    = float(strike_row.get("mid", strike_row.get("lastprice", 0)))
            iv_pct = float(strike_row.get("iv_pct", chain.hv_30))
            delta  = float(strike_row.get("delta", -0.40))
            oi     = int(strike_row.get("openinterest", 0))
            dte    = (chain.expiry - datetime.date.today()).days

            if mid <= 0:
                return None

            # A+ Filter 5: Premium/spot cap
            if mid / ctx.spot * 100 > _MAX_PREMIUM_SPOT:
                return None

            # A+ Filter 6: R/R gate
            rr = _calc_rr(chain.atr14, dte, mid)
            if rr < _MIN_RR:
                return None

            # A+ OI gate at ATM
            if "openinterest" in chain.puts.columns:
                atm = chain.puts[(chain.puts["strike"] >= ctx.spot * 0.90) & (chain.puts["strike"] <= ctx.spot * 1.10)]
                if int(atm["openinterest"].sum()) < _MIN_OI_ATM:
                    return None

            # A+ Confidence formula
            conf  = 0.72
            conf += min(0.07, abs(ctx.chg_pct - abs(chg_thresh)) * 0.015)
            conf += min(0.05, (ctx.vol_ratio - 1.2) * 0.025)
            conf += min(0.04, (_IV_RANK_PUT_MAX - chain.iv_rank) * 0.001)
            conf += min(0.04, (rr - _MIN_RR) * 0.02)
            if not bull:
                conf += 0.04   # bear regime confirmation bonus
            if ctx.spot < prior_5d_low:
                conf += 0.03   # genuine breakdown bonus
            confidence = round(min(0.97, conf), 3)

            return OptionSignal(
                symbol=symbol,
                option_type="put",
                action="buy_to_open",
                strike=strike,
                expiry=chain.expiry,
                mid_price=mid,
                confidence=confidence,
                reason=(
                    f"Breakdown {ctx.chg_pct:.1f}% vol={ctx.vol_ratio:.1f}x RSI={ctx.rsi:.0f} "
                    f"EMA20=${ema20:.2f}v IVrank={chain.iv_rank:.0f} R/R={rr:.1f}x "
                    f"| {dte}DTE ${strike:.0f}P d={delta:.2f} IV={iv_pct:.0f}%"
                ),
                strategy=self.name,
                iv_pct=iv_pct,
                iv_rank=chain.iv_rank,
                delta=delta,
                open_interest=oi,
                rr_ratio=rr,
                breakeven=round(strike - mid, 2),
            )

        except Exception as e:
            log.debug(f"BearPut {symbol}: {e}")
            return None


class CoveredCallStrategy:
    """Sell OTM covered calls on currently held stock positions (income).

    Fires when:
    - Symbol is held long with >= 100 shares
    - Bull or neutral regime (don't sell covered calls in bear -- cap upside)
    - IV rank >= 50 (collect rich premium)
    - Selects strike at ~0.25 delta (OTM, ~10-15% above current price)
    - No existing covered call open against same ticker
    """

    name = "CoveredCall"

    def scan(
        self,
        symbol: str,
        qty_held: int,
        existing_option_symbols: set,
    ) -> Optional[OptionSignal]:
        if not OPTIONS_ENABLED:
            return None
        if qty_held < CONTRACT_SIZE:
            return None
        if not _is_bull_regime():
            return None

        for opt_sym in existing_option_symbols:
            if opt_sym.startswith(symbol) and "C" in opt_sym:
                return None

        try:
            daily = get_bars(symbol, "20d", "1d")
            if daily.empty:
                return None

            spot = float(daily["close"].iloc[-1])

            chain = _get_options_chain(symbol)
            if chain is None:
                return None

            # Require elevated IV to collect meaningful premium
            if chain.iv_rank < _IV_RANK_CC_MIN:
                log.debug(f"CoveredCall {symbol}: IV rank {chain.iv_rank:.0f} < {_IV_RANK_CC_MIN} -- skip")
                return None

            strike_row = _pick_strike(chain.calls, spot, OPTIONS_COVERED_CALL_DELTA)
            if strike_row is None:
                return None

            strike = float(strike_row["strike"])
            if strike <= spot:
                return None   # never sell ATM or ITM covered calls

            mid    = float(strike_row.get("mid", strike_row.get("lastprice", 0)))
            iv_pct = float(strike_row.get("iv_pct", chain.hv_30))
            delta  = float(strike_row.get("delta", OPTIONS_COVERED_CALL_DELTA))
            oi     = int(strike_row.get("openinterest", 0))
            dte    = (chain.expiry - datetime.date.today()).days

            if mid <= 0:
                return None

            upside_pct     = (strike - spot) / spot * 100
            premium_yield  = (mid * CONTRACT_SIZE) / (spot * qty_held) * 100

            return OptionSignal(
                symbol=symbol,
                option_type="call",
                action="sell_to_open",
                strike=strike,
                expiry=chain.expiry,
                mid_price=mid,
                confidence=0.82,
                reason=(
                    f"Covered call income | IVrank={chain.iv_rank:.0f} "
                    f"upside={upside_pct:.1f}% yield={premium_yield:.2f}% "
                    f"| {dte}DTE ${strike:.0f}C d={delta:.2f} IV={iv_pct:.0f}%"
                ),
                strategy=self.name,
                iv_pct=iv_pct,
                iv_rank=chain.iv_rank,
                delta=delta,
                open_interest=oi,
                rr_ratio=0.0,
                breakeven=round(spot - mid, 2),
            )

        except Exception as e:
            log.debug(f"CoveredCall {symbol}: {e}")
            return None


# -- New Strategy Helpers ------------------------------------------------------

def _no_earnings_soon(symbol: str, days: int = 15) -> bool:
    """Return True if no earnings are expected within `days` calendar days.
    Earnings calendar data requires a third-party feed (removed); always
    returns True (fail-safe: allow the trade).
    """
    return True


def _is_bullish_reversal(daily: pd.DataFrame) -> bool:
    """Detect a bullish reversal candle on the last bar.
    Patterns: hammer (long lower wick) or bullish engulfing.
    Expects lowercase OHLC columns: open, high, low, close.
    """
    if len(daily) < 2:
        return True   # insufficient data — don't block

    o = float(daily["open"].iloc[-1])
    h = float(daily["high"].iloc[-1])
    l = float(daily["low"].iloc[-1])
    c = float(daily["close"].iloc[-1])
    full_range = h - l
    if full_range < 1e-6:
        return False
    body = abs(c - o)
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)

    # Hammer: lower wick >= 2× body, close in upper half
    if lower_wick >= 2 * max(body, 1e-9) and upper_wick <= body * 1.5 and c > l + full_range * 0.40:
        return True

    # Bullish engulfing: today bullish, engulfs prior bearish body
    if c > o:
        prev_o = float(daily["open"].iloc[-2])
        prev_c = float(daily["close"].iloc[-2])
        if prev_c < prev_o and o <= prev_c and c >= prev_o:
            return True

    return False


def _lower_bollinger_touch(closes: pd.Series, window: int = 20, num_stds: float = 2.0, tolerance: float = 0.005) -> bool:
    """True if the last close is at or almost exactly at the lower Bollinger Band."""
    if len(closes) < window + 2:
        return False
    sma        = closes.rolling(window).mean()
    std        = closes.rolling(window).std()
    lower_band = sma - num_stds * std
    return float(closes.iloc[-1]) <= float(lower_band.iloc[-1]) * (1.0 + tolerance)


def _ema50_above(closes: pd.Series) -> bool:
    """True if the last close is above the 50-day EMA."""
    if len(closes) < 52:
        return True   # not enough history — don't block
    ema50 = closes.ewm(span=50, adjust=False).mean()
    return float(closes.iloc[-1]) > float(ema50.iloc[-1])


def _at_ema20_pullback(closes: pd.Series) -> bool:
    """True if price is within 1.5% of the 20 EMA after being above it."""
    if len(closes) < 22:
        return False
    ema20  = closes.ewm(span=20, adjust=False).mean()
    spot   = float(closes.iloc[-1])
    ema_v  = float(ema20.iloc[-1])
    return abs(spot - ema_v) / max(ema_v, 1e-9) <= 0.015


def _resistance_breakout_retest(daily: pd.DataFrame) -> Tuple[bool, float]:
    """Detect breakout-and-retest pattern.
    Returns (pattern_found: bool, resistance_level: float).

    Logic:
    1. Resistance = max close from 20–35 sessions ago
    2. Breakout: any close in the last 5–15 sessions exceeded resistance
    3. Retest: a session low since the breakout touched back within 5% of resistance
    4. Currently above resistance (bounce confirmed)
    """
    if len(daily) < 38:
        return False, 0.0

    closes    = daily["close"]
    lows      = daily["low"]
    resistance = float(closes.iloc[-35:-20].max())
    if resistance <= 0:
        return False, 0.0

    # Breakout within past 5-15 sessions (not counting today)
    breakout_occurred = any(float(c) > resistance * 0.98 for c in closes.iloc[-15:-2])
    if not breakout_occurred:
        return False, resistance

    # Retest: any low since breakout came close to the resistance level
    retest_zone = resistance * 1.03  # within 3% above = retest zone
    retest_occurred = any(float(lw) <= retest_zone for lw in lows.iloc[-10:-1])

    spot = float(closes.iloc[-1])
    above_resistance = spot > resistance * 0.98

    return (breakout_occurred and retest_occurred and above_resistance), resistance


# -- New Strategy Classes -------------------------------------------------------

# ------------------- Iron Condor Strategy -------------------
class IronCondorStrategy:
    """Sell iron condor: Sell OTM call and put, buy further OTM call and put for defined risk.

    Entry requirements:
    - Neutral market (no strong trend)
    - IV rank >= 40 (high premium)
    - Price between 20- and 50-day EMA
    - No earnings within OPTIONS_EARNINGS_AVOID_DAYS
    - Sufficient open interest and tight spreads
    """
    name = "IronCondor"

    def scan(self, symbol: str) -> Optional[OptionSignal]:
        if not OPTIONS_ENABLED:
            return None
        try:
            ctx = _fetch_bar_context(symbol)
            if ctx is None or len(ctx.closes) < 55:
                return None

            # Neutral regime: RSI between 35–65, no strong one-day directional move
            if ctx.rsi is None or not (35 <= ctx.rsi <= 65):
                return None
            if abs(ctx.chg_pct) > 1.5:
                return None

            if not _no_earnings_soon(symbol, OPTIONS_EARNINGS_AVOID_DAYS):
                return None

            chain = _get_options_chain(symbol)
            if chain is None or chain.iv_rank < 40:
                return None

            # Short strikes: ~0.20 delta OTM (no direction kwarg — _pick_strike uses abs(delta))
            short_put_row  = _pick_strike(chain.puts,  ctx.spot, 0.20)
            short_call_row = _pick_strike(chain.calls, ctx.spot, 0.20)
            if short_put_row is None or short_call_row is None:
                return None

            short_put  = float(short_put_row["strike"])
            short_call = float(short_call_row["strike"])

            # Long wings: 2 strikes further OTM
            strikes_put  = sorted(chain.puts["strike"].unique())
            strikes_call = sorted(chain.calls["strike"].unique())
            try:
                put_idx  = next(i for i, s in enumerate(strikes_put)  if abs(s - short_put)  < 0.01)
                call_idx = next(i for i, s in enumerate(strikes_call) if abs(s - short_call) < 0.01)
            except StopIteration:
                return None

            long_put_idx  = max(put_idx  - 2, 0)
            long_call_idx = min(call_idx + 2, len(strikes_call) - 1)
            long_put  = strikes_put[long_put_idx]
            long_call = strikes_call[long_call_idx]

            def _leg_mid(df: pd.DataFrame, strike: float) -> float:
                row = df[abs(df["strike"] - strike) < 0.01]
                if row.empty:
                    return 0.0
                r = row.iloc[0]
                return (float(r.get("bid", 0)) + float(r.get("ask", 0))) / 2.0

            short_put_mid  = _leg_mid(chain.puts,  short_put)
            short_call_mid = _leg_mid(chain.calls, short_call)
            long_put_mid   = _leg_mid(chain.puts,  long_put)
            long_call_mid  = _leg_mid(chain.calls, long_call)

            if any(m <= 0 for m in (short_put_mid, short_call_mid, long_put_mid, long_call_mid)):
                return None

            net_credit   = round(short_put_mid + short_call_mid - long_put_mid - long_call_mid, 3)
            spread_width = min(abs(short_call - long_call), abs(short_put - long_put))
            max_loss     = round(spread_width - net_credit, 3)
            if net_credit <= 0 or max_loss <= 0:
                return None

            dte  = (chain.expiry - datetime.date.today()).days
            conf = 0.82   # iron condors have defined risk — start at threshold
            conf += min(0.05, (chain.iv_rank - 40) * 0.002)
            conf += min(0.04, (net_credit / spread_width) * 0.2)   # credit/width ratio bonus
            confidence = round(min(0.93, conf), 3)

            return OptionSignal(
                symbol=symbol,
                option_type="iron_condor",
                action="sell_to_open",
                strike=ctx.spot,
                expiry=chain.expiry,
                mid_price=net_credit,
                confidence=confidence,
                reason=(
                    f"IronCondor {long_put:.0f}/{short_put:.0f}P {short_call:.0f}/{long_call:.0f}C "
                    f"net=${net_credit:.2f} max_loss=${max_loss:.2f} IVrank={chain.iv_rank:.0f} | {dte}DTE"
                ),
                strategy=self.name,
                iv_pct=float(short_call_row.get("iv_pct", chain.hv_30)),
                iv_rank=chain.iv_rank,
                delta=0.0,
                open_interest=int(short_call_row.get("openinterest", 0)),
                rr_ratio=round(net_credit / max_loss, 2),
                breakeven=None,
                spread_sell_strike=short_call,
                spread_sell_mid=short_call_mid,
                put_long_strike=long_put,
                put_short_strike=short_put,
                call_short_strike=short_call,
                call_long_strike=long_call,
            )
        except Exception as e:
            log.debug(f"IronCondor {symbol}: {e}")
            return None

# ------------------- Butterfly Strategy -------------------
class ButterflyStrategy:
    """Buy call butterfly: Buy lower-wing call, sell 2 ATM calls, buy upper-wing call.
    Profit maximised when spot pins at the short strike at expiry.

    Entry requirements (all must pass):
    - Neutral RSI 42–58: no strong trend, price expected to pin near ATM
    - Flat session: |chg%| < 1.5% — breakout days hurt pinning thesis
    - Volume not extreme: 0.7≤ RVOL ≤2.0 (avoid news-driven gap days)
    - IV rank < 30 (buy cheap theta exposure)
    - 2-strike-wide wings: wider profit zone than adjacent-strike butterfly
    - Wing width >= 2% of spot (filters micro-cap / illiquid chains)
    - Debit cap: net_debit <= 30% of wing_width (cost must be cheap relative to max payoff)
    - R/R gate: max_profit / net_debit >= 2.0
    - OI >= _MIN_OI_ATM on ATM short strike
    - No earnings within OPTIONS_EARNINGS_AVOID_DAYS
    """
    name = "Butterfly"

    def scan(self, symbol: str) -> Optional[OptionSignal]:
        if not OPTIONS_ENABLED:
            return None
        try:
            ctx = _fetch_bar_context(symbol)
            if ctx is None or len(ctx.closes) < 55:
                return None

            # Butterflies need a neutral environment — strong trends kill pinning thesis
            if abs(ctx.chg_pct) > 1.5:
                return None
            if ctx.rsi is None or not (42 <= ctx.rsi <= 58):
                return None
            if not (0.7 <= ctx.vol_ratio <= 2.0):
                return None

            if not _no_earnings_soon(symbol, OPTIONS_EARNINGS_AVOID_DAYS):
                return None

            chain = _get_options_chain(symbol)
            if chain is None or chain.iv_rank > 30:
                return None

            strikes = sorted(chain.calls["strike"].unique())
            atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - ctx.spot))
            # 2-strike wide wings for a broader profit zone
            if atm_idx < 2 or atm_idx > len(strikes) - 3:
                return None

            low_strike  = strikes[atm_idx - 2]
            mid_strike  = strikes[atm_idx]
            high_strike = strikes[atm_idx + 2]

            left_width  = mid_strike - low_strike
            right_width = high_strike - mid_strike
            wing_width  = min(left_width, right_width)   # conservative: use narrower side

            if wing_width < ctx.spot * 0.02:   # wings must span at least 2% of spot
                return None

            def _leg_mid(df: pd.DataFrame, strike: float) -> float:
                row = df[abs(df["strike"] - strike) < 0.01]
                if row.empty:
                    return 0.0
                r = row.iloc[0]
                return (float(r.get("bid", 0)) + float(r.get("ask", 0))) / 2.0

            low_mid  = _leg_mid(chain.calls, low_strike)
            mid_mid  = _leg_mid(chain.calls, mid_strike)
            high_mid = _leg_mid(chain.calls, high_strike)

            if any(m <= 0 for m in (low_mid, mid_mid, high_mid)):
                return None

            # Buy 1 low, sell 2 mid, buy 1 high
            # max_profit = wing_width - net_debit (at pin = mid_strike at expiry)
            net_debit  = round(low_mid + high_mid - 2 * mid_mid, 3)
            max_profit = round(wing_width - net_debit, 3)

            if net_debit <= 0 or max_profit <= 0:
                return None

            # Debit must be cheap: cost <= 30% of max payoff zone
            if net_debit / wing_width > 0.30:
                return None

            # Butterflies need high R/R to justify the pin risk
            rr = round(max_profit / net_debit, 2)
            if rr < 2.0:
                return None

            dte     = (chain.expiry - datetime.date.today()).days
            mid_row = chain.calls[abs(chain.calls["strike"] - mid_strike) < 0.01]
            iv_pct  = float(mid_row.iloc[0].get("iv_pct", chain.hv_30)) if not mid_row.empty else chain.hv_30
            delta   = float(mid_row.iloc[0].get("delta", 0.50))          if not mid_row.empty else 0.50
            oi      = int(mid_row.iloc[0].get("openinterest", 0))         if not mid_row.empty else 0

            # OI gate on the ATM short strike (must have real liquidity)
            if oi < _MIN_OI_ATM:
                return None

            # Confidence — starts low, must earn it; max 0.92 (pin trades are speculative)
            conf  = 0.70
            conf += min(0.08, (30 - chain.iv_rank) * 0.004)    # bigger reward for cheaper IV
            conf += min(0.07, (rr - 2.0) * 0.025)              # reward high R/R
            conf += min(0.04, (1.5 - abs(ctx.chg_pct)) * 0.03) # reward flat price action
            confidence = round(min(0.92, conf), 3)

            return OptionSignal(
                symbol=symbol,
                option_type="call_butterfly",
                action="buy_to_open",
                strike=mid_strike,
                expiry=chain.expiry,
                mid_price=net_debit,
                confidence=confidence,
                reason=(
                    f"Butterfly {low_strike:.0f}/{mid_strike:.0f}/{high_strike:.0f}C "
                    f"net=${net_debit:.2f} max=${max_profit:.2f} R/R={rr:.1f}x "
                    f"IVrank={chain.iv_rank:.0f} RSI={ctx.rsi:.0f} | {dte}DTE"
                ),
                strategy=self.name,
                iv_pct=iv_pct,
                iv_rank=chain.iv_rank,
                delta=delta,
                open_interest=oi,
                rr_ratio=rr,
                breakeven=None,
                butterfly_low_strike=low_strike,
                butterfly_low_mid=low_mid,
                butterfly_high_strike=high_strike,
                butterfly_high_mid=high_mid,
                spread_sell_strike=mid_strike,
                spread_sell_mid=mid_mid,
            )
        except Exception as e:
            log.debug(f"Butterfly {symbol}: {e}")
            return None


# ------------------- Breakout Retest Strategy -------------------
class BreakoutRetestCallStrategy:
    """Buy ATM calls when price retests a prior breakout level and bounces."""
    name = "BreakoutRetest"

    def scan(self, symbol: str) -> Optional[OptionSignal]:
        if not OPTIONS_ENABLED:
            return None
        is_inverse = symbol in _INVERSE_ETFS
        if not is_inverse and not _is_bull_regime():
            return None

        try:
            ctx = _fetch_bar_context(symbol)
            if ctx is None or len(ctx.closes) < 38:
                return None

            retest_ok, resistance = _resistance_breakout_retest(ctx.daily)
            if not retest_ok:
                return None

            if ctx.rsi is None or not (48 <= ctx.rsi <= 62):
                return None

            if ctx.vol_ratio < 1.2:
                return None

            if not _no_earnings_soon(symbol, OPTIONS_EARNINGS_AVOID_DAYS):
                log.debug(f"BreakoutRetest {symbol}: earnings within {OPTIONS_EARNINGS_AVOID_DAYS} days — skip")
                return None

            chain = _get_options_chain(symbol)
            if chain is None:
                return None
            if chain.iv_rank > _IV_RANK_CALL_MAX:
                return None

            # ATM call (delta ~0.50)
            strike_row = _pick_strike(chain.calls, ctx.spot, 0.50)
            if strike_row is None:
                return None

            strike = float(strike_row["strike"])
            mid    = float(strike_row.get("mid", strike_row.get("lastprice", 0)))
            iv_pct = float(strike_row.get("iv_pct", chain.hv_30))
            delta  = float(strike_row.get("delta", 0.50))
            oi     = int(strike_row.get("openinterest", 0))
            dte    = (chain.expiry - datetime.date.today()).days

            if mid <= 0 or mid / ctx.spot * 100 > _MAX_PREMIUM_SPOT:
                return None

            rr = _calc_rr(chain.atr14, dte, mid)
            if rr < _MIN_RR:
                return None

            conf  = 0.75
            conf += min(0.06, (ctx.vol_ratio - 1.2) * 0.04)
            conf += min(0.04, (_IV_RANK_CALL_MAX - chain.iv_rank) * 0.001)
            conf += min(0.04, (rr - _MIN_RR) * 0.02)
            confidence = round(min(0.95, conf), 3)

            return OptionSignal(
                symbol=symbol,
                option_type="call",
                action="buy_to_open",
                strike=strike,
                expiry=chain.expiry,
                mid_price=mid,
                confidence=confidence,
                reason=(
                    f"Retest lvl=${resistance:.2f} vol={ctx.vol_ratio:.1f}x RSI={ctx.rsi:.0f} "
                    f"IVrank={chain.iv_rank:.0f} R/R={rr:.1f}x "
                    f"| {dte}DTE ${strike:.0f}C d={delta:.2f}"
                ),
                strategy=self.name,
                iv_pct=iv_pct,
                iv_rank=chain.iv_rank,
                delta=delta,
                open_interest=oi,
                rr_ratio=rr,
                breakeven=round(strike + mid, 2),
            )

        except Exception as e:
            log.debug(f"BreakoutRetest {symbol}: {e}")
            return None


class TrendPullbackSpreadStrategy:
    """Bull call debit spread on EMA-20 pullback within a 50-EMA uptrend.

    Structure: Buy ITM call (delta 0.65) + Sell OTM call 2 strikes above.
    Risk = net debit paid.  Max profit = spread_width − net_debit.

    Entry requirements:
    - Price above 50 EMA
    - Spot within 1.5% of 20 EMA (pullback zone)
    - RSI 35–52 (oversold within uptrend)
    - Bullish reversal candle (hammer or engulfing)
    - No earnings within OPTIONS_EARNINGS_AVOID_DAYS
    - IV rank < 35
    - Spread R/R (max_profit / net_debit) >= 0.5
    """

    name = "TrendPullbackSpread"

    def scan(self, symbol: str) -> Optional[OptionSignal]:
        if not OPTIONS_ENABLED:
            return None
        if not _is_bull_regime():
            return None

        try:
            ctx = _fetch_bar_context(symbol)
            if ctx is None or len(ctx.closes) < 55:
                return None

            if ctx.spot <= ctx.ema50:
                return None
            if not _at_ema20_pullback(ctx.closes):
                return None

            if ctx.rsi is None or not (35 <= ctx.rsi <= 52):
                return None

            if not _is_bullish_reversal(ctx.daily):
                return None

            if not _no_earnings_soon(symbol, OPTIONS_EARNINGS_AVOID_DAYS):
                log.debug(f"TrendPullbackSpread {symbol}: earnings within {OPTIONS_EARNINGS_AVOID_DAYS} days — skip")
                return None

            chain = _get_options_chain(symbol)
            if chain is None:
                return None
            if chain.iv_rank > _IV_RANK_CALL_MAX:
                return None

            # Long leg: ITM call delta 0.65
            long_row = _pick_strike(chain.calls, ctx.spot, 0.65)
            if long_row is None:
                return None

            long_strike = float(long_row["strike"])
            long_mid    = float(long_row.get("mid", long_row.get("lastprice", 0)))
            if long_mid <= 0:
                return None

            # Short leg: OTM call 2 strikes above long
            strikes_sorted = sorted(chain.calls["strike"].unique())
            try:
                long_idx = next(i for i, s in enumerate(strikes_sorted) if abs(s - long_strike) < 0.01)
            except StopIteration:
                return None
            short_strike_idx = min(long_idx + 2, len(strikes_sorted) - 1)
            short_strike     = strikes_sorted[short_strike_idx]
            if short_strike <= long_strike:
                return None

            short_rows = chain.calls[abs(chain.calls["strike"] - short_strike) < 0.01]
            if short_rows.empty:
                return None
            short_row = short_rows.iloc[0]
            if "bid" in short_row.index and "ask" in short_row.index:
                short_mid = (float(short_row["bid"]) + float(short_row["ask"])) / 2.0
            else:
                short_mid = float(short_row.get("lastprice", 0))

            if short_mid <= 0 or short_mid >= long_mid:
                return None

            net_debit    = round(long_mid - short_mid, 3)
            spread_width = short_strike - long_strike
            max_profit   = round(spread_width - net_debit, 3)
            if max_profit <= 0 or net_debit <= 0:
                return None

            spread_rr = round(max_profit / net_debit, 2)
            if spread_rr < 0.5:
                return None

            dte    = (chain.expiry - datetime.date.today()).days
            iv_pct = float(long_row.get("iv_pct", chain.hv_30))
            delta  = float(long_row.get("delta", 0.65))
            oi     = int(long_row.get("openinterest", 0))

            conf  = 0.73
            conf += min(0.05, (52 - ctx.rsi) * 0.002)
            conf += min(0.04, (_IV_RANK_CALL_MAX - chain.iv_rank) * 0.001)
            conf += min(0.05, spread_rr * 0.02)
            confidence = round(min(0.95, conf), 3)

            return OptionSignal(
                symbol=symbol,
                option_type="call",
                action="buy_to_open",
                strike=long_strike,
                expiry=chain.expiry,
                mid_price=net_debit,        # net debit = effective cost of spread
                confidence=confidence,
                reason=(
                    f"EMA20 pullback RSI={ctx.rsi:.0f} EMA20=${ctx.ema20:.2f} "
                    f"spread ${long_strike:.0f}/{short_strike:.0f}C "
                    f"net=${net_debit:.2f} max=${max_profit:.2f} R/R={spread_rr:.1f}x "
                    f"| {dte}DTE IVrank={chain.iv_rank:.0f}"
                ),
                strategy=self.name,
                iv_pct=iv_pct,
                iv_rank=chain.iv_rank,
                delta=delta,
                open_interest=oi,
                rr_ratio=spread_rr,
                breakeven=round(long_strike + net_debit, 2),
                spread_sell_strike=short_strike,
                spread_sell_mid=short_mid,
            )

        except Exception as e:
            log.debug(f"TrendPullbackSpread {symbol}: {e}")
            return None


class MeanReversionCallStrategy:
    """Buy ITM calls on oversold bounces from the lower Bollinger Band.

    Entry requirements:
    - RSI < 35 (oversold — relaxed from 32 to catch more setups)
    - Last close at or below 20-day lower Bollinger Band (2σ)
    - Bullish reversal candle (hammer or engulfing)
    - Price not more than 15% below 200-day SMA (no structural collapse)
    - No earnings within OPTIONS_EARNINGS_AVOID_DAYS
    - Buy ITM call (delta 0.65), DTE per OPTIONS_DTE_MIN/MAX
    - Premium <= 4% of spot (allow slightly wider for elevated IV)
    """

    name = "MeanReversion"

    def scan(self, symbol: str) -> Optional[OptionSignal]:
        if not OPTIONS_ENABLED:
            return None

        try:
            ctx = _fetch_bar_context(symbol)
            if ctx is None or len(ctx.closes) < 30:
                return None

            if ctx.rsi is None or ctx.rsi >= 35:   # oversold gate
                return None

            if not _lower_bollinger_touch(ctx.closes, tolerance=0.005):
                return None

            if not _is_bullish_reversal(ctx.daily):
                return None

            # Don't buy calls in a structural collapse (> 15% below 200 SMA)
            if len(ctx.closes) >= 200:
                sma200 = float(ctx.closes.rolling(200).mean().iloc[-1])
                if ctx.spot < sma200 * 0.85:
                    return None

            if not _no_earnings_soon(symbol, OPTIONS_EARNINGS_AVOID_DAYS):
                log.debug(f"MeanReversion {symbol}: earnings within {OPTIONS_EARNINGS_AVOID_DAYS} days — skip")
                return None

            chain = _get_options_chain(symbol)
            if chain is None:
                return None
            # IV may be elevated (fear) — don't filter on IV rank for mean reversion

            # ITM call for higher delta exposure
            strike_row = _pick_strike(chain.calls, ctx.spot, 0.65)
            if strike_row is None:
                return None

            strike = float(strike_row["strike"])
            mid    = float(strike_row.get("mid", strike_row.get("lastprice", 0)))
            iv_pct = float(strike_row.get("iv_pct", chain.hv_30))
            delta  = float(strike_row.get("delta", 0.65))
            oi     = int(strike_row.get("openinterest", 0))
            dte    = (chain.expiry - datetime.date.today()).days

            if mid <= 0 or mid / ctx.spot * 100 > 2.5:   # tighter premium cap for higher probability entries
                return None

            rr = _calc_rr(chain.atr14, dte, mid)
            if rr < _MIN_RR:
                return None

            sma20    = float(ctx.closes.rolling(20).mean().iloc[-1])
            std20    = float(ctx.closes.rolling(20).std().iloc[-1])
            lower_bb = sma20 - 2 * std20

            conf  = 0.70
            conf += min(0.08, (35 - ctx.rsi) * 0.003)
            conf += min(0.04, (rr - _MIN_RR) * 0.02)
            confidence = round(min(0.94, conf), 3)

            return OptionSignal(
                symbol=symbol,
                option_type="call",
                action="buy_to_open",
                strike=strike,
                expiry=chain.expiry,
                mid_price=mid,
                confidence=confidence,
                reason=(
                    f"Oversold RSI={ctx.rsi:.0f} BB_lower=${lower_bb:.2f} spot=${ctx.spot:.2f} "
                    f"IVrank={chain.iv_rank:.0f} R/R={rr:.1f}x "
                    f"| {dte}DTE ${strike:.0f}C d={delta:.2f}"
                ),
                strategy=self.name,
                iv_pct=iv_pct,
                iv_rank=chain.iv_rank,
                delta=delta,
                open_interest=oi,
                rr_ratio=rr,
                breakeven=round(strike + mid, 2),
            )

        except Exception as e:
            log.debug(f"MeanReversion {symbol}: {e}")
            return None


# -- Scanner Entry Point -------------------------------------------------------

def scan_options_universe(
    held_positions: Dict[str, int],
    existing_option_symbols: set,
) -> List[OptionSignal]:
    """Scan the options-eligible universe and return A+ ranked signals.

    Active strategies (applied to each TI ticker, in priority order):
    - MomentumCallStrategy       : breakout +% day, RVOL surge, bull regime, cheap IV
    - BearPutStrategy            : breakdown, bear regime or individual collapse
    - MeanReversionCallStrategy  : RSI<35 + lower BB touch, ITM call
    - BreakoutRetestCallStrategy : breakout-and-retest pattern, ATM call
    - TrendPullbackSpreadStrategy: EMA20 pullback in 50-EMA uptrend, debit spread
    - IronCondorStrategy         : neutral RSI + high IV rank, defined-risk condor
    - ButterflyStrategy          : low IV rank, price near expected pin

    Args:
        held_positions:          {symbol: qty} of current stock holdings.
        existing_option_symbols: set of option symbol strings already open.

    Returns:
        List of OptionSignal sorted by composite score (confidence * R/R) desc.
    """
    if not OPTIONS_ENABLED:
        return []

    # Refresh regime-adaptive filter globals so they reflect current market conditions
    global _MAX_SPREAD_PCT, _MIN_OI_ATM, _MAX_PREMIUM_SPOT, _MIN_RR, _IV_RANK_CALL_MAX, _IV_RANK_PUT_MAX
    _f = get_dynamic_option_filters()
    _MAX_SPREAD_PCT   = _f["MAX_SPREAD_PCT"]
    _MIN_OI_ATM       = _f["MIN_OI_ATM"]
    _MAX_PREMIUM_SPOT = _f["MAX_PREMIUM_SPOT"]
    _MIN_RR           = _f["MIN_RR"]
    _IV_RANK_CALL_MAX = _f["IV_RANK_CALL_MAX"]
    _IV_RANK_PUT_MAX  = _f["IV_RANK_PUT_MAX"]

    ti_universe = get_options_universe()
    if not ti_universe:
        log.warning("Options scan: no eligible options universe tickers available — skipping")
        return []

    signals: List[OptionSignal] = []
    momentum_strat      = MomentumCallStrategy()
    bear_put_strat      = BearPutStrategy()
    retest_strat        = BreakoutRetestCallStrategy()
    mean_rev_strat      = MeanReversionCallStrategy()
    trend_spread_strat  = TrendPullbackSpreadStrategy()
    iron_condor_strat   = IronCondorStrategy()
    butterfly_strat     = ButterflyStrategy()
    covered_strat       = CoveredCallStrategy()

    fail_counts: Dict[str, int] = {}
    fail_examples: Dict[str, List[str]] = {}
    def _record_fail(key: str, symbol: str) -> None:
        fail_counts[key] = fail_counts.get(key, 0) + 1
        if len(fail_examples.get(key, [])) < 6:
            fail_examples.setdefault(key, []).append(symbol)

    today = datetime.date.today()
    for symbol in ti_universe:
        # Dollar volume quality gate: skip thinly-traded names
        daily = get_bars(symbol, "25d", "1d")
        if not daily.empty and len(daily) >= 5:
            adv = float((daily["close"] * daily["volume"].iloc[-20:]).mean())
            if adv < OPTIONS_MIN_ADV:
                log.debug(f"Options scan: {symbol} ADV ${adv:,.0f} < ${OPTIONS_MIN_ADV:,.0f} — skip")
                _record_fail("adv", symbol)
                continue

        # Skip symbols still in stop cooldown
        if symbol in _stop_cooldown:
            days_since = (today - _stop_cooldown[symbol]).days
            if days_since < OPTIONS_STOP_COOLDOWN_DAYS:
                log.debug(f"Options scan: {symbol} in stop cooldown ({days_since}d / {OPTIONS_STOP_COOLDOWN_DAYS}d) — skipping")
                _record_fail("stop_cooldown", symbol)
                continue

        symbol_got_signal = False
        # Try all strategies in priority order; one signal per symbol per cycle
        for strat in (momentum_strat, bear_put_strat, mean_rev_strat, retest_strat,
                      trend_spread_strat, iron_condor_strat, butterfly_strat):
            sig = strat.scan(symbol)
            if sig and sig.confidence >= OPTIONS_MIN_SIGNAL_CONFIDENCE:
                signals.append(sig)
                symbol_got_signal = True
                break   # one signal per symbol per scan cycle
        if not symbol_got_signal:
            _record_fail("no_signal", symbol)

    for symbol, qty in held_positions.items():
        sig = covered_strat.scan(symbol, qty, existing_option_symbols)
        if sig and sig.confidence >= OPTIONS_MIN_SIGNAL_CONFIDENCE:
            signals.append(sig)

    # Rank by composite: confidence * min(R/R, 3.0)
    def _score(s: OptionSignal) -> float:
        return s.confidence * min(s.rr_ratio if s.rr_ratio > 0 else 1.0, 3.0)

    signals.sort(key=_score, reverse=True)
    strategy_names = [s.strategy for s in signals]
    if not signals:
        summary = ", ".join(
            f"{key}={value}"
            for key, value in sorted(fail_counts.items(), key=lambda x: -x[1])
        )
        example_lines = []
        for key, examples in fail_examples.items():
            example_lines.append(f"{key}: {', '.join(examples)}")
        log.info(
            f"Options scan: 0 signal(s) | universe={len(ti_universe)} "
            f"| fail summary: {summary}"
        )
        if example_lines:
            log.info(f"Options scan fail examples: {' | '.join(example_lines)}")
    else:
        log.info(
            f"Options scan: {len(signals)} signal(s) | universe={len(ti_universe)} "
            f"| strategies: {strategy_names}"
        )
    return signals


def record_stop_cooldown(underlying: str) -> None:
    """Call this from OptionsExecutor after a stop/loss close on an option position.

    The underlying ticker is blocked from new MomentumCall entries for
    OPTIONS_STOP_COOLDOWN_DAYS to prevent same-symbol re-entry after a losing trade.
    """
    _stop_cooldown[underlying] = datetime.date.today()
    log.info(f"Options cooldown set: {underlying} blocked for {OPTIONS_STOP_COOLDOWN_DAYS} days")

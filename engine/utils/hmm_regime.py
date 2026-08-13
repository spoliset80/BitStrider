"""Per-symbol 2-state Gaussian HMM regime detector.

Fits on 7 days of 1-min close-price log returns -- the same per-symbol 1-min
bar convention most strategies already use (Momentum, GapBreakout, ORB,
VWAPReclaim) -- to classify the symbol's current regime as bullish or
bearish. Used only as a confidence *bonus* in scan_universe() when a
signal's direction agrees with the detected regime; it is never a hard gate,
so a fit failure or insufficient history just means no bonus, not a blocked
signal.

Fitting an HMM per symbol on every scan cycle would be wasteful, so results
are cached per symbol for _CACHE_TTL_SEC.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np

log = logging.getLogger("ApexTrader")

_cache: dict = {}  # symbol -> (monotonic_ts, Optional[bool])
_CACHE_TTL_SEC = 900  # 15 min -- re-fit periodically, not every scan cycle
_MIN_SAMPLES = 200    # need enough 1-min returns for a stable 2-state fit


def get_hmm_regime(symbol: str, lookback_days: int = 7) -> Optional[bool]:
    """Return True (bull) / False (bear) / None (insufficient data or fit failure).

    Fits a 2-state Gaussian HMM on `lookback_days` of 1-min log returns for
    `symbol`, then classifies the most recent bar's hidden state by comparing
    the two states' mean returns (higher mean = bull state).
    """
    now = time.monotonic()
    cached = _cache.get(symbol)
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SEC:
        return cached[1]

    result = _fit_and_classify(symbol, lookback_days)
    _cache[symbol] = (now, result)
    return result


def _fetch_hmm_bars(symbol: str, days: int):
    """Fetch historical 1-min bars for HMM fitting directly via Alpaca.

    Bypasses get_bars()'s real-time "is the latest bar stale" check, which is
    the wrong test here: an HMM fit over several days of history is expected
    to end on an old bar whenever the market is closed (after-hours,
    pre-market, weekends) -- that's not staleness, it's just when trading
    last happened. Applying that live-freshness check was forcing a slow
    yfinance fallback on almost every off-hours call.
    """
    import datetime
    import pytz
    from engine.utils.bars import get_data_client, _normalize_df, _parse_timeframe
    from alpaca.data.requests import StockBarsRequest

    et = pytz.timezone("America/New_York")
    client = get_data_client()
    tf = _parse_timeframe("1m")
    end_dt = datetime.datetime.now(et)
    start_dt = end_dt - datetime.timedelta(days=days)
    bars = client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=tf,
        start=start_dt.astimezone(pytz.UTC).isoformat().replace("+00:00", "Z"),
        end=end_dt.astimezone(pytz.UTC).isoformat().replace("+00:00", "Z"),
        feed="iex",
    ))
    if symbol in bars.data:
        return _normalize_df(bars.df.reset_index())
    return None


def _fit_and_classify(symbol: str, lookback_days: int) -> Optional[bool]:
    try:
        from hmmlearn.hmm import GaussianHMM

        # No yfinance fallback here on purpose: this bonus is optional, and
        # get_bars()'s yfinance path retries up to 3x with exponential
        # backoff (2-10s each) on thin/illiquid symbols -- multiplied across
        # a scan cycle that's a real slowdown for a signal that isn't
        # required. Fail fast instead; the symbol just gets no HMM bonus.
        try:
            bars = _fetch_hmm_bars(symbol, lookback_days)
        except Exception as e:
            log.debug(f"{symbol}: HMM direct Alpaca fetch failed: {e}")
            return None

        if bars is None or bars.empty or len(bars) < _MIN_SAMPLES:
            return None

        closes  = bars["close"].astype(float).values
        returns = np.diff(np.log(closes))
        returns = returns[np.isfinite(returns)]
        if len(returns) < _MIN_SAMPLES:
            return None

        # n_iter kept small: we only need a stable 2-way bull/bear split, not
        # full log-likelihood convergence -- this is the dominant cost after
        # the data fetch, and cuts fit time substantially with no meaningful
        # change to the resulting classification.
        X = returns.reshape(-1, 1)
        model = GaussianHMM(n_components=2, covariance_type="diag", n_iter=15, tol=1e-2, random_state=42)
        model.fit(X)

        hidden_states = model.predict(X)
        state_means = [
            returns[hidden_states == i].mean() if (hidden_states == i).any() else -np.inf
            for i in range(2)
        ]
        bull_state    = int(np.argmax(state_means))
        current_state = int(hidden_states[-1])
        return current_state == bull_state
    except Exception as e:
        log.debug(f"{symbol}: HMM regime fit failed: {e}")
        return None

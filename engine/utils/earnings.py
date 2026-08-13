"""Shared earnings-date lookup, used by both options and equity strategies
to avoid entering right before an earnings report."""

import datetime
import logging

log = logging.getLogger(__name__)

# Session-level earnings cache — {symbol: next_earnings_date or None}
# Earnings dates are stable intraday so a session cache is sufficient.
_earnings_cache: dict = {}


def no_earnings_soon(symbol: str, days: int = 15) -> bool:
    """Return True if no earnings are expected within *days* calendar days.

    Data source: yfinance Ticker.calendar — returns the next earnings date
    for most US-listed stocks. Falls back to True (allow trade) when:
      - yfinance is not installed
      - the calendar field is absent or unparseable
      - the API call fails
    so a yfinance outage never blocks trading.
    """
    if symbol in _earnings_cache:
        next_date = _earnings_cache[symbol]
        if next_date is None:
            return True   # no date known — allow
        return (next_date - datetime.date.today()).days > days

    try:
        import yfinance as _yf
        cal = _yf.Ticker(symbol).calendar
        # calendar is a dict; earnings date is under "Earnings Date" key
        # yfinance returns it as a list of Timestamps or a single Timestamp
        earnings_entry = None
        if cal is not None:
            raw = cal.get("Earnings Date") or cal.get("Earnings Dates")
            if raw is not None:
                if hasattr(raw, "__iter__") and not isinstance(raw, str):
                    raw_list = list(raw)
                    earnings_entry = raw_list[0] if raw_list else None
                else:
                    earnings_entry = raw
        if earnings_entry is not None:
            import pandas as _pd
            ts = _pd.Timestamp(earnings_entry)
            next_date = ts.date()
            _earnings_cache[symbol] = next_date
            gap = (next_date - datetime.date.today()).days
            if gap <= days:
                log.debug(f"{symbol}: earnings in {gap}d — skipping entry")
            return gap > days
    except Exception as e:
        log.debug(f"no_earnings_soon({symbol}): yfinance failed ({e}) — allowing trade")

    _earnings_cache[symbol] = None   # cache miss — no date available
    return True

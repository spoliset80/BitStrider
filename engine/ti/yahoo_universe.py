"""
Yahoo Finance equity universe -- the source for data/ti_primary.json.

2026-08-28, user request ("stop the webscrapping from Trade ideas. instead
use yahoo finance trending now, top gainer and top looser list"): TI's
Selenium/Edge scraper was replaced as the equity-universe source.
2026-09-01: the scraper (capture_tradeideas.py) was deleted outright.

No login, no browser, no session to expire -- three plain JSON GETs
(yfinance's screener wraps Yahoo's own endpoints; trending is a second
undocumented-but-stable endpoint yfinance doesn't wrap, called directly).
Writes data/ti_primary.json in the same {"updated", "tickers"} shape the
scraper used to, so get_ti_primary() (engine/equity/universe.py) and
everything downstream (scan.py's staleness/TTL check, batch cap, guardrails)
needed zero changes -- only the producer changed.

day_gainers -> long candidates, day_losers -> short candidates, each tagged
with its live regularMarketChangePercent and written to their own universe.json
tier (PRIORITY_1_MOMENTUM = long, PRIORITY_2_ESTABLISHED = short). Trending has
no inherent direction (it's attention, not movement), so it stays in the
combined ti_primary.json pool only -- not tier-tagged.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from pathlib import Path
from typing import List, Tuple

import requests
import yfinance as yf

log = logging.getLogger("ApexTrader")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
TI_PRIMARY_FILE = DATA_DIR / "ti_primary.json"


# -- Hard wall-clock bound on yfinance's screener ------------------------------
# Confirmed 2026-09-02: yf.screen() (day_gainers/day_losers) carries no HTTP
# timeout and black-holed for ~7 minutes, wedging the whole main-loop cycle
# (heartbeat not written, watchdog crash logic never fired). Run it on a
# daemon thread and abandon it after _YF_SCREEN_TIMEOUT_SEC; an empty result
# for that cycle is fail-open (the tier TTL / staleness checks downstream
# handle a missed refresh), exactly like the existing exception path.
_YF_SCREEN_TIMEOUT_SEC = float(os.getenv("YF_SCREEN_TIMEOUT_SEC", "30"))


def _screen_with_timeout(query: str, count: int) -> dict:
    box: dict = {}

    def _runner() -> None:
        try:
            box["r"] = yf.screen(query, count=count)
        except BaseException as exc:  # noqa: BLE001 -- surfaced to caller
            box["e"] = exc

    th = threading.Thread(target=_runner, daemon=True, name=f"yahoo-screen-{query}")
    th.start()
    th.join(_YF_SCREEN_TIMEOUT_SEC)
    if th.is_alive():
        log.warning(f"[YAHOO] {query} screener exceeded {_YF_SCREEN_TIMEOUT_SEC:.0f}s wall-clock bound -- empty this cycle")
        return {}
    if "e" in box:
        raise box["e"]  # type: ignore[misc]
    return box["r"]


def _is_valid_ti_ticker(symbol: str) -> bool:
    """Same ticker-sanity filter the TI scraper used (1-5 alpha chars)."""
    if not symbol or not isinstance(symbol, str):
        return False
    symbol = symbol.strip().upper()
    return 1 <= len(symbol) <= 5 and symbol.isalpha()


_TRENDING_URL = "https://query1.finance.yahoo.com/v1/finance/trending/US"
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_SCREENER_COUNT = 50  # match Yahoo's 50-row gainers/losers tables; deduped downstream


def _screener_quotes(query: str, count: int = _SCREENER_COUNT) -> List[Tuple[str, float]]:
    """Return [(symbol, pct_change), ...], filtered to valid tickers, deduped,
    sorted by |pct_change| descending (biggest movers first)."""
    try:
        result = _screen_with_timeout(query, count)
    except Exception as e:
        log.warning(f"[YAHOO] {query} screener failed: {e}")
        return []
    seen: set = set()
    out: List[Tuple[str, float]] = []
    for q in result.get("quotes", []):
        sym = str(q.get("symbol", "")).strip().upper()
        if sym in seen or not _is_valid_ti_ticker(sym):
            continue
        pct = q.get("regularMarketChangePercent")
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            pct = 0.0
        seen.add(sym)
        out.append((sym, pct))
    out.sort(key=lambda t: abs(t[1]), reverse=True)
    return out


def _trending_symbols() -> List[str]:
    try:
        resp = requests.get(_TRENDING_URL, headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        quotes = resp.json()["finance"]["result"][0]["quotes"]
        out = []
        seen = set()
        for q in quotes:
            sym = str(q.get("symbol", "")).strip().upper()
            if sym and sym not in seen and _is_valid_ti_ticker(sym):
                seen.add(sym)
                out.append(sym)
        return out
    except Exception as e:
        log.warning(f"[YAHOO] trending endpoint failed: {e}")
        return []


def fetch_long_short_candidates() -> Tuple[List[Tuple[str, float]], List[Tuple[str, float]]]:
    """Return (gainers, losers), each [(symbol, pct_change_today), ...].
    Gainers = long candidates, losers = short candidates -- both today's
    real % move, straight from the same day_gainers/day_losers screeners."""
    return _screener_quotes("day_gainers"), _screener_quotes("day_losers")


def fetch_yahoo_universe() -> List[str]:
    """Gainers + losers + trending, deduped (order preserved, first-seen wins),
    filtered to real equity tickers (_is_valid_ti_ticker drops crypto pairs
    like BTC-USD, indices, garbage) -- same shape TI's scrape used to produce.
    This is the combined, direction-agnostic pool (data/ti_primary.json);
    see fetch_long_short_candidates() for the tier-split version."""
    gainers, losers = fetch_long_short_candidates()
    raw = [s for s, _ in gainers] + [s for s, _ in losers] + _trending_symbols()
    seen: set = set()
    out: List[str] = []
    for s in raw:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def write_long_short_lists(gainers: List[Tuple[str, float]], losers: List[Tuple[str, float]]) -> None:
    """Write gainers -> universe.json tier 1 (long candidates), losers ->
    tier 2 (short candidates). Best-effort: a failure here must not affect
    ti_primary.json, which is the half everything else actually depends on."""
    try:
        from engine.equity.universe import add_tickers
        added_long  = add_tickers([s for s, _ in gainers], tier=1)
        added_short = add_tickers([s for s, _ in losers], tier=2)
        _fmt = lambda lst: ", ".join(f"{s} {p:+.1f}%" for s, p in lst[:10])
        log.info(f"[YAHOO] LONG candidates (gainers, +{added_long} new -> universe.json tier 1): {_fmt(gainers)}")
        log.info(f"[YAHOO] SHORT candidates (losers, +{added_short} new -> universe.json tier 2): {_fmt(losers)}")
    except Exception as e:
        log.warning(f"[YAHOO] long/short tier write failed: {e}")


def write_ti_primary() -> int:
    """Fetch + write data/ti_primary.json, and separately tag today's
    gainers/losers into their own long/short tiers in universe.json. Returns
    ticker count written to ti_primary.json (0 on total failure -- file is
    left untouched, same fail-open behavior TI's scrape had via the
    staleness TTL in universe.py)."""
    gainers, losers = fetch_long_short_candidates()
    if gainers or losers:
        write_long_short_lists(gainers, losers)

    trending = _trending_symbols()
    seen: set = set()
    tickers: List[str] = []
    for s in [s for s, _ in gainers] + [s for s, _ in losers] + trending:
        if s in seen:
            continue
        seen.add(s)
        tickers.append(s)

    if not tickers:
        log.warning("[YAHOO] fetch returned 0 tickers -- leaving ti_primary.json untouched")
        return 0
    data = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tickers": tickers,
    }
    TI_PRIMARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    TI_PRIMARY_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info(f"[YAHOO] ti_primary.json updated: {len(tickers)} tickers -- {tickers[:10]}{'...' if len(tickers) > 10 else ''}")
    return len(tickers)


def demo() -> None:
    """Smallest runnable check: live-fetch and confirm both the combined
    pool and the long/short split are sane."""
    gainers, losers = fetch_long_short_candidates()
    assert isinstance(gainers, list) and isinstance(losers, list)
    assert all(isinstance(p, float) for _, p in gainers + losers), "pct_change must be float"
    # Directional sanity: screeners can occasionally return a flat/zero mover,
    # but the bulk of each list should point the labeled direction.
    if gainers:
        up = sum(1 for _, p in gainers if p >= 0)
        assert up / len(gainers) > 0.7, f"day_gainers mostly non-positive ({up}/{len(gainers)}) -- screener query wrong?"
    if losers:
        down = sum(1 for _, p in losers if p <= 0)
        assert down / len(losers) > 0.7, f"day_losers mostly non-negative ({down}/{len(losers)}) -- screener query wrong?"
    print(f"[demo] LONG candidates ({len(gainers)}): {gainers[:10]}")
    print(f"[demo] SHORT candidates ({len(losers)}): {losers[:10]}")

    tickers = fetch_yahoo_universe()
    assert isinstance(tickers, list), "fetch_yahoo_universe must return a list"
    assert len(tickers) == len(set(tickers)), "duplicates leaked through dedup"
    assert all(_is_valid_ti_ticker(t) for t in tickers), "invalid ticker leaked through filter"
    print(f"[demo] combined pool: {len(tickers)} tickers")

    n = write_ti_primary()
    assert n == len(tickers), "write_ti_primary count mismatch"
    written = json.loads(TI_PRIMARY_FILE.read_text(encoding="utf-8"))
    assert written["tickers"] == tickers, "file content doesn't match fetch"
    print(f"[demo] wrote {n} tickers to {TI_PRIMARY_FILE}")
    print("[demo] OK")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    demo()

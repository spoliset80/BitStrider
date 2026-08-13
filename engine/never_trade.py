"""Permanent ticker exclusion list — data/never_trade.txt.

Shared by universe filtering, TI scraping, and both execution paths so a
listed ticker can never be scanned, added to universe.json, or ordered.
"""

from __future__ import annotations

from pathlib import Path

_FILE = Path(__file__).resolve().parent.parent / "data" / "never_trade.txt"

_cache: set[str] = set()
_cache_mtime: float = 0.0


def load_never_trade() -> set[str]:
    """Return the set of permanently excluded tickers.

    Re-reads the file when its mtime changes, so edits apply on the next
    scan cycle without restarting the bot.
    """
    global _cache, _cache_mtime
    try:
        mtime = _FILE.stat().st_mtime
    except FileNotFoundError:
        return set()

    if mtime != _cache_mtime:
        tickers: set[str] = set()
        for line in _FILE.read_text(encoding="utf-8").splitlines():
            sym = line.split("#", 1)[0].strip().upper()
            if sym:
                tickers.add(sym)
        _cache = tickers
        _cache_mtime = mtime

    return _cache


def is_never_trade(symbol: str) -> bool:
    return symbol.strip().upper() in load_never_trade()

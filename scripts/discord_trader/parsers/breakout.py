"""Breakout channel parser — extracts swing signals from rich embeds.

The Invex_BreakOuts bot posts embeds like:

    title:       "GE  -  Breakout Alert"
    description: "**$366.18**  +2.72%  |  Daily
                  52-Week High (52w: $369.25)
                  Resistance Break (broke $364.70)"
    fields[0]:   name="Trade Levels"
                 value="Entry **$366.18**
                        Support $306.35   |   Resistance $364.70
                        Stop $306.35  (risk $59.83 / 16.3%)
                        T1 $426.01 (+16.3%, 1R)
                        T2 $485.84 (+32.7%, 2R)
                        T3 $545.67 (+49.0%, 3R)"

These are bullish breakouts to 52-week highs → traded as swing CALLS.
"""
from __future__ import annotations
import re
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_RE_TITLE   = re.compile(r"^([A-Z]{1,6})\b")
_RE_ENTRY   = re.compile(r"Entry\s*\**\$?([\d,]+(?:\.\d+)?)", re.I)
_RE_STOP    = re.compile(r"Stop\s*\**\$?([\d,]+(?:\.\d+)?)", re.I)
_RE_SUPPORT = re.compile(r"Support\s*\**\$?([\d,]+(?:\.\d+)?)", re.I)
_RE_RESIST  = re.compile(r"Resistance\s*\**\$?([\d,]+(?:\.\d+)?)", re.I)
_RE_T1      = re.compile(r"T1\s*\**\$?([\d,]+(?:\.\d+)?)", re.I)
_RE_T2      = re.compile(r"T2\s*\**\$?([\d,]+(?:\.\d+)?)", re.I)
_RE_T3      = re.compile(r"T3\s*\**\$?([\d,]+(?:\.\d+)?)", re.I)


@dataclass
class BreakoutSignal:
    ticker:     str
    entry:      float
    stop:       Optional[float] = None
    support:    Optional[float] = None
    resistance: Optional[float] = None
    t1:         Optional[float] = None
    t2:         Optional[float] = None
    t3:         Optional[float] = None
    raw:        str = ""


def _num(m) -> Optional[float]:
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except (ValueError, IndexError):
        return None


def parse_breakout(msg: dict) -> Optional[BreakoutSignal]:
    """Parse a Discord message dict (with embeds) into a BreakoutSignal."""
    embeds = msg.get("embeds") or []
    if not embeds:
        return None

    emb   = embeds[0]
    title = emb.get("title", "") or ""
    if "breakout" not in title.lower():
        return None

    tm = _RE_TITLE.match(title.strip())
    if not tm:
        return None
    ticker = tm.group(1).upper()

    # Gather all text (description + field values) to search for levels
    text = emb.get("description", "") or ""
    for f in emb.get("fields", []):
        text += "\n" + (f.get("value", "") or "")

    entry = _num(_RE_ENTRY.search(text))
    if entry is None:
        logger.info(f"  [BREAKOUT] {ticker}: no entry price in embed — skipping")
        return None

    return BreakoutSignal(
        ticker     = ticker,
        entry      = entry,
        stop       = _num(_RE_STOP.search(text)),
        support    = _num(_RE_SUPPORT.search(text)),
        resistance = _num(_RE_RESIST.search(text)),
        t1         = _num(_RE_T1.search(text)),
        t2         = _num(_RE_T2.search(text)),
        t3         = _num(_RE_T3.search(text)),
        raw        = title.strip(),
    )

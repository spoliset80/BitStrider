"""
Smart Universe Manager — Tier-Based Ticker Segmentation

Organizes options trading universe into 3 tiers:
1. TIER A (Mega-Liquid): AAPL, MSFT, NVDA, SPY, QQQ — Always scan, strict filters
2. TIER B (Unusual Vol): Dynamic (TI unusual options volume) — Rotate, relaxed filters
3. TIER C (Equity Bridge): Linked to equity breakouts — Hedging + correlation

Provides configurable allocation, position limits, and entry thresholds per tier.
"""

import logging
import datetime
import time
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import json

from engine.config import (
    TIER_A_ALLOCATION_PCT,
    TIER_A_MAX_POSITIONS,
    TIER_A_CONFIDENCE_MIN,
    TIER_A_SCAN_EVERY_MIN,
    TIER_A_TICKERS,
    
    TIER_B_ALLOCATION_PCT,
    TIER_B_MAX_POSITIONS,
    TIER_B_CONFIDENCE_MIN,
    TIER_B_IV_RANK_MAX,
    TIER_B_HOLD_DAYS,
    TIER_B_SCAN_EVERY_MIN,
    TIER_B_LIQUID_HOURS_ONLY,
    
    TIER_C_ALLOCATION_PCT,
    TIER_C_MAX_POSITIONS,
    TIER_C_CONFIDENCE_MIN,
    TIER_C_SELL_PUT_BUFFER_PCT,
    TIER_C_BUY_CALL_BUFFER_PCT,
    
    SMART_UNIVERSE_ENABLED,
)
from engine.utils import is_market_open

log = logging.getLogger("ApexTrader.SmartUniverse")


class UniverseTier:
    """Single tier configuration snapshot."""
    def __init__(
        self,
        name: str,
        allocation_pct: float,
        max_positions: int,
        confidence_min: float,
        tickers: List[str],
        iv_rank_max: Optional[float] = None,
        hold_days: Optional[int] = None,
        scan_interval_min: Optional[int] = None,
        liquid_hours_only: bool = False,
    ):
        self.name = name
        self.allocation_pct = allocation_pct
        self.max_positions = max_positions
        self.confidence_min = confidence_min
        self.tickers = tickers or []
        self.iv_rank_max = iv_rank_max
        self.hold_days = hold_days
        self.scan_interval_min = scan_interval_min
        self.liquid_hours_only = liquid_hours_only
        self.last_scan_time: float = 0.0
        self.active_positions: Dict[str, dict] = {}  # {symbol: {"entered_at": date, ...}}

    def can_scan(self) -> bool:
        """Check if enough time elapsed since last scan."""
        if not self.scan_interval_min:
            return True
        elapsed_min = (time.time() - self.last_scan_time) / 60.0
        return elapsed_min >= self.scan_interval_min

    def should_scan_now(self) -> bool:
        """Check if tier should scan at this moment."""
        if not self.can_scan():
            return False
        if self.liquid_hours_only:
            # 9:30-11:00 AM OR 2:00-3:00 PM ET
            now = datetime.datetime.now(datetime.timezone.utc).astimezone()
            hour = now.hour
            minute = now.minute
            hm = hour * 60 + minute
            in_morning = 570 <= hm <= 660  # 9:30-11:00
            in_afternoon = 840 <= hm <= 900  # 2:00-3:00 PM
            return in_morning or in_afternoon
        return True

    def available_positions(self) -> int:
        """How many more positions can this tier open?"""
        return max(0, self.max_positions - len(self.active_positions))

    def is_full(self) -> bool:
        """Is tier at max position capacity?"""
        return len(self.active_positions) >= self.max_positions

    def get_capital_allocation(self, total_equity: float) -> float:
        """Allocated capital for this tier."""
        return total_equity * (self.allocation_pct / 100.0)

    def __repr__(self) -> str:
        return (
            f"UniverseTier({self.name}, alloc={self.allocation_pct}%, "
            f"max_pos={self.max_positions}, conf_min={self.confidence_min}, "
            f"tickers={len(self.tickers)}, active={len(self.active_positions)})"
        )


class SmartUniverse:
    """Manages all 3 tiers, tracks active positions, enforces limits."""

    def __init__(self):
        self.tier_a = UniverseTier(
            name="Tier A (Mega-Liquid)",
            allocation_pct=TIER_A_ALLOCATION_PCT,
            max_positions=TIER_A_MAX_POSITIONS,
            confidence_min=TIER_A_CONFIDENCE_MIN,
            tickers=TIER_A_TICKERS,
            scan_interval_min=TIER_A_SCAN_EVERY_MIN,
        )
        
        self.tier_b = UniverseTier(
            name="Tier B (Unusual Vol)",
            allocation_pct=TIER_B_ALLOCATION_PCT,
            max_positions=TIER_B_MAX_POSITIONS,
            confidence_min=TIER_B_CONFIDENCE_MIN,
            tickers=[],  # Loaded dynamically from TI
            iv_rank_max=TIER_B_IV_RANK_MAX,
            hold_days=TIER_B_HOLD_DAYS,
            scan_interval_min=TIER_B_SCAN_EVERY_MIN,
            liquid_hours_only=TIER_B_LIQUID_HOURS_ONLY,
        )
        
        self.tier_c = UniverseTier(
            name="Tier C (Equity Bridge)",
            allocation_pct=TIER_C_ALLOCATION_PCT,
            max_positions=TIER_C_MAX_POSITIONS,
            confidence_min=TIER_C_CONFIDENCE_MIN,
            tickers=[],  # Populated from equity signals
        )
        
        self.all_tiers = [self.tier_a, self.tier_b, self.tier_c]
        log.info(f"SmartUniverse initialized: {self.tier_a} | {self.tier_b} | {self.tier_c}")

    def load_tier_b_universe(self, tickers: List[str]) -> None:
        """Update Tier B dynamic universe from TI unusual options volume."""
        self.tier_b.tickers = list(dict.fromkeys(tickers))  # Dedupe
        self.tier_b.last_scan_time = time.time()
        log.info(f"Tier B universe updated: {len(self.tier_b.tickers)} tickers from TI")

    def add_position(self, symbol: str, tier_name: str, entry_date: Optional[datetime.date] = None) -> bool:
        """Register a newly-opened position in a tier."""
        tier = self._get_tier(tier_name)
        if not tier:
            log.warning(f"Unknown tier: {tier_name}")
            return False
        
        if tier.is_full():
            log.warning(f"{tier.name} is full (max {tier.max_positions})")
            return False
        
        if symbol in tier.active_positions:
            log.warning(f"{symbol} already in {tier.name}")
            return False
        
        tier.active_positions[symbol] = {"entered_at": entry_date or datetime.date.today()}
        log.info(f"Position added to {tier.name}: {symbol}")
        return True

    def remove_position(self, symbol: str, tier_name: str) -> bool:
        """Close a position in a tier."""
        tier = self._get_tier(tier_name)
        if not tier or symbol not in tier.active_positions:
            return False
        
        tier.active_positions.pop(symbol, None)
        log.info(f"Position removed from {tier.name}: {symbol}")
        return True

    def get_tier_positions(self, tier_name: str) -> Dict[str, dict]:
        """Get all active positions in a tier."""
        tier = self._get_tier(tier_name)
        return tier.active_positions if tier else {}

    def get_recommendations(self, total_equity: float) -> Dict[str, any]:
        """Return scanning recommendations for each tier."""
        now = datetime.datetime.now()
        recs = {}
        
        for tier in self.all_tiers:
            should_scan = tier.should_scan_now() if is_market_open() else False
            available_pos = tier.available_positions()
            capital = tier.get_capital_allocation(total_equity)
            
            recs[tier.name] = {
                "should_scan": should_scan,
                "available_positions": available_pos,
                "allocated_capital": capital,
                "tickers_to_scan": tier.tickers[:available_pos * 5],  # Show 5 tickers per available slot
                "confidence_min": tier.confidence_min,
                "timestamp": now.isoformat(),
            }
            
            # Special flags for each tier
            if tier.name == "Tier B (Unusual Vol)":
                recs[tier.name]["iv_rank_max"] = tier.iv_rank_max
                recs[tier.name]["hold_days"] = tier.hold_days
            elif tier.name == "Tier C (Equity Bridge)":
                recs[tier.name]["sell_put_buffer_pct"] = TIER_C_SELL_PUT_BUFFER_PCT
                recs[tier.name]["buy_call_buffer_pct"] = TIER_C_BUY_CALL_BUFFER_PCT
        
        return recs

    def prune_tier_b_stale(self) -> List[str]:
        """Close Tier B positions older than TIER_B_HOLD_DAYS."""
        closed = []
        today = datetime.date.today()
        cutoff = today - datetime.timedelta(days=TIER_B_HOLD_DAYS)
        
        for symbol, info in list(self.tier_b.active_positions.items()):
            entered_at = info.get("entered_at", today)
            if entered_at <= cutoff:
                self.remove_position(symbol, "Tier B (Unusual Vol)")
                closed.append(symbol)
                log.info(f"Tier B stale position closed: {symbol} (held {(today - entered_at).days} days)")
        
        return closed

    def log_status(self) -> None:
        """Print comprehensive status of all tiers."""
        log.info("=" * 80)
        log.info("SMART UNIVERSE STATUS")
        log.info("=" * 80)
        for tier in self.all_tiers:
            log.info(f"{tier.name}")
            log.info(f"  Allocation: {tier.allocation_pct}% | Max Positions: {tier.max_positions}")
            log.info(f"  Active: {len(tier.active_positions)} | Available slots: {tier.available_positions()}")
            log.info(f"  Confidence Min: {tier.confidence_min:.0%} | Universe size: {len(tier.tickers)}")
            if tier.active_positions:
                log.info(f"  Positions: {', '.join(tier.active_positions.keys())}")
        log.info("=" * 80)

    def _get_tier(self, tier_name: str) -> Optional[UniverseTier]:
        """Resolve tier by name."""
        tier_map = {
            "Tier A (Mega-Liquid)": self.tier_a,
            "Tier B (Unusual Vol)": self.tier_b,
            "Tier C (Equity Bridge)": self.tier_c,
        }
        return tier_map.get(tier_name)


# Singleton instance
_smart_universe: Optional[SmartUniverse] = None


def get_smart_universe() -> SmartUniverse:
    """Get or create singleton SmartUniverse."""
    global _smart_universe
    if _smart_universe is None:
        _smart_universe = SmartUniverse()
    return _smart_universe


def reset_smart_universe() -> None:
    """Reset singleton (for testing)."""
    global _smart_universe
    _smart_universe = None

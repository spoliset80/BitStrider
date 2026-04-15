"""
Tier-Aware Position Sizing & Allocation Helper

Calculates contract quantities and capital allocation based on:
- Available tier capital
- Current tier positions
- Market regime multipliers (from adaptive coordinator)
- Timing phase multipliers (from execution timing engine)
- Risk parameters (entry confidence, IV rank, R/R)
"""

import logging
from typing import Dict, Optional, Tuple
import datetime

from engine.options.smart_universe import get_smart_universe
from engine.execution.adaptive_coordinator import get_adaptive_coordinator
from engine.execution.timing import get_timing_engine
from engine.options.strategies import CONTRACT_SIZE

log = logging.getLogger("ApexTrader.TierSizing")


class TierAllocationEngine:
    """Calculates position sizes and capital allocation per tier."""

    def __init__(self):
        self.smart_universe = get_smart_universe()
        self.coordinator = get_adaptive_coordinator()
        self.timing_engine = get_timing_engine()

    def calculate_tier_position_size(
        self,
        tier_name: str,
        total_equity: float,
        entry_confidence: float,
        iv_rank: float,
        rr_ratio: float,
        option_price: float,  # premium per share
    ) -> Tuple[int, Dict[str, any]]:
        """
        Calculate contract quantity for a tier-appropriate position.
        
        Returns:
            (contracts: int, metadata: Dict with sizing breakdown)
        """
        tier = self.smart_universe._get_tier(tier_name)
        if not tier:
            return (0, {"error": f"Unknown tier: {tier_name}"})

        # Base capital allocation for this tier
        tier_capital = tier.get_capital_allocation(total_equity)
        
        # Reduce if tier is filling up (use only available position slots)
        available_slots = tier.available_positions()
        if available_slots <= 0:
            return (0, {"error": f"{tier_name} is at max positions"})
        
        # Distribute across available slots
        position_capital = tier_capital / available_slots
        
        # Apply regime multiplier (from adaptive coordinator)
        regime_mult = self.coordinator.get_adaptive_position_size_mult() if self.coordinator else 1.0
        position_capital *= regime_mult
        
        # Apply timing multiplier (from execution timing engine)
        timing_mult = self.timing_engine.get_position_size_multiplier() if self.timing_engine else 1.0
        position_capital *= timing_mult
        
        # Apply confidence-based scaling (higher confidence = bigger position, but capped)
        if entry_confidence >= tier.confidence_min:
            conf_excess = entry_confidence - tier.confidence_min
            conf_mult = 1.0 + min(0.3, conf_excess * 0.5)  # +0% to +30% for confidence
            position_capital *= conf_mult
        else:
            # Below tier min: reduce sizing
            conf_deficit = tier.confidence_min - entry_confidence
            conf_mult = max(0.5, 1.0 - conf_deficit * 0.5)  # 50%-100% based on deficit
            position_capital *= conf_mult
        
        # Apply IV rank scaling (lower IV rank = cheaper premium = more contracts)
        if tier.iv_rank_max and iv_rank < tier.iv_rank_max:
            iv_bonus = (tier.iv_rank_max - iv_rank) / tier.iv_rank_max
            iv_mult = 1.0 + min(0.2, iv_bonus * 0.3)  # +0% to +20% for cheap vol
            position_capital *= iv_mult
        
        # Apply R/R scaling (higher R/R = more attractive = slightly bigger)
        if rr_ratio > 1.0:
            rr_mult = min(1.3, 1.0 + (rr_ratio - 1.0) * 0.1)  # +0% to +30% for R/R
            position_capital *= rr_mult
        
        # Convert capital to contract quantity
        capital_per_contract = option_price * CONTRACT_SIZE  # $cost_per_share × 100
        if capital_per_contract <= 0:
            return (0, {"error": "Option price invalid"})
        
        contracts = int(position_capital / capital_per_contract)
        contracts = max(1, min(contracts, 5))  # Min 1, max 5 per tier signal
        
        # Metadata for logging/transparency
        metadata = {
            "tier_name": tier_name,
            "tier_capital": tier_capital,
            "available_slots": available_slots,
            "base_position_capital": tier_capital / available_slots,
            "regime_multiplier": regime_mult,
            "timing_multiplier": timing_mult,
            "confidence_multiplier": conf_mult,
            "iv_multiplier": iv_mult if tier.iv_rank_max else 1.0,
            "rr_multiplier": rr_mult if rr_ratio > 1.0 else 1.0,
            "final_capital": position_capital,
            "capital_per_contract": capital_per_contract,
            "contracts": contracts,
            "entry_confidence": entry_confidence,
            "iv_rank": iv_rank,
            "rr_ratio": rr_ratio,
        }
        
        return (contracts, metadata)

    def get_tier_status(self, total_equity: float) -> Dict[str, any]:
        """Get comprehensive status of all tiers for logging."""
        status = {}
        for tier in self.smart_universe.all_tiers:
            recs = self.smart_universe.get_recommendations(total_equity)
            tier_key = tier.name
            status[tier_key] = {
                "allocation_pct": tier.allocation_pct,
                "allocated_capital": tier.get_capital_allocation(total_equity),
                "active_positions": len(tier.active_positions),
                "max_positions": tier.max_positions,
                "available_slots": tier.available_positions(),
                "confidence_min": tier.confidence_min,
                "can_scan": tier.can_scan(),
                "tickers_available": len(tier.tickers),
            }
            if tier.iv_rank_max:
                status[tier_key]["iv_rank_max"] = tier.iv_rank_max
            if tier.hold_days:
                status[tier_key]["hold_days"] = tier.hold_days
        return status

    def log_sizing_breakdown(self, contracts: int, metadata: Dict) -> None:
        """Log detailed sizing calculation for transparency."""
        if "error" in metadata:
            log.warning(f"Sizing failed: {metadata['error']}")
            return
        
        log.info(f"[SIZING] {metadata['tier_name']} position")
        log.info(f"  Tier capital: ${metadata['tier_capital']:,.0f} / {metadata['available_slots']} slots = ${metadata['base_position_capital']:,.0f}")
        log.info(f"  Regime mult: {metadata['regime_multiplier']:.2f}x | Timing mult: {metadata['timing_multiplier']:.2f}x")
        log.info(f"  Confidence {metadata['entry_confidence']:.0%} mult: {metadata['confidence_multiplier']:.2f}x")
        log.info(f"  IV Rank {metadata['iv_rank']:.0f}% mult: {metadata['iv_multiplier']:.2f}x | R/R {metadata['rr_ratio']:.1f}x mult: {metadata['rr_multiplier']:.2f}x")
        log.info(f"  → Final capital: ${metadata['final_capital']:,.0f} → {contracts} contracts @ ${metadata['capital_per_contract']:,.0f}/contract")


# Singleton instance
_tier_allocation_engine: Optional[TierAllocationEngine] = None


def get_tier_allocation_engine() -> TierAllocationEngine:
    """Get or create singleton."""
    global _tier_allocation_engine
    if _tier_allocation_engine is None:
        _tier_allocation_engine = TierAllocationEngine()
    return _tier_allocation_engine

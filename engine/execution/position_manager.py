"""
Position Manager - Intelligent SWAP & Rotation Logic
Replaces the dumb "oldest position" rotation with score-based decision making.

Score Composition:
  - Momentum (20%): Recent direction + velocity
  - Profitability (30%): Unrealized P&L vs entry
  - Confidence Decay (20%): How stale is the original signal?
  - Hold Duration (15%): How long held (prefer shorter in bear regime)
  - Volatility Impact (15%): Position size vs current volatility

Only rotates positions with lowest composite score when full.
"""

import logging
import datetime
import time
from typing import Optional, Dict, Tuple
from dataclasses import dataclass, field

log = logging.getLogger("ApexTrader")


@dataclass
class PositionScore:
    """Composite health score for a position (0-100, lower = worse)."""
    symbol: str
    momentum_score: float      # 0-100 (20% weight)
    profitability_score: float # 0-100 (30% weight)
    confidence_decay_score: float  # 0-100 (20% weight)
    hold_duration_score: float     # 0-100 (15% weight)
    volatility_impact_score: float # 0-100 (15% weight)
    composite: float           # 0-100 weighted average
    entry_time: datetime.datetime = field(default_factory=datetime.datetime.now)
    unrealized_pnl_pct: float = 0.0
    
    def __str__(self) -> str:
        return (
            f"{self.symbol} Score: {self.composite:.0f} "
            f"(Mom:{self.momentum_score:.0f} Prof:{self.profitability_score:.0f} "
            f"Conf:{self.confidence_decay_score:.0f} Hold:{self.hold_duration_score:.0f} Vol:{self.volatility_impact_score:.0f}) "
            f"P&L: {self.unrealized_pnl_pct:+.1f}%"
        )


class PositionManager:
    """
    Manages intelligent position rotation based on multi-factor scoring.
    
    Usage:
        manager = PositionManager()
        score = manager.calculate_position_score(symbol, trend_direction, momentum_pct, pnl_pct, hold_mins, volatility_pct)
        worst_symbol = manager.select_weakest_position(scores_dict, regime)
    """

    def __init__(self):
        self.scores_history: Dict[str, list] = {}  # {symbol: [scores over time]}
        self._last_score_time: float = 0
        self._score_cache: Dict[str, Tuple[float, float]] = {}  # {symbol: (score, timestamp)}
        self._cache_ttl = 30.0  # 30sec cache TTL

    def calculate_position_score(
        self,
        symbol: str,
        trend_direction: str,
        momentum_pct: float,
        unrealized_pnl_pct: float,
        hold_minutes: int,
        volatility_pct: float,
        regime: str = "bull",
        entry_time: Optional[datetime.datetime] = None,
    ) -> PositionScore:
        """
        Calculate composite score for a position (0-100, lower = worse).
        
        Args:
            symbol: Ticker symbol
            trend_direction: "up", "down", or "neutral"
            momentum_pct: % momentum velocity (1.5% = strong, 0.2% = weak)
            unrealized_pnl_pct: Current P&L %
            hold_minutes: Minutes held since entry
            volatility_pct: Current ATR/close volatility %
            regime: "bull", "bear", or "range"
            entry_time: When position was entered (for decay calculation)
        
        Returns:
            PositionScore with composite 0-100 rating
        """
        if entry_time is None:
            entry_time = datetime.datetime.now()

        # ─── 1. MOMENTUM SCORE (0-100, 20% weight) ───────────────────────────
        # Strong up momentum = good (keep), strong down = bad (rotate)
        if trend_direction == "up":
            momentum_base = 70
        elif trend_direction == "down":
            momentum_base = 20
        else:
            momentum_base = 40
        
        # Adjust for velocity
        momentum_velocity_adj = min(30, max(-30, momentum_pct * 20))
        momentum_score = max(0, min(100, momentum_base + momentum_velocity_adj))

        # ─── 2. PROFITABILITY SCORE (0-100, 30% weight) ─────────────────────
        # Positions with +20% or better = excellent (100)
        # Positions at -10% or worse = poor (0)
        # Linear between
        if unrealized_pnl_pct >= 20:
            profitability_score = 100
        elif unrealized_pnl_pct <= -10:
            profitability_score = 0
        else:
            # Linear: (-10 to +20) maps to (0 to 100)
            profitability_score = ((unrealized_pnl_pct + 10) / 30.0) * 100

        # ─── 3. CONFIDENCE DECAY SCORE (0-100, 20% weight) ──────────────────
        # Fresh signals (< 15 min old) = 100
        # Stale signals (> 60 min old) = 30
        # Original signal quality was calculated at entry; this decays it
        hold_hours = hold_minutes / 60.0
        if hold_hours < 0.25:  # < 15 min
            confidence_decay_score = 100
        elif hold_hours > 1.0:  # > 60 min
            confidence_decay_score = 30
        else:
            # Linear decay from 100 to 30 over 45 minutes
            confidence_decay_score = 100 - ((hold_hours - 0.25) / 0.75 * 70)

        # ─── 4. HOLD DURATION SCORE (0-100, 15% weight) ────────────────────
        # Prefer shorter holds in bear regime, longer in bull
        target_hold_mins = 45 if regime == "bull" else 20
        if hold_minutes <= target_hold_mins:
            hold_duration_score = 100
        elif hold_minutes > (target_hold_mins + 120):  # 2 hours past target
            hold_duration_score = 20
        else:
            # Linear: target to target+120
            hold_duration_score = 100 - ((hold_minutes - target_hold_mins) / 120.0 * 80)

        # ─── 5. VOLATILITY IMPACT SCORE (0-100, 15% weight) ────────────────
        # High volatility (>15% ATR/close) = harder to manage, lower score
        # Low volatility (<3%) = great, full score
        if volatility_pct < 3.0:
            volatility_impact_score = 100
        elif volatility_pct > 15.0:
            volatility_impact_score = 40
        else:
            # Linear: 3% to 15% → 100 to 40
            volatility_impact_score = 100 - ((volatility_pct - 3.0) / 12.0 * 60)

        # ─── COMPOSITE SCORE (Weighted Average) ───────────────────────────
        composite = (
            (momentum_score * 0.20)
            + (profitability_score * 0.30)
            + (confidence_decay_score * 0.20)
            + (hold_duration_score * 0.15)
            + (volatility_impact_score * 0.15)
        )

        score = PositionScore(
            symbol=symbol,
            momentum_score=momentum_score,
            profitability_score=profitability_score,
            confidence_decay_score=confidence_decay_score,
            hold_duration_score=hold_duration_score,
            volatility_impact_score=volatility_impact_score,
            composite=composite,
            entry_time=entry_time,
            unrealized_pnl_pct=unrealized_pnl_pct,
        )

        # Cache the score
        self._score_cache[symbol] = (composite, time.time())
        
        # Track history
        if symbol not in self.scores_history:
            self.scores_history[symbol] = []
        self.scores_history[symbol].append(composite)
        if len(self.scores_history[symbol]) > 100:  # Keep last 100
            self.scores_history[symbol] = self.scores_history[symbol][-100:]

        return score

    def select_weakest_position(
        self, scores: Dict[str, PositionScore], regime: str = "bull"
    ) -> Optional[Tuple[str, PositionScore]]:
        """
        Select the worst-performing position to rotate out.
        
        Returns:
            (symbol, position_score) of weakest position, or None if all strong
        """
        if not scores:
            return None

        # Filter: only consider positions scoring below 50 (below median)
        weak_positions = {sym: score for sym, score in scores.items() if score.composite < 50}
        
        if not weak_positions:
            # No weak positions, pick the lowest score anyway
            weak_positions = scores

        # Return lowest score (worst position)
        weakest_sym = min(weak_positions.keys(), key=lambda sym: weak_positions[sym].composite)
        return (weakest_sym, weak_positions[weakest_sym])

    def log_position_scores(self, scores: Dict[str, PositionScore]) -> None:
        """Log all current position scores for visibility."""
        if not scores:
            return
        
        sorted_scores = sorted(scores.items(), key=lambda x: x[1].composite, reverse=True)
        log.info(f"[POSITIONS] Position health scores ({len(scores)} open):")
        for sym, score in sorted_scores:
            log.info(f"  {score}")


# Singleton
_position_manager: Optional[PositionManager] = None


def get_position_manager() -> PositionManager:
    global _position_manager
    if _position_manager is None:
        _position_manager = PositionManager()
    return _position_manager


def reset_position_manager() -> None:
    global _position_manager
    _position_manager = None

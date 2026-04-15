"""
Intelligent Execution Timing Engine
Optimizes trade execution based on market phase, strategy type, and intraday patterns.

Phases & optimal times:
  - PREMARKET (7:00-9:30)  : Low volume, wide spreads → skip or limit to setups
  - MOMENTUM_WINDOW (9:30-11:00): Strongest momentum, highest volume → aggressive entries
  - MID_MORNING (11:00-12:00): Post-cap-raise consolidation → momentum fades
  - LUNCH (12:00-13:00): Thinned volume, wider spreads → reduced entry size
  - AFTERNOON (13:00-15:00): Secondary breakouts + reversals → moderate activity
  - HOT_CLOSE (15:00-16:00): FOMO, short-covering, reversals → mixed signals
  - EXTENDED (16:00-20:00): Lower volume, wider spreads
"""

import datetime
import logging
import os
import pytz
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

log = logging.getLogger("ApexTrader.Timing")

ET = pytz.timezone("America/New_York")


class MarketPhase(Enum):
    """Market intraday phases."""
    PREMARKET = "premarket"
    MOMENTUM_WINDOW = "momentum_window"
    MID_MORNING = "mid_morning"
    LUNCH = "lunch"
    AFTERNOON = "afternoon"
    HOT_CLOSE = "hot_close"
    EXTENDED = "extended"
    CLOSED = "closed"


@dataclass
class PhaseProfile:
    """Execution characteristics per market phase."""
    name: str
    start_hour: float         # 24-hour float (9.5 = 9:30 AM)
    end_hour: float
    momentum_level: str       # "high", "medium", "low"
    liquidity_level: str      # "excellent", "good", "fair", "thin"
    recommended_entry_ratio: float  # 0.5 = avg 50% of normal sizing
    trend_bias: str           # "bullish", "neutral", "mixed"
    best_strategies: list     # ["TrendBreaker", "Momentum", ...]
    avoid_strategies: list    # ["ORB", ...]


# Phase configuration — edit for market microstructure tuning
PHASE_CONFIG = {
    MarketPhase.PREMARKET: PhaseProfile(
        name="Premarket",
        start_hour=7.0,
        end_hour=9.5,
        momentum_level="low",
        liquidity_level="thin",
        recommended_entry_ratio=0.0,  # Skip pre-market entries (use setups only)
        trend_bias="mixed",
        best_strategies=[],
        avoid_strategies=["Momentum", "TrendBreaker", "FloatRotation"],
    ),
    MarketPhase.MOMENTUM_WINDOW: PhaseProfile(
        name="Momentum Window (9:30-11:00 AM)",
        start_hour=9.5,
        end_hour=11.0,
        momentum_level="high",
        liquidity_level="excellent",
        recommended_entry_ratio=1.25,  # Aggressive: 125% sizing
        trend_bias="bullish",
        best_strategies=["TrendBreaker", "Momentum", "FloatRotation", "GapBreakout"],
        avoid_strategies=["Sweepea"],  # Range-bound strategies too slow
    ),
    MarketPhase.MID_MORNING: PhaseProfile(
        name="Mid-Morning (11:00 AM-12:00 PM)",
        start_hour=11.0,
        end_hour=12.0,
        momentum_level="medium",
        liquidity_level="good",
        recommended_entry_ratio=0.85,  # Default
        trend_bias="neutral",
        best_strategies=["TrendBreaker", "Momentum", "ORB"],
        avoid_strategies=[],
    ),
    MarketPhase.LUNCH: PhaseProfile(
        name="Lunch (12:00-1:00 PM)",
        start_hour=12.0,
        end_hour=13.0,
        momentum_level="low",
        liquidity_level="fair",
        recommended_entry_ratio=0.6,  # Conservative: 60% sizing
        trend_bias="mixed",
        best_strategies=["Sweepea", "VWAP Reclaim"],  # Range traders
        avoid_strategies=["FloatRotation", "GapBreakout"],
    ),
    MarketPhase.AFTERNOON: PhaseProfile(
        name="Afternoon (1:00-3:00 PM)",
        start_hour=13.0,
        end_hour=15.0,
        momentum_level="medium",
        liquidity_level="good",
        recommended_entry_ratio=0.9,  # Near-normal
        trend_bias="neutral",
        best_strategies=["TrendBreaker", "Momentum", "ORB", "VWAP Reclaim"],
        avoid_strategies=[],
    ),
    MarketPhase.HOT_CLOSE: PhaseProfile(
        name="Hot Close (3:00-4:00 PM)",
        start_hour=15.0,
        end_hour=16.0,
        momentum_level="high",
        liquidity_level="good",
        recommended_entry_ratio=0.4,  # Reduce: chaotic + PDT risk EOD
        trend_bias="bullish",  # FOMO + short-covering bias
        best_strategies=["Momentum", "ORB"],  # Quick scalps only
        avoid_strategies=["Sweepea", "FloatRotation"],  # Hold to next day
    ),
    MarketPhase.EXTENDED: PhaseProfile(
        name="Extended Hours (4:00-8:00 PM)",
        start_hour=16.0,
        end_hour=20.0,
        momentum_level="low",
        liquidity_level="thin",
        recommended_entry_ratio=0.1,  # Minimal: wide spreads
        trend_bias="mixed",
        best_strategies=[],
        avoid_strategies=["All directional"],
    ),
    MarketPhase.CLOSED: PhaseProfile(
        name="Market Closed",
        start_hour=20.0,
        end_hour=7.0,
        momentum_level="none",
        liquidity_level="none",
        recommended_entry_ratio=0.0,
        trend_bias="neutral",
        best_strategies=[],
        avoid_strategies=["All"],
    ),
}


class ExecutionTimingEngine:
    """Intelligent timing for trade execution."""

    def __init__(self):
        self.tz = ET
        self._phase_cache: Optional[Tuple[MarketPhase, float]] = None
        self._phase_cache_ts = 0.0
        self._phase_cache_ttl = 60  # 1 minute

    def get_market_phase(self, force_refresh: bool = False) -> MarketPhase:
        """Get current market phase (cached for 1 min)."""
        now = time.time()
        if (
            not force_refresh
            and self._phase_cache is not None
            and (now - self._phase_cache_ts) < self._phase_cache_ttl
        ):
            return self._phase_cache[0]

        et_now = datetime.datetime.now(self.tz)
        hour = et_now.hour + et_now.minute / 60.0

        if et_now.weekday() >= 5:  # Saturday/Sunday
            phase = MarketPhase.CLOSED
        elif hour >= 16.0 or hour < 7.0:
            phase = MarketPhase.CLOSED
        elif hour >= 15.0:
            phase = MarketPhase.HOT_CLOSE
        elif hour >= 13.0:
            phase = MarketPhase.AFTERNOON
        elif hour >= 12.0:
            phase = MarketPhase.LUNCH
        elif hour >= 11.0:
            phase = MarketPhase.MID_MORNING
        elif hour >= 9.5:
            phase = MarketPhase.MOMENTUM_WINDOW
        elif hour >= 7.0:
            phase = MarketPhase.PREMARKET
        else:
            phase = MarketPhase.CLOSED

        self._phase_cache = (phase, now)
        self._phase_cache_ts = now
        return phase

    def get_phase_profile(self, phase: Optional[MarketPhase] = None) -> PhaseProfile:
        """Get execution profile for a phase."""
        if phase is None:
            phase = self.get_market_phase()
        return PHASE_CONFIG.get(phase, PHASE_CONFIG[MarketPhase.CLOSED])

    def is_optimal_entry_time(
        self, strategy: str, phase: Optional[MarketPhase] = None
    ) -> Tuple[bool, str]:
        """Check if current phase is optimal for a strategy."""
        if phase is None:
            phase = self.get_market_phase()

        profile = self.get_phase_profile(phase)

        if phase == MarketPhase.CLOSED:
            return False, "Market closed"

        if profile.recommended_entry_ratio < 0.3:
            return False, f"{profile.name}: low liquidity ({profile.liquidity_level})"

        if strategy in profile.avoid_strategies:
            return (
                False,
                f"{profile.name}: {strategy} not optimal (avoided)",
            )

        if strategy in profile.best_strategies:
            return True, f"{profile.name}: {strategy} optimal"

        return (
            True,
            f"{profile.name}: {strategy} neutral (default allowed)",
        )

    def get_position_size_multiplier(
        self, phase: Optional[MarketPhase] = None
    ) -> float:
        """Position sizing multiplier for current phase (0.0 = skip, 1.0 = normal, 1.25 = aggressive)."""
        if phase is None:
            phase = self.get_market_phase()
        profile = self.get_phase_profile(phase)
        return profile.recommended_entry_ratio

    def should_hold_to_eod(self, strategy: str, phase: Optional[MarketPhase] = None) -> bool:
        """Whether to hold a position into EOD or close before close."""
        if phase is None:
            phase = self.get_market_phase()

        # Intraday strategies that MUST close before EOD
        EOD_CLOSE_STRATEGIES = {
            "GapBreakout",
            "ORB",
            "PreMarketMomentum",
            "OpeningBellSurge",
            "EarlySqueeze",
            "FloatRotation",
        }

        if strategy in EOD_CLOSE_STRATEGIES:
            return False

        # Standard position holds overnight
        return True

    def get_minutes_to_eod(self) -> int:
        """Minutes until market close (4:00 PM ET)."""
        et_now = datetime.datetime.now(self.tz)
        market_close = et_now.replace(hour=16, minute=0, second=0, microsecond=0)

        if et_now >= market_close:
            return 0

        delta = market_close - et_now
        return int(delta.total_seconds() / 60)

    def should_close_position_eod(
        self, strategy: str, entry_time: datetime.datetime, current_pnl_pct: float
    ) -> bool:
        """Determine if position should be force-closed by EOD_CLOSE_TIME."""
        EOD_CLOSE_STRATEGIES = {
            "GapBreakout",
            "ORB",
            "PreMarketMomentum",
            "OpeningBellSurge",
            "EarlySqueeze",
            "FloatRotation",
        }

        if strategy not in EOD_CLOSE_STRATEGIES:
            return False  # Hold overnight

        eod_close_time = datetime.time(15, 50)  # 3:50 PM
        et_now = datetime.datetime.now(self.tz)

        if et_now.time() >= eod_close_time:
            return True  # Force close within 10 min of market close

        return False  # Still time to hold

    def get_recommended_dca_timing(
        self, target_position_size: int, phase: Optional[MarketPhase] = None
    ) -> Tuple[int, float]:
        """Smart position sizing: single entry vs staged.
        Returns (entry_shares, dca_ratio).
          - dca_ratio 1.0: single entry
          - dca_ratio 0.5: 2 entries (50% initial, 50% retest)
          - dca_ratio 0.33: 3 entries
        """
        if phase is None:
            phase = self.get_market_phase()

        profile = self.get_phase_profile(phase)

        # In high-momentum windows: single aggressive entry
        if profile.momentum_level == "high":
            return target_position_size, 1.0

        # In medium phases: split 2 entries
        if profile.momentum_level == "medium":
            return int(target_position_size * 0.65), 0.65  # 65% now, 35% on retest

        # In low-activity phases: staged entry
        return int(target_position_size * 0.4), 0.4  # 40% now, 60% averaged in

    def log_phase_status(self):
        """Log current market phase and timing info."""
        phase = self.get_market_phase()
        profile = self.get_phase_profile(phase)
        mins_to_eod = self.get_minutes_to_eod()

        log.info(
            f"[TIMING] {profile.name} | "
            f"Liquidity: {profile.liquidity_level} | "
            f"Momentum: {profile.momentum_level} | "
            f"Entry ratio: {profile.recommended_entry_ratio:.0%} | "
            f"EOD in {mins_to_eod}m"
        )


# Singleton instance
_timing_engine = ExecutionTimingEngine()


def get_timing_engine() -> ExecutionTimingEngine:
    """Get the global timing engine."""
    return _timing_engine

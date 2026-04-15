"""
AdaptiveExecutionCoordinator
Brings together multi-regime detection, strategy feedback, timing awareness, 
and adaptive PDT/stop logic into unified execution flow.
"""

import logging
from typing import Dict, Optional, Tuple, List

from engine.market.regime import MarketRegime, get_regime_engine
from engine.analytics.strategy_metrics import get_strategy_tracker
from engine.execution.timing import get_timing_engine
import engine.config as cfg

log = logging.getLogger("ApexTrader.Adaptive")


class AdaptiveExecutionCoordinator:
    """Orchestrates all adaptive mechanisms for intelligent trading."""
    
    def __init__(self):
        self.regime_engine = get_regime_engine()
        self.strategy_tracker = get_strategy_tracker()
        self.timing_engine = get_timing_engine()
        self._current_regime: Optional[MarketRegime] = None
    
    def update_regime(self, force_refresh: bool = False) -> MarketRegime:
        """Detect and cache current market regime."""
        if not cfg.USE_MULTI_REGIME:
            # Fallback to neutral regime
            return MarketRegime(
                trend="range", trend_strength=0.5,
                volatility="normal", vol_level=20.0,
                breadth="mixed", breadth_pct=0.5,
                correlation="tight", corr_score=0.5,
                composite_confidence=0.3,
                timestamp=0,
            )
        
        self._current_regime = self.regime_engine.detect(force_refresh)
        return self._current_regime
    
    def get_regime(self) -> MarketRegime:
        """Get cached regime (updates if not set)."""
        if self._current_regime is None:
            self.update_regime()
        return self._current_regime
    
    def get_adaptive_config(self) -> Dict:
        """Get regime-specific strategy configuration."""
        regime = self.get_regime()
        
        # Look up in config
        key = (regime.trend, regime.volatility)
        config = cfg.STRATEGY_REGIME_CONFIG.get(key)
        
        if config is None:
            # Fallback to neutral
            config = cfg.STRATEGY_REGIME_CONFIG.get(
                ("range", "normal"),
                {
                    "active_strategies": ["TrendBreaker", "Momentum", "Sweepea"],
                    "confidence_min": cfg.MIN_SIGNAL_CONFIDENCE,
                    "rvol_min_override": None,
                    "max_positions_override": cfg.MAX_POSITIONS,
                    "position_size_mult": 1.0,
                    "description": "Default fallback",
                },
            )
        
        return config
    
    def get_adaptive_confidence_threshold(self) -> float:
        """Market-regime-aware minimum confidence."""
        if not cfg.USE_MULTI_REGIME:
            return cfg.MIN_SIGNAL_CONFIDENCE
        
        config = self.get_adaptive_config()
        return config.get("confidence_min", cfg.MIN_SIGNAL_CONFIDENCE)
    
    def get_adaptive_max_positions(self) -> int:
        """Market-regime-aware position limit."""
        if not cfg.USE_MULTI_REGIME:
            return cfg.MAX_POSITIONS
        
        config = self.get_adaptive_config()
        override = config.get("max_positions_override")
        return override if override is not None else cfg.MAX_POSITIONS
    
    def get_adaptive_position_size_mult(self) -> float:
        """Get total position sizing multiplier (regime + timing combined)."""
        if not cfg.USE_MULTI_REGIME:
            regime_mult = 1.0
        else:
            config = self.get_adaptive_config()
            regime_mult = config.get("position_size_mult", 1.0)
        
        if cfg.USE_TIMING_AWARE_ENTRY:
            timing_mult = self.timing_engine.get_position_size_multiplier()
        else:
            timing_mult = 1.0
        
        return regime_mult * timing_mult
    
    def get_adaptive_strategy_list(self) -> List[str]:
        """Get active strategy list for current regime."""
        if not cfg.USE_MULTI_REGIME:
            # All strategies active by default
            return [
                "TrendBreaker", "Momentum", "Sweepea", "FloatRotation",
                "GapBreakout", "ORB", "VWAP Reclaim",
            ]
        
        config = self.get_adaptive_config()
        return config.get("active_strategies", [])
    
    def get_adaptive_stop_multiplier(self) -> float:
        """Get stop loss tightening multiplier based on volatility."""
        if not cfg.USE_REGIME_STOPS:
            return 1.0
        
        regime = self.get_regime()
        
        if regime.volatility == "extreme":
            return cfg.STOP_LOSS_MULTIPLIER_EXTREME
        elif regime.volatility == "high":
            return cfg.STOP_LOSS_MULTIPLIER_HIGH
        elif regime.volatility == "calm":
            return cfg.STOP_LOSS_MULTIPLIER_CALM
        else:
            return cfg.STOP_LOSS_MULTIPLIER_NORMAL
    
    def get_adaptive_pdt_reserve(self) -> int:
        """Dynamically adjust PDT day trade reserve based on volatility."""
        if not cfg.USE_ADAPTIVE_PDT_RESERVE:
            return 2  # Default
        
        regime = self.get_regime()
        
        if regime.volatility == "extreme":
            return 3  # Tight control during crashes
        elif regime.volatility == "high":
            return 2  # Normal reserve
        elif regime.volatility in ("normal", "calm"):
            # During normal/calm vol: use feedback to decide
            if regime.trend == "uptrend" and regime.composite_confidence > 0.8:
                return 1  # Aggressive in confirmed uptrends
            else:
                return 2  # Default
        else:
            return 2
    
    def get_strategy_allocation_weights(self) -> Dict[str, float]:
        """Get scan budget allocation based on strategy performance + regime."""
        if not cfg.USE_STRATEGY_FEEDBACK:
            # Even distribution
            strategies = self.get_adaptive_strategy_list()
            return {s: 1.0 / len(strategies) for s in strategies}
        
        strategies = self.get_adaptive_strategy_list()
        regime = self.get_regime()
        
        # Get performance-based weights
        weights = self.strategy_tracker.recommend_scan_allocation(
            strategies,
            regime_constraint=regime.trend,  # "uptrend", "range", "downtrend"
        )
        
        # Filter to active strategies only
        return {s: w for s, w in weights.items() if s in strategies}
    
    def log_regime_status(self):
        """Log current regime and adaptive settings."""
        regime = self.get_regime()
        config = self.get_adaptive_config()
        pos_mult = self.get_adaptive_position_size_mult()
        stop_mult = self.get_adaptive_stop_multiplier()
        pdt_reserve = self.get_adaptive_pdt_reserve()
        
        log.info(
            f"[REGIME] {regime.trend.upper()} {regime.trend_strength:.0%} | "
            f"Vol: {regime.volatility.upper()} ({regime.vol_level:.0f}) | "
            f"Breadth: {regime.breadth.upper()} ({regime.breadth_pct:.0%}) | "
            f"Corr: {regime.correlation.upper()} ({regime.corr_score:.0%}) | "
            f"Confidence: {regime.composite_confidence:.0%}"
        )
        
        log.info(
            f"[ADAPTIVE] Strategies: {','.join(config.get('active_strategies', []))} | "
            f"ConfMin: {self.get_adaptive_confidence_threshold():.0%} | "
            f"PosSize: {pos_mult:.0%} | "
            f"StopMult: {stop_mult:.0%} | "
            f"PDT Reserve: {pdt_reserve}DT | "
            f"{config.get('description', '')}"
        )


# Singleton
_coordinator = AdaptiveExecutionCoordinator()


def get_adaptive_coordinator() -> AdaptiveExecutionCoordinator:
    """Get global adaptive execution coordinator."""
    return _coordinator

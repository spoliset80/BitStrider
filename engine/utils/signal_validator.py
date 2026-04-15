"""
Signal Validator - Multi-Stage Signal Filtering
Applies regime-aware confidence gating, prevents low-quality signals, 
and matches signals to market conditions.

Validation Stages:
  1. Confidence gate (regime-aware)
  2. Risk/reward ratio check
  3. Regime correlation (is this signal type good NOW?)
  4. Volatility filter (avoid extremes)
  5. Liquidity filter (can we actually execute?)
"""

import logging
from typing import Optional, List
from dataclasses import dataclass

from engine.config import (
    TIER_A_CONFIDENCE_MIN,
    TIER_B_CONFIDENCE_MIN,
    TIER_C_CONFIDENCE_MIN,
)

log = logging.getLogger("ApexTrader")


@dataclass
class SignalValidation:
    """Result of signal validation."""
    symbol: str
    valid: bool
    confidence: float
    reasons: List[str]  # Why blocked, if blocked
    tier: str  # Tier assignment
    adjusted_confidence: float  # After regime adjustment


class SignalValidator:
    """
    Multi-stage signal filtering for regime-aware execution.
    
    Usage:
        validator = SignalValidator()
        result = validator.validate_signal(
            symbol, confidence, regime, volatility_pct, 
            liquidity_level, risk_reward_ratio
        )
        if result.valid:
            executor.execute_signal(...)
    """

    def __init__(self):
        self.blocked_reasons = {}  # {symbol: [reasons]} for logging

    def validate_signal(
        self,
        symbol: str,
        confidence: float,
        regime: str = "bull",
        volatility_pct: float = 5.0,
        liquidity_level: str = "high",
        risk_reward_ratio: float = 2.0,
        entry_price: float = 0.0,
        tier: str = "A",
    ) -> SignalValidation:
        """
        Validate a signal through multi-stage filtering.
        
        Args:
            symbol: Ticker
            confidence: Original signal confidence (0-100)
            regime: Current market regime ("bull", "bear", "range")
            volatility_pct: Current ATR/close volatility %
            liquidity_level: "high", "medium", "low"
            risk_reward_ratio: R/R ratio of trade
            tier: Universe tier (A/B/C)
            
        Returns:
            SignalValidation with validity and adjustment reasons
        """
        reasons = []
        adjusted_confidence = confidence

        # ─── STAGE 1: Confidence Gate (Regime-Aware) ──────────────────────
        tier_min_conf = {
            "A": TIER_A_CONFIDENCE_MIN,
            "B": TIER_B_CONFIDENCE_MIN,
            "C": TIER_C_CONFIDENCE_MIN,
        }.get(tier, 0.70)

        # Relax gates in bull, tighten in bear
        if regime == "bull":
            effective_min_conf = tier_min_conf * 0.95  # 5% relaxation
        elif regime == "bear":
            effective_min_conf = tier_min_conf * 1.05  # 5% tightening
        else:  # range
            effective_min_conf = tier_min_conf  # No adjustment

        if confidence < effective_min_conf * 100:
            reasons.append(f"Confidence {confidence:.0f}% below gate {effective_min_conf*100:.0f}% ({regime} regime)")
            adjusted_confidence *= 0.8  # Penalize

        # ─── STAGE 2: Risk/Reward Filter ──────────────────────────────────
        if risk_reward_ratio < 1.5:
            reasons.append(f"Poor R/R ratio {risk_reward_ratio:.2f} (min 1.5)")
            adjusted_confidence *= 0.7

        # ─── STAGE 3: Regime Correlation ──────────────────────────────────
        # This maps signal type to regime performance
        # In bear market, avoid "momentum" signals (they fade)
        # In bull market, "momentum" signals are great
        
        if regime == "bear" and entry_price > 0:
            # Penalize high-momentum signals in bear (less reliable)
            if confidence > 80:
                reasons.append(f"High-confidence signal in bear regime (less reliable)")
                adjusted_confidence *= 0.85
        
        if regime == "range":
            # In range, mean-reversion signals are better than breakouts
            # This would need signal type metadata; skipping for now
            pass

        # ─── STAGE 4: Volatility Filter ───────────────────────────────────
        if volatility_pct > 15.0:
            reasons.append(f"High volatility {volatility_pct:.1f}% (hard to manage)")
            adjusted_confidence *= 0.8
        
        if volatility_pct < 1.5:
            # Too flat? Could be before a big move (not necessarily bad)
            # Don't penalize, but track
            log.debug(f"{symbol}: Low volatility ({volatility_pct:.1f}%), watch for breakout")

        # ─── STAGE 5: Liquidity Filter ────────────────────────────────────
        if liquidity_level == "low":
            reasons.append(f"Low liquidity (wide spreads, slow fills)")
            adjusted_confidence *= 0.6  # Heavy penalty
        elif liquidity_level == "medium":
            adjusted_confidence *= 0.9  # Minor penalty

        # ─── FINAL DECISION ───────────────────────────────────────────────
        # After all adjustments, check if still above gate
        final_gate = effective_min_conf * 100
        valid = adjusted_confidence >= final_gate
        
        if not valid:
            if symbol not in self.blocked_reasons:
                self.blocked_reasons[symbol] = reasons
            log.debug(f"[SIGNAL] {symbol} BLOCKED: {', '.join(reasons)}")
        else:
            self.blocked_reasons.pop(symbol, None)

        return SignalValidation(
            symbol=symbol,
            valid=valid,
            confidence=confidence,
            reasons=reasons,
            tier=tier,
            adjusted_confidence=adjusted_confidence,
        )

    def filter_signals(
        self,
        signals: List[tuple],  # [(symbol, confidence, regime, vol%, liquidity, r/r, price)]
        regime: str = "bull",
    ) -> List[tuple]:
        """
        Batch validate multiple signals.
        
        Returns:
            List of (symbol, adjusted_confidence) for valid signals only
        """
        valid_signals = []
        
        for signal_tuple in signals:
            if len(signal_tuple) < 6:
                continue
            
            symbol, confidence, vol_pct, liquidity, risk_reward, entry_price = signal_tuple[:6]
            tier = signal_tuple[6] if len(signal_tuple) > 6 else "A"
            
            result = self.validate_signal(
                symbol=symbol,
                confidence=confidence,
                regime=regime,
                volatility_pct=vol_pct,
                liquidity_level=liquidity,
                risk_reward_ratio=risk_reward,
                entry_price=entry_price,
                tier=tier,
            )
            
            if result.valid:
                valid_signals.append((symbol, result.adjusted_confidence))
        
        return sorted(valid_signals, key=lambda x: x[1], reverse=True)

    def log_filtering_summary(self) -> None:
        """Log summary of filtering decisions."""
        if not self.blocked_reasons:
            return
        
        log.info(f"[SIGNAL] {len(self.blocked_reasons)} signals blocked:")
        for symbol, reasons in sorted(self.blocked_reasons.items())[:10]:  # Top 10
            log.info(f"  {symbol}: {reasons[0]}")


# Singleton
_validator: Optional[SignalValidator] = None


def get_signal_validator() -> SignalValidator:
    global _validator
    if _validator is None:
        _validator = SignalValidator()
    return _validator


def reset_signal_validator() -> None:
    global _validator
    _validator = None

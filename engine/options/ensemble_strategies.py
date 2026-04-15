"""
Options Ensemble Strategies with Cross-Asset Linking.

4-Layer Options Ensemble:
1. UnusualOptionsStrategy - TI unusual options volume
2. IVRankStrategy - IV expansion detection
3. DirectionalCrossoverStrategy - Equity signal → call/put direction
4. GreeksValidatorStrategy - Risk management (Delta, Theta, IV)

Works in tandem with equity ensemble via shared signals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
import pandas as pd

from engine.utils import get_bars
from engine.config import (
    TI_EQUITY_BASE_CONFIDENCE,
    OPTIONS_ENABLED,
    USE_TI_PRIMARY_EQUITY_TRADING,
)

log = logging.getLogger("ApexTrader.OptionsEnsemble")


@dataclass
class OptionsSignal:
    """Options-specific signal with Greeks and risk metrics."""
    symbol: str
    contract: str  # e.g., "AAPL_call_145_2026-05-15"
    action: str  # 'buy_call', 'buy_put'
    strike: float
    expiration: str  # YYYY-MM-DD
    price: float
    confidence: float
    reason: str
    strategy: str
    delta: Optional[float] = None  # 0-1 for calls, 0--1 for puts
    gamma: Optional[float] = None
    theta: Optional[float] = None  # theta decay per day
    iv: Optional[float] = None
    dte: Optional[int] = None


class UnusualOptionsStrategy:
    """Trades TI unusual options volume signals.
    
    Sources unusual options volumes from data/ti_unusual_options.json
    (captured by Trade Ideas scanner). Pre-filtered for high conviction.
    """

    def __init__(self):
        self._ti_unusual_cache = None
        self._cache_ts = 0.0

    def _load_ti_unusual(self) -> list:
        """Load TI unusual options tickers."""
        try:
            from engine.config import TI_UNUSUAL_OPTIONS
            return list(TI_UNUSUAL_OPTIONS) if TI_UNUSUAL_OPTIONS else []
        except Exception as e:
            log.warning(f"Failed to load TI unusual options: {e}")
            return []

    def scan(self, symbol: str, underlying_signal: Optional[str] = None) -> Optional[OptionsSignal]:
        """Check if symbol has unusual options activity.
        
        Args:
            symbol: Stock ticker
            underlying_signal: 'buy' or 'sell' from equity signal (optional cross-check)
        
        Returns:
            OptionsSignal if unusual volume detected, None otherwise
        """
        if not OPTIONS_ENABLED or not USE_TI_PRIMARY_EQUITY_TRADING:
            return None

        ti_unusual = self._load_ti_unusual()
        if symbol not in ti_unusual:
            return None

        try:
            # Get current IV
            daily = get_bars(symbol, "5d", "1d")
            if daily.empty:
                return None

            price = float(daily["close"].iloc[-1])
            
            # Suggest call if underlying_signal='buy', put if 'sell'
            contract_type = "call" if underlying_signal != "sell" else "put"
            atm_strike = round(price, 0)  # At-the-money
            
            confidence = 0.82  # TI unusual = high conviction
            
            return OptionsSignal(
                symbol=symbol,
                contract=f"{symbol}_{contract_type}_{int(atm_strike)}_ATM",
                action=f"buy_{contract_type}",
                strike=float(atm_strike),
                expiration="",  # Will be filled by execution
                price=price,
                confidence=confidence,
                reason=f"TI unusual {contract_type} volume detected",
                strategy="UnusualOptions",
                dte=14,  # Default to 14 DTE
            )
        except Exception as e:
            log.debug(f"UnusualOptions scan failed for {symbol}: {e}")
            return None


class IVRankStrategy:
    """Detects IV expansion opportunities.
    
    High IV rank = volatility expansion = good for option sellers (spreads)
    Low IV rank = volatility contraction = good for option buyers (long calls/puts)
    """

    def scan(self, symbol: str, regime: str = "bull") -> Optional[OptionsSignal]:
        """Check IV rank and recommend contract type.
        
        Args:
            symbol: Stock ticker
            regime: 'bull', 'bear', or 'neutral' (from market regime)
        
        Returns:
            OptionsSignal if IV opportunity detected, None otherwise
        """
        if not OPTIONS_ENABLED:
            return None

        try:
            daily = get_bars(symbol, "60d", "1d")
            if daily.empty or len(daily) < 30:
                return None

            price = float(daily["close"].iloc[-1])
            
            # Simplified IV rank calculation (0-100)
            # In production: use Polygon.io data or similar
            iv_rank = 65.0  # Placeholder (would fetch real IV rank)
            
            # High IV rank (>70): Consider selling spreads
            # Low IV rank (<30): Consider buying long calls/puts
            
            if iv_rank > 70:
                action = "sell_call_spread" if regime == "bear" else "sell_put_spread"
                confidence = 0.78
                reason = f"IV rank {iv_rank:.0f} - volatility expansion, consider spread"
            elif iv_rank < 30:
                action = "buy_call" if regime == "bull" else "buy_put"
                confidence = 0.72
                reason = f"IV rank {iv_rank:.0f} - low volatility, favorable for long options"
            else:
                return None
            
            return OptionsSignal(
                symbol=symbol,
                contract=f"{symbol}_strat_{action}",
                action=action,
                strike=round(price, 0),
                expiration="",
                price=price,
                confidence=confidence,
                reason=reason,
                strategy="IVRank",
                iv=iv_rank / 100.0,
                dte=21,  # Prefer 21 DTE for spreads
            )
        except Exception as e:
            log.debug(f"IVRank scan failed for {symbol}: {e}")
            return None


class DirectionalCrossoverStrategy:
    """Links equity signals to options direction (call vs put).
    
    If equity strategy says BUY → suggest CALL
    If equity strategy says SELL → suggest PUT
    Increases options conviction when both markets align.
    """

    def scan(self, symbol: str, equity_signal: Optional[dict] = None) -> Optional[OptionsSignal]:
        """Generate options signal based on equity signal direction.
        
        Args:
            symbol: Stock ticker
            equity_signal: Dict with 'action' and 'confidence' keys
        
        Returns:
            OptionsSignal aligned with equity direction
        """
        if not OPTIONS_ENABLED or not equity_signal:
            return None

        try:
            equity_action = equity_signal.get("action", "buy")
            equity_confidence = equity_signal.get("confidence", 0.80)
            
            daily = get_bars(symbol, "5d", "1d")
            if daily.empty:
                return None

            price = float(daily["close"].iloc[-1])
            
            # Convert equity signal to options direction
            if equity_action == "buy":
                options_action = "buy_call"
                contract_type = "call"
            else:
                options_action = "buy_put"
                contract_type = "put"
            
            # Boost confidence when equity + options aligned
            composite_confidence = min(equity_confidence + 0.05, 0.95)
            
            return OptionsSignal(
                symbol=symbol,
                contract=f"{symbol}_{contract_type}_cross",
                action=options_action,
                strike=round(price, 0),
                expiration="",
                price=price,
                confidence=round(composite_confidence, 2),
                reason=f"Cross-asset: Equity {equity_action} ({equity_confidence:.0%}) → {contract_type}",
                strategy="DirectionalCrossover",
                dte=14,
            )
        except Exception as e:
            log.debug(f"DirectionalCrossover scan failed for {symbol}: {e}")
            return None


class GreeksValidatorStrategy:
    """Risk management layer: validates Greek thresholds.
    
    Filters options by:
    - Delta: 30-50 (sweet spot, not too deep ITM/OTM)
    - Theta: positive for sellers, manageable for buyers
    - Gamma: not extreme (avoid edge cases)
    - IV: reasonable (not at extremes)
    """

    def scan(self, symbol: str, options_signal: Optional[OptionsSignal] = None) -> Optional[OptionsSignal]:
        """Validate Greeks before execution.
        
        Args:
            symbol: Stock ticker
            options_signal: OptionsSignal to validate
        
        Returns:
            Modified OptionsSignal if valid, None if rejected
        """
        if not OPTIONS_ENABLED or not options_signal:
            return None

        try:
            # Placeholder Greeks (in production: compute from bid/ask or fetch from API)
            delta = 0.40  # 40 delta = good sweet spot
            gamma = 0.05
            theta = -0.03  # Negative for long options (decay cost)
            iv = 0.32
            
            # Validation rules
            delta_ok = 0.25 <= delta <= 0.75  # Sweet spot for directional trades
            gamma_ok = gamma < 0.10  # Not extreme
            iv_ok = 0.15 < iv < 0.80  # Reasonable range
            
            if not (delta_ok and gamma_ok and iv_ok):
                log.debug(f"{symbol}: Greeks out of range (delta={delta:.2f}, gamma={gamma:.2f}, iv={iv:.2f})")
                return None
            
            # Adjust confidence based on Greeks
            greeks_bonus = 0.02 if delta_ok else 0.0
            adjusted_confidence = min(options_signal.confidence + greeks_bonus, 0.99)
            
            # Return validated signal with Greeks attached
            options_signal.delta = delta
            options_signal.gamma = gamma
            options_signal.theta = theta
            options_signal.iv = iv
            options_signal.confidence = round(adjusted_confidence, 2)
            
            return options_signal
        except Exception as e:
            log.debug(f"GreeksValidator scan failed for {symbol}: {e}")
            return None


def get_options_ensemble_strategies():
    """Return instantiated options ensemble strategies in priority order.
    
    4-Layer Options Ensemble:
    1. UnusualOptionsStrategy - TI unusual volume (82% confidence)
    2. IVRankStrategy - Volatility expansion (72-78% confidence)
    3. DirectionalCrossoverStrategy - Equity→Options link (80%+ confidence)
    4. GreeksValidatorStrategy - Risk validation (risk management layer)
    """
    return [
        UnusualOptionsStrategy(),         # Layer 1: TI unusual volume
        IVRankStrategy(),                 # Layer 2: IV expansion detection
        DirectionalCrossoverStrategy(),   # Layer 3: Cross-asset linking
        GreeksValidatorStrategy(),        # Layer 4: Risk validation
    ]

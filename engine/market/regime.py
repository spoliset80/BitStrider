"""
Multi-Dimensional Market Regime Detection Engine
Combines trend + volatility + breadth + correlation for adaptive trading.

Dimensions:
  - Trend: uptrend/range/downtrend via MA slopes (SPY, QQQ, IWM)
  - Volatility: extreme/high/normal/calm via VIX + realized ATR
  - Breadth: leading/mixed/lagging based on % above 50-MA
  - Correlation: tight/disperse based on sector decoupling
"""

import datetime
import logging
import time
from dataclasses import dataclass
from typing import Optional, Tuple
import numpy as np

from engine.utils import get_bars, get_vix
from engine.config import ADAPTIVE_INTERVALS

log = logging.getLogger("ApexTrader.Regime")


@dataclass
class MarketRegime:
    """Multi-axis market regime with composite confidence."""
    trend: str           # "uptrend", "range", "downtrend"
    trend_strength: float # 0.0-1.0
    volatility: str      # "extreme", "high", "normal", "calm"
    vol_level: float     # 0.0-100.0 (VIX percentile)
    breadth: str         # "leading", "mixed", "lagging"
    breadth_pct: float   # 0.0-1.0 (% above 50-MA)
    correlation: str     # "tight", "disperse"
    corr_score: float    # 0.0-1.0
    composite_confidence: float  # 0.0-1.0 (ensemble)
    timestamp: float
    
    def is_bull_regime(self) -> bool:
        return self.trend == "uptrend"
    
    def is_bear_regime(self) -> bool:
        return self.trend == "downtrend"
    
    def is_high_vol(self) -> bool:
        return self.volatility in ("extreme", "high")
    
    def is_calm(self) -> bool:
        return self.volatility == "calm"


class RegimeEngine:
    """Detects market regime across multiple axes."""
    
    def __init__(self):
        self._cache: Optional[MarketRegime] = None
        self._cache_ts = 0.0
        self._cache_ttl = 900  # 15 minutes
        self._universe_for_breadth = ["SPY"] * 500  # Placeholder for S&P 500 tickers
        
    def detect(self, force_refresh: bool = False) -> MarketRegime:
        """Multi-axis regime detection with caching."""
        now = time.time()
        if not force_refresh and self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache
        
        try:
            # Axis 1: Trend
            trend, trend_strength = self._detect_trend()
            
            # Axis 2: Volatility
            vol_regime, vol_level = self._classify_volatility()
            
            # Axis 3: Breadth
            breadth, breadth_pct = self._calculate_breadth()
            
            # Axis 4: Correlation
            corr_regime, corr_score = self._measure_correlation()
            
            # Composite confidence: weighted ensemble
            conf = (trend_strength * 0.4 + 
                   (1.0 - vol_level / 100.0) * 0.25 + 
                   breadth_pct * 0.2 + 
                   corr_score * 0.15)
            
            regime = MarketRegime(
                trend=trend,
                trend_strength=trend_strength,
                volatility=vol_regime,
                vol_level=vol_level,
                breadth=breadth,
                breadth_pct=breadth_pct,
                correlation=corr_regime,
                corr_score=corr_score,
                composite_confidence=min(1.0, conf),
                timestamp=now,
            )
            
            self._cache = regime
            self._cache_ts = now
            return regime
            
        except Exception as e:
            log.error(f"Regime detection failed: {e}")
            # Return default neutral regime on error
            return MarketRegime(
                trend="range", trend_strength=0.5,
                volatility="normal", vol_level=20.0,
                breadth="mixed", breadth_pct=0.5,
                correlation="tight", corr_score=0.5,
                composite_confidence=0.3,
                timestamp=now,
            )
    
    def _detect_trend(self) -> Tuple[str, float]:
        """Trend via QQQ/IWM relative to SPY MAs."""
        try:
            spy = get_bars("SPY", "250d", "1d")
            qqq = get_bars("QQQ", "250d", "1d")
            
            if spy.empty or qqq.empty or len(spy) < 200:
                return "range", 0.5
            
            # 50/200 slopes
            spy_close = spy["close"]
            spy_50ma = spy_close.rolling(50).mean()
            spy_200ma = spy_close.rolling(200).mean()
            spy_slope = (spy_50ma.iloc[-1] - spy_200ma.iloc[-1]) / spy_200ma.iloc[-1]
            
            qqq_close = qqq["close"]
            qqq_50ma = qqq_close.rolling(50).mean()
            qqq_200ma = qqq_close.rolling(200).mean()
            qqq_slope = (qqq_50ma.iloc[-1] - qqq_200ma.iloc[-1]) / qqq_200ma.iloc[-1]
            
            max_slope = max(spy_slope, qqq_slope)
            min_slope = min(spy_slope, qqq_slope)
            
            if max_slope > 0.015:
                # Both positive and strong uptrend
                strength = min(1.0, max_slope / 0.03)
                return "uptrend", strength
            elif min_slope < -0.015:
                # Both negative or strong downtrend
                strength = min(1.0, abs(min_slope) / 0.03)
                return "downtrend", strength
            else:
                # Range-bound
                return "range", 0.5
                
        except Exception as e:
            log.debug(f"Trend detection error: {e}")
            return "range", 0.5
    
    def _classify_volatility(self) -> Tuple[str, float]:
        """VIX + realized ATR."""
        try:
            vix = get_vix()
            spy_bars = get_bars("SPY", "100d", "1d")
            
            if spy_bars.empty or len(spy_bars) < 20:
                return "normal", 20.0
            
            # Realized volatility (SPY ATR %)
            hi = spy_bars["high"]
            lo = spy_bars["low"]
            pc = spy_bars["close"].shift(1)
            tr = pd.concat([(hi - lo), (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
            atr20 = tr.rolling(20).mean().iloc[-1]
            spy_price = spy_bars["close"].iloc[-1]
            realized_vol_pct = (atr20 / spy_price) * 100
            
            # Composite vol: average VIX and realized
            composite = (vix + realized_vol_pct * 0.5) / 2.0
            
            if composite > 40 or vix > 40:
                return "extreme", min(100.0, max(vix, realized_vol_pct))
            elif composite > 25 or vix > 25:
                return "high", composite
            elif composite > 15 or vix > 15:
                return "normal", composite
            else:
                return "calm", composite
                
        except Exception as e:
            log.debug(f"Volatility detection error: {e}")
            return "normal", 20.0
    
    def _calculate_breadth(self) -> Tuple[str, float]:
        """% of stocks above 50-MA (simplified: check top SPY holdings)."""
        try:
            # Use representative tickers instead of full universe
            sample_tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "BRK.B"]
            above_50ma = 0
            
            for sym in sample_tickers:
                try:
                    bars = get_bars(sym, "100d", "1d")
                    if not bars.empty and len(bars) >= 50:
                        close = bars["close"].iloc[-1]
                        ma50 = bars["close"].rolling(50).mean().iloc[-1]
                        if close > ma50:
                            above_50ma += 1
                except:
                    pass
            
            pct = above_50ma / len(sample_tickers)
            
            if pct > 0.65:
                return "leading", pct
            elif pct < 0.35:
                return "lagging", pct
            else:
                return "mixed", pct
                
        except Exception as e:
            log.debug(f"Breadth detection error: {e}")
            return "mixed", 0.5
    
    def _measure_correlation(self) -> Tuple[str, float]:
        """Tech/Finance/Energy correlation divergence."""
        try:
            # Fetch sector ETFs
            tech = get_bars("XLK", "60d", "1d")["close"]
            fin = get_bars("XLF", "60d", "1d")["close"]
            energy = get_bars("XLE", "60d", "1d")["close"]
            
            if tech.empty or fin.empty or energy.empty:
                return "tight", 0.5
            
            # Returns correlation
            tech_returns = tech.pct_change().dropna()
            fin_returns = fin.pct_change().dropna()
            energy_returns = energy.pct_change().dropna()
            
            if len(tech_returns) < 10:
                return "tight", 0.5
            
            corr_tf = tech_returns.corr(fin_returns)
            corr_te = tech_returns.corr(energy_returns)
            corr_fe = fin_returns.corr(energy_returns)
            
            avg_corr = (corr_tf + corr_te + corr_fe) / 3.0
            
            # High correlation = tight, low correlation = disperse
            if avg_corr > 0.65:
                return "tight", min(1.0, avg_corr)
            else:
                return "disperse", 1.0 - max(0, avg_corr)
                
        except Exception as e:
            log.debug(f"Correlation detection error: {e}")
            return "tight", 0.5


# Singleton
_regime_engine = RegimeEngine()


def get_regime_engine() -> RegimeEngine:
    """Get global regime engine."""
    return _regime_engine


def detect_market_regime(force_refresh: bool = False) -> MarketRegime:
    """Convenience function to detect current market regime."""
    return _regime_engine.detect(force_refresh)


import pandas as pd  # Add this import at top

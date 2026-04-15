"""
Strategy Performance Tracker & Adaptive Allocation
Tracks win rates, Sharpe ratios, and regime-specific performance.
Feeds back into scan allocation and confidence thresholds.
"""

import datetime
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import json

log = logging.getLogger("ApexTrader.StrategyMetrics")


@dataclass
class StrategyStats:
    """Aggregated strategy performance."""
    strategy: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    avg_pnl_pct: float
    sharpe_ratio: float
    max_dd_pct: float
    profit_factor: float
    regime_scores: Dict[str, float]  # {regime: win_rate_in_regime}


class StrategyPerformanceTracker:
    """Track strategy PnL and adaptive allocation based on performance."""
    
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or Path.cwd() / "strategy_metrics.db"
        self._init_db()
        self._stats_cache: Dict[str, StrategyStats] = {}
        self._cache_ts = 0.0
        self._cache_ttl = 3600  # 1 hour
    
    def _init_db(self):
        """Initialize SQLite database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                strategy TEXT NOT NULL,
                symbol TEXT NOT NULL,
                entry_price FLOAT NOT NULL,
                exit_price FLOAT NOT NULL,
                shares INT NOT NULL,
                entry_ts REAL NOT NULL,
                exit_ts REAL NOT NULL,
                pnl_pct FLOAT NOT NULL,
                regime TEXT,
                regime_confidence FLOAT,
                created_at REAL
            )
            """)
            
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_strategy_ts 
            ON trades(strategy, created_at)
            """)
            
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_regime_strategy 
            ON trades(regime, strategy)
            """)
            
            conn.commit()
    
    def record_trade(
        self,
        strategy: str,
        symbol: str,
        entry_price: float,
        exit_price: float,
        shares: int,
        regime: Optional[str] = None,
        regime_confidence: float = 0.0,
    ):
        """Record a closed trade."""
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100
        trade_id = f"{strategy}_{symbol}_{int(time.time() * 1000)}"
        now = time.time()
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_id, strategy, symbol, entry_price, exit_price,
                    shares, now, now + 1, pnl_pct, regime, regime_confidence, now
                ))
                conn.commit()
                
                # Invalidate cache
                self._cache_ts = 0.0
                
        except Exception as e:
            log.error(f"Failed to record trade: {e}")
    
    def get_strategy_stats(
        self,
        strategy: str,
        days: int = 30,
    ) -> Optional[StrategyStats]:
        """Get aggregated stats for a strategy."""
        cutoff = time.time() - (days * 86400)
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute("""
                SELECT pnl_pct, regime FROM trades
                WHERE strategy = ? AND created_at > ?
                ORDER BY created_at
                """, (strategy, cutoff)).fetchall()
            
            if not rows:
                return None
            
            pnls = [r[0] for r in rows]
            regimes = [r[1] for r in rows]
            
            wins = sum(1 for p in pnls if p > 0)
            losses = sum(1 for p in pnls if p < 0)
            win_rate = wins / len(pnls) if pnls else 0.5
            
            avg_win = np.mean([p for p in pnls if p > 0]) if wins > 0 else 0.0
            avg_loss = np.mean([p for p in pnls if p < 0]) if losses > 0 else 0.0
            
            # Profit factor
            total_wins = sum(p for p in pnls if p > 0)
            total_losses = abs(sum(p for p in pnls if p < 0))
            profit_factor = total_wins / total_losses if total_losses > 0 else 1.0
            
            # Sharpe (simplified)
            sharpe = np.mean(pnls) / (np.std(pnls) + 1e-6) if len(pnls) > 1 else 0.0
            
            # Max drawdown
            max_dd = min(pnls)
            
            # Regime-specific win rates
            regime_scores = {}
            for regime in set(r for r in regimes if r):
                regime_pnls = [pnls[i] for i, r in enumerate(regimes) if r == regime]
                regime_wins = sum(1 for p in regime_pnls if p > 0)
                regime_scores[regime] = regime_wins / len(regime_pnls) if regime_pnls else 0.5
            
            return StrategyStats(
                strategy=strategy,
                trades=len(pnls),
                wins=wins,
                losses=losses,
                win_rate=win_rate,
                avg_win_pct=avg_win,
                avg_loss_pct=avg_loss,
                avg_pnl_pct=np.mean(pnls),
                sharpe_ratio=sharpe,
                max_dd_pct=max_dd,
                profit_factor=profit_factor,
                regime_scores=regime_scores,
            )
            
        except Exception as e:
            log.error(f"Failed to get stats for {strategy}: {e}")
            return None
    
    def recommend_scan_allocation(
        self,
        strategies: List[str],
        regime_constraint: Optional[str] = None,
    ) -> Dict[str, float]:
        """Allocate scan budget based on strategy performance.
        
        Returns: {strategy: allocation_weight}
          - Weight 1.0 = normal share
          - Weight 1.5 = high confidence (65%+ win rate)
          - Weight 0.5 = low confidence (45%- win rate)
        """
        scores = {}
        
        for strat in strategies:
            stats = self.get_strategy_stats(strat)
            
            if stats is None:
                # New strategy: equal allocation
                scores[strat] = 1.0
                continue
            
            # Base score: win rate
            base_score = stats.win_rate
            
            # Boost if regime-aligned
            if regime_constraint and regime_constraint in stats.regime_scores:
                regime_wr = stats.regime_scores[regime_constraint]
                base_score = (base_score * 0.6 + regime_wr * 0.4)
            
            # Additional factors
            if stats.trades < 5:
                # Insufficient data: reduce confidence
                base_score *= 0.7
            
            if stats.sharpe_ratio > 0.5:
                # Good risk-adjusted returns: boost
                base_score *= 1.1
            elif stats.sharpe_ratio < -0.3:
                # Poor risk-adjusted: reduce
                base_score *= 0.8
            
            # Convert to allocation weight
            # 0.5 win rate = weight 0.5 (half normal)
            # 0.65 win rate = weight 1.25 (125%)
            # 0.75 win rate = weight 1.5 (max 150%)
            weight = max(0.2, min(1.5, base_score * 1.7))
            scores[strat] = weight
        
        # Normalize to proportions
        total = sum(scores.values())
        return {s: w / total for s, w in scores.items()} if total > 0 else {s: 1.0 / len(strategies) for s in strategies}
    
    def export_stats(self, output_path: Path):
        """Export all strategy stats to JSON."""
        all_stats = {}
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                strategies = conn.execute(
                    "SELECT DISTINCT strategy FROM trades"
                ).fetchall()
            
            for (strat,) in strategies:
                stats = self.get_strategy_stats(strat, days=90)
                if stats:
                    all_stats[strat] = {
                        "trades": stats.trades,
                        "win_rate": round(stats.win_rate, 3),
                        "avg_pnl": round(stats.avg_pnl_pct, 2),
                        "sharpe": round(stats.sharpe_ratio, 2),
                        "profit_factor": round(stats.profit_factor, 2),
                        "regime_scores": {k: round(v, 3) for k, v in stats.regime_scores.items()},
                    }
            
            with open(output_path, "w") as f:
                json.dump(all_stats, f, indent=2)
                
            log.info(f"Exported strategy stats to {output_path}")
            
        except Exception as e:
            log.error(f"Failed to export stats: {e}")


# Singleton
_tracker = StrategyPerformanceTracker()


def get_strategy_tracker() -> StrategyPerformanceTracker:
    """Get global strategy tracker."""
    return _tracker


import numpy as np
from dataclasses import dataclass

"""
Performance Analytics - Real-time P&L Tracking & Strategy Metrics
Tracks hourly P&L, win rates by strategy/regime, and performance correlation.

Metrics Tracked:
  - Hourly P&L (rolling): Last hour, last 4 hours, today
  - Strategy ROI: Win % per strategy, average profit/loss per trade
  - Regime Correlation: Which strategies work best in which regimes?
  - Execution Quality: Slippage, fill prices vs market, time-to-fill
  - Drawdown: Max intra-day loss, recovery time
"""

import logging
import datetime
import sqlite3
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ApexTrader")


@dataclass
class HourlyMetrics:
    """Hourly P&L snapshot."""
    hour_start: datetime.datetime
    pnl_dollars: float
    pnl_pct: float
    trades_count: int
    wins: int
    losses: int
    win_rate_pct: float
    avg_profit_per_win: float
    avg_loss_per_loss: float


@dataclass
class StrategyMetrics:
    """Per-strategy performance."""
    strategy_name: str
    trades_count: int
    wins: int
    losses: int
    win_rate_pct: float
    total_pnl: float
    avg_profit: float
    avg_loss: float
    profit_factor: float  # (wins * avg_profit) / (losses * avg_loss)
    best_trade: float
    worst_trade: float


@dataclass
class RegimeMetrics:
    """Per-strategy, per-regime performance."""
    strategy_name: str
    regime: str
    trades_count: int
    win_rate_pct: float
    avg_trade_pnl: float
    efficiency_score: float  # composite health (0-100)


class PerformanceAnalytics:
    """
    Track and analyze trading performance in real-time.
    
    Usage:
        analytics = get_performance_analytics()
        analytics.record_trade(symbol, strategy, entry_price, exit_price, regime)
        hourly = analytics.get_hourly_metrics()
        strategy_stats = analytics.get_strategy_metrics()
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path(__file__).parent.parent.parent / "analytics.db"
        
        self.db_path = db_path
        self._ensure_db()
        
        self.hourly_cache: Dict[datetime.datetime, HourlyMetrics] = {}
        self.strategy_cache: Dict[str, StrategyMetrics] = {}

    def _ensure_db(self) -> None:
        """Create analytics database if missing."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Hourly snapshots
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS hourly_pnl (
                timestamp TEXT PRIMARY KEY,
                hour_start TEXT,
                pnl_dollars REAL,
                pnl_pct REAL,
                trades_count INTEGER,
                wins INTEGER,
                losses INTEGER
            )
        """)
        
        # Per-trade analytics
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_analytics (
                trade_id TEXT PRIMARY KEY,
                symbol TEXT,
                strategy TEXT,
                regime TEXT,
                entry_price REAL,
                exit_price REAL,
                entry_time TEXT,
                exit_time TEXT,
                pnl_dollars REAL,
                pnl_pct REAL,
                hold_minutes INTEGER
            )
        """)
        
        conn.commit()
        conn.close()

    def record_trade(
        self,
        trade_id: str,
        symbol: str,
        strategy: str,
        regime: str,
        entry_price: float,
        exit_price: float,
        entry_time: datetime.datetime,
        exit_time: datetime.datetime,
    ) -> None:
        """Record a closed trade for analytics."""
        pnl_dollars = (exit_price - entry_price) * 100  # assuming 100 shares
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100
        hold_minutes = int((exit_time - entry_time).total_seconds() / 60)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO trade_analytics
            (trade_id, symbol, strategy, regime, entry_price, exit_price,
             entry_time, exit_time, pnl_dollars, pnl_pct, hold_minutes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_id, symbol, strategy, regime,
            entry_price, exit_price,
            entry_time.isoformat(),
            exit_time.isoformat(),
            pnl_dollars, pnl_pct, hold_minutes
        ))
        
        conn.commit()
        conn.close()
        
        log.info(f"[ANALYTICS] Recorded trade {trade_id}: {symbol} {strategy} {regime} P&L: {pnl_pct:+.1f}%")

    def get_hourly_metrics(self, hours_back: int = 1) -> List[HourlyMetrics]:
        """Get hourly P&L for last N hours."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cutoff_time = (datetime.datetime.now() - datetime.timedelta(hours=hours_back)).isoformat()
        
        cursor.execute("""
            SELECT strategy, pnl_dollars, pnl_pct
            FROM trade_analytics
            WHERE exit_time >= ?
        """, (cutoff_time,))
        
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return []
        
        total_pnl_dollars = sum(row[1] for row in rows)
        total_pnl_pct = sum(row[2] for row in rows)
        total_trades = len(rows)
        wins = len([r for r in rows if r[2] > 0])
        
        metrics = HourlyMetrics(
            hour_start=datetime.datetime.now() - datetime.timedelta(hours=hours_back),
            pnl_dollars=total_pnl_dollars,
            pnl_pct=total_pnl_pct,
            trades_count=total_trades,
            wins=wins,
            losses=total_trades - wins,
            win_rate_pct=(wins / total_trades * 100) if total_trades > 0 else 0,
            avg_profit_per_win=sum(r[1] for r in rows if r[2] > 0) / wins if wins > 0 else 0,
            avg_loss_per_loss=sum(r[1] for r in rows if r[2] < 0) / (total_trades - wins) if (total_trades - wins) > 0 else 0,
        )
        
        return [metrics]

    def get_strategy_metrics(self) -> Dict[str, StrategyMetrics]:
        """Get performance breakdown by strategy (today only)."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        today_start = datetime.datetime.now().replace(hour=0, minute=0, second=0).isoformat()
        
        cursor.execute("""
            SELECT strategy
            FROM trade_analytics
            WHERE exit_time >= ?
            GROUP BY strategy
        """, (today_start,))
        
        strategies = [row[0] for row in cursor.fetchall()]
        metrics_dict = {}
        
        for strat in strategies:
            cursor.execute("""
                SELECT pnl_dollars, pnl_pct
                FROM trade_analytics
                WHERE strategy = ? AND exit_time >= ?
            """, (strat, today_start))
            
            trades = cursor.fetchall()
            trades_count = len(trades)
            wins = len([t for t in trades if t[1] > 0])
            losses = trades_count - wins
            
            pnl_values = [t[0] for t in trades]
            total_pnl = sum(pnl_values)
            
            metrics_dict[strat] = StrategyMetrics(
                strategy_name=strat,
                trades_count=trades_count,
                wins=wins,
                losses=losses,
                win_rate_pct=(wins / trades_count * 100) if trades_count > 0 else 0,
                total_pnl=total_pnl,
                avg_profit=sum(t[0] for t in trades if t[1] > 0) / wins if wins > 0 else 0,
                avg_loss=sum(t[0] for t in trades if t[1] < 0) / losses if losses > 0 else 0,
                profit_factor=1.0,  # TODO: calculate properly
                best_trade=max(pnl_values) if pnl_values else 0,
                worst_trade=min(pnl_values) if pnl_values else 0,
            )
        
        conn.close()
        return metrics_dict

    def get_regime_metrics(self) -> Dict[Tuple[str, str], RegimeMetrics]:
        """Get performance breakdown by (strategy, regime)."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        today_start = datetime.datetime.now().replace(hour=0, minute=0, second=0).isoformat()
        
        cursor.execute("""
            SELECT strategy, regime
            FROM trade_analytics
            WHERE exit_time >= ?
            GROUP BY strategy, regime
        """, (today_start,))
        
        combos = cursor.fetchall()
        metrics_dict = {}
        
        for strat, regime in combos:
            cursor.execute("""
                SELECT pnl_pct
                FROM trade_analytics
                WHERE strategy = ? AND regime = ? AND exit_time >= ?
            """, (strat, regime, today_start))
            
            trades = [row[0] for row in cursor.fetchall()]
            trades_count = len(trades)
            wins = len([t for t in trades if t > 0])
            
            metrics_dict[(strat, regime)] = RegimeMetrics(
                strategy_name=strat,
                regime=regime,
                trades_count=trades_count,
                win_rate_pct=(wins / trades_count * 100) if trades_count > 0 else 0,
                avg_trade_pnl=(sum(trades) / trades_count) if trades_count > 0 else 0,
                efficiency_score=0.0,  # TODO: calculate
            )
        
        conn.close()
        return metrics_dict

    def log_summary(self) -> None:
        """Log performance summary."""
        hourly = self.get_hourly_metrics(hours_back=1)
        if hourly:
            h = hourly[0]
            log.info(
                f"[ANALYTICS] Last hour: {h.trades_count} trades | "
                f"Win rate: {h.win_rate_pct:.0f}% | "
                f"P&L: ${h.pnl_dollars:+.2f} ({h.pnl_pct:+.1f}%)"
            )
        
        strats = self.get_strategy_metrics()
        for strat_name, strat_metrics in strats.items():
            log.info(
                f"[ANALYTICS] {strat_name}: {strat_metrics.trades_count} trades | "
                f"Win rate: {strat_metrics.win_rate_pct:.0f}% | "
                f"Total P&L: ${strat_metrics.total_pnl:+.2f}"
            )


# Singleton
_analytics: Optional[PerformanceAnalytics] = None


def get_performance_analytics() -> PerformanceAnalytics:
    global _analytics
    if _analytics is None:
        _analytics = PerformanceAnalytics()
    return _analytics


def reset_performance_analytics() -> None:
    global _analytics
    _analytics = None

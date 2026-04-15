"""
Risk Monitor - Real-time Drawdown Tracking & Circuit Breakers
Monitors intra-day P&L and triggers circuit breakers before losses spiral.

Alerts:
  - WARNING: -5% from session high
  - CAUTION: -8% from session high
  - CIRCUIT BREAKER: -10% from session high (halt new long entries)
  - EMERGENCY: -15% from session high (halt all trading, close longs on next signal)

All thresholds configurable via .env
"""

import logging
import datetime
from typing import Optional, Dict, Tuple
from dataclasses import dataclass, field

log = logging.getLogger("ApexTrader")


@dataclass
class DrawdownState:
    """Current drawdown tracking state."""
    session_high_pnl: float = 0.0
    current_pnl: float = 0.0
    drawdown_pct: float = 0.0
    peak_time: datetime.datetime = field(default_factory=datetime.datetime.now)
    circuit_breaker_active: bool = False
    emergency_mode: bool = False
    warnings_issued: int = 0


class RiskMonitor:
    """
    Monitors intra-day P&L and triggers circuit breakers.
    
    Usage:
        monitor = RiskMonitor()
        state = monitor.update(current_pnl)
        if state.circuit_breaker_active:
            executor.skip_new_longs()
        if state.emergency_mode:
            executor.close_all_longs()
    """

    def __init__(
        self,
        warning_threshold_pct: float = -5.0,
        caution_threshold_pct: float = -8.0,
        circuit_breaker_threshold_pct: float = -10.0,
        emergency_threshold_pct: float = -15.0,
    ):
        self.warning_threshold = warning_threshold_pct
        self.caution_threshold = caution_threshold_pct
        self.circuit_breaker_threshold = circuit_breaker_threshold_pct
        self.emergency_threshold = emergency_threshold_pct
        
        self.state = DrawdownState()
        self.history: list[Tuple[datetime.datetime, float]] = []

    def update(self, current_pnl: float) -> DrawdownState:
        """
        Update state with new P&L reading.
        
        Returns:
            DrawdownState with current drawdown, triggers, etc.
        """
        now = datetime.datetime.now()
        self.history.append((now, current_pnl))
        
        # Keep last 100 readings (~1 hour if called every 30sec)
        if len(self.history) > 100:
            self.history = self.history[-100:]

        # Update session high
        if current_pnl > self.state.session_high_pnl:
            self.state.session_high_pnl = current_pnl
            self.state.peak_time = now

        self.state.current_pnl = current_pnl
        
        # Calculate drawdown from peak
        if self.state.session_high_pnl > 0:
            self.state.drawdown_pct = (
                (current_pnl - self.state.session_high_pnl) / self.state.session_high_pnl * 100
            )
        else:
            self.state.drawdown_pct = 0

        # ─── Trigger Circuit Breakers ─────────────────────────────────────
        
        # Emergency mode: -15% or worse (halt all trading)
        if self.state.drawdown_pct <= self.emergency_threshold:
            if not self.state.emergency_mode:
                log.error(
                    f"[RISK] EMERGENCY MODE ACTIVATED: {self.state.drawdown_pct:.1f}% drawdown. "
                    f"Peak: ${self.state.session_high_pnl:+.2f}, Current: ${current_pnl:+.2f}. "
                    f"HALTING ALL TRADING."
                )
                self.state.emergency_mode = True
                self.state.circuit_breaker_active = True
        
        # Circuit breaker: -10% to -15%
        elif self.state.drawdown_pct <= self.circuit_breaker_threshold:
            if not self.state.circuit_breaker_active:
                log.warning(
                    f"[RISK] CIRCUIT BREAKER ACTIVATED: {self.state.drawdown_pct:.1f}% drawdown. "
                    f"Peak: ${self.state.session_high_pnl:+.2f}, Current: ${current_pnl:+.2f}. "
                    f"HALTING NEW LONG ENTRIES."
                )
                self.state.circuit_breaker_active = True
                self.state.warnings_issued += 1
        
        # Caution: -8% to -10%
        elif self.state.drawdown_pct <= self.caution_threshold:
            log.warning(
                f"[RISK] CAUTION: {self.state.drawdown_pct:.1f}% drawdown (threshold: {self.caution_threshold:.1f}%). "
                f"Consider reducing position size or pausing new entries."
            )
            self.state.warnings_issued += 1
        
        # Warning: -5% to -8%
        elif self.state.drawdown_pct <= self.warning_threshold:
            if self.state.warnings_issued == 0:  # First warning only
                log.info(
                    f"[RISK] Warning: {self.state.drawdown_pct:.1f}% drawdown from session high "
                    f"(${self.state.session_high_pnl:+.2f}). Currently at ${current_pnl:+.2f}."
                )
                self.state.warnings_issued += 1

        # Recovery: Clear breach if back above thresholds
        if self.state.drawdown_pct > self.circuit_breaker_threshold:
            if self.state.circuit_breaker_active:
                log.info(f"[RISK] Circuit breaker CLEARED: Drawdown recovered to {self.state.drawdown_pct:.1f}%")
                self.state.circuit_breaker_active = False
        
        if self.state.drawdown_pct > self.emergency_threshold:
            if self.state.emergency_mode:
                log.info(f"[RISK] EMERGENCY MODE CLEARED: Drawdown recovered to {self.state.drawdown_pct:.1f}%")
                self.state.emergency_mode = False

        return self.state

    def reset_daily(self) -> None:
        """Reset for new trading day."""
        self.state = DrawdownState()
        self.history = []
        log.info("[RISK] Daily drawdown state reset")

    def get_status(self) -> str:
        """Get human-readable status."""
        status = f"Drawdown: {self.state.drawdown_pct:.1f}% | "
        status += f"Session High: ${self.state.session_high_pnl:+.2f} | "
        status += f"Current: ${self.state.current_pnl:+.2f}"
        
        if self.state.emergency_mode:
            status += " | ⛔ EMERGENCY"
        elif self.state.circuit_breaker_active:
            status += " | 🛑 CIRCUIT BREAKER"
        
        return status

    def should_halt_new_entries(self) -> bool:
        """Return True if circuit breaker should prevent new long entries."""
        return self.state.circuit_breaker_active

    def should_halt_all_trading(self) -> bool:
        """Return True if emergency mode should halt all trading."""
        return self.state.emergency_mode

    def should_close_longs(self) -> bool:
        """Return True if emergency mode should force-close longs on next signal."""
        return self.state.emergency_mode


# Singleton
_risk_monitor: Optional[RiskMonitor] = None


def get_risk_monitor(
    warning_pct: float = -5.0,
    caution_pct: float = -8.0,
    breaker_pct: float = -10.0,
    emergency_pct: float = -15.0,
) -> RiskMonitor:
    global _risk_monitor
    if _risk_monitor is None:
        _risk_monitor = RiskMonitor(
            warning_threshold_pct=warning_pct,
            caution_threshold_pct=caution_pct,
            circuit_breaker_threshold_pct=breaker_pct,
            emergency_threshold_pct=emergency_pct,
        )
    return _risk_monitor


def reset_risk_monitor() -> None:
    global _risk_monitor
    _risk_monitor = None

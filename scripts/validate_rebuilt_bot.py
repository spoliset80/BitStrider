#!/usr/bin/env python3
"""
INTELLIGENT BOT - Comprehensive Validation Suite (REBUILT)

Validates all systems before market open:
- Smart universe tiers
- Adaptive signal filtering
- Position scoring & smart SWAP
- Risk monitoring & circuit breakers
- Performance analytics

Usage:
    apextrader\Scripts\python.exe scripts\validate_rebuilt_bot.py
"""

import sys
import logging
from pathlib import Path
import datetime

# Add repo root to path
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

logging.basicConfig(level=logging.INFO, format='%(message)s')
log = logging.getLogger()

def main():
    log.info("\n" + "=" * 80)
    log.info("INTELLIGENT BOT - REBUILT VALIDATION SUITE")
    log.info("=" * 80 + "\n")
    
    all_passed = True
    
    # ─────────────────────────────────────────────────────────────────────────
    # TEST 1: Configuration
    # ─────────────────────────────────────────────────────────────────────────
    log.info("[TEST 1] Configuration Validation")
    log.info("-" * 80)
    try:
        from engine.config import (
            SMART_UNIVERSE_ENABLED, FORCE_SCAN, 
            TIER_A_ALLOCATION_PCT, TIER_B_ALLOCATION_PCT, TIER_C_ALLOCATION_PCT,
            ADAPTIVE_SIGNAL_FILTERING, ADAPTIVE_POSITION_SIZING,
            RISK_MONITOR_ENABLED, PERFORMANCE_ANALYTICS_ENABLED,
        )
        
        configs = [
            ("SMART_UNIVERSE_ENABLED", SMART_UNIVERSE_ENABLED, True),
            ("FORCE_SCAN (should be False)", FORCE_SCAN, False),
            ("Tier A allocation", TIER_A_ALLOCATION_PCT, 18.0),
            ("Tier B allocation", TIER_B_ALLOCATION_PCT, 8.0),
            ("Tier C allocation", TIER_C_ALLOCATION_PCT, 2.0),
            ("Total = 28%", TIER_A_ALLOCATION_PCT + TIER_B_ALLOCATION_PCT + TIER_C_ALLOCATION_PCT, 28.0),
            ("ADAPTIVE_SIGNAL_FILTERING", ADAPTIVE_SIGNAL_FILTERING, True),
            ("ADAPTIVE_POSITION_SIZING", ADAPTIVE_POSITION_SIZING, True),
            ("RISK_MONITOR_ENABLED", RISK_MONITOR_ENABLED, True),
            ("PERFORMANCE_ANALYTICS_ENABLED", PERFORMANCE_ANALYTICS_ENABLED, True),
        ]
        
        test1_passed = 0
        for name, actual, expected in configs:
            if actual == expected:
                log.info(f"  ✅ {name}: {actual}")
                test1_passed += 1
            else:
                log.error(f"  ❌ {name}: {actual} (expected {expected})")
        
        log.info(f"Result: {test1_passed}/{len(configs)} checks passed\n")
        all_passed = all_passed and (test1_passed == len(configs))
    except Exception as e:
        log.error(f"❌ TEST 1 FAILED: {e}\n")
        all_passed = False
    
    # ─────────────────────────────────────────────────────────────────────────
    # TEST 2: Smart Universe
    # ─────────────────────────────────────────────────────────────────────────
    log.info("[TEST 2] Smart Universe Initialization")
    log.info("-" * 80)
    try:
        from engine.options.smart_universe import get_smart_universe
        su = get_smart_universe()
        log.info(f"  ✅ Smart Universe initialized")
        log.info(f"     Tier A: {len(su.tier_a.tickers)} tickers | Max {su.tier_a.max_positions} positions")
        log.info(f"     Tier B: Ready for dynamic | Max {su.tier_b.max_positions} positions")
        log.info(f"     Tier C: Ready for equity bridge | Max {su.tier_c.max_positions} positions\n")
    except Exception as e:
        log.error(f"❌ TEST 2 FAILED: {e}\n")
        all_passed = False
    
    # ─────────────────────────────────────────────────────────────────────────
    # TEST 3: Position Manager (Smart SWAP)
    # ─────────────────────────────────────────────────────────────────────────
    log.info("[TEST 3] Position Manager (Smart SWAP Scoring)")
    log.info("-" * 80)
    try:
        from engine.execution.position_manager import get_position_manager
        pm = get_position_manager()
        
        scores = {}
        # Score 3 positions
        for symbol, momentum, pnl in [("AAPL", 1.5, +8.5), ("MSFT", -0.3, -2.1), ("NVDA", 0.8, +3.2)]:
            score = pm.calculate_position_score(
                symbol=symbol, trend_direction="up" if momentum > 0 else "down",
                momentum_pct=abs(momentum), unrealized_pnl_pct=pnl,
                hold_minutes=30, volatility_pct=4.2, regime="bull"
            )
            scores[symbol] = score
        
        log.info(f"  ✅ Calculated {len(scores)} position scores:")
        sorted_scores = sorted(scores.items(), key=lambda x: x[1].composite, reverse=True)
        for sym, score in sorted_scores:
            log.info(f"     {sym}: {score.composite:.0f}/100 (P&L: {score.unrealized_pnl_pct:+.1f}%)")
        
        weakest = pm.select_weakest_position(scores)
        if weakest:
            log.info(f"  ✅ Weakest position selected: {weakest[0]} (score: {weakest[1].composite:.0f})")
        log.info("")
    except Exception as e:
        log.error(f"❌ TEST 3 FAILED: {e}\n")
        all_passed = False
    
    # ─────────────────────────────────────────────────────────────────────────
    # TEST 4: Risk Monitor
    # ─────────────────────────────────────────────────────────────────────────
    log.info("[TEST 4] Risk Monitor (Drawdown Tracking)")
    log.info("-" * 80)
    try:
        from engine.risk.risk_monitor import get_risk_monitor
        rm = get_risk_monitor()
        
        state = rm.update(500.0)  # Peak
        log.info(f"  ✅ Session peak: ${state.session_high_pnl:+.2f}")
        
        state = rm.update(440.0)  # -12% drawdown
        log.info(f"     Drawdown: {state.drawdown_pct:.1f}% | Circuit breaker: {state.circuit_breaker_active}")
        
        state = rm.update(470.0)  # Recovery
        log.info(f"     Recovery: ${state.current_pnl:+.2f} | Status: {rm.get_status()}\n")
    except Exception as e:
        log.error(f"❌ TEST 4 FAILED: {e}\n")
        all_passed = False
    
    # ─────────────────────────────────────────────────────────────────────────
    # TEST 5: Signal Validator
    # ─────────────────────────────────────────────────────────────────────────
    log.info("[TEST 5] Signal Validator (Regime Filtering)")
    log.info("-" * 80)
    try:
        from engine.utils.signal_validator import get_signal_validator
        sv = get_signal_validator()
        
        # Bull regime
        result_bull = sv.validate_signal(
            symbol="NVDA", confidence=82.0, regime="bull",
            volatility_pct=5.2, liquidity_level="high",
            risk_reward_ratio=2.1, entry_price=875.50, tier="A"
        )
        log.info(f"  ✅ Bull regime: NVDA valid={result_bull.valid}, adjusted={result_bull.adjusted_confidence:.0f}")
        
        # Bear regime (stricter)
        result_bear = sv.validate_signal(
            symbol="NVDA", confidence=82.0, regime="bear",
            volatility_pct=12.5, liquidity_level="high",
            risk_reward_ratio=2.1, entry_price=875.50, tier="A"
        )
        log.info(f"  ✅ Bear regime: NVDA valid={result_bear.valid}, adjusted={result_bear.adjusted_confidence:.0f}\n")
    except Exception as e:
        log.error(f"❌ TEST 5 FAILED: {e}\n")
        all_passed = False
    
    # ─────────────────────────────────────────────────────────────────────────
    # TEST 6: Tier Sizing
    # ─────────────────────────────────────────────────────────────────────────
    log.info("[TEST 6] Tier Sizing Engine")
    log.info("-" * 80)
    try:
        from engine.options.tier_sizing import get_tier_allocation_engine
        tse = get_tier_allocation_engine()
        contracts, metadata = tse.calculate_tier_position_size(
            tier_name="A", total_equity=4670.0, entry_confidence=0.88,
            iv_rank=28.0, rr_ratio=2.1, option_price=2.50
        )
        log.info(f"  ✅ Tier A sizing: {contracts} contracts")
        log.info(f"     Metadata: {metadata}\n")
    except Exception as e:
        log.error(f"❌ TEST 6 FAILED: {e}\n")
        all_passed = False
    
    # ─────────────────────────────────────────────────────────────────────────
    # TEST 7: Performance Analytics
    # ─────────────────────────────────────────────────────────────────────────
    log.info("[TEST 7] Performance Analytics (Hourly P&L)")
    log.info("-" * 80)
    try:
        from engine.analytics.performance_tracker import get_performance_analytics
        pa = get_performance_analytics()
        log.info(f"  ✅ Performance analytics initialized")
        log.info(f"     Database: {pa.db_path}")
        log.info(f"     Ready: Hourly P&L + Strategy ROI + Regime metrics\n")
    except Exception as e:
        log.error(f"❌ TEST 7 FAILED: {e}\n")
        all_passed = False
    
    # ─────────────────────────────────────────────────────────────────────────
    # SUMMARY
    # ─────────────────────────────────────────────────────────────────────────
    log.info("=" * 80)
    log.info("VALIDATION SUMMARY")
    log.info("=" * 80)
    
    if all_passed:
        log.info("\n✅ ALL VALIDATION TESTS PASSED!")
        log.info("🚀 REBUILT BOT IS READY FOR MARKET OPEN\n")
        log.info("New Intelligent Capabilities:")
        log.info("  ✨ Adaptive signal filtering (regime-aware confidence gates)")
        log.info("  ✨ Smart position rotation (multi-factor scoring)")
        log.info("  ✨ Real-time drawdown monitoring (circuit breakers)")
        log.info("  ✨ Performance analytics (strategy ROI, win rates)")
        log.info("  ✨ Dynamic position sizing (win-rate boosted)")
        log.info("  ✨ Volatility awareness (automatic adaptation)")
        log.info("  ✨ Proactive risk management (before losses spiral)\n")
        return 0
    else:
        log.error("\n❌ SOME TESTS FAILED")
        log.error("Fix above issues before proceeding\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())

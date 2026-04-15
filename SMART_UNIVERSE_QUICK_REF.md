"""
QUICK REFERENCE — Smart Universe Tier System

Embedded in orchestrator for automatic tier-aware options scanning.
"""

# ═══════════════════════════════════════════════════════════════════════════════
# ONE-MINUTE TUNING GUIDE (Edit .env file, restart bot)
# ═══════════════════════════════════════════════════════════════════════════════

# WANT MORE SIGNALS?
TIER_A_CONFIDENCE_MIN=0.80                    # Relax from 0.85
TIER_B_CONFIDENCE_MIN=0.65                    # Relax from 0.70
TIER_B_IV_RANK_MAX=40.0                       # Relax from 30.0
TIER_B_LIQUID_HOURS_ONLY=false                # Scan anytime

# WANT FEWER, HIGHER-QUALITY SIGNALS?
TIER_A_CONFIDENCE_MIN=0.90                    # Stricter
TIER_B_CONFIDENCE_MIN=0.75                    # Stricter
TIER_B_IV_RANK_MAX=25.0                       # Stricter
TIER_A_ALLOCATION_PCT=20.0                    # More capital to quality tier

# WANT FASTER ROTATION (5+ Trades/Day)?
TIER_B_HOLD_DAYS=2                            # Close after 2 days
TIER_B_MAX_POSITIONS=5                        # More positions open
TIER_B_SCAN_EVERY_MIN=15                      # Re-scan frequently

# WANT LONGER HOLDS (Swing Trading, 1-2 Trades/Day)?
TIER_B_HOLD_DAYS=10                           # Hold 10 days
TIER_B_MAX_POSITIONS=1                        # One at a time
TIER_A_ALLOCATION_PCT=25.0                    # More capital to reliable tier

# WANT MAXIMUM CAPITAL PRESERVATION?
TIER_A_ALLOCATION_PCT=28.0                    # Heavy mega-cap bias
TIER_B_ALLOCATION_PCT=0.0                     # No unusual vol
TIER_C_ALLOCATION_PCT=0.0                     # No hedge plays

# ═══════════════════════════════════════════════════════════════════════════════
# PROGRAMMATIC ACCESS (From Python code)
# ═══════════════════════════════════════════════════════════════════════════════

from engine.options.smart_universe import get_smart_universe
from engine.options.tier_sizing import get_tier_allocation_engine

# Get singleton instances
smart_universe = get_smart_universe()
tier_engine = get_tier_allocation_engine()

# ───────────────────────────────────────────────────────────────────────────────
# Tier Management
# ───────────────────────────────────────────────────────────────────────────────

# Load dynamic Tier B universe from TI unusual options volume
smart_universe.load_tier_b_universe(['BCRX', 'MRNA', 'MARA', 'COIN', 'PLTR'])

# Add a position to a tier
smart_universe.add_position('AAPL', 'Tier A (Mega-Liquid)', entry_date=None)

# Remove a position (on close)
smart_universe.remove_position('AAPL', 'Tier A (Mega-Liquid)')

# Get active positions in a tier
positions = smart_universe.get_tier_positions('Tier A (Mega-Liquid)')
# Returns: {'AAPL': {'entered_at': date(2026, 4, 14)}, ...}

# Auto-close stale Tier B positions (older than TIER_B_HOLD_DAYS)
closed_symbols = smart_universe.prune_tier_b_stale()

# Log comprehensive status
smart_universe.log_status()

# Get recommendations for all tiers
recs = smart_universe.get_recommendations(total_equity=10000)
# Returns: {
#   'Tier A (Mega-Liquid)': {
#       'should_scan': True,
#       'available_positions': 2,
#       'allocated_capital': 1800,
#       'tickers_to_scan': ['SPY', 'QQQ', ...],
#       'confidence_min': 0.85,
#   },
#   ...
# }

# ───────────────────────────────────────────────────────────────────────────────
# Position Sizing
# ───────────────────────────────────────────────────────────────────────────────

# Calculate tier-aware contract quantity
contracts, metadata = tier_engine.calculate_tier_position_size(
    tier_name='Tier A (Mega-Liquid)',
    total_equity=10000,
    entry_confidence=0.88,
    iv_rank=25.0,
    rr_ratio=2.5,
    option_price=2.50,  # premium per share
)

# Returns:
# contracts = 2  (number of contracts to open)
# metadata = {
#     'tier_name': 'Tier A (Mega-Liquid)',
#     'tier_capital': 1800,
#     'regime_multiplier': 1.0,
#     'timing_multiplier': 0.9,
#     'confidence_multiplier': 1.12,
#     'contracts': 2,
#     'final_capital': 450,
#     ...
# }

# Log sizing breakdown for transparency
tier_engine.log_sizing_breakdown(contracts, metadata)

# Get status of all tiers
status = tier_engine.get_tier_status(total_equity=10000)
# Returns: {
#   'Tier A (Mega-Liquid)': {
#       'allocation_pct': 18.0,
#       'allocated_capital': 1800,
#       'active_positions': 1,
#       'max_positions': 2,
#       'available_slots': 1,
#       'confidence_min': 0.85,
#   },
#   ...
# }

# ═══════════════════════════════════════════════════════════════════════════════
# COMMON WORKFLOWS
# ═══════════════════════════════════════════════════════════════════════════════

# Workflow 1: Scan Tier A and Execute High-Confidence Signal
# ──────────────────────────────────────────────────────────
def scan_and_execute_tier_a():
    su = get_smart_universe()
    tier_engine = get_tier_allocation_engine()
    
    # Check if tier can scan and accept new positions
    if not su.tier_a.should_scan_now():
        print("Tier A not ready to scan")
        return
    
    if su.tier_a.is_full():
        print("Tier A is at max positions")
        return
    
    # Scan ticker (e.g., NVDA)
    confidence = 0.88
    iv_rank = 28.0
    rr_ratio = 2.1
    option_price = 3.25
    
    if confidence >= su.tier_a.confidence_min:
        contracts, meta = tier_engine.calculate_tier_position_size(
            'Tier A (Mega-Liquid)', total_equity=10000,
            entry_confidence=confidence, iv_rank=iv_rank,
            rr_ratio=rr_ratio, option_price=option_price
        )
        
        if contracts > 0:
            # Execute
            su.add_position('NVDA', 'Tier A (Mega-Liquid)')
            tier_engine.log_sizing_breakdown(contracts, meta)
            print(f"Executed {contracts} contracts of NVDA")

# Workflow 2: Close and Rotate Tier B Position
# ─────────────────────────────────────────────
def close_and_check_tier_b():
    su = get_smart_universe()
    
    # Remove closed position
    su.remove_position('BCRX', 'Tier B (Unusual Vol)')
    print("BCRX position closed")
    
    # Auto-close stale positions
    stale = su.prune_tier_b_stale()
    if stale:
        print(f"Auto-closed stale Tier B: {stale}")
    
    # Update with new TI universe
    new_universe = ['COIN', 'PLTR', 'MRNA', 'CRWD']
    su.load_tier_b_universe(new_universe)

# Workflow 3: Check Tier Status Before Scan Cycle
# ─────────────────────────────────────────────────
def check_tier_status():
    su = get_smart_universe()
    tier_engine = get_tier_allocation_engine()
    equity = 10000
    
    # Get recommendations
    recs = su.get_recommendations(equity)
    
    for tier_name, rec in recs.items():
        if rec['should_scan']:
            print(f"\n{tier_name}:")
            print(f"  Available positions: {rec['available_positions']}")
            print(f"  Allocated capital: ${rec['allocated_capital']:,.0f}")
            print(f"  Tickers to scan: {rec['tickers_to_scan']}")
            print(f"  Confidence min: {rec['confidence_min']:.0%}")
        else:
            print(f"{tier_name}: Not ready (on cooldown or outside liquid hours)")

# ═══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR INTEGRATION (Automatic)
# ═══════════════════════════════════════════════════════════════════════════════

# The smart universe is automatically initialized in orchestrator.py:
# 
#   from engine.orchestrator import smart_universe, tier_allocation_engine
#   
#   if smart_universe:
#       recs = smart_universe.get_recommendations(account_equity)
#       # Use recs to determine which tiers should scan next

# ═══════════════════════════════════════════════════════════════════════════════
# POSITION SIZING FORMULA
# ═══════════════════════════════════════════════════════════════════════════════

# Base Position Capital = Tier Allocation / Available Slots
# Final Position Capital = Base × Regime Mult × Timing Mult × Conf Mult × IV Mult × R/R Mult
# Contracts = Final Capital / (Option Price × 100)

# Example:
# Tier A alloc = 18% of $10k = $1800
# Available slots = 2
# Base = $1800 / 2 = $900
# 
# Regime mult = 1.0 (neutral)
# Timing mult = 0.85 (lunch hour)
# Conf mult = 1.1 (88% > 85% min)
# IV mult = 1.15 (25% < 30% III, bonus applied)
# R/R mult = 1.1 (2.5x R/R, bonus applied)
# 
# Final = $900 × 1.0 × 0.85 × 1.1 × 1.15 × 1.1 = ~$1,210
# Contracts = $1,210 / ($2.50 × 100) = 4.8 → 4 contracts

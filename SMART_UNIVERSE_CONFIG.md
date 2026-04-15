"""
Smart Universe Configuration Examples

Setup via .env file for easy parameter tuning.
"""

# ═══════════════════════════════════════════════════════════════════════════════
# TIER A — MEGA-LIQUID SEGMENT (Always scan, high bar)
# ═══════════════════════════════════════════════════════════════════════════════

# Enable/disable entire smart universe system
SMART_UNIVERSE_ENABLED=true

# Allocation: % of total portfolio equity
TIER_A_ALLOCATION_PCT=18.0                    # 18% of equity to mega-caps

# Position limits
TIER_A_MAX_POSITIONS=2                        # Never more than 2 concurrent mega-cap positions

# Entry filters
TIER_A_CONFIDENCE_MIN=0.85                    # 85%+ confidence only (strict)

# Scanning frequency (faster = more signal opportunities but more CPU)
TIER_A_SCAN_EVERY_MIN=5                       # Re-scan every 5 minutes

# Ticker list (comma-separated, no spaces)
TIER_A_TICKERS=SPY,QQQ,AAPL,MSFT,NVDA,TSLA,AMD,GOOGL,META,AMZN,NFLX

# ═══════════════════════════════════════════════════════════════════════════════
# TIER B — UNUSUAL VOLUME/OPPORTUNITY (Dynamic, high-turnover rotation)
# ═══════════════════════════════════════════════════════════════════════════════

# Allocation: % of total portfolio equity
TIER_B_ALLOCATION_PCT=8.0                     # 8% of equity to unusual vol plays

# Position limits (shorter hold duration = higher rotation)
TIER_B_MAX_POSITIONS=3                        # Max 3 concurrent positions
TIER_B_HOLD_DAYS=5                            # Auto-close after 5 days (rotate out)

# Entry filters (relaxed for faster signal flow)
TIER_B_CONFIDENCE_MIN=0.70                    # 70%+ OK (lower bar = faster rotation)
TIER_B_IV_RANK_MAX=30.0                       # Hunt for cheap vol (IV Rank <30%)

# Enable/disable liquid hours restriction (9:30-11am, 2-3pm ET only)
TIER_B_LIQUID_HOURS_ONLY=true                 # true = avoid lunch dead zone

# Scanning frequency (less frequent = TI file dependency)
TIER_B_SCAN_EVERY_MIN=30                      # Re-scan TI unusual vol every 30 min

# ═══════════════════════════════════════════════════════════════════════════════
# TIER C — EQUITY BRIDGE (Correlation hedge + downside protection)
# ═══════════════════════════════════════════════════════════════════════════════

# Allocation: % of total portfolio equity
TIER_C_ALLOCATION_PCT=2.0                     # 2% of equity (small, tactical)

# Position limits
TIER_C_MAX_POSITIONS=2                        # Max 2 concurrent hedge positions

# Entry filters (relaxed = supporting equities)
TIER_C_CONFIDENCE_MIN=0.65                    # 65%+ OK (relaxed, supports equity)

# Put/call strike selection buffers
TIER_C_SELL_PUT_BUFFER_PCT=5.0                # Sell puts 5% below support (yield play)
TIER_C_BUY_CALL_BUFFER_PCT=2.0                # Buy calls 2% above resistance (recovery play)

# ═══════════════════════════════════════════════════════════════════════════════
# TUNING GUIDE
# ═══════════════════════════════════════════════════════════════════════════════

# Scenario 1: Aggressive Scanner (More Signals, Higher Risk)
# TIER_A_CONFIDENCE_MIN=0.80                  # Slightly relaxed
# TIER_B_CONFIDENCE_MIN=0.65                  # Lower bar
# TIER_B_HOLD_DAYS=3                          # Faster rotation
# TIER_B_SCAN_EVERY_MIN=15                    # More frequent scans
# TIER_A_ALLOCATION_PCT=20.0                  # More capital to tier A

# Scenario 2: Conservative Scanner (Fewer Signals, Lower Risk)
# TIER_A_CONFIDENCE_MIN=0.90                  # Stricter
# TIER_B_CONFIDENCE_MIN=0.75                  # Higher bar
# TIER_B_HOLD_DAYS=7                          # Longer hold (patience)
# TIER_B_SCAN_EVERY_MIN=45                    # Less frequent scans
# TIER_A_ALLOCATION_PCT=15.0                  # Less capital, focus quality

# Scenario 3: High-Turnover (5+ Trades/Day Target)
# TIER_B_MAX_POSITIONS=5                      # More concurrent positions
# TIER_B_HOLD_DAYS=2                          # Very fast rotation (2 days)
# TIER_B_IV_RANK_MAX=40.0                     # Relax IV Rank filter (catch more)
# TIER_B_LIQUID_HOURS_ONLY=false              # Scan anytime (more opportunities)
# TIER_B_ALLOCATION_PCT=12.0                  # Higher allocation to rotation tier

# Scenario 4: Capital Preservation (Low Risk)
# TIER_A_ALLOCATION_PCT=25.0                  # Heavy mega-cap bias
# TIER_B_ALLOCATION_PCT=3.0                   # Minimal unusual vol
# TIER_C_ALLOCATION_PCT=0.0                   # No hedge plays
# TIER_A_CONFIDENCE_MIN=0.88                  # Very strict
# TIER_B_CONFIDENCE_MIN=0.80                  # Strict

# ═══════════════════════════════════════════════════════════════════════════════
# EXAMPLE .env File (Copy & Paste to .env)
# ═══════════════════════════════════════════════════════════════════════════════

# SMART_UNIVERSE_ENABLED=true
# TIER_A_ALLOCATION_PCT=18.0
# TIER_A_MAX_POSITIONS=2
# TIER_A_CONFIDENCE_MIN=0.85
# TIER_A_SCAN_EVERY_MIN=5
# TIER_A_TICKERS=SPY,QQQ,AAPL,MSFT,NVDA,TSLA,AMD,GOOGL,META,AMZN,NFLX
# 
# TIER_B_ALLOCATION_PCT=8.0
# TIER_B_MAX_POSITIONS=3
# TIER_B_CONFIDENCE_MIN=0.70
# TIER_B_IV_RANK_MAX=30.0
# TIER_B_HOLD_DAYS=5
# TIER_B_SCAN_EVERY_MIN=30
# TIER_B_LIQUID_HOURS_ONLY=true
# 
# TIER_C_ALLOCATION_PCT=2.0
# TIER_C_MAX_POSITIONS=2
# TIER_C_CONFIDENCE_MIN=0.65
# TIER_C_SELL_PUT_BUFFER_PCT=5.0
# TIER_C_BUY_CALL_BUFFER_PCT=2.0

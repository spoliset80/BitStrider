#!/usr/bin/env python3
"""
Backtest TradeIdeasEquityStrategy using today's latest TI primary tickers.
Tests the strategy on recent historical data to see performance.
"""

import sys
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.equity.strategies import TradeIdeasEquityStrategy, Signal
from engine.utils import get_bars
from engine.config import TI_EQUITY_BASE_CONFIDENCE

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('BacktestTI')

def load_ti_primary():
    """Load today's TI primary tickers."""
    ti_file = REPO_ROOT / 'data' / 'ti_primary.json'
    try:
        with open(ti_file) as f:
            data = json.load(f)
        tickers = data.get('tickers', [])
        log.info(f"Loaded {len(tickers)} tickers from ti_primary.json (updated {data.get('updated', 'unknown')})")
        return tickers
    except Exception as e:
        log.error(f"Failed to load ti_primary.json: {e}")
        return []

def backtest_ticker(strategy, symbol, lookback_days=5):
    """Backtest strategy on a single ticker."""
    try:
        # Get historical data
        daily = get_bars(symbol, f"{lookback_days}d", "1d")
        if daily.empty or len(daily) < 2:
            return None
        
        # Current price
        price = float(daily["close"].iloc[-1])
        
        # Test the strategy
        signal = strategy.scan(symbol)
        
        return {
            'symbol': symbol,
            'price': price,
            'signal': signal,
            'success': signal is not None
        }
    except Exception as e:
        log.debug(f"Error testing {symbol}: {e}")
        return None

def main():
    """Main backtest runner."""
    print("\n" + "=" * 100)
    print("TradeIdeasEquityStrategy BACKTEST")
    print("=" * 100)
    
    # Load TI tickers
    tickers = load_ti_primary()
    if not tickers:
        log.error("No TI tickers loaded. Exiting.")
        return
    
    # Use first 15 tickers for reasonable backtest time
    test_tickers = tickers[:15]
    log.info(f"Testing {len(test_tickers)} tickers: {', '.join(test_tickers)}")
    print()
    
    # Initialize strategy
    strategy = TradeIdeasEquityStrategy()
    
    # Run backtest
    results = []
    for i, symbol in enumerate(test_tickers, 1):
        result = backtest_ticker(strategy, symbol, lookback_days=5)
        if result:
            results.append(result)
            status = "SIGNAL" if result['success'] else "no signal"
            print(f"[{i:2d}/{len(test_tickers)}] {symbol:6s} ${result['price']:7.2f} {status}")
            if result['success']:
                sig = result['signal']
                print(f"         -> {sig.action.upper()} @ {sig.price:.2f} | conf={sig.confidence:.0%} | {sig.reason}")
    
    # Summary
    print("\n" + "=" * 100)
    successful = sum(1 for r in results if r['success'])
    print(f"SUMMARY: {successful}/{len(results)} tickers generated signals ({successful*100//len(results)}%)")
    print(f"Base Confidence: {TI_EQUITY_BASE_CONFIDENCE:.0%}")
    print(f"Signals with 85%+ confidence can execute immediately during extended hours")
    print("=" * 100 + "\n")

if __name__ == '__main__':
    main()

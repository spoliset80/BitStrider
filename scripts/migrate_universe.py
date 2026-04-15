"""One-shot: migrate all current config.py universe tickers into data/universe.json."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config import PRIORITY_1_MOMENTUM, PRIORITY_2_ESTABLISHED, PRIORITY_FOLLOWING
from engine.equity.universe import add_tickers, stats

add_tickers(PRIORITY_1_MOMENTUM, tier=1)
add_tickers(PRIORITY_2_ESTABLISHED, tier=2)
add_tickers(PRIORITY_FOLLOWING, tier=3)

s = stats()
t = s["by_tier"]
print(f"Migrated: tier1={t.get(1,0)}  tier2={t.get(2,0)}  tier3={t.get(3,0)}  total={s['total_alive']}")
print(f"File: {s['file']}")

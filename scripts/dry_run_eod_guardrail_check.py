"""Read-only dry run: which currently-open positions WOULD close_eod_positions
and close_guardrail_fail_positions act on, if run right now?

Never submits an order or cancels anything -- reuses the exact same
selection logic those two live functions use (same-day entry only for the
former -- 2026-08-24, user request: "I wouldn't expect any positions to
stay active at 3:50pm ET", dropped the EOD_CLOSE_STRATEGIES allow-list
gate that used to also apply here; EnhancedExecutor._guardrail_fail_reason
for the latter) against real current positions/orders, but only prints.
Ignores both functions' own time-of-day gate, since the point is to
preview the decision, not to check whether it's currently their scheduled
window.

Run with:
  python scripts/dry_run_eod_guardrail_check.py
Hits the live broker (read-only: get_all_positions/get_orders) and
yfinance (float/mcap/volume lookups) -- no network calls are mutating.
"""
import sys
import re
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")  # main.py loads this the same way before touching engine.config

import datetime
from engine.orchestrator import _build_context
from engine.utils.bars import get_daily_volume_bars
from engine.equity.strategies import _get_float_shares, _get_market_cap

ctx   = _build_context()
today = datetime.date.today()

try:
    positions = ctx.client.get_all_positions()
except Exception as e:
    print(f"ERROR: could not fetch positions: {e}")
    sys.exit(1)

print(f"{len(positions)} open position(s) as of now\n")

eod_would_close = []
guardrail_would_close = []
guardrail_pass = []

for pos in positions:
    sym = pos.symbol
    if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
        continue  # options legs -- managed separately, out of scope for both checks
    qty = int(float(pos.qty))
    if qty == 0:
        continue

    # --- close_eod_positions criteria: any same-day entry, any strategy ---
    entry_info = ctx.executor._entry_log.get(sym)
    if entry_info and entry_info.get("date") == today:
        eod_would_close.append((sym, entry_info.get("strategy")))

    # --- close_guardrail_fail_positions criteria ---
    try:
        daily = get_daily_volume_bars(sym)
        avg_daily_vol = float(daily["volume"].iloc[:-1].mean()) if not daily.empty and len(daily) >= 2 else None
    except Exception as e:
        print(f"  [warn] {sym}: volume lookup failed: {e}")
        avg_daily_vol = None
    shares_float = _get_float_shares(sym)
    market_cap   = _get_market_cap(sym)
    fail_reason  = ctx.executor._guardrail_fail_reason(avg_daily_vol, shares_float, market_cap)
    if fail_reason is not None:
        guardrail_would_close.append((sym, fail_reason))
    else:
        guardrail_pass.append(sym)

print(f"close_eod_positions WOULD close: {len(eod_would_close)}")
for sym, strat in eod_would_close:
    print(f"  {sym:6s} strategy={strat}")

print(f"\nclose_guardrail_fail_positions WOULD close: {len(guardrail_would_close)}")
for sym, reason in guardrail_would_close:
    print(f"  {sym:6s} {reason}")

print(f"\nguardrail-pass, held overnight either way: {len(guardrail_pass)}")
for sym in guardrail_pass:
    print(f"  {sym}")

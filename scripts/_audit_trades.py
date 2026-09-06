"""Temp audit: today's fills + per-symbol round-trip P&L (read-only)."""
import datetime
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for line in (Path(__file__).resolve().parents[1] / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import pytz
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest

ET = pytz.timezone("America/New_York")
key = os.environ.get("LIVE_ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
secret = os.environ.get("LIVE_ALPACA_API_SECRET") or os.environ.get("APCA_API_SECRET_KEY")
tc = TradingClient(key, secret, paper=False)
req = GetOrdersRequest(status="all", limit=500, after=datetime.datetime(2026, 8, 31, tzinfo=datetime.timezone.utc))
orders = tc.get_orders(filter=req)
fills = [o for o in orders if str(o.status) == "OrderStatus.FILLED"]
print(f"TOTAL orders today: {len(orders)} | FILLED: {len(fills)}")

buys, sells = defaultdict(float), defaultdict(float)
print("\n-- fills (ET) --")
for o in sorted(fills, key=lambda x: x.filled_at or datetime.datetime.max):
    ts = o.filled_at.astimezone(ET).strftime("%H:%M:%S") if o.filled_at else "??????"
    side = str(o.side).split(".")[-1].upper()
    price = float(o.filled_avg_price) if o.filled_avg_price else 0.0
    qty = float(o.qty)
    print(f"  {ts} {side:4} {o.symbol:6} q={qty:9.0f} @ {price:8.2f}")
    if side == "BUY":
        buys[o.symbol] += qty * price
    else:
        sells[o.symbol] += qty * price

print("\n-- per-symbol round trips (sells-buys) --")
tot = 0.0
for sym in sorted(set(list(buys) + list(sells))):
    pnl = sells.get(sym, 0.0) - buys.get(sym, 0.0)
    tot += pnl
    print(f"  {sym:6} bought=${buys.get(sym,0):9.2f} sold=${sells.get(sym,0):9.2f} pnl={pnl:+8.2f}")
print(f"  TOTAL realized pnl ~ {tot:+.2f}")

"""
Manual close: Buy-to-close SMH 550/555 Bear Call Credit Spread
Limit price: $2.55 debit (10 contracts) — spread mark at time of submission
Total cost: $2,550 debit to close
"""
import os, json, sys

# Load .env before importing engine.config (which reads env vars at import time)
from dotenv import load_dotenv
load_dotenv()
os.environ.setdefault("TRADE_MODE", "live")

from engine.broker.broker_factory import BrokerFactory
import engine.config as cfg
client = BrokerFactory.create_stock_client(cfg.STOCKS_BROKER)

# Cancel any previous open order if needed (set to None to skip)
PREV_ORDER_ID = "3e4c3020-e486-4e71-87d9-9b089adab661"
if PREV_ORDER_ID:
    try:
        client.cancel_order_by_id(PREV_ORDER_ID)
        print(f"Cancelled previous order {PREV_ORDER_ID}")
    except Exception as e:
        print(f"Cancel skipped (may already be filled/cancelled): {e}")
    print()

# The two legs (reversed from entry: buy back the short $550, sell back the long $555)
LIMIT_PRICE = 2.55   # debit to close — spread mark as of submission
CONTRACTS   = 10

payload = {
    "symbol": "",
    "qty": str(CONTRACTS),
    "order_class": "mleg",
    "type": "limit",
    "limit_price": str(LIMIT_PRICE),
    "time_in_force": "day",
    "legs": [
        # Buy back the short $550 call
        {
            "symbol": "SMH260522C00550000",
            "side": "buy",
            "ratio_qty": "1",
            "position_intent": "buy_to_close",
        },
        # Sell back the long $555 call
        {
            "symbol": "SMH260522C00555000",
            "side": "sell",
            "ratio_qty": "1",
            "position_intent": "sell_to_close",
        },
    ],
}

print(f"Submitting BUY-TO-CLOSE: SMH 550/555 Bear Call Spread")
print(f"  Contracts : {CONTRACTS}")
print(f"  Limit     : ${LIMIT_PRICE:.2f} debit per spread")
print(f"  Total cost: ${LIMIT_PRICE * CONTRACTS * 100:,.0f}")
print()

try:
    resp = client.post("/orders", payload)
    print("Order submitted successfully:")
    print(json.dumps(resp if isinstance(resp, dict) else vars(resp), indent=2, default=str))
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)

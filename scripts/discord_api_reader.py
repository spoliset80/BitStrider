from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from scripts.discord_parser import parse_trade, Trade

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# -- Config --------------------------------------------------------------------

_raw = os.getenv("DISCORD_CHANNEL_IDS", "753377655532945558,752750381918060589,769046364738289734")
CHANNEL_IDS    = [c.strip() for c in _raw.split(",") if c.strip()]
USER_TOKEN     = os.getenv("DISCORD_USER_TOKEN", "")
MODE           = os.getenv("DISCORD_OPTIONS_MODE", "paper")
API_BASE       = "https://discord.com/api/v10"

# Risk / allocation
CONFIDENCE_MIN    = int(os.getenv("DISCORD_CONFIDENCE_MIN", "70"))
ORDER_NOTIONAL    = float(os.getenv("DISCORD_ORDER_NOTIONAL", "500"))
MAX_POSITIONS     = int(os.getenv("DISCORD_MAX_POSITIONS", "10"))
MAX_DAILY_SPEND   = float(os.getenv("DISCORD_MAX_DAILY_SPEND", "5000"))
DEDUPE_TICKER     = os.getenv("DISCORD_DEDUPE_TICKER", "true").lower() == "true"

_BP_TIERS = [
    (90, float(os.getenv("DISCORD_ALLOC_HIGH_PCT", "3.0"))),
    (80, float(os.getenv("DISCORD_ALLOC_MED_PCT",  "2.0"))),
    (70, float(os.getenv("DISCORD_ALLOC_LOW_PCT",  "1.0"))),
]

def _alloc_pct(conf: int) -> float:
    for threshold, pct in _BP_TIERS:
        if conf >= threshold:
            return pct
    return _BP_TIERS[-1][1]

def notional_for(conf: int, buying_power: float | None = None) -> float:
    """Return $ to deploy. Uses % of buying_power when available, else ORDER_NOTIONAL fallback."""
    if buying_power and buying_power > 0:
        return round(buying_power * _alloc_pct(conf) / 100, 2)
    pct = _alloc_pct(conf)
    return round(ORDER_NOTIONAL * pct / _BP_TIERS[-1][1], 2)

# -- Alpaca Client & Telemetry -------------------------------------------------

def _make_client():
    try:
        from alpaca.trading.client import TradingClient
        if MODE == "live":
            key, secret, paper = os.getenv("LIVE_ALPACA_API_KEY",""), os.getenv("LIVE_ALPACA_API_SECRET",""), False
        else:
            key, secret, paper = os.getenv("PAPER_ALPACA_API_KEY",""), os.getenv("PAPER_ALPACA_API_SECRET",""), True
        if not key or not secret:
            logger.warning("Alpaca keys missing -- DRY RUN mode")
            return None
        c = TradingClient(key, secret, paper=paper)
        logger.info(f"Alpaca connected [{MODE.upper()}]")
        return c
    except Exception as e:
        logger.warning(f"Alpaca init failed: {e} -- DRY RUN mode")
        return None

_alpaca = None
_buying_power_cache: dict = {"bp": None, "ts": 0.0}

def get_buying_power() -> float | None:
    """Fetch option buying power from Alpaca; cached for 60s."""
    global _alpaca
    if _alpaca is None:
        _alpaca = _make_client()
    if _alpaca is None:
        return None
    now = time.time()
    if _buying_power_cache["bp"] is not None and now - _buying_power_cache["ts"] < 60:
        return _buying_power_cache["bp"]
    try:
        acct = _alpaca.get_account()
        bp = float(getattr(acct, "options_buying_power", None) or acct.buying_power or 0)
        _buying_power_cache["bp"] = bp
        _buying_power_cache["ts"] = now
        logger.info(f"  [BP] options_buying_power=${bp:,.2f}")
        return bp
    except Exception as e:
        logger.warning(f"  [BP] failed to fetch buying power: {e}")
        return None


# -- Order placement -----------------------------------------------------------

def place_equity_order(ticker: str, side: str, notional: float) -> dict:
    global _alpaca
    if _alpaca is None:
        _alpaca = _make_client()
    if _alpaca is None:
        logger.info(f"  [DRY RUN] EQUITY {side.upper()} ${notional} {ticker}")
        return {"status": "dry_run", "type": "equity", "ticker": ticker, "side": side}
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        order = _alpaca.submit_order(MarketOrderRequest(
            symbol=ticker, notional=notional,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))
        logger.info(f"  SUBMITTED EQUITY {side.upper()} ${notional} {ticker} id={order.id}")
        return {"status": "submitted", "type": "equity", "id": str(order.id), "ticker": ticker}
    except Exception as e:
        logger.error(f"  ORDER FAILED {ticker}: {e}")
        return {"status": "error", "ticker": ticker, "error": str(e)}

def place_option_order(occ: str, side: str, qty: int = 1) -> dict:
    global _alpaca
    if _alpaca is None:
        _alpaca = _make_client()
    if _alpaca is None:
        logger.info(f"  [DRY RUN] OPTION {side.upper()} {qty}x {occ}")
        return {"status": "dry_run", "type": "option", "occ": occ, "side": side, "qty": qty}
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        order = _alpaca.submit_order(MarketOrderRequest(
            symbol=occ, qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))
        logger.info(f"  SUBMITTED OPTION {side.upper()} {qty}x {occ} id={order.id}")
        return {"status": "submitted", "type": "option", "id": str(order.id), "occ": occ, "qty": qty}
    except Exception as e:
        logger.error(f"  ORDER FAILED {occ}: {e}")
        return {"status": "error", "occ": occ, "error": str(e)}


# -- Discord API ---------------------------------------------------------------

def fetch(channel_id: str, after: str = None, limit: int = 50) -> list[dict]:
    params = {"limit": limit}
    if after: 
        params["after"] = after
    r = requests.get(f"{API_BASE}/channels/{channel_id}/messages",
                     headers={"Authorization": USER_TOKEN}, params=params, timeout=10)
    if r.status_code == 200: 
        return r.json()
    if r.status_code == 401: 
        logger.error("Invalid DISCORD_USER_TOKEN"); sys.exit(1)
    if r.status_code == 403: 
        logger.warning(f"No access to channel {channel_id}"); return []
    if r.status_code == 429:
        wait = r.json().get("retry_after", 5)
        logger.warning(f"Rate limited {wait}s"); time.sleep(float(wait))
        return fetch(channel_id, after, limit)
    logger.error(f"API {r.status_code}: {r.text[:100]}"); return []


# -- Main Execution Pipeline ---------------------------------------------------

def run(loop: bool = False, poll_secs: int = 30):
    if not USER_TOKEN:
        logger.error("DISCORD_USER_TOKEN not set"); sys.exit(1)

    logger.info(f"Discord Alert Trader | mode={MODE} | conf>={CONFIDENCE_MIN}% | fallback=${ORDER_NOTIONAL}")
    logger.info(f"  alloc tiers: 70%={_alloc_pct(70)}% | 80%={_alloc_pct(80)}% | 90%={_alloc_pct(90)}%")
    
    last: dict        = {cid: None for cid in CHANNEL_IDS}
    first             = True
    today             = datetime.now().strftime("%Y%m%d")
    daily_spent       = 0.0  
    bought_today: set = set()  
    log_dir = Path("logs"); log_dir.mkdir(exist_ok=True)

    while True:
        if datetime.now().strftime("%Y%m%d") != today:
            today = datetime.now().strftime("%Y%m%d")
            daily_spent = 0.0
            bought_today.clear()
            logger.info("New trading day -- daily counters reset")

        for cid in CHANNEL_IDS:
            # SAFETY FIX: On the first loop iteration, gather only the single most recent message 
            # to set the baseline anchor ID without processing historical trade logs.
            if first:
                initial_msgs = fetch(cid, limit=1)
                if initial_msgs:
                    last[cid] = initial_msgs[0]["id"]
                continue

            msgs = fetch(cid, after=last[cid]) if last[cid] else []
            if not msgs: 
                continue
            
            msgs = list(reversed(msgs))
            last[cid] = msgs[-1]["id"]

            for msg in msgs:
                content = msg.get("content", "").strip()
                if not content: 
                    continue

                trade = parse_trade(content)
                if not trade or trade.confidence < CONFIDENCE_MIN:
                    continue

                author   = msg.get("author", {}).get("username", "?")
                ticker   = trade.ticker
                is_buy   = trade.action == "BUY"
                bp       = get_buying_power() if is_buy else None
                notional = notional_for(trade.confidence, bp)

                if is_buy:
                    # -- Risk limits validation (BUY orders) -------------------
                    if DEDUPE_TICKER and ticker in bought_today:
                        logger.info(f"  [SKIP DEDUPE] Already bought {ticker} today")
                        continue
                    if daily_spent + notional > MAX_DAILY_SPEND:
                        logger.warning(f"  [SKIP DAILY CAP] ${daily_spent:.0f} spent, cap=${MAX_DAILY_SPEND}")
                        continue
                    
                    open_count = 0
                    if _alpaca:
                        try:
                            open_count = len(_alpaca.get_all_positions())
                        except Exception as e:
                            logger.error(f"  [CRITICAL] Position check failed: {e}. Skipping trade for safety.")
                            continue  # Stop execution loop if risk telemetry is broken
                    
                    if open_count >= MAX_POSITIONS:
                        logger.warning(f"  [SKIP MAX POS] {open_count}/{MAX_POSITIONS} positions open")
                        continue
                else:
                    # -- Validation check (SELL orders) -----------------------
                    pos = None
                    if _alpaca:
                        try:
                            # If it's an option symbol trade, query Alpaca using the full OCC identifier string
                            lookup_symbol = trade.occ if trade.occ else ticker
                            pos = _alpaca.get_open_position(lookup_symbol)
                        except Exception:
                            pass
                    if not pos:
                        logger.info(f"  [SKIP SELL] No asset position found in {trade.occ or ticker}")
                        continue

                # -- Order routing execution engine ---------------------------
                if trade.occ:
                    # Safety guard: Prevent divide-by-zero or default mapping executions
                    if not trade.entry_price or trade.entry_price <= 0:
                        logger.warning(f"  [SKIP TRADE] Option order execution requires a clear premiums layout: {trade}")
                        continue
                    qty = max(1, int(notional // (trade.entry_price * 100)))
                    result = place_option_order(trade.occ, "buy" if is_buy else "sell", qty)
                else:
                    result = place_equity_order(ticker, "buy" if is_buy else "sell", notional)

                logger.info(f"[{trade.confidence}%] {trade} @{author}")

                if is_buy and result.get("status") == "submitted":
                    daily_spent += notional
                    bought_today.add(ticker)

                entry = {
                    "ts": datetime.now(timezone.utc).isoformat(), "channel": cid, "author": author,
                    "trade": str(trade), "occ": trade.occ, "conf": trade.confidence,
                    "notional": notional, "daily_spent": daily_spent,
                    "order": result, "msg": content[:200]
                }
                with open(log_dir / f"discord_trades_{today}.jsonl", "a") as f:
                    f.write(json.dumps(entry) + "\n")

        first = False
        if not loop: 
            break
        time.sleep(poll_secs)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--poll", type=int, default=30)
    args = ap.parse_args()
    run(loop=args.loop, poll_secs=args.poll)
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
try:
    from scripts.discord_parser import parse_trade, Trade
except ModuleNotFoundError:
    # Allow direct execution via `python scripts/discord_api_reader.py`.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.discord_parser import parse_trade, Trade

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# -- Config --------------------------------------------------------------------

_raw = os.getenv("DISCORD_CHANNEL_IDS", "753377655532945558,752750381918060589,769046364738289734,744643208973254726")
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
        if side == "sell":
            # Use actual held qty for sells to avoid "insufficient qty" error
            try:
                pos = _alpaca.get_open_position(ticker)
                qty = float(pos.qty)
            except Exception:
                qty = None
            if qty:
                order = _alpaca.submit_order(MarketOrderRequest(
                    symbol=ticker, qty=qty,
                    side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                ))
                logger.info(f"  SUBMITTED EQUITY SELL {qty} shares {ticker} id={order.id}")
                return {"status": "submitted", "type": "equity", "id": str(order.id), "ticker": ticker}
        order = _alpaca.submit_order(MarketOrderRequest(
            symbol=ticker, notional=notional,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))
        logger.info(f"  SUBMITTED EQUITY {side.upper()} ${notional} {ticker} id={order.id}")
        return {"status": "submitted", "type": "equity", "id": str(order.id), "ticker": ticker}
    except Exception as e:
        err = str(e)
        if "not fractionable" in err and side == "buy":
            # Fallback: use qty=1 for non-fractionable assets
            try:
                order = _alpaca.submit_order(MarketOrderRequest(
                    symbol=ticker, qty=1,
                    side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                ))
                logger.info(f"  SUBMITTED EQUITY BUY 1 share {ticker} (non-fractionable) id={order.id}")
                return {"status": "submitted", "type": "equity", "id": str(order.id), "ticker": ticker}
            except Exception as e2:
                logger.error(f"  ORDER FAILED {ticker} (qty=1 fallback): {e2}")
                return {"status": "error", "ticker": ticker, "error": str(e2)}
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

def run(loop: bool = False, poll_secs: int = 30, history_limit: int = 50):
    if not USER_TOKEN:
        logger.error("DISCORD_USER_TOKEN not set"); sys.exit(1)

    logger.info(f"Discord Alert Trader | mode={MODE} | conf>={CONFIDENCE_MIN}% | fallback=${ORDER_NOTIONAL}")
    logger.info(f"  alloc tiers: 70%={_alloc_pct(70)}% | 80%={_alloc_pct(80)}% | 90%={_alloc_pct(90)}%")
    logger.info(f"  channels: {', '.join(CHANNEL_IDS)}")
    logger.info(f"  startup: processing last {history_limit} messages per channel, then polling every {poll_secs}s")
    
    last: dict        = {cid: None for cid in CHANNEL_IDS}
    today             = datetime.now().strftime("%Y%m%d")
    daily_spent       = 0.0  
    bought_today: set = set()  
    log_dir = Path("logs"); log_dir.mkdir(exist_ok=True)

    while True:
        poll_new_messages = 0
        poll_actionable = 0

        if datetime.now().strftime("%Y%m%d") != today:
            today = datetime.now().strftime("%Y%m%d")
            daily_spent = 0.0
            bought_today.clear()
            logger.info("New trading day -- daily counters reset")

        for cid in CHANNEL_IDS:
            # First pass: fetch recent history and process it immediately.
            # Subsequent passes: fetch only messages newer than last seen.
            if last[cid] is None:
                msgs = fetch(cid, limit=history_limit)
                if not msgs:
                    logger.info(f"  channel {cid}: no messages found")
                    continue
                msgs = list(reversed(msgs))  # oldest first
                last[cid] = msgs[-1]["id"]
                logger.info(f"  channel {cid}: processing {len(msgs)} history messages")
            else:
                msgs = fetch(cid, after=last[cid])
                if not msgs:
                    continue
                msgs = list(reversed(msgs))
                last[cid] = msgs[-1]["id"]

            poll_new_messages += len(msgs)

            for msg in msgs:
                content = msg.get("content", "").strip()
                if not content: 
                    continue

                trade = parse_trade(content)
                if not trade:
                    continue
                # Confidence gate applies to BUYs only — always allow closes/sells
                if trade.action == "BUY" and trade.confidence < CONFIDENCE_MIN:
                    continue
                poll_actionable += 1

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
                        # If message said "Closed" with no OCC, check if there's an open
                        # option position for this underlying ticker and use that OCC instead.
                        if not trade.occ:
                            try:
                                all_positions = _alpaca.get_all_positions()
                                for p in all_positions:
                                    sym = p.symbol
                                    # OCC symbols start with the ticker (left-padded to 6 chars)
                                    if sym.upper().startswith(ticker.upper()) and len(sym) > 6:
                                        trade = type(trade)(
                                            ticker=trade.ticker, action=trade.action,
                                            option_type=trade.option_type, strike=trade.strike,
                                            expiry_str=trade.expiry_str, expiry_date=trade.expiry_date,
                                            occ=sym, entry_price=trade.entry_price,
                                            confidence=trade.confidence, notes=trade.notes,
                                        )
                                        pos = p
                                        logger.info(f"  [CLOSE] Resolved {ticker} close to OCC {sym}")
                                        break
                            except Exception as e:
                                logger.warning(f"  [SELL] Position scan failed: {e}")
                        if not pos:
                            try:
                                lookup_symbol = trade.occ if trade.occ else ticker
                                pos = _alpaca.get_open_position(lookup_symbol)
                            except Exception:
                                pass
                    if not pos:
                        logger.info(f"  [SKIP SELL] No open position found for {trade.occ or ticker}")
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

        if not loop:
            break
        if poll_new_messages == 0:
            logger.info(f"heartbeat: no new Discord messages across {len(CHANNEL_IDS)} channels; next poll in {poll_secs}s")
        elif poll_actionable == 0:
            logger.info(f"heartbeat: {poll_new_messages} new Discord messages, 0 actionable trades; next poll in {poll_secs}s")
        else:
            logger.info(f"heartbeat: {poll_new_messages} new Discord messages, {poll_actionable} actionable trades; next poll in {poll_secs}s")
        time.sleep(poll_secs)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--poll", type=int, default=30)
    ap.add_argument("--history", type=int, default=50,
                    help="Number of recent messages to process on startup (default 50)")
    args = ap.parse_args()
    run(loop=args.loop, poll_secs=args.poll, history_limit=args.history)
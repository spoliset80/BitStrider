"""
Discord Alert Trader
=====================
Polls Discord channels -> parses alerts -> executes market orders on Alpaca.
No local algos, no TI, no scanning. Discord-only signal source.

Required env vars (loaded from .env):
  DISCORD_USER_TOKEN       - Your Discord user token
  PAPER_ALPACA_API_KEY     - Alpaca paper key
  PAPER_ALPACA_API_SECRET  - Alpaca paper secret

Optional:
  DISCORD_CHANNEL_IDS      - Comma-separated channel IDs (default: 3 hardcoded)
  DISCORD_CONFIDENCE_MIN   - Min confidence to execute (default: 70)
  DISCORD_ORDER_NOTIONAL   - $ per trade (default: 500)
  DISCORD_OPTIONS_MODE     - paper or live (default: paper)

Usage:
  apextrader\Scripts\python.exe scripts/discord_api_reader.py --loop
  apextrader\Scripts\python.exe scripts/discord_api_reader.py --loop --poll 60
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, date, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

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
CONFIDENCE_MIN    = int(os.getenv("DISCORD_CONFIDENCE_MIN", "70"))   # minimum score to trade
ORDER_NOTIONAL    = float(os.getenv("DISCORD_ORDER_NOTIONAL", "500")) # base $ per trade
MAX_POSITIONS     = int(os.getenv("DISCORD_MAX_POSITIONS", "10"))     # max open positions at once
MAX_DAILY_SPEND   = float(os.getenv("DISCORD_MAX_DAILY_SPEND", "5000")) # hard $ cap per day
DEDUPE_TICKER     = os.getenv("DISCORD_DEDUPE_TICKER", "true").lower() == "true" # skip repeat buys

# Confidence tiers: multiplier applied to ORDER_NOTIONAL
# conf 70-79 → 0.5x,  80-89 → 1.0x,  90+ → 1.5x
_CONF_TIERS = [
    (90, float(os.getenv("DISCORD_TIER_HIGH_MULT",  "1.5"))),
    (80, float(os.getenv("DISCORD_TIER_MED_MULT",   "1.0"))),
    (70, float(os.getenv("DISCORD_TIER_LOW_MULT",   "0.5"))),
]

def notional_for(conf: int) -> float:
    for threshold, mult in _CONF_TIERS:
        if conf >= threshold:
            return round(ORDER_NOTIONAL * mult, 2)
    return ORDER_NOTIONAL

# -- Alpaca --------------------------------------------------------------------

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

# -- OCC symbol builder --------------------------------------------------------

_MONTHS = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}
# e.g. "July 24th", "Jul 24", "July 24th 2026", "7/24", "7/24/25"
_EXPIRY_NATURAL = re.compile(
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]+(\d{2,4}))?",
    re.I,
)
_EXPIRY_SLASH = re.compile(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?")

def _parse_expiry_date(text: str) -> date | None:
    """Return expiry as a date object, or None if unparseable."""
    m = _EXPIRY_NATURAL.search(text)
    if m:
        mon = _MONTHS[m.group(1).lower()[:3]]
        day = int(m.group(2))
        yr_raw = m.group(3)
        if yr_raw:
            yr = int(yr_raw)
            yr = yr + 2000 if yr < 100 else yr
        else:
            today = date.today()
            yr = today.year
            if date(yr, mon, day) < today:
                yr += 1
        return date(yr, mon, day)
    m = _EXPIRY_SLASH.search(text)
    if m:
        mon, day = int(m.group(1)), int(m.group(2))
        yr_raw = m.group(3)
        if yr_raw:
            yr = int(yr_raw)
            yr = yr + 2000 if yr < 100 else yr
        else:
            today = date.today()
            yr = today.year
            if date(yr, mon, day) < today:
                yr += 1
        return date(yr, mon, day)
    return None

def build_occ(ticker: str, expiry_str: str, opt_type: str, strike: float) -> str | None:
    """Build OCC option symbol e.g. HIMS240724C00042000"""
    d = _parse_expiry_date(expiry_str)
    if not d:
        return None
    cp = "C" if opt_type == "CALL" else "P"
    strike_int = int(round(strike * 1000))
    return f"{ticker.upper()}{d.strftime('%y%m%d')}{cp}{strike_int:08d}"

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

# -- Parser --------------------------------------------------------------------

SKIP = {
    "THE","AND","FOR","ARE","BUT","NOT","YOU","ALL","CAN","WAS","ONE","OUR",
    "OUT","DAY","GET","HAS","HOW","MAY","NEW","NOW","OLD","SEE","TWO","WHO",
    "DID","LET","SAY","SHE","TOO","USE","ATM","OTM","ITM","EOD","EOW","CEO",
    "CFO","IPO","ETF","EPS","GDP","CPI","PPI","FED","SEC","FDA","BOT","TOP",
    "LOW","HIGH","TYPE","MID","LONG","TERM","SWING","STOP","SYMBOL","TARGET",
}

SYMBOL_LINE = re.compile(r"Symbol:\s*\$([A-Z]{1,5})", re.I)
DOLLAR_TKR  = re.compile(r"\$([A-Z]{1,5})\b")
BARE_TKR    = re.compile(r"\b([A-Z]{2,5})\b")
ACTION      = re.compile(r"\b(BUY|SELL|Entered|Exited|Closed|Bought|Sold)\b", re.I)
# Matches: $42C, $42P, $42.5C, CALLS, PUTS, CSP
OPT_TYPE    = re.compile(r"\$(\d+(?:\.\d+)?)(C|P)\b|\b(CALLS?|PUTS?|CSP)\b", re.I)
STRIKE      = re.compile(r"\$(\d{2,4}(?:\.\d+)?)[CP]?\b")
# Matches: 7/24, 7/24/25, July 24th, Jan 2028, Jan-2028
EXPIRY      = re.compile(
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[.\-]?\s*\d{1,2}(?:st|nd|rd|th)?(?:[,\s]+\d{2,4})?)"
    r"|\b(\d{1,2}/\d{1,2}(?:/\d{2,4})?)"
    r"|\b(\d+\s*DTE)\b",
    re.I,
)
AT_PRICE    = re.compile(r"@\s*\$?(\d+(?:\.\d+)?)")


def parse(text: str) -> dict:
    r = {"ticker": None, "action": None, "option_type": None,
         "strike": None, "expiry": None, "price": None}

    # Ticker
    m = SYMBOL_LINE.search(text) or DOLLAR_TKR.search(text)
    if m:
        r["ticker"] = m.group(1).upper()
    else:
        for m2 in BARE_TKR.finditer(text):
            if m2.group(1) not in SKIP:
                r["ticker"] = m2.group(1); break

    # Action
    m = ACTION.search(text)
    if m:
        v = m.group(1).upper()
        r["action"] = "BUY" if v in ("BUY","ENTERED","BOUGHT") else "SELL"

    # Option type — check $42C/$42P first, then CALL/PUT/CSP words
    m = OPT_TYPE.search(text)
    if m:
        if m.group(2):  # matched $42C or $42P form
            r["option_type"] = "CALL" if m.group(2).upper() == "C" else "PUT"
            r["strike"] = float(m.group(1))
        else:           # matched CALLS/PUTS/CSP word
            r["option_type"] = "PUT" if m.group(3).upper().startswith("PUT") else "CALL"

    # Strike (if not already set from $42C pattern)
    if not r["strike"]:
        m = STRIKE.search(text)
        if m: r["strike"] = float(m.group(1))

    # Expiry
    m = EXPIRY.search(text)
    if m: r["expiry"] = (m.group(1) or m.group(2) or m.group(3)).strip()

    # Entry price
    m = AT_PRICE.search(text)
    if m: r["price"] = float(m.group(1))

    return r


def confidence(p: dict) -> int:
    s = 50
    if p.get("strike"):      s += 20
    if p.get("expiry"):      s += 15
    if p.get("price"):       s += 10
    if p.get("action"):      s += 5
    return min(s, 100)

# -- Discord API ---------------------------------------------------------------

def fetch(channel_id: str, after: str = None, limit: int = 50) -> list[dict]:
    params = {"limit": limit}
    if after: params["after"] = after
    r = requests.get(f"{API_BASE}/channels/{channel_id}/messages",
                     headers={"Authorization": USER_TOKEN}, params=params, timeout=10)
    if r.status_code == 200: return r.json()
    if r.status_code == 401: logger.error("Invalid DISCORD_USER_TOKEN"); sys.exit(1)
    if r.status_code == 403: logger.warning(f"No access to channel {channel_id}"); return []
    if r.status_code == 429:
        wait = r.json().get("retry_after", 5)
        logger.warning(f"Rate limited {wait}s"); time.sleep(float(wait))
        return fetch(channel_id, after, limit)
    logger.error(f"API {r.status_code}: {r.text[:100]}"); return []

# -- Main ----------------------------------------------------------------------

def run(loop: bool = False, poll_secs: int = 30):
    if not USER_TOKEN:
        logger.error("DISCORD_USER_TOKEN not set"); sys.exit(1)

    logger.info(f"Discord Alert Trader | mode={MODE} | conf>={CONFIDENCE_MIN}% | base=${ORDER_NOTIONAL}")
    logger.info(f"  risk limits: max_positions={MAX_POSITIONS} | max_daily_spend=${MAX_DAILY_SPEND} | dedupe={DEDUPE_TICKER}")
    logger.info(f"  tiers: 70%={notional_for(70)} | 80%={notional_for(80)} | 90%={notional_for(90)}")
    for cid in CHANNEL_IDS:
        logger.info(f"  channel {cid}")

    last: dict      = {cid: None for cid in CHANNEL_IDS}
    first           = True
    today           = datetime.now().strftime("%Y%m%d")
    daily_spent     = 0.0      # tracks $ deployed today
    bought_today: set = set()  # deduplication: tickers bought this session
    log_dir = Path("logs"); log_dir.mkdir(exist_ok=True)

    while True:
        # Reset daily counters on date rollover
        if datetime.now().strftime("%Y%m%d") != today:
            today = datetime.now().strftime("%Y%m%d")
            daily_spent = 0.0
            bought_today.clear()
            logger.info("New trading day -- daily counters reset")

        for cid in CHANNEL_IDS:
            msgs = fetch(cid, limit=50) if first else (fetch(cid, after=last[cid]) if last[cid] else [])
            if not msgs: continue
            msgs = list(reversed(msgs))
            last[cid] = msgs[-1]["id"]

            for msg in msgs:
                content = msg.get("content", "").strip()
                if not content: continue
                p = parse(content)
                if not p["ticker"] or not p["action"]: continue
                conf = confidence(p)
                if conf < CONFIDENCE_MIN: continue

                author  = msg.get("author", {}).get("username", "?")
                ticker  = p["ticker"]
                is_buy  = p["action"] == "BUY"
                notional = notional_for(conf)

                if is_buy:
                    # -- Risk checks (BUY only) --------------------------------
                    if DEDUPE_TICKER and ticker in bought_today:
                        logger.info(f"  [SKIP DEDUPE] Already bought {ticker} today")
                        continue
                    if daily_spent + notional > MAX_DAILY_SPEND:
                        logger.warning(f"  [SKIP DAILY CAP] ${daily_spent:.0f} spent, cap=${MAX_DAILY_SPEND}")
                        continue
                    # Count open positions
                    open_count = 0
                    if _alpaca:
                        try:
                            open_count = len(_alpaca.get_all_positions())
                        except Exception:
                            pass
                    if open_count >= MAX_POSITIONS:
                        logger.warning(f"  [SKIP MAX POS] {open_count}/{MAX_POSITIONS} positions open")
                        continue
                else:
                    # -- SELL: only if position held ---------------------------
                    pos = None
                    if _alpaca:
                        try:
                            pos = _alpaca.get_open_position(ticker)
                        except Exception:
                            pass
                    if not pos:
                        logger.info(f"  [SKIP SELL] No position in {ticker}")
                        continue

                # -- Route to options or equity order -------------------------
                occ = None
                if p["option_type"] and p["strike"] and p["expiry"]:
                    occ = build_occ(ticker, p["expiry"], p["option_type"], p["strike"])

                if occ:
                    # Options contract order (1 contract = 100 shares)
                    qty = max(1, int(notional // (p["price"] * 100))) if p["price"] else 1
                    result = place_option_order(occ, "buy" if is_buy else "sell", qty)
                    label = f"{ticker} {p['option_type']} ${p['strike']} exp={p['expiry']} x{qty}"
                else:
                    # Equity market order
                    result = place_equity_order(ticker, "buy" if is_buy else "sell", notional)
                    label = f"{ticker} equity ${notional}"

                logger.info(f"[{conf}%] {p['action']} {label} @{author}: {content[:60].replace(chr(10),' ')}")

                if is_buy and result.get("status") == "submitted":
                    daily_spent += notional
                    bought_today.add(ticker)

                entry = {"ts": datetime.now(timezone.utc).isoformat(), "channel": cid, "author": author,
                         "ticker": ticker, "action": p["action"], "conf": conf, "notional": notional,
                         "occ": occ, "option_type": p["option_type"],
                         "daily_spent": daily_spent, "order": result, "msg": content[:200]}
                with open(log_dir / f"discord_trades_{today}.jsonl", "a") as f:
                    f.write(json.dumps(entry) + "\n")

        first = False
        if not loop: break
        time.sleep(poll_secs)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--poll", type=int, default=30)
    args = ap.parse_args()
    run(loop=args.loop, poll_secs=args.poll)

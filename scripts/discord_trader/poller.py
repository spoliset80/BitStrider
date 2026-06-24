"""Discord poll loop — fetches messages and dispatches to ChannelRouter."""
from __future__ import annotations
import json
import logging
import sys
import time
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Dict, Optional
from zoneinfo import ZoneInfo

import requests

from .config import Config
from .broker import Broker
from .risk   import RiskManager
from .router import ChannelRouter

logger = logging.getLogger(__name__)

_API_BASE = "https://discord.com/api/v10"
_CURSOR_FILE = Path("logs") / "discord_cursors.json"


def _is_market_open(cfg: Config) -> bool:
    """True if now is within the configured market-hours window (Mon–Fri, ET)."""
    if not cfg.market_hours_only:
        return True
    try:
        tz  = ZoneInfo(cfg.market_tz)
        now = datetime.now(tz)
        if now.weekday() >= 5:            # Sat/Sun
            return False
        oh, om = map(int, cfg.market_open.split(":"))
        ch, cm = map(int, cfg.market_close.split(":"))
        return dtime(oh, om) <= now.time() < dtime(ch, cm)
    except Exception as e:
        logger.warning(f"market-hours check failed ({e}); defaulting to OPEN")
        return True


def _load_cursors() -> Dict[str, str]:
    if _CURSOR_FILE.exists():
        try:
            return json.loads(_CURSOR_FILE.read_text())
        except Exception as e:
            logger.warning(f"could not read cursor file: {e}")
    return {}


def _save_cursors(cursors: Dict[str, Optional[str]]):
    try:
        _CURSOR_FILE.parent.mkdir(exist_ok=True)
        _CURSOR_FILE.write_text(json.dumps({k: v for k, v in cursors.items() if v}))
    except Exception as e:
        logger.warning(f"could not write cursor file: {e}")


def _fetch(channel_id: str, token: str, after: Optional[str] = None,
           limit: int = 50) -> list:
    params = {"limit": limit}
    if after:
        params["after"] = after
    try:
        r = requests.get(
            f"{_API_BASE}/channels/{channel_id}/messages",
            headers={"Authorization": token},
            params=params,
            timeout=15,
        )
    except requests.exceptions.Timeout:
        logger.warning(f"Discord request timed out for channel {channel_id} — skipping poll")
        return []
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"Discord connection error for channel {channel_id}: {e} — skipping poll")
        return []
    if r.status_code == 200:
        return r.json()
    if r.status_code == 401:
        logger.error("Invalid DISCORD_USER_TOKEN"); sys.exit(1)
    if r.status_code == 403:
        logger.warning(f"No access to channel {channel_id}"); return []
    if r.status_code == 429:
        wait = r.json().get("retry_after", 5)
        logger.warning(f"Rate limited — waiting {wait}s")
        time.sleep(float(wait))
        return _fetch(channel_id, token, after, limit)
    logger.error(f"API {r.status_code}: {r.text[:100]}")
    return []


def run(config: Config, loop: bool = False,
        poll_secs: int = 60, history_limit: int = 50):
    """Main entry point — create all dependencies and start polling."""
    if not config.user_token:
        logger.error("DISCORD_USER_TOKEN not set"); sys.exit(1)

    broker = Broker(config.alpaca_key, config.alpaca_secret, config.alpaca_paper)
    risk   = RiskManager(
        max_positions  = config.max_positions,
        max_daily_spend= config.max_daily_spend,
        dedupe_ticker  = config.dedupe_ticker,
        confidence_min = config.confidence_min,
        alloc_low_pct  = config.alloc_low_pct,
        alloc_med_pct  = config.alloc_med_pct,
        alloc_high_pct = config.alloc_high_pct,
        order_notional = config.order_notional,
    )
    router = ChannelRouter(config, broker, risk, Path("logs"))

    channel_ids = list(config.channel_types.keys())

    logger.info(f"Discord Alert Trader | mode={config.mode} | conf>={config.confidence_min}%")
    logger.info(f"  channels: {', '.join(f'{cid}({t})' for cid,t in config.channel_types.items())}")
    if config.market_hours_only:
        logger.info(f"  orders gated to {config.market_open}-{config.market_close} {config.market_tz} (Mon-Fri); polling 24/7")
    else:
        logger.info(f"  market-hours gating disabled — orders allowed anytime")

    # Load persisted cursors (survive restarts). Channels with no saved cursor
    # get initialised from history on their first pass (no dispatch of old msgs).
    saved = _load_cursors()
    last: Dict[str, Optional[str]] = {cid: saved.get(cid) for cid in channel_ids}
    for cid in channel_ids:
        if last[cid]:
            logger.info(f"  channel {cid}({config.channel_types[cid]}): resumed at cursor {last[cid]}")

    today = datetime.now().strftime("%Y%m%d")
    market_was_open: Optional[bool] = None

    while True:
        # Daily rollover
        if datetime.now().strftime("%Y%m%d") != today:
            today = datetime.now().strftime("%Y%m%d")
            router.reset_daily()
            logger.info("new trading day — risk counters and SPX state reset")

        # Market-hours gate (single chokepoint via broker)
        market_open = _is_market_open(config)
        if market_open != market_was_open:
            broker.set_trading_enabled(market_open)
            logger.info(f"market {'OPEN — orders enabled' if market_open else 'CLOSED — orders gated (state still tracked)'}")
            market_was_open = market_open

        poll_msgs = 0
        poll_acted = 0
        cursor_changed = False

        for cid in channel_ids:
            if last[cid] is None:
                # First-ever run for this channel: set cursor from history, don't dispatch old msgs
                history = _fetch(cid, config.user_token, limit=history_limit)
                if history:
                    last[cid] = max(history, key=lambda m: m["id"])["id"]
                    logger.info(f"  channel {cid}({config.channel_types[cid]}): "
                                f"cursor initialised at {last[cid]} ({len(history)} history msgs skipped)")
                else:
                    last[cid] = "0"
                cursor_changed = True

            msgs = _fetch(cid, config.user_token, after=last[cid])
            if not msgs:
                continue
            msgs = list(reversed(msgs))
            last[cid] = msgs[-1]["id"]
            cursor_changed = True

            poll_msgs += len(msgs)
            for msg in msgs:
                result = router.dispatch(cid, msg)
                if result and result.get("status") == "submitted":
                    poll_acted += 1

        if cursor_changed:
            _save_cursors(last)

        if not loop:
            break

        gate = "" if market_open else " [market closed]"
        if poll_msgs == 0:
            logger.info(f"heartbeat: no new messages across {len(channel_ids)} channels{gate}; "
                        f"next poll in {poll_secs}s")
        elif poll_acted == 0:
            logger.info(f"heartbeat: {poll_msgs} new messages, 0 orders placed{gate}; "
                        f"next poll in {poll_secs}s")
        else:
            logger.info(f"heartbeat: {poll_msgs} messages, {poll_acted} orders placed{gate}; "
                        f"next poll in {poll_secs}s")

        time.sleep(poll_secs)

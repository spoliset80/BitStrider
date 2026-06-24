"""Discord poll loop — fetches messages and dispatches to ChannelRouter."""
from __future__ import annotations
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import requests

from .config import Config
from .broker import Broker
from .risk   import RiskManager
from .router import ChannelRouter

logger = logging.getLogger(__name__)

_API_BASE = "https://discord.com/api/v10"


def _fetch(channel_id: str, token: str, after: Optional[str] = None,
           limit: int = 50) -> list:
    params = {"limit": limit}
    if after:
        params["after"] = after
    r = requests.get(
        f"{_API_BASE}/channels/{channel_id}/messages",
        headers={"Authorization": token},
        params=params,
        timeout=10,
    )
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
    logger.info(f"  startup: processing last {history_limit} msgs per channel, polling every {poll_secs}s")

    last: Dict[str, Optional[str]] = {cid: None for cid in channel_ids}
    today = datetime.now().strftime("%Y%m%d")

    while True:
        # Daily rollover
        if datetime.now().strftime("%Y%m%d") != today:
            today = datetime.now().strftime("%Y%m%d")
            router.reset_daily()

        poll_msgs = 0
        poll_acted = 0

        for cid in channel_ids:
            if last[cid] is None:
                msgs = _fetch(cid, config.user_token, limit=history_limit)
                if not msgs:
                    logger.info(f"  channel {cid}: no messages")
                    continue
                msgs = list(reversed(msgs))
                last[cid] = msgs[-1]["id"]
                logger.info(f"  channel {cid}({config.channel_types[cid]}): "
                            f"processing {len(msgs)} history messages")
            else:
                msgs = _fetch(cid, config.user_token, after=last[cid])
                if not msgs:
                    continue
                msgs = list(reversed(msgs))
                last[cid] = msgs[-1]["id"]

            poll_msgs += len(msgs)
            for msg in msgs:
                result = router.dispatch(cid, msg)
                if result and result.get("status") == "submitted":
                    poll_acted += 1

        if not loop:
            break

        if poll_msgs == 0:
            logger.info(f"heartbeat: no new messages across {len(channel_ids)} channels; "
                        f"next poll in {poll_secs}s")
        elif poll_acted == 0:
            logger.info(f"heartbeat: {poll_msgs} new messages, 0 orders placed; "
                        f"next poll in {poll_secs}s")
        else:
            logger.info(f"heartbeat: {poll_msgs} messages, {poll_acted} orders placed; "
                        f"next poll in {poll_secs}s")

        time.sleep(poll_secs)

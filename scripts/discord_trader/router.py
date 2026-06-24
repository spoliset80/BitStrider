"""ChannelRouter — dispatches each message to the correct strategy handler."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .config   import Config
from .broker   import Broker
from .risk     import RiskManager
from .parsers  import parse_trade
from .parsers.spx import SpxStateMachine
from .parsers.breakout import parse_breakout
from .strategies import handle_equity, handle_options, handle_spx_action, handle_breakout

logger = logging.getLogger(__name__)


class ChannelRouter:
    """
    Holds one SpxStateMachine per SPX channel.
    Routes each (channel_id, message) to the right strategy.
    Logs executed trades to JSONL.
    """

    def __init__(self, config: Config, broker: Broker, risk: RiskManager, log_dir: Path):
        self.config   = config
        self.broker   = broker
        self.risk     = risk
        self.log_dir  = log_dir
        self.log_dir.mkdir(exist_ok=True)

        # One state machine per SPX channel
        self._spx_machines: Dict[str, SpxStateMachine] = {
            cid: SpxStateMachine()
            for cid, ctype in config.channel_types.items()
            if ctype == "spx"
        }

    def reset_daily(self):
        """Call at start of each trading day."""
        self.risk.reset_daily() if hasattr(self.risk, "reset_daily") else None  # type: ignore
        for sm in self._spx_machines.values():
            sm.reset_daily()

    def dispatch(self, channel_id: str, msg: dict) -> Optional[dict]:
        """
        Process one Discord message dict.
        Returns order result or None if skipped/not actionable.
        """
        content = msg.get("content", "").strip()
        author  = msg.get("author", {}).get("username", "?")
        ctype   = self.config.channel_types.get(channel_id, "options")

        # ── Breakout channel (rich embeds, no plain content) ──────────────────
        if ctype == "breakout":
            signal = parse_breakout(msg)
            if not signal:
                return None
            logger.info(f"  MSG [breakout] @{author}: {signal.ticker} "
                        f"entry=${signal.entry} stop=${signal.stop}")
            result = handle_breakout(signal, self.broker, self.risk, self.config)
            if result and result.get("status") == "submitted":
                label = f"BREAKOUT {signal.ticker} {result.get('occ','')}"
                logger.info(f"  [SWING] {label} @{author}")
                self._log(channel_id, author, signal.raw, label, result)
            else:
                status = (result or {}).get("status", "no-op")
                reason = (result or {}).get("reason", "")
                logger.info(f"    -> {signal.ticker} breakout not placed "
                            f"(status={status}{': ' + reason if reason else ''})")
            return result

        if not content:
            return None

        # Always show what arrived, even when no action is taken.
        preview = content.replace("\n", " ")
        if len(preview) > 160:
            preview = preview[:157] + "..."
        logger.info(f"  MSG [{ctype}] @{author}: {preview}")

        bp      = self.broker.buying_power() if ctype in ("options", "equity") else None
        result  = None

        # ── SPX channel ───────────────────────────────────────────────────────
        if ctype == "spx":
            sm     = self._spx_machines.get(channel_id)
            if not sm:
                return None
            action = sm.on_message(content)
            if action:
                result = handle_spx_action(
                    action, self.broker,
                    spx_notional=self.config.spx_notional,
                    stop_pct=self.config.spx_stop_pct,
                )
                if result:
                    self._log(channel_id, author, content, f"SPX:{action.kind}", result)
            else:
                logger.info("    -> no SPX action (waiting for setup/entry/exit trigger)")
            return result

        # ── Options / equity channels ─────────────────────────────────────────
        trade = parse_trade(content)
        if not trade:
            logger.info("    -> not a trade signal (parse skip)")
            return None

        is_buy = trade.action == "BUY"

        # Confidence gate — BUYs on options channels only
        # (equity messages never have strike/expiry so scoring is structurally lower)
        if ctype == "options" and is_buy and trade.confidence < self.config.confidence_min:
            logger.info(f"    -> {trade.action} {trade.ticker} skipped: "
                        f"conf {trade.confidence}% < min {self.config.confidence_min}%")
            return None

        if ctype == "equity":
            result = handle_equity(trade, self.broker, self.risk, bp)
        else:  # options (default)
            result = handle_options(trade, self.broker, self.risk, bp)

        if result and result.get("status") == "submitted":
            label = f"{trade.action} {trade.ticker}"
            if trade.occ:
                label += f" OCC={trade.occ}"
            logger.info(f"  [{trade.confidence}%] {label} @{author}")
            self._log(channel_id, author, content, label, result)
        else:
            status = (result or {}).get("status", "no-op")
            reason = (result or {}).get("reason", "")
            logger.info(f"    -> {trade.action} {trade.ticker} not placed "
                        f"(status={status}{': ' + reason if reason else ''})")

        return result

    def _log(self, channel_id: str, author: str, content: str, label: str, result: dict):
        today = datetime.now().strftime("%Y%m%d")
        entry = {
            "ts":       datetime.now(timezone.utc).isoformat(),
            "channel":  channel_id,
            "author":   author,
            "label":    label,
            "result":   result,
            "msg":      content[:200],
        }
        with open(self.log_dir / f"discord_trades_{today}.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")

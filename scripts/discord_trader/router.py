"""ChannelRouter — dispatches each message to the correct strategy handler."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Dict, Optional

from .config   import Config
from .broker   import Broker
from .risk     import RiskManager
from .parsers  import parse_trade
from .parsers.spx import SpxStateMachine
from .parsers.breakout import parse_breakout
from .strategy import score_signal
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
                    action, self.broker, self.config,
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

        # Technical gate — BUYs are graded on price action, not on parse completeness.
        if is_buy and self.config.use_technical_score:
            score, reasons = score_signal(trade, self.broker)
            for r in reasons:
                logger.info(f"      · {r}")
            logger.info(f"    [SCORE] {trade.ticker} technical={score}% "
                        f"(parse={trade.confidence}%)")
            trade.confidence = score

        if is_buy and trade.confidence < self.config.confidence_min:
            logger.info(f"    -> {trade.action} {trade.ticker} skipped: "
                        f"score {trade.confidence}% < min {self.config.confidence_min}%")
            return None

        if ctype == "equity":
            result = handle_equity(trade, self.broker, self.risk, bp, config=self.config)
        else:  # options (default)
            result = handle_options(trade, self.broker, self.risk, bp,
                                     price_above_last_pct=self.config.price_above_last_pct,
                                     config=self.config)

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
        self._track_position(label, result)

    # ── Entry-date tracking (drives the max-hold exit) ────────────────────────

    def _entry_file(self) -> Path:
        return self.log_dir / "open_entries.json"

    def _load_entries(self) -> dict:
        try:
            return json.loads(self._entry_file().read_text())
        except Exception:
            return {}

    def _save_entries(self, data: dict):
        try:
            self._entry_file().write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning(f"could not write entry file: {e}")

    def _track_position(self, label: str, result: dict):
        """Record open date on BUY, clear it on SELL, so age can be measured later."""
        symbol = result.get("occ") or result.get("ticker")
        if not symbol:
            return
        entries = self._load_entries()
        if label.startswith("BUY") or label.startswith("BREAKOUT"):
            entries.setdefault(symbol, datetime.now(timezone.utc).date().isoformat())
        else:
            entries.pop(symbol, None)
            for leg in result.get("legs", []):
                entries.pop(leg.get("occ", ""), None)
        self._save_entries(entries)

    def close_stale_positions(self) -> int:
        """Close positions older than config.max_hold_days. Returns count closed."""
        max_days = self.config.max_hold_days
        if not max_days or max_days <= 0:
            return 0

        entries = self._load_entries()
        if not entries:
            return 0

        today  = datetime.now(timezone.utc).date()
        closed = 0
        for p in (self.broker.get_all_positions() or []):
            opened = entries.get(p.symbol)
            if not opened:
                continue
            age = (today - date.fromisoformat(opened)).days
            if age < max_days:
                continue
            qty = max(1, int(float(p.qty)))
            is_option = len(p.symbol) > 6 and p.symbol[-9] in "CP"
            logger.info(f"  [MAX HOLD] {p.symbol} open {age}d >= {max_days}d — closing")
            result = (self.broker.sell_option(p.symbol, qty) if is_option
                      else self.broker.sell_equity(p.symbol))
            if result.get("status") == "submitted":
                entries.pop(p.symbol, None)
                closed += 1
        if closed:
            self._save_entries(entries)
        return closed

"""SPX channel parser — extracts setup/enter/exit signals via state machine."""
from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum, auto

logger = logging.getLogger(__name__)


class SpxState(Enum):
    IDLE    = auto()   # no active setup
    PENDING = auto()   # setup posted, waiting for "Entered"
    ACTIVE  = auto()   # position is open


@dataclass
class SpxSignal:
    direction:   str            # "LONG" | "SHORT"
    trigger:     float          # price level to enter
    target:      Optional[float]
    stop:        Optional[float]
    raw_text:    str            # original setup message


@dataclass
class SpxAction:
    """Instruction returned to the executor."""
    kind:        str            # "ENTER" | "EXIT" | "UPDATE_STOP"
    direction:   Optional[str] = None
    signal:      Optional[SpxSignal] = None
    new_stop:    Optional[float] = None
    exit_price:  Optional[float] = None


# ── Regexes ───────────────────────────────────────────────────────────────────

_RE_SETUP    = re.compile(r"SPX\s+(long|short)\s+(above|below)\s+([\d.]+)", re.I)
_RE_TARGET   = re.compile(r"target[:\s]+([\d.]+)", re.I)
_RE_STOP     = re.compile(r"stop[:\s]+([\d.]+)", re.I)
_RE_ENTER    = re.compile(r"\b(entered|triggered)\b", re.I)
_RE_EXIT     = re.compile(r"\b(out|target\s*hit|stopped\s*out|take\s*profit|hope\s*you.*exit|hope\s*you.*close)\b", re.I)
_RE_STOP_MV  = re.compile(r"\b(move|moved|moving)\s+stop\s+(?:to\s+)?([\d.]+)", re.I)
_RE_EXIT_PX  = re.compile(r"@\s*([\d.]+)", re.I)
_RE_NOISE    = re.compile(r"(support|resistance|levels|intraday|Market Net Flow)", re.I)


class SpxStateMachine:
    """
    One instance per SPX channel.

    Call  on_message(content) → Optional[SpxAction]
    
    State transitions:
      IDLE    + setup msg    → PENDING  (store signal)
      PENDING + new setup    → PENDING  (replace signal — latest wins)
      PENDING + "Entered"   → ACTIVE   → returns SpxAction(ENTER)
      ACTIVE  + stop_move    → ACTIVE   → returns SpxAction(UPDATE_STOP)
      ACTIVE  + exit msg     → IDLE     → returns SpxAction(EXIT)
      ACTIVE  + new setup    → PENDING  (position still open — caller handles close)
    """

    def __init__(self):
        self.state:   SpxState            = SpxState.IDLE
        self.pending: Optional[SpxSignal] = None   # latest setup not yet entered
        self.active:  Optional[SpxSignal] = None   # signal that was entered

    def reset_daily(self):
        """Call at start of each trading day."""
        self.state   = SpxState.IDLE
        self.pending = None
        self.active  = None
        logger.info("  [SPX] State machine reset for new day")

    def on_message(self, content: str) -> Optional[SpxAction]:
        content = content.strip()
        if not content or _RE_NOISE.search(content):
            return None

        # ── Setup ─────────────────────────────────────────────────────────────
        m = _RE_SETUP.search(content)
        if m:
            direction = m.group(1).upper()
            trigger   = float(m.group(3))
            tgt_m     = _RE_TARGET.search(content)
            stp_m     = _RE_STOP.search(content)
            sig = SpxSignal(
                direction = direction,
                trigger   = trigger,
                target    = float(tgt_m.group(1)) if tgt_m else None,
                stop      = float(stp_m.group(1)) if stp_m else None,
                raw_text  = content[:120],
            )
            self.pending = sig
            if self.state == SpxState.IDLE:
                self.state = SpxState.PENDING
            # If ACTIVE, new setup posted — keep active position open, update pending
            logger.info(f"  [SPX] SETUP {direction} trigger={trigger} "
                        f"target={sig.target} stop={sig.stop}")
            return None  # setup itself doesn't trigger execution

        # ── Entry confirmation ────────────────────────────────────────────────
        if _RE_ENTER.search(content):
            if self.state == SpxState.PENDING and self.pending:
                self.active = self.pending
                self.pending = None
                self.state   = SpxState.ACTIVE
                logger.info(f"  [SPX] ENTER confirmed → "
                            f"{self.active.direction} {self.active.trigger}")
                return SpxAction(kind="ENTER", direction=self.active.direction,
                                 signal=self.active)
            logger.debug("  [SPX] 'Entered' but no pending setup — ignoring")
            return None

        # ── Stop move ─────────────────────────────────────────────────────────
        sm = _RE_STOP_MV.search(content)
        if sm and self.state == SpxState.ACTIVE:
            new_stop = float(sm.group(2))
            if self.active:
                self.active.stop = new_stop
            logger.info(f"  [SPX] STOP moved to {new_stop}")
            return SpxAction(kind="UPDATE_STOP", new_stop=new_stop)

        # ── Exit ─────────────────────────────────────────────────────────────
        if _RE_EXIT.search(content) and self.state == SpxState.ACTIVE:
            px_m = _RE_EXIT_PX.search(content)
            exit_price = float(px_m.group(1)) if px_m else None
            direction  = self.active.direction if self.active else None
            logger.info(f"  [SPX] EXIT signal price={exit_price}")
            self.state  = SpxState.IDLE
            self.active = None
            return SpxAction(kind="EXIT", direction=direction, exit_price=exit_price)

        return None

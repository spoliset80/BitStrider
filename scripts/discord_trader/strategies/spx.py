"""SPX strategy — execute SPY 0DTE options on SPX channel signals."""
from __future__ import annotations
import logging
from typing import Optional

from ..parsers.spx import SpxAction

logger = logging.getLogger(__name__)

# Track the active SPY OCC position symbol so we can close it on EXIT
_active_spy_occ: Optional[str] = None


def handle_spx_action(action: SpxAction, broker, spx_notional: float,
                       stop_pct: float = 50.0, use_limit: bool = False) -> Optional[dict]:
    """
    Execute a SPY 0DTE order based on an SpxAction from the state machine.

    ENTER  → find ATM SPY 0DTE contract, place market/limit buy
    EXIT   → close active SPY position
    UPDATE_STOP → log only (stop managed manually or via bracket)
    """
    global _active_spy_occ

    if action.kind == "UPDATE_STOP":
        logger.info(f"  [SPX] Stop updated to {action.new_stop} — no order change")
        return None

    if action.kind == "EXIT":
        if not _active_spy_occ:
            logger.info("  [SPX] EXIT signal but no active SPY position tracked")
            return None
        pos = broker.get_position(_active_spy_occ)
        if not pos:
            # Also scan all open option positions
            for p in (broker.get_all_positions() or []):
                if p.symbol.upper().startswith("SPY") and len(p.symbol) > 6:
                    _active_spy_occ = p.symbol
                    pos = p
                    break
        if not pos:
            logger.info(f"  [SPX] EXIT — no open SPY position found")
            _active_spy_occ = None
            return None
        qty = max(1, int(float(pos.qty)))
        result = broker.sell_option(_active_spy_occ, qty)
        if result.get("status") == "submitted":
            logger.info(f"  [SPX] Closed SPY position {_active_spy_occ}")
            _active_spy_occ = None
        return result

    if action.kind == "ENTER":
        direction = action.direction  # "LONG" | "SHORT"
        signal    = action.signal

        # Find ATM SPY 0DTE contract
        occ = broker.find_spy_0dte_contract(direction)
        if not occ:
            logger.warning("  [SPX] Could not find SPY 0DTE contract — skipping")
            return None

        # Calculate qty from notional (need ask price — use $2 estimate if unavailable)
        qty = max(1, int(spx_notional // 200))  # default $2/contract estimate

        # Get current ask price for limit order
        limit_px = None
        if use_limit:
            try:
                import requests as req_lib
                c = broker._client
                headers = {
                    "APCA-API-KEY-ID":     c._api_key,
                    "APCA-API-SECRET-KEY": c._secret_key,
                }
                base = "https://paper-api.alpaca.markets" if broker._paper else "https://api.alpaca.markets"
                r = req_lib.get(f"{base}/v2/options/contracts/{occ}", headers=headers, timeout=5)
                if r.ok:
                    close_px = r.json().get("close_price")
                    if close_px:
                        limit_px = float(close_px)
                        logger.info(f"  [SPX] Using last close price as limit: ${limit_px}")
            except Exception as e:
                logger.warning(f"  [SPX] Could not get limit price: {e}")

        result = broker.buy_option(occ, qty, limit_price=limit_px)
        if result.get("status") == "submitted":
            _active_spy_occ = occ
            tgt = signal.target if signal else "?"
            stp = signal.stop   if signal else "?"
            logger.info(f"  [SPX] ENTERED {direction} via {occ} qty={qty} "
                        f"spx_target={tgt} spx_stop={stp}")
        return result

    return None

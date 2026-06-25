"""SPX strategy — execute SPY 0DTE options on SPX channel signals."""
from __future__ import annotations
import logging
from typing import Optional

from ..parsers.spx import SpxAction

logger = logging.getLogger(__name__)

# Track the active SPY OCC position symbol so we can close it on EXIT
_active_spy_occ: Optional[str] = None


def handle_spx_action(action: SpxAction, broker, config) -> Optional[dict]:
    """
    Execute a SPY 0DTE order based on an SpxAction from the state machine.

    ENTER  → find ATM SPY 0DTE contract, size by available BP, place buy +
             immediately attach a GTC trailing stop
    EXIT   → close active SPY position (broker-truth based — restart safe)
    UPDATE_STOP → log only
    """
    global _active_spy_occ

    if action.kind == "UPDATE_STOP":
        logger.info(f"  [SPX] Stop updated to {action.new_stop} — no order change")
        return None

    if action.kind == "EXIT":
        target_occ = _active_spy_occ
        pos = broker.get_position(target_occ) if target_occ else None
        if not pos:
            for p in (broker.get_all_positions() or []):
                sym = p.symbol.upper()
                if sym.startswith("SPY") and len(sym) > 6:
                    target_occ = p.symbol
                    pos = p
                    break
        if not pos:
            logger.info("  [SPX] EXIT signal but no open SPY option position found")
            _active_spy_occ = None
            return None
        qty = max(1, int(float(pos.qty)))
        result = broker.sell_option(target_occ, qty)
        if result.get("status") == "submitted":
            logger.info(f"  [SPX] Closed SPY position {target_occ} qty={qty}")
            _active_spy_occ = None
        return result

    if action.kind == "ENTER":
        direction = action.direction  # "LONG" | "SHORT"
        signal    = action.signal

        occ = broker.find_spy_0dte_contract(direction)
        if not occ:
            logger.warning("  [SPX] Could not find SPY 0DTE contract — skipping")
            return None

        # ── Size by real buying power ─────────────────────────────────────────
        # Fetch current ask/close price for the contract
        limit_px = None
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
                    limit_px = round(float(close_px) * 1.05, 2)  # 5% over last close
        except Exception as e:
            logger.warning(f"  [SPX] Could not get contract price: {e}")

        # Use real BP, fall back to config.spx_notional
        bp = broker.buying_power()
        if bp and bp > 0 and limit_px:
            notional = bp * (config.spx_bp_pct / 100.0)
            qty = max(1, int(notional // (limit_px * 100)))
            logger.info(f"  [SPX] BP=${bp:,.0f} → deploying {config.spx_bp_pct}% "
                        f"= ${notional:,.0f}, premium≈${limit_px}, qty={qty}")
        elif limit_px:
            notional = config.spx_notional
            qty = max(1, int(notional // (limit_px * 100)))
            logger.info(f"  [SPX] BP unavailable — using notional ${notional}, qty={qty}")
        else:
            qty = max(1, int(config.spx_notional // 200))
            logger.info(f"  [SPX] No price data — fallback qty={qty}")

        result = broker.buy_option(occ, qty, limit_price=limit_px)
        if result.get("status") == "submitted":
            _active_spy_occ = occ
            tgt = signal.target if signal else "?"
            stp = signal.stop   if signal else "?"
            logger.info(f"  [SPX] ENTERED {direction} via {occ} qty={qty} "
                        f"spx_target={tgt} spx_stop={stp}")
            # Trailing stop to protect the position
            stop_result = broker.place_option_trailing_stop(
                occ, qty, trail_pct=config.spx_stop_pct
            )
            if stop_result.get("status") == "submitted":
                logger.info(f"  [SPX] Trailing stop {config.spx_stop_pct}% GTC placed for {occ}")
            else:
                logger.warning(f"  [SPX] *** NO STOP on {occ}: "
                               f"{stop_result.get('error','?')} — close manually ***")
        return result

    return None


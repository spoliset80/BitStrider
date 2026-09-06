"""Self-check for order normalization & classification (2026-09-03).

SNOW 9/3 post-mortem root cause #1: exit paths treated "symbol has an open
order" as proof of protection. In reality that order was the GTC trailing stop
reserving the only share, so three exit paths kept submitting closes that
Alpaca rejected 40310000 "insufficient qty available ... held_for_orders".

classify_symbol_order() must tell these apart: entry/staged/reentry orders and
wrong-side orders are NEVER protection; only GTC trailing stops on the
position-closing side are, and a partially-filled one leaves the rest exposed.

Run with:
  python scripts/test_order_classification.py
No network calls -- pure decision functions on stub order objects.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.execution.enhanced import (
    _normalize_order_view, classify_symbol_order, _pending_close_client_id,
    ActiveOrderView, CloseResult,
)

# --- normalization: enum-tolerant, stub-tolerant, fails safe ---
enum_side = SimpleNamespace(value="sell")
raw = SimpleNamespace(
    id="o1", symbol="SNOW", side=enum_side, order_type="trailing_stop",
    time_in_force=SimpleNamespace(value="GTC"), status=SimpleNamespace(value="accepted"),
    qty="1", filled_qty=None, client_order_id="apex-entry-TopList-SNOW-123",
)
v = _normalize_order_view(raw)
assert v is not None and v.side == "sell" and v.time_in_force == "gtc", v
assert v.qty == 1.0 and v.filled_qty == 0.0 and v.remaining_qty == 1.0
assert v.client_order_id == "apex-entry-TopList-SNOW-123"

assert _normalize_order_view(SimpleNamespace(id="", symbol="X")) is None, "no id -> None"
assert _normalize_order_view(None) is None
assert _normalize_order_view(SimpleNamespace(id="o2")) is None, "no symbol -> None"
broken = SimpleNamespace(id=object(), symbol=object(), side=object(), order_type=object(),
                         time_in_force=object(), status=object(), qty="abc", client_order_id=object())
assert _normalize_order_view(broken) is None or isinstance(_normalize_order_view(broken), ActiveOrderView), \
    "garbage fields must either normalize safely or return None -- never raise"

# --- classification: client-order-id prefixes are authoritative ---
def _view(**kw):
    base = dict(order_id="o", symbol="SNOW", side="sell", order_type="trailing_stop",
                time_in_force="gtc", status="accepted", qty=1.0, filled_qty=0.0,
                client_order_id="")
    base.update(kw)
    return ActiveOrderView(**base)

LONG_QTY, SHORT_QTY = 1, -1

# The real 9/3 SNOW protective stop: sell GTC trail on a 1-share long.
assert classify_symbol_order(_view(), LONG_QTY) == "valid_protection"
# ...and NOT protection for a short (wrong side -- a sell ADDS to a short's cover, hmm: sell on a short is an ENTRY side)
assert classify_symbol_order(_view(), SHORT_QTY) == "wrong_side"
# Mirror: buy GTC trail covers a short.
assert classify_symbol_order(_view(side="buy"), SHORT_QTY) == "valid_protection"
assert classify_symbol_order(_view(side="buy"), LONG_QTY) == "wrong_side"

# Entry/staged/reentry ids are NEVER protection, even on the closing side.
assert classify_symbol_order(_view(client_order_id="apex-entry-TopList-SNOW-1"), LONG_QTY) == "entry"
assert classify_symbol_order(_view(client_order_id="apex-staged-SNOW-1"), LONG_QTY) == "staged_entry"
assert classify_symbol_order(_view(client_order_id="apex-reentry-trail-SNOW-1"), LONG_QTY) == "reentry"
assert classify_symbol_order(_view(client_order_id="apex-close-ema9-SNOW-1"), LONG_QTY) == "pending_close"

# Inactive terminal statuses are not actionable.
for dead in ("filled", "canceled", "cancelled", "expired", "rejected"):
    assert classify_symbol_order(_view(status=dead), LONG_QTY) == "unknown", dead

# Partial protection: a half-filled trail leaves half the position uncovered.
assert classify_symbol_order(_view(qty=1.0, filled_qty=0.5), 2) == "partial_protection"
assert classify_symbol_order(_view(qty=2.0, filled_qty=0.0), 2) == "valid_protection"

# DAY trailing-stop entry (the 0.25% trailing buy) is NOT protection: DAY, not GTC.
assert classify_symbol_order(_view(time_in_force="day"), LONG_QTY) == "pending_close_legacy"

# A plain sell limit on a long = closing intent (legacy close), not protection.
assert classify_symbol_order(_view(order_type="limit", time_in_force="day"), LONG_QTY) == "pending_close_legacy"

# No position -> nothing is protection or a close.
assert classify_symbol_order(_view(), 0) == "unknown"
assert classify_symbol_order(None, LONG_QTY) == "unknown"

# --- pending-close client ids: stable prefix, sanitized reason ---
coid = _pending_close_client_id("SNOW", "EMA9 EXIT!! -- trailing EMA9 stop hit")
assert coid.startswith("apex-close-") and "-SNOW-" in coid, coid
# reason token is sanitized AND capped at 20 chars; no whitespace/punctuation survives
assert all(ch.isalnum() or ch == "-" for ch in coid), coid
assert "!!" not in coid and " " not in coid
coid2 = _pending_close_client_id("SNOW", "software-sl")
assert coid2.startswith("apex-close-software-sl-SNOW-"), coid2

# --- CloseResult surface ---
r = CloseResult("submitted", "SNOW", "abc", 1, 1, "ok")
assert r.state == "submitted" and r.order_id == "abc" and r.requested_qty == 1

print("OK: order views normalize enum/stub-tolerantly and classify protection vs entry vs close -- "
      "a GTC trail on the closing side is the ONLY thing that counts as protection, and client-order-id "
      "prefixes are authoritative")
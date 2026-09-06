"""Self-check for the margin-cushion safeguard (2026-08-12, at the user's
request): block new entries once equity falls below MARGIN_CUSHION_MIN_RATIO
x maintenance_margin, to stay ahead of an actual Alpaca maintenance margin
call (equity < maintenance_margin, ratio 1.0x).

Run with:
  python scripts/test_margin_safeguard.py
No network calls -- exercises the pure decision function _margin_cushion_ok
directly.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.orchestrator import _margin_cushion_ok
from engine.config import MARGIN_CUSHION_MIN_RATIO

assert MARGIN_CUSHION_MIN_RATIO > 1.0, "must require MORE equity than the bare maintenance requirement to mean anything"

# No margin exposure at all -> always safe, nothing to protect against.
assert _margin_cushion_ok(equity=1000.0, maintenance_margin=0.0, min_ratio=MARGIN_CUSHION_MIN_RATIO) is True

# Comfortably above the ratio -> ok.
assert _margin_cushion_ok(equity=2171.81, maintenance_margin=730.46, min_ratio=1.5) is True

# Exactly at the ratio -> ok (>=, not >).
assert _margin_cushion_ok(equity=1500.0, maintenance_margin=1000.0, min_ratio=1.5) is True

# Just under the ratio -> trips.
assert _margin_cushion_ok(equity=1499.0, maintenance_margin=1000.0, min_ratio=1.5) is False

# At the actual Alpaca call boundary (equity == maintenance_margin) -> trips
# well before this with the default 1.5x ratio.
assert _margin_cushion_ok(equity=1000.0, maintenance_margin=1000.0, min_ratio=1.5) is False

print("OK: margin-cushion safeguard trips below the configured ratio, never on zero margin exposure")

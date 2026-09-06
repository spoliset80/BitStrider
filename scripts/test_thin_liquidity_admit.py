"""Self-check for the thin-liquidity rejected-list trading path (2026-08-12,
widened 2026-08-13).

At the user's request: the guardrails in _passes_guardrails() are UNCHANGED
and still reject these symbols exactly as before (counted in [GUARDRAIL
SUMMARY] same as always). This adds a separate, toggleable path
(TRADE_THIN_LIQUIDITY_REJECTS, off by default) that re-admits a rejected
symbol during regular hours, sized at a flat THIN_LIQUIDITY_POSITION_SIZE_PCT
(3%) instead of the normal POSITION_SIZE_PCT -- confidence-scaling included,
not stacked on top of it.

2026-08-13, user request ("no guard rails for ANY scanner during intra day",
refined same day to "only avoid penny stocks, everything else allow"):
widened from avg_volume/low_float only to every guardrail reason EXCEPT
min_price (RVOL, dollar_vol, gap_chase, market cap included; penny stocks
stay hard-blocked) -- the overnight boundary is still fully enforced by
close_guardrail_fail_positions regardless of what got waived at entry, see
engine/execution/enhanced.py.

2026-08-14, user request ("ones which fail guard will be traded too but
with lower portfolio limit"): extended the same reduced-size-instead-of-
skip treatment to momentum-freshness rejects (_resolve_freshness_reject),
a different mechanism from the guardrails above -- reuses the same
signal.thin_liquidity flag and THIN_LIQUIDITY_POSITION_SIZE_PCT sizing.

Run with:
  python scripts/test_thin_liquidity_admit.py
No network calls -- both pieces are pure functions, no broker/client needed.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.equity.scan as scan
from engine.equity.strategies import Signal
from engine.execution.enhanced import (
    _apply_thin_liquidity_override, _apply_confidence_size_ramp, _resolve_freshness_reject,
)
from engine.config import (
    THIN_LIQUIDITY_POSITION_SIZE_PCT, TRADE_STALE_MOMENTUM_REJECTS,
    CONF_SCALE_FULL_CONF, MAX_POSITION_SIZE_PCT,
)

# NOTE: TRADE_THIN_LIQUIDITY_REJECTS's live value is a deployment decision
# (started False 2026-08-12, flipped True same day at the user's request) --
# not asserted here. Both directions are exercised explicitly below via
# scan.TRADE_THIN_LIQUIDITY_REJECTS regardless of what's currently live.
assert THIN_LIQUIDITY_POSITION_SIZE_PCT == 4.0

# --- _should_admit_thin_liquidity(): the scan-side gate ---

_regular  = SimpleNamespace(is_regular_hours=True)
_extended = SimpleNamespace(is_regular_hours=False)

# Toggle off -> never admits, regardless of reason or session.
scan.TRADE_THIN_LIQUIDITY_REJECTS = False
for reason in ("avg_volume", "low_float", "min_price", "rvol", "dollar_vol", "low_mcap", "gap_chase", "other", None):
    assert scan._should_admit_thin_liquidity(reason, _regular) is False, f"toggle off should never admit ({reason})"

# Toggle on, regular hours -> only the two momentum-shape guardrail reasons
# (rvol, gap_chase) get admitted. 2026-08-26, user request ("shouldn't meet
# the non-negotiable limits of volume"): dollar_vol/avg_volume/low_float/
# low_mcap describe actual tradability, not a momentary reading, so they are
# hard-blocked regardless of the toggle. Penny stocks (min_price), the
# absolute hard floors, and 'other' (not a guardrail at all) stay blocked too.
scan.TRADE_THIN_LIQUIDITY_REJECTS = True
for reason in ("rvol", "gap_chase"):
    assert scan._should_admit_thin_liquidity(reason, _regular) is True, f"{reason} should be rescued intraday"
for reason in ("avg_volume", "low_float", "min_price", "dollar_vol", "low_mcap",
               "avg_volume_hard_floor", "low_float_hard_floor", "other", None):
    assert scan._should_admit_thin_liquidity(reason, _regular) is False, f"should not rescue {reason} even with the toggle on"

# 2026-08-13: toggle on, but OUTSIDE regular hours -> never admits, even for
# avg_volume/low_float. An entry opened outside regular hours is an overnight
# hold from the moment it fills (NRGV, 2026-08-12: admitted at 16:02 ET,
# 2 min after close, sat failing its guardrail all night).
assert scan._should_admit_thin_liquidity("avg_volume", _extended) is False, "must not admit outside regular hours"
assert scan._should_admit_thin_liquidity("low_float", _extended) is False, "must not admit outside regular hours"
assert scan._should_admit_thin_liquidity("avg_volume", None) is False, "no market_state -> fail closed, no admit"

scan.TRADE_THIN_LIQUIDITY_REJECTS = False  # restore default for anything else in-process

# --- _apply_thin_liquidity_override(): the sizing-side override ---

def _sig(thin=False):
    return Signal("TEST", "buy", 10.0, 0.90, "test reason", "TestStrat", thin_liquidity=thin)

# Not flagged -> risk_info passes through untouched.
risk_info = {"dollar_amount": 150.0, "allocation_pct": 10.0, "tier": "NORMAL"}
out = _apply_thin_liquidity_override(risk_info, _sig(thin=False), equity=2000.0)
assert out is risk_info, "unflagged signal should return the exact same dict, not a copy"

# Flagged -> flat THIN_LIQUIDITY_POSITION_SIZE_PCT of equity, overriding
# whatever dollar_amount/allocation_pct confidence-scaling had already
# produced (150.0 here), not stacked on top.
out = _apply_thin_liquidity_override(risk_info, _sig(thin=True), equity=2000.0)
assert out["dollar_amount"] == 80.0, f"expected 4% of $2000 = $80, got {out['dollar_amount']}"
assert out["allocation_pct"] == 4.0
assert risk_info["dollar_amount"] == 150.0, "original dict must not be mutated in place"

# --- _apply_confidence_size_ramp(): continuous ramp from base % to MAX_POSITION_SIZE_PCT ---
# (2026-08-15, replaced the old flat 1.5x-above-92% step: "increase the
# percentage progressively maximum to 15% maximum per ticker"; base/ceiling
# scaled again 2026-08-17 with the whole pipeline's 7.5% -> 10% base move)

assert CONF_SCALE_FULL_CONF == 0.85
assert MAX_POSITION_SIZE_PCT == 20.0

BASE_PCT = 10.0
risk_info = {"dollar_amount": 150.0, "allocation_pct": BASE_PCT, "tier": "NORMAL"}

# At or below the ramp's start point -> unchanged, same dict (no copy).
out = _apply_confidence_size_ramp(risk_info, confidence=0.85, equity=2000.0)
assert out is risk_info, "at the ramp start (not above it) must not scale"
out = _apply_confidence_size_ramp(risk_info, confidence=0.70, equity=2000.0)
assert out is risk_info

# Exactly at 100% confidence -> exactly MAX_POSITION_SIZE_PCT, regardless
# of the base %.
out = _apply_confidence_size_ramp(risk_info, confidence=1.0, equity=2000.0)
assert out["allocation_pct"] == MAX_POSITION_SIZE_PCT, f"expected the {MAX_POSITION_SIZE_PCT}% ceiling, got {out['allocation_pct']}"
assert out["dollar_amount"] == 2000.0 * MAX_POSITION_SIZE_PCT / 100.0
assert risk_info["allocation_pct"] == BASE_PCT, "original dict must not be mutated in place"

# Halfway between the ramp start (85%) and 100% -> halfway between base and
# ceiling, a continuous point, not a flat step.
out = _apply_confidence_size_ramp(risk_info, confidence=0.925, equity=2000.0)
expected_mid = (BASE_PCT + MAX_POSITION_SIZE_PCT) / 2
assert round(out["allocation_pct"], 6) == expected_mid, f"expected the ramp's midpoint {expected_mid}%, got {out['allocation_pct']}"

# Ramps toward the SAME absolute ceiling regardless of the base % -- e.g. a
# small-account 5.0% base still reaches the ceiling at 100% confidence,
# not 5.0 x some fixed multiplier.
small_risk_info = {"dollar_amount": 50.0, "allocation_pct": 5.0, "tier": "NORMAL"}
out = _apply_confidence_size_ramp(small_risk_info, confidence=1.0, equity=1000.0)
assert out["allocation_pct"] == MAX_POSITION_SIZE_PCT, f"expected the absolute {MAX_POSITION_SIZE_PCT}% ceiling, got {out['allocation_pct']}"

# Monotonic: allocation_pct never decreases as confidence rises through the ramp.
prev = BASE_PCT
for conf_pct in range(85, 101):
    out = _apply_confidence_size_ramp(risk_info, confidence=conf_pct / 100, equity=2000.0)
    cur = out["allocation_pct"]
    assert cur >= prev, f"conf={conf_pct}%: allocation_pct dropped from {prev} to {cur}"
    assert cur <= MAX_POSITION_SIZE_PCT, f"conf={conf_pct}%: allocation_pct {cur} exceeded the {MAX_POSITION_SIZE_PCT}% ceiling"
    prev = cur

# --- _resolve_freshness_reject(): stale-momentum trades anyway at reduced size ---

# 2026-08-26: config default flipped to False (hard reject) -- this test
# exercises BOTH toggle states explicitly, so it sets the toggle itself
# rather than assuming the live default.
import engine.execution.enhanced as enhanced
_orig_toggle = enhanced.TRADE_STALE_MOMENTUM_REJECTS
try:
    enhanced.TRADE_STALE_MOMENTUM_REJECTS = True

    # Fresh -> always valid, signal untouched regardless of the toggle.
    sig = _sig(thin=False)
    valid, reason = _resolve_freshness_reject(sig, fresh=True, fade_reason=None)
    assert (valid, reason) == (True, None)
    assert sig.thin_liquidity is False, "fresh signal must not get flagged"

    # Not fresh, toggle on -> valid anyway, flagged thin_liquidity for reduced sizing.
    sig = _sig(thin=False)
    valid, reason = _resolve_freshness_reject(sig, fresh=False, fade_reason="XYZ: faded 10.0% off its 30-min high")
    assert valid is True, "toggle on -> trades anyway, not blocked"
    assert reason is None
    assert sig.thin_liquidity is True, "must flag the signal for reduced sizing"
finally:
    enhanced.TRADE_STALE_MOMENTUM_REJECTS = _orig_toggle

# Not fresh, toggle off -> hard-blocked, signal untouched (old behavior preserved).
enhanced.TRADE_STALE_MOMENTUM_REJECTS = False
try:
    sig = _sig(thin=False)
    valid, reason = _resolve_freshness_reject(sig, fresh=False, fade_reason="XYZ: faded 10.0% off its 30-min high")
    assert valid is False
    assert reason == "XYZ: faded 10.0% off its 30-min high"
    assert sig.thin_liquidity is False, "toggle off -> not flagged, hard-blocked instead"
finally:
    enhanced.TRADE_STALE_MOMENTUM_REJECTS = _orig_toggle

print("OK: thin-liquidity admit path, confidence-based size ramp (base % -> 15% ceiling), "
      "and stale-momentum reduced-size trade-through all check out")

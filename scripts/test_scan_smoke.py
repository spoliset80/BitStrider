"""Regression net for the 2026-09-02 scan NameError cascade.

The morning freeze fix (logging lock) unmasked a stack of lost module-level
definitions in engine/equity/scan.py that crashed scan_universe() on EVERY
cycle -- but the scan never completed all morning (earlier freezes masked
them), so the bot looked healthy (heartbeats flowed) while trading nothing
for ~6 minutes after the RTH open. A deploy gate that imports scan.py and
resolves every module-level name the scan path calls would have caught all
of them pre-market.

This test is static + import-level (no network, no Alpaca): it imports the
same modules the scan path uses and asserts the previously-undefined names
now resolve.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

failures = []


def _check(mod_name: str, names: list[str]) -> None:
    try:
        mod = __import__(mod_name, fromlist=["*"])
    except Exception as exc:  # noqa: BLE001
        failures.append(f"{mod_name}: import failed: {exc!r}")
        return
    for n in names:
        if not hasattr(mod, n):
            failures.append(f"{mod_name}.{n}: MISSING")


# Names that were referenced but undefined on 2026-09-02 (NameError per cycle).
_check("engine.equity.scan", [
    "_adaptive_state", "_ADAPTIVE_MAX_EMPTY", "_ADAPTIVE_MIN_RVOL",
    "_ADAPTIVE_STEP_RVOL", "_ADAPTIVE_MIN_CONF", "_ADAPTIVE_STEP_CONF",
    "_SCAN_TOTAL_BUDGET_SEC", "scan_universe", "get_scan_targets",
    "get_strategy_instances", "_get_float_shares", "_get_market_cap",
])
_check("engine.equity.strategies", ["get_strategy_instances", "_get_float_shares", "_get_market_cap", "Signal"])
_check("engine.utils.bars", ["get_bars", "get_bars_batch", "get_premarket_bars", "get_daily_volume_bars", "get_data_client"])
_check("engine.utils.data", ["setup_logging"])

# _adaptive_state must be initialized with the live-config defaults.
try:
    from engine.equity.scan import _adaptive_state
    assert set(_adaptive_state) == {"empty_scans", "rvol_min", "min_conf"}, _adaptive_state
except Exception as exc:  # noqa: BLE001
    failures.append(f"_adaptive_state init: {exc!r}")

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print(f"ok: scan-path names resolve ({6 - len(failures)} module groups clean)")
print("TEST RESULT: scan smoke checks passed")

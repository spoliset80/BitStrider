"""Self-check for engine.telemetry (2026-09-03).

Safety contract: log_event() must NEVER raise and NEVER block trading, a full
queue must DROP (counted) instead of stalling the poller, and events must land
as valid JSONL in the analytics dir.

Run with:
  python scripts/test_telemetry.py
"""
import json
import sys
import tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.telemetry as tel

_tmp = tempfile.mkdtemp(prefix="apex-telemetry-test-")
tel._analytics_dir = lambda: Path(_tmp)
tel.reset_for_tests()

# 1. Events land as valid JSONL, with the safety-relevant fields present.
tel.log_event("close_submitted", symbol="SNOW", qty=1, reason="software-sl",
              order_id="abc", canceled_protection=True)
tel.log_event("critical_unprotected", symbol="X", error="boom")
tel.reset_for_tests()  # shutdown() drains the queue synchronously

files = list(Path(_tmp).glob("execution-events-*.jsonl"))
assert files, "no analytics file written"
lines = [ln for ln in files[0].read_text(encoding="utf-8").splitlines() if ln.strip()]
assert len(lines) == 2, lines
recs = [json.loads(ln) for ln in lines]
assert recs[0]["event_type"] == "close_submitted" and recs[0]["symbol"] == "SNOW"
assert recs[0]["schema_version"] == 1 and "timestamp_et" in recs[0] and "event_id" in recs[0]
assert recs[1]["event_type"] == "critical_unprotected"

# 2. Unserializable payloads degrade to a short repr instead of raising.
tel.log_event("weird", payload=object())
tel.reset_for_tests()
lines = [ln for ln in files[0].read_text(encoding="utf-8").splitlines() if ln.strip()]
last = json.loads(lines[-1])
assert isinstance(last["payload"], str) and len(last["payload"]) <= 200

# 3. Full queue DROPS (counted), never blocks and never raises.
t = tel._Telemetry(queue_max=2, flush_interval=999)
t._stop.set()  # writer stopped -> nothing drains -> the queue fills
for i in range(10):
    t.log_event("flood", i=i)
assert t.dropped_total() >= 1, "a full queue must drop and count, not block"

# 4. Global entry point survives a broken instance.
tel._instance = None
tel.log_event("still-safe", ok=True)  # must not raise under any config state

print("OK: telemetry writes valid JSONL, degrades unserializable payloads, drops on full queue "
      "without blocking, and never raises into trading code")
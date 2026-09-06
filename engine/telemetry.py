"""
engine.telemetry
----------------
Non-blocking JSONL execution-event log for post-mortems (2026-09-03).

Writes one JSON object per line to
    %LOCALAPPDATA%\\ApexTrader\\analytics\\execution-events-YYYY-MM-DD.jsonl
(machine-local -- deliberately OUTSIDE the OneDrive repo; repo-root *.log files
are legacy and OneDrive-synced coordination files proved unreliable).

Hard safety contract (this sits on the SoftwareStopPoller's 5s budget):
  - log_event() NEVER raises and NEVER blocks on disk I/O -- it only appends to a
    bounded in-memory queue;
  - a full queue DROPS the event (counted in `dropped_total`) instead of stalling
    the trading thread;
  - the writer is a daemon thread: a crash in it cannot take down main.py;
  - serialization errors are logged once per event type, never raised;
  - no network calls, no credentials, no full broker response objects.

Usage:
    from engine.telemetry import log_event
    log_event("close_submitted", symbol="SNOW", qty=1, reason="ema9")
"""

import atexit
import datetime
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from queue import Full, Queue
from typing import Any, Dict, Optional

log = logging.getLogger("ApexTrader")

_SCHEMA_VERSION = 1
_ET = None  # lazy pytz; telemetry must never fail on tz problems either


def _analytics_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    d = Path(base) / "ApexTrader" / "analytics"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass  # writer thread will surface failures; log_event still never raises
    return d


def _et_now():
    global _ET
    if _ET is None:
        try:
            import pytz as _pytz
            _ET = _pytz.timezone("America/New_York")
        except Exception:
            _ET = datetime.timezone.utc
    return datetime.datetime.now(_ET)


class _Telemetry:
    """Bounded-queue JSONL writer. One instance per process."""

    def __init__(self, queue_max: int = 2000, flush_interval: float = 2.0):
        self._q: "Queue[Dict[str, Any]]" = Queue(maxsize=max(1, int(queue_max)))
        self._flush_interval = max(0.2, float(flush_interval))
        self._stop = threading.Event()
        self._dropped_total = 0
        self._drop_logged_at = 0.0
        self._bad_type_logged: set = set()
        self._thread = threading.Thread(target=self._run, name="TelemetryWriter", daemon=True)
        self._thread.start()
        try:
            atexit.register(self.shutdown)
        except Exception:
            pass

    # -- producer side (trading threads) -------------------------------------
    def log_event(self, event_type: str, **fields: Any) -> None:
        """Enqueue one event. Never raises, never blocks indefinitely."""
        try:
            et_now = _et_now()
            record = {
                "schema_version": _SCHEMA_VERSION,
                "event_id": uuid.uuid4().hex[:16],
                "event_type": str(event_type),
                "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "timestamp_et": et_now.isoformat(),
                "process_id": os.getpid(),
            }
            # Keep the payload bounded: anything unserializable becomes a short repr.
            for k, v in fields.items():
                try:
                    json.dumps(v)
                    record[k] = v
                except (TypeError, ValueError):
                    record[k] = repr(v)[:200]
            self._q.put_nowait(record)
        except Full:
            self._dropped_total += 1
            now = time.monotonic()
            if now - self._drop_logged_at > 60:
                self._drop_logged_at = now
                log.warning(
                    f"[TELEMETRY] queue full -- {self._dropped_total} events dropped total "
                    f"(trading NOT affected)"
                )
        except Exception as e:  # absolute last resort -- never propagate
            log.debug(f"[TELEMETRY] log_event failed (ignored): {e}")

    def dropped_total(self) -> int:
        return self._dropped_total

    # -- consumer side (daemon) ----------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            wrote = False
            try:
                record = self._q.get(timeout=self._flush_interval)
                self._write_one(record)
                wrote = True
            except Exception:
                pass  # queue.Empty or a write hiccup -- keep the loop alive
            # Drain whatever else is ready this cycle so bursts don't linger.
            if wrote:
                deadline = time.monotonic() + self._flush_interval
                while time.monotonic() < deadline:
                    try:
                        record = self._q.get_nowait()
                    except Exception:
                        break
                    try:
                        self._write_one(record)
                    except Exception:
                        break

    def _write_one(self, record: Dict[str, Any]) -> None:
        try:
            day = str(record.get("timestamp_et", ""))[:10] or "unknown"
            path = _analytics_dir() / f"execution-events-{day}.jsonl"
            line = json.dumps(record, default=repr, separators=(",", ":"))
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as e:
            etype = record.get("event_type", "?") if isinstance(record, dict) else "?"
            if etype not in self._bad_type_logged:
                self._bad_type_logged.add(etype)
                log.warning(f"[TELEMETRY] write failed for {etype} (future failures silent): {e}")

    def shutdown(self, timeout: float = 2.0) -> None:
        try:
            self._stop.set()
            # Drain what's already queued, best-effort.
            while True:
                try:
                    record = self._q.get_nowait()
                except Exception:
                    break
                try:
                    self._write_one(record)
                except Exception:
                    break
        except Exception:
            pass


_instance: Optional[_Telemetry] = None
_init_lock = threading.Lock()


def _get_instance() -> _Telemetry:
    global _instance
    if _instance is None:
        with _init_lock:
            if _instance is None:
                try:
                    from engine.config import TELEMETRY_QUEUE_MAX, TELEMETRY_FLUSH_INTERVAL_SEC
                    _instance = _Telemetry(TELEMETRY_QUEUE_MAX, TELEMETRY_FLUSH_INTERVAL_SEC)
                except Exception:
                    _instance = _Telemetry()
    return _instance


def log_event(event_type: str, **fields: Any) -> None:
    """Module entry point -- safe under any config/import failure."""
    try:
        from engine.config import EXECUTION_TELEMETRY_ENABLED
        if not EXECUTION_TELEMETRY_ENABLED:
            return
    except Exception:
        pass  # telemetry must be usable even if config import hiccups
    try:
        _get_instance().log_event(event_type, **fields)
    except Exception:
        pass


def reset_for_tests() -> None:
    """Test hook -- drop the singleton so each test starts fresh."""
    global _instance
    with _init_lock:
        if _instance is not None:
            try:
                _instance.shutdown(timeout=0.5)
            except Exception:
                pass
        _instance = None


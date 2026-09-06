"""Offline tests for the 2026-09-02 loss-guardian + auto-deploy + re-entry
daily-loss gap work. No network: every broker/daily-P&L interaction is stubbed.

Covers:
  1. session.daily_loss_halted() -- bull/bear thresholds, no-baseline fail-safe
  2. EnhancedExecutor entry halt:
       - _entry_halt_active() honors _halt_until_eod and the session helper
       - _submit_entry_order (the single entry funnel) refuses when halted and
         submits when not -- covers every re-entry path by construction
  3. EnhancedExecutor.guardian_halt_flatten():
       - cancels all orders, closes every position, sets _halt_until_eod
       - per-day dedupe (2nd call same day is a no-op)
  4. orchestrator._guardian_flat_requested(): today vs stale-date scoping
   4b. orchestrator._maybe_guardian_halt(): per-DAY flatten dedupe
        (date-scoped: same-day no-op, next-day flag re-arms without restart)
  5. watchdog deploy-window + flag-consume logic
   5b. watchdog .env DEPLOY_RESTART_ENABLED kill switch (live-read, default
        enabled, only explicit false disables; gates _deploy_restart_requested)

Run:  python scripts/test_guardian_and_deploy.py
"""
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"{name}: FAILED {detail}"
    PASS += 1
    print(f"ok {name}")


class _NullLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


# -- Fakes ---------------------------------------------------------------------
class FakeClient:
    def __init__(self):
        self.submitted = []
        self.cancelled = []
        self.orders = []
        self.positions = []
        self.acct_equity = 1000.0

    def get_orders(self):
        return list(self.orders)

    def get_all_positions(self):
        return list(self.positions)

    def get_account(self):
        return SimpleNamespace(equity=self.acct_equity)

    def cancel_order_by_id(self, oid):
        self.cancelled.append(str(oid))
        self.orders = [o for o in self.orders if str(o.id) != str(oid)]

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id="filled")


def pos(symbol, qty, price=10.0):
    return SimpleNamespace(symbol=symbol, qty=qty, current_price=price, unrealized_pl=-1.0)


# -- 1. session.daily_loss_halted ----------------------------------------------
def test_session_halted() -> None:
    import engine.session.session as s

    saved = (s.daily_start_equity, s.daily_pnl, s.daily_reset)
    try:
        s.daily_start_equity, s.daily_reset = 1000.0, datetime.date.today()

        s.daily_pnl = -5.0
        check("bull: -0.5% not halted", s.daily_loss_halted(client=None, refresh=False) is False)

        s.daily_pnl = -11.0
        check("bull: -1.1% halted (1% limit)", s.daily_loss_halted(client=None, refresh=False) is True)

        s.daily_pnl = -11.0
        check("bear: -1.1% NOT halted (2% bear limit)", s.daily_loss_halted(client=None, regime="bear", refresh=False) is False)

        s.daily_pnl = -21.0
        check("bear: -2.1% halted", s.daily_loss_halted(client=None, regime="bear", refresh=False) is True)

        # no baseline -> limit sentinel -999_999 -> never halts
        s.daily_start_equity, s.daily_pnl = 0.0, -999_998.0
        check("no baseline: fail-safe never halts", s.daily_loss_halted(client=None, refresh=False) is False)

        # refresh=True path derives pnl from account equity
        fc = FakeClient()
        fc.acct_equity = 985.0
        s.daily_start_equity, s.daily_pnl = 1000.0, 0.0
        check("refresh: equity 985 vs start 1000 halted", s.daily_loss_halted(client=fc, refresh=True) is True)
    finally:
        s.daily_start_equity, s.daily_pnl, s.daily_reset = saved
# -- 2. entry funnel halt ------------------------------------------------------
def _make_executor() -> tuple:
    from engine.execution.enhanced import EnhancedExecutor

    ex = EnhancedExecutor.__new__(EnhancedExecutor)  # bypass broker-touching __init__
    fc = FakeClient()
    ex.client = fc
    ex.order_cache = {}
    ex._entry_submission_lock = None
    ex._recent_entry_submits = {}
    ex._entry_pending = {}
    ex._pending_entry_signals = {}
    ex._halt_until_eod = False
    ex._guardian_halt_closed = None
    ex._loss_halted_cache = None
    ex._loss_halted_cache_ts = 0.0
    ex.market_state = None
    ex._no_rearm = set()
    ex._force_close_pending = {}
    ex._entry_log = {}
    ex._get_positions = lambda force_refresh=False: SimpleNamespace(has_position=lambda s: False)
    return ex, fc


def test_entry_funnel_halted() -> None:
    import engine.execution.enhanced as enhanced

    ex, fc = _make_executor()
    orig = enhanced._session.daily_loss_halted
    try:
        enhanced._session.daily_loss_halted = lambda client, regime="bull": True
        ex._loss_halted_cache = None
        out = ex._submit_entry_order("AAA", "REQ")
        check("halted: _submit_entry_order returns None", out is None, str(out))
        check("halted: no order reached broker", fc.submitted == [])

        # guardian _halt_until_eod alone blocks too (no session call needed)
        ex._halt_until_eod = True
        out = ex._submit_entry_order("BBB", "REQ")
        check("guardian halt: funnel refuses", out is None)

        # not halted -> submits
        ex._halt_until_eod = False
        enhanced._session.daily_loss_halted = lambda client, regime="bull": False
        ex._loss_halted_cache = None
        out = ex._submit_entry_order("CCC", "REQ")
        check("not halted: order submitted", out is not None and len(fc.submitted) == 1, str(out))

        # session error fails open (never silently freezes the book shut)
        def boom(client, regime="bull"):
            raise RuntimeError("api down")

        enhanced._session.daily_loss_halted = boom
        ex._loss_halted_cache = None
        out = ex._submit_entry_order("DDD", "REQ")
        check("session error: fail-open submits", out is not None and len(fc.submitted) == 2, str(out))
    finally:
        enhanced._session.daily_loss_halted = orig


# -- 3. guardian_halt_flatten --------------------------------------------------
def test_guardian_halt_flatten() -> None:
    import engine.execution.enhanced as enhanced

    ex, fc = _make_executor()
    ex._submit_closing_order = lambda sym, qty, side, price, no_extended_hours=False: None
    orig_email = enhanced.send_email
    enhanced.send_email = lambda *a, **k: None
    try:
        fc.orders = [SimpleNamespace(id="o1", symbol="AAA"), SimpleNamespace(id="o2", symbol="BBB")]
        fc.positions = [pos("AAA", 5), pos("BBB", -3), pos("XYZ", 0)]
        summary = ex.guardian_halt_flatten("test-halt")
        check("flatten: closed 2 positions", summary and summary["closed"] == 2, str(summary))
        check("flatten: cancelled 2 orders", summary["cancelled_orders"] == 2, str(summary))
        check("flatten: _halt_until_eod set", ex._halt_until_eod is True)
        check("flatten: both symbols marked no-rearm", ex._no_rearm == {"AAA", "BBB"})
        check("flatten: force-close pending queued", set(ex._force_close_pending) == {"AAA", "BBB"})

        fc.cancelled.clear()
        fc.positions = [pos("AAA", 5)]
        again = ex.guardian_halt_flatten("test-halt-2")
        check("flatten: 2nd call same day deduped (no new cancels)", again is None and fc.cancelled == [])
    finally:
        enhanced.send_email = orig_email
# -- 4. orchestrator flat-flag date scoping ------------------------------------
def test_flat_flag_date_scoping() -> None:
    from engine.orchestrator import _guardian_flat_requested
    from engine import config as cfg

    today = datetime.datetime.now(_pytz()).date().isoformat()
    with tempfile.TemporaryDirectory() as tmp:
        old = cfg.GUARDIAN_FLAT_FILE
        cfg.GUARDIAN_FLAT_FILE = str(Path(tmp) / "flat_request.flag")
        try:
            Path(tmp, "flat_request.flag").write_text(
                json.dumps({"date": today, "pnl": -50.0, "pct": -2.5, "reason": "guardian_daily_loss"}),
                encoding="utf-8")
            check("flat flag: today's date honored", _guardian_flat_requested() is not None)

            Path(tmp, "flat_request.flag").write_text(
                json.dumps({"date": "2000-01-01", "pnl": -50.0, "pct": -2.5}), encoding="utf-8")
            check("flat flag: stale date ignored", _guardian_flat_requested() is None)

            Path(tmp, "flat_request.flag").write_text("not json", encoding="utf-8")
            check("flat flag: corrupt file ignored", _guardian_flat_requested() is None)

            Path(tmp, "flat_request.flag").unlink()
            check("flat flag: missing file -> None", _guardian_flat_requested() is None)
        finally:
            cfg.GUARDIAN_FLAT_FILE = old


# -- 4b. orchestrator guardian-halt per-day dedupe (date-scoped) ---------------
def test_guardian_halt_dedupe_per_day() -> None:
    """_maybe_guardian_halt must dedupe on the FLAG's date, not for the
    process lifetime: the watchdog keeps main.py alive across midnight, so a
    bool would block the NEXT day's legitimate guardian flatten."""
    import engine.orchestrator as orch

    class _RecExecutor:
        def __init__(self):
            self.calls = []

        def guardian_halt_flatten(self, reason):
            self.calls.append(reason)
            return {"closed": 0}

    ctx = SimpleNamespace(executor=_RecExecutor(), guardian_halt_acted_date=None)
    holder = {"payload": None}
    orig = orch._guardian_flat_requested
    orch._guardian_flat_requested = lambda: holder["payload"]
    try:
        today = datetime.datetime.now(_pytz()).date()

        # 1. first today flag -> flatten
        holder["payload"] = {"date": today.isoformat(), "pnl": -50.0, "pct": -2.5,
                             "reason": "guardian_daily_loss"}
        r = orch._maybe_guardian_halt(ctx)
        check("halt dedupe: first today flag flattens",
              r is not None and len(ctx.executor.calls) == 1, str(r))
        check("halt dedupe: acted_date latched to flag date",
              ctx.guardian_halt_acted_date == today)

        # 2. repeated same-day flag -> no second flatten
        orch._maybe_guardian_halt(ctx)
        check("halt dedupe: second same-day flag is a no-op",
              len(ctx.executor.calls) == 1)

        # 3. simulated next-day flag -> flatten again without process restart
        next_day = today + datetime.timedelta(days=1)
        holder["payload"] = {"date": next_day.isoformat(), "pnl": -60.0, "pct": -3.0,
                             "reason": "guardian_daily_loss"}
        r = orch._maybe_guardian_halt(ctx)
        check("halt dedupe: next-day flag re-arms flatten",
              r is not None and len(ctx.executor.calls) == 2, str(r))
        check("halt dedupe: acted_date advanced to next day",
              ctx.guardian_halt_acted_date == next_day)

        # 4. unparsable/missing date -> falls back to today ET, still dedupes
        holder["payload"] = {"pnl": -70.0, "pct": -3.5, "reason": "corrupt-date"}
        r = orch._maybe_guardian_halt(ctx)
        check("halt dedupe: unparsable date falls back to today ET and flattens",
              r is not None and len(ctx.executor.calls) == 3
              and ctx.guardian_halt_acted_date == datetime.datetime.now(_pytz()).date())
        orch._maybe_guardian_halt(ctx)
        check("halt dedupe: fallback date also dedupes", len(ctx.executor.calls) == 3)

        # 5. no flag -> nothing happens
        holder["payload"] = None
        r = orch._maybe_guardian_halt(ctx)
        check("halt dedupe: no flag -> no action", r is None and len(ctx.executor.calls) == 3)
    finally:
        orch._guardian_flat_requested = orig


def _pytz():
    import pytz
    return pytz.timezone("America/New_York")


# -- 5. watchdog deploy logic --------------------------------------------------
def test_watchdog_deploy_window_and_flag() -> None:
    from engine.watchdog import AutoBotWatchdog

    w = AutoBotWatchdog.__new__(AutoBotWatchdog)
    w.logger = _NullLogger()

    def at(h, m):
        return datetime.datetime(2026, 9, 2, h, m, tzinfo=datetime.timezone(datetime.timedelta(hours=-4)))

    check("deploy window 10:59 ET blocked", w._deploy_window_allows(at(10, 59)) is False)
    check("deploy window 11:00 ET allowed (lunch flat)", w._deploy_window_allows(at(11, 0)) is True)
    check("deploy window 14:14 ET allowed (last lunch-flat minute)", w._deploy_window_allows(at(14, 14)) is True)
    check("deploy window 14:15 ET blocked (afternoon reopen, 2026-09-04)", w._deploy_window_allows(at(14, 15)) is False)
    check("deploy window 14:44 ET blocked (afternoon session)", w._deploy_window_allows(at(14, 44)) is False)
    check("deploy window 15:43 ET blocked (still inside session)", w._deploy_window_allows(at(15, 43)) is False)
    check("deploy window 15:44 ET blocked (EOD close firing at 15:44)", w._deploy_window_allows(at(15, 44)) is False)
    check("deploy window 15:45 ET allowed (after 15:44 EOD)", w._deploy_window_allows(at(15, 45)) is True)
    check("deploy window 23:00 ET allowed", w._deploy_window_allows(at(23, 0)) is True)
    check("deploy window 09:04 ET allowed (pre-prep)", w._deploy_window_allows(at(9, 4)) is True)
    check("deploy window 09:05 ET blocked (prep scan)", w._deploy_window_allows(at(9, 5)) is False)

    # flag consume
    import engine.watchdog as wd
    with tempfile.TemporaryDirectory() as tmp:
        old = wd.DEPLOY_FLAG_FILE
        wd.DEPLOY_FLAG_FILE = Path(tmp) / "deploy_requested.flag"
        try:
            check("consume: no flag -> None", w._consume_deploy_flag() is None)
            Path(tmp, "deploy_requested.flag").write_text("1,test reason,git=abc", encoding="utf-8")
            content = w._consume_deploy_flag()
            check("consume: reads + unlinks",
                  content == "1,test reason,git=abc" and not wd.DEPLOY_FLAG_FILE.exists())
        finally:
            wd.DEPLOY_FLAG_FILE = old


# -- 5b. watchdog .env DEPLOY_RESTART_ENABLED kill switch ----------------------
def _kill_switch_env_checks(w, env_file: Path) -> None:
    check("kill switch: missing key defaults enabled",
          w._deploy_restart_enabled() is True)

    env_file.write_text("DEPLOY_RESTART_ENABLED=false\n", encoding="utf-8")
    check("kill switch: .env false disables", w._deploy_restart_enabled() is False)
    env_file.write_text('DEPLOY_RESTART_ENABLED="FALSE"\n', encoding="utf-8")
    check("kill switch: quoted/uppercase false disables",
          w._deploy_restart_enabled() is False)

    for v in ("true", "1", "yes", "on"):
        env_file.write_text(f"DEPLOY_RESTART_ENABLED={v}\n", encoding="utf-8")
        check(f"kill switch: .env {v} enables", w._deploy_restart_enabled() is True)

    env_file.write_text("DEPLOY_RESTART_ENABLED=maybe\n", encoding="utf-8")
    check("kill switch: invalid value falls back to enabled "
          "(only explicit false disables)", w._deploy_restart_enabled() is True)


def _kill_switch_gate_checks(w, env_file: Path, flag: Path) -> None:
    env_file.write_text("DEPLOY_RESTART_ENABLED=false\n", encoding="utf-8")
    flag.write_text("1,test reason,git=abc", encoding="utf-8")
    w._now_et = lambda: datetime.datetime(
        2026, 9, 2, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-4)))
    w._stop_child = lambda process: None
    dummy = SimpleNamespace()
    check("gate: disabled + flag present -> no restart",
          w._deploy_restart_requested(dummy) is False)
    check("gate: disabled leaves the flag in place", flag.exists())

    env_file.write_text("DEPLOY_RESTART_ENABLED=true\n", encoding="utf-8")
    check("gate: enabled + flag + flat window -> restarts",
          w._deploy_restart_requested(dummy) is True)
    check("gate: flag consumed on restart", not flag.exists())
    check("gate: no flag -> False", w._deploy_restart_requested(dummy) is False)


def test_watchdog_deploy_env_kill_switch() -> None:
    """The kill switch must be read LIVE from os.environ + .env (previously a
    frozen module-level True made '.env DEPLOY_RESTART_ENABLED=false' a no-op).
    Documented semantics: missing/blank/invalid -> enabled (only an explicit
    false disables, so a typo can never silently disable auto-deploy);
    0/false/no/off -> disabled. .env wins over the process env, matching the
    child-env merge in _run_loop."""
    from engine.watchdog import AutoBotWatchdog
    import engine.watchdog as wd

    w = AutoBotWatchdog.__new__(AutoBotWatchdog)
    w.logger = _NullLogger()
    saved_env_val = os.environ.pop("DEPLOY_RESTART_ENABLED", None)
    with tempfile.TemporaryDirectory() as tmp:
        old_env_file, old_flag = wd.ENV_FILE, wd.DEPLOY_FLAG_FILE
        env_file = Path(tmp) / ".env"
        flag = Path(tmp) / "deploy_requested.flag"
        wd.ENV_FILE, wd.DEPLOY_FLAG_FILE = env_file, flag
        try:
            _kill_switch_env_checks(w, env_file)

            # process env is honored when .env lacks the key
            env_file.unlink()
            os.environ["DEPLOY_RESTART_ENABLED"] = "false"
            try:
                check("kill switch: process-env false disables when .env absent",
                      w._deploy_restart_enabled() is False)
            finally:
                os.environ.pop("DEPLOY_RESTART_ENABLED", None)

            # .env wins over the process env (same precedence as child env)
            env_file.write_text("DEPLOY_RESTART_ENABLED=true\n", encoding="utf-8")
            os.environ["DEPLOY_RESTART_ENABLED"] = "false"
            try:
                check("kill switch: .env true wins over process-env false",
                      w._deploy_restart_enabled() is True)
            finally:
                os.environ.pop("DEPLOY_RESTART_ENABLED", None)

            _kill_switch_gate_checks(w, env_file, flag)
        finally:
            wd.ENV_FILE, wd.DEPLOY_FLAG_FILE = old_env_file, old_flag
            os.environ.pop("DEPLOY_RESTART_ENABLED", None)
            if saved_env_val is not None:
                os.environ["DEPLOY_RESTART_ENABLED"] = saved_env_val


# ── 5c. watchdog .env content-hash gating ─────────────────────────────────────
def test_watchdog_env_hash_gating() -> None:
    """The .env restart trigger must compare CONTENT (sha256), not mtime:
    OneDrive sync touches .env's mtime without changing content, which caused
    a main.py restart storm (10+ restarts, 2026-09-02 morning)."""
    import os
    import engine.watchdog as wd

    with tempfile.TemporaryDirectory() as tmp:
        old = wd.ENV_FILE
        env_file = Path(tmp) / ".env"
        wd.ENV_FILE = env_file
        try:
            env_file.unlink(missing_ok=True)
            check("env hash: missing .env -> None", wd.AutoBotWatchdog._env_hash() is None)

            env_file.write_text("A=1\n", encoding="utf-8")
            h1 = wd.AutoBotWatchdog._env_hash()
            check("env hash: 64-hex digest", isinstance(h1, str) and len(h1) == 64
                  and all(c in "0123456789abcdef" for c in h1), str(h1))
            check("env hash: stable across calls", wd.AutoBotWatchdog._env_hash() == h1)

            # THE regression: mtime changes, content does NOT -> hash unchanged
            old_stat = env_file.stat()
            os.utime(env_file, (old_stat.st_atime + 5, old_stat.st_mtime + 5))
            check("env hash: mtime-only touch does NOT change hash",
                  wd.AutoBotWatchdog._env_hash() == h1,
                  "mtime gating would have restarted main.py here")

            # real content change -> hash changes
            env_file.write_text("A=2\n", encoding="utf-8")
            check("env hash: content change changes hash",
                  wd.AutoBotWatchdog._env_hash() != h1)

            # wiring: the run loop uses the hash, not st_mtime
            src = (Path(__file__).resolve().parent.parent / "engine" / "watchdog.py").read_text(encoding="utf-8")
            check("env hash: run loop uses _env_hash(), not st_mtime",
                  "_env_hash()" in src and "st_mtime if ENV_FILE.exists" not in src)
        finally:
            wd.ENV_FILE = old


if __name__ == "__main__":
    test_session_halted()
    test_entry_funnel_halted()
    test_guardian_halt_flatten()
    test_flat_flag_date_scoping()
    test_guardian_halt_dedupe_per_day()
    test_watchdog_deploy_window_and_flag()
    test_watchdog_deploy_env_kill_switch()
    test_watchdog_env_hash_gating()
    print(f"\nTEST RESULT: {PASS} checks passed")

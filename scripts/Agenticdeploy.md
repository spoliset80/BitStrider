Everything needed is confirmed. Here is the full file-by-file design -- no code written yet, as requested.

---

# Agentic Loss-Guardian + Auto-Deploy -- Design

## 0. Goals / non-goals

**Goals**
1. A deterministic, always-on **loss guardian** (no LLM in the fast path) that watches real day P&L, alerts at a soft threshold, and hard-flattens + halts at a hard threshold -- even if the bot is hung.
2. **Auto-deploy**: I edit code -> tests pass -> I drop a flag -> the supervisor restarts the bot on the new code. **Zero PowerShell per deploy.**
3. Close the known **re-entry daily-loss gap** in the bot itself.
4. Agent visibility: every alert/halt/deploy lands in auditable local state files + log + email, so I can analyze when you call me.

**Non-goals**: fully unattended LLM code-editing on a live-money bot. The fast path stays deterministic; code changes stay human-gated (you approve, I implement, tests gate the deploy).

---

## 1. Architecture

```
                     -----------------------------------------------
                     |  Task Scheduler (2 registered tasks)        |
  08:00 + logon ---->|  ApexTraderAutoRun  -> run_autobot_task.ps1  |--> autobot.py (WATCHDOG)
   every 1 min ----->|  ApexTraderGuardian -> run_guardian_task.ps1 |--> guardian.py (--once)
                     -----------------------------------------------

  WATCHDOG (engine/watchdog.py, stdlib-only)
    * launches main.py child (venv), crash-backoff restart, heartbeat-stall email
    * EXISTING: restarts child when .env mtime changes
    * NEW:      restarts child when deploy_requested.flag appears  (flat-window gated)

  BOT (main.py -> orchestrator)
    * in-bot daily-loss halt + kill mode (existing)
    * NEW: polls guardian flat_request.flag -> in-bot flatten+halt (5s tick)
    * NEW: re-entry paths honor daily-loss halt

  GUARDIAN (scripts/guardian.py, one-shot per minute)
    * reads daily_state.json baseline + Alpaca equity  ->  day P&L %
    * alert tier   -> email + state file
    * halt tier    -> write flat_request.flag (bot flattens); if bot heartbeat stale -> flat-sell DIRECTLY

  AGENT (me)
    * you: "losses are bad" -> me: read guardian_state.json/guardian.log/apextrader.log -> fix code
    * I write data via editor -> tests -> deploy_requested.flag -> watchdog restarts. Done. No PS.
```

**All flags/state live OUTSIDE the OneDrive repo** at `%LOCALAPPDATA%\ApexTrader\state\` -- the `.mainbot.lock` double-runner proved OneDrive-synced coordination files are unreliable across elevations. (`.daily_state.json`, `heartbeat.txt`, logs stay where they are today.)

---

## 2. State & flag files

| File | Location | Written by | Read by | Format |
|---|---|---|---|---|
| `deploy_requested.flag` | `%LOCALAPPDATA%\ApexTrader\state\` | agent/user | watchdog | text: `epoch,reason,git_head` |
| `flat_request.flag` | same | guardian | orchestrator | JSON `{date, pct, equity, reason, ts}` |
| `guardian_state.json` | same | guardian | agent/guardian | JSON snapshot each run |
| `guardian.log` | repo root | guardian | agent | text lines |
| `.daily_state.json` | repo `engine/` | bot (existing) | guardian | existing `{daily_reset, daily_start_equity}` |
| `heartbeat.txt` | repo (existing) | bot | guardian, watchdog | existing |
| `autobot.pid/.mainbot.lock` | repo (existing) | -- | -- | **migrate `.mainbot.lock` to local dir too** (fixes the duplicate-runner root cause) |

Deploy flag content carries the git HEAD + reason so `autobot.log` records exactly what was deployed.

---

## 3. File-by-file

### 3.1 `engine/config.py` -- new constants (pure, env-overridable)

```python
# -- Local (non-synced) coordination state -----------------------------
STATE_DIR = Path(os.getenv("LOCALAPPDATA", str(Path.home()/ "AppData"/"Local"))) / "ApexTrader" / "state"
GUARDIAN_FLAT_FILE  = STATE_DIR / "flat_request.flag"        # guardian -> orchestrator
LOCK_FILE_LOCAL     = STATE_DIR / "mainbot.lock"             # main.py single-instance (migrated)

# -- Guardian ----------------------------------------------------------
GUARDIAN_ALERT_PCT = float(os.getenv("GUARDIAN_ALERT_PCT", "0.75"))   # email at -0.75%
GUARDIAN_HALT_PCT  = float(os.getenv("GUARDIAN_HALT_PCT", "1.5"))     # flatten+halt at -1.5%
GUARDIAN_STALE_HEARTBEAT_SEC = int(os.getenv("GUARDIAN_STALE_HEARTBEAT_SEC", "300"))
GUARDIAN_POLL_START_ET, GUARDIAN_POLL_END_ET = "09:35", "15:50"      # only act inside this band

# -- Watchdog deploy ---------------------------------------------------
DEPLOY_FLAG_FILE = STATE_DIR / "deploy_requested.flag"
DEPLOY_RESTART_ENABLED = _env_bool("DEPLOY_RESTART_ENABLED", True)
# Restart allowed only when the book is guaranteed flat:
#   lunch 11:00-14:45 ET (rule-enforced flat) and 15:50-09:05 ET (EOD flat -> next prep).
DEPLOY_WINDOWS = (("11:00", "14:45"), ("15:50", "09:05"))
```
`config.py` already imports `Path`; `STATE_DIR` must be created lazily (`mkdir(exist_ok=True)`) by the writers.

### 3.2 `engine/session/session.py` -- shared halt helper + day cleanup

Add one function (module-level, imports `config` lazily like the rest of the codebase does):

```python
def daily_loss_halted(client, regime: str, refresh: bool = True) -> bool:
    """True when daily P&L is at/below the regime loss limit (shared by
    orchestrator scan gate, bull-plan, poller, and every enhanced.py re-entry path)."""
    if refresh:
        refresh_daily_pnl(client)
    from engine import config as _cfg
    loss_pct = _cfg.DAILY_LOSS_LIMIT_BEAR_PCT if regime == "bear" else _cfg.DAILY_LOSS_LIMIT_BULL_PCT
    limit = -(daily_start_equity * loss_pct / 100) if daily_start_equity > 0 else -999_999
    return daily_pnl <= limit
```
- In `reset_daily(client)` (line 118): after `save_daily_state()`, delete `GUARDIAN_FLAT_FILE` if `daily_reset == today` (new day clears the halt). Import `cfg.GUARDIAN_FLAT_FILE` lazily.
- `load_daily_state()` unchanged (guardian reads the same file).

### 3.3 `engine/execution/enhanced.py` -- close the re-entry gap + halt flatten

**a) Re-entry loss gate (the known 9/1 loss driver).** `EnhancedExecutor` does not import `session` today (confirmed). Add:
```python
from engine.session import session as _session
```
and one cached check method on the executor (cache ~15s so the 5s tick doesn't hammer `get_account`):
```python
def _daily_loss_halted(self) -> bool:
    if time.time() - getattr(self, "_loss_check_ts", 0) < 15:
        return bool(getattr(self, "_loss_halted_cached", False))
    try:
        halted = _session.daily_loss_halted(self._client, self._regime or "bull")
    except Exception:
        halted = False          # fail-open to existing behavior on API errors
    self._loss_halted_cached, self._loss_check_ts = halted, time.time()
    return halted
```
Guard the **top of each** (early-return, log `[LOSS-HALT]`):
- `_maybe_rearm_reentry` (line 5035) -- the no-rearm branch is exactly what the 9/1 reconstruction showed was missing
- `detect_stopped_out_positions` (3109) -- only its *re-arm* half; stop-exits still run
- `check_blocked_entries_ema` (5540)
- `check_pending_entries_ema` (5361) and `maybe_add_staged_tranches` -- so the 5s poller never re-enters either
Do **not** gate exit-side paths (`check_ema9_exit`, `check_software_stops`, `check_afterhours_stops`, `_cover_naked_positions`) -- losses still need exits.

**b) `guardian_halt_flatten(reason)`** -- new method reusing the lunch-flat machinery's internals (it's time-gated as a *job*; the method itself closes regardless -- verify at implementation, and if any internal `in_lunch_break` gate exists, bypass it with a `force=True` param):
- cancel **all** open orders (incl. GTC protective stops -- deduped, as `lunch_flat_positions` does at 3930)
- market-close every position (same close helper lunch-flat uses)
- set `self._halt_until_eod = True` -> `execute()`, rearm, blocked/pending EMA, staged-tranche all check it
- log + `send_email("[APEXTRADER] GUARDIAN HALT ...")`

### 3.4 `engine/orchestrator.py` -- consume the halt flag

- New helper near `_check_kill_mode` (line 425):
```python
def _guardian_flat_requested() -> Optional[dict]:
    # read cfg.GUARDIAN_FLAT_FILE; None if missing / parse-fail / date != today (ET)
```
- `_tick` (914): once per ~30s (new `ctx.last_guardian_check`), if requested and `not ctx.guardian_halt_acted` -> `ctx.executor.guardian_halt_flatten(reason)`; set `ctx.guardian_halt_acted = True`. Keep it in the poll thread (genuine concurrency -- same rationale as the 2026-08-24 comment: it must run even while `scan_and_trade` is mid-cycle).
- `scan_and_trade` (474): right after the kill-mode check (510) add:
```python
if _guardian_flat_requested():
    log.warning("[SYSTEM] Guardian halt active -- skipping discovery/scan/entries")
    return
```
- `start()` (1176): after `_session.load_daily_state()` (1180), delete any flag whose `date < today` (stale from a prior day); confirm at implementation where the executor gets its `_client`/`_regime` so `_daily_loss_halted()` works.

### 3.5 `engine/watchdog.py` -- deploy auto-restart (stdlib-only, keeps its zero-engine-import bootstrap property)

Constants near the top (alongside `VENV_DIR` at line 21):
```python
STATE_DIR  = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ApexTrader" / "state"
DEPLOY_FLAG = STATE_DIR / "deploy_requested.flag"
from zoneinfo import ZoneInfo          # py3.9+ stdlib -- ET clock without pytz/deps
```

`_run_loop` (337) inner `while process.poll() is None:` loop -- extend the existing per-cycle check (currently just `.env` mtime at 379-390):
```python
while process.poll() is None:
    time.sleep(2)
    if _env_changed():          # existing .env check -> _stop_child + break
        ...
    if self._deploy_requested(process):   # NEW
        break
```
New methods:
```python
def _deploy_allowed_now(self) -> bool        # ET within DEPLOY_WINDOWS (zoneinfo) -- weekend/overnight fine
def _deploy_requested(self, process) -> bool # flag exists + allowed -> log reason/HEAD, unlink flag, _stop_child, return True
                                             # flag exists + NOT allowed -> leave flag, log "deferred until <next window>", return False
def _stop_child(self, process) -> None       # extracted terminate->15s->kill (same as 383-390)
```
Why flag-primary and NOT raw source-mtime watching: I make *many* incremental edits per fix; mtime-watching would restart mid-edit repeatedly. The flag makes deploys atomic and review/test-gated. (A source-watch toggle is a possible later opt-in for OneDrive cross-machine edits -- default **off**.)

### 3.6 `scripts/guardian.py` (new, one-shot per minute; mirrors `_review_30d.py`'s self-contained pattern)

1. Parse `.env` itself (copy watchdog's `_parse_env_file`); set `TRADE_MODE` so the right keys are used.
2. Read `engine/.daily_state.json` -> `(daily_reset, day_start_equity)`.
3. If ET not in `09:35-15:50` -> exit (write nothing heavy).
4. If `daily_reset != today(ET)` -> bot hasn't reset yet this morning -> use *current* equity as baseline and exit (guardian re-baselines next minute; no halting before the bot's own baseline exists).
5. `acct = TradingClient(...).get_account()`; `pnl = equity - day_start_equity`; `pct = pnl/day_start_equity*100`. (Same formula as `session.refresh_daily_pnl` -- one source of truth.)
6. Read heartbeat age (`heartbeat.txt`). Read `guardian_state.json` for dedupe (`alerted_date`, `halted_date`, `baseline_date`).
7. **Alert** (`pct <= -GUARDIAN_ALERT_PCT`, once/day): email via `engine.notifications.send_email` (env already loaded) + write state.
8. **Halt** (`pct <= -GUARDIAN_HALT_PCT`, once/day):
   - write `flat_request.flag` (JSON above) -> if bot heartbeat fresh, bot flattens <= ~5 s;
   - if heartbeat **stale** (> `GUARDIAN_STALE_HEARTBEAT_SEC`) -> guardian itself market-sells every open position via the TradingClient, then emails "GUARDIAN EMERGENCY FLATTEN (bot unresponsive)" -- documented last resort;
   - never place buys, never touches cash otherwise.
9. Overlap guard: `%LOCALAPPDATA%\ApexTrader\state\guardian.lock` (O_EXCL + pid; steal if > 5 min old) so a slow API call can't double-fire two task invocations.
10. Append one line to `guardian.log` every run + write `guardian_state.json` (ts, equity, pct, heartbeat_age, positions, alert/halt state, baseline source).

**Deliberate defaults:** `ALERT 0.75%`, `HALT 1.5%` -- independent of the `.env` `DAILY_LOSS_LIMIT_BULL_PCT=5.0` mismatch (currently the in-bot limit is far looser than the code's 1% default intended). The guardian is the hard backstop; fixing the `.env` to 1/2 is a separate follow-up I'll flag.

### 3.7 Launch scripts (new) + task registration

`scripts/run_guardian_task.ps1` -- clone of `run_autobot_task.ps1` (resolve `py`/`python`, cd repo, run `python scripts/guardian.py --once`, tee to `guardian_scheduler.log`, propagate exit code).

Re-register the two tasks (admin PowerShell, exact commands in the runbook):
```powershell
# 1) Trading supervisor -> watchdog (the deploy pipeline's foundation)
#    easiest: run windows_schedule_apextrader.ps1 (registers run_autobot_task.ps1, daily+logon, HIGHEST)
# 2) Guardian every minute, Mon-Fri
schtasks /Create /F /TN ApexTraderGuardian /SC MINUTE /MO 1 /D MON,TUE,WED,THU,FRI `
  /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\...\BitStrider-main\scripts\run_guardian_task.ps1" /RL HIGHEST
```

### 3.8 Small extras
- **`scripts/deploy.py`** (new): run the 42-test gate + `compileall` -> on green, write `deploy_requested.flag` (reason/HEAD). So a deploy is *always* test-gated; one command, and even that becomes unnecessary once I write the flag directly after my own test run.
- **`scripts/status.py`** (new, read-only): one-glance dashboard -- equity, day P&L vs baseline, open positions, guardian state, heartbeat age, watchdog pid. The "watch losses" answer when you're at the keyboard.

---

## 4. Ops runbook (post-implementation)

**Deploy a fix (zero PowerShell):** I edit -> I run tests -> I write `deploy_requested.flag` (with HEAD+reason) -> watchdog restarts within ~2 s **if** ET is in a flat window (11:00-14:45 or after 15:50); otherwise it defers and applies at the next window, logging `[DEPLOY] deferred`. Critical mid-session hotfixes get an explicit `force=1` flag (still requires tests to pass in `deploy.py`).

**Rollback:** `git revert` (or I restore the files) -> new flag -> same path. Every deploy is in `autobot.log` with HEAD.

**Halt event:** guardian email + `guardian_state.json` + `flat_request.flag`; bot logs `[SYSTEM] Guardian halt` and flattens; you page me with the event -> I read state/log/`_review_30d.py` -> propose fix.

**Once-only migration:** stop the current direct-task instance (kill as before) -> register watchdog task -> register guardian task -> verify one `main.py` + one watchdog (`autobot.pid`) + guardian ticking.

---

## 5. Test plan

| Change | Test |
|---|---|
| `session.daily_loss_halted` | unit: pnl below/at/above limit in bull & bear |
| enhanced re-entry gates | monkeypatch `_daily_loss_halted`/session pnl -> `_maybe_rearm_reentry`, blocked/pending EMA, staged tranches do **not** fire; exits still run |
| `guardian_halt_flatten` | idempotent; cancels stops; market-closes; sets `_halt_until_eod` |
| orchestrator flag consume | `_guardian_flat_requested` date-scoping; `scan_and_trade` returns early; `_tick` flattens once |
| watchdog deploy | flag in-window -> restart; out-of-window -> deferred (flag kept); stale-date flag cleared |
| guardian (mock broker) | alert fires once/day; halt writes flag; stale-heartbeat path flat-sells; baseline handling |
| `test_deploy.py`, `test_guardian.py`, extended `test_loss_reentry_30m_gate.py` | new files in `scripts/` |

All existing 42 tests must stay green.

---

## 6. Risks & open decisions (recommended defaults in bold)

1. **Re-enabling the watchdog** -- on 8/31 it crash-loop-restarted (`Restarting in 5 seconds`) before you switched the task to run `main.py` directly. Implementation must reproduce/verify that 8/31 failure is gone (it may have been main.py startup crashing, not the watchdog). **Stage the cutover on an evening/weekend; keep the one-line re-register rollback documented.**
2. **Thresholds -- approve `ALERT 0.75%` / `HALT 1.5%`**, or set `GUARDIAN_ALERT_PCT`/`GUARDIAN_HALT_PCT` in `.env` (guardian is independent of the misconfigured 5/8% in-bot loss limits).
3. **Deploy windows** (lunch-flat + after-hours) -- approve, or add force-only mid-session.
4. **Mid-session restart safety**: restarts re-sync from the broker and the `.mainbot.lock` guards dupes; the lunch window is rule-flat so it's the natural mid-day slot.
5. **`.mainbot.lock` migration** to `STATE_DIR` fixes the original duplicate-runner root cause -- bundled.

---

If you approve this design (and the defaults in items 1-3), I'll implement it in the same order: session helper -> enhanced re-entry gates + halt flatten -> orchestrator flag consumption -> guardian + deploy script -> watchdog deploy hook -> task scripts -> tests, then hand you the one-time migration commands. Want me to proceed, or adjust thresholds/windows first?
# AGENT_CHECKPOINT — coding-agent resume point (keep updated)

> **READ `AGENT_CONTEXT.md` FIRST** — it is the always-current quick-reload
> file (environment, live behavior, architecture map, conventions, deferred
> work). This file below is append-only SESSION HISTORY (newest snapshot at
> top). Reload protocol: AGENT_CONTEXT.md → newest snapshot here → work.

> **Purpose:** the 2026-09-02 16:50 ET session was killed mid-run by an
> upstream stream timeout (`BodyTimeoutError: UND_ERR_BODY_TIMEOUT` — Node/undici
> inside the coding CLI, NOT this repo's code; bot was unaffected). To make any
> future kill lossless: **the coding agent updates this file after every verified
> milestone** (edits made, tests run + results, deploy state, what's next), and a
> replacement session reads it first and finishes the remaining work without
> re-asking questions. Update BEFORE starting a risky step, and again after it.

---
## Snapshot — 2026-09-06c (TRADING_SYSTEM.md — full trading-system reference doc created)

### Done
- Created `TRADING_SYSTEM.md` (23 sections): daily ET timeline, discovery
  sources (TI + Alpaca movers + static fallback), active/disabled strategy
  matrix per get_strategy_instances(), guardrail thresholds (price/RVOL/
  dollar-vol/float/mcap/gap-chase), signal ranking, final entry validation
  (EMA7/15, freshness, PDT, VIX ROC), sizing pipeline (10% base -> caps),
  2.0x gross ceiling (pre+post trade), 0.25% DAY trailing entries, staged
  4x25%, ATR GTC protection (1.5-4%), 11-layer exit hierarchy, reconciled
  close, re-entry/loss blocks, guardian (-0.75%/-1.5%), kill mode, watchdog,
  daily AI loop, config matrix, known risks, ops checklists.
- Values verified against source 2026-09-06; config.py declared authoritative.
- Validation: test_strategy_toggles / test_strategy_scoreboard /
  test_entry_window all exit 0; compileall exit 0; git diff --check clean;
  all 23 sections present, no leftover build markers.
- AGENT_CONTEXT.md §5 now points to TRADING_SYSTEM.md as the trading-behavior
  reference.

### Next
1. Monday 9/7: first scheduled runs (Labor Day — market closed, observe-only).
2. Deferred: MAE/MFE per chain, Kelly via scoreboard, saturation counters;
   legacy task health audit (2147946720).

---
## Snapshot — 2026-09-06b (PROVIDER FALLBACK CHAIN: default -> deepseek -> moonshot, commit 414aee2)

### Done (verified live)
- Both DeepSeek and Moonshot API keys were ALREADY stored in
  ~/.cline/data/settings/providers.json (deepseek: deepseek-v4-flash,
  moonshot model field was wrong). Verified BOTH keys live against each
  provider's /models endpoint. Valid Moonshot models: kimi-k2.6, kimi-k3,
  kimi-k2.7-code, kimi-k2.7-code-highspeed.
- daily_automation.run_cline now tries: CLI default provider first, then
  CLINE_PROVIDER_FALLBACKS chain (default "deepseek:deepseek-v4-flash,
  moonshot:kimi-k2.7-code"). Failure = non-zero exit OR error run_result
  inside the JSON stream even on exit 0 (verified live: bogus provider
  exits 1 with "finishReason":"error"). Every attempt audited in the
  output tail that lands in run-state.json.
- Override: setx CLINE_PROVIDER_FALLBACKS "provider:model,..."; "off" or
  empty disables fallbacks. Worst case adds 2 extra session timeouts
  before fail-closed OBSERVE_ONLY.
- Tests: 78/78 (13 new fallback checks incl. exit-0-error case).
  compileall clean.

### Next
1. Monday: watch scheduled runs; deploy only if gates+tests+verify pass.
2. Deferred: MAE/MFE per chain, per-strategy Kelly via scoreboard import,
   saturation streak counters; legacy task health audit (2147946720).

---
## Snapshot — 2026-09-06 (DEPLOY ARMED by user; launcher -Force bug found+fixed; 63/63 green)

### Done (verified live)
- User set AUTOMATION_ALLOW_DEPLOY=1 (User env). Full gated pipeline
  including auto-deploy is ARMED for Monday 2026-09-07 runs.
- Cline CLI installed (v3.0.61, node v24) and ALREADY AUTHENTICATED (probe
  session returned OK from deepseek/deepseek-v4-flash; no `cline auth`
  needed). Real plan session validated: 2026-09-05 forced run produced a
  grounded schema-valid candidate.json -> OBSERVE_ONLY (Saturday, market
  closed), gate rejected, lock released, full artifact set written.
- BUG fixed (commit 54933fa): launcher passed `-Force` straight to
  daily_automation.py -> "unrecognized arguments: -Force" exit 2 (scheduler
  log 2026-09-04 22:52, StartWhenAvailable catch-up fire). Fixed BOTH sides:
  argparse aliases (-Force/-Offline/-DryRun/-SkipAgent/-AllowDeploy) in
  daily_automation.py AND generic -Flag -> --flag translation in
  run_daily_automation_task.ps1. Verified: launcher `-Force -Offline
  -DryRun -SkipAgent` exits 0; 63/63 tests; compileall clean.

### Next
1. Monday: watch the scheduled runs produce full observe->plan artifacts;
   deploy only happens if gates + tests + verify pass (live money!).
2. Deferred: MAE/MFE per chain, per-strategy Kelly via scoreboard import,
   saturation streak counters across days; health audit of legacy
   ApexTraderAutoRun/ApexTraderGuardian tasks (2147946720).

---
## Snapshot — 2026-09-08 ~22:45 ET (FIX + REGISTERED: ApexTraderDailyImprovement task is LIVE, observe-only)

### Goal
Fix the task-registration failure the user hit ("Register the schedule" step).

### Root cause + fix
1. `windows_schedule_daily_automation.ps1` threw "Not running as
   Administrator" — unnecessary: a current-user, LeastPrivilege,
   InteractiveToken task registers fine WITHOUT elevation (verified live:
   Register/Unregister of a probe task succeeded non-elevated).
2. Latent bug found while reproing: New-ScheduledTaskTrigger silently
   rounded -RepetitionDuration 3.5h -> PT4H. Rewrote the registration to
   the repo's PROVEN XML pattern (fix_autorun_task.ps1) with exact
   PT15M / PT3H30M / StopAtDurationEnd, plus post-register self-verification
   that throws if the repetition differs.

### Done (verified live)
- Task `ApexTraderDailyImprovement` REGISTERED: State=Ready, weekly
  Mon-Fri 11:00 local, every PT15M for PT3H30M, NextRun 2026-09-07 11:00.
- End-to-end trigger test: Start-ScheduledTask -> launcher ->
  daily_automation.py exited 0, LastTaskResult=0, run-state decision
  OUTSIDE_WINDOW (22:43 ET, correctly outside 12:05-14:00), scheduler log
  clean, lock released.
- Script committed with the fix; runs remain OBSERVE_ONLY until
  AUTOMATION_ALLOW_DEPLOY=1 and Cline CLI is installed.

### Next (remaining activation steps)
1. Install Cline CLI + `cline auth` (until then: observation-only days).
2. After market data exists, do one supervised in-window check:
   `powershell -File scripts\run_daily_automation_task.ps1 -Force`
   (verify artifacts under %LOCALAPPDATA%\ApexTrader\automation\<date>\).
3. After a clean observe-week, opt into the gated deploy:
   set AUTOMATION_ALLOW_DEPLOY=1 machine-wide.
4. Deferred: MAE/MFE per chain, per-strategy Kelly via scoreboard import,
   saturation streak counters across days.

---
## Snapshot — 2026-09-08 (DAILY AUTOMATION LOOP BUILT — observe-only; task NOT yet registered; no trading-code changes)

### Goal
Implement the user's daily automation request (weekday 12:05–14:00 ET:
observe P&L/code → plan → implement → test → deploy, with saturation days
recorded) WITHOUT violating the live-money safety design (Agenticdeploy.md
non-goal: no unattended LLM fast path). Result: a deterministic,
evidence-gated controller around bounded Cline CLI sessions.

### Done (all verified by tests + live smoke)
- `scripts/analyze_daily_portfolio.py` — READ-ONLY daily observation: Alpaca
  fills → position-ladder round trips (same method as `_review_30d.py`),
  per-symbol stats, churn chains (≥3 RT/day net-neg), entry-band violations,
  max daily-cumulative drawdown, runtime health (heartbeat/guardian/flags).
  Writes machine-local artifacts only.
- `scripts/daily_automation.py` — controller: ET window [12:05,14:00) + phase
  deadlines; machine-local stale-recovering lock; PLAN (Cline CLI `--plan`,
  bounded timeouts, `CLINE_COMMAND_PERMISSIONS` allow/deny policy) →
  `evaluate_candidate` gates (≥5 days/≥20 trades, min effect $5 + ≥5% relative,
  drawdown tail, allowed_files inside repo and never `.env`, prohibited changes
  + acceptance tests declared, heartbeat/flat/guardian/deploy-flag healthy,
  market open) → ACT (allowed_files only, act-report.json required) → FULL
  test gate (all `scripts/test_*.py` + compileall; `--skip-tests` impossible)
  → VERIFY (controller diff-vs-allowed_files + independent agent report) →
  deploy ONLY via `scripts/deploy.py` and only with `--allow-deploy` /
  `AUTOMATION_ALLOW_DEPLOY=1`. OBSERVE_ONLY on: no Cline CLI, invalid plan
  artifact, gate failure, saturation flag, dry-run, skip-agent.
- `scripts/test_daily_automation.py` — 63 offline checks (window/deadline/
  redaction/lock/gates/ladder-reconstruction/test-gate/deploy-guard/
  unexpected-change filter/5 end-to-end main() paths). ALL PASS.
- `scripts/run_daily_automation_task.ps1` (launcher, mirrors
  run_autobot_task.ps1 incl. real-exit-code propagation) +
  `scripts/windows_schedule_daily_automation.ps1` (registers
  `ApexTraderDailyImprovement`: 15-min cadence, local 11:00–14:30 weekdays;
  the SCRIPT is the ET-window authority). Both parse-checked; launcher
  pattern matches repo convention.
- AGENT_CONTEXT.md §3/§5 updated with the loop's facts.
- Validation: new suite 63/63; full existing loop 55/55 suites exit 0
  (test_notifications.py excluded — sends real email outside deploy gate);
  compileall exit 0; `git diff --check` clean; live smoke run
  (`--force --offline`) → decision OBSERVE_ONLY, reason
  `cline_cli_unavailable`, no deploy, lock released, artifacts complete.

### Current mapping
- Live runtime baseline UNCHANGED = `8afbfb2` (no trading-code edits this
  session; bot untouched). New files are automation tooling only.

### Next (exact steps to activate)
1. Install Cline CLI (`npm i -g @cline/cli` or official installer) + `cline auth`.
2. Register the task: elevated PowerShell →
   `scripts\windows_schedule_daily_automation.ps1`.
3. Run one supervised dry run:
   `powershell -File scripts\run_daily_automation_task.ps1 -Force -Offline`
   then a live one (`--force`) after market data is available.
4. After a clean observe-week, opt into the gated deploy:
   set `AUTOMATION_ALLOW_DEPLOY=1` (machine env) — deploy still requires all
   evidence gates + full test suite + verify session + `deploy.py` gate.
5. Deferred: MAE/MFE per chain (needs intraday bars), per-strategy Kelly via
   scoreboard import, saturation streak counters across days.

---
## Snapshot — 2026-09-04 ~19:00 ET (AUDIT VERIFIED + follow-up checkpoint fix — all 8 audit items confirmed in place)

### Goal
Verify every audit fix from `842fea7` was actually applied, close the one
remaining freshness gap, and leave the checkpoint consistent.

### Done (verified this session)
- **All 8 audit items verified in place** by direct file inspection +
  greps + test re-run:
  1. Working Windows test loop in §3 ✅
  2. HEAD-vs-runtime distinction present ✅
  3. Cross-ref fixed (state-dir bullet) ✅
  4. Guardian 09:35–15:44 ET band documented ✅
  5. Heartbeat-over-Get-Process liveness guidance ✅
  6. Stale `11:00-14:45` / `after 15:50` strings: ZERO remaining in
     `scripts/deploy.py` / `engine/watchdog.py` (grep clean); correct
     `11:00-14:15` / `after 15:44` text present; window LOGIC untouched ✅
  7. Root `AGENTS.md` routes to these files ✅
  8. Commit/push/verify chain intact ✅
- **Validation re-run this session:** `scripts/test_guardian_and_deploy.py`
  → **63 checks passed** (deploy-window assertions: 10:59 blocked, 11:00
  allowed, 14:14 allowed, 14:15 blocked, 15:44 blocked, 15:45 allowed,
  09:05 blocked); `compileall engine scripts` → exit 0.
- **Follow-up commit `f6989ee` (pushed, remote-verified):** the snapshot
  below hard-coded "Repository HEAD = 1a6f66b", which went stale as soon
  as the audit commits landed. Replaced with a durable mapping: live
  runtime baseline `8afbfb2` = last trading-code commit; check
  `git --no-pager log --oneline 8afbfb2..HEAD` — all-docs range = no
  deploy needed, any code commit = runtime behind, deploy due.
- Working tree dirty files are ALL runtime noise (`data/*.json`,
  `graphify-out/*`, `heartbeat.txt`, `.daily_state.json`, `day_picks.json`)
  — correctly never committed per convention #5.

### Current mapping (restate — do not re-derive)
- **Live runtime baseline = `8afbfb2`** (last commit with trading code).
- **Repo HEAD at this snapshot's writing = `f6989ee`** — docs-only commits
  on top of it (`5ccd85d`, `842fea7`, `dd786fc`, `f6989ee`), plus this
  checkpoint-update commit itself landing above. No restart needed; bot
  unaffected by docs commits.

### Next (unchanged — carries over from the 8afbfb2 snapshot)
1. Observe one session: `held_for_orders`/duplicate-close counts,
   `[CLOSE-RECON]`/`[BOUNDARY]` lines, `leverage_snapshot` events.
2. Build `scripts/analyze_daily_portfolio.py` (per-chain MAE/MFE +
   counterfactuals: leverage bands, churn caps, runway cutoffs).
3. Per-symbol daily chain ledger (telemetry → enforced loss budget).
4. Late-morning ~10:55 pending-entry cutoff (multi-day replay first).
5. Shadow high-momentum classification (Release 2; never blanket-reject
   big movers).
6. Whole-share risk-overshoot policy after analyzer data.

### Files
- This session: `AGENT_CHECKPOINT.md` (this snapshot + the `f6989ee`
  distinction rewrite in the snapshot below). No source changes.

---
## Snapshot — 2026-09-04 ~17:55 ET (CURRENT STATE — 2.0x release LIVE + AGENT_CONTEXT.md created; audit found doc fixes pending)

### Goal
Run the trading bot live with a 2.0× portfolio leverage ceiling, a 14:15–15:44 ET
afternoon session, and corrected boundary/order handling; leave the repo with a
quick-reload context file so any future session is immediately current.

### Done (verified live and committed)
- **Deployed:** commit `8afbfb2` was consumed by the watchdog 2026-09-04 15:25:22
  (log clock; = 16:25 ET wall clock, logs run ET−1h) and main.py relaunched in
  LIVE mode. Deploy flag absent; heartbeat fresh; book flat (0 positions).
  Day P&L +$7.53 (+0.371%). "ti_primary.json is empty!" errors after ~17:53 ET
  are the known TTL expiry fail-open (non-issue, §8 of AGENT_CONTEXT.md).
- **Live behavior:** gross stock exposure ceiling 2.0× with PRE-TRADE
  pending-notional headroom (`_size_with_buying_power`) + post-fill
  `enforce_portfolio_leverage`; entry windows second-precise/end-exclusive
  `[09:14,11:00)` + `[14:15,15:44)`; boundary cancellation of pending DAY
  entries at 11:00:00/15:44:00; NFLX-style race-fill re-close; reconciled
  closes (`dc40b1a`) via `_request_reconciled_close()`.
- **Repo/docs:** `AGENT_CONTEXT.md` created (commit `1a6f66b`, pushed, remote
  hash verified via `git ls-remote`) — the always-current quick-reload file,
  read FIRST in every session. `AGENT_CHECKPOINT.md` header now points to it.
- Tests at deploy time: 55/55 suites + compileall green.

### Important distinction (read before claiming deploy state)
- **Live runtime baseline = `8afbfb2`** (what main.py was restarted on 9/4 —
  the last commit containing trading code).
- **Repo HEAD at this snapshot's creation = `1a6f66b`** (docs-only commit on
  top of the release). Later docs-only commits (`5ccd85d`, `842fea7`,
  `dd786fc`) resolved the audit findings below — see the RESOLVED banner in
  the Next section — so the documentation HEAD can always sit AHEAD of the
  runtime baseline. Run `git --no-pager log --oneline 8afbfb2..HEAD` to see
  exactly how far ahead, and confirm every commit in that range is
  docs/tests-only before claiming the live bot is stale (a code commit in
  that range means the runtime IS behind and a deploy is due).
- The difference so far is documentation-only; no restart needed or wanted
  for docs commits.

### Known issue: this checkpoint's archive is NOT strictly date-sorted
The historical snapshots below are not in strict newest-first order (the
9/4 16:10 snapshot sits between two 9/3 ones). Always scan the snapshot
headings rather than trusting "first heading = latest".

### Next (audit findings on AGENT_CONTEXT.md — apply in a docs pass)
> **RESOLVED 2026-09-04 ~18:15 ET in commit `842fea7` (pushed, remote-verified):**
> items 1–7 below are done — Windows test loop fixed in §3, HEAD-vs-runtime
> distinction added to the footer, cross-ref fixed, guardian 09:35–15:44 ET band
> documented, liveness guidance (heartbeat over Get-Process) added, stale
> 14:45/15:50 strings corrected in `scripts/deploy.py` + `engine/watchdog.py`
> (logic untouched; 63-check watchdog/deploy suite + compileall green), and a
> root `AGENTS.md` now routes new agents to these files. No restart needed —
> live runtime remains on `8afbfb2`. Historical record of the findings:
1. Fix the Windows test command in §3: `python scripts\test_*.py` does NOT
   expand the wildcard (verified: exit 2, "Invalid argument"). Document either
   a PowerShell loop over `Get-ChildItem scripts\test_*.py` running the venv
   python, or the deploy-gate path `scripts/deploy.py --reason "..."`.
2. Clarify §3/§4 wording so repository HEAD is never conflated with the
   deployed runtime commit (see distinction above).
3. Fix the wrong cross-reference at §2 line 25 ("see §4" → coordination-file
   rationale lives in §2/state-dir bullet, not §4).
4. Add guardian's action band to §3: polls every minute but only ACTS
   09:35–15:44 ET (env `GUARDIAN_POLL_START_ET`/`GUARDIAN_POLL_END_ET`,
   defaults 09:35/15:44); `--force` overrides; weekend no-op.
5. Note in §2 that process-path-based `Get-Process` discovery of the bot is
   unreliable on Windows — heartbeat.txt freshness + log tails are the
   authoritative liveness checks.
6. Source still carries stale operator-facing times ("11:00-14:45", "after
   15:50") in `scripts/deploy.py` docstring/prints and `engine/watchdog.py`
   comments/log line ~453, even though the actual windows are 11:00–14:15 and
   after 15:44 (watchdog `_in_flat_deploy_window` logic is correct). Fix the
   strings; do NOT touch the window logic.
7. Optionally add a minimal root `AGENTS.md` that just says: read
   `AGENT_CONTEXT.md` first, then the newest snapshot in `AGENT_CHECKPOINT.md`.
8. Run compileall + watchdog/deploy-related tests, commit, push, verify with
   `git ls-remote`. No bot restart required (docs/strings only).

### Files
- `AGENT_CONTEXT.md` (new, `1a6f66b`), `AGENT_CHECKPOINT.md` (this update),
  plus the `8afbfb2` release files: `engine/config.py`,
  `engine/execution/enhanced.py`, `engine/utils/market.py`,
  `engine/orchestrator.py`, `engine/watchdog.py`, `engine/telemetry.py`,
  `scripts/deploy.py`, `scripts/guardian.py`, `scripts/_review_30d.py`,
  related tests.


## Snapshot — 2026-09-03 ~15:20 ET (Release 1: close/protection order reconciliation — IMPLEMENTED, TESTED, NOT YET DEPLOYED)

### Goal
Fix the 9/3 SNOW exit failure root cause (execution layer only — NO entry/sizing/stop changes):
the 1-share SNOW long's GTC trailing stop reserved the only share, so software SL / EMA9 / MFE
exits blind-cancelled + slept 0.4s + closed and Alpaca rejected the close 9x with 40310000
"insufficient qty available ... held_for_orders" while the position bled $380.25 -> $365.62 (-3.85%).

### What (portfolio policy unchanged: no entry blocks, no size reductions, no stop tightening)
- `engine/execution/enhanced.py`:
  - `ActiveOrderView` + `_normalize_order_view()` (enum/stub-tolerant order snapshot) and
    `classify_symbol_order()` — pure: valid_protection requires GTC + trailing_stop + closing side;
    entry/staged/reentry ids (apex-*) are NEVER protection; wrong-side never protection; partial
    protection detected via remaining_qty vs position.
  - `CloseResult` + `_request_reconciled_close()` — THE one entry point for intentional software
    closes: refresh position -> dedupe pending close (reconciled vs broker, resubmits only if the
    close order died and position remains) -> cancel ONLY classified protection -> bounded POLL
    (CLOSE_CANCEL_CONFIRM_TIMEOUT_SEC / _POLL_SEC) for cancel confirmation -> re-read position
    (cancelled stop may have filled) -> close exactly remaining qty via `_submit_closing_order`
    (now returns the accepted order, takes optional client_order_id) -> on close failure re-arm GTC
    trail (centralized fail-safe; both failing => `critical_unprotected` + telemetry).
  - Legacy behavior preserved when `CLOSE_RECONCILIATION_ENABLED=False` (same method, legacy branch).
  - Wired into: `check_software_stops` (stop-watch now cleared only on confirmed flat / PDT-block,
    not optimistically on submit; position-gone also clears `_pending_closes`), `check_ema9_exit`,
    `check_mfe_giveback_exit`. Pending-close dedupe guarantees ONE intentional close per symbol.
  - Self-test stub `_Ema9Stub` updated for the new indirection.
- `engine/telemetry.py` (NEW): non-blocking JSONL event log to
  `%LOCALAPPDATA%\ApexTrader\analytics\execution-events-YYYY-MM-DD.jsonl` — bounded queue, daemon
  writer, drops-on-full (counted), never raises into trading code, no credentials/network.
  Events: close_submitted / close_rejected / protection_cancel_requested / critical_unprotected.
- `engine/config.py`: `CLOSE_RECONCILIATION_ENABLED=True`, `CLOSE_CANCEL_CONFIRM_TIMEOUT_SEC=2.0`,
  `CLOSE_CANCEL_CONFIRM_POLL_SEC=0.25`, `PENDING_CLOSE_RETRY_SEC=10`, `EXECUTION_TELEMETRY_ENABLED=True`,
  `TELEMETRY_QUEUE_MAX`, `TELEMETRY_FLUSH_INTERVAL_SEC`.

### Tests — ALL GREEN
- NEW: `scripts/test_order_classification.py`, `scripts/test_reconciled_close.py` (12 scenarios incl.
  cancel-timeout defer, stop-fills-during-cancel, partial-fill qty re-read, re-arm fail-safe, PDT block),
  `scripts/test_snow_exit_reconciliation.py` (real 9/3 sequence replayed: FIRST breach closes, 6 poller
  ticks submit no duplicate, watch clears on confirmed flat), `scripts/test_telemetry.py`.
- Full suite: **54/54 `scripts/test_*.py` exit 0** + `python -m compileall -q engine scripts` OK +
  `git diff --check` clean.
- Note: working-copy AGENT_CHECKPOINT.md was stale (missing the 9/3 14:05/12:00/11:30 snapshots);
  restored from HEAD before this session's edits (no unique local content was lost).

### Deploy state — NOT DEPLOYED (human-gated)
- Nothing pushed, no deploy flag written. To deploy after review:
  `python scripts/deploy.py --reason "reconcile protective and software close orders"`
  (watchdog applies at the next flat window). Rollback: set `CLOSE_RECONCILIATION_ENABLED=False`
  (legacy cancel+sleep path restored through the same method) + redeploy.
- What's next: observe `held_for_orders` / duplicate-close counts + fill-to-protection latency for a
  session; then Release 2 (shadow high-momentum classification + offline portfolio evaluator) per plan.
- Commit plan: commit engine/config.py, engine/execution/enhanced.py, engine/telemetry.py,
  scripts/test_{order_classification,reconciled_close,snow_exit_reconciliation,telemetry}.py —
  NOT the runtime/generated noise (data/*.json, graphify-out, heartbeat).

---
## Snapshot — 2026-09-04 ~16:10 ET (2.0x leverage cap + 14:15 afternoon reopen + boundary fixes — IMPLEMENTED, TESTED)

### Implemented (all verified by tests)
- `engine/config.py`: `MAX_PORTFOLIO_LEVERAGE` env-overridable, default **2.0**, import assert
  [1.0, 2.0]; `ENTRY_WINDOW_BREAK_END_ET` **"14:15"** (afternoon reopen 30 min earlier; end stays 15:44).
- `engine/utils/market.py`: `within_entry_window`/`in_lunch_break` now SECOND-PRECISE and
  END-EXCLUSIVE — [09:14,11:00) + [14:15,15:44); the 11:00 and 15:44 minutes belong to the flatten
  (NFLX 15:44:37 race-fill lesson).
- `engine/execution/enhanced.py`:
  - `_size_with_buying_power`: PRE-TRADE gross-exposure headroom — filled exposure (abs, options
    excluded) + resting entry notional (`_pending_entry_signals` x `_entry_pending` qty) + this order
    <= equity x 2.0; downsizes to headroom, skips at 0 (no more fill-then-trim churn).
  - `_validate_trade`: equity_capacity now leverage-aware (equity x 2.0 x 0.95 / pos size = ~19
    slots at 10% base, was ~9 silently).
  - NEW `_cancel_pending_entry_orders(reason)`: cancels every resting DAY entry/re-entry/staged
    order (classify-based, GTC stops untouched), clears order_cache/_entry_pending/
    _pending_entry_signals/_staged_allocation. Called at EOD window open.
  - `close_eod_positions` reappearance fix: a symbol in `_eod_closed` whose position is STILL open
    with no active close order gets its done-mark cleared and is re-closed (NFLX race-fill).
  - `lunch_flat_positions`: clears local pending state after the sweep-wide cancel (no reviving
    dead morning orders at the 14:15 reopen).
  - `_maybe_rearm_reentry`/`check_blocked_entries_ema`: `>=` ENTRY_WINDOW_END_ET (past window at
    15:44:00, not 15:45).
  - `enforce_portfolio_leverage`: `leverage_snapshot` telemetry every grid tick.
- `engine/watchdog.py`: flat-deploy lunch window 11:00-**14:15**.
- `scripts/deploy.py`/`guardian.py`/`_review_30d.py`: 14:15/15:44 wording.

### Tests — ALL GREEN (55/55)
- Updated: test_entry_window (new matrix incl. end-exclusive 11:00/15:44), test_guardian_and_deploy
  (14:14 allowed/14:15 blocked), test_lunch_flat (14:15 wording), test_eod_close_rerun (resting-order
  contract + race-fill re-close), test_portfolio_leverage_cap (2.0).
- NEW: `scripts/test_pretrade_leverage_headroom.py` (8 cases: desired pass-through, headroom clip,
  at-cap skip, over-cap clamp, pending reservation, own-symbol exclusion, options exclusion, short
  abs-value).
- compileall engine+scripts OK. Deferred to a later release: daily portfolio analyzer script,
  per-symbol chain ledger (telemetry-only), late-morning 10:55 cutoff — per plan promotion criteria.

### Deploy state — commit + deploy gate next (human-gated flag write via scripts/deploy.py)

---
## Snapshot — 2026-09-03 ~16:20 ET (afternoon end 15:45 -> 15:44 ET — IMPLEMENTED, TESTED, DEPLOYING)

### Goal
User: "change afternoon trade ending at 3.44pm ET" — motivated by the 9/3 afternoon
post-mortem: MARA entered 15:43:42 and MSTX re-entered 15:44:43 were flattened at
15:50 with ~4 min of runway (-$2.55/-$1.20); afternoon net was -$1.02 vs a +$11.75 day.

### What — all boundaries moved together, same contract as the prior 15:50->15:45 move
- `engine/config.py`: `ENTRY_WINDOW_END_ET` "15:45"->"15:44", `EOD_CLOSE_TIME`
  "15:45"->"15:44", `GUARDIAN_POLL_END_ET` default ->"15:44"; import-time EOD-gap
  assert widened 10-15 -> 10-16 min (15:44 is a 16-min gap to the 16:00 close).
- `engine/execution/enhanced.py` `_exchange_close_for_today()`: ROOT-CAUSE FIX --
  previously the exchange-calendar path OVERRODE the configured EOD time
  (eod_at = close-10min = 15:50, which is why the live log showed eod_exit=15:50
  while config said 15:45). Now takes the EARLIER of configured EOD_CLOSE_TIME
  and calendar-close-minus-10min, so the user's 15:44 governs regular sessions
  AND early-close sessions still flatten in time.
- `engine/watchdog.py`: flat-deploy window after EOD now 15:45+ ET (was 15:46+).
- `scripts/guardian.py`: poll-end fallback ->"15:44". `scripts/deploy.py`: doc/messages.
- Tests updated: entry-window boundaries (15:44 inclusive / 15:45 outside),
  deploy-window matrix, timeline-sim discovery end, thin-liquidity comment,
  `_review_30d.py` W_PM_CLOSE + prints.

### Verification — ALL GREEN
- **54/54 `scripts/test_*.py` exit 0** + compileall OK + `git diff --check` clean.

### Deploy state — see next snapshot (committed dc40b1a lineage continues)
---
## Snapshot — 2026-09-03 ~14:05 ET (MFE give-back stop implemented + deploy)

### What
- New gain-retention exit: `check_mfe_giveback_exit()` in `engine/execution/enhanced.py`, wired into the SoftwareStopPoller `_tick()` in `engine/orchestrator.py`. Config: `MFE_GIVEBACK_ENABLED/ARM_PROFIT_PCT/GIVEBACK_FRACTION/BREAKEVEN_FLOOR_PCT` in `engine/config.py`.
- Rule: once a same-day position's peak unrealized gain reaches +0.5%, exit when current gain falls below max(60% of peak, entry+0.1%) — breakeven-plus ratchet. Pure decision fn `_mfe_giveback_reason()` is unit-testable; short-mirrored; same-day scope; GTC re-arm fallback on close failure.
- Motivation: 9/3 morning post-mortem — 41 trips peaked +$90.56 unrealized, realized +$1.22 (1.3% MFE capture). Analysis tooling: `%TEMP%\apex_peak_hold_analysis.py` (read-only Alpaca).
- Also fixed 2 time-dependent tests (test_pending_entry_ema_recheck, test_staged_allocation) that failed whenever run during the 11:00-14:45 lunch window — added the module's own `in_lunch_break = lambda *_: False` shim. This was silently blocking midday deploys.

### Verification
- New `scripts/test_mfe_giveback.py` green (uses the real 9/3 CONL/SMMT/HOOD/ASST/PLTR cases).
- Full suite: ALL 49 runnable tests exit 0 (test_notifications still skipped by hand, runs in deploy gate).

### Deployed LIVE (13:05:37 local / 14:05:37 ET)
- Commit `80cfe5a` pushed to GitHub (`origin/fix/ti-scraper-devtools-and-gtc-order-bug`, verified via ls-remote).
- `scripts/deploy.py` gate passed (all tests + compileall) -> flag written -> watchdog consumed it within ~2s -> main.py restarted 13:05:37 during the lunch flat window, 0 open positions. `.mainbot.lock` recreated 13:05:38; heartbeat fresh; guardian polling clean (+0.60% day, positions=0).
- MFE give-back is active for all same-day entries from the 14:45 ET PM window onward. NOTE: OneDrive made local `origin/*` refs stale twice today -- after any push, confirm with `git ls-remote origin` before trusting "Everything up-to-date".

### Deployed LIVE (13:19:47 local / 14:19:47 ET) -- commit `e82b3b6`
- Afternoon session ends **15:45 ET** (was 15:50): `ENTRY_WINDOW_END_ET`, `EOD_CLOSE_TIME`, `GUARDIAN_POLL_END_ET` (config default + guardian.py fallback), watchdog flat-deploy window now 15:46+ ET. `GUARDRAIL_EOD_CLOSE_TIME` follows `EOD_CLOSE_TIME` automatically.
- config.py import-time assert widened: EOD close must be 10-15 min before the 16:00 close.
- Tests updated (entry-window boundaries, deploy-window checks, timeline sim, thin-liquidity comment); ALL TESTS EXIT 0; gate passed; main.py restarted 13:19:47 during lunch flat, 0 positions, heartbeat fresh.



---

## Snapshot — 2026-09-03 ~12:00 UTC (cleanup pass)

- Removed `engine/equity/scan.py.bak` (stale backup, was never imported; scan smoke + morning readiness re-passed after removal).
- Earlier same day: removed stray junk file `t-Path ..claude) {...}` at repo root.
- Working tree categorized for commit (see session log): source changes vs runtime-generated noise. NO commits made by agent.

---

## Snapshot — 2026-09-03 ~11:30 UTC (readiness validation pass, no code changes)

### Goal
"Check everything is good to go on the code" — full read-only validation.

### Result — ALL GREEN
- Interpreter: managed venv `%LOCALAPPDATA%\ApexTrader\venv\Scripts\python.exe` (Python 3.12.9). Core imports (alpaca, pandas, dotenv, requests, psutil, tenacity, pytz) OK via `scripts/test_scan_smoke.py` import chain.
- `python -m compileall`: no syntax errors in project code.
- Test suite: **all 48 runnable `scripts/test_*.py` exit 0** (skipped `test_notifications.py` — sends a real email). Verified with direct exit codes, not just job state.
- Live bot healthy: main PID 34084, watchdog PID 17484, heartbeat fresh.
- Cleanup: removed stray junk file `t-Path ..claude) {...}` at repo root (accidentally created by a malformed pasted PowerShell command).

### Notes / non-blockers
- pytest is NOT installed in either venv — tests here are script-style (`python scripts/test_x.py`), which is the convention; install pytest only if pytest-style tests are wanted.
- Working tree still has large uncommitted diff (62 files, +861/−7574 incl. options-module deletion — matches "options removed 2026-09-01"); commit when convenient.
- `engine/equity/scan.py.bak` leftover backup file — since removed (see 12:00 UTC snapshot above).
- LF→CRLF warnings on `git diff` are cosmetic (core.autocrlf).

### What's next
Nothing pending from this pass.

---


## Persistent codebase context — graphify knowledge graph (2026-09-03)

A graphify knowledge graph of the entire codebase lives at `graphify-out/`
(built from commit `4cc9faac`; 110 code files -> 1124 nodes, 2209 edges,
91 communities). Use it FIRST for architecture/codebase questions instead of
grepping — it survives LLM restarts because it is on disk.

- `graphify-out/GRAPH_REPORT.md` — plain-language audit report (god nodes,
  community hubs, surprising connections). Read for broad orientation.
- `graphify-out/graph.html` — interactive graph; open in a browser.
- CLI (venv: `.\apextrader\Scripts\graphify.exe`):
  - `graphify query <symbol>` — BFS neighborhood of a symbol/file
    (e.g. `query EnhancedExecutor`). Works with node names only.
  - `graphify path 'main.py' 'scan_and_trade()'` — shortest path; function
    labels need trailing `()`, files just the filename.
  - `graphify god-nodes --top 12` — architectural hubs
    (top: EnhancedExecutor, Signal, get_bars(), MarketState, AutoBotWatchdog).
  - `graphify update .` — refresh after code edits (AST-only, no API cost).
  - `graphify cluster-only .` — full recluster + report after big refactors.
  - NOTE: `graphify explain` is broken in installed graphify 0.9.32
    (ValueError in `_find_node_tiers`) — use `query` instead.
- Scoping: `.graphifyignore` excludes `apextrader/` (venv), `data/`,
  `predictions/`, logs, `.env`, caches — code + docs only.
- After changing code, run `graphify update .` so the graph stays current.

---


## Snapshot — 2026-09-02 ~22:40 ET (session complete, nothing pending)

### Goal
"Auto implement everything" — close out the remaining open recommendations from
the 09:25-morning-readiness work: (1) the .env-mtime restart storm vector, (2)
the `ti_primary.json is empty` log spam, (3) stage session work, (4) live-roll
both fixes.

### State — DONE, DEPLOYED LIVE, VERIFIED
- `engine/watchdog.py`: `.env` restart trigger now compares **content hash**
  (new `AutoBotWatchdog._env_hash()` staticmethod, sha256 of raw bytes, None
  when missing) instead of mtime. OneDrive sync touches `.env` mtime without
  changing content -> NO MORE main.py restart storm (this was the 2026-09-02
  morning 10-restart root cause; top open risk now CLOSED). Log line is now
  "Detected .env content change, restarting main.py".
- `engine/equity/scan.py` `get_scan_targets()`: universe-health notices
  (empty/too-small) rate-limited to once per 5 min via module-level
  `_UNIVERSE_HEALTH_LAST_LOG` (monotonic); limiter RE-ARMS when the universe is
  healthy again so a fresh outage logs immediately. "Static universe lists are
  empty" still logs every occurrence (true zero-coverage is critical). Was
  spamming every 5s overnight (10k+ lines/day, TTL 125-min expiry).
- Tests: `scripts/test_guardian_and_deploy.py` extended to 61 checks (new
  `test_watchdog_env_hash_gating`: missing->None, 64-hex, stable, **mtime-only
  touch does NOT change hash** regression proof, content change -> new hash,
  run-loop wiring uses `_env_hash()` not st_mtime). NEW
  `scripts/test_universe_health_ratelimit.py` (5 checks: logs once, suppressed
  second call, healthy re-arms, fresh episode logs, too-small=warning).
- **Full battery: 49 suites, 0 failures, run twice.** compileall clean.
- **Deployed live 22:28 ET** via controlled watchdog restart (needed because
  watchdog code changes only take effect when the watchdog itself restarts):
  `schtasks /End` + kill old tree (28440 watchdog, 27376 main.py, 4760 worker)
  + `schtasks /Run /TN ApexTraderAutoRun`. New watchdog pid 31496; main.py
  relaunched on new code; heartbeat advancing (22:32:33); **live proof: exactly
  1 empty-universe error since boot vs one every 5s before.**
- Staged (git): engine/watchdog.py, engine/equity/scan.py (M);
  scripts/test_guardian_and_deploy.py, scripts/test_universe_health_ratelimit.py (A).
  AGENT_CHECKPOINT.md left untracked by design.

### 2026-09-03 ~05:40 — COMMITTED & PUSHED to github.com/itisbg/BitStrider
- Branch `fix/ti-scraper-devtools-and-gtc-order-bug` pushed; session commit
  "Morning-readiness pipeline, watchdog hardening, and test battery"
  (local 4cc9faa / remote faca8f3 = WIP snapshot of the working tree on top).
- Push initially REJECTED: historical commit e5fee9c contained
  autobot_scheduler.log.broken_20260806 (110.77 MB > GitHub 100 MB limit).
  Purged via filter-branch in a temp clone (OneDrive .git hung the in-place
  rewrite), force-pushed. Remote tip f38cbd9 was verified an ancestor of local
  work first -> nothing lost. Local repo realigned to the purged history
  (trees verified identical before ref move). Old 110MB blob still exists in
  local .git objects (harmless; GC eventually). NEVER re-add
  autobot_scheduler.log.broken_20260806 (and *.log files generally) to git.
- `git stash@{0}` ("pre-purge-backup") kept as a full working-tree snapshot;
  can be dropped once comfortable: git stash drop.
- Uncommitted user refactor (options/etrade/ti-capture module removals etc.)
  intentionally NOT committed — only session work was pushed.

### Still open (user decisions / future work)
- **FORCE_SCAN is active in .env** (log: "[SYSTEM] FORCE_SCAN active -- bypassing
  market-hours gate" on every boot incl. 22:28). If unintentional, remove it
  from .env — NOTE: with the hash-gated watchdog, editing .env CONTENT now
  correctly triggers ONE restart (desired behavior, no storm).
- venv is OneDrive-fragile (pyvenv.cfg clobber) — watchdog already self-heals
  via `_ensure_virtualenv()` each cycle.
- Commit the staged work when convenient (OneDrive has reverted files before).

---

## Snapshot — 2026-09-02 ~17:00 ET (session complete, nothing pending)

### Goal
Make all polling loops ready by 09:25 ET (user: "check all the polling loops start
at 9.25AM ET to avoid delays") — fix the 09:35:43 first-order delay caused by the
morning restart storm + ActiveListRefresher spacing + clock-grid blind spots.

### State — DONE, DEPLOYED, VERIFIED (no remaining work on this goal)
- `engine/config.py`: added `MORNING_READINESS_ET = "09:25"` + import-time assert
  `PREP(09:05) < ENTRY(09:14) < READINESS(09:25) < MARKET_OPEN(09:30)`.
- `engine/orchestrator.py`:
  - module-level `_readiness_kick = threading.Event()` (+ `import threading`);
  - ActiveListRefresher waits on `_readiness_kick` (with timeout) instead of a bare
    sleep — kick forces immediate ti_capture + Alpaca movers + `prewarm_entry_ema`;
  - main-loop once-per-day 09:25 ET trigger (`readiness_due`, state var
    `readiness_scan_date`): forces fresh scan + sets the kick; fires immediately on
    a late boot (covers the 09:29:46-restart case); scoped to the morning segment
    only (afternoon has its own 14:45 reopen);
  - `_schedule_on_clock_grid` fires each job once immediately at registration
    (pre-grid warm-up) so drift/concentration checks are never blind across the open.
- `scripts/test_morning_readiness.py`: NEW regression net (10 checks).
- **Tests all green:** `py_compile` both files; `test_morning_readiness.py` 10/10;
  `test_scan_smoke.py`; `test_entry_window.py`; `test_lunch_flat.py`;
  `python -m engine.orchestrator` self-test (its 16:39:49 ERROR lines in
  apextrader.log are SIMULATED demo failures — not real).
- **Deployed live:** `deploy_requested.flag` consumed by watchdog 16:40:31 ET,
  main.py relaunched 16:40:36 ET on new code; boot log shows the new
  "[SCHEDULE] ... first tick fired at registration" lines; heartbeats flowing.

### Verified non-issues (do not re-investigate)
- `BodyTimeoutError (UND_ERR_BODY_TIMEOUT)`: Node/undici timeout inside the **Cline
  VS Code extension's** API request to the LLM provider (confirmed in VS Code logs:
  `%APPDATA%\Code\logs\<session>\window...` `1-Cline.log` — "send() completed:
  terminated: BodyTimeoutError ... inputTokens=330489" at 21:17:10 UTC / 17:17 ET,
  2026-09-02). The streamed response stalled longer than undici's body gap timeout,
  so the request was aborted and Cline paused. Network/tooling event — NOT this
  repo's code; bot unaffected (0 hits in ApexTrader logs). Mitigation: keep
  per-task context small (330K-token request = high stall exposure), start fresh
  tasks when context balloons, resume from AGENT_CHECKPOINT.md after any kill.
- "SoftwareStopPoller has not ticked in 200s" / "network down" / "_TickExecutor has
  no attribute" log lines at 16:39:49: orchestrator `_demo()` self-test output
  sharing the same log file. Harmless.
- `[UNIVERSE HEALTH] ti_primary.json is empty!` spam after ~17:53 ET: PRE-EXISTING
  designed fail-open, not from the 09:25 changes. `data/ti_primary.json` is written
  by the Yahoo-universe producer; `TI_PRIMARY_TTL_MINUTES = 125`
  (engine/equity/universe.py:44) — 125 min after the last capture (15:48 ET)
  `_get_ti_primary()` returns [] and every scan cycle logs this + falls back to the
  static universe. Yesterday's log had 10,477 of these (today: 216). Bot is flat
  after hours so nothing trades on the fallback. Optional cleanup: demote to
  DEBUG/warning when outside the discovery window, and/or extend the overnight
  capture cadence so the TTL doesn't expire every evening.

### Rigorous re-verification — 2026-09-02 ~18:00 ET (all green)
- `python -m compileall engine`: OK (whole package).
- ALL 45 `scripts/test_*.py` suites: PASS (44 pass + `test_clock_grid_schedule.py`
  was updated 2026-09-02 to pin the new contract: registration fires the job
  exactly ONCE immediately (pre-grid warm-up) then registers the :00/:10/... grid;
  args forwarded on both the immediate and grid fires; grid shape + loud interval
  validation unchanged). Re-run of it and test_morning_readiness.py: green.
- Code review of every edited region (imports, _schedule_on_clock_grid,
  _readiness_kick + refresher wait, main-loop readiness block, config asserts):
  intact and correct.
- Live process: main.py PID 6612 started 16:40:36 ET (matches watchdog relaunch);
  disk mtimes of engine/orchestrator.py + config.py predate start => running code
  == disk code. Watchdog PID 28440 up since 15:02. Deploy flag consumed (none
  pending). Heartbeats current, zero real ERRORs from the live process post-boot.

### Red-team pass — 2026-09-02 ~18:20 ET (2 hardenings applied, deployed #2)
- NEW `scripts/test_readiness_redteam.py` (39 checks, all green): trigger
  boundary matrix (09:24:59/09:25:00/10:59:59/11:00:00/afternoon, late boot,
  once-per-day latch, next-day re-arm), weekend boots, kick Event races
  (set-before-wait / set-during-work / past-deadline clamp / concurrent set),
  grid job raising at registration (grid survives), malformed config times,
  ordering-guard effectiveness, discovery-window at the kick moment.
- HARDENING 1 (orchestrator readiness_due): weekday gate `now_et.weekday() < 5`
  — a Saturday/Sunday 09:25 boot no longer forces scans/kicks (red-team found
  NO weekday gating anywhere in discovery/orchestrator).
- HARDENING 2 (config.py): `_require_hhmm()` import-time validation of all 13
  "HH:MM" constants. Red-team caught that strptime ALONE accepts "9:5", which
  breaks `_within_discovery_window`'s RAW-STRING comparisons ("9:5" > "10:00"
  lexicographically) — so the validator enforces strict zero-padded `\d{2}:\d{2}`
  + range validity. A malformed constant now fails loudly at import instead of
  error-spamming the live loop into a watchdog stall-restart storm.
- Full regression after hardening: 47/47 suites PASS (incl. the two new ones).
- DEPLOY #2: flag consumed 17:23:51 ET, main.py PID 17296 relaunched 17:23:56 ET
  on the hardened code; heartbeat at boot; ti_primary TTL message still present
  (documented pre-existing non-issue above).

### Deep dive — 2026-09-02 ~18:50 ET (timeline simulation, 3rd hardening, deployed #3)
- NEW `scripts/test_morning_timeline_sim.py`: deterministic 1s-step discrete-event
  simulation of the whole morning (main loop 5s tick + blocking ~100s scans,
  ActiveListRefresher cadence + kick semantics incl. kick-while-busy, clock-grid
  registration fires, watchdog restarts resetting everything). 8 scenarios, 20
  checks, all green: normal day, TODAY'S restart storm (9 kills 08:52-09:34),
  late boot 09:29:46, boot 09:21 (grid-blind window), Saturday boot, mid-morning
  boot, after-hours boot, exact-09:25:00 boundary boot.
- FINDING 1 (design confirmed): readiness re-fires once PER BOOT inside
  [09:25,11:00) — correct self-healing, since every restart wipes the in-memory
  state; each continuous run fires at most once. In the storm sim, EVERY run
  overlapping the window re-armed scan+prewarm immediately and a prewarm still
  started in [09:25, 09:30) despite 9 restarts.
- FINDING 2 (real bug fixed): the main loop's scan-trigger block had NO weekday
  gate and the discovery-window check is time-only — a Saturday 09:14/adaptive
  trigger would run a full scan against closed markets. Fixed: the whole trigger
  block is now weekday-gated (protective + poller schedule jobs unaffected —
  they self-gate). Deployed: flag consumed 17:48:15 ET, main.py PID 26672
  relaunched 17:48:20 ET on the final code.
- Full regression after deep-dive: 47/47 suites PASS, compileall OK.
- DST analysis: trigger/window comparisons use America/New_York local time via
  now() (pytz); the 09:25 band never intersects the 02:00 DST transitions
  (2026: Mar 8 started EDT, Nov 1 ends), so no ambiguity possible.
- Watchdog interplay: heartbeat.txt was written after every scan cycle; scans
  run ~100s vs STALL_RESTART_SECONDS=900 (9x margin); stall restarts are also
  flat-window-gated like deploys.
  **2026-09-02 ~22:00 CORRECTION (this analysis was WRONG):** off-hours the
  adaptive interval stretches to 20 min (SCAN_INTERVAL_CALM_VOL) > the 900s
  stall threshold, so a healthy sleeping bot was killed as "hung" every ~15
  min -- live-observed 19:23-21:30 ET as 7+ consecutive stall restarts. FIX
  (deploy #5, 21:54:37 ET): heartbeat is now MAIN-LOOP LIVENESS -- the loop
  touches it every 5s tick via _touch_heartbeat() (60s rate limit; force=True
  after each cycle). Regression test: scripts/test_heartbeat_liveness.py
  (10 checks). Verified live: heartbeat advances with zero scan cycles.

### Targeted hardening pass #2 — 2026-09-02 (evening): DONE, deployed

1. **Guardian halt dedupe now date-scoped** (orchestrator.py): `guardian_halt_acted` bool
   → `guardian_halt_acted_date: Optional[date]`. New testable helper `_maybe_guardian_halt(ctx)`
   (called first on every `_tick`); dedupes on the flag payload's own date, unparsable date
   falls back to today ET. Fixes: a process-lifetime bool blocked the NEXT day's guardian
   flatten (watchdog keeps main.py alive across midnight).
2. **Watchdog `.env` kill switch is real** (watchdog.py): module const `DEPLOY_RESTART_ENABLED=True`
   removed; new `_deploy_restart_enabled()` reads os.environ + `.env` live (`.env` wins).
   Semantics: missing/blank/invalid → enabled (one-time warning on invalid); only explicit
   0/false/no/off disables. Gates `_deploy_restart_requested`.
3. **Staged tranches no longer blocked by first-entry state** (enhanced.py): `_submit_entry_order`
   gained `scale_in=True` — bypasses ONLY the 60s `_recent_entry_submits` debounce + `order_cache`
   slot; `_entry_pending`/`_pending_entry_signals`/broker active-order checks stay enforced.
   `maybe_add_staged_tranches` now submits with `scale_in=True`. Decision: Option B (scale in
   promptly after first fill) — the staged path already proves open position + gain + fresh EMA;
   a resting unfilled first tranche still blocks via the broker check.
Tests: test_guardian_and_deploy.py 55/55 (+22 new: 4b halt dedupe, 5b kill switch incl. full
gate flow), test_staged_allocation.py all pass (scale-in add + pending/broker/fresh-entry
guards), test_morning_readiness.py pass, compileall clean, git diff --check clean (pre-existing
CRLF warnings only). Deploy #4 via flag → verify "[DEPLOY] deploy flag consumed" in autobot.log.

### Open recommendations (next candidates, not started)
1. Watchdog `.env`-mtime restart is still armed with `.env` in the OneDrive repo —
   the root cause of the 2026-09-02 morning restart storm (10 kills 08:48–09:35 ET).
   Recommend: restart on content-hash change only, or move `.env` machine-local
   (STATE_DIR), or defer `.env` restarts during 09:05–09:35 ET.
2. Prep-scan state (`_prep_scan_date`) is in-memory only — a restart re-runs prep.
   Consider persisting to STATE_DIR (like `.quarterly_state.json`).
3. urllib3 pool size (20) < concurrent demand — consider raising above worker count.
4. Old 375MB `autobot_scheduler.log` still in the OneDrive repo — archive/delete;
   live logs now go to `%LOCALAPPDATA%\ApexTrader\logs\` (machine timestamps = ET−1).

### Environment quick facts
- Venv: `$env:LOCALAPPDATA\ApexTrader\venv\Scripts\python.exe`
- Live logs: `%LOCALAPPDATA%\ApexTrader\logs\apextrader.log` (bot) +
  `autobot.log` (watchdog); repo-root `*.log` files are LEGACY.
- Deploy: write reason text into `%LOCALAPPDATA%\ApexTrader\state\deploy_requested.flag`
  — watchdog consumes it and restarts main.py during flat windows (11:00–14:45,
  after 15:50 ET). Never edit `.env` to deploy.
- Tests: `scripts\test_*.py`, run with the venv python from repo root.

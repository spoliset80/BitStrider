# AGENT_CONTEXT — quick-reload file (read this FIRST in every new session)

> **Reload protocol:** read this file top-to-bottom (~2 min), then the LATEST
> snapshot section at the top of `AGENT_CHECKPOINT.md` (session history, newest
> first). You are then current: environment, live state, conventions, and open
> work — without re-asking questions. Update THIS file whenever a fact in it
> goes stale (config values, deploy state, file map); use AGENT_CHECKPOINT.md
> for per-session history. Update AGENT_CHECKPOINT.md before risky steps and
> after verified milestones.

---

## 1. What this project is

ApexTrader — LIVE-money automated trading bot (Alpaca, `TRADE_MODE=live`).
Multi-strategy equity momentum (TopList fallback + named strategies), two
entry windows/day, lunch-flat, EOD-flat, layered exits (broker GTC trail,
EMA9/EMA7-15, MFE give-back, software SL), loss guardian + watchdog +
test-gated auto-deploy. Account ~$2,000 equity, small-account rules apply
(<$5,000 threshold). Gross stock exposure ceiling 2.0x equity.

## 2. Environment quick facts

- **Repo:** `C:\Users\BG\OneDrive\Returns-LSTM\StockPricePrediction\BitStrider-main`
  (OneDrive-synced — coordination files must NEVER live here; they live in the
  machine-local state dir, see the State-dir bullet below).
- **Branch:** `fix/ti-scraper-devtools-and-gtc-order-bug` (working branch; pushed to origin).
- **Python (bot + tests):** `C:\Users\BG\AppData\Local\ApexTrader\venv\Scripts\python.exe`.
  Repo-root `apextrader\` venv is LEGACY/OneDrive-broken — do not use.
- **Live logs (machine-local, authoritative):** `%LOCALAPPDATA%\ApexTrader\logs\`
  — `apextrader.log` (bot), `autobot.log` (watchdog), `guardian.log`.
  Repo-root `*.log` files are LEGACY. **Log timestamps = ET − 1h** (machine clock);
  Alpaca fill timestamps converted via pytz are true ET. Always convert before
  comparing log lines to fills.
- **State dir (flags, machine-local):** `%LOCALAPPDATA%\ApexTrader\state\`
  — `deploy_requested.flag`, `flat_request.flag`, `guardian_state.json`, locks.
- **Liveness checks (authoritative):** `heartbeat.txt` freshness in the repo
  root (orchestrator rewrites it every completed main-loop tick) + tails of
  the live logs. Do NOT rely on `Get-Process`/process-path discovery to prove
  the bot is down — command-line/path matching is unreliable under Windows
  permissions and has produced false "bot not running" reads; a fresh
  heartbeat outranks a missing process listing.
- **Analytics/telemetry:** `%LOCALAPPDATA%\ApexTrader\analytics\execution-events-*.jsonl`
  (non-blocking JSONL via `engine/telemetry.py`).
- **OneDrive gotchas:** local `origin/*` refs go stale after pushes — verify with
  `git ls-remote origin`; `.env` mtime syncs caused the 9/2 restart storm
  (watchdog now keys on content hash); never put coordination flags in the repo.

## 3. Run / test / deploy

- **Run all tests (script-style, no pytest):** Windows does NOT expand
  `scripts\test_*.py` when passed to python (verified: exit 2 "Invalid
  argument"). Run the ~55 suites via a PowerShell loop from the repo root:
  ```powershell
  $py = "$env:LOCALAPPDATA\ApexTrader\venv\Scripts\python.exe"
  Get-ChildItem scripts\test_*.py | ForEach-Object {
      & $py $_.FullName
      if ($LASTEXITCODE -ne 0) { throw "$($_.Name) failed" }
  }
  & $py -m compileall -q engine scripts
  ```
  Every suite must exit 0. (The deploy gate in `scripts/deploy.py` runs the
  same loop internally; use deploy.py only when you also want the restart
  flag written.) `git diff --check` (CRLF warnings are cosmetic).
- **Deploy (zero PowerShell, test-gated):**
  `%LOCALAPPDATA%\ApexTrader\venv\Scripts\python.exe scripts\deploy.py --reason "..."`
  → runs the full test gate → writes `deploy_requested.flag` → watchdog restarts
  main.py ONLY in flat windows: **11:00–14:15 ET** (lunch flat) or **after 15:44 ET**
  (post-EOD) until 09:05 next prep. Outside windows the flag waits. Verify:
  "[DEPLOY] deploy flag consumed" in autobot.log + fresh heartbeat.
- **Daily improvement loop (2026-09-08, observe-only until opted in):**
  `scripts/daily_automation.py` runs 12:05–14:00 ET weekdays (ET enforced in-script
  via pytz; the `ApexTraderDailyImprovement` task fires a 15-min cadence through the
  local midday as a carrier). Flow: observe (`scripts/analyze_daily_portfolio.py`,
  read-only Alpaca fills → ladder round trips, churn chains, runtime health) →
  PLAN (Cline CLI `--plan`, bounded; writes `candidate.json`) → objective evidence
  gates (`evaluate_candidate`: ≥5 days/≥20 trades, min effect + relative improvement,
  drawdown tail, allowed_files inside repo and never `.env`, prohibited changes +
  acceptance tests declared, heartbeat/guardian/flat/deploy-flag healthy, market open)
  → ACT (bounded, allowed_files only) → full test gate → VERIFY (independent agent +
  controller diff check) → deploy ONLY via `scripts/deploy.py` (never `--skip-tests`)
  and only with `--allow-deploy` or `AUTOMATION_ALLOW_DEPLOY=1`; default is
  OBSERVE_ONLY (Cline CLI missing / gates fail / saturation → recorded, no code
  change). Artifacts machine-local: `%LOCALAPPDATA%\ApexTrader\automation\<date>\`
  (`observation.*`, `candidate.json`, `test-results.json`, `run-state.json`,
  `compact-handoff.md`, `daily-run.log`). Lock:
  `%LOCALAPPDATA%\ApexTrader\state\daily_automation.lock` (stale-recovering).
- **Rollback:** per-feature config toggles (§6) or `git revert` + redeploy.
- **Never** edit `.env` to deploy; never bypass the test gate (`--skip-tests`).
- **Guardian (independent, scheduled 1/min):** alert −0.75% / hard halt −1.5% →
  `flat_request.flag` → bot flattens ≤5s; direct flat-sell if heartbeat stale
  (>300s). It only ACTS inside its band **09:35–15:44 ET** weekdays
  (`GUARDIAN_POLL_START_ET`/`GUARDIAN_POLL_END_ET` in .env; `--force` overrides
  for manual tests); outside the band it logs a no-op.

## 4. Current live behavior (verify against config.py if in doubt)

- **Schedule (second-precise, END-EXCLUSIVE — the end minute belongs to the flatten):**
  Morning entries `[09:14, 11:00)` → lunch flat 11:00 → afternoon entries
  `[14:15, 15:44)` → EOD flat 15:44. `_exchange_close_for_today` takes the
  EARLIER of configured EOD time and calendar-close−10min (early closes safe).
- **Leverage:** `MAX_PORTFOLIO_LEVERAGE = 2.0` (env-overridable, guard [1.0, 2.0]).
  Enforced PRE-TRADE in `_size_with_buying_power` (filled exposure + resting entry
  notional + this order ≤ equity×cap; options excluded; shorts by abs value) AND
  post-fill by `enforce_portfolio_leverage` (10-min grid, losers trimmed first,
  cancels outstanding entry orders on breach). Capacity ~19 slots at 10% base
  (SMALL_ACCOUNT_MAX_POSITIONS=24 count cap on top).
- **Sizing:** 10% base allocation, round-to-nearest share, caps = min(desired,
  usable BP/price, concentration 26.7%, gross headroom). Confidence ramp ≥85%,
  Kelly mult (GapBreakout 2.0 / TrendBreaker 0.25), thin-liquidity 4% override,
  re-entry after prior loss −30% size. NO account-risk-per-trade ceiling (one
  share of a $380 stock ≈ 19% of equity — known accepted risk; warning-only).
- **Entries:** 0.25% trailing-stop DAY orders (`REENTRY_TRAIL_PCT`), staged 4×25%
  (never add while losing + fresh EMA recheck), EMA7/EMA15 hard gate (2 closed
  candles), momentum-freshness 5%/30min hard reject, TopList = fallback strategy.
- **Exits (all same-day, nothing overnight):** broker GTC ATR trail (floor 1.5%,
  ATR×1.5, cap 4%), EMA9 0.5% trail + EMA7/15 reversal, MFE give-back
  (arm +0.5%, exit below max(60% of peak, +0.1%)), software SL for PDT-blocked,
  after-hours marketable-limit stops, no-gain exit, lunch/EOD flatten.
  ALL intentional software closes go through `_request_reconciled_close()`
  (classify orders → cancel only GTC protection → bounded poll for cancel
  confirmation → re-read qty → close exactly the remainder → re-arm on failure).
- **Boundaries:** at 11:00:00 and 15:44:00 ALL pending DAY entry orders are
  cancelled (`_cancel_pending_entry_orders`), local pending state cleared,
  flatten begins; race-fills are re-closed (EOD `_eod_closed` reappearance logic).
- **Re-entry:** no time cooldown; first-30-min loss blocks until 10:30; 30-min
  directional gate after 10:00; max 2 daily losses per symbol; `_loss_reentry_required`.

## 5. Architecture map (single sources of truth)

- **TRADING_SYSTEM.md — full trading-system specification** (strategy
  selection, guardrails, sizing, exits, time windows, safeguards; values
  verified 2026-09-06; config.py still authoritative). Read this before
  tracing any trading behavior.

- `engine/config.py` — ALL constants + import-time validation (`_require_hhmm`,
  window ordering asserts, leverage guard). Time constants are zero-padded
  "HH:MM" strings, also compared as raw strings — keep padding.
- `engine/utils/market.py` — `within_entry_window` / `in_lunch_break` (the ONLY
  window membership source; second-precise, half-open intervals).
- `engine/execution/enhanced.py` (~6.5k lines, god-module) — EnhancedExecutor:
  sizing, entries, all exits, protection, reconciliation, EOD/lunch flatten,
  leverage enforcement. Key helpers: `classify_symbol_order` (order purpose;
  client-id prefixes `apex-entry-/apex-staged-/apex-reentry-trail-/apex-close-`
  are authoritative), `_request_reconciled_close`, `_cancel_pending_entry_orders`,
  `_atr_trail_pct_for`, `_trail_pct_for`.
- `engine/orchestrator.py` — main loop, clock-grid jobs, SoftwareStopPoller
  thread (5s: pending-entry EMA recheck, staged adds, naked-cover, software SL,
  EMA9, MFE, blocked entries), guardian halt consumer, entry-window triggers.
- `engine/telemetry.py` — never-raise JSONL logger; trading must NEVER block on it.
- `engine/watchdog.py` — stdlib-only supervisor: crash restart, heartbeat stall,
  deploy-flag consumer (flat-window gated), .env content-hash restart.
- `scripts/guardian.py` — independent loss backstop (self-contained env parsing).
- `scripts/deploy.py` — test gate + flag writer.
- `engine/session/session.py` — daily equity baseline/P&L (`.daily_state.json`).
- `scripts/analyze_daily_portfolio.py` — daily observation (fills → position-ladder
  round trips, per-symbol/churn/entry-band stats, runtime health). READ-ONLY.
- `scripts/daily_automation.py` — daily improvement controller (observe → plan →
  act → verify → deploy, all evidence-gated, fail-closed, deadline-aware;
  `scripts/test_daily_automation.py` covers it offline).
- `scripts/_review_30d.py` — reliable Alpaca fill→position-ladder reconstruction
  (use this for trade analysis; `_audit_trades.py` naive sums are misleading).
- `graphify-out/` — codebase knowledge graph (see AGENT_CHECKPOINT "Persistent
  codebase context" section; use `graphify query <Name>` for orientation).

## 6. Feature toggles (rollback switches)

`CLOSE_RECONCILIATION_ENABLED` (reconciled closes), `MFE_GIVEBACK_ENABLED`,
`ATR_TRAIL_ENABLED`, `STAGED_ALLOCATION_ENABLED`, `LUNCH_FLAT_ENABLED`,
`EOD_CLOSE_ENABLED`, `DEPLOY_RESTART_ENABLED` (.env, watchdog),
`MAX_PORTFOLIO_LEVERAGE` (.env), `TRADE_STALE_MOMENTUM_REJECTS=False`
(hard reject), `GAP_CHASE_GUARD_ENABLED=False`.

## 7. Conventions & hard rules

1. Tests are script-style asserts ending with `print("OK: ...")` — mirror that
   pattern; no pytest. Time-dependent tests shim `in_lunch_break` /
   `ENTRY_WINDOW_END_ET` at module level (see existing examples).
2. Pure decision functions get extracted for testability (`_x_reason(...)`
   static methods tested without a broker).
3. Never block the 5s poller on unbounded network waits; every check keeps its
   own try/except; telemetry never raises into trading code.
4. GTC trailing stop = protective exit, NEVER an entry; DAY trailing stop =
   entry. Classification goes through `classify_symbol_order`, not ad-hoc checks.
5. Commits: source/tests/docs only — never `data/*.json`, `graphify-out/*`,
   `heartbeat.txt`, `.daily_state.json`, `day_picks.json` (runtime noise).
6. Update AGENT_CHECKPOINT.md before risky steps and after milestones; keep
   THIS file's facts current.
7. User works plan-mode first for analysis, then approves act mode.

## 8. Verified non-issues (do not re-investigate)

- `UND_ERR_BODY_TIMEOUT` = Cline/VS Code API stall, not the bot.
- `ti_primary.json is empty!` after ~17:53 ET = TTL (125 min) expiry fail-open, by design.
- orchestrator `_demo()` self-test writes SIMULATED ERROR lines into apextrader.log — harmless.
- Repo-root `*.log` are legacy; live logs in %LOCALAPPDATA%.
- `test_notifications.py` sends real email — runs in deploy gate only.
- LF→CRLF git warnings are cosmetic.

## 9. Deferred / next candidates (evidence-gated, from 9/3–9/4 post-mortems)

1. `scripts/analyze_daily_portfolio.py` — per-chain MAE/MFE + portfolio
   counterfactuals (leverage bands, churn caps, runway cutoffs). Tooling only.
2. Per-symbol daily chain ledger (telemetry → then enforced cumulative loss
   budget). Motivation: 9/4 GPRO+AXTX = 24% of gross losses via repeated churn.
3. Late-morning cutoff ~10:55 (cancel pending entries; 10:30–11:00 was net
   −$8.02 on 9/4). Multi-day replay first.
4. Post-MFE structural-high re-entry confirmation (shadow first).
5. Shadow high-momentum classification (strong/mixed/reversing) — Release 2 of
   the SNOW-loss plan; NEVER blanket-reject big movers (user priority: keep
   high-momentum winners — they drive account growth).
6. Whole-share risk overshoot: warning-only today; decide after analyzer data.
7. Still-open old recommendations: prep-scan state persistence, urllib3 pool
   size, 375MB legacy `autobot_scheduler.log` cleanup.

## 10. Known open risks (accepted, monitored)

- One-share minimum can exceed the nominal 10% allocation on high-priced stocks
  (SNOW 9/3: 18.8% of equity; −0.72% account on one loss). Mitigations live:
  concentration cap 26.7%, 2.0x gross cap, guardian halt −1.5%. No auto-reject.
- A 0.25% trailing entry can fill on a micro-bounce inside a larger fade —
  mitigated by the 5s EMA recheck + exit stack; further fix = shadow-classified.
- Alpaca trailing stops are inert outside regular hours → after-hours software
  stops chase with extended-hours limits (AFTERHOURS_CHASE_STALE_SECONDS=45).

---

*Last verified: 2026-09-04 ~19:00 ET — live runtime baseline `8afbfb2` deployed
(2.0x gross ceiling with pre-trade headroom, 14:15–15:44 afternoon, boundary
pending-entry cancels, reconciled closes); day P&L +$7.53 (+0.371%), book flat.
The repository HEAD can sit AHEAD of the runtime baseline by docs-only commits
(such as AGENT_CONTEXT.md/AGENT_CHECKPOINT.md updates) — check the newest
AGENT_CHECKPOINT.md snapshot for the exact current mapping before claiming
deploy state.*

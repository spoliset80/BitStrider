# ApexTrader — Trading System Reference

> Full operating specification of the live equity trading system: how stocks
> are selected, the conditions they must meet, entry/exit mechanics, time
> windows, risk limits, safeguards, and the supporting automation.
>
> **Authoritative-source rule:** this document describes the implementation as
> of **2026-09-06**. `engine\config.py` and the source files listed in §23 are
> authoritative — if this document and the code diverge, the code wins.
>
> **Risk warning:** live-money automated trading. Past performance does not
> guarantee future results.

---

## Table of contents

1. System overview
2. Daily timeline (ET)
3. Stock discovery and universe construction
4. Trading strategies (active vs disabled)
5. Scan-time guardrails
6. Signal generation and ranking
7. Final entry validation
8. Position sizing
9. Portfolio-level risk limits
10. Entry-order mechanics
11. Staged allocation
12. Protective orders
13. Exit hierarchy
14. Reconciled-close process
15. Re-entry behavior
16. PDT controls
17. Independent loss guardian
18. Extreme-market kill mode
19. Watchdog and runtime health
20. Daily AI improvement automation
21. Configuration matrix
22. Known limitations and accepted risks
23. Operational checklists

---

## 1. System overview

ApexTrader is a live-money, multi-strategy equity momentum day-trading bot on
Alpaca (`TRADE_MODE=live`), ~$2,000 account (small-account rules apply). It
trades longs and shorts (`LONG_ONLY_MODE=False`), same-day only — nothing is
held overnight by design.

Four cooperating processes:

| Process | Role | Source |
|---|---|---|
| **Bot** (`main.py` → orchestrator) | Discovery, strategies, entries, exits, flatten | `engine\orchestrator.py`, `engine\execution\enhanced.py` |
| **Guardian** (scheduled 1/min) | Independent daily-loss backstop, outside the bot process | `scripts\guardian.py` |
| **Watchdog** (`autobot.py`) | Crash restart, heartbeat stall, deploy-flag consumer | `engine\watchdog.py` |
| **Daily AI loop** (12:05–14:00 ET) | Observes results, may improve code via evidence gates | `scripts\daily_automation.py` |

Design principle (from `scripts\Agenticdeploy.md`): the **fast trading path
is deterministic** — no LLM decides trades. The LLM (Cline) is confined to
bounded plan/act/verify sessions whose outputs the controller re-validates.

> **Do not confuse the two windows:** 12:05–14:00 ET is the *code-improvement*
> window. Stock entries happen in [09:14, 11:00) and [14:15, 15:44) ET.

---

## 2. Daily timeline (ET)

| Time | What happens |
|---|---|
| 08:55 | Discovery window opens (TI capture, movers, preopen intelligence) |
| 09:05 | Prep scans warm EMA/strategy data — **orders still blocked** |
| 09:14 | **Morning entry window opens** `[09:14, 11:00)` |
| 09:25 | Morning-readiness refresh (fresh scan + universe + EMA prewarm) |
| 09:30 | Regular market opens |
| 11:00:00 | Entries stop; ALL pending DAY entry orders cancelled; **lunch flatten begins** |
| 12:05 | Daily AI improvement loop opens (separate system, §20) |
| 14:00 | Daily AI improvement loop closes |
| 14:15 | **Afternoon entry window opens** `[14:15, 15:44)` |
| 15:44:00 | Entries stop; ALL pending entries cancelled; **EOD flatten begins** |
| 16:00 | Regular market close |
| after 15:44 | Watchdog may apply pending deploy flags (flat window) |

**Boundary semantics** (second-precise, END-EXCLUSIVE — enforced by
`engine\utils\market.py`, the only window-membership source):

```text
Morning entries:   [09:14, 11:00)
Lunch flat:        [11:00, 14:15)
Afternoon entries: [14:15, 15:44)
```

The 11:00 and 15:44 minutes belong to the flatten, not to entries (the NFLX
15:44:37 race-fill lesson).

**Early closes:** `_exchange_close_for_today()` takes the EARLIER of the
configured 15:44 EOD time and the Alpaca calendar close minus 10 minutes, so
early-close sessions still flatten in time. Import-time asserts keep
`EOD_CLOSE_TIME` 10–16 minutes before the 16:00 close.

**Adaptive scan cadence:** scan interval adapts to VIX (3 min at VIX>30 up to
30 min at VIX<15), market phase, and open-position count. Regular-hours
discovery scans run every 1 minute.

---

## 3. Stock discovery and universe construction

The scan universe is deliberately narrow — the primary sources are the latest
Trade Ideas capture plus the Alpaca movers queue:

1. **Trade Ideas** — `data\ti_primary.json`, refreshed in place by the
   `ApexTraderTICapture` scheduled task (3 min cadence 08:25–09:30 ET, 10 min
   09:30–14:50 ET). Top-N batch in TI's own rank order. Freshness TTL 125 min
   (covers the 2h overnight capture cadence); a stale list is discarded, not
   traded.
2. **Alpaca movers** — own dedicated queue (`engine\equity\discovery.py`),
   re-polled every 10 minutes, reset once per trading day, persisted so
   restarts don't wipe it. Movers are activity-confirmed at add time
   (trade_count ≥ 10K, real price/move bands).
3. **Static fallback** (`engine\config.py` core lists) only when dynamic
   sources are unavailable: liquid tech (AAPL/MSFT/NVDA/AMD/...), momentum
   names (MARA/WULF/CORZ/HUT/IREN/...), inverse ETFs (SQQQ/SPXU/UVXY/TZA/...).

### Cleanup applied before scanning

- Delisted/broken tickers removed (`DELISTED_STOCKS`)
- Dead tickers suppressed (`is_dead_ticker`)
- Dynamic (TTL-managed) universe entries expire (15-min default TTL)
- Already-held symbols and symbols with pending entries excluded
- Thinly traded symbols prefiltered (3-month avg daily volume below the
  910K floor → dropped before strategy evaluation; 1-hour cache)
- Deduplication; dynamic candidates ordered before static fallback

EDGAR/sympathy/watchlist queues are **not** part of the current equity scan
(removed with options trading, 2026-09-01).

---

## 4. Trading strategies (active vs disabled)

`get_strategy_instances()` (in `engine\equity\strategies.py`) is the
authoritative composition. State as of 2026-09-06:

### Always instantiated

| Strategy | Setup | Notes |
|---|---|---|
| **GapBreakout** | Gap-up from prior close with volume confirmation | Kelly sizing mult 2.0× |
| **ORB** | Opening Range Breakout — first-15-min range break | |
| **VWAPReclaim** | Price reclaims VWAP from below with volume | |
| **TrendBreaker** | Trend continuation setup | Kelly mult 0.25× |
| **OpeningBellSurge** | Opening surge continuation | |
| **EarlySqueeze** | Early-session squeeze detection | |
| **PowerOf3** | ICT accumulation→manipulation→distribution; needs ≥120 min after open, stale after 300 min | |
| **BearBreakdown** | Below 20SMA + 10-day low, vol ≥1.5× 20-day avg, RSI 30–65 | Always on regardless of broad regime — the *stock's own* breakdown decides the short |

### Enabled via toggles

| Strategy | Flag |
|---|---|
| Sentiment | `SENTIMENT_ENABLED=True` |
| LiquiditySweep | `LIQUIDITY_SWEEP_ENABLED=True` — swing-low sweep that holds, Break-of-Structure above the local high, elevated volume; conf `0.72 + 0.03×vol-ratio`, cap 0.92 |
| PMHighBreakout | `PM_HIGH_BREAKOUT_ENABLED=True` |
| Technical | `TECHNICAL_ENABLED=True` — RSI/MACD/trend multi-indicator |

### Currently disabled (backtested net-negative)

| Strategy | Flag |
|---|---|
| Momentum | `MOMENTUM_ENABLED=False` |
| PreMarketMomentum | `PRE_MARKET_MOMENTUM_ENABLED=False` |
| FloatRotation | `FLOAT_ROTATION_ENABLED=False` |
| VWAPFade | `VWAP_FADE_ENABLED=False` (37% win-rate baseline) |

Disabling policy (user rule, 2026-08-14): a strategy is disabled when its
matched-trade win rate falls below VWAPFade's own 37% baseline, except a
strategy is not judged before it has enough trades (`MIN_TRADES_TO_JUDGE=10`
in the scoreboard). `scripts\strategy_scoreboard.py` re-derives these stats.

Each strategy returns a common `Signal`:

```text
symbol, action (buy/short/sell), price, confidence, reason, strategy,
atr_stop (optional), thin_liquidity, stale_entry
```

Prohibited proposal/entry directions (standing user rules): no leverage
increase, no guardian loosening, no stop-removal, no overnight holds, never
blanket-reject high-momentum names.

---

## 5. Scan-time guardrails

Pre-scan gates in `engine\equity\scan.py` (`_passes_guardrails`) — a symbol
must clear these before strategy evaluation counts (they also gate scan-list
membership so permanent rejects can't occupy slots):

| Check | Baseline value |
|---|---:|
| Minimum price | `$2.00` (adaptive variants may relax toward ~$1.50 on data-driven paths) |
| Relative volume (RVOL) | `1.5×` (adaptive: up to +0.3 in high-VIX bull, down to ~0.9 calm) |
| Current dollar volume | `price × day volume ≥ $1.3M` |
| Average daily volume (3-mo) | `≥ 910,000 shares` |
| Float | `≥ 13M shares` |
| Market capitalization | `≥ $100M` |
| Gap-chase guard | up >15% without consolidation → reject — **currently disabled** (`GAP_CHASE_GUARD_ENABLED=False`) |
| EOD admit cutoff | no thin-liquidity admits after 15:44 ET |

**Adaptive note:** price/RVOL/dollar-volume floors have VIX- and
regime-driven adaptive variants in the scan; the table values are configured
baselines, not unconditional absolutes in every path.

**Fail-safe semantics:** guardrail errors fail **closed** (symbol skipped).
Only the thin-liquidity *pre-check* (volume-history fetch) fails **open** —
a data hiccup doesn't prune a real symbol; the same thresholds are
re-checked downstream. Rejections are logged per cycle as
`[GUARDRAIL SUMMARY]` with per-reason counters.

---

## 6. Signal generation and ranking

- All instantiated strategies scan every eligible symbol in parallel
  (16 workers, 15 s per-symbol timeout, 120 s total scan budget — a slow
  symbol can never hold the cycle hostage).
- One signal per symbol survives: the best (highest-confidence) strategy hit.
- HMM regime alignment adds a **+0.03 confidence bonus** when the signal
  direction agrees with the symbol's own 2-state HMM — a bonus, never a gate.
- Signals are sorted by confidence (descending).
- Confidence floor: **72%** (`MIN_SIGNAL_CONFIDENCE`); a 65% bear-regime
  short threshold exists but the stock-level execution path is largely
  regime-neutral.
- Sector cap: max ~3 signals per identified sector (missing sector info
  never rejects).
- Long-only enforcement applies only if `LONG_ONLY_MODE` were enabled
  (currently `False` — shorts are live).
- `TOP_N_SIGNALS=60` is **visibility breadth** (logging/watchlist), not an
  execution cap. Execution attempts proceed down the ranked list until the
  per-cycle entry caps are reached — a failed top-ranked candidate does not
  waste the cycle (the 2026-08-14 "5 signals, top 3 all failed" fix).
- Broad-market regime does not decide stock direction: "stock" capacity mode
  uses the stock's own conditions (SPY/VIX no longer gate individual stocks).

---

## 7. Final entry validation

Immediately before submission (`_validate_trade` + `_execute_entry` in
`engine\execution\enhanced.py`), every signal is re-validated with fresh data:

| Check | Rule |
|---|---|
| Entry window | Must be inside [09:14,11:00) or [14:15,15:44) — a signal can't fire from an earlier scan |
| Position/duplicate guard | No existing position, pending entry, staged add, or duplicate order |
| EMA7/EMA15 alignment | EMA7 slope + EMA7-vs-EMA15 crossover, 2 CLOSED candles, both directions; missing bars block |
| Momentum freshness (longs) | Hard reject if faded ~5% off its 30-min high (`TRADE_STALE_MOMENTUM_REJECTS=False` = reject, don't trade smaller) |
| EMA-blocked recheck | An entry blocked ONLY by the EMA gate is queued and re-checked every minute until the gate agrees, the window closes, or the symbol drops out of the TI universe |
| Symbol loss block | Loss in first 30 min → blocked until 10:30; 2 losses → blocked rest of day |
| Daily loss limit | In-cycle check against −1.0% (bull) / −2.0% (bear) of start equity — halts the cycle |
| Guardian halt | `flat_request.flag` present → flatten + block entries |
| VIX ROC filter | Blocks entries when VIX is up >20% over its 5-bar hourly lookback |
| PDT | Day-trade count vs 3/day (accounts < $25K); re-entries may bypass the bot's own limit and let the broker decide |
| Buying power | Sizing bound incl. 5% equity reserve |
| Short requirements | Equity ≥ $2,000; asset shortable; not hard-to-borrow (session cache) |
| Short-float cap | High-short-float symbols capped at `MAX_SHORT_FLOAT_PCT` of equity |
| Opposite-order cancel | Resting opposite-side DAY orders cancelled before a fresh entry; GTC stops untouched |
| Swap-on-full | When full, weakest position may be closed for a new signal ≥ 75% confidence (swap-only for longs in bear) |

---

## 8. Position sizing

Pipeline (each step bounds the next):

```text
10% base allocation (POSITION_SIZE_PCT / SMALL_ACCOUNT_POSITION_SIZE_PCT)
    ↓ confidence ramp (from 85% conf, linear to 20% at 100%)
    ↓ strategy Kelly multiplier (GapBreakout 2.0x, TrendBreaker 0.25x)
    ↓ thin-liquidity override (flat 4% when admitted)
    ↓ re-entry reduction (-30% after a prior symbol loss)
    ↓ per-symbol concentration cap (26.7% of equity)
    ↓ correlated-basket cap (leveraged inverse ETFs 33.3% combined)
    ↓ high-short-float cap (MAX_SHORT_FLOAT_PCT)
    ↓ usable buying power (5% equity reserve held back)
    ↓ 2.0x gross-exposure headroom (pre-trade, incl. resting orders)
    ↓ whole-share rounding
```

| Parameter | Value |
|---|---:|
| Base allocation | 10% of equity |
| Confidence ramp | starts 85%, ceiling 20% at 100% confidence |
| Per-symbol concentration (new entry) | 26.7% |
| Growing-winner cap | 26.7 + 0.25×gain%, absolute max 46.7% |
| Correlated inverse-ETF basket | 33.3% combined |
| Gross exposure ceiling | 2.0× equity (env-overridable, guarded [1.0, 2.0]) |
| Normal max positions | 12 (small-account count cap 24) |
| Thin-liquidity flat size | 4% |
| Minimum final order | $5 — below that the order is skipped |
| Buying-power reserve | 5% of equity never committed |

**Known accepted risk:** there is NO account-risk-per-trade ceiling — one
share of a $380 stock ≈ 19% of equity. Warning-only (SNOW 9/3: 18.8%).

---

## 9. Portfolio-level risk limits

- **2.0× gross exposure ceiling** — enforced twice:
  1. PRE-TRADE in `_size_with_buying_power`: filled exposure (absolute,
     options excluded, shorts by abs value) + resting entry notional + this
     order ≤ equity × 2.0. Downsizes to headroom, skips at zero.
  2. POST-FILL by `enforce_portfolio_leverage` on a 10-min grid: cancels
     outstanding entry orders on breach and trims positions, losers first.
- The ceiling is a **cap, not a target** — utilization still requires
  qualified signals; nothing sizes up to "use" the extra room.
- Import-time assert: a typo'd `.env` value can never raise the cap beyond
  2.0 or below 1.0.
- `SWAP_ON_FULL=True`: when at capacity, the weakest position may be closed
  for a stronger signal (conf ≥ 0.75); same-day and same-cycle swap
  protections prevent thrashing.
- Concentration/correlation enforcement runs on its own 10-min clock-grid
  schedule so risk-REDUCTION actions execute even when entry gates would
  early-return.

---

## 10. Entry-order mechanics

- Entries are submitted as **DAY trailing-stop orders** at
  `REENTRY_TRAIL_PCT = 0.25%` — the order follows favorable movement and
  fills on a small reversal, instead of chasing a marketable price into a
  fade (the PFSA lesson: chased $13.52, filled $12.50 while fading 15%).
- 2nd+ same-day entries ALWAYS use this trailing-buy path.
- All entries/exits price off a **live bid/ask quote fetched at submit time**
  (not the possibly-stale scan price), bounded to ~1% of the live midpoint —
  fills like a market order in normal conditions but caps worst-case spread
  absorption. Unfilled DAY orders that never fill are themselves a signal
  the spread was too wide.
- Client-order-ID prefixes are authoritative order classification:
  `apex-entry-` / `apex-staged-` / `apex-reentry-trail-` / `apex-close-`;
  protective GTC stops are never mistaken for entries
  (`classify_symbol_order` — pure function, unit-tested).
- Stale-order cleanup: unfilled orders older than 30 min (intraday
  strategies) / 360 min are cancelled and re-submitted.

---

## 11. Staged allocation

A fresh (first-time) entry is split into **4 × 25% tranches**
(`STAGED_ALLOCATION_ENABLED=True`):

- Only the first tranche is submitted at signal time.
- The poller adds remaining tranches ONLY when:
  1. the position is NOT losing (gain strictly above
     `STAGED_ALLOCATION_MIN_GAIN_PCT = 0.0%`), AND
  2. a fresh EMA trend-alignment check passes immediately before the add.
- Never averages down into a losing position.
- Re-entries and re-entry-trail orders are exempt from staging (full size,
  trailing mechanism handles entry price).

---

## 12. Protective orders

After a fill, `protect_positions()` attaches a **broker-held GTC trailing
stop** to every open position whose shares are free:

```text
Trail distance = ATR(14) x 1.5
Floor: 1.5% (TRAIL_STOP_PCT)
Cap:   4.0% (ATR_TRAIL_MAX_PCT)
```

- Volatility-sized: quiet names keep the flat floor, volatile names get more
  room; profit give-back on winners can widen past both.
- GTC + at the broker → survives bot restart/outage.
- Naked-position repair: any position without active coverage gets a stop
  re-placed on the next cycle (covers rejected bracket step-2s).
- Broker PDT stop rejection (40310100) → falls back to a **software stop**
  tracked by `check_software_stops()` at the ATR-derived price; the software
  stop is dropped once real broker protection lands.
- Confidence ratchet: once a position is up `CONF_RATCHET_TRIGGER_GAIN_PCT`
  or more (and entry confidence > swap threshold), its trailing stop is
  tightened once, scaled by entry confidence.
- Thin-liquidity positions get HALF the normal trail% everywhere
  (`THIN_LIQUIDITY_TRAILING_STOP_MULT = 0.5`).
- Growing winners are allowed wider caps (see §8) but stops are never
  loosened for them.

---

## 13. Exit hierarchy

Multiple independent exit layers run concurrently; the first valid condition
closes the position. All closes are same-day — no overnight holds.

| # | Exit | Trigger | Notes |
|---|---|---|---|
| 1 | Broker GTC trailing stop | Price reverses by the ATR trail (1.5–4%) | Primary protection, survives restarts |
| 2 | Software stop loss | ~3% adverse move (`STOP_LOSS_PCT`) | Especially for PDT-blocked positions where the broker rejected the stop |
| 3 | EMA9 trailing exit | EMA9 pulls back 0.5% from its own peak (`EMA9_TRAIL_PCT`) | Per-minute check on fresh (bypass-cache) 1-min bars, parallelized |
| 4 | EMA7/EMA15 reversal | Trend relationship flips | Thesis break |
| 5 | MFE give-back | Armed at +0.5% peak gain; exits below max(60% of peak gain, +0.1%) | "An armed trade can never round-trip through entry" |
| 6 | No-gain/stale exit | ~8h held, gain ≤ 0% or loss ≤ −1.5% | Usually preempted by lunch/EOD flat |
| 7 | After-hours software stop | Extended-hours breach | Marketable-limit close; re-chases after 45 s if unfilled — broker trails are inert outside regular hours |
| 8 | Guardian emergency flatten | `flat_request.flag` (see §17) | Bot flattens within ~5 s |
| 9 | Kill mode | VIX ≥ 40 / SPY −3% / VIX +50% in 5 h | Emergency close-all + PDT-safe 0.5% hairpin trails |
| 10 | Lunch flatten | 11:00 ET | Cancel ALL open orders sweep-wide + close every position |
| 11 | EOD flatten | 15:44 ET | Close every same-day position; race-fills re-closed (`_eod_closed` reappearance logic) |

Tiered take-profit references exist in the bracket config
(TAKE_PROFIT_NORMAL/HIGH/EXTREME = 27/33/40/50%), but the trailing-stop
stack is the operative exit in practice.

The SoftwareStopPoller thread runs all software exits on a ~5 s tick;
heavy per-position work (fresh bar fetches) is parallelized so the poll
budget is never exceeded.

---

## 14. Reconciled-close process

Every intentional software close goes through
`_request_reconciled_close()` (`CLOSE_RECONCILIATION_ENABLED=True`) —
this exists because of the 9/3 SNOW incident where a GTC stop reserved the
only share and the software close was rejected 9× with "insufficient qty
available (held_for_orders)" while the position bled:

1. Classify every open order on the symbol (entry / staged / re-entry /
   protection / close) via `classify_symbol_order`.
2. Cancel ONLY conflicting broker protection (GTC trailing stop).
3. Poll until cancellation is CONFIRMED (bounded: 2.0 s timeout, 0.25 s poll).
4. Re-read the live position quantity.
5. Submit a close for exactly the remaining quantity.
6. Deduplicate: ONE intentional close per symbol
   (`PENDING_CLOSE_RETRY_SEC = 10`).
7. Confirm the position goes flat; clear the stop-watch only on confirmed
   flat.
8. If the close FAILS, re-arm GTC protection so the position is never left
   naked.

Legacy path (cancel + sleep + close) is restored by
`CLOSE_RECONCILIATION_ENABLED=False`.

---

## 15. Re-entry behavior

- **No blanket time cooldown** after an exit (removed 2026-08-24) — a
  momentum stock can make multiple valid moves in a day, so re-entry needs a
  FRESH qualifying signal instead.
- Re-entry path: symbol back in the TI universe → new strategy signal →
  EMA7/EMA15 gate → momentum freshness → entry window open → not loss-blocked.
- 2nd+ same-day entries always use the 0.25% trailing-buy entry (never a
  marketable chase).
- Size reduced **30%** after a prior same-day loss
  (`REENTRY_SIZE_REDUCTION_PCT`).
- Loss in the first 30 minutes → symbol blocked until **10:30 ET**
  (`LOSS_BLOCK_MORNING_END_ET`).
- **2 losses in one symbol** → blocked for the rest of the day
  (`SYMBOL_DAILY_LOSS_BLOCK_COUNT`).
- Re-entries may bypass the bot's own PDT counter (broker still decides) —
  but re-entry paths honor a daily-loss halt or guardian flag.

---

## 16. PDT controls

```text
PDT_ACCOUNT_MIN = $25,000
PDT_MAX_TRADES  = 3
```

- Warns when remaining day trades fall to the warning level (1).
- Entry vs exit accounting: closing a position is NEVER a new day trade
  (the round trip was counted at entry).
- Re-entries may bypass the bot's own PDT counter ("let the broker decide")
  but broker rejection still governs.
- If the broker rejects a protective stop for PDT reasons, the bot maintains
  a software stop instead — risk control outranks maintaining exposure.
- Forced-overnight positions (PDT blocked the close) are tracked and not
  re-stopped as if they were fresh entries.

---

## 17. Independent loss guardian

`scripts\guardian.py` — scheduled every minute, runs OUTSIDE the bot process,
self-contained (parses `.env` itself; runs even if the bot's config is
mid-deploy). Deterministic: no LLM in the fast path. Never places buys.

```text
Alert tier:    day P&L <= -0.75% of start equity
               -> one email/day + state record
Halt tier:     day P&L <= -1.5%
               -> write flat_request.flag
               -> bot's 5s poller flattens + blocks entries until next daily reset
Stale bot:     heartbeat age > 300 s at halt time
               -> guardian flat-sells every position DIRECTLY via Alpaca (live mode)
```

- Baseline = `engine\.daily_state.json` `daily_start_equity` (the bot's own
  daily reset); if the bot hasn't reset for today, guardian takes no action.
- Action band: **09:35–15:44 ET weekdays** (`GUARDIAN_POLL_START_ET` /
  `GUARDIAN_POLL_END_ET`); outside the band it logs a no-op. `--force`
  overrides for manual tests.
- Idempotent per day; audit state in
  `%LOCALAPPDATA%\ApexTrader\state\guardian_state.json`.

---

## 18. Extreme-market kill mode

Separate from the account-level guardian — reacts to MARKET conditions:

```text
VIX >= 40
SPY intraday drop from open >= 3%
VIX up >= 50% within ~5 hours
```

Response: emergency capital protection — close-all posture, entries blocked
for the day, and a PDT-safe **0.5% hairpin trailing stop** placed on
whatever positions exist (`KILL_MODE_TRAIL_PCT`).

---

## 19. Watchdog and runtime health

`engine\watchdog.py` (stdlib-only, runs as `autobot.py` under Task
Scheduler):

- Launches/restarts `main.py` (crash backoff, heartbeat-stall restart).
- Consumes `deploy_requested.flag` — restarts on new code ONLY inside flat
  deploy windows (lunch 11:00–14:15 ET rule-flat, or after 15:44 ET until
  09:05 next prep); otherwise defers and logs.
- Restarts on `.env` CONTENT change (content-hash, not mtime — the 9/2
  OneDrive mtime restart-storm fix).
- Duplicate-runner prevention via machine-local lock.

**Liveness authority:** `heartbeat.txt` freshness in the repo root
(rewritten every completed main-loop tick). A fresh heartbeat outranks a
missing process listing — path-based process discovery has produced false
"bot not running" reads.

**Logs (machine-local, authoritative):**
`%LOCALAPPDATA%\ApexTrader\logs\` — `apextrader.log` (bot), `autobot.log`
(watchdog), `guardian.log`. Log timestamps are ET−1h (machine clock); Alpaca
fill timestamps converted via pytz are true ET.

**Telemetry:** `engine\telemetry.py` writes non-blocking JSONL execution
events to `%LOCALAPPDATA%\ApexTrader\analytics\` — a telemetry failure must
never delay or break a trading decision.

---

## 20. Daily AI improvement automation

`scripts\daily_automation.py` — separate from the trading decision loop.
Window: **12:05–14:00 ET weekdays** (enforced in-script via pytz; the
`ApexTraderDailyImprovement` task fires a 15-min carrier cadence starting
11:50 local so the first in-window fire lands at 12:05 ET).

```text
OBSERVE  read-only: Alpaca fills -> position-ladder round trips, per-symbol
         stats, churn chains, entry-band performance, drawdown, runtime health
PLAN     bounded Cline session (Plan mode) writes candidate.json
GATE     deterministic evaluate_candidate():
           >= 5 trading days, >= 20 round trips
           >= $5 AND >= 5% relative improvement
           no worse drawdown tail (>10% worsening rejected)
           allowed_files inside repo, never .env/secret files
           prohibited changes + acceptance tests declared
           heartbeat/flat/guardian/deploy-flag healthy
           market OPEN (Alpaca calendar; False blocks, None = unknown)
ACT      bounded Cline session, allowed_files only, act-report.json required
TEST     full scripts\test_*.py suite + compileall (--skip-tests impossible)
VERIFY   controller diff-vs-allowed_files + independent Cline verify session
DEPLOY   ONLY via scripts\deploy.py (its own full test gate), only with
         --allow-deploy / AUTOMATION_ALLOW_DEPLOY=1; watchdog restarts in a
         flat window
```

Fail-closed to `OBSERVE_ONLY` when: no Cline CLI, invalid/missing plan
artifact, any gate failure, saturation declared, dry-run, or skip-agent.
LLM provider fallback: CLI default → DeepSeek (deepseek-v4-flash) →
Moonshot (kimi-k2.7-code), audited per attempt. Artifacts under
`%LOCALAPPDATA%\ApexTrader\automation\<ET-date>\`.

---

## 21. Configuration matrix

| Subject | Source |
|---|---|
| ALL constants, time windows, limits, toggles | `engine\config.py` (import-time validation) |
| Window membership (`within_entry_window`, `in_lunch_break`) | `engine\utils\market.py` |
| Universe + scan guardrails + signal scan | `engine\equity\scan.py` |
| Strategy conditions | `engine\equity\strategies.py` (`get_strategy_instances`) |
| Discovery (TI, movers, preopen intelligence) | `engine\equity\discovery.py` |
| Dynamic universe TTL | `engine\equity\universe.py` |
| Sizing, entries, exits, protection, flatten | `engine\execution\enhanced.py` |
| Main loop, clock-grid jobs, poller, regime | `engine\orchestrator.py` |
| Daily equity baseline/P&L | `engine\session\session.py` |
| Loss guardian | `scripts\guardian.py` |
| Watchdog | `engine\watchdog.py` |
| Test-gated deploy | `scripts\deploy.py` |
| Daily improvement automation | `scripts\daily_automation.py` |
| Portfolio observation (read-only) | `scripts\analyze_daily_portfolio.py` |
| Strategy performance scoreboard | `scripts\strategy_scoreboard.py` |

Feature toggles (rollback switches, `.env` or config):
`CLOSE_RECONCILIATION_ENABLED`, `MFE_GIVEBACK_ENABLED`, `ATR_TRAIL_ENABLED`,
`STAGED_ALLOCATION_ENABLED`, `LUNCH_FLAT_ENABLED`, `EOD_CLOSE_ENABLED`,
`DEPLOY_RESTART_ENABLED`, `MAX_PORTFOLIO_LEVERAGE`,
`TRADE_STALE_MOMENTUM_REJECTS`, `GAP_CHASE_GUARD_ENABLED`, strategy
`*_ENABLED` flags. Rollback = toggle off, or `git revert` + redeploy.

---

## 22. Known limitations and accepted risks

- **No account-risk-per-trade ceiling** — one whole share of an expensive
  stock can exceed the nominal 10% allocation (SNOW 9/3: 18.8% of equity).
  Mitigations: 26.7% concentration cap, 2.0× gross cap, guardian −1.5% halt.
- **Whole-share rounding** can overshoot the calculated allocation.
- A 0.25% trailing entry can fill on a micro-bounce inside a larger fade —
  mitigated by the 5 s EMA recheck + exit stack; fuller fix (shadow
  high-momentum classification) is deferred.
- Market-calendar "unknown" (network failure) is not an explicit rejection in
  the improvement-loop gates — evidence gates (no fills → 0 trades) fail
  closed anyway, but it is not a formal `is True` requirement.
- The improvement task may fire multiple times per day (15-min carrier);
  the lock prevents overlap but completed-cycle deduplication is not enforced.
- The task uses `InteractiveToken` — the Windows user must be logged in for
  scheduled runs.
- The main trader's market-hours model is weekday/time based; only the
  improvement loop consults the Alpaca calendar directly.
- Docs drift: parts of `README.md` describe older behavior (e.g. "long-only",
  7 strategies) — this file and the code supersede it.
- Past performance does not guarantee future results.

---

## 23. Operational checklists

### Before market open
```powershell
& "$env:LOCALAPPDATA\ApexTrader\venv\Scripts\python.exe" scripts\status.py --account
Get-Content "$env:LOCALAPPDATA\ApexTrader\logs\autobot.log" -Tail 20
Get-Content heartbeat.txt
Get-Content data\ti_primary.json -TotalCount 3
```

### During the session
```powershell
Get-Content "$env:LOCALAPPDATA\ApexTrader\logs\apextrader.log" -Tail 30 -Wait
Get-Content "$env:LOCALAPPDATA\ApexTrader\logs\apextrader.log" -Tail 50 |
    Select-String "ERROR|TOP|EXECUTE|SWAP|KILL|GUARDRAIL|BOUNDARY|HALT"
```

### After a guardian event
1. Read `%LOCALAPPDATA%\ApexTrader\state\guardian_state.json`.
2. Read `guardian.log` + `apextrader.log` around the halt timestamp.
3. Review the day's round trips (`scripts\analyze_daily_portfolio.py`).
4. Change code only via the evidence-gated daily loop or an approved manual
   change.

### After an automatic deployment
1. Confirm `[DEPLOY] deploy flag consumed` in `autobot.log`.
2. Confirm a fresh `heartbeat.txt`.
3. Review `run-state.json` for that date under
   `%LOCALAPPDATA%\ApexTrader\automation\`.

### Daily improvement artifacts
```powershell
Get-Content "$env:LOCALAPPDATA\ApexTrader\automation\<date>\run-state.json"
Get-Content "$env:LOCALAPPDATA\ApexTrader\automation\<date>\observation.md"
```

---

*Document created 2026-09-06. Values verified against source on that date;
`engine\config.py` remains authoritative.*









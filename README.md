# ApexTrader

> Automated trading bot — multi-strategy equity signals, adaptive scan intervals, tiered risk management, and clean email reports.

**Version:** v1.3.0 · **Python:** 3.10+ · **Broker:** Alpaca (paper & live) · **Platform:** Windows / Linux / macOS

---

## Table of Contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Quick Start](#quick-start)
4. [Configuration Reference](#configuration-reference)
5. [Equity Strategies](#equity-strategies)
7. [CLI Modes](#cli-modes)
8. [Task Scheduler (Windows)](#task-scheduler-windows)
9. [Email Notifications](#email-notifications)
10. [Risk Controls](#risk-controls)
11. [Log Files](#log-files)
12. [Contributing](#contributing)
13. [Disclaimer](#disclaimer)

---

## Features

**Equity trading**
- **7 strategies** — momentum/RVOL, breakout, VWAP reclaim, gap-up, float rotation, ORB, Sweepea
- **Adaptive scan intervals** — adjusts every cycle based on VIX level, market hours, and open position count
- **Bear regime detection** — SPY < 200-SMA flips to bear: long cap 1/cycle, inverse ETFs front-weighted, shorts unlocked
- **Kill mode** — emergency close-all on VIX ≥ 40, SPY intraday drop ≥ 3%, or VIX +50% in 5 h
- **Position swap** — when at max 12 positions, auto-closes weakest for a higher-confidence new signal (swap-only in bear)
- **Confidence gate** — executes signals ≥ 72% (longs); position sizing now scales with confidence up to 100% at 85%+

**Infrastructure**
- **Trade Ideas integration** — headless Selenium scrape refreshes the universe every 30 min
- **TI primary universe** — latest TI captures persist to `data/ti_primary.json`, with `data/universe.json` as TTL-backed fallback
- **Dynamic universe** — `data/universe.json` with TTL-managed tiers; auto-pruned daily
- **Clean email reports** — light-theme HTML with regime badge, confidence bars, per-pick metrics
- **Watchdog auto-restart** — `autobot.py` relaunches `main.py` on crash, recovers stale PID locks, and supports hard restart cleanup
- **Windows Task Scheduler** ready — Mon–Fri 7 AM auto-start
- **Manual live/paper mode** — watchdog respects `TRADE_MODE` from `.env`, no automatic time-based switching
- **PDT-safe** — long-only mode, daily loss/profit caps, position-size guardrails

---

## Architecture

```
apextrader/
├── main.py                       # Orchestrator: scan loop, execution, EOD close
├── autobot.py                    # Watchdog launcher that keeps main.py running
├── engine/
│   ├── config.py                 # All runtime constants
│   ├── scan.py                   # get_scan_targets(), scan_universe(), filter_signals()
│   ├── strategies.py             # 7 equity strategy classes
│   ├── execution/enhanced.py      # Equity order placement, swap logic, bracket/stop orders
│   ├── notifications.py          # Email templates: build_top5_report(), build_eod_report()
│   ├── universe.py               # TTL-managed ticker universe (JSON-backed)
│   ├── predictions.py            # Day-picks persistence (predictions/day_picks.json)
│   ├── utils.py                  # Data services: get_bars(), get_vix(), sentiment, trending
│   └── broker_factory.py         # Alpaca client factory (paper / live)
├── scripts/
│   ├── run_autobot_task.ps1      # Task Scheduler launcher
│   ├── run_top3.py               # Standalone equity top-3 scan (dry-run)
│   ├── predict_tomorrow.py       # Next-day prediction generator
│   ├── _validate_universe.py     # Validate universe.json integrity
│   └── prune_universe.py         # Manual universe prune utility
├── data/
│   ├── ti_primary.json           # Latest universe capture (Yahoo Finance, primary scan source)
│   └── universe.json             # Dynamic ticker universe with TTL tiers
├── predictions/
│   ├── day_picks.json            # Today's top picks (persisted each cycle)
│   └── watchlist.json            # Prediction watchlist
├── requirements.txt
└── .env                          # Secrets — never commit
```

---

## Quick Start

### 1. Clone & install

```powershell
git clone https://github.com/spolisetti-corp/apextrader.git
cd apextrader

python -m venv apextrader
.\apextrader\Scripts\Activate.ps1       # Windows PowerShell
# source apextrader/bin/activate        # Linux/macOS

pip install -r requirements.txt
```

### 2. Configure secrets

Create `.env` in the project root:

```env
# ── Trade mode ────────────────────────────────────────────────────
TRADE_MODE=paper                    # paper | live

# ── Alpaca credentials ────────────────────────────────────────────
PAPER_ALPACA_API_KEY=your_paper_key
PAPER_ALPACA_API_SECRET=your_paper_secret
LIVE_ALPACA_API_KEY=your_live_key
LIVE_ALPACA_API_SECRET=your_live_secret


# ── Email notifications ───────────────────────────────────────────
USE_EMAIL_NOTIFICATIONS=true
EMAIL_SMTP_SERVER=smtp.gmail.com
EMAIL_SMTP_PORT=587
EMAIL_SMTP_USER=you@gmail.com
EMAIL_SMTP_PASSWORD=your_app_password
EMAIL_FROM_ADDRESS=you@gmail.com
EMAIL_TO_ADDRESSES=you@gmail.com
```

> **Gmail**: use an [App Password](https://myaccount.google.com/apppasswords), not your login password.

### 3. Run

```powershell
# Full continuous scan loop (normal operation)
python main.py

# Or via watchdog (recommended — auto-restarts on crash)
python autobot.py
```

### Quick live/paper switch

```powershell
# Windows PowerShell scripts
.\run_local_ps.ps1 -Mode paper
.\run_local_ps.ps1 -Mode live
```

```bash
# macOS / Linux
./run_local_sh.sh paper
./run_local_sh.sh live
```

---

## Configuration Reference

All tunable constants live in [`engine/config.py`](engine/config.py). Key settings:

### Equity trading

| Setting | Default | Description |
|---|---|---|
| `TRADE_MODE` | `paper` | `paper` or `live` — set via env var |
| `MIN_SIGNAL_CONFIDENCE` | `0.72` | Minimum confidence to execute a long |
| `MAX_POSITIONS` | `12` | Max concurrent equity positions (7.5% × 12 = 90%) |
| `POSITION_SIZE_PCT` | `7.5` | Per-trade allocation (% of account) |
| `SWAP_ON_FULL` | `True` | Close weakest position for a better signal when full |
| `SWAP_MIN_CONFIDENCE` | `0.75` | Minimum confidence to trigger a swap |
| `LONG_ONLY_MODE` | `True` | Disables short entries (PDT-safe) |
| `MARKET_REGIME_SIGNALS_CAP` | `1` | Max long signals per cycle in bear regime |
| `DAILY_LOSS_LIMIT_BULL_PCT` | configured | Halt trading if daily P&L drops by this % in bull |
| `DAILY_LOSS_LIMIT_BEAR_PCT` | configured | Tighter limit for bear days |
| `DAILY_PROFIT_TARGET` | configured | Lock in gains above this |
| `KILL_MODE_VIX_LEVEL` | `40.0` | Emergency close-all VIX threshold |
| `KILL_MODE_SPY_DROP_PCT` | `3.0` | Emergency close-all SPY intraday drop % |

## Equity Strategies

Each strategy in [`engine/strategies.py`](engine/strategies.py) receives OHLCV bars and returns a `Signal` with `confidence` (0–1). All 7 run in parallel via `ThreadPoolExecutor`:


### Alpaca API Integration (Equity)

All equity strategies use the Alpaca API for historical price and volume data (options trading was removed 2026-09-01). This ensures consistent, reliable data and enables seamless live trading and backtesting. The yfinance fallback is retained for redundancy only.

**Current Focus:**
- Refactoring and enhancing equity strategies to leverage Alpaca data for all scans and signals
- Unified data access layer for equities
- Improved reliability and speed for live and backtest modes

| Strategy | Edge |
|---|---|
| `MomentumStrategy` | Pure momentum — RVOL ≥ 1.5× + price velocity |
| `SweepeaStrategy` | Daily pullback to 8-EMA with liquidity sweep setup |
| `GapBreakoutStrategy` | Gap + consolidation range breakout |
| `ORBStrategy` | Opening range breakout with follow-through |
| `VWAPReclaimStrategy` | Price reclaims VWAP with volume surge |
| `FloatRotationStrategy` | High short-float momentum rotation |
| `TechnicalStrategy` | RSI / MACD / MA trend alignment |

Bear regime note: inverse ETFs (SQQQ, SPXU, UVXY, TZA, FAZ, SOXS, LABD, DUST) are front-ranked in `PRIORITY_1_MOMENTUM` and treated as standard BUY signals.

---

## CLI Modes

| Command | What it does |
|---|---|
| `python main.py` | Full loop: scan → signal → execute → EOD close |
| `python autobot.py` | Watchdog: keeps main.py running, respects `TRADE_MODE` from `.env` |
| `python scripts/run_top3.py` | Standalone equity top-3 scan (dry-run, no orders) |
| `python scripts/predict_tomorrow.py` | Generate next-day prediction picks |
| `python scripts/test_notifications.py` | Send a test email to verify SMTP config |

---

## Task Scheduler (Windows)

The bot auto-starts Mon–Fri at 7:00 AM via Windows Task Scheduler.

**Launcher:** [`scripts/run_autobot_task.ps1`](scripts/run_autobot_task.ps1)
**Watchdog:** [`autobot.py`](autobot.py) — relaunches `main.py` on crash (10 s delay)

### Manual live/paper mode

`autobot.py` respects `TRADE_MODE` from `.env` and does not switch automatically by time.

Override mode in `.env` by setting:

- `TRADE_MODE=live`
- `TRADE_MODE=paper`

```powershell
$env:LIVE_TRADE_WINDOWS_ET = "09:30-11:00,15:00-16:00"
```

### Watchdog code changes require a watchdog restart

The watchdog (`autobot.py` / `engine/watchdog.py`) is a long-running process.
Deploying new code via the deploy flag restarts `main.py` only - it does NOT
restart the watchdog itself. Therefore:

- Changes to `engine/watchdog.py` (or `autobot.py`) take effect only after the
  watchdog process itself is restarted (end the Task Scheduler task / kill the
  watchdog PID and let it relaunch, or reboot-schedule it).
- The `.env` kill switch `DEPLOY_RESTART_ENABLED=false` is read live by the
  updated watchdog - it has no effect until the updated watchdog code is
  running.
- Restarting `main.py` alone is never enough for watchdog-logic changes.

### Task Scheduler commands

```powershell
# Trigger manually
schtasks /Run /TN "ApexTraderAutoRun"

# Check status
schtasks /Query /TN "ApexTraderAutoRun" /FO LIST

# Check for duplicate processes
Get-Process python | Format-Table Id, @{N='MB';E={[math]::Round($_.WorkingSet/1MB,1)}}, StartTime

# Gracefully restart (watchdog auto-relaunches main.py)
Stop-Process -Id <main_py_pid> -Force
```

---

## Email Notifications

Two email types are sent automatically via Gmail SMTP (light-theme HTML).

### Equity Scan Email
Sent after each scan cycle with signals. Includes:
- Market regime badge (BULL / BEAR) and sentiment
- Top-3–5 signal cards with confidence bar and strategy
- Per-pick: price, R/R, breakeven, entry reason
- `[WATCH]` prefix in subject when emailing near-miss candidates (no A+ signals today)

### EOD Report
Sent at end of trading day. Includes:
- Account equity / buying power snapshot
- Daily P&L + trade count
- Closed positions with P&L per trade
- Open positions sorted by unrealized P&L

### Test the email

```powershell
python scripts/test_notifications.py
```

---

## Risk Controls

| Control | Behavior |
|---|---|
| **Kill mode** | VIX ≥ 40, SPY drop ≥ 3%, or VIX +50% in 5 h → emergency close all, block entries all day |
| **Daily loss limit** | Regime-aware % of start equity → stops all new trades for the day |
| **Daily profit target** | Locks in gains, halts new entries |
| **Max positions cap** | Hard 12-position limit (90% equity deployed, 10% BP reserve) |
| **Position swap** | Auto-exits weakest long for a stronger signal; swap-only in bear regime |
| **Equity confidence gate** | 72% minimum for longs; position sizing scales with confidence up to 100% at 85%+ |
| **Dollar volume guardrail** | Skips illiquid symbols below minimum dollar volume |
| **Long-only mode** | No short entries — avoids margin, HTB, PDT complications |
| **Quarterly P&L target** | Tracks and logs progress toward quarterly gain goal |
| **Same-day swap protection** | Positions entered today cannot be swapped out within the same day |
| **Cycle swap protection** | Each symbol can only be swapped once per scan cycle |
| **Staged allocation (25% x 4)** | Fresh entries split into 4 equal tranches; first submitted at signal time, rest added by the poller only while the position is not losing and the fresh EMA gate still passes |
| **Never add while losing** | A staged tranche is only added when unrealized gain is strictly above `STAGED_ALLOCATION_MIN_GAIN_PCT` (default 0.0%) |
| **Pre-entry opposite-order cancel** | Before a fresh entry, any resting DAY order on the opposite side for that symbol is cancelled; GTC protective trailing stops are never touched |
| **ATR-based trailing stop** | Exit trailing stops scale per-symbol with ATR(14): `ATR × 1.5`, floored at the 1.5% `TRAIL_STOP_PCT` and capped at 4.0%, so volatile names get a volatility-sized leash while quiet names keep the flat floor; profit giveback widens past both on winners |

---

## Log Files

| File | Contents |
|---|---|
| `apextrader.log` | Main trading log — signals, execution, regime, guardrails |
| `autobot.log` | Watchdog log — restarts, main.py stdout relay |

```powershell
# Tail live
Get-Content .\apextrader.log -Tail 30 -Wait

# Key events only
Get-Content .\apextrader.log -Tail 50 | Select-String "ERROR|TOP 5|EXECUTE|SWAP|KILL|gate"
```

> Log files are git-ignored and stay local only.

---

## Contributing

```
main                      ← stable releases (tagged vX.Y.Z)
feature/*                 ← active development branches
```

1. Branch off `main` for new work
3. Test equity scan: `python scripts/run_top3.py`
4. Merge to `main` when stable, tag with `git tag vX.Y.Z`

---

## Disclaimer

This software is for educational and research purposes only. Automated trading carries significant financial risk. Always test thoroughly in **paper mode** (`TRADE_MODE=paper`) before using real capital. Past performance does not guarantee future results.


# BitStrider — Discord Alert Trader

> Polls Discord trading-alert channels and auto-executes market orders on Alpaca.
> No local algos, no scanning, no TI — Discord signals only.

**Python:** 3.10+ · **Broker:** Alpaca (paper & live) · **Branch:** `feature/discord-alerts`

---

## How It Works

```
Discord channels
     │  REST API poll (every 60s, market hours only)
     ▼
  Parse alert  →  ticker + action + confidence score
     │
     ▼  (score ≥ CONFIDENCE_MIN)
  Risk checks  →  max positions · daily spend cap · dedupe
     │
     ▼
  Alpaca market order  (notional scaled by confidence tier)
     │
     ▼
  logs/discord_trades_YYYYMMDD.jsonl
```

---

## Running Locally

### Prerequisites

```powershell
# Clone and switch to the discord branch
git checkout feature/discord-alerts

# Create and activate the virtual environment (first time only)
python -m venv apextrader
apextrader\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

### Configure `.env`

Create a `.env` file in the project root:

```env
# ── Discord ────────────────────────────────────────────────────────────────────
DISCORD_USER_TOKEN=your-discord-user-token

# Channel IDs mapped to handler types (options | equity | spx)
DISCORD_CHANNEL_TYPES=753377655532945558:options,752750381918060589:equity,769046364738289734:options,744643208973254726:options,1119663300909731910:spx

DISCORD_OPTIONS_MODE=paper

# ── Alpaca ─────────────────────────────────────────────────────────────────────
PAPER_ALPACA_API_KEY=your-paper-key
PAPER_ALPACA_API_SECRET=your-paper-secret
LIVE_ALPACA_API_KEY=your-live-key
LIVE_ALPACA_API_SECRET=your-live-secret

# ── Risk / tuning (optional) ───────────────────────────────────────────────────
DISCORD_CONFIDENCE_MIN=70
DISCORD_ORDER_NOTIONAL=500
DISCORD_MAX_POSITIONS=10
DISCORD_MAX_DAILY_SPEND=5000
DISCORD_SPX_NOTIONAL=300
```

---

## Finding Your Credentials

### Discord User Token

> **Warning:** your user token gives full access to your Discord account. Keep it in `.env` and never commit it.

1. Open Discord in a browser (chrome/edge — **not** the desktop app)
2. Open DevTools → **Network** tab → filter by `messages`
3. Click any channel to trigger a request
4. In the request headers find `Authorization` — that value is your token
5. Copy it into `DISCORD_USER_TOKEN=...`

Alternatively on the desktop app:
1. Press `Ctrl+Shift+I` to open DevTools
2. Go to **Application → Local Storage → https://discord.com**
3. Find the key `token` — the quoted string is your user token

Tokens do not expire unless you change your password or log out of all sessions.

### Discord Channel IDs

1. In Discord settings enable **Developer Mode**: User Settings → Advanced → Developer Mode ✓
2. Right-click any channel → **Copy Channel ID**
3. Add it to `DISCORD_CHANNEL_TYPES` with its type:
   - `<id>:options` — options alert channel (parses strike/expiry/OCC)
   - `<id>:equity` — stock alert channel (buys/sells shares)
   - `<id>:spx` — SPX level channel (drives SPY 0DTE state machine)

### Alpaca API Keys

**Paper (for testing):**
1. Go to [app.alpaca.markets](https://app.alpaca.markets)
2. Switch to the **Paper** environment (toggle top-left)
3. Click your account → **API Keys** → Generate New Key
4. Copy key + secret into `PAPER_ALPACA_API_KEY` / `PAPER_ALPACA_API_SECRET`

**Live:**
1. Same steps but with the **Live** environment selected
2. Copy into `LIVE_ALPACA_API_KEY` / `LIVE_ALPACA_API_SECRET`
3. Set `DISCORD_OPTIONS_MODE=live` in `.env` or run `.\run.ps1 -Mode live`

### Start the bot

```powershell
# Paper trading (default) — polls every 60s during market hours 9:30am–4pm EST
.\run.ps1

# Live trading
.\run.ps1 -Mode live

# Custom poll interval (e.g. every 30 seconds)
.\run.ps1 -Poll 30

# Pre-market start
.\run.ps1 -MarketOpen 09:00

# Specific paper key via env (no .env file)
$env:DISCORD_USER_TOKEN="..."; $env:PAPER_ALPACA_API_KEY="..."; .\run.ps1
```

The script:
- Only polls between `$MarketOpen` and `$MarketClose` (default 9:30–16:00 EST)
- Skips weekends automatically
- Sleeps until next market open when outside hours
- Writes a `discord_bot.pid` lock file — a second `.\run.ps1` will exit immediately to prevent duplicate orders
- Removes the PID file on exit (Ctrl+C or normal stop)

### Add a channel

In `.env`, add the new ID and its type to `DISCORD_CHANNEL_TYPES`:
```env
DISCORD_CHANNEL_TYPES=...,<new_channel_id>:options
```
Types: `options` · `equity` · `spx`

### End-to-end test

Place real paper orders across all channel types without waiting for live Discord messages:
```powershell
apextrader\Scripts\python.exe scripts\test_e2e_discord.py

# Parse only — no orders placed
apextrader\Scripts\python.exe scripts\test_e2e_discord.py --dry-run
```

### Trade log

Every executed trade is appended to `logs/discord_trades_YYYYMMDD.jsonl`:
```json
{"ts": "2026-06-23T14:00:00Z", "channel": "769046364738289734",
 "ticker": "HIMS", "action": "BUY", "conf": 100,
 "occ": "HIMS  260724C00042000", "notional": 410.0,
 "order": {"status": "submitted", "id": "..."}}
```

---

## Configuration (`.env`)

### Mode
| Key | Default | Description |
|-----|---------|-------------|
| `DISCORD_OPTIONS_MODE` | `paper` | `paper` or `live` |

### Credentials
| Key | Description |
|-----|-------------|
| `DISCORD_USER_TOKEN` | Your Discord user token (see [Finding Your Credentials](#finding-your-credentials)) |
| `PAPER_ALPACA_API_KEY` / `PAPER_ALPACA_API_SECRET` | Alpaca paper keys |
| `LIVE_ALPACA_API_KEY` / `LIVE_ALPACA_API_SECRET` | Alpaca live keys |

### Channels
| Key | Default | Description |
|-----|---------|-------------|
| `DISCORD_CHANNEL_TYPES` | — | `id:type` pairs, comma-separated. Types: `options` `equity` `spx` |

### Risk / Allocation
| Key | Default | Description |
|-----|---------|-------------|
| `DISCORD_OPTIONS_MODE` | `paper` | `paper` or `live` |
| `DISCORD_ORDER_NOTIONAL` | `500` | Base $ per equity/options trade |
| `DISCORD_CONFIDENCE_MIN` | `70` | Min confidence to place options BUYs (equity bypasses this) |
| `DISCORD_ALLOC_LOW_PCT` | `1.0` | % of buying power for conf 70–79% |
| `DISCORD_ALLOC_MED_PCT` | `2.0` | % of buying power for conf 80–89% |
| `DISCORD_ALLOC_HIGH_PCT` | `3.0` | % of buying power for conf 90%+ |
| `DISCORD_MAX_POSITIONS` | `70` | Max open positions at once |
| `DISCORD_MAX_DAILY_SPEND` | `5000` | Hard daily $ cap |
| `DISCORD_DEDUPE_TICKER` | `true` | Skip repeat buys of same ticker same day |
| `DISCORD_SPX_NOTIONAL` | `300` | $ per SPY 0DTE trade on SPX signals |
| `DISCORD_SPX_STOP_PCT` | `50` | Stop-loss at X% of premium paid |

---

## Confidence Scoring

| Signal | Points |
|--------|--------|
| Base | 50 |
| Strike price found | +20 |
| Expiry found | +15 |
| Entry price found | +10 |
| Action word found | +5 |
| **Max** | **100** |

---

## Trade Log

Every executed trade is appended to `logs/discord_trades_YYYYMMDD.jsonl`:

```json
{"ts": "2026-06-23T01:00:00Z", "ticker": "MSFT", "action": "BUY",
 "conf": 85, "notional": 750.0, "daily_spent": 1500.0,
 "order": {"status": "submitted", "id": "..."}}
```

---

## Disclaimer

This software is for educational and research purposes only. Use at your own risk.


**Version:** v1.4.0 · **Python:** 3.10+ · **Broker:** Alpaca (paper & live) · **Data:** Market Data App (primary) + Alpaca · **Platform:** Windows / Linux / macOS

---

## Table of Contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Quick Start](#quick-start)
4. [Configuration Reference](#configuration-reference)
5. [Equity Strategies](#equity-strategies)
6. [Options Strategies](#options-strategies)
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
- **Position swap** — when at max 10 positions, auto-closes weakest for a higher-confidence new signal (swap-only in bear)
- **Confidence gate** — executes signals ≥ 72% (longs); position sizing scales with confidence up to 100% at 85%+
- **VIX-adaptive RVOL guardrails** — RVOL threshold scales with VIX level and bull/bear regime (0.4× in calm bull, up to 1.3× in high-VIX bull); timestamp-based RVOL@TIME compares same-elapsed-minute windows across prior sessions, with proportional scaling for partial sessions

**Options trading (Level 3, Alpaca)**
- **10-strategy A+ scanner** — MomentumCall, BearPut, BearCallSpread, ShortSqueeze, MeanReversionCall, BreakoutRetest, TrendPullbackSpread, IronCondor, Butterfly, CoveredCall
- **76% sniper threshold** — configurable via `OPTIONS_MIN_SIGNAL_CONFIDENCE`; filters to the cleanest setups
- **45% portfolio allocation**, max 4 concurrent option positions; equity capped at 5% × 10 = 50%
- **Trailing stop** — activates at +20% gain, trails with 15% drawdown
- **Open-window spread-first logic** — 9:35–9:45 ET prefers spreads; naked entries only when IV is very low and pre-market gap is small
- **Parallel prefetch** — bars + OI + chains fetched concurrently before the strategy loop (up to 12 workers)
- **5-min universe cache** — avoids hammering the options universe API on every cycle
- **Options diagnostic logging** — zero-signal scans report per-strategy fail reasons; near-miss `[WATCH]` email
- **Master kill-switch** — `OPTIONS_ENABLED=false` in `.env` disables the entire options system without restart

**Infrastructure**
- **Market Data App (primary)** + Alpaca + yfinance (fallback) — MDA provides consolidated SIP-level data for bars and quotes
- **Trade Ideas integration** — headless Selenium scrape refreshes the universe every 30 min
- **Discord alerts listener** — real-time parsing of Discord channel alerts, auto-executes options trades on identified signals with confidence scoring
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
├── main.py                            # Entry point: scan loop, execution, EOD close
├── autobot.py                         # Watchdog launcher that keeps main.py running
├── engine/
│   ├── config.py                      # All runtime constants
│   ├── orchestrator.py                # Main trading loop coordinator
│   ├── equity/
│   │   ├── scan.py                    # get_scan_targets(), scan_universe(), _passes_guardrails()
│   │   ├── strategies.py              # 7 equity strategy classes
│   │   ├── discovery.py               # Priority scan queue (screener/sympathy/EDGAR)
│   │   └── universe.py                # TTL-managed ticker universe (JSON-backed)
│   ├── options/
│   │   ├── strategies.py              # 10 option strategies + scan_options_universe()
│   │   └── executor.py                # Options order placement (buy-to-open, close, trails)
│   ├── execution/
│   │   └── enhanced.py                # Equity order placement, swap logic, bracket/stop orders
│   ├── broker/
│   │   └── broker_factory.py          # Alpaca client factory (paper / live)
│   ├── notifications/
│   │   └── notifications.py           # Email templates: scan report, EOD report
│   ├── predictions/
│   │   └── predictions.py             # Day-picks persistence (predictions/day_picks.json)
│   ├── utils/
│   │   ├── bars.py                    # Bar fetching: MDA-primary → Alpaca → yfinance; RSI/ATR
│   │   ├── market.py                  # MarketState, regime detection, VIX, IEX-feed detection
│   │   └── risk.py                    # Position sizing, daily P&L caps
│   └── risk/
│       └── kill_mode.py               # Kill-mode triggers and enforcement
├── scripts/
│   ├── backtest_options.py            # Options strategy backtest runner
│   ├── run_top3.py                    # Standalone equity top-3 scan (dry-run)
│   ├── capture_tradeideas.py          # Trade Ideas Selenium scraper
│   ├── predict_tomorrow.py            # Next-day prediction generator
│   └── prune_universe.py             # Manual universe prune utility
├── data/
│   ├── ti_primary.json                # Latest TI capture universe (primary scan source)
│   └── universe.json                  # Dynamic ticker universe with TTL tiers
├── predictions/
│   ├── day_picks.json                 # Today's top picks (persisted each cycle)
│   └── watchlist.json                 # Prediction watchlist
├── requirements.txt
└── .env                               # Secrets — never commit
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

# ── Market Data App (primary bar/quote data) ──────────────────────
MARKETDATA_API_KEY=your_mda_bearer_token

# ── Options trading ───────────────────────────────────────────────
OPTIONS_ENABLED=true                # false = kill-switch (no restart needed)
OPTIONS_ALLOCATION_PCT=45.0         # % of equity reserved for all options combined
OPTIONS_TRAIL_ACTIVATE_PCT=20.0     # activate trailing stop at +20% gain
OPTIONS_TRAIL_DRAWDOWN_PCT=15.0     # trail with 15% drawdown from peak

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
| `MAX_POSITIONS` | `10` | Max concurrent equity positions (5% × 10 = 50%) |
| `POSITION_SIZE_PCT` | `5.0` | Per-trade allocation (% of account) |
| `SWAP_ON_FULL` | `True` | Close weakest position for a better signal when full |
| `SWAP_MIN_CONFIDENCE` | `0.75` | Minimum confidence to trigger a swap |
| `LONG_ONLY_MODE` | `True` | Disables short entries (PDT-safe) |
| `RVOL_MIN` | `1.0` | Base RVOL threshold; adaptive clamps scale with VIX and regime |
| `MARKET_REGIME_SIGNALS_CAP` | `1` | Max long signals per cycle in bear regime |
| `DAILY_LOSS_LIMIT_BULL_PCT` | configured | Halt trading if daily P&L drops by this % in bull |
| `DAILY_LOSS_LIMIT_BEAR_PCT` | configured | Tighter limit for bear days |
| `DAILY_PROFIT_TARGET` | configured | Lock in gains above this |
| `KILL_MODE_VIX_LEVEL` | `40.0` | Emergency close-all VIX threshold |
| `KILL_MODE_SPY_DROP_PCT` | `3.0` | Emergency close-all SPY intraday drop % |
| `USE_TRADEIDEAS_DISCOVERY` | `True` | Enable Trade Ideas selenium universe refresh |

### Options trading

| Setting | Default | Description |
|---|---|---|
| `OPTIONS_ENABLED` | `true` | Master kill-switch — set `false` to disable everything |
| `OPTIONS_ALLOCATION_PCT` | `45.0` | % of equity for all options combined |
| `OPTIONS_MAX_POSITIONS` | `4` | Max concurrent option positions |
| `OPTIONS_DTE_MIN` / `MAX` | `7` / `21` | Expiry window (near-term, higher-theta) |
| `OPTIONS_DELTA_TARGET` | `0.40` | Target delta at entry (0.30–0.50 range) |
| `OPTIONS_MIN_SIGNAL_CONFIDENCE` | `0.76` | Minimum confidence to execute an options signal |
| `OPTIONS_MIN_RVOL` | `1.5` | Minimum RVOL for options momentum entries |
| `OPTIONS_PROFIT_TARGET_PCT` | `45.0` | Close contract at +45% gain |
| `OPTIONS_STOP_LOSS_PCT` | `30.0` | Close contract at -30% loss |
| `OPTIONS_TRAIL_ACTIVATE_PCT` | `20.0` | Activate trailing stop at +20% gain |
| `OPTIONS_TRAIL_DRAWDOWN_PCT` | `15.0` | Trail with 15% drawdown from peak gain |

---

## Equity Strategies

Each strategy in [`engine/equity/strategies.py`](engine/equity/strategies.py) receives OHLCV bars and returns a `Signal` with `confidence` (0–1). All 7 run in parallel via `ThreadPoolExecutor`.

### RVOL@TIME Guardrail

Before any strategy evaluates a symbol, `_passes_guardrails()` runs a time-aware relative volume check. It filters today's 1-min bars from 9:30 AM to the current minute, then compares against the same elapsed-minute window from the prior 5 trading sessions. Partial sessions (≥ 50% coverage) are accepted and proportionally scaled. This ensures a stock's volume pace is genuinely elevated at the current time of day — not just occasionally spiked at open.

RVOL thresholds are VIX- and regime-adaptive:

| Regime | VIX | Adaptive RVOL floor |
|---|---|---|
| Bull | > 25 | 1.3× |
| Bull | 18–25 | 0.8× |
| Bull | 15–18 | 0.5× |
| Bull | < 15 | 0.4× |
| Bear | < 18 | 0.4× |
| Bear | ≥ 18 | 0.6× |

| Strategy | Edge |
|---|---|
| `MomentumStrategy` | Pure momentum — RVOL ≥ 1.0× base + price velocity |
| `SweepeaStrategy` | Daily pullback to 8-EMA with liquidity sweep setup |
| `GapBreakoutStrategy` | Gap + consolidation range breakout |
| `ORBStrategy` | Opening range breakout with follow-through |
| `VWAPReclaimStrategy` | Price reclaims VWAP with volume surge |
| `FloatRotationStrategy` | High short-float momentum rotation |
| `TechnicalStrategy` | RSI / MACD / MA trend alignment |

Bear regime note: inverse ETFs (SQQQ, SPXU, UVXY, TZA, FAZ, SOXS, LABD, DUST) are front-ranked in `PRIORITY_1_MOMENTUM` and treated as standard BUY signals.

---

## Options Strategies

Implemented in [`engine/options/strategies.py`](engine/options/strategies.py). The standalone daily scanner is [`scripts/_options_today.py`](scripts/_options_today.py).

### A+ Filter Pipeline (all 9 must pass)

1. **Liquid options chain** — expiry must exist in 7–21 DTE window  
2. **IV rank gate** — calls: IV rank < 35%, puts: IV rank < 55% (not over-priced)  
3. **EMA-20 trend** — price above EMA for calls, below for puts  
4. **3-day momentum** — 3-day return in correct direction  
5. **5-day breakout / breakdown** — price must clear prior 5-day high (calls) or break below prior 5-day low (puts)  
6. **ATM open interest** — ≥ 500 contracts (liquidity floor)  
7. **Bid/ask spread** — ≤ 15% of mid (not wide)  
8. **R/R ratio** — ≥ 1.5× (breakeven vs. underlying move required)  
9. **Premium cap** — mid ≤ 3% of spot price (avoids paying inflated premium)

**Confidence gate:** sniper threshold defaults to 82% via `OPTIONS_MIN_SIGNAL_CONFIDENCE`; score is derived from the A+ filter composite and R/R adjustment.

### Watch list fallback

When zero signals clear the sniper threshold, the scanner shows the **top-3 near-miss candidates** — tickers that passed all 9 structural filters but scored below the gate — with their confidence gap and full metrics. A `[WATCH]` email is sent instead of suppressing output entirely.

### Strategies

| Strategy | Regime | Entry | IV constraint |
|---|---|---|---|
| `MomentumCallStrategy` | Bull | +3% day, RVOL ≥ 1.5×, RSI 50–72, prior 5d high breakout | IV rank < 35% |
| `BearPutStrategy` | Bear / any | −2% day (bear) or −4% (bull), RVOL ≥ 1.2×, prior 5d low breakdown | IV rank < 55% |
| `CoveredCallStrategy` | Bull | Existing long ≥ 100 shares, sell OTM calls ~0.25 delta | IV rank ≥ 50% (sell when expensive) |

---

## CLI Modes

| Command | What it does |
|---|---|
| `python main.py` | Full loop: scan → signal → execute → EOD close |
| `python autobot.py` | Watchdog: keeps main.py running, respects `TRADE_MODE` from `.env` |
| `python scripts/_options_today.py` | Standalone A+ options scan — no orders placed |
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

### Options / Equity Scan Email
Sent after each scan cycle with signals. Includes:
- Market regime badge (BULL / BEAR) and sentiment
- Top-3–5 signal cards with confidence bar, strike/expiry (options) or strategy (equity)
- Per-pick: price, R/R, IV rank, breakeven, entry reason
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
| **Options confidence gate** | 82% default sniper threshold; only the strongest option setups are entered |
| **Options kill-switch** | `OPTIONS_ENABLED=false` disables entire options system without restart |
| **Dollar volume guardrail** | Skips illiquid symbols below minimum dollar volume |
| **Long-only mode** | No short entries — avoids margin, HTB, PDT complications |
| **Quarterly P&L target** | Tracks and logs progress toward quarterly gain goal |
| **Same-day swap protection** | Positions entered today cannot be swapped out within the same day |
| **Cycle swap protection** | Each symbol can only be swapped once per scan cycle |

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
feature/options-trading   ← active development branch
main                      ← stable releases (tagged vX.Y.Z)
```

1. Branch off `main` for new work
2. Test the options scanner: `python scripts/_options_today.py`
3. Test equity scan: `python scripts/run_top3.py`
4. Merge to `main` when stable, tag with `git tag vX.Y.Z`

---

## Disclaimer

This software is for educational and research purposes only. Automated trading carries significant financial risk. Always test thoroughly in **paper mode** (`TRADE_MODE=paper`) before using real capital. Past performance does not guarantee future results.


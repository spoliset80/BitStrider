# ApexTrader Configuration Reference

This document lists all environment variables and config options for easy, holistic, and modular setup. Set these in your `.env` file or environment.

| Variable                    | Default   | Description                                      |
|-----------------------------|-----------|--------------------------------------------------|
| STOCKS_BROKER               | alpaca    | Broker for stocks (alpaca only)                |
| OPTIONS_* (removed 2026-09-01) | —      | Options trading config keys were deleted with the options system      |
| STAGED_ALLOCATION_ENABLED    | true      | Scale in over tranches instead of one full entry order |
| STAGED_ALLOCATION_TRANCHES   | 4         | # of equal tranches (4 x 25% of full size)             |
| STAGED_ALLOCATION_MIN_GAIN_PCT | 0.0    | Only add a tranche while unrealized gain > this (never add while losing) |
| STAGED_ALLOCATION_MAX_ADD_PCT | 25.0    | Each add is this % of the ORIGINAL full size           |
| ATR_TRAIL_ENABLED            | true      | Widen the exit trailing stop per-symbol by ATR          |
| ATR_TRAIL_PERIOD             | 14        | ATR lookback (bars) for the trailing-stop distance      |
| ATR_TRAIL_MULTIPLIER         | 1.5       | Stop distance = ATR x this                              |
| ATR_TRAIL_MAX_PCT            | 4.0       | Ceiling on the ATR-based stop % (floor is TRAIL_STOP_PCT = 1.5) |
| TRADE_MODE                  | paper     | 'paper' or 'live' trading mode                   |
| ...                         | ...       | ... (add more as needed)                         |

- See `engine/config.py` for all tunable settings.
- All variables can be set in `.env` or as environment variables.
- For advanced users: add/override variables per user or profile.

---

This file is auto-generated and should be updated with every new config option.

"""
discord_api_reader.py — thin entry point.

All logic lives in scripts/discord_trader/.
This file only wires CLI args → Config → run().
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Support both `python scripts/discord_api_reader.py` and `python -m scripts.discord_api_reader`
load_dotenv(Path(__file__).parent.parent / ".env")

try:
    from scripts.discord_trader.config import load_config
    from scripts.discord_trader.poller import run
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.discord_trader.config import load_config
    from scripts.discord_trader.poller import run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Discord Alert Trader")
    ap.add_argument("--loop",    action="store_true", help="Poll continuously")
    ap.add_argument("--poll",    type=int, default=60,  help="Poll interval in seconds")
    ap.add_argument("--history", type=int, default=50,  help="Messages to fetch on startup per channel")
    args = ap.parse_args()

    config = load_config()

    # Allow CLI override of mode via env (run.ps1 sets DISCORD_OPTIONS_MODE)
    run(config, loop=args.loop, poll_secs=args.poll, history_limit=args.history)

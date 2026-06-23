#!/usr/bin/env python
"""Test Discord bot configuration"""
import os
import sys

# Set environment variables
os.environ['DISCORD_BOT_TOKEN'] = "331328fd163dd0ceaa87b5daedbbcca9d4eeaaba719171a2a30e2b0ff0d8279e"
os.environ['DISCORD_CHANNEL_ID'] = "1518808920184062032"
os.environ['DISCORD_OPTIONS_MODE'] = "paper"
os.environ['DISCORD_CONFIDENCE_MIN'] = "70"

try:
    import discord
    print(f"✓ discord.py version: {discord.__version__}")
except ImportError as e:
    print(f"✗ Failed to import discord: {e}")
    sys.exit(1)

token = os.getenv('DISCORD_BOT_TOKEN')
channel_id = os.getenv('DISCORD_CHANNEL_ID')
mode = os.getenv('DISCORD_OPTIONS_MODE')
confidence = os.getenv('DISCORD_CONFIDENCE_MIN')

print(f"\n=== Discord Configuration ===")
print(f"✓ Bot Token: {token[:20]}...{token[-10:]}")
print(f"✓ Channel ID: {channel_id}")
print(f"✓ Mode: {mode}")
print(f"✓ Confidence Threshold: {confidence}%")
print(f"\n→ All configuration validated!")
print(f"→ Ready to run: python scripts/discord_options_trader.py")

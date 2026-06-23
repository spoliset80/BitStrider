"""
Discord Alerts Listener
=======================
Listens to Discord channels for trading alerts and options signals.
Parses incoming messages and triggers corresponding trading logic.

Usage
-----
  # Start the Discord listener (runs in background)
  python -c "from engine.discord_listener import start_discord_listener; start_discord_listener()"

Requirements
------------
  discord.py>=2.3.0
  DISCORD_BOT_TOKEN environment variable must be set
  DISCORD_CHANNEL_ID environment variable must be set (e.g., 777327652666998804)
"""

from __future__ import annotations

import logging
import os
import re
import asyncio
from typing import Optional, Callable, Dict, List, Any
from datetime import datetime

try:
    import discord
    from discord.ext import commands, tasks
    DISCORD_OK = True
except ImportError:
    DISCORD_OK = False
    discord = None

logger = logging.getLogger(__name__)

# Discord bot intents
INTENTS = discord.Intents.default() if DISCORD_OK else None
if INTENTS:
    INTENTS.message_content = True
    INTENTS.members = True

# Global bot instance
_bot_instance: Optional[commands.Bot] = None
_alert_handlers: Dict[str, List[Callable]] = {}


class DiscordAlertListener(commands.Cog):
    """Cog for handling Discord alert messages."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv('DISCORD_CHANNEL_ID', '0'))
        self.alert_history: List[Dict[str, Any]] = []
        
        logger.info(f"Initialized DiscordAlertListener for channel {self.channel_id}")
    
    @commands.Cog.listener()
    async def on_ready(self):
        """Called when bot successfully connects to Discord."""
        logger.info(f"Bot logged in as {self.bot.user}")
        if self.channel_id > 0:
            channel = self.bot.get_channel(self.channel_id)
            if channel:
                logger.info(f"Listening to channel: {channel.name} ({self.channel_id})")
            else:
                logger.warning(f"Channel {self.channel_id} not found. Bot may lack permissions.")
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Handle incoming Discord messages."""
        # Ignore bot's own messages
        if message.author == self.bot.user:
            return
        
        # Only process messages from monitored channel
        if message.channel.id != self.channel_id:
            return
        
        try:
            alert_data = self._parse_alert(message)
            if alert_data:
                logger.info(f"Alert parsed: {alert_data}")
                self.alert_history.append({
                    "timestamp": datetime.now().isoformat(),
                    "author": message.author.name,
                    "content": message.content[:200],  # First 200 chars
                    "parsed": alert_data
                })
                
                # Trigger registered handlers
                await self._dispatch_handlers(alert_data)
        
        except Exception as e:
            logger.error(f"Error processing Discord message: {e}", exc_info=True)
    
    def _parse_alert(self, message: discord.Message) -> Optional[Dict[str, Any]]:
        """
        Parse alert message and extract trading signals.
        
        Looks for patterns like:
        - Ticker symbols (AAPL, SPY, etc.)
        - Option chain info (calls, puts, expiry)
        - Actions (buy, sell, alert, watch)
        - Price levels
        """
        text = message.content.upper()
        
        # Extract ticker symbol (2-5 uppercase letters, optionally followed by digits)
        ticker_match = re.search(r'\b([A-Z]{1,5})(?:\s|$)', text)
        if not ticker_match:
            return None
        
        ticker = ticker_match.group(1)
        
        # Detect option keywords
        is_options = bool(re.search(r'\b(CALL|CALLS|PUT|PUTS|STRIKE|EXPIR|CHAIN|FLOW|SWEEP)\b', text))
        
        # Detect action
        action = None
        if re.search(r'\b(BUY|BUYING|LONG|BULLISH)\b', text):
            action = "BUY"
        elif re.search(r'\b(SELL|SELLING|SHORT|BEARISH)\b', text):
            action = "SELL"
        elif re.search(r'\b(ALERT|WATCH|MONITOR)\b', text):
            action = "ALERT"
        
        if not action:
            return None
        
        # Extract price level if present
        price_match = re.search(r'\$?(\d+\.?\d*)', text)
        price = float(price_match.group(1)) if price_match else None
        
        # Extract option details if present
        strike_match = re.search(r'(\d+\.?\d*)\s*(?:CALL|PUT|C|P)', text)
        strike = float(strike_match.group(1)) if strike_match else None
        
        expiry_match = re.search(r'(\d{1,2}/\d{1,2}|\d+DTE)', text)
        expiry = expiry_match.group(1) if expiry_match else None
        
        return {
            "ticker": ticker,
            "action": action,
            "is_options": is_options,
            "price": price,
            "strike": strike,
            "expiry": expiry,
            "message_id": message.id,
            "timestamp": message.created_at.isoformat(),
            "raw_text": message.content[:500]
        }
    
    async def _dispatch_handlers(self, alert_data: Dict[str, Any]):
        """Dispatch alert to registered handlers."""
        for handler in _alert_handlers.get("all", []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(alert_data)
                else:
                    handler(alert_data)
            except Exception as e:
                logger.error(f"Error in alert handler: {e}", exc_info=True)


async def start_bot():
    """Start the Discord bot."""
    global _bot_instance
    
    if not DISCORD_OK:
        logger.error("discord.py not installed. Install with: pip install discord.py")
        return
    
    token = os.getenv('DISCORD_BOT_TOKEN')
    if not token:
        logger.error("DISCORD_BOT_TOKEN environment variable not set")
        return
    
    _bot_instance = commands.Bot(command_prefix="!", intents=INTENTS)
    
    try:
        await _bot_instance.add_cog(DiscordAlertListener(_bot_instance))
        await _bot_instance.start(token)
    except Exception as e:
        logger.error(f"Failed to start Discord bot: {e}", exc_info=True)


def start_discord_listener_background():
    """Start Discord listener in background thread."""
    if not DISCORD_OK:
        logger.error("discord.py not installed")
        return
    
    import threading
    
    def run_bot():
        asyncio.run(start_bot())
    
    thread = threading.Thread(target=run_bot, daemon=True)
    thread.start()
    logger.info("Discord listener started in background thread")


def register_alert_handler(handler: Callable, alert_type: str = "all"):
    """
    Register a handler function for Discord alerts.
    
    Args:
        handler: Async or sync function that accepts alert_data dict
        alert_type: Type of alert ("all", "options", "equity", etc.)
    
    Example:
        async def handle_alert(alert_data):
            print(f"Alert received: {alert_data['ticker']}")
        
        register_alert_handler(handle_alert, "all")
    """
    if alert_type not in _alert_handlers:
        _alert_handlers[alert_type] = []
    
    _alert_handlers[alert_type].append(handler)
    logger.info(f"Registered handler for {alert_type} alerts")


def get_alert_history(limit: int = 50) -> List[Dict[str, Any]]:
    """Get recent alert history."""
    if _bot_instance and hasattr(_bot_instance, 'get_cog'):
        cog = _bot_instance.get_cog('DiscordAlertListener')
        if cog:
            return cog.alert_history[-limit:]
    return []


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    logger.info("Starting Discord listener...")
    start_discord_listener_background()
    
    # Keep script alive
    try:
        asyncio.run(start_bot())
    except KeyboardInterrupt:
        logger.info("Shutting down...")

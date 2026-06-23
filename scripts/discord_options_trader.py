"""
Discord Alerts to Options Execution
====================================
Listens for Discord alerts and automatically triggers options trading logic.
Integrates with the existing options execution engine.

Usage
-----
  # Run with Discord listener enabled
  DISCORD_BOT_TOKEN=<token> DISCORD_CHANNEL_ID=777327652666998804 python scripts/discord_options_trader.py

Environment Variables
---------------------
  DISCORD_BOT_TOKEN        Discord bot token (required)
  DISCORD_CHANNEL_ID       Channel to monitor (default: 777327652666998804)
  DISCORD_OPTIONS_MODE     "paper" or "live" (default: paper)
  DISCORD_CONFIDENCE_MIN   Minimum confidence threshold 0-100 (default: 70)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional
import asyncio

# Add repo root to path
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.discord_listener import (
    start_discord_listener_background,
    register_alert_handler,
    get_alert_history,
    DISCORD_OK
)

logger = logging.getLogger(__name__)


class DiscordOptionsExecutor:
    """Executes options trades based on Discord alerts."""
    
    def __init__(self):
        self.mode = os.getenv('DISCORD_OPTIONS_MODE', 'paper')
        self.confidence_min = int(os.getenv('DISCORD_CONFIDENCE_MIN', '70'))
        self.trade_count = 0
        self.alert_count = 0
        
        logger.info(f"DiscordOptionsExecutor initialized: mode={self.mode}, min_confidence={self.confidence_min}%")
    
    async def handle_alert(self, alert_data: dict):
        """Process Discord alert and execute options trade if conditions met."""
        self.alert_count += 1
        
        ticker = alert_data.get('ticker')
        action = alert_data.get('action')
        is_options = alert_data.get('is_options')
        
        logger.info(f"[Alert #{self.alert_count}] {ticker} - {action} - Options: {is_options}")
        
        # Only process options alerts
        if not is_options:
            logger.debug(f"Skipping non-options alert for {ticker}")
            return
        
        # Validate alert data
        if not self._validate_alert(alert_data):
            logger.warning(f"Alert validation failed for {ticker}")
            return
        
        # Calculate confidence/score
        confidence = self._score_alert(alert_data)
        logger.info(f"Alert confidence for {ticker}: {confidence}%")
        
        if confidence < self.confidence_min:
            logger.info(f"Confidence {confidence}% < threshold {self.confidence_min}%. Skipping.")
            return
        
        # Execute trade based on alert
        await self._execute_trade(alert_data, confidence)
    
    def _validate_alert(self, alert_data: dict) -> bool:
        """Validate alert has minimum required data."""
        required = ['ticker', 'action']
        for field in required:
            if not alert_data.get(field):
                logger.warning(f"Missing required field: {field}")
                return False
        return True
    
    def _score_alert(self, alert_data: dict) -> int:
        """Score alert confidence 0-100 based on data richness and specificity."""
        score = 50  # base score
        
        # Add points for specific data
        if alert_data.get('strike'):
            score += 20
        if alert_data.get('expiry'):
            score += 15
        if alert_data.get('price'):
            score += 10
        if alert_data.get('action') in ['BUY', 'SELL']:
            score += 5
        
        # Cap at 100
        return min(score, 100)
    
    async def _execute_trade(self, alert_data: dict, confidence: int):
        """Execute options trade based on alert."""
        ticker = alert_data['ticker']
        action = alert_data['action']
        strike = alert_data.get('strike')
        expiry = alert_data.get('expiry')
        
        self.trade_count += 1
        
        logger.info(f"[Trade #{self.trade_count}] Executing {action} for {ticker}")
        logger.info(f"  Strike: {strike}, Expiry: {expiry}, Confidence: {confidence}%")
        logger.info(f"  Mode: {self.mode}, Message ID: {alert_data.get('message_id')}")
        
        # TODO: Integrate with actual options execution engine
        # For now, log the execution and store in audit log
        
        # Example integration point:
        # from engine.options.execution import execute_options_trade
        # result = await execute_options_trade(
        #     ticker=ticker,
        #     action=action.lower(),
        #     strike=strike,
        #     expiry=expiry,
        #     mode=self.mode,
        #     confidence=confidence,
        #     source="discord",
        #     message_id=alert_data.get('message_id')
        # )
        
        logger.info(f"Trade execution recorded (demo mode)")


async def main():
    """Main entry point for Discord options trader."""
    if not DISCORD_OK:
        logger.error("discord.py not installed. Run: pip install discord.py")
        return
    
    executor = DiscordOptionsExecutor()
    
    # Register the alert handler
    register_alert_handler(executor.handle_alert, alert_type="all")
    
    # Start Discord listener in background
    logger.info("Starting Discord listener...")
    start_discord_listener_background()
    
    # Keep alive and monitor
    try:
        logger.info("Discord options trader running. Press Ctrl+C to stop.")
        while True:
            await asyncio.sleep(60)
            
            # Periodic status
            history = get_alert_history(limit=5)
            if history:
                logger.info(f"Recent alerts: {len(history)} in last check")
    
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Verify environment
    if not os.getenv('DISCORD_BOT_TOKEN'):
        logger.error("DISCORD_BOT_TOKEN not set. Set it with:")
        logger.error("  export DISCORD_BOT_TOKEN=<your-bot-token>")
        sys.exit(1)
    
    channel_id = os.getenv('DISCORD_CHANNEL_ID', '777327652666998804')
    logger.info(f"Configured for Discord channel: {channel_id}")
    
    asyncio.run(main())

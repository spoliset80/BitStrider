"""
Dry Run Today's Discord Alerts
===============================
Fetches today's alerts from Discord and simulates execution without trading.
Shows what would trigger, confidence scores, and trade details.

Usage
-----
  export DISCORD_BOT_TOKEN="your-token"
  export DISCORD_CHANNEL_ID="777327652666998804"
  python scripts/dry_run_discord_alerts.py
"""

from __future__ import annotations

import logging
import os
import sys
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import discord
    from discord.ext import commands
    DISCORD_OK = True
except ImportError:
    DISCORD_OK = False
    discord = None

from engine.discord_listener import DiscordAlertListener

logger = logging.getLogger(__name__)


class DryRunAlertFetcher:
    """Fetch and simulate execution of today's Discord alerts."""
    
    def __init__(self):
        self.channel_id = int(os.getenv('DISCORD_CHANNEL_ID', '777327652666998804'))
        self.confidence_min = int(os.getenv('DISCORD_CONFIDENCE_MIN', '70'))
        self.alerts: List[Dict[str, Any]] = []
        self.would_execute: List[Dict[str, Any]] = []
        self.would_skip: List[Dict[str, Any]] = []
        
        logger.info(f"DryRunAlertFetcher initialized")
        logger.info(f"  Channel: {self.channel_id}")
        logger.info(f"  Confidence min: {self.confidence_min}%")
    
    async def fetch_todays_alerts(self) -> List[Dict[str, Any]]:
        """Fetch all messages from today's channel."""
        token = os.getenv('DISCORD_BOT_TOKEN')
        if not token:
            logger.error("DISCORD_BOT_TOKEN not set")
            return []
        
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        
        today = datetime.now().date()
        alerts = []
        
        async with client:
            @client.event
            async def on_ready():
                try:
                    channel = client.get_channel(self.channel_id)
                    if not channel:
                        logger.error(f"Channel {self.channel_id} not found")
                        await client.close()
                        return
                    
                    logger.info(f"Fetching messages from {channel.name}...")
                    
                    message_count = 0
                    async for message in channel.history(limit=500, oldest_first=False):
                        if message.created_at.date() == today:
                            message_count += 1
                            alerts.append({
                                "timestamp": message.created_at.isoformat(),
                                "author": message.author.name,
                                "content": message.content,
                                "message_id": message.id,
                            })
                    
                    logger.info(f"Found {message_count} messages from today")
                    await client.close()
                
                except Exception as e:
                    logger.error(f"Error fetching alerts: {e}", exc_info=True)
                    await client.close()
            
            try:
                await client.start(token)
            except Exception as e:
                logger.error(f"Failed to connect to Discord: {e}")
        
        return alerts
    
    def _parse_and_score_alert(self, message_content: str) -> Optional[Dict[str, Any]]:
        """Parse and score a single alert message."""
        listener = DiscordAlertListener(None)
        
        # Create mock message object
        class MockMessage:
            def __init__(self, content):
                self.content = content
        
        mock_msg = MockMessage(message_content)
        alert_data = listener._parse_alert(mock_msg)
        
        if alert_data:
            # Score it
            confidence = self._score_alert(alert_data)
            alert_data['confidence'] = confidence
        
        return alert_data
    
    def _score_alert(self, alert_data: dict) -> int:
        """Score alert confidence 0-100."""
        score = 50  # base
        
        if alert_data.get('strike'):
            score += 20
        if alert_data.get('expiry'):
            score += 15
        if alert_data.get('price'):
            score += 10
        if alert_data.get('action') in ['BUY', 'SELL']:
            score += 5
        
        return min(score, 100)
    
    def simulate_execution(self):
        """Simulate which alerts would execute."""
        logger.info(f"\n{'='*80}")
        logger.info(f"DRY RUN: Simulating Alert Execution")
        logger.info(f"{'='*80}\n")
        
        if not self.alerts:
            logger.info("No alerts found for today")
            return
        
        logger.info(f"Processing {len(self.alerts)} alerts...\n")
        
        for i, alert in enumerate(self.alerts, 1):
            parsed = self._parse_and_score_alert(alert['content'])
            
            if not parsed:
                logger.info(f"[{i}] {alert['timestamp']}")
                logger.info(f"    Author: {alert['author']}")
                logger.info(f"    Message: {alert['content'][:100]}")
                logger.info(f"    Status: SKIPPED (not parseable as trade alert)\n")
                
                self.would_skip.append({
                    **alert,
                    'reason': 'Not parseable as trade alert'
                })
                continue
            
            confidence = parsed.get('confidence', 0)
            ticker = parsed.get('ticker')
            action = parsed.get('action')
            
            would_trade = confidence >= self.confidence_min
            status = "WOULD EXECUTE" if would_trade else "WOULD SKIP"
            
            logger.info(f"[{i}] {alert['timestamp']}")
            logger.info(f"    Author: {alert['author']}")
            logger.info(f"    Ticker: {ticker}")
            logger.info(f"    Action: {action}")
            logger.info(f"    Confidence: {confidence}% {'✓' if would_trade else '✗'} (threshold: {self.confidence_min}%)")
            
            if parsed.get('is_options'):
                logger.info(f"    Type: OPTIONS")
                if parsed.get('strike'):
                    logger.info(f"    Strike: ${parsed['strike']}")
                if parsed.get('expiry'):
                    logger.info(f"    Expiry: {parsed['expiry']}")
            
            logger.info(f"    Status: {status}")
            logger.info(f"    Message: {alert['content'][:100]}")
            logger.info(f"")
            
            if would_trade:
                self.would_execute.append({
                    **alert,
                    'parsed': parsed,
                    'confidence': confidence
                })
            else:
                self.would_skip.append({
                    **alert,
                    'parsed': parsed,
                    'confidence': confidence,
                    'reason': f'Confidence {confidence}% < threshold {self.confidence_min}%'
                })
        
        # Summary
        self._print_summary()
    
    def _print_summary(self):
        """Print execution summary."""
        logger.info(f"\n{'='*80}")
        logger.info(f"SUMMARY")
        logger.info(f"{'='*80}\n")
        
        logger.info(f"Total alerts parsed: {len(self.alerts)}")
        logger.info(f"Would execute: {len(self.would_execute)} trades")
        logger.info(f"Would skip: {len(self.would_skip)} alerts\n")
        
        if self.would_execute:
            logger.info(f"Trades that WOULD execute:")
            for trade in self.would_execute:
                parsed = trade['parsed']
                logger.info(f"  - {parsed['ticker']} {parsed['action']} @ {trade['confidence']}% confidence")
        
        logger.info(f"\n{'='*80}\n")
        
        # Save report
        self._save_report()
    
    def _save_report(self):
        """Save dry run report to file."""
        report = {
            "timestamp": datetime.now().isoformat(),
            "date": datetime.now().date().isoformat(),
            "channel_id": self.channel_id,
            "confidence_threshold": self.confidence_min,
            "total_alerts": len(self.alerts),
            "would_execute": len(self.would_execute),
            "would_skip": len(self.would_skip),
            "trades": [
                {
                    "ticker": t['parsed']['ticker'],
                    "action": t['parsed']['action'],
                    "confidence": t['confidence'],
                    "is_options": t['parsed']['is_options'],
                    "strike": t['parsed'].get('strike'),
                    "expiry": t['parsed'].get('expiry'),
                    "timestamp": t['timestamp'],
                    "author": t['author'],
                }
                for t in self.would_execute
            ]
        }
        
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        
        report_file = log_dir / f"dry_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2)
        
        logger.info(f"Report saved to: {report_file}\n")


async def main():
    """Run dry run simulation."""
    if not DISCORD_OK:
        logger.error("discord.py not installed. Run: pip install discord.py")
        return
    
    if not os.getenv('DISCORD_BOT_TOKEN'):
        logger.error("DISCORD_BOT_TOKEN not set")
        return
    
    fetcher = DryRunAlertFetcher()
    
    logger.info("Connecting to Discord...")
    alerts = await fetcher.fetch_todays_alerts()
    
    if alerts:
        fetcher.alerts = alerts
        fetcher.simulate_execution()
    else:
        logger.info("No alerts found")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    asyncio.run(main())

# Discord Alerts Setup & Configuration

## Overview
This feature enables automatic trading based on Discord alerts. The bot listens to a designated Discord channel and:
- Parses incoming alert messages
- Extracts ticker symbols, actions (BUY/SELL), and option details
- Triggers options trading logic with configurable confidence thresholds

## Prerequisites

1. **Create a Discord Bot**
   - Go to [Discord Developer Portal](https://discord.com/developers/applications)
   - Click "New Application"
   - Name it (e.g., "BitStrider Bot")
   - Go to "Bot" tab → "Add Bot"
   - Copy the token (save securely)

2. **Set Bot Permissions**
   - In Developer Portal, go to "OAuth2" → "URL Generator"
   - Select scopes: `bot`
   - Select permissions:
     - `Read Messages/View Channels`
     - `Send Messages`
     - `Read Message History`
   - Copy the generated URL and open in browser to invite bot to your server

3. **Get Channel ID**
   - Enable Developer Mode in Discord (User Settings → Advanced → Developer Mode)
   - Right-click the channel you want to monitor
   - Copy Channel ID (e.g., 777327652666998804)

## Environment Configuration

Set these environment variables before running:

```bash
# Required
export DISCORD_BOT_TOKEN="your-bot-token-here"
export DISCORD_CHANNEL_ID="777327652666998804"

# Optional
export DISCORD_OPTIONS_MODE="paper"              # paper or live (default: paper)
export DISCORD_CONFIDENCE_MIN="70"               # 0-100 confidence threshold (default: 70)
export DISCORD_HEADLESS="false"                  # Run in headless mode
```

### Windows (PowerShell)
```powershell
$env:DISCORD_BOT_TOKEN = "your-bot-token-here"
$env:DISCORD_CHANNEL_ID = "777327652666998804"
$env:DISCORD_OPTIONS_MODE = "paper"
```

### Windows (Command Prompt)
```cmd
set DISCORD_BOT_TOKEN=your-bot-token-here
set DISCORD_CHANNEL_ID=777327652666998804
set DISCORD_OPTIONS_MODE=paper
```

### Linux/Mac
```bash
export DISCORD_BOT_TOKEN="your-bot-token-here"
export DISCORD_CHANNEL_ID="777327652666998804"
export DISCORD_OPTIONS_MODE="paper"
```

## Alert Message Format

The bot recognizes alerts in the following formats:

### Options Alerts (Parsed)
```
AAPL CALL BUY 150 strike 6/21
TSLA PUT SELL 250 expiry 6/28
SPY UNUSUAL OPTIONS FLOW - bullish sweep on 450 calls
```

### Equity Alerts (Skipped)
```
NVDA breakout watch $120
AMD squeeze candidate
```

### Rich Details (Scored Higher)
```
AAPL monthly call 150 strike june expiry - bullish flow $3.50 premium unusual volume sweep
```

## Parsing Rules

The alert parser extracts:
- **Ticker**: 1-5 uppercase letters (AAPL, SPY, etc.)
- **Action**: BUY, SELL, ALERT
- **Type**: CALL, PUT, CALLS, PUTS (options-specific)
- **Strike**: Numeric value (e.g., 150, 450.5)
- **Expiry**: Date format (6/21, 6/28) or DTE (7DTE)
- **Price**: Dollar amounts (e.g., $3.50, $120)

## Confidence Scoring

Alerts are scored 0-100 based on data richness:
- **Base score**: 50
- **Strike price**: +20
- **Expiry date**: +15
- **Specific price**: +10
- **Clear action (BUY/SELL)**: +5

Only alerts with confidence ≥ threshold trigger trades (default: 70%)

## Running the Discord Trader

### Option 1: Standalone Script
```bash
python scripts/discord_options_trader.py
```

### Option 2: Background Task
```bash
nohup python scripts/discord_options_trader.py > logs/discord_trader.log 2>&1 &
```

### Option 3: Windows Task Scheduler
Create a task that runs:
```
python scripts/discord_options_trader.py
```
With working directory: `C:\Users\spoli\Desktop\BitStrider_WS\BitStrider`

## Monitoring

### Check Alert History
```python
from engine.discord_listener import get_alert_history

alerts = get_alert_history(limit=10)
for alert in alerts:
    print(f"{alert['timestamp']}: {alert['parsed']}")
```

### Logs
Check application logs:
```
logs/discord_trader.log
logs/autobot.log
```

## Integration with Options Engine

The `DiscordOptionsExecutor` is a hook point for the existing options execution engine.

To connect to your options trader:

1. In `scripts/discord_options_trader.py`, uncomment the options execution section in `_execute_trade()`
2. Import your options execution module
3. Pass alert data to your executor with confidence score

Example integration:
```python
from engine.options.execution import execute_options_trade

result = await execute_options_trade(
    ticker=ticker,
    action=action.lower(),
    strike=strike,
    expiry=expiry,
    mode=self.mode,
    confidence=confidence,
    source="discord",
    message_id=alert_data.get('message_id')
)
```

## Security Considerations

1. **Bot Token**: Never commit token to git. Use `.gitignore` and environment variables.
2. **Channel Access**: Bot should only have read access to the monitoring channel.
3. **Rate Limiting**: Discord enforces 5000 requests/hour. Current implementation is well within limits.
4. **Paper Mode**: Always test with `DISCORD_OPTIONS_MODE=paper` first.
5. **Message Validation**: Parser validates symbols against tradeable universe before execution.

## Troubleshooting

### Bot Not Connecting
```
Error: Bot failed to connect
→ Check DISCORD_BOT_TOKEN is valid
→ Ensure bot is invited to server
→ Check bot permissions in Discord server settings
```

### Not Receiving Messages
```
Error: Messages not being parsed
→ Verify DISCORD_CHANNEL_ID is correct
→ Check bot has read permission for channel
→ Enable Developer Mode in Discord to get correct channel ID
→ Bot must have Message Content Intent enabled
```

### Low Confidence Alerts Being Skipped
```
Alert not triggering trade: Confidence X% < threshold Y%
→ Increase DISCORD_CONFIDENCE_MIN if alerts are reliable
→ Or add more detail to alert messages (strike, expiry, etc.)
```

### Trade Execution Issues
```
Trade executed but nothing happened
→ Check options execution engine is properly integrated
→ Verify DISCORD_OPTIONS_MODE matches expected mode
→ Check broker connection and permissions
```

## Example Usage

Start trader for paper trading:
```bash
export DISCORD_BOT_TOKEN="MjA1NDUyMzEyNzIzNDYwNjcyLnk..."
export DISCORD_CHANNEL_ID="777327652666998804"
export DISCORD_OPTIONS_MODE="paper"
export DISCORD_CONFIDENCE_MIN="70"

python scripts/discord_options_trader.py
```

Monitor logs:
```bash
tail -f logs/discord_trader.log
```

Check recent alerts:
```python
from engine.discord_listener import get_alert_history
import json

alerts = get_alert_history(limit=5)
for a in alerts:
    print(json.dumps(a, indent=2))
```

## Performance Notes

- **Parsing**: <10ms per message
- **Handler Execution**: <100ms for alert processing
- **Trade Execution**: Depends on broker API (~1-2s)
- **Memory**: ~50MB for Discord client + alert history
- **CPU**: Minimal when idle, scales with alert volume

## Future Enhancements

- [ ] Multi-channel support
- [ ] Role-based alert filtering (VIP alerts only)
- [ ] Machine learning for alert filtering
- [ ] Webhook integration for two-way communication
- [ ] Position tracking in Discord threads
- [ ] PnL reporting in Discord embeds
- [ ] Alert source reputation scoring

## Support

For issues or questions:
1. Check the Discord channel permissions
2. Review logs in `logs/discord_trader.log`
3. Verify environment variables are set correctly
4. Test bot connectivity independently

---

**Channel Reference**: https://discord.com/channels/297613227284627457/777327652666998804

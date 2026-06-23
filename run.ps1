param(
    [ValidateSet("paper","live")][string]$Mode = "paper",
    [int]$Poll = 30
)

$env:DISCORD_OPTIONS_MODE = $Mode
apextrader\Scripts\python.exe scripts/discord_api_reader.py --loop --poll $Poll

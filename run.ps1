param(
    [ValidateSet("paper","live")][string]$Mode = "paper",
    [int]$Poll = 30
)

$env:DISCORD_OPTIONS_MODE = $Mode
# Invokes python using the module path relative to your project root
apextrader\Scripts\python.exe -m scripts.discord_api_reader --loop --poll $Poll
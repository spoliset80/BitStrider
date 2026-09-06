# Run this in a REGULAR (non-admin) PowerShell window and leave the window open.
# Bypasses Task Scheduler/S4U entirely - today's mystery is specific to that
# on-demand trigger path, not to the bot itself. Ctrl+C to stop.

$BaseDir = "c:\Users\BG\OneDrive\Returns-LSTM\StockPricePrediction\BitStrider-main"
Set-Location $BaseDir

$Python = (Get-Command py -ErrorAction SilentlyContinue).Source
if (-not $Python) { $Python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $Python) { Write-Host "No Python found on PATH." -ForegroundColor Red; exit 1 }

Write-Host "Starting AutoBot in the foreground - leave this window open." -ForegroundColor Cyan
& $Python autobot.py

# ApexTrader local runner (Windows PowerShell)
param(
    [ValidateSet('paper', 'live')]
    [string]$Mode = 'paper'
)

$ErrorActionPreference = 'Stop'

# Machine-local venv, not the repo's old OneDrive-synced apextrader/ folder:
# a synced venv gets its pyvenv.cfg "home" clobbered by whichever machine
# wrote it last, breaking python.exe on every other machine. Run autobot.py
# (or main.py once) first to create/bootstrap it here if it doesn't exist yet.
$venvActivate = Join-Path $env:LOCALAPPDATA 'ApexTrader\venv\Scripts\Activate.ps1'
if (Test-Path $venvActivate) {
    Write-Host "Activating venv: $venvActivate"
    . $venvActivate
} else {
    Write-Warning "Virtualenv activation script not found at $venvActivate. Run autobot.py once to create it."
}

$env:TRADE_MODE = $Mode

$modeKey = if ($Mode -eq 'paper') { $env:PAPER_ALPACA_API_KEY } else { $env:LIVE_ALPACA_API_KEY }
if (-not $modeKey) {
    Write-Warning "Keys not set: define PAPER_ALPACA_API_KEY or LIVE_ALPACA_API_KEY in .env"
}

Write-Host "Launching main.py in $Mode mode (TRADE_MODE=$($env:TRADE_MODE))"
python .\main.py

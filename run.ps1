param(
    [ValidateSet("paper","live")][string]$Mode = "paper",
    [int]$Poll       = 60,
    [string]$MarketOpen  = "09:30",
    [string]$MarketClose = "16:00",
    [string]$TimeZone    = "Eastern Standard Time"
)

$env:DISCORD_OPTIONS_MODE = $Mode

# ── Single-instance guard (PID file) ─────────────────────────────────────────
$PidFile = Join-Path $PSScriptRoot "discord_bot.pid"
if (Test-Path $PidFile) {
    $oldPid = Get-Content $PidFile -Raw
    $running = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($running) {
        Write-Warning "Bot already running (PID $oldPid). Exiting to prevent duplicate orders."
        exit 1
    } else {
        Write-Host "Stale PID file found (PID $oldPid no longer running). Continuing."
    }
}
$PID | Out-File $PidFile -Encoding ascii
Write-Host "PID $PID written to $PidFile"

# Clean up PID file on exit (Ctrl+C, error, normal exit)
$null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
    Remove-Item $PidFile -ErrorAction SilentlyContinue
}
try {

# Pass market-hours settings to Python (which owns the loop + gating).
$env:DISCORD_MARKET_OPEN  = $MarketOpen
$env:DISCORD_MARKET_CLOSE = $MarketClose

Write-Host "Discord Alert Trader (supervisor) | mode=$Mode | poll=${Poll}s | market $MarketOpen-$MarketClose ($TimeZone)"
Write-Host "Python owns the poll loop + market-hours gating. Supervisor restarts it on crash."

# ── Supervisor loop: keep Python alive, restart with backoff on crash ─────────
$restarts = 0
while ($true) {
    $startedAt = Get-Date
    Write-Host "[$($startedAt.ToString('HH:mm:ss'))] Starting poller (restart #$restarts)..."

    apextrader\Scripts\python.exe -m scripts.discord_api_reader --loop --poll $Poll --history 10
    $code = $LASTEXITCODE

    $ranSec = [int]((Get-Date) - $startedAt).TotalSeconds
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Poller exited (code=$code) after ${ranSec}s."

    # If it ran for a while then died, reset backoff. If it crashed instantly, back off.
    if ($ranSec -gt 60) { $restarts = 0 } else { $restarts++ }
    $backoff = [Math]::Min(300, 5 * [Math]::Max(1, $restarts))
    Write-Host "Restarting in ${backoff}s... (Ctrl+C to stop)"
    Start-Sleep -Seconds $backoff
}
} finally {
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Write-Host "Bot stopped. PID file removed."
}
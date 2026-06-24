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

$tz     = [System.TimeZoneInfo]::FindSystemTimeZoneById($TimeZone)
$open   = [System.TimeSpan]::Parse($MarketOpen)
$close  = [System.TimeSpan]::Parse($MarketClose)

Write-Host "Discord Alert Trader | mode=$Mode | poll=${Poll}s | market $MarketOpen-$MarketClose EST"

while ($true) {
    $nowEst  = [System.TimeZoneInfo]::ConvertTimeFromUtc([System.DateTime]::UtcNow, $tz)
    $nowTime = $nowEst.TimeOfDay
    $today   = $nowEst.DayOfWeek

    # Skip weekends entirely
    if ($today -eq "Saturday" -or $today -eq "Sunday") {
        $waitSec = 3600
        Write-Host "[$($nowEst.ToString('HH:mm')) EST] Weekend -- sleeping 60m"
        Start-Sleep -Seconds $waitSec
        continue
    }

    if ($nowTime -ge $open -and $nowTime -lt $close) {
        # Market hours: run one poll cycle and sleep $Poll seconds
        apextrader\Scripts\python.exe -m scripts.discord_api_reader_v2 --poll $Poll --history 10
        Start-Sleep -Seconds $Poll
    } else {
        # Outside market hours: sleep until next open (or 5 min if already past close)
        if ($nowTime -lt $open) {
            $secsUntilOpen = [int](($open - $nowTime).TotalSeconds)
        } else {
            # Past close -- sleep until next day open (rough: remaining day + open offset)
            $midnight = [System.TimeSpan]::FromHours(24)
            $secsUntilOpen = [int](($midnight - $nowTime + $open).TotalSeconds)
        }
        $waitMin = [int]($secsUntilOpen / 60)
        Write-Host "[$($nowEst.ToString('HH:mm')) EST] Outside market hours -- sleeping ${waitMin}m until $MarketOpen open"
        # Sleep in chunks so Ctrl+C still works
        $slept = 0
        while ($slept -lt $secsUntilOpen) {
            Start-Sleep -Seconds ([Math]::Min(300, $secsUntilOpen - $slept))
            $slept += 300
        }
    }
}
} finally {
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Write-Host "Bot stopped. PID file removed."
}
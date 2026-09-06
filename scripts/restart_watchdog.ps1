# Run this in an elevated (Administrator) PowerShell - right-click PowerShell,
# "Run as administrator", then paste/run this file.
#
# Restarts the ApexTraderAutoRun watchdog task from an elevated session, which
# is required for its Highest/S4U configuration to establish its session
# correctly on an on-demand (non-triggered) start - confirmed 2026-08-05: the
# same restart attempted from a non-elevated session died in ~1 second.
#
# Also clears out any orphaned main.py from a prior watchdog death before
# restarting, though AutoBotWatchdog.kill_existing_project_processes() does
# this too on its own once it's actually running.

$ErrorActionPreference = 'Continue'

Write-Host "Clearing any orphaned python processes first..." -ForegroundColor Yellow
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object {
    if ($_.CommandLine -and $_.CommandLine -match 'main\.py|watchdog\.py') {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
            Write-Host "  Killed orphan pid $($_.ProcessId)" -ForegroundColor Green
        } catch {
            Write-Host "  Could not kill pid $($_.ProcessId): $($_.Exception.Message)" -ForegroundColor Red
        }
    }
}

Write-Host "`nStarting ApexTraderAutoRun..." -ForegroundColor Yellow
Start-ScheduledTask -TaskName 'ApexTraderAutoRun'

Start-Sleep -Seconds 8
$pidFile = 'c:\Users\BG\OneDrive\Returns-LSTM\StockPricePrediction\BitStrider-main\autobot.pid'
$newPid = (Get-Content $pidFile -ErrorAction SilentlyContinue)
if ($newPid -and (Get-Process -Id $newPid -ErrorAction SilentlyContinue)) {
    Write-Host "`nOK: watchdog is alive (pid $newPid), still running 8s after start." -ForegroundColor Cyan
} else {
    Write-Host "`nWARNING: watchdog pid $newPid is not running 8s after start - check autobot.log." -ForegroundColor Red
}

Write-Host "`n--- Task info ---"
Get-ScheduledTaskInfo -TaskName 'ApexTraderAutoRun' | Select-Object LastRunTime, LastTaskResult, NextRunTime

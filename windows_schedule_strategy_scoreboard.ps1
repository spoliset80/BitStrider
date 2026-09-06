# Windows Scheduled Task Install for the Strategy Scoreboard
# Run in PowerShell as Administrator.
#
# 2026-08-15, user request: idea #4 of six suggested improvements -- "a
# recurring strategy scoreboard instead of one-off manual reviews". Weekly
# is plenty (unlike TI capture, this isn't time-sensitive market data --
# it's a health check over accumulated trade history, which doesn't shift
# meaningfully day to day). Mirrors windows_schedule_ti_capture.ps1's
# pattern: Task Scheduler owns the cadence, not an internal Python loop.

$taskName = 'ApexTraderStrategyScoreboard'
$taskDescription = 'Weekly Kelly/win-rate health check across every strategy -- flags any enabled strategy with n>=10 trades and negative Kelly. Writes strategy_scoreboard.log.'
$BaseDir = $PSScriptRoot

$action = New-ScheduledTaskAction -Execute 'python.exe' -Argument "`"$BaseDir\scripts\strategy_scoreboard.py`"" -WorkingDirectory $BaseDir

# Every Monday 07:00 -- before the trading week starts, using the prior
# week's data.
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 07:00

# No elevation needed -- this only reads Alpaca order history (network,
# same creds as the live bot via .env) and writes a log file under the
# user's own OneDrive folder.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:UserName" -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $taskDescription

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    throw "Not running as Administrator. Right-click PowerShell and 'Run as administrator', then re-run this script."
}

Register-ScheduledTask -TaskName $taskName -InputObject $task -Force -ErrorAction Stop
Write-Host "Scheduled task '$taskName' installed successfully. Use 'schtasks /query /tn $taskName' to inspect."
Write-Host "Run it once now with: Start-ScheduledTask -TaskName '$taskName'"

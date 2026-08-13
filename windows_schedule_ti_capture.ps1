# Windows Scheduled Task Install for the TI Capture scraper
# Run in PowerShell as Administrator.

$taskName = 'ApexTraderTICapture'
$taskDescription = 'Refresh data/ti_primary.json every 20 min, Mon-Fri 06:00-20:00 — single-shot runs owned by Task Scheduler'
$BaseDir = $PSScriptRoot

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$BaseDir\scripts\run_ti_capture_task.ps1`""

# Starts immediately at log on (needs your real, already-logged-in Edge/TI session)...
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn

# ...then re-fires every 20 min, Mon-Fri, independent of whether the previous run
# crashed. Task Scheduler owns the cadence instead of an internal Python loop —
# that loop crashed once (2026-08-03, locked Edge profile) and stayed dead for
# 30+ hours because AtLogOn was the only trigger and nothing re-armed it.
$weekdayTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 06:00
# .Repetition isn't settable in place (returns a fresh, disconnected CIM instance
# each access) — build the repetition pattern separately and assign it whole.
$repetitionClass = Get-CimClass -ClassName MSFT_TaskRepetitionPattern -Namespace Root/Microsoft/Windows/TaskScheduler
$repetition = New-CimInstance -CimClass $repetitionClass -ClientOnly
$repetition.Interval = 'PT20M'
$repetition.Duration = 'PT14H'   # 06:00 -> 20:00, covers pre-market through after-hours
$repetition.StopAtDurationEnd = $false
$weekdayTrigger.Repetition = $repetition

$trigger = @($logonTrigger, $weekdayTrigger)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:UserName" -LogonType Interactive -RunLevel Highest

# IgnoreNew: if a run is still going (or wedged) when the next 20-min slot fires,
# skip that slot rather than piling up overlapping Edge sessions.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $taskDescription

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    throw "Not running as Administrator. Right-click PowerShell and 'Run as administrator', then re-run this script."
}

Register-ScheduledTask -TaskName $taskName -InputObject $task -Force -ErrorAction Stop
Write-Host "Scheduled task '$taskName' installed successfully. Use 'schtasks /query /tn $taskName' to inspect."

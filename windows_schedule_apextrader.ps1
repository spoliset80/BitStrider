# Windows Scheduled Task Install for ApexTrader
# Run in PowerShell as Administrator.

$taskName = 'ApexTraderAutoRun'
$taskDescription = 'Run ApexTrader trading bot watchdog daily at 8:00 AM ET on weekdays'
$BaseDir = $PSScriptRoot

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$BaseDir\scripts\run_autobot_task.ps1`""
# Run at 08:00 local time on weekdays (Mon-Fri). Ensure the machine local timezone is EST/EDT as desired.
$dailyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 08:00
# Also start on log on, so a power cycle / reboot during the trading day brings the bot back up
# immediately instead of waiting for tomorrow's 08:00 trigger (the daily trigger only fires once
# per day and doesn't know if that day's launch later died to a reboot).
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$trigger = @($dailyTrigger, $logonTrigger)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:UserName" -LogonType Interactive -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable -WakeToRun -DontStopIfGoingOnBatteries -Hidden

$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    throw "Not running as Administrator. Right-click PowerShell and 'Run as administrator', then re-run this script."
}

Register-ScheduledTask -TaskName $taskName -InputObject $task -Force -ErrorAction Stop
Write-Host "Scheduled task '$taskName' installed successfully. Use 'schtasks /query /tn $taskName' to inspect."
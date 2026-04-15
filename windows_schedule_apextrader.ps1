# Windows Scheduled Task Install for ApexTrader
# Run in PowerShell as Administrator.

$taskName = 'ApexTraderAutoRun'
$taskDescription = 'Run ApexTrader trading bot watchdog daily at 7:00 AM ET on weekdays'

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -WindowStyle Hidden -Command "cd C:\Users\user\Desktop\wsapex\apextrader; .\\apextrader\\Scripts\\python.exe .\\autobot.py | Tee-Object -FilePath .\\autobot_scheduler.log"'
# Run at 07:00 local time on weekdays (Mon-Fri). Ensure the machine local timezone is EST/EDT as desired.
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 07:00

$principal = New-ScheduledTaskPrincipal -UserId "$env:UserName" -LogonType Interactive -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable -WakeToRun -DontStopIfGoingOnBatteries -Hidden

$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings

Register-ScheduledTask -TaskName $taskName -InputObject $task -Force
Write-Host "Scheduled task '$taskName' installed successfully. Use 'schtasks /query /tn $taskName' to inspect."
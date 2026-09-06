# Run this in an elevated (Administrator) PowerShell - right-click PowerShell,
# "Run as administrator", then paste/run this file.
#
# Adds a trigger that checks every 30 minutes, 24/7, and (re)launches
# ApexTraderAutoRun if nothing is currently running - a safety net against a
# reboot or an unexplained mid-day death (confirmed 2026-08-05: the watchdog
# can silently die with no restart until the next scheduled trigger). Safe to
# add: MultipleInstancesPolicy=IgnoreNew means this is a total no-op whenever
# the watchdog is already alive - it only does anything the moment it finds
# nothing running.
#
# Keeps the existing BootTrigger + weekday-8am CalendarTrigger and S4U
# principal exactly as they are; only adds the new recurring trigger.

$ErrorActionPreference = 'Stop'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    throw "Not elevated. Right-click PowerShell -> Run as administrator, then re-run this script."
}

$sid = (Get-ScheduledTask -TaskName 'ApexTraderAutoRun').Principal.UserId
if ($sid -notmatch '^S-1-5-21-') {
    $sid = (New-Object System.Security.Principal.NTAccount($sid)).Translate([System.Security.Principal.SecurityIdentifier]).Value
}

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>\ApexTraderAutoRun</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>$sid</UserId>
      <LogonType>S4U</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <Hidden>true</Hidden>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <WakeToRun>true</WakeToRun>
    <RestartOnFailure>
      <Interval>PT2M</Interval>
      <Count>5</Count>
    </RestartOnFailure>
    <IdleSettings>
      <Duration>PT10M</Duration>
      <WaitTimeout>PT1H</WaitTimeout>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
    </BootTrigger>
    <CalendarTrigger>
      <StartBoundary>2026-08-03T08:00:00-05:00</StartBoundary>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek>
          <Monday />
          <Tuesday />
          <Wednesday />
          <Thursday />
          <Friday />
        </DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>
    <CalendarTrigger>
      <StartBoundary>2026-08-05T00:00:00-05:00</StartBoundary>
      <Repetition>
        <Interval>PT30M</Interval>
        <Duration>P1D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\Users\BG\OneDrive\Returns-LSTM\StockPricePrediction\BitStrider-main\scripts\run_autobot_task.ps1"</Arguments>
    </Exec>
  </Actions>
</Task>
"@

Register-ScheduledTask -TaskName 'ApexTraderAutoRun' -Xml $xml -Force | Out-Null
Write-Host "Task updated - now checks every 30 minutes in addition to boot + weekday 8am." -ForegroundColor Green
(Get-ScheduledTask -TaskName 'ApexTraderAutoRun').Triggers | Select-Object CimClass, StartBoundary

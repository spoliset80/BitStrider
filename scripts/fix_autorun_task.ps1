# Run this in an elevated (Administrator) PowerShell — right-click PowerShell,
# "Run as administrator", then paste/run this file.
#
# What it does to \ApexTraderAutoRun:
#   - Logon type -> S4U ("run whether user is logged on or not"): the bot no
#     longer depends on an interactive desktop session, so screen lock / sleep-
#     wake session churn can't collide with it or kill it anymore.
#   - Triggers -> "At startup" (fires once per boot, background, no login
#     needed) + weekday 8:00 AM as a same-day safety net if the machine was
#     already on and the process died for some other reason.
#   - Removed the old "At logon" trigger — that's what raced against the
#     8am-triggered run on 2026-08-04 and caused the collision.
#   - Added RestartOnFailure (5 attempts, 2 min apart) so a bad exit gets
#     retried same-day instead of waiting for the next trigger.
#   - Added a 3rd trigger: daily @ 00:00, repeating every 30 min for 24h.
#     RestartOnFailure only covers 5 attempts / ~10 min before giving up --
#     without this, a crash that outlasts that window sits dead until the
#     next boot or the following day's 8am trigger. Same failure shape that
#     bit the TI-capture task once already (crashed 2026-08-03, stayed dead
#     30+ hours, nothing re-armed it). This was live on the task since
#     2026-08-05 but missing from this script's XML -- folded in
#     2026-08-12 so the two stop drifting apart. IgnoreNew (below) makes it
#     a no-op whenever the bot is already running.
#
# ponytail: single XML re-register, no new dependencies — Task Scheduler's
# native retry/boot-trigger/repeating-trigger covers this, no custom
# supervisor script needed.

$ErrorActionPreference = 'Stop'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    throw "Not elevated. Right-click PowerShell -> Run as administrator, then re-run this script."
}

$sid = (Get-ScheduledTask -TaskName 'ApexTraderAutoRun').Principal.UserId
if ($sid -notmatch '^S-1-5-21-') {
    # UserId came back as a plain account name (e.g. "BG") rather than a SID — resolve it.
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
Write-Host "Task updated." -ForegroundColor Green
Get-ScheduledTask -TaskName 'ApexTraderAutoRun' | Select-Object TaskName, State
(Get-ScheduledTask -TaskName 'ApexTraderAutoRun').Triggers | Select-Object CimClass, StartBoundary
(Get-ScheduledTask -TaskName 'ApexTraderAutoRun').Principal | Select-Object UserId, LogonType

Write-Host "`nStarting the bot now (it's been down since last night 21:34)..." -ForegroundColor Yellow
Start-ScheduledTask -TaskName 'ApexTraderAutoRun'
Start-Sleep -Seconds 5
Get-ScheduledTask -TaskName 'ApexTraderAutoRun' | Select-Object TaskName, State

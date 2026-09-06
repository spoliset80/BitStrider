# Windows Scheduled Task Install for the Daily Improvement loop (2026-09-08,
# fixed 2026-09-08 later: XML registration + NO elevation requirement).
#
# Registers 'ApexTraderDailyImprovement': a repeated 15-min cadence through
# the LOCAL midday (11:00-14:30) on weekdays. daily_automation.py itself is
# the authority on the ET window (12:05-14:00 ET, pytz) and on the market-day
# check, so DST drift / clock skew cannot push the work into an active
# trading window. MultipleInstancesPolicy=IgnoreNew + the machine-local lock
# file prevent overlapping runs.
#
# WHY XML (fix_autorun_task.ps1 pattern): New-ScheduledTaskTrigger silently
# rounds -RepetitionDuration to whole hours (3.5h became PT4H when tested
# live), and its Repetition object assignment is fragile across builds.
# Hand-built XML registers exactly what we specify, and registers fine
# WITHOUT elevation for a current-user, LeastPrivilege, InteractiveToken
# task (verified: non-admin Register/Unregister OK on this machine).
#
# SAFETY: the task runs WITHOUT AUTOMATION_ALLOW_DEPLOY set, so runs are
# observe/plan-only until you explicitly opt in to the test-gated deploy
# (set AUTOMATION_ALLOW_DEPLOY=1 machine-wide).

$ErrorActionPreference = 'Stop'

$taskName = 'ApexTraderDailyImprovement'
$BaseDir  = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $BaseDir 'scripts\run_daily_automation_task.ps1'
if (-not (Test-Path $launcher)) { throw "launcher not found: $launcher" }

# Resolve the SID (plain account names like "BG" must be translated first --
# same handling as fix_autorun_task.ps1).
$sid = (New-Object System.Security.Principal.NTAccount("$env:USERDOMAIN\$env:UserName")).Translate(
    [System.Security.Principal.SecurityIdentifier]).Value

# Local-timezone offset for StartBoundary (e.g. "-05:00").
# Start 11:50 so the 15-min cadence lands exactly on the 12:05 ET window
# open: 11:50, 12:05, 12:20, ... (fires before 12:05 no-op; duration PT2H25M
# covers through the 14:00 ET window close with margin).
$offset = (Get-Date -Format 'zzz')
$start  = "{0:yyyy-MM-dd}T11:50:00{1}" -f (Get-Date), $offset

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>\$taskName</URI>
    <Description>ApexTrader daily observation + evidence-gated improvement loop (12:05-14:00 ET enforced inside the script; runs are observe-only until AUTOMATION_ALLOW_DEPLOY=1).</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>$sid</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <Hidden>true</Hidden>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$start</StartBoundary>
      <Repetition>
        <Interval>PT15M</Interval>
        <Duration>PT2H25M</Duration>
        <StopAtDurationEnd>true</StopAtDurationEnd>
      </Repetition>
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
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$launcher"</Arguments>
    </Exec>
  </Actions>
</Task>
"@

Register-ScheduledTask -TaskName $taskName -Xml $xml -Force | Out-Null

# ---- self-verification -------------------------------------------------
$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host "Scheduled task '$taskName' registered (user-level, no elevation needed)." -ForegroundColor Green
Write-Host "State: $($task.State) | LastRun: $($info.LastRunTime) | NextRun: $($info.NextRunTime)"
$t = $task.Triggers[0]
Write-Host ("Trigger: weekly 11:50 local, every {0} for {1} (StopAtDurationEnd={2})" -f $t.Repetition.Interval, $t.Repetition.Duration, $t.Repetition.StopAtDurationEnd)
Write-Host "Manual one-shot run now:  Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Manual foreground run:    powershell -File `"$launcher`" -Force -Offline"
Write-Host "Deploy opt-in (test-gated): set AUTOMATION_ALLOW_DEPLOY=1 machine-wide."
if ($t.Repetition.Interval -ne 'PT15M' -or $t.Repetition.Duration -ne 'PT2H25M') {
    throw "Registered repetition is $($t.Repetition.Interval)/$($t.Repetition.Duration) -- expected PT15M/PT2H25M"
}


# ApexTrader Daily Improvement launcher (2026-09-08)
# Called by the ApexTraderDailyImprovement scheduled task. Mirrors
# run_autobot_task.ps1: prefer the machine-local venv python, never hardcode
# user-specific paths beyond LOCALAPPDATA, propagate the real exit code so
# Task Scheduler's "Last Result" is truthful.
#
# The ET time window (12:05-14:00) and the market-day check live INSIDE
# daily_automation.py (pytz, authoritative); the task fires on a repeated
# 15-min cadence through the local midday and the script no-ops when the ET
# window is not active. This keeps the schedule correct even if the machine
# clock drifts from ET (the known ET-1h log-clock quirk).
$ErrorActionPreference = 'Continue'

$BaseDir = Split-Path -Parent $PSScriptRoot
$Script  = "$BaseDir\scripts\daily_automation.py"
$LogDir  = Join-Path $env:LOCALAPPDATA "ApexTrader\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
$Log     = Join-Path $LogDir "daily_automation_scheduler.log"

$Python = Join-Path $env:LOCALAPPDATA "ApexTrader\venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    # This folder syncs via OneDrive across machines -- resolve generically.
    $Python = (Get-Command py -ErrorAction SilentlyContinue).Source
    if (-not $Python) { $Python = (Get-Command python -ErrorAction SilentlyContinue).Source }
}
if (-not $Python) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [TASK] ERROR: no Python interpreter found" | Tee-Object -FilePath $Log -Append
    exit 1
}

Set-Location $BaseDir

"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [TASK] DailyImprovement triggered" | Tee-Object -FilePath $Log -Append

# Translate PowerShell-style flags to argparse style (-Force -> --force,
# -DryRun -> --dry-run): the documented invocation is
# `run_daily_automation_task.ps1 -Force -Offline`, and argparse would
# otherwise exit 2 on the single-dash form (2026-09-05 scheduler-log bug:
# "unrecognized arguments: -Force" -> exit 2). Anything not starting with a
# single dash (paths, values) passes through untouched.
$pyArgs = foreach ($a in $args) {
    if ($a -match '^-[A-Za-z]') {
        '--' + (($a.Substring(1) -creplace '(?<!^)(?=[A-Z])', '-').ToLower())
    } else { $a }
}

& $Python $Script @pyArgs 2>&1 | Tee-Object -FilePath $Log -Append
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [TASK] python exited with code $LASTEXITCODE" | Tee-Object -FilePath $Log -Append
exit $LASTEXITCODE

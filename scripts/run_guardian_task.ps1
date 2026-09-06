# ApexTrader Loss Guardian Task Scheduler launcher (2026-09-02)
# Called by the ApexTraderGuardian scheduled task every 1 minute, Mon-Fri.
# Mirrors run_autobot_task.ps1: no hardcoded machine/user python path (this
# folder syncs via OneDrive), resolve via py/python, propagate the real exit
# code so Task Scheduler's "Last Result" is truthful.
$ErrorActionPreference = 'Continue'

$BaseDir = Split-Path -Parent $PSScriptRoot
$Script  = "$BaseDir\scripts\guardian.py"
# 2026-09-02: scheduler tee moved OUT of the OneDrive repo (a blocking
# append there wedged the guardian mid-run; same class as the bot freeze).
$LogDir  = Join-Path $env:LOCALAPPDATA "ApexTrader\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
$Log     = Join-Path $LogDir "guardian_scheduler.log"

$Python = (Get-Command py -ErrorAction SilentlyContinue).Source
if (-not $Python) { $Python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $Python) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [GUARDIAN] ERROR: no Python interpreter found on PATH (py/python)" | Tee-Object -FilePath $Log -Append
    exit 1
}

Set-Location $BaseDir

"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [GUARDIAN] run triggered" | Tee-Object -FilePath $Log -Append

& $Python $Script --once 2>&1 | Tee-Object -FilePath $Log -Append
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [GUARDIAN] python exited with code $LASTEXITCODE" | Tee-Object -FilePath $Log -Append
exit $LASTEXITCODE

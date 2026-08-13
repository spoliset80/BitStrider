# ApexTrader TI Capture Task Scheduler launcher
# Called by Windows Task Scheduler - avoids quoting issues with inline -Command
$ErrorActionPreference = 'Continue'

$BaseDir = Split-Path -Parent $PSScriptRoot
$Script  = "$BaseDir\engine\ti\capture_tradeideas.py"
$Log     = "$BaseDir\ti_capture_scheduler.log"

# This folder syncs across multiple machines via OneDrive, so don't hardcode
# a machine/user-specific python.exe path here (it breaks on every other
# machine, or after a profile rename). Resolve via the launcher / PATH instead.
$Python = (Get-Command py -ErrorAction SilentlyContinue).Source
if (-not $Python) { $Python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $Python) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [TASK] ERROR: no Python interpreter found on PATH (py/python)" | Tee-Object -FilePath $Log -Append
    exit 1
}

Set-Location $BaseDir

"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [TASK] ApexTraderTICapture triggered by Task Scheduler" | Tee-Object -FilePath $Log -Append

# Single-shot (no --loop): Task Scheduler's own 15-min repetition trigger owns the
# cadence now, not an internal Python loop that can silently die and stay dead
# until the next log on (see 2026-08-03 Edge-profile-lock crash).
& $Python $Script --update-config --chrome-profile "Default" 2>&1 | Tee-Object -FilePath $Log -Append

# Touches .env's timestamp so the watchdog's own change-detector restarts
# main.py -- same mechanism used manually all day 2026-08-24. No elevation
# needed (unlike restart_watchdog.ps1's Start-ScheduledTask path); this
# only nudges a file the already-running watchdog is already polling.
$EnvFile = "c:\Users\BG\OneDrive\Returns-LSTM\StockPricePrediction\BitStrider-main\.env"
(Get-Item $EnvFile).LastWriteTime = Get-Date
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') touched .env to trigger restart" | Out-File -Append "c:\Users\BG\OneDrive\Returns-LSTM\StockPricePrediction\BitStrider-main\touch_env_restart.log"

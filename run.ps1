param(
    [ValidateSet("paper","live")][string]$Mode = "paper",
    [int]$Poll       = 60,
    [string]$MarketOpen  = "09:30",
    [string]$MarketClose = "16:00",
    [string]$TimeZone    = "Eastern Standard Time"
)

$env:DISCORD_OPTIONS_MODE = $Mode

$tz     = [System.TimeZoneInfo]::FindSystemTimeZoneById($TimeZone)
$open   = [System.TimeSpan]::Parse($MarketOpen)
$close  = [System.TimeSpan]::Parse($MarketClose)

Write-Host "Discord Alert Trader | mode=$Mode | poll=${Poll}s | market $MarketOpen-$MarketClose EST"

while ($true) {
    $nowEst  = [System.TimeZoneInfo]::ConvertTimeFromUtc([System.DateTime]::UtcNow, $tz)
    $nowTime = $nowEst.TimeOfDay
    $today   = $nowEst.DayOfWeek

    # Skip weekends entirely
    if ($today -eq "Saturday" -or $today -eq "Sunday") {
        $waitSec = 3600
        Write-Host "[$($nowEst.ToString('HH:mm')) EST] Weekend -- sleeping 60m"
        Start-Sleep -Seconds $waitSec
        continue
    }

    if ($nowTime -ge $open -and $nowTime -lt $close) {
        # Market hours: run one poll cycle and sleep $Poll seconds
        apextrader\Scripts\python.exe -m scripts.discord_api_reader --poll $Poll --history 10
        Start-Sleep -Seconds $Poll
    } else {
        # Outside market hours: sleep until next open (or 5 min if already past close)
        if ($nowTime -lt $open) {
            $secsUntilOpen = [int](($open - $nowTime).TotalSeconds)
        } else {
            # Past close -- sleep until next day open (rough: remaining day + open offset)
            $midnight = [System.TimeSpan]::FromHours(24)
            $secsUntilOpen = [int](($midnight - $nowTime + $open).TotalSeconds)
        }
        $waitMin = [int]($secsUntilOpen / 60)
        Write-Host "[$($nowEst.ToString('HH:mm')) EST] Outside market hours -- sleeping ${waitMin}m until $MarketOpen open"
        # Sleep in chunks so Ctrl+C still works
        $slept = 0
        while ($slept -lt $secsUntilOpen) {
            Start-Sleep -Seconds ([Math]::Min(300, $secsUntilOpen - $slept))
            $slept += 300
        }
    }
}
# Auto-generated wrapper (Register-AutoTune.ps1). Keeps the auto-improve loop alive and logged.
# The loop itself polls; this layer restarts it if it ever throws, so a transient network or ssh
# failure cannot end the session permanently. The Scheduled Task restarts THIS if the process dies.
$repo   = 'C:\Users\toddm\OneDrive\Desktop\runtime'
$script = Join-Path $repo 'desktop\Auto-Tune-Loop.ps1'
$logdir = Join-Path $repo 'runs\_autotune_logs'
if (-not (Test-Path $logdir)) { New-Item -ItemType Directory -Force -Path $logdir | Out-Null }
# Keep only the last 14 transcripts so this never fills the disk.
Get-ChildItem $logdir -Filter 'autotune_*.log' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -Skip 14 |
    Remove-Item -Force -ErrorAction SilentlyContinue
$log = Join-Path $logdir ("autotune_{0}.log" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
"[$(Get-Date -Format o)] auto-improve service starting" | Out-File -FilePath $log -Encoding utf8
while ($true) {
    try {
        "[$(Get-Date -Format o)] launching Auto-Tune-Loop.ps1" | Out-File -FilePath $log -Append -Encoding utf8
        & $script *>&1 | Out-File -FilePath $log -Append -Encoding utf8
        "[$(Get-Date -Format o)] loop EXITED cleanly (unexpected for a poll loop); restarting in 30s" | Out-File -FilePath $log -Append -Encoding utf8
    } catch {
        "[$(Get-Date -Format o)] loop THREW: $($_.Exception.Message); restarting in 30s" | Out-File -FilePath $log -Append -Encoding utf8
    }
    Start-Sleep -Seconds 30
}

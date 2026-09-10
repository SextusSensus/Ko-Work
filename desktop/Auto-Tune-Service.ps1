# Auto-generated wrapper (Register-AutoTune.ps1). Keeps the auto-improve loop alive and logged.
# The loop itself polls; this layer restarts it if it ever throws, so a transient network or ssh
# failure cannot end the session permanently. The Scheduled Task restarts THIS if the process dies.
$repo   = 'C:\Users\toddm\OneDrive\Desktop\runtime'
# SINGLE INSTANCE. The Startup-folder launcher cannot know whether a service is already up, so a
# second logon (or a manual start on top of the logon one) would run two pollers that process the
# same runs and race on the ledger. A named mutex makes every extra copy exit at once. A previous
# owner that died without releasing leaves the mutex ABANDONED; .NET then throws
# AbandonedMutexException while still granting ownership -- that counts as acquired.
try { $mutex = New-Object System.Threading.Mutex($false, 'Global\K1-AutoTune-Service') }
catch { $mutex = New-Object System.Threading.Mutex($false, 'Local\K1-AutoTune-Service') }
try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
if (-not $owned) { exit 0 }
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

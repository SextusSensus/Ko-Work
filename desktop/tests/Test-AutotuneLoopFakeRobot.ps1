# Test-AutotuneLoopFakeRobot.ps1 -- the REAL Auto-Tune-Loop.ps1 against a FAKE robot (ssh.exe / scp.exe shadowed by
# PowerShell functions, remote paths mapped into a temp dir). Covers what cannot run with the robot off:
#   L1 a clean run: text pulls verified, hints pushed + run LEDGERED before the .rrd pull starts
#   L2 an OLDER run afterwards: <run>.yaml pushed, latest.yaml left on the newer run
#   L3 a NEWEST run whose k1_follow.err arrives short of its manifest size: not analysed, NOT ledgered
# The service must be stopped first (the loop is single-instance); the caller restarts it.
$ErrorActionPreference = 'Continue'
# The loop is single-instance: while the autotune service runs, this test cannot start the loop.
foreach ($n in 'Global\K1-AutoTune-Loop', 'Local\K1-AutoTune-Loop') {
  $held = $null
  if ([System.Threading.Mutex]::TryOpenExisting($n, [ref]$held)) {
    $held.Dispose()
    Write-Host 'SKIP: the autotune service holds the loop mutex -- stop it to run this test' -ForegroundColor Yellow
    exit 0
  }
}
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$loop = Join-Path $repo 'desktop\Auto-Tune-Loop.ps1'
$T = Join-Path $env:TEMP ('k1_loop_test_' + (Get-Date -Format 'HHmmss'))
$R = Join-Path $T 'robot'                     # the fake robot's /fake/...
New-Item -ItemType Directory -Force -Path (Join-Path $R 'runs'), (Join-Path $R 'hints'), (Join-Path $T 'local_runs'), (Join-Path $T 'autotune') | Out-Null
$fails = 0
function Check([string]$name, [bool]$cond, [string]$detail) {
  if ($cond) { Write-Host ("PASS  {0}  {1}" -f $name, $detail) -ForegroundColor Green }
  else { Write-Host ("FAIL  {0}  {1}" -f $name, $detail) -ForegroundColor Red; $script:fails++ }
}
function Map([string]$remote) { Join-Path $R (($remote -replace '^x@fake:', '') -replace '^/fake/', '' -replace '/', '\') }

# ---- the fake robot's shell and copy
function ssh.exe {
  $cmd = [string]$args[-1]
  $global:LASTEXITCODE = 0
  if ($cmd -match "^cd '([^']+)' 2>/dev/null && for d in 20") { Get-ChildItem (Map $Matches[1]) -Directory -ErrorAction SilentlyContinue | Where-Object { Test-Path (Join-Path $_.FullName 'manifest.json') } | Sort-Object Name | ForEach-Object { $_.Name }; return }
  if ($cmd -match "^ls -1 '([^']+)'") { Get-ChildItem (Map $Matches[1]) -Directory -ErrorAction SilentlyContinue | Sort-Object Name | ForEach-Object { $_.Name }; return }
  if ($cmd -match "^test -f '([^']+)'") { if (Test-Path (Map $Matches[1])) { 'yes' }; return }
  if ($cmd -match '^if pgrep') { 'idle'; return }
  if ($cmd -match "^mkdir -p '([^']+)'") { New-Item -ItemType Directory -Force -Path (Map $Matches[1]) | Out-Null; return }
}
function scp.exe {
  $src = [string]$args[-2]; $dst = [string]$args[-1]
  $global:LASTEXITCODE = 1
  if ($src -like 'x@fake:*') { $s = Map $src; if (Test-Path $s) { Copy-Item $s $dst -Force; $global:LASTEXITCODE = 0 } }
  elseif ($dst -like 'x@fake:*') { $d = Map $dst; New-Item -ItemType Directory -Force -Path (Split-Path $d) | Out-Null; Copy-Item $src $d -Force; $global:LASTEXITCODE = 0 }
}

# ---- a realistic bundle, from a real local run
$real = Get-ChildItem (Join-Path $repo 'runs') -Directory | Where-Object { (Test-Path "$($_.FullName)\k1_follow.err") -and (Test-Path "$($_.FullName)\config\defaults.yaml") } | Sort-Object Name -Descending | Select-Object -First 1
function New-FakeRun([string]$rid, [int]$errShortBy = 0, [switch]$NoErr) {
  $d = Join-Path $R "runs\$rid"; New-Item -ItemType Directory -Force -Path (Join-Path $d 'config') | Out-Null
  Copy-Item "$($real.FullName)\k1_follow.err" "$d\k1_follow.err"
  Copy-Item "$($real.FullName)\config\defaults.yaml" "$d\config\defaults.yaml"
  $eb = (Get-Item "$d\k1_follow.err").Length
  $yb = (Get-Item "$d\config\defaults.yaml").Length
  $files = @(@{ name = 'k1_follow.err'; bytes = ($eb + $errShortBy) }, @{ name = 'config/defaults.yaml'; bytes = $yb }, @{ name = 'k1_follow_1.rrd'; bytes = 1000 })
  if ($NoErr) { $files = @($files | Where-Object { $_.name -ne 'k1_follow.err' }) }
  @{ run_id = $rid; files = $files } | ConvertTo-Json -Depth 4 | Out-File -Encoding ascii "$d\manifest.json"
}
# Hashtable splatting: an ARRAY splatted into a PowerShell script binds positionally ('-User' is a value).
$common = @{ Once = $true; User = 'x'; Ip = 'fake'; PollSec = 1; RemoteRuns = '/fake/runs'; RemoteHints = '/fake/hints';
             LocalRuns = (Join-Path $T 'local_runs'); AutotuneDir = (Join-Path $T 'autotune'); RemoteAutotune = '/fake/autotune' }
$ledger = Join-Path $T 'local_runs\.auto_tune_processed.txt'

# L1 clean newest run
New-FakeRun '20990102T000002Z_bbbbbbb'
$o1 = & $loop @common *>&1 | Out-String
$led = @(Get-Content $ledger -ErrorAction SilentlyContinue)
$iOk = $o1.IndexOf('OK: report'); $iRrd = $o1.IndexOf('rrd: pulling')
Check 'L1 clean run -> hints pushed, ledgered BEFORE the .rrd pull' (($led -contains '20990102T000002Z_bbbbbbb') -and (Test-Path "$R\hints\latest.yaml") -and (Test-Path "$R\hints\20990102T000002Z_bbbbbbb.yaml") -and $iOk -ge 0 -and ($iRrd -lt 0 -or $iOk -lt $iRrd)) ("ledger={0} okAt={1} rrdAt={2}" -f ($led -join ','), $iOk, $iRrd)
$latest1 = Get-Content "$R\hints\latest.yaml" -Raw

# L2 an OLDER run -> its own yaml only; latest.yaml stays on the newer run
New-FakeRun '20990101T000001Z_aaaaaaa'
'# marker: newer run hints' | Out-File -Encoding ascii -Append "$R\hints\latest.yaml"
$latestBefore = Get-Content "$R\hints\latest.yaml" -Raw
$o2 = & $loop @common *>&1 | Out-String
$led = @(Get-Content $ledger -ErrorAction SilentlyContinue)
Check 'L2 older run -> own yaml, latest.yaml untouched' (($led -contains '20990101T000001Z_aaaaaaa') -and (Test-Path "$R\hints\20990101T000001Z_aaaaaaa.yaml") -and ((Get-Content "$R\hints\latest.yaml" -Raw) -eq $latestBefore) -and $o2 -match 'latest.yaml stays on the newest') ("ledger={0}" -f ($led -join ','))

# L3 newest run whose log arrives SHORT of its manifest size -> not analysed, not ledgered
New-FakeRun '20990103T000003Z_ccccccc' 50
$o3 = & $loop @common *>&1 | Out-String
$led = @(Get-Content $ledger -ErrorAction SilentlyContinue)
Check 'L3 short k1_follow.err -> WARN, not ledgered, no hints' ((-not ($led -contains '20990103T000003Z_ccccccc')) -and $o3 -match 'incomplete pull' -and -not (Test-Path "$R\hints\20990103T000003Z_ccccccc.yaml")) ("ledger={0}" -f ($led -join ','))

# L4 a NEWER run ledgered by the SKIP path (no k1_follow.err) must not keep latest.yaml off an older run's hints
New-FakeRun '20990106T000006Z_fffffff' -NoErr
$o4a = & $loop @common *>&1 | Out-String                       # SKIP-ledgers fffffff
New-FakeRun '20990105T000005Z_eeeeeee'
$o4b = & $loop @common *>&1 | Out-String                       # processes eeeeeee
Check 'L4 SKIP-ledgered newer run does not block latest.yaml' (($o4a -match 'SKIP') -and ($o4b -match 'OK: report') -and ($o4b -notmatch 'latest.yaml stays on the newest')) ''

# L5 a run that THROWS after taking the loop mutex (bad -Analyser) must release it on the way out
$bad = $common.Clone(); $bad.Analyser = 'C:\nope\missing_analyser.py'
try { & $loop @bad *>&1 | Out-Null } catch { }
$state = & powershell.exe -NoProfile -Command "try { `$m = [System.Threading.Mutex]::OpenExisting('Global\K1-AutoTune-Loop'); if (`$m.WaitOne(0)) { `$m.ReleaseMutex(); 'free' } else { 'held' } } catch [System.Threading.WaitHandleCannotBeOpenedException] { 'gone' } catch [System.Threading.AbandonedMutexException] { 'abandoned' }"
Check 'L5 loop mutex released after a throw' ($state -in @('free', 'gone')) ("mutex={0}" -f $state)

"---- L1 output ----"; $o1
"---- L3 output ----"; $o3
Write-Host ("`n{0} failure(s); scratch in {1}" -f $fails, $T)
exit $fails

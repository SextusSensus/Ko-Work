# Test-AutotuneStage.ps1 -- functional test of Autotune-Stage.ps1's exit contract, run exactly as the loop's -File path would.
# No labeller is ever launched: every case stops before it (validation present, truncated, capped).
$ErrorActionPreference = 'Continue'
$stage = Join-Path (Split-Path $PSScriptRoot -Parent) 'Autotune-Stage.ps1'
$T = Join-Path $env:TEMP ('k1_stage_test_' + (Get-Date -Format 'HHmmss'))
New-Item -ItemType Directory -Force -Path $T | Out-Null
$dead = '192.0.2.1'          # TEST-NET-1: never routable, so ssh fails after ConnectTimeout
$fails = 0

function New-Case([string]$rid, [string]$status) {
  $run = Join-Path $T "runs\$rid"; $aut = Join-Path $T 'auto'
  New-Item -ItemType Directory -Force -Path $run, (Join-Path $aut $rid) | Out-Null
  'tune' | Out-File -Encoding ascii (Join-Path $run 'tune_report.json')
  if ($status) {
    @{ gate_version = 3; status = $status; checks = @{ floor = @{ status = 'PASS' } } } | ConvertTo-Json -Depth 4 |
      Out-File -Encoding utf8 (Join-Path $aut "$rid\label_validation.json")
  }
  return @{ run = $run; aut = $aut; adir = (Join-Path $aut $rid) }
}
function Invoke-Stage([string]$name, [string[]]$extra) {
  $log = Join-Path $T "$name.log"
  $t0 = Get-Date
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $stage @extra *> $log
  $rc = $LASTEXITCODE
  return @{ rc = $rc; log = (Get-Content $log -Raw); s = [int]((Get-Date) - $t0).TotalSeconds }
}
function Check([string]$name, [bool]$cond, [string]$detail) {
  if ($cond) { Write-Host ("PASS  {0}  {1}" -f $name, $detail) -ForegroundColor Green }
  else { Write-Host ("FAIL  {0}  {1}" -f $name, $detail) -ForegroundColor Red; $script:fails++ }
}

# T1 INCONCLUSIVE -> exit 1, nothing sent, nothing held
$c = New-Case '20990101T000001Z_t1' 'INCONCLUSIVE'
$r = Invoke-Stage 't1' @('-RunId', '20990101T000001Z_t1', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Ip', $dead)
Check 'T1 INCONCLUSIVE -> 1' ($r.rc -eq 1 -and -not (Test-Path "$($c.adir)\.sent_to_robot") -and $r.log -match 'INCONCLUSIVE') ("rc={0} {1}s" -f $r.rc, $r.s)

# T2 PASS -NoSend -> exit 0 + durable .no_send ; T2b same run without -NoSend -> held, exit 0, no ssh
$c = New-Case '20990101T000002Z_t2' 'PASS'
$r = Invoke-Stage 't2' @('-RunId', '20990101T000002Z_t2', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Ip', $dead, '-NoSend')
Check 'T2 PASS -NoSend -> 0 + .no_send' ($r.rc -eq 0 -and (Test-Path "$($c.adir)\.no_send")) ("rc={0}" -f $r.rc)
$r = Invoke-Stage 't2b' @('-RunId', '20990101T000002Z_t2', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Ip', $dead)
Check 'T2b held bundle -> 0, not sent' ($r.rc -eq 0 -and $r.log -match 'held' -and -not (Test-Path "$($c.adir)\.sent_to_robot") -and $r.s -lt 7) ("rc={0} {1}s" -f $r.rc, $r.s)

# T3 PASS, robot unreachable -> exit 2, no marker
$c = New-Case '20990101T000003Z_t3' 'PASS'
$r = Invoke-Stage 't3' @('-RunId', '20990101T000003Z_t3', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Ip', $dead)
Check 'T3 unreachable -> 2, no marker' ($r.rc -eq 2 -and $r.log -match 'unreachable' -and -not (Test-Path "$($c.adir)\.sent_to_robot")) ("rc={0} {1}s" -f $r.rc, $r.s)

# T4 no validation + truncated recording -> exit 2, labeller never launched, no attempt consumed
$c = New-Case '20990101T000004Z_t4' $null
'{"files":[{"name":"x.rrd","bytes":100}]}' | Out-File -Encoding ascii (Join-Path $c.run 'manifest.json')
[IO.File]::WriteAllBytes((Join-Path $c.run 'x.rrd'), (New-Object byte[] 50))
$r = Invoke-Stage 't4' @('-RunId', '20990101T000004Z_t4', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Ip', $dead)
Check 'T4 truncated rrd -> 2, no attempt used' ($r.rc -eq 2 -and $r.log -match 'truncated' -and -not (Test-Path "$($c.adir)\.label_attempts")) ("rc={0}" -f $r.rc)

# T5 correct size but 3 attempts already used -> exit 2 before launching (or CUDA-deferred -> 2)
$c = New-Case '20990101T000005Z_t5' $null
'{"files":[{"name":"x.rrd","bytes":100}]}' | Out-File -Encoding ascii (Join-Path $c.run 'manifest.json')
[IO.File]::WriteAllBytes((Join-Path $c.run 'x.rrd'), (New-Object byte[] 100))
'3' | Out-File -Encoding ascii (Join-Path $c.adir '.label_attempts')
$r = Invoke-Stage 't5' @('-RunId', '20990101T000005Z_t5', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Ip', $dead)
$why = if ($r.log -match 'already ran') { 'attempt cap' } elseif ($r.log -match 'CUDA unavailable') { 'CUDA deferred' } else { 'other' }
Check 'T5 attempts exhausted -> 2, not launched' ($r.rc -eq 2 -and $why -ne 'other' -and -not (Test-Path "$($c.adir)\labeled.rrd") -and ((Get-Content "$($c.adir)\.label_attempts") -eq '3')) ("rc={0} via {1}" -f $r.rc, $why)

# T6 relative paths after a cd (C5): $PWD differs from the process directory in-process
$c = New-Case '20990101T000006Z_t6' 'PASS'
$cmd = "Set-Location '$T'; & '$stage' -RunId 20990101T000006Z_t6 -LocalRunDir 'runs\20990101T000006Z_t6' -AutotuneDir 'auto' -NoSend; exit `$LASTEXITCODE"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -Command $cmd *> (Join-Path $T 't6.log')
$rc = $LASTEXITCODE
Check 'T6 relative paths resolve against $PWD' ($rc -eq 0 -and (Test-Path "$($c.adir)\tune_report.json")) ("rc={0}" -f $rc)

function New-RrdCase([string]$rid) {
  $c = New-Case $rid $null
  '{"files":[{"name":"x.rrd","bytes":100}]}' | Out-File -Encoding ascii (Join-Path $c.run 'manifest.json')
  [IO.File]::WriteAllBytes((Join-Path $c.run 'x.rrd'), (New-Object byte[] 100))
  return $c
}
$fake = Join-Path $PSScriptRoot 'fake_label.py'

# T7 lock held by a LIVE powershell (this harness) -> exit 2, no attempt consumed
$c = New-RrdCase '20990101T000007Z_t7'
$me = Get-Process -Id $PID
('{0} {1} 90' -f $PID, $me.StartTime.ToUniversalTime().Ticks) | Out-File -Encoding ascii (Join-Path $c.adir '.labelling')
$r = Invoke-Stage 't7' @('-RunId', '20990101T000007Z_t7', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Label', $fake, '-NoSend')
Check 'T7 live lock -> 2, skipped, no attempt' ($r.rc -eq 2 -and $r.log -match 'being labelled' -and -not (Test-Path "$($c.adir)\.label_attempts")) ("rc={0}" -f $r.rc)

# T8 STALE lock (owner exited) -> ignored; the run proceeds and labels
$c = New-RrdCase '20990101T000008Z_t8'
$gone = Start-Process cmd.exe -ArgumentList '/c', 'exit' -PassThru -WindowStyle Hidden; $gone.WaitForExit()
('{0} 0 90' -f $gone.Id) | Out-File -Encoding ascii (Join-Path $c.adir '.labelling')
$env:FAKE_LABEL_SLEEP = '0'
$r = Invoke-Stage 't8' @('-RunId', '20990101T000008Z_t8', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Label', $fake, '-NoSend')
Check 'T8 stale lock ignored -> labelled, 0' ($r.rc -eq 0 -and (Test-Path "$($c.adir)\label_validation.json")) ("rc={0}" -f $r.rc)

# T9 real launch path: Start-Process + handle + redirect; lock released, 1 attempt, PASS, held
$c = New-RrdCase '20990101T000009Z_t9'
$r = Invoke-Stage 't9' @('-RunId', '20990101T000009Z_t9', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Label', $fake, '-NoSend')
$att = Get-Content "$($c.adir)\.label_attempts" -ErrorAction SilentlyContinue
Check 'T9 labeller launched -> 0, lock gone, attempt 1' ($r.rc -eq 0 -and -not (Test-Path "$($c.adir)\.labelling") -and $att -eq '1' -and (Test-Path "$($c.adir)\labeled.rrd") -and (Test-Path "$($c.adir)\SUMMARY.txt") -and (Test-Path "$($c.adir)\.no_send")) ("rc={0} attempts={1} {2}s" -f $r.rc, $att, $r.s)

# T10 wall-clock cap: labeller sleeps 150 s, cap 1 min -> killed, exit 2, lock gone, no python left
$c = New-RrdCase '20990101T000010Z_t10'
$env:FAKE_LABEL_SLEEP = '150'
$r = Invoke-Stage 't10' @('-RunId', '20990101T000010Z_t10', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Label', $fake, '-LabelTimeoutMin', '1', '-NoSend')
$env:FAKE_LABEL_SLEEP = '0'
Start-Sleep -Seconds 1
$orphans = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'fake_label\.py' -and $_.CommandLine -match '_t10' })
Check 'T10 timeout -> 2, killed, lock gone' ($r.rc -eq 2 -and $r.log -match 'exceeded' -and -not (Test-Path "$($c.adir)\.labelling") -and -not (Test-Path "$($c.adir)\label_validation.json") -and $orphans.Count -eq 0) ("rc={0} {1}s orphans={2}" -f $r.rc, $r.s, $orphans.Count)

# T11 -NoSend on a run that exits EARLY (truncated) -> the hold is recorded anyway (round 2)
$c = New-Case '20990101T000011Z_t11' $null
'{"files":[{"name":"x.rrd","bytes":100}]}' | Out-File -Encoding ascii (Join-Path $c.run 'manifest.json')
[IO.File]::WriteAllBytes((Join-Path $c.run 'x.rrd'), (New-Object byte[] 40))
$r = Invoke-Stage 't11' @('-RunId', '20990101T000011Z_t11', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-NoSend')
Check 'T11 -NoSend + early exit -> still held' ($r.rc -eq 2 -and (Test-Path "$($c.adir)\.no_send")) ("rc={0}" -f $r.rc)

# T12 a report from an OLDER gate (v2) is moved aside and the run re-labelled at v3
$c = New-RrdCase '20990101T000012Z_t12'
'{"gate_version": 2, "status": "PASS", "checks": {}}' | Out-File -Encoding ascii (Join-Path $c.adir 'label_validation.json')
$env:FAKE_LABEL_SLEEP = '0'
$r = Invoke-Stage 't12' @('-RunId', '20990101T000012Z_t12', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Label', $fake, '-NoSend')
$gv = try { (Get-Content "$($c.adir)\label_validation.json" -Raw | ConvertFrom-Json).gate_version } catch { $null }
Check 'T12 gate v2 -> moved aside, re-labelled at v3' ($r.rc -eq 0 -and (Test-Path "$($c.adir)\label_validation.gate2.json") -and $gv -eq 3) ("rc={0} gate={1}" -f $r.rc, $gv)

# T13 the .rrd is not listed in the manifest -> size unverifiable -> never labelled (fail-closed)
$c = New-Case '20990101T000013Z_t13' $null
'{"files":[{"name":"other.rrd","bytes":100}]}' | Out-File -Encoding ascii (Join-Path $c.run 'manifest.json')
[IO.File]::WriteAllBytes((Join-Path $c.run 'x.rrd'), (New-Object byte[] 100))
$r = Invoke-Stage 't13' @('-RunId', '20990101T000013Z_t13', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Label', $fake, '-NoSend')
Check 'T13 no manifest entry -> 2, not labelled' ($r.rc -eq 2 -and $r.log -match 'cannot be verified' -and -not (Test-Path "$($c.adir)\labeled.rrd")) ("rc={0}" -f $r.rc)

# T14 an unexpected error (malformed manifest) exits 2 (retryable), never 1 (the verdict code)
$c = New-Case '20990101T000014Z_t14' $null
'{ this is not json' | Out-File -Encoding ascii (Join-Path $c.run 'manifest.json')
[IO.File]::WriteAllBytes((Join-Path $c.run 'x.rrd'), (New-Object byte[] 100))
$r = Invoke-Stage 't14' @('-RunId', '20990101T000014Z_t14', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Label', $fake, '-NoSend')
Check 'T14 unexpected error -> 2, not 1' ($r.rc -eq 2 -and $r.log -match 'unexpected error') ("rc={0}" -f $r.rc)

# T15 PID REUSE: the lock names a live process whose start time does not match -> stale -> labels
$c = New-RrdCase '20990101T000015Z_t15'
('{0} 12345 90' -f $PID) | Out-File -Encoding ascii (Join-Path $c.adir '.labelling')
$r = Invoke-Stage 't15' @('-RunId', '20990101T000015Z_t15', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Label', $fake, '-NoSend')
Check 'T15 reused PID in lock -> stale, labelled' ($r.rc -eq 0 -and (Test-Path "$($c.adir)\label_validation.json") -and -not (Test-Path "$($c.adir)\.labelling")) ("rc={0}" -f $r.rc)

# T16 K1Finder holds the operator session -> the send is deferred (2) before any ssh, nothing sent or counted
$c = New-Case '20990101T000016Z_t16' 'PASS'
try { $op = New-Object System.Threading.Mutex($false, 'Global\K1-Operator-Session') } catch { $op = New-Object System.Threading.Mutex($false, 'Local\K1-Operator-Session') }
$r = Invoke-Stage 't16' @('-RunId', '20990101T000016Z_t16', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Ip', $dead)
$op.Dispose()
Check 'T16 operator session -> send deferred, not counted' ($r.rc -eq 2 -and $r.log -match 'operator session' -and $r.s -lt 7 -and -not (Test-Path "$($c.adir)\.sent_to_robot") -and -not (Test-Path "$($c.adir)\.send_attempts")) ("rc={0} {1}s" -f $r.rc, $r.s)

# T17 robot unreachable at send time -> deferred by the probe, NOT counted as a failed send
$c = New-Case '20990101T000017Z_t17' 'PASS'
$r = Invoke-Stage 't17' @('-RunId', '20990101T000017Z_t17', '-LocalRunDir', $c.run, '-AutotuneDir', $c.aut, '-Ip', $dead)
Check 'T17 unreachable at send -> deferred, not counted' ($r.rc -eq 2 -and $r.log -match 'deferred' -and -not (Test-Path "$($c.adir)\.send_attempts")) ("rc={0} {1}s" -f $r.rc, $r.s)

Write-Host ("`n{0} failure(s); logs in {1}" -f $fails, $T)
exit $fails

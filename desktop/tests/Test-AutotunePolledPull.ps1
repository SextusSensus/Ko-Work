# Test-AutotunePolledPull.ps1 -- the watched .rrd pull (ONE streamed robot monitor per pull) and the
# K1-Operator-Session contract, taken from the REAL Auto-Tune-Loop.ps1 via the AST. The scp and the robot
# monitor are fake .cmd files. P4 and P5 create the operator-session mutex for a few seconds, so a running
# autotune service defers briefly.
$ErrorActionPreference = 'Continue'
$loop = Join-Path (Split-Path $PSScriptRoot -Parent) 'Auto-Tune-Loop.ps1'
$T = Join-Path $env:TEMP ('k1_pull_test_' + (Get-Date -Format 'HHmmss'))
New-Item -ItemType Directory -Force -Path $T | Out-Null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($loop, [ref]$null, [ref]$null)
foreach ($fn in 'Invoke-Logged', 'Test-OperatorSession', 'Test-MonitorIdle', 'Receive-RrdPolled', 'Test-RobotIdle') {
  $def = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $fn }, $true) | Select-Object -First 1
  if (-not $def) { Write-Host "missing function $fn" -ForegroundColor Red; exit 1 }
  . ([scriptblock]::Create($def.Extent.Text))
}
$SSH_OPTS = @('-o', 'BatchMode=yes'); $target = 'x@fake'; $RrdPullKbps = 40000
$fails = 0
function Check([string]$name, [bool]$cond, [string]$detail) {
  if ($cond) { Write-Host ("PASS  {0}  {1}" -f $name, $detail) -ForegroundColor Green }
  else { Write-Host ("FAIL  {0}  {1}" -f $name, $detail) -ForegroundColor Red; $script:fails++ }
}

# fake scp: sleeps FAKE_SCP_SECS, then writes the target file and exits 0
$ScpExe = Join-Path $T 'fake_scp.cmd'
@'
@echo off
ping -n %FAKE_SCP_SECS% 127.0.0.1 >nul
for %%a in (%*) do set LAST=%%a
echo data> "%LAST%"
exit /b 0
'@ | Out-File -Encoding ascii $ScpExe
# fake robot monitor: 'idle' about once a second for FAKE_IDLE lines, then 'busy' lines if FAKE_THEN=busy
$SshExe = Join-Path $T 'fake_mon.cmd'
@'
@echo off
for /l %%i in (1,1,%FAKE_IDLE%) do (
  echo idle
  ping -n 2 127.0.0.1 >nul
)
if "%FAKE_THEN%"=="busy" for /l %%i in (1,1,60) do (
  echo busy
  ping -n 2 127.0.0.1 >nul
)
'@ | Out-File -Encoding ascii $SshExe

function Run([string]$name, [int]$idle, [string]$then, [int]$scpSecs) {
  $env:FAKE_IDLE = "$idle"; $env:FAKE_THEN = $then; $env:FAKE_SCP_SECS = "$scpSecs"
  $dst = Join-Path $T "$name.rrd"
  $t0 = Get-Date
  $rc = Receive-RrdPolled "/r/$name.rrd" $dst
  Start-Sleep -Milliseconds 700
  return @{ rc = $rc; s = ((Get-Date) - $t0).TotalSeconds; dst = $dst }
}
function Leftovers { @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'fake_(scp|mon)\.cmd' }).Count }

# P1 robot turns busy ~4 s into a 60 s pull -> aborted within ~2 s of the change, nothing left running
$r = Run 'p1' 4 'busy' 60
Check 'P1 follow starts mid-pull -> aborted (10)' ($r.rc -eq 10 -and $r.s -lt 12 -and (Leftovers) -eq 0 -and -not (Test-Path $r.dst)) ("rc={0} after {1:N1}s leftover={2}" -f $r.rc, $r.s, (Leftovers))

# P2 robot stays idle, the transfer takes ~2 s -> completes (0), file written, monitor stopped
$r = Run 'p2' 30 'none' 3
Check 'P2 idle robot -> completes (0), monitor stopped' ($r.rc -eq 0 -and (Test-Path $r.dst) -and (Leftovers) -eq 0 -and -not (Test-Path ($r.dst + '.mon.log'))) ("rc={0} in {1:N1}s" -f $r.rc, $r.s)

# P3 robot unreachable: the monitor dies at once -> the transfer never starts (10)
$r = Run 'p3' 0 'none' 60
Check 'P3 unreachable -> never started (10)' ($r.rc -eq 10 -and $r.s -lt 12 -and -not (Test-Path $r.dst) -and (Leftovers) -eq 0) ("rc={0} after {1:N1}s" -f $r.rc, $r.s)

# P4 K1Finder starts a launch ~3 s into the pull (operator-session mutex appears) -> aborted
$helper = Start-Process powershell.exe -PassThru -WindowStyle Hidden -ArgumentList '-NoProfile -Command "Start-Sleep 3; $m = New-Object System.Threading.Mutex($false, ''Global\K1-Operator-Session''); Start-Sleep 10"'
$r = Run 'p4' 40 'none' 60
Check 'P4 K1Finder launch mid-pull -> aborted (10)' ($r.rc -eq 10 -and $r.s -lt 12 -and (Leftovers) -eq 0) ("rc={0} after {1:N1}s" -f $r.rc, $r.s)
try { $helper.Kill() } catch { }
Start-Sleep -Milliseconds 500

# P5 the REAL Test-RobotIdle reads busy while the operator-session mutex exists, idle once it is gone
function Ssh-Text([string]$cmd) { 'idle' }
$before = Test-RobotIdle
try { $op = New-Object System.Threading.Mutex($false, 'Global\K1-Operator-Session') } catch { $op = New-Object System.Threading.Mutex($false, 'Local\K1-Operator-Session') }
$during = Test-RobotIdle
$op.Dispose()
$after = Test-RobotIdle
Check 'P5 operator session -> busy, then idle' ($before -and -not $during -and $after) ("before={0} during={1} after={2}" -f $before, $during, $after)

Write-Host ("`n{0} failure(s)" -f $fails)
exit $fails

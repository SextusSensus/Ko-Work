# Test-AutotunePolledPull.ps1 -- Receive-RrdPolled + the K1-Operator-Session contract, taken from the REAL
# Auto-Tune-Loop.ps1 via the AST. P4 creates the operator-session mutex for about a second, so a running
# autotune service defers one tick. Original notes:
# a fake scp that runs long, and a fake robot that turns busy mid-pull.
$ErrorActionPreference = 'Continue'
$loop = Join-Path (Split-Path $PSScriptRoot -Parent) 'Auto-Tune-Loop.ps1'
$T = Join-Path $env:TEMP ('k1_pull_test_' + (Get-Date -Format 'HHmmss'))
New-Item -ItemType Directory -Force -Path $T | Out-Null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($loop, [ref]$null, [ref]$null)
foreach ($fn in 'Invoke-Logged', 'Receive-RrdPolled') {
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

# fake scp: sleeps N s (ping), then writes the target file and exits 0
$fake = Join-Path $T 'fake_scp.cmd'
@'
@echo off
ping -n %FAKE_SCP_SECS% 127.0.0.1 >nul
for %%a in (%*) do set LAST=%%a
echo data> "%LAST%"
exit /b 0
'@ | Out-File -Encoding ascii $fake
$ScpExe = $fake

# P1: robot turns busy 3 s into a 60 s pull -> aborted within one poll, returns 10, no child left
$env:FAKE_SCP_SECS = '60'
$script:t0 = Get-Date
function Test-RobotIdle { return (((Get-Date) - $script:t0).TotalSeconds -lt 3) }
$dst = Join-Path $T 'p1.rrd'
$rc = Receive-RrdPolled '/r/p1.rrd' $dst
$secs = ((Get-Date) - $script:t0).TotalSeconds
Start-Sleep -Milliseconds 500
$left = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'fake_scp' -or ($_.Name -eq 'PING.EXE' -and $_.CommandLine -match '127\.0\.0\.1') })
Check 'P1 follow starts mid-pull -> aborted (10)' ($rc -eq 10 -and $secs -lt 12 -and $left.Count -eq 0 -and -not (Test-Path $dst)) ("rc={0} after {1:N1}s, leftover={2}" -f $rc, $secs, $left.Count)

# P2: robot stays idle, pull finishes in ~2 s -> returns 0 and the file exists
$env:FAKE_SCP_SECS = '3'
function Test-RobotIdle { return $true }
$dst = Join-Path $T 'p2.rrd'
$t0 = Get-Date
$rc = Receive-RrdPolled '/r/p2.rrd' $dst
Check 'P2 idle robot -> completes (0)' ($rc -eq 0 -and (Test-Path $dst) -and -not (Test-Path ($dst + '.scp.log'))) ("rc={0} in {1:N1}s" -f $rc, ((Get-Date) - $t0).TotalSeconds)

# P3: robot unreachable from the start of the second poll -> aborted too (fail-closed)
$env:FAKE_SCP_SECS = '60'
function Test-RobotIdle { return $false }
$dst = Join-Path $T 'p3.rrd'
$t0 = Get-Date
$rc = Receive-RrdPolled '/r/p3.rrd' $dst
Check 'P3 unreachable mid-pull -> aborted (10)' ($rc -eq 10 -and ((Get-Date) - $t0).TotalSeconds -lt 12) ("rc={0}" -f $rc)

# P4 operator-session contract: while K1Finder's named mutex exists, the REAL Test-RobotIdle reads
# busy even though the robot answers idle; once it is disposed, idle again.
foreach ($fn in 'Test-OperatorSession', 'Test-RobotIdle') {
  $def = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $fn }, $true) | Select-Object -First 1
  if (-not $def) { Write-Host "missing function $fn" -ForegroundColor Red; $fails++; continue }
  . ([scriptblock]::Create($def.Extent.Text))
}
function Ssh-Text([string]$cmd) { 'idle' }
$before = Test-RobotIdle
try { $op = New-Object System.Threading.Mutex($false, 'Global\K1-Operator-Session') } catch { $op = New-Object System.Threading.Mutex($false, 'Local\K1-Operator-Session') }
$during = Test-RobotIdle
$op.Dispose()
$after = Test-RobotIdle
Check 'P4 K1Finder operator session -> busy, then idle' ($before -and -not $during -and $after) ("before={0} during={1} after={2}" -f $before, $during, $after)

Write-Host ("`n{0} failure(s)" -f $fails)
exit $fails

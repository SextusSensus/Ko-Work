<#
  Run-AutotuneTests.ps1 -- the autotune pipeline's offline test suite. No robot, no GPU needed.
    Test-AutotuneStage.ps1          Autotune-Stage.ps1's 0/1/2 exit contract (16 cases, ~3 min: one case
                                    waits out a 1-minute labeller cap)
    Test-AutotunePolledPull.ps1     the watched .rrd pull + the K1-Operator-Session contract (4 cases)
    Test-AutotuneLoopFakeRobot.ps1  Auto-Tune-Loop.ps1 end to end against a fake robot (3 cases; skipped
                                    while the autotune service holds the loop mutex)
  Exit code = number of failed suites.
#>
$ErrorActionPreference = 'Continue'
$fail = 0
foreach ($t in 'Test-AutotuneStage.ps1', 'Test-AutotunePolledPull.ps1', 'Test-AutotuneLoopFakeRobot.ps1') {
  Write-Host ("==== {0}" -f $t) -ForegroundColor Cyan
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot $t)
  if ($LASTEXITCODE -ne 0) { $fail++ }
}
Write-Host ("`n{0} suite(s) failed" -f $fail)
exit $fail

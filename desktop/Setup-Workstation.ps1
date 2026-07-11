<#
  Setup-Workstation.ps1  (P7/P8 desktop bootstrap, Windows-native)

  Create a Python venv on the DESKTOP workstation and install a pinned requirements file into it.
  Runs on the desktop (where P7 ingest/training + P8 reconstruction happen), NOT the laptop. Generic on
  purpose: the actual version pins + any torch CUDA --index-url live INSIDE the requirements file (pip
  honors --extra-index-url directives there), so this bootstrap never needs editing when pins change.

  See docs/WORKSTATION_SETUP.md for which requirements file to use and why (P7 vs P8, one venv or two).

  Examples:
    .\Setup-Workstation.ps1 -Req ..\requirements-p7.txt -Venv .venv-p7
    .\Setup-Workstation.ps1 -Req ..\requirements-p8.txt -Venv .venv-p8
    .\Setup-Workstation.ps1 -Req ..\requirements-p7.txt -Venv .venv-p7 -WhatIf   # show plan, install nothing
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Req,
  [string]$Venv = '.venv',
  [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Req)) { throw "requirements file not found: $Req" }
$Req = (Resolve-Path -LiteralPath $Req).Path

# Find a REAL Python 3 (not the Windows App-Execution-Alias stub, which resolves via Get-Command but
# exits non-zero). Invoke --version and check the exit code + output.
$pyExe = $null; $pyArgs = @(); $ver = ''
foreach ($cand in @(@('py', '-3'), @('python'), @('python3'))) {
  try {
    $v = (& $cand[0] $cand[1..($cand.Count - 1)] --version) 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0 -and $v -match 'Python\s+3') {
      $pyExe = $cand[0]; $pyArgs = @($cand[1..($cand.Count - 1)]); $ver = $v.Trim(); break
    }
  } catch { }
}
if (-not $pyExe) { throw "no working Python 3 found. Install Python 3.12 for Windows (python.org) with 'Add to PATH', then re-run. (A bare 'py'/'python' that prints 'Python was not found' is the Store stub, not Python.)" }
Write-Host "python: $pyExe $($pyArgs -join ' ')  ($ver)"
Write-Host "venv  : $Venv"
Write-Host "reqs  : $Req"
if ($WhatIf) { Write-Host "(dry-run) would: create venv, upgrade pip, pip install -r <reqs>"; return }

if (-not (Test-Path -LiteralPath $Venv)) {
  Write-Host "creating venv $Venv ..."
  & $pyExe @pyArgs -m venv $Venv
  if ($LASTEXITCODE -ne 0) { throw "venv creation failed (exit $LASTEXITCODE)" }
} else {
  Write-Host "venv $Venv already exists -- installing into it."
}

$venvPy = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPy)) { throw "venv python not found at $venvPy" }

Write-Host "upgrading pip/setuptools/wheel ..."
& $venvPy -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed (exit $LASTEXITCODE)" }

Write-Host "installing -r $Req ..."
& $venvPy -m pip install -r $Req
if ($LASTEXITCODE -ne 0) { throw "requirements install failed (exit $LASTEXITCODE) -- see the error above." }

Write-Host ""
Write-Host "SETUP-OK  ($Venv)"
Write-Host "activate with:  $Venv\Scripts\Activate.ps1"
Write-Host "then run the smoke tests in docs/WORKSTATION_SETUP.md."

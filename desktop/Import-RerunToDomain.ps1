# Import a Rerun .rrd (or synthetic fixture) into a Local Map domain with exact poses.
# Requires Python 3 + numpy. Real .rrd decode needs rerun-sdk matching the recording.
#
# Examples:
#   .\Import-RerunToDomain.ps1 -Domain exact-lab -Fixture
#   .\Import-RerunToDomain.ps1 -Domain exact-lab -Rrd D:\runs\cap.rrd -Labels D:\runs\labels.jsonl -Place
#   .\Import-RerunToDomain.ps1 -Domain exact-lab -RunDir D:\runs\2026-09-11_follow -Place -AllowGroundRaycast

param(
  [Parameter(Mandatory = $true)][string]$Domain,
  [string]$Rrd,
  [string]$RunDir,
  [switch]$Fixture,
  [string]$FixturePath,
  [string]$Labels,
  [string]$RunId,
  [switch]$Place,
  [switch]$NoPlace,
  [switch]$AllowGroundRaycast,
  [double]$CamH = 1.35,
  [double]$CamPitch = 8.0,
  [string]$DataRoot
)

$ErrorActionPreference = 'Stop'
$Desktop = $PSScriptRoot
$Repo = Split-Path -Parent $Desktop
$Py = Join-Path $Repo 'eval\localmap_rerun_ingest.py'
if (-not (Test-Path $Py)) { throw "missing $Py" }

$args = @($Py, '--domain', $Domain)
if ($DataRoot) { $args += @('--data-root', $DataRoot) }
else { $args += @('--data-root', (Join-Path $Desktop 'localmap-data')) }
$args += @('--cam-h', "$CamH", '--cam-pitch', "$CamPitch")
if ($RunId) { $args += @('--run-id', $RunId) }
if ($Labels) { $args += @('--labels', $Labels) }
if ($AllowGroundRaycast) { $args += '--allow-ground-raycast' }
if ($NoPlace) { $args += '--no-place' }
elseif ($Place) { $args += '--place' }

if ($Fixture) {
  $fp = if ($FixturePath) { $FixturePath } else { Join-Path $Desktop 'localmap-data\fixtures\rerun_exact_place' }
  $args += @('--fixture', $fp)
  if (-not $AllowGroundRaycast) { $args += '--allow-ground-raycast' }  # fixture C needs it
} elseif ($Rrd) {
  $args += @('--rrd', $Rrd)
} elseif ($RunDir) {
  $args += @('--run-dir', $RunDir)
} else {
  throw 'Specify -Rrd, -RunDir, or -Fixture'
}

Write-Host ("python " + ($args -join ' '))
& python @args
exit $LASTEXITCODE

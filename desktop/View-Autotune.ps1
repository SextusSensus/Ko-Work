<#
.SYNOPSIS
  Open an autotuned run's labelled recording in the rerun 0.23.1 viewer.
.DESCRIPTION
  .\View-Autotune.ps1            newest run in runtime\autotune
  .\View-Autotune.ps1 -Run <id>  a specific run
  .\View-Autotune.ps1 -List      list runs with their validation status
  Uses the analysis venv's viewer: it matches the robot's writer (rerun 0.23.1).
#>
param([string]$Run, [switch]$List)
$repo = Split-Path $PSScriptRoot -Parent
$root = Join-Path $repo 'autotune'
if (-not (Test-Path $root)) { Write-Host "no autotune folder yet: $root"; exit 1 }
if ($List) {
  Get-ChildItem $root -Directory | Sort-Object Name -Descending | ForEach-Object {
    $vf = Join-Path $_.FullName 'label_validation.json'
    $st = if (Test-Path $vf) { (Get-Content $vf -Raw | ConvertFrom-Json).status } else { '-' }
    $sent = if (Test-Path (Join-Path $_.FullName '.sent_to_robot')) { 'sent' } else { '' }
    '{0}  {1,-12} {2}' -f $_.Name, $st, $sent
  }
  exit 0
}
# Newest FINISHED labelling (review C10), ordered by NAME: run ids are UTC timestamps, so name order is
# chronological and is not disturbed by a later marker write bumping a folder's LastWriteTime.
$dir = if ($Run) { Join-Path $root $Run } else {
  (Get-ChildItem $root -Directory |
     Where-Object { (Test-Path (Join-Path $_.FullName 'labeled.rrd')) -and (Test-Path (Join-Path $_.FullName 'label_validation.json')) } |
     Sort-Object Name -Descending | Select-Object -First 1).FullName
}
if (-not $dir) { Write-Host "no labelled run in $root"; exit 1 }
$rrd = Join-Path $dir 'labeled.rrd'
if (-not (Test-Path $rrd)) { Write-Host "no labeled.rrd in $dir"; exit 1 }
$sum = Join-Path $dir 'SUMMARY.txt'
if (Test-Path $sum) { Get-Content $sum }
$viewer = Join-Path $repo '.venv-analysis\Scripts\rerun.exe'
if (-not (Test-Path $viewer)) { Write-Host "viewer not found: $viewer"; exit 1 }
Start-Process -FilePath $viewer -ArgumentList ('"{0}"' -f $rrd)

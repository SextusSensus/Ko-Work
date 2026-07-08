<#
  Runs.ps1  (P6.3, workstation run index)

  A tiny filesystem+JSON index over the run bundles pulled to this workstation by Pull-Run.ps1.
  Derives runs/index.json by scanning each runs/<run_id>/manifest.json -- no database, no daemon.

  NOTE ON LANGUAGE: the plan names a Python `runs.py`; this workstation is the Windows control PC
  with no Python (the run store lives here per the user's choice), so the helper is PowerShell to
  match Pull-Run.ps1 and the app. index.json is language-neutral, so a Python `runs.py` on a future
  GPU box can read/extend the same file unchanged.

  Commands:
    .\Runs.ps1 list            # refresh index.json from the manifests on disk, print a table
    .\Runs.ps1 show <run_id>   # per-file detail (size, sha256) for one run
    .\Runs.ps1 reindex         # rewrite index.json only (no table)

  index.json entry: { run_id, created_utc, profile, duration_s, file_count, total_bytes, outcome }
  `outcome` is a slot for the eval task-success label (P5.1); it defaults to null and is PRESERVED
  across reindex, so labels you (or the scorer) attach are never clobbered by a rescan.
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)][ValidateSet('list', 'show', 'reindex')][string]$Cmd = 'list',
  [Parameter(Position = 1)][string]$RunId = '',
  [string]$LocalRuns = (Join-Path $PSScriptRoot '..\runs')
)

$ErrorActionPreference = 'Stop'
if (-not [System.IO.Path]::IsPathRooted($LocalRuns)) { $LocalRuns = Join-Path $PSScriptRoot '..\runs' }
$LocalRuns  = [System.IO.Path]::GetFullPath($LocalRuns)
$indexPath  = Join-Path $LocalRuns 'index.json'

function Read-Index {
  # existing run_id -> outcome, so a rescan preserves attached labels.
  $map = @{}
  if (Test-Path -LiteralPath $indexPath) {
    try {
      (Get-Content -LiteralPath $indexPath -Raw | ConvertFrom-Json).runs | ForEach-Object {
        if ($_.run_id) { $map[$_.run_id] = $_.outcome }
      }
    } catch { }   # a corrupt index is rebuilt from the manifests, not trusted
  }
  return $map
}

function Build-Index {
  $prevOutcome = Read-Index
  $entries = @()
  if (Test-Path -LiteralPath $LocalRuns) {
    Get-ChildItem -LiteralPath $LocalRuns -Directory -ErrorAction SilentlyContinue | ForEach-Object {
      $man = Join-Path $_.FullName 'manifest.json'
      if (-not (Test-Path -LiteralPath $man)) { return }   # not a complete bundle (e.g. a partial pull)
      try { $m = Get-Content -LiteralPath $man -Raw | ConvertFrom-Json } catch { return }
      $rid = if ($m.run_id) { $m.run_id } else { $_.Name }
      $entries += [ordered]@{
        run_id      = $rid
        created_utc = $m.created_utc
        profile     = $m.profile
        duration_s  = $m.duration_s
        file_count  = $m.file_count
        total_bytes = $m.total_bytes
        outcome     = if ($prevOutcome.ContainsKey($rid)) { $prevOutcome[$rid] } else { $null }
      }
    }
  }
  # newest first by created_utc (the id's UTC stamp sorts lexically = chronologically)
  $entries = @($entries | Sort-Object { $_.created_utc } -Descending)
  New-Item -ItemType Directory -Force -Path $LocalRuns | Out-Null
  @{ runs = $entries } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $indexPath -Encoding utf8
  return $entries
}

switch ($Cmd) {
  'reindex' {
    $e = Build-Index
    Write-Host "reindexed $($e.Count) run(s) -> $indexPath"
  }
  'show' {
    if (-not $RunId) { throw "usage: Runs.ps1 show <run_id>" }
    $man = Join-Path (Join-Path $LocalRuns $RunId) 'manifest.json'
    if (-not (Test-Path -LiteralPath $man)) { throw "no manifest for '$RunId' under $LocalRuns" }
    $m = Get-Content -LiteralPath $man -Raw | ConvertFrom-Json
    Write-Host ("run_id     : {0}" -f $m.run_id)
    Write-Host ("created_utc: {0}" -f $m.created_utc)
    Write-Host ("profile    : {0}" -f $m.profile)
    Write-Host ("duration_s : {0}" -f $m.duration_s)
    Write-Host ("git_version: {0}" -f $m.git_version)
    Write-Host ("files      : {0}  ({1:n0} bytes)" -f $m.file_count, $m.total_bytes)
    $m.files | ForEach-Object { Write-Host ("  {0,-24} {1,12:n0}  {2}" -f $_.name, $_.bytes, $_.sha256.Substring(0, 12)) }
  }
  default {  # 'list'
    $e = Build-Index
    if (-not $e.Count) { Write-Host "no run bundles under $LocalRuns (pull one with Pull-Run.ps1)"; return }
    Write-Host ("{0,-32} {1,-10} {2,8}  {3,10}  {4}" -f 'run_id', 'profile', 'dur_s', 'MB', 'outcome')
    Write-Host ('-' * 78)
    foreach ($r in $e) {
      Write-Host ("{0,-32} {1,-10} {2,8}  {3,10:n1}  {4}" -f `
        $r.run_id, $r.profile, ($(if ($null -ne $r.duration_s) { $r.duration_s } else { '-' })),
        ($r.total_bytes / 1MB), ($(if ($r.outcome) { $r.outcome } else { '-' })))
    }
  }
}

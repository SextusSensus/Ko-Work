<#
.SYNOPSIS
  Offline batch labelling for existing local runs\ (Plan A label-quality scoring).

.DESCRIPTION
  Scans ..\runs\<run_id>\ for capture-complete bundles (manifest.json + size-verified .rrd),
  then invokes Autotune-Stage.ps1 -NoSend on each. NEVER sends to the robot, NEVER uses a
  password (BatchMode SSH only exists inside Autotune-Stage, and -NoSend exits before any
  robot contact).

  Outputs land under ..\autotune\<run_id>\ (same layout as the live loop):
    labeled.rrd, label_validation.json, obstacle_summary.json, obstacles.jsonl, SUMMARY.txt

  Already-labelled runs (label_validation.json at the current gate) are skipped unless -Force.
  Prints a final PASS / FAIL / INCONCLUSIVE / skipped / errors tally and writes
  ..\autotune\batch_label_tally.json for scoring.

.EXAMPLE
  # Dry-run: list eligible runs, label nothing
  .\Batch-Label-Runs.ps1 -DryRun

  # First 10 newest capture runs (overnight-friendly sample)
  .\Batch-Label-Runs.ps1 -Newest 10

  # Random sample of 14 from all eligible
  .\Batch-Label-Runs.ps1 -Sample 14

  # Everything (hours of GPU time on ~14 GB of .rrd)
  .\Batch-Label-Runs.ps1

  # Re-label even if a current-gate report already exists
  .\Batch-Label-Runs.ps1 -Newest 5 -Force
#>
param(
  [string]$LocalRuns = '',
  [string]$AutotuneDir = '',
  [string]$StageScript = '',
  [string]$Python = '',
  [int]$Newest = 0,
  [int]$Sample = 0,
  [int]$LabelTimeoutMin = 90,
  [switch]$Force,
  [switch]$DryRun,
  [string]$OutTally = ''
)
$ErrorActionPreference = 'Stop'
# Defaults in the body: Windows PowerShell 5.1 leaves $PSScriptRoot empty while an ADVANCED
# script's param() defaults are evaluated under powershell.exe -File (same pattern as
# Autotune-Stage.ps1 / Auto-Tune-Loop.ps1).
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $LocalRuns)   { $LocalRuns   = Join-Path $here '..\runs' }
if (-not $AutotuneDir) { $AutotuneDir = Join-Path $here '..\autotune' }
if (-not $StageScript) { $StageScript = Join-Path $here 'Autotune-Stage.ps1' }
if (-not $Python)      { $Python      = Join-Path $here '..\.venv-analysis\Scripts\python.exe' }
if (-not $OutTally)    { $OutTally    = Join-Path $AutotuneDir 'batch_label_tally.json' }

$LocalRuns   = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($LocalRuns)
$AutotuneDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($AutotuneDir)
$StageScript = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($StageScript)
$Python      = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Python)
$OutTally    = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutTally)

# Keep in step with Autotune-Stage.ps1 $GateVersion / eval/rrd_label.py gate_version.
$GateVersion = 3

function Get-ValidationReport([string]$adir) {
  $vf = Join-Path $adir 'label_validation.json'
  if (-not (Test-Path -LiteralPath $vf)) { return $null }
  try {
    $j = Get-Content -LiteralPath $vf -Raw | ConvertFrom-Json
    $gv = [int]$j.gate_version
    if ($gv -lt $GateVersion) { return $null }   # stale gate -> treat as unlabelled
    return $j
  } catch { return $null }
}

function Get-ValidationStatus([string]$adir) {
  $j = Get-ValidationReport $adir
  if ($null -eq $j) { return $null }
  return [string]$j.status
}

function Get-DepthPairingStats([string]$adir) {
  $j = Get-ValidationReport $adir
  if ($null -eq $j -or -not $j.checks -or -not $j.checks.depth_pairing) { return $null }
  $dp = $j.checks.depth_pairing
  return [ordered]@{
    status      = [string]$dp.status
    frac_paired = $(if ($null -ne $dp.frac_paired) { [double]$dp.frac_paired } else { $null })
    paired      = $(if ($null -ne $dp.paired) { [int]$dp.paired } else { $null })
    labelled_frames = $(if ($null -ne $dp.labelled_frames) { [int]$dp.labelled_frames } else { $null })
    why         = $(if ($dp.why) { [string]$dp.why } else { $null })
  }
}

function Get-IdDriftStats([string]$adir, [string]$py) {
  # suspect_iddrift lives in eval/rrd_obstacles.py (Phase 2), not obstacle_summary.json.
  $jsonl = Join-Path $adir 'obstacles.jsonl'
  $outJs = Join-Path $adir 'obstacle_set.json'
  if (-not (Test-Path -LiteralPath $jsonl)) { return $null }
  $obstaclesPy = Join-Path (Split-Path $here -Parent) 'eval\rrd_obstacles.py'
  if (-not (Test-Path -LiteralPath $obstaclesPy)) { return $null }
  if (-not (Test-Path -LiteralPath $py)) { return $null }
  $ErrorActionPreference = 'Continue'
  & $py $obstaclesPy $jsonl --json $outJs 2>$null | Out-Null
  $rc = $LASTEXITCODE
  $ErrorActionPreference = 'Stop'
  if ($rc -ne 0 -or -not (Test-Path -LiteralPath $outJs)) { return $null }
  try {
    $set = Get-Content -LiteralPath $outJs -Raw | ConvertFrom-Json
    $obs = @($set.obstacles)
    $nsus = @($obs | Where-Object { $_.suspect_iddrift }).Count
    return [ordered]@{
      n_obstacles = $obs.Count
      n_suspect_iddrift = [int]$nsus
      suspects = @($obs | Where-Object { $_.suspect_iddrift } | ForEach-Object {
        [ordered]@{ id = $_.id; cls = $_.cls; spread_m = $_.spread_m; range_m = $_.range_m }
      })
    }
  } catch { return $null }
}

function Test-CaptureComplete([string]$dir) {
  # Same contract as Autotune-Stage: manifest lists the .rrd and bytes must match exactly.
  $mf = Join-Path $dir 'manifest.json'
  if (-not (Test-Path -LiteralPath $mf)) { return $null }
  $m = try { Get-Content -LiteralPath $mf -Raw | ConvertFrom-Json } catch { $null }
  if (-not $m) { return $null }
  $name = $null; $bytes = [int64]0
  foreach ($f in $m.files) {
    if ($f.name -like '*.rrd') { $name = [string]$f.name; $bytes = [int64]$f.bytes; break }
  }
  if (-not $name) { return $null }
  $rrd = Join-Path $dir $name
  if (-not (Test-Path -LiteralPath $rrd)) { return $null }
  if ((Get-Item -LiteralPath $rrd).Length -ne $bytes) { return $null }
  return $rrd
}

if (-not (Test-Path -LiteralPath $StageScript)) {
  Write-Host ("batch: Autotune-Stage.ps1 not found: {0}" -f $StageScript) -ForegroundColor Red
  exit 2
}
if (-not (Test-Path -LiteralPath $LocalRuns)) {
  Write-Host ("batch: no runs folder: {0}" -f $LocalRuns) -ForegroundColor Red
  exit 2
}
New-Item -ItemType Directory -Force -Path $AutotuneDir | Out-Null

# Discover capture-complete run dirs (run_id = UTC stamp + hash).
$candidates = @(Get-ChildItem -LiteralPath $LocalRuns -Directory -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -match '^[0-9]{8}T[0-9]{6}Z_' } |
  Sort-Object Name -Descending |
  ForEach-Object {
    $rrd = Test-CaptureComplete $_.FullName
    if ($rrd) {
      [pscustomobject]@{ RunId = $_.Name; Dir = $_.FullName; Rrd = $rrd; Bytes = (Get-Item -LiteralPath $rrd).Length }
    }
  })

if ($Newest -gt 0 -and $candidates.Count -gt $Newest) {
  $candidates = @($candidates | Select-Object -First $Newest)
}
if ($Sample -gt 0 -and $candidates.Count -gt $Sample) {
  # Deterministic-enough shuffle without requiring -Seed plumbing: Random with tick seed.
  $rng = New-Object System.Random
  $candidates = @($candidates | Sort-Object { $rng.Next() } | Select-Object -First $Sample | Sort-Object RunId -Descending)
}

Write-Host ("Batch-Label-Runs  runs={0}  autotune={1}  gate=v{2}  NoSend=ALWAYS" -f $LocalRuns, $AutotuneDir, $GateVersion) -ForegroundColor Cyan
Write-Host ("eligible capture-complete: {0}   DryRun={1}  Force={2}  Newest={3}  Sample={4}" -f `
  $candidates.Count, [bool]$DryRun, [bool]$Force, $Newest, $Sample) -ForegroundColor DarkGray
if ($candidates.Count -eq 0) {
  Write-Host 'batch: nothing to do (need manifest.json + size-matched .rrd under runs\)' -ForegroundColor Yellow
  exit 0
}

# Preflight once: CUDA + analysis venv (Autotune-Stage would exit 2 per run otherwise).
if (-not $DryRun) {
  if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host ("batch: analysis python missing: {0}" -f $Python) -ForegroundColor Red
    Write-Host '  expected ..\.venv-analysis\Scripts\python.exe (CUDA torch + ultralytics + transformers)' -ForegroundColor Yellow
    exit 2
  }
  $ErrorActionPreference = 'Continue'
  & $Python -c 'import sys, torch; sys.exit(0 if torch.cuda.is_available() else 3)' 2>$null
  $cudaRc = $LASTEXITCODE
  $ErrorActionPreference = 'Stop'
  if ($cudaRc -ne 0) {
    Write-Host 'batch: CUDA unavailable -- labelling needs a GPU (same gate as Autotune-Stage)' -ForegroundColor Red
    exit 2
  }
  Write-Host 'preflight: CUDA OK' -ForegroundColor Green
}

$tally = [ordered]@{
  # Mutually exclusive primary buckets for this batch invocation.
  pass = 0; fail = 0; inconclusive = 0; skipped = 0; errors = 0
  # Among skipped: prior gate status (for corpus quality without re-labelling).
  skipped_pass = 0; skipped_fail = 0; skipped_inconclusive = 0; skipped_other = 0
  started_utc = (Get-Date).ToUniversalTime().ToString('o')
  gate_version = $GateVersion
  runs = $null
}
# ArrayList avoids the PS 5.1 `@() += $x` scalarization trap on the first append.
$runRows = New-Object System.Collections.ArrayList

foreach ($c in $candidates) {
  $adir = Join-Path $AutotuneDir $c.RunId
  $prior = Get-ValidationStatus $adir
  $row = [ordered]@{
    run_id = $c.RunId
    rrd_mb = [math]::Round($c.Bytes / 1MB, 1)
    action = $null
    status = $null
    stage_exit = $null
    note = $null
  }

  if ($prior -and -not $Force) {
    $row.action = 'skipped'
    $row.status = $prior
    $row.note = 'already labelled at current gate'
    $tally.skipped++
    switch ($prior) {
      'PASS'         { $tally.skipped_pass++ }
      'FAIL'         { $tally.skipped_fail++ }
      'INCONCLUSIVE' { $tally.skipped_inconclusive++ }
      default        { $tally.skipped_other++ }
    }
    Write-Host ("  SKIP  {0}  ({1:N0} MB) -- already {2}" -f $c.RunId, ($c.Bytes / 1MB), $prior) -ForegroundColor DarkGray
    [void]$runRows.Add($row)
    continue
  }

  if ($DryRun) {
    $row.action = 'dry_run'
    $row.note = if ($prior) { "would re-label (was $prior)" } else { 'would label' }
    Write-Host ("  PLAN  {0}  ({1:N0} MB)  {2}" -f $c.RunId, ($c.Bytes / 1MB), $row.note) -ForegroundColor Cyan
    [void]$runRows.Add($row)
    continue
  }

  if ($Force -and $prior) {
    # Move the current-gate report aside so Autotune-Stage re-launches the labeller.
    New-Item -ItemType Directory -Force -Path $adir | Out-Null
    $vf = Join-Path $adir 'label_validation.json'
    if (Test-Path -LiteralPath $vf) {
      $bak = Join-Path $adir ('label_validation.force{0}.json' -f (Get-Date -Format 'yyyyMMddHHmmss'))
      Move-Item -LiteralPath $vf -Destination $bak -Force
    }
    Remove-Item -LiteralPath (Join-Path $adir '.label_attempts') -Force -ErrorAction SilentlyContinue
  }

  Write-Host ("`n>>> LABEL {0}  ({1:N0} MB) ..." -f $c.RunId, ($c.Bytes / 1MB)) -ForegroundColor Green
  $row.action = 'labelled'
  $rc = 0
  try {
    # -File so exit codes match the loop's contract; -NoSend is unconditional.
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $StageScript `
      -RunId $c.RunId `
      -LocalRunDir $c.Dir `
      -RrdPath $c.Rrd `
      -AutotuneDir $AutotuneDir `
      -Python $Python `
      -LabelTimeoutMin $LabelTimeoutMin `
      -NoSend
    $rc = $LASTEXITCODE
  } catch {
    $rc = 2
    $row.note = $_.Exception.Message
  }
  $row.stage_exit = $rc
  $st = Get-ValidationStatus $adir
  $row.status = $st

  if ($rc -eq 0 -and $st -eq 'PASS') {
    $tally.pass++
    Write-Host ("  OK    {0} -> PASS" -f $c.RunId) -ForegroundColor Green
  } elseif ($rc -eq 1 -or $st -eq 'FAIL' -or $st -eq 'INCONCLUSIVE') {
    if ($st -eq 'FAIL') { $tally.fail++; Write-Host ("  FAIL  {0}" -f $c.RunId) -ForegroundColor Red }
    elseif ($st -eq 'INCONCLUSIVE') { $tally.inconclusive++; Write-Host ("  INC   {0} -> INCONCLUSIVE" -f $c.RunId) -ForegroundColor Yellow }
    else { $tally.errors++; $row.note = "stage exit 1 but status='$st'"; Write-Host ("  ERR   {0} exit 1 status={1}" -f $c.RunId, $st) -ForegroundColor Red }
  } else {
    $tally.errors++
    if (-not $row.note) { $row.note = "stage exit $rc (retryable: no GPU, truncated, timeout, attempts, tools)" }
    Write-Host ("  ERR   {0}  exit={1}  {2}" -f $c.RunId, $rc, $row.note) -ForegroundColor Yellow
  }
  [void]$runRows.Add($row)
}

$tally.runs = @($runRows.ToArray())
$tally.finished_utc = (Get-Date).ToUniversalTime().ToString('o')
$tally | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $OutTally -Encoding utf8

Write-Host ''
Write-Host '======== batch tally (mutually exclusive) ========' -ForegroundColor Cyan
Write-Host ("  PASS          {0}  (labelled this run)" -f $tally.pass)
Write-Host ("  FAIL          {0}" -f $tally.fail)
Write-Host ("  INCONCLUSIVE  {0}" -f $tally.inconclusive)
Write-Host ("  skipped       {0}  (already labelled; prior PASS={1} FAIL={2} INC={3})" -f `
  $tally.skipped, $tally.skipped_pass, $tally.skipped_fail, $tally.skipped_inconclusive)
Write-Host ("  errors        {0}  (no validation / tool / CUDA / timeout)" -f $tally.errors)
Write-Host ("  eligible      {0}" -f $candidates.Count)
$corpusPass = $tally.pass + $tally.skipped_pass
$corpusFail = $tally.fail + $tally.skipped_fail
$corpusInc  = $tally.inconclusive + $tally.skipped_inconclusive
Write-Host ("  corpus        PASS={0} FAIL={1} INC={2}  (this batch + skipped priors)" -f $corpusPass, $corpusFail, $corpusInc) -ForegroundColor DarkGray
Write-Host ("tally -> {0}" -f $OutTally) -ForegroundColor DarkGray
Write-Host ''
Write-Host 'Send back for Plan A scoring (small; do NOT zip labeled.rrd unless asked):' -ForegroundColor Cyan
Write-Host '  autotune\batch_label_tally.json'
Write-Host '  autotune\<run_id>\label_validation.json'
Write-Host '  autotune\<run_id>\obstacle_summary.json'
Write-Host '  autotune\<run_id>\SUMMARY.txt'
Write-Host 'Optional per-detection detail: autotune\<run_id>\obstacles.jsonl'
Write-Host 'View a PASS locally:  .\View-Autotune.ps1 -List   then   .\View-Autotune.ps1 -Run <id>'

if ($DryRun) { exit 0 }
if ($tally.errors -gt 0) { exit 2 }
exit 0

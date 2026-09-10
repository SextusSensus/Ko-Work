<#
.SYNOPSIS
  One run: GPU labelling -> geometry validation -> viewable folder -> (PASS only) robot bundle.

.DESCRIPTION
  Called per finished run by Auto-Tune-Loop.ps1, and runnable by hand:
    .\Autotune-Stage.ps1 -RunId <id> -LocalRunDir ..\runs\<id> -NoSend

  Writes <AutotuneDir>\<RunId>\ -- the folder to look at:
    labeled.rrd            objects + walls/floor in 3-D (open with View-Autotune.ps1)
    obstacle_summary.json  the deduped obstacle set and per-class counts
    label_validation.json  the geometry gate: floor tilt/height, camera-height band, walls
    obstacles.jsonl        every detection
    SUMMARY.txt            human-readable digest
    + the loop's tune_report.json / tune_patch.yaml / depth_replay.json, mirrored in

  Then, ONLY when the geometry validation PASSES, sends the SMALL bundle (summary, validation,
  hints -- never the multi-hundred-MB labelled recording) to <RemoteAutotune>/<RunId>/ and repoints
  <RemoteAutotune>/latest at the newest complete bundle there. FAIL or INCONCLUSIVE is reported and
  never sent: labels that land in the wrong place are worse than no labels. Idempotent: a
  .sent_to_robot marker records a completed send. -NoSend leaves a durable .no_send marker (delete
  it to release the bundle); .label_attempts caps labelling at 3 runs of the labeller.

  Exit codes: 0 done (validated; sent, held by .no_send, or already sent), 1 validation FAIL or
  INCONCLUSIVE (never sent), 2 skipped or retryable (no or truncated .rrd, tool missing, no CUDA,
  labelling error or timeout, attempts exhausted, robot unreachable).
#>
param(
  [Parameter(Mandatory = $true)][string]$RunId,
  [Parameter(Mandatory = $true)][string]$LocalRunDir,
  [string]$RrdPath,
  [string]$AutotuneDir  = '',
  [string]$Python       = '',
  [string]$Label        = '',
  [string]$WeightsDir   = '',
  [string[]]$LabelArgs  = @('--track', '--merge-radius', '0.5', '--seg', '--seg-every', '2',
                            '--stride', '2', '--model', 'yolo11x.pt',
                            '--seg-model', 'nvidia/segformer-b2-finetuned-ade-512-512'),
  [string]$User           = 'booster',
  [string]$Ip             = '192.168.9.75',
  [string]$RemoteAutotune = '/home/booster/autotune',
  [ValidateRange(1, 1440)][int]$LabelTimeoutMin = 90,
  [switch]$NoSend
)
$ErrorActionPreference = 'Stop'
# Unexpected errors are RETRYABLE (exit 2), never the verdict code 1: under powershell.exe -File an
# unhandled throw exits 1, which the loop and a by-hand caller would read as a final geometry FAIL.
trap { Write-Host ("  stage: unexpected error -- {0} (retried later)" -f $_.Exception.Message) -ForegroundColor Yellow; exit 2 }
# Defaults resolved HERE, not in param(): in Windows PowerShell 5.1, $PSScriptRoot is EMPTY while an
# ADVANCED script's param() defaults are evaluated -- [CmdletBinding()] or any [Parameter()], as the
# Mandatory RunId above makes this one -- when it is started with `powershell.exe -File`. Isolated
# by per-factor repro: a plain param() works; encoding, BOM and help blocks are irrelevant.
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $AutotuneDir) { $AutotuneDir = Join-Path $here '..\autotune' }
if (-not $Python)      { $Python      = Join-Path $here '..\.venv-analysis\Scripts\python.exe' }
if (-not $Label)       { $Label       = Join-Path $here '..\eval\rrd_label.py' }
if (-not $WeightsDir)  { $WeightsDir  = Join-Path $env:USERPROFILE '.cache\k1_weights' }
$SSH_OPTS = @('-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=8',
              '-o', 'ServerAliveInterval=10', '-o', 'BatchMode=yes')
$target = '{0}@{1}' -f $User, $Ip

function Invoke-Logged([string]$exe, [string[]]$argv, [string]$log) {
  # Windows PowerShell 5.1 wraps every stderr line of a native command in an ErrorRecord, and under
  # $ErrorActionPreference = 'Stop' the FIRST one is a terminating error. Python tools write
  # warnings and progress to stderr, ssh writes host-key notices there. So natives run with a local
  # 'Continue' and are judged by their exit code, never by whether they printed to stderr.
  $ErrorActionPreference = 'Continue'
  if ($log) { & $exe @argv *> $log } else { & $exe @argv *> $null }
  return $LASTEXITCODE
}

# Provider-aware: [IO.Path]::GetFullPath resolves against the PROCESS working directory, not $PWD, so
# a relative -LocalRunDir typed at a prompt after a cd resolved somewhere else (review C5).
$LocalRunDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($LocalRunDir)
$Python      = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Python)
$Label       = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Label)
$WeightsDir  = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($WeightsDir)
$adir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath((Join-Path $AutotuneDir $RunId))
New-Item -ItemType Directory -Force -Path $adir | Out-Null
# -NoSend is recorded FIRST (round 2): every exit below -- no GPU, a busy lock, a timeout, a FAIL --
# would otherwise leave no hold, and the loop's retry would label and send a run the operator held.
if ($NoSend) { (Get-Date -Format o) | Out-File -Encoding utf8 (Join-Path $adir '.no_send') }

# One folder holds everything about this run: mirror the loop's analysis products in.
foreach ($f in @('tune_report.json', 'tune_patch.yaml', 'depth_replay.json', 'manifest.json')) {
  $src = Join-Path $LocalRunDir $f
  if (Test-Path $src) { Copy-Item $src $adir -Force }
}

$val = Join-Path $adir 'label_validation.json'
# A report from an older gate is not trusted: eval/rrd_label.py writes gate_version, bumped whenever the
# gate's meaning changes (3 = wall-time depth pairing + coverage gate, c7f3cb1). An older or unreadable
# report is moved aside, earns fresh attempts, and the run is labelled again.
$GateVersion = 3
if (Test-Path $val) {
  $gv = try { [int]((Get-Content $val -Raw | ConvertFrom-Json).gate_version) } catch { -1 }
  if ($gv -lt $GateVersion) {
    Move-Item -LiteralPath $val -Destination (Join-Path $adir ('label_validation.gate{0}.json' -f [math]::Max($gv, 0))) -Force
    Remove-Item -LiteralPath (Join-Path $adir '.label_attempts') -Force -ErrorAction SilentlyContinue
    Write-Host ("  stage: {0} was validated by gate v{1}, current is v{2} -- re-labelling" -f $RunId, [math]::Max($gv, 0), $GateVersion) -ForegroundColor DarkGray
  }
}
if (-not (Test-Path $val)) {
  if (-not $RrdPath) {
    $cand = Get-ChildItem $LocalRunDir -Filter '*.rrd' -ErrorAction SilentlyContinue |
            Sort-Object Length -Descending | Select-Object -First 1
    if ($cand) { $RrdPath = $cand.FullName }
  }
  if (-not $RrdPath -or -not (Test-Path $RrdPath)) {
    Write-Host ("  stage: no local .rrd for {0} -- nothing to label" -f $RunId) -ForegroundColor DarkGray
    exit 2
  }
  $RrdPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($RrdPath)
  # A TRUNCATED recording must never be labelled (review C1): scp leaves a partial file when the link
  # drops, and labels from part of a run would be sent as the whole run's -- then locked in, because
  # a validation that exists is never redone. Accept it only at the byte count its manifest recorded.
  $mf = Join-Path $LocalRunDir 'manifest.json'
  $leaf = Split-Path $RrdPath -Leaf
  $want = $null
  if (Test-Path $mf) {
    foreach ($f in (Get-Content $mf -Raw | ConvertFrom-Json).files) { if ($f.name -eq $leaf) { $want = [int64]$f.bytes } }
  }
  if ($null -eq $want) {
    # Fail-closed (round 2): with no manifest entry the size cannot be verified -- never label it.
    Write-Host ("  stage: {0} is not listed in {1} -- its size cannot be verified, not labelling" -f $leaf, $mf) -ForegroundColor Yellow
    exit 2
  }
  $have = (Get-Item $RrdPath).Length
  if ($have -ne $want) {
    Write-Host ("  stage: {0} is {1:N0} bytes but the manifest says {2:N0} -- truncated, not labelling" -f $leaf, $have, $want) -ForegroundColor Yellow
    exit 2
  }
  foreach ($need in @($Python, $Label)) {
    if (-not (Test-Path $need)) { Write-Host ("  stage: missing {0}" -f $need) -ForegroundColor Yellow; exit 2 }
  }
  # GPU or nothing (review C14): on the CPU this labeller takes hours and would block the loop.
  $rcCuda = Invoke-Logged $Python @('-c', 'import sys, torch; sys.exit(0 if torch.cuda.is_available() else 3)') $null
  if ($rcCuda -ne 0) {
    Write-Host "  stage: CUDA unavailable -- labelling deferred (retried later)" -ForegroundColor Yellow
    exit 2
  }
  # One labeller per run: a by-hand stage run and the loop's retry must never label the same run at
  # once (both write labeled.rrd and the validation). The lock names the LABELLER -- pid, start time,
  # cap -- not its PowerShell host (round 2): a host killed mid-labelling leaves the labeller running,
  # and that orphan must still read as busy. The start time catches PID reuse. An orphan past its cap
  # + 5 min is one nobody will stop, so it is killed here.
  $lock = Join-Path $adir '.labelling'
  $lk = @(([string](Get-Content $lock -ErrorAction SilentlyContinue | Select-Object -First 1)) -split '\s+' | Where-Object { $_ })
  if ($lk.Count -eq 3) {
    $lp = Get-Process -Id ([int]$lk[0]) -ErrorAction SilentlyContinue
    $lt = if ($lp) { try { $lp.StartTime.ToUniversalTime().Ticks } catch { -1 } } else { -1 }
    if ($lt -eq [int64]$lk[1]) {
      $ageMin = ((Get-Date).ToUniversalTime() - $lp.StartTime.ToUniversalTime()).TotalMinutes
      if ($ageMin -le ([int]$lk[2] + 5)) {
        Write-Host ("  stage: {0} is being labelled by pid {1} ({2:N0} min in) -- skipped (retried later)" -f $RunId, $lk[0], $ageMin) -ForegroundColor DarkGray
        exit 2
      }
      $null = Invoke-Logged 'taskkill.exe' @('/T', '/F', '/PID', $lk[0]) $null
      Write-Host ("  stage: orphaned labeller pid {0} ran {1:N0} min, past its {2} min cap -- killed" -f $lk[0], $ageMin, $lk[2]) -ForegroundColor Yellow
    }
  }
  # Counted only when the labeller is really launched -- a missing GPU or a truncated pull (both cured
  # by waiting) never uses one up -- and capped, so a recording that always breaks the labeller is not
  # re-run forever (review C9).
  $af = Join-Path $adir '.label_attempts'
  $prev = [int]((Get-Content $af -ErrorAction SilentlyContinue | Select-Object -First 1) -as [int])
  if ($prev -ge 3) {
    Write-Host ("  stage: labelling already ran {0} times for {1} without a result -- not retrying (delete {2} to allow it)" -f $prev, $RunId, $af) -ForegroundColor Yellow
    exit 2
  }
  "$($prev + 1)" | Out-File -Encoding ascii $af
  New-Item -ItemType Directory -Force -Path $WeightsDir | Out-Null
  $largv = @($Label, $RrdPath,
             '--out', (Join-Path $adir 'labeled.rrd'),
             '--jsonl', (Join-Path $adir 'obstacles.jsonl'),
             '--summary-json', (Join-Path $adir 'obstacle_summary.json'),
             '--validation-json', $val) + $LabelArgs
  Write-Host ("  stage: labelling {0} on the GPU (attempt {1}, cap {2} min) ..." -f (Split-Path $RrdPath -Leaf), ($prev + 1), $LabelTimeoutMin) -ForegroundColor DarkGray
  $t0 = Get-Date
  $llog = Join-Path $adir '.label.log'
  # Bounded (review C14): a wall-clock cap, and taskkill /T so no python child is left behind. The
  # working directory is $WeightsDir so model weights download there, never into the repo.
  $argline = ($largv | ForEach-Object { '"' + $_ + '"' }) -join ' '
  $p = $null
  try {
    $p = Start-Process -FilePath $Python -ArgumentList $argline -WorkingDirectory $WeightsDir -NoNewWindow -PassThru `
                       -RedirectStandardOutput $llog -RedirectStandardError ($llog + '.err')
    $null = $p.Handle                  # PS 5.1: cache the handle, or ExitCode reads back empty
    ('{0} {1} {2}' -f $p.Id, $p.StartTime.ToUniversalTime().Ticks, $LabelTimeoutMin) | Out-File -Encoding ascii $lock
    $done = $p.WaitForExit([int]$LabelTimeoutMin * 60000)
    if (-not $done) { $null = Invoke-Logged 'taskkill.exe' @('/T', '/F', '/PID', "$($p.Id)") $null }
  } finally {
    # A throw after the launch must not leave the labeller running unowned (round 2).
    if ($p -and -not $p.HasExited) { $null = Invoke-Logged 'taskkill.exe' @('/T', '/F', '/PID', "$($p.Id)") $null }
    Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue
  }
  if (-not $done) {
    Write-Host ("  stage: labelling exceeded {0} min -- killed (retried later)" -f $LabelTimeoutMin) -ForegroundColor Yellow
    exit 2
  }
  if (-not (Test-Path $val)) {
    Write-Host ("  stage: labelling produced no validation (exit {0}) -- see {1}(.err)" -f $p.ExitCode, $llog) -ForegroundColor Yellow
    exit 2
  }
  Write-Host ("  stage: labelled in {0:N0}s" -f ((Get-Date) - $t0).TotalSeconds) -ForegroundColor DarkGray
}

# ---- SUMMARY.txt: the human-readable digest of this run's folder ----
$v = Get-Content $val -Raw | ConvertFrom-Json
$lines = @(('K1 autotune -- {0}   (generated {1})' -f $RunId, (Get-Date -Format 'yyyy-MM-dd HH:mm')),
           ('geometry validation: {0}' -f $v.status))
foreach ($c in $v.checks.PSObject.Properties) {
  $lines += ('  {0,-6} {1}' -f $c.Name, ($c.Value | ConvertTo-Json -Compress))
}
if ($v.floor_calibration) {
  $lines += ('floor calibration: {0}' -f ($v.floor_calibration | ConvertTo-Json -Compress))
}
# Steering invariants BEFORE the per-class list: the robot prints only the first 30 lines (round 2).
$rj = Join-Path $adir 'depth_replay.json'
if (Test-Path $rj) {
  $r = Get-Content $rj -Raw | ConvertFrom-Json
  foreach ($i in $r.invariants) { $lines += ('steering invariant "{0}": {1}/{2}' -f $i.name, $i.good, $i.total) }
}
$os = Join-Path $adir 'obstacle_summary.json'
if (Test-Path $os) {
  $o = Get-Content $os -Raw | ConvertFrom-Json
  if ($null -ne $o.unique) {
    $lines += ('obstacles: {0} unique from {1} detections' -f $o.unique, $o.detections)
    foreach ($c in $o.by_class.PSObject.Properties | Sort-Object Value -Descending) {
      $lines += ('  {0,-16} {1}' -f $c.Name, $c.Value)
    }
  } else {
    $lines += ('detections: {0}' -f $o.detections)
  }
}
# UTF-8 WITHOUT a byte-order mark: run_follow.sh prints this file on the robot, and Windows PowerShell
# 5.1's Out-File -Encoding utf8 writes a BOM that showed up as a stray character in the digest.
[IO.File]::WriteAllLines((Join-Path $adir 'SUMMARY.txt'), [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))

if ($v.status -ne 'PASS') {
  Write-Host ("  *** stage: geometry validation {0} for {1} -- labels NOT sent to the robot (see {2}) ***" -f $v.status, $RunId, $val) -ForegroundColor Red
  exit 1
}
Write-Host ("  stage: validation PASS -> {0}" -f $adir) -ForegroundColor Green
$hold = Join-Path $adir '.no_send'
if ($NoSend) { exit 0 }      # held: .no_send was written at the top. Delete it to release the bundle.
if (Test-Path $hold) {
  Write-Host ("  stage: {0} is held (.no_send) -- not sending" -f $RunId) -ForegroundColor DarkGray
  exit 0
}

$mark = Join-Path $adir '.sent_to_robot'
if (Test-Path $mark) { exit 0 }

# ---- never START a send inside an operator session or during a live follow ----
# The loop's idle check can be many minutes old by now (labelling takes 1-90 min), and a send is up to 7
# robot logins. Re-check right here: K1Finder's operator-session mutex (shared contract; same semantics
# as the loop's Test-OperatorSession, a mutex that exists but cannot be opened counts as held), then one
# fail-closed pgrep probe. Busy or unreachable -> deferred (exit 2), not counted as a failed send.
foreach ($n in 'Global\K1-Operator-Session', 'Local\K1-Operator-Session') {
  $om = $null
  $held = try { [System.Threading.Mutex]::TryOpenExisting($n, [ref]$om) } catch [System.UnauthorizedAccessException] { $true } catch { $false }
  if ($held) {
    if ($om) { $om.Dispose() }
    Write-Host ("  stage: K1Finder operator session active -- send of {0} deferred (retried later)" -f $RunId) -ForegroundColor DarkGray
    exit 2
  }
}
$probe = 'if pgrep -f ''follow_person_k1\.py'' >/dev/null; then echo busy; elif [ $? -eq 1 ]; then echo idle; else echo err; fi'
$pl = @(& { $ErrorActionPreference = 'Continue'; & ssh.exe @($SSH_OPTS + @($target, $probe)) 2>$null } | Where-Object { $_ -and $_.Trim() })
if (-not ($pl.Count -gt 0 -and $pl[-1].Trim() -eq 'idle')) {
  Write-Host ("  stage: robot busy (follow live) or unreachable -- send of {0} deferred (retried later)" -f $RunId) -ForegroundColor Yellow
  exit 2
}

# ---- send the SMALL bundle, from this folder, to the robot ----
$rdir = '{0}/{1}' -f $RemoteAutotune, $RunId
$rc = Invoke-Logged 'ssh.exe' ($SSH_OPTS + @($target, ("mkdir -p '{0}'" -f $rdir))) $null
if ($rc -ne 0) {
  Write-Host ("  stage: robot unreachable (ssh exit {0}) -- bundle kept, send retried later" -f $rc) -ForegroundColor Yellow
  exit 2
}
# SUMMARY.txt goes LAST, as SUMMARY.txt.part, and the first failure stops the send. It becomes
# SUMMARY.txt only by an atomic rename inside the same remote command that repoints 'latest', and
# 'latest' only ever selects a folder holding SUMMARY.txt -- so neither a half-sent bundle nor a
# half-written summary can be selected (round 2).
$sa = Join-Path $adir '.send_attempts'
$ok = $true
foreach ($f in @('label_validation.json', 'obstacle_summary.json', 'tune_patch.yaml', 'depth_replay.json', 'SUMMARY.txt')) {
  $src = Join-Path $adir $f
  if (Test-Path $src) {
    $dst = if ($f -eq 'SUMMARY.txt') { 'SUMMARY.txt.part' } else { $f }
    $rc = Invoke-Logged 'scp.exe' ($SSH_OPTS + @($src, ('{0}:{1}/{2}' -f $target, $rdir, $dst))) $null
    if ($rc -ne 0) { $ok = $false; break }
  }
}
# 'latest' -> the NEWEST complete bundle on the robot, not "this run" (review C11): retries arrive out
# of order, so "this run" can move latest BACKWARDS. Then VERIFIED to be a symlink to that bundle: GNU
# ln into a real directory named latest exits 0 without repointing anything. No double quotes on
# purpose: Windows PowerShell 5.1 mangles embedded double quotes passed to native programs.
if ($ok) {
  $lncmd = "cd '" + $RemoteAutotune + "' && mv -f '" + $RunId + "/SUMMARY.txt.part' '" + $RunId + "/SUMMARY.txt' && " +
           'n=$(ls -1d 20*/SUMMARY.txt 2>/dev/null | sort | tail -n 1) && test ${#n} -gt 0 && ln -sfn ${n%/SUMMARY.txt} latest && test -L latest && test $(readlink latest) = ${n%/SUMMARY.txt}'
  $rc = Invoke-Logged 'ssh.exe' ($SSH_OPTS + @($target, $lncmd)) $null
  $ok = ($rc -eq 0)
}
if (-not $ok) {
  # No marker (review C15). The robot answered the mkdir, so this failure is on the robot's side:
  # counted, and the loop backs off 10, 20 ... 60 min between tries (.send_attempts).
  "$([int]((Get-Content $sa -ErrorAction SilentlyContinue | Select-Object -First 1) -as [int]) + 1)" | Out-File -Encoding ascii $sa
  Write-Host ("  stage: send to {0} failed after the robot answered -- retried with back-off" -f $rdir) -ForegroundColor Yellow
  exit 2
}
Remove-Item -LiteralPath $sa -Force -ErrorAction SilentlyContinue
(Get-Date -Format o) | Out-File -Encoding utf8 $mark
Write-Host ("  stage: sent to robot {0}  (latest -> newest complete bundle)" -f $rdir) -ForegroundColor Green

exit 0

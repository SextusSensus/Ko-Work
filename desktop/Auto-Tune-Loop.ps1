<#
  Auto-Tune-Loop.ps1 -- closed-loop tuning agent for the K1 follow.

  WHAT IT DOES
    On a background loop (poll every -PollSec, default 20 s):
      1. Ask the Jetson for the newest run_id under ~/runs/.
      2. If it's a run we haven't processed yet AND its manifest.json exists
         (offload finished cleanly), pull the bundle via Pull-Run.ps1 into
         ..\runs\<run_id>\ on THIS workstation.
      3. Run eval/rerun_tune.py against <run_id>/k1_follow.err with the
         bundle's config/defaults.yaml as the "current" reference. The
         analyser reads the plain stderr file so we don't need rerun-sdk on
         the workstation to close the loop.
      4. Write the JSON report to <run_id>/tune_report.json and the
         suggested YAML patch to <run_id>/tune_patch.yaml.
      5. scp tune_patch.yaml back to the Jetson at
         /home/booster/tune_hints/<run_id>.yaml AND overwrite
         /home/booster/tune_hints/latest.yaml. The launcher's next run
         reads latest.yaml and logs the recommendations at the top of
         k1_follow.err (SAFETY: recommendations are LOGGED, NOT
         auto-applied -- config still has to be edited to take effect).

  WHY IT'S SAFE
    - Pull is workstation-initiated, so nothing runs on the robot's control
      loop during analysis.
    - The patch that lands on the robot is an ADVISORY YAML, not something
      run_follow.sh sources -- the operator sees it, decides whether to
      merge it into a profile. Auto-applying config to a robot that's about
      to walk into people is exactly what CLAUDE.md forbids.
    - Idempotent: an already-processed run_id is skipped on the next poll,
      so a restart of this loop doesn't re-work anything.
    - Never blocks: any step failure (offline Jetson, missing pyyaml,
      analyser error) logs a WARN and the loop moves on.

  USAGE
    .\Auto-Tune-Loop.ps1                    # default: 192.168.9.75, 20s poll
    .\Auto-Tune-Loop.ps1 -Ip 192.168.9.75 -PollSec 15
    .\Auto-Tune-Loop.ps1 -Once              # single-shot: process the
                                             #  latest run and exit
#>
[CmdletBinding()]
param(
  [string]$Ip         = '192.168.9.75',
  [string]$User       = 'booster',
  [int]$PollSec       = 20,
  [switch]$Once,
  [string]$RemoteRuns = '/home/booster/runs',
  [string]$RemoteHints= '/home/booster/tune_hints',
  [string]$LocalRuns  = '',
  [string]$Analyser   = '',
  [string]$PullScript = '',
  [string]$Python     = 'python',
  # The analysis venv pins rerun-sdk 0.23.1 to MATCH THE WRITER on the robot, and it holds
  # the CUDA torch + ultralytics + transformers the labelling stage needs.
  [string]$ReplayPython = '',
  [string]$Replay     = '',
  # The run's .rrd is pulled ONCE and both the depth replay and the GPU labelling run on it
  # here. Nothing heavy runs on the Jetson: a full-recording read is gigabytes of RAM next to
  # the loco daemons. A 2000 s capture is ~1.7 GB, so the cap sits above that.
  [int]$MaxRrdPullMB  = 4096,
  [string]$StageScript = '',
  [string]$AutotuneDir = '',
  [string]$RemoteAutotune = '/home/booster/autotune',
  # scp -l (kbit/s) for the .rrd pull: bounded so a multi-GB transfer cannot saturate the Wi-Fi
  # the operator deadman heartbeat shares (review C6). 40000 kbit/s = 5 MB/s.
  [int]$RrdPullKbps = 40000
)
$ErrorActionPreference = 'Stop'
# Defaults resolved HERE, not in param(): in Windows PowerShell 5.1, $PSScriptRoot is EMPTY while an
# ADVANCED script's param() defaults are evaluated when it is started with `powershell.exe -File`
# (proven by per-factor repro). The service calls this loop in-process, which is why it only ever
# failed as `powershell.exe -File Auto-Tune-Loop.ps1 -Once`, at parameter binding.
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $LocalRuns   ) { $LocalRuns    = Join-Path $here '..\runs' }
if (-not $Analyser    ) { $Analyser     = Join-Path $here '..\eval\rerun_tune.py' }
if (-not $PullScript  ) { $PullScript   = Join-Path $here 'Pull-Run.ps1' }
if (-not $ReplayPython) { $ReplayPython = Join-Path $here '..\.venv-analysis\Scripts\python.exe' }
if (-not $Replay      ) { $Replay       = Join-Path $here '..\eval\depth_replay.py' }
if (-not $StageScript ) { $StageScript  = Join-Path $here 'Autotune-Stage.ps1' }
if (-not $AutotuneDir ) { $AutotuneDir  = Join-Path $here '..\autotune' }

# SINGLE INSTANCE of the loop itself (review U3): the service wrapper's mutex does not cover a manual
# -Once run, which would process the same runs and race on the ledger. A mutex is re-entrant for
# its owning thread, so the wrapper's in-process relaunch after a throw still gets in.
try { $loopMutex = New-Object System.Threading.Mutex($false, 'Global\K1-AutoTune-Loop') }
catch { $loopMutex = New-Object System.Threading.Mutex($false, 'Local\K1-AutoTune-Loop') }
try { $ownLoop = $loopMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $ownLoop = $true }
if (-not $ownLoop) { Write-Host 'another Auto-Tune-Loop is already running -- exiting' -ForegroundColor Yellow; exit 3 }

# Key-only SSH -- K1Finder handles pass auth separately; the loop must never
# prompt.
$SSH_OPTS = @('-o','StrictHostKeyChecking=accept-new','-o','ConnectTimeout=8',
              '-o','ServerAliveInterval=10','-o','BatchMode=yes')
$target = ('{0}@{1}' -f $User, $Ip)
$ScpExe = 'scp.exe'      # the watched .rrd pull runs it as a child process (Receive-RrdPolled)

# Windows PowerShell 5.1 wraps every stderr line of a native command in an ErrorRecord, and with
# $ErrorActionPreference = 'Stop' (above) the FIRST one is a TERMINATING error. The service log
# proved it: "loop THREW: ssh: connect to host ... Connection timed out" is ssh's own stderr, raised
# from the startup mkdir outside any try, so the whole loop died and restarted every ~38 s while the
# robot was off. Every native call therefore runs with a local 'Continue' and is judged by its exit
# code -- an unreachable robot now just means "nothing to do this tick".
function Ssh-Text([string]$cmd) {
  $ErrorActionPreference = 'Continue'
  $argv = $SSH_OPTS + @($target, $cmd)
  & ssh.exe @argv 2>$null
}
function Scp-Up([string]$local, [string]$remote) {
  $ErrorActionPreference = 'Continue'
  $argv = $SSH_OPTS + @($local, ("{0}:{1}" -f $target, $remote))
  & scp.exe @argv 2>$null | Out-Null
  return $LASTEXITCODE
}
function Scp-Down([string]$remote, [string]$local, [int]$LimitKbps = 0) {
  $ErrorActionPreference = 'Continue'
  $argv = $SSH_OPTS
  if ($LimitKbps -gt 0) { $argv = $argv + @('-l', "$LimitKbps") }
  $argv = $argv + @(("{0}:{1}" -f $target, $remote), $local)
  & scp.exe @argv 2>$null | Out-Null
  return $LASTEXITCODE
}
function Test-RobotIdle {
  # 'idle' ONLY on a positive answer: the robot replies AND pgrep reports no follow node (exit 1). A
  # pgrep error (exit 2/3) or an unreachable robot reads as busy -- fail-closed (round 2). Judged on
  # the LAST output line, so a login banner cannot fake an answer. The escaped \. keeps pgrep from
  # matching this command's own shell line.
  $st = @(Ssh-Text 'if pgrep -f ''follow_person_k1\.py'' >/dev/null; then echo busy; elif [ $? -eq 1 ]; then echo idle; else echo err; fi' |
          Where-Object { $_ -and $_.Trim() })
  return ($st.Count -gt 0 -and $st[-1].Trim() -eq 'idle')
}
function Get-Count([string]$file) {
  return [int]((Get-Content $file -ErrorAction SilentlyContinue | Select-Object -First 1) -as [int])
}
function Ensure-AutotuneFolder([string]$rid) {
  # The run's autotune folder is what Retry-PendingSends revisits: leaving it behind hands the run's
  # labelling to the retry (the stage is only ever given a verified recording).
  $ad = Join-Path $AutotuneDir $rid
  New-Item -ItemType Directory -Force -Path $ad | Out-Null
  return $ad
}
function Note-PullFailure([string]$rid) {
  # A pull that FAILED (a deferral is not one): counted, so a pull that can never succeed stops after
  # 3. Delete .pull_attempts in the run's autotune folder to allow more.
  $pf = Join-Path (Ensure-AutotuneFolder $rid) '.pull_attempts'
  $n = (Get-Count $pf) + 1
  "$n" | Out-File -Encoding ascii $pf
  if ($n -ge 3) { Write-Host ("  rrd: {0} failed {1} pulls -- no more automatic tries (delete {2} to allow more)" -f $rid, $n, $pf) -ForegroundColor Yellow }
}
function Invoke-Logged([string]$exe, [string[]]$argv, [string]$log) {
  $ErrorActionPreference = 'Continue'
  if ($log) { & $exe @argv *> $log } else { & $exe @argv *> $null }
  return $LASTEXITCODE
}
function Receive-RrdPolled([string]$remote, [string]$local) {
  # The .rrd pull, WATCHED (round-2 review, reproduced): an idle check before a multi-minute transfer
  # is not enough -- the operator can start a follow mid-pull, and a follow's launcher publishes the
  # previous run seconds before its node starts. So scp runs as a child process and the robot is
  # re-probed every 5 s; the moment a follow is live, or the robot stops answering, the transfer is
  # killed. A bulk pull then shares the Wi-Fi with the deadman heartbeat for at most ~5 s.
  # Returns scp's exit code, or 10 when aborted because the robot went busy.
  $ErrorActionPreference = 'Continue'
  $argv = $SSH_OPTS + @('-l', "$RrdPullKbps", ("{0}:{1}" -f $target, $remote), $local)
  $argline = ($argv | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
  $slog = $local + '.scp.log'
  $p = Start-Process -FilePath $ScpExe -ArgumentList $argline -NoNewWindow -PassThru `
                     -RedirectStandardOutput $slog -RedirectStandardError ($slog + '.err')
  $null = $p.Handle                  # PS 5.1: cache the handle, or ExitCode reads back empty
  try {
    while (-not $p.WaitForExit(5000)) {
      if (-not (Test-RobotIdle)) {
        $null = Invoke-Logged 'taskkill.exe' @('/T', '/F', '/PID', "$($p.Id)") $null
        $null = $p.WaitForExit(10000)
        return 10
      }
    }
    return $p.ExitCode
  } finally {
    if (-not $p.HasExited) { $null = Invoke-Logged 'taskkill.exe' @('/T', '/F', '/PID', "$($p.Id)") $null }
    Remove-Item -LiteralPath $slog, ($slog + '.err') -Force -ErrorAction SilentlyContinue
  }
}
function Get-LocalRrd([string]$dir, [string]$rid) {
  # The manifest records the .rrd name AND its exact byte count, so the pull decision is made
  # from data. Returns the local path, or $null with the reason printed.
  $mf = Join-Path $dir 'manifest.json'
  if (-not (Test-Path $mf)) { return $null }
  $m = Get-Content $mf -Raw | ConvertFrom-Json
  $name = $null; $bytes = 0
  foreach ($f in $m.files) { if ($f.name -like '*.rrd') { $name = $f.name; $bytes = [int64]$f.bytes; break } }
  if (-not $name) {
    Write-Host "  rrd: none in this run's manifest (run had --rerun off?)" -ForegroundColor DarkGray
    return $null
  }
  $local = Join-Path $dir $name
  if ((Test-Path $local) -and ((Get-Item $local).Length -eq $bytes)) { return $local }
  if ($bytes -gt ([int64]$MaxRrdPullMB * 1MB)) {
    Write-Host ("  rrd: {0} is {1:N0} MB, over the {2} MB cap -- replay/labelling skipped" -f $name, ($bytes / 1MB), $MaxRrdPullMB) -ForegroundColor Yellow
    return $null
  }
  # Room for it? Never fill the disk -- and never auto-delete an older recording to make room: the
  # robot rotates its own bundles, so the copy here may be the only one left (review U4). Deferred,
  # not counted as a failure: freeing space cures it.
  try {
    $drv = Get-PSDrive -Name ((Split-Path -Qualifier $local).TrimEnd(':')) -ErrorAction Stop
    if ($drv.Free -lt ($bytes + 10GB)) {
      Write-Host ("  rrd: only {0:N1} GB free -- need {1:N1} GB + 10 GB headroom; not pulling" -f ($drv.Free / 1GB), ($bytes / 1GB)) -ForegroundColor Yellow
      $null = Ensure-AutotuneFolder $rid
      return $null
    }
  } catch { }
  Write-Host ("  rrd: pulling {0} ({1:N0} MB, capped at {2} kbit/s, aborted if a follow starts)" -f $name, ($bytes / 1MB), $RrdPullKbps) -ForegroundColor DarkGray
  $rc = Receive-RrdPolled ("{0}/{1}/{2}" -f $RemoteRuns, $rid, $name) $local
  $got = if (Test-Path $local) { (Get-Item $local).Length } else { -1 }
  if ($rc -eq 0 -and $got -eq $bytes) { return $local }
  # scp leaves a PARTIAL file when the link drops or the pull is aborted (review C1). Remove it so
  # nothing downstream can mistake it for the recording; the next try re-pulls from scratch.
  Remove-Item -LiteralPath $local -Force -ErrorAction SilentlyContinue
  if ($rc -eq 10) {
    Write-Host ("  rrd: a follow started (or the robot stopped answering) -- pull aborted at {0:N0} of {1:N0} bytes, retried when idle" -f [math]::Max($got, 0), $bytes) -ForegroundColor Yellow
    $null = Ensure-AutotuneFolder $rid
    return $null
  }
  Write-Host ("  rrd: pull failed or incomplete (scp exit {0}, {1:N0} of {2:N0} bytes) -- partial removed" -f $rc, [math]::Max($got, 0), $bytes) -ForegroundColor Yellow
  Note-PullFailure $rid
  return $null
}
$script:lastRelabel = [datetime]::MinValue
function Retry-PendingSends {
  # Once per tick, FIRST. Revisits unfinished autotune folders:
  #   * a PASS bundle that never reached the robot (robot off, link dropped) -> send only (<= 3/tick).
  #     Any run-id folder qualifies: a folder labelled by hand WITHOUT -NoSend means "send it";
  #   * no validation yet (pull failed, no GPU, labeller error or timeout) -> re-pull and re-label,
  #     ONLY for runs in the ledger, one run per 10 min, each cause capped at 3 tries by
  #     .pull_attempts / .label_attempts (C9).
  # Never touched: a bundle held with .no_send (C7/C13), a folder not named like a run id. Nothing at
  # all while the robot is busy or unreachable -- ONE probe per tick, not one timeout per bundle (U1).
  if (-not (Test-Path $StageScript) -or -not (Test-Path $AutotuneDir)) { return }
  $cands = @(Get-ChildItem $AutotuneDir -Directory |
             Where-Object { $_.Name -match '^[0-9]{8}T[0-9]{6}Z_' } |
             Where-Object { -not (Test-Path (Join-Path $_.FullName '.sent_to_robot')) -and
                            -not (Test-Path (Join-Path $_.FullName '.no_send')) } |
             Sort-Object Name -Descending)
  if ($cands.Count -eq 0) { return }
  if (-not (Test-RobotIdle)) { return }
  $sends = 0; $relabelled = $false
  foreach ($d in $cands) {
    $vf = Join-Path $d.FullName 'label_validation.json'
    $rrd = $null
    if (Test-Path $vf) {
      # Parsed per candidate: one unreadable validation must not throw out of the whole function and
      # starve every folder behind it. Unreadable = not PASS.
      $st = try { (Get-Content $vf -Raw | ConvertFrom-Json).status } catch { $null }
      if ($st -ne 'PASS') { continue }
      if ($sends -ge 3) { continue }
      $sends++
    } else {
      # Re-labelling is only for runs the loop itself delivered hints for: anything else is a folder
      # made by hand, or a run the main path has not reached yet.
      if (-not $processed.ContainsKey($d.Name)) { continue }
      if ($relabelled -or ((Get-Date) - $script:lastRelabel).TotalMinutes -lt 10) { continue }
      if ((Get-Count (Join-Path $d.FullName '.label_attempts')) -ge 3) { continue }
      if ((Get-Count (Join-Path $d.FullName '.pull_attempts')) -ge 3) { continue }
      $relabelled = $true
      $script:lastRelabel = Get-Date
      $rrd = Get-LocalRrd (Join-Path $LocalRuns $d.Name) $d.Name
      if (-not $rrd) { continue }
    }
    & $StageScript -RunId $d.Name -LocalRunDir (Join-Path $LocalRuns $d.Name) -RrdPath $rrd -AutotuneDir $AutotuneDir `
                   -Python $ReplayPython -User $User -Ip $Ip -RemoteAutotune $RemoteAutotune | Out-Host
  }
}

if (-not (Test-Path $Analyser)) { throw "analyser not found: $Analyser" }
if (-not (Test-Path $PullScript)) { throw "Pull-Run.ps1 not found: $PullScript" }
if (-not (Test-Path $LocalRuns)) { New-Item -ItemType Directory -Force -Path $LocalRuns | Out-Null }
# Provider-aware: [IO.Path]::GetFullPath resolves against the PROCESS directory, not $PWD (review C5).
$LocalRuns = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($LocalRuns)
$Analyser  = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Analyser)
$PullScript= $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($PullScript)

# Idempotency ledger: a run_id we've already processed doesn't get redone on
# the next tick. We persist it so a restart of this loop stays sane.
$ledgerPath = Join-Path $LocalRuns '.auto_tune_processed.txt'
$processed = @{}
if (Test-Path $ledgerPath) {
  Get-Content $ledgerPath | ForEach-Object { if ($_) { $processed[$_.Trim()] = $true } }
}

# Ensure the remote hints dir exists (silent create -- best effort).
Ssh-Text ("mkdir -p '{0}'" -f $RemoteHints) | Out-Null

Write-Host ("Auto-Tune-Loop -> {0}   poll {1}s   local runs -> {2}" -f $target, $PollSec, $LocalRuns) -ForegroundColor Cyan
Write-Host ("Analyser: {0}" -f $Analyser) -ForegroundColor DarkGray
Write-Host ("Hints dir on robot: {0}   (advisory only; not auto-applied)" -f $RemoteHints) -ForegroundColor DarkGray
Write-Host ""

$stopReq = $false
$null = Register-EngineEvent PowerShell.Exiting -SupportEvent -Action { $script:stopReq = $true }

while (-not $stopReq) {
  try {
    # 0) unfinished autotune folders -- once per tick and FIRST, so a newest run that keeps failing
    # further down can never starve them (review U2).
    if (-not $Once) {
      try { Retry-PendingSends } catch { Write-Host ("  retry-send: {0}" -f $_.Exception.Message) -ForegroundColor Yellow }
    }

    # 1) discover: newest run_id whose manifest.json exists (offload finished)
    $listCmd = ("ls -1 '{0}' 2>/dev/null | sort" -f $RemoteRuns)
    $runs = @((Ssh-Text $listCmd) | Where-Object { $_ -match '^[0-9]{8}T[0-9]{6}Z_' })
    $chosen = $null
    for ($i = $runs.Count - 1; $i -ge 0; $i--) {
      $rid = $runs[$i].Trim()
      if ($processed.ContainsKey($rid)) { continue }
      # Skip incomplete offloads: no manifest.json means offload hadn't finished
      $check = Ssh-Text ("test -f '{0}/{1}/manifest.json' && echo yes" -f $RemoteRuns, $rid)
      if ($check -match 'yes') { $chosen = $rid; break }
    }
    if (-not $chosen) {
      if ($Once) { Write-Host "no new completed run to process." -ForegroundColor DarkYellow; break }
      Start-Sleep -Seconds $PollSec
      continue
    }

    # 1b) never work on a run while the follow node is live (review C6): the .rrd pull below is
    # multi-GB on the same Wi-Fi as the operator deadman heartbeat. Unreachable also defers.
    if (-not (Test-RobotIdle)) {
      Write-Host ("  robot busy (follow node live) or unreachable -- deferring {0}" -f $chosen) -ForegroundColor DarkGray
      if ($Once) { break }
      Start-Sleep -Seconds $PollSec
      continue
    }

    Write-Host ("`n[{0}Z] processing {1}" -f (Get-Date).ToUniversalTime().ToString('HH:mm:ss'), $chosen) -ForegroundColor Green

    # 2) pull the bundle to <LocalRuns>\<run_id>\  via key-auth scp
    # (avoids Pull-Run.ps1's K1PW requirement, which was retired when the robot
    # went key-only.) We only need the small text artefacts for the analyser --
    # NOT the .rrd, which is 100-200 MB and never inspected here.
    $localDir = Join-Path $LocalRuns $chosen
    if (-not (Test-Path $localDir)) { New-Item -ItemType Directory -Force -Path $localDir | Out-Null }
    $remoteDir = "{0}/{1}" -f $RemoteRuns, $chosen
    foreach ($f in @('k1_follow.err','manifest.json','config/defaults.yaml')) {
      $localPath = Join-Path $localDir $f
      $parentDir = Split-Path -Parent $localPath
      if (-not (Test-Path $parentDir)) { New-Item -ItemType Directory -Force -Path $parentDir | Out-Null }
      $null = Scp-Down ("{0}/{1}" -f $remoteDir, $f) $localPath   # local 'Continue' (review C4)
    }
    if (-not (Test-Path (Join-Path $localDir 'k1_follow.err'))) {
      # Check if the source file even exists on the robot -- a bundle without k1_follow.err
      # on the robot side is a permanent condition (crashed offload, wrong bundle shape), so
      # mark it processed to stop the loop from retrying forever. A pull that fails for a
      # TRANSIENT reason (ssh timeout) will re-fire cleanly on the next tick because
      # ledger-mark below only lands if the source-check confirms permanent absence.
      $exists = Ssh-Text ("test -f '{0}/{1}/k1_follow.err' && echo yes" -f $RemoteRuns, $chosen)
      if ($exists -match 'yes') {
        Write-Host ("  WARN: pull incomplete for {0} (transient) -- will retry next tick" -f $chosen) -ForegroundColor Yellow
      } else {
        Write-Host ("  SKIP: {0} has no k1_follow.err on robot -- marking processed" -f $chosen) -ForegroundColor DarkYellow
        $processed[$chosen] = $true
        $chosen | Out-File -Append -Encoding utf8 $ledgerPath
      }
      Start-Sleep -Seconds $PollSec
      continue
    }

    # 3) analyse
    $err = Join-Path $localDir 'k1_follow.err'
    $cfg = Join-Path $localDir 'config\defaults.yaml'
    if (-not (Test-Path $cfg)) { $cfg = Join-Path $localDir 'config/defaults.yaml' }
    $out = Join-Path $localDir 'tune_report.json'
    $pat = Join-Path $localDir 'tune_patch.yaml'
    $an  = @($Analyser, '--log', $err, '--out', $out, '--emit-patch', $pat)
    if (Test-Path $cfg) { $an += @('--cfg', $cfg) }
    $null = Invoke-Logged $Python $an (Join-Path $localDir '.analyser.log')   # review C4
    if (-not (Test-Path $out)) {
      Write-Host ("  WARN: analyser produced no report for {0}" -f $chosen) -ForegroundColor Yellow
      # Do NOT mark processed; a partial analyser hiccup should not lock the run out.
      Start-Sleep -Seconds $PollSec
      continue
    }

    # 3b) the run's .rrd, pulled ONCE for both stages below (never processed on the Jetson).
    $localRrd = $null
    try { $localRrd = Get-LocalRrd $localDir $chosen } catch { Write-Host ("  rrd: {0}" -f $_.Exception.Message) -ForegroundColor Yellow }

    # 3c) DEPTH REPLAY -- the steering invariants on the recorded depth. The regression net for the
    # class of bug thresholds cannot express: the gap-steer sign inversion (fc88b19) made the robot
    # steer AWAY from its chosen gap on every run since August. Fail-soft: the advisory hints below
    # are the loop's primary job and must never be blocked by a replay problem.
    try {
      if ($localRrd -and (Test-Path $Replay) -and (Test-Path $ReplayPython)) {
        $rj = Join-Path $localDir 'depth_replay.json'
        $rlog = Join-Path $localDir '.replay.log'
        $null = Invoke-Logged $ReplayPython @($Replay, '--rrd', $localRrd, '--json', $rj) $rlog
        if (Test-Path $rj) {
          $r = Get-Content $rj -Raw | ConvertFrom-Json
          $bad = @($r.invariants | Where-Object { $_.total -gt 0 -and $_.good -lt $_.total })
          Write-Host ("  replay: {0} frames, {1} scored, {2} steers" -f $r.frames, $r.scored, $r.steers) -ForegroundColor DarkGray
          if ($bad.Count -gt 0) {
            # A failed invariant is a DEFECT IN THE SHIPPED CODE, not a tuning hint -- printed in
            # red and never folded into the advisory YAML.
            Write-Host "  *** INVARIANT VIOLATION -- the steering law is WRONG, not mistuned ***" -ForegroundColor Red
            foreach ($b in $bad) { Write-Host ("      {0}: {1}/{2}   rule: {3}" -f $b.name, $b.good, $b.total, $b.rule) -ForegroundColor Red }
          } else {
            Write-Host "  replay: all steering invariants PASS" -ForegroundColor Green
          }
        } else {
          Write-Host ("  replay: produced no scorecard -- see {0}" -f $rlog) -ForegroundColor Yellow
        }
      }
    } catch { Write-Host ("  replay: skipped ({0})" -f $_.Exception.Message) -ForegroundColor Yellow }

    # 4) push the patch back as ADVISORY hints -- BEFORE the labelling, which can take many minutes
    # (review C14): the hints are the loop's primary job, and a late push is what makes run_follow.sh
    # report TUNE-STALE.
    $src = $pat
    if (-not (Test-Path $pat)) {
      # Analyser had nothing to recommend -- write an empty file so the launcher's
      # "there are hints" check reflects "yes we processed, nothing to change".
      $src = Join-Path $localDir '.empty_hints.yaml'
      "# No tuning recommendations from $chosen`n" | Out-File -Encoding utf8 $src
    }
    # mkdir on demand: the one at startup is lost whenever the robot was off then (round 2).
    $null = Ssh-Text ("mkdir -p '{0}'" -f $RemoteHints)
    $ok1 = Scp-Up $src ("{0}/{1}.yaml" -f $RemoteHints, $chosen)
    # latest.yaml must never move BACKWARDS (round 2): a backlog is processed newest-first, so an older
    # run's hints go to <run>.yaml only and latest.yaml keeps the newest processed run's.
    $newer = @($processed.Keys | Where-Object { [string]::CompareOrdinal([string]$_, $chosen) -gt 0 })
    if ($newer.Count -eq 0) {
      $ok2 = Scp-Up $src ("{0}/latest.yaml" -f $RemoteHints)
    } else {
      $ok2 = 0
      Write-Host ("  hints: {0} is older than {1} processed run(s) -- latest.yaml stays on the newest" -f $chosen, $newer.Count) -ForegroundColor DarkGray
    }
    if ($ok1 -eq 0 -and $ok2 -eq 0) {
      Write-Host ("  OK: report -> {0}   patch -> {1}   hints on robot -> {2}/{3}.yaml (advisory)" -f `
                    $out, $pat, $RemoteHints, $chosen) -ForegroundColor Green
      $processed[$chosen] = $true
      $chosen | Out-File -Append -Encoding utf8 $ledgerPath
    } else {
      Write-Host ("  WARN: scp back failed (rc1={0} rc2={1}) -- will retry next tick" -f $ok1, $ok2) -ForegroundColor Yellow
    }

    # 3d) SEMANTIC LABELLING -> GEOMETRY VALIDATION -> runtime\autotune\<run_id>\ -> robot.
    # Autotune-Stage.ps1 labels the recording on the GPU (objects + walls/floor), gates it on the
    # geometry check, writes the viewable folder, and sends the small bundle to the robot FROM that
    # folder -- only on PASS. Given ONLY a size-verified recording (review C1); a failed pull already
    # left the run's folder for Retry-PendingSends, which also retries a failed labelling.
    try {
      if ($localRrd -and (Test-Path $StageScript)) {
        & $StageScript -RunId $chosen -LocalRunDir $localDir -RrdPath $localRrd -AutotuneDir $AutotuneDir `
                       -Python $ReplayPython -User $User -Ip $Ip -RemoteAutotune $RemoteAutotune | Out-Host
      }
    } catch { Write-Host ("  stage: skipped ({0})" -f $_.Exception.Message) -ForegroundColor Yellow }
  } catch {
    Write-Host ("ERR loop tick: {0}" -f $_.Exception.Message) -ForegroundColor Red
  }

  if ($Once) { break }
  Start-Sleep -Seconds $PollSec
}
# Release the single-instance mutex: an interactive -Once leaves its console thread alive, and an
# unreleased mutex would lock the service's loop out until that window closed (round 2).
try { $loopMutex.ReleaseMutex() } catch { }
Write-Host "`nAuto-Tune-Loop exiting." -ForegroundColor Cyan

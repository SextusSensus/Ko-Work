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
  [string]$LocalRuns  = (Join-Path $PSScriptRoot '..\runs'),
  [string]$Analyser   = (Join-Path $PSScriptRoot '..\eval\rerun_tune.py'),
  [string]$PullScript = (Join-Path $PSScriptRoot 'Pull-Run.ps1'),
  [string]$Python     = 'python'
)
$ErrorActionPreference = 'Stop'

# Key-only SSH -- K1Finder handles pass auth separately; the loop must never
# prompt.
$SSH_OPTS = @('-o','StrictHostKeyChecking=accept-new','-o','ConnectTimeout=8',
              '-o','ServerAliveInterval=10','-o','BatchMode=yes')
$target = ('{0}@{1}' -f $User, $Ip)

function Ssh-Text([string]$cmd) {
  $args = $SSH_OPTS + @($target, $cmd)
  & ssh.exe @args 2>$null
}
function Scp-Up([string]$local, [string]$remote) {
  $args = $SSH_OPTS + @($local, ("{0}:{1}" -f $target, $remote))
  & scp.exe @args 2>$null
  return $LASTEXITCODE
}

if (-not (Test-Path $Analyser)) { throw "analyser not found: $Analyser" }
if (-not (Test-Path $PullScript)) { throw "Pull-Run.ps1 not found: $PullScript" }
if (-not (Test-Path $LocalRuns)) { New-Item -ItemType Directory -Force -Path $LocalRuns | Out-Null }
$LocalRuns = [System.IO.Path]::GetFullPath($LocalRuns)
$Analyser  = [System.IO.Path]::GetFullPath($Analyser)
$PullScript= [System.IO.Path]::GetFullPath($PullScript)

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
      $args = $SSH_OPTS + @(("{0}:{1}/{2}" -f $target, $remoteDir, $f), $localPath)
      & scp.exe @args 2>$null | Out-Null
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
    & $Python @an *> (Join-Path $localDir '.analyser.log')
    if (-not (Test-Path $out)) {
      Write-Host ("  WARN: analyser produced no report for {0}" -f $chosen) -ForegroundColor Yellow
      # Do NOT mark processed; a partial analyser hiccup should not lock the run out.
      Start-Sleep -Seconds $PollSec
      continue
    }

    # 4) push patch back as ADVISORY hints on the robot
    $ok1 = 1; $ok2 = 1
    if (Test-Path $pat) {
      $ok1 = Scp-Up $pat ("{0}/{1}.yaml" -f $RemoteHints, $chosen)
      $ok2 = Scp-Up $pat ("{0}/latest.yaml" -f $RemoteHints)
    } else {
      # Analyser had nothing to recommend -- write an empty file so the launcher's
      # "there are hints" check reflects "yes we processed, nothing to change".
      $empty = Join-Path $localDir '.empty_hints.yaml'
      "# No tuning recommendations from $chosen`n" | Out-File -Encoding utf8 $empty
      $ok1 = Scp-Up $empty ("{0}/{1}.yaml" -f $RemoteHints, $chosen)
      $ok2 = Scp-Up $empty ("{0}/latest.yaml" -f $RemoteHints)
    }
    if ($ok1 -eq 0 -and $ok2 -eq 0) {
      Write-Host ("  OK: report -> {0}   patch -> {1}   hints on robot -> {2}/{3}.yaml (advisory)" -f `
                    $out, $pat, $RemoteHints, $chosen) -ForegroundColor Green
      $processed[$chosen] = $true
      $chosen | Out-File -Append -Encoding utf8 $ledgerPath
    } else {
      Write-Host ("  WARN: scp back failed (rc1={0} rc2={1}) -- will retry next tick" -f $ok1, $ok2) -ForegroundColor Yellow
    }
  } catch {
    Write-Host ("ERR loop tick: {0}" -f $_.Exception.Message) -ForegroundColor Red
  }

  if ($Once) { break }
  Start-Sleep -Seconds $PollSec
}
Write-Host "`nAuto-Tune-Loop exiting." -ForegroundColor Cyan

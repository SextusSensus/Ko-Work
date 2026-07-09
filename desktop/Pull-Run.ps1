<#
  Pull-Run.ps1  (P6.2, workstation side)

  Pull a finished run bundle from the Jetson (assembled by robot/offload_run.sh) down to a local
  runs/ store on THIS workstation, over the same Windows-OpenSSH channel the app already uses
  (ssh.exe/scp.exe, password via askpass). No rsync, no Python required.

  Resumable / kill-test safe: the manifest.json is fetched first, then each file is fetched ONLY if
  it is missing locally or its SHA-256 does not match the manifest. Re-running after an interrupted
  pull therefore transfers just the missing/corrupt files and converges -- nothing is re-copied
  needlessly and a half-pulled bundle is never mistaken for complete.

  Examples:
    .\Pull-Run.ps1 -List                 # list run bundles present on the Jetson
    .\Pull-Run.ps1                        # pull the latest run to ..\runs\<run_id>\
    .\Pull-Run.ps1 -RunId 20260708T2130Z_ab12cd34
#>
[CmdletBinding()]
param(
  [string]$Ip         = '192.168.1.81',
  [string]$User       = 'booster',
  [string]$Pass       = '123456',
  [string]$RunId      = '',                                   # empty => latest on the Jetson
  [string]$RemoteRuns = '/home/booster/runs',
  [string]$LocalRuns  = (Join-Path $PSScriptRoot '..\runs'),
  [switch]$List
)

$ErrorActionPreference = 'Stop'

# ---- SSH password via askpass (same non-interactive pattern as K1Finder.ps1) ----
$askpass = Join-Path $env:TEMP 'k1_pull_askpass.cmd'
Set-Content -Path $askpass -Value "@echo off`r`necho $Pass" -Encoding ASCII
$env:SSH_ASKPASS         = $askpass
$env:SSH_ASKPASS_REQUIRE = 'force'
$env:DISPLAY             = 'localhost:0.0'
$SSH_OPTS = @('-o','StrictHostKeyChecking=accept-new','-o','PreferredAuthentications=password',
              '-o','PubkeyAuthentication=no','-o','ConnectTimeout=8','-o','ServerAliveInterval=5')
$target = ('{0}@{1}' -f $User, $Ip)

function Invoke-SshCapture {
  # Run a remote command, return stdout as string[] (throws on non-zero exit).
  param([string]$RemoteCmd, [int]$TimeoutMs = 20000)
  $out = New-TemporaryFile
  try {
    $p = Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @($target, $RemoteCmd)) `
         -NoNewWindow -PassThru -RedirectStandardOutput $out
    try { $null = $p.Handle } catch {}
    if (-not $p.WaitForExit($TimeoutMs)) { try { $p.Kill() } catch {}; throw "ssh timed out: $RemoteCmd" }
    if ($p.ExitCode -ne 0) { throw "ssh exit $($p.ExitCode): $RemoteCmd" }
    return @(Get-Content -LiteralPath $out)
  } finally { Remove-Item -LiteralPath $out -ErrorAction SilentlyContinue }
}

function Invoke-Scp {
  # Copy one remote file to a local path (throws on failure). Ensures the local dir exists.
  param([string]$RemoteRel, [string]$LocalPath)
  $dir = Split-Path -Parent $LocalPath
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  $remote = ('{0}:{1}/{2}/{3}' -f $target, $RemoteRuns, $RunId, $RemoteRel)
  $p = Start-Process scp.exe -ArgumentList ($SSH_OPTS + @($remote, $LocalPath)) -NoNewWindow -PassThru
  try { $null = $p.Handle } catch {}
  if (-not $p.WaitForExit(120000)) { try { $p.Kill() } catch {}; throw "scp timed out: $RemoteRel" }
  if ($p.ExitCode -ne 0) { throw "scp exit $($p.ExitCode): $RemoteRel" }
}

function Get-Sha256Lower { param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) { return $null }
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
}

# ---- list remote bundles (newest first) ----
# basename of each runs/*/ dir; ignore .partial (hidden) staging dirs.
$remoteRuns = @(Invoke-SshCapture "ls -1dt $RemoteRuns/*/ 2>/dev/null | xargs -n1 basename 2>/dev/null") |
              Where-Object { $_ -and ($_ -notlike '.*') }

if ($List) {
  if (-not $remoteRuns) { Write-Host "no run bundles under $RemoteRuns on $Ip" }
  else { Write-Host "run bundles on $Ip ($RemoteRuns), newest first:"; $remoteRuns | ForEach-Object { "  $_" } }
  return
}

if (-not $RunId) {
  if (-not $remoteRuns) { throw "no run bundles under $RemoteRuns on $Ip (nothing to pull)" }
  $RunId = $remoteRuns[0]
  Write-Host "pulling latest: $RunId"
} elseif ($remoteRuns -notcontains $RunId) {
  throw "run '$RunId' not found on $Ip (have: $($remoteRuns -join ', '))"
}

# Resolve LocalRuns to a full path whether or not it exists yet (GetFullPath is anchored on the
# process CWD for a relative default, so make the default explicit relative to this script).
if (-not [System.IO.Path]::IsPathRooted($LocalRuns)) {
  $LocalRuns = Join-Path $PSScriptRoot '..\runs'
}
$LocalRuns = [System.IO.Path]::GetFullPath($LocalRuns)
$localDir  = Join-Path $LocalRuns $RunId
New-Item -ItemType Directory -Force -Path $localDir | Out-Null

# ---- fetch manifest first, then verify-or-fetch each file (resumable) ----
$manLocal = Join-Path $localDir 'manifest.json'
Invoke-Scp -RemoteRel 'manifest.json' -LocalPath $manLocal
$man = Get-Content -LiteralPath $manLocal -Raw | ConvertFrom-Json

$fetched = 0; $skipped = 0; $bad = @()
foreach ($f in $man.files) {
  $localPath = Join-Path $localDir ($f.name -replace '/', '\')
  if ((Get-Sha256Lower $localPath) -eq $f.sha256) { $skipped++; continue }
  Write-Host ("  fetch {0} ({1:n0} bytes)" -f $f.name, $f.bytes)
  Invoke-Scp -RemoteRel $f.name -LocalPath $localPath
  if ((Get-Sha256Lower $localPath) -ne $f.sha256) { $bad += $f.name } else { $fetched++ }
}

Write-Host ("run $RunId -> $localDir  (fetched=$fetched skipped=$skipped of $($man.file_count))")
if ($bad.Count) {
  Write-Host "PULL-INCOMPLETE: hash mismatch after fetch -> re-run to retry: $($bad -join ', ')"
  exit 1
}

# P6.2: mark the bundle VERIFIED on the Jetson so offload_run.sh's retention may prune it. Retention
# NEVER prunes an un-verified bundle, so a failure here just keeps the run on the robot (safe) -- the
# whole bundle was already hash-verified locally above, so the data is preserved regardless.
try {
  Invoke-SshCapture "touch $RemoteRuns/$RunId/.verified" | Out-Null
  Write-Host "marked verified on robot -> $RunId (retention may now prune it)"
} catch {
  Write-Host "note: could not mark verified on robot ($_) -- bundle stays RETAINED on the robot (safe)."
}
Write-Host "PULL-OK $RunId profile=$($man.profile) duration_s=$($man.duration_s)"

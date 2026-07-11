<#
  Offload-Run.ps1  (P6.2b)

  Bundle the just-finished follow run on the Jetson (offload_run.sh), then pull it to the local runs\
  store (Pull-Run.ps1). Designed to be launched DETACHED (fire-and-forget) by K1Finder.ps1 at clean
  session end, and also runnable by hand. Never interactive; failures are its own (the app is never
  blocked). Uses the same Windows-OpenSSH + askpass pattern as the app.
#>
[CmdletBinding()]
param(
  [string]$Ip      = '192.168.1.81',
  [string]$User    = 'booster',
  [string]$Pass    = '123456',
  [string]$Profile = 'capture',
  [int]$Keep       = 10
)
$ErrorActionPreference = 'Continue'

$askpass = Join-Path $env:TEMP 'k1_offload_askpass.cmd'
Set-Content -Path $askpass -Value "@echo off`r`necho $Pass" -Encoding ASCII
$env:SSH_ASKPASS = $askpass
$env:SSH_ASKPASS_REQUIRE = 'force'
$env:DISPLAY = 'localhost:0.0'
$opts = @('-o','StrictHostKeyChecking=accept-new','-o','PreferredAuthentications=password',
          '-o','PubkeyAuthentication=no','-o','ConnectTimeout=8','-o','ServerAliveInterval=5')
$target = "$User@$Ip"

# 1) Jetson-side: assemble the bundle + manifest + retention (post-session; never touches the loop).
& ssh.exe @opts $target "bash /home/booster/offload_run.sh --profile $Profile --keep $Keep"

# 2) Workstation-side: pull the latest bundle down (hash-verified, resumable).
$pull = Join-Path $PSScriptRoot 'Pull-Run.ps1'
if (Test-Path $pull) {
  & $pull -Ip $Ip -User $User -Pass $Pass
} else {
  Write-Host "Offload-Run: Pull-Run.ps1 not found beside this script -- bundle assembled on robot but not pulled."
}

# 3) Workstation-side: auto-label the pulled bundle(s) so batch_ingest can include the 'pass' runs
# (P6.4). Best-effort: needs a laptop python with rerun (the anaconda 'train' env this project uses);
# absent -> the bundle is still pulled, just label it later with `Runs.ps1 label`. label_run --all is
# idempotent per SCORER_VERSION, so re-runs only score new/unlabeled bundles. The labeler reads each
# run's true standoff/geofence from the .rrd's own static refs, so app runs (CLI standoff) score right.
$repoRoot = Split-Path $PSScriptRoot -Parent
$labeler  = Join-Path $repoRoot 'eval\label_run.py'
$runsDir  = Join-Path $repoRoot 'runs'
$pyCand   = @(
  'C:\Users\toddm\anaconda3\envs\train\python.exe',
  (Join-Path $env:USERPROFILE 'anaconda3\envs\train\python.exe'),
  (Join-Path $env:USERPROFILE 'miniconda3\envs\train\python.exe')
)
$py = $pyCand | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($py -and (Test-Path $labeler)) {
  $env:KMP_DUPLICATE_LIB_OK = 'TRUE'
  Write-Host "Offload-Run: labeling pulled runs (label_run --all) ..."
  & $py $labeler --runs-dir $runsDir --all 2>&1 | Where-Object { $_ -match '^label:' }
} else {
  Write-Host "Offload-Run: no train-env python / label_run.py -> bundle pulled but NOT labeled (run Runs.ps1 label)."
}

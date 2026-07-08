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

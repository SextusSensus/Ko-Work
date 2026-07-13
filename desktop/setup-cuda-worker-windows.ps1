<#
  setup-cuda-worker-windows.ps1  (P6G.1 -- Windows-elevated half; docs/WSL2_SUBSTRATE.md)

  Run in an **ELEVATED** PowerShell on the DESKTOP. Provisions the Windows side of the CUDA worker:
  .wslconfig (48 GB cap + mirrored networking), the 2222 firewall opening, a logon task that boots WSL,
  and the no-sleep-on-AC guarantee. The in-distro half is desktop/setup-cuda-worker-distro.sh (run it
  with sudo INSIDE Ubuntu-24.04 AFTER this script's `wsl --shutdown`).

  As-built for this box (observed 2026-07-13): Ubuntu-24.04, WSL 2.4.11, RTX 3080 driver 591.44,
  no-sleep already set, networking currently NAT (this switches it to mirrored). Docker Desktop is
  installed -- the distro half installs NATIVE docker-ce; disable Docker Desktop's WSL integration for
  Ubuntu-24.04 (Docker Desktop -> Settings -> Resources -> WSL Integration -> untick Ubuntu-24.04) so the
  native daemon/socket wins, or the two dockers fight over the PATH.

  NOT auto-run: Claude can't elevate non-interactively. Read it, then run it yourself.
#>
[CmdletBinding()]
param([string]$Distro = "Ubuntu-24.04", [int]$MemoryGB = 48, [int]$Port = 2222)
$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
      ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    throw "Run this in an ELEVATED PowerShell (Right-click -> Run as administrator)."
}

Write-Host "== 1. .wslconfig: cap RAM at ${MemoryGB}GB + mirrored networking =="
@"
[wsl2]
memory=${MemoryGB}GB
networkingMode=mirrored
"@ | Out-File "$env:USERPROFILE\.wslconfig" -Encoding ascii
Write-Host "  wrote $env:USERPROFILE\.wslconfig"

Write-Host "== 2. No sleep on AC (a suspended host kills detached GPU jobs) =="
powercfg /change standby-timeout-ac 0

Write-Host "== 3. Firewall: allow inbound TCP $Port (sshd) =="
if (-not (Get-NetFirewallRule -DisplayName "WSL2 k1 sshd $Port" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "WSL2 k1 sshd $Port" -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $Port | Out-Null
    Write-Host "  added rule 'WSL2 k1 sshd $Port'"
} else { Write-Host "  rule already present" }

Write-Host "== 4. Logon task: boot WSL so sshd is up after a reboot =="
schtasks /Create /TN "WSL-K1-Boot" /TR "wsl.exe -d $Distro -e /bin/true" /SC ONLOGON /RL HIGHEST /F | Out-Null

Write-Host "== 5. Restart the WSL VM so .wslconfig takes effect =="
wsl --shutdown
Start-Sleep -Seconds 3
wsl -d $Distro -e /bin/true   # boot it back

Write-Host ""
Write-Host "WINDOWS-HALF-OK. Next:"
Write-Host "  a) Confirm mirrored networking took:  wsl -d $Distro -- wslinfo --networking-mode   (expect 'mirrored')"
Write-Host "     If it prints 'nat', run 'wsl --update' and re-run this script; if still 'nat', see"
Write-Host "     WSL2_SUBSTRATE.md Branch B (netsh portproxy)."
Write-Host "  b) Disable Docker Desktop WSL integration for $Distro (Settings->Resources->WSL Integration)."
Write-Host "  c) Inside the distro, run the distro half with sudo:"
Write-Host "       wsl -d $Distro"
Write-Host "       sudo bash /mnt/c/Users/$env:USERNAME/OneDrive/Desktop/runtime/Ko-Work-main/desktop/setup-cuda-worker-distro.sh"
Write-Host "  d) Mirrored firewall gotcha: if the laptop's :$Port test times out with sshd provably up,"
Write-Host "     run (elevated):  Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow"

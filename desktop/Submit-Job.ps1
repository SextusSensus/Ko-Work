<#
  Submit-Job.ps1  (LAN job-pool -- laptop-side submit; docs/CLUSTER_PLAN.md)

  The laptop has no Python, so it enqueues a job by ssh-ing to the COORDINATOR (the Python node that
  hosts the scheduler + queue -- typically the desktop/WSL) and running `python -m cluster.submit`
  there. Mirrors Pull-Run.ps1's Windows-OpenSSH idiom; KEY auth to the coordinator's sshd (port 2222),
  no password/askpass. Windows PowerShell 5.1 compatible.

  Examples:
    .\Submit-Job.ps1 -Host desktop.lan -Type recon -Image k1recon:dev -Inputs 20260709T101010-abc123
    .\Submit-Job.ps1 -Host desktop.lan -Type train -Image k1train:dev -Requires 'backend=cuda','min_vram_gb=8' -Priority 50
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$HostName,
  [Parameter(Mandatory = $true)][ValidateSet('ingest','recon','train','splat')][string]$Type,
  [Parameter(Mandatory = $true)][string]$Image,
  [string[]]$Inputs = @(),
  [string[]]$Requires = @('backend=cpu'),
  [string[]]$Arg = @(),
  [int]$Priority = 100,
  [int]$Seed = 0,
  [int]$Port = 2222,
  [string]$User = 'k1',
  [string]$RepoDir = '~/k1/Ko-Work',            # the coordinator's repo checkout (holds cluster/)
  [string]$QueueDir = '~/k1/queue',             # the filesystem queue on the coordinator
  [string]$IdentityFile = "$env:USERPROFILE\.ssh\id_k1",
  [string]$Python = 'python3'
)
$ErrorActionPreference = 'Stop'

$sshOpts = @('-p', "$Port", '-o', 'StrictHostKeyChecking=accept-new', '-o', 'BatchMode=yes',
             '-o', 'ConnectTimeout=8')
if (Test-Path -LiteralPath $IdentityFile) { $sshOpts += @('-i', $IdentityFile) }

# Build the remote submit command. Each arg is single-quoted for the remote shell (values here are
# repo-controlled enums / run_ids / key=value, not free text -- but quote anyway for safety).
function Q([string]$s) { "'" + ($s -replace "'", "'\''") + "'" }
$parts = @("cd $(Q $RepoDir)", '&&', $Python, '-m', 'cluster.submit',
           '--queue', (Q $QueueDir), '--type', (Q $Type), '--image', (Q $Image),
           '--priority', "$Priority", '--seed', "$Seed")
if ($Inputs.Count)   { $parts += '--inputs';   foreach ($i in $Inputs)   { $parts += (Q $i) } }
if ($Requires.Count) { $parts += '--requires'; foreach ($r in $Requires) { $parts += (Q $r) } }
foreach ($a in $Arg) { $parts += @('--arg', (Q $a)) }
$remote = ($parts -join ' ')

$target = "$User@$HostName"
Write-Host "submit -> $target : $Type/$Image  requires=$($Requires -join ',')  inputs=$($Inputs -join ',')"
$out = & ssh.exe @sshOpts $target $remote 2>&1
$rc = $LASTEXITCODE
$out | ForEach-Object { Write-Host $_ }
if ($rc -ne 0) {
  Write-Host "SUBMIT-FAILED (ssh exit $rc) -- is the coordinator reachable on ${HostName}:${Port} with key $IdentityFile, and is $RepoDir a repo checkout with cluster/?"
  exit 1
}
Write-Host "SUBMIT-OK"

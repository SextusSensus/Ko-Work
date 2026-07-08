<#
  Sync-Runs.ps1  (P6/P7 data flow, laptop -> desktop leg)

  Mirror the local runs\ store (where Pull-Run.ps1 lands Jetson bundles) to the DESKTOP workstation,
  where P7 ingest/training and P8 reconstruction run. Windows-native: robocopy over a UNC share or a
  mapped drive -- no rsync, no Python. Incremental + restartable (/Z), so large .rrd re-copies resume.

  The desktop must expose the destination as a filesystem path: a share (\\DESKTOP-PC\k1\runs) or a
  mapped drive (Z:\k1\runs). (If your desktop is reachable only over SSH/OpenSSH-Server instead, use
  scp -r there rather than this script.)

  Examples:
    .\Sync-Runs.ps1 -Dest \\DESKTOP-PC\k1\runs             # copy new/changed bundles
    .\Sync-Runs.ps1 -Dest Z:\k1\runs -WhatIf               # preview only
    .\Sync-Runs.ps1 -Dest \\DESKTOP-PC\k1\runs -Mirror     # also delete dest files not in src (careful)

  ⚠ Sync capture runs to the desktop BEFORE the Jetson/laptop retention (offload_run.sh --keep N)
  prunes them -- after offload a run lives in exactly one place until it is synced onward.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Dest,
  [string]$Src = (Join-Path $PSScriptRoot '..\runs'),
  [switch]$Mirror,
  [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
if (-not [System.IO.Path]::IsPathRooted($Src)) { $Src = Join-Path $PSScriptRoot '..\runs' }
$Src = [System.IO.Path]::GetFullPath($Src)
if (-not (Test-Path -LiteralPath $Src)) { throw "source runs\ not found: $Src (pull a run with Pull-Run.ps1 first)" }

# robocopy flags: /E all subdirs, /Z restartable (survives a network blip on a big .rrd),
# /R:2 /W:5 bounded retries, /NP no per-file percent, /NDL quiet dir list. /MIR only on -Mirror
# (it DELETES dest files missing from src). /L list-only on -WhatIf.
$flags = @('/E', '/Z', '/R:2', '/W:5', '/NP', '/NDL')
if ($Mirror) { $flags += '/MIR' }
if ($WhatIf) { $flags += '/L' }

Write-Host "sync runs\  $Src  ->  $Dest  $(if($Mirror){'(mirror)'}) $(if($WhatIf){'(dry-run)'})"
& robocopy.exe $Src $Dest @flags | Write-Host
$rc = $LASTEXITCODE

# robocopy exit codes are bit flags: 0-7 = success (>=8 = failure). 1=copied, 2=extra, 3=copied+extra...
if ($rc -ge 8) {
  Write-Host "SYNC-FAILED (robocopy exit $rc) -- check the dest path/share is reachable and writable."
  exit 1
}
Write-Host "SYNC-OK (robocopy exit $rc)$(if($rc -eq 0){' -- nothing new to copy'})"

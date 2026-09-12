# Sync this local "runtime" checkout to the latest Sky Connect launcher UI.
# Run from PowerShell inside your runtime folder (the Ko-Work / K1Finder checkout):
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Sync-Runtime-From-Git.ps1
#
# Optional:
#   -Branch cursor/sky-connect-launcher-shell-85c6
#   -Remote origin   # or ko-work if you use that remote name

param(
    [string]$Branch = 'cursor/sky-connect-launcher-shell-85c6',
    [string]$Remote = 'origin'
)

$ErrorActionPreference = 'Stop'
$root = if ($PSScriptRoot) {
    # scripts/ -> repo root
    Split-Path -Parent $PSScriptRoot
} else {
    (Get-Location).Path
}

# Allow running from repo root or from desktop/
if (-not (Test-Path (Join-Path $root '.git')) -and (Test-Path (Join-Path (Get-Location) '.git'))) {
    $root = (Get-Location).Path
}
if (-not (Test-Path (Join-Path $root '.git'))) {
    throw "Not a git checkout: $root — run this from your local runtime/Ko-Work folder."
}

Set-Location $root
Write-Host "Repo: $root" -ForegroundColor Cyan
Write-Host "Fetching $Remote / $Branch ..." -ForegroundColor Cyan

git fetch $Remote $Branch
git checkout $Branch
git pull $Remote $Branch

$web = Join-Path $root 'desktop\k1finder-web'
if (-not (Test-Path $web)) {
    throw "Missing desktop\k1finder-web — is this the Ko-Work tree?"
}

Push-Location $web
try {
    Write-Host "Building Sky Connect web shell..." -ForegroundColor Cyan
    npm install
    npm run build
} finally {
    Pop-Location
}

$dist = Join-Path $web 'dist\index.html'
if (-not (Test-Path $dist)) {
    throw "Build finished but dist\index.html is missing."
}

Write-Host ""
Write-Host "Runtime updated." -ForegroundColor Green
Write-Host "Launch: desktop\Sky Connect.bat" -ForegroundColor Green
Write-Host "(Classic WinForms: set SKY_CONNECT_CLASSIC=1 before launch)" -ForegroundColor DarkGray

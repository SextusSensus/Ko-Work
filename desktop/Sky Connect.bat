@echo off
REM Launch Sky Connect. Prefers the built React shell (k1finder-web/dist).
REM Set SKY_CONNECT_CLASSIC=1 to force the legacy WinForms tabs.
setlocal
cd /d "%~dp0"

if not exist "k1finder-web\dist\index.html" (
  echo [Sky Connect] Web shell not built yet. Building k1finder-web...
  pushd k1finder-web
  call npm install
  call npm run build
  popd
  if not exist "k1finder-web\dist\index.html" (
    echo [Sky Connect] Build failed — falling back to classic WinForms UI.
  )
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0K1Finder.ps1"
endlocal

@echo off
REM Launch the K1 Finder (WPF) scaffold (visible console for debug output).
REM Bypass is required: this machine's default execution policy blocks bare .ps1 scripts.
REM -STA is required: WPF's Window constructor throws on an MTA thread.
powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0K1Finder.wpf.ps1"
pause

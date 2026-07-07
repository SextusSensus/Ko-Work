@echo off
REM Launch the K1 Finder desktop app (visible console for debug output).
powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0K1Finder.ps1"

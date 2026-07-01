' Launch K1 Finder (WPF) with no console window (double-click for the clean experience).
' Use the .bat instead while debugging, so any startup error stays visible.
Dim shell, scriptDir
Set shell = CreateObject("WScript.Shell")
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File """ & scriptDir & "K1Finder.wpf.ps1""", 0, False

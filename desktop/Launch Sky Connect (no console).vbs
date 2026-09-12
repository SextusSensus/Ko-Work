' Launch Sky Connect with no console window (double-click for the clean experience).
' Prefers the modern React shell (k1finder-web/dist). Set SKY_CONNECT_CLASSIC=1 for legacy tabs.
Dim shell, scriptDir, dist, cmd
Set shell = CreateObject("WScript.Shell")
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
dist = scriptDir & "k1finder-web\dist\index.html"
If Not CreateObject("Scripting.FileSystemObject").FileExists(dist) Then
  ' Best-effort build so the launcher stays on the modern UI.
  shell.Run "cmd.exe /c cd /d """ & scriptDir & "k1finder-web"" && npm install && npm run build", 0, True
End If
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File """ & scriptDir & "K1Finder.ps1"""
shell.Run cmd, 0, False

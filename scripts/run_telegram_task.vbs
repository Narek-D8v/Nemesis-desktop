' Starts the NemusTelegram scheduled task without a console window.
' Used by the Startup shortcut on Windows.
Set shell = CreateObject("WScript.Shell")
exe = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\schtasks.exe"
shell.Run """" & exe & """ /Run /TN ""NemusTelegram""", 0, False

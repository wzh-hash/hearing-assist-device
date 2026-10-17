' Silent launcher: no window, write hits to recog.log
' Stop: Task Manager -> end python.exe
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = base
cmdLine = """" & base & "\run.bat"" listen --quiet --log recog.log"
sh.Run cmdLine, 0, False

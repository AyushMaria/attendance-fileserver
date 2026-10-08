' run_hidden.vbs - starts run.bat with no window, so nobody at the store can
' click into it (which freezes it) or close it. Task Scheduler runs this:
'     Program:   wscript.exe
'     Arguments: "<this folder>\run_hidden.vbs"
' It waits for run.bat to finish and passes on its exit code, so Task
' Scheduler still knows when a run is going and how it ended.
' DO NOT EDIT OR CHANGE THIS FILE IN FUTURE VERSIONS (like run.bat).

Dim sh, here
Set sh = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
WScript.Quit sh.Run("cmd /c """ & here & "\run.bat""", 0, True)

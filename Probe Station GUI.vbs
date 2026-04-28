Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = fso.BuildPath(root, ".venv\Scripts\pythonw.exe")
main = fso.BuildPath(root, "main.py")

If Not fso.FileExists(pythonw) Then
    MsgBox "Python launcher not found: " & pythonw, vbCritical, "Probe Station GUI"
    WScript.Quit 1
End If

If Not fso.FileExists(main) Then
    MsgBox "Application entry point not found: " & main, vbCritical, "Probe Station GUI"
    WScript.Quit 1
End If

shell.CurrentDirectory = root
shell.Run """" & pythonw & """ """ & main & """", 0, False

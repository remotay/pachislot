Get-CimInstance Win32_Process -Filter "name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*uvicorn app:app*" -and $_.CommandLine -like "*C:\pcontrol*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

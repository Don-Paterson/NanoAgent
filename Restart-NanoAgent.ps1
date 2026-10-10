# NanoAgent self-update helper: swap in nano_agent.py.new (if present) and restart the agent
Start-Sleep 2
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*nano_agent.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 1
if (Test-Path C:\NanoAgent\nano_agent.py.new) {
    Move-Item C:\NanoAgent\nano_agent.py.new C:\NanoAgent\nano_agent.py -Force
}
Start-Process C:\NanoAgent\.venv\Scripts\pythonw.exe -ArgumentList '"C:\NanoAgent\nano_agent.py"' -WorkingDirectory C:\NanoAgent

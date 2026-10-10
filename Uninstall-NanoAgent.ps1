<#
.SYNOPSIS
    Removes NanoAgent from a Windows machine (A-GUI or a support PC).

.DESCRIPTION
    Stops the running agent, removes the "NanoAgent" logon task and deletes the
    install folder (agent, venv, agent.json with the secret, and nano_agent.log).
    Python itself is left installed.  -KeepLog copies the log to the Desktop first.

.EXAMPLE
    .\Uninstall-NanoAgent.ps1
    .\Uninstall-NanoAgent.ps1 -InstallDir D:\NanoAgent -KeepLog
#>
[CmdletBinding()]
param(
    [string]$InstallDir = $(if ($env:NANO_DIR) { $env:NANO_DIR } else { 'C:\NanoAgent' }),
    [switch]$KeepLog
)
$ErrorActionPreference = 'Stop'

Write-Host '== Stopping agent'
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*nano_agent.py*' }
foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "   stopped PID $($p.ProcessId)"
}

if (Get-ScheduledTask -TaskName 'NanoAgent' -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName 'NanoAgent' -Confirm:$false
    Write-Host '== Removed logon task NanoAgent'
}

if (Test-Path $InstallDir) {
    $log = Join-Path $InstallDir 'nano_agent.log'
    if ($KeepLog -and (Test-Path $log)) {
        $dest = Join-Path ([Environment]::GetFolderPath('Desktop')) ("nano_agent_{0:yyyyMMdd_HHmm}.log" -f (Get-Date))
        Copy-Item $log $dest
        Write-Host "   log saved to $dest"
    }
    Start-Sleep -Milliseconds 500         # let file handles close
    if ($PSScriptRoot -and ((Resolve-Path $PSScriptRoot).Path -eq (Resolve-Path $InstallDir).Path)) {
        Set-Location $env:TEMP            # can't delete the folder we're running from otherwise
    }
    Remove-Item $InstallDir -Recurse -Force
    Write-Host "== Removed $InstallDir"
}
else {
    Write-Host "$InstallDir not found."
}
Write-Host 'NanoAgent uninstalled.' -ForegroundColor Green

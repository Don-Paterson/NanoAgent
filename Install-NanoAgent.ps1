<#
.SYNOPSIS
    Installs NanoAgent on a Windows machine (e.g. A-GUI): Python (if needed),
    the agent files, a private venv, a Start shortcut and optional logon start.

.EXAMPLE
    # one-liner - generate it with NanoAzure.ps1 option 9 (config travels inline, base64):
    $env:NANO_CONFIG_B64='eyJu...'; $env:NANO_AUTOSTART='1'; irm https://raw.githubusercontent.com/Don-Paterson/NanoAgent/main/Install-NanoAgent.ps1 | iex

    # or with agent.json fetched from a URL you control:
    $env:NANO_CONFIG_URL = 'https://.../agent.json'; $env:NANO_AUTOSTART = '1'
    irm https://raw.githubusercontent.com/Don-Paterson/NanoAgent/main/Install-NanoAgent.ps1 | iex

    # or locally, with agent.json next to the script:
    .\Install-NanoAgent.ps1 -AutoStart

.NOTES
    Without a config the agent can still run a support session:  Start-NanoAgent.cmd --support
#>
[CmdletBinding()]
param(
    [string]$InstallDir = $(if ($env:NANO_DIR) { $env:NANO_DIR } else { 'C:\NanoAgent' }),
    [string]$ConfigUrl = $env:NANO_CONFIG_URL,
    [string]$Repo = $(if ($env:NANO_REPO) { $env:NANO_REPO } else { 'Don-Paterson/NanoAgent' }),
    [string]$Branch = 'main',
    [switch]$AutoStart = ($env:NANO_AUTOSTART -eq '1'),
    [switch]$Start = ($env:NANO_START -eq '1')
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$PyVersion = '3.12.10'

function Remove-SecretFromHistory {
    # The one-liner contains the shared secret: scrub it from PSReadLine's saved and in-memory history
    try {
        $hp = (Get-PSReadLineOption -ErrorAction Stop).HistorySavePath
        if ($hp -and (Test-Path $hp)) {
            $lines = @(Get-Content $hp)
            $keep = @($lines | Where-Object { $_ -notmatch 'NANO_CONFIG_B64' })
            if ($keep.Count -ne $lines.Count) {
                Set-Content -Path $hp -Value $keep -Encoding UTF8
                Write-Host '   removed the install command from PowerShell history'
            }
        }
        [Microsoft.PowerShell.PSConsoleReadLine]::ClearHistory()
    }
    catch { }
    Clear-History -ErrorAction SilentlyContinue
}

function Stop-RunningAgent {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*nano_agent.py*' } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "   stopped running agent (PID $($_.ProcessId))"
        }
}

function Find-Python {
    foreach ($c in @('py -3.12', 'py -3', 'python')) {
        $exe, $arg = $c -split ' ', 2
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $v = & $exe $arg -c "import sys;print(sys.executable if sys.version_info>=(3,10) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -and (Test-Path $v)) { return $v.Trim() }
    }
    foreach ($p in @("$env:ProgramFiles\Python312\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

Write-Host '== NanoAgent installer' -ForegroundColor Cyan
# Read an inline config first so the secret is out of the environment as early as possible
$inlineConfig = $null
if ($env:NANO_CONFIG_B64) {
    try {
        $inlineConfig = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:NANO_CONFIG_B64.Trim()))
        $o = $inlineConfig | ConvertFrom-Json
    }
    catch { throw 'NANO_CONFIG_B64 is not valid base64-encoded JSON (re-copy it from NanoAzure.ps1 option 9)' }
    foreach ($k in 'negotiate_url', 'group', 'secret') { if (-not $o.$k) { throw "inline config is missing '$k'" } }
    Remove-Item Env:NANO_CONFIG_B64 -ErrorAction SilentlyContinue
    Remove-SecretFromHistory
}
Stop-RunningAgent        # a running agent locks files in the venv during an upgrade
$py = Find-Python
if (-not $py) {
    Write-Host "   Python not found - installing $PyVersion (per-user, silent)"
    $exe = Join-Path $env:TEMP "python-$PyVersion-amd64.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-amd64.exe" -OutFile $exe
    Start-Process $exe -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_test=0', 'Include_tcltk=1', 'Include_launcher=1' -Wait
    $py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (-not (Test-Path $py)) { throw 'Python install failed' }
}
Write-Host "   python: $py"

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$files = 'nano_agent.py', 'nano_proto.py', 'requirements-agent.txt', 'Install-NanoAgent.ps1', 'Uninstall-NanoAgent.ps1'
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'nano_agent.py'))) {
    # Run from a copy of the repo (USB, shared folder, Skillable file drop): no download needed
    Write-Host "== Copying agent files from $PSScriptRoot -> $InstallDir"
    if ((Resolve-Path $PSScriptRoot).Path -ne (Resolve-Path $InstallDir).Path) {
        foreach ($f in $files) { Copy-Item (Join-Path $PSScriptRoot $f) $InstallDir -Force }
    }
}
else {
    # Private repo: set $env:GITHUB_TOKEN to a fine-grained token with Contents:read on the repo
    Write-Host "== Fetching $Repo@$Branch -> $InstallDir"
    $zip = Join-Path $env:TEMP 'nanoagent.zip'
    $hdr = @{ 'User-Agent' = 'NanoAgent-Installer' }
    if ($env:GITHUB_TOKEN) {
        $hdr['Authorization'] = "Bearer $env:GITHUB_TOKEN"
        Invoke-WebRequest "https://api.github.com/repos/$Repo/zipball/$Branch" -Headers $hdr -OutFile $zip
    }
    else {
        try { Invoke-WebRequest "https://github.com/$Repo/archive/refs/heads/$Branch.zip" -Headers $hdr -OutFile $zip }
        catch { throw "Download failed. If $Repo is private, set `$env:GITHUB_TOKEN or run this script from a copy of the repo." }
    }
    $tmp = Join-Path $env:TEMP ('nano_' + [guid]::NewGuid().ToString('N'))
    Expand-Archive $zip $tmp -Force
    $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
    foreach ($f in $files) { Copy-Item (Join-Path $src.FullName $f) $InstallDir -Force }
    Remove-Item $tmp -Recurse -Force
}

Write-Host '== Creating venv and installing packages'
$venv = Join-Path $InstallDir '.venv'
if (-not (Test-Path "$venv\Scripts\python.exe")) { & $py -m venv $venv }
& "$venv\Scripts\python.exe" -m pip install -q --upgrade pip
& "$venv\Scripts\python.exe" -m pip install -q -r (Join-Path $InstallDir 'requirements-agent.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

$cfg = Join-Path $InstallDir 'agent.json'
if ($inlineConfig) {
    Write-Host '== Writing agent.json from the inline config'
    if (Test-Path $cfg) { Remove-Item $cfg -Force }      # previous copy may be read-only for us
    [IO.File]::WriteAllText($cfg, $inlineConfig, (New-Object Text.UTF8Encoding $false))
}
elseif ($ConfigUrl) {
    if (Test-Path $cfg) { Remove-Item $cfg -Force }
    Write-Host '== Downloading agent.json'
    Invoke-WebRequest $ConfigUrl -OutFile $cfg
}
elseif ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'agent.json')) -and
    (Join-Path $PSScriptRoot 'agent.json') -ne $cfg) {
    if (Test-Path $cfg) { Remove-Item $cfg -Force }
    Copy-Item (Join-Path $PSScriptRoot 'agent.json') $cfg
}
if (Test-Path $cfg) {
    # agent.json holds the shared secret: restrict it to the current user + SYSTEM + Administrators
    icacls $cfg /inheritance:r /grant:r "${env:USERNAME}:M" 'SYSTEM:F' 'Administrators:F' | Out-Null
}
else {
    Write-Warning "No agent.json yet - put one in $InstallDir, or run Start-NanoAgent.cmd --support with a negotiate URL."
}

$cmd = Join-Path $InstallDir 'Start-NanoAgent.cmd'
@"
@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" nano_agent.py %*
"@ | Set-Content -Encoding ASCII $cmd

if ($AutoStart) {
    Write-Host '== Registering logon task "NanoAgent"'
    $act = New-ScheduledTaskAction -Execute "$venv\Scripts\pythonw.exe" -Argument "`"$InstallDir\nano_agent.py`"" -WorkingDirectory $InstallDir
    $trg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $pri = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
    $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName 'NanoAgent' -Action $act -Trigger $trg -Principal $pri -Settings $set -Force | Out-Null
}
if ($Start -or $AutoStart) {
    if (Test-Path $cfg) {
        Start-Process "$venv\Scripts\pythonw.exe" -ArgumentList "`"$InstallDir\nano_agent.py`"" -WorkingDirectory $InstallDir
        Write-Host '   agent started (red banner at the top of the screen)'
    }
}
Write-Host ''
Write-Host "Installed to $InstallDir" -ForegroundColor Green
Write-Host '  Start:   Start-NanoAgent.cmd          (Ctrl+Alt+Shift+K stops it)'
Write-Host '  Support: Start-NanoAgent.cmd --support --negotiate-url <url>'
Write-Host "  Log:     $InstallDir\nano_agent.log"

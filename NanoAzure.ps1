<#
.SYNOPSIS
    Menu for managing the NanoAgent Azure side: status, deploy, rotate, test, tear down.

.DESCRIPTION
    Wraps deploy.ps1 and Remove-NanoAzure.ps1 (which still work on their own for
    unattended use).  Non-secret settings (resource group, location, tier, group)
    are remembered in config\settings.json.

.EXAMPLE
    .\NanoAzure.ps1
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$ConfigDir = Join-Path $Root 'config'
$SettingsFile = Join-Path $ConfigDir 'settings.json'
$CtlFile = Join-Path $ConfigDir 'controller.json'
$AgentFile = Join-Path $ConfigDir 'agent.json'

# ------------------------------------------------------------------ settings
$Settings = [ordered]@{ ResourceGroup = 'rg-nanoagent'; Location = 'uksouth'; Sku = 'Free_F1'; Group = 'lab-a-gui'; Repo = 'Don-Paterson/NanoAgent' }
if (Test-Path $SettingsFile) {
    $saved = Get-Content -Raw $SettingsFile | ConvertFrom-Json
    foreach ($k in @($Settings.Keys)) { if ($saved.$k) { $Settings[$k] = $saved.$k } }
}
function Save-Settings {
    New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
    $Settings | ConvertTo-Json | Set-Content -Encoding UTF8 $SettingsFile
}

# ------------------------------------------------------------------ helpers
function Pause-Menu { Write-Host ''; Read-Host 'Press Enter to return to the menu' | Out-Null }

function Get-AzAccount {
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) { return $null }
    $j = az account show -o json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $j) { return $null }
    return ($j | ConvertFrom-Json)
}

function Test-RgExists {
    $r = az group exists -n $Settings.ResourceGroup 2>$null
    return ($LASTEXITCODE -eq 0 -and $r -eq 'true')
}

function Get-Ctl {
    if (Test-Path $CtlFile) { return (Get-Content -Raw $CtlFile | ConvertFrom-Json) }
    return $null
}

function Confirm-Yes([string]$Prompt) {
    return ((Read-Host "$Prompt (yes/no)") -eq 'yes')
}

function Show-Header {
    Clear-Host
    Write-Host '=================================================' -ForegroundColor DarkCyan
    Write-Host '  NanoAgent - Azure rendezvous' -ForegroundColor Cyan
    Write-Host '=================================================' -ForegroundColor DarkCyan
    $acct = Get-AzAccount
    if ($acct) {
        Write-Host ("  Azure     : {0}  ({1})" -f $acct.name, $acct.user.name)
        $state = if (Test-RgExists) { 'DEPLOYED' } else { 'not deployed' }
        $colour = if ($state -eq 'DEPLOYED') { 'Green' } else { 'Yellow' }
        Write-Host ("  Resources : {0} in {1}" -f $state, $Settings.ResourceGroup) -ForegroundColor $colour
    }
    elseif (-not (Get-Command az -ErrorAction SilentlyContinue)) {
        Write-Host '  Azure     : Azure CLI not installed (winget install Microsoft.AzureCLI)' -ForegroundColor Red
    }
    else {
        Write-Host '  Azure     : not logged in (option L)' -ForegroundColor Yellow
    }
    $ctl = Get-Ctl
    if ($ctl) {
        Write-Host ("  Config    : {0}  group={1}" -f ([uri]$ctl.negotiate_url).Host, $ctl.group)
    }
    else {
        Write-Host '  Config    : none yet (created by Deploy)'
    }
    Write-Host ("  Settings  : RG={0}  location={1}  tier={2}  group={3}" -f `
            $Settings.ResourceGroup, $Settings.Location, $Settings.Sku, $Settings.Group) -ForegroundColor DarkGray
    Write-Host '-------------------------------------------------' -ForegroundColor DarkCyan
}

# ------------------------------------------------------------------ actions
function Invoke-Login {
    az login -o none
    if ($LASTEXITCODE -eq 0) {
        $subs = az account list --query "[?state=='Enabled'].{name:name, id:id, default:isDefault}" -o json | ConvertFrom-Json
        if (@($subs).Count -gt 1) {
            Write-Host ''
            for ($i = 0; $i -lt $subs.Count; $i++) {
                $mark = if ($subs[$i].default) { '*' } else { ' ' }
                Write-Host ("  {0}{1}) {2}" -f $mark, ($i + 1), $subs[$i].name)
            }
            $c = Read-Host 'Subscription number (Enter = keep *)'
            if ($c -match '^\d+$' -and [int]$c -ge 1 -and [int]$c -le $subs.Count) {
                az account set --subscription $subs[[int]$c - 1].id
            }
        }
    }
}

function Show-Status {
    if (-not (Get-AzAccount)) { Write-Host 'Not logged in.'; return }
    if (Test-RgExists) {
        az resource list -g $Settings.ResourceGroup --query '[].{Type:type, Name:name}' -o table
    }
    else {
        Write-Host "Resource group $($Settings.ResourceGroup) does not exist."
    }
    $ctl = Get-Ctl
    if ($ctl) {
        Write-Host ''
        Write-Host "Negotiate URL : $(([uri]$ctl.negotiate_url).GetLeftPart('Path'))?code=****"
        Write-Host "Group         : $($ctl.group)"
        Write-Host ("Secret        : {0}...{1}" -f $ctl.secret.Substring(0, 4), $ctl.secret.Substring($ctl.secret.Length - 4))
    }
}

function Invoke-Deploy([switch]$NewSecrets) {
    if (-not (Get-AzAccount)) { Write-Host 'Log in first (option L).'; return }
    $p = @{ ResourceGroup = $Settings.ResourceGroup; Location = $Settings.Location; Sku = $Settings.Sku; Group = $Settings.Group }
    if ($NewSecrets) {
        if (-not (Confirm-Yes 'Rotate the function key AND shared secret? Every agent.json in use stops working')) { return }
        $p.NewSecrets = $true
    }
    & (Join-Path $Root 'deploy.ps1') @p
}

function Test-Negotiate {
    $ctl = Get-Ctl
    if (-not $ctl) { Write-Host 'No config yet - deploy first.'; return }
    try {
        $t = Measure-Command { $r = Invoke-RestMethod "$($ctl.negotiate_url)&role=controller&group=$($ctl.group)" -TimeoutSec 60 }
        if ($r.url -like 'wss://*') {
            Write-Host ("OK - negotiate answered in {0:n1}s (first call after idle includes cold start)" -f $t.TotalSeconds) -ForegroundColor Green
        }
        else { Write-Host "Unexpected reply: $r" -ForegroundColor Yellow }
    }
    catch { Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red }
}

function Test-Agent {
    if (-not (Test-Path $CtlFile)) { Write-Host 'No config yet - deploy first.'; return }
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
    if (-not $py) { Write-Host 'Python not found (needed for the controller).'; return }
    Write-Host 'Asking the agent for its info (up to 30s)...'
    & $py.Source (Join-Path $Root 'nano_cli.py') --config $CtlFile info
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'No answer. Is the agent running on A-GUI with the current agent.json?' -ForegroundColor Yellow
        Write-Host '(Needs: pip install -r requirements-controller.txt)' -ForegroundColor DarkGray
    }
}

function Invoke-Teardown([switch]$RemoveConfig) {
    if (-not (Get-AzAccount)) { Write-Host 'Log in first (option L).'; return }
    $p = @{ ResourceGroup = $Settings.ResourceGroup }
    if ($RemoveConfig) {
        if (-not (Confirm-Yes 'Also delete config\agent.json + controller.json? (next deploy issues new ones)')) { return }
        $p.RemoveConfig = $true
    }
    & (Join-Path $Root 'Remove-NanoAzure.ps1') @p
}

function New-AgentOneLiner {
    if (-not (Test-Path $AgentFile)) { Write-Host 'No config\agent.json yet - deploy first.'; return }
    $json = (Get-Content -Raw $AgentFile | ConvertFrom-Json) | ConvertTo-Json -Compress
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
    $url = "https://raw.githubusercontent.com/$($Settings.Repo)/main/Install-NanoAgent.ps1"
    $line = "`$env:NANO_CONFIG_B64='$b64'; `$env:NANO_AUTOSTART='1'; irm $url | iex"

    # Is the installer reachable anonymously (i.e. is the repo public)?
    try {
        Invoke-WebRequest $url -UseBasicParsing -Method Head -TimeoutSec 15 | Out-Null
        Write-Host "Installer reachable: $url" -ForegroundColor Green
    }
    catch {
        Write-Host "WARNING: $url is not reachable anonymously." -ForegroundColor Yellow
        Write-Host '         If the repo is private, irm on A-GUI will fail (404). Make it public, or' -ForegroundColor Yellow
        Write-Host '         copy the repo folder + agent.json to A-GUI and run Install-NanoAgent.ps1 there.' -ForegroundColor Yellow
    }
    try {
        Set-Clipboard -Value $line
        Write-Host "Copied to clipboard ($($line.Length) chars). Paste into PowerShell on A-GUI." -ForegroundColor Green
    }
    catch { Write-Host 'Could not use the clipboard - copy the line below.' -ForegroundColor Yellow }
    Write-Host ''
    if ((Read-Host 'Show the command here? It contains the secret (y/N)') -eq 'y') {
        Write-Host ''
        Write-Host $line
    }
    Write-Host ''
    Write-Host 'On A-GUI the installer removes the command from PowerShell history once it has read it.' -ForegroundColor DarkGray
}

function Edit-Settings {
    foreach ($k in @('ResourceGroup', 'Location', 'Sku', 'Group', 'Repo')) {
        $hint = switch ($k) {
            'Sku' { ' (Free_F1 | Standard_S1)' }
            'Group' { ' (must start lab- or sup-)' }
            'Repo' { ' (owner/name on GitHub)' }
            default { '' }
        }
        $v = Read-Host "$k$hint [$($Settings[$k])]"
        if ($v) {
            if ($k -eq 'Sku' -and $v -notin 'Free_F1', 'Standard_S1') { Write-Host '  ignored: invalid tier'; continue }
            if ($k -eq 'Group' -and $v -notmatch '^(lab|sup)-[A-Za-z0-9_-]{1,60}$') { Write-Host '  ignored: invalid group'; continue }
            if ($k -eq 'Repo' -and $v -notmatch '^[\w.-]+/[\w.-]+$') { Write-Host '  ignored: use owner/name'; continue }
            $Settings[$k] = $v
        }
    }
    Save-Settings
    Write-Host 'Saved. Tier or group changes take effect on the next Deploy.'
}

function Open-ConfigFolder {
    New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
    if (Test-Path $AgentFile) { Start-Process explorer.exe "/select,`"$AgentFile`"" }
    else { Start-Process explorer.exe $ConfigDir }
}

# ------------------------------------------------------------------ menu loop
while ($true) {
    Show-Header
    Write-Host '   1) Status          - resources in Azure and current config'
    Write-Host '   2) Deploy          - create/update (reuses existing key + secret)'
    Write-Host '   3) Test negotiate  - is the Function answering?'
    Write-Host '   4) Test agent      - is A-GUI connected? (needs Python controller deps)'
    Write-Host '   5) Open config     - folder with agent.json to copy to A-GUI'
    Write-Host '   6) Tear down       - delete Azure resources, keep config (redeploy later)'
    Write-Host '   7) Tear down + wipe config'
    Write-Host '   8) Rotate secrets  - new function key + new shared secret'
    Write-Host '   9) Agent one-liner - irm | iex install command for A-GUI (to clipboard)'
    Write-Host '   S) Settings        - resource group, location, tier, group'
    Write-Host '   L) Azure login / choose subscription'
    Write-Host '   Q) Quit'
    $choice = (Read-Host "`nChoice").Trim().ToUpper()
    Write-Host ''
    $pause = $true
    try {
        switch ($choice) {
            '1' { Show-Status }
            '2' { Invoke-Deploy }
            '3' { Test-Negotiate }
            '4' { Test-Agent }
            '5' { Open-ConfigFolder; $pause = $false }
            '6' { Invoke-Teardown }
            '7' { Invoke-Teardown -RemoveConfig }
            '8' { Invoke-Deploy -NewSecrets }
            '9' { New-AgentOneLiner }
            'S' { Edit-Settings }
            'L' { Invoke-Login }
            'Q' { return }
            default { $pause = $false }
        }
    }
    catch { Write-Host "Error: $($_.Exception.Message)" -ForegroundColor Red }
    if ($pause) { Pause-Menu }
}

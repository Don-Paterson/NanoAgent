<#
.SYNOPSIS
    Deploys the NanoAgent rendezvous (Azure Web PubSub + negotiate Function) and
    writes ready-to-use agent.json / controller.json.

.EXAMPLE
    .\deploy.ps1 -ResourceGroup rg-nanoagent -Location uksouth
    .\deploy.ps1 -ResourceGroup rg-nanoagent -Sku Standard_S1 -Group lab-a-gui
    .\deploy.ps1 -NewSecrets          # rotate: new function key + new shared secret

    Redeploying after Remove-NanoAzure.ps1 (same resource group name, same
    subscription) gives the same host name, and the existing function key and
    secret in config\controller.json are reused - so an agent.json already on
    A-GUI keeps working.  Tear down when idle, redeploy when needed.

.NOTES
    Needs: Azure CLI (az login done), and either Azure Functions Core Tools (func)
    or az's zip deploy.  Works in Windows PowerShell 5.1 and PowerShell 7.
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = 'rg-nanoagent',
    [string]$Location = 'uksouth',
    [string]$BaseName = 'nanoagent',
    [ValidateSet('Free_F1', 'Standard_S1')][string]$Sku = 'Free_F1',
    [string]$Group = 'lab-a-gui',
    [string]$OutDir = (Join-Path $PSScriptRoot 'config'),
    [switch]$NewSecrets
)
$ErrorActionPreference = 'Stop'

function Invoke-Az {
    $out = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }
    $out
}

# Reuse key + secret from a previous deployment unless rotating
$prev = $null
$prevFile = Join-Path $OutDir 'controller.json'
if (-not $NewSecrets -and (Test-Path $prevFile)) {
    $prev = Get-Content -Raw $prevFile | ConvertFrom-Json
    $prevKey = ([regex]::Match($prev.negotiate_url, 'code=([^&]+)')).Groups[1].Value
    $prevHost = ([uri]$prev.negotiate_url).Host
    Write-Host "== Reusing function key and secret from $prevFile (use -NewSecrets to rotate)"
}

Write-Host "== Resource group $ResourceGroup ($Location)" -ForegroundColor Cyan
Invoke-Az group create -n $ResourceGroup -l $Location -o none

Write-Host "== Deploying Web PubSub ($Sku) + Function infrastructure" -ForegroundColor Cyan
$dep = Invoke-Az deployment group create -g $ResourceGroup -f (Join-Path $PSScriptRoot 'infra/main.bicep') `
    -p baseName=$BaseName pubSubSku=$Sku --query properties.outputs -o json | ConvertFrom-Json
$fnName = $dep.functionAppName.value
$fnHost = $dep.functionHost.value
Write-Host "   function app: $fnName  ($fnHost)"
Write-Host "   web pubsub:   $($dep.webPubSubHost.value)"

Write-Host "== Publishing negotiate function" -ForegroundColor Cyan
Push-Location (Join-Path $PSScriptRoot 'function')
try {
    if (Get-Command func -ErrorAction SilentlyContinue) {
        func azure functionapp publish $fnName --python
        if ($LASTEXITCODE -ne 0) { throw 'func publish failed' }
    }
    else {
        $zip = Join-Path ([IO.Path]::GetTempPath()) 'nano-function.zip'
        if (Test-Path $zip) { Remove-Item $zip }
        Compress-Archive -Path function_app.py, host.json, requirements.txt -DestinationPath $zip
        Invoke-Az functionapp deployment source config-zip -g $ResourceGroup -n $fnName --src $zip --build-remote true -o none
    }
}
finally { Pop-Location }

$key = $null
if ($prev -and $prevKey) {
    Write-Host "== Restoring previous function key" -ForegroundColor Cyan
    for ($i = 0; $i -lt 12; $i++) {
        az functionapp keys set -g $ResourceGroup -n $fnName --key-type functionKeys --key-name default --key-value $prevKey -o none 2>$null
        if ($LASTEXITCODE -eq 0) { $key = $prevKey; break }
        Start-Sleep 10
    }
    if (-not $key) { Write-Warning 'Could not restore the previous key; using the new one (agent.json must be redistributed).' }
    if ($prevHost -and $prevHost -ne $fnHost) {
        Write-Warning "Host changed ($prevHost -> $fnHost): agent.json must be redistributed."
    }
}
Write-Host "== Fetching function key" -ForegroundColor Cyan
for ($i = 0; $i -lt 12 -and -not $key; $i++) {   # skipped when restored above
    $key = az functionapp keys list -g $ResourceGroup -n $fnName --query functionKeys.default -o tsv 2>$null
    if (-not $key) { Start-Sleep 10 }
}
if (-not $key) { throw 'Could not read the function key - try again in a minute.' }
$negotiate = "https://$fnHost/api/negotiate?code=$key"

# 32-byte shared secret for signing + encryption (PS 5.1 compatible)
if ($prev -and $prev.secret) {
    $secret = $prev.secret
}
else {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $secret = -join ($bytes | ForEach-Object { $_.ToString('x2') })
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$agent = [ordered]@{
    negotiate_url = $negotiate; group = $Group; secret = $secret
    monitor = 1; max_long_edge = 1280; allow_run = $true; allow_files = $true
}
$ctl = [ordered]@{ negotiate_url = $negotiate; group = $Group; secret = $secret }
$agent | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $OutDir 'agent.json')
$ctl | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $OutDir 'controller.json')

Write-Host "== Testing negotiate" -ForegroundColor Cyan
try {
    $r = Invoke-RestMethod "$negotiate&role=controller&group=$Group"
    if ($r.url -like 'wss://*') { Write-Host '   OK - negotiate returns a Web PubSub URL' -ForegroundColor Green }
}
catch { Write-Warning "negotiate test failed (cold start?): $($_.Exception.Message)" }

Write-Host ""
Write-Host "Done. Config written to $OutDir" -ForegroundColor Green
Write-Host "  agent.json      -> copy to A-GUI (C:\NanoAgent\agent.json)"
Write-Host "  controller.json -> keep next to nano_cli.py / claude_driver.py"
Write-Host "Both contain the shared secret and function key: treat them like passwords."

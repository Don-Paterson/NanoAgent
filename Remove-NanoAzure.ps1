<#
.SYNOPSIS
    Removes everything NanoAgent created in Azure (and optionally the local config).

.DESCRIPTION
    Deletes the resource group, which removes in one go: Web PubSub, the Function
    App and its plan, the storage account (function package + host keys), the
    managed identity, its role assignments and the deployment history.
    None of these resources have soft-delete/retention in this template, so
    nothing is left behind and nothing keeps billing.

    By default config\agent.json / controller.json are KEPT so that a later
    .\deploy.ps1 (same resource group name, same subscription) restores the same
    URL, function key and secret, and A-GUI needs no reconfiguring.
    Use -RemoveConfig to wipe them too (then the next deploy issues new ones).

.EXAMPLE
    .\Remove-NanoAzure.ps1                       # asks to confirm, waits until gone
    .\Remove-NanoAzure.ps1 -Force -NoWait        # unattended, returns immediately
    .\Remove-NanoAzure.ps1 -RemoveConfig         # also delete local config files
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = 'rg-nanoagent',
    [string]$ConfigDir = (Join-Path $PSScriptRoot 'config'),
    [switch]$RemoveConfig,
    [switch]$Force,
    [switch]$NoWait
)
$ErrorActionPreference = 'Stop'

$exists = az group exists -n $ResourceGroup
if ($LASTEXITCODE -ne 0) { throw 'az failed - are you logged in? (az login)' }

if ($exists -eq 'true') {
    Write-Host "Resources in $($ResourceGroup):" -ForegroundColor Cyan
    az resource list -g $ResourceGroup --query '[].{type:type, name:name}' -o table
    if (-not $Force) {
        $ans = Read-Host "Delete resource group '$ResourceGroup' and everything in it? (yes/no)"
        if ($ans -ne 'yes') { Write-Host 'Cancelled.'; return }
    }
    $azArgs = @('group', 'delete', '-n', $ResourceGroup, '--yes')
    if ($NoWait) { $azArgs += '--no-wait' }
    Write-Host "Deleting $ResourceGroup ..." -ForegroundColor Cyan
    az @azArgs
    if ($LASTEXITCODE -ne 0) { throw 'az group delete failed' }
    if ($NoWait) {
        Write-Host 'Deletion started (takes a few minutes). Check with: az group exists -n' $ResourceGroup
    }
    else {
        Write-Host 'Azure resources removed.' -ForegroundColor Green
    }
}
else {
    Write-Host "Resource group '$ResourceGroup' does not exist - nothing to remove in Azure."
}

if ($RemoveConfig) {
    foreach ($f in 'agent.json', 'controller.json') {
        $p = Join-Path $ConfigDir $f
        if (Test-Path $p) { Remove-Item $p -Force; Write-Host "removed $p" }
    }
}
else {
    Write-Host "Local config kept in $ConfigDir (redeploy reuses it; -RemoveConfig to delete)."
}

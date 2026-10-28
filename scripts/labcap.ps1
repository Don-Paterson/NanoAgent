<#
.SYNOPSIS
  One short, read-only capture session to a Gaia host from A-GUI: run a script, fetch files, clean up.

.DESCRIPTION
  Built for shared labs where CLI access must be brief and must change nothing.
  - The password is read on A-GUI from a protected file; it is never passed in a job script or chat.
  - The host key is pinned (-HostKey). Use -GetHostKey first to read the fingerprint.
  - Uses plink -pwfile when the installed PuTTY supports it (0.77+), otherwise -pw.
  - Users whose shell is clish: -Clish feeds the script to clish/gclish line by line; -Expert first
    enters expert mode (password read from -ExpertPwFile, default the same file). Input is sent with
    pauses so prompts are answered after they appear and the password is never echoed.
  - Users whose shell is bash (expert): the script runs with plink -m.
  - -Fetch copies remote files back with pscp; -Cleanup removes the listed remote temp paths afterwards.

.EXAMPLE
  .\labcap.ps1 -Target 10.1.1.101 -GetHostKey
.EXAMPLE
  .\labcap.ps1 -Target 10.1.1.101 -User admin -HostKey SHA256:xxxx -PwFile $env:USERPROFILE\Downloads\pw.txt `
      -Script .\collect.sh -Fetch /tmp/cap.tgz -Cleanup /tmp/cap,/tmp/cap.tgz -OutDir .\caps
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Target,
    [string]$User = "admin",
    [string]$HostKey,
    [string]$PwFile,
    [string]$ExpertPwFile,
    [string]$Script,
    [switch]$Expert,
    [switch]$Clish,
    [string[]]$Fetch = @(),
    [string[]]$Cleanup = @(),
    [string]$OutDir = ".",
    [int]$Wait = 60,
    [switch]$GetHostKey,
    [string]$PuttyDir = "C:\Program Files\PuTTY"
)
$ErrorActionPreference = "Stop"
$plink = Join-Path $PuttyDir "plink.exe"
$pscp  = Join-Path $PuttyDir "pscp.exe"
if (-not (Test-Path $plink)) { throw "plink not found in $PuttyDir" }

if ($GetHostKey) {
    # 'n' refuses to cache the key; plink prints the fingerprint and exits.
    $o = cmd /c "echo n | `"$plink`" -ssh -batch -l $User $Target exit 2>&1"
    $o | Select-String "SHA256:\S+" | ForEach-Object { $_.Matches[0].Value }
    return
}
foreach ($p in 'HostKey','PwFile') {
    if (-not $PSBoundParameters[$p]) { throw "-$p is required (run -GetHostKey first for the fingerprint)" }
}
if (-not (Test-Path $PwFile)) { throw "Password file not found: $PwFile" }
if (-not $ExpertPwFile) { $ExpertPwFile = $PwFile }

# Password handling: prefer -pwfile (not visible in the process list).
$ver = (& $plink -V 2>&1 | Out-String)
$hasPwFile = $false
if ($ver -match 'Release (\d+)\.(\d+)') { $hasPwFile = ([int]$Matches[1] -gt 0) -or ([int]$Matches[2] -ge 77) }
function Auth-Args {
    if ($hasPwFile) { return @("-pwfile", $PwFile) }
    return @("-pw", (Get-Content $PwFile -Raw).Trim())
}
$common = @("-batch", "-ssh", "-hostkey", $HostKey, "-l", $User)

function Invoke-Plink([string[]]$extra, [string]$stdinText) {
    $out = [IO.Path]::GetTempFileName(); $err = [IO.Path]::GetTempFileName()
    $argv = ($common + (Auth-Args) + $extra + @($Target)) | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }
    $sp = @{ FilePath = $plink; ArgumentList = $argv; NoNewWindow = $true; PassThru = $true
             RedirectStandardOutput = $out; RedirectStandardError = $err }
    $in = $null
    if ($stdinText) {
        $in = [IO.Path]::GetTempFileName()
        [IO.File]::WriteAllText($in, $stdinText.Replace("`r", ""))
        $sp.RedirectStandardInput = $in
    }
    $p = Start-Process @sp
    $timedOut = -not $p.WaitForExit($Wait * 1000)
    if ($timedOut) { $p.Kill() }
    $res = (Get-Content $out -Raw) + (Get-Content $err -Raw)
    Remove-Item $out, $err -Force
    if ($in) { Remove-Item $in -Force }
    if ($timedOut) { $res += "`nTIMEOUT after $Wait s (session killed)" }
    return $res
}

function Invoke-Interactive([string[]]$steps) {
    # Steps are sent one at a time with pauses, so prompts (e.g. the expert password) are answered
    # after they appear: typed-ahead input is flushed by the prompt and would be echoed.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $plink
    $argv = ($common + (Auth-Args) + @("-t", $Target)) | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }
    $psi.Arguments = ($argv -join " ")
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $outTask = $p.StandardOutput.ReadToEndAsync(); $errTask = $p.StandardError.ReadToEndAsync()
    foreach ($st in $steps) {
        $delay, $text = $st -split "\|", 2
        Start-Sleep -Milliseconds ([int]$delay)
        if ($p.HasExited) { break }
        $p.StandardInput.Write($text.Replace("`r", "") + "`n"); $p.StandardInput.Flush()
    }
    if (-not $p.WaitForExit($Wait * 1000)) { $p.Kill(); $tail = "`nTIMEOUT after $Wait s (session killed)" } else { $tail = "" }
    return $outTask.Result + $errTask.Result + $tail
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$log = Join-Path $OutDir "labcap_${Target}_$stamp.log"

if ($Script) {
    if (-not (Test-Path $Script)) { throw "Script not found: $Script" }
    if ($Expert -or $Clish) {
        $body = (Get-Content $Script -Raw).Replace("`r", "").TrimEnd("`n").Split("`n")
        $steps = @("3000|")
        if ($Expert) {
            $steps += "500|expert"
            $steps += ("2500|" + (Get-Content $ExpertPwFile -Raw).Trim())
            $steps += "1500|stty -echo 2>/dev/null"
        }
        foreach ($l in $body) { $steps += "400|$l" }
        $steps += "1500|exit"
        if ($Expert) { $steps += "800|exit" }
        $res = Invoke-Interactive $steps
        $steps = $null
    } else {
        $res = Invoke-Plink @("-m", (Resolve-Path $Script).Path) $null
    }
    Set-Content -Path $log -Value $res -Encoding UTF8
    $res
}

foreach ($f in $Fetch) {
    $dest = Join-Path $OutDir (Split-Path $f -Leaf)
    $argv = @("-batch", "-scp", "-hostkey", $HostKey, "-l", $User) + (Auth-Args) + @("${Target}:$f", $dest)
    & $pscp @argv 2>&1 | Out-String
    if (Test-Path $dest) { Get-Item $dest | Select-Object Name, Length | Out-String }
}

if ($Cleanup.Count) {
    # Only removes the paths given; refuses anything outside /tmp or /var/tmp.
    $safe = $Cleanup | Where-Object { $_ -match '^/(var/)?tmp/[^ ;&|]+$' }
    $bad = $Cleanup | Where-Object { $_ -notin $safe }
    if ($bad) { "Skipped cleanup of (not under /tmp): $($bad -join ', ')" }
    if ($safe) {
        $tmp = [IO.Path]::GetTempFileName()
        [IO.File]::WriteAllText($tmp, "rm -rf $($safe -join ' ')`n")
        if ($Expert) { Invoke-Interactive @("3000|", "500|expert", ("2500|" + (Get-Content $ExpertPwFile -Raw).Trim()), "1500|rm -rf $($safe -join ' ')", "1000|exit", "800|exit") | Out-Null }
        else { Invoke-Plink @("-m", $tmp) $null | Out-Null }
        Remove-Item $tmp -Force
        "Cleaned up: $($safe -join ', ')"
    }
}
"Log: $log"

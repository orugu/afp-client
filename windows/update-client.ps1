Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptRoot "lib\Common.ps1")

try {
    $state = Get-ClientState
    if (-not $state) {
        Write-InstallLog "No local state; skipping update" "WARN"
        exit 0
    }

    $manifestUrl = "$($state.app_base_url)/downloads/manifest.json"
    Write-InstallLog "Checking update manifest: $manifestUrl"
    $manifest = Invoke-RestMethod -Uri $manifestUrl -TimeoutSec 20
    $remoteVersion = [string]$manifest.version
    $localVersion = [string]$state.version

    if ($remoteVersion -and $localVersion -and ($remoteVersion -eq $localVersion)) {
        Write-InstallLog "Client already at version $localVersion"
        exit 0
    }

    $downloadsBase = "$($state.app_base_url)/downloads"
    foreach ($fileName in @($manifest.files)) {
        if (-not $fileName) { continue }
        if ($fileName -eq "FileSortingManager.exe") {
            $dest = Join-Path $ProgramDataRoot $fileName
        } else {
            $dest = Join-Path $ScriptsDir $fileName
        }
        $parent = Split-Path -Parent $dest
        if ($parent -and -not (Test-Path $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        $url = "$downloadsBase/$fileName"
        Write-InstallLog "Downloading $url"
        Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -TimeoutSec 120
    }

    $state.version = $remoteVersion
    Save-ClientState -State $state
    Write-InstallLog "Updated client to version $remoteVersion"
}
catch {
    Write-InstallLog $_.Exception.Message "ERROR"
    exit 1
}

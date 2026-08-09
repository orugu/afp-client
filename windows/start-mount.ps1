Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptRoot "lib\Common.ps1")

try {
    $state = Get-ClientState
    if (-not $state) {
        throw "Client is not installed. Run install.ps1 first."
    }

    Start-FileSortingMount -State $state
}
catch {
    Write-InstallLog $_.Exception.Message "ERROR"
    exit 1
}

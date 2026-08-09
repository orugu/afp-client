Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptRoot "lib\Common.ps1")

try {
    Ensure-ClientDirectories
    Invoke-CacheCleanup -CacheDir $CacheDir -MaxAgeHours 1
}
catch {
    Write-InstallLog $_.Exception.Message "ERROR"
    exit 1
}

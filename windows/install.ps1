#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Install File Sorting Windows client (WinFsp + rclone + on-demand cache mount).

.EXAMPLE
  .\install.ps1 -AppBaseUrl "https://devlovers.cloud/file_managing" -ApiToken "your-token"
#>
[CmdletBinding()]
param(
    [string]$AppBaseUrl = "https://devlovers.cloud/file_managing",
    [string]$ApiToken = "",
    [string]$DriveLetter = "",
    [ValidateSet("sync", "mount", "hybrid")][string]$Mode = "sync",
    [string]$LocalSyncDir = "$env:USERPROFILE\File Sorting",
    [switch]$SkipMount
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$CommonPath = Join-Path $ScriptRoot "lib\Common.ps1"

if (-not (Test-Path $CommonPath)) {
    $bootstrapLog = Join-Path $env:TEMP "file-sorting-installer-bootstrap.log"
    Add-Content -Path $bootstrapLog -Value "[$(Get-Date -Format o)] lib/Common.ps1 missing; bootstrapping from server"
    $downloadsBase = "$($AppBaseUrl.TrimEnd('/'))/downloads"
    $manifestUrl = "$downloadsBase/manifest.json"
    $manifest = Invoke-RestMethod -Uri $manifestUrl -TimeoutSec 30
    foreach ($fileName in @($manifest.files)) {
        if (-not $fileName) { continue }
        $dest = Join-Path $ScriptRoot $fileName
        $parent = Split-Path -Parent $dest
        if ($parent -and -not (Test-Path $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        Invoke-WebRequest -Uri "$downloadsBase/$fileName" -OutFile $dest -UseBasicParsing -TimeoutSec 120
    }
    $libDest = Join-Path $ScriptRoot "lib"
    if (-not (Test-Path (Join-Path $libDest "Common.ps1"))) {
        New-Item -ItemType Directory -Path $libDest -Force | Out-Null
        Invoke-WebRequest -Uri "$downloadsBase/lib/Common.ps1" -OutFile (Join-Path $libDest "Common.ps1") -UseBasicParsing -TimeoutSec 120
    }
}

. $CommonPath

try {
    Ensure-ClientDirectories
    Write-InstallLog "=== File Sorting Client install started ==="

    if (-not $ApiToken) {
        $secure = Read-Host "Enter API token" -AsSecureString
        $ApiToken = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        )
    }
    if (-not $ApiToken) {
        throw "API token is required."
    }

    $AppBaseUrl = $AppBaseUrl.TrimEnd("/")
    $apiBase = "$AppBaseUrl/api"

    Write-InstallLog "Fetching bootstrap from $apiBase/windows/bootstrap"
    $bootstrap = Invoke-ApiJson -Url "$apiBase/windows/bootstrap" -Token $ApiToken
    Write-InstallLog "Fetching WebDAV credentials"
    $credentials = Invoke-ApiJson -Url "$apiBase/windows/credentials" -Token $ApiToken

    $resolvedDrive = if ($DriveLetter) { $DriveLetter.Substring(0, 1).ToUpperInvariant() } else { $bootstrap.drive_letter }
    $remoteName = $bootstrap.remote_name
    $davUrl = $bootstrap.dav_url

    Install-WinFsp
    Install-Rclone
    Set-RcloneRemote -RemoteName $remoteName -DavUrl $davUrl -Username $credentials.username -Password $credentials.password

    Copy-ClientScripts -SourceDir $ScriptRoot

    $state = [ordered]@{
        app_base_url = $AppBaseUrl
        api_base_url = $apiBase
        api_token = $ApiToken
        dav_url = (Normalize-DavUrl -Url $davUrl)
        remote_name = $remoteName
        drive_letter = $resolvedDrive
        webdav_username = $credentials.username
        webdav_password = $credentials.password
        client_mode = $Mode
        local_sync_dir = $LocalSyncDir
        installed_at = (Get-Date).ToString("o")
        version = "0.1.3"
    }
    Save-ClientState -State $state

    Register-ClientScheduledTasks -ScriptsDir $ScriptsDir -Mode $Mode

    $managerExe = Join-Path $ScriptRoot "FileSortingManager.exe"
    if (Test-Path $managerExe) {
        Copy-Item -Path $managerExe -Destination (Join-Path $ProgramDataRoot "FileSortingManager.exe") -Force
    }

    if (($Mode -in @("mount", "hybrid")) -and (-not $SkipMount)) {
        Start-FileSortingMount -State $state
    }

    if ($Mode -in @("sync", "hybrid")) {
        Invoke-LocalSync -State $state
    }

    $desktop = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktop "File Sorting.lnk"
    $targetExe = Join-Path $ProgramDataRoot "FileSortingManager.exe"
    if ($Mode -eq "sync") {
        $targetExe = "explorer.exe"
        $targetArgs = $LocalSyncDir
    }
    elseif (-not (Test-Path $targetExe)) {
        $targetExe = "explorer.exe"
        $targetArgs = "$resolvedDrive`:\"
    } else {
        $targetArgs = ""
    }

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $targetExe
    if ($targetArgs) { $shortcut.Arguments = $targetArgs }
    $shortcut.WorkingDirectory = $ProgramDataRoot
    if ($Mode -eq "sync") {
        $shortcut.Description = "File Sorting local sync folder"
    } else {
        $shortcut.Description = "File Sorting cached drive"
    }
    $shortcut.Save()

    Write-InstallLog "Install completed. Drive ${resolvedDrive}: is ready."
    Write-Host ""
    Write-Host "Install complete." -ForegroundColor Green
    if ($Mode -eq "sync") {
        Write-Host "Local sync folder: $LocalSyncDir"
    } elseif ($Mode -eq "hybrid") {
        Write-Host "Hybrid mode enabled: drive ${resolvedDrive}: + local sync folder $LocalSyncDir"
    } else {
        Write-Host "Drive ${resolvedDrive}:\ is mounted with on-demand cache."
    }
    Write-Host "Logs: $InstallLog"
}
catch {
    Write-InstallLog $_.Exception.Message "ERROR"
    Write-Host "Install failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

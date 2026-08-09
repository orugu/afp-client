Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:ProgramDataRoot = Join-Path $env:ProgramData "FileSortingClient"
$script:CacheDir = Join-Path $script:ProgramDataRoot "cache"
$script:ConfigDir = Join-Path $script:ProgramDataRoot "config"
$script:ScriptsDir = Join-Path $script:ProgramDataRoot "scripts"
$script:StateFile = Join-Path $script:ProgramDataRoot "state.json"
$script:InstallLog = Join-Path $script:ProgramDataRoot "install.log"
$script:MountLog = Join-Path $script:ProgramDataRoot "mount.log"
$script:SyncLog = Join-Path $script:ProgramDataRoot "sync.log"
$script:MountPidFile = Join-Path $script:ProgramDataRoot "mount.pid"
$script:SyncInitMarker = Join-Path $script:ProgramDataRoot "sync-initialized.flag"

function Write-InstallLog {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet("INFO", "WARN", "ERROR")][string]$Level = "INFO"
    )

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] [$Level] $Message"
    if (-not (Test-Path $script:ProgramDataRoot)) {
        New-Item -ItemType Directory -Path $script:ProgramDataRoot -Force | Out-Null
    }
    Add-Content -Path $script:InstallLog -Value $line -Encoding UTF8
    Write-Host $line
}

function Ensure-ClientDirectories {
    foreach ($path in @($script:ProgramDataRoot, $script:CacheDir, $script:ConfigDir, $script:ScriptsDir)) {
        if (-not (Test-Path $path)) {
            New-Item -ItemType Directory -Path $path -Force | Out-Null
        }
    }
}

function Get-ClientState {
    if (-not (Test-Path $script:StateFile)) {
        return $null
    }
    return Get-Content -Path $script:StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Save-ClientState {
    param([Parameter(Mandatory = $true)]$State)

    Ensure-ClientDirectories
    $State | ConvertTo-Json -Depth 6 | Set-Content -Path $script:StateFile -Encoding UTF8
}

function Invoke-ApiJson {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Token,
        [ValidateSet("GET", "POST")][string]$Method = "GET",
        $Body = $null
    )

    $headers = @{
        Authorization = "Bearer $Token"
        Accept = "application/json"
    }

    $params = @{
        Uri = $Url
        Method = $Method
        Headers = $headers
        TimeoutSec = 30
        UseBasicParsing = $true
    }
    if ($Body -ne $null) {
        $params.Body = ($Body | ConvertTo-Json -Compress)
        $params.ContentType = "application/json"
    }

    $response = Invoke-WebRequest @params
    return $response.Content | ConvertFrom-Json
}

function Resolve-RclonePath {
    $cmd = Get-Command rclone -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $candidates = @(
        "$env:ProgramFiles\rclone\rclone.exe",
        "$env:LocalAppData\Microsoft\WinGet\Links\rclone.exe",
        "$env:LocalAppData\Programs\rclone\rclone.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    throw "rclone not found. Run install.ps1 first."
}

function Ensure-WinFspReady {
    $service = Get-Service -Name "WinFsp.Launcher" -ErrorAction SilentlyContinue
    if (-not $service) {
        throw "WinFsp is not installed. Re-run install.ps1."
    }
    if ($service.Status -ne "Running") {
        Write-InstallLog "Starting WinFsp.Launcher service"
        Start-Service -Name "WinFsp.Launcher"
        Start-Sleep -Seconds 2
    }
}

function Enable-WebDavClient {
    $webClientKey = "HKLM:\SYSTEM\CurrentControlSet\Services\WebClient\Parameters"
    if (-not (Test-Path $webClientKey)) {
        return
    }

    New-ItemProperty -Path $webClientKey -Name "BasicAuthLevel" -Value 2 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $webClientKey -Name "FileSizeLimitInBytes" -Value 4294967295 -PropertyType DWord -Force | Out-Null

    $service = Get-Service -Name "WebClient" -ErrorAction SilentlyContinue
    if ($service -and $service.Status -ne "Running") {
        Set-Service -Name "WebClient" -StartupType Manual
        Start-Service -Name "WebClient" -ErrorAction SilentlyContinue
    }
    Write-InstallLog "Configured Windows WebClient for HTTPS Basic auth"
}

function Normalize-DavUrl {
    param([Parameter(Mandatory = $true)][string]$Url)
    $normalized = $Url.Trim().TrimEnd("/")
    return "$normalized/"
}

function Get-RcloneObscuredPassword {
    param(
        [Parameter(Mandatory = $true)][string]$RclonePath,
        [Parameter(Mandatory = $true)][string]$Password
    )

    $output = & $RclonePath obscure $Password 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to obscure rclone password: $output"
    }
    return ($output | Select-Object -Last 1).ToString().Trim()
}

function Test-DriveMounted {
    param([Parameter(Mandatory = $true)][string]$DriveLetter)

    $root = "$DriveLetter`:"
    return Test-Path $root
}

function Get-MountProcess {
    param([Parameter(Mandatory = $true)][string]$DriveLetter)

    $pattern = "rclone.*mount.*$DriveLetter`:"
    return Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.CommandLine -match $pattern) }
}

function Stop-MountProcess {
    param([Parameter(Mandatory = $true)][string]$DriveLetter)

    $processes = Get-MountProcess -DriveLetter $DriveLetter
    foreach ($proc in $processes) {
        Write-InstallLog "Stopping rclone mount PID=$($proc.ProcessId)"
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }

    if (Test-Path $script:MountPidFile) {
        Remove-Item -Path $script:MountPidFile -Force -ErrorAction SilentlyContinue
    }

    $root = "$DriveLetter`:"
    cmd.exe /c "net use $root /delete /y" 2>$null | Out-Null
}

function Install-WinFsp {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-InstallLog "Installing WinFsp via winget"
        winget install -e --id WinFsp.WinFsp --accept-package-agreements --accept-source-agreements --silent | Out-Null
        Start-Sleep -Seconds 3
        Ensure-WinFspReady
        return
    }
    throw "winget is required to install WinFsp automatically."
}

function Install-Rclone {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-InstallLog "Installing rclone via winget"
        winget install -e --id Rclone.Rclone --accept-package-agreements --accept-source-agreements --silent | Out-Null
        Start-Sleep -Seconds 2

        $rcloneDir = "$env:LocalAppData\Microsoft\WinGet\Links"
        if (Test-Path $rcloneDir) {
            $env:Path = "$rcloneDir;$env:Path"
        }
        return
    }
    throw "winget is required to install rclone automatically."
}

function Set-RcloneRemote {
    param(
        [Parameter(Mandatory = $true)][string]$RemoteName,
        [Parameter(Mandatory = $true)][string]$DavUrl,
        [Parameter(Mandatory = $true)][string]$Username,
        [Parameter(Mandatory = $true)][string]$Password
    )

    $rclone = Resolve-RclonePath
    $davUrl = Normalize-DavUrl -Url $DavUrl
    $passObscured = Get-RcloneObscuredPassword -RclonePath $rclone -Password $Password

    & $rclone config delete $RemoteName 2>$null | Out-Null
    & $rclone config create $RemoteName webdav `
        url $davUrl `
        vendor other `
        user $Username `
        pass $passObscured `
        --non-interactive | Out-Null

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to configure rclone remote '$RemoteName'"
    }

    Write-InstallLog "Configured rclone remote '$RemoteName' -> $davUrl"
    Test-RcloneRemote -RemoteName $RemoteName
}

function Test-RcloneRemote {
    param([Parameter(Mandatory = $true)][string]$RemoteName)

    $rclone = Resolve-RclonePath
    Write-InstallLog "Testing rclone remote '$RemoteName'"
    $output = & $rclone lsd "${RemoteName}:" 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "rclone remote test failed: $output"
    }
    Write-InstallLog "rclone remote test OK"
}

function Get-MountLogTail {
    param([int]$Lines = 20)
    if (-not (Test-Path $script:MountLog)) {
        return "No mount log yet."
    }
    return (Get-Content -Path $script:MountLog -Tail $Lines -ErrorAction SilentlyContinue) -join [Environment]::NewLine
}

function Start-RcloneMount {
    param(
        [Parameter(Mandatory = $true)][string]$RemoteName,
        [Parameter(Mandatory = $true)][string]$DriveLetter,
        [string]$CacheDir = $script:CacheDir
    )

    Ensure-ClientDirectories
    Ensure-WinFspReady
    Enable-WebDavClient

    $letter = $DriveLetter.Substring(0, 1).ToUpperInvariant()
    if (Test-DriveMounted -DriveLetter $letter) {
        Write-InstallLog "Drive ${letter}: already mounted" "WARN"
        return
    }

    Stop-MountProcess -DriveLetter $letter
    Test-RcloneRemote -RemoteName $RemoteName

    $rclone = Resolve-RclonePath
    $mountTarget = "$letter`:"
    if (Test-Path $script:MountLog) {
        Remove-Item -Path $script:MountLog -Force -ErrorAction SilentlyContinue
    }

    $args = @(
        "mount",
        "$RemoteName`:",
        $mountTarget,
        "--vfs-cache-mode", "full",
        "--vfs-cache-max-age", "1h",
        "--vfs-write-back", "10s",
        "--vfs-read-chunk-size", "32M",
        "--vfs-read-chunk-size-limit", "256M",
        "--dir-cache-time", "30s",
        "--poll-interval", "15s",
        "--cache-dir", $CacheDir,
        "--volname", "File Sorting",
        "--links",
        "--timeout", "60s",
        "--attr-timeout", "30s",
        "--log-file", $script:MountLog,
        "--log-level", "INFO"
    )

    Write-InstallLog "Starting rclone mount on ${letter}: (WinFsp drive, on-demand cache)"
    $process = Start-Process -FilePath $rclone -ArgumentList $args -PassThru -WindowStyle Hidden
    Set-Content -Path $script:MountPidFile -Value $process.Id -Encoding ASCII

    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            throw "rclone mount exited early (code=$($process.ExitCode)).`n$(Get-MountLogTail)"
        }
        if (Test-DriveMounted -DriveLetter $letter) {
            Write-InstallLog "Mount ready at ${letter}: (This PC -> File Sorting)"
            return
        }
        Start-Sleep -Milliseconds 700
    }

    throw "Mount did not become ready within 45 seconds.`n$(Get-MountLogTail)"
}

function Start-WebDavNetUseMount {
    param(
        [Parameter(Mandatory = $true)][string]$DavUrl,
        [Parameter(Mandatory = $true)][string]$Username,
        [Parameter(Mandatory = $true)][string]$Password,
        [Parameter(Mandatory = $true)][string]$DriveLetter
    )

    Enable-WebDavClient
    $letter = $DriveLetter.Substring(0, 1).ToUpperInvariant()
    $target = "$letter`:"
    $davUrl = Normalize-DavUrl -Url $DavUrl

    Stop-MountProcess -DriveLetter $letter
    Write-InstallLog "Fallback mount via Windows WebClient -> $davUrl"

    cmd.exe /c "net use $target `"$davUrl`" /user:$Username $Password /persistent:yes" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Windows WebDAV mapping failed for $target"
    }
    if (-not (Test-DriveMounted -DriveLetter $letter)) {
        throw "Windows WebDAV mapping did not create $target"
    }
    Write-InstallLog "Fallback WebDAV mount ready at ${letter}:"
}

function Start-FileSortingMount {
    param(
        [Parameter(Mandatory = $true)]$State
    )

    try {
        Start-RcloneMount -RemoteName $State.remote_name -DriveLetter $State.drive_letter -CacheDir $CacheDir
    }
    catch {
        Write-InstallLog "rclone mount failed, trying Windows WebDAV fallback: $($_.Exception.Message)" "WARN"
        Start-WebDavNetUseMount `
            -DavUrl $State.dav_url `
            -Username $State.webdav_username `
            -Password (Get-WebDavPasswordFromState -State $State) `
            -DriveLetter $State.drive_letter
    }
}

function Get-WebDavPasswordFromState {
    param([Parameter(Mandatory = $true)]$State)

    if ($State.PSObject.Properties.Name -contains "webdav_password" -and $State.webdav_password) {
        return [string]$State.webdav_password
    }

    $apiBase = [string]$State.api_base_url
    $token = [string]$State.api_token
    $credentials = Invoke-ApiJson -Url "$apiBase/windows/credentials" -Token $token
    return [string]$credentials.password
}

function Invoke-CacheCleanup {
    param(
        [Parameter(Mandatory = $true)][string]$CacheDir,
        [int]$MaxAgeHours = 1
    )

    if (-not (Test-Path $CacheDir)) {
        return
    }

    $cutoff = (Get-Date).AddHours(-1 * $MaxAgeHours)
    $removed = 0
    Get-ChildItem -Path $CacheDir -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.LastAccessTime -lt $cutoff) {
            Remove-Item -Path $_.FullName -Force -ErrorAction SilentlyContinue
            $removed++
        }
    }
    Write-InstallLog "Cache cleanup removed $removed stale file(s) from $CacheDir"
}

function Register-ClientScheduledTasks {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptsDir,
        [ValidateSet("sync", "mount", "hybrid")][string]$Mode = "sync"
    )

    $startScript = Join-Path $ScriptsDir "start-mount.ps1"
    $syncScript = Join-Path $ScriptsDir "sync-local-folder.ps1"
    $cleanupScript = Join-Path $ScriptsDir "cleanup-cache.ps1"
    $updateScript = Join-Path $ScriptsDir "update-client.ps1"

    if ($Mode -in @("mount", "hybrid")) {
        schtasks /Delete /TN "FileSortingMount" /F 2>$null | Out-Null
        schtasks /Create /TN "FileSortingMount" /SC ONLOGON /RL HIGHEST /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$startScript`"" /F | Out-Null
    } else {
        schtasks /Delete /TN "FileSortingMount" /F 2>$null | Out-Null
    }

    if ($Mode -in @("sync", "hybrid")) {
        schtasks /Delete /TN "FileSortingLocalSync" /F 2>$null | Out-Null
        schtasks /Create /TN "FileSortingLocalSync" /SC MINUTE /MO 1 /RL HIGHEST /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$syncScript`"" /F | Out-Null

        schtasks /Delete /TN "FileSortingLocalSyncOnLogon" /F 2>$null | Out-Null
        schtasks /Create /TN "FileSortingLocalSyncOnLogon" /SC ONLOGON /RL HIGHEST /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$syncScript`"" /F | Out-Null
    } else {
        schtasks /Delete /TN "FileSortingLocalSync" /F 2>$null | Out-Null
        schtasks /Delete /TN "FileSortingLocalSyncOnLogon" /F 2>$null | Out-Null
    }

    schtasks /Delete /TN "FileSortingCacheCleanup" /F 2>$null | Out-Null
    schtasks /Create /TN "FileSortingCacheCleanup" /SC MINUTE /MO 15 /RL HIGHEST /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$cleanupScript`"" /F | Out-Null

    schtasks /Delete /TN "FileSortingAutoUpdate" /F 2>$null | Out-Null
    schtasks /Create /TN "FileSortingAutoUpdate" /SC MINUTE /MO 30 /RL HIGHEST /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$updateScript`"" /F | Out-Null

    Write-InstallLog "Registered scheduled tasks for mode '$Mode'"
}

function Invoke-LocalSync {
    param([Parameter(Mandatory = $true)]$State)

    $rclone = Resolve-RclonePath
    $remoteName = [string]$State.remote_name
    $localDir = if ($State.PSObject.Properties.Name -contains "local_sync_dir" -and $State.local_sync_dir) {
        [string]$State.local_sync_dir
    } else {
        Join-Path $env:USERPROFILE "File Sorting"
    }

    if (-not (Test-Path $localDir)) {
        New-Item -ItemType Directory -Path $localDir -Force | Out-Null
    }

    $commonArgs = @(
        "--create-empty-src-dirs",
        "--check-access",
        "--transfers", "8",
        "--checkers", "16",
        "--timeout", "120s",
        "--contimeout", "30s",
        "--retries", "2",
        "--log-file", $script:SyncLog,
        "--log-level", "INFO"
    )

    if (-not (Test-Path $script:SyncInitMarker)) {
        Write-InstallLog "Initial local sync (remote -> local): $localDir"
        $initArgs = @("sync", "$remoteName`:", $localDir) + $commonArgs
        & $rclone @initArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Initial sync failed (code=$LASTEXITCODE)."
        }
        Set-Content -Path $script:SyncInitMarker -Value (Get-Date -Format o) -Encoding ASCII
    }

    Write-InstallLog "Running two-way sync: $localDir <-> $remoteName`:"
    $bisyncArgs = @("bisync", "$remoteName`:", $localDir) + $commonArgs
    & $rclone @bisyncArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Bi-directional sync failed (code=$LASTEXITCODE)."
    }
}

function Copy-ClientScripts {
    param([Parameter(Mandatory = $true)][string]$SourceDir)

    Ensure-ClientDirectories
    $files = @(
        "install.ps1",
        "install.bat",
        "start-mount.ps1",
        "sync-local-folder.ps1",
        "stop-mount.ps1",
        "cleanup-cache.ps1",
        "update-client.ps1"
    )
    foreach ($name in $files) {
        $src = Join-Path $SourceDir $name
        if (Test-Path $src) {
            $dest = Join-Path $script:ScriptsDir $name
            $parent = Split-Path -Parent $dest
            if ($parent -and -not (Test-Path $parent)) {
                New-Item -ItemType Directory -Path $parent -Force | Out-Null
            }
            Copy-Item -Path $src -Destination $dest -Force
        }
    }
    $libSource = Join-Path $SourceDir "lib"
    if (Test-Path $libSource) {
        Copy-Item -Path $libSource -Destination (Join-Path $script:ScriptsDir "lib") -Recurse -Force
    }
}

function Open-MountedDrive {
    param([Parameter(Mandatory = $true)][string]$DriveLetter)
    $letter = $DriveLetter.Substring(0, 1).ToUpperInvariant()
    if (-not (Test-DriveMounted -DriveLetter $letter)) {
        throw "Drive ${letter}: is not mounted."
    }
    Start-Process "explorer.exe" -ArgumentList "$letter`:\"
}

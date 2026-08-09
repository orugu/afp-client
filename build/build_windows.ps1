#Requires -Version 5.1
<#
.SYNOPSIS
  Build FileSortingUploader.exe (the new cross-platform upload/download GUI)
  with PyInstaller on Windows. Separate from
  client/windows/build-manager.ps1, which builds FileSortingManager.exe
  (the existing rclone/WebDAV mount tool) -- the two ship side by side.
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$BuildDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ClientRoot = Split-Path -Parent $BuildDir
$OutDir = Join-Path $BuildDir "dist-windows"
$TargetDir = Join-Path $ClientRoot "windows"

Push-Location $ClientRoot
try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv is required."
    }

    uv sync
    uv pip install pyinstaller

    $entry = Join-Path $ClientRoot "src/file_sorting_client/gui.py"
    uv run pyinstaller `
        --noconfirm `
        --onefile `
        --windowed `
        --name FileSortingUploader `
        --distpath $OutDir `
        $entry

    Copy-Item -Path (Join-Path $OutDir "FileSortingUploader.exe") `
        -Destination (Join-Path $TargetDir "FileSortingUploader.exe") `
        -Force

    python (Join-Path $BuildDir "update_manifest.py") (Join-Path $TargetDir "uploader-manifest.json") "FileSortingUploader.exe"

    Write-Host "Built FileSortingUploader.exe"
}
finally {
    Pop-Location
}

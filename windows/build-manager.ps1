#Requires -Version 5.1
<#
.SYNOPSIS
  Build FileSortingManager.exe with PyInstaller on Windows.
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ClientWindows = Split-Path -Parent $MyInvocation.MyCommand.Path
$ClientRoot = Split-Path -Parent $ClientWindows
$OutDir = Join-Path $ClientWindows "dist"

Push-Location $ClientRoot
try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv is required."
    }

    uv sync
    uv pip install pyinstaller

    $entry = Join-Path $ClientRoot "src/file_sorting_client/windows/manager.py"
    uv run pyinstaller `
        --noconfirm `
        --onefile `
        --windowed `
        --name FileSortingManager `
        --distpath $OutDir `
        $entry

    Copy-Item -Path (Join-Path $OutDir "FileSortingManager.exe") `
        -Destination (Join-Path $ClientWindows "FileSortingManager.exe") `
        -Force

    $manifestPath = Join-Path $ClientWindows "manifest.json"
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.files -notcontains "FileSortingManager.exe") {
        $manifest.files += "FileSortingManager.exe"
        $manifest | ConvertTo-Json -Depth 4 | Set-Content $manifestPath -Encoding UTF8
    }

    Write-Host "Built FileSortingManager.exe"
}
finally {
    Pop-Location
}

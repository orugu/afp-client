Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

param(
    [string]$AppBaseUrl = "https://devlovers.cloud/file_managing",
    [string]$ApiToken = "",
    [ValidateSet("sync", "mount", "hybrid")][string]$Mode = "sync",
    [string]$LocalSyncDir = "$env:USERPROFILE\File Sorting"
)

$ProgramDataRoot = Join-Path $env:ProgramData "FileSortingClient"
$InstallLog = Join-Path $ProgramDataRoot "install.log"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallScript = Join-Path $ScriptRoot "install.ps1"

$form = New-Object System.Windows.Forms.Form
$form.Text = "File Sorting 설치"
$form.Size = New-Object System.Drawing.Size(760, 520)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false

$title = New-Object System.Windows.Forms.Label
$title.Text = "File Sorting Client 설치 상태"
$title.AutoSize = $true
$title.Location = New-Object System.Drawing.Point(20, 15)
$title.Font = New-Object System.Drawing.Font("Malgun Gothic", 12, [System.Drawing.FontStyle]::Bold)

$status = New-Object System.Windows.Forms.Label
$status.Text = "설치 준비 중..."
$status.AutoSize = $true
$status.Location = New-Object System.Drawing.Point(20, 48)
$status.Font = New-Object System.Drawing.Font("Malgun Gothic", 10)

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Location = New-Object System.Drawing.Point(20, 75)
$progress.Size = New-Object System.Drawing.Size(700, 20)
$progress.Style = "Marquee"

$logBox = New-Object System.Windows.Forms.TextBox
$logBox.Location = New-Object System.Drawing.Point(20, 110)
$logBox.Size = New-Object System.Drawing.Size(700, 320)
$logBox.Multiline = $true
$logBox.ScrollBars = "Vertical"
$logBox.ReadOnly = $true
$logBox.Font = New-Object System.Drawing.Font("Consolas", 9)

$closeBtn = New-Object System.Windows.Forms.Button
$closeBtn.Text = "닫기"
$closeBtn.Location = New-Object System.Drawing.Point(640, 445)
$closeBtn.Size = New-Object System.Drawing.Size(80, 30)
$closeBtn.Enabled = $false
$closeBtn.Add_Click({ $form.Close() })

$form.Controls.Add($title)
$form.Controls.Add($status)
$form.Controls.Add($progress)
$form.Controls.Add($logBox)
$form.Controls.Add($closeBtn)

function Resolve-KoreanStatus {
    param([string]$line)

    if (-not $line) { return "설치 진행 중..." }
    $text = $line.ToLowerInvariant()

    if ($text.Contains("fetching bootstrap")) { return "서버 설정 정보를 가져오는 중..." }
    if ($text.Contains("fetching webdav credentials")) { return "WebDAV 계정 정보를 가져오는 중..." }
    if ($text.Contains("installing winfsp")) { return "WinFsp 설치 중..." }
    if ($text.Contains("installing rclone")) { return "rclone 설치 중..." }
    if ($text.Contains("configured rclone remote")) { return "동기화 연결 설정 완료" }
    if ($text.Contains("initial local sync")) { return "초기 동기화(서버 -> 내 PC) 진행 중..." }
    if ($text.Contains("running two-way sync")) { return "양방향 동기화 준비 중..." }
    if ($text.Contains("registered scheduled tasks")) { return "자동 동기화 작업 등록 중..." }
    if ($text.Contains("install completed")) { return "설치가 완료되었습니다." }
    if ($text.Contains("error")) { return "오류가 발생했습니다. 로그를 확인하세요." }
    return "설치 진행 중..."
}

try {
    if (-not (Test-Path $InstallScript)) {
        throw "install.ps1 파일을 찾을 수 없습니다."
    }

    if (-not $ApiToken) {
        throw "API 토큰이 필요합니다. 설치 배치 파일을 다시 다운로드해서 실행하세요."
    }

    $argList = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $InstallScript,
        "-AppBaseUrl", $AppBaseUrl,
        "-ApiToken", $ApiToken,
        "-Mode", $Mode,
        "-LocalSyncDir", $LocalSyncDir
    )

    $status.Text = "관리자 권한 설치 시작 중..."
    $installProc = Start-Process powershell.exe -Verb RunAs -ArgumentList $argList -PassThru -WindowStyle Hidden

    $timer = New-Object System.Windows.Forms.Timer
    $timer.Interval = 700

    $lastText = ""
    $timer.Add_Tick({
        if (Test-Path $InstallLog) {
            $lines = Get-Content -Path $InstallLog -Tail 120 -ErrorAction SilentlyContinue
            if ($lines) {
                $joined = ($lines -join [Environment]::NewLine)
                if ($joined -ne $lastText) {
                    $lastText = $joined
                    $logBox.Text = $joined
                    $logBox.SelectionStart = $logBox.TextLength
                    $logBox.ScrollToCaret()
                }
                $status.Text = Resolve-KoreanStatus -line $lines[-1]
            }
        }

        if ($installProc.HasExited) {
            $timer.Stop()
            $progress.Style = "Blocks"
            $progress.Value = 100
            $closeBtn.Enabled = $true

            if ($installProc.ExitCode -eq 0) {
                $status.Text = "설치 완료. 동기화가 시작됩니다."
                [System.Windows.Forms.MessageBox]::Show("설치가 완료되었습니다.", "완료", "OK", "Information") | Out-Null
            } else {
                $status.Text = "설치 실패. 로그를 확인하세요."
                [System.Windows.Forms.MessageBox]::Show("설치 중 오류가 발생했습니다.`n$InstallLog", "오류", "OK", "Error") | Out-Null
            }
        }
    })

    $timer.Start()
}
catch {
    $progress.Style = "Blocks"
    $closeBtn.Enabled = $true
    $status.Text = "실행 실패"
    $logBox.Text = $_.Exception.Message
}

[void]$form.ShowDialog()

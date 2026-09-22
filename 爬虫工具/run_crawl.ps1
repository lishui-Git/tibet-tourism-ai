# ============================================================
#  Ctrip Tibet review collection - guardian script (v5)
#  - single instance lock
#  - auto restart on crash (already-collected reviews skipped by comment ID)
#  - uses pending.txt if present (only not-yet-collected spots), else spots.txt
#  - after finishing: parse + merge + quality report
#  Usage: right click -> Run with PowerShell
# ============================================================
$ErrorActionPreference = 'Continue'
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force -Path 'data' | Out-Null

$err = 'data\errorlog.txt'

# ---------- single instance lock ----------
$me = $PID
$others = @()
try {
    $others = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*run_crawl.ps1*' -and $_.ProcessId -ne $me }
} catch { }
if ($others.Count -gt 0) {
    "another collector is running, exit." | Out-File -Append -Encoding utf8 $err
    exit 0
}

$py = Get-Process python -ErrorAction SilentlyContinue
if ($py) {
    "found stale python, killing." | Out-File -Append -Encoding utf8 $err
    $py | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

"===== guardian start PID=$me $(Get-Date -Format 'MM-dd HH:mm:ss') =====" | Out-File -Append -Encoding utf8 $err

$usePending = Test-Path 'pending_tibet.txt'
$pendingFile = 'pending_tibet.txt'
if (-not $usePending) {
    $usePending = Test-Path 'pending.txt'
    $pendingFile = 'pending.txt'
}

for ($i = 1; $i -le 60; $i++) {
    "----- round $i start $(Get-Date -Format 'MM-dd HH:mm:ss') pending=$pendingFile -----" | Out-File -Append -Encoding utf8 $err
    if ($usePending) {
        python ctrip_tools.py api --spots $pendingFile 2>> $err
    } else {
        python ctrip_tools.py api 2>> $err
    }
    $code = $LASTEXITCODE
    "----- round $i end code=$code $(Get-Date -Format 'MM-dd HH:mm:ss') -----" | Out-File -Append -Encoding utf8 $err
    if ($code -eq 0) { break }
    Start-Sleep -Seconds 30
}

python ctrip_tools.py parse 2>> $err
python ctrip_tools.py quality 2>> $err

"===== ALL DONE $(Get-Date -Format 'MM-dd HH:mm:ss') =====" | Out-File -Append -Encoding utf8 $err

Copy-Item 'data\out\合并_总表.csv' '..\旅游评论数据集_扩充版.csv' -Force -ErrorAction SilentlyContinue
Copy-Item 'data\out\质量报告.md' '..\数据质量报告.md' -Force -ErrorAction SilentlyContinue

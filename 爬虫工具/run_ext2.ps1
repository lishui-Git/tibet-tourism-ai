# ============================================================
#  Extension collector: Jinzang route (non-Tibet) spots
#  Data goes to data\raw_ext and is parsed into a SEPARATE csv.
#  It is NOT merged into the Tibet dataset.
#  Usage: right click -> Run with PowerShell
# ============================================================
$ErrorActionPreference = 'Continue'
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force -Path 'data' | Out-Null

$err = 'data\errorlog_ext.txt'
$me = $PID

$others = @()
try {
    $others = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*run_ext.ps1*' -and $_.ProcessId -ne $me }
} catch { }
if ($others.Count -gt 0) {
    "another ext collector is running, exit." | Out-File -Append -Encoding utf8 $err
    exit 0
}

"===== ext guardian start PID=$me $(Get-Date -Format 'MM-dd HH:mm:ss') =====" | Out-File -Append -Encoding utf8 $err

for ($i = 1; $i -le 20; $i++) {
    "----- ext round $i $(Get-Date -Format 'MM-dd HH:mm:ss') -----" | Out-File -Append -Encoding utf8 $err
    python ctrip_tools.py api --ext --spots ext_pending2.txt 2>> $err
    $code = $LASTEXITCODE
    "----- ext round $i end code=$code $(Get-Date -Format 'MM-dd HH:mm:ss') -----" | Out-File -Append -Encoding utf8 $err
    if ($code -eq 0) { break }
    Start-Sleep -Seconds 30
}

python ctrip_tools.py parse-ext 2>> $err
"===== EXT DONE $(Get-Date -Format 'MM-dd HH:mm:ss') =====" | Out-File -Append -Encoding utf8 $err
Copy-Item 'data\out\???_??????.csv' '..\??????????????csv' -Force -ErrorAction SilentlyContinue


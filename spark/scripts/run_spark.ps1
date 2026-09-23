# =============================================================================
#  Stage 3 - build and run the Spark offline analysis module (C-SPK-01~05)
#
#  ENCODING: this file must stay ASCII-only.
#  Windows PowerShell 5.1 reads .ps1 files with the system ANSI code page; on a
#  Chinese Windows that is GBK, so non-ASCII bytes in a UTF-8 file without a BOM
#  break the parser. Chinese documentation lives in spark/README.md.
#
#  What it does:
#    1) builds the module with Maven (skip with -SkipBuild)
#    2) runs it with spark-submit, wiring in:
#         - the MySQL JDBC driver via a jars list
#         - the runtime classpath (jieba, mysql driver) via -cp
#         - JVM memory suitable for JDK 1.8: -Xms512m -Xmx2g
#
#  Usage examples (run from the project root):
#    # small sample, no DB writes (safest first step)
#    powershell -ExecutionPolicy Bypass -File spark\scripts\run_spark.ps1 -Source csv -Limit 2000 -SkipDbWrite
#    # full run, all stages, write results
#    powershell -ExecutionPolicy Bypass -File spark\scripts\run_spark.ps1 -Stage all
#    # only the statistics stage against MySQL
#    powershell -ExecutionPolicy Bypass -File spark\scripts\run_spark.ps1 -Stage 2
# =============================================================================

param(
    [string]$Source = 'mysql',
    [string]$Stage = 'all',
    [int]$Limit = 0,                 # 0 = no limit (full run)
    [switch]$SkipDbWrite,
    [switch]$SkipBuild,
    [string]$Master = 'local[*]'
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ---- paths (absolute; this script must not depend on the current directory) ----
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$SparkDir    = Split-Path -Parent $ScriptDir            # ...\spark
$ProjectRoot = Split-Path -Parent $SparkDir             # ...\tibet-tourism-ai

$Mvn         = 'D:\bigdata\apache-maven-3.9.9\bin\mvn.cmd'
$Jar         = Join-Path $SparkDir 'target\spark-offline-analysis-1.0.0.jar'
$Classes     = Join-Path $SparkDir 'target\classes'
$JdbcDriver  = 'D:\bigdata\jars\mysql-connector-j-8.0.33.jar'
$MainClass   = 'com.tibet.tourism.spark.Main'

function Fail($msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }

Write-Host '======================================================================' -ForegroundColor Cyan
Write-Host ' Spark offline analysis (C-SPK-01~05) - build and run' -ForegroundColor Cyan
Write-Host '======================================================================' -ForegroundColor Cyan
Write-Host "  project root : $ProjectRoot"
Write-Host "  source       : $Source"
Write-Host "  stage        : $Stage"
Write-Host "  limit        : $(if ($Limit -gt 0) { $Limit } else { 'none (full)' })"
Write-Host "  write DB     : $(-not $SkipDbWrite)"
Write-Host "  master       : $Master"

# ---------------------------------------------------------------------------
# 1) Build
# ---------------------------------------------------------------------------
if (-not $SkipBuild) {
    if (-not (Test-Path $Mvn)) { Fail "Maven not found: $Mvn (run spark\tools\setup_spark_env.ps1 first)" }
    $env:JAVA_HOME = 'D:\programming\JAVA\jdk'
    Write-Host "`n[1/2] Maven build..." -ForegroundColor Cyan
    Push-Location $SparkDir
    try {
        & $Mvn -B clean package 2>&1 | Select-String -Pattern 'BUILD|ERROR|\.scala:' | ForEach-Object { "    $($_.Line)" }
        if ($LASTEXITCODE -ne 0) { Fail 'Maven build failed' }
    } finally { Pop-Location }
} else {
    Write-Host "`n[1/2] Maven build skipped (-SkipBuild)" -ForegroundColor Yellow
}

if (-not (Test-Path $Jar)) { Fail "job jar not found: $Jar" }

# ---------------------------------------------------------------------------
# 2) Assemble the runtime classpath
#     The shaded job jar already contains jieba, so only the MySQL JDBC driver has
#     to be supplied externally. Passing a SECOND jar through --jars is unreliable on
#     Windows (observed "java.io.IOException: Invalid argument"), and the local .m2
#     path contains non-ASCII characters, so we keep --jars to exactly one entry.
# ---------------------------------------------------------------------------
if (-not (Test-Path $JdbcDriver)) { Fail "JDBC driver not found: $JdbcDriver (run spark\tools\setup_spark_env.ps1)" }

$sparkArgs = @(
    '--class', $MainClass,
    '--master', $Master,
    # Heap size must go through --driver-memory: spark-submit rejects -Xms/-Xmx
    # inside extraJavaOptions ("Not allowed to specify max heap(Xmx)...").
    '--driver-memory', '2g',
    '--driver-java-options', '-Dfile.encoding=UTF-8',
    '--jars', $JdbcDriver,
    $Jar,
    '--source', $Source,
    '--stage', $Stage
)
if ($Limit -gt 0)    { $sparkArgs += @('--limit', "$Limit") }
if ($SkipDbWrite)    { $sparkArgs += '--skip-db-write' }

Write-Host "`n[2/2] spark-submit..." -ForegroundColor Cyan
Write-Host ("    " + ($sparkArgs -join ' ')) -ForegroundColor DarkGray

Push-Location $ProjectRoot
try {
    & spark-submit @sparkArgs
    $code = $LASTEXITCODE
} finally { Pop-Location }

Write-Host ''
if ($code -eq 0) {
    Write-Host '======================================================================' -ForegroundColor Green
    Write-Host ' Done: exit code 0' -ForegroundColor Green
    Write-Host '======================================================================' -ForegroundColor Green
} else {
    Write-Host " Failed: exit code $code" -ForegroundColor Red
}
exit $code

# =============================================================================
#  Stage 3 - Spark environment setup (idempotent, re-runnable)
#
#  Purpose: download and install the two external dependencies that the frozen
#  design requires for the Spark module:
#     1) Apache Maven          -- build tool required by spark/README.md section 2
#     2) MySQL Connector/J 8.0.33 -- required dependency coordinate (same section)
#
#  Why a script instead of manual steps:
#     - it documents HOW the environment was built (useful for the thesis defence);
#     - it is safe to re-run: anything already present is skipped;
#     - it performs no destructive action and does not touch system settings.
#
#  NOTE ON ENCODING (important, this file must stay ASCII-only):
#     Windows PowerShell 5.1 reads .ps1 files using the system ANSI code page.
#     On a Chinese Windows that is GBK, so a UTF-8 file WITHOUT a BOM has its
#     non-ASCII bytes mis-decoded and the script fails with a parser error.
#     The project already hit this class of problem for requirements.txt, so this
#     script deliberately keeps ALL text ASCII. Chinese docs live in spark/README.md.
#
#  Usage (run from the project root):
#     powershell -ExecutionPolicy Bypass -File spark\tools\setup_spark_env.ps1
#  Verify only (no download):
#     powershell -ExecutionPolicy Bypass -File spark\tools\setup_spark_env.ps1 -VerifyOnly
# =============================================================================

param(
    [switch]$VerifyOnly,
    [string]$InstallRoot = 'D:\bigdata',
    [string]$MavenVersion = '3.9.9'
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# Maven Central (verified reachable: HTTP 200)
$MavenUrl     = "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/$MavenVersion/apache-maven-$MavenVersion-bin.zip"
# mysql-connector-j 8.0.33: MySQL 8 requires an 8.x driver (project analysis 8.4 K4/K5)
$ConnectorUrl = "https://repo.maven.apache.org/maven2/com/mysql/mysql-connector-j/8.0.33/mysql-connector-j-8.0.33.jar"

$MavenHome    = Join-Path $InstallRoot "apache-maven-$MavenVersion"
$MavenBin     = Join-Path $MavenHome 'bin\mvn.cmd'
$ConnectorDir = Join-Path $InstallRoot 'jars'
$ConnectorJar = Join-Path $ConnectorDir 'mysql-connector-j-8.0.33.jar'

function Write-Step($msg) { Write-Host "`n[STEP] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Skip($msg) { Write-Host "  [SKIP] $msg" -ForegroundColor Yellow }

Write-Host '======================================================================' -ForegroundColor Cyan
Write-Host ' Spark environment setup: Maven + MySQL Connector/J 8.0.33' -ForegroundColor Cyan
Write-Host '======================================================================' -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# 0) Inspect current state
# ---------------------------------------------------------------------------
Write-Step '0/4 Inspect current environment'

$existingMvn = Get-Command mvn -ErrorAction SilentlyContinue
if ($existingMvn) {
    Write-Ok "Maven already on PATH: $($existingMvn.Source)"
} elseif (Test-Path $MavenBin) {
    Write-Ok "Installed at $MavenHome (not on PATH; call it by absolute path)"
} else {
    Write-Host '  Maven not found; it will be installed.'
}

if (Test-Path $ConnectorJar) {
    Write-Ok "MySQL driver present: $ConnectorJar"
} else {
    Write-Host '  mysql-connector-j-8.0.33.jar not found; it will be downloaded.'
}

if ($VerifyOnly) {
    Write-Host "`n[VERIFY-ONLY] Skipping download and install." -ForegroundColor Yellow
    if (Test-Path $MavenBin) { & $MavenBin -v | Select-Object -First 1 }
    if (Test-Path $ConnectorJar) { Write-Ok "Driver ready: $ConnectorJar" }
    exit 0
}

# ---------------------------------------------------------------------------
# 1) Install root
# ---------------------------------------------------------------------------
Write-Step "1/4 Prepare install root $InstallRoot"
if (-not (Test-Path $InstallRoot)) {
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    Write-Ok "Created $InstallRoot"
} else {
    Write-Ok "$InstallRoot already exists"
}

# ---------------------------------------------------------------------------
# 2) Download and unpack Maven
# ---------------------------------------------------------------------------
Write-Step "2/4 Maven $MavenVersion"
if (Test-Path $MavenBin) {
    Write-Skip "Already installed: $MavenHome"
} else {
    $zip = Join-Path $env:TEMP "apache-maven-$MavenVersion-bin.zip"
    Write-Host "  Downloading $MavenUrl"
    Invoke-WebRequest -Uri $MavenUrl -OutFile $zip -UseBasicParsing
    Write-Ok ("Downloaded: {0:N1} MB" -f ((Get-Item $zip).Length / 1MB))

    Expand-Archive -Path $zip -DestinationPath $InstallRoot -Force
    Remove-Item $zip -Force
    if (-not (Test-Path $MavenBin)) { throw "mvn.cmd not found after unpack: $MavenBin" }
    Write-Ok "Unpacked to $MavenHome"
}

# ---------------------------------------------------------------------------
# 3) Download MySQL Connector/J
# ---------------------------------------------------------------------------
Write-Step '3/4 MySQL Connector/J 8.0.33'
if (Test-Path $ConnectorJar) {
    Write-Skip "Already present: $ConnectorJar"
} else {
    if (-not (Test-Path $ConnectorDir)) {
        New-Item -ItemType Directory -Path $ConnectorDir -Force | Out-Null
    }
    Write-Host "  Downloading $ConnectorUrl"
    Invoke-WebRequest -Uri $ConnectorUrl -OutFile $ConnectorJar -UseBasicParsing
    Write-Ok ("Downloaded: {0:N0} KB" -f ((Get-Item $ConnectorJar).Length / 1KB))
}

# ---------------------------------------------------------------------------
# 4) Verify
# ---------------------------------------------------------------------------
Write-Step '4/4 Verify installation'

# PowerShell quirk: with $ErrorActionPreference='Stop', a native command that writes to
# stderr (java -version and spark-submit both do) is treated as a TERMINATING error even
# when it succeeds. Verification output is informational, so relax the preference here.
$ErrorActionPreference = 'Continue'

Write-Host '  --- Maven ---'
& $MavenBin -v 2>&1 | Select-Object -First 3 | ForEach-Object { "    $_" }

Write-Host '  --- JDK (must be 1.8) ---'
& "$env:JAVA_HOME\bin\java.exe" -version 2>&1 | Select-Object -First 1 | ForEach-Object { "    $_" }

Write-Host '  --- Spark ---'
# On this machine SPARK_HOME is empty (Spark is provided via PATH), so prefer PATH.
$sparkSubmit = (Get-Command spark-submit -ErrorAction SilentlyContinue).Source
if (-not $sparkSubmit -and $env:SPARK_HOME) {
    $sparkSubmit = Join-Path $env:SPARK_HOME 'bin\spark-submit.cmd'
}
if ($sparkSubmit) {
    & $sparkSubmit --version 2>&1 |
        Select-String -Pattern 'version 3|Scala version' | ForEach-Object { "    $($_.Line.Trim())" }
} else {
    Write-Host '    [WARN] spark-submit not found on PATH nor via SPARK_HOME' -ForegroundColor Yellow
}

Write-Host '  --- MySQL driver ---'
"    $ConnectorJar"
$ErrorActionPreference = 'Stop'

Write-Host "`n======================================================================" -ForegroundColor Green
Write-Host ' Environment ready.' -ForegroundColor Green
Write-Host ' Call Maven by absolute path (this script does not change system PATH):' -ForegroundColor Green
Write-Host "   $MavenBin" -ForegroundColor Green
Write-Host ' Pass the JDBC driver to Spark jobs with --jars:' -ForegroundColor Green
Write-Host "   --jars `"$ConnectorJar`"" -ForegroundColor Green
Write-Host '======================================================================' -ForegroundColor Green

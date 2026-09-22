<#
================================================================================
 开发辅助脚本（阶段一）
 用途：在 VS Code 终端中一键完成「激活虚拟环境 → 环境自检 → 启动 Flask 开发服务器」。

 用法（在本项目根目录 E:\tibet-tourism-ai 下执行）：
     powershell -ExecutionPolicy Bypass -File scripts\run_dev.ps1

 说明：
   · 本脚本不做任何破坏性操作，也不修改数据库。
   · 首次运行前请确认已执行过：
       python -m venv .venv
       .\.venv\Scripts\python.exe -m pip install -r requirements.txt
================================================================================
#>

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 切到项目根目录（脚本位于 scripts\ 下）
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host '======================================================================' -ForegroundColor Cyan
Write-Host ' 西藏旅游景点智能评价与分析系统 · 开发服务器启动脚本' -ForegroundColor Cyan
Write-Host '======================================================================' -ForegroundColor Cyan
Write-Host " 项目根目录：$ProjectRoot"

# 1) 虚拟环境检查
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    Write-Host ' [错误] 未找到虚拟环境 .venv' -ForegroundColor Red
    Write-Host '        请先执行：' -ForegroundColor Yellow
    Write-Host '          python -m venv .venv' -ForegroundColor Yellow
    Write-Host '          .\.venv\Scripts\python.exe -m pip install -r requirements.txt' -ForegroundColor Yellow
    exit 1
}
Write-Host ' [1/3] 虚拟环境已就绪：.venv' -ForegroundColor Green

# 2) 环境自检（Python 依赖 / .env / MySQL / 17 张表 / 数据导入现状）
Write-Host ' [2/3] 运行环境自检…' -ForegroundColor Green
& $VenvPython 'scripts\check_env.py'
if ($LASTEXITCODE -ne 0) {
    Write-Host ' [警告] 环境自检未全部通过，服务仍会尝试启动；请按上面提示处理。' -ForegroundColor Yellow
}

# 3) 启动 Flask 开发服务器
Write-Host ' [3/3] 启动 Flask 开发服务器（Ctrl+C 停止）…' -ForegroundColor Green
& $VenvPython 'run.py'

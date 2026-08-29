# Telnix 一键运行脚本
# Usage: .\run.ps1 [args...]
#
# 传递给 python -m telnix 的参数：
#   .\run.ps1                  # 默认（自动开浏览器）
#   .\run.ps1 --no-browser     # 不开浏览器
#   .\run.ps1 --port 18902     # 自定义端口

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path $PSScriptRoot).Path
$hostDir = Join-Path $projectRoot "src\host"

# 检查后端是否已安装
$telnixPkg = Join-Path $hostDir "telnix\__init__.py"
if (-not (Test-Path $telnixPkg)) {
    Write-Host "ERROR: telnix 包未找到: $telnixPkg" -ForegroundColor Red
    Write-Host "请先运行: .\install.ps1" -ForegroundColor Yellow
    exit 1
}

# 检查前端是否已构建（src\host\telnix\web 或 src\ui\dist）
$webDir = Join-Path $hostDir "telnix\web"
$uiDist = Join-Path $projectRoot "src\ui\dist"
if (-not (Test-Path $webDir) -and -not (Test-Path $uiDist)) {
    Write-Host "WARNING: 前端未构建，启动后浏览器会显示空白" -ForegroundColor Yellow
    Write-Host "请先运行: .\build.ps1" -ForegroundColor Yellow
    Write-Host ""
}

# ---------- 杀掉所有 Telnix 相关进程 ----------
Write-Host "[INFO] 正在清理 Telnix 相关进程..." -ForegroundColor Gray

$telnixProcPatterns = @("python%", "node%")
foreach ($pattern in $telnixProcPatterns) {
    Get-CimInstance Win32_Process -Filter "Name LIKE '$pattern'" | Where-Object {
        $cmdline = $_.CommandLine
        if ([string]::IsNullOrEmpty($cmdline)) { return $false }
        $cmdline -match "telnix|mcp_server|mitmproxy"
    } | ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "[OK] 已终止进程: $($_.Name) (PID $($_.ProcessId))" -ForegroundColor Gray
        } catch { }
    }
}

# ---------- 关闭残留进程（占用 18901 / 8888 端口）----------
foreach ($port in @(18901, 8888)) {
    try {
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        foreach ($conn in $conns) {
            $pid = $conn.OwningProcess
            if ($pid -and $pid -ne 0) {
                try {
                    $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
                    $procName = if ($proc) { $proc.ProcessName } else { "unknown" }
                    Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
                    Write-Host "[OK] 已关闭占用端口 $port 的残留进程: $procName (PID $pid)" -ForegroundColor Gray
                } catch { }
            }
        }
    } catch { }
}

# 等待端口释放
Start-Sleep -Milliseconds 500

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Starting Telnix..." -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Directory: $hostDir" -ForegroundColor Gray
Write-Host "  URL:       http://127.0.0.1:18901" -ForegroundColor Gray
Write-Host "  Args:      $args" -ForegroundColor Gray
Write-Host ""

Set-Location $hostDir
& python -m telnix @args

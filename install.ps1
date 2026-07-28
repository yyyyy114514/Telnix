# Telnix 一键安装脚本
# Usage: .\install.ps1
#
# 安装内容：
#   1. Python 后端依赖（src\host\pip install -e .）
#   2. Node.js 前端依赖（src\ui\npm install）
#   3. 构建前端产物（src\ui\dist → src\host\telnix\web）

param(
    [switch]$SkipUi,      # 跳过前端（只装后端）
    [switch]$SkipBuild    # 跳过构建（只装依赖）
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path $PSScriptRoot).Path

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Telnix Installer" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ---------- 查找系统 Python（避免用到 TRAE 内置 Python）----------
function Find-SystemPython {
    # 1. py launcher（官方 Windows Python 启动器，指向系统 Python）
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        try {
            $exe = & py -3 -c "import sys; print(sys.executable)" 2>$null
            if ($exe -and $exe -notmatch 'TRAE') {
                return $exe.Trim()
            }
        } catch { }
    }
    # 2. 搜索常见系统安装路径
    $candidates = @()
    $localApp = [Environment]::GetEnvironmentVariable('LOCALAPPDATA')
    if ($localApp) {
        $candidates += Get-ChildItem "$localApp\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch 'TRAE' } |
            Sort-Object Name -Descending |
            Select-Object -ExpandProperty FullName
    }
    $candidates += Get-ChildItem "C:\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch 'TRAE' } |
        Select-Object -ExpandProperty FullName
    $candidates += Get-ChildItem "C:\Program Files\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch 'TRAE' } |
        Select-Object -ExpandProperty FullName
    if ($candidates.Count -gt 0) {
        return $candidates[0]
    }
    # 3. 兜底：PATH 中的 python（可能不是系统 Python）
    return 'python'
}

# ---------- 检查 Python ----------
Write-Host "[0/4] Checking Python..." -ForegroundColor Yellow
$pythonExe = Find-SystemPython
try {
    $pyVer = & $pythonExe --version 2>&1
    if ($pyVer -notmatch "Python (\d+)\.(\d+)") { throw "无法识别 Python 版本" }
    $major = [int]$Matches[1]; $minor = [int]$Matches[2]
    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
        throw "Python $major.$minor 版本过低，需要 3.10+"
    }
    Write-Host "  OK: $pyVer ($pythonExe)" -ForegroundColor Green
} catch {
    Write-Host "  ERROR: $_" -ForegroundColor Red
    Write-Host "  请先安装 Python 3.10+: https://www.python.org/downloads/" -ForegroundColor Yellow
    exit 1
}

# ---------- 检查 Node.js ----------
if (-not $SkipUi) {
    Write-Host ""
    Write-Host "[0/4] Checking Node.js..." -ForegroundColor Yellow
    try {
        $nodeVer = node --version 2>&1
        if ($nodeVer -notmatch "v(\d+)") { throw "无法识别 Node 版本" }
        $major = [int]$Matches[1]
        if ($major -lt 18) {
            Write-Host "  WARNING: Node $nodeVer 版本过低，建议 18+" -ForegroundColor Yellow
        } else {
            Write-Host "  OK: $nodeVer" -ForegroundColor Green
        }
    } catch {
        Write-Host "  WARNING: Node.js 未安装，跳过前端构建" -ForegroundColor Yellow
        Write-Host "  如需修改前端，请安装 Node.js 18+: https://nodejs.org/" -ForegroundColor Yellow
        $SkipUi = $true
    }
}

# ---------- 1. 安装后端依赖 ----------
Write-Host ""
Write-Host "[1/4] Installing Python backend dependencies..." -ForegroundColor Yellow
$hostDir = Join-Path $projectRoot "src\host"
Set-Location $hostDir
& $pythonExe -m pip install -e . 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: pip install 失败" -ForegroundColor Red
    exit 1
}
Write-Host "  Backend deps installed" -ForegroundColor Green

# 可选依赖：IP 属地查询（非阻塞，失败不影响核心功能）
Write-Host "  Installing optional: py-ip2region (IP region lookup)..." -ForegroundColor Gray
& $pythonExe -m pip install py-ip2region 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  py-ip2region installed" -ForegroundColor Green
} else {
    Write-Host "  py-ip2region skipped (optional, IP region will be empty)" -ForegroundColor DarkYellow
}

# ---------- 2. 安装前端依赖 ----------
if (-not $SkipUi) {
    Write-Host ""
    Write-Host "[2/4] Installing Node.js frontend dependencies..." -ForegroundColor Yellow
    $uiDir = Join-Path $projectRoot "src\ui"
    Set-Location $uiDir
    if (-not (Test-Path "node_modules")) {
        npm install 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  ERROR: npm install 失败" -ForegroundColor Red
            exit 1
        }
    } else {
        Write-Host "  node_modules exists, skip npm install" -ForegroundColor Gray
    }
    Write-Host "  Frontend deps installed" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "[2/4] Skipping frontend dependencies (-SkipUi)" -ForegroundColor Gray
}

# ---------- 3. 构建前端 ----------
if (-not $SkipUi -and -not $SkipBuild) {
    Write-Host ""
    Write-Host "[3/4] Building frontend..." -ForegroundColor Yellow
    & (Join-Path $projectRoot "build.ps1") 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: 前端构建失败" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host ""
    Write-Host "[3/4] Skipping frontend build" -ForegroundColor Gray
}

# ---------- 4. 完成 ----------
Write-Host ""
Write-Host "[4/4] Done!" -ForegroundColor Green
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Installation complete!" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Start Telnix:  .\run.ps1" -ForegroundColor White
Write-Host "  2. Open browser:   http://127.0.0.1:18901" -ForegroundColor White
Write-Host ""
Write-Host "For TCP/UDP capture (admin required):" -ForegroundColor Gray
Write-Host "  python -m telnix.cli system restart-as-admin" -ForegroundColor Gray
Write-Host ""

#!/bin/bash
# Telnix 一键安装脚本 (Linux/macOS)
# Usage: ./install.sh
#
# 安装内容：
#   1. Python 后端依赖（src/host/pip install -e .）
#   2. Node.js 前端依赖（src/ui/npm install）
#   3. 构建前端产物（src/ui/dist → src/host/telnix/web）

set -e

SKIP_UI=false
SKIP_BUILD=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-ui) SKIP_UI=true; shift ;;
        --skip-build) SKIP_BUILD=true; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "========================================"
echo "  Telnix Installer"
echo "========================================"
echo ""

# ---------- 检查 Python ----------
echo "[0/4] Checking Python..."
if ! command -v python3 &> /dev/null; then
    echo "  ERROR: Python 3 not found"
    echo "  Please install Python 3.10+: https://www.python.org/downloads/"
    exit 1
fi

PY_VER=$(python3 --version 2>&1)
echo "  OK: $PY_VER"

# ---------- 检查 Node.js ----------
if [ "$SKIP_UI" = false ]; then
    echo ""
    echo "[0/4] Checking Node.js..."
    if ! command -v node &> /dev/null; then
        echo "  WARNING: Node.js not found, skipping frontend build"
        echo "  To modify frontend, install Node.js 18+: https://nodejs.org/"
        SKIP_UI=true
        SKIP_BUILD=true
    else
        NODE_VER=$(node --version 2>&1)
        echo "  OK: $NODE_VER"
    fi
fi

# ---------- 1. 安装后端依赖 ----------
echo ""
echo "[1/4] Installing Python backend dependencies..."
cd "$PROJECT_ROOT/src/host"
python3 -m pip install -e . 2>&1 || { echo "  ERROR: pip install failed"; exit 1; }
echo "  Backend deps installed"

# 可选依赖：IP 属地查询（非阻塞，失败不影响核心功能）
echo "  Installing optional: py-ip2region (IP region lookup)..."
python3 -m pip install py-ip2region 2>/dev/null || echo "  py-ip2region skipped (optional)"

# 下载 ip2region 数据库文件
echo "  Downloading ip2region database..."
GEOIP_DIR="$PROJECT_ROOT/data/geoip"
DB_FILE="$GEOIP_DIR/ip2region_v4.xdb"
if [ ! -f "$DB_FILE" ]; then
    mkdir -p "$GEOIP_DIR"
    DB_URL="https://gh-proxy.org/https://github.com/lionsoul2014/ip2region/raw/master/data/ip2region_v4.xdb"
    if command -v curl &> /dev/null; then
        curl -sL --connect-timeout 30 -o "$DB_FILE" "$DB_URL" 2>/dev/null && echo "  ip2region database downloaded" || echo "  ip2region database download failed (optional)"
    elif command -v wget &> /dev/null; then
        wget -q -O "$DB_FILE" "$DB_URL" 2>/dev/null && echo "  ip2region database downloaded" || echo "  ip2region database download failed (optional)"
    else
        echo "  ip2region database download failed (no curl/wget)"
    fi
else
    echo "  ip2region database exists, skip"
fi

# ---------- 2. 安装前端依赖 ----------
if [ "$SKIP_UI" = false ]; then
    echo ""
    echo "[2/4] Installing Node.js frontend dependencies..."
    cd "$PROJECT_ROOT/src/ui"
    if [ ! -d "node_modules" ]; then
        npm install 2>&1 || { echo "  ERROR: npm install failed"; exit 1; }
    else
        echo "  node_modules exists, skip npm install"
    fi
    echo "  Frontend deps installed"
else
    echo ""
    echo "[2/4] Skipping frontend dependencies (--skip-ui)"
fi

# ---------- 3. 构建前端 ----------
if [ "$SKIP_UI" = false ] && [ "$SKIP_BUILD" = false ]; then
    echo ""
    echo "[3/4] Building frontend..."
    cd "$PROJECT_ROOT"
    chmod +x build.sh
    ./build.sh 2>&1 || { echo "  ERROR: frontend build failed"; exit 1; }
else
    echo ""
    echo "[3/4] Skipping frontend build"
fi

# ---------- 4. 完成 ----------
echo ""
echo "[4/4] Done!"
echo ""
echo "========================================"
echo "  Installation complete!"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Start Telnix:  ./run.sh"
echo "  2. Open browser:   http://127.0.0.1:18901"
echo ""
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo "For TCP/UDP capture (admin required):"
    echo "  sudo python3 -m telnix.cli system restart-as-admin"
    echo ""
    echo "Note: On macOS, you may need to install WinDivert equivalent or run as root for raw capture."
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    echo "For TCP/UDP capture (admin/root required):"
    echo "  sudo python3 -m telnix.cli system restart-as-admin"
    echo ""
    echo "Note: On Linux, raw packet capture requires root or CAP_NET_RAW capability."
fi
echo ""

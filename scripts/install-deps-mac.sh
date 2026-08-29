#!/usr/bin/env bash
# Telnix macOS 依赖安装脚本
# 用法：bash install-deps-mac.sh
#
# 在 macOS 上安装 Telnix 运行所需的 Python 依赖。
# Windows 专属依赖（pydivert/WinDivert）不会安装，TCP/UDP 抓包功能在 macOS 上不可用，
# 但 HTTP 代理抓包、SSL bump、自动修改等核心功能可正常使用。
#
# macOS 证书安装：Telnix 会用 security add-trusted-cert 安装根证书到系统钥匙串，
# 安装时会弹窗要求用户输入密码授权。

set -e

# 脚本所在目录（用于定位 requirements.txt）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REQ_FILE="$PROJECT_ROOT/src/host/requirements.txt"

echo "========================================"
echo " Telnix macOS 依赖安装"
echo "========================================"
echo ""

# 检查 Python3（macOS 自带的 python3 可能版本较旧，推荐用 Homebrew 安装）
if ! command -v python3 >/dev/null 2>&1; then
    echo "[错误] 未找到 python3，请先安装 Python 3.10+"
    echo "  推荐用 Homebrew 安装："
    echo "    /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    echo "    brew install python"
    echo "  或从官网下载：https://www.python.org/downloads/macos/"
    exit 1
fi

# 显式校验 Python 版本 >= 3.10（与 pyproject.toml requires-python 一致）
PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if ! python3 -c 'import sys; assert sys.version_info >= (3,10)' 2>/dev/null; then
    echo "[错误] Python 版本过低，需要 3.10+（当前: $PYTHON_VERSION）"
    exit 1
fi
echo "[信息] Python 版本: $PYTHON_VERSION"

# 检查 pip
if ! python3 -m pip --version >/dev/null 2>&1; then
    echo "[错误] 未找到 pip，请重新安装 Python 或运行："
    echo "  python3 -m ensurepip --upgrade"
    exit 1
fi

# 检查 requirements.txt
if [ ! -f "$REQ_FILE" ]; then
    echo "[错误] 未找到 requirements.txt: $REQ_FILE"
    exit 1
fi

echo "[信息] requirements.txt: $REQ_FILE"
echo ""

# 安装核心依赖
echo "----------------------------------------"
echo " 安装核心依赖（requirements.txt）"
echo "----------------------------------------"
# 注意：requirements.txt 中的 pyinstaller 是打包工具，macOS 上也可安装
# mitmproxy 是可选依赖，pip 会自动处理
python3 -m pip install -r "$REQ_FILE" || {
    echo "[警告] 部分依赖安装失败，尝试跳过 mitmproxy 可选依赖..."
    # 如果 mitmproxy 安装失败，逐个安装除 mitmproxy 外的依赖
    grep -v '^\s*#' "$REQ_FILE" | grep -v '^\s*$' | grep -v mitmproxy | while read -r pkg; do
        echo "[信息] 安装: $pkg"
        python3 -m pip install "$pkg" || echo "[警告] $pkg 安装失败，跳过"
    done
}

echo ""
echo "----------------------------------------"
echo " 可选依赖说明"
echo "----------------------------------------"
echo "以下 Windows 专属依赖在 macOS 上不可用（不影响核心功能）："
echo "  - pydivert: TCP/UDP 抓包依赖（WinDivert 是 Windows 内核驱动）"
echo "  - winreg/ctypes.windll: 系统代理注册表操作（仅 Windows）"
echo ""
echo "可选增强依赖（按需安装）："
echo "  - mitmproxy: 已在 requirements.txt 中，提供更强的 HTTPS 拦截能力"
echo "    若未安装，Telnix 会自动回退到内置引擎"
echo ""

echo "----------------------------------------"
echo " macOS 特定说明"
echo "----------------------------------------"
echo "1. 证书安装：Telnix 会用 security add-trusted-cert 安装根证书到系统钥匙串，"
echo "   安装时会弹窗要求输入密码授权。"
echo ""
echo "2. 系统代理：macOS 上 Telnix 不会自动设置系统代理，需手动配置："
echo "   系统偏好设置 → 网络 → 高级 → 代理 → 网页代理(HTTP)/安全网页代理(HTTPS)"
echo "   填入 127.0.0.1:8888"
echo ""
echo "3. 防火墙：如需手机抓包，需在系统偏好设置 → 安全性与隐私 → 防火墙中放行 8888/18901 端口"
echo ""

echo "----------------------------------------"
echo " 安装完成"
echo "----------------------------------------"
echo ""
echo "启动 Telnix："
echo "  cd $PROJECT_ROOT/src/host"
echo "  python3 -m telnix"
echo ""

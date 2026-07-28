"""配置管理：端口、路径、进程名伪装。

进程伪装名从注册表 HKCU\\Software\\Telnix\\MasqueradeName 读取
（安装时由 Inno Setup 写入），读不到则用默认名。非 Windows 平台直接返回默认名。
向后兼容：若新键 Software\\Telnix 不存在，回退读取旧键 Software\\OpenNet
（旧版本安装写入的值），保证升级后伪装名不丢失。
"""

import os
import sys

# 平台判断：winreg 仅 Windows 可用，条件导入避免非 Windows 平台 ImportError
IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import winreg
else:
    winreg = None  # type: ignore[assignment]

# Web 后端端口
# 注意：18899/18900 在本机被 Windows 动态端口保留（bind 报 WSAEACCES=13，netstat 看不到），改用 18901
DEFAULT_PORT = 18901

# 代理服务器端口（Fiddler 默认 8888）
PROXY_PORT = 8888

# 代理监听地址（仅本地）
PROXY_HOST = "127.0.0.1"

# 默认进程伪装名
DEFAULT_MASQUERADE_NAME = "SystemMetrics.exe"

# 注册表键路径
REG_KEY = r"Software\Telnix"
# 旧版本注册表键路径（向后兼容：升级后旧键可能仍在）
LEGACY_REG_KEY = r"Software\OpenNet"


def get_masquerade_name() -> str:
    """从注册表读取进程伪装名，读不到用默认名。

    非 Windows 平台：直接返回默认名（无注册表）。
    向后兼容：先读新键 Software\\Telnix，读不到再回退读旧键 Software\\OpenNet
    （旧版本安装写入的值），保证升级后伪装名不丢失。
    """
    if not IS_WINDOWS:
        return DEFAULT_MASQUERADE_NAME
    # 依次尝试新键和旧键
    for reg_key in (REG_KEY, LEGACY_REG_KEY):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key)
            try:
                value, _ = winreg.QueryValueEx(key, "MasqueradeName")
                if value:
                    return value
            finally:
                winreg.CloseKey(key)
        except OSError:
            pass
    return DEFAULT_MASQUERADE_NAME


def get_port() -> int:
    """Web 后端端口（可由环境变量 TELNIX_PORT 覆盖）。"""
    return int(os.environ.get("TELNIX_PORT", DEFAULT_PORT))


def get_host() -> str:
    """Web 监听地址。

    跟随"允许局域网设备连接"开关：开启后返回 0.0.0.0，手机/局域网设备可访问
    API（扫码下载证书、安装证书等），否则仅本机访问。
    """
    # 与代理监听地址联动：开启局域网访问时 API 也监听 0.0.0.0
    env = os.environ.get("TELNIX_HOST", "").strip()
    if env:
        return env
    try:
        from . import settings_store
        v = settings_store.get_setting("proxy_listen_host", "")
        if v == "0.0.0.0":
            return "0.0.0.0"
    except Exception:  # noqa: BLE001
        pass
    return "127.0.0.1"


def get_proxy_host() -> str:
    """代理监听地址。

    从 settings.json 读 proxy_listen_host（设置页"允许局域网设备连接"开关控制）：
    - "0.0.0.0"：监听所有网卡，手机/局域网设备可连（安卓抓包必备）
    - "127.0.0.1"：仅本机（默认，安全）
    读不到则用环境变量 TELNIX_PROXY_HOST，再退回 127.0.0.1。
    """
    env = os.environ.get("TELNIX_PROXY_HOST", "").strip()
    if env:
        return env
    try:
        # 延迟导入避免循环依赖
        from . import settings_store
        v = settings_store.get_setting("proxy_listen_host", "")
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    return PROXY_HOST


def get_proxy_port() -> int:
    return int(os.environ.get("TELNIX_PROXY_PORT", PROXY_PORT))


def _project_root() -> str:
    """开发环境下定位项目根目录（src 的上一级）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    # telnix/config.py -> telnix -> host -> src -> 项目根
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


def get_data_dir() -> str:
    """数据目录（SQLite + 证书存放位置）。

    优先级：
    1. 环境变量 TELNIX_DATA_DIR（方便 Linux/WSL 用户把数据放到 ext4 分区，
       避免 /mnt/* 跨文件系统 SQLite I/O 错误）
    2. 打包后：%APPDATA%/Telnix（Windows）或 ~/.telnix（其他平台）
    3. 开发环境：<project_root>/data
    """
    env_dir = os.environ.get("TELNIX_DATA_DIR", "").strip()
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return env_dir
    if getattr(sys, "frozen", False):
        # 打包后：Windows 用 %APPDATA%/Telnix，其他平台用 ~/.telnix
        if IS_WINDOWS:
            base = os.environ.get("APPDATA", os.path.expanduser("~"))
            data_dir = os.path.join(base, "Telnix")
        else:
            data_dir = os.path.join(os.path.expanduser("~"), ".telnix")
    else:
        data_dir = os.path.join(_project_root(), "data")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def get_db_path() -> str:
    """SQLite 数据库文件路径。"""
    return os.path.join(get_data_dir(), "telnix.db")


def get_cert_dir() -> str:
    """证书目录（根证书 + 动态签发的域名证书）。"""
    cert_dir = os.path.join(get_data_dir(), "certs")
    os.makedirs(cert_dir, exist_ok=True)
    return cert_dir


def get_ui_dist_dir() -> str:
    """前端静态文件目录。打包后在 _internal/ui/dist，开发时在 src/ui/dist。"""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "_internal", "ui", "dist")
    here = os.path.dirname(os.path.abspath(__file__))
    # telnix/ -> host/ -> src/ -> src/ui/dist
    return os.path.abspath(os.path.join(here, "..", "..", "ui", "dist"))


def get_project_root() -> str:
    """项目根目录。打包后为 exe 同级目录，开发时为 src 的上一级。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return _project_root()


def get_docs_dir() -> str:
    """文档目录（CLASH_SET.md 引用的图片等资源）。打包后在 _internal/docs，开发时在项目根/docs。"""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "_internal", "docs")
    return os.path.join(_project_root(), "docs")


def get_tutorial_md_path() -> str:
    """Clash 教程 markdown 文件路径。打包后在 _internal/CLASH_SET.md，开发时在项目根/CLASH_SET.md。"""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "_internal", "CLASH_SET.md")
    return os.path.join(_project_root(), "CLASH_SET.md")

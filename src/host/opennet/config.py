"""配置管理：端口、路径、进程名伪装。

进程伪装名从注册表 HKCU\\Software\\OpenNet\\MasqueradeName 读取
（安装时由 Inno Setup 写入），读不到则用默认名。
"""

import os
import sys
import winreg

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
REG_KEY = r"Software\OpenNet"


def get_masquerade_name() -> str:
    """从注册表读取进程伪装名，读不到用默认名。"""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY)
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
    """Web 后端端口（可由环境变量 OPENNET_PORT 覆盖）。"""
    return int(os.environ.get("OPENNET_PORT", DEFAULT_PORT))


def get_host() -> str:
    """Web 监听地址。

    跟随"允许局域网设备连接"开关：开启后返回 0.0.0.0，手机/局域网设备可访问
    API（扫码下载证书、安装证书等），否则仅本机访问。
    """
    # 与代理监听地址联动：开启局域网访问时 API 也监听 0.0.0.0
    env = os.environ.get("OPENNET_HOST", "").strip()
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
    读不到则用环境变量 OPENNET_PROXY_HOST，再退回 127.0.0.1。
    """
    env = os.environ.get("OPENNET_PROXY_HOST", "").strip()
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
    return int(os.environ.get("OPENNET_PROXY_PORT", PROXY_PORT))


def _project_root() -> str:
    """开发环境下定位项目根目录（src 的上一级）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    # opennet/config.py -> opennet -> host -> src -> 项目根
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


def get_data_dir() -> str:
    """数据目录（SQLite + 证书存放位置）。"""
    if getattr(sys, "frozen", False):
        # 打包后用 %APPDATA%/OpenNet
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        data_dir = os.path.join(base, "OpenNet")
    else:
        data_dir = os.path.join(_project_root(), "data")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def get_db_path() -> str:
    """SQLite 数据库文件路径。"""
    return os.path.join(get_data_dir(), "opennet.db")


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
    # opennet/ -> host/ -> src/ -> src/ui/dist
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

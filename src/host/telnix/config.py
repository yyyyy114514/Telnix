"""Configuration management: ports, paths, process name masquerade.

The process masquerade name is read from the registry HKCU\\Software\\Telnix\\MasqueradeName
(written by Inno Setup at install time); if it cannot be read, the default name is used.
On non-Windows platforms the default name is returned directly.
Backward compatibility: if the new key Software\\Telnix does not exist, falls back to
reading the legacy key Software\\OpenNet (written by older installer versions) to ensure
the masquerade name is not lost after upgrade.
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
    """Read the process masquerade name from the registry, falling back to the default name.

    Non-Windows platforms: returns the default name directly (no registry).
    Backward compatibility: reads the new key Software\\Telnix first, then falls back to
    the legacy key Software\\OpenNet (written by older installer versions) to ensure the
    masquerade name is not lost after upgrade.
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
    """Web backend (API) port.

    Priority:
    1. TELNIX_PORT environment variable (highest, used by --mcp mode and overrides)
    2. settings.json `api_port` (set via settings page)
    3. DEFAULT_PORT (18901)
    """
    env = os.environ.get("TELNIX_PORT")
    if env:
        return int(env)
    try:
        # 延迟导入避免循环依赖
        from . import settings_store
        v = settings_store.get_setting("api_port")
        if v and int(v) > 0:
            return int(v)
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_PORT


def get_host() -> str:
    """Web listen address.

    Follows the "allow LAN devices to connect" toggle: when enabled returns 0.0.0.0 so
    that phones/LAN devices can access the API (scan to download certificate, install
    certificate, etc.); otherwise only localhost access is allowed.
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
    """Proxy listen address.

    Reads proxy_listen_host from settings.json (controlled by the "allow LAN devices
    to connect" toggle on the settings page):
    - "0.0.0.0": listen on all NICs, phones/LAN devices can connect (required for Android capture)
    - "127.0.0.1": localhost only (default, secure)
    Falls back to the TELNIX_PROXY_HOST environment variable, then to 127.0.0.1.
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
    """Proxy server port.

    Priority:
    1. TELNIX_PROXY_PORT environment variable (highest, used by --mcp mode and overrides)
    2. settings.json `proxy_port` (set via settings page)
    3. PROXY_PORT (8888)
    """
    env = os.environ.get("TELNIX_PROXY_PORT")
    if env:
        return int(env)
    try:
        # 延迟导入避免循环依赖
        from . import settings_store
        v = settings_store.get_setting("proxy_port")
        if v and int(v) > 0:
            return int(v)
    except Exception:  # noqa: BLE001
        pass
    return PROXY_PORT


def _project_root() -> str:
    """Locate the project root directory in the dev environment (parent of src)."""
    here = os.path.dirname(os.path.abspath(__file__))
    # telnix/config.py -> telnix -> host -> src -> 项目根
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


def get_data_dir() -> str:
    """Data directory (SQLite + certificate storage location).

    Priority:
    1. Environment variable TELNIX_DATA_DIR (convenient for Linux/WSL users to put data
       on an ext4 partition, avoiding cross-filesystem SQLite I/O errors on /mnt/*)
    2. Packaged: %APPDATA%/Telnix (Windows) or ~/.telnix (other platforms)
    3. Dev environment: <project_root>/data
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
    """SQLite database file path."""
    return os.path.join(get_data_dir(), "telnix.db")


def get_cert_dir() -> str:
    """Certificate directory (root certificate + dynamically issued domain certificates)."""
    cert_dir = os.path.join(get_data_dir(), "certs")
    os.makedirs(cert_dir, exist_ok=True)
    return cert_dir


def get_ui_dist_dir() -> str:
    """Frontend static files directory. Packaged at _internal/ui/dist, dev at src/ui/dist."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "_internal", "ui", "dist")
    here = os.path.dirname(os.path.abspath(__file__))
    # telnix/ -> host/ -> src/ -> src/ui/dist
    return os.path.abspath(os.path.join(here, "..", "..", "ui", "dist"))


def get_project_root() -> str:
    """Project root directory. Packaged: same directory as the exe; dev: parent of src."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return _project_root()


def get_docs_dir() -> str:
    """Docs directory (images and other resources referenced by CLASH_SET.md).
    Packaged at _internal/docs, dev at project root/docs."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "_internal", "docs")
    return os.path.join(_project_root(), "docs")


def get_tutorial_md_path() -> str:
    """Clash tutorial markdown file path. Packaged at _internal/CLASH_SET.md, dev at project root/CLASH_SET.md."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "_internal", "CLASH_SET.md")
    return os.path.join(_project_root(), "CLASH_SET.md")

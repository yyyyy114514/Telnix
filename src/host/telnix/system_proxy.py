"""Cross-platform system proxy configuration.

Platform support:
- Windows: write registry (ProxyServer/ProxyEnable/ProxyOverride) + notify system to refresh
- macOS: networksetup -setwebproxy/-setsecurewebproxy/-setproxybypassdomains
- Linux: gsettings set org.gnome.system.proxy mode/host/port/ignore-hosts (GNOME)
        + KDE: kwriteconfig5 (KDE)

Design notes:
- Each platform has its own set/clear implementation, exposing a unified interface
- When setting the proxy, localhost/127.0.0.1 is excluded so the browser connects
  directly to the local API (for real-time SSE push)
- Proxy state marker file: written when enabled, deleted when disabled (for crash recovery)
- Graceful degradation: prints a warning but does not raise when a tool is unavailable
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Optional

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_UNIX = IS_LINUX or IS_MACOS


def _get_data_dir() -> str:
    """Get the data directory (avoids circular imports)."""
    try:
        from .config import get_data_dir
        return get_data_dir()
    except Exception:  # noqa: BLE001
        return os.path.join(os.path.expanduser("~"), ".telnix")


_PROXY_ACTIVE_FLAG: Optional[str] = None


def _get_proxy_flag_path() -> str:
    """Path to the proxy state marker file (for crash recovery)."""
    global _PROXY_ACTIVE_FLAG
    if _PROXY_ACTIVE_FLAG is None:
        try:
            _PROXY_ACTIVE_FLAG = os.path.join(_get_data_dir(), ".proxy_active")
        except Exception:  # noqa: BLE001
            _PROXY_ACTIVE_FLAG = os.path.join(os.path.expanduser("~"), ".telnix_proxy_active")
    return _PROXY_ACTIVE_FLAG


def _write_proxy_flag(host: str, port: int):
    """Write the proxy state marker file."""
    try:
        flag_path = _get_proxy_flag_path()
        os.makedirs(os.path.dirname(flag_path), exist_ok=True)
        with open(flag_path, "w", encoding="utf-8") as f:
            f.write(f"{time.time()}\n{host}:{port}\n")
    except OSError:
        pass


def _delete_proxy_flag():
    """Delete the proxy state marker file."""
    try:
        os.unlink(_get_proxy_flag_path())
    except OSError:
        pass


def proxy_flag_exists() -> bool:
    """Check whether the proxy state marker file exists (for crash recovery on startup)."""
    return os.path.exists(_get_proxy_flag_path())


def read_proxy_flag() -> str:
    """Read the proxy state marker file contents (for diagnostics)."""
    try:
        with open(_get_proxy_flag_path(), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


# ---------- Windows 实现 ----------

_INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37


def _set_windows_proxy(host: str, port: int) -> bool:
    """Windows: write registry + notify system to refresh."""
    try:
        import winreg
        proxy_str = f"{host}:{port}"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, proxy_str)
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            # 排除本地地址：浏览器直连 127.0.0.1:18901（API/SSE），不走代理
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ,
                              "localhost;127.0.0.1;<local>")
        _notify_windows_settings_changed()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[Telnix] Failed to set Windows system proxy: {e}", file=sys.stderr)
        return False


def _clear_windows_proxy() -> bool:
    """Windows: clear registry proxy settings."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        _notify_windows_settings_changed()
        return True
    except OSError as e:
        print(f"[Telnix] Failed to clear Windows system proxy: {e}", file=sys.stderr)
        return False


def _notify_windows_settings_changed():
    """Notify the system that proxy settings have changed, so applications pick them up immediately."""
    try:
        import ctypes
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, 0, 0)
    except Exception:  # noqa: BLE001
        pass


def _read_windows_proxy_enabled() -> bool:
    """Read the actual proxy toggle from the Windows registry."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS) as key:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            return bool(enable)
    except Exception:  # noqa: BLE001
        return False


# ---------- macOS 实现 ----------
# networksetup 是 macOS 系统代理配置命令行工具
# 需要知道网络服务名（如 "Wi-Fi"、"Ethernet"），通过 networksetup -listallnetworkservices 获取

_MACOS_BYPASS_DOMAINS = "localhost,127.0.0.1,*.local,169.254/16"


def _get_macos_network_services() -> list[str]:
    """Get all macOS network service names (Wi-Fi/Ethernet/etc.)."""
    try:
        result = subprocess.run(
            ["networksetup", "-listallnetworkservices"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        # 输出第一行是标题 "An asterisk (*) denotes...", 跳过
        lines = result.stdout.strip().split("\n")[1:]
        # 去重并过滤空行
        services = []
        for line in lines:
            name = line.strip().lstrip("*").strip()
            if name and name not in services:
                services.append(name)
        return services
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _set_macos_proxy(host: str, port: int) -> bool:
    """macOS: use networksetup to set HTTP/HTTPS proxy."""
    services = _get_macos_network_services()
    if not services:
        print("[Telnix] macOS: no network service found, cannot set system proxy", file=sys.stderr)
        return False
    success_count = 0
    for service in services:
        # 设置 HTTP 代理
        for cmd_type, set_cmd in [
            ("web", ["-setwebproxy"]),
            ("secure", ["-setsecurewebproxy"]),
        ]:
            try:
                result = subprocess.run(
                    ["networksetup", set_cmd[0], service, host, str(port)],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    success_count += 1
                # 部分服务（如 VPN）不支持设置代理，忽略错误
            except (subprocess.TimeoutExpired, OSError):
                pass
        # 设置代理绕过域名（排除 localhost）
        try:
            subprocess.run(
                ["networksetup", "-setproxybypassdomains", service, _MACOS_BYPASS_DOMAINS],
                capture_output=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
    if success_count == 0:
        print("[Telnix] macOS: failed to set proxy on all network services", file=sys.stderr)
        return False
    return True


def _clear_macos_proxy() -> bool:
    """macOS: use networksetup to disable HTTP/HTTPS proxy."""
    services = _get_macos_network_services()
    if not services:
        return True  # 没有服务可清理，视为成功
    for service in services:
        for cmd in ["-setwebproxystate", "-setsecurewebproxystate"]:
            try:
                subprocess.run(
                    ["networksetup", cmd, service, "off"],
                    capture_output=True, timeout=5,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass
    return True


def _read_macos_proxy_enabled() -> bool:
    """Read the current macOS proxy state (checks the webproxy status of the Wi-Fi service)."""
    try:
        services = _get_macos_network_services()
        if not services:
            return False
        # 检查第一个服务（通常是 Wi-Fi 或 Ethernet）
        result = subprocess.run(
            ["networksetup", "-getwebproxy", services[0]],
            capture_output=True, text=True, timeout=5,
        )
        # 输出包含 "Enabled: Yes" 表示已启用
        return "Enabled: Yes" in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# ---------- Linux 实现 ----------
# GNOME 用 gsettings，KDE 用 kwriteconfig5
# 检测桌面环境：XDG_CURRENT_DESKTOP 环境变量

_LINUX_BYPASS_DOMAINS = "['localhost', '127.0.0.0/8', '::1', '*.local', '169.254.0.0/16']"


def _detect_linux_desktop() -> str:
    """Detect the Linux desktop environment: gnome / kde / other."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    if "GNOME" in desktop:
        return "gnome"
    if "KDE" in desktop:
        return "kde"
    # 检查 gsettings 是否可用
    try:
        subprocess.run(["gsettings", "--version"], capture_output=True, check=True, timeout=5)
        return "gnome"  # 有 gsettings 就按 GNOME 处理
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    # 检查 kwriteconfig5 是否可用
    try:
        subprocess.run(["kwriteconfig5", "--version"], capture_output=True, check=True, timeout=5)
        return "kde"
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    return "other"


def _set_linux_gnome_proxy(host: str, port: int) -> bool:
    """Linux GNOME: use gsettings to set the system proxy."""
    try:
        # 设置代理模式为 manual
        subprocess.run(
            ["gsettings", "set", "org.gnome.system.proxy", "mode", "manual"],
            capture_output=True, timeout=5, check=True,
        )
        # 设置 HTTP 代理
        subprocess.run(
            ["gsettings", "set", "org.gnome.system.proxy.http", "host", host],
            capture_output=True, timeout=5, check=True,
        )
        subprocess.run(
            ["gsettings", "set", "org.gnome.system.proxy.http", "port", str(port)],
            capture_output=True, timeout=5, check=True,
        )
        # 设置 HTTPS 代理
        subprocess.run(
            ["gsettings", "set", "org.gnome.system.proxy.https", "host", host],
            capture_output=True, timeout=5, check=True,
        )
        subprocess.run(
            ["gsettings", "set", "org.gnome.system.proxy.https", "port", str(port)],
            capture_output=True, timeout=5, check=True,
        )
        # 设置忽略主机（排除 localhost）
        subprocess.run(
            ["gsettings", "set", "org.gnome.system.proxy", "ignore-hosts",
             _LINUX_BYPASS_DOMAINS],
            capture_output=True, timeout=5,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[Telnix] Linux GNOME: failed to set proxy: {e}", file=sys.stderr)
        return False


def _clear_linux_gnome_proxy() -> bool:
    """Linux GNOME: use gsettings to disable the system proxy."""
    try:
        subprocess.run(
            ["gsettings", "set", "org.gnome.system.proxy", "mode", "none"],
            capture_output=True, timeout=5, check=True,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[Telnix] Linux GNOME: failed to clear proxy: {e}", file=sys.stderr)
        return False


def _read_linux_gnome_proxy_enabled() -> bool:
    """Read the current Linux GNOME proxy mode."""
    try:
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.system.proxy", "mode"],
            capture_output=True, text=True, timeout=5,
        )
        # 输出 'manual' 表示已启用
        return "manual" in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _set_linux_kde_proxy(host: str, port: int) -> bool:
    """Linux KDE: use kwriteconfig5 to set the system proxy."""
    try:
        # KDE 代理配置文件：~/.config/kioslaverc
        for proto in ["http", "https"]:
            subprocess.run(
                ["kwriteconfig5", "--file", "kioslaverc", "--group", "Proxy Settings",
                 f"--key", f"{proto}Proxy", f"http://{host}:{port}"],
                capture_output=True, timeout=5, check=True,
            )
        # 设置代理类型
        subprocess.run(
            ["kwriteconfig5", "--file", "kioslaverc", "--group", "Proxy Settings",
             "--key", "ProxyType", "1"],  # 1=Manual
            capture_output=True, timeout=5, check=True,
        )
        # 设置忽略主机
        subprocess.run(
            ["kwriteconfig5", "--file", "kioslaverc", "--group", "Proxy Settings",
             "--key", "NoProxyFor", "localhost,127.0.0.1,::1,*.local"],
            capture_output=True, timeout=5,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[Telnix] Linux KDE: failed to set proxy: {e}", file=sys.stderr)
        return False


def _clear_linux_kde_proxy() -> bool:
    """Linux KDE: use kwriteconfig5 to disable the system proxy."""
    try:
        subprocess.run(
            ["kwriteconfig5", "--file", "kioslaverc", "--group", "Proxy Settings",
             "--key", "ProxyType", "0"],  # 0=None
            capture_output=True, timeout=5, check=True,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[Telnix] Linux KDE: failed to clear proxy: {e}", file=sys.stderr)
        return False


def _read_linux_kde_proxy_enabled() -> bool:
    """Read the current Linux KDE proxy state."""
    try:
        result = subprocess.run(
            ["kreadconfig5", "--file", "kioslaverc", "--group", "Proxy Settings",
             "--key", "ProxyType"],
            capture_output=True, text=True, timeout=5,
        )
        # 输出 "1" 表示手动代理已启用
        return result.stdout.strip() == "1"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# ---------- 统一接口 ----------

def set_system_proxy(host: str = "127.0.0.1", port: int = 8888) -> bool:
    """Set the system proxy (cross-platform).

    Platform support:
    - Windows: write registry + notify system to refresh
    - macOS: networksetup to set HTTP/HTTPS proxy
    - Linux GNOME: gsettings to set org.gnome.system.proxy
    - Linux KDE: kwriteconfig5 to set kioslaverc

    Returns True on success, False on failure (tool unavailable, etc.).
    """
    if IS_WINDOWS:
        ok = _set_windows_proxy(host, port)
    elif IS_MACOS:
        ok = _set_macos_proxy(host, port)
    elif IS_LINUX:
        desktop = _detect_linux_desktop()
        if desktop == "gnome":
            ok = _set_linux_gnome_proxy(host, port)
        elif desktop == "kde":
            ok = _set_linux_kde_proxy(host, port)
        else:
            print(f"[Telnix] The current Linux desktop environment '{desktop}' does not support "
                  f"automatic system proxy configuration; please manually configure the "
                  f"browser/system proxy to {host}:{port}", file=sys.stderr)
            ok = False
    else:
        print(f"[Telnix] The current platform {sys.platform} does not support automatic "
              f"system proxy configuration; please manually configure the browser/system "
              f"proxy to {host}:{port}", file=sys.stderr)
        ok = False
    if ok:
        _write_proxy_flag(host, port)
    return ok


def clear_system_proxy() -> bool:
    """Clear the system proxy (cross-platform).

    Returns True on success, False on failure.
    """
    if IS_WINDOWS:
        ok = _clear_windows_proxy()
    elif IS_MACOS:
        ok = _clear_macos_proxy()
    elif IS_LINUX:
        desktop = _detect_linux_desktop()
        if desktop == "gnome":
            ok = _clear_linux_gnome_proxy()
        elif desktop == "kde":
            ok = _clear_linux_kde_proxy()
        else:
            ok = True  # 未设置过代理，无需清理
    else:
        ok = True
    _delete_proxy_flag()
    return ok


def read_actual_proxy_enabled() -> bool:
    """Read the actual system proxy toggle state (used for proxy-loss detection).

    Platform support:
    - Windows: read registry ProxyEnable
    - macOS: networksetup -getwebproxy to check Enabled
    - Linux GNOME: gsettings get org.gnome.system.proxy mode
    - Linux KDE: kreadconfig5 to read ProxyType
    """
    if IS_WINDOWS:
        return _read_windows_proxy_enabled()
    elif IS_MACOS:
        return _read_macos_proxy_enabled()
    elif IS_LINUX:
        desktop = _detect_linux_desktop()
        if desktop == "gnome":
            return _read_linux_gnome_proxy_enabled()
        elif desktop == "kde":
            return _read_linux_kde_proxy_enabled()
        return False
    return False


def get_system_proxy_info() -> dict:
    """Return system proxy backend info (for frontend display)."""
    if IS_WINDOWS:
        return {
            "platform": "windows",
            "backend": "registry",
            "supported": True,
            "hint": "就绪" if _read_windows_proxy_enabled() is not None else "不可用",
        }
    elif IS_MACOS:
        services = _get_macos_network_services()
        return {
            "platform": "macos",
            "backend": "networksetup",
            "supported": True,
            "services": services,
            "hint": f"就绪（{len(services)} 个网络服务）" if services else "未找到网络服务",
        }
    elif IS_LINUX:
        desktop = _detect_linux_desktop()
        return {
            "platform": "linux",
            "backend": desktop,
            "supported": desktop in ("gnome", "kde"),
            "hint": f"就绪（{desktop}）" if desktop in ("gnome", "kde") else
                    f"当前桌面环境 '{desktop}' 不支持自动设置系统代理",
        }
    return {
        "platform": sys.platform,
        "backend": "none",
        "supported": False,
        "hint": "不支持的平台",
    }

"""Cross-platform elevation module.

Platform support:
- Windows: ShellExecuteW('runas') triggers UAC elevation
- macOS: osascript pops up the system authorization dialog (requires the user to enter a password)
- Linux: pkexec (PolicyKit GUI popup) or sudo (CLI)

Design notes:
- After elevation, restarts the Telnix process (preserving launch arguments)
- The old process exits, the new process runs with elevated privileges
- Graceful degradation: returns an error message when the tool is unavailable
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_UNIX = IS_LINUX or IS_MACOS


def is_admin() -> bool:
    """Check whether the current process has administrator/root privileges.

    Windows: ctypes.windll.shell32.IsUserAnAdmin()
    Unix: os.geteuid() == 0 (root)
    """
    if IS_WINDOWS:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def _build_restart_argv() -> list[str]:
    """Build the argument list for the restart command (preserving launch arguments)."""
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包：直接运行 exe
        return [sys.executable]
    # Python 脚本：python -m telnix [flags...]
    argv = [sys.executable, "-m", "telnix"]
    # 透传启动参数（保留 --no-browser 等），过滤非 flag 参数
    argv.extend(a for a in sys.argv[1:] if a.startswith('-'))
    return argv


def _get_restart_cwd() -> str:
    """Get the working directory for the restarted process."""
    # telnix 包的父目录（即 src/host/），让 `python -m telnix` 能找到包
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def elevate_windows() -> tuple[bool, str]:
    """Windows: use ShellExecuteW('runas') to trigger UAC elevation.

    Returns (success, message).
    """
    try:
        import ctypes
        exe = sys.executable
        if getattr(sys, 'frozen', False):
            # PyInstaller 打包：直接运行 exe
            params = ''
        else:
            # python -m telnix [flags...]
            argv_extra = [a for a in sys.argv[1:] if a.startswith('-')]
            # Windows 下 ShellExecuteW 的 lpParameters 按命令行规则解析
            # （空格分隔，双引号包裹含空格/特殊字符的参数，内部双引号用 \" 转义）。
            # 使用 subprocess.list2cmdline() 按 Windows 规则转义每个参数，
            # 与 macOS/Linux 版本的 shlex.quote() 等效，防止参数注入
            params = subprocess.list2cmdline(['-m', 'telnix'] + argv_extra)
        cwd = _get_restart_cwd()
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, 'runas', exe, params, cwd, 1  # SW_SHOWNORMAL
        )
        if ret <= 32:
            return False, f'UAC elevation failed, return code {ret} (user may have canceled UAC)'
        return True, 'UAC elevation approved'
    except Exception as e:  # noqa: BLE001
        return False, f'UAC elevation failed: {e}'


def elevate_macos() -> tuple[bool, str]:
    """macOS: use osascript to pop up the system authorization dialog.

    Executes `do shell script ... with administrator privileges` via AppleScript;
    the system pops up a password input box, and after the user enters the password,
    the command runs as root.

    Returns (success, message).

    Security: uses shlex.quote() to shell-escape each argument, avoiding command injection.
    Backslashes and double quotes in cwd are also escaped for AppleScript strings.
    """
    import shlex
    argv = _build_restart_argv()
    cwd = _get_restart_cwd()
    # 用 osascript 执行 sudo 命令（会弹出系统密码框）
    # 注意：osascript 的 administrator privileges 只能执行单条 shell 命令
    # 安全转义：每个参数用 shlex.quote() 包裹，防止 argv 中混入 shell 元字符
    cmd_str = ' '.join(shlex.quote(a) for a in argv)
    # AppleScript 字符串中反斜杠和双引号需要转义
    cwd_escaped = cwd.replace('\\', '\\\\').replace('"', '\\"')
    applescript = (
        f'do shell script "cd \\"{cwd_escaped}\\" && {cmd_str}" '
        f'with administrator privileges'
    )
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            err_msg = result.stderr.strip()
            if 'User canceled' in err_msg or 'user canceled' in err_msg.lower():
                return False, 'User canceled authorization'
            return False, f'osascript authorization failed: {err_msg}'
        return True, 'macOS authorization approved'
    except subprocess.TimeoutExpired:
        return False, 'osascript authorization timed out (no response within 60 seconds)'
    except FileNotFoundError:
        return False, 'osascript unavailable (macOS only, built-in)'
    except Exception as e:  # noqa: BLE001
        return False, f'macOS authorization failed: {e}'


def elevate_linux_pkexec() -> tuple[bool, str]:
    """Linux: use pkexec (PolicyKit) to pop up a GUI authorization dialog.

    pkexec is the command-line tool for PolicyKit; desktop environments usually pop up a password box.
    Returns (success, message).

    Security: uses shlex.quote() to shell-escape each argument, avoiding command injection.
    """
    import shlex
    argv = _build_restart_argv()
    cwd = _get_restart_cwd()
    # pkexec 直接执行命令（会弹出 GUI 授权框）
    try:
        # pkexec 不支持设置 cwd，用 sh -c 包装
        # 安全转义：每个参数用 shlex.quote() 包裹
        cmd_str = ' '.join(shlex.quote(a) for a in argv)
        result = subprocess.run(
            ['pkexec', 'sh', '-c', f'cd {shlex.quote(cwd)} && {cmd_str}'],
            capture_output=True, timeout=60,
        )
        if result.returncode != 0:
            return False, f'pkexec authorization failed (return code {result.returncode})'
        return True, 'pkexec authorization approved'
    except subprocess.TimeoutExpired:
        return False, 'pkexec authorization timed out (no response within 60 seconds)'
    except FileNotFoundError:
        return False, 'pkexec unavailable (please install policykit-1)'
    except Exception as e:  # noqa: BLE001
        return False, f'pkexec authorization failed: {e}'


def elevate_linux_sudo() -> tuple[bool, str]:
    """Linux: use the sudo command to elevate (CLI mode, requires the user to enter a password in the terminal).

    Returns (success, message).

    Security: uses shlex.quote() to shell-escape each argument, avoiding command injection.
    """
    import shlex
    argv = _build_restart_argv()
    cwd = _get_restart_cwd()
    # sudo 会继承当前终端的 stdin，用户可以输入密码
    try:
        # 用 sudo -k 强制要求密码（避免缓存），-- 表示后续是非选项参数
        # 安全转义：每个参数用 shlex.quote() 包裹
        cmd_str = ' '.join(shlex.quote(a) for a in argv)
        result = subprocess.run(
            ['sudo', '-k', 'sh', '-c', f'cd {shlex.quote(cwd)} && {cmd_str}'],
            stdin=sys.stdin,  # 继承终端 stdin 让用户输入密码
        )
        if result.returncode != 0:
            return False, f'sudo elevation failed (return code {result.returncode})'
        return True, 'Elevated via sudo'
    except FileNotFoundError:
        return False, 'sudo unavailable'
    except Exception as e:  # noqa: BLE001
        return False, f'sudo elevation failed: {e}'


def elevate() -> tuple[bool, str]:
    """Cross-platform elevation: restart the Telnix process with administrator/root privileges.

    Platform support:
    - Windows: ShellExecuteW('runas') triggers UAC
    - macOS: osascript with administrator privileges
    - Linux: pkexec (GUI) or sudo (CLI)

    Returns (success, message).
    On success, the caller should exit the current process (the new process is already running independently).
    """
    if IS_WINDOWS:
        return elevate_windows()
    elif IS_MACOS:
        return elevate_macos()
    elif IS_LINUX:
        # 优先用 pkexec（GUI 弹窗更友好），失败时回退到 sudo
        ok, msg = elevate_linux_pkexec()
        if not ok and 'pkexec unavailable' in msg:
            # pkexec 不存在，回退到 sudo
            return elevate_linux_sudo()
        return ok, msg
    else:
        return False, f'Elevation not supported on platform {sys.platform}'


def get_elevation_info() -> dict:
    """Return elevation backend info (for frontend display)."""
    admin = is_admin()
    if IS_WINDOWS:
        return {
            "platform": "windows",
            "backend": "uac",
            "is_admin": admin,
            "supported": True,
            "hint": "Ready" if not admin else "Already an administrator",
        }
    elif IS_MACOS:
        return {
            "platform": "macos",
            "backend": "osascript",
            "is_admin": admin,
            "supported": True,
            "hint": "Ready" if not admin else "Already root",
        }
    elif IS_LINUX:
        # 检测 pkexec 是否可用
        pkexec_available = os.path.exists('/usr/bin/pkexec') or os.path.exists('/bin/pkexec')
        sudo_available = os.path.exists('/usr/bin/sudo') or os.path.exists('/bin/sudo')
        backend = "pkexec" if pkexec_available else ("sudo" if sudo_available else "none")
        return {
            "platform": "linux",
            "backend": backend,
            "is_admin": admin,
            "supported": pkexec_available or sudo_available,
            "hint": (
                "Ready" if not admin and (pkexec_available or sudo_available)
                else "Already root" if admin
                else "pkexec/sudo not found, please start manually with sudo"
            ),
        }
    return {
        "platform": sys.platform,
        "backend": "none",
        "is_admin": admin,
        "supported": False,
        "hint": "Unsupported platform",
    }

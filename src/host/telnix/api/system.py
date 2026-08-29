"""System control API: restart service / quit / disable system proxy / cleanup db.

Design points:
- Restart: Restart within the same process via os.execv, inheriting the same console window. Clear system proxy before restart.
- Quit: Call _quit_hook (injected by __main__.py), clear proxy + sys.exit.
- Disable system proxy: Call injected _clear_proxy_hook.
- Restart as admin (restart-as-admin): First confirm with GUI user (topmost popup), then ShellExecuteW runas if agreed.
  CLI initiates -> backend creates pending request and blocks waiting -> frontend polls popup -> user responds -> unblock.
- WinDivert risk warning: Popup to inform user that this driver may be blocked by antivirus when enabling WinDivert-related features (TCP/UDP capture / transparent proxy / DNS hijack) for the first time. GUI uses Vue dialog, CLI/MCP uses native MessageBox.
"""

import os
import subprocess
import sys
import threading
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import settings_store
from ..logger import _capture_log
from .. import db
from . import ok, err

router = APIRouter()

# 由 __main__.py 注入的钩子（可选）
_restart_hook = None
_quit_hook = None
_clear_proxy_hook = None
_enable_proxy_hook = None
# 系统代理当前是否开启（由 __main__ 更新）
system_proxy_on = False

# 平台能力接口中 is_admin 等稳定结果的内存缓存（避免设置页等频繁检测导致 2 秒跳动）
_admin_cache: dict | None = None
_ADMIN_CACHE_TTL = 30.0

# ------ 代理丢失检测 ------
# _proxy_lost：期望开启（system_proxy_on=True）但实际注册表已关闭/被抢占时置 True
# 前端轮询 /status 时检测该字段，弹窗询问是否重新开启
# 用户主动关闭代理（/system/clear-proxy）时同步重置为 False，避免误报
_proxy_lost = False
_proxy_monitor_thread: threading.Thread | None = None
_proxy_monitor_started = False
_proxy_monitor_lock = threading.Lock()


def set_hooks(restart=None, quit=None, clear_proxy=None, enable_proxy=None):
    """Inject callbacks for restart/quit/clear proxy/enable proxy."""
    global _restart_hook, _quit_hook, _clear_proxy_hook, _enable_proxy_hook
    if restart is not None:
        _restart_hook = restart
    if quit is not None:
        _quit_hook = quit
    if clear_proxy is not None:
        _clear_proxy_hook = clear_proxy
    if enable_proxy is not None:
        _enable_proxy_hook = enable_proxy


def mark_proxy_on(v: bool):
    """Update system proxy on/off state.

    Called when user actively enables/disables proxy.
    When actively disabled, also reset _proxy_lost=False to avoid frontend continuing to popup.
    """
    global system_proxy_on, _proxy_lost
    system_proxy_on = v
    if not v:
        _proxy_lost = False


def is_proxy_lost() -> bool:
    """Query whether proxy is lost (used when frontend polls /status)."""
    return _proxy_lost


def start_proxy_monitor():
    """Start proxy status monitor thread (started only once, called by __main__.py).

    Reads actual proxy status from registry every 5 seconds and compares with system_proxy_on:
    - Expected on but actually off -> _proxy_lost=True
    - Not monitored when expected off (to avoid false positives)
    """
    global _proxy_monitor_thread, _proxy_monitor_started
    with _proxy_monitor_lock:
        if _proxy_monitor_started:
            return
        _proxy_monitor_started = True
    _proxy_monitor_thread = threading.Thread(
        target=_proxy_monitor_loop,
        daemon=True,
        name="proxy-monitor",
    )
    _proxy_monitor_thread.start()


def _read_actual_proxy_enabled() -> bool:
    """Read actual system proxy on/off state (cross-platform).

    Platform support:
    - Windows: Read registry ProxyEnable
    - macOS: networksetup -getwebproxy check Enabled
    - Linux GNOME: gsettings get org.gnome.system.proxy mode
    - Linux KDE: kreadconfig5 read ProxyType
    """
    try:
        from .. import system_proxy
        return system_proxy.read_actual_proxy_enabled()
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in system.py", extra={"exc": repr(e)})
        return False


def _proxy_monitor_loop():
    """Proxy status monitor loop (background thread, cross-platform).

    Reads actual system proxy status every 5 seconds and compares with system_proxy_on:
    - Expected on but actually off -> _proxy_lost=True
    - Not monitored when expected off (to avoid false positives)

    Platform support:
    - Windows: Read registry ProxyEnable
    - macOS: networksetup -getwebproxy
    - Linux: gsettings/kreadconfig5
    - Unsupported platforms: exit directly (cannot monitor)
    """
    global _proxy_lost
    # 启动时先等 10 秒，让前端初始化完成，避免初次轮询误触发
    time.sleep(10)
    last_seen_actual = None  # 上一次实际状态（None 表示未初始化）
    while True:
        time.sleep(5)
        try:
            # 只在用户期望开启时监控
            if not system_proxy_on:
                last_seen_actual = None
                if _proxy_lost:
                    global_set_proxy_lost(False)
                continue
            actual = _read_actual_proxy_enabled()
            if not actual and (last_seen_actual is None or last_seen_actual):
                # 期望开但实际关：代理丢失
                if not _proxy_lost:
                    global_set_proxy_lost(True)
                    print("[Telnix] System proxy lost detected", file=sys.stderr)
            elif actual and not (last_seen_actual is None or last_seen_actual):
                # 代理恢复了（前端弹窗后用户同意重新开启）
                if _proxy_lost:
                    global_set_proxy_lost(False)
                    print("[Telnix] System proxy restored", file=sys.stderr)
            last_seen_actual = actual
        except Exception as e:  # noqa: BLE001
            print(f"[Telnix] Proxy monitor exception: {e}", file=sys.stderr)


def global_set_proxy_lost(v: bool):
    """Thread-safely update _proxy_lost."""
    global _proxy_lost
    _proxy_lost = v


# ---------- 管理员重启：用户确认流程 ----------
# 待确认请求池：request_id → {event, response, created_at, source}
# - event: threading.Event，用户响应时 set
# - response: 'accept' | 'reject' | None（未响应）
# - source: 'cli' | 'api'
_pending_admin_requests: dict[str, dict] = {}
_pending_lock = threading.Lock()


def _create_pending_request(source: str = "cli") -> str:
    """Create a pending admin restart request, return request_id.

    After creation, immediately popup native Windows MessageBox (topmost) in a new thread,
    and pass back the result after user responds.
    """
    rid = uuid.uuid4().hex[:12]
    with _pending_lock:
        _pending_admin_requests[rid] = {
            "event": threading.Event(),
            "response": None,
            "created_at": time.time(),
            "source": source,
        }
    # 启动线程弹原生 Windows 置顶 Yes/No 弹窗（不阻塞 API 线程）
    threading.Thread(
        target=_native_message_box_thread,
        args=(rid, source),
        daemon=True,
    ).start()
    return rid


def _native_message_box_thread(rid: str, source: str):
    """Popup native Windows Yes/No dialog in a new thread, pass result back to pending request."""
    title = "Telnix Admin Restart Request"
    message = (
        f"Source: {source}\n\n"
        "CLI / Agent requests to restart Telnix as administrator\n"
        "to assist agent packet capture (TCP/UDP capture requires administrator privileges).\n\n"
        "If agreed, a UAC elevation prompt will appear, and Telnix will restart as administrator.\n\n"
        "Do you agree?"
    )
    result = _show_native_message_box(title, message)
    response = 'accept' if result == 'yes' else 'reject'
    _respond_pending_request(rid, response)


def _show_confirm_box_unix(title: str, message: str) -> str:
    """在 macOS/Linux 弹出原生 Yes/No 确认框，返回 'yes' / 'no'。

    安全策略：无法弹出任何确认框时返回 'no'（保守拒绝），
    确保管理员重启这类高危操作必须经过真实的用户交互确认。
    """
    import shutil
    try:
        if sys.platform == 'darwin':
            # macOS: osascript 显示确认对话框，默认聚焦「取消」
            script = (
                f'display dialog {_applescript_quote(message)} '
                f'with title {_applescript_quote(title)} '
                f'buttons {{"Cancel", "OK"}} default button "Cancel" '
                f'with icon caution'
            )
            r = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True, text=True, timeout=120,
            )
            # 点 OK 返回码 0；取消返回非 0（User canceled）
            return 'yes' if r.returncode == 0 else 'no'
        # Linux: 优先 zenity，其次 kdialog
        if shutil.which('zenity'):
            r = subprocess.run(
                ['zenity', '--question', '--title', title, '--text', message,
                 '--ok-label', 'OK', '--cancel-label', 'Cancel', '--default-cancel'],
                capture_output=True, timeout=120,
            )
            return 'yes' if r.returncode == 0 else 'no'
        if shutil.which('kdialog'):
            r = subprocess.run(
                ['kdialog', '--title', title, '--warningyesno', message],
                capture_output=True, timeout=120,
            )
            return 'yes' if r.returncode == 0 else 'no'
    except subprocess.TimeoutExpired:
        return 'no'
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in system.py", extra={"exc": repr(e)})
        return 'no'
    # 无任何可用的确认工具：保守拒绝
    return 'no'


def _applescript_quote(s: str) -> str:
    """把字符串安全嵌入 AppleScript 字面量（转义反斜杠与双引号）。"""
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _show_native_message_box(title: str, message: str) -> str:
    """Show native Windows topmost Yes/No dialog, return 'yes' / 'no'.

    Uses MB_TOPMOST | MB_SYSTEMMODAL | MB_SETFOREGROUND to ensure the window is displayed on top,
    drawing user attention (taskbar icon flashing + force foreground).
    """
    if sys.platform != 'win32':
        # 非 Windows 平台：弹出原生确认框（macOS osascript / Linux zenity|kdialog）。
        # 安全：绝不自动同意——无法弹出确认框时保守拒绝，
        # 避免局域网/CSRF 请求在无用户交互下静默触发管理员重启。
        return _show_confirm_box_unix(title, message)
    MB_YESNO = 0x00000004
    MB_ICONQUESTION = 0x00000020
    MB_TOPMOST = 0x00040000
    MB_SETFOREGROUND = 0x00010000
    MB_SYSTEMMODAL = 0x00001000
    MB_DEFBUTTON2 = 0x00000100  # 默认聚焦「否」按钮，避免误按回车同意
    IDYES = 6
    IDNO = 7
    flags = (
        MB_YESNO
        | MB_ICONQUESTION
        | MB_TOPMOST
        | MB_SETFOREGROUND
        | MB_SYSTEMMODAL
        | MB_DEFBUTTON2
    )
    try:
        import ctypes
        # MessageBoxW 是 Unicode 版本，支持中文
        result = ctypes.windll.user32.MessageBoxW(
            None,                  # hWnd: 无父窗口（桌面）
            message,
            title,
            flags,
        )
        return 'yes' if result == IDYES else 'no'
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in system.py", extra={"exc": repr(e)})
        # 任何异常都视为拒绝（保守安全策略）
        return 'no'


def _wait_pending_request(rid: str, timeout: float = 60.0) -> str | None:
    """Block waiting for user response, return 'accept' / 'reject' / None (timeout)."""
    with _pending_lock:
        item = _pending_admin_requests.get(rid)
    if not item:
        return None
    # 阻塞等待，最多 timeout 秒
    triggered = item["event"].wait(timeout=timeout)
    if not triggered:
        return None
    return item["response"]


def _respond_pending_request(rid: str, response: str) -> bool:
    """User responds to pending request, return whether successful."""
    with _pending_lock:
        item = _pending_admin_requests.get(rid)
        if not item or item["response"] is not None:
            return False
        item["response"] = response
        item["event"].set()
    return True


def _cleanup_pending_request(rid: str):
    """Clean up processed pending request."""
    with _pending_lock:
        _pending_admin_requests.pop(rid, None)


def _list_pending_requests() -> list[dict]:
    """List all pending requests (for frontend polling)."""
    now = time.time()
    out = []
    with _pending_lock:
        items = list(_pending_admin_requests.items())
    for rid, item in items:
        # 超过 120s 未响应的视为过期，跳过（CLI 端已超时退出）
        if now - item["created_at"] > 120:
            continue
        if item["response"] is not None:
            continue
        out.append({
            "request_id": rid,
            "source": item["source"],
            "created_at": item["created_at"],
            "action": "restart-as-admin",
            "message": "CLI requests to restart Telnix as administrator to assist agent packet capture (TCP/UDP capture requires administrator privileges)",
        })
    return out


def _do_shell_elevate() -> tuple[bool, str]:
    """Perform elevation, start a new administrator/root process. Return (success, message).

    Platform support:
    - Windows: ShellExecuteW('runas') triggers UAC
    - macOS: osascript with administrator privileges
    - Linux: pkexec (GUI) or sudo (CLI)
    """
    # 先清理系统代理（避免提权后旧进程残留代理设置）
    if _clear_proxy_hook is not None:
        try:
            _clear_proxy_hook()
        except Exception:  # noqa: BLE001
            pass
    try:
        from .. import elevation
        ok, msg = elevation.elevate()
    except Exception as e:  # noqa: BLE001
        return False, f'Elevation failed: {e}'
    if not ok:
        return False, msg
    # 退出当前非管理员进程
    threading.Thread(target=_delayed_quit, daemon=True).start()
    return True, 'Approved'


# ---------- WinDivert 风险提示 ----------
# WinDivert 是 Windows 内核驱动，Telnix 用它做 TCP/UDP 抓包 / 透明代理 / DNS 劫持。
# 部分杀毒软件会把 WinDivert64.sys 当成"漏洞驱动"拦截（漏洞利用工具也用它）。
# 因此首次启用相关功能前必须让用户知情同意，ack 后存到 settings.json 永久不再提示。

WINDIVERT_ACK_KEY = "windivert_warning_acknowledged"

_WINDIVERT_WARNING_MESSAGE = (
    "The feature about to be enabled requires loading the WinDivert64.sys kernel driver.\n\n"
    "This driver is commonly used by exploit tools, and some antivirus software (360 / Huorong / Windows Defender, etc.) "
    "may block or alert on it as a \"vulnerable driver\", causing the feature to fail to start.\n\n"
    "Telnix only uses this driver for packet capture / transparent proxy / DNS hijack, "
    "and will not bring any security risks to your device.\n\n"
    "Do you confirm to enable it?"
)

_WINDIVERT_WARNING_BRIEF = (
    "About to load WinDivert64.sys kernel driver, which is commonly used by exploit tools. "
    "Some antivirus software may block or alert on it as a \"vulnerable driver\". "
    "Telnix only uses it for packet capture / transparent proxy, and will not bring any security risks to your device."
)


def is_windivert_acknowledged() -> bool:
    """Whether user has acknowledged WinDivert risk warning."""
    return settings_store.get_setting(WINDIVERT_ACK_KEY, "0") == "1"


def windivert_warning_needed() -> bool:
    """Whether to prompt: True only on Windows platform + not acknowledged."""
    return sys.platform == "win32" and not is_windivert_acknowledged()


def check_windivert_ack_or_block():
    """Called at WinDivert-related API entry points.

    Returns None if acknowledged (or non-Windows platform, where underlying functions return unsupported), can continue execution.
    Returns JSONResponse (HTTP 403 + need_ack=true) if acknowledgment is required, caller should directly return this response.
    """
    if sys.platform != "win32":
        # 非 Windows 平台：不检查 ack，由底层函数返回"不支持"错误
        return None
    if is_windivert_acknowledged():
        return None
    # 未确认：返回 403 + need_ack=true，前端/CLI 据此弹窗
    return JSONResponse(
        status_code=403,
        content={
            "code": -1,
            "msg": "WinDivert risk warning must be acknowledged first",
            "data": {"need_ack": True},
            "need_ack": True,
        },
    )


def acknowledge_windivert_warning() -> None:
    """Mark as acknowledged (will not prompt again permanently)."""
    settings_store.set_setting(WINDIVERT_ACK_KEY, "1")


# ---- CLI/MCP 原生弹窗确认流程（类似 _pending_admin_requests） ----
# 待确认 ack 请求池：request_id → {event, response, created_at}
# - event: threading.Event，用户响应时 set
# - response: 'accept' | 'reject' | None（未响应）
_pending_windivert_acks: dict[str, dict] = {}
_pending_windivert_lock = threading.Lock()


def _create_pending_windivert_ack() -> str:
    """Create a pending WinDivert ack request, return request_id.

    After creation, immediately popup native Windows MessageBox (topmost) in a new thread,
    and pass back the result after user responds.
    """
    rid = uuid.uuid4().hex[:12]
    with _pending_windivert_lock:
        _pending_windivert_acks[rid] = {
            "event": threading.Event(),
            "response": None,
            "created_at": time.time(),
        }
    threading.Thread(
        target=_native_windivert_message_box_thread,
        args=(rid,),
        daemon=True,
        name=f"windivert-ack-{rid}",
    ).start()
    return rid


def _native_windivert_message_box_thread(rid: str):
    """Popup native Windows Yes/No dialog in a new thread, pass result back to pending ack request."""
    title = "Telnix WinDivert Driver Risk Warning"
    message = _WINDIVERT_WARNING_MESSAGE
    result = _show_native_message_box(title, message)
    response = 'accept' if result == 'yes' else 'reject'
    _respond_pending_windivert_ack(rid, response)
    # 若用户同意，立即标记 ack=1（settings.json 持久化，永久不再提示）
    if response == 'accept':
        try:
            acknowledge_windivert_warning()
        except Exception as e:  # noqa: BLE001
            _capture_log("error", "API exception", extra={"exc": repr(e)})
            pass


def _wait_pending_windivert_ack(rid: str, timeout: float = 60.0) -> str | None:
    """Block waiting for user response, return 'accept' / 'reject' / None (timeout)."""
    with _pending_windivert_lock:
        item = _pending_windivert_acks.get(rid)
    if not item:
        return None
    triggered = item["event"].wait(timeout=timeout)
    if not triggered:
        return None
    return item["response"]


def _respond_pending_windivert_ack(rid: str, response: str) -> bool:
    """User responds to pending ack request, return whether successful."""
    with _pending_windivert_lock:
        item = _pending_windivert_acks.get(rid)
        if not item or item["response"] is not None:
            return False
        item["response"] = response
        item["event"].set()
    return True


def _cleanup_pending_windivert_ack(rid: str):
    """Clean up processed pending ack request."""
    with _pending_windivert_lock:
        _pending_windivert_acks.pop(rid, None)


@router.post("/system/restart")
async def restart_service(request: Request):
    """Restart frontend and backend services (in-process os.execv restart)."""
    if _restart_hook is None:
        return err("Restart hook not injected")
    # 异步执行，避免请求未返回就退出
    threading.Thread(target=_delayed_restart, daemon=True).start()
    return ok({"restarting": True}, "Restarting service, please wait...")


@router.post("/system/quit")
async def quit_service(request: Request):
    """Quit Telnix (close frontend and backend + clear system proxy)."""
    threading.Thread(target=_delayed_quit, daemon=True).start()
    return ok({"quitting": True}, "Quitting Telnix...")


@router.post("/system/clear-proxy")
async def clear_proxy(request: Request):
    """Only disable system proxy (keep service running)."""
    if _clear_proxy_hook is not None:
        try:
            _clear_proxy_hook()
        except Exception as e:  # noqa: BLE001
            return err(f"Clear proxy failed: {e}")
    mark_proxy_on(False)
    return ok({"cleared": True, "system_proxy_on": False}, "System proxy disabled")


@router.post("/system/enable-proxy")
async def enable_proxy(request: Request):
    """Re-enable system proxy (pointing to Telnix proxy port)."""
    if _enable_proxy_hook is None:
        return err("Enable proxy hook not injected")
    try:
        _enable_proxy_hook()
    except Exception as e:  # noqa: BLE001
        return err(f"Enable proxy failed: {e}")
    mark_proxy_on(True)
    return ok({"enabled": True, "system_proxy_on": True}, "System proxy enabled")


@router.post("/system/restart-as-admin")
async def restart_as_admin(request: Request):
    """Restart Telnix as administrator (UAC elevation).

    Legacy interface: directly pops UAC without GUI confirmation.
    For new CLI flows, use /system/request-admin-restart + /system/admin-request/{id}/wait.
    """
    success, msg = _do_shell_elevate()
    if not success:
        return err(msg)
    return ok({'restarting': True}, 'Restarting Telnix as administrator...')


@router.post("/system/request-admin-restart")
async def request_admin_restart(request: Request):
    """Create a pending admin restart request awaiting GUI confirmation.

    Returns request_id, CLI should long-poll via /system/admin-request/{id}/wait for response.
    """
    rid = _create_pending_request(source="cli")
    return ok({
        "request_id": rid,
        "message": "Pending request created, waiting for GUI user response",
    })


@router.get("/system/admin-request/{rid}/wait")
async def admin_request_wait(rid: str, request: Request):
    """Long-poll waiting for user response, returns pending on 60s timeout."""
    import asyncio
    # 检查 rid 是否存在
    with _pending_lock:
        item = _pending_admin_requests.get(rid)
    if not item:
        return err("Request not found or already processed")
    # 阻塞等待放到线程池，避免阻塞事件循环
    response = await asyncio.to_thread(_wait_pending_request, rid, 60.0)
    if response is None:
        return ok({"status": "pending", "request_id": rid}, "Still waiting for user response")
    # 已响应
    if response == "accept":
        # 执行 UAC 提权
        success, msg = await asyncio.to_thread(_do_shell_elevate)
        _cleanup_pending_request(rid)
        if not success:
            return err(msg)
        return ok({"status": "accepted", "request_id": rid}, "User accepted, restarting as administrator")
    else:
        _cleanup_pending_request(rid)
        return ok({"status": "rejected", "request_id": rid}, "User rejected the admin restart request")


@router.get("/system/pending-admin-actions")
async def pending_admin_actions(request: Request):
    """Frontend polling: get list of pending admin actions."""
    return ok({"items": _list_pending_requests()})


@router.post("/system/admin-request/{rid}/respond")
async def admin_request_respond(rid: str, request: Request):
    """Frontend user responds to pending request (accept/reject)."""
    try:
        body = await request.json()
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in system.py", extra={"exc": repr(e)})
        body = {}
    response = (body.get("response") or "").lower()
    if response not in ("accept", "reject"):
        return err("response must be accept or reject")
    success = _respond_pending_request(rid, response)
    if not success:
        return err("Request not found or already responded")
    return ok({"request_id": rid, "response": response}, "Response submitted")


# ---------- WinDivert 风险提示 API ----------
# GET  /api/system/windivert-warning       - 查询是否需要提示 + 当前 ack 状态 + 风险说明文本
# POST /api/system/windivert-warning/ack    - 标记为已确认（前端"了解，不再显示此提示"按钮调用）
# POST /api/system/request-windivert-ack   - 创建 pending ack 请求 + 桌面置顶原生弹窗（CLI/MCP 用）
# GET  /api/system/windivert-ack-request/{rid}/wait - 长轮询等待用户响应（CLI/MCP 用）


@router.get("/system/windivert-warning")
async def get_windivert_warning(request: Request):
    """Query WinDivert risk warning status.

    Returns:
      needed: whether to prompt (true only on Windows platform + not acknowledged)
      message: risk explanation text (for frontend popup body)
      ack: whether currently acknowledged
    """
    needed = windivert_warning_needed()
    return ok({
        "needed": needed,
        "message": _WINDIVERT_WARNING_MESSAGE,
        "brief": _WINDIVERT_WARNING_BRIEF,
        "ack": is_windivert_acknowledged(),
        "platform": sys.platform,
    })


@router.post("/system/windivert-warning/ack")
async def ack_windivert_warning(request: Request):
    """Mark WinDivert risk warning as acknowledged (will not prompt again permanently).

    Called by the frontend "Got it, do not show this prompt again" button, or by CLI/MCP after the user selects "Yes" in the native popup.
    """
    acknowledge_windivert_warning()
    return ok({"ack": True}, "WinDivert risk warning confirmed, will not prompt again")


@router.post("/system/request-windivert-ack")
async def request_windivert_ack(request: Request):
    """Create a pending WinDivert ack request awaiting GUI user confirmation (for CLI/MCP).

    After creation, immediately popup native Windows MessageBox (topmost) in a new thread.
    CLI should long-poll via /system/windivert-ack-request/{id}/wait for response.
    Returns request_id.
    """
    # 非 Windows 平台：无需 ack（底层功能直接返回不支持）
    if sys.platform != "win32":
        return ok({"request_id": None, "skipped": True},
                  "Non-Windows platform does not require WinDivert risk warning")
    # 已确认：直接跳过
    if is_windivert_acknowledged():
        return ok({"request_id": None, "skipped": True, "ack": True},
                  "WinDivert risk warning already confirmed")
    rid = _create_pending_windivert_ack()
    return ok({
        "request_id": rid,
        "message": "Pending request created, waiting for GUI user response",
    })


@router.get("/system/windivert-ack-request/{rid}/wait")
async def windivert_ack_request_wait(rid: str, request: Request):
    """Long-poll waiting for user response, returns pending on 60s timeout."""
    import asyncio
    with _pending_windivert_lock:
        item = _pending_windivert_acks.get(rid)
    if not item:
        return err("Request not found or already processed")
    response = await asyncio.to_thread(_wait_pending_windivert_ack, rid, 60.0)
    if response is None:
        return ok({"status": "pending", "request_id": rid}, "Still waiting for user response")
    _cleanup_pending_windivert_ack(rid)
    if response == "accept":
        return ok({"status": "accepted", "request_id": rid, "ack": True},
                  "User confirmed WinDivert risk warning")
    else:
        return ok({"status": "rejected", "request_id": rid, "ack": False},
                   "User rejected WinDivert risk warning")


def _delayed_restart():
    """Delay 500ms before restart, letting HTTP response return first."""
    import time
    time.sleep(0.5)
    try:
        _restart_hook()
    except Exception as e:  # noqa: BLE001
        print(f"[Telnix] Restart failed: {e}", file=sys.stderr)


def _delayed_quit():
    """Delay 500ms before quit."""
    import time
    time.sleep(0.5)
    try:
        _quit_hook()
    except Exception as e:  # noqa: BLE001
        print(f"[Telnix] Quit failed: {e}", file=sys.stderr)


@router.post("/system/firewall-allow")
async def firewall_allow(request: Request):
    """Add Windows firewall inbound rules to allow ports 8888 (proxy) and 18901 (API).

    Windows only: uses netsh to configure Windows Firewall. Non-Windows returns error.
    Requires administrator privileges.
    """
    if not sys.platform.startswith("win"):
        return err("firewall-allow is Windows only (netsh firewall). On macOS/Linux, use ufw/firewall-cmd/System Settings to allow ports manually")
    import subprocess
    rules = [
        ("Telnix-Proxy-8888", 8888, "TCP"),
        ("Telnix-API-18901", 18901, "TCP"),
    ]
    results = []
    failed = False
    for name, port, proto in rules:
        cmd = [
            "netsh", "advfirewall", "firewall", "add", "rule",
            f"name={name}",
            f"dir=in", f"action=allow", f"protocol={proto}",
            f"localport={port}",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                results.append({"rule": name, "port": port, "ok": True})
            else:
                results.append({"rule": name, "port": port, "ok": False,
                                "error": (r.stderr or r.stdout or "").strip()})
                failed = True
        except Exception as e:  # noqa: BLE001
            results.append({"rule": name, "port": port, "ok": False, "error": str(e)})
            failed = True
    return ok({
        "firewall_allow": not failed,
        "rules": results,
        "hint": "Firewall rules added" if not failed else
                "Some rules failed to add. Run Telnix as administrator and retry",
    })


@router.get("/system/firewall-status")
async def firewall_status(request: Request):
    """Query whether Telnix-related firewall rules exist.

    Windows only: netsh firewall query. Non-Windows returns error.
    """
    if not sys.platform.startswith("win"):
        return err("firewall-status is Windows only (netsh firewall). On macOS/Linux, use ufw status/firewall-cmd --list-ports to view firewall status")
    import subprocess
    cmd = ["netsh", "advfirewall", "firewall", "show", "rule",
           "name=Telnix-Proxy-8888"]
    try:
        r1 = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in system.py", extra={"exc": repr(e)})
        r1 = None
    cmd = ["netsh", "advfirewall", "firewall", "show", "rule",
           "name=Telnix-API-18901"]
    try:
        r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in system.py", extra={"exc": repr(e)})
        r2 = None
    proxy_ok = bool(r1 and r1.returncode == 0 and "Telnix-Proxy-8888" in (r1.stdout or ""))
    api_ok = bool(r2 and r2.returncode == 0 and "Telnix-API-18901" in (r2.stdout or ""))
    return ok({
        "proxy_8888_allowed": proxy_ok,
        "api_18901_allowed": api_ok,
        "hint": "If false, the rule is not added and the phone cannot connect. Call firewall-allow with administrator privileges",
    })


@router.get("/system/db-stats")
async def system_db_stats(request: Request):
    """Return current database file size and row counts of major tables."""
    try:
        return ok(db.get_db_stats())
    except Exception as e:  # noqa: BLE001
        return err(str(e))


@router.get("/system/ports")
async def get_ports(request: Request):
    """Return the actual API/proxy ports used by the running backend.

    Used by CLI/agent to discover Telnix's listening ports when random-port mode
    or auto port-fallback is active (the actual port may differ from settings.json
    or the default 18901/8888).

    Returns:
        api_port: actual API port (uvicorn listen port)
        proxy_port: actual proxy port (proxy server listen port)
        api_host: API listen host (127.0.0.1 or 0.0.0.0)
        proxy_host: proxy listen host (127.0.0.1 or 0.0.0.0)
        port_conflict: port conflict info (None if no conflict)
    """
    state = request.app.state.telnix
    from ..config import get_host, get_proxy_host
    return ok({
        "api_port": state.api_port,
        "proxy_port": state.proxy_port,
        "api_host": get_host(),
        "proxy_host": get_proxy_host(),
        "port_conflict": state.port_conflict,
    })


@router.post("/system/cleanup-db")
async def system_cleanup_db(request: Request):
    """Clean up and shrink the database file.

    Drops and recreates transient tables (flows/sessions) and runs VACUUM to reclaim
    disk space. Preserves settings, auto-reply rules, ignored lists, and AI chats.
    """
    try:
        result = db.cleanup_database()
        return ok(result)
    except Exception as e:  # noqa: BLE001
        return err(str(e))


class ClearDataBody(BaseModel):
    type: str


_CLEAR_DATA_TYPES = {"flows", "sessions", "rules", "ai_chats", "all"}


@router.post("/system/clear-data")
async def system_clear_data(request: Request, body: ClearDataBody):
    """Clear database data by category.

    Supported types:
    - flows / sessions / rules / ai_chats: clear the corresponding table(s)
    - all: clear all of the above (settings are preserved)

    For the destructive "all" type, the frontend should enforce a 3-second
    countdown confirmation before calling this endpoint.
    """
    if body.type not in _CLEAR_DATA_TYPES:
        return err(f"Invalid clear type: {body.type}")
    try:
        result = db.clear_data_by_type(body.type)
        return ok({"cleared": True, "type": body.type, **result})
    except Exception as e:  # noqa: BLE001
        return err(str(e))


@router.get("/system/platform-capabilities")
async def get_platform_capabilities(request: Request):
    """Return current platform capability info (frontend uses to show/hide feature buttons).

    Returns whether each feature is supported on this platform:
    - raw_capture: TCP/UDP capture (Windows WinDivert / Linux AF_PACKET / macOS BPF)
    - transparent_proxy: transparent proxy (Windows WinDivert / Linux iptables / macOS pf)
    - dns_hijack: DNS hijack (Windows WinDivert / Linux iptables+local_dns / macOS pf+local_dns)
    - system_proxy: system proxy config (Windows registry / macOS networksetup / Linux gsettings)
    - windivert_warning: WinDivert risk warning (Windows only)
    - admin_elevation: admin elevation (Windows UAC / Unix sudo)

    The is_admin result is cached in memory for 30 seconds to avoid flickering on the
    settings page caused by repeated privilege checks.
    """
    global _admin_cache
    now = time.time()
    if _admin_cache is not None and (now - _admin_cache["ts"]) < _ADMIN_CACHE_TTL:
        return ok(_admin_cache["data"])

    import os
    is_windows = sys.platform == "win32"
    is_linux = sys.platform.startswith("linux")
    is_macos = sys.platform == "darwin"
    is_unix = is_linux or is_macos

    # 检查 root/管理员权限
    if is_windows:
        try:
            import ctypes
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception as e:  # noqa: BLE001
            _capture_log("error", "API exception in system.py", extra={"exc": repr(e)})
            is_admin = False
    else:
        try:
            is_admin = os.geteuid() == 0
        except AttributeError:
            is_admin = False

    # 系统代理后端信息
    try:
        from .. import system_proxy
        sys_proxy_info = system_proxy.get_system_proxy_info()
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in system.py", extra={"exc": repr(e)})
        sys_proxy_info = {"supported": False, "backend": "none"}

    # 提权后端信息（UAC / osascript / pkexec / sudo）
    try:
        from .. import elevation
        elev_info = elevation.get_elevation_info()
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in system.py", extra={"exc": repr(e)})
        elev_info = {"backend": "uac" if is_windows else "sudo", "hint": ""}

    # 抓包后端
    if is_windows:
        raw_backend = "windivert"
        transparent_backend = "windivert"
        dns_backend = "windivert"
    elif is_linux:
        raw_backend = "af_packet"
        transparent_backend = "iptables"
        dns_backend = "iptables+local_dns"
    elif is_macos:
        raw_backend = "bpf"
        transparent_backend = "pf"
        dns_backend = "pf+local_dns"
    else:
        raw_backend = "none"
        transparent_backend = "none"
        dns_backend = "none"

    result = {
        "platform": sys.platform,
        "is_windows": is_windows,
        "is_linux": is_linux,
        "is_macos": is_macos,
        "is_unix": is_unix,
        "is_admin": is_admin,
        "capabilities": {
            "raw_capture": {
                "supported": is_windows or is_unix,
                "backend": raw_backend,
                "needs_admin": True,
                "admin_hint": (
                    "Administrator privileges required" if is_windows else
                    "Root privileges required (start with sudo)" if is_unix else
                    "Not supported"
                ),
            },
            "transparent_proxy": {
                "supported": is_windows or is_unix,
                "backend": transparent_backend,
                "needs_admin": True,
                "admin_hint": (
                    "Administrator privileges required" if is_windows else
                    "Root privileges required (start with sudo)" if is_unix else
                    "Not supported"
                ),
            },
            "dns_hijack": {
                "supported": is_windows or is_unix,
                "backend": dns_backend,
                "needs_admin": True,
                "admin_hint": (
                    "Administrator privileges required" if is_windows else
                    "Root privileges required (start with sudo)" if is_unix else
                    "Not supported"
                ),
            },
            "system_proxy": {
                "supported": sys_proxy_info.get("supported", False),
                "backend": sys_proxy_info.get("backend", "none"),
                "hint": sys_proxy_info.get("hint", ""),
            },
            "windivert_warning": {
                "supported": is_windows,
                "needed": windivert_warning_needed(),
                "ack": is_windivert_acknowledged(),
            },
            "admin_elevation": {
                "supported": True,
                # 用 elevation.get_elevation_info() 获取准确后端（uac/osascript/pkexec/sudo）
                "backend": elev_info.get("backend", "uac" if is_windows else "sudo"),
                "hint": elev_info.get("hint", ""),
            },
        },
    }
    _admin_cache = {"ts": time.time(), "data": result}
    return ok(result)

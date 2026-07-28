"""系统控制 API：重启服务 / 退出 / 关闭系统代理。

设计要点：
- 重启：通过 os.execv 在同进程内重启，继承同一控制台窗口。重启前清系统代理。
- 退出：调用 _quit_hook（由 __main__.py 注入），清代理 + sys.exit。
- 关闭系统代理：调用注入的 _clear_proxy_hook。
- 管理员重启（restart-as-admin）：先经 GUI 用户确认（置顶弹窗），同意后 ShellExecuteW runas。
  CLI 发起 → 后端创建 pending 请求并阻塞等待 → 前端轮询弹窗 → 用户响应 → 解除阻塞。
- WinDivert 风险提示：首次启用 WinDivert 相关功能（TCP/UDP 抓包 / 透明代理 / DNS 劫持）
  时弹窗告知用户该驱动可能被杀软拦截。GUI 走 Vue 对话框，CLI/MCP 走原生 MessageBox。
"""

import os
import sys
import threading
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import settings_store
from . import ok, err

router = APIRouter()

# 由 __main__.py 注入的钩子（可选）
_restart_hook = None
_quit_hook = None
_clear_proxy_hook = None
_enable_proxy_hook = None
# 系统代理当前是否开启（由 __main__ 更新）
system_proxy_on = False

# ------ 代理丢失检测 ------
# _proxy_lost：期望开启（system_proxy_on=True）但实际注册表已关闭/被抢占时置 True
# 前端轮询 /status 时检测该字段，弹窗询问是否重新开启
# 用户主动关闭代理（/system/clear-proxy）时同步重置为 False，避免误报
_proxy_lost = False
_proxy_monitor_thread: threading.Thread | None = None
_proxy_monitor_started = False
_proxy_monitor_lock = threading.Lock()


def set_hooks(restart=None, quit=None, clear_proxy=None, enable_proxy=None):
    """注入重启/退出/清代理/开代理的回调。"""
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
    """更新系统代理开关状态。

    用户主动开启/关闭代理时调用此函数。
    主动关闭时同时重置 _proxy_lost=False，避免前端继续弹窗。
    """
    global system_proxy_on, _proxy_lost
    system_proxy_on = v
    if not v:
        _proxy_lost = False


def is_proxy_lost() -> bool:
    """查询代理是否丢失（前端轮询 /status 时使用）。"""
    return _proxy_lost


def start_proxy_monitor():
    """启动代理状态监控线程（仅启动一次，由 __main__.py 调用）。

    每 5 秒读取注册表实际代理状态，与 system_proxy_on 对比：
    - 期望开启但实际关闭 → _proxy_lost=True
    - 期望关闭时不监控（避免误报）
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
    """读取系统实际代理开关状态（跨平台）。

    平台支持：
    - Windows: 读注册表 ProxyEnable
    - macOS: networksetup -getwebproxy 检查 Enabled
    - Linux GNOME: gsettings get org.gnome.system.proxy mode
    - Linux KDE: kreadconfig5 读 ProxyType
    """
    try:
        from .. import system_proxy
        return system_proxy.read_actual_proxy_enabled()
    except Exception:  # noqa: BLE001
        return False


def _proxy_monitor_loop():
    """代理状态监控循环（后台线程，跨平台）。

    每 5 秒读取系统实际代理状态，与 system_proxy_on 对比：
    - 期望开启但实际关闭 → _proxy_lost=True
    - 期望关闭时不监控（避免误报）

    平台支持：
    - Windows: 读注册表 ProxyEnable
    - macOS: networksetup -getwebproxy
    - Linux: gsettings/kreadconfig5
    - 不支持的平台：直接退出（无法监控）
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
                    print("[Telnix] 检测到系统代理已丢失", file=sys.stderr)
            elif actual and not (last_seen_actual is None or last_seen_actual):
                # 代理恢复了（前端弹窗后用户同意重新开启）
                if _proxy_lost:
                    global_set_proxy_lost(False)
                    print("[Telnix] 系统代理已恢复", file=sys.stderr)
            last_seen_actual = actual
        except Exception as e:  # noqa: BLE001
            print(f"[Telnix] 代理监控异常: {e}", file=sys.stderr)


def global_set_proxy_lost(v: bool):
    """线程安全地更新 _proxy_lost。"""
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
    """创建一个待确认的管理员重启请求，返回 request_id。

    创建后立即在新线程中弹原生 Windows MessageBox（置顶），用户响应后回传结果。
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
    """在新线程中弹原生 Windows Yes/No 弹窗，把结果回传到 pending 请求。"""
    title = "Telnix 管理员重启请求"
    message = (
        f"来源: {source}\n\n"
        "CLI / Agent 请求以管理员身份重启 Telnix，\n"
        "以辅助 agent 抓包（TCP/UDP 抓包需要管理员权限）。\n\n"
        "同意后将弹出 UAC 提权窗口，Telnix 会以管理员身份重启。\n\n"
        "是否同意？"
    )
    result = _show_native_message_box(title, message)
    response = 'accept' if result == 'yes' else 'reject'
    _respond_pending_request(rid, response)


def _show_native_message_box(title: str, message: str) -> str:
    """显示原生 Windows 置顶 Yes/No 弹窗，返回 'yes' / 'no'。

    使用 MB_TOPMOST | MB_SYSTEMMODAL | MB_SETFOREGROUND 确保窗口置顶显示，
    引起用户注意（任务栏图标闪烁 + 强制前置）。
    """
    if sys.platform != 'win32':
        # 非 Windows 平台退化：直接同意（无原生弹窗）
        return 'yes'
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
    except Exception:  # noqa: BLE001
        # 任何异常都视为拒绝（保守安全策略）
        return 'no'


def _wait_pending_request(rid: str, timeout: float = 60.0) -> str | None:
    """阻塞等待用户响应，返回 'accept' / 'reject' / None（超时）。"""
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
    """用户响应 pending 请求，返回是否成功。"""
    with _pending_lock:
        item = _pending_admin_requests.get(rid)
        if not item or item["response"] is not None:
            return False
        item["response"] = response
        item["event"].set()
    return True


def _cleanup_pending_request(rid: str):
    """清理已处理的 pending 请求。"""
    with _pending_lock:
        _pending_admin_requests.pop(rid, None)


def _list_pending_requests() -> list[dict]:
    """列出所有待确认请求（用于前端轮询）。"""
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
            "message": "CLI 请求以管理员身份重启 Telnix 以辅助 agent 抓包（TCP/UDP 抓包需要管理员权限）",
        })
    return out


def _do_shell_elevate() -> tuple[bool, str]:
    """执行提权，启动新的管理员/root 进程。返回 (success, message)。

    平台支持：
    - Windows: ShellExecuteW('runas') 触发 UAC
    - macOS: osascript with administrator privileges
    - Linux: pkexec（GUI）或 sudo（CLI）
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
        return False, f'提权失败: {e}'
    if not ok:
        return False, msg
    # 退出当前非管理员进程
    threading.Thread(target=_delayed_quit, daemon=True).start()
    return True, '已批准'


# ---------- WinDivert 风险提示 ----------
# WinDivert 是 Windows 内核驱动，Telnix 用它做 TCP/UDP 抓包 / 透明代理 / DNS 劫持。
# 部分杀毒软件会把 WinDivert64.sys 当成"漏洞驱动"拦截（漏洞利用工具也用它）。
# 因此首次启用相关功能前必须让用户知情同意，ack 后存到 settings.json 永久不再提示。

WINDIVERT_ACK_KEY = "windivert_warning_acknowledged"

_WINDIVERT_WARNING_MESSAGE = (
    "即将启用的功能需要加载 WinDivert64.sys 内核驱动。\n\n"
    "该驱动常被漏洞利用工具使用，部分杀毒软件（360 / 火绒 / Windows Defender 等）"
    "可能将其作为\"漏洞驱动\"拦截或报警，导致功能无法启动。\n\n"
    "Telnix 仅将该驱动用于抓包 / 透明代理 / DNS 劫持，"
    "不会对您的设备带来任何安全隐患。\n\n"
    "是否确认开启？"
)

_WINDIVERT_WARNING_BRIEF = (
    "即将加载 WinDivert64.sys 内核驱动，该驱动常被漏洞利用工具使用，"
    "部分杀毒软件可能将其作为\"漏洞驱动\"拦截或报警。"
    "Telnix 仅用于抓包 / 透明代理，不会对您的设备带来安全隐患。"
)


def is_windivert_acknowledged() -> bool:
    """用户是否已确认 WinDivert 风险提示。"""
    return settings_store.get_setting(WINDIVERT_ACK_KEY, "0") == "1"


def windivert_warning_needed() -> bool:
    """是否需要提示：仅 Windows 平台 + 未确认时为 True。"""
    return sys.platform == "win32" and not is_windivert_acknowledged()


def check_windivert_ack_or_block():
    """在 WinDivert 相关 API 入口调用。

    返回 None 表示已确认（或非 Windows 平台，由底层函数返回不支持），可继续执行。
    返回 JSONResponse（HTTP 403 + need_ack=true）表示需要确认，调用方应直接 return 该响应。
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
            "msg": "需要先确认 WinDivert 风险提示",
            "data": {"need_ack": True},
            "need_ack": True,
        },
    )


def acknowledge_windivert_warning() -> None:
    """标记为已确认（永久不再提示）。"""
    settings_store.set_setting(WINDIVERT_ACK_KEY, "1")


# ---- CLI/MCP 原生弹窗确认流程（类似 _pending_admin_requests） ----
# 待确认 ack 请求池：request_id → {event, response, created_at}
# - event: threading.Event，用户响应时 set
# - response: 'accept' | 'reject' | None（未响应）
_pending_windivert_acks: dict[str, dict] = {}
_pending_windivert_lock = threading.Lock()


def _create_pending_windivert_ack() -> str:
    """创建一个待确认的 WinDivert ack 请求，返回 request_id。

    创建后立即在新线程中弹原生 Windows MessageBox（置顶），用户响应后回传结果。
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
    """在新线程中弹原生 Windows Yes/No 弹窗，把结果回传到 pending ack 请求。"""
    title = "Telnix WinDivert 驱动风险提示"
    message = _WINDIVERT_WARNING_MESSAGE
    result = _show_native_message_box(title, message)
    response = 'accept' if result == 'yes' else 'reject'
    _respond_pending_windivert_ack(rid, response)
    # 若用户同意，立即标记 ack=1（settings.json 持久化，永久不再提示）
    if response == 'accept':
        try:
            acknowledge_windivert_warning()
        except Exception:  # noqa: BLE001
            pass


def _wait_pending_windivert_ack(rid: str, timeout: float = 60.0) -> str | None:
    """阻塞等待用户响应，返回 'accept' / 'reject' / None（超时）。"""
    with _pending_windivert_lock:
        item = _pending_windivert_acks.get(rid)
    if not item:
        return None
    triggered = item["event"].wait(timeout=timeout)
    if not triggered:
        return None
    return item["response"]


def _respond_pending_windivert_ack(rid: str, response: str) -> bool:
    """用户响应 pending ack 请求，返回是否成功。"""
    with _pending_windivert_lock:
        item = _pending_windivert_acks.get(rid)
        if not item or item["response"] is not None:
            return False
        item["response"] = response
        item["event"].set()
    return True


def _cleanup_pending_windivert_ack(rid: str):
    """清理已处理的 pending ack 请求。"""
    with _pending_windivert_lock:
        _pending_windivert_acks.pop(rid, None)


@router.post("/system/restart")
async def restart_service(request: Request):
    """重启前后端服务（同进程内 os.execv 重启）。"""
    if _restart_hook is None:
        return err("重启钩子未注入")
    # 异步执行，避免请求未返回就退出
    threading.Thread(target=_delayed_restart, daemon=True).start()
    return ok({"restarting": True}, "正在重启服务，请稍候...")


@router.post("/system/quit")
async def quit_service(request: Request):
    """退出 Telnix（关闭前后端 + 清系统代理）。"""
    threading.Thread(target=_delayed_quit, daemon=True).start()
    return ok({"quitting": True}, "正在退出 Telnix...")


@router.post("/system/clear-proxy")
async def clear_proxy(request: Request):
    """仅关闭系统代理（保留服务运行）。"""
    if _clear_proxy_hook is not None:
        try:
            _clear_proxy_hook()
        except Exception as e:  # noqa: BLE001
            return err(f"清代理失败: {e}")
    mark_proxy_on(False)
    return ok({"cleared": True, "system_proxy_on": False}, "系统代理已关闭")


@router.post("/system/enable-proxy")
async def enable_proxy(request: Request):
    """重新开启系统代理（指向 Telnix 代理端口）。"""
    if _enable_proxy_hook is None:
        return err("开代理钩子未注入")
    try:
        _enable_proxy_hook()
    except Exception as e:  # noqa: BLE001
        return err(f"开启代理失败: {e}")
    mark_proxy_on(True)
    return ok({"enabled": True, "system_proxy_on": True}, "系统代理已开启")


@router.post("/system/restart-as-admin")
async def restart_as_admin(request: Request):
    """以管理员身份重启 Telnix（UAC 提权）。

    兼容旧接口：直接弹 UAC，不经 GUI 确认。
    新的 CLI 流程请使用 /system/request-admin-restart + /system/admin-request/{id}/wait。
    """
    success, msg = _do_shell_elevate()
    if not success:
        return err(msg)
    return ok({'restarting': True}, '正在以管理员身份重启 Telnix...')


@router.post("/system/request-admin-restart")
async def request_admin_restart(request: Request):
    """创建一个待 GUI 确认的管理员重启请求。

    返回 request_id，CLI 应通过 /system/admin-request/{id}/wait 长轮询等待响应。
    """
    rid = _create_pending_request(source="cli")
    return ok({
        "request_id": rid,
        "message": "已创建待确认请求，等待 GUI 用户响应",
    })


@router.get("/system/admin-request/{rid}/wait")
async def admin_request_wait(rid: str, request: Request):
    """长轮询等待用户响应，超时 60s 返回 pending。"""
    import asyncio
    # 检查 rid 是否存在
    with _pending_lock:
        item = _pending_admin_requests.get(rid)
    if not item:
        return err("请求不存在或已处理")
    # 阻塞等待放到线程池，避免阻塞事件循环
    response = await asyncio.to_thread(_wait_pending_request, rid, 60.0)
    if response is None:
        return ok({"status": "pending", "request_id": rid}, "仍在等待用户响应")
    # 已响应
    if response == "accept":
        # 执行 UAC 提权
        success, msg = await asyncio.to_thread(_do_shell_elevate)
        _cleanup_pending_request(rid)
        if not success:
            return err(msg)
        return ok({"status": "accepted", "request_id": rid}, "用户已批准，正在以管理员身份重启")
    else:
        _cleanup_pending_request(rid)
        return ok({"status": "rejected", "request_id": rid}, "用户拒绝了管理员重启请求")


@router.get("/system/pending-admin-actions")
async def pending_admin_actions(request: Request):
    """前端轮询：获取待确认的管理员动作列表。"""
    return ok({"items": _list_pending_requests()})


@router.post("/system/admin-request/{rid}/respond")
async def admin_request_respond(rid: str, request: Request):
    """前端用户响应 pending 请求（accept/reject）。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    response = (body.get("response") or "").lower()
    if response not in ("accept", "reject"):
        return err("response 必须是 accept 或 reject")
    success = _respond_pending_request(rid, response)
    if not success:
        return err("请求不存在或已响应")
    return ok({"request_id": rid, "response": response}, "已提交响应")


# ---------- WinDivert 风险提示 API ----------
# GET  /api/system/windivert-warning       - 查询是否需要提示 + 当前 ack 状态 + 风险说明文本
# POST /api/system/windivert-warning/ack    - 标记为已确认（前端"了解，不再显示此提示"按钮调用）
# POST /api/system/request-windivert-ack   - 创建 pending ack 请求 + 桌面置顶原生弹窗（CLI/MCP 用）
# GET  /api/system/windivert-ack-request/{rid}/wait - 长轮询等待用户响应（CLI/MCP 用）


@router.get("/system/windivert-warning")
async def get_windivert_warning(request: Request):
    """查询 WinDivert 风险提示状态。

    返回:
      needed: 是否需要提示（仅 Windows 平台 + 未确认时为 true）
      message: 风险说明文本（前端弹窗正文用）
      ack: 当前是否已确认
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
    """标记 WinDivert 风险提示为已确认（永久不再提示）。

    由前端"了解，不再显示此提示"按钮调用，也可由 CLI/MCP 在原生弹窗用户选"是"后调用。
    """
    acknowledge_windivert_warning()
    return ok({"ack": True}, "已确认 WinDivert 风险提示，后续不再提示")


@router.post("/system/request-windivert-ack")
async def request_windivert_ack(request: Request):
    """创建一个待 GUI 用户确认的 WinDivert ack 请求（CLI/MCP 用）。

    创建后立即在新线程中弹原生 Windows MessageBox（置顶）。
    CLI 应通过 /system/windivert-ack-request/{id}/wait 长轮询等待响应。
    返回 request_id。
    """
    # 非 Windows 平台：无需 ack（底层功能直接返回不支持）
    if sys.platform != "win32":
        return ok({"request_id": None, "skipped": True},
                  "非 Windows 平台无需 WinDivert 风险提示")
    # 已确认：直接跳过
    if is_windivert_acknowledged():
        return ok({"request_id": None, "skipped": True, "ack": True},
                  "已确认过 WinDivert 风险提示")
    rid = _create_pending_windivert_ack()
    return ok({
        "request_id": rid,
        "message": "已创建待确认请求，等待 GUI 用户响应",
    })


@router.get("/system/windivert-ack-request/{rid}/wait")
async def windivert_ack_request_wait(rid: str, request: Request):
    """长轮询等待用户响应，超时 60s 返回 pending。"""
    import asyncio
    with _pending_windivert_lock:
        item = _pending_windivert_acks.get(rid)
    if not item:
        return err("请求不存在或已处理")
    response = await asyncio.to_thread(_wait_pending_windivert_ack, rid, 60.0)
    if response is None:
        return ok({"status": "pending", "request_id": rid}, "仍在等待用户响应")
    _cleanup_pending_windivert_ack(rid)
    if response == "accept":
        return ok({"status": "accepted", "request_id": rid, "ack": True},
                  "用户已确认 WinDivert 风险提示")
    else:
        return ok({"status": "rejected", "request_id": rid, "ack": False},
                   "用户拒绝了 WinDivert 风险提示")


def _delayed_restart():
    """延迟 500ms 执行重启，让 HTTP 响应先返回。"""
    import time
    time.sleep(0.5)
    try:
        _restart_hook()
    except Exception as e:  # noqa: BLE001
        print(f"[Telnix] 重启失败: {e}", file=sys.stderr)


def _delayed_quit():
    """延迟 500ms 执行退出。"""
    import time
    time.sleep(0.5)
    try:
        _quit_hook()
    except Exception as e:  # noqa: BLE001
        print(f"[Telnix] 退出失败: {e}", file=sys.stderr)


# ---------- 可选依赖安装（pip install mitmproxy） ----------
# 安装任务状态：idle / running / success / failed
# 只允许同时一个安装任务（避免 pip 冲突）
_install_lock = threading.Lock()
_install_state = {
    "status": "idle",       # idle / running / success / failed
    "package": None,        # 当前/上次安装的包名
    "started_at": None,     # 开始时间戳
    "finished_at": None,    # 结束时间戳
    "log": "",              # 累计日志输出（尾部截断到 64KB 避免无限增长）
    "return_code": None,    # pip 返回码（0=成功）
}
_INSTALL_LOG_MAX = 64 * 1024  # 日志最大长度


def _append_install_log(text: str):
    """追加安装日志，超过上限时丢弃头部。"""
    global _install_state
    _install_state["log"] = (_install_state["log"] + text)
    if len(_install_state["log"]) > _INSTALL_LOG_MAX:
        _install_state["log"] = _install_state["log"][-_INSTALL_LOG_MAX:]


def _run_pip_install(package: str, lock: threading.Lock):
    """在工作线程中执行 pip install，并更新全局 _install_state。

    使用 subprocess.Popen 行读取 stdout/stderr 实时累计日志。
    lock 由调用线程传入，本函数负责在结束时释放。
    """
    global _install_state
    import subprocess
    python = sys.executable
    cmd = [python, "-m", "pip", "install", "--disable-pip-version-check", package]
    _append_install_log(f"$ {' '.join(cmd)}\n")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            _append_install_log(line)
        proc.wait()
        rc = proc.returncode
        _install_state["return_code"] = rc
        if rc == 0:
            _install_state["status"] = "success"
            _append_install_log(f"\n[OK] {package} 安装成功\n")
        else:
            _install_state["status"] = "failed"
            _append_install_log(f"\n[FAIL] {package} 安装失败，返回码 {rc}\n")
    except Exception as e:  # noqa: BLE001
        _install_state["status"] = "failed"
        _install_state["return_code"] = -1
        _append_install_log(f"\n[ERROR] 执行异常: {e}\n")
    finally:
        _install_state["finished_at"] = time.time()
        # 释放锁，允许后续安装任务
        try:
            lock.release()
        except RuntimeError:  # noqa: BLE001
            pass  # 锁已被释放（异常路径兜底）


# 允许安装的可选依赖白名单（防止任意命令注入）
_INSTALLABLE_PACKAGES = {
    "mitmproxy": "mitmproxy",
    "pydivert": "pydivert",  # Windows 抓包驱动；Unix 不需要（AF_PACKET/BPF）
}


@router.post("/system/install-dep")
async def install_dep(request: Request):
    """触发 pip 安装可选依赖（如 mitmproxy）。

    请求体: {"package": "mitmproxy"}
    返回: {"status": "running"} 或错误信息
    同一时刻只允许一个安装任务，已有任务运行中时返回 409。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    package = (body.get("package") or "").strip().lower()
    if package not in _INSTALLABLE_PACKAGES:
        return err(f"不支持的包名: {package}（当前仅支持: {', '.join(_INSTALLABLE_PACKAGES)}）")
    real_name = _INSTALLABLE_PACKAGES[package]
    acquired = _install_lock.acquire(blocking=False)
    if not acquired:
        return err("已有安装任务在执行中，请等待完成", code=409)
    try:
        # 重置状态
        _install_state.update({
            "status": "running",
            "package": real_name,
            "started_at": time.time(),
            "finished_at": None,
            "log": "",
            "return_code": None,
        })
        threading.Thread(
            target=_run_pip_install,
            args=(real_name, _install_lock),
            daemon=True,
            name=f"pip-install-{real_name}",
        ).start()
        return ok({
            "status": "running",
            "package": real_name,
        }, f"正在安装 {real_name}，可通过 /system/install-dep/status 查询进度")
    except Exception as e:  # noqa: BLE001
        # 启动线程失败：释放锁并重置状态
        try:
            _install_lock.release()
        except RuntimeError:  # noqa: BLE001
            pass
        _install_state["status"] = "failed"
        _install_state["finished_at"] = time.time()
        return err(f"启动安装任务失败: {e}")


@router.get("/system/install-dep/status")
async def install_dep_status(request: Request):
    """查询安装任务状态。

    返回: {"status": "idle|running|success|failed", "package": ..., "log": ..., ...}
    status=success 时同时返回 mitmproxy_available（重新检测是否可导入）。
    """
    state = dict(_install_state)
    # 安装成功后重新检测 mitmproxy 可用性
    if state.get("status") == "success" and state.get("package") == "mitmproxy":
        try:
            # 子进程安装完，当前进程需重新 import（pip 装到 site-packages 后新 import 可生效）
            import importlib
            mod = importlib.import_module("mitmproxy")
            state["mitmproxy_available"] = True
            state["mitmproxy_version"] = getattr(mod, "__version__", None)
        except Exception:  # noqa: BLE001
            # 安装成功但当前进程未生效（需重启 Telnix）
            state["mitmproxy_available"] = False
            state["mitmproxy_version"] = None
            state["note"] = "安装成功但当前进程尚未加载，需重启 Telnix 后生效"
    return ok(state)


@router.post("/system/install-dep/cancel")
async def install_dep_cancel(request: Request):
    """取消正在运行的安装任务（仅标记状态，实际 pip 进程会被强杀）。

    简化实现：仅在状态为 running 时返回提示（pip 子进程无法干净终止）。
    """
    if _install_state["status"] != "running":
        return ok({"status": _install_state["status"]}, "无运行中的安装任务")
    return err("pip 安装无法干净取消，请等待完成或重启 Telnix", code=400)


@router.get("/system/platform-capabilities")
async def get_platform_capabilities(request: Request):
    """返回当前平台的能力信息（前端用于显示/隐藏功能按钮）。

    返回各功能在该平台是否支持：
    - raw_capture: TCP/UDP 抓包（Windows WinDivert / Linux AF_PACKET / macOS BPF）
    - transparent_proxy: 透明代理（Windows WinDivert / Linux iptables / macOS pf）
    - dns_hijack: DNS 劫持（Windows WinDivert / Linux iptables+local_dns / macOS pf+local_dns）
    - system_proxy: 系统代理配置（Windows registry / macOS networksetup / Linux gsettings）
    - windivert_warning: WinDivert 风险提示（仅 Windows 需要）
    - admin_elevation: 管理员提权（Windows UAC / Unix sudo）
    """
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
        except Exception:  # noqa: BLE001
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
    except Exception:  # noqa: BLE001
        sys_proxy_info = {"supported": False, "backend": "none"}

    # 提权后端信息（UAC / osascript / pkexec / sudo）
    try:
        from .. import elevation
        elev_info = elevation.get_elevation_info()
    except Exception:  # noqa: BLE001
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

    return ok({
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
                    "需要管理员权限" if is_windows else
                    "需要 root 权限（sudo 启动）" if is_unix else
                    "不支持"
                ),
            },
            "transparent_proxy": {
                "supported": is_windows or is_unix,
                "backend": transparent_backend,
                "needs_admin": True,
                "admin_hint": (
                    "需要管理员权限" if is_windows else
                    "需要 root 权限（sudo 启动）" if is_unix else
                    "不支持"
                ),
            },
            "dns_hijack": {
                "supported": is_windows or is_unix,
                "backend": dns_backend,
                "needs_admin": True,
                "admin_hint": (
                    "需要管理员权限" if is_windows else
                    "需要 root 权限（sudo 启动）" if is_unix else
                    "不支持"
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
    })

"""TCP/UDP 原始抓包 API（跨平台）。

平台支持：
- Windows: WinDivert 内核驱动（需 pydivert + 管理员权限）
- Linux: AF_PACKET raw socket（需 root）
- macOS: BPF 设备（需 root）
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..proxy.raw_capture import (
    start_raw_capture, stop_raw_capture, raw_capture_status,
)
from . import err, ok
from .system import check_windivert_ack_or_block

router = APIRouter()


class RawStartBody(BaseModel):
    pid_filter: list[int] | None = None
    port_filter: list[int] | None = None
    filter_str: str = ""


@router.get("/raw/status")
async def raw_status():
    """TCP/UDP 抓包状态。"""
    return ok(raw_capture_status())


@router.post("/raw/start")
async def raw_start(request: Request, body: RawStartBody):
    """启动 TCP/UDP 抓包。需要管理员权限 + pydivert。

    无活动会话时自动创建一个，避免要求用户先去抓包页点开始。
    首次启用前必须确认 WinDivert 风险提示（Windows 平台），未确认时返回 403 + need_ack=true。
    """
    # WinDivert 风险提示检查（仅 Windows + 未确认时拦截）
    block = check_windivert_ack_or_block()
    if block is not None:
        return block
    from datetime import datetime
    from .. import db
    state = request.app.state.telnix
    session_id = state.current_session_id
    if not session_id:
        # 自动创建会话（不开启 HTTP 抓包，仅用于承载 TCP/UDP 流量）
        session_id = db.create_session(
            name=f"TCP/UDP 会话 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        state.current_session_id = session_id
    pid_set = set(body.pid_filter) if body.pid_filter else None
    port_set = set(body.port_filter) if body.port_filter else None
    success, msg = start_raw_capture(
        session_id, pid_filter=pid_set, port_filter=port_set,
        filter_str=body.filter_str,
    )
    if success:
        return ok({"running": True, "msg": msg, "session_id": session_id})
    return err(msg)


@router.post("/raw/stop")
async def raw_stop():
    """停止 TCP/UDP 抓包。"""
    success, msg = stop_raw_capture()
    if success:
        return ok({"running": False, "msg": msg})
    return err(msg)

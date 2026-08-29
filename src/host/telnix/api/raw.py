"""TCP/UDP raw capture API (cross-platform).

Platform support:
- Windows: WinDivert kernel driver (requires pydivert + administrator privileges)
- Linux: AF_PACKET raw socket (requires root)
- macOS: BPF device (requires root)
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..proxy.raw_capture import (
    start_raw_capture, stop_raw_capture, raw_capture_status,
    _RAW_STATS, _get_raw_stats, _reset_raw_stats,
)
from . import err, ok
from .system import check_windivert_ack_or_block

router = APIRouter()


class RawStartBody(BaseModel):
    pid_filter: list[int] | None = None
    port_filter: list[int] | None = None
    filter_str: str = ""


# ---------- P1 监控统计 API ----------

@router.get("/raw/stats")
async def raw_stats():
    """Return real-time capture statistics (pps, bandwidth, drops, protocol distribution)."""
    stats = _get_raw_stats()
    return ok(stats)


@router.post("/raw/stats/reset")
async def raw_stats_reset():
    """Reset capture statistics counters."""
    _reset_raw_stats()
    return ok({"reset": True})


@router.get("/raw/status")
async def raw_status():
    """TCP/UDP capture status."""
    return ok(raw_capture_status())


@router.post("/raw/start")
async def raw_start(request: Request, body: RawStartBody):
    """Start TCP/UDP capture. Requires administrator privileges + pydivert.

    Auto-creates a session when there's no active session, to avoid requiring user to go to capture page to start first.
    Before first enable, must confirm WinDivert risk warning (Windows platform), returns 403 + need_ack=true if not confirmed.
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
            name=f"TCP/UDP Session {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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
    """Stop TCP/UDP capture."""
    success, msg = stop_raw_capture()
    if success:
        return ok({"running": False, "msg": msg})
    return err(msg)

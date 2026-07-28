"""透明代理模式 API。"""

from fastapi import APIRouter

from ..proxy.transparent_proxy import (
    start_transparent_proxy, stop_transparent_proxy, transparent_proxy_status,
)
from . import err, ok
from .system import check_windivert_ack_or_block

router = APIRouter()


@router.get("/transparent-proxy/status")
async def status():
    """透明代理状态。"""
    return ok(transparent_proxy_status())


@router.post("/transparent-proxy/start")
async def start():
    """启动透明代理。需管理员权限。

    首次启用前必须确认 WinDivert 风险提示（Windows 平台），未确认时返回 403 + need_ack=true。
    """
    # WinDivert 风险提示检查（仅 Windows + 未确认时拦截）
    block = check_windivert_ack_or_block()
    if block is not None:
        return block
    success, msg = start_transparent_proxy()
    if success:
        return ok({"running": True, "msg": msg})
    return err(msg)


@router.post("/transparent-proxy/stop")
async def stop():
    """停止透明代理。"""
    success, msg = stop_transparent_proxy()
    if success:
        return ok({"running": False, "msg": msg})
    return err(msg)

"""Transparent proxy mode API."""

from fastapi import APIRouter

from ..proxy.transparent_proxy import (
    start_transparent_proxy, stop_transparent_proxy, transparent_proxy_status,
)
from ..logger import _capture_log
from . import err, ok
from .system import check_windivert_ack_or_block

router = APIRouter()


@router.get("/transparent-proxy/status")
async def status():
    """Transparent proxy status."""
    return ok(transparent_proxy_status())


@router.post("/transparent-proxy/start")
async def start():
    """Start transparent proxy. Requires administrator privileges.

    WinDivert risk warning must be acknowledged before first enable (Windows platform), returns 403 + need_ack=true if not acknowledged.
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
    """Stop transparent proxy."""
    success, msg = stop_transparent_proxy()
    if success:
        return ok({"running": False, "msg": msg})
    return err(msg)

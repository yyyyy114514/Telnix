"""DNS 劫持 REST API。"""
from fastapi import APIRouter
from pydantic import BaseModel

from ..proxy.dns_hijack import (
    start_hijack, stop_hijack, update_rules, hijack_status, clear_log,
)
from . import err, ok
from .system import check_windivert_ack_or_block

router = APIRouter()


class RulesBody(BaseModel):
    """规则更新请求体。

    rules: {domain: fake_ip}，domain 支持通配符前缀 *.example.com
    default_ip: 默认劫持 IP（所有未匹配规则的 A 记录查询都返回此 IP）
    """
    rules: dict[str, str] = {}
    default_ip: str = ""


class StartBody(RulesBody):
    """启动请求体。"""
    pass


@router.get("/dns-hijack/status")
async def get_status():
    """获取 DNS 劫持当前状态。"""
    return ok(hijack_status())


@router.post("/dns-hijack/start")
async def start(body: StartBody):
    """启动 DNS 劫持。

    需要管理员权限 + WinDivert 驱动。
    启动时传入初始规则；运行中可通过 /dns-hijack/rules 热更新。
    首次启用前必须确认 WinDivert 风险提示（Windows 平台），未确认时返回 403 + need_ack=true。
    """
    # WinDivert 风险提示检查（仅 Windows + 未确认时拦截）
    block = check_windivert_ack_or_block()
    if block is not None:
        return block
    success, msg = start_hijack(body.rules, body.default_ip)
    if success:
        return ok({"running": True}, msg)
    return err(msg)


@router.post("/dns-hijack/stop")
async def stop():
    """停止 DNS 劫持。"""
    success, msg = stop_hijack()
    if success:
        return ok({"running": False}, msg)
    return err(msg)


@router.put("/dns-hijack/rules")
async def set_rules(body: RulesBody):
    """运行时更新规则（热更新，无需重启）。"""
    success, msg = update_rules(body.rules, body.default_ip)
    if success:
        return ok(hijack_status(), msg)
    return err(msg)


@router.post("/dns-hijack/clear-log")
async def do_clear_log():
    """清空劫持日志和统计计数器。"""
    clear_log()
    return ok({"cleared": True}, "已清空")

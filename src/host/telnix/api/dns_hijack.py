"""DNS 劫持 REST API。"""
import ipaddress

from fastapi import APIRouter
from pydantic import BaseModel

from ..proxy.dns_hijack import (
    start_hijack, stop_hijack, update_rules, hijack_status, clear_log,
)
from . import err, ok
from .system import check_windivert_ack_or_block

router = APIRouter()


def _validate_hijack_ips(rules: dict, default_ip: str) -> str | None:
    """校验劫持规则中的 fake IP 是否合法（IPv4 / IPv6 字面量）。

    返回 None 表示全部合法；否则返回一条可读的错误描述。
    防止把非 IP 字符串当作解析目标（运行时 inet_aton 会跳过，但
    提前校验能让用户在配置阶段就发现错误，避免静默失效）。
    """
    for domain, ip in (rules or {}).items():
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return f"规则 IP 非法：{domain} -> {ip!r}（必须为合法 IPv4/IPv6 地址）"
    if default_ip:
        try:
            ipaddress.ip_address(default_ip)
        except ValueError:
            return f"默认劫持 IP 非法：{default_ip!r}（必须为合法 IPv4/IPv6 地址）"
    return None


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
    """启动 DNS 劫持（跨平台）。

    平台支持：
    - Windows: WinDivert 拦截模式（需 pydivert + 管理员权限）
    - Linux: iptables NAT + 本地 DNS 服务器（需 root）
    - macOS: pf rdr + 本地 DNS 服务器（需 root）

    启动时传入初始规则；运行中可通过 /dns-hijack/rules 热更新。
    首次启用前必须确认 WinDivert 风险提示（仅 Windows 平台），未确认时返回 403 + need_ack=true。
    """
    # WinDivert 风险提示检查（仅 Windows + 未确认时拦截）
    block = check_windivert_ack_or_block()
    if block is not None:
        return block
    # 校验劫持 IP 合法性（防误配 + 输入净化）
    err_msg = _validate_hijack_ips(body.rules, body.default_ip)
    if err_msg is not None:
        return err(err_msg)
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
    # 校验劫持 IP 合法性（防误配 + 输入净化）
    err_msg = _validate_hijack_ips(body.rules, body.default_ip)
    if err_msg is not None:
        return err(err_msg)
    success, msg = update_rules(body.rules, body.default_ip)
    if success:
        return ok(hijack_status(), msg)
    return err(msg)


@router.post("/dns-hijack/clear-log")
async def do_clear_log():
    """清空劫持日志和统计计数器。"""
    clear_log()
    return ok({"cleared": True}, "已清空")

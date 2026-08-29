"""DNS hijack REST API."""
import ipaddress

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db
from ..proxy.dns_hijack import (
    start_hijack, stop_hijack, update_rules, hijack_status, clear_log,
    detect_doh_request, detect_doh_in_flows, get_doh_status,
)
from ..logger import _capture_log
from . import err, ok
from .system import check_windivert_ack_or_block

router = APIRouter()


# ============ Pydantic 模型 ============

class DnsGroupCreate(BaseModel):
    """创建分组请求体"""
    name: str
    priority: int = 0
    enabled: bool = True


class DnsGroupUpdate(BaseModel):
    """更新分组请求体"""
    name: str | None = None
    priority: int | None = None
    enabled: bool | None = None


class DnsGroupReorder(BaseModel):
    """重排分组请求体"""
    group_ids: list[int]


class DnsRuleCreate(BaseModel):
    """创建规则请求体"""
    pattern: str
    mode: str = "wildcard"  # wildcard | exact | regex
    action: str = "block"  # allow | block | redirect
    redirect_to: str | None = None


class DnsRuleUpdate(BaseModel):
    """更新规则请求体"""
    pattern: str | None = None
    mode: str | None = None
    action: str | None = None
    redirect_to: str | None = None


# ============ 辅助函数 ============

def _validate_hijack_ips(rules: dict, default_ip: str) -> str | None:
    """Validate whether fake IP in hijack rules is valid (IPv4 / IPv6 literal).

    Returns None if all valid; otherwise returns a readable error description.
    Prevents treating non-IP strings as parse targets (runtime inet_aton will skip, but
    early validation lets users discover errors at config stage, avoiding silent failures).
    """
    for domain, ip in (rules or {}).items():
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return f"Invalid rule IP: {domain} -> {ip!r} (must be a valid IPv4/IPv6 address)"
    if default_ip:
        try:
            ipaddress.ip_address(default_ip)
        except ValueError:
            return f"Invalid default hijack IP: {default_ip!r} (must be a valid IPv4/IPv6 address)"
    return None


# ============ 原有的规则 API（保留兼容） ============

class RulesBody(BaseModel):
    """Rule update request body.

    rules: {domain: fake_ip}, domain supports wildcard prefix *.example.com
    default_ip: default hijack IP (all A record queries not matching rules return this IP)
    """
    rules: dict[str, str] = {}
    default_ip: str = ""


class StartBody(RulesBody):
    """Start request body."""
    pass


# ============ DNS 分组管理 API ============

@router.get("/dns-hijack/groups")
async def get_groups():
    """获取所有 DNS 规则分组（按优先级排序）。"""
    groups = db.get_dns_groups()
    return ok(groups)


@router.post("/dns-hijack/groups")
async def create_group(body: DnsGroupCreate):
    """创建 DNS 规则分组。"""
    if not body.name.strip():
        return err("分组名称不能为空")
    group_id = db.create_dns_group(body.name.strip(), body.priority, body.enabled)
    # 返回创建后的分组（含 rule_count=0）
    groups = db.get_dns_groups()
    for g in groups:
        if g["id"] == group_id:
            return ok(g, "分组创建成功")
    return ok({"id": group_id, "name": body.name, "priority": body.priority, "enabled": body.enabled, "rule_count": 0})


@router.put("/dns-hijack/groups/{group_id}")
async def update_group(group_id: int, body: DnsGroupUpdate):
    """更新 DNS 规则分组。"""
    if body.name is not None and not body.name.strip():
        return err("分组名称不能为空")
    success = db.update_dns_group(
        group_id,
        name=body.name.strip() if body.name is not None else None,
        priority=body.priority,
        enabled=body.enabled,
    )
    if not success:
        return err("分组不存在或更新失败", code=404)
    groups = db.get_dns_groups()
    for g in groups:
        if g["id"] == group_id:
            return ok(g)
    return ok({"id": group_id})


@router.delete("/dns-hijack/groups/{group_id}")
async def delete_group(group_id: int):
    """删除 DNS 规则分组及其所有规则。"""
    success = db.delete_dns_group(group_id)
    if not success:
        return err("分组不存在或删除失败", code=404)
    return ok({"deleted": True}, "分组已删除")


@router.post("/dns-hijack/groups/reorder")
async def reorder_groups(body: DnsGroupReorder):
    """批量更新分组优先级（拖拽排序）。"""
    if not body.group_ids:
        return err("分组 ID 列表不能为空")
    db.reorder_dns_groups(body.group_ids)
    groups = db.get_dns_groups()
    return ok(groups)


@router.get("/dns-hijack/groups/{group_id}/rules")
async def get_group_rules(group_id: int):
    """获取指定分组的所有规则。"""
    rules = db.get_dns_rules(group_id)
    return ok(rules)


@router.post("/dns-hijack/groups/{group_id}/rules")
async def create_rule(group_id: int, body: DnsRuleCreate):
    """在指定分组中添加规则。"""
    if not body.pattern.strip():
        return err("域名模式不能为空")
    if body.action not in ("allow", "block", "redirect"):
        return err("无效的动作类型")
    if body.action == "redirect" and not body.redirect_to:
        return err("redirect 动作需要指定 redirect_to")
    rule_id = db.create_dns_rule(
        group_id,
        body.pattern.strip(),
        body.mode,
        body.action,
        body.redirect_to.strip() if body.redirect_to else None,
    )
    rules = db.get_dns_rules(group_id)
    for r in rules:
        if r["id"] == rule_id:
            return ok(r)
    return ok({"id": rule_id, "pattern": body.pattern, "mode": body.mode, "action": body.action, "redirect_to": body.redirect_to})


@router.put("/dns-hijack/rules/{rule_id}")
async def update_rule(rule_id: int, body: DnsRuleUpdate):
    """更新 DNS 规则。"""
    if body.action not in (None, "allow", "block", "redirect"):
        return err("无效的动作类型")
    if body.action == "redirect" and not body.redirect_to:
        return err("redirect 动作需要指定 redirect_to")
    success = db.update_dns_rule(
        rule_id,
        pattern=body.pattern.strip() if body.pattern is not None else None,
        mode=body.mode,
        action=body.action,
        redirect_to=body.redirect_to.strip() if body.redirect_to is not None else None,
    )
    if not success:
        return err("规则不存在或更新失败", code=404)
    # 获取规则所在分组
    rules = db.get_dns_rules()
    for r in rules:
        if r["id"] == rule_id:
            return ok(r)
    return ok({"id": rule_id})


@router.delete("/dns-hijack/rules/{rule_id}")
async def delete_rule(rule_id: int):
    """删除 DNS 规则。"""
    success = db.delete_dns_rule(rule_id)
    if not success:
        return err("规则不存在或删除失败", code=404)
    return ok({"deleted": True}, "规则已删除")


@router.get("/dns-hijack/rules")
async def get_all_rules():
    """获取所有 DNS 规则（带分组信息）。"""
    rules = db.get_dns_rules()
    return ok(rules)


@router.post("/dns-hijack/apply")
async def apply_group_rules():
    """从分组规则生成并应用劫持规则（将分组规则转换为旧格式的 {domain: ip} 字典）。"""
    rules = db.get_dns_rules_for_hijack()
    success, msg = update_rules(rules, "")
    if success:
        return ok({"applied": True, "rule_count": len(rules)}, "已应用分组规则")
    return err(msg)


# ============ 原有的 DNS 劫持 API ============

@router.get("/dns-hijack/status")
async def get_status():
    """Get DNS hijack current status."""
    return ok(hijack_status())


@router.post("/dns-hijack/start")
async def start(body: StartBody):
    """Start DNS hijack (cross-platform).

    Platform support:
    - Windows: WinDivert interception mode (requires pydivert + administrator privileges)
    - Linux: iptables NAT + local DNS server (requires root)
    - macOS: pf rdr + local DNS server (requires root)

    Initial rules passed in at start; can be hot-updated via /dns-hijack/rules during runtime.
    Before first enable, must confirm WinDivert risk warning (Windows only), returns 403 + need_ack=true if not confirmed.
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
    """Stop DNS hijack."""
    success, msg = stop_hijack()
    if success:
        return ok({"running": False}, msg)
    return err(msg)


@router.put("/dns-hijack/rules")
async def set_rules(body: RulesBody):
    """Update rules at runtime (hot update, no restart needed)."""
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
    """Clear hijack log and statistics counters."""
    clear_log()
    return ok({"cleared": True}, "Cleared")


# ============ DoH/DoT 检测相关 API ============

class FlowItem(BaseModel):
    """单个流量数据模型（用于 DoH 检测）"""
    id: int | str | None = None
    host: str | None = None
    url: str | None = None
    method: str | None = None
    sni: str | None = None
    headers: dict[str, str] | None = None
    port: int | None = None
    scheme: str | None = None


class DoHDetectBody(BaseModel):
    """DoH 检测请求体"""
    flows: list[FlowItem] = []


@router.get("/dns-hijack/doh-status")
async def doh_status():
    """获取 DoH 检测状态和已知 DoH 提供商列表。

    Returns:
        - known_providers: 已知 DoH 服务器域名列表
        - known_dot_providers: 已知 DoT 服务器域名列表
        - supported_content_types: 支持的 DoH Content-Type
    """
    return ok(get_doh_status())


@router.post("/dns-hijack/doh-detect")
async def doh_detect(body: DoHDetectBody):
    """检测一批流量中的 DoH/DoT 请求。

    用于在抓包过程中检测是否有 DoH 流量绕过 DNS 劫持。

    Args:
        body.flows: 流量列表，每个流量应包含 host, url, headers, sni, method 等字段

    Returns:
        - doh_count: DoH 请求数量
        - dot_count: DoT 请求数量
        - doh_flows: DoH 流量详情列表
        - dot_flows: DoT 流量详情列表
        - doh_providers: 各 DoH 提供商的数量统计
        - total_checked: 检测的流量总数
    """
    # 空列表快速返回
    if not body.flows:
        return ok({
            "doh_count": 0,
            "dot_count": 0,
            "doh_flows": [],
            "dot_flows": [],
            "doh_providers": {},
            "total_checked": 0,
        })

    # 限制单次检测的流量数量，防止资源耗尽
    MAX_FLOWS_PER_REQUEST = 10000
    if len(body.flows) > MAX_FLOWS_PER_REQUEST:
        return err(f"单次检测流量数量不能超过 {MAX_FLOWS_PER_REQUEST}", code=400)

    try:
        # 将 Pydantic 模型转换为字典（兼容 detect_doh_in_flows 的类型注解）
        flow_dicts = [flow.model_dump() for flow in body.flows]
        result = detect_doh_in_flows(flow_dicts)
        result["total_checked"] = len(body.flows)
        return ok(result)
    except Exception as e:
        _capture_log("error", "doh_detect", str(e))
        return err(f"DoH 检测失败: {str(e)}", code=500)


@router.post("/dns-hijack/doh-detect-single")
async def doh_detect_single(flow: FlowItem):
    """检测单个流量是否为 DoH/DoT 请求。

    Args:
        flow: 单个流量字典，包含 host, url, headers, sni, method 等字段

    Returns:
        - is_doh: 是否为 DoH 请求
        - is_dot: 是否为 DoT 请求
        - provider: DoH 提供商域名
        - reason: 检测依据
        - confidence: 置信度 (high/medium/low/none)
    """
    try:
        flow_dict = flow.model_dump()
        result = detect_doh_request(flow_dict)
        return ok(result)
    except Exception as e:
        _capture_log("error", "doh_detect_single", str(e))
        return err(f"DoH 单次检测失败: {str(e)}", code=500)

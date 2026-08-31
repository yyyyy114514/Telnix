"""请求/响应延迟规则管理。

延迟规则存储在 settings.json 的 ``delay_rules`` 键下（列表），按 host/url/phase
匹配后由代理层注入延迟。match_mode 支持 wildcard / regex / exact 三种匹配方式。

增强功能：
- 从 flow 一键生成延迟规则
- 延迟命中日志
- 延迟分布热力图
- 梯度延迟配置（jitter）
"""

import fnmatch
import heapq
import json
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db, settings_store
from ..logger import _capture_log
from . import err, ok

router = APIRouter()

_DELAY_KEY = "delay_rules"
_HITS_KEY = "delay_rule_hits"
_JITTER_KEY = "delay_jitter_config"

_VALID_MATCH_MODES = {"wildcard", "regex", "exact"}
_VALID_PHASES = {"request", "response"}


class DelayRule(BaseModel):
    """延迟规则数据模型。"""

    id: str | None = None
    enabled: bool = True
    pattern: str = ""
    match_mode: str = "wildcard"  # wildcard | regex | exact
    phase: str = "request"  # request | response
    delay_ms: int = 0
    host: str = ""
    note: str = ""


# ---------- 内部工具 ----------

def _get_rules() -> list[dict]:
    """从 settings.json 读取延迟规则列表。"""
    data = settings_store.get_setting(_DELAY_KEY, [])
    if isinstance(data, list):
        return data
    return []


def _save_rules(rules: list[dict]) -> None:
    """写回延迟规则列表到 settings.json。"""
    settings_store.set_setting(_DELAY_KEY, rules)


def _normalize(rule: DelayRule, rule_id: str | None = None) -> dict:
    """把 DelayRule 规范化为可存储的 dict（校验枚举字段、补默认值）。"""
    data = rule.model_dump(exclude_none=False)
    if rule_id is not None:
        data["id"] = rule_id
    elif not data.get("id"):
        data["id"] = uuid.uuid4().hex[:8]
    if data.get("match_mode") not in _VALID_MATCH_MODES:
        data["match_mode"] = "wildcard"
    if data.get("phase") not in _VALID_PHASES:
        data["phase"] = "request"
    try:
        data["delay_ms"] = int(data.get("delay_ms") or 0)
    except (TypeError, ValueError):
        data["delay_ms"] = 0
    if data["delay_ms"] < 0:
        data["delay_ms"] = 0
    data.setdefault("enabled", True)
    data.setdefault("host", "")
    data.setdefault("note", "")
    data.setdefault("pattern", "")
    return data


def _match_url(url: str, pattern: str, mode: str) -> bool:
    """按 match_mode 匹配 url 与 pattern。

    wildcard: fnmatch（支持 * ? []）；regex: re.search；exact: 完全相等。

    性能优化：regex 模式预编译缓存。
    """
    if not pattern:
        return False
    mode = (mode or "wildcard").lower()
    if mode == "regex":
        # 预编译缓存
        if not hasattr(_match_url, "_rx_cache"):
            _match_url._rx_cache = {}
        cache = _match_url._rx_cache
        compiled = cache.get(pattern)
        if compiled is None:
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
            except re.error:
                cache[pattern] = False
                return False
            cache[pattern] = compiled
        if compiled is False:
            return False
        return compiled.search(url or "") is not None
    if mode == "exact":
        return (url or "") == pattern
    # wildcard（默认）
    return fnmatch.fnmatch(url or "", pattern)


# ---------- 供代理层调用 ----------

def check_delay(host: str, url: str, phase: str) -> int:
    """返回第一个匹配的启用规则的延迟毫秒数；无匹配返回 0。

    匹配条件（全部满足）：
    1. 规则启用；
    2. 规则 phase 与入参 phase 一致；
    3. 规则 host 为空（匹配所有）或与入参 host 做 fnmatch（大小写不敏感）；
    4. 规则 pattern 按 match_mode 匹配 url。

    供代理 server.py 在请求/响应阶段调用以决定注入多少延迟。
    """
    rules = _get_rules()
    for r in rules:
        if not r.get("enabled"):
            continue
        if (r.get("phase") or "request") != phase:
            continue
        rule_host = r.get("host") or ""
        if rule_host and not fnmatch.fnmatch((host or "").lower(), rule_host.lower()):
            continue
        if _match_url(url or "", r.get("pattern") or "", r.get("match_mode")):
            try:
                return int(r.get("delay_ms") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


# ---------- REST 接口 ----------

@router.get("/delay-rules")
async def list_delay_rules():
    """返回全部延迟规则列表。"""
    return ok(_get_rules())


@router.post("/delay-rules")
async def create_delay_rule(rule: DelayRule):
    """新增延迟规则（自动生成 uuid id）。"""
    data = _normalize(rule)
    rules = _get_rules()
    rules.append(data)
    _save_rules(rules)
    return ok(data)


@router.put("/delay-rules/{rule_id}")
async def update_delay_rule(rule_id: str, rule: DelayRule):
    """更新指定延迟规则。"""
    rules = _get_rules()
    for i, r in enumerate(rules):
        if r.get("id") == rule_id:
            data = _normalize(rule, rule_id=rule_id)
            rules[i] = data
            _save_rules(rules)
            return ok(data)
    return err("Delay rule not found")


@router.delete("/delay-rules/{rule_id}")
async def delete_delay_rule(rule_id: str):
    """删除指定延迟规则。"""
    rules = _get_rules()
    new_rules = [r for r in rules if r.get("id") != rule_id]
    if len(new_rules) == len(rules):
        return err("Delay rule not found")
    _save_rules(new_rules)
    return ok({"id": rule_id})


@router.post("/delay-rules/{rule_id}/toggle")
async def toggle_delay_rule(rule_id: str):
    """切换延迟规则的 enabled 状态。"""
    rules = _get_rules()
    for r in rules:
        if r.get("id") == rule_id:
            r["enabled"] = not r.get("enabled", True)
            _save_rules(rules)
            return ok(r)
    return err("Delay rule not found")


# ---------- 从 Flow 生成延迟规则 ----------

class CreateFromFlowRequest(BaseModel):
    """从 flow 生成延迟规则的请求。"""
    flow_id: int
    phase: str = "response"  # request | response
    delay_ms: int = 500
    jitter_enabled: bool = False
    jitter_base: int = 300
    jitter_variance: int = 50


def _parse_flow_url(flow: dict) -> tuple[str, str]:
    """从 flow 解析 host 和 path。"""
    url = flow.get("url", "") or ""
    host = flow.get("host", "") or ""

    # 尝试从 url 解析
    if not host:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            host = parsed.netloc or ""
        except Exception:  # noqa: BLE001
            pass

    path = ""
    if url:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            path = parsed.path or ""
            if parsed.query:
                path += "?" + parsed.query
        except Exception:  # noqa: BLE001
            pass

    return host, path


@router.post("/delay-rules/from-flow")
async def create_from_flow(body: CreateFromFlowRequest):
    """从 flow 一键生成延迟规则。"""
    # 获取 flow 数据
    flows = db.get_flows_by_ids([body.flow_id])
    flow = flows.get(body.flow_id)

    if not flow:
        return err("Flow not found")

    host, path = _parse_flow_url(flow)

    # 创建规则
    rule = {
        "id": uuid.uuid4().hex[:8],
        "enabled": True,
        "pattern": path or "/",
        "match_mode": "prefix",
        "phase": body.phase,
        "delay_ms": body.delay_ms,
        "host": host,
        "note": f"From flow #{body.flow_id}",
        "jitter_enabled": body.jitter_enabled,
        "jitter_base": body.jitter_base,
        "jitter_variance": body.jitter_variance,
    }

    rules = _get_rules()
    rules.append(rule)
    _save_rules(rules)
    return ok(rule)


# ---------- 延迟命中日志 ----------

def _get_hits() -> list[dict]:
    """获取延迟命中日志（最多 1000 条）。"""
    data = settings_store.get_setting(_HITS_KEY, [])
    if isinstance(data, list):
        return data[-1000:]
    return []


def _add_hit(hit: dict) -> None:
    """添加一条命中日志。"""
    hits = _get_hits()
    hits.append(hit)
    # 只保留最近 1000 条
    if len(hits) > 1000:
        hits = hits[-1000:]
    settings_store.set_setting(_HITS_KEY, hits)


def _record_hit(rule_id: str, rule_pattern: str, flow_id: int, url: str, matched_delay_ms: int) -> None:
    """记录延迟命中（供代理层调用）。"""
    hit = {
        "id": int(time.time() * 1000),
        "timestamp": datetime.now().isoformat(),
        "rule_id": rule_id,
        "rule_pattern": rule_pattern,
        "flow_id": flow_id,
        "url": url,
        "matched_delay_ms": matched_delay_ms,
    }
    _add_hit(hit)


@router.get("/delay-rules/hits")
async def list_delay_hits(limit: int | None = None):
    """获取延迟命中日志（可选 limit，返回最新 N 条）。"""
    hits = _get_hits()
    if limit is not None and limit > 0:
        hits = hits[:limit]
    return ok(hits)


@router.delete("/delay-rules/hits")
async def clear_delay_hits():
    """清空延迟命中日志。"""
    settings_store.set_setting(_HITS_KEY, [])
    return ok({"cleared": True})


# ---------- 延迟分布热力图 ----------

@router.get("/delay-rules/heatmap")
async def get_delay_heatmap(bucket_seconds: int = 3600, top_n: int = 20):
    """获取延迟分布热力图数据（bucket_seconds 支持自定义分桶粒度，top_n 限制 host 数）。"""
    hits = _get_hits()

    # 按时间桶和 host 聚合
    time_buckets: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    host_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0, "delays": []})

    for hit in hits:
        timestamp = hit.get("timestamp", "")
        host = hit.get("url", "")
        # 提取 host
        try:
            from urllib.parse import urlparse
            parsed = urlparse(host)
            host = parsed.netloc or host
        except Exception:  # noqa: BLE001
            pass

        delay = hit.get("matched_delay_ms", 0)

        # 按指定粒度分桶（默认 1 小时）
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(timestamp)
            bucket_ts = int(dt.timestamp()) // max(1, bucket_seconds) * max(1, bucket_seconds)
            time_bucket = datetime.fromtimestamp(bucket_ts).isoformat()
        except Exception:  # noqa: BLE001
            time_bucket = timestamp[:13] if len(timestamp) >= 13 else timestamp[:10]

        time_buckets[time_bucket][host].append(delay)

        # host 统计
        host_stats[host]["total"] += 1
        host_stats[host]["delays"].append(delay)

    # 构建热力图数据
    buckets = []
    for time_key, hosts in sorted(time_buckets.items()):
        for host, delays in hosts.items():
            avg_delay = sum(delays) / len(delays) if delays else 0
            buckets.append({
                "time": time_key,
                "host": host,
                "avg_delay": round(avg_delay, 2),
                "count": len(delays),
            })

    # host 分布（使用 heapq.nlargest 优化 Top N）
    top_hosts = heapq.nlargest(max(1, top_n), host_stats.items(), key=lambda x: x[1]["total"])
    host_distribution = []
    for host, stats in top_hosts:
        delays = stats["delays"]
        avg_delay = sum(delays) / len(delays) if delays else 0
        host_distribution.append({
            "host": host,
            "total_count": stats["total"],
            "avg_delay": round(avg_delay, 2),
        })

    return ok({
        "buckets": buckets,
        "host_distribution": host_distribution,
    })


# ---------- 梯度延迟配置（jitter）----------

class JitterConfig(BaseModel):
    """梯度延迟配置。"""
    enabled: bool = False
    base_ms: int = 300
    variance_ms: int = 50


@router.get("/delay-rules/jitter-config")
async def get_jitter_config():
    """获取全局梯度延迟配置。"""
    config = settings_store.get_setting(_JITTER_KEY, {"enabled": False, "base_ms": 300, "variance_ms": 50})
    return ok(config)


@router.post("/delay-rules/jitter-config")
async def set_jitter_config(config: JitterConfig):
    """设置全局梯度延迟配置。"""
    settings_store.set_setting(_JITTER_KEY, config.model_dump())
    return ok(config)


def calculate_jitter(base_ms: int, variance_ms: int) -> int:
    """计算带抖动的实际延迟（用于代理层调用）。"""
    import random
    # 均匀分布：base ± variance
    return base_ms + random.randint(-variance_ms, variance_ms)


def check_delay_with_jitter(host: str, url: str, phase: str) -> tuple[int, int]:
    """返回 (delay_ms, actual_delay_ms)，actual_delay_ms 在 base ± variance 范围内。

    供代理层调用以注入带抖动的延迟。
    """
    delay_ms = check_delay(host, url, phase)
    if delay_ms <= 0:
        return 0, 0

    # 检查全局 jitter 配置
    jitter_config = settings_store.get_setting(_JITTER_KEY, {"enabled": False, "base_ms": 300, "variance_ms": 50})
    if not jitter_config.get("enabled", False):
        return delay_ms, delay_ms

    base = jitter_config.get("base_ms", delay_ms)
    variance = jitter_config.get("variance_ms", 50)
    actual = calculate_jitter(base, variance)

    return delay_ms, max(0, actual)


# 在模块级别添加类型提示所需导入
from typing import Any

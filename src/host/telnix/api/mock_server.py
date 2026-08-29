"""Mock Server 管理 API。

规则存储在 settings.json 的 ``mock_rules`` 键下（列表）。提供规则 CRUD、
服务器启停、状态查询、请求日志查询，以及从 flow 导入规则。
"""

import json as _json
import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db, mock_server as _mock, settings_store
from ..logger import _capture_log
from . import err, ok

router = APIRouter()

_MOCK_RULES_KEY = "mock_rules"
_VALID_MATCH_MODES = {"exact", "prefix", "regex"}


class MockRule(BaseModel):
    """Mock 规则数据模型。"""

    id: str | None = None
    enabled: bool = True
    method: str = "GET"
    path: str = "/"
    match_mode: str = "exact"  # exact | prefix | regex
    status_code: int = 200
    headers: dict = {}
    body: str = ""
    content_type: str = "application/json"
    delay_ms: int = 0
    note: str = ""


class StartReq(BaseModel):
    """启动服务器请求。"""

    port: int | None = None


class ImportFlowReq(BaseModel):
    """从 flow 导入规则请求。"""

    flow_id: int


# ---------- 内部工具 ----------

def _get_rules() -> list[dict]:
    """从 settings.json 读取 mock 规则列表。"""
    data = settings_store.get_setting(_MOCK_RULES_KEY, [])
    if isinstance(data, list):
        return data
    return []


def _save_rules(rules: list[dict]) -> None:
    """写回 mock 规则列表到 settings.json。"""
    settings_store.set_setting(_MOCK_RULES_KEY, rules)


def _normalize(rule: MockRule, rule_id: str | None = None) -> dict:
    """把 MockRule 规范化为可存储的 dict（校验枚举字段、补默认值）。"""
    data = rule.model_dump(exclude_none=False)
    if rule_id is not None:
        data["id"] = rule_id
    elif not data.get("id"):
        data["id"] = uuid.uuid4().hex[:8]
    if data.get("match_mode") not in _VALID_MATCH_MODES:
        data["match_mode"] = "exact"
    try:
        data["status_code"] = int(data.get("status_code") or 200)
    except (TypeError, ValueError):
        data["status_code"] = 200
    try:
        data["delay_ms"] = int(data.get("delay_ms") or 0)
    except (TypeError, ValueError):
        data["delay_ms"] = 0
    if data["delay_ms"] < 0:
        data["delay_ms"] = 0
    data["method"] = (data.get("method") or "GET").upper()
    data.setdefault("path", "/")
    data.setdefault("headers", {})
    data.setdefault("body", "")
    data.setdefault("content_type", "application/json")
    data.setdefault("note", "")
    data.setdefault("enabled", True)
    return data


def _extract_content_type(headers_str: str) -> str:
    """从 flow 的 response_headers（JSON 字符串）中提取 Content-Type。"""
    try:
        obj = _json.loads(headers_str or "{}")
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in mock_server.py", extra={"exc": repr(e)})
        return "application/json"
    for k, v in obj.items():
        if k.lower() == "content-type":
            return str(v)
    return "application/json"


# ---------- 规则 CRUD ----------

@router.get("/mock/rules")
async def list_rules():
    """获取规则列表。"""
    return ok(_get_rules())


@router.post("/mock/rules")
async def create_rule(rule: MockRule):
    """新增规则。"""
    data = _normalize(rule)
    rules = _get_rules()
    rules.append(data)
    _save_rules(rules)
    return ok(data)


@router.put("/mock/rules/{rule_id}")
async def update_rule(rule_id: str, rule: MockRule):
    """更新指定规则。"""
    rules = _get_rules()
    for i, r in enumerate(rules):
        if r.get("id") == rule_id:
            data = _normalize(rule, rule_id=rule_id)
            rules[i] = data
            _save_rules(rules)
            return ok(data)
    return err("Mock rule not found")


@router.delete("/mock/rules/{rule_id}")
async def delete_rule(rule_id: str):
    """删除指定规则。"""
    rules = _get_rules()
    new_rules = [r for r in rules if r.get("id") != rule_id]
    if len(new_rules) == len(rules):
        return err("Mock rule not found")
    _save_rules(new_rules)
    return ok({"id": rule_id})


@router.post("/mock/rules/{rule_id}/toggle")
async def toggle_rule(rule_id: str):
    """切换规则启用状态。"""
    rules = _get_rules()
    for r in rules:
        if r.get("id") == rule_id:
            r["enabled"] = not r.get("enabled", True)
            _save_rules(rules)
            return ok(r)
    return err("Mock rule not found")


# ---------- 服务器状态与控制 ----------

@router.get("/mock/status")
async def status():
    """获取服务器状态（running, port, request_count）。"""
    srv = _mock.get_mock_server()
    return ok({
        "running": srv.is_running(),
        "port": srv.port,
        "request_count": srv.get_request_count(),
    })


@router.post("/mock/start")
async def start_server(body: StartReq | None = None):
    """启动服务器（body: {port?}）。"""
    srv = _mock.get_mock_server()
    port = body.port if body and body.port else None
    if port is not None and port != srv.port:
        # 切换端口：先停止再启动
        if srv.is_running():
            srv.stop()
        srv.port = port
    if not srv.is_running():
        srv.start()
    return ok({
        "running": srv.is_running(),
        "port": srv.port,
        "request_count": srv.get_request_count(),
    })


@router.post("/mock/stop")
async def stop_server():
    """停止服务器。"""
    srv = _mock.get_mock_server()
    srv.stop()
    return ok({
        "running": srv.is_running(),
        "port": srv.port,
        "request_count": srv.get_request_count(),
    })


# ---------- 请求日志 ----------

@router.get("/mock/logs")
async def get_logs():
    """获取最近请求日志。"""
    return ok(_mock.get_mock_logs())


@router.delete("/mock/logs")
async def clear_logs():
    """清空日志。"""
    _mock.clear_mock_logs()
    return ok({"cleared": True})


# ---------- 从 flow 导入 ----------

@router.post("/mock/import-flow")
async def import_from_flow(body: ImportFlowReq):
    """从 flow 导入规则：以 flow 的响应作为 mock 规则。"""
    flow = db.get_flow(body.flow_id)
    if not flow:
        return err("Flow not found")
    # 解析 response_headers 提取 Content-Type
    content_type = _extract_content_type(flow.get("response_headers") or "")
    rule = MockRule(
        id=uuid.uuid4().hex[:8],
        enabled=True,
        method=(flow.get("method") or "GET").upper(),
        path=flow.get("path") or "/",
        match_mode="exact",
        status_code=int(flow.get("status_code") or 200),
        headers={},
        body=flow.get("response_body") or "",
        content_type=content_type,
        delay_ms=0,
        note=f"imported from flow #{body.flow_id}",
    )
    data = _normalize(rule)
    rules = _get_rules()
    rules.append(data)
    _save_rules(rules)
    return ok(data)

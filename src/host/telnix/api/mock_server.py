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


# ---------- 动态响应模板 ----------

_MOCK_TEMPLATES_KEY = "mock_templates"


class TemplateVariableModel(BaseModel):
    """模板变量定义。"""

    name: str
    description: str = ""
    type: str = "string"  # string | number | boolean | json | regex
    default_value: str = ""
    extraction_pattern: str = ""
    from_flow_field: str = ""


class MockTemplateModel(BaseModel):
    """动态响应模板。"""

    id: str | None = None
    name: str
    description: str = ""
    variables: list[TemplateVariableModel] = []
    body_template: str = ""
    headers_template: str = ""
    status_code: int = 200
    content_type: str = "application/json"
    delay_ms: int = 0


class PreviewReq(BaseModel):
    """模板预览请求。"""

    variables: dict[str, str] = {}


def _get_templates() -> list[dict]:
    data = settings_store.get_setting(_MOCK_TEMPLATES_KEY, [])
    return data if isinstance(data, list) else []


def _save_templates(templates: list[dict]) -> None:
    settings_store.set_setting(_MOCK_TEMPLATES_KEY, templates)


def _template_to_dict(tpl: MockTemplateModel, tpl_id: str | None = None) -> dict:
    data = tpl.model_dump()
    data["id"] = tpl_id or data.get("id") or uuid.uuid4().hex[:8]
    return data


@router.get("/mock/templates")
async def list_templates():
    """获取动态响应模板列表。"""
    return ok(_get_templates())


@router.post("/mock/templates")
async def create_template(tpl: MockTemplateModel):
    """新增动态响应模板。"""
    data = _template_to_dict(tpl)
    templates = _get_templates()
    templates.append(data)
    _save_templates(templates)
    return ok(data)


@router.put("/mock/templates/{tpl_id}")
async def update_template(tpl_id: str, tpl: MockTemplateModel):
    """更新指定模板。"""
    templates = _get_templates()
    for i, t in enumerate(templates):
        if t.get("id") == tpl_id:
            data = _template_to_dict(tpl, tpl_id=tpl_id)
            templates[i] = data
            _save_templates(templates)
            return ok(data)
    return err("Mock template not found")


@router.delete("/mock/templates/{tpl_id}")
async def delete_template(tpl_id: str):
    """删除指定模板。"""
    templates = _get_templates()
    new_templates = [t for t in templates if t.get("id") != tpl_id]
    if len(new_templates) == len(templates):
        return err("Mock template not found")
    _save_templates(new_templates)
    return ok({"id": tpl_id})


@router.post("/mock/templates/{tpl_id}/preview")
async def preview_template(tpl_id: str, body: PreviewReq):
    """用给定变量渲染模板，预览最终响应。"""
    templates = _get_templates()
    tpl = next((t for t in templates if t.get("id") == tpl_id), None)
    if tpl is None:
        return err("Mock template not found")

    # 变量值：请求提供 > 模板默认值
    variables: dict[str, str] = {}
    for v in tpl.get("variables") or []:
        if v.get("name"):
            variables[v["name"]] = v.get("default_value") or ""

    rendered_body, missing_body = _mock.render_template(
        tpl.get("body_template") or "", {**variables, **(body.variables or {})}
    )
    rendered_headers, missing_headers = _mock.render_template(
        tpl.get("headers_template") or "", {**variables, **(body.variables or {})}
    )

    # headers_template 按行解析为 dict
    rendered_headers_dict: dict[str, str] = {}
    for line in rendered_headers.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        rendered_headers_dict[k.strip()] = v.strip()

    errors = [f"undefined variable: {name}" for name in missing_body + missing_headers]
    return ok({
        "rendered_body": rendered_body,
        "rendered_headers": rendered_headers_dict,
        "errors": errors,
        "status_code": tpl.get("status_code") or 200,
        "content_type": tpl.get("content_type") or "application/json",
        "delay_ms": tpl.get("delay_ms") or 0,
    })


@router.get("/mock/templates/extract-variables/{flow_id}")
async def extract_template_variables(flow_id: int):
    """从流量中提取可用作模板变量的字段。"""
    flow = db.get_flow(flow_id)
    if not flow:
        return err("Flow not found")

    variables: list[dict] = [
        {"name": "method", "description": "HTTP method", "type": "string",
         "default_value": flow.get("method") or "", "extraction_pattern": "", "from_flow_field": "method"},
        {"name": "path", "description": "Request path", "type": "string",
         "default_value": flow.get("path") or "", "extraction_pattern": "", "from_flow_field": "path"},
        {"name": "host", "description": "Request host", "type": "string",
         "default_value": flow.get("host") or "", "extraction_pattern": "", "from_flow_field": "host"},
        {"name": "status_code", "description": "Response status code", "type": "number",
         "default_value": str(flow.get("status_code") or ""), "extraction_pattern": "", "from_flow_field": "status_code"},
    ]

    # 响应体为 JSON 时提取顶层字段
    try:
        obj = _json.loads(flow.get("response_body") or "")
        if isinstance(obj, dict):
            for k, v in list(obj.items())[:20]:
                variables.append({
                    "name": f"response_{k}", "description": f"Response JSON field: {k}",
                    "type": "string", "default_value": str(v)[:200],
                    "extraction_pattern": "", "from_flow_field": f"response_body.{k}",
                })
    except Exception:  # noqa: BLE001
        pass

    return ok({"variables": variables})


# ---------- 多条件匹配规则 ----------

class MockMatchConditionModel(BaseModel):
    """匹配条件。"""

    field: str = "method"  # method | path | host | header | body | query | status
    operator: str = "equals"  # equals | contains | startsWith | endsWith | regex | exists | notExists
    value: str = ""
    header_name: str = ""


class MockResponseModel(BaseModel):
    """命中后的响应模板。"""

    status_code: int = 200
    headers: dict[str, str] = {}
    body: str = ""
    delay_ms: int = 0
    template_mode: bool = False


class MockMultiMatchRuleModel(BaseModel):
    """多条件匹配规则。"""

    id: str | None = None
    enabled: bool = True
    name: str
    conditions: list[MockMatchConditionModel] = []
    mock_response: MockResponseModel = MockResponseModel()
    priority: int = 0
    note: str = ""


def _multi_rule_to_dict(rule: MockMultiMatchRuleModel, rule_id: str | None = None) -> dict:
    data = rule.model_dump()
    data["id"] = rule_id or data.get("id") or uuid.uuid4().hex[:8]
    return data


@router.get("/mock/multi-match-rules")
async def list_multi_match_rules():
    """获取多条件匹配规则列表。"""
    return ok(_mock.get_multi_match_rules())


@router.post("/mock/multi-match-rules")
async def create_multi_match_rule(rule: MockMultiMatchRuleModel):
    """新增多条件匹配规则。"""
    data = _multi_rule_to_dict(rule)
    rules = _mock.get_multi_match_rules()
    rules.append(data)
    _mock.save_multi_match_rules(rules)
    return ok(data)


@router.put("/mock/multi-match-rules/{rule_id}")
async def update_multi_match_rule(rule_id: str, rule: MockMultiMatchRuleModel):
    """更新指定多条件匹配规则。"""
    rules = _mock.get_multi_match_rules()
    for i, r in enumerate(rules):
        if r.get("id") == rule_id:
            data = _multi_rule_to_dict(rule, rule_id=rule_id)
            rules[i] = data
            _mock.save_multi_match_rules(rules)
            return ok(data)
    return err("Multi-match rule not found")


@router.delete("/mock/multi-match-rules/{rule_id}")
async def delete_multi_match_rule(rule_id: str):
    """删除指定多条件匹配规则。"""
    rules = _mock.get_multi_match_rules()
    new_rules = [r for r in rules if r.get("id") != rule_id]
    if len(new_rules) == len(rules):
        return err("Multi-match rule not found")
    _mock.save_multi_match_rules(new_rules)
    return ok({"id": rule_id})


@router.post("/mock/multi-match-rules/{rule_id}/toggle")
async def toggle_multi_match_rule(rule_id: str):
    """切换多条件匹配规则启用状态。"""
    rules = _mock.get_multi_match_rules()
    for r in rules:
        if r.get("id") == rule_id:
            r["enabled"] = not r.get("enabled", True)
            _mock.save_multi_match_rules(rules)
            return ok(r)
    return err("Multi-match rule not found")

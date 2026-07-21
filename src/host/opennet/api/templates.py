"""§3.15 规则模板库：内置常见规则模板，可一键应用到指定 match 表达式。

模板列表：
- mock-404: 返回 404 响应
- mock-500: 返回 500 响应
- strip-auth: 删除请求 Authorization 头
- unlock-vip: 改响应体 is_vip=true
- bypass-pay: 改响应体 price=0
- slow-response: 延迟 5 秒响应

GET /templates              返回模板列表
POST /templates/{name}/apply  应用模板到指定 match 表达式，创建规则
"""

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db
from ..auto_reply.rules import invalidate_cache
from . import err, ok

router = APIRouter()


# 内置模板定义
# action: mock | modify_request | modify_response
# 字段与 RuleCreate 对齐
TEMPLATES: dict[str, dict] = {
    "mock-404": {
        "name": "mock-404",
        "description": "返回 404 Not Found 响应（不转发到服务器）",
        "action": "mock",
        "mock_status": 404,
        "mock_headers": {"Content-Type": "application/json"},
        "mock_body": '{"error": "not found"}',
        "modify_rules": [],
    },
    "mock-500": {
        "name": "mock-500",
        "description": "返回 500 Internal Server Error 响应（不转发到服务器）",
        "action": "mock",
        "mock_status": 500,
        "mock_headers": {"Content-Type": "application/json"},
        "mock_body": '{"error": "internal server error"}',
        "modify_rules": [],
    },
    "strip-auth": {
        "name": "strip-auth",
        "description": "删除请求 Authorization 头（modify_request）",
        "action": "modify_request",
        "modify_rules": [
            {"target": "request_header", "op": "remove", "key": "Authorization", "value": ""}
        ],
    },
    "unlock-vip": {
        "name": "unlock-vip",
        "description": "改响应体 is_vip=true（modify_response，全局字段替换）",
        "action": "modify_response",
        "modify_rules": [
            {"target": "response_body", "op": "replace", "key": "is_vip", "value": True}
        ],
    },
    "bypass-pay": {
        "name": "bypass-pay",
        "description": "改响应体 price=0（modify_response，全局字段替换）",
        "action": "modify_response",
        "modify_rules": [
            {"target": "response_body", "op": "replace", "key": "price", "value": 0}
        ],
    },
    "slow-response": {
        "name": "slow-response",
        "description": "延迟 5 秒响应（modify_response，target=delay）",
        "action": "modify_response",
        "modify_rules": [
            {"target": "delay", "op": "sleep", "value": 5000}
        ],
    },
}


class ApplyTemplateBody(BaseModel):
    """应用模板到指定 match 表达式。"""
    pattern: str  # URL 匹配 pattern（wildcard 通配符）
    match_mode: str = "wildcard"  # wildcard | exact | regex
    note: str = ""  # 规则备注
    enabled: bool = True
    # 可选覆盖模板的过滤字段（§4.1）
    method_filter: str = ""
    status_filter: str = ""
    pid_filter: str = ""
    process_filter: str = ""


def _template_spec(name: str) -> dict:
    """返回模板的 action_spec（不含 name/description，便于前端展示）。"""
    t = TEMPLATES[name]
    return {
        "action": t["action"],
        "mock_status": t.get("mock_status"),
        "mock_headers": t.get("mock_headers", {}),
        "mock_body": t.get("mock_body", ""),
        "modify_rules": t.get("modify_rules", []),
    }


@router.get("/templates")
async def list_templates():
    """返回所有模板列表（name/description/action_spec）。"""
    items = []
    for name in TEMPLATES:
        t = TEMPLATES[name]
        items.append({
            "name": t["name"],
            "description": t["description"],
            "action_spec": _template_spec(name),
        })
    return ok(items)


@router.post("/templates/{name}/apply")
async def apply_template(name: str, body: ApplyTemplateBody):
    """应用模板到指定 match 表达式，创建规则。"""
    if name not in TEMPLATES:
        return err(f"模板不存在: {name}")
    t = TEMPLATES[name]
    import json
    now = datetime.now().isoformat()
    rule = {
        "id": uuid.uuid4().hex,
        "enabled": 1 if body.enabled else 0,
        "match_mode": body.match_mode,
        "pattern": body.pattern,
        "action": t["action"],
        "mock_status": t.get("mock_status") if t.get("mock_status") is not None else 200,
        "mock_headers": json.dumps(t.get("mock_headers", {})),
        "mock_body": t.get("mock_body", ""),
        "modify_rules": json.dumps(t.get("modify_rules", [])),
        "note": body.note or f"模板: {name}",
        # §4.1 过滤字段
        "method_filter": body.method_filter or "",
        "status_filter": body.status_filter or "",
        "pid_filter": body.pid_filter or "",
        "process_filter": body.process_filter or "",
        # §3.2 命中统计字段初始值
        "hit_count": 0,
        "last_hit_at": "",
        "last_hit_flow_id": None,
        "created_at": now,
        "updated_at": now,
    }
    db.insert_rule(rule)
    invalidate_cache()
    return ok({
        "created": True,
        "rule_id": rule["id"],
        "template": name,
        "pattern": body.pattern,
    })

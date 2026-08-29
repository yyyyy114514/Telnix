"""§3.15 Rule template library: built-in common rule templates, can be applied to specified match expression with one click.

Template list:
- mock-404: return 404 response
- mock-500: return 500 response
- strip-auth: remove request Authorization header
- unlock-vip: modify response body is_vip=true
- bypass-pay: modify response body price=0
- slow-response: delay 5 seconds response

GET /templates              Return template list
POST /templates/{name}/apply  Apply template to specified match expression, create rule
"""

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db
from ..logger import _capture_log
from ..auto_reply.rules import invalidate_cache
from . import err, ok

router = APIRouter()


# 内置模板定义
# action: mock | modify_request | modify_response
# 字段与 RuleCreate 对齐
TEMPLATES: dict[str, dict] = {
    "mock-404": {
        "name": "mock-404",
        "description": "Return 404 Not Found response (does not forward to server)",
        "action": "mock",
        "mock_status": 404,
        "mock_headers": {"Content-Type": "application/json"},
        "mock_body": '{"error": "not found"}',
        "modify_rules": [],
    },
    "mock-500": {
        "name": "mock-500",
        "description": "Return 500 Internal Server Error response (does not forward to server)",
        "action": "mock",
        "mock_status": 500,
        "mock_headers": {"Content-Type": "application/json"},
        "mock_body": '{"error": "internal server error"}',
        "modify_rules": [],
    },
    "strip-auth": {
        "name": "strip-auth",
        "description": "Remove request Authorization header (modify_request)",
        "action": "modify_request",
        "modify_rules": [
            {"target": "request_header", "op": "remove", "key": "Authorization", "value": ""}
        ],
    },
    "unlock-vip": {
        "name": "unlock-vip",
        "description": "Modify response body is_vip=true (modify_response, global field replacement)",
        "action": "modify_response",
        "modify_rules": [
            {"target": "response_body", "op": "replace", "key": "is_vip", "value": True}
        ],
    },
    "bypass-pay": {
        "name": "bypass-pay",
        "description": "Modify response body price=0 (modify_response, global field replacement)",
        "action": "modify_response",
        "modify_rules": [
            {"target": "response_body", "op": "replace", "key": "price", "value": 0}
        ],
    },
    "slow-response": {
        "name": "slow-response",
        "description": "Delay 5 seconds response (modify_response, target=delay)",
        "action": "modify_response",
        "modify_rules": [
            {"target": "delay", "op": "sleep", "value": 5000}
        ],
    },
}


class ApplyTemplateBody(BaseModel):
    """Apply template to specified match expression."""
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
    """Return the template's action_spec (excluding name/description, for frontend display)."""
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
    """Return all template list (name/description/action_spec)."""
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
    """Apply template to specified match expression, create rule."""
    if name not in TEMPLATES:
        return err(f"Template not found: {name}")
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
        "note": body.note or f"Template: {name}",
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

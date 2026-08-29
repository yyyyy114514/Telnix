"""§3.16 Environment snapshot: export/import current environment state.

Environment state includes:
- All auto-reply rules
- focus settings (focus mode)
- Breakpoint settings (request/response breakpoint toggle + timeout)

GET /snapshot  Returns current environment state
POST /snapshot  Import environment state (clear existing rules then import new + set focus + breakpoint)
"""

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import db
from ..logger import _capture_log
from ..auto_reply.rules import invalidate_cache
from . import err, ok

router = APIRouter()


def _serialize_rule(rule: dict) -> dict:
    """Serialize rule for snapshot export (deserialize mock_headers/modify_rules into objects)."""
    out = dict(rule)
    try:
        out["mock_headers"] = json.loads(out.get("mock_headers") or "{}")
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in snapshot.py", extra={"exc": repr(e)})
        out["mock_headers"] = {}
    try:
        out["modify_rules"] = json.loads(out.get("modify_rules") or "[]")
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in snapshot.py", extra={"exc": repr(e)})
        out["modify_rules"] = []
    return out


@router.get("/snapshot")
async def get_snapshot(request: Request):
    """Return current environment state (all rules + focus settings + breakpoint settings)."""
    rules = [_serialize_rule(r) for r in db.get_rules()]

    # focus 设置：从运行时代理对象获取
    focus_state = {"enabled": False, "pids": [], "hosts": [], "process_names": [],
                   "methods": [], "status_codes": [], "content_types": [],
                   "include_children": True}
    proxy = request.app.state.telnix.proxy
    if proxy is not None:
        focus_state = proxy.get_focus_mode()
        # get_focus_mode 不含 process_names，补一个空列表保持结构
        focus_state.setdefault("process_names", [])
        focus_state.setdefault("include_children", True)

    # 断点设置：从 db 读取
    breakpoint_state = {
        "break_on_request": db.get_setting("break_on_request", "0") == "1",
        "break_on_response": db.get_setting("break_on_response", "0") == "1",
        "breakpoint_timeout": float(db.get_setting("breakpoint_timeout", "0") or "0"),
    }

    return ok({
        "rules": rules,
        "focus": focus_state,
        "breakpoint": breakpoint_state,
        "exported_at": datetime.now().isoformat(),
    })


class SnapshotImport(BaseModel):
    """Environment snapshot import. All fields optional; if not provided, corresponding part is not modified."""
    rules: list[dict] | None = None  # Rule list (consistent with export format)
    focus: dict | None = None  # focus settings
    breakpoint: dict | None = None  # Breakpoint settings
    # Whether to clear existing rules (default True; False=append)
    clear_rules: bool = True


@router.post("/snapshot")
async def import_snapshot(body: SnapshotImport, request: Request):
    """Import environment state: clear existing rules then import new + set focus + breakpoint.

    Rule fields are consistent with GET /snapshot export format (mock_headers/modify_rules are objects).
    """
    result = {"rules_imported": 0, "rules_cleared": 0,
              "focus_set": False, "breakpoint_set": False}

    # 1. 规则导入
    if body.rules is not None:
        if body.clear_rules:
            result["rules_cleared"] = db.delete_all_rules()
        now = datetime.now().isoformat()
        imported = 0
        for r in body.rules:
            try:
                # 生成新 id（避免与已有规则冲突；如果原 id 不存在也可保留）
                rule_id = r.get("id") or uuid.uuid4().hex
                # 确保唯一性：若 id 已存在则生成新的
                if db.get_rule(rule_id):
                    rule_id = uuid.uuid4().hex
                rule = {
                    "id": rule_id,
                    "enabled": 1 if r.get("enabled", True) else 0,
                    "match_mode": r.get("match_mode", "wildcard"),
                    "pattern": r.get("pattern", ""),
                    "action": r.get("action", "mock"),
                    "mock_status": r.get("mock_status") if r.get("mock_status") is not None else 200,
                    "mock_headers": json.dumps(r.get("mock_headers") or {}),
                    "mock_body": r.get("mock_body", ""),
                    "modify_rules": json.dumps(r.get("modify_rules") or []),
                    "note": r.get("note", ""),
                    # §4.1 过滤字段
                    "method_filter": r.get("method_filter", ""),
                    "status_filter": r.get("status_filter", ""),
                    "pid_filter": r.get("pid_filter", ""),
                    "process_filter": r.get("process_filter", ""),
                    # §3.2 命中统计字段重置
                    "hit_count": 0,
                    "last_hit_at": "",
                    "last_hit_flow_id": None,
                    "created_at": r.get("created_at") or now,
                    "updated_at": now,
                }
                db.insert_rule(rule)
                imported += 1
            except Exception as e:  # noqa: BLE001
                _capture_log("error", "API exception in snapshot.py", extra={"exc": repr(e)})
                # 单条规则导入失败不影响其他规则
                continue
        result["rules_imported"] = imported
        invalidate_cache()

    # 2. focus 设置
    if body.focus is not None:
        proxy = request.app.state.telnix.proxy
        if proxy is not None:
            f = body.focus
            pids = list(f.get("pids", []))
            # 若有 process_names，需调用 focus API 的转换逻辑
            # 此处简化：直接传 pids + hosts + methods + status_codes + content_types
            # process_names 字段保留供前端使用，但不在此处解析
            proxy.set_focus_mode(
                enabled=f.get("enabled", False),
                pids=pids,
                hosts=list(f.get("hosts", [])),
                methods=list(f.get("methods", [])),
                status_codes=list(f.get("status_codes", [])),
                content_types=list(f.get("content_types", [])),
            )
            result["focus_set"] = True

    # 3. 断点设置
    if body.breakpoint is not None:
        proxy = request.app.state.telnix.proxy
        bp = body.breakpoint
        if proxy is not None:
            timeout = float(bp.get("breakpoint_timeout", 0) or 0)
            req_on = bool(bp.get("break_on_request", False))
            resp_on = bool(bp.get("break_on_response", False))
            proxy.breakpoint.set_request(req_on, timeout=timeout)
            proxy.breakpoint.set_response(resp_on, timeout=timeout)
            proxy.breakpoint.set_timeout(timeout)
            db.set_setting("break_on_request", "1" if req_on else "0")
            db.set_setting("break_on_response", "1" if resp_on else "0")
            db.set_setting("breakpoint_timeout", str(timeout))
            result["breakpoint_set"] = True

    return ok(result)

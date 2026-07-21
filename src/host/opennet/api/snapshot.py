"""§3.16 环境快照：导出/导入当前环境状态。

环境状态包含：
- 所有自动回复规则
- focus 设置（专注模式）
- 断点设置（请求/响应断点开关 + 超时）

GET /snapshot  返回当前环境状态
POST /snapshot  导入环境状态（清空现有规则后导入新的 + 设置 focus + 断点）
"""

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import db
from ..auto_reply.rules import invalidate_cache
from . import err, ok

router = APIRouter()


def _serialize_rule(rule: dict) -> dict:
    """序列化规则用于快照导出（mock_headers/modify_rules 反序列化为对象）。"""
    out = dict(rule)
    try:
        out["mock_headers"] = json.loads(out.get("mock_headers") or "{}")
    except Exception:  # noqa: BLE001
        out["mock_headers"] = {}
    try:
        out["modify_rules"] = json.loads(out.get("modify_rules") or "[]")
    except Exception:  # noqa: BLE001
        out["modify_rules"] = []
    return out


@router.get("/snapshot")
async def get_snapshot(request: Request):
    """返回当前环境状态（所有规则 + focus 设置 + 断点设置）。"""
    rules = [_serialize_rule(r) for r in db.get_rules()]

    # focus 设置：从运行时代理对象获取
    focus_state = {"enabled": False, "pids": [], "hosts": [], "process_names": [],
                   "methods": [], "status_codes": [], "content_types": [],
                   "include_children": True}
    proxy = request.app.state.opennet.proxy
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
    """环境快照导入。所有字段可选；未提供则不修改对应部分。"""
    rules: list[dict] | None = None  # 规则列表（与导出格式一致）
    focus: dict | None = None  # focus 设置
    breakpoint: dict | None = None  # 断点设置
    # 是否清空现有规则（默认 True；False=追加）
    clear_rules: bool = True


@router.post("/snapshot")
async def import_snapshot(body: SnapshotImport, request: Request):
    """导入环境状态：清空现有规则后导入新的 + 设置 focus + 断点。

    规则字段与 GET /snapshot 导出格式一致（mock_headers/modify_rules 为对象）。
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
            except Exception:  # noqa: BLE001
                # 单条规则导入失败不影响其他规则
                continue
        result["rules_imported"] = imported
        invalidate_cache()

    # 2. focus 设置
    if body.focus is not None:
        proxy = request.app.state.opennet.proxy
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
        proxy = request.app.state.opennet.proxy
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

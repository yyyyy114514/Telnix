"""自动回复规则 CRUD API。"""

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db
from ..auto_reply.rules import invalidate_cache
from . import err, ok

router = APIRouter()


class RuleCreate(BaseModel):
    enabled: bool = True
    match_mode: str = "wildcard"  # wildcard | exact | regex
    pattern: str
    action: str  # mock | mock_request | modify_request | modify_response | script
    mock_status: int | None = None
    mock_headers: dict | None = None
    mock_body: str = ""
    # modify_rules：modify_* 动作时为 list[dict]，script 动作时为 str（Python 脚本内容）
    modify_rules: list | str | None = None
    note: str = ""
    # §4.1 规则匹配过滤字段（空字符串=不过滤，非空=必须匹配；逗号分隔多个值）
    method_filter: str = ""
    status_filter: str = ""
    pid_filter: str = ""
    process_filter: str = ""
    # mock_request：写死请求（用预设请求转发到目标服务器，返回真实响应）
    mock_method: str = "GET"
    mock_url: str = ""


class RuleUpdate(BaseModel):
    enabled: bool | None = None
    match_mode: str | None = None
    pattern: str | None = None
    action: str | None = None
    mock_status: int | None = None
    mock_headers: dict | None = None
    mock_body: str | None = None
    # modify_rules：modify_* 动作时为 list[dict]，script 动作时为 str
    modify_rules: list | str | None = None
    note: str | None = None
    # §4.1 规则匹配过滤字段
    method_filter: str | None = None
    status_filter: str | None = None
    pid_filter: str | None = None
    process_filter: str | None = None
    # mock_request
    mock_method: str | None = None
    mock_url: str | None = None


class BatchUpdate(BaseModel):
    """批量更新规则。"""
    ids: list[str]
    enabled: bool | None = None


class BatchDelete(BaseModel):
    """批量删除规则。"""
    ids: list[str]


class BatchCreate(BaseModel):
    """§2.2 批量创建规则。"""
    rules: list[RuleCreate]


def _store_modify_rules(action: str, modify_rules) -> str:
    """序列化 modify_rules 字段为存储字符串。

    - script action：modify_rules 是 Python 脚本源码（str），原样返回
    - 其他 action：modify_rules 是 list[dict]，json.dumps
    """
    import json
    if action == "script":
        return modify_rules if isinstance(modify_rules, str) else ""
    return json.dumps(modify_rules or [])


def _serialize(rule: dict) -> dict:
    """把 mock_headers/modify_rules 反序列化为对象返回给前端。

    script action 时 modify_rules 返回原始字符串（Python 脚本内容）。
    """
    import json
    out = dict(rule)
    out["enabled"] = bool(out.get("enabled"))
    try:
        out["mock_headers"] = json.loads(out.get("mock_headers") or "{}")
    except Exception:  # noqa: BLE001
        out["mock_headers"] = {}
    # script action：modify_rules 存的是 Python 脚本源码（字符串），原样返回
    if out.get("action") == "script":
        out["modify_rules"] = out.get("modify_rules") or ""
    else:
        try:
            out["modify_rules"] = json.loads(out.get("modify_rules") or "[]")
        except Exception:  # noqa: BLE001
            out["modify_rules"] = []
    # §4.1 过滤字段默认空字符串
    out.setdefault("method_filter", "")
    out.setdefault("status_filter", "")
    out.setdefault("pid_filter", "")
    out.setdefault("process_filter", "")
    # §3.2 命中统计字段默认值
    out.setdefault("hit_count", 0)
    out.setdefault("last_hit_at", "")
    out.setdefault("last_hit_flow_id", None)
    # mock_request 字段默认值
    out.setdefault("mock_method", "GET")
    out.setdefault("mock_url", "")
    return out


@router.get("/auto-reply/rules")
async def list_rules():
    """规则列表。"""
    rules = db.get_rules()
    return ok([_serialize(r) for r in rules])


@router.get("/auto-reply/rules/export")
async def export_rules():
    """导出所有规则为 JSON（兼容 EzReply 导入格式）。

    返回纯 JSON（非 {code,data,msg} 包装），便于直接保存为文件。
    """
    import json
    from datetime import datetime
    rules = db.get_rules()
    data = {
        "version": 1,
        "exported_from": "telnix",
        "exported_at": datetime.now().isoformat(),
        "rules": [_serialize(r) for r in rules],
    }
    from fastapi.responses import Response
    content = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=telnix_rules.json"},
    )


@router.post("/auto-reply/rules/import")
async def import_rules(body: dict):
    """导入规则（兼容 EzReply / telnix 导出格式）。

    body: {"rules": [...], "mode": "merge"|"replace"|"append"}
    返回 {"imported": N}
    """
    rules = body.get("rules", [])
    mode = body.get("mode", "merge")
    if not isinstance(rules, list):
        return err("rules 必须是数组")
    imported = 0
    now = datetime.now().isoformat()
    if mode == "replace":
        db.delete_all_rules()
    existing_ids = {r["id"] for r in db.get_rules()}
    for r in rules:
        if not isinstance(r, dict):
            continue
        # 标准化字段
        rid = r.get("id") or uuid.uuid4().hex
        if mode == "append":
            rid = uuid.uuid4().hex
        if mode == "merge" and rid in existing_ids:
            # 更新已有
            db.update_rule(rid, {
                "enabled": 1 if r.get("enabled", True) else 0,
                "match_mode": r.get("match_mode", "wildcard"),
                "pattern": r.get("pattern", ""),
                "action": r.get("action", "mock"),
                "mock_status": r.get("mock_status", 200) or 200,
                "mock_headers": r.get("mock_headers", {}) or {},
                "mock_body": r.get("mock_body", "") or "",
                "modify_rules": r.get("modify_rules", []) or [],
                "note": r.get("note", "") or "",
                "method_filter": r.get("method_filter", "") or "",
                "status_filter": r.get("status_filter", "") or "",
                "pid_filter": r.get("pid_filter", "") or "",
                "process_filter": r.get("process_filter", "") or "",
                "mock_method": r.get("mock_method", "GET") or "GET",
                "mock_url": r.get("mock_url", "") or "",
                "updated_at": now,
            })
            imported += 1
        else:
            # 新增
            db.add_rule({
                "id": rid,
                "enabled": 1 if r.get("enabled", True) else 0,
                "match_mode": r.get("match_mode", "wildcard"),
                "pattern": r.get("pattern", ""),
                "action": r.get("action", "mock"),
                "mock_status": r.get("mock_status", 200) or 200,
                "mock_headers": r.get("mock_headers", {}) or {},
                "mock_body": r.get("mock_body", "") or "",
                "modify_rules": r.get("modify_rules", []) or [],
                "note": r.get("note", "") or "",
                "method_filter": r.get("method_filter", "") or "",
                "status_filter": r.get("status_filter", "") or "",
                "pid_filter": r.get("pid_filter", "") or "",
                "process_filter": r.get("process_filter", "") or "",
                "mock_method": r.get("mock_method", "GET") or "GET",
                "mock_url": r.get("mock_url", "") or "",
            })
            imported += 1
    invalidate_cache()
    return ok({"imported": imported, "mode": mode})


@router.post("/auto-reply/rules")
async def create_rule(body: RuleCreate):
    """创建规则。"""
    import json
    now = datetime.now().isoformat()
    rule = {
        "id": uuid.uuid4().hex,
        "enabled": 1 if body.enabled else 0,
        "match_mode": body.match_mode,
        "pattern": body.pattern,
        "action": body.action,
        "mock_status": body.mock_status if body.mock_status is not None else 200,
        "mock_headers": json.dumps(body.mock_headers or {}),
        "mock_body": body.mock_body,
        "modify_rules": _store_modify_rules(body.action, body.modify_rules),
        "note": body.note or "",
        # §4.1 过滤字段
        "method_filter": body.method_filter or "",
        "status_filter": body.status_filter or "",
        "pid_filter": body.pid_filter or "",
        "process_filter": body.process_filter or "",
        # §3.2 命中统计字段初始值
        "hit_count": 0,
        "last_hit_at": "",
        "last_hit_flow_id": None,
        # mock_request 字段
        "mock_method": body.mock_method or "GET",
        "mock_url": body.mock_url or "",
        "created_at": now,
        "updated_at": now,
    }
    db.insert_rule(rule)
    invalidate_cache()
    return ok(_serialize(db.get_rule(rule["id"])))


@router.post("/auto-reply/rules/batch-create")
async def batch_create_rules(body: BatchCreate):
    """§2.2 批量创建规则。返回 {created, errors}。"""
    if not body.rules:
        return err("rules 不能为空")
    created_ids: list[str] = []
    errors: list[dict] = []
    for i, rule_create in enumerate(body.rules):
        try:
            import json
            now = datetime.now().isoformat()
            rule = {
                "id": uuid.uuid4().hex,
                "enabled": 1 if rule_create.enabled else 0,
                "match_mode": rule_create.match_mode,
                "pattern": rule_create.pattern,
                "action": rule_create.action,
                "mock_status": rule_create.mock_status if rule_create.mock_status is not None else 200,
                "mock_headers": json.dumps(rule_create.mock_headers or {}),
                "mock_body": rule_create.mock_body,
                "modify_rules": _store_modify_rules(rule_create.action, rule_create.modify_rules),
                "note": rule_create.note or "",
                "method_filter": rule_create.method_filter or "",
                "status_filter": rule_create.status_filter or "",
                "pid_filter": rule_create.pid_filter or "",
                "process_filter": rule_create.process_filter or "",
                "mock_method": rule_create.mock_method or "GET",
                "mock_url": rule_create.mock_url or "",
                "hit_count": 0,
                "last_hit_at": "",
                "last_hit_flow_id": None,
                "created_at": now,
                "updated_at": now,
            }
            db.insert_rule(rule)
            created_ids.append(rule["id"])
        except Exception as e:  # noqa: BLE001
            errors.append({
                "index": i,
                "pattern": getattr(rule_create, "pattern", ""),
                "error": str(e),
            })
    if created_ids:
        invalidate_cache()
    return ok({"created": len(created_ids), "ids": created_ids, "errors": errors})


def _rule_signature(rule_create: RuleCreate) -> str:
    """计算规则签名（pattern+match_mode+action+modify_rules 的 JSON 序列化）。

    用于 --idempotent 比对：签名相同视为同一规则。
    注意：modify_rules 顺序敏感（[{a:1},{b:2}] 与 [{b:2},{a:1}] 签名不同）。
    """
    import json
    return json.dumps({
        "pattern": rule_create.pattern,
        "match_mode": rule_create.match_mode,
        "action": rule_create.action,
        "modify_rules": rule_create.modify_rules or [],
    }, sort_keys=True, ensure_ascii=False)


@router.post("/auto-reply/rules/idempotent")
async def create_rule_idempotent(body: RuleCreate):
    """§3.1 幂等创建规则：已存在相同签名（pattern+match_mode+action+modify_rules）的规则则返回现有 rule_id 不重复创建。

    比 CLI 客户端遍历更高效（单次请求 + 服务端比对）。
    返回 {created: bool, idempotent: bool, rule_id, rule?}。
    """
    import json
    new_sig = _rule_signature(body)
    existing_rules = db.get_rules()
    for r in existing_rules:
        try:
            existing_modify = json.loads(r.get("modify_rules") or "[]")
        except Exception:  # noqa: BLE001
            existing_modify = []
        existing_sig = json.dumps({
            "pattern": r.get("pattern"),
            "match_mode": r.get("match_mode"),
            "action": r.get("action"),
            "modify_rules": existing_modify,
        }, sort_keys=True, ensure_ascii=False)
        if existing_sig == new_sig:
            return ok({
                "created": False,
                "idempotent": True,
                "rule_id": r.get("id"),
                "rule": _serialize(r),
                "hint": "已存在相同规则，未重复创建",
            })
    # 未命中，创建新规则
    now = datetime.now().isoformat()
    rule = {
        "id": uuid.uuid4().hex,
        "enabled": 1 if body.enabled else 0,
        "match_mode": body.match_mode,
        "pattern": body.pattern,
        "action": body.action,
        "mock_status": body.mock_status if body.mock_status is not None else 200,
        "mock_headers": json.dumps(body.mock_headers or {}),
        "mock_body": body.mock_body,
        "modify_rules": _store_modify_rules(body.action, body.modify_rules),
        "note": body.note or "",
        "method_filter": body.method_filter or "",
        "status_filter": body.status_filter or "",
        "pid_filter": body.pid_filter or "",
        "process_filter": body.process_filter or "",
        "mock_method": body.mock_method or "GET",
        "mock_url": body.mock_url or "",
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
        "idempotent": False,
        "rule_id": rule["id"],
        "rule": _serialize(db.get_rule(rule["id"])),
    })


@router.put("/auto-reply/rules/{rule_id}")
async def update_rule(rule_id: str, body: RuleUpdate):
    """更新规则。"""
    import json
    rule = db.get_rule(rule_id)
    if not rule:
        return err("规则不存在")
    updates = {}
    if body.enabled is not None:
        updates["enabled"] = 1 if body.enabled else 0
    if body.match_mode is not None:
        updates["match_mode"] = body.match_mode
    if body.pattern is not None:
        updates["pattern"] = body.pattern
    if body.action is not None:
        updates["action"] = body.action
    if body.mock_status is not None:
        updates["mock_status"] = body.mock_status
    if body.mock_headers is not None:
        updates["mock_headers"] = json.dumps(body.mock_headers)
    if body.mock_body is not None:
        updates["mock_body"] = body.mock_body
    if body.modify_rules is not None:
        # script action 时 modify_rules 是 str（脚本源码），否则 list
        new_action = body.action if body.action is not None else rule.get("action", "")
        updates["modify_rules"] = _store_modify_rules(new_action, body.modify_rules)
    if body.note is not None:
        updates["note"] = body.note
    # §4.1 过滤字段
    if body.method_filter is not None:
        updates["method_filter"] = body.method_filter
    if body.status_filter is not None:
        updates["status_filter"] = body.status_filter
    if body.pid_filter is not None:
        updates["pid_filter"] = body.pid_filter
    if body.process_filter is not None:
        updates["process_filter"] = body.process_filter
    # mock_request 字段
    if body.mock_method is not None:
        updates["mock_method"] = body.mock_method
    if body.mock_url is not None:
        updates["mock_url"] = body.mock_url
    updates["updated_at"] = datetime.now().isoformat()
    db.update_rule(rule_id, updates)
    invalidate_cache()
    # script 规则：脚本内容变化时重启 worker 加载新脚本
    if "modify_rules" in updates or "action" in updates:
        try:
            from ..auto_reply.script_runner import reload_runner, remove_runner
            updated = db.get_rule(rule_id) or {}
            if updated.get("action") == "script":
                reload_runner(rule_id, updates["modify_rules"])
            else:
                # action 改成非 script，清理可能存在的 runner
                remove_runner(rule_id)
        except Exception:  # noqa: BLE001
            pass
    return ok(_serialize(db.get_rule(rule_id)))


@router.post("/auto-reply/rules/batch-update")
async def batch_update_rules(body: BatchUpdate):
    """批量更新规则（启用/禁用）。"""
    if not body.ids:
        return err("ids 不能为空")
    now = datetime.now().isoformat()
    for rid in body.ids:
        updates = {"updated_at": now}
        if body.enabled is not None:
            updates["enabled"] = 1 if body.enabled else 0
        db.update_rule(rid, updates)
        # 禁用 script 规则时停止 worker（释放资源）
        if body.enabled is False:
            try:
                from ..auto_reply.script_runner import remove_runner
                remove_runner(rid)
            except Exception:  # noqa: BLE001
                pass
    invalidate_cache()
    return ok({"updated": len(body.ids)})


@router.post("/auto-reply/rules/batch-delete")
async def batch_delete_rules(body: BatchDelete):
    """批量删除规则。"""
    if not body.ids:
        return err("ids 不能为空")
    for rid in body.ids:
        db.delete_rule(rid)
        try:
            from ..auto_reply.script_runner import remove_runner
            remove_runner(rid)
        except Exception:  # noqa: BLE001
            pass
    invalidate_cache()
    return ok({"deleted": len(body.ids)})


@router.delete("/auto-reply/rules/{rule_id}")
async def delete_rule(rule_id: str):
    """删除规则。"""
    db.delete_rule(rule_id)
    invalidate_cache()
    try:
        from ..auto_reply.script_runner import remove_runner
        remove_runner(rule_id)
    except Exception:  # noqa: BLE001
        pass
    return ok({"deleted": True})

"""Auto-reply rule CRUD API."""

import base64
import json as _json
import time
import traceback
import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db, logger
from ..logger import _capture_log
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
    """Batch update rules."""
    ids: list[str]
    enabled: bool | None = None


class BatchDelete(BaseModel):
    """Batch delete rules."""
    ids: list[str]


class BatchCreate(BaseModel):
    """§2.2 Batch create rules."""
    rules: list[RuleCreate]


def _store_modify_rules(action: str, modify_rules) -> str:
    """Serialize modify_rules field to storage string.

    - script action: modify_rules is Python script source code (str), return as-is
    - Other actions: modify_rules is list[dict], json.dumps
    """
    import json
    if action == "script":
        return modify_rules if isinstance(modify_rules, str) else ""
    return json.dumps(modify_rules or [])


def _serialize(rule: dict) -> dict:
    """Deserialize mock_headers/modify_rules into objects to return to frontend.

    For script action, modify_rules returns original string (Python script content).
    """
    import json
    out = dict(rule)
    out["enabled"] = bool(out.get("enabled"))
    try:
        out["mock_headers"] = json.loads(out.get("mock_headers") or "{}")
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in auto_reply.py", extra={"exc": repr(e)})
        out["mock_headers"] = {}
    # script action：modify_rules 存的是 Python 脚本源码（字符串），原样返回
    if out.get("action") == "script":
        out["modify_rules"] = out.get("modify_rules") or ""
    else:
        try:
            out["modify_rules"] = json.loads(out.get("modify_rules") or "[]")
        except Exception as e:  # noqa: BLE001
            _capture_log("error", "API exception in auto_reply.py", extra={"exc": repr(e)})
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
    """Rule list."""
    rules = db.get_rules()
    return ok([_serialize(r) for r in rules])


@router.get("/auto-reply/rules/export")
async def export_rules():
    """Export all rules as JSON (compatible with EzReply import format).

    Returns pure JSON (not wrapped in {code,data,msg}), convenient for direct saving to file.
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
    """Import rules (compatible with EzReply / telnix export format).

    body: {"rules": [...], "mode": "merge"|"replace"|"append"}
    Returns {"imported": N}
    """
    rules = body.get("rules", [])
    mode = body.get("mode", "merge")
    if mode not in ("merge", "replace", "append"):
        return err(f"Invalid mode: {mode} (must be merge/replace/append)")
    if not isinstance(rules, list):
        return err("rules must be an array")
    imported = 0
    now = datetime.now().isoformat()
    if mode == "replace":
        db.delete_all_rules()
    existing_ids = {r["id"] for r in db.get_rules()}
    for r in rules:
        if not isinstance(r, dict):
            continue
        try:
            # 标准化字段
            rid = r.get("id") or uuid.uuid4().hex
            if mode == "append":
                rid = uuid.uuid4().hex
            action = r.get("action", "mock")
            # 序列化 modify_rules：script 时为脚本字符串，其他时为 list 转 JSON
            # 复用 _store_modify_rules 保持与 create/update 一致的存储格式
            modify_rules_stored = _store_modify_rules(action, r.get("modify_rules", []))
            # 序列化 mock_headers：dict -> JSON 字符串才能入库；已是字符串则校验后保留
            mock_headers_raw = r.get("mock_headers", {}) or {}
            if isinstance(mock_headers_raw, str):
                try:
                    _json.loads(mock_headers_raw)
                    mock_headers_stored = mock_headers_raw
                except Exception as e:
                    mock_headers_stored = "{}"
            else:
                mock_headers_stored = _json.dumps(mock_headers_raw, ensure_ascii=False)
            rule_data = {
                "enabled": 1 if r.get("enabled", True) else 0,
                "match_mode": r.get("match_mode", "wildcard"),
                "pattern": r.get("pattern", ""),
                "action": action,
                "mock_status": r.get("mock_status", 200) or 200,
                "mock_headers": mock_headers_stored,
                "mock_body": r.get("mock_body", "") or "",
                "modify_rules": modify_rules_stored,
                "note": r.get("note", "") or "",
                "method_filter": r.get("method_filter", "") or "",
                "status_filter": r.get("status_filter", "") or "",
                "pid_filter": r.get("pid_filter", "") or "",
                "process_filter": r.get("process_filter", "") or "",
                "mock_method": r.get("mock_method", "GET") or "GET",
                "mock_url": r.get("mock_url", "") or "",
                "updated_at": now,
            }
            if mode == "merge" and rid in existing_ids:
                # 更新已有
                db.update_rule(rid, rule_data)
            else:
                # 新增
                rule_data["id"] = rid
                db.add_rule(rule_data)
            imported += 1
        except Exception as e:
            # 单条失败不影响其他
            logger.warning("auto_reply", f"Import rule failed: {e}", "")
            continue
    invalidate_cache()
    return ok({"imported": imported, "mode": mode})


@router.post("/auto-reply/rules")
async def create_rule(body: RuleCreate):
    """Create rule."""
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
    """§2.2 Batch create rules. Returns {created, errors}."""
    if not body.rules:
        return err("rules cannot be empty")
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
    """Compute rule signature (JSON serialization of pattern+match_mode+action+modify_rules).

    Used for --idempotent comparison: same signature means same rule.
    Note: modify_rules is order-sensitive ([{a:1},{b:2}] and [{b:2},{a:1}] have different signatures).
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
    """§3.1 Idempotent create rule: if a rule with the same signature (pattern+match_mode+action+modify_rules) already exists, return existing rule_id without creating duplicate.

    More efficient than CLI client traversal (single request + server-side comparison).
    Returns {created: bool, idempotent: bool, rule_id, rule?}.
    """
    import json
    new_sig = _rule_signature(body)
    existing_rules = db.get_rules()
    for r in existing_rules:
        try:
            existing_modify = json.loads(r.get("modify_rules") or "[]")
        except Exception as e:  # noqa: BLE001
            _capture_log("error", "API exception in auto_reply.py", extra={"exc": repr(e)})
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
                "hint": "Same rule already exists, not created again",
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
    """Update rule."""
    import json
    rule = db.get_rule(rule_id)
    if not rule:
        return err("Rule not found")
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
                # 仅更新 action（未传 modify_rules）时从已落库的 updated 取最新脚本
                new_script = updates.get("modify_rules") or updated.get("modify_rules", "")
                reload_runner(rule_id, new_script)
            else:
                # action 改成非 script，清理可能存在的 runner
                remove_runner(rule_id)
        except Exception as e:

            _capture_log("error", "API exception", extra={"exc": repr(e)})

            pass
    return ok(_serialize(db.get_rule(rule_id)))


@router.post("/auto-reply/rules/batch-update")
async def batch_update_rules(body: BatchUpdate):
    """Batch update rules (enable/disable)."""
    if not body.ids:
        return err("ids cannot be empty")
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
            except Exception as e:

                _capture_log("error", "API exception", extra={"exc": repr(e)})

                pass
    invalidate_cache()
    return ok({"updated": len(body.ids)})


@router.post("/auto-reply/rules/batch-delete")
async def batch_delete_rules(body: BatchDelete):
    """Batch delete rules."""
    if not body.ids:
        return err("ids cannot be empty")
    for rid in body.ids:
        db.delete_rule(rid)
        try:
            from ..auto_reply.script_runner import remove_runner
            remove_runner(rid)
        except Exception as e:

            _capture_log("error", "API exception", extra={"exc": repr(e)})

            pass
    invalidate_cache()
    return ok({"deleted": len(body.ids)})


@router.delete("/auto-reply/rules/{rule_id}")
async def delete_rule(rule_id: str):
    """Delete rule."""
    db.delete_rule(rule_id)
    invalidate_cache()
    try:
        from ..auto_reply.script_runner import remove_runner
        remove_runner(rule_id)
    except Exception as e:

        _capture_log("error", "API exception", extra={"exc": repr(e)})

        pass
    return ok({"deleted": True})


@router.get("/auto-reply/rules/{rule_id}/script-error")
async def get_script_error(rule_id: str):
    """Query script rule's error history (syntax error / runtime exception / timeout).

    Design fix: returns full error history from database instead of just last error.
    Only meaningful for rules with action=script; other rules return empty list.
    """
    errors = db.get_script_errors(rule_id, limit=100)
    latest = db.get_latest_script_error(rule_id)
    return ok({
        "errors": errors,
        "latest_error": latest.get("error_message") if latest else None,
        "error_count": len(errors)
    })


@router.delete("/auto-reply/rules/{rule_id}/script-error")
async def delete_script_error_history(rule_id: str):
    """Clear script error history for a rule."""
    db.delete_script_errors(rule_id)
    return ok({"deleted": True})


# ---------- Script testing ----------


class MockRequest(BaseModel):
    """Mock request for testing."""
    host: str = "api.example.com"
    path: str = "/v1/user"
    method: str = "GET"
    scheme: str = "https"
    http_version: str = "HTTP/1.1"
    headers: dict = {}
    body: str = ""


class MockResponse(BaseModel):
    """Mock response for testing (optional, used to test on_response hook)."""
    status_code: int = 200
    headers: dict = {}
    body: str = ""


class TestScriptReq(BaseModel):
    """Test script request body."""
    script: str
    mock_request: MockRequest = MockRequest()
    mock_response: MockResponse | None = None


def _decode_body(b: bytes) -> str:
    """Try to decode body as string (for frontend display)."""
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return f"<binary {len(b)} bytes>"


def _b64decode_safe(s: str) -> bytes:
    if not s:
        return b""
    try:
        return base64.b64decode(s)
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in auto_reply.py", extra={"exc": repr(e)})
        return b""


def _parse_request_phase(resp: dict | None, mock_req: MockRequest) -> dict:
    """Parse on_request phase result, build frontend-readable structure."""
    orig_headers = {k: str(v) for k, v in (mock_req.headers or {}).items()}
    orig_body = (mock_req.body or "").encode("utf-8")

    if resp is None:
        return {
            "available": False,
            "action": "continue",
            "modified": False,
            "headers": orig_headers,
            "body": mock_req.body or "",
            "mock_status": None,
            "mock_headers": None,
            "mock_body": "",
            "error": None,
            "traceback": "",
            # === 调试增强 ===
            "logs": [],
            "variables": {},
        }

    action = resp.get("action", "continue")
    modified_headers = orig_headers
    if resp.get("request_headers"):
        modified_headers = {k: str(v) for k, v in resp["request_headers"].items()}
    modified_body = orig_body
    if resp.get("request_body_b64"):
        modified_body = _b64decode_safe(resp["request_body_b64"])

    modified = (
        resp.get("request_headers") is not None
        or resp.get("request_body_b64") is not None
    )
    return {
        "available": True,
        "action": action,
        "modified": modified,
        "headers": modified_headers,
        "body": _decode_body(modified_body),
        "mock_status": resp.get("mock_status"),
        "mock_headers": resp.get("mock_headers"),
        "mock_body": _decode_body(_b64decode_safe(resp.get("mock_body_b64") or "")),
        "error": resp.get("error"),
        "traceback": resp.get("traceback", ""),
        # === 调试增强 ===
        # 脚本中 ctx.log() 和 print() 的输出
        "logs": resp.get("logs", []),
        # 脚本中 set_var() 设置的中间变量
        "variables": resp.get("variables", {}),
    }


def _parse_response_phase(resp: dict | None, mock_resp: MockResponse) -> dict:
    """Parse on_response phase result."""
    orig_headers = {k: str(v) for k, v in (mock_resp.headers or {}).items()}
    orig_body = (mock_resp.body or "").encode("utf-8")
    orig_status = int(mock_resp.status_code or 200)

    if resp is None:
        return {
            "available": False,
            "action": "continue",
            "modified": False,
            "status_code": orig_status,
            "headers": orig_headers,
            "body": mock_resp.body or "",
            "error": None,
            "traceback": "",
            # === 调试增强 ===
            "logs": [],
            "variables": {},
        }

    action = resp.get("action", "continue")
    modified_headers = orig_headers
    if resp.get("response_headers"):
        modified_headers = {k: str(v) for k, v in resp["response_headers"].items()}
    modified_body = orig_body
    if resp.get("response_body_b64"):
        modified_body = _b64decode_safe(resp["response_body_b64"])
    modified_status = orig_status
    if resp.get("status_code") is not None:
        modified_status = int(resp["status_code"])

    modified = (
        resp.get("response_headers") is not None
        or resp.get("response_body_b64") is not None
        or resp.get("status_code") is not None
    )
    return {
        "available": True,
        "action": action,
        "modified": modified,
        "status_code": modified_status,
        "headers": modified_headers,
        "body": _decode_body(modified_body),
        "error": resp.get("error"),
        "traceback": resp.get("traceback", ""),
        # === 调试增强 ===
        # 脚本中 ctx.log() 和 print() 的输出
        "logs": resp.get("logs", []),
        # 脚本中 set_var() 设置的中间变量
        "variables": resp.get("variables", {}),
    }


@router.post("/auto-reply/test-script")
async def test_script(body: dict):
    """Test Python script execution (no real request initiated).

    Request body:
        {
            "script": "def on_request(ctx): ...",
            "mock_request": {host, path, method, scheme, http_version, headers, body},
            "mock_response": {status_code, headers, body}  # optional
            "mode": "request" | "response" | "both"  # default "both"
        }

    mode description:
        - "request"  : only call on_request (ignore mock_response)
        - "response" : only call on_response (requires mock_response, skip on_request modifications)
        - "both"     : on_request + on_response (default)

    Returns:
        {
            "ok": true/false,
            "duration_ms": int,
            "error": str | null,
            "traceback": str | null,
            "request_phase": {...} | null,
            "response_phase": {...} | null
        }

    Security: script runs in isolated worker subprocess (consistent with production environment), single call 5s timeout,
    all exceptions are caught, will not crash backend.
    """
    from ..auto_reply.script_runner import ScriptRunner, build_ctx, _b64encode

    script = body.get("script", "") or ""
    if not script.strip():
        return err("Script content is empty")

    mode = (body.get("mode") or "both").lower()
    if mode not in ("request", "response", "both"):
        return err(f"mode must be request/response/both, got: {mode}")

    mr_raw = body.get("mock_request") or {}
    try:
        mock_req = MockRequest(**mr_raw)
    except Exception as e:  # noqa: BLE001
        return err(f"Invalid mock_request parameter: {e}")

    mock_resp_raw = body.get("mock_response")
    mock_resp: MockResponse | None = None
    if mock_resp_raw is not None:
        try:
            mock_resp = MockResponse(**mock_resp_raw)
        except Exception as e:  # noqa: BLE001
            return err(f"Invalid mock_response parameter: {e}")

    # 模式校验：response/both 模式必须有 mock_response
    if mode in ("response", "both") and mock_resp is None:
        return err(f"mode={mode} requires mock_response")

    # 构造请求 ctx（与生产 build_ctx 完全一致）
    req_body_bytes = (mock_req.body or "").encode("utf-8")
    url = f"{mock_req.scheme}://{mock_req.host}{mock_req.path}"
    ctx_req = build_ctx(
        host=mock_req.host,
        path=mock_req.path,
        method=mock_req.method.upper(),
        url=url,
        scheme=mock_req.scheme,
        pid=None,
        process_name="",
        session_id=None,
        request_headers={k: str(v) for k, v in (mock_req.headers or {}).items()},
        request_body=req_body_bytes,
    )

    # 临时 runner（不放入全局注册表，避免污染生产规则状态）
    test_id = f"__test_{uuid.uuid4().hex[:8]}"
    runner = ScriptRunner(test_id, script)
    start = time.time()

    try:
        # 阶段 1：on_request（"response" 模式跳过）
        req_resp = None
        if mode in ("request", "both"):
            req_resp = runner.run_request(ctx_req)

        # 阶段 2：on_response（"request" 模式跳过）
        resp_resp = None
        if mode in ("response", "both") and mock_resp is not None:
            ctx_resp = dict(ctx_req)
            ctx_resp["status_code"] = int(mock_resp.status_code or 200)
            ctx_resp["response_headers"] = {
                k: str(v) for k, v in (mock_resp.headers or {}).items()
            }
            resp_body_bytes = (mock_resp.body or "").encode("utf-8")
            ctx_resp["response_body_b64"] = _b64encode(resp_body_bytes)
            resp_resp = runner.run_response(ctx_resp)

        duration_ms = int((time.time() - start) * 1000)

        # worker 启动失败 / 调用失败：run_request 返回 None
        last_error = runner.get_last_error()
        if req_resp is None and mode != "response":
            return ok({
                "ok": False,
                "duration_ms": duration_ms,
                "error": last_error or "Script execution failed (worker not started or call timeout)",
                "traceback": "",
                "request_phase": None,
                "response_phase": None,
            })

        req_phase = _parse_request_phase(req_resp, mock_req) if req_resp is not None else None
        resp_phase = None
        if mock_resp is not None and resp_resp is not None:
            resp_phase = _parse_response_phase(resp_resp, mock_resp)

        # 收集阶段内的运行时错误（脚本异常被 worker 捕获，action=continue + error 字段）
        phase_error = None
        phase_traceback = ""
        if req_phase and req_phase.get("error"):
            phase_error = f"on_request: {req_phase['error']}"
            phase_traceback = req_phase.get("traceback", "")
        elif resp_phase and resp_phase.get("error"):
            phase_error = f"on_response: {resp_phase['error']}"
            phase_traceback = resp_phase.get("traceback", "")

        return ok({
            "ok": phase_error is None,
            "duration_ms": duration_ms,
            "error": phase_error,
            "traceback": phase_traceback,
            "request_phase": req_phase,
            "response_phase": resp_phase,
        })
    except Exception as e:  # noqa: BLE001
        # 兜底：任何未预期异常都返回错误，不让后端崩溃
        return ok({
            "ok": False,
            "duration_ms": int((time.time() - start) * 1000),
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
            "request_phase": None,
            "response_phase": None,
        })
    finally:
        try:
            runner.stop()
        except Exception as e:

            _capture_log("error", "API exception", extra={"exc": repr(e)})

            pass


# ---------- 规则匹配预览（§3.2 P0 功能增强）----------

class PreviewMatchReq(BaseModel):
    """Preview match request body."""
    pattern: str
    match_mode: str = "wildcard"
    method_filter: str = ""
    status_filter: str = ""
    pid_filter: str = ""
    process_filter: str = ""
    limit: int = 20  # 限制返回数量


@router.post("/auto-reply/rules/preview-match")
async def preview_match(body: PreviewMatchReq):
    """Preview which flows would match this rule pattern.

    Returns the count and sample URLs of matching flows from the database.
    Performance: uses SQLite LIKE for quick preview (not regex evaluation).
    """
    if not body.pattern.strip():
        return ok({"total": 0, "samples": []})

    # 转义 SQL LIKE 特殊字符
    escaped = body.pattern.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
    # 构造 LIKE 模式
    like_pattern = f"%{escaped}%"

    # 构建 WHERE 条件
    conditions = ["url LIKE ?"]
    params = [like_pattern]

    if body.method_filter:
        methods = [m.strip().upper() for m in body.method_filter.split(",") if m.strip()]
        if methods:
            placeholders = ",".join("?" * len(methods))
            conditions.append(f"method IN ({placeholders})")
            params.extend(methods)

    if body.status_filter:
        statuses = [s.strip() for s in body.status_filter.split(",") if s.strip()]
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            conditions.append(f"status_code IN ({placeholders})")
            params.extend(statuses)

    where_clause = " AND ".join(conditions)

    with db.get_connection() as conn:
        # 统计总数
        total_row = conn.execute(
            f"SELECT COUNT(*) as cnt FROM flows WHERE {where_clause}",
            params
        ).fetchone()
        total = total_row["cnt"] if total_row else 0

        # 获取示例（限制数量）
        samples = []
        rows = conn.execute(
            f"SELECT id, method, host, path, url FROM flows WHERE {where_clause} ORDER BY id DESC LIMIT ?",
            params + [body.limit]
        ).fetchall()
        for row in rows:
            samples.append({
                "flow_id": row["id"],
                "method": row["method"] or "",
                "host": row["host"] or "",
                "path": row["path"] or "",
                "url": row["url"] or "",
            })

    return ok({
        "total": total,
        "samples": samples,
        "pattern": body.pattern,
        "match_mode": body.match_mode,
    })


# ---------- 规则分组管理（§3.2 P0 功能增强）----------

class RuleGroupCreate(BaseModel):
    name: str
    enabled: bool = True


class RuleGroupUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    sort_order: int | None = None


@router.get("/auto-reply/groups")
async def list_rule_groups():
    """List all rule groups with rule counts."""
    groups = db.get_rule_groups()
    # 统计每个分组的规则数量
    rules = db.get_rules()
    group_counts = {}
    for r in rules:
        gid = r.get("group_id")
        if gid:
            group_counts[gid] = group_counts.get(gid, 0) + 1
    for g in groups:
        g["rule_count"] = group_counts.get(g["id"], 0)
    return ok(groups)


@router.post("/auto-reply/groups")
async def create_rule_group(body: RuleGroupCreate):
    """Create a new rule group."""
    if not body.name.strip():
        return err("Group name cannot be empty")
    group_id = db.create_rule_group(body.name, body.enabled)
    group = db.get_rule_group(group_id)
    return ok(group)


@router.put("/auto-reply/groups/{group_id}")
async def update_rule_group(group_id: int, body: RuleGroupUpdate):
    """Update a rule group."""
    updates = {}
    if body.name is not None:
        updates["name"] = body.name.strip()
    if body.enabled is not None:
        updates["enabled"] = body.enabled
    if body.sort_order is not None:
        updates["sort_order"] = body.sort_order
    if not updates:
        return err("No fields to update")
    if "name" in updates and not updates["name"]:
        return err("Group name cannot be empty")
    db.update_rule_group(group_id, updates)
    group = db.get_rule_group(group_id)
    if not group:
        return err("Group not found")
    return ok(group)


@router.delete("/auto-reply/groups/{group_id}")
async def delete_rule_group(group_id: int):
    """Delete a rule group. Rules in the group will have group_id set to NULL."""
    success = db.delete_rule_group(group_id)
    if not success:
        return err("Group not found")
    invalidate_cache()
    return ok({"deleted": True})


@router.put("/auto-reply/rules/{rule_id}/group")
async def update_rule_group_membership(rule_id: str, body: dict):
    """Update a rule's group membership.

    body: {"group_id": int | null}
    """
    group_id = body.get("group_id")
    if group_id is not None:
        # 验证分组存在
        group = db.get_rule_group(group_id)
        if not group:
            return err("Group not found")
    db.update_rule_group_id(rule_id, group_id)
    invalidate_cache()
    return ok({"updated": True})


@router.put("/auto-reply/rules/{rule_id}/tags")
async def update_rule_tags(rule_id: str, body: dict):
    """Update a rule's tags.

    body: {"tags": "tag1,tag2,tag3"}
    """
    tags = body.get("tags", "")
    rule = db.get_rule(rule_id)
    if not rule:
        return err("Rule not found")
    import json
    db.update_rule(rule_id, {"tags": tags})
    invalidate_cache()
    return ok({"updated": True})


# ---------- 规则命中统计（§3.2 P0 功能增强）----------

@router.get("/auto-reply/stats/hits")
async def get_rule_hit_stats():
    """Get rule hit statistics: leaderboard and recent hits."""
    rules = db.get_rules()
    # 排行榜：按命中次数排序
    leaderboard = []
    for r in rules:
        if r.get("hit_count", 0) > 0:
            leaderboard.append({
                "rule_id": r["id"],
                "pattern": r.get("pattern", ""),
                "action": r.get("action", ""),
                "note": r.get("note", ""),
                "hit_count": r.get("hit_count", 0),
                "last_hit_at": r.get("last_hit_at", ""),
            })
    leaderboard.sort(key=lambda x: x["hit_count"], reverse=True)

    # 最近命中
    recent_hits = db.get_recent_rule_hits(limit=10)

    # 总命中次数
    total_hits = sum(r.get("hit_count", 0) for r in rules)

    return ok({
        "total_hits": total_hits,
        "leaderboard": leaderboard[:20],  # 最多 20 条
        "recent_hits": recent_hits,
    })


@router.post("/auto-reply/stats/hits/clear")
async def clear_rule_hit_stats():
    """Clear all rule hit statistics."""
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE auto_reply_rules SET hit_count = 0, last_hit_at = '', last_hit_flow_id = NULL"
        )
    # 清除实时追踪数据
    from ..auto_reply.hit_tracker import clear_stats as clear_realtime_stats
    clear_realtime_stats()
    return ok({"cleared": True})


# ---------- 实时命中追踪（热力图 + 时间线 + 排行榜）----------

import asyncio
import json
from fastapi.responses import StreamingResponse


@router.get("/auto-reply/stats/hits/stream")
async def stream_hit_stats():
    """SSE 实时推送规则命中统计数据。

    推送频率：每 2 秒一次完整快照 + 每次命中立即推送增量。

    数据格式:
    {
        "type": "snapshot" | "hit" | "stats",
        "total_hits": int,
        "leaderboard": [...],  // TOP 10
        "heatmap": [...],      // 所有规则的耗时统计
        "timeline": [...],     // 最近 100 次命中
    }
    """
    from ..auto_reply.hit_tracker import get_stats, hit_tracker

    async def event_generator():
        # 初始快照
        stats = get_stats()
        yield f"data: {json.dumps({'type': 'init', **stats})}\n\n"

        # 记录上次的 timeline 长度，用于检测新命中
        last_timeline_len = len(stats.get("timeline", []))

        # 循环推送（每 2 秒一次完整快照）
        while True:
            await asyncio.sleep(2)
            try:
                stats = get_stats()
                timeline = stats.get("timeline", [])

                # 检测是否有新命中（时间线长度增加）
                if len(timeline) > last_timeline_len:
                    # 有新命中，发送增量更新
                    new_hits = timeline[:len(timeline) - last_timeline_len]
                    for hit in reversed(new_hits):
                        yield f"data: {json.dumps({'type': 'hit', 'data': hit, 'total_hits': stats['total_hits']})}\n\n"
                    last_timeline_len = len(timeline)

                # 发送完整快照
                yield f"data: {json.dumps({'type': 'snapshot', **stats})}\n\n"
            except Exception:
                # 出错时发送心跳保持连接
                yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲
        },
    )


@router.get("/auto-reply/stats/hits/realtime")
async def get_realtime_hit_stats():
    """获取实时命中统计数据（轮询接口，返回完整快照）。"""
    from ..auto_reply.hit_tracker import get_stats as get_realtime_stats
    return ok(get_realtime_stats())

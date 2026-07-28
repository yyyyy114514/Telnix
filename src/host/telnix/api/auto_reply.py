"""自动回复规则 CRUD API。"""

import base64
import json as _json
import time
import traceback
import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db, logger
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
                except Exception:
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
            logger.warning("auto_reply", f"导入规则失败: {e}", "")
            continue
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


@router.get("/auto-reply/rules/{rule_id}/script-error")
async def get_script_error(rule_id: str):
    """查询脚本规则最近一次错误（语法错误 / 运行异常 / 超时）。

    仅对 action=script 的规则有意义；其他规则返回 error=None。
    """
    try:
        from ..auto_reply.script_runner import get_runner_error
        err_msg = get_runner_error(rule_id)
    except Exception as e:  # noqa: BLE001
        err_msg = f"查询错误失败: {e}"
    return ok({"error": err_msg})


# ---------- 脚本测试 ----------


class MockRequest(BaseModel):
    """测试用模拟请求。"""
    host: str = "api.example.com"
    path: str = "/v1/user"
    method: str = "GET"
    scheme: str = "https"
    http_version: str = "HTTP/1.1"
    headers: dict = {}
    body: str = ""


class MockResponse(BaseModel):
    """测试用模拟响应（可选，用于测试 on_response 钩子）。"""
    status_code: int = 200
    headers: dict = {}
    body: str = ""


class TestScriptReq(BaseModel):
    """测试脚本请求体。"""
    script: str
    mock_request: MockRequest = MockRequest()
    mock_response: MockResponse | None = None


def _decode_body(b: bytes) -> str:
    """尝试解码 body 为字符串（前端展示用）。"""
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
    except Exception:  # noqa: BLE001
        return b""


def _parse_request_phase(resp: dict | None, mock_req: MockRequest) -> dict:
    """解析 on_request 阶段结果，构造前端可读结构。"""
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
    }


def _parse_response_phase(resp: dict | None, mock_resp: MockResponse) -> dict:
    """解析 on_response 阶段结果。"""
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
    }


@router.post("/auto-reply/test-script")
async def test_script(body: dict):
    """测试 Python 脚本执行（不发起真实请求）。

    请求体：
        {
            "script": "def on_request(ctx): ...",
            "mock_request": {host, path, method, scheme, http_version, headers, body},
            "mock_response": {status_code, headers, body}  # 可选
            "mode": "request" | "response" | "both"  # 默认 "both"
        }

    mode 说明：
        - "request"  : 只调用 on_request（忽略 mock_response）
        - "response" : 只调用 on_response（需要 mock_response，跳过 on_request 修改）
        - "both"     : on_request + on_response（默认）

    返回：
        {
            "ok": true/false,
            "duration_ms": int,
            "error": str | null,
            "traceback": str | null,
            "request_phase": {...} | null,
            "response_phase": {...} | null
        }

    安全：脚本在独立 worker 子进程运行（与生产环境一致），单次调用 5s 超时，
    所有异常被捕获，不会让后端崩溃。
    """
    from ..auto_reply.script_runner import ScriptRunner, build_ctx, _b64encode

    script = body.get("script", "") or ""
    if not script.strip():
        return err("脚本内容为空")

    mode = (body.get("mode") or "both").lower()
    if mode not in ("request", "response", "both"):
        return err(f"mode 必须为 request/response/both，收到: {mode}")

    mr_raw = body.get("mock_request") or {}
    try:
        mock_req = MockRequest(**mr_raw)
    except Exception as e:  # noqa: BLE001
        return err(f"mock_request 参数无效: {e}")

    mock_resp_raw = body.get("mock_response")
    mock_resp: MockResponse | None = None
    if mock_resp_raw is not None:
        try:
            mock_resp = MockResponse(**mock_resp_raw)
        except Exception as e:  # noqa: BLE001
            return err(f"mock_response 参数无效: {e}")

    # 模式校验：response/both 模式必须有 mock_response
    if mode in ("response", "both") and mock_resp is None:
        return err(f"mode={mode} 需要提供 mock_response")

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
                "error": last_error or "脚本执行失败（worker 未启动或调用超时）",
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
        except Exception:  # noqa: BLE001
            pass

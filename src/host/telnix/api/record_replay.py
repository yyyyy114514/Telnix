"""流量录制/回放管理。

录制脚本存储在 settings.json 的 ``record_scripts`` 键下，每个脚本包含
flow_ids 及创建时从 flows 表快照下来的完整 flows 数据。回放使用
httpx.AsyncClient 并发重放，返回统计结果。

录制状态用模块级变量管理；代理层（server.py）通过 ``add_flow_to_recording``
把新 flow 加入当前录制会话。

增强功能：
- 变量提取与多环境配置
- 响应断言
- 条件执行规则
- 录制 → Mock 自举

性能优化：
- 预编译正则模式（path_pattern, query_pattern）
"""

import asyncio
import json
import re
import threading

# 性能优化：预编译正则模式
_PATH_PATTERN = re.compile(r'/\{(\w+)\}|\{(\w+)\}')
_QUERY_PATTERN = re.compile(r'[\?&](\w+)=([^&\s]+)')
import time
import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db, settings_store
from ..logger import _capture_log
from . import err, ok

router = APIRouter()

_SCRIPTS_KEY = "record_scripts"
_ENVIRONMENTS_KEY = "record_environments"
_VARIABLES_KEY = "record_variables"
_ASSERTIONS_KEY = "record_assertions"
_CONDITIONS_KEY = "record_conditions"


# ---------- 脚本数据模型 ----------

class ScriptCreate(BaseModel):
    """新建脚本请求体。"""

    name: str
    flow_ids: list[int] = []
    note: str = ""


class ScriptStopRecord(BaseModel):
    """停止录制请求体（name/note 可选）。"""

    name: str | None = None
    note: str = ""


# ---------- 内部工具 ----------

def _get_scripts() -> list[dict]:
    """从 settings.json 读取脚本列表（不含 flows 大字段，用于列表展示）。"""
    data = settings_store.get_setting(_SCRIPTS_KEY, [])
    if isinstance(data, list):
        # 列表展示时剥离 flows 大字段
        return [{k: v for k, v in s.items() if k != "flows"} for s in data if isinstance(s, dict)]
    return []


def _get_all_scripts_with_flows() -> list[dict]:
    """读取完整脚本列表（含 flows）。"""
    data = settings_store.get_setting(_SCRIPTS_KEY, [])
    return data if isinstance(data, list) else []


def _save_scripts(scripts: list[dict]) -> None:
    """写回脚本列表到 settings.json。"""
    settings_store.set_setting(_SCRIPTS_KEY, scripts)


def _find_script(scripts: list[dict], script_id: str) -> dict | None:
    for s in scripts:
        if s.get("id") == script_id:
            return s
    return None


def _safe_json(s):
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in record_replay.py", extra={"exc": repr(e)})
        return {}


def _to_bytes(s) -> bytes:
    """把 request_body 文本还原为字节（兼容 base64: 前缀）。"""
    if not s:
        return b""
    if isinstance(s, bytes):
        return s
    if s.startswith("base64:"):
        import base64
        try:
            return base64.b64decode(s[7:])
        except Exception as e:  # noqa: BLE001
            _capture_log("error", "API exception in record_replay.py", extra={"exc": repr(e)})
            return b""
    return s.encode("utf-8")


# ---------- 录制状态（模块级）----------

_recording_state: dict = {
    "active": False,
    "recording_id": None,
    "session_start": None,
    "flow_ids": set(),
}
_recording_lock = threading.Lock()


def is_recording() -> bool:
    """供代理层查询当前是否处于录制状态。"""
    with _recording_lock:
        return bool(_recording_state["active"])


def add_flow_to_recording(flow_id: int) -> None:
    """供代理层调用：若处于录制状态，把新 flow_id 加入当前录制会话。"""
    with _recording_lock:
        if _recording_state["active"]:
            _recording_state["flow_ids"].add(flow_id)


def _recording_snapshot() -> dict:
    """获取录制状态快照（set 转 list 以便 JSON 序列化）。"""
    with _recording_lock:
        return {
            "active": _recording_state["active"],
            "recording_id": _recording_state["recording_id"],
            "session_start": _recording_state["session_start"],
            "flow_count": len(_recording_state["flow_ids"]),
            "flow_ids": sorted(_recording_state["flow_ids"]),
        }


# ---------- 回放实现 ----------

async def _replay_flows(flows: list[dict]) -> dict:
    """用 httpx.AsyncClient 并发重放 flows，返回统计结果。

    仅重放 url 以 http 开头的 flow；并发上限 10；超时 30s；不校验 TLS 证书、
    不跟随重定向，以如实记录原始响应。
    """
    try:
        import httpx
    except ImportError:  # noqa: BLE001
        raise RuntimeError("httpx is not installed, replay unavailable")

    # 仅重放合法 HTTP(S) 流量
    http_flows = [f for f in flows if (f.get("url") or "").lower().startswith("http")]

    results: list[dict] = []
    success = 0
    fail = 0
    status_dist: dict[str, int] = {}
    sem = asyncio.Semaphore(10)

    async with httpx.AsyncClient(timeout=30.0, verify=False, follow_redirects=False, proxy=None) as client:

        async def _one(flow: dict):
            async with sem:
                t0 = time.time()
                method = (flow.get("method") or "GET").upper()
                url = flow.get("url") or ""
                headers = _safe_json(flow.get("request_headers"))
                # 移除 hop-by-hop / Host 头，交给 httpx 自行管理
                for k in list(headers.keys()):
                    if k.lower() in ("host", "content-length", "transfer-encoding",
                                      "connection", "keep-alive"):
                        del headers[k]
                content = _to_bytes(flow.get("request_body"))
                try:
                    resp = await client.request(method, url, headers=headers, content=content)
                    dt = (time.time() - t0) * 1000.0
                    results.append({
                        "flow_id": flow.get("id"),
                        "url": url,
                        "method": method,
                        "status_code": resp.status_code,
                        "duration_ms": round(dt, 2),
                        "ok": True,
                    })
                except Exception as e:  # noqa: BLE001
                    dt = (time.time() - t0) * 1000.0
                    results.append({
                        "flow_id": flow.get("id"),
                        "url": url,
                        "method": method,
                        "error": str(e),
                        "duration_ms": round(dt, 2),
                        "ok": False,
                    })

        await asyncio.gather(*[_one(f) for f in http_flows], return_exceptions=True)

    for r in results:
        if r.get("ok"):
            success += 1
            sc = str(r.get("status_code"))
            status_dist[sc] = status_dist.get(sc, 0) + 1
        else:
            fail += 1

    return {
        "total": len(flows),
        "replayed": len(http_flows),
        "skipped": len(flows) - len(http_flows),
        "success": success,
        "fail": fail,
        "status_distribution": status_dist,
        "results": results,
    }


# ---------- REST 接口 ----------

@router.get("/record-scripts")
async def list_scripts():
    """返回脚本列表（不含 flows 大字段）。"""
    return ok(_get_scripts())


@router.post("/record-scripts")
async def create_script(body: ScriptCreate):
    """新建脚本：从 flows 表查询 flow_ids 的完整数据并快照到脚本。"""
    flow_ids = body.flow_ids or []
    flows_map = db.get_flows_by_ids(flow_ids) if flow_ids else {}
    # 按 body 顺序保留
    flows = [flows_map[fid] for fid in flow_ids if fid in flows_map]
    script = {
        "id": uuid.uuid4().hex[:8],
        "name": body.name,
        "created_at": datetime.now().isoformat(),
        "flow_ids": list(flow_ids),
        "note": body.note or "",
        "flows": flows,
    }
    scripts = _get_all_scripts_with_flows()
    scripts.append(script)
    _save_scripts(scripts)
    # 返回时剥离 flows，列表展示用
    out = {k: v for k, v in script.items() if k != "flows"}
    out["flow_count"] = len(flows)
    return ok(out)


# ---------- 变量提取与多环境配置 ----------

class VariableExtractionRequest(BaseModel):
    """从 flows 提取变量的请求。"""
    flow_ids: list[int]
    extraction_rules: list[dict] = []  # [{name, field, pattern}]


class EnvironmentCreate(BaseModel):
    """创建/更新环境。"""
    id: str | None = None
    name: str
    description: str = ""
    variables: dict[str, str] = {}
    is_default: bool = False


class ScriptVariable(BaseModel):
    """脚本变量定义。"""
    name: str
    description: str = ""
    type: str = "string"  # string | number | boolean | json_path | regex
    extraction_rule: str = ""  # field.path 或 regex pattern
    sample_values: list[str] = []
    from_flow_id: int = 0


def _get_script_variables(script_id: str) -> list[dict]:
    """获取脚本的变量定义。"""
    data = settings_store.get_setting(f"{_VARIABLES_KEY}_{script_id}", [])
    return data if isinstance(data, list) else []


def _save_script_variables(script_id: str, variables: list[dict]) -> None:
    """保存脚本变量定义。"""
    settings_store.set_setting(f"{_VARIABLES_KEY}_{script_id}", variables)


def _get_environments() -> list[dict]:
    """获取所有环境配置。"""
    data = settings_store.get_setting(_ENVIRONMENTS_KEY, [])
    return data if isinstance(data, list) else []


def _save_environments(environments: list[dict]) -> None:
    """保存环境配置列表。"""
    settings_store.set_setting(_ENVIRONMENTS_KEY, environments)


def _extract_variable_from_flow(flow: dict, rule: dict) -> str | None:
    """根据提取规则从 flow 中提取变量值。"""
    field = rule.get("field", "")
    pattern = rule.get("pattern", "")
    name = rule.get("name", "")

    if not field or not name:
        return None

    # 获取字段值
    value = flow
    for part in field.split("."):
        if value is None:
            return None
        value = value.get(part) if isinstance(value, dict) else None

    if value is None:
        return None

    value_str = str(value)

    # 根据类型提取
    var_type = rule.get("type", "string")
    if var_type == "regex" and pattern:
        match = re.search(pattern, value_str)
        if match:
            return match.group(1) if match.lastindex else match.group(0)
    elif var_type == "json_path" and pattern:
        # 简单的 JSONPath 提取（支持 .key 和 .key[0]）
        try:
            data = json.loads(value_str) if isinstance(value, str) else value
            parts = pattern.lstrip(".").split(".")
            for p in parts:
                if data is None:
                    return None
                if p.endswith("]"):
                    key, idx = p[:-1], int(p[p.index("[") + 1:-1])
                    data = data.get(key)
                    if isinstance(data, list) and 0 <= idx < len(data):
                        data = data[idx]
                    else:
                        return None
                else:
                    data = data.get(p)
            return str(data) if data is not None else None
        except Exception:  # noqa: BLE001
            return None
    else:
        return value_str

    return None


@router.get("/record-scripts/{script_id}/variables")
async def get_script_variables(script_id: str):
    """获取脚本的变量定义。"""
    variables = _get_script_variables(script_id)
    return ok(variables)


@router.post("/record-scripts/{script_id}/variables/extract")
async def extract_variables(script_id: str, body: VariableExtractionRequest):
    """从 flows 中自动提取变量。"""
    flows_map = db.get_flows_by_ids(body.flow_ids) if body.flow_ids else {}
    flows = [flows_map[fid] for fid in body.flow_ids if fid in flows_map]

    # 自动检测可提取的变量
    detected_vars: dict[str, dict] = {}

    # 内置提取规则：从 URL path、query、body 中检测参数（使用预编译正则）
    path_pattern = _PATH_PATTERN
    query_pattern = _QUERY_PATTERN

    for flow in flows:
        url = flow.get("url", "")
        body_str = flow.get("request_body") or ""

        # 从 path 提取变量
        for match in path_pattern.finditer(url):
            var_name = match.group(1) or match.group(2)
            if var_name:
                if var_name not in detected_vars:
                    detected_vars[var_name] = {
                        "name": var_name,
                        "type": "path",
                        "extraction_rule": "url.path",
                        "sample_values": [],
                        "description": f"Path variable: {var_name}",
                        "from_flow_id": flow.get("id", 0),
                    }
                # 尝试从其他 flow 中提取实际值
                if len(detected_vars[var_name]["sample_values"]) < 3:
                    for other_flow in flows:
                        if other_flow.get("id") != flow.get("id"):
                            extracted = _extract_variable_from_flow(
                                other_flow,
                                {"field": "url", "pattern": rf'/{var_name}/([^/\s]+)', "type": "regex", "name": var_name}
                            )
                            if extracted and extracted not in detected_vars[var_name]["sample_values"]:
                                detected_vars[var_name]["sample_values"].append(extracted)

        # 从 query 提取变量
        for match in query_pattern.finditer(url):
            var_name = match.group(1)
            var_value = match.group(2)
            if var_name and var_value:
                if var_name not in detected_vars:
                    detected_vars[var_name] = {
                        "name": var_name,
                        "type": "query",
                        "extraction_rule": "url.query",
                        "sample_values": [],
                        "description": f"Query parameter: {var_name}",
                        "from_flow_id": flow.get("id", 0),
                    }
                if var_value not in detected_vars[var_name]["sample_values"]:
                    detected_vars[var_name]["sample_values"].append(var_value)

        # 从 JSON body 提取变量
        try:
            if body_str:
                body_data = json.loads(body_str) if isinstance(body_str, str) else body_str
                if isinstance(body_data, dict):
                    for key, value in body_data.items():
                        if key not in detected_vars:
                            detected_vars[key] = {
                                "name": key,
                                "type": "body",
                                "extraction_rule": f"body.{key}",
                                "sample_values": [],
                                "description": f"Request body field: {key}",
                                "from_flow_id": flow.get("id", 0),
                            }
                        if isinstance(value, (str, int, float, bool)):
                            val_str = str(value)
                            if val_str not in detected_vars[key]["sample_values"]:
                                detected_vars[key]["sample_values"].append(val_str)
        except Exception:  # noqa: BLE001
            pass

    variables = list(detected_vars.values())
    # 保存到脚本变量定义
    _save_script_variables(script_id, variables)
    return ok({"variables": variables, "count": len(variables)})


@router.get("/record-scripts/environments")
async def list_environments():
    """获取所有环境配置。"""
    return ok(_get_environments())


@router.post("/record-scripts/environments")
async def create_environment(body: EnvironmentCreate):
    """创建新环境。"""
    environments = _get_environments()

    # 检查名称唯一性
    if any(e.get("name") == body.name for e in environments):
        return err("Environment name already exists")

    # 如果设为默认，取消其他默认
    if body.is_default:
        for e in environments:
            e["is_default"] = False

    env = {
        "id": body.id or uuid.uuid4().hex[:8],
        "name": body.name,
        "description": body.description,
        "variables": body.variables,
        "is_default": body.is_default,
    }
    environments.append(env)
    _save_environments(environments)
    return ok(env)


@router.put("/record-scripts/environments/{env_id}")
async def update_environment(env_id: str, body: EnvironmentCreate):
    """更新环境配置。"""
    environments = _get_environments()
    idx = -1
    for i, e in enumerate(environments):
        if e.get("id") == env_id:
            idx = i
            break

    if idx < 0:
        return err("Environment not found")

    # 如果设为默认，取消其他默认
    if body.is_default:
        for e in environments:
            e["is_default"] = False

    environments[idx] = {
        "id": env_id,
        "name": body.name,
        "description": body.description,
        "variables": body.variables,
        "is_default": body.is_default,
    }
    _save_environments(environments)
    return ok(environments[idx])


@router.delete("/record-scripts/environments/{env_id}")
async def delete_environment(env_id: str):
    """删除环境。"""
    environments = _get_environments()
    new_envs = [e for e in environments if e.get("id") != env_id]
    if len(new_envs) == len(environments):
        return err("Environment not found")
    _save_environments(new_envs)
    return ok({"id": env_id})


# ---------- 响应断言 ----------

class AssertionRule(BaseModel):
    """断言规则。"""
    id: str | None = None
    name: str
    field: str  # status_code | response_body | response_header | request_header
    operator: str  # equals | contains | regex | greater | less
    expected_value: str
    enabled: bool = True
    note: str = ""


@router.get("/record-scripts/assertions")
async def list_assertions():
    """获取所有断言规则。"""
    data = settings_store.get_setting(_ASSERTIONS_KEY, [])
    return ok(data if isinstance(data, list) else [])


@router.post("/record-scripts/assertions")
async def create_assertion(body: AssertionRule):
    """创建断言规则。"""
    data = settings_store.get_setting(_ASSERTIONS_KEY, [])
    assertions = data if isinstance(data, list) else []
    assertion = {
        "id": body.id or uuid.uuid4().hex[:8],
        "name": body.name,
        "field": body.field,
        "operator": body.operator,
        "expected_value": body.expected_value,
        "enabled": body.enabled,
        "note": body.note,
    }
    assertions.append(assertion)
    settings_store.set_setting(_ASSERTIONS_KEY, assertions)
    return ok(assertion)


@router.put("/record-scripts/assertions/{assertion_id}")
async def update_assertion(assertion_id: str, body: AssertionRule):
    """更新断言规则。"""
    data = settings_store.get_setting(_ASSERTIONS_KEY, [])
    assertions = data if isinstance(data, list) else []
    for i, a in enumerate(assertions):
        if a.get("id") == assertion_id:
            assertions[i] = {
                "id": assertion_id,
                "name": body.name,
                "field": body.field,
                "operator": body.operator,
                "expected_value": body.expected_value,
                "enabled": body.enabled,
                "note": body.note,
            }
            settings_store.set_setting(_ASSERTIONS_KEY, assertions)
            return ok(assertions[i])
    return err("Assertion not found")


@router.delete("/record-scripts/assertions/{assertion_id}")
async def delete_assertion(assertion_id: str):
    """删除断言规则。"""
    data = settings_store.get_setting(_ASSERTIONS_KEY, [])
    assertions = data if isinstance(data, list) else []
    new_assertions = [a for a in assertions if a.get("id") != assertion_id]
    if len(new_assertions) == len(assertions):
        return err("Assertion not found")
    settings_store.set_setting(_ASSERTIONS_KEY, new_assertions)
    return ok({"id": assertion_id})


def _check_assertion(assertion: dict, response_data: dict) -> tuple[bool, str]:
    """检查单条断言是否通过。"""
    field = assertion.get("field", "")
    operator = assertion.get("operator", "")
    expected = assertion.get("expected_value", "")

    # 获取字段值
    actual = ""
    if field == "status_code":
        actual = str(response_data.get("status_code", ""))
    elif field == "response_body":
        actual = str(response_data.get("response_body", ""))
    elif field.startswith("response_header."):
        header_name = field.replace("response_header.", "")
        headers = response_data.get("response_headers", {})
        actual = str(headers.get(header_name, ""))
    elif field.startswith("request_header."):
        header_name = field.replace("request_header.", "")
        headers = response_data.get("request_headers", {})
        actual = str(headers.get(header_name, ""))

    # 比较
    passed = False
    if operator == "equals":
        passed = actual == expected
    elif operator == "contains":
        passed = expected in actual
    elif operator == "regex":
        passed = bool(re.search(expected, actual))
    elif operator == "greater":
        try:
            passed = float(actual) > float(expected)
        except (ValueError, TypeError):
            passed = False
    elif operator == "less":
        try:
            passed = float(actual) < float(expected)
        except (ValueError, TypeError):
            passed = False

    return passed, f"{field} {operator} {expected}: got {actual}"


# ---------- 条件执行规则 ----------

class ConditionRule(BaseModel):
    """条件执行规则。"""
    id: str | None = None
    name: str
    condition_type: str  # status_code | response_body | response_header | request_header
    operator: str  # equals | contains | regex | greater | less
    value: str
    action: str  # skip | delay | mock | abort
    action_params: dict = {}
    enabled: bool = True


def _get_condition_rules(script_id: str) -> list[dict]:
    """获取脚本的条件执行规则。"""
    data = settings_store.get_setting(f"{_CONDITIONS_KEY}_{script_id}", [])
    return data if isinstance(data, list) else []


def _save_condition_rules(script_id: str, rules: list[dict]) -> None:
    """保存脚本的条件执行规则。"""
    settings_store.set_setting(f"{_CONDITIONS_KEY}_{script_id}", rules)


@router.get("/record-scripts/{script_id}/condition-rules")
async def list_condition_rules(script_id: str):
    """获取脚本的条件执行规则。"""
    return ok(_get_condition_rules(script_id))


@router.post("/record-scripts/{script_id}/condition-rules")
async def create_condition_rule(script_id: str, body: ConditionRule):
    """创建条件执行规则。"""
    rules = _get_condition_rules(script_id)
    rule = {
        "id": body.id or uuid.uuid4().hex[:8],
        "name": body.name,
        "condition_type": body.condition_type,
        "operator": body.operator,
        "value": body.value,
        "action": body.action,
        "action_params": body.action_params,
        "enabled": body.enabled,
    }
    rules.append(rule)
    _save_condition_rules(script_id, rules)
    return ok(rule)


@router.put("/record-scripts/{script_id}/condition-rules/{rule_id}")
async def update_condition_rule(script_id: str, rule_id: str, body: ConditionRule):
    """更新条件执行规则。"""
    rules = _get_condition_rules(script_id)
    for i, r in enumerate(rules):
        if r.get("id") == rule_id:
            rules[i] = {
                "id": rule_id,
                "name": body.name,
                "condition_type": body.condition_type,
                "operator": body.operator,
                "value": body.value,
                "action": body.action,
                "action_params": body.action_params,
                "enabled": body.enabled,
            }
            _save_condition_rules(script_id, rules)
            return ok(rules[i])
    return err("Condition rule not found")


@router.delete("/record-scripts/{script_id}/condition-rules/{rule_id}")
async def delete_condition_rule(script_id: str, rule_id: str):
    """删除条件执行规则。"""
    rules = _get_condition_rules(script_id)
    new_rules = [r for r in rules if r.get("id") != rule_id]
    if len(new_rules) == len(rules):
        return err("Condition rule not found")
    _save_condition_rules(script_id, new_rules)
    return ok({"id": rule_id})


def _check_condition(rule: dict, response_data: dict) -> tuple[bool, str]:
    """检查条件是否满足。"""
    cond_type = rule.get("condition_type", "")
    operator = rule.get("operator", "")
    expected = rule.get("value", "")

    # 获取条件值
    actual = ""
    if cond_type == "status_code":
        actual = str(response_data.get("status_code", ""))
    elif cond_type == "response_body":
        actual = str(response_data.get("response_body", ""))
    elif cond_type.startswith("response_header."):
        header_name = cond_type.replace("response_header.", "")
        headers = response_data.get("response_headers", {})
        actual = str(headers.get(header_name, ""))
    elif cond_type.startswith("request_header."):
        header_name = cond_type.replace("request_header.", "")
        headers = response_data.get("request_headers", {})
        actual = str(headers.get(header_name, ""))

    # 比较
    matched = False
    if operator == "equals":
        matched = actual == expected
    elif operator == "contains":
        matched = expected in actual
    elif operator == "regex":
        matched = bool(re.search(expected, actual))
    elif operator == "greater":
        try:
            matched = float(actual) > float(expected)
        except (ValueError, TypeError):
            matched = False
    elif operator == "less":
        try:
            matched = float(actual) < float(expected)
        except (ValueError, TypeError):
            matched = False

    return matched, rule.get("action", "")


# ---------- 增强回放：支持变量替换、环境、断言、条件执行 ----------

def _substitute_template(text: str, variables: dict[str, str]) -> str:
    """在文本中替换变量占位符。"""
    if not text or not variables:
        return text

    result = text
    for key, value in variables.items():
        # {{variable}} 风格
        result = result.replace(f"{{{{{key}}}}}", str(value))
        # ${variable} 风格
        result = result.replace(f"${{{key}}}", str(value))
        # $variable 风格
        result = result.replace(f"${key}", str(value))

    return result


def _substitute_headers(headers: dict, variables: dict[str, str]) -> dict:
    """替换请求头中的变量。"""
    result = {}
    for key, value in headers.items():
        result[_substitute_template(str(key), variables)] = _substitute_template(str(value), variables)
    return result


async def _replay_flows_enhanced(
    flows: list[dict],
    environment_id: str | None = None,
    apply_assertions: bool = True,
    apply_conditions: bool = True,
) -> dict:
    """增强版回放：支持变量替换、环境、断言和条件执行。"""
    try:
        import httpx
    except ImportError:  # noqa: BLE001
        raise RuntimeError("httpx is not installed, replay unavailable")

    # 获取环境变量
    env_vars: dict[str, str] = {}
    if environment_id:
        environments = _get_environments()
        for env in environments:
            if env.get("id") == environment_id:
                env_vars = env.get("variables", {})
                break

    # 获取断言规则
    assertions = settings_store.get_setting(_ASSERTIONS_KEY, []) if apply_assertions else []
    if not isinstance(assertions, list):
        assertions = []

    # 仅重放合法 HTTP(S) 流量
    http_flows = [f for f in flows if (f.get("url") or "").lower().startswith("http")]

    results: list[dict] = []
    success = 0
    fail = 0
    skipped = 0
    assertion_fails = 0
    status_dist: dict[str, int] = {}
    sem = asyncio.Semaphore(10)

    async with httpx.AsyncClient(timeout=30.0, verify=False, follow_redirects=False, proxy=None) as client:

        async def _one(flow: dict):
            nonlocal success, fail, skipped, assertion_fails

            async with sem:
                t0 = time.time()

                # 变量替换
                url = _substitute_template(flow.get("url") or "", env_vars)
                headers = _safe_json(flow.get("request_headers"))
                headers = _substitute_headers(headers, env_vars)
                # 移除 hop-by-hop / Host 头
                for k in list(headers.keys()):
                    if k.lower() in ("host", "content-length", "transfer-encoding",
                                      "connection", "keep-alive"):
                        del headers[k]
                body = _substitute_template(flow.get("request_body") or "", env_vars)
                content = _to_bytes(body)

                method = _substitute_template((flow.get("method") or "GET").upper(), env_vars)

                try:
                    resp = await client.request(method, url, headers=headers, content=content)
                    dt = (time.time() - t0) * 1000.0

                    response_data = {
                        "status_code": resp.status_code,
                        "response_body": resp.text,
                        "response_headers": dict(resp.headers),
                        "request_headers": headers,
                    }

                    # 检查断言
                    assertion_passed = True
                    assertion_results = []
                    if assertions:
                        for assertion in assertions:
                            if not assertion.get("enabled", True):
                                continue
                            passed, msg = _check_assertion(assertion, response_data)
                            assertion_results.append({"assertion": assertion.get("name"), "passed": passed, "message": msg})
                            if not passed:
                                assertion_passed = False
                                assertion_fails += 1

                    result_entry = {
                        "flow_id": flow.get("id"),
                        "url": url,
                        "method": method,
                        "status_code": resp.status_code,
                        "duration_ms": round(dt, 2),
                        "ok": assertion_passed,
                        "assertions": assertion_results,
                    }

                    # 检查条件执行
                    if apply_conditions:
                        for rule in assertions:  # TODO: 需要传入条件规则
                            matched, action = _check_condition(rule, response_data)
                            if matched:
                                if action == "skip":
                                    result_entry["skipped"] = True
                                    skipped += 1
                                    results.append(result_entry)
                                    return
                                elif action == "delay":
                                    delay_ms = rule.get("action_params", {}).get("delay_ms", 1000)
                                    await asyncio.sleep(delay_ms / 1000.0)

                    results.append(result_entry)

                    if assertion_passed:
                        success += 1
                        sc = str(resp.status_code)
                        status_dist[sc] = status_dist.get(sc, 0) + 1
                    else:
                        fail += 1

                except Exception as e:  # noqa: BLE001
                    dt = (time.time() - t0) * 1000.0
                    results.append({
                        "flow_id": flow.get("id"),
                        "url": url,
                        "method": method,
                        "error": str(e),
                        "duration_ms": round(dt, 2),
                        "ok": False,
                    })
                    fail += 1

        await asyncio.gather(*[_one(f) for f in http_flows], return_exceptions=True)

    return {
        "total": len(flows),
        "replayed": len(http_flows),
        "skipped": skipped,
        "success": success,
        "fail": fail,
        "assertion_fails": assertion_fails,
        "status_distribution": status_dist,
        "results": results,
    }


@router.post("/record-scripts/{script_id}/replay-enhanced")
async def replay_script_enhanced(script_id: str, environment_id: str | None = None):
    """增强回放：支持变量替换、环境、断言和条件执行。"""
    scripts = _get_all_scripts_with_flows()
    s = _find_script(scripts, script_id)
    if not s:
        return err("Record script not found")
    flows = s.get("flows") or []
    if not flows:
        return err("Script has no flows to replay")
    try:
        stats = await _replay_flows_enhanced(flows, environment_id)
        return ok(stats)
    except Exception as e:  # noqa: BLE001
        return err(f"Enhanced replay failed: {e}")


# ---------- 录制 → Mock 自举 ----------

class ConvertToMockRequest(BaseModel):
    """转换到 Mock 的请求。"""
    flow_ids: list[int]
    use_template: bool = True  # 是否使用模板变量
    add_delay: bool = False
    delay_ms: int = 0


@router.post("/record-scripts/convert-to-mock")
async def convert_to_mock(body: ConvertToMockRequest):
    """将录制的 flows 转换为 Mock 规则。"""
    flows_map = db.get_flows_by_ids(body.flow_ids) if body.flow_ids else {}
    flows = [flows_map[fid] for fid in body.flow_ids if fid in flows_map]

    mock_rules = []
    for flow in flows:
        url = flow.get("url", "")
        method = (flow.get("method") or "GET").upper()

        # 提取 path
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
        except Exception:  # noqa: BLE001
            path = "/"

        # 响应头
        response_headers = {}
        try:
            raw_headers = flow.get("response_headers")
            if raw_headers:
                response_headers = json.loads(raw_headers) if isinstance(raw_headers, str) else raw_headers
        except Exception:  # noqa: BLE001
            pass

        # 响应体
        response_body = flow.get("response_body") or ""
        content_type = response_headers.get("Content-Type", "application/json") if isinstance(response_headers, dict) else "application/json"

        # 如果使用模板，替换变量占位符
        if body.use_template:
            # 检测可能的变量
            path = re.sub(r'/[a-f0-9-]{36}(?=/|$)', '/{{id}}', path)
            path = re.sub(r'/(\d+)(?=/|$)', '/{{id}}', path)
            response_body = re.sub(r'"id"\s*:\s*"?(\d+|[\w-]+)"?', '"id": "{{id}}"', response_body)

        rule = {
            "id": uuid.uuid4().hex[:8],
            "enabled": True,
            "method": method,
            "path": path,
            "match_mode": "exact",
            "status_code": flow.get("status_code") or 200,
            "headers": response_headers,
            "body": response_body,
            "content_type": content_type,
            "delay_ms": body.delay_ms if body.add_delay else 0,
            "note": f"Converted from flow {flow.get('id')}",
            "template_mode": body.use_template,
        }
        mock_rules.append(rule)

    # 保存到 mock 规则
    try:
        mock_rules_data = settings_store.get_setting("mock_rules", [])
        if not isinstance(mock_rules_data, list):
            mock_rules_data = []
        mock_rules_data.extend(mock_rules)
        settings_store.set_setting("mock_rules", mock_rules_data)
    except Exception as e:  # noqa: BLE001
        return err(f"Failed to save mock rules: {e}")

    return ok({"rules": mock_rules, "count": len(mock_rules)})


@router.get("/record-scripts/recording-status")
async def recording_status():
    """查询当前录制状态及已录制 flow 数量。

    注意：此路由必须声明在 /record-scripts/{script_id} 之前，
    否则 FastAPI 会将 "recording-status" 当作 script_id 参数匹配。
    """
    return ok(_recording_snapshot())


@router.get("/record-scripts/{script_id}")
async def get_script(script_id: str):
    """获取脚本详情（含 flows 数据）。"""
    scripts = _get_all_scripts_with_flows()
    s = _find_script(scripts, script_id)
    if not s:
        return err("Record script not found")
    return ok(s)


@router.delete("/record-scripts/{script_id}")
async def delete_script(script_id: str):
    """删除脚本。"""
    scripts = _get_all_scripts_with_flows()
    new_scripts = [s for s in scripts if s.get("id") != script_id]
    if len(new_scripts) == len(scripts):
        return err("Record script not found")
    _save_scripts(new_scripts)
    return ok({"id": script_id})


@router.post("/record-scripts/{script_id}/replay")
async def replay_script(script_id: str):
    """回放脚本：并发重放脚本中的 flows，返回统计结果。"""
    scripts = _get_all_scripts_with_flows()
    s = _find_script(scripts, script_id)
    if not s:
        return err("Record script not found")
    flows = s.get("flows") or []
    if not flows:
        return err("Script has no flows to replay")
    try:
        stats = await _replay_flows(flows)
        return ok(stats)
    except Exception as e:  # noqa: BLE001
        return err(f"Replay failed: {e}")


@router.post("/record-scripts/start-recording")
async def start_recording():
    """开始录制：返回 recording_id，后续新 flow 由代理层自动加入。"""
    with _recording_lock:
        if _recording_state["active"]:
            return err("Already recording", data=_recording_snapshot())
        recording_id = uuid.uuid4().hex[:8]
        _recording_state["active"] = True
        _recording_state["recording_id"] = recording_id
        _recording_state["session_start"] = datetime.now().isoformat()
        _recording_state["flow_ids"] = set()
    return ok({"recording_id": recording_id, "status": _recording_snapshot()})


@router.post("/record-scripts/stop-recording")
async def stop_recording(body: ScriptStopRecord | None = None):
    """停止录制：把录制期间的 flow_ids 快照为脚本并保存。"""
    with _recording_lock:
        if not _recording_state["active"]:
            return err("Not recording")
        flow_ids = sorted(_recording_state["flow_ids"])
        recording_id = _recording_state["recording_id"]
        session_start = _recording_state["session_start"]
        # 状态先保留，仅在保存成功后重置；保存失败时用户可重试

    # 从 flows 表快照完整数据
    try:
        flows_map = db.get_flows_by_ids(flow_ids) if flow_ids else {}
        flows = [flows_map[fid] for fid in flow_ids if fid in flows_map]
        name = (body.name if body and body.name else f"Recording {recording_id}")
        note = (body.note if body else "") or f"session_start={session_start}"
        script = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "created_at": datetime.now().isoformat(),
            "flow_ids": list(flow_ids),
            "note": note,
            "flows": flows,
        }
        scripts = _get_all_scripts_with_flows()
        scripts.append(script)
        _save_scripts(scripts)
    except Exception:
        # 保存失败：保持录制状态不变，用户可重试 stop-recording
        raise

    # 保存成功后再重置状态
    with _recording_lock:
        _recording_state["active"] = False
        _recording_state["recording_id"] = None
        _recording_state["session_start"] = None
        # 只移除本次保存的 flow_ids，保留保存期间新到达的 flow
        _recording_state["flow_ids"].difference_update(flow_ids)

    out = {k: v for k, v in script.items() if k != "flows"}
    out["flow_count"] = len(flows)
    return ok(out)


# ---------- 变量提取与多环境配置 ----------

class VariableExtractionRequest(BaseModel):
    """从 flows 提取变量的请求。"""
    flow_ids: list[int]
    extraction_rules: list[dict] = []  # [{name, field, pattern}]


class EnvironmentCreate(BaseModel):
    """创建/更新环境。"""
    id: str | None = None
    name: str
    description: str = ""
    variables: dict[str, str] = {}
    is_default: bool = False


class ScriptVariable(BaseModel):
    """脚本变量定义。"""
    name: str
    description: str = ""
    type: str = "string"  # string | number | boolean | json_path | regex
    extraction_rule: str = ""  # field.path 或 regex pattern
    sample_values: list[str] = []
    from_flow_id: int = 0


def _get_script_variables(script_id: str) -> list[dict]:
    """获取脚本的变量定义。"""
    data = settings_store.get_setting(f"{_VARIABLES_KEY}_{script_id}", [])
    return data if isinstance(data, list) else []


def _save_script_variables(script_id: str, variables: list[dict]) -> None:
    """保存脚本变量定义。"""
    settings_store.set_setting(f"{_VARIABLES_KEY}_{script_id}", variables)


def _get_environments() -> list[dict]:
    """获取所有环境配置。"""
    data = settings_store.get_setting(_ENVIRONMENTS_KEY, [])
    return data if isinstance(data, list) else []


def _save_environments(environments: list[dict]) -> None:
    """保存环境配置列表。"""
    settings_store.set_setting(_ENVIRONMENTS_KEY, environments)


def _extract_variable_from_flow(flow: dict, rule: dict) -> str | None:
    """根据提取规则从 flow 中提取变量值。"""
    field = rule.get("field", "")
    pattern = rule.get("pattern", "")
    name = rule.get("name", "")

    if not field or not name:
        return None

    # 获取字段值
    value = flow
    for part in field.split("."):
        if value is None:
            return None
        value = value.get(part) if isinstance(value, dict) else None

    if value is None:
        return None

    value_str = str(value)

    # 根据类型提取
    var_type = rule.get("type", "string")
    if var_type == "regex" and pattern:
        match = re.search(pattern, value_str)
        if match:
            return match.group(1) if match.lastindex else match.group(0)
    elif var_type == "json_path" and pattern:
        # 简单的 JSONPath 提取（支持 .key 和 .key[0]）
        try:
            data = json.loads(value_str) if isinstance(value, str) else value
            parts = pattern.lstrip(".").split(".")
            for p in parts:
                if data is None:
                    return None
                if p.endswith("]"):
                    key, idx = p[:-1], int(p[p.index("[") + 1:-1])
                    data = data.get(key)
                    if isinstance(data, list) and 0 <= idx < len(data):
                        data = data[idx]
                    else:
                        return None
                else:
                    data = data.get(p)
            return str(data) if data is not None else None
        except Exception:  # noqa: BLE001
            return None
    else:
        return value_str

    return None


@router.get("/record-scripts/{script_id}/variables")
async def get_script_variables(script_id: str):
    """获取脚本的变量定义。"""
    variables = _get_script_variables(script_id)
    return ok(variables)


@router.post("/record-scripts/{script_id}/variables/extract")
async def extract_variables(script_id: str, body: VariableExtractionRequest):
    """从 flows 中自动提取变量。"""
    flows_map = db.get_flows_by_ids(body.flow_ids) if body.flow_ids else {}
    flows = [flows_map[fid] for fid in body.flow_ids if fid in flows_map]

    # 自动检测可提取的变量
    detected_vars: dict[str, dict] = {}

    # 内置提取规则：从 URL path、query、body 中检测参数（使用预编译正则）
    path_pattern = _PATH_PATTERN
    query_pattern = _QUERY_PATTERN

    for flow in flows:
        url = flow.get("url", "")
        body_str = flow.get("request_body") or ""

        # 从 path 提取变量
        for match in path_pattern.finditer(url):
            var_name = match.group(1) or match.group(2)
            if var_name:
                if var_name not in detected_vars:
                    detected_vars[var_name] = {
                        "name": var_name,
                        "type": "path",
                        "extraction_rule": "url.path",
                        "sample_values": [],
                        "description": f"Path variable: {var_name}",
                        "from_flow_id": flow.get("id", 0),
                    }
                # 尝试从其他 flow 中提取实际值
                if len(detected_vars[var_name]["sample_values"]) < 3:
                    for other_flow in flows:
                        if other_flow.get("id") != flow.get("id"):
                            extracted = _extract_variable_from_flow(
                                other_flow,
                                {"field": "url", "pattern": rf'/{var_name}/([^/\s]+)', "type": "regex", "name": var_name}
                            )
                            if extracted and extracted not in detected_vars[var_name]["sample_values"]:
                                detected_vars[var_name]["sample_values"].append(extracted)

        # 从 query 提取变量
        for match in query_pattern.finditer(url):
            var_name = match.group(1)
            var_value = match.group(2)
            if var_name and var_value:
                if var_name not in detected_vars:
                    detected_vars[var_name] = {
                        "name": var_name,
                        "type": "query",
                        "extraction_rule": "url.query",
                        "sample_values": [],
                        "description": f"Query parameter: {var_name}",
                        "from_flow_id": flow.get("id", 0),
                    }
                if var_value not in detected_vars[var_name]["sample_values"]:
                    detected_vars[var_name]["sample_values"].append(var_value)

        # 从 JSON body 提取变量
        try:
            if body_str:
                body_data = json.loads(body_str) if isinstance(body_str, str) else body_str
                if isinstance(body_data, dict):
                    for key, value in body_data.items():
                        if key not in detected_vars:
                            detected_vars[key] = {
                                "name": key,
                                "type": "body",
                                "extraction_rule": f"body.{key}",
                                "sample_values": [],
                                "description": f"Request body field: {key}",
                                "from_flow_id": flow.get("id", 0),
                            }
                        if isinstance(value, (str, int, float, bool)):
                            val_str = str(value)
                            if val_str not in detected_vars[key]["sample_values"]:
                                detected_vars[key]["sample_values"].append(val_str)
        except Exception:  # noqa: BLE001
            pass

    variables = list(detected_vars.values())
    # 保存到脚本变量定义
    _save_script_variables(script_id, variables)
    return ok({"variables": variables, "count": len(variables)})


@router.get("/record-scripts/environments")
async def list_environments():
    """获取所有环境配置。"""
    return ok(_get_environments())


@router.post("/record-scripts/environments")
async def create_environment(body: EnvironmentCreate):
    """创建新环境。"""
    environments = _get_environments()

    # 检查名称唯一性
    if any(e.get("name") == body.name for e in environments):
        return err("Environment name already exists")

    # 如果设为默认，取消其他默认
    if body.is_default:
        for e in environments:
            e["is_default"] = False

    env = {
        "id": body.id or uuid.uuid4().hex[:8],
        "name": body.name,
        "description": body.description,
        "variables": body.variables,
        "is_default": body.is_default,
    }
    environments.append(env)
    _save_environments(environments)
    return ok(env)


@router.put("/record-scripts/environments/{env_id}")
async def update_environment(env_id: str, body: EnvironmentCreate):
    """更新环境配置。"""
    environments = _get_environments()
    idx = -1
    for i, e in enumerate(environments):
        if e.get("id") == env_id:
            idx = i
            break

    if idx < 0:
        return err("Environment not found")

    # 如果设为默认，取消其他默认
    if body.is_default:
        for e in environments:
            e["is_default"] = False

    environments[idx] = {
        "id": env_id,
        "name": body.name,
        "description": body.description,
        "variables": body.variables,
        "is_default": body.is_default,
    }
    _save_environments(environments)
    return ok(environments[idx])


@router.delete("/record-scripts/environments/{env_id}")
async def delete_environment(env_id: str):
    """删除环境。"""
    environments = _get_environments()
    new_envs = [e for e in environments if e.get("id") != env_id]
    if len(new_envs) == len(environments):
        return err("Environment not found")
    _save_environments(new_envs)
    return ok({"id": env_id})


# ---------- 响应断言 ----------

class AssertionRule(BaseModel):
    """断言规则。"""
    id: str | None = None
    name: str
    field: str  # status_code | response_body | response_header | request_header
    operator: str  # equals | contains | regex | greater | less
    expected_value: str
    enabled: bool = True
    note: str = ""


@router.get("/record-scripts/assertions")
async def list_assertions():
    """获取所有断言规则。"""
    data = settings_store.get_setting(_ASSERTIONS_KEY, [])
    return ok(data if isinstance(data, list) else [])


@router.post("/record-scripts/assertions")
async def create_assertion(body: AssertionRule):
    """创建断言规则。"""
    data = settings_store.get_setting(_ASSERTIONS_KEY, [])
    assertions = data if isinstance(data, list) else []
    assertion = {
        "id": body.id or uuid.uuid4().hex[:8],
        "name": body.name,
        "field": body.field,
        "operator": body.operator,
        "expected_value": body.expected_value,
        "enabled": body.enabled,
        "note": body.note,
    }
    assertions.append(assertion)
    settings_store.set_setting(_ASSERTIONS_KEY, assertions)
    return ok(assertion)


@router.put("/record-scripts/assertions/{assertion_id}")
async def update_assertion(assertion_id: str, body: AssertionRule):
    """更新断言规则。"""
    data = settings_store.get_setting(_ASSERTIONS_KEY, [])
    assertions = data if isinstance(data, list) else []
    for i, a in enumerate(assertions):
        if a.get("id") == assertion_id:
            assertions[i] = {
                "id": assertion_id,
                "name": body.name,
                "field": body.field,
                "operator": body.operator,
                "expected_value": body.expected_value,
                "enabled": body.enabled,
                "note": body.note,
            }
            settings_store.set_setting(_ASSERTIONS_KEY, assertions)
            return ok(assertions[i])
    return err("Assertion not found")


@router.delete("/record-scripts/assertions/{assertion_id}")
async def delete_assertion(assertion_id: str):
    """删除断言规则。"""
    data = settings_store.get_setting(_ASSERTIONS_KEY, [])
    assertions = data if isinstance(data, list) else []
    new_assertions = [a for a in assertions if a.get("id") != assertion_id]
    if len(new_assertions) == len(assertions):
        return err("Assertion not found")
    settings_store.set_setting(_ASSERTIONS_KEY, new_assertions)
    return ok({"id": assertion_id})


def _check_assertion(assertion: dict, response_data: dict) -> tuple[bool, str]:
    """检查单条断言是否通过。"""
    field = assertion.get("field", "")
    operator = assertion.get("operator", "")
    expected = assertion.get("expected_value", "")

    # 获取字段值
    actual = ""
    if field == "status_code":
        actual = str(response_data.get("status_code", ""))
    elif field == "response_body":
        actual = str(response_data.get("response_body", ""))
    elif field.startswith("response_header."):
        header_name = field.replace("response_header.", "")
        headers = response_data.get("response_headers", {})
        actual = str(headers.get(header_name, ""))
    elif field.startswith("request_header."):
        header_name = field.replace("request_header.", "")
        headers = response_data.get("request_headers", {})
        actual = str(headers.get(header_name, ""))

    # 比较
    passed = False
    if operator == "equals":
        passed = actual == expected
    elif operator == "contains":
        passed = expected in actual
    elif operator == "regex":
        passed = bool(re.search(expected, actual))
    elif operator == "greater":
        try:
            passed = float(actual) > float(expected)
        except (ValueError, TypeError):
            passed = False
    elif operator == "less":
        try:
            passed = float(actual) < float(expected)
        except (ValueError, TypeError):
            passed = False

    return passed, f"{field} {operator} {expected}: got {actual}"


# ---------- 条件执行规则 ----------

class ConditionRule(BaseModel):
    """条件执行规则。"""
    id: str | None = None
    name: str
    condition_type: str  # status_code | response_body | response_header | request_header
    operator: str  # equals | contains | regex | greater | less
    value: str
    action: str  # skip | delay | mock | abort
    action_params: dict = {}
    enabled: bool = True


def _get_condition_rules(script_id: str) -> list[dict]:
    """获取脚本的条件执行规则。"""
    data = settings_store.get_setting(f"{_CONDITIONS_KEY}_{script_id}", [])
    return data if isinstance(data, list) else []


def _save_condition_rules(script_id: str, rules: list[dict]) -> None:
    """保存脚本的条件执行规则。"""
    settings_store.set_setting(f"{_CONDITIONS_KEY}_{script_id}", rules)


@router.get("/record-scripts/{script_id}/condition-rules")
async def list_condition_rules(script_id: str):
    """获取脚本的条件执行规则。"""
    return ok(_get_condition_rules(script_id))


@router.post("/record-scripts/{script_id}/condition-rules")
async def create_condition_rule(script_id: str, body: ConditionRule):
    """创建条件执行规则。"""
    rules = _get_condition_rules(script_id)
    rule = {
        "id": body.id or uuid.uuid4().hex[:8],
        "name": body.name,
        "condition_type": body.condition_type,
        "operator": body.operator,
        "value": body.value,
        "action": body.action,
        "action_params": body.action_params,
        "enabled": body.enabled,
    }
    rules.append(rule)
    _save_condition_rules(script_id, rules)
    return ok(rule)


@router.put("/record-scripts/{script_id}/condition-rules/{rule_id}")
async def update_condition_rule(script_id: str, rule_id: str, body: ConditionRule):
    """更新条件执行规则。"""
    rules = _get_condition_rules(script_id)
    for i, r in enumerate(rules):
        if r.get("id") == rule_id:
            rules[i] = {
                "id": rule_id,
                "name": body.name,
                "condition_type": body.condition_type,
                "operator": body.operator,
                "value": body.value,
                "action": body.action,
                "action_params": body.action_params,
                "enabled": body.enabled,
            }
            _save_condition_rules(script_id, rules)
            return ok(rules[i])
    return err("Condition rule not found")


@router.delete("/record-scripts/{script_id}/condition-rules/{rule_id}")
async def delete_condition_rule(script_id: str, rule_id: str):
    """删除条件执行规则。"""
    rules = _get_condition_rules(script_id)
    new_rules = [r for r in rules if r.get("id") != rule_id]
    if len(new_rules) == len(rules):
        return err("Condition rule not found")
    _save_condition_rules(script_id, new_rules)
    return ok({"id": rule_id})


def _check_condition(rule: dict, response_data: dict) -> tuple[bool, str]:
    """检查条件是否满足。"""
    cond_type = rule.get("condition_type", "")
    operator = rule.get("operator", "")
    expected = rule.get("value", "")

    # 获取条件值
    actual = ""
    if cond_type == "status_code":
        actual = str(response_data.get("status_code", ""))
    elif cond_type == "response_body":
        actual = str(response_data.get("response_body", ""))
    elif cond_type.startswith("response_header."):
        header_name = cond_type.replace("response_header.", "")
        headers = response_data.get("response_headers", {})
        actual = str(headers.get(header_name, ""))
    elif cond_type.startswith("request_header."):
        header_name = cond_type.replace("request_header.", "")
        headers = response_data.get("request_headers", {})
        actual = str(headers.get(header_name, ""))

    # 比较
    matched = False
    if operator == "equals":
        matched = actual == expected
    elif operator == "contains":
        matched = expected in actual
    elif operator == "regex":
        matched = bool(re.search(expected, actual))
    elif operator == "greater":
        try:
            matched = float(actual) > float(expected)
        except (ValueError, TypeError):
            matched = False
    elif operator == "less":
        try:
            matched = float(actual) < float(expected)
        except (ValueError, TypeError):
            matched = False

    return matched, rule.get("action", "")


# ---------- 增强回放：支持变量替换、环境、断言、条件执行 ----------

def _substitute_template(text: str, variables: dict[str, str]) -> str:
    """在文本中替换变量占位符。"""
    if not text or not variables:
        return text

    result = text
    for key, value in variables.items():
        # {{variable}} 风格
        result = result.replace(f"{{{{{key}}}}}", str(value))
        # ${variable} 风格
        result = result.replace(f"${{{key}}}", str(value))
        # $variable 风格
        result = result.replace(f"${key}", str(value))

    return result


def _substitute_headers(headers: dict, variables: dict[str, str]) -> dict:
    """替换请求头中的变量。"""
    result = {}
    for key, value in headers.items():
        result[_substitute_template(str(key), variables)] = _substitute_template(str(value), variables)
    return result


async def _replay_flows_enhanced(
    flows: list[dict],
    environment_id: str | None = None,
    apply_assertions: bool = True,
    apply_conditions: bool = True,
) -> dict:
    """增强版回放：支持变量替换、环境、断言和条件执行。"""
    try:
        import httpx
    except ImportError:  # noqa: BLE001
        raise RuntimeError("httpx is not installed, replay unavailable")

    # 获取环境变量
    env_vars: dict[str, str] = {}
    if environment_id:
        environments = _get_environments()
        for env in environments:
            if env.get("id") == environment_id:
                env_vars = env.get("variables", {})
                break

    # 获取断言规则
    assertions = settings_store.get_setting(_ASSERTIONS_KEY, []) if apply_assertions else []
    if not isinstance(assertions, list):
        assertions = []

    # 仅重放合法 HTTP(S) 流量
    http_flows = [f for f in flows if (f.get("url") or "").lower().startswith("http")]

    results: list[dict] = []
    success = 0
    fail = 0
    skipped = 0
    assertion_fails = 0
    status_dist: dict[str, int] = {}
    sem = asyncio.Semaphore(10)

    async with httpx.AsyncClient(timeout=30.0, verify=False, follow_redirects=False, proxy=None) as client:

        async def _one(flow: dict):
            nonlocal success, fail, skipped, assertion_fails

            async with sem:
                t0 = time.time()

                # 变量替换
                url = _substitute_template(flow.get("url") or "", env_vars)
                headers = _safe_json(flow.get("request_headers"))
                headers = _substitute_headers(headers, env_vars)
                # 移除 hop-by-hop / Host 头
                for k in list(headers.keys()):
                    if k.lower() in ("host", "content-length", "transfer-encoding",
                                      "connection", "keep-alive"):
                        del headers[k]
                body = _substitute_template(flow.get("request_body") or "", env_vars)
                content = _to_bytes(body)

                method = _substitute_template((flow.get("method") or "GET").upper(), env_vars)

                try:
                    resp = await client.request(method, url, headers=headers, content=content)
                    dt = (time.time() - t0) * 1000.0

                    response_data = {
                        "status_code": resp.status_code,
                        "response_body": resp.text,
                        "response_headers": dict(resp.headers),
                        "request_headers": headers,
                    }

                    # 检查断言
                    assertion_passed = True
                    assertion_results = []
                    if assertions:
                        for assertion in assertions:
                            if not assertion.get("enabled", True):
                                continue
                            passed, msg = _check_assertion(assertion, response_data)
                            assertion_results.append({"assertion": assertion.get("name"), "passed": passed, "message": msg})
                            if not passed:
                                assertion_passed = False
                                assertion_fails += 1

                    result_entry = {
                        "flow_id": flow.get("id"),
                        "url": url,
                        "method": method,
                        "status_code": resp.status_code,
                        "duration_ms": round(dt, 2),
                        "ok": assertion_passed,
                        "assertions": assertion_results,
                    }

                    # 检查条件执行
                    if apply_conditions:
                        for rule in assertions:  # TODO: 需要传入条件规则
                            matched, action = _check_condition(rule, response_data)
                            if matched:
                                if action == "skip":
                                    result_entry["skipped"] = True
                                    skipped += 1
                                    results.append(result_entry)
                                    return
                                elif action == "delay":
                                    delay_ms = rule.get("action_params", {}).get("delay_ms", 1000)
                                    await asyncio.sleep(delay_ms / 1000.0)

                    results.append(result_entry)

                    if assertion_passed:
                        success += 1
                        sc = str(resp.status_code)
                        status_dist[sc] = status_dist.get(sc, 0) + 1
                    else:
                        fail += 1

                except Exception as e:  # noqa: BLE001
                    dt = (time.time() - t0) * 1000.0
                    results.append({
                        "flow_id": flow.get("id"),
                        "url": url,
                        "method": method,
                        "error": str(e),
                        "duration_ms": round(dt, 2),
                        "ok": False,
                    })
                    fail += 1

        await asyncio.gather(*[_one(f) for f in http_flows], return_exceptions=True)

    return {
        "total": len(flows),
        "replayed": len(http_flows),
        "skipped": skipped,
        "success": success,
        "fail": fail,
        "assertion_fails": assertion_fails,
        "status_distribution": status_dist,
        "results": results,
    }


@router.post("/record-scripts/{script_id}/replay-enhanced")
async def replay_script_enhanced(script_id: str, environment_id: str | None = None):
    """增强回放：支持变量替换、环境、断言和条件执行。"""
    scripts = _get_all_scripts_with_flows()
    s = _find_script(scripts, script_id)
    if not s:
        return err("Record script not found")
    flows = s.get("flows") or []
    if not flows:
        return err("Script has no flows to replay")
    try:
        stats = await _replay_flows_enhanced(flows, environment_id)
        return ok(stats)
    except Exception as e:  # noqa: BLE001
        return err(f"Enhanced replay failed: {e}")


# ---------- 录制 → Mock 自举 ----------

class ConvertToMockRequest(BaseModel):
    """转换到 Mock 的请求。"""
    flow_ids: list[int]
    use_template: bool = True  # 是否使用模板变量
    add_delay: bool = False
    delay_ms: int = 0


@router.post("/record-scripts/convert-to-mock")
async def convert_to_mock(body: ConvertToMockRequest):
    """将录制的 flows 转换为 Mock 规则。"""
    flows_map = db.get_flows_by_ids(body.flow_ids) if body.flow_ids else {}
    flows = [flows_map[fid] for fid in body.flow_ids if fid in flows_map]

    mock_rules = []
    for flow in flows:
        url = flow.get("url", "")
        method = (flow.get("method") or "GET").upper()

        # 提取 path
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
        except Exception:  # noqa: BLE001
            path = "/"

        # 响应头
        response_headers = {}
        try:
            raw_headers = flow.get("response_headers")
            if raw_headers:
                response_headers = json.loads(raw_headers) if isinstance(raw_headers, str) else raw_headers
        except Exception:  # noqa: BLE001
            pass

        # 响应体
        response_body = flow.get("response_body") or ""
        content_type = response_headers.get("Content-Type", "application/json") if isinstance(response_headers, dict) else "application/json"

        # 如果使用模板，替换变量占位符
        if body.use_template:
            # 检测可能的变量
            path = re.sub(r'/[a-f0-9-]{36}(?=/|$)', '/{{id}}', path)
            path = re.sub(r'/(\d+)(?=/|$)', '/{{id}}', path)
            response_body = re.sub(r'"id"\s*:\s*"?(\d+|[\w-]+)"?', '"id": "{{id}}"', response_body)

        rule = {
            "id": uuid.uuid4().hex[:8],
            "enabled": True,
            "method": method,
            "path": path,
            "match_mode": "exact",
            "status_code": flow.get("status_code") or 200,
            "headers": response_headers,
            "body": response_body,
            "content_type": content_type,
            "delay_ms": body.delay_ms if body.add_delay else 0,
            "note": f"Converted from flow {flow.get('id')}",
            "template_mode": body.use_template,
        }
        mock_rules.append(rule)

    # 保存到 mock 规则
    try:
        mock_rules_data = settings_store.get_setting("mock_rules", [])
        if not isinstance(mock_rules_data, list):
            mock_rules_data = []
        mock_rules_data.extend(mock_rules)
        settings_store.set_setting("mock_rules", mock_rules_data)
    except Exception as e:  # noqa: BLE001
        return err(f"Failed to save mock rules: {e}")

    return ok({"rules": mock_rules, "count": len(mock_rules)})

"""Telnix MCP Server —— 把 Telnix 的抓包/拦截/改包能力暴露给 MCP 客户端。

基于 MCP Python SDK (FastMCP)，通过 stdio 与 MCP 客户端通信。
复用 Telnix 后端 HTTP API（默认 http://127.0.0.1:18901），不直接操作数据库。

设计原则：
1. 工具返回 JSON 字符串（text content），错误也封装成 JSON 而非抛异常
2. 大输出自动截断（默认 64KB），防止 MCP 消息过大卡死客户端
3. 参数 schema 由 Python 类型注解 + docstring 自动生成
4. 不写 stdout（STDIO 协议约束），日志走 stderr
5. 纯 HTTP 调用，无本地文件/进程操作（除 agent_workspace 需要备份文件）

启动：
    python -m telnix.mcp_server
    python -m telnix.mcp_server --base-url http://127.0.0.1:18901
    set TELNIX_API=http://127.0.0.1:18901 && python -m telnix.mcp_server

MCP 客户端配置示例（claude_desktop_config.json）：
    {
      "mcpServers": {
        "telnix": {
          "command": "python",
          "args": ["-m", "telnix.mcp_server"],
          "cwd": ".\\src\\host",
          "env": { "TELNIX_API": "http://127.0.0.1:18901" }
        }
      }
    }
"""

from __future__ import annotations

import json
import os
import re
import functools
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

# MCP SDK
from mcp.server.fastmcp import FastMCP

# ---------- 配置 ----------

DEFAULT_HOST = "127.0.0.1"
try:
    from .config import DEFAULT_PORT as _CFG_PORT
    DEFAULT_PORT = _CFG_PORT
except Exception:  # noqa: BLE001
    DEFAULT_PORT = 18901
BASE_URL = os.environ.get("TELNIX_API", f"http://{DEFAULT_HOST}:{DEFAULT_PORT}").rstrip("/")

# 输出截断阈值（字节）。MCP 工具返回过大会让客户端卡死，64KB 是安全上限。
MAX_OUTPUT_BYTES = 64 * 1024
# 单流量字段截断（如 response_body 可能几 MB）
MAX_FIELD_BYTES = 16 * 1024

# ---------- MCP 实例 ----------

mcp = FastMCP("telnix")


# ---------- HTTP 客户端 ----------

def _api(method: str, path: str, body: Any = None, timeout: float = 30.0) -> dict:
    """调用后端 API，返回 {code, data, msg}。失败不 sys.exit，返回错误 dict。"""
    url = f"{BASE_URL}/api{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"code": 0, "data": raw, "msg": "ok"}
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            return {"code": e.code, "msg": f"HTTP {e.code}: {e.reason}", "data": None}
    except urllib.error.URLError as e:
        return {"code": -1, "msg": f"无法连接后端 {BASE_URL}: {e.reason}", "data": None,
                "hint": "后端未启动？运行: cd src\\host && python -m telnix"}
    except Exception as e:  # noqa: BLE001
        return {"code": -1, "msg": f"连接异常: {e}", "data": None}


def _ok(res: dict) -> tuple[Optional[Any], Optional[str]]:
    """提取 data。成功返回 (data, None)，失败返回 (None, error_msg)。"""
    if res.get("code") == 0:
        return res.get("data"), None
    msg = res.get("msg") or "未知错误"
    hint = _hint_for_error(msg)
    if hint:
        msg = f"{msg} | 修复建议: {hint}"
    return None, msg


def _api_with_windivert_ack(method: str, path: str, body: Any = None,
                            timeout: float = 30.0) -> dict:
    """调用可能触发 WinDivert 加载的 API（raw/transparent-proxy start）。

    若后端返回 need_ack=true（首次启用未确认），自动触发桌面置顶原生弹窗流程：
    1. POST /system/request-windivert-ack 创建 pending 请求 + 弹原生 Yes/No
    2. 长轮询 /system/windivert-ack-request/{rid}/wait 等待用户响应
    3. 用户选「是」→ ack 已持久化 → 重试原请求并返回结果
    4. 用户选「否」→ 返回包含 need_ack=true 的错误响应（caller 用 _ok 处理）

    非 Windows 平台 / 已 ack 时后端不会返回 need_ack，本函数等同 _api。
    """
    res = _api(method, path, body, timeout=timeout)
    # 检测是否需要 WinDivert 风险提示确认
    if not (res.get("need_ack") is True or
            (isinstance(res.get("data"), dict) and res["data"].get("need_ack"))):
        return res
    # 触发原生弹窗流程
    ack_res = _api("POST", "/system/request-windivert-ack", timeout=10.0)
    if ack_res.get("code") != 0:
        return ack_res
    ack_data = ack_res.get("data") or {}
    # 非 Windows 或已 ack：后端返回 skipped=true，直接重试原请求
    if ack_data.get("skipped"):
        return _api(method, path, body, timeout=timeout)
    rid = ack_data.get("request_id")
    if not rid:
        return res
    # 长轮询：最多重试 3 次（每次 60s），覆盖 3 分钟窗口
    for _ in range(3):
        r = _api("GET", f"/system/windivert-ack-request/{rid}/wait", timeout=65.0)
        if r.get("code") != 0:
            return r
        d = r.get("data") or {}
        status = d.get("status")
        if status == "accepted":
            # 用户已确认：重试原请求
            return _api(method, path, body, timeout=timeout)
        elif status == "rejected":
            # 用户拒绝：返回原错误响应让 caller 处理
            return res
        # status == "pending"，继续下一轮
    # 超时：返回原错误响应
    return res


def _hint_for_error(msg: str) -> str | None:
    """根据错误信息生成 agent 可操作的修复建议。"""
    msg_l = msg.lower()
    if "无法连接" in msg or "connection" in msg_l:
        return "后端未启动？运行: cd src\\host && python -m telnix"
    if "证书" in msg or "cert" in msg_l:
        return "HTTPS 解密需要证书: 调用 cert_install 工具"
    if "pydivert" in msg_l:
        return "TCP/UDP 抓包需要: pip install pydivert（并用管理员身份运行）"
    if "管理员" in msg or "admin" in msg_l:
        return "请用 system_restart_as_admin 工具以管理员身份重启"
    if "会话" in msg and "不存在" in msg:
        return "先调用 capture_start 创建会话"
    if "not found" in msg_l or "找不到" in msg:
        return "后端可能未重启，旧进程缺少新路由。调用 system_restart 重启后端"
    return None


# ---------- 输出处理 ----------

def _to_text(obj: Any) -> str:
    """把任意对象转成 JSON 文本，超长截断。"""
    text = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        # 截断到 MAX_OUTPUT_BYTES 字节（按字符近似）
        cut = int(MAX_OUTPUT_BYTES * 0.9)
        text = text[:cut] + f"\n\n... [输出已截断，原始大小 {len(text)} 字符，超过 {MAX_OUTPUT_BYTES} 字节上限]"
    return text


def _truncate_fields(obj: Any, fields: list[str], limit: int = MAX_FIELD_BYTES) -> Any:
    """递归截断指定字段的值（如 response_body）。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in fields and isinstance(v, str) and len(v.encode("utf-8")) > limit:
                out[k] = v[:limit] + f"... [字段已截断，原始 {len(v)} 字符]"
            else:
                out[k] = _truncate_fields(v, fields, limit)
        return out
    if isinstance(obj, list):
        return [_truncate_fields(item, fields, limit) for item in obj]
    return obj


def _result(obj: Any) -> str:
    """标准成功结果。"""
    return _to_text(obj)


def _error(msg: str, **extra) -> str:
    """标准错误结果。"""
    err = {"ok": False, "error": msg}
    err.update(extra)
    return _to_text(err)


# ---------- 匹配表达式解析（复用 cli.py 逻辑，去掉 sys.exit 副作用） ----------

def parse_match(expr: str) -> dict:
    """解析匹配表达式 'key op value && ...'，返回 {pattern, match_mode, filters, *_filter}。"""
    filters: list[tuple[str, str, str]] = []
    url_pattern_parts: list[str] = []
    for tok in expr.split("&&"):
        tok = tok.strip()
        if not tok:
            continue
        for op in ("~=", "!=", ">=", "<=", "=", ">", "<"):
            if op in tok:
                k, v = tok.split(op, 1)
                k = k.strip().lower()
                v = v.strip().strip('"').strip("'")
                filters.append((k, op, v))
                if k in ("host", "path", "url") and op in ("~=", "="):
                    if k == "url":
                        url_pattern_parts = [v]
                    elif k == "host":
                        url_pattern_parts.append(f"*{v}*")
                    elif k == "path":
                        url_pattern_parts.append(f"*{v}*")
                break
        else:
            raise ValueError(f"无法解析匹配条件: {tok}")
    if url_pattern_parts:
        seen = []
        for p in url_pattern_parts:
            if p not in seen:
                seen.append(p)
        if len(seen) == 1:
            pattern = seen[0]
        else:
            combined = ""
            for p in seen:
                p_core = p.strip("*")
                if p_core and p_core not in combined:
                    combined += p_core
            pattern = f"*{combined}*" if combined else "*"
        match_mode = "wildcard"
    else:
        pattern = "*"
        match_mode = "wildcard"
    filter_fields = {"method": [], "status": [], "pid": [], "process": []}
    for k, op, v in filters:
        if k in filter_fields and op in ("=", "~=", "!="):
            if op in ("=", "~="):
                filter_fields[k].append(v)
    return {
        "pattern": pattern,
        "match_mode": match_mode,
        "filters": filters,
        "method_filter": ",".join(filter_fields["method"]),
        "status_filter": ",".join(filter_fields["status"]),
        "pid_filter": ",".join(filter_fields["pid"]),
        "process_filter": ",".join(filter_fields["process"]),
    }


def flow_matches(flow: dict, filters: list[tuple[str, str, str]]) -> bool:
    """客户端过滤（用于 dry-run 预览）。"""
    for k, op, v in filters:
        fv = _get_flow_field(flow, k)
        if fv is None:
            return False
        fv_s = str(fv)
        if op == "=" and fv_s != v:
            return False
        if op == "!=" and fv_s == v:
            return False
        if op == "~=":
            rx = _wildcard_to_regex(v)
            if not rx.search(fv_s):
                return False
        if op in (">=", "<=", ">", "<") and not _num_cmp(fv_s, v, op):
            return False
    return True


def _get_flow_field(flow: dict, key: str) -> Any:
    m = {
        "host": flow.get("host"),
        "method": flow.get("method"),
        "path": flow.get("path"),
        "url": flow.get("url"),
        "status": flow.get("status_code"),
        "pid": flow.get("pid"),
        "process": flow.get("process_name"),
        "protocol": flow.get("protocol"),
    }
    return m.get(key)


@functools.lru_cache(maxsize=512)
def _wildcard_to_regex(pat: str) -> "re.Pattern[str]":
    rx = "^" + re.escape(pat).replace(r"\*", ".*").replace(r"\?", ".") + "$"
    return re.compile(rx)


def _num_cmp(a: str, b: str, op: str) -> bool:
    try:
        fa, fb = float(a), float(b)
    except ValueError:
        return False
    if op == ">=":
        return fa >= fb
    if op == "<=":
        return fa <= fb
    if op == ">":
        return fa > fb
    if op == "<":
        return fa < fb
    return False


# ---------- 动作解析（复用 cli.py 逻辑） ----------

def parse_action(spec: str) -> dict:
    """解析动作规范字符串，返回后端规则字段。"""
    parts = _split_action(spec)
    if not parts:
        raise ValueError("动作规范不能为空")
    name = parts[0].lower()
    args = parts[1:]

    # ---- 改响应 ----
    if name == "replace-header":
        if len(args) < 2:
            raise ValueError("replace-header 需要: K V")
        return {"action": "modify_response",
                "modify_rules": [{"target": "response_header", "op": "replace", "key": args[0], "value": args[1]}]}
    if name == "set-json":
        if len(args) < 2:
            raise ValueError("set-json 需要: key value")
        return {"action": "modify_response",
                "modify_rules": [{"target": "response_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}]}
    if name == "set-json-path":
        if len(args) < 2:
            raise ValueError("set-json-path 需要: path value")
        return {"action": "modify_response",
                "modify_rules": [{"target": "response_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}]}
    if name == "remove-json":
        if len(args) < 1:
            raise ValueError("remove-json 需要: key")
        return {"action": "modify_response",
                "modify_rules": [{"target": "response_body", "op": "remove", "key": args[0]}]}
    if name == "remove-json-path":
        if len(args) < 1:
            raise ValueError("remove-json-path 需要: path")
        return {"action": "modify_response",
                "modify_rules": [{"target": "response_body", "op": "remove", "key": args[0]}]}
    if name == "replace-bytes":
        if len(args) < 1:
            raise ValueError("replace-bytes 需要: offset:hex")
        return {"action": "modify_response",
                "modify_rules": [{"target": "response_body", "op": "replace-bytes", "key": "", "value": args[0]}]}
    if name == "replace-bytes-regex":
        if len(args) < 2:
            raise ValueError("replace-bytes-regex 需要: regex hex")
        return {"action": "modify_response",
                "modify_rules": [{"target": "response_body", "op": "replace-bytes-regex", "key": args[0], "value": args[1]}]}
    if name == "mock":
        code = int(args[0]) if args else 200
        body = args[1] if len(args) > 1 else ""
        return {"action": "mock", "mock_status": code, "mock_body": body,
                "mock_headers": {"Content-Type": "application/json"}}
    if name == "status":
        code = int(args[0]) if args else 200
        return {"action": "mock", "mock_status": code, "mock_body": "",
                "mock_headers": {"Content-Type": "application/json"}}
    if name == "drop":
        return {"action": "mock", "mock_status": 503, "mock_body": "", "mock_headers": {}}
    if name == "mock-request":
        body = args[0] if args else ""
        ctype = args[1] if len(args) > 1 else "application/json"
        return {"action": "mock_request", "mock_body": body, "mock_headers": {"Content-Type": ctype}}

    # ---- 改请求 ----
    if name == "set-request-header":
        if len(args) < 2:
            raise ValueError("set-request-header 需要: K V")
        return {"action": "modify_request",
                "modify_rules": [{"target": "request_header", "op": "replace", "key": args[0], "value": args[1]}]}
    if name == "set-request-json":
        if len(args) < 2:
            raise ValueError("set-request-json 需要: key value")
        return {"action": "modify_request",
                "modify_rules": [{"target": "request_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}]}
    if name == "set-request-json-path":
        if len(args) < 2:
            raise ValueError("set-request-json-path 需要: path value")
        return {"action": "modify_request",
                "modify_rules": [{"target": "request_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}]}
    if name == "remove-request-json":
        if len(args) < 1:
            raise ValueError("remove-request-json 需要: key")
        return {"action": "modify_request",
                "modify_rules": [{"target": "request_body", "op": "remove", "key": args[0]}]}
    if name == "set-request-body-hex":
        if len(args) < 1:
            raise ValueError("set-request-body-hex 需要: hex")
        return {"action": "modify_request",
                "modify_rules": [{"target": "request_body", "op": "replace", "key": "", "value": _hex_to_b64(args[0])}]}
    if name == "replace-request-bytes":
        if len(args) < 1:
            raise ValueError("replace-request-bytes 需要: offset:hex")
        return {"action": "modify_request",
                "modify_rules": [{"target": "request_body", "op": "replace-bytes", "key": "", "value": args[0]}]}

    # ---- 时序动作 ----
    if name == "delay":
        if len(args) < 1:
            raise ValueError("delay 需要: N（毫秒）")
        return {"action": "modify_response",
                "modify_rules": [{"target": "delay", "op": "sleep", "value": int(args[0])}]}
    if name == "delay-request":
        if len(args) < 1:
            raise ValueError("delay-request 需要: N（毫秒）")
        return {"action": "modify_request",
                "modify_rules": [{"target": "delay-request", "op": "sleep", "value": int(args[0])}]}

    # ---- Python 脚本 ----
    if name == "script":
        if not args:
            raise ValueError("script 需要: '<inline source>' 或 script file <path>")
        if args[0] == "file" and len(args) >= 2:
            with open(args[1], "r", encoding="utf-8") as f:
                source = f.read()
        else:
            source = args[0]
        return {"action": "script", "modify_rules": source}

    raise ValueError(f"未知动作: {name}（支持: set-json/set-json-path/remove-json/replace-header/"
                     f"replace-bytes/replace-bytes-regex/mock/status/drop/mock-request/"
                     f"set-request-header/set-request-json/set-request-json-path/remove-request-json/"
                     f"set-request-body-hex/replace-request-bytes/delay/delay-request/script）")


def _split_action(spec: str) -> list[str]:
    try:
        return shlex.split(spec)
    except ValueError:
        return spec.split()


def _auto_type(v: str) -> Any:
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if v.lower() == "null":
        return None
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        return v


def _hex_to_b64(hex_str: str) -> str:
    import base64
    raw = bytes.fromhex(hex_str.replace(" ", "").replace("0x", ""))
    return "base64:" + base64.b64encode(raw).decode("ascii")


# 会话辅助
def _get_session_id(session: int = 0) -> tuple[int, Optional[str]]:
    """获取会话 ID。成功返回 (sid, None)，失败返回 (0, error)。"""
    if session:
        return int(session), None
    env_session = os.environ.get("TELNIX_SESSION", "").strip()
    if env_session:
        try:
            return int(env_session), None
        except ValueError:
            return 0, f"TELNIX_SESSION 环境变量值无效: {env_session}"
    res = _api("GET", "/status")
    data, err = _ok(res)
    if err:
        return 0, err
    sid = data.get("session_id") if isinstance(data, dict) else None
    if not sid:
        return 0, "无活动会话，请先 capture_start 或用 session 参数指定"
    return int(sid), None


# ======================================================================
# 工具集：状态 & 抓包控制
# ======================================================================

@mcp.tool()
def get_status() -> str:
    """获取 Telnix 后端状态（抓包状态、会话、代理、证书等）。

    返回 JSON，包含 capturing/session_id/system_proxy_on/cert_installed 等字段。
    """
    res = _api("GET", "/status")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def capture_start(
    layer: str = "http",
    auto_stop_seconds: float = 0,
    pid_filter: str = "",
    port_filter: str = "",
    bpf_filter: str = "",
) -> str:
    """开始抓包。返回 session_id。

    Args:
        layer: 抓包层 http=仅HTTP代理(默认), tcp=仅TCP/UDP(WinDivert需管理员), all=两者都抓
        auto_stop_seconds: N 秒后自动停止抓包（0=不自动停，agent 不用自己 sleep+stop）
        pid_filter: TCP/UDP 模式按 PID 过滤，逗号分隔
        port_filter: TCP/UDP 模式按端口过滤，逗号分隔
        bpf_filter: TCP/UDP 模式 WinDivert filter 字符串
    """
    body: dict = {}
    if auto_stop_seconds and auto_stop_seconds > 0:
        body["auto_stop_seconds"] = float(auto_stop_seconds)
    res = _api("POST", "/capture/start", body, timeout=10)
    data, err = _ok(res)
    if err:
        return _error(err)
    out = {"session_id": data.get("session_id"), "capturing": True}
    if data.get("auto_stop_seconds"):
        out["auto_stop_seconds"] = data["auto_stop_seconds"]
        out["hint"] = f"后端将在 {data['auto_stop_seconds']}s 后自动停止抓包"
    if layer in ("tcp", "all"):
        raw_body: dict = {}
        if pid_filter:
            raw_body["pid_filter"] = [int(p) for p in pid_filter.split(",") if p.strip()]
        if port_filter:
            raw_body["port_filter"] = [int(p) for p in port_filter.split(",") if p.strip()]
        if bpf_filter:
            raw_body["filter_str"] = bpf_filter
        # 首次启用未确认 WinDivert 风险提示时，自动触发桌面置顶原生弹窗
        raw_res = _api_with_windivert_ack("POST", "/raw/start", raw_body, timeout=10)
        if raw_res.get("code") == 0:
            out["raw_capture"] = "started"
        else:
            out["raw_capture"] = "failed"
            out["raw_error"] = raw_res.get("msg")
            out["raw_hint"] = _hint_for_error(raw_res.get("msg") or "")
    return _result(out)


@mcp.tool()
def capture_stop(layer: str = "http") -> str:
    """停止抓包（会话保留，代理仍运行）。

    Args:
        layer: 停止哪层 http=仅HTTP(默认), all=同时停TCP/UDP
    """
    res = _api("POST", "/capture/stop")
    _, err = _ok(res)
    if err:
        return _error(err)
    if layer == "all":
        _api("POST", "/raw/stop")
    return _result({"capturing": False})


@mcp.tool()
def capture_clear() -> str:
    """清空当前会话的所有流量记录。"""
    res = _api("POST", "/capture/clear")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"cleared": True})


@mcp.tool()
def capture_pause() -> str:
    """暂停抓包（会话保留，代理仍跑，区别于 stop）。"""
    res = _api("POST", "/capture/pause")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result({"paused": True, "session_id": data.get("session_id") if isinstance(data, dict) else None,
                    "hint": "会话保留，代理仍跑。capture_resume 恢复，capture_stop 真正停止"})


@mcp.tool()
def capture_resume() -> str:
    """恢复抓包记录。"""
    res = _api("POST", "/capture/resume")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result({"resumed": True, "session_id": data.get("session_id") if isinstance(data, dict) else None})


# ======================================================================
# 工具集：流量查询
# ======================================================================

@mcp.tool()
def packets_list(
    session: int = 0,
    limit: int = 100,
    since_id: int = 0,
    filter_expr: str = "",
    host: str = "",
    status_code: str = "",
    method: str = "",
    protocol: str = "",
) -> str:
    """列出当前会话的流量（默认 NDJSON 风格的 JSON 数组）。

    Args:
        session: 会话 ID（0=当前活动会话）
        limit: 最多返回条数（默认100）
        since_id: 增量查询，只返回 id > N 的流量（非阻塞轮询）
        filter_expr: 过滤表达式 'key op value && ...'（key: host/method/path/url/status/pid/process, op: = ~= != >= <= > <）
        host: 快捷按主机过滤
        status_code: 快捷按状态码过滤
        method: 快捷按方法过滤
        protocol: 协议过滤 http|tcp|udp|ws|dns
    """
    sid, err = _get_session_id(session)
    if err:
        return _error(err)
    params = [f"limit={limit}", "offset=0"]
    if host:
        params.append(f"host={urllib.parse.quote(host)}")
    if status_code:
        params.append(f"status_code={urllib.parse.quote(status_code)}")
    if method:
        params.append(f"method={urllib.parse.quote(method.upper())}")
    if since_id:
        params.append(f"since_id={since_id}")
    if protocol:
        params.append(f"protocol={urllib.parse.quote(protocol)}")
    qs = "&".join(params)
    res = _api("GET", f"/sessions/{sid}/flows?{qs}")
    data, err = _ok(res)
    if err:
        return _error(err)
    flows = data.get("flows", []) if isinstance(data, dict) else data
    # 客户端过滤
    if filter_expr:
        try:
            filters = parse_match(filter_expr)["filters"]
            flows = [f for f in flows if flow_matches(f, filters)]
        except ValueError as e:
            return _error(str(e))
    # 截断大字段
    flows = _truncate_fields(flows, ["request_body", "response_body", "raw_data"])
    return _result({"flows": flows, "count": len(flows), "session_id": sid})


@mcp.tool()
def packets_list_all(
    host: str = "",
    process: str = "",
    method: str = "",
    status_code: str = "",
    protocol: str = "",
    limit: int = 100,
    offset: int = 0,
    since_id: int = 0,
) -> str:
    """列出跨所有会话的流量（全局分析用）。

    Args:
        host: 按主机过滤
        process: 按进程名过滤
        method: 按方法过滤
        status_code: 按状态码过滤
        protocol: 协议过滤 http|tcp|udp|ws|dns
        limit: 最多返回条数
        offset: 分页偏移
        since_id: 增量查询
    """
    params = [f"limit={limit}", f"offset={offset}"]
    if host:
        params.append(f"host={urllib.parse.quote(host)}")
    if process:
        params.append(f"process={urllib.parse.quote(process)}")
    if method:
        params.append(f"method={urllib.parse.quote(method.upper())}")
    if status_code:
        params.append(f"status_code={urllib.parse.quote(status_code)}")
    if since_id:
        params.append(f"since_id={since_id}")
    if protocol:
        params.append(f"protocol={urllib.parse.quote(protocol)}")
    qs = "&".join(params)
    res = _api("GET", f"/flows/all?{qs}")
    data, err = _ok(res)
    if err:
        return _error(err)
    flows = data.get("flows", []) if isinstance(data, dict) else data
    flows = _truncate_fields(flows, ["request_body", "response_body", "raw_data"])
    return _result({"flows": flows, "count": len(flows)})


@mcp.tool()
def packets_get(
    flow_id: int,
    field: str = "",
) -> str:
    """获取单个流量详情。

    Args:
        flow_id: 流量 ID
        field: 只取指定字段 request_body|response_body|raw_data（空=全部）
    """
    res = _api("GET", f"/flows/{flow_id}")
    data, err = _ok(res)
    if err:
        return _error(err)
    if field:
        return _result({"field": field, "value": data.get(field) if isinstance(data, dict) else None})
    data = _truncate_fields(data, ["request_body", "response_body", "raw_data"])
    return _result(data)


@mcp.tool()
def packets_search(
    body_regex: str = "",
    binary_hex: str = "",
    search_all: bool = False,
) -> str:
    """正则搜索流量 body。

    Args:
        body_regex: 正则表达式（搜索 request_body + response_body）
        binary_hex: 二进制搜索（hex 字符串，与 body_regex 互斥）
        search_all: true=跨所有会话搜索, false=仅当前会话
    """
    if not body_regex and not binary_hex:
        return _error("需要 body_regex 或 binary_hex 参数")
    body: dict = {}
    if body_regex:
        body["body_regex"] = body_regex
    if binary_hex:
        body["binary_hex"] = binary_hex
    if search_all:
        body["all"] = True
    res = _api("POST", "/flows/search", body, timeout=60)
    data, err = _ok(res)
    if err:
        return _error(err)
    data = _truncate_fields(data, ["matched_text", "request_body", "response_body"])
    return _result(data)


@mcp.tool()
def packets_stats(session: int = 0) -> str:
    """流量统计（按 host/method/status 分组计数）。

    Args:
        session: 会话 ID（0=当前活动会话）
    """
    sid, err = _get_session_id(session)
    if err:
        return _error(err)
    res = _api("GET", f"/sessions/{sid}/stats")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def packets_overview() -> str:
    """多维聚合统计概览（CoolUI 仪表盘数据源，跨会话全量）。

    一次返回所有维度的聚合统计，包括：总数、总字节、入站/出站字节、成功/错误计数、
    平均耗时，以及按协议/方法/状态码区间/Host/进程/IP属地 分组的计数列表（各 top 20）。
    适用于生成流量监控仪表盘、全局概览报告。不需要会话 ID 参数。
    """
    res = _api("GET", "/flows/overview")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def packets_delete(flow_id: int = 0, ids: str = "") -> str:
    """删除流量。

    Args:
        flow_id: 单个流量 ID
        ids: 批量删除，逗号分隔的 ID 列表（优先于 flow_id）
    """
    if ids:
        id_list = [x.strip() for x in ids.split(",") if x.strip()]
        res = _api("POST", "/flows/batch-delete", {"ids": id_list})
        _, err = _ok(res)
        if err:
            return _error(err)
        return _result({"deleted": len(id_list)})
    if not flow_id:
        return _error("需要 flow_id 或 ids 参数")
    res = _api("DELETE", f"/flows/{flow_id}")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"deleted": 1, "id": flow_id})


@mcp.tool()
def packets_clear(all_sessions: bool = False, before_id: int = 0) -> str:
    """清空流量。

    Args:
        all_sessions: true=清空所有会话流量, false=仅当前会话
        before_id: 只清 id < N 的流量（0=清全部）
    """
    if all_sessions:
        res = _api("POST", "/flows/clear-all")
        _, err = _ok(res)
        if err:
            return _error(err)
        return _result({"cleared": "all_sessions"})
    sid, err = _get_session_id(0)
    if err:
        return _error(err)
    if before_id:
        res = _api("POST", f"/sessions/{sid}/flows/clear-before", {"before_id": before_id})
    else:
        res = _api("POST", f"/sessions/{sid}/flows/clear")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"cleared": True, "session_id": sid})


# ======================================================================
# 工具集：拦截规则（自动修改）
# ======================================================================

@mcp.tool()
def intercept_add(
    match: str,
    action: str,
    note: str = "",
    dry_run: bool = False,
    idempotent: bool = False,
) -> str:
    """添加拦截规则（自动修改规则）。

    Args:
        match: 匹配表达式 'key op value && ...'（key: host/method/path/url/status/pid/process, op: = ~= != >= <= > <）
            示例: 'host~=api.example.com && method=POST'
        action: 动作规范。支持:
            改响应: set-json key value | set-json-path path value | remove-json key | remove-json-path path |
                    replace-header K V | replace-bytes offset:hex | replace-bytes-regex regex hex |
                    mock CODE BODY | status CODE | drop | mock-request BODY [CTYPE]
            改请求: set-request-header K V | set-request-json key value | set-request-json-path path value |
                    remove-request-json key | set-request-body-hex hex | replace-request-bytes offset:hex
            时序: delay N | delay-request N (毫秒)
            Python 脚本: script '<source>' | script file <path>
                脚本定义 on_request(ctx)/on_response(ctx)，独立 worker 子进程运行
                ctx 属性: host/path/method/url/scheme/pid/process_name/request_headers/request_body
                          (on_response 额外: status_code/response_headers/response_body)
                修改: ctx.set_request_header/set_request_body/set_response_header/set_response_body/set_status_code
                返回 None=继续, {"drop": True}=拒绝, {"mock": True, "status": 200, "headers": {}, "body": b""}=伪造响应
        note: 规则备注（必填，方便后续管理）
        dry_run: true=预览会命中的流量但不创建规则
        idempotent: true=幂等创建（已存在相同规则则不重复创建）
    """
    if not note:
        note = f"MCP: {action}"
    try:
        m = parse_match(match)
        a = parse_action(action)
    except ValueError as e:
        return _error(str(e))

    if dry_run:
        sid, err = _get_session_id(0)
        if err:
            return _error(err)
        res = _api("GET", f"/sessions/{sid}/flows?limit=500&offset=0")
        data, err = _ok(res)
        if err:
            return _error(err)
        flows = data.get("flows", []) if isinstance(data, dict) else data
        matched = [f for f in flows if flow_matches(f, m["filters"])]
        would_create = {
            "pattern": m["pattern"], "match_mode": m["match_mode"],
            "action": a.get("action"), "modify_rules": a.get("modify_rules", []),
            "mock_status": a.get("mock_status"), "mock_body": a.get("mock_body", ""),
            "note": note,
        }
        for fk in ("method_filter", "status_filter", "pid_filter", "process_filter"):
            if m.get(fk):
                would_create[fk] = m[fk]
        return _result({
            "dry_run": True, "would_create": would_create,
            "matched_count": len(matched),
            "matched_flows": [{"id": f.get("id"), "method": f.get("method"),
                               "host": f.get("host"), "path": f.get("path")} for f in matched[:20]],
        })

    rule = {
        "enabled": True,
        "match_mode": m["match_mode"],
        "pattern": m["pattern"],
        "action": a.get("action", "modify_response"),
        "mock_status": a.get("mock_status"),
        "mock_headers": a.get("mock_headers"),
        "mock_body": a.get("mock_body", ""),
        "modify_rules": a.get("modify_rules", []),
        "note": note,
        "method_filter": m.get("method_filter", ""),
        "status_filter": m.get("status_filter", ""),
        "pid_filter": m.get("pid_filter", ""),
        "process_filter": m.get("process_filter", ""),
    }

    if idempotent:
        idem_res = _api("POST", "/auto-reply/rules/idempotent", rule)
        if idem_res.get("code") == 0:
            idem_data = idem_res.get("data") or {}
            out = {"created": idem_data.get("created", False),
                   "idempotent": idem_data.get("idempotent", not idem_data.get("created", False)),
                   "rule_id": idem_data.get("rule_id"), "pattern": m["pattern"], "note": note}
            if idem_data.get("hint"):
                out["hint"] = idem_data["hint"]
            return _result(out)
        # 降级：客户端遍历
        list_res = _api("GET", "/auto-reply/rules")
        existing, _ = _ok(list_res)
        existing = existing if isinstance(existing, list) else []
        new_sig = json.dumps(rule.get("modify_rules", []), sort_keys=True)
        for r in existing:
            if (isinstance(r, dict) and r.get("pattern") == rule["pattern"]
                    and r.get("match_mode") == rule["match_mode"]
                    and r.get("action") == rule["action"]
                    and json.dumps(r.get("modify_rules", []), sort_keys=True) == new_sig):
                return _result({"created": False, "idempotent": True, "rule_id": r.get("id"),
                                "pattern": m["pattern"], "note": note,
                                "hint": "已存在相同规则，未重复创建"})

    res = _api("POST", "/auto-reply/rules", rule)
    created, err = _ok(res)
    if err:
        return _error(err)
    out = {"created": True, "rule_id": created.get("id") if isinstance(created, dict) else None,
           "pattern": m["pattern"], "note": note}
    for fk in ("method_filter", "status_filter", "pid_filter", "process_filter"):
        if m.get(fk):
            out[fk] = m[fk]
    return _result(out)


@mcp.tool()
def intercept_list() -> str:
    """列出所有拦截规则（含命中统计）。"""
    res = _api("GET", "/auto-reply/rules")
    data, err = _ok(res)
    if err:
        return _error(err)
    rules = data if isinstance(data, list) else []
    for r in rules:
        if isinstance(r, dict):
            if "rule_id" not in r:
                r["rule_id"] = r.get("id")
            for fk in ("method_filter", "status_filter", "pid_filter", "process_filter"):
                if r.get(fk) == "":
                    r[fk] = None
    return _result(rules)


@mcp.tool()
def intercept_del(rule_id: int = 0, ids: str = "") -> str:
    """删除拦截规则。

    Args:
        rule_id: 单个规则 ID
        ids: 批量删除，逗号分隔（优先于 rule_id）
    """
    if ids:
        id_list = [x.strip() for x in ids.split(",") if x.strip()]
        res = _api("POST", "/auto-reply/rules/batch-delete", {"ids": id_list})
        _, err = _ok(res)
        if err:
            return _error(err)
        return _result({"deleted": len(id_list)})
    if not rule_id:
        return _error("需要 rule_id 或 ids 参数")
    res = _api("DELETE", f"/auto-reply/rules/{rule_id}")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"deleted": 1, "rule_id": rule_id})


@mcp.tool()
def intercept_hits(rule_id: int) -> str:
    """查看某规则的命中统计 + 最后命中的流量详情。

    Args:
        rule_id: 规则 ID
    """
    res = _api("GET", "/auto-reply/rules")
    data, err = _ok(res)
    if err:
        return _error(err)
    rules = data if isinstance(data, list) else []
    rule = None
    for r in rules:
        if isinstance(r, dict) and (r.get("id") == rule_id or r.get("rule_id") == rule_id):
            rule = r
            break
    if not rule:
        return _error(f"规则不存在: {rule_id}")
    out = {"rule_id": rule.get("id"), "pattern": rule.get("pattern"), "action": rule.get("action"),
           "hit_count": rule.get("hit_count", 0), "last_hit_at": rule.get("last_hit_at", ""),
           "last_hit_flow_id": rule.get("last_hit_flow_id")}
    last_fid = rule.get("last_hit_flow_id")
    if last_fid:
        flow_res = _api("GET", f"/flows/{last_fid}")
        flow_data, _ = _ok(flow_res)
        out["last_hit_flow"] = _truncate_fields(flow_data, ["request_body", "response_body"]) if flow_data else None
    else:
        out["last_hit_flow"] = None
    return _result(out)


@mcp.tool()
def intercept_toggle(rule_id: int, enabled: bool) -> str:
    """启用/禁用拦截规则。

    Args:
        rule_id: 规则 ID
        enabled: true=启用, false=禁用
    """
    res = _api("PUT", f"/auto-reply/rules/{rule_id}", {"enabled": enabled})
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"rule_id": rule_id, "enabled": enabled})


# ======================================================================
# 工具集：重放 / 发包
# ======================================================================

@mcp.tool()
def replay(
    flow_id: int,
    body: str = "",
    method: str = "",
    url: str = "",
    host: str = "",
    port: int = 0,
    headers: str = "",
    timeout: float = 0,
) -> str:
    """重放指定流量。可覆盖 body/method/url/headers。

    Args:
        flow_id: 要重放的流量 ID
        body: 覆盖请求 body（空=用原 body）
        method: 覆盖 HTTP 方法
        url: 覆盖目标 URL
        host: 覆盖目标 host
        port: 覆盖目标端口
        headers: 覆盖请求头，JSON 字符串如 '{"K":"V"}' 或 HTTP 文本格式 'K:V\\nK2:V2'
        timeout: 超时秒数（0=自动：带body 120s，无body 30s）
    """
    req_body: dict = {}
    if body:
        req_body["body"] = body
    if method:
        req_body["method"] = method
    if url:
        req_body["url"] = url
    if host:
        req_body["host"] = host
    if port:
        req_body["port"] = port
    if headers:
        parsed_h = _parse_headers(headers)
        if parsed_h:
            req_body["headers"] = parsed_h
    t = float(timeout) if timeout > 0 else (120.0 if body else 30.0)
    res = _api("POST", f"/flows/{flow_id}/replay", req_body if req_body else None, timeout=t + 5)
    data, err = _ok(res)
    if err:
        return _error(err)
    data = _truncate_fields(data, ["response_body", "request_body"])
    return _result(data)


@mcp.tool()
def send_request(
    method: str = "GET",
    url: str = "",
    headers: str = "",
    body: str = "",
    timeout: float = 30,
) -> str:
    """从零发包（Composer 功能），不依赖已有 flow。

    Args:
        method: HTTP 方法（GET/POST/PUT/DELETE 等）
        url: 目标 URL（必须以 http:// 或 https:// 开头）
        headers: 请求头，JSON 字符串 '{"K":"V"}' 或 HTTP 文本 'K:V\\nK2:V2'
        body: 请求 body
        timeout: 超时秒数
    """
    if not url:
        return _error("url 必填")
    if not url.startswith(("http://", "https://")):
        return _error("url 必须以 http:// 或 https:// 开头")
    req_body: dict = {"method": method.upper(), "url": url, "timeout": float(timeout)}
    if headers:
        parsed_h = _parse_headers(headers)
        if parsed_h:
            req_body["headers"] = parsed_h
    if body:
        req_body["body"] = body
    t = float(timeout) + 5
    res = _api("POST", "/send", req_body, timeout=t)
    data, err = _ok(res)
    if err:
        return _error(err)
    data = _truncate_fields(data, ["response_body"])
    return _result(data)


@mcp.tool()
def replay_batch(
    session: int,
    filter_expr: str = "",
    parallel: int = 1,
    preserve_timing: bool = False,
) -> str:
    """批量时序重放指定会话的全部流量。

    Args:
        session: 会话 ID（必填）
        filter_expr: 客户端过滤表达式，只重放匹配的流量
        parallel: 并发数（1=串行）
        preserve_timing: true=按原始时间间隔重放（测限流/风控）
    """
    res = _api("GET", f"/sessions/{session}/flows?limit=50000&offset=0", timeout=60)
    data, err = _ok(res)
    if err:
        return _error(err)
    flows = data.get("flows", []) if isinstance(data, dict) else data
    if not flows:
        return _result({"replayed": True, "session": session, "count": 0, "results": []})
    flows = [f for f in flows if isinstance(f, dict)]
    if filter_expr:
        try:
            filters = parse_match(filter_expr)["filters"]
            flows = [f for f in flows if flow_matches(f, filters)]
        except ValueError as e:
            return _error(str(e))
    flows.sort(key=lambda f: f.get("timestamp") or "")

    from concurrent.futures import ThreadPoolExecutor

    def _do_replay(flow: dict, idx: int) -> dict:
        fid = flow.get("id")
        if not fid:
            return {"index": idx, "ok": False, "error": "no flow id"}
        r = _api("POST", f"/flows/{fid}/replay", timeout=120)
        d, e = _ok(r)
        if e:
            return {"index": idx, "flow_id": fid, "ok": False, "error": e}
        return {"index": idx, "flow_id": fid, "ok": True,
                "status": d.get("status_code") if isinstance(d, dict) else None,
                "duration": d.get("duration_ms") if isinstance(d, dict) else None}

    if preserve_timing:
        from datetime import datetime
        prev_ts = None
        results = []
        for idx, f in enumerate(flows):
            ts_str = f.get("timestamp")
            if ts_str and prev_ts:
                try:
                    cur = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    prev = datetime.fromisoformat(prev_ts.replace("Z", "+00:00"))
                    gap = (cur - prev).total_seconds()
                    if gap > 0:
                        import time as _time
                        _time.sleep(min(gap, 60))  # 最多等60s
                except Exception:  # noqa: BLE001
                    pass
            prev_ts = ts_str
            results.append(_do_replay(f, idx))
    elif parallel > 1:
        results = []
        with ThreadPoolExecutor(max_workers=parallel) as ex:
            futures = [ex.submit(_do_replay, f, i) for i, f in enumerate(flows)]
            for fut in futures:
                results.append(fut.result())
        results.sort(key=lambda x: x.get("index", 0))
    else:
        results = [_do_replay(f, i) for i, f in enumerate(flows)]

    return _result({"replayed": True, "session": session, "count": len(results), "results": results})


def _parse_headers(raw_h: str) -> dict:
    """健壮解析 headers（JSON 字符串或 HTTP 文本格式）。"""
    headers = {}
    if not raw_h or not isinstance(raw_h, str):
        return headers
    try:
        h_obj = json.loads(raw_h)
        if isinstance(h_obj, dict):
            return {str(k): str(v) for k, v in h_obj.items()}
    except Exception:  # noqa: BLE001
        pass
    for line in raw_h.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    return headers


# ======================================================================
# 工具集：断点控制
# ======================================================================

@mcp.tool()
def breakpoint_status() -> str:
    """查看断点状态（是否开启、pending 流量列表）。"""
    res = _api("GET", "/breakpoint/status")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def breakpoint_on(bp_type: str = "request", timeout_seconds: int = 0) -> str:
    """开启断点。

    Args:
        bp_type: 断点类型 request|response
        timeout_seconds: N 秒未放行自动 release（0=不自动超时）
    """
    body = {"enabled": True}
    if timeout_seconds and timeout_seconds > 0:
        body["timeout"] = timeout_seconds
    res = _api("POST", f"/breakpoint/{bp_type}", body)
    data, err = _ok(res)
    if err:
        return _error(err)
    out = {f"break_on_{bp_type}": True}
    if timeout_seconds and timeout_seconds > 0:
        out["timeout_seconds"] = timeout_seconds
        out["hint"] = f"断点 {bp_type} 已开启，{timeout_seconds}s 未放行自动 release"
    return _result(out)


@mcp.tool()
def breakpoint_off(bp_type: str = "request") -> str:
    """关闭断点。

    Args:
        bp_type: 断点类型 request|response
    """
    res = _api("POST", f"/breakpoint/{bp_type}", {"enabled": False})
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({f"break_on_{bp_type}": False})


@mcp.tool()
def breakpoint_release(flow_id: int = 0, action: str = "release", all_pending: bool = False) -> str:
    """放行/丢弃断点暂停的流量。

    Args:
        flow_id: 单个流量 ID（与 all_pending 互斥）
        action: release=放行, drop=丢弃
        all_pending: true=批量操作所有 pending 断点
    """
    if all_pending:
        res = _api("GET", "/breakpoint/status")
        data, err = _ok(res)
        if err:
            return _error(err)
        pending = data.get("pending", []) if isinstance(data, dict) else []
        ids = []
        for p in pending:
            fid = p.get("flow_id") if isinstance(p, dict) else p
            if fid:
                ids.append(int(fid))
        if not ids:
            return _result({"action": action, "total": 0, "hint": "无 pending 断点"})
        res = _api("POST", "/flows/batch-release", {"ids": ids, "action": action})
        data, err = _ok(res)
        if err:
            return _error(err)
        return _result({"action": action, "total": len(ids),
                        "released": data.get("released", 0) if isinstance(data, dict) else 0})
    if not flow_id:
        return _error("需要 flow_id 或 all_pending=true")
    res = _api("POST", f"/flows/{flow_id}/release", {"action": action})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


# ======================================================================
# 工具集：系统控制
# ======================================================================

@mcp.tool()
def proxy_status() -> str:
    """查看系统代理状态。"""
    res = _api("GET", "/status")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result({"system_proxy_on": data.get("system_proxy_on"),
                    "proxy_host": data.get("proxy_host"), "proxy_port": data.get("proxy_port")})


@mcp.tool()
def proxy_on() -> str:
    """开启系统代理（让所有应用流量走 Telnix）。"""
    res = _api("POST", "/system/enable-proxy")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"system_proxy_on": True})


@mcp.tool()
def proxy_off() -> str:
    """关闭系统代理。"""
    res = _api("POST", "/system/clear-proxy")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"system_proxy_on": False})


@mcp.tool()
def cert_status() -> str:
    """查看 HTTPS 证书安装状态。"""
    res = _api("GET", "/cert/status")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def cert_install() -> str:
    """安装 HTTPS 根证书（需要 UAC 提权）。"""
    res = _api("POST", "/cert/install", timeout=60)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def focus_status() -> str:
    """查看专注模式状态。"""
    res = _api("GET", "/focus")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def focus_on(pids: str = "", process_names: str = "", hosts: str = "", include_children: bool = True) -> str:
    """开启专注模式（只抓指定进程/host 的流量）。

    Args:
        pids: PID 列表，逗号分隔
        process_names: 进程名列表，逗号分隔
        hosts: host 通配符列表，逗号分隔
        include_children: 是否包含子进程
    """
    body = {"enabled": True, "pids": [], "process_names": [], "hosts": [],
            "include_children": include_children}
    if pids:
        body["pids"] = [int(p) for p in pids.split(",") if p.strip()]
    if process_names:
        body["process_names"] = [n.strip() for n in process_names.split(",") if n.strip()]
    if hosts:
        body["hosts"] = [h.strip() for h in hosts.split(",") if h.strip()]
    if not body["pids"] and not body["process_names"] and not body["hosts"]:
        return _error("需要 pids 或 process_names 或 hosts 参数")
    res = _api("POST", "/focus", body)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def focus_off() -> str:
    """关闭专注模式。"""
    res = _api("POST", "/focus", {"enabled": False, "pids": []})
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"focus_on": False})


@mcp.tool()
def raw_capture_status() -> str:
    """查看 TCP/UDP 抓包状态。"""
    res = _api("GET", "/raw/status")
    data, err = _ok(res)
    if err:
        return _error(err)
    if not data.get("pydivert_installed"):
        data["hint"] = "运行 raw_capture_install 安装 pydivert"
    elif not data.get("is_admin"):
        data["hint"] = "需要管理员权限。调用 system_restart_as_admin"
    return _result(data)


@mcp.tool()
def raw_capture_start(pid_filter: str = "", port_filter: str = "", bpf_filter: str = "") -> str:
    """启动 TCP/UDP 抓包（需管理员权限 + pydivert）。

    Args:
        pid_filter: 按 PID 过滤，逗号分隔
        port_filter: 按端口过滤，逗号分隔
        bpf_filter: WinDivert filter 字符串

    非 Windows 平台：WinDivert 是 Windows 专属驱动，直接返回"不支持"错误。
    """
    # 非 Windows 平台：WinDivert 不可用，提前返回错误
    if not sys.platform.startswith("win"):
        return _error("TCP/UDP 抓包仅 Windows 支持（WinDivert 驱动）",
                      hint="macOS/Linux 可使用 HTTP 代理抓包功能（capture_start layer=http）")
    body: dict = {}
    if pid_filter:
        body["pid_filter"] = [int(p) for p in pid_filter.split(",") if p.strip()]
    if port_filter:
        body["port_filter"] = [int(p) for p in port_filter.split(",") if p.strip()]
    if bpf_filter:
        body["filter_str"] = bpf_filter
    # 首次启用未确认 WinDivert 风险提示时，自动触发桌面置顶原生弹窗
    res = _api_with_windivert_ack("POST", "/raw/start", body, timeout=10)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def raw_capture_stop() -> str:
    """停止 TCP/UDP 抓包。"""
    res = _api("POST", "/raw/stop")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def raw_capture_install() -> str:
    """安装 pydivert（TCP/UDP 抓包依赖）。

    非 Windows 平台：pydivert 是 Windows 专属包，直接返回"不支持"错误。
    """
    # 非 Windows 平台：pydivert 是 Windows 专属包，提前返回错误
    if not sys.platform.startswith("win"):
        return _error("pydivert 是 Windows 专属包，macOS/Linux 无法安装",
                      hint="macOS/Linux 可使用 HTTP 代理抓包功能（capture_start layer=http）")
    # 复用 /system/install-dep 端点（统一的 pip 安装基础设施：锁、状态、日志、取消）
    res = _api("POST", "/system/install-dep", body={"package": "pydivert"}, timeout=180)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def system_restart() -> str:
    """重启 Telnix 后端（同进程内 os.execv 重启）。"""
    res = _api("POST", "/system/restart")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"restarting": True})


@mcp.tool()
def system_quit() -> str:
    """退出 Telnix（清系统代理 + 退出）。"""
    res = _api("POST", "/system/quit")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"quitting": True})


@mcp.tool()
def system_install_dep(package: str = "mitmproxy") -> str:
    """触发 pip 安装可选依赖（如 mitmproxy）。

    异步任务：本工具返回 status=running 后需轮询 system_install_dep_status 查询进度。
    安装完成后需调用 system_restart 让新引擎加载到当前进程。

    Args:
        package: 包名，目前仅支持 "mitmproxy"

    Returns:
        {"status": "running", "package": "mitmproxy"}
    """
    res = _api("POST", "/system/install-dep", body={"package": package}, timeout=30)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def system_install_dep_status() -> str:
    """查询 install_dep 任务状态。

    Returns:
        {"status": "idle|running|success|failed", "package": ..., "log": ..., ...}
        status=success 且 package=mitmproxy 时额外返回 mitmproxy_available 字段。
    """
    res = _api("GET", "/system/install-dep/status")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


# ---------- 设置管理 ----------

@mcp.tool()
def settings_get(key: str = "") -> str:
    """读取所有设置或单个 key 的值。

    Args:
        key: 可选。指定时只返回该 key 的值；省略则返回全部设置。

    Returns:
        全部设置 dict，或 {"key": ..., "value": ...}
    """
    res = _api("GET", "/settings")
    data, err = _ok(res)
    if err:
        return _error(err)
    if key:
        return _result({"key": key, "value": data.get(key)})
    return _result(data)


@mcp.tool()
def settings_set(key: str, value: str) -> str:
    """写入单个设置项。value 字符串会自动尝试 JSON 反序列化（bool/数字/list/dict）。

    Args:
        key: 设置项 key（如 "proxy_engine"）
        value: 设置项 value。传 "true"/"false" 会被解析成 bool，
               传 "123" 会被解析成数字，传 "[1,2]" 会被解析成 list。

    Returns:
        更新后的设置 dict。
    """
    import json as _json
    parsed_value: Any = value
    try:
        parsed = _json.loads(value)
        if isinstance(parsed, (bool, int, float, list, dict)) or parsed is None:
            parsed_value = parsed
    except (ValueError, TypeError):
        pass  # 保持字符串
    res = _api("PUT", "/settings", body={key: parsed_value})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def settings_proxy_engine(name: str = "") -> str:
    """查看或切换代理引擎（builtin / async / mitmproxy）。

    Args:
        name: 引擎名。省略则仅查看当前引擎，不修改。
            - builtin：内置线程代理（默认，零依赖，稳定）
            - async：asyncio 代理（实验性，高并发无 GIL 瓶颈）
            - mitmproxy：mitmproxy 引擎（需先 system_install_dep 安装）

    Returns:
        {"proxy_engine": "builtin|async|mitmproxy", "previous": ..., "mitmproxy_available": bool, ...}
        切换后需调用 system_restart 让新引擎加载到当前进程。
    """
    VALID = {"builtin", "async", "mitmproxy"}
    # 先读当前状态
    res = _api("GET", "/settings")
    data, err = _ok(res)
    if err:
        return _error(err)
    current = data.get("proxy_engine") or "builtin"
    mitm_available = bool(data.get("mitmproxy_available"))

    if not name:
        return _result({
            "proxy_engine": current,
            "mitmproxy_available": mitm_available,
            "available_engines": sorted(VALID),
            "hint": "切换引擎: settings_proxy_engine(name='async'); 切换后需 system_restart",
        })

    n = name.lower().strip()
    if n not in VALID:
        return _error(f"不支持的代理引擎: {n}（可选: {', '.join(sorted(VALID))}）")

    if n == "mitmproxy" and not mitm_available:
        return _error(
            "mitmproxy 未安装，无法切换到该引擎",
            hint="先调用 system_install_dep 安装 mitmproxy，再切换引擎",
        )

    res = _api("PUT", "/settings", body={"proxy_engine": n})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result({
        "ok": True,
        "proxy_engine": n,
        "previous": current,
        "mitmproxy_available": mitm_available,
        "hint": "需调用 system_restart 让新引擎生效",
    })


@mcp.tool()
def system_restart_as_admin() -> str:
    """以管理员身份重启 Telnix（UAC 提权，用于 TCP/UDP 抓包）。

    会触发 GUI 用户确认流程：后端创建 pending 请求 → 弹原生 Windows 顶层弹窗 →
    用户同意 → UAC 提权 → 返回 accepted；用户拒绝 → 返回 rejected。
    最多等待 3 分钟（3 次 60s 长轮询）。

    非 Windows 平台：UAC 是 Windows 专属机制，直接返回"不支持"错误，
    提示用户用 sudo 手动以 root 身份运行。
    """
    # 非 Windows 平台：UAC 是 Windows 专属，提前返回错误避免无效请求
    if not sys.platform.startswith("win"):
        return _error("restart-as-admin 仅 Windows 支持（UAC 提权）",
                      hint="macOS/Linux 请用 sudo 手动以 root 身份运行 Telnix")
    res = _api("POST", "/system/request-admin-restart", timeout=10.0)
    data, err = _ok(res)
    if err:
        return _error(err)
    rid = data.get("request_id")
    if not rid:
        return _error("后端未返回 request_id")
    final_status = None
    final_msg = None
    for _ in range(3):
        r = _api("GET", f"/system/admin-request/{rid}/wait", timeout=65.0)
        if r.get("code") != 0:
            return _error(r.get("msg") or "请求不存在或已处理")
        d = r.get("data") or {}
        status = d.get("status")
        if status == "accepted":
            final_status = "accepted"
            final_msg = r.get("msg") or "用户已批准，正在以管理员身份重启"
            break
        elif status == "rejected":
            final_status = "rejected"
            final_msg = r.get("msg") or "用户拒绝了管理员重启请求"
            break
    if final_status is None:
        return _error("等待用户响应超时（3 分钟无响应）")
    if final_status == "accepted":
        return _result({"restarting": True, "as_admin": True, "approved": True, "message": final_msg})
    return _error(final_msg, rejected_by_user=True,
                  hint="用户拒绝了管理员重启请求。可在 GUI 中手动重启，或请用户同意后重试")


@mcp.tool()
def system_windivert_warning_status() -> str:
    """查询 WinDivert 风险提示状态。

    返回:
      needed: 是否需要提示（仅 Windows 平台 + 未确认时为 true）
      message: 风险说明文本
      ack: 当前是否已确认
      platform: 当前平台

    说明：raw_capture_start / transparent_proxy_start / dns_hijack_start 等触发 WinDivert
    加载的工具会自动处理 ack 流程（首次未确认时弹桌面置顶原生弹窗）。本工具仅供 agent
    主动查询当前状态（如检查是否已确认、是否在 Windows 平台）。
    """
    res = _api("GET", "/system/windivert-warning")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def system_windivert_warning_ack() -> str:
    """标记 WinDivert 风险提示为已确认（永久不再提示）。

    调用后 settings.json 的 windivert_warning_acknowledged 设为 1，
    后续 raw_capture_start / transparent_proxy_start / dns_hijack_start 不再被拦截。

    注意：通常无需手动调用——上述 start 工具首次调用时会自动触发桌面原生弹窗让用户确认。
    本工具用于 agent 在用户已通过其他渠道（如读 README）知情后主动跳过弹窗的场景。
    """
    res = _api("POST", "/system/windivert-warning/ack")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


# ======================================================================
# 工具集：会话管理
# ======================================================================

@mcp.tool()
def sessions_list() -> str:
    """列出所有会话。"""
    res = _api("GET", "/sessions")
    data, err = _ok(res)
    if err:
        return _error(err)
    sessions = data if isinstance(data, list) else data.get("sessions", [])
    return _result(sessions)


@mcp.tool()
def sessions_show(session_id: int) -> str:
    """查看会话详情。

    Args:
        session_id: 会话 ID
    """
    res = _api("GET", f"/sessions/{session_id}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def sessions_switch(session_id: int) -> str:
    """切换到指定会话（后续操作默认在此会话）。

    Args:
        session_id: 会话 ID
    """
    res = _api("POST", f"/sessions/{session_id}/switch")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def sessions_delete(session_id: int) -> str:
    """删除会话。

    Args:
        session_id: 会话 ID
    """
    res = _api("DELETE", f"/sessions/{session_id}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def sessions_create(name: str = "") -> str:
    """创建新会话并切换为活动会话。

    Args:
        name: 会话名称（可选）
    """
    body = {"name": name} if name else {}
    res = _api("POST", "/sessions", body)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


# ======================================================================
# 工具集：Agent 工作区管理
# ======================================================================

AGENT_IGNORE_PROCESSES = ["TRAE SOLO CN.exe"]


def _agent_backup_path() -> str:
    import tempfile
    return os.path.join(tempfile.gettempdir(), "telnix_agent_workspace_backup.json")


@mcp.tool()
def agent_start() -> str:
    """开启 agent 工作区：保存当前状态（规则/专注/断点）→ 禁用规则 → 关闭专注/断点 →
    把 agent 进程加入忽略列表。

    完成工作后必须调 agent_end 恢复原状。重复 start 会报错（避免覆盖未恢复的备份）。
    """
    backup_path = _agent_backup_path()
    if os.path.exists(backup_path):
        return _error("已有未恢复的 agent 工作区备份，请先调用 agent_end 恢复后再 agent_start")

    res = _api("GET", "/snapshot")
    snapshot, err = _ok(res)
    if err:
        return _error(err)

    ignored_res = _api("GET", "/processes/ignored")
    ignored_before, _ = _ok(ignored_res)
    ignored_before = ignored_before or []
    ignored_names_before = set()
    for item in ignored_before:
        if isinstance(item, dict):
            n = item.get("process_name") or item.get("name")
            if n:
                ignored_names_before.add(n.lower())

    agent_added = []
    for proc_name in AGENT_IGNORE_PROCESSES:
        if proc_name.lower() not in ignored_names_before:
            r = _api("POST", "/processes/ignore", {"name": proc_name})
            if r.get("code") == 0:
                agent_added.append(proc_name)

    snapshot["agent_added_ignored_processes"] = agent_added
    try:
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return _error(f"保存备份失败: {e}")

    rules = snapshot.get("rules", []) or []
    rule_ids = [r.get("id") for r in rules if isinstance(r, dict) and r.get("id")]
    rules_disabled = 0
    if rule_ids:
        r = _api("POST", "/auto-reply/rules/batch-update", {"ids": rule_ids, "enabled": False})
        if r.get("code") == 0:
            rules_disabled = len(rule_ids)

    _api("POST", "/focus", {"enabled": False, "pids": [], "hosts": [],
                            "methods": [], "status_codes": [], "content_types": []})
    _api("POST", "/breakpoint/request", {"enabled": False})
    _api("POST", "/breakpoint/response", {"enabled": False})

    return _result({
        "agent_workspace": "started", "backup_path": backup_path,
        "rules_disabled": rules_disabled, "focus_cleared": True, "breakpoint_cleared": True,
        "ignored_processes_added": agent_added,
        "hint": "工作区已清空。完成工作后请调 agent_end 恢复原状。",
    })


@mcp.tool()
def agent_end() -> str:
    """恢复 agent_start 之前保存的状态（规则/专注/断点/忽略列表）。

    注意：自动修改规则需要 Telnix 运行才生效，因此系统代理未关闭、Telnix 未退出。
    请询问用户是否关闭 Telnix；用户同意后再调 system_quit。
    """
    backup_path = _agent_backup_path()
    if not os.path.exists(backup_path):
        return _error("没有未恢复的 agent 工作区备份（可能已 agent_end 或从未 agent_start）")
    try:
        with open(backup_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return _error(f"读取备份失败: {e}")

    restore_body = {
        "rules": snapshot.get("rules", []) or [],
        "focus": snapshot.get("focus", {}) or {},
        "breakpoint": snapshot.get("breakpoint", {}) or {},
        "clear_rules": True,
    }
    res = _api("POST", "/snapshot", restore_body)
    result, err = _ok(res)
    if err:
        return _error(err)

    agent_added = snapshot.get("agent_added_ignored_processes", []) or []
    ignored_removed = 0
    if agent_added:
        try:
            cur_ignored, _ = _ok(_api("GET", "/processes/ignored"))
            cur_ignored = cur_ignored or []
            added_lower = {n.lower() for n in agent_added}
            for item in cur_ignored:
                if not isinstance(item, dict):
                    continue
                n = (item.get("process_name") or item.get("name") or "").lower()
                if n in added_lower:
                    r = _api("DELETE", f"/processes/ignore/{item['id']}")
                    if r.get("code") == 0:
                        ignored_removed += 1
        except Exception:  # noqa: BLE001
            pass

    try:
        os.remove(backup_path)
    except OSError:
        pass

    return _result({
        "agent_workspace": "ended", "restored": True,
        "rules_restored": result.get("rules_imported", 0) if isinstance(result, dict) else 0,
        "focus_set": result.get("focus_set", False) if isinstance(result, dict) else False,
        "breakpoint_set": result.get("breakpoint_set", False) if isinstance(result, dict) else False,
        "ignored_processes_removed": ignored_removed,
        "hint": "工作区已恢复。请询问用户是否关闭 Telnix；用户同意后调 system_quit。",
    })


@mcp.tool()
def agent_status() -> str:
    """查看 agent 工作区状态。"""
    backup_path = _agent_backup_path()
    if not os.path.exists(backup_path):
        return _result({"agent_workspace": "inactive",
                        "hint": "没有活跃的 agent 工作区（可调 agent_start 开始）"})
    try:
        with open(backup_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
        return _result({
            "agent_workspace": "active", "backup_path": backup_path,
            "rules_in_backup": len(snapshot.get("rules", []) or []),
            "focus_was_enabled": bool((snapshot.get("focus") or {}).get("enabled")),
            "break_on_request_was_on": bool((snapshot.get("breakpoint") or {}).get("break_on_request")),
            "break_on_response_was_on": bool((snapshot.get("breakpoint") or {}).get("break_on_response")),
            "hint": "工作区已清空，调 agent_end 恢复。",
        })
    except (OSError, json.JSONDecodeError) as e:
        return _error(f"备份文件损坏: {e}")


# ======================================================================
# 工具集：日志
# ======================================================================

@mcp.tool()
def log_tail(limit: int = 100, level: str = "", category: str = "") -> str:
    """查看最近日志。

    Args:
        limit: 返回条数
        level: 日志级别过滤（DEBUG/INFO/WARN/ERROR）
        category: 日志分类过滤
    """
    params = f"?limit={limit}&level={level}&category={category}"
    res = _api("GET", f"/logs{params}")
    data, err = _ok(res)
    if err:
        return _error(err)
    logs = data if isinstance(data, list) else (data.get("items") or data.get("logs") or [])
    return _result(logs)


@mcp.tool()
def log_clear() -> str:
    """清空所有日志。"""
    res = _api("DELETE", "/logs")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"cleared": True})


@mcp.tool()
def log_export(level: str = "", category: str = "", keyword: str = "") -> str:
    """导出日志为 JSONL 文本（直接返回内容，不写文件）。

    Args:
        level: 日志级别过滤
        category: 日志分类过滤
        keyword: 关键词过滤
    """
    params = []
    if level:
        params.append(f"level={urllib.parse.quote(level)}")
    if category:
        params.append(f"category={urllib.parse.quote(category)}")
    if keyword:
        params.append(f"keyword={urllib.parse.quote(keyword)}")
    qs = "&".join(params)
    url = f"{BASE_URL}/api/logs/export" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={"Accept": "application/x-jsonlines"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return _error(f"导出日志失败: {e}")
    # 截断
    if len(content.encode("utf-8")) > MAX_OUTPUT_BYTES:
        cut = int(MAX_OUTPUT_BYTES * 0.9)
        content = content[:cut] + f"\n... [已截断，原始 {len(content)} 字符]"
    return content


# ======================================================================
# 工具集：证书完整管理
# ======================================================================

@mcp.tool()
def cert_remove() -> str:
    """移除已安装的 HTTPS 根证书。"""
    res = _api("POST", "/cert/remove", timeout=60)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


# ======================================================================
# 工具集：断点超时
# ======================================================================

@mcp.tool()
def breakpoint_timeout(seconds: int) -> str:
    """设置断点自动放行超时（N 秒未放行自动 release）。

    Args:
        seconds: 超时秒数（0=禁用自动超时）
    """
    res = _api("POST", "/breakpoint/timeout", {"timeout": seconds})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


# ======================================================================
# 辅助函数：path 模板归一化、百分位计算
# ======================================================================

def _path_template(path: str, keep_query: bool = False) -> tuple[str, list[str]]:
    """path 模板归一化：把数字、UUID、长 hex 段替换为 {id}。"""
    if not path:
        return "/", []
    raw_path, _, query_str = path.partition("?")
    segs = raw_path.split("/")
    out = []
    for seg in segs:
        if not seg:
            out.append("")
            continue
        if re.match(r"^\d+$", seg):
            out.append("{id}")
        elif re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", seg):
            out.append("{uuid}")
        elif re.match(r"^[0-9a-fA-F]{16,}$", seg):
            out.append("{hex}")
        elif len(seg) >= 24 and re.match(r"^[A-Za-z0-9_-]+$", seg):
            out.append("{token}")
        else:
            out.append(seg)
    tpl = "/".join(out)
    query_keys = []
    if query_str:
        for pair in query_str.split("&"):
            k = pair.split("=", 1)[0]
            if k and k not in query_keys:
                query_keys.append(k)
    if keep_query and query_keys:
        tpl += "?" + "&".join(query_keys)
    return tpl, query_keys


def _percentiles(values: list) -> dict:
    """计算 p50/p95/max/min。"""
    if not values:
        return {"p50": 0, "p95": 0, "max": 0, "min": 0, "count": 0}
    s = sorted(values)
    n = len(s)
    return {"p50": s[n // 2], "p95": s[min(n - 1, int(n * 0.95))],
            "max": s[-1], "min": s[0], "count": n}


def _walk_json_leaves(obj, prefix: str = ""):
    """递归遍历 JSON，输出 (path, leaf_value)。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            yield from _walk_json_leaves(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_json_leaves(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def _classify_charset(s: str) -> str:
    """分类字符串字符集（用于签名字段检测）。"""
    if not s:
        return "empty"
    if re.match(r"^\d+$", s):
        return "numeric"
    if re.match(r"^[0-9a-fA-F]+$", s) and re.search(r"[a-fA-F]", s):
        return "hex"
    if re.match(r"^[A-Za-z0-9+/=]+$", s):
        return "base64"
    if re.match(r"^[A-Za-z0-9_\-]+$", s):
        return "alphanumeric"
    if re.match(r"^[A-Za-z]+$", s):
        return "alpha"
    return "mixed"


def _extract_trace_values(body: str, min_length: int = 4) -> list[dict]:
    """从响应 body 提取可追踪字符串值。"""
    out = []
    seen = set()
    if not body or body.startswith("base64:"):
        return out
    try:
        obj = json.loads(body)
        for path, val in _walk_json_leaves(obj):
            if isinstance(val, str) and len(val) >= min_length and val not in seen:
                seen.add(val)
                out.append({"field": path, "value": val})
    except Exception:  # noqa: BLE001
        pass
    patterns = {
        "token": r'(?:token|access_token|auth_token|csrf_token)["\']?\s*[:=]\s*["\']?([A-Za-z0-9_\-\.]{8,})["\']?',
        "session_id": r'(?:session_id|sid|sessionid|session)["\']?\s*[:=]\s*["\']?([A-Za-z0-9_\-]{6,})["\']?',
        "uid": r'(?:uid|user_id|userid|user_id)["\']?\s*[:=]\s*["\']?(\d{3,})["\']?',
    }
    for fname, pat in patterns.items():
        for m in re.finditer(pat, body, re.IGNORECASE):
            v = m.group(1)
            if v and len(v) >= min_length and v not in seen:
                seen.add(v)
                out.append({"field": fname, "value": v})
    return out


def _fetch_flows_for_analysis(session: int = 0, limit: int = 2000, host: str = "") -> tuple[list[dict], Optional[str]]:
    """拉取流量用于分析（endpoints/timeline/stats 公共逻辑）。"""
    if limit == 0:
        limit = 50000
    if session:
        params = [f"limit={limit}", "offset=0"]
        if host:
            params.append(f"host={urllib.parse.quote(host)}")
        res = _api("GET", f"/sessions/{session}/flows?{'&'.join(params)}", timeout=60)
    else:
        params = [f"limit={limit}", "offset=0"]
        if host:
            params.append(f"host={urllib.parse.quote(host)}")
        res = _api("GET", f"/flows/all?{'&'.join(params)}", timeout=60)
    data, err = _ok(res)
    if err:
        return [], err
    return data.get("flows", []) if isinstance(data, dict) else data, None


# ======================================================================
# 工具集：流量高级分析
# ======================================================================

@mcp.tool()
def packets_export(flow_id: int, fmt: str = "json") -> str:
    """导出单个流量为指定格式。

    Args:
        flow_id: 流量 ID
        fmt: 格式 curl|python-requests|postman|json|csv
    """
    res = _api("GET", f"/flows/{flow_id}")
    flow, err = _ok(res)
    if err:
        return _error(err)
    flow = flow if isinstance(flow, dict) else {}
    fmt = fmt.lower()
    if fmt == "curl":
        return _build_curl(flow)
    if fmt == "python-requests":
        return _flow_to_python_requests(flow)
    if fmt == "postman":
        return _to_text(_flow_to_postman(flow))
    if fmt == "csv":
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["id", "timestamp", "method", "host", "path", "url",
                    "status_code", "duration_ms", "size", "process_name", "pid", "protocol"])
        w.writerow([flow.get("id", ""), flow.get("timestamp", ""), flow.get("method", ""),
                    flow.get("host", ""), flow.get("path", ""), flow.get("url", ""),
                    flow.get("status_code", ""), flow.get("duration_ms", ""), flow.get("size", ""),
                    flow.get("process_name", ""), flow.get("pid", ""), flow.get("protocol", "http")])
        return buf.getvalue()
    return _to_text(flow)


def _build_curl(flow: dict) -> str:
    """构建 curl 命令。"""
    method = flow.get("method", "GET")
    url = flow.get("url") or flow.get("request_url") or ""
    headers = _parse_headers(flow.get("request_headers") or "")
    for k in list(headers.keys()):
        if k.lower() in ("host", "content-length", "connection"):
            del headers[k]
    body = flow.get("request_body") or ""
    parts = ["curl", "-X", method]
    for k, v in headers.items():
        parts += ["-H", f'"{k}: {v}"']
    if body:
        if body.startswith("base64:"):
            import base64
            try:
                raw = base64.b64decode(body[7:])
                parts += ["--data-binary", f'@<(echo {raw!r})']
            except Exception:  # noqa: BLE001
                parts += ["--data", body]
        else:
            parts += ["--data", body]
    parts.append(f'"{url}"')
    return " ".join(parts)


def _flow_to_python_requests(flow: dict) -> str:
    """转 python-requests 脚本。"""
    method = flow.get("method", "GET")
    url = flow.get("url") or ""
    headers = _parse_headers(flow.get("request_headers") or "")
    for k in list(headers.keys()):
        if k.lower() in ("host", "content-length", "connection"):
            del headers[k]
    body = flow.get("request_body") or ""
    lines = ["import requests", "import base64", "",
             f"url = {url!r}", f"headers = {headers!r}", ""]
    if body:
        if body.startswith("base64:"):
            b64 = body[7:]
            lines.append(f"data = base64.b64decode({b64!r})")
            lines.append(f"resp = requests.{method.lower()}(url, headers=headers, data=data, timeout=30, verify=False)")
        else:
            try:
                parsed = json.loads(body)
                lines.append(f"json_body = {parsed!r}")
                lines.append(f"resp = requests.{method.lower()}(url, headers=headers, json=json_body, timeout=30, verify=False)")
            except Exception:  # noqa: BLE001
                lines.append(f"data = {body!r}")
                lines.append(f"resp = requests.{method.lower()}(url, headers=headers, data=data, timeout=30, verify=False)")
    else:
        lines.append(f"resp = requests.{method.lower()}(url, headers=headers, timeout=30, verify=False)")
    lines.append("print(resp.status_code, resp.text[:500])")
    return "\n".join(lines)


def _flow_to_postman(flow: dict) -> dict:
    """转 Postman Collection v2.1 单 item。"""
    method = flow.get("method", "GET")
    url = flow.get("url") or ""
    headers = _parse_headers(flow.get("request_headers") or "")
    header_list = [{"key": k, "value": v, "type": "text"} for k, v in headers.items()]
    body = flow.get("request_body") or ""
    item = {
        "name": f"{method} {url}",
        "request": {
            "method": method,
            "header": header_list,
            "url": {"raw": url, "protocol": url.split("://")[0] if "://" in url else "",
                    "host": url.split("://")[1].split("/")[0] if "://" in url else url},
        },
    }
    if body:
        item["request"]["body"] = {"mode": "raw", "raw": body}
    return {"info": {"name": "Telnix Export", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
            "item": [item]}


@mcp.tool()
def packets_tag(
    flow_id: int = 0,
    add: str = "",
    remove: str = "",
    clear: bool = False,
    note: str = "",
    clear_note: bool = False,
    list_all: bool = False,
) -> str:
    """流量标签管理。

    Args:
        flow_id: 流量 ID（list_all=true 时可省略）
        add: 添加标签
        remove: 移除标签
        clear: 清空所有标签
        note: 设置备注
        clear_note: 清除备注
        list_all: 列出全局所有标签及每标签的 flow 数
    """
    if list_all:
        res = _api("GET", "/flows/tags")
        data, err = _ok(res)
        if err:
            return _error(err)
        tags = data.get("tags", []) if isinstance(data, dict) else data
        return _result(tags)
    if not flow_id:
        return _error("需要 flow_id 或 list_all=true")
    res = _api("GET", f"/flows/{flow_id}")
    flow, err = _ok(res)
    if err:
        return _error(err)
    if not isinstance(flow, dict):
        return _error("无法获取 flow")
    existing_tags = [t.strip() for t in (flow.get("tags") or "").split(",") if t.strip()]
    if clear:
        new_tags = []
    elif add:
        if add not in existing_tags:
            existing_tags.append(add)
        new_tags = existing_tags
    elif remove:
        new_tags = [t for t in existing_tags if t != remove]
    else:
        if note is None and not clear_note:
            return _result({"flow_id": flow_id, "tags": flow.get("tags") or "",
                            "tag_note": flow.get("tag_note") or ""})
        new_tags = existing_tags
    body = {"tags": ",".join(new_tags)}
    if note:
        body["tag_note"] = note
    elif clear_note:
        body["tag_note"] = ""
    res = _api("PATCH", f"/flows/{flow_id}/tags", body)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def packets_diff(flow_id1: int, flow_id2: int, field: str = "response_body") -> str:
    """对比两条流量的指定字段，输出 unified diff。

    Args:
        flow_id1: 第一条流量 ID
        flow_id2: 第二条流量 ID
        field: 对比字段 response_body|request_body|request_headers|response_headers
    """
    import difflib
    res1 = _api("GET", f"/flows/{flow_id1}")
    f1, err = _ok(res1)
    if err:
        return _error(err)
    res2 = _api("GET", f"/flows/{flow_id2}")
    f2, err = _ok(res2)
    if err:
        return _error(err)
    v1 = (f1.get(field) if isinstance(f1, dict) else "") or ""
    v2 = (f2.get(field) if isinstance(f2, dict) else "") or ""
    # JSON 字段尝试格式化
    if field.endswith("_body") and v1 and v2:
        try:
            v1 = json.dumps(json.loads(v1), ensure_ascii=False, indent=2, sort_keys=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            v2 = json.dumps(json.loads(v2), ensure_ascii=False, indent=2, sort_keys=True)
        except Exception:  # noqa: BLE001
            pass
    lines1 = v1.splitlines(keepends=False) if isinstance(v1, str) else [str(v1)]
    lines2 = v2.splitlines(keepends=False) if isinstance(v2, str) else [str(v2)]
    diff = list(difflib.unified_diff(lines1, lines2,
                                     fromfile=f"#{flow_id1}.{field}", tofile=f"#{flow_id2}.{field}",
                                     lineterm=""))
    return _result({"id1": flow_id1, "id2": flow_id2, "field": field,
                    "diff": "\n".join(diff), "identical": not diff})


@mcp.tool()
def packets_endpoints(
    session: int = 0,
    limit: int = 2000,
    host: str = "",
    keep_query: bool = False,
    sample_strategy: str = "first",
) -> str:
    """唯一 endpoint 提取（path 模板归一化，画 API 地图）。

    Args:
        session: 会话 ID（0=跨会话）
        limit: 拉取流量上限（0=不限）
        host: 按主机过滤
        keep_query: 保留 query 参数名
        sample_strategy: 采样策略 first|last|random
    """
    flows, err = _fetch_flows_for_analysis(session, limit, host)
    if err:
        return _error(err)
    endpoints: dict[str, dict] = {}
    for f in flows:
        if not isinstance(f, dict) or f.get("protocol") in ("tcp", "udp"):
            continue
        tpl, qkeys = _path_template(f.get("path") or "", keep_query=keep_query)
        method = f.get("method") or "-"
        key = f"{method} {f.get('host') or ''}{tpl}"
        ep = endpoints.setdefault(key, {"method": method, "host": f.get("host"),
                                         "path_template": tpl, "count": 0,
                                         "status_set": [], "sample_ids": [],
                                         "_all_ids": [], "query_keys": qkeys})
        ep["count"] += 1
        sc = f.get("status_code")
        if sc and sc not in ep["status_set"]:
            ep["status_set"].append(sc)
        ep["_all_ids"].append(f.get("id"))
        for qk in qkeys:
            if qk not in ep["query_keys"]:
                ep["query_keys"].append(qk)
    import random as _random
    for ep in endpoints.values():
        all_ids = ep.pop("_all_ids", [])
        if sample_strategy == "last":
            ep["sample_ids"] = all_ids[-3:]
        elif sample_strategy == "random":
            ep["sample_ids"] = _random.sample(all_ids, min(3, len(all_ids))) if all_ids else []
        else:
            ep["sample_ids"] = all_ids[:3]
    out = list(endpoints.values())
    out.sort(key=lambda x: -x["count"])
    return _result(out)


@mcp.tool()
def packets_timeline(
    session: int = 0,
    limit: int = 2000,
    host: str = "",
    gap_seconds: float = 1.0,
) -> str:
    """流量时间线（按时间排序，标注大间隔段落）。

    Args:
        session: 会话 ID（0=跨会话）
        limit: 拉取流量上限
        host: 按主机过滤
        gap_seconds: 大间隔阈值（秒），超过此值算新段落
    """
    flows, err = _fetch_flows_for_analysis(session, limit, host)
    if err:
        return _error(err)
    from datetime import datetime
    items = []
    for f in flows:
        if not isinstance(f, dict):
            continue
        items.append({"id": f.get("id"), "ts": f.get("timestamp"), "method": f.get("method"),
                      "host": f.get("host"), "path": f.get("path"), "status": f.get("status_code"),
                      "duration_ms": f.get("duration_ms")})
    items.sort(key=lambda x: x.get("ts") or "")
    prev_dt = None
    segment = 0
    for it in items:
        try:
            dt = datetime.fromisoformat(it["ts"].replace("Z", "+00:00")) if it.get("ts") else None
        except Exception:  # noqa: BLE001
            dt = None
        if dt and prev_dt:
            gap = (dt - prev_dt).total_seconds()
            if gap > gap_seconds:
                segment += 1
                it["segment_break"] = True
                it["gap_seconds"] = round(gap, 2)
        prev_dt = dt or prev_dt
        it["segment"] = segment
    return _result({"timeline": items, "count": len(items),
                    "segments": segment + 1, "gap_threshold": gap_seconds})


@mcp.tool()
def packets_trace(
    flow_id: int,
    min_length: int = 4,
    limit: int = 500,
    search_all: bool = False,
) -> str:
    """请求依赖链 trace：从指定 flow 的响应提取字符串值，在后续流量的请求里搜索。

    Args:
        flow_id: 源流量 ID
        min_length: 最小字符串长度（过滤短串减少误报）
        limit: 拉取后续流量上限
        search_all: true=跨会话扫描, false=仅当前会话
    """
    res = _api("GET", f"/flows/{flow_id}")
    src, err = _ok(res)
    if err:
        return _error(err)
    if not isinstance(src, dict):
        return _error("无法获取源 flow")
    values = _extract_trace_values(src.get("response_body") or "", min_length=min_length)
    if not values:
        return _result({"traced": True, "source_flow": flow_id, "dependencies": [],
                        "hint": "源 flow 响应无可追踪字符串"})
    if search_all:
        res = _api("GET", f"/flows/all?limit={limit}&offset=0&since_id={flow_id}", timeout=60)
    else:
        sid, err = _get_session_id(0)
        if err:
            return _error(err)
        res = _api("GET", f"/sessions/{sid}/flows?limit={limit}&offset=0&since_id={flow_id}", timeout=60)
    data, err = _ok(res)
    if err:
        return _error(err)
    flows = data.get("flows", []) if isinstance(data, dict) else data
    deps = []
    for f in flows:
        if not isinstance(f, dict):
            continue
        fid = f.get("id")
        if not fid or fid == flow_id:
            continue
        req_headers = f.get("request_headers") or ""
        req_body = f.get("request_body") or ""
        if not isinstance(req_headers, str):
            req_headers = json.dumps(req_headers, ensure_ascii=False)
        if not isinstance(req_body, str):
            req_body = json.dumps(req_body, ensure_ascii=False)
        search_text = req_headers + "\n" + req_body
        for v in values:
            if v.get("value") and v["value"] in search_text:
                deps.append({"source_flow": flow_id, "target_flow": fid,
                             "field": v.get("field", ""), "value": v["value"]})
    return _result({"traced": True, "source_flow": flow_id, "dependencies": deps, "count": len(deps)})


@mcp.tool()
def packets_analyze(
    flow_ids: str = "",
    search_all: bool = False,
    limit: int = 500,
) -> str:
    """签名字段自动检测：对比多条同接口请求的 JSON body，找可疑签名/token 字段。

    Args:
        flow_ids: 流量 ID 列表，逗号分隔（至少 2 条；search_all 时至少 1 条作为源）
        search_all: true=以第一个 ID 为源，从 /flows/all 拉后续流量
        limit: search_all 模式下拉取流量上限
    """
    ids = [int(x.strip()) for x in flow_ids.split(",") if x.strip()] if flow_ids else []
    if search_all:
        if not ids:
            return _error("analyze search_all 至少需要 1 个源 flow ID")
        src_id = ids[0]
        res = _api("GET", f"/flows/all?limit={limit}&offset=0&since_id={src_id}", timeout=60)
        data, err = _ok(res)
        if err:
            return _error(err)
        flows = data.get("flows", []) if isinstance(data, dict) else data
        # 确保源 flow 在内
        src_res = _api("GET", f"/flows/{src_id}")
        src_flow, _ = _ok(src_res)
        if isinstance(src_flow, dict):
            flows = [src_flow] + [f for f in flows if f.get("id") != src_id]
    else:
        if len(ids) < 2:
            return _error("analyze 至少需要 2 个 flow ID（或用 search_all=true）")
        flows = []
        for fid in ids:
            res = _api("GET", f"/flows/{fid}")
            flow, _ = _ok(res)
            if isinstance(flow, dict):
                flows.append(flow)
    if len(flows) < 2:
        return _error("成功拉取的 flow 不足 2 条，无法对比")

    field_values: dict[str, list] = {}
    for f in flows:
        body = f.get("request_body") or ""
        if not body or body.startswith("base64:"):
            continue
        try:
            obj = json.loads(body)
        except Exception:  # noqa: BLE001
            continue
        for path, val in _walk_json_leaves(obj):
            field_values.setdefault(path, []).append(val)

    findings = []
    for path, vals in field_values.items():
        if len(vals) < 2:
            continue
        str_vals = [str(v) for v in vals]
        lengths = sorted(set(len(v) for v in str_vals))
        charsets = sorted(set(_classify_charset(v) for v in str_vals))
        varies = len(set(str_vals)) > 1
        score = 0.0
        if varies:
            score = 0.34
            if len(lengths) == 1:
                score += 0.33
            if len(charsets) == 1 and charsets[0] in ("hex", "base64", "alphanumeric"):
                score += 0.33
        findings.append({"field_path": path, "lengths": lengths, "charsets": charsets,
                         "varies": varies, "sample_values": str_vals[:5],
                         "suspicion_score": round(min(1.0, score), 2)})
    findings.sort(key=lambda x: -x["suspicion_score"])
    return _result(findings)


# ======================================================================
# 工具集：拦截规则高级管理
# ======================================================================

@mcp.tool()
def intercept_update(
    rule_id: int,
    match: str = "",
    action: str = "",
    note: str = "",
    enable: bool = False,
    disable: bool = False,
) -> str:
    """修改现有规则（不删除重建）。

    Args:
        rule_id: 规则 ID
        match: 新的匹配表达式（空=不改）
        action: 新的动作规范（空=不改）
        note: 新备注（空=不改）
        enable: 启用规则
        disable: 禁用规则
    """
    res = _api("GET", "/auto-reply/rules")
    rules, err = _ok(res)
    if err:
        return _error(err)
    rules = rules if isinstance(rules, list) else []
    target = None
    for r in rules:
        if isinstance(r, dict) and str(r.get("id")) == str(rule_id):
            target = r
            break
    if not target:
        return _error(f"规则不存在: {rule_id}")
    body = dict(target)
    body.pop("id", None)
    updated_fields = []
    if match:
        try:
            m = parse_match(match)
        except ValueError as e:
            return _error(str(e))
        body["pattern"] = m["pattern"]
        body["match_mode"] = m["match_mode"]
        updated_fields.extend(["pattern", "match_mode"])
    if action:
        try:
            a = parse_action(action)
        except ValueError as e:
            return _error(str(e))
        if a.get("action"):
            body["action"] = a["action"]
        if a.get("mock_status") is not None:
            body["mock_status"] = a["mock_status"]
        if a.get("mock_headers") is not None:
            body["mock_headers"] = a["mock_headers"]
        if a.get("mock_body") is not None:
            body["mock_body"] = a["mock_body"]
        if a.get("modify_rules") is not None:
            body["modify_rules"] = a["modify_rules"]
        updated_fields.append("action")
    if note:
        body["note"] = note
        updated_fields.append("note")
    if enable:
        body["enabled"] = True
        updated_fields.append("enabled")
    elif disable:
        body["enabled"] = False
        updated_fields.append("enabled")
    res = _api("PUT", f"/auto-reply/rules/{rule_id}", body)
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"updated": True, "rule_id": rule_id, "fields": updated_fields})


@mcp.tool()
def intercept_export() -> str:
    """导出所有规则为 JSON（直接返回内容，不写文件）。"""
    res = _api("GET", "/auto-reply/rules")
    rules, err = _ok(res)
    if err:
        return _error(err)
    rules = rules if isinstance(rules, list) else []
    import time as _time
    return _to_text({"rules": rules, "exported_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "count": len(rules)})


@mcp.tool()
def intercept_import(
    rules_json: str,
    mode: str = "merge",
) -> str:
    """从 JSON 字符串导入规则。

    Args:
        rules_json: JSON 字符串，格式 {"rules": [...]} 或 [...]（直接用 intercept_export 的输出）
        mode: merge=追加, replace=先清空再导入
    """
    try:
        data = json.loads(rules_json)
    except json.JSONDecodeError as e:
        return _error(f"JSON 解析失败: {e}")
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        return _error("JSON 格式错误：缺少 rules 数组")
    deleted = 0
    if mode == "replace":
        res = _api("GET", "/auto-reply/rules")
        existing, _ = _ok(res)
        existing = existing if isinstance(existing, list) else []
        existing_ids = [r.get("id") for r in existing if isinstance(r, dict) and r.get("id")]
        if existing_ids:
            _api("POST", "/auto-reply/rules/batch-delete", {"ids": existing_ids})
            deleted = len(existing_ids)
    created = 0
    results = []
    for idx, r in enumerate(rules):
        if not isinstance(r, dict):
            results.append({"index": idx, "ok": False, "error": "非对象"})
            continue
        body = {k: v for k, v in r.items() if k != "id"}
        body["enabled"] = r.get("enabled", True)
        res = _api("POST", "/auto-reply/rules", body)
        if res.get("code") == 0:
            created += 1
            results.append({"index": idx, "ok": True, "pattern": body.get("pattern", "")})
        else:
            results.append({"index": idx, "ok": False, "error": res.get("msg", "API失败"),
                            "pattern": body.get("pattern", "")})
    return _result({"imported": True, "mode": mode, "created": created, "deleted_old": deleted,
                    "failed": len(rules) - created, "total_in_file": len(rules), "results": results})


@mcp.tool()
def intercept_template_list() -> str:
    """列出所有内置规则模板。"""
    res = _api("GET", "/templates")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data if isinstance(data, list) else [])


@mcp.tool()
def intercept_template_apply(
    name: str,
    match: str,
    note: str = "",
    disabled: bool = False,
    method_filter: str = "",
    status_filter: str = "",
    pid_filter: str = "",
    process_filter: str = "",
) -> str:
    """应用规则模板创建规则。

    Args:
        name: 模板名称
        match: 匹配表达式
        note: 备注
        disabled: true=创建为禁用状态
        method_filter: 方法过滤
        status_filter: 状态码过滤
        pid_filter: PID 过滤
        process_filter: 进程名过滤
    """
    body: dict = {"pattern": match, "match_mode": "wildcard",
                  "note": note, "enabled": not disabled}
    for fk in ("method_filter", "status_filter", "pid_filter", "process_filter"):
        v = locals().get(fk, "") or ""
        if v:
            body[fk] = v
    res = _api("POST", f"/templates/{name}/apply", body)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


# ======================================================================
# 工具集：进程管理
# ======================================================================

@mcp.tool()
def processes_list(
    name: str = "",
    with_connections: bool = False,
    tree: bool = False,
    include_listen: bool = False,
) -> str:
    """列出有网络连接的进程。

    Args:
        name: 按名称模糊过滤
        with_connections: 包含连接快照
        tree: 进程树模式
        include_listen: 包含监听端口
    """
    params = {}
    if with_connections:
        params["with_connections"] = "1"
    if tree:
        params["tree"] = "1"
    if name:
        params["name"] = name
    if include_listen:
        params["include_listen"] = "1"
    qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    url = "/processes/snapshot" + (f"?{qs}" if qs else "")
    res = _api("GET", url)
    data, err = _ok(res)
    if err:
        # 回退到普通 /processes
        res = _api("GET", "/processes")
        data, err = _ok(res)
        if err:
            return _error(err)
    procs = data if isinstance(data, list) else data.get("processes", [])
    if name:
        nl = name.lower()
        procs = [p for p in procs if isinstance(p, dict) and nl in (p.get("name") or "").lower()]
    return _result(procs)


@mcp.tool()
def processes_ignore(pid: int = 0, name: str = "") -> str:
    """添加忽略进程（不抓该进程的流量）。

    Args:
        pid: 进程 PID（与 name 二选一或都填）
        name: 进程名（如 chrome.exe）
    """
    if not pid and not name:
        return _error("需要 pid 或 name 参数")
    body = {"pid": pid if pid else None, "name": name}
    res = _api("POST", "/processes/ignore", body)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def processes_unignore(row_id: int) -> str:
    """取消忽略进程。

    Args:
        row_id: 忽略列表中的行 ID
    """
    res = _api("DELETE", f"/processes/ignore/{row_id}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def processes_ignored_list() -> str:
    """列出已忽略的进程。"""
    res = _api("GET", "/processes/ignored")
    data, err = _ok(res)
    if err:
        return _error(err)
    items = data if isinstance(data, list) else data.get("items", data.get("processes", []))
    return _result(items)


@mcp.tool()
def processes_ignore_host(host: str) -> str:
    """添加忽略 host 通配符（如 *.example.com）。

    Args:
        host: host 通配符
    """
    res = _api("POST", "/processes/ignore-host", {"host": host})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def processes_unignore_host(row_id: int) -> str:
    """取消忽略 host。

    Args:
        row_id: 忽略 host 列表中的行 ID
    """
    res = _api("DELETE", f"/processes/ignore-host/{row_id}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def processes_ignored_hosts_list() -> str:
    """列出已忽略的 host。"""
    res = _api("GET", "/processes/ignored-hosts")
    data, err = _ok(res)
    if err:
        return _error(err)
    items = data if isinstance(data, list) else data.get("items", data.get("hosts", []))
    return _result(items)


# ======================================================================
# 工具集：会话导出
# ======================================================================

@mcp.tool()
def session_export(
    session: int = 0,
    fmt: str = "har",
) -> str:
    """导出会话为指定格式（直接返回内容，不写文件）。

    Args:
        session: 会话 ID（0=当前活动会话）
        fmt: 格式 har|json|csv|python-requests|postman|curl
    """
    sid, err = _get_session_id(session)
    if err:
        return _error(err)
    res = _api("POST", f"/export/{sid}", {"format": fmt}, timeout=60)
    data, err = _ok(res)
    if err:
        return _error(err)
    content = data.get("content") if isinstance(data, dict) else data
    if isinstance(content, str):
        if len(content.encode("utf-8")) > MAX_OUTPUT_BYTES:
            cut = int(MAX_OUTPUT_BYTES * 0.9)
            content = content[:cut] + f"\n... [已截断，原始 {len(content)} 字符]"
        return content
    return _to_text(content)


# ======================================================================
# 工具集：透明代理
# ======================================================================

@mcp.tool()
def transparent_proxy_status() -> str:
    """查看透明代理状态（运行中/已重定向包数/NAT 表大小/错误）。

    透明代理用 WinDivert NETWORK 层重定向出站 HTTP(80)/HTTPS(443) 流量到本地代理，
    应用无需配置代理即可被抓包。返回字段：
    running（是否运行中）、redirected_count（已重定向包数）、nat_table_size（NAT 表大小）、
    last_error（最近错误）、supported（平台是否支持）。
    """
    res = _api("GET", "/transparent-proxy/status")
    data, err = _ok(res)
    if err:
        return _error(err)
    if isinstance(data, dict):
        if not data.get("supported"):
            data["hint"] = "透明代理仅 Windows 可用（依赖 WinDivert）"
        elif not data.get("running") and data.get("last_error"):
            le = data.get("last_error", "")
            if "管理员" in le or "admin" in le.lower():
                data["hint"] = "需要管理员权限。调用 system_restart_as_admin 以管理员身份重启后端"
    return _result(data)


@mcp.tool()
def transparent_proxy_start() -> str:
    """启动透明代理（WinDivert NETWORK 层重定向，需管理员权限）。

    将出站 HTTP(80)/HTTPS(443) 流量重定向到本地代理端口，应用无需配置代理即可被抓包。
    需要 Windows + 管理员权限 + pydivert。
    失败时返回明确错误（如未提权会提示调用 system_restart_as_admin）。
    首次启用未确认 WinDivert 风险提示时，自动触发桌面置顶原生弹窗。
    """
    # 首次启用未确认 WinDivert 风险提示时，自动触发桌面置顶原生弹窗
    res = _api_with_windivert_ack("POST", "/transparent-proxy/start")
    data, err = _ok(res)
    if err:
        if "管理员" in err or "admin" in err.lower():
            return _error(err,
                          hint="需要管理员权限。调用 system_restart_as_admin 以管理员身份重启后端")
        return _error(err)
    return _result(data)


@mcp.tool()
def transparent_proxy_stop() -> str:
    """停止透明代理。"""
    res = _api("POST", "/transparent-proxy/stop")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


# ======================================================================
# 工具集：自动修改规则（Python 脚本友好）
# ======================================================================

@mcp.tool()
def auto_reply_list() -> str:
    """列出所有自动修改规则（含命中统计）。

    与 intercept_list 等价，返回所有规则。每条规则含：
    id/rule_id、pattern、action、enabled、hit_count、last_hit_at、last_hit_flow_id 等。
    """
    res = _api("GET", "/auto-reply/rules")
    data, err = _ok(res)
    if err:
        return _error(err)
    rules = data if isinstance(data, list) else []
    for r in rules:
        if isinstance(r, dict) and "rule_id" not in r:
            r["rule_id"] = r.get("id")
    return _result(rules)


@mcp.tool()
def auto_reply_get(rule_id: str) -> str:
    """查看规则详情。

    Args:
        rule_id: 规则 ID
    """
    res = _api("GET", "/auto-reply/rules")
    data, err = _ok(res)
    if err:
        return _error(err)
    rules = data if isinstance(data, list) else []
    for r in rules:
        if isinstance(r, dict) and str(r.get("id")) == str(rule_id):
            return _result(r)
    return _error(f"规则不存在: {rule_id}")


@mcp.tool()
def auto_reply_create(
    pattern: str,
    action: str = "script",
    script_path: str = "",
    script: str = "",
    action_spec: str = "",
    match_mode: str = "wildcard",
    note: str = "",
    method_filter: str = "",
    status_filter: str = "",
    pid_filter: str = "",
    process_filter: str = "",
    disabled: bool = False,
) -> str:
    """创建自动修改规则（支持从本地 .py 文件加载 Python 脚本）。

    与 intercept_add 互补：本工具专注 Python 脚本规则的创建，
    通过 script_path 参数从本地 .py 文件加载脚本内容，方便 agent 操作。

    Args:
        pattern: URL 匹配 pattern（如 *api.example.com*/v1/*）
        action: 动作类型: script=Python脚本, mock=伪造响应, modify_response=改响应,
            modify_request=改请求, mock_request=写死请求转发
        script_path: action=script 时，从本地 .py 文件加载脚本内容（agent 友好，与 script 互斥）
        script: action=script 时，内联 Python 脚本源码（与 script_path 互斥）
        action_spec: 非 script 动作时，动作规范字符串（如 'set-json key value' / 'mock 200 {}'），
            复用 intercept_add 的语法
        match_mode: 匹配模式 wildcard|exact|regex（默认 wildcard）
        note: 规则备注
        method_filter: 方法过滤（逗号分隔）
        status_filter: 状态码过滤（逗号分隔）
        pid_filter: PID 过滤
        process_filter: 进程名过滤
        disabled: true=创建为禁用状态
    """
    body: dict = {
        "enabled": not disabled,
        "match_mode": match_mode,
        "pattern": pattern,
        "action": action,
        "note": note,
        "method_filter": method_filter,
        "status_filter": status_filter,
        "pid_filter": pid_filter,
        "process_filter": process_filter,
    }
    if action == "script":
        if script_path:
            try:
                with open(script_path, "r", encoding="utf-8") as f:
                    source = f.read()
            except OSError as e:
                return _error(f"读取脚本文件失败: {e}")
            body["modify_rules"] = source
        elif script:
            body["modify_rules"] = script
        else:
            return _error("action=script 时需要 script_path 或 script 参数")
        body["mock_status"] = None
        body["mock_headers"] = {}
        body["mock_body"] = ""
    else:
        if not action_spec:
            return _error(f"action={action} 时需要 action_spec 参数（如 'set-json key value'）")
        try:
            a = parse_action(action_spec)
        except ValueError as e:
            return _error(str(e))
        if a.get("action"):
            body["action"] = a["action"]
        body["mock_status"] = a.get("mock_status")
        body["mock_headers"] = a.get("mock_headers")
        body["mock_body"] = a.get("mock_body", "")
        body["modify_rules"] = a.get("modify_rules", [])
    res = _api("POST", "/auto-reply/rules", body)
    created, err = _ok(res)
    if err:
        return _error(err)
    return _result({"created": True,
                    "rule_id": created.get("id") if isinstance(created, dict) else None,
                    "pattern": pattern, "action": body["action"]})


@mcp.tool()
def auto_reply_enable(rule_id: str) -> str:
    """启用规则。

    Args:
        rule_id: 规则 ID
    """
    res = _api("PUT", f"/auto-reply/rules/{rule_id}", {"enabled": True})
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"rule_id": rule_id, "enabled": True})


@mcp.tool()
def auto_reply_disable(rule_id: str) -> str:
    """禁用规则。

    Args:
        rule_id: 规则 ID
    """
    res = _api("PUT", f"/auto-reply/rules/{rule_id}", {"enabled": False})
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"rule_id": rule_id, "enabled": False})


@mcp.tool()
def auto_reply_delete(rule_id: str) -> str:
    """删除规则。

    Args:
        rule_id: 规则 ID
    """
    res = _api("DELETE", f"/auto-reply/rules/{rule_id}")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"deleted": True, "rule_id": rule_id})


@mcp.tool()
def auto_reply_test_script(
    script: str = "",
    script_path: str = "",
    mock_host: str = "api.example.com",
    mock_path: str = "/v1/user",
    mock_method: str = "GET",
    mock_scheme: str = "https",
    mock_http_version: str = "HTTP/1.1",
    mock_headers: dict = None,
    mock_body: str = "",
    mock_resp_status: int = None,
    mock_resp_headers: dict = None,
    mock_resp_body: str = "",
) -> str:
    """测试 Python 脚本执行（不创建规则，用 mock 数据走 worker 子进程）。

    agent 在用 auto_reply_create 创建脚本规则前，可先用本工具验证脚本逻辑：
    用 mock 请求/响应数据调用 on_request / on_response 钩子，查看脚本执行结果、
    是否报错、是否对请求/响应做了修改。

    Args:
        script: 内联 Python 脚本源码（与 script_path 互斥）
        script_path: 从本地 .py 文件加载脚本（与 script 互斥，agent 友好）
        mock_host: mock 请求 host
        mock_path: mock 请求 path
        mock_method: mock 请求方法（GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS）
        mock_scheme: mock 请求 scheme（http/https）
        mock_http_version: mock HTTP 版本（HTTP/1.1）
        mock_headers: mock 请求 headers（dict）
        mock_body: mock 请求 body 字符串
        mock_resp_status: mock 响应状态码（提供则同时调用 on_response 钩子）
        mock_resp_headers: mock 响应 headers（dict）
        mock_resp_body: mock 响应 body 字符串

    Returns:
        测试结果 JSON：{ok, duration_ms, error, traceback, request_phase, response_phase}
        - ok: 脚本是否执行成功
        - duration_ms: 总耗时（毫秒）
        - error: 错误信息（如有）
        - traceback: 异常 traceback（如有）
        - request_phase: on_request 阶段结果（action/modified/headers/body/error）
        - response_phase: on_response 阶段结果（仅当提供 mock_resp_* 时返回）
    """
    # 优先用 script_path 加载本地文件
    if script_path:
        import os
        if not os.path.isfile(script_path):
            return _error(f"脚本文件不存在: {script_path}")
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                script_source = f.read()
        except OSError as e:
            return _error(f"读取脚本文件失败: {e}")
    elif script:
        script_source = script
    else:
        return _error("需要提供 script 或 script_path 参数")

    body = {
        "script": script_source,
        "mock_request": {
            "host": mock_host,
            "path": mock_path,
            "method": mock_method.upper(),
            "scheme": mock_scheme,
            "http_version": mock_http_version,
            "headers": mock_headers or {},
            "body": mock_body or "",
        },
    }
    # 可选：mock_response
    if mock_resp_status is not None:
        body["mock_response"] = {
            "status_code": int(mock_resp_status),
            "headers": mock_resp_headers or {},
            "body": mock_resp_body or "",
        }
    res = _api("POST", "/auto-reply/test-script", body, timeout=15.0)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


# ======================================================================
# 主入口
# ======================================================================

def main():
    import argparse
    p = argparse.ArgumentParser(description="Telnix MCP Server")
    p.add_argument("--base-url", default="", help="后端 API 地址（默认从 TELNIX_API 环境变量或 127.0.0.1:18901）")
    args = p.parse_args()
    if args.base_url:
        global BASE_URL
        BASE_URL = args.base_url.rstrip("/")
    # STDIO 模式：绝不写 stdout，日志走 stderr
    print(f"[telnix-mcp] starting, base_url={BASE_URL}", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

"""Telnix MCP Server — Exposes Telnix's capture/intercept/modify capabilities to MCP clients.

Built on the MCP Python SDK (FastMCP), communicates with MCP clients via stdio.
Reuses the Telnix backend HTTP API (default http://127.0.0.1:18901), does not touch the database directly.

Design principles:
1. Tools return JSON strings (text content); errors are also wrapped as JSON rather than thrown
2. Large output is auto-truncated (default 64KB) to prevent oversized MCP messages from freezing clients
3. Parameter schema is auto-generated from Python type annotations + docstring
4. Never writes to stdout (STDIO protocol constraint); logs go to stderr
5. Pure HTTP calls, no local file/process operations (except agent_workspace which needs a backup file)

Startup:
    python -m telnix.mcp_server
    python -m telnix.mcp_server --base-url http://127.0.0.1:18901
    set TELNIX_API=http://127.0.0.1:18901 && python -m telnix.mcp_server

MCP client config example (claude_desktop_config.json):
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
import urllib.parse
from typing import Any, Optional

import httpx

# MCP SDK
from mcp.server.fastmcp import FastMCP

# ---------- Config ----------

DEFAULT_HOST = "127.0.0.1"
try:
    from .config import DEFAULT_PORT as _CFG_PORT
    DEFAULT_PORT = _CFG_PORT
except Exception:  # noqa: BLE001
    DEFAULT_PORT = 18901
BASE_URL = os.environ.get("TELNIX_API", f"http://{DEFAULT_HOST}:{DEFAULT_PORT}").rstrip("/")

# Output truncation threshold (bytes). Oversized MCP tool returns freeze clients; 64KB is a safe upper bound.
# 设计修复：从环境变量读取，支持部署时配置
MAX_OUTPUT_BYTES = int(os.environ.get("TELNIX_MCP_MAX_OUTPUT_BYTES", 64 * 1024))
# Single-flow field truncation (e.g. response_body can be several MB)
MAX_FIELD_BYTES = int(os.environ.get("TELNIX_MCP_MAX_FIELD_BYTES", 16 * 1024))

# ---------- MCP instance ----------

mcp = FastMCP("telnix")


# ---------- HTTP client ----------

def _api(method: str, path: str, body: Any = None, timeout: float = 30.0) -> dict:
    """Call backend API, returns {code, data, msg}. On failure does not sys.exit, returns error dict."""
    url = f"{BASE_URL}/api{path}"
    headers = {"Accept": "application/json"}
    content = None
    if body is not None:
        content = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
            resp = client.request(method, url, content=content, headers=headers)
            try:
                return resp.json()
            except Exception:
                return {"code": 0, "data": resp.text, "msg": "ok"}
    except httpx.HTTPStatusError as e:
        try:
            return e.response.json()
        except Exception:  # noqa: BLE001
            return {"code": e.response.status_code, "msg": f"HTTP {e.response.status_code}: {e.response.reason_phrase}", "data": None}
    except httpx.ConnectError as e:
        return {"code": -1, "msg": f"Cannot connect to backend {BASE_URL}: {e}", "data": None,
                "hint": "Backend not started? Run: cd src\\host && python -m telnix"}
    except Exception as e:  # noqa: BLE001
        return {"code": -1, "msg": f"Connection error: {e}", "data": None}


def _ok(res: dict) -> tuple[Optional[Any], Optional[str]]:
    """Extract data. Returns (data, None) on success, (None, error_msg) on failure."""
    if res.get("code") == 0:
        return res.get("data"), None
    msg = res.get("msg") or "Unknown error"
    hint = _hint_for_error(msg)
    if hint:
        msg = f"{msg} | Fix suggestion: {hint}"
    return None, msg


def _api_with_windivert_ack(method: str, path: str, body: Any = None,
                            timeout: float = 30.0) -> dict:
    """Call APIs that may trigger WinDivert loading (raw/transparent-proxy start).

    If the backend returns need_ack=true (first enable not yet confirmed), auto-trigger
    the desktop topmost native dialog flow:
    1. POST /system/request-windivert-ack creates a pending request + shows a native Yes/No dialog
    2. Long-poll /system/windivert-ack-request/{rid}/wait for user response
    3. User picks "Yes" -> ack persisted -> retry original request and return result
    4. User picks "No" -> return an error response containing need_ack=true (caller handles via _ok)

    On non-Windows platforms / already acked, the backend will not return need_ack, so this
    function behaves identically to _api.
    """
    res = _api(method, path, body, timeout=timeout)
    # Detect whether WinDivert risk acknowledgement is required
    if not (res.get("need_ack") is True or
            (isinstance(res.get("data"), dict) and res["data"].get("need_ack"))):
        return res
    # Trigger the native dialog flow
    ack_res = _api("POST", "/system/request-windivert-ack", timeout=10.0)
    if ack_res.get("code") != 0:
        return ack_res
    ack_data = ack_res.get("data") or {}
    # Non-Windows or already acked: backend returns skipped=true, retry original request directly
    if ack_data.get("skipped"):
        return _api(method, path, body, timeout=timeout)
    rid = ack_data.get("request_id")
    if not rid:
        return res
    # Long-poll: retry up to 3 times (60s each), covering a 3-minute window
    for _ in range(3):
        r = _api("GET", f"/system/windivert-ack-request/{rid}/wait", timeout=65.0)
        if r.get("code") != 0:
            return r
        d = r.get("data") or {}
        status = d.get("status")
        if status == "accepted":
            # User confirmed: retry original request
            return _api(method, path, body, timeout=timeout)
        elif status == "rejected":
            # User rejected: return original error response for caller to handle
            return res
        # status == "pending", continue to next round
    # Timeout: return original error response
    return res


def _hint_for_error(msg: str) -> str | None:
    """Generate an actionable fix suggestion for the agent based on the error message."""
    msg_l = msg.lower()
    if "cannot connect" in msg_l or "connection" in msg_l:
        return "Backend not started? Run: cd src\\host && python -m telnix"
    if "cert" in msg_l:
        return "HTTPS decryption requires a certificate: call the cert_install tool"
    if "pydivert" in msg_l:
        return "TCP/UDP capture requires: pip install pydivert (and run as administrator)"
    if "admin" in msg_l:
        return "Use the system_restart_as_admin tool to restart as administrator"
    if "session" in msg_l and ("not exist" in msg_l or "not found" in msg_l):
        return "Call capture_start first to create a session"
    if "not found" in msg_l:
        return "Backend may not have been restarted; the old process is missing new routes. Call system_restart to restart the backend"
    return None


# ---------- Output handling ----------

def _to_text(obj: Any) -> str:
    """Convert any object to JSON text, truncating if too long."""
    text = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        # Truncate to MAX_OUTPUT_BYTES bytes (approximate by characters)
        cut = int(MAX_OUTPUT_BYTES * 0.9)
        text = text[:cut] + f"\n\n... [output truncated, original size {len(text)} chars, exceeds {MAX_OUTPUT_BYTES} byte limit]"
    return text


def _truncate_fields(obj: Any, fields: list[str], limit: int = MAX_FIELD_BYTES) -> Any:
    """Recursively truncate values of specified fields (e.g. response_body)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in fields and isinstance(v, str) and len(v.encode("utf-8")) > limit:
                out[k] = v[:limit] + f"... [field truncated, original {len(v)} chars]"
            else:
                out[k] = _truncate_fields(v, fields, limit)
        return out
    if isinstance(obj, list):
        return [_truncate_fields(item, fields, limit) for item in obj]
    return obj


def _result(obj: Any) -> str:
    """Standard success result."""
    return _to_text(obj)


def _error(msg: str, **extra) -> str:
    """Standard error result."""
    err = {"ok": False, "error": msg}
    err.update(extra)
    return _to_text(err)


# ---------- Match expression parsing (reuses cli.py logic, drops sys.exit side effects) ----------

def parse_match(expr: str) -> dict:
    """Parse match expression 'key op value && ...', returns {pattern, match_mode, filters, *_filter}."""
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
            raise ValueError(f"Cannot parse match condition: {tok}")
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
    """Client-side filtering (used for dry-run preview)."""
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


# ---------- Action parsing (reuses cli.py logic) ----------

def parse_action(spec: str) -> dict:
    """Parse action spec string, returns backend rule fields."""
    parts = _split_action(spec)
    if not parts:
        raise ValueError("Action spec cannot be empty")
    name = parts[0].lower()
    args = parts[1:]

    # ---- Modify response ----
    if name == "replace-header":
        if len(args) < 2:
            raise ValueError("replace-header requires: K V")
        return {"action": "modify_response",
                "modify_rules": [{"target": "response_header", "op": "replace", "key": args[0], "value": args[1]}]}
    if name == "set-json":
        if len(args) < 2:
            raise ValueError("set-json requires: key value")
        return {"action": "modify_response",
                "modify_rules": [{"target": "response_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}]}
    if name == "set-json-path":
        if len(args) < 2:
            raise ValueError("set-json-path requires: path value")
        return {"action": "modify_response",
                "modify_rules": [{"target": "response_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}]}
    if name == "remove-json":
        if len(args) < 1:
            raise ValueError("remove-json requires: key")
        return {"action": "modify_response",
                "modify_rules": [{"target": "response_body", "op": "remove", "key": args[0]}]}
    if name == "remove-json-path":
        if len(args) < 1:
            raise ValueError("remove-json-path requires: path")
        return {"action": "modify_response",
                "modify_rules": [{"target": "response_body", "op": "remove", "key": args[0]}]}
    if name == "replace-bytes":
        if len(args) < 1:
            raise ValueError("replace-bytes requires: offset:hex")
        return {"action": "modify_response",
                "modify_rules": [{"target": "response_body", "op": "replace-bytes", "key": "", "value": args[0]}]}
    if name == "replace-bytes-regex":
        if len(args) < 2:
            raise ValueError("replace-bytes-regex requires: regex hex")
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

    # ---- Modify request ----
    if name == "set-request-header":
        if len(args) < 2:
            raise ValueError("set-request-header requires: K V")
        return {"action": "modify_request",
                "modify_rules": [{"target": "request_header", "op": "replace", "key": args[0], "value": args[1]}]}
    if name == "set-request-json":
        if len(args) < 2:
            raise ValueError("set-request-json requires: key value")
        return {"action": "modify_request",
                "modify_rules": [{"target": "request_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}]}
    if name == "set-request-json-path":
        if len(args) < 2:
            raise ValueError("set-request-json-path requires: path value")
        return {"action": "modify_request",
                "modify_rules": [{"target": "request_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}]}
    if name == "remove-request-json":
        if len(args) < 1:
            raise ValueError("remove-request-json requires: key")
        return {"action": "modify_request",
                "modify_rules": [{"target": "request_body", "op": "remove", "key": args[0]}]}
    if name == "set-request-body-hex":
        if len(args) < 1:
            raise ValueError("set-request-body-hex requires: hex")
        return {"action": "modify_request",
                "modify_rules": [{"target": "request_body", "op": "replace", "key": "", "value": _hex_to_b64(args[0])}]}
    if name == "replace-request-bytes":
        if len(args) < 1:
            raise ValueError("replace-request-bytes requires: offset:hex")
        return {"action": "modify_request",
                "modify_rules": [{"target": "request_body", "op": "replace-bytes", "key": "", "value": args[0]}]}

    # ---- Timing actions ----
    if name == "delay":
        if len(args) < 1:
            raise ValueError("delay requires: N (milliseconds)")
        return {"action": "modify_response",
                "modify_rules": [{"target": "delay", "op": "sleep", "value": int(args[0])}]}
    if name == "delay-request":
        if len(args) < 1:
            raise ValueError("delay-request requires: N (milliseconds)")
        return {"action": "modify_request",
                "modify_rules": [{"target": "delay-request", "op": "sleep", "value": int(args[0])}]}

    # ---- Python script ----
    if name == "script":
        if not args:
            raise ValueError("script requires: '<inline source>' or script file <path>")
        if args[0] == "file" and len(args) >= 2:
            with open(args[1], "r", encoding="utf-8") as f:
                source = f.read()
        else:
            source = args[0]
        return {"action": "script", "modify_rules": source}

    raise ValueError(f"Unknown action: {name} (supported: set-json/set-json-path/remove-json/replace-header/"
                     f"replace-bytes/replace-bytes-regex/mock/status/drop/mock-request/"
                     f"set-request-header/set-request-json/set-request-json-path/remove-request-json/"
                     f"set-request-body-hex/replace-request-bytes/delay/delay-request/script)")


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


# Session helper
def _get_session_id(session: int = 0) -> tuple[int, Optional[str]]:
    """Get session ID. Returns (sid, None) on success, (0, error) on failure."""
    if session:
        return int(session), None
    env_session = os.environ.get("TELNIX_SESSION", "").strip()
    if env_session:
        try:
            return int(env_session), None
        except ValueError:
            return 0, f"Invalid TELNIX_SESSION env value: {env_session}"
    res = _api("GET", "/status")
    data, err = _ok(res)
    if err:
        return 0, err
    sid = data.get("session_id") if isinstance(data, dict) else None
    if not sid:
        return 0, "No active session, call capture_start first or specify session parameter"
    return int(sid), None


# ======================================================================
# Toolset: Status & capture control
# ======================================================================

@mcp.tool()
def get_status() -> str:
    """Get Telnix backend status (capture state, session, proxy, cert, etc).

    Returns JSON containing capturing/session_id/system_proxy_on/cert_installed and other fields.
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
    """Start capturing. Returns session_id.

    Args:
        layer: Capture layer; http=HTTP proxy only (default), tcp=TCP/UDP only (WinDivert, requires admin), all=capture both
        auto_stop_seconds: Auto-stop capture after N seconds (0=no auto-stop, agent does not need to sleep+stop)
        pid_filter: TCP/UDP mode PID filter, comma-separated
        port_filter: TCP/UDP mode port filter, comma-separated
        bpf_filter: TCP/UDP mode WinDivert filter string
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
        out["hint"] = f"Backend will auto-stop capture after {data['auto_stop_seconds']}s"
    if layer in ("tcp", "all"):
        raw_body: dict = {}
        if pid_filter:
            raw_body["pid_filter"] = [int(p) for p in pid_filter.split(",") if p.strip()]
        if port_filter:
            raw_body["port_filter"] = [int(p) for p in port_filter.split(",") if p.strip()]
        if bpf_filter:
            raw_body["filter_str"] = bpf_filter
        # On first enable without WinDivert risk acknowledgement, auto-trigger desktop topmost native dialog
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
    """Stop capturing (session is preserved, proxy keeps running).

    Args:
        layer: Which layer to stop; http=HTTP only (default), all=also stop TCP/UDP
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
    """Clear all flow records of the current session."""
    res = _api("POST", "/capture/clear")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"cleared": True})


@mcp.tool()
def capture_pause() -> str:
    """Pause capturing (session is preserved, proxy keeps running; differs from stop)."""
    res = _api("POST", "/capture/pause")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result({"paused": True, "session_id": data.get("session_id") if isinstance(data, dict) else None,
                    "hint": "Session preserved, proxy still running. capture_resume to resume, capture_stop to truly stop"})


@mcp.tool()
def capture_resume() -> str:
    """Resume capturing records."""
    res = _api("POST", "/capture/resume")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result({"resumed": True, "session_id": data.get("session_id") if isinstance(data, dict) else None})


# ======================================================================
# Toolset: Flow query
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
    """List flows of the current session (default NDJSON-style JSON array).

    Args:
        session: Session ID (0=current active session)
        limit: Maximum number of entries to return (default 100)
        since_id: Incremental query, only return flows with id > N (non-blocking polling)
        filter_expr: Filter expression 'key op value && ...' (key: host/method/path/url/status/pid/process, op: = ~= != >= <= > <)
        host: Shortcut to filter by host
        status_code: Shortcut to filter by status code
        method: Shortcut to filter by method
        protocol: Protocol filter http|tcp|udp|ws|dns
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
    # Client-side filtering
    if filter_expr:
        try:
            filters = parse_match(filter_expr)["filters"]
            flows = [f for f in flows if flow_matches(f, filters)]
        except ValueError as e:
            return _error(str(e))
    # Truncate large fields
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
    """List flows across all sessions (for global analysis).

    Args:
        host: Filter by host
        process: Filter by process name
        method: Filter by method
        status_code: Filter by status code
        protocol: Protocol filter http|tcp|udp|ws|dns
        limit: Maximum number of entries to return
        offset: Pagination offset
        since_id: Incremental query
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
    """Get a single flow's details.

    Args:
        flow_id: Flow ID
        field: Only fetch a specific field request_body|response_body|raw_data (empty=all)
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
    """Regex search flow bodies.

    Args:
        body_regex: Regex pattern (searches request_body + response_body)
        binary_hex: Binary search (hex string, mutually exclusive with body_regex)
        search_all: true=search across all sessions, false=current session only
    """
    if not body_regex and not binary_hex:
        return _error("body_regex or binary_hex parameter required")
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
    """Flow statistics (grouped counts by host/method/status).

    Args:
        session: Session ID (0=current active session)
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
    """Multi-dimensional aggregate statistics overview (CoolUI dashboard data source, cross-session full).

    Returns aggregate statistics for all dimensions at once, including: total count, total bytes,
    inbound/outbound bytes, success/error counts, average duration, as well as grouped count lists
    by protocol/method/status-code-range/host/process/IP-region (top 20 each).
    Suitable for generating traffic monitoring dashboards and global overview reports.
    No session ID parameter required.
    """
    res = _api("GET", "/flows/overview")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def packets_delete(flow_id: int = 0, ids: str = "") -> str:
    """Delete flows.

    Args:
        flow_id: Single flow ID
        ids: Batch delete, comma-separated ID list (takes precedence over flow_id)
    """
    if ids:
        id_list = [x.strip() for x in ids.split(",") if x.strip()]
        res = _api("POST", "/flows/batch-delete", {"ids": id_list})
        _, err = _ok(res)
        if err:
            return _error(err)
        return _result({"deleted": len(id_list)})
    if not flow_id:
        return _error("flow_id or ids parameter required")
    res = _api("DELETE", f"/flows/{flow_id}")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"deleted": 1, "id": flow_id})


@mcp.tool()
def packets_clear(all_sessions: bool = False, before_id: int = 0) -> str:
    """Clear flows.

    Args:
        all_sessions: true=clear all session flows, false=current session only
        before_id: Only clear flows with id < N (0=clear all)
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
# Toolset: Intercept rules (auto-modify)
# ======================================================================

@mcp.tool()
def intercept_add(
    match: str,
    action: str,
    note: str = "",
    dry_run: bool = False,
    idempotent: bool = False,
) -> str:
    """Add an intercept rule (auto-modify rule).

    Args:
        match: Match expression 'key op value && ...' (key: host/method/path/url/status/pid/process, op: = ~= != >= <= > <)
            Example: 'host~=api.example.com && method=POST'
        action: Action spec. Supported:
            Modify response: set-json key value | set-json-path path value | remove-json key | remove-json-path path |
                    replace-header K V | replace-bytes offset:hex | replace-bytes-regex regex hex |
                    mock CODE BODY | status CODE | drop | mock-request BODY [CTYPE]
            Modify request: set-request-header K V | set-request-json key value | set-request-json-path path value |
                    remove-request-json key | set-request-body-hex hex | replace-request-bytes offset:hex
            Timing: delay N | delay-request N (milliseconds)
            Python script: script '<source>' | script file <path>
                Script defines on_request(ctx)/on_response(ctx), runs in an independent worker subprocess
                ctx attributes: host/path/method/url/scheme/pid/process_name/request_headers/request_body
                          (on_response additionally: status_code/response_headers/response_body)
                Modify: ctx.set_request_header/set_request_body/set_response_header/set_response_body/set_status_code
                Return None=continue, {"drop": True}=reject, {"mock": True, "status": 200, "headers": {}, "body": b""}=fake response
        note: Rule note (required, for easier management)
        dry_run: true=preview flows that would be matched but do not create the rule
        idempotent: true=idempotent create (do not duplicate if the same rule already exists)
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
        # Fallback: client-side traversal
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
                                "hint": "Same rule already exists, not duplicated"})

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
    """List all intercept rules (including hit statistics)."""
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
    """Delete intercept rules.

    Args:
        rule_id: Single rule ID
        ids: Batch delete, comma-separated (takes precedence over rule_id)
    """
    if ids:
        id_list = [x.strip() for x in ids.split(",") if x.strip()]
        res = _api("POST", "/auto-reply/rules/batch-delete", {"ids": id_list})
        _, err = _ok(res)
        if err:
            return _error(err)
        return _result({"deleted": len(id_list)})
    if not rule_id:
        return _error("rule_id or ids parameter required")
    res = _api("DELETE", f"/auto-reply/rules/{rule_id}")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"deleted": 1, "rule_id": rule_id})


@mcp.tool()
def intercept_hits(rule_id: int) -> str:
    """View a rule's hit statistics + last hit flow details.

    Args:
        rule_id: Rule ID
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
        return _error(f"Rule not found: {rule_id}")
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
    """Enable/disable an intercept rule.

    Args:
        rule_id: Rule ID
        enabled: true=enable, false=disable
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
    repeat: int = 0,
    concurrency: int = 1,
    interval_ms: int = 0,
) -> str:
    """Replay a specific flow. Can override body/method/url/headers.

    Args:
        flow_id: Flow ID to replay
        body: Override request body (empty=use original body)
        method: Override HTTP method
        url: Override target URL
        host: Override target host
        port: Override target port
        headers: Override request headers, JSON string like '{"K":"V"}' or HTTP text format 'K:V\\nK2:V2'
        timeout: Timeout seconds (0=auto: 120s with body, 30s without body)
        repeat: Repeat Advanced - total replay count (0/1=single replay; >1=batch replay via /repeat endpoint with stats)
        concurrency: Repeat Advanced - concurrent workers (1-50, default 1; only used when repeat>1)
        interval_ms: Repeat Advanced - delay between submissions in ms (0-60000, default 0=no delay; only used when repeat>1)
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

    # Repeat Advanced: offload to backend /repeat endpoint for unified concurrency/interval/stats
    if repeat and repeat > 1:
        if concurrency < 1 or concurrency > 50:
            return _error("concurrency must be between 1 and 50")
        if interval_ms < 0 or interval_ms > 60000:
            return _error("interval_ms must be between 0 and 60000")
        rp_body = {
            "count": int(repeat),
            "concurrency": int(concurrency),
            "interval_ms": int(interval_ms),
            "override": req_body if req_body else None,
        }
        # 服务端耗时按 count * 单次最大耗时估算
        srv_timeout = max(t * repeat / max(concurrency, 1), 60.0)
        res = _api("POST", f"/flows/{flow_id}/repeat", rp_body, timeout=srv_timeout)
        data, err = _ok(res)
        if err:
            return _error(err)
        # 截断每条结果的响应体，避免输出过长
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            for r in data["results"]:
                if isinstance(r, dict):
                    r.pop("response_body", None)
                    r.pop("response_headers", None)
        return _result(data)

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
    """Send a request from scratch (Composer feature), does not depend on existing flow.

    Args:
        method: HTTP method (GET/POST/PUT/DELETE etc.)
        url: Target URL (must start with http:// or https://)
        headers: Request headers, JSON string '{"K":"V"}' or HTTP text 'K:V\\nK2:V2'
        body: Request body
        timeout: Timeout seconds
    """
    if not url:
        return _error("url is required")
    if not url.startswith(("http://", "https://")):
        return _error("url must start with http:// or https://")
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
    """Batch timed replay of all flows in the specified session.

    Args:
        session: Session ID (required)
        filter_expr: Client-side filter expression, only replay matching flows
        parallel: Concurrency (1=serial)
        preserve_timing: true=replay with original time intervals (for rate-limit/risk-control testing)
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
    """Robustly parse headers (JSON string or HTTP text format)."""
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
    """View breakpoint status (whether enabled, pending flow list)."""
    res = _api("GET", "/breakpoint/status")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def breakpoint_on(bp_type: str = "request", timeout_seconds: int = 0) -> str:
    """Enable breakpoint.

    Args:
        bp_type: Breakpoint type request|response
        timeout_seconds: Auto-release after N seconds without pass-through (0=no auto timeout)
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
        out["hint"] = f"Breakpoint {bp_type} enabled, auto-release after {timeout_seconds}s without pass-through"
    return _result(out)


@mcp.tool()
def breakpoint_off(bp_type: str = "request") -> str:
    """Disable breakpoint.

    Args:
        bp_type: Breakpoint type request|response
    """
    res = _api("POST", f"/breakpoint/{bp_type}", {"enabled": False})
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({f"break_on_{bp_type}": False})


@mcp.tool()
def breakpoint_release(flow_id: int = 0, action: str = "release", all_pending: bool = False) -> str:
    """Release/drop flows paused by breakpoint.

    Args:
        flow_id: Single flow ID (mutually exclusive with all_pending)
        action: release=pass-through, drop=discard
        all_pending: true=batch operate on all pending breakpoints
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
            return _result({"action": action, "total": 0, "hint": "No pending breakpoints"})
        res = _api("POST", "/flows/batch-release", {"ids": ids, "action": action})
        data, err = _ok(res)
        if err:
            return _error(err)
        return _result({"action": action, "total": len(ids),
                        "released": data.get("released", 0) if isinstance(data, dict) else 0})
    if not flow_id:
        return _error("flow_id or all_pending=true required")
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
    """View system proxy status."""
    res = _api("GET", "/status")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result({"system_proxy_on": data.get("system_proxy_on"),
                    "proxy_host": data.get("proxy_host"), "proxy_port": data.get("proxy_port")})


@mcp.tool()
def proxy_on() -> str:
    """Enable system proxy (route all application traffic through Telnix)."""
    res = _api("POST", "/system/enable-proxy")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"system_proxy_on": True})


@mcp.tool()
def proxy_off() -> str:
    """Disable system proxy."""
    res = _api("POST", "/system/clear-proxy")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"system_proxy_on": False})


@mcp.tool()
def cert_status() -> str:
    """View HTTPS certificate installation status."""
    res = _api("GET", "/cert/status")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def cert_install() -> str:
    """Install HTTPS root certificate (requires UAC elevation)."""
    res = _api("POST", "/cert/install", timeout=60)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def focus_status() -> str:
    """View focus mode status."""
    res = _api("GET", "/focus")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def focus_on(pids: str = "", process_names: str = "", hosts: str = "", include_children: bool = True) -> str:
    """Enable focus mode (only capture traffic of specified processes/hosts).

    Args:
        pids: PID list, comma-separated
        process_names: Process name list, comma-separated
        hosts: Host wildcard list, comma-separated
        include_children: Whether to include child processes
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
        return _error("pids or process_names or hosts parameter required")
    res = _api("POST", "/focus", body)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def focus_off() -> str:
    """Disable focus mode."""
    res = _api("POST", "/focus", {"enabled": False, "pids": []})
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"focus_on": False})


@mcp.tool()
def raw_capture_status() -> str:
    """View TCP/UDP capture status."""
    res = _api("GET", "/raw/status")
    data, err = _ok(res)
    if err:
        return _error(err)
    if not data.get("pydivert_installed"):
        data["hint"] = "Run raw_capture_install to install pydivert"
    elif not data.get("is_admin"):
        data["hint"] = "Administrator privilege required. Call system_restart_as_admin"
    return _result(data)


@mcp.tool()
def raw_capture_start(pid_filter: str = "", port_filter: str = "", bpf_filter: str = "") -> str:
    """Start TCP/UDP capture (requires administrator privilege + pydivert).

    Args:
        pid_filter: Filter by PID, comma-separated
        port_filter: Filter by port, comma-separated
        bpf_filter: WinDivert filter string

    Non-Windows platforms: WinDivert is a Windows-only driver, returns "not supported" error directly.
    """
    # 非 Windows 平台：WinDivert 不可用，提前返回错误
    if not sys.platform.startswith("win"):
        return _error("TCP/UDP capture is supported only on Windows (WinDivert driver)",
                      hint="macOS/Linux can use the HTTP proxy capture feature (capture_start layer=http)")
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
    """Stop TCP/UDP capture."""
    res = _api("POST", "/raw/stop")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def system_restart() -> str:
    """Restart Telnix backend (in-process os.execv restart)."""
    res = _api("POST", "/system/restart")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"restarting": True})


@mcp.tool()
def system_quit() -> str:
    """Quit Telnix (clears system proxy + exits)."""
    res = _api("POST", "/system/quit")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"quitting": True})


# ---------- 设置管理 ----------

@mcp.tool()
def settings_get(key: str = "") -> str:
    """Read all settings or the value of a single key.

    Args:
        key: Optional. When specified, only returns the value of that key; when omitted, returns all settings.

    Returns:
        All settings dict, or {"key": ..., "value": ...}
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
    """Write a single setting item. The value string will be auto-deserialized as JSON (bool/number/list/dict).

    Args:
        key: Setting key (e.g. "proxy_engine")
        value: Setting value. "true"/"false" will be parsed as bool,
               "123" will be parsed as a number, "[1,2]" will be parsed as a list.

    Returns:
        The updated settings dict.
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
    """View or switch the proxy engine (builtin / async / mitmproxy).

    Args:
        name: Engine name. When omitted, only views the current engine without modifying.
            - builtin: built-in threaded proxy (default, zero-dependency, stable)
            - async: asyncio proxy (experimental, high-concurrency no GIL bottleneck)
            - mitmproxy: mitmproxy engine (bundled as dependency)

    Returns:
        {"proxy_engine": "builtin|async|mitmproxy", "previous": ..., "mitmproxy_available": bool, ...}
        After switching, call system_restart to load the new engine into the current process.
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
            "hint": "Switch engine: settings_proxy_engine(name='async'); system_restart required after switching",
        })

    n = name.lower().strip()
    if n not in VALID:
        return _error(f"Unsupported proxy engine: {n} (options: {', '.join(sorted(VALID))})")

    if n == "mitmproxy" and not mitm_available:
        return _error(
            "mitmproxy not available, cannot switch to this engine",
            hint="mitmproxy is bundled as a dependency; if unavailable, reinstall Telnix dependencies",
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
        "hint": "Call system_restart to apply the new engine",
    })


# ---------- 代理工具 ----------

@mcp.tool()
def proxy_tools_status() -> str:
    """Get proxy tools configuration (no-caching, force-cors, block-list, allow-list).

    Returns JSON with no_caching, force_cors, block_list_enabled, block_list,
    allow_list_enabled, allow_list fields.
    """
    res = _api("GET", "/proxy-tools")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def proxy_tools_no_cache(enable: bool = True) -> str:
    """Toggle No-Caching: inject Cache-Control: no-cache to all requests.

    Args:
        enable: True to enable, False to disable.
    """
    res = _api("PUT", "/proxy-tools", {"no_caching": enable})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def proxy_tools_force_cors(enable: bool = True) -> str:
    """Toggle Force-CORS: inject Access-Control-Allow-Origin: * to all responses.

    Args:
        enable: True to enable, False to disable.
    """
    res = _api("PUT", "/proxy-tools", {"force_cors": enable})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def proxy_tools_block_list_add(pattern: str, mode: str = "wildcard") -> str:
    """Add a pattern to the block list (blocked requests return 403).

    Args:
        pattern: Pattern to match (host or URL). Wildcards: * matches any, ? matches single char.
        mode: Match mode - "wildcard" (default), "exact", or "regex".
    """
    res = _api("POST", "/proxy-tools/block-list", {"pattern": pattern, "mode": mode})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def proxy_tools_block_list_remove(index: int) -> str:
    """Remove a rule from the block list by index.

    Args:
        index: Rule index (0-based, from proxy_tools_status).
    """
    res = _api("DELETE", f"/proxy-tools/block-list/{index}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def proxy_tools_block_list_enable(enable: bool = True) -> str:
    """Enable or disable the block list.

    Args:
        enable: True to enable, False to disable.
    """
    res = _api("PUT", "/proxy-tools", {"block_list_enabled": enable})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def proxy_tools_allow_list_add(pattern: str, mode: str = "wildcard") -> str:
    """Add a pattern to the allow list (whitelisted requests bypass block list).

    Args:
        pattern: Pattern to match (host or URL). Wildcards: * matches any, ? matches single char.
        mode: Match mode - "wildcard" (default), "exact", or "regex".
    """
    res = _api("POST", "/proxy-tools/allow-list", {"pattern": pattern, "mode": mode})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def proxy_tools_allow_list_remove(index: int) -> str:
    """Remove a rule from the allow list by index.

    Args:
        index: Rule index (0-based, from proxy_tools_status).
    """
    res = _api("DELETE", f"/proxy-tools/allow-list/{index}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def proxy_tools_allow_list_enable(enable: bool = True) -> str:
    """Enable or disable the allow list.

    Args:
        enable: True to enable, False to disable.
    """
    res = _api("PUT", "/proxy-tools", {"allow_list_enabled": enable})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


# ---------- Map Local / Map Remote ----------

@mcp.tool()
def proxy_tools_map_local_add(pattern: str, file_path: str, mode: str = "wildcard",
                              status: int = 0, content_type: str = "") -> str:
    """Add a Map Local rule: matched requests return a local file instead of forwarding.

    Args:
        pattern: Pattern to match (host or URL). Wildcards: * matches any, ? matches single char.
        file_path: Absolute path to the local file to serve.
        mode: Match mode - "wildcard" (default), "exact", or "regex".
        status: HTTP status code to return (0 = default 200).
        content_type: Override Content-Type (empty = infer from file extension).
    """
    body = {"pattern": pattern, "mode": mode, "file_path": file_path}
    if status:
        body["status"] = status
    if content_type:
        body["content_type"] = content_type
    res = _api("POST", "/proxy-tools/map-local", body)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def proxy_tools_map_local_remove(index: int) -> str:
    """Remove a Map Local rule by index.

    Args:
        index: Rule index (0-based, from proxy_tools_status).
    """
    res = _api("DELETE", f"/proxy-tools/map-local/{index}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def proxy_tools_map_local_enable(enable: bool = True) -> str:
    """Enable or disable Map Local.

    Args:
        enable: True to enable, False to disable.
    """
    res = _api("PUT", "/proxy-tools", {"map_local_enabled": enable})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def proxy_tools_map_remote_add(pattern: str, target_url: str, mode: str = "wildcard") -> str:
    """Add a Map Remote rule: redirect matched requests to another remote URL.

    Args:
        pattern: Pattern to match (host or URL). Wildcards: * matches any, ? matches single char.
        target_url: Target URL to redirect to (e.g. https://other.example.com/api/v2).
        mode: Match mode - "wildcard" (default), "exact", or "regex".
    """
    res = _api("POST", "/proxy-tools/map-remote", {
        "pattern": pattern, "mode": mode, "target_url": target_url})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def proxy_tools_map_remote_remove(index: int) -> str:
    """Remove a Map Remote rule by index.

    Args:
        index: Rule index (0-based, from proxy_tools_status).
    """
    res = _api("DELETE", f"/proxy-tools/map-remote/{index}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def proxy_tools_map_remote_enable(enable: bool = True) -> str:
    """Enable or disable Map Remote.

    Args:
        enable: True to enable, False to disable.
    """
    res = _api("PUT", "/proxy-tools", {"map_remote_enabled": enable})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


# ---------- Mirror ----------

@mcp.tool()
def proxy_tools_mirror_add(pattern: str, save_dir: str, mode: str = "wildcard") -> str:
    """Add a Mirror rule: automatically save matched responses to a local directory.

    Args:
        pattern: Pattern to match (host or URL). Wildcards: * matches any, ? matches single char.
        save_dir: Directory path to save response files.
        mode: Match mode - "wildcard" (default), "exact", or "regex".
    """
    res = _api("POST", "/proxy-tools/mirror", {
        "pattern": pattern, "mode": mode, "save_dir": save_dir})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def proxy_tools_mirror_remove(index: int) -> str:
    """Remove a Mirror rule by index.

    Args:
        index: Rule index (0-based, from proxy_tools_status).
    """
    res = _api("DELETE", f"/proxy-tools/mirror/{index}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def proxy_tools_mirror_enable(enable: bool = True) -> str:
    """Enable or disable Mirror.

    Args:
        enable: True to enable, False to disable.
    """
    res = _api("PUT", "/proxy-tools", {"mirror_enabled": enable})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def system_restart_as_admin() -> str:
    """Restart Telnix as administrator (UAC elevation, for TCP/UDP capture).

    Triggers a GUI user confirmation flow: backend creates a pending request -> shows a native Windows topmost dialog ->
    user agrees -> UAC elevation -> returns accepted; user rejects -> returns rejected.
    Waits up to 3 minutes (3 rounds of 60s long-polling).

    Non-Windows platforms: UAC is a Windows-only mechanism, returns "not supported" error directly,
    prompting the user to manually run as root with sudo.
    """
    # 非 Windows 平台：UAC 是 Windows 专属，提前返回错误避免无效请求
    if not sys.platform.startswith("win"):
        return _error("restart-as-admin is supported only on Windows (UAC elevation)",
                      hint="macOS/Linux: please run Telnix as root manually with sudo")
    res = _api("POST", "/system/request-admin-restart", timeout=10.0)
    data, err = _ok(res)
    if err:
        return _error(err)
    rid = data.get("request_id")
    if not rid:
        return _error("Backend did not return request_id")
    final_status = None
    final_msg = None
    for _ in range(3):
        r = _api("GET", f"/system/admin-request/{rid}/wait", timeout=65.0)
        if r.get("code") != 0:
            return _error(r.get("msg") or "Request does not exist or has been processed")
        d = r.get("data") or {}
        status = d.get("status")
        if status == "accepted":
            final_status = "accepted"
            final_msg = r.get("msg") or "User approved, restarting as administrator"
            break
        elif status == "rejected":
            final_status = "rejected"
            final_msg = r.get("msg") or "User rejected the admin restart request"
            break
    if final_status is None:
        return _error("Timed out waiting for user response (no response within 3 minutes)")
    if final_status == "accepted":
        return _result({"restarting": True, "as_admin": True, "approved": True, "message": final_msg})
    return _error(final_msg, rejected_by_user=True,
                  hint="User rejected the admin restart request. You can restart manually in the GUI, or retry after the user agrees")


@mcp.tool()
def system_windivert_warning_status() -> str:
    """Query WinDivert risk warning status.

    Returns:
      needed: Whether a warning is needed (true only on Windows platform + not yet acknowledged)
      message: Risk description text
      ack: Whether currently acknowledged
      platform: Current platform

    Note: Tools that trigger WinDivert loading such as raw_capture_start / transparent_proxy_start / dns_hijack_start
    automatically handle the ack flow (show a desktop topmost native dialog on first unacknowledged use).
    This tool is only for the agent to actively query the current status (e.g. check whether acknowledged,
    whether on Windows platform).
    """
    res = _api("GET", "/system/windivert-warning")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def system_windivert_warning_ack() -> str:
    """Mark the WinDivert risk warning as acknowledged (permanently no longer prompted).

    After calling, settings.json's windivert_warning_acknowledged is set to 1,
    and subsequent raw_capture_start / transparent_proxy_start / dns_hijack_start calls will no longer be blocked.

    Note: Usually no need to call manually -- the start tools above will automatically trigger a native desktop
    dialog on first call for the user to confirm. This tool is for scenarios where the agent wants to skip the
    dialog after the user has been informed through other channels (e.g. reading the README).
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
    """List all sessions."""
    res = _api("GET", "/sessions")
    data, err = _ok(res)
    if err:
        return _error(err)
    sessions = data if isinstance(data, list) else data.get("sessions", [])
    return _result(sessions)


@mcp.tool()
def sessions_show(session_id: int) -> str:
    """View session details.

    Args:
        session_id: Session ID
    """
    res = _api("GET", f"/sessions/{session_id}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def sessions_switch(session_id: int) -> str:
    """Switch to the specified session (subsequent operations default to this session).

    Args:
        session_id: Session ID
    """
    res = _api("POST", f"/sessions/{session_id}/switch")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def sessions_delete(session_id: int) -> str:
    """Delete a session.

    Args:
        session_id: Session ID
    """
    res = _api("DELETE", f"/sessions/{session_id}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def sessions_create(name: str = "", color: str = "") -> str:
    """Create a new session and switch to it as the active session.

    Args:
        name: Session name (optional)
        color: Session color in hex format (e.g. "#FF5733")
    """
    body = {}
    if name:
        body["name"] = name
    if color:
        body["color"] = color
    res = _api("POST", "/sessions", body if body else None)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def sessions_update(session_id: int, name: str = "", color: str = "") -> str:
    """Update session name and/or color.

    Args:
        session_id: Session ID
        name: New session name (optional)
        color: New session color hex (optional, e.g. "#FF5733")
    """
    body = {}
    if name:
        body["name"] = name
    if color:
        body["color"] = color
    if not body:
        return _error("name or color required")
    res = _api("PATCH", f"/sessions/{session_id}", body)
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
    """Start agent workspace: save current state (rules/focus/breakpoint) -> disable rules -> turn off focus/breakpoint ->
    add agent processes to the ignore list.

    After finishing work, you must call agent_end to restore the original state. Repeated start will fail (to avoid overwriting unrestored backups).
    """
    backup_path = _agent_backup_path()
    if os.path.exists(backup_path):
        return _error("An unrestored agent workspace backup exists. Please call agent_end to restore it before calling agent_start again")

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
        return _error(f"Failed to save backup: {e}")

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
        "hint": "Workspace cleared. After finishing work, call agent_end to restore the original state.",
    })


@mcp.tool()
def agent_end() -> str:
    """Restore the state saved before agent_start (rules/focus/breakpoint/ignore list).

    Note: Auto-modify rules require Telnix to be running to take effect, so the system proxy is not closed and Telnix is not exited.
    Please ask the user whether to close Telnix; only call system_quit after the user agrees.
    """
    backup_path = _agent_backup_path()
    if not os.path.exists(backup_path):
        return _error("No unrestored agent workspace backup (may have already called agent_end or never called agent_start)")
    try:
        with open(backup_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return _error(f"Failed to read backup: {e}")

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
        "hint": "Workspace restored. Please ask the user whether to close Telnix; call system_quit after the user agrees.",
    })


@mcp.tool()
def agent_status() -> str:
    """View agent workspace status."""
    backup_path = _agent_backup_path()
    if not os.path.exists(backup_path):
        return _result({"agent_workspace": "inactive",
                        "hint": "No active agent workspace (call agent_start to begin)"})
    try:
        with open(backup_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
        return _result({
            "agent_workspace": "active", "backup_path": backup_path,
            "rules_in_backup": len(snapshot.get("rules", []) or []),
            "focus_was_enabled": bool((snapshot.get("focus") or {}).get("enabled")),
            "break_on_request_was_on": bool((snapshot.get("breakpoint") or {}).get("break_on_request")),
            "break_on_response_was_on": bool((snapshot.get("breakpoint") or {}).get("break_on_response")),
            "hint": "Workspace cleared, call agent_end to restore.",
        })
    except (OSError, json.JSONDecodeError) as e:
        return _error(f"Backup file corrupted: {e}")


# ======================================================================
# 工具集：日志
# ======================================================================

@mcp.tool()
def log_tail(limit: int = 100, level: str = "", category: str = "") -> str:
    """View recent logs.

    Args:
        limit: Number of entries to return
        level: Log level filter (DEBUG/INFO/WARN/ERROR)
        category: Log category filter
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
    """Clear all logs."""
    res = _api("DELETE", "/logs")
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"cleared": True})


@mcp.tool()
def log_export(level: str = "", category: str = "", keyword: str = "") -> str:
    """Export logs as JSONL text (returns content directly, does not write a file).

    Args:
        level: Log level filter
        category: Log category filter
        keyword: Keyword filter
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
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0), trust_env=False) as client:
            resp = client.get(url, headers={"Accept": "application/x-jsonlines"})
            content = resp.text
    except Exception as e:  # noqa: BLE001
        return _error(f"Failed to export logs: {e}")
    # 截断
    if len(content.encode("utf-8")) > MAX_OUTPUT_BYTES:
        cut = int(MAX_OUTPUT_BYTES * 0.9)
        content = content[:cut] + f"\n... [truncated, original {len(content)} chars]"
    return content


# ======================================================================
# 工具集：证书完整管理
# ======================================================================

@mcp.tool()
def cert_remove() -> str:
    """Remove the installed HTTPS root certificate."""
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
    """Set breakpoint auto-release timeout (auto-release after N seconds without pass-through).

    Args:
        seconds: Timeout seconds (0=disable auto timeout)
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
    """Path template normalization: replace numbers, UUIDs, long hex segments with {id}."""
    # 性能优化：预编译路径模板化正则（避免每次调用都 re.match 重新编译）
    if not hasattr(_path_template, "_rx_num"):
        _path_template._rx_num = re.compile(r"^\d+$")
        _path_template._rx_uuid = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
        _path_template._rx_hex = re.compile(r"^[0-9a-fA-F]{16,}$")
        _path_template._rx_token = re.compile(r"^[A-Za-z0-9_-]+$")
    rx_num = _path_template._rx_num
    rx_uuid = _path_template._rx_uuid
    rx_hex = _path_template._rx_hex
    rx_token = _path_template._rx_token

    if not path:
        return "/", []
    raw_path, _, query_str = path.partition("?")
    segs = raw_path.split("/")
    out = []
    for seg in segs:
        if not seg:
            out.append("")
            continue
        if rx_num.match(seg):
            out.append("{id}")
        elif rx_uuid.match(seg):
            out.append("{uuid}")
        elif rx_hex.match(seg):
            out.append("{hex}")
        elif len(seg) >= 24 and rx_token.match(seg):
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
    """Compute p50/p95/max/min."""
    if not values:
        return {"p50": 0, "p95": 0, "max": 0, "min": 0, "count": 0}
    s = sorted(values)
    n = len(s)
    return {"p50": s[n // 2], "p95": s[min(n - 1, int(n * 0.95))],
            "max": s[-1], "min": s[0], "count": n}


def _walk_json_leaves(obj, prefix: str = ""):
    """Recursively traverse JSON, yielding (path, leaf_value)."""
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
    """Classify string charset (for signature field detection)."""
    # 性能优化：预编译正则
    if not hasattr(_classify_charset, "_rx"):
        _classify_charset._rx_num = re.compile(r"^\d+$")
        _classify_charset._rx_hex = re.compile(r"^[0-9a-fA-F]+$")
        _classify_charset._rx_hex_needed = re.compile(r"[a-fA-F]")
        _classify_charset._rx_b64 = re.compile(r"^[A-Za-z0-9+/=]+$")
        _classify_charset._rx_alnum = re.compile(r"^[A-Za-z0-9_\-]+$")
        _classify_charset._rx_alpha = re.compile(r"^[A-Za-z]+$")

    if not s:
        return "empty"
    if _classify_charset._rx_num.match(s):
        return "numeric"
    if _classify_charset._rx_hex.match(s) and _classify_charset._rx_hex_needed.search(s):
        return "hex"
    if _classify_charset._rx_b64.match(s):
        return "base64"
    if _classify_charset._rx_alnum.match(s):
        return "alphanumeric"
    if _classify_charset._rx_alpha.match(s):
        return "alpha"
    return "mixed"


def _extract_trace_values(body: str, min_length: int = 4) -> list[dict]:
    """Extract traceable string values from response body."""
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
    """Fetch flows for analysis (shared logic for endpoints/timeline/stats)."""
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
    """Export a single flow to the specified format.

    Args:
        flow_id: Flow ID
        fmt: Format curl|python-requests|postman|json|csv
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
    """Build a curl command.

    安全：URL/Header/Body 均经 shlex.quote 转义，防止用户复制执行导出的 curl
    命令时，因抓包数据中含引号/反引号/分号/$() 等 shell 元字符而触发命令注入
    （与 api/export.py 的 _build_curl 实现保持一致）。
    """
    method = flow.get("method", "GET")
    url = flow.get("url") or flow.get("request_url") or ""
    headers = _parse_headers(flow.get("request_headers") or "")
    for k in list(headers.keys()):
        if k.lower() in ("host", "content-length", "connection"):
            del headers[k]
    body = flow.get("request_body") or ""
    if body and body.startswith("base64:"):
        # 二进制 body：echo + base64 -d 管道，避免内联原始字节
        b64 = body[7:]
        prefix = f'echo {shlex.quote(b64)} | base64 -d | '
        parts = ["curl", "-X", shlex.quote(method)]
        for k, v in headers.items():
            parts += ["-H", shlex.quote(f'{k}: {v}')]
        parts += ["--data-binary", "@-"]
        parts.append(shlex.quote(url))
        return prefix + " ".join(parts)
    parts = ["curl", "-X", shlex.quote(method)]
    for k, v in headers.items():
        parts += ["-H", shlex.quote(f'{k}: {v}')]
    if body:
        parts += ["--data", shlex.quote(body)]
    parts.append(shlex.quote(url))
    return " ".join(parts)


def _flow_to_python_requests(flow: dict) -> str:
    """Convert to python-requests script."""
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
    """Convert to Postman Collection v2.1 single item."""
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
    """Flow tag management.

    Args:
        flow_id: Flow ID (can be omitted when list_all=true)
        add: Add a tag
        remove: Remove a tag
        clear: Clear all tags
        note: Set a note
        clear_note: Clear the note
        list_all: List all global tags and the flow count per tag
    """
    if list_all:
        res = _api("GET", "/flows/tags")
        data, err = _ok(res)
        if err:
            return _error(err)
        tags = data.get("tags", []) if isinstance(data, dict) else data
        return _result(tags)
    if not flow_id:
        return _error("flow_id or list_all=true required")
    res = _api("GET", f"/flows/{flow_id}")
    flow, err = _ok(res)
    if err:
        return _error(err)
    if not isinstance(flow, dict):
        return _error("Cannot fetch flow")
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
    """Compare a specified field of two flows, output unified diff.

    Args:
        flow_id1: First flow ID
        flow_id2: Second flow ID
        field: Field to compare response_body|request_body|request_headers|response_headers
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
    """Unique endpoint extraction (path template normalization, draws an API map).

    Args:
        session: Session ID (0=cross-session)
        limit: Max flows to fetch (0=no limit)
        host: Filter by host
        keep_query: Keep query parameter names
        sample_strategy: Sampling strategy first|last|random
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
    """Flow timeline (sorted by time, marking large-gap segments).

    Args:
        session: Session ID (0=cross-session)
        limit: Max flows to fetch
        host: Filter by host
        gap_seconds: Large gap threshold (seconds); gaps exceeding this start a new segment
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
    """Request dependency chain trace: extract string values from the specified flow's response, search within subsequent flows' requests.

    Args:
        flow_id: Source flow ID
        min_length: Minimum string length (filters short strings to reduce false positives)
        limit: Max subsequent flows to fetch
        search_all: true=scan across sessions, false=current session only
    """
    res = _api("GET", f"/flows/{flow_id}")
    src, err = _ok(res)
    if err:
        return _error(err)
    if not isinstance(src, dict):
        return _error("Cannot fetch source flow")
    values = _extract_trace_values(src.get("response_body") or "", min_length=min_length)
    if not values:
        return _result({"traced": True, "source_flow": flow_id, "dependencies": [],
                        "hint": "Source flow response has no traceable strings"})
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
    """Signature field auto-detection: compare JSON bodies of multiple requests to the same API to find suspicious signature/token fields.

    Args:
        flow_ids: Flow ID list, comma-separated (at least 2; for search_all at least 1 as the source)
        search_all: true=use the first ID as source, pull subsequent flows from /flows/all
        limit: Max flows to fetch in search_all mode
    """
    ids = [int(x.strip()) for x in flow_ids.split(",") if x.strip()] if flow_ids else []
    if search_all:
        if not ids:
            return _error("analyze search_all requires at least 1 source flow ID")
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
            return _error("analyze requires at least 2 flow IDs (or use search_all=true)")
        flows = []
        for fid in ids:
            res = _api("GET", f"/flows/{fid}")
            flow, _ = _ok(res)
            if isinstance(flow, dict):
                flows.append(flow)
    if len(flows) < 2:
        return _error("Fewer than 2 flows fetched successfully, cannot compare")

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
    """Modify an existing rule (without delete and recreate).

    Args:
        rule_id: Rule ID
        match: New match expression (empty=do not change)
        action: New action spec (empty=do not change)
        note: New note (empty=do not change)
        enable: Enable the rule
        disable: Disable the rule
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
        return _error(f"Rule does not exist: {rule_id}")
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
    """Export all rules as JSON (returns content directly, does not write a file)."""
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
    """Import rules from a JSON string.

    Args:
        rules_json: JSON string, format {"rules": [...]} or [...] (use the output of intercept_export directly)
        mode: merge=append, replace=clear first then import
    """
    try:
        data = json.loads(rules_json)
    except json.JSONDecodeError as e:
        return _error(f"JSON parse failed: {e}")
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        return _error("Invalid JSON format: missing rules array")
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
            results.append({"index": idx, "ok": False, "error": "not an object"})
            continue
        body = {k: v for k, v in r.items() if k != "id"}
        body["enabled"] = r.get("enabled", True)
        res = _api("POST", "/auto-reply/rules", body)
        if res.get("code") == 0:
            created += 1
            results.append({"index": idx, "ok": True, "pattern": body.get("pattern", "")})
        else:
            results.append({"index": idx, "ok": False, "error": res.get("msg", "API failed"),
                            "pattern": body.get("pattern", "")})
    return _result({"imported": True, "mode": mode, "created": created, "deleted_old": deleted,
                    "failed": len(rules) - created, "total_in_file": len(rules), "results": results})


@mcp.tool()
def intercept_template_list() -> str:
    """List all built-in rule templates."""
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
    """Apply a rule template to create a rule.

    Args:
        name: Template name
        match: Match expression
        note: Note
        disabled: true=create in disabled state
        method_filter: Method filter
        status_filter: Status code filter
        pid_filter: PID filter
        process_filter: Process name filter
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
    """List processes with network connections.

    Args:
        name: Filter by name (fuzzy)
        with_connections: Include connection snapshot
        tree: Process tree mode
        include_listen: Include listening ports
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
    """Add an ignored process (do not capture this process's traffic).

    Args:
        pid: Process PID (choose one of pid/name, or fill both)
        name: Process name (e.g. chrome.exe)
    """
    if not pid and not name:
        return _error("pid or name parameter required")
    body = {"pid": pid if pid else None, "name": name}
    res = _api("POST", "/processes/ignore", body)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def processes_unignore(row_id: int) -> str:
    """Unignore a process.

    Args:
        row_id: Row ID in the ignore list
    """
    res = _api("DELETE", f"/processes/ignore/{row_id}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def processes_ignored_list() -> str:
    """List ignored processes."""
    res = _api("GET", "/processes/ignored")
    data, err = _ok(res)
    if err:
        return _error(err)
    items = data if isinstance(data, list) else data.get("items", data.get("processes", []))
    return _result(items)


@mcp.tool()
def processes_ignore_host(host: str) -> str:
    """Add an ignored host wildcard (e.g. *.example.com).

    Args:
        host: Host wildcard
    """
    res = _api("POST", "/processes/ignore-host", {"host": host})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def processes_unignore_host(row_id: int) -> str:
    """Unignore a host.

    Args:
        row_id: Row ID in the ignored host list
    """
    res = _api("DELETE", f"/processes/ignore-host/{row_id}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def processes_ignored_hosts_list() -> str:
    """List ignored hosts."""
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
    """Export a session to the specified format (returns content directly, does not write a file).

    Args:
        session: Session ID (0=current active session)
        fmt: Format har|json|csv|python-requests|postman|curl|pcap
    """
    sid, err = _get_session_id(session)
    if err:
        return _error(err)
    res = _api("POST", f"/export/{sid}", {"format": fmt}, timeout=60)
    data, err = _ok(res)
    if err:
        return _error(err)
    content = data.get("content") if isinstance(data, dict) else data
    # pcap 格式：后端返回 base64 编码的二进制，MCP 直接返回 base64 字符串
    if fmt == "pcap" and isinstance(content, str):
        return f"[base64-encoded pcap data, {len(content)} chars]\n{content}"
    if isinstance(content, str):
        if len(content.encode("utf-8")) > MAX_OUTPUT_BYTES:
            cut = int(MAX_OUTPUT_BYTES * 0.9)
            content = content[:cut] + f"\n... [truncated, original {len(content)} chars]"
        return content
    return _to_text(content)


# ======================================================================
# 工具集：触发式捕获 / 热力图 / 拓扑图 / 时序回放
# ======================================================================

@mcp.tool()
def trigger_capture_set(dsl: str = "") -> str:
    """Configure trigger capture conditions. Only flows matching conditions will be recorded.

    Args:
        dsl: Trigger condition DSL, e.g. 'host=example.com & status>=500'.
             Supported fields: host/method/status/status>=N/path/process/url/protocol.
             Joined by & or AND (all must match). Empty string disables trigger capture.

    Once triggered (a matching flow appears), all subsequent flows are recorded.
    """
    res = _api("PUT", "/capture/trigger", {"dsl": dsl})
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def trigger_capture_get() -> str:
    """Get current trigger capture state (enabled/triggered/conditions)."""
    res = _api("GET", "/capture/trigger")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def trigger_capture_reset() -> str:
    """Reset trigger state (clears the 'triggered' flag, keeps conditions).

    Use this to re-arm the trigger after it has fired.
    """
    res = _api("POST", "/capture/trigger/reset")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def flows_heatmap(
    group_by: str = "host",
    bucket_seconds: int = 60,
    max_buckets: int = 120,
    top_n: int = 20,
    host: str = "",
    process: str = "",
) -> str:
    """2D traffic heatmap aggregation (time bucket x dimension).

    Args:
        group_by: Dimension to group by: host|process|method|status_range|ip_region
        bucket_seconds: Time bucket size in seconds (default 60)
        max_buckets: Max time buckets to return, newest first (default 120)
        top_n: Top N dimensions by total count (default 20)
        host: Filter by host (fuzzy match, optional)
        process: Filter by process (fuzzy match, optional)

    Returns buckets (time axis labels), dimensions (top N), and a matrix where
    matrix[dim_idx][bucket_idx] = count. Useful for spotting traffic anomalies
    across time and dimensions.
    """
    params: dict = {
        "group_by": group_by,
        "bucket_seconds": bucket_seconds,
        "max_buckets": max_buckets,
        "top_n": top_n,
    }
    if host:
        params["host"] = host
    if process:
        params["process"] = process
    res = _api("GET", "/flows/heatmap", params)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


@mcp.tool()
def flows_topology(
    host: str = "",
    process: str = "",
    max_nodes: int = 100,
) -> str:
    """Network topology: process -> remote IP -> host connection graph.

    Args:
        host: Filter by host (fuzzy match, optional)
        process: Filter by process (fuzzy match, optional)
        max_nodes: Max nodes to return (default 100)

    Returns nodes (id/label/type/count, type=process|ip|host) and edges
    (source/target/count/size). Useful for security audit: discover which
    processes connect to which external servers.
    """
    params: dict = {"max_nodes": max_nodes}
    if host:
        params["host"] = host
    if process:
        params["process"] = process
    res = _api("GET", "/flows/topology", params)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)


# ======================================================================
# 工具集：透明代理
# ======================================================================

@mcp.tool()
def transparent_proxy_status() -> str:
    """View transparent proxy status (running/redirected packet count/NAT table size/errors).

    The transparent proxy uses the WinDivert NETWORK layer to redirect outbound HTTP(80)/HTTPS(443) traffic
    to a local proxy, so applications can be captured without configuring a proxy. Returns fields:
    running (whether running), redirected_count (redirected packet count), nat_table_size (NAT table size),
    last_error (most recent error), supported (whether the platform supports it).
    """
    res = _api("GET", "/transparent-proxy/status")
    data, err = _ok(res)
    if err:
        return _error(err)
    if isinstance(data, dict):
        if not data.get("supported"):
            data["hint"] = "Transparent proxy is available only on Windows (depends on WinDivert)"
        elif not data.get("running") and data.get("last_error"):
            le = data.get("last_error", "")
            if "admin" in le.lower():
                data["hint"] = "Administrator privilege required. Call system_restart_as_admin to restart the backend as administrator"
    return _result(data)


@mcp.tool()
def transparent_proxy_start() -> str:
    """Start transparent proxy (WinDivert NETWORK layer redirection, requires administrator privilege).

    Redirects outbound HTTP(80)/HTTPS(443) traffic to a local proxy port, so applications can be captured without configuring a proxy.
    Requires Windows + administrator privilege + pydivert.
    On failure, returns a clear error (e.g. if not elevated, it will prompt to call system_restart_as_admin).
    On first use without WinDivert risk acknowledgement, automatically triggers a desktop topmost native dialog.
    """
    # 首次启用未确认 WinDivert 风险提示时，自动触发桌面置顶原生弹窗
    res = _api_with_windivert_ack("POST", "/transparent-proxy/start")
    data, err = _ok(res)
    if err:
        if "admin" in err.lower():
            return _error(err,
                          hint="Administrator privilege required. Call system_restart_as_admin to restart the backend as administrator")
        return _error(err)
    return _result(data)


@mcp.tool()
def transparent_proxy_stop() -> str:
    """Stop transparent proxy."""
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
    """List all auto-modify rules (including hit statistics).

    Equivalent to intercept_list, returns all rules. Each rule contains:
    id/rule_id, pattern, action, enabled, hit_count, last_hit_at, last_hit_flow_id, etc.
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
    """View rule details.

    Args:
        rule_id: Rule ID
    """
    res = _api("GET", "/auto-reply/rules")
    data, err = _ok(res)
    if err:
        return _error(err)
    rules = data if isinstance(data, list) else []
    for r in rules:
        if isinstance(r, dict) and str(r.get("id")) == str(rule_id):
            return _result(r)
    return _error(f"Rule does not exist: {rule_id}")


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
    """Create an auto-modify rule (supports loading Python scripts from a local .py file).

    Complementary to intercept_add: this tool focuses on creating Python script rules,
    loading script content from a local .py file via the script_path parameter, making it agent-friendly.

    Args:
        pattern: URL match pattern (e.g. *api.example.com*/v1/*)
        action: Action type: script=Python script, mock=fake response, modify_response=modify response,
            modify_request=modify request, mock_request=hardcoded request forwarding
        script_path: When action=script, load script content from a local .py file (agent-friendly, mutually exclusive with script)
        script: When action=script, inline Python script source code (mutually exclusive with script_path)
        action_spec: For non-script actions, the action spec string (e.g. 'set-json key value' / 'mock 200 {}'),
            reuses the syntax of intercept_add
        match_mode: Match mode wildcard|exact|regex (default wildcard)
        note: Rule note
        method_filter: Method filter (comma-separated)
        status_filter: Status code filter (comma-separated)
        pid_filter: PID filter
        process_filter: Process name filter
        disabled: true=create in disabled state
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
                return _error(f"Failed to read script file: {e}")
            body["modify_rules"] = source
        elif script:
            body["modify_rules"] = script
        else:
            return _error("action=script requires the script_path or script parameter")
        body["mock_status"] = None
        body["mock_headers"] = {}
        body["mock_body"] = ""
    else:
        if not action_spec:
            return _error(f"action={action} requires the action_spec parameter (e.g. 'set-json key value')")
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
    """Enable a rule.

    Args:
        rule_id: Rule ID
    """
    res = _api("PUT", f"/auto-reply/rules/{rule_id}", {"enabled": True})
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"rule_id": rule_id, "enabled": True})


@mcp.tool()
def auto_reply_disable(rule_id: str) -> str:
    """Disable a rule.

    Args:
        rule_id: Rule ID
    """
    res = _api("PUT", f"/auto-reply/rules/{rule_id}", {"enabled": False})
    _, err = _ok(res)
    if err:
        return _error(err)
    return _result({"rule_id": rule_id, "enabled": False})


@mcp.tool()
def auto_reply_delete(rule_id: str) -> str:
    """Delete a rule.

    Args:
        rule_id: Rule ID
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
    """Test Python script execution (does not create a rule, uses mock data through a worker subprocess).

    Before using auto_reply_create to create a script rule, the agent can use this tool to validate the script logic:
    calls on_request / on_response hooks with mock request/response data, and shows the script execution result,
    whether errors occurred, and whether the request/response was modified.

    Args:
        script: Inline Python script source code (mutually exclusive with script_path)
        script_path: Load script from a local .py file (mutually exclusive with script, agent-friendly)
        mock_host: Mock request host
        mock_path: Mock request path
        mock_method: Mock request method (GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS)
        mock_scheme: Mock request scheme (http/https)
        mock_http_version: Mock HTTP version (HTTP/1.1)
        mock_headers: Mock request headers (dict)
        mock_body: Mock request body string
        mock_resp_status: Mock response status code (if provided, also calls the on_response hook)
        mock_resp_headers: Mock response headers (dict)
        mock_resp_body: Mock response body string

    Returns:
        Test result JSON: {ok, duration_ms, error, traceback, request_phase, response_phase}
        - ok: Whether the script executed successfully
        - duration_ms: Total duration (milliseconds)
        - error: Error message (if any)
        - traceback: Exception traceback (if any)
        - request_phase: on_request phase result (action/modified/headers/body/error)
        - response_phase: on_response phase result (returned only when mock_resp_* is provided)
    """
    # 优先用 script_path 加载本地文件
    if script_path:
        import os
        if not os.path.isfile(script_path):
            return _error(f"Script file does not exist: {script_path}")
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                script_source = f.read()
        except OSError as e:
            return _error(f"Failed to read script file: {e}")
    elif script:
        script_source = script
    else:
        return _error("script or script_path parameter required")

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
# 工具集：Cookie 管理
# ======================================================================

@mcp.tool()
def cookies_list(host: str = "") -> str:
    """List all cookies grouped by host, aggregated from captured flows.

    Args:
        host: Optional fuzzy host filter (case-insensitive substring).
    """
    qs = f"?host={urllib.parse.quote(host)}" if host else ""
    res = _api("GET", f"/cookies{qs}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def cookies_clear_host(host: str) -> str:
    """Clear cookies for a specific host (strips Cookie/Set-Cookie headers from stored flows).

    Args:
        host: The host name to clear cookies for (exact match, case-insensitive).
    """
    res = _api("DELETE", f"/cookies/{urllib.parse.quote(host)}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def cookies_clear_all() -> str:
    """Clear all cookies from all hosts (strips Cookie/Set-Cookie headers from all stored flows)."""
    res = _api("DELETE", "/cookies")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


# ======================================================================
# 工具集：站点地图
# ======================================================================

@mcp.tool()
def site_map() -> str:
    """Get the site map tree structure of all visited URLs (Burp Suite-style).

    Returns a nested tree grouped by host and URL path segments.
    """
    res = _api("GET", "/site-map")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


# ======================================================================
# 工具集：录制/回放
# ======================================================================

@mcp.tool()
def record_scripts_list() -> str:
    """List all recorded scripts (without flow details)."""
    res = _api("GET", "/record-scripts")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def record_scripts_create(name: str, flow_ids: list[int], note: str = "") -> str:
    """Create a new recorded script from specified flow IDs.

    Args:
        name: Script name.
        flow_ids: List of flow IDs to include in the script.
        note: Optional note for the script.
    """
    body = {"name": name, "flow_ids": flow_ids, "note": note}
    res = _api("POST", "/record-scripts", body)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def record_scripts_get(script_id: str) -> str:
    """Get details of a recorded script (including flow data).

    Args:
        script_id: The script ID.
    """
    res = _api("GET", f"/record-scripts/{urllib.parse.quote(script_id)}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def record_scripts_delete(script_id: str) -> str:
    """Delete a recorded script.

    Args:
        script_id: The script ID to delete.
    """
    res = _api("DELETE", f"/record-scripts/{urllib.parse.quote(script_id)}")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def record_scripts_replay(script_id: str) -> str:
    """Replay a recorded script (concurrent replay of all flows).

    Args:
        script_id: The script ID to replay.
    """
    res = _api("POST", f"/record-scripts/{urllib.parse.quote(script_id)}/replay")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def record_start() -> str:
    """Start recording: subsequent flows will be captured into the recording session."""
    res = _api("POST", "/record-scripts/start-recording")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def record_stop(name: str = "", note: str = "") -> str:
    """Stop recording and save the captured flows as a script.

    Args:
        name: Optional script name (auto-generated if empty).
        note: Optional note.
    """
    body = {"name": name, "note": note}
    res = _api("POST", "/record-scripts/stop-recording", body)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def record_status() -> str:
    """Query current recording status and flow count."""
    res = _api("GET", "/record-scripts/recording-status")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


# ======================================================================
# 工具集：被动扫描
# ======================================================================

# ======================================================================
# 工具集：DNS 劫持
# ======================================================================

@mcp.tool()
def dns_hijack_status() -> str:
    """Get DNS hijack status (running state, rules, stats, and recent logs).

    Cross-platform: Windows uses WinDivert to modify A records in local DNS responses;
    Linux/macOS use iptables/pf NAT redirection.
    """
    res = _api("GET", "/dns-hijack/status")
    data, err = _ok(res)
    if err:
        return _error(err)
    if isinstance(data, dict):
        if not data.get("running") and data.get("last_error"):
            le = data.get("last_error", "")
            if "administrator" in le.lower() or "admin" in le.lower() or "root" in le.lower() or "privilege" in le.lower():
                data["hint"] = "Administrator/root privileges required. Call system_restart_as_admin to restart the backend as administrator"
    return _result(data or res)


@mcp.tool()
def dns_hijack_start(rules: dict = None, default_ip: str = "") -> str:
    """Start DNS hijacking (modifies A records in local DNS responses).

    Cross-platform: Windows uses WinDivert (requires admin + pydivert); Linux/macOS use
    iptables/pf NAT redirection (requires root). On first use without WinDivert risk
    acknowledgement, automatically triggers a desktop topmost native dialog.

    Args:
        rules: Optional initial rules mapping domain -> IP (e.g. {"example.com": "127.0.0.1"}).
        default_ip: Optional default IP for domains not in rules.
    """
    body: dict = {}
    if rules:
        body["rules"] = rules
    if default_ip:
        body["default_ip"] = default_ip.strip()
    # 首次启用未确认 WinDivert 风险提示时，自动触发桌面置顶原生弹窗
    res = _api_with_windivert_ack("POST", "/dns-hijack/start", body or None)
    data, err = _ok(res)
    if err:
        if "admin" in err.lower():
            return _error(err,
                          hint="Administrator/root privileges required. Call system_restart_as_admin to restart the backend as administrator")
        return _error(err)
    return _result(data or res)


@mcp.tool()
def dns_hijack_stop() -> str:
    """Stop DNS hijacking."""
    res = _api("POST", "/dns-hijack/stop")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def dns_hijack_rules_get() -> str:
    """Get current DNS hijack rules and default IP."""
    res = _api("GET", "/dns-hijack/status")
    data, err = _ok(res)
    if err:
        return _error(err)
    if isinstance(data, dict):
        return _result({"rules": data.get("rules", {}), "default_ip": data.get("default_ip", "")})
    return _result(data or res)


@mcp.tool()
def dns_hijack_rules_set(rules: dict, default_ip: str = "") -> str:
    """Update DNS hijack rules (replaces all rules).

    To add rules, provide the full desired ruleset. To delete a rule, omit it from the
    rules dict (the API replaces all rules atomically).

    Args:
        rules: Rules mapping domain -> IP (e.g. {"example.com": "127.0.0.1"}).
        default_ip: Optional default IP for domains not in rules (empty to clear).
    """
    body = {"rules": rules, "default_ip": default_ip.strip() if default_ip else ""}
    res = _api("PUT", "/dns-hijack/rules", body)
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


@mcp.tool()
def dns_hijack_clear_log() -> str:
    """Clear DNS hijack logs and stat counters."""
    res = _api("POST", "/dns-hijack/clear-log")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data or res)


# ======================================================================
# 工具集：Intruder 爆破攻击（Burp Intruder 风格）
# ======================================================================

# ======================================================================
# 工具集：主动扫描器（漏洞扫描 + 轻量爬虫）
# ======================================================================

# ======================================================================
# 主入口
# ======================================================================

def main():
    import argparse
    p = argparse.ArgumentParser(description="Telnix MCP Server")
    p.add_argument("--base-url", default="", help="Backend API address (defaults to TELNIX_API env var or 127.0.0.1:18901)")
    args = p.parse_args()
    if args.base_url:
        global BASE_URL
        BASE_URL = args.base_url.rstrip("/")
    # STDIO 模式：绝不写 stdout，日志走 stderr
    print(f"[telnix-mcp] starting, base_url={BASE_URL}", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

"""Telnix Agent CLI - command-line capture/interception control tool for AI agents.

Design principles (agent-friendly):
1. Machine-readable output: lists default to NDJSON (one JSON object per line), single objects emit compact JSON.
2. Non-interactive, no TTY assumptions: zero paging, zero confirmation, zero colored TUI.
3. Session-based: capture start returns a session_id; subsequent commands use --session to continue.
4. Crash-safe: capture start supports --max-duration / --auto-stop.
5. Declarative interception: intercept add --match 'expr' --action 'spec', no scripting.
6. --dry-run: intercept rule preview (lists flows that would match, without creating the rule).
7. --emit-curl: each HTTP flow emits a replayable curl command directly.
8. --since-id: non-blocking incremental query, replaces --tail for polling.

Usage:
    python -m telnix.cli <subcommand> [options]

Subcommands:
    status                              backend status (JSON)
    capture start [--max-duration S] [--layer http|tcp|all]
    capture stop [--layer all]
    capture clear
    packets list [--session S] [--limit N] [--since-id N] [--filter 'expr'] [--emit-curl] [--json-array]
    packets list-all [--host H] [--process P] [--method M] [--status N] [--protocol http|tcp|udp|ws|dns]
                     [--limit N] [--offset N] [--since-id N] [--emit-curl] [--json-array]
    packets get <id> [--emit-curl] [--hex] [--field request_body|response_body|raw_data]
    packets delete <id> [--ids a,b,c]
    packets search --body-regex 'PATTERN' [--binary-hex HEX] [--all]
    packets clear --all | --before-id N
    packets export <id> --format curl|python-requests|postman|json [-o FILE]
    packets stats
    packets overview
    intercept add --match 'expr' --action 'spec' [--name N] [--note N] [--dry-run]
    intercept list [--json-array]
    intercept del <id> [--ids a,b,c]
    replay <id> [--body B] [--host H] [--port P] [--method M] [--header K:V] [--fuzz 'k=1..100']
    export [--session S] --format har|json|python-requests|postman|curl -o FILE
    proxy status | on | off
    raw status | start | stop [--pid P] [--port P] [--filter F]
    cert status | install
    log tail [--level L] [--category C] [--limit N]
    focus status | on --pid P | --name N [--no-children] | off
    breakpoint status | on [--type request|response] [--timeout N] | off | timeout --timeout N
                        | release <flow_id> | drop <flow_id> | release --all | drop --all
    tools status | no-cache [on|off] | force-cors [on|off]
         | block-list <list|on|off|add PATTERN [-m MODE]|del INDEX>
         | allow-list <list|on|off|add PATTERN [-m MODE]|del INDEX>
         | map-local <list|on|off|add PATTERN FILE [-m MODE]|del INDEX>
         | map-remote <list|on|off|add PATTERN URL [-m MODE]|del INDEX>
         | mirror <list|on|off|add PATTERN DIR [-m MODE]|del INDEX>
    auto-reply list | get ID | create ... | enable ID | disable ID | delete ID | test-script ...
    cookies list [--host H] | clear-host HOST | clear-all
    site-map [--host H]
    record-replay list | create NAME --flow-ids 1,2,3 | show ID | delete ID | replay ID
                   | start-record | stop-record [--name N] | status
    sessions list | show ID | delete ID
    processes list | ignore PID | unignore PID | ignored-list | ignore-host PATTERN | unignore-host PATTERN
    dns-hijack status | start | stop | rules-get | rules-set ...
    transparent-proxy status | start | stop
    settings get [-k KEY] | set -k KEY -v VALUE | engine [builtin|async|mitmproxy]
    system restart | quit | restart-as-admin | firewall-allow | platform-capabilities
    agent start | end | status
    log tail | clear | export [-o FILE]

Match expression (--match):
    key op value [ && key op value ...]
    key:   host | method | path | url | status | pid | process
    op:    = (exact) ~= (wildcard/contains) != >= <= > <
    Example: 'host~=api.example.com && method=POST && path~=/api/v1/*'

Action spec (--action):
    Modify response:
    set-json key value            set response body JSON field (global search by key name)
    set-json-path path value      set response body JSON field (exact path)
    remove-json key               remove response body JSON field (global)
    remove-json-path path         remove response body JSON field (exact path)
    replace-header K V            replace response header
    replace-bytes offset:hex      binary offset replace response body
    replace-bytes-regex regex hex regex replace response body bytes
    mock CODE BODY                return CODE + BODY directly (skip server)
    status CODE                   return CODE directly (empty body)
    drop                          simulate failure (503 empty body)
    mock-request BODY [CTYPE]     fix request body, forward to real server for real response
    delay N                       sleep N ms in response phase (test timeout/retry)
    throttle N                    throttle response to N kbps

    Modify request:
    delay-request N               sleep N ms in request phase (test rate limiting)
    set-request-header K V        replace request header
    set-request-json key value    set request body JSON field
    set-request-json-path path v  set request body JSON field (exact path)
    remove-request-json key       remove request body JSON field
    set-request-body-hex hex      replace entire request body with hex bytes
    replace-request-bytes off:hex binary offset replace request body

    Python script (for complex logic, runs in a separate worker subprocess):
    script '<source>'             inline script (wrapped in single quotes)
    script file <path>            read script from file
    Script defines on_request(ctx)/on_response(ctx); use ctx.set_* to modify or return {drop/mock}.

Exit codes for all commands: 0 success, 1 business error, 2 connection error, 3 argument error.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
import time
import urllib.parse
from typing import Any

import httpx

DEFAULT_HOST = "127.0.0.1"
# Keep in sync with config.DEFAULT_PORT (avoid CLI defaulting to the old port 18899)
try:
    from .config import DEFAULT_PORT as _CFG_PORT
    DEFAULT_PORT = _CFG_PORT
except Exception:  # noqa: BLE001
    DEFAULT_PORT = 18901
BASE_URL = os.environ.get("TELNIX_API", f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")


# ---------- HTTP ----------

# Module-level httpx client (reused across requests for connection pooling)
_http_client: httpx.Client | None = None


def _get_http_client() -> httpx.Client:
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(timeout=httpx.Timeout(30.0))
    return _http_client


def _req(method: str, path: str, body: Any = None, timeout: float = 30.0) -> dict:
    """Call backend API, returns {code, data, msg}.

    trust_env=False 防止 httpx 读取系统代理设置——CLI 调用的后端 API
    (127.0.0.1:18901) 不应走代理，否则系统代理开启时会产生环回死循环。
    """
    url = f"{BASE_URL}/api{path}"
    headers = {"Accept": "application/json"}
    content = None
    if body is not None:
        content = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout), trust_env=False) as client:
            resp = client.request(method, url, content=content, headers=headers)
            return resp.json()
    except httpx.ConnectError as e:
        _die_conn(f"Cannot connect to backend {BASE_URL}: {e}")
    except httpx.RequestError as e:
        _die_conn(f"Connection error: {e}")
    except Exception as e:  # noqa: BLE001
        _die_conn(f"Connection error: {e}")


def _ok(res: dict) -> Any:
    """Extract data; die on failure."""
    if res.get("code") == 0:
        return res.get("data")
    msg = res.get("msg") or "Unknown error"
    hint = _hint_for_error(msg)
    err_obj = {"ok": False, "error": msg}
    if hint:
        err_obj["hint"] = hint
    print(json.dumps(err_obj, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)


def _req_with_windivert_ack(method: str, path: str, body: Any = None,
                            timeout: float = 30.0) -> dict:
    """Call APIs that may trigger WinDivert loading (raw/transparent-proxy/dns-hijack start).

    If the backend returns need_ack=true (first enable, not yet acknowledged), automatically
    trigger the desktop-foreground native popup flow:
    1. POST /system/request-windivert-ack creates a pending request + shows a native Yes/No popup
    2. Long-poll /system/windivert-ack-request/{rid}/wait for the user response
    3. User selects "Yes" -> ack persisted -> retry original request and return the result
    4. User selects "No" -> return an error response (caller dies via _ok)

    On non-Windows platforms / when already acked, the backend will not return need_ack; this
    function then behaves the same as _req.
    """
    res = _req(method, path, body, timeout=timeout)
    # Detect whether WinDivert risk-prompt acknowledgment is required
    if not (res.get("need_ack") is True or
            (isinstance(res.get("data"), dict) and res["data"].get("need_ack"))):
        return res
    # Trigger the native popup flow
    ack_res = _req("POST", "/system/request-windivert-ack", timeout=10.0)
    if ack_res.get("code") != 0:
        return ack_res  # creation failed; return the error so caller dies
    ack_data = ack_res.get("data") or {}
    # Non-Windows or already acked: backend returns skipped=true, retry the original request directly
    if ack_data.get("skipped"):
        return _req(method, path, body, timeout=timeout)
    rid = ack_data.get("request_id")
    if not rid:
        return res  # fallback: no rid obtained, return the original error
    # Long-poll: retry up to 3 times (60s each), covering a 3-minute window
    final_status = None
    final_msg = None
    for _ in range(3):
        r = _req("GET", f"/system/windivert-ack-request/{rid}/wait", timeout=65.0)
        if r.get("code") != 0:
            return r
        d = r.get("data") or {}
        status = d.get("status")
        if status == "accepted":
            final_status = "accepted"
            final_msg = r.get("msg") or "User confirmed the WinDivert risk prompt"
            break
        elif status == "rejected":
            final_status = "rejected"
            final_msg = r.get("msg") or "User rejected the WinDivert risk prompt"
            break
        # status == "pending", continue to the next round
    if final_status is None:
        err_obj = {
            "ok": False,
            "error": "Timed out waiting for user response to WinDivert risk prompt (3 minutes with no response)",
            "hint": "Acknowledge it on the GUI settings page, or retry with the user present",
        }
        print(json.dumps(err_obj, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    if final_status == "rejected":
        err_obj = {
            "ok": False,
            "error": final_msg,
            "rejected_by_user": True,
            "hint": "User rejected the WinDivert risk prompt. Acknowledge it on the GUI settings page and retry",
        }
        print(json.dumps(err_obj, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    # User confirmed: retry the original request
    return _req(method, path, body, timeout=timeout)


# ---------- Error hints ----------

def _hint_for_error(msg: str) -> str | None:
    """Generate an agent-actionable fix hint based on the error message."""
    msg_l = msg.lower()
    if "cannot connect" in msg_l or "connection" in msg_l:
        return "Backend not started? Run: cd src\\host && python -m telnix"
    if "certificate" in msg_l or "cert" in msg_l:
        return "HTTPS decryption requires a certificate: python -m telnix.cli cert install"
    if "pydivert" in msg_l:
        return "TCP/UDP capture requires administrator privileges (pydivert is bundled as a dependency)"
    if "administrator" in msg_l or "admin" in msg_l:
        return "Restart Telnix as administrator"
    if "session" in msg_l and "does not exist" in msg_l:
        return "Run first: python -m telnix.cli capture start"
    if "not found" in msg_l:
        return "Backend may not have been restarted; the old process is missing new routes. Restart backend: python -m telnix.cli restart or restart manually"
    return None


# ---------- Output ----------

def emit_obj(obj: Any) -> None:
    """Single object: compact JSON on one line."""
    print(json.dumps(obj, ensure_ascii=False))


def emit_list(items: list[Any], emit_curl: bool = False, json_array: bool = False) -> None:
    """List: NDJSON by default; JSON array when --json-array is set."""
    if json_array:
        out = []
        for it in items:
            if emit_curl:
                o = dict(it) if isinstance(it, dict) else {"value": it}
                o["curl"] = build_curl(it)
                out.append(o)
            else:
                out.append(it)
        print(json.dumps(out, ensure_ascii=False))
        return
    for it in items:
        if emit_curl:
            out = dict(it) if isinstance(it, dict) else {"value": it}
            out["curl"] = build_curl(it)
            print(json.dumps(out, ensure_ascii=False))
        else:
            print(json.dumps(it, ensure_ascii=False))


# ---------- curl builder ----------

def _parse_headers(raw_h) -> dict:
    """Robustly parse headers (JSON string or HTTP text format)."""
    headers = {}
    if not raw_h or not isinstance(raw_h, str):
        return headers
    # Try JSON first
    try:
        h_obj = json.loads(raw_h)
        if isinstance(h_obj, dict):
            return {str(k): str(v) for k, v in h_obj.items()}
    except Exception:  # noqa: BLE001
        pass
    # Text format: split by line with split(":", 1) to avoid splitting values containing ":"
    for line in raw_h.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    return headers


def build_curl(flow: dict, output_file: str = "") -> str:
    """Build a replayable curl command from a flow. Binary body uses a temp-file scheme on Windows.

    - When output_file is specified (e.g. req.sh): also generates <stem>_body.bin, and curl uses --data-binary @<stem>_body.bin
    - When output_file is empty: Windows decodes to body.bin via PowerShell; non-Windows keeps the Unix pipe
    """
    method = flow.get("method", "GET")
    url = flow.get("url") or flow.get("request_url") or ""
    if not url:
        return ""
    headers = _parse_headers(flow.get("request_headers"))
    parts = ["curl", "-X", method]
    for k, v in headers.items():
        if k.lower() in ("host", "content-length", "connection"):
            continue
        parts += ["-H", f'"{k}: {v}"']
    body = flow.get("request_body") or ""
    if body:
        if body.startswith("base64:"):
            # Binary body: choose a scheme based on output mode and platform
            b64 = body[7:]
            is_windows = sys.platform.startswith("win")
            if output_file:
                # File mode: base64-decode into <stem>_body.bin; the curl script references that file
                stem = os.path.splitext(output_file)[0]
                bin_path = f"{stem}_body.bin"
                try:
                    import base64 as _b64
                    raw = _b64.b64decode(b64)
                    with open(bin_path, "wb") as bf:
                        bf.write(raw)
                except Exception:  # noqa: BLE001
                    pass  # On decode failure still emit the script; an error at run time is easier to locate
                parts += ["--data-binary", f"@{bin_path}"]
            else:
                # stdout mode: with no -o we cannot write external files, fall back to a Unix pipe
                if is_windows:
                    # base64 -d is unavailable on Windows; warn and suggest using -o
                    print("Warning: -o not specified on Windows; the binary body uses a Unix pipe "
                          "(echo|base64 -d) which may be incompatible. Use -o to write a file and generate _body.bin",
                          file=sys.stderr)
                parts = ['echo', f'"{b64}"', '|', 'base64', '-d', '|'] + parts + ["--data-binary", "@-"]
        else:
            b = body.replace("'", "'\\''")
            parts += ["-d", f"'{b}'"]
    parts.append(f'"{url}"')
    return " ".join(parts)


# ---------- Match expression parsing ----------

def parse_match(expr: str) -> dict:
    """Parse a match expression, returns {pattern, match_mode, filters}."""
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
            _die_arg(f"Cannot parse match condition: {tok}")
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
    # Extract method/status/pid/process into independent filter fields (comma-separated multi-values).
    # cmd_intercept_add writes these into the rule's method_filter/status_filter/pid_filter/process_filter;
    # the proxy layer's find_matching_rule validates these fields (§4.1 trap fixed).
    # Only collect conditions with op = / ~= / !=; numeric comparisons (>=/<=/>/<) stay in filters
    # for client-side filtering only.
    filter_fields = {"method": [], "status": [], "pid": [], "process": []}
    for k, op, v in filters:
        if k in filter_fields and op in ("=", "~=", "!="):
            # != is not yet supported for backend filtering (needs negative match); only collect = / ~=
            if op in ("=", "~="):
                filter_fields[k].append(v)
    out = {
        "pattern": pattern,
        "match_mode": match_mode,
        "filters": filters,
        "method_filter": ",".join(filter_fields["method"]),
        "status_filter": ",".join(filter_fields["status"]),
        "pid_filter": ",".join(filter_fields["pid"]),
        "process_filter": ",".join(filter_fields["process"]),
    }
    return out


def flow_matches(flow: dict, filters: list[tuple[str, str, str]]) -> bool:
    """Client-side filtering (used by dry-run / packets list --filter)."""
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
        if op == ">=" and not _num_cmp(fv_s, v, ">="):
            return False
        if op == "<=" and not _num_cmp(fv_s, v, "<="):
            return False
        if op == ">" and not _num_cmp(fv_s, v, ">"):
            return False
        if op == "<" and not _num_cmp(fv_s, v, "<"):
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


# ---------- Action parsing ----------

def parse_action(spec: str) -> dict:
    """Parse an action spec, returns backend rule fields."""
    parts = _split_action(spec)
    if not parts:
        _die_arg("Action spec cannot be empty")
    name = parts[0].lower()
    args = parts[1:]

    # ---- Modify response ----
    if name == "replace-header":
        if len(args) < 2:
            _die_arg("replace-header requires: K V")
        return {
            "action": "modify_response",
            "modify_rules": [{"target": "response_header", "op": "replace", "key": args[0], "value": args[1]}],
        }
    if name == "set-json":
        if len(args) < 2:
            _die_arg("set-json requires: key value")
        return {
            "action": "modify_response",
            "modify_rules": [{"target": "response_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}],
        }
    if name == "set-json-path":
        if len(args) < 2:
            _die_arg("set-json-path requires: path value")
        return {
            "action": "modify_response",
            "modify_rules": [{"target": "response_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}],
        }
    if name == "remove-json":
        if len(args) < 1:
            _die_arg("remove-json requires: key")
        return {
            "action": "modify_response",
            "modify_rules": [{"target": "response_body", "op": "remove", "key": args[0]}],
        }
    if name == "remove-json-path":
        if len(args) < 1:
            _die_arg("remove-json-path requires: path")
        return {
            "action": "modify_response",
            "modify_rules": [{"target": "response_body", "op": "remove", "key": args[0]}],
        }
    if name == "replace-bytes":
        # replace-bytes offset:hex
        if len(args) < 1:
            _die_arg("replace-bytes requires: offset:hex")
        return {
            "action": "modify_response",
            "modify_rules": [{"target": "response_body", "op": "replace-bytes", "key": "", "value": args[0]}],
        }
    if name == "replace-bytes-regex":
        if len(args) < 2:
            _die_arg("replace-bytes-regex requires: regex hex")
        return {
            "action": "modify_response",
            "modify_rules": [{"target": "response_body", "op": "replace-bytes-regex", "key": args[0], "value": args[1]}],
        }
    if name == "mock":
        code = int(args[0]) if args else 200
        body = args[1] if len(args) > 1 else ""
        return {"action": "mock", "mock_status": code, "mock_body": body, "mock_headers": {"Content-Type": "application/json"}}
    if name == "status":
        code = int(args[0]) if args else 200
        return {"action": "mock", "mock_status": code, "mock_body": "", "mock_headers": {"Content-Type": "application/json"}}
    if name == "drop":
        return {"action": "mock", "mock_status": 503, "mock_body": "", "mock_headers": {}}
    if name == "mock-request":
        # Fix request content (mock request body), forward to the real server for a real response
        # Usage: mock-request '<json body>' or mock-request '<json body>' 'application/json'
        body = args[0] if args else ""
        ctype = args[1] if len(args) > 1 else "application/json"
        return {
            "action": "mock_request",
            "mock_body": body,
            "mock_headers": {"Content-Type": ctype},
        }

    # ---- Modify request ----
    if name == "set-request-header":
        if len(args) < 2:
            _die_arg("set-request-header requires: K V")
        return {
            "action": "modify_request",
            "modify_rules": [{"target": "request_header", "op": "replace", "key": args[0], "value": args[1]}],
        }
    if name == "set-request-json":
        if len(args) < 2:
            _die_arg("set-request-json requires: key value")
        return {
            "action": "modify_request",
            "modify_rules": [{"target": "request_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}],
        }
    if name == "set-request-json-path":
        if len(args) < 2:
            _die_arg("set-request-json-path requires: path value")
        return {
            "action": "modify_request",
            "modify_rules": [{"target": "request_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}],
        }
    if name == "remove-request-json":
        if len(args) < 1:
            _die_arg("remove-request-json requires: key")
        return {
            "action": "modify_request",
            "modify_rules": [{"target": "request_body", "op": "remove", "key": args[0]}],
        }
    if name == "set-request-body-hex":
        if len(args) < 1:
            _die_arg("set-request-body-hex requires: hex")
        return {
            "action": "modify_request",
            "modify_rules": [{"target": "request_body", "op": "replace", "key": "", "value": _hex_to_b64(args[0])}],
        }
    if name == "replace-request-bytes":
        if len(args) < 1:
            _die_arg("replace-request-bytes requires: offset:hex")
        return {
            "action": "modify_request",
            "modify_rules": [{"target": "request_body", "op": "replace-bytes", "key": "", "value": args[0]}],
        }

    # ---- Timing actions ----
    if name == "delay":
        # delay N: sleep N milliseconds in the response phase (test frontend timeout/retry logic)
        if len(args) < 1:
            _die_arg("delay requires: N (milliseconds)")
        try:
            val = int(args[0])
        except ValueError:
            _die_arg(f"delay parameter must be an integer number of milliseconds: {args[0]}")
        return {
            "action": "modify_response",
            "modify_rules": [{"target": "delay", "op": "sleep", "value": val}],
        }
    if name == "delay-request":
        # delay-request N: sleep N milliseconds in the request phase (test server-side rate limiting/risk control)
        if len(args) < 1:
            _die_arg("delay-request requires: N (milliseconds)")
        try:
            val = int(args[0])
        except ValueError:
            _die_arg(f"delay-request parameter must be an integer number of milliseconds: {args[0]}")
        return {
            "action": "modify_request",
            "modify_rules": [{"target": "delay-request", "op": "sleep", "value": val}],
        }

    # ---- Python script ----
    if name == "script":
        # script '<inline source>' or script-file <path>
        # Inline script wrapped in single quotes (single argument after shlex parsing)
        if len(args) < 1:
            _die_arg("script requires: '<inline source>' or script-file <path>")
        # Note: args[0] may be the "file" subcommand
        if args[0] == "file" and len(args) >= 2:
            path = args[1]
            try:
                with open(path, "r", encoding="utf-8") as f:
                    source = f.read()
            except OSError as e:
                _die_arg(f"Failed to read script file: {e}")
        else:
            source = args[0]
        return {"action": "script", "modify_rules": source}

    _die_arg(f"Unknown action: {name} (supported: set-json/set-json-path/remove-json/replace-header/replace-bytes/replace-bytes-regex/mock/status/drop/mock-request/set-request-header/set-request-json/set-request-json-path/remove-request-json/set-request-body-hex/replace-request-bytes/delay/delay-request/script)")
    return {}


def _split_action(spec: str) -> list[str]:
    import shlex
    try:
        return shlex.split(spec)
    except ValueError:
        return spec.split()


def _auto_type(v: str) -> Any:
    """Auto-detect number/bool/null; otherwise string."""
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
    """Convert a hex string to a base64:-prefixed string."""
    import base64
    raw = bytes.fromhex(hex_str.replace(" ", "").replace("0x", ""))
    return "base64:" + base64.b64encode(raw).decode("ascii")


# ---------- Exit ----------

def _die_arg(msg: str):
    err = {"ok": False, "error": msg, "kind": "arg"}
    hint = _hint_for_error(msg)
    if hint:
        err["hint"] = hint
    print(json.dumps(err, ensure_ascii=False), file=sys.stderr)
    sys.exit(3)


def _die_conn(msg: str):
    err = {"ok": False, "error": msg, "kind": "conn"}
    hint = _hint_for_error(msg)
    if hint:
        err["hint"] = hint
    print(json.dumps(err, ensure_ascii=False), file=sys.stderr)
    sys.exit(2)


# ---------- Subcommands ----------

def cmd_status(args):
    res = _req("GET", "/status")
    emit_obj(res.get("data") or res)


def cmd_capture_start(args):
    body: dict = {}
    # --auto-stop N: passed to the backend; the backend's persistent process handles timed stop
    # (unaffected by the agent CLI exiting)
    auto_stop = getattr(args, "auto_stop", 0) or 0
    if auto_stop and auto_stop > 0:
        body["auto_stop_seconds"] = float(auto_stop)
    res = _req("POST", "/capture/start", body, timeout=10)
    data = _ok(res)
    out = {"session_id": data.get("session_id"), "capturing": True}
    if args.max_duration and data.get("session_id"):
        out["auto_stop_after"] = args.max_duration
        out["hint"] = f"Recommended: run after {args.max_duration}s: python -m telnix.cli capture stop"
    if data.get("auto_stop_seconds"):
        out["auto_stop_seconds"] = data["auto_stop_seconds"]
        out["hint"] = f"Backend will auto-stop capture after {data['auto_stop_seconds']}s"
    # If --layer tcp/all is specified, start the TCP/UDP backend
    if args.layer in ("tcp", "all"):
        raw_body = {}
        if args.pid:
            raw_body["pid_filter"] = [int(p) for p in args.pid.split(",") if p.strip()]
        if args.port:
            raw_body["port_filter"] = [int(p) for p in args.port.split(",") if p.strip()]
        if args.bpf:
            raw_body["filter_str"] = args.bpf
        # On first enable without an acknowledged WinDivert risk prompt, auto-trigger the desktop-foreground native popup
        raw_res = _req_with_windivert_ack("POST", "/raw/start", raw_body, timeout=10)
        if raw_res.get("code") == 0:
            out["raw_capture"] = "started"
        else:
            out["raw_capture"] = "failed"
            out["raw_error"] = raw_res.get("msg")
            out["raw_hint"] = _hint_for_error(raw_res.get("msg") or "")
    emit_obj(out)


def cmd_capture_stop(args):
    res = _req("POST", "/capture/stop")
    _ok(res)
    # Also stop the TCP/UDP backend
    if args.layer in ("tcp", "all"):
        _req("POST", "/raw/stop")
    emit_obj({"capturing": False})


def cmd_capture_clear(args):
    res = _req("POST", "/capture/clear")
    _ok(res)
    emit_obj({"cleared": True})


def cmd_capture_pause(args):
    """Pause capture (session retained, proxy still running; unlike stop)."""
    res = _req("POST", "/capture/pause")
    data = _ok(res)
    emit_obj({"paused": True, "session_id": data.get("session_id"),
              "hint": "Session retained, proxy still running. Use resume to resume, stop to truly stop"})


def cmd_capture_resume(args):
    """Resume capture recording."""
    res = _req("POST", "/capture/resume")
    data = _ok(res)
    emit_obj({"resumed": True, "session_id": data.get("session_id")})


def cmd_sessions(args):
    """sessions management: list / show / delete / create / rename."""
    action = args.action
    if action == "list":
        res = _req("GET", "/sessions")
        data = _ok(res)
        sessions = data if isinstance(data, list) else data.get("sessions", [])
        emit_list(sessions, json_array=getattr(args, "json_array", False))
    elif action == "show":
        if not args.id:
            _die_arg("sessions show requires <id>")
        res = _req("GET", f"/sessions/{args.id}")
        emit_obj(_ok(res))
    elif action == "create":
        body = {}
        if getattr(args, "name", ""):
            body["name"] = args.name
        if getattr(args, "color", ""):
            body["color"] = args.color
        res = _req("POST", "/sessions", body if body else None)
        emit_obj(_ok(res))
    elif action == "rename":
        if not args.id:
            _die_arg("sessions rename requires <id>")
        body = {}
        if getattr(args, "name", ""):
            body["name"] = args.name
        if getattr(args, "color", ""):
            body["color"] = args.color
        res = _req("PATCH", f"/sessions/{args.id}", body if body else None)
        emit_obj(_ok(res))
    elif action == "delete":
        if not args.id:
            _die_arg("sessions delete requires <id>")
        res = _req("DELETE", f"/sessions/{args.id}")
        emit_obj(_ok(res))
    else:
        _die_arg("sessions requires: list | show | delete")


def _get_session(args) -> int:
    sid_val = getattr(args, "session", 0)
    if sid_val:
        return int(sid_val)
    # §2.2 TELNIX_SESSION env var: when an agent script pins a single session, set this var to
    # avoid passing --session on every command
    env_session = os.environ.get("TELNIX_SESSION", "").strip()
    if env_session:
        try:
            return int(env_session)
        except ValueError:
            _die_arg(f"Invalid TELNIX_SESSION env var value: {env_session} (must be an integer session_id)")
    res = _req("GET", "/status")
    data = _ok(res)
    sid = data.get("session_id")
    if not sid:
        _die_arg("No active session; run capture start first or specify --session (or set the TELNIX_SESSION env var)")
    return int(sid)


def cmd_packets_list(args):
    sid = _get_session(args)
    params_list = [f"limit={args.limit}", "offset=0"]
    if args.filter_host:
        params_list.append(f"host={urllib.parse.quote(args.filter_host)}")
    if args.filter_status:
        params_list.append(f"status_code={args.filter_status}")
    if args.filter_method:
        params_list.append(f"method={urllib.parse.quote(args.filter_method.upper())}")
    if args.since_id:
        params_list.append(f"since_id={args.since_id}")
    if args.protocol:
        params_list.append(f"protocol={args.protocol}")
    # §3.1 tag filtering
    tag = getattr(args, "tag", "") or ""
    if tag:
        params_list.append(f"tag={urllib.parse.quote(tag)}")
    if getattr(args, "has_tags", False):
        params_list.append("has_tags=true")
    params = "&".join(params_list)
    res = _req("GET", f"/sessions/{sid}/flows?{params}")
    data = _ok(res)
    flows = data.get("flows", []) if isinstance(data, dict) else data
    max_id = data.get("max_id", 0) if isinstance(data, dict) else 0
    # Client-side expression filtering
    filters = []
    if args.filter:
        filters = parse_match(args.filter)["filters"]
        flows = [f for f in flows if flow_matches(f, filters)]
    if args.tail:
        _tail_flows(sid, flows, filters, args)
    else:
        # Batch decoder: emit a decoded field for each flow
        decode_path = getattr(args, "decode", "") or ""
        if decode_path:
            decode_field = getattr(args, "decode_field", "") or "response_body"
            for f in flows:
                try:
                    f["decoded"] = _apply_decoder(decode_path, f if isinstance(f, dict) else {}, field=decode_field)
                    f["decoded_field"] = decode_field
                except SystemExit:
                    f["decoded"] = None
        emit_list(flows, emit_curl=args.emit_curl, json_array=args.json_array)
        # On incremental queries, emit max_id to stderr for the agent to record
        # (only emitted when --since-id is explicitly set, to avoid polluting plain list output)
        if args.since_id is not None and max_id:
            print(json.dumps({"max_id": max_id, "count": len(flows)}, ensure_ascii=False), file=sys.stderr)


def _tail_flows(sid: int, initial: list, filters: list, args):
    """Stream tail: pull new flows every 1s and emit them. Exit with Ctrl+C."""
    seen_ids = {f.get("id") for f in initial}
    last_max_id = max((f.get("id") or 0) for f in initial) if initial else 0
    print(json.dumps({"tail": True, "session": sid, "waiting": True, "last_id": last_max_id},
                     ensure_ascii=False), file=sys.stderr)
    try:
        while True:
            res = _req("GET", f"/sessions/{sid}/flows?limit=200&offset=0&since_id={last_max_id}")
            data = _ok(res)
            flows = data.get("flows", []) if isinstance(data, dict) else data
            for f in flows:
                fid = f.get("id")
                if fid and fid not in seen_ids:
                    seen_ids.add(fid)
                    if fid > last_max_id:
                        last_max_id = fid
                    if not filters or flow_matches(f, filters):
                        if args.emit_curl:
                            out = dict(f)
                            out["curl"] = build_curl(f)
                            print(json.dumps(out, ensure_ascii=False))
                        else:
                            print(json.dumps(f, ensure_ascii=False))
            time.sleep(1)
    except KeyboardInterrupt:
        print(json.dumps({"tail": "stopped", "last_id": last_max_id}, ensure_ascii=False), file=sys.stderr)


def cmd_packets_get(args):
    if args.hex:
        # hex dump mode
        field = args.field or "response_body"
        params = f"?field={field}"
        if args.offset:
            params += f"&offset={args.offset}"
        if args.length:
            params += f"&length={args.length}"
        res = _req("GET", f"/flows/{args.id}/hex{params}")
        emit_obj(_ok(res))
        return
    res = _req("GET", f"/flows/{args.id}")
    flow = _ok(res)
    # --decode plugin.py: load a custom decoder to decode the body
    decode_path = getattr(args, "decode", "") or ""
    if decode_path:
        decode_field = getattr(args, "decode_field", "") or "response_body"
        decoded = _apply_decoder(decode_path, flow if isinstance(flow, dict) else {}, field=decode_field)
        out = dict(flow) if isinstance(flow, dict) else {}
        out["decoded"] = decoded
        out["decoded_field"] = decode_field
        emit_obj(out)
        return
    if args.emit_curl:
        out = dict(flow) if isinstance(flow, dict) else {}
        out["curl"] = build_curl(flow if isinstance(flow, dict) else {})
        emit_obj(out)
    else:
        emit_obj(flow)


def _apply_decoder(plugin_path: str, flow: dict, field: str = "response_body") -> dict:
    """Apply a Python decoder plugin to decode the specified field of the flow.

    Delegates to backend /flows/apply-decoder endpoint (plugin loading happens server-side).
    Decoder interface:
        def decode(data: bytes, flow: dict) -> dict:
            return {"messages": [...], "fields": {...}}
    data is the raw bytes decoded from field (default response_body; request_body also supported);
    base64: prefix is handled automatically.
    The return value is emitted as-is into the flow["decoded"] field.
    """
    res = _req("POST", "/flows/apply-decoder",
               body={"plugin_path": plugin_path, "flow": flow, "field": field}, timeout=30)
    if res.get("code") != 0:
        err_obj = {"ok": False, "error": res.get("msg") or "Decoder failed"}
        print(json.dumps(err_obj, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    data = res.get("data") or {}
    decoded = data.get("decoded")
    if not isinstance(decoded, dict):
        return {"value": decoded}
    return decoded


def cmd_packets_delete(args):
    if args.ids:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
        res = _req("POST", "/flows/batch-delete", {"ids": ids})
        _ok(res)
        emit_obj({"deleted": len(ids)})
    elif args.id:
        res = _req("DELETE", f"/flows/{args.id}")
        _ok(res)
        emit_obj({"deleted": 1, "id": args.id})
    else:
        _die_arg("<id> or --ids required")


def cmd_packets_search(args):
    # --all searches across sessions (session_id=0); otherwise the current session
    if getattr(args, "all", False):
        sid = 0
    else:
        sid = _get_session(args)
    body = {"session_id": sid}
    if args.body_regex:
        body["body_regex"] = args.body_regex
    if args.binary_hex:
        body["binary_hex"] = args.binary_hex
    # §3.1 Multi-condition combined search (AND with body_regex/binary_hex)
    header_regex = getattr(args, "header_regex", "") or ""
    if header_regex:
        body["header_regex"] = header_regex
    method = getattr(args, "method", "") or ""
    if method:
        body["method"] = method
    status = getattr(args, "status", None)
    if status:
        body["status_code"] = status
    pid = getattr(args, "pid", None)
    if pid:
        body["pid"] = pid
    process = getattr(args, "process", "") or ""
    if process:
        body["process_name"] = process
    # §3.14 hex offset-range search: parse the "START:END" format
    offset_str = getattr(args, "offset", "") or ""
    if offset_str:
        try:
            parts = offset_str.split(":", 1)
            if len(parts) == 2:
                s = int(parts[0]) if parts[0] else 0
                e = int(parts[1]) if parts[1] else None
            else:
                s = int(parts[0])
                e = None
            body["offset_start"] = s
            if e is not None:
                body["offset_end"] = e
        except ValueError:
            _die_arg(f"Invalid --offset format: {offset_str} (expected START:END, e.g. 0:1024)")
    body["limit"] = args.limit
    res = _req("POST", "/flows/search", body)
    data = _ok(res)
    flows = data.get("matches", []) if isinstance(data, dict) else data
    emit_list(flows, json_array=args.json_array)


def cmd_packets_tag(args):
    """§3.1 Flow tag management: --add/--remove/--clear operate on tags, --note sets a note,
    --clear-note clears the note.

    Usage:
        packets tag <id> --add analyzed
        packets tag <id> --remove suspicious
        packets tag <id> --clear
        packets tag <id> --note "note content"
        packets tag <id> --clear-note
        packets tag <id> --add analyzed --note "analyzed"
        packets tag --list              # list all global tags and the flow count per tag (§4.2)
    """
    # §4.2 global tag list
    if getattr(args, "list", False):
        res = _req("GET", "/flows/tags")
        data = _ok(res)
        tags = data.get("tags", []) if isinstance(data, dict) else data
        emit_list(tags, json_array=getattr(args, "json_array", False))
        return
    flow_id = args.id
    if not flow_id:
        _die_arg("tag requires <id> or --list")
    # Pull the current flow to get existing tags
    res = _req("GET", f"/flows/{flow_id}")
    flow = _ok(res)
    if not isinstance(flow, dict):
        _die_arg("Cannot fetch flow")

    existing_tags_str = flow.get("tags") or ""
    existing_tags = [t.strip() for t in existing_tags_str.split(",") if t.strip()]
    note = getattr(args, "note", None)
    clear_note = getattr(args, "clear_note", False)

    if getattr(args, "clear", False):
        # Clear all tags
        new_tags = []
    elif getattr(args, "add", ""):
        # Add a tag (deduplicated)
        add_tag = args.add.strip()
        if add_tag and add_tag not in existing_tags:
            existing_tags.append(add_tag)
        new_tags = existing_tags
    elif getattr(args, "remove", ""):
        # Remove a tag
        rm_tag = args.remove.strip()
        new_tags = [t for t in existing_tags if t != rm_tag]
    else:
        # No operation argument: only update note (if any) or show current tags
        if note is None and not clear_note:
            emit_obj({"flow_id": flow_id, "tags": existing_tags_str,
                      "tag_note": flow.get("tag_note") or ""})
            return
        new_tags = existing_tags

    new_tags_str = ",".join(new_tags)
    # Call PATCH /flows/{id}/tags
    body = {"tags": new_tags_str}
    if note is not None:
        body["tag_note"] = note
    elif clear_note:
        # --clear-note explicitly clears the note (overwrite with an empty string)
        body["tag_note"] = ""
    res = _req("PATCH", f"/flows/{flow_id}/tags", body)
    data = _ok(res)
    if isinstance(data, dict):
        emit_obj(data)
    else:
        emit_obj({"flow_id": flow_id, "tags": new_tags_str,
                  "tag_note": body.get("tag_note", flow.get("tag_note") or "")})


def cmd_packets_list_all(args):
    """Query all flows across sessions (does not depend on an active session)."""
    params_list = [f"limit={args.limit}", f"offset={args.offset}"]
    if args.host:
        params_list.append(f"host={urllib.parse.quote(args.host)}")
    if args.process:
        params_list.append(f"process={urllib.parse.quote(args.process)}")
    if args.status:
        params_list.append(f"status_code={args.status}")
    if args.method:
        params_list.append(f"method={urllib.parse.quote(args.method.upper())}")
    if args.protocol:
        params_list.append(f"protocol={args.protocol}")
    if args.since_id:
        params_list.append(f"since_id={args.since_id}")
    if getattr(args, "filter_path", ""):
        params_list.append(f"path={urllib.parse.quote(args.filter_path)}")
    if getattr(args, "filter_url", ""):
        params_list.append(f"url={urllib.parse.quote(args.filter_url)}")
    # §3.1 tag filtering
    tag = getattr(args, "tag", "") or ""
    if tag:
        params_list.append(f"tag={urllib.parse.quote(tag)}")
    if getattr(args, "has_tags", False):
        params_list.append("has_tags=true")
    params = "&".join(params_list)
    res = _req("GET", f"/flows/all?{params}")
    data = _ok(res)
    flows = data.get("flows", []) if isinstance(data, dict) else data
    total = data.get("total", 0) if isinstance(data, dict) else 0
    max_id = max((f.get("id") or 0 for f in flows), default=0)
    # Client-side expression filtering
    if args.filter:
        filters = parse_match(args.filter)["filters"]
        flows = [f for f in flows if flow_matches(f, filters)]
    # Batch decoder
    decode_path = getattr(args, "decode", "") or ""
    if decode_path:
        decode_field = getattr(args, "decode_field", "") or "response_body"
        for f in flows:
            try:
                f["decoded"] = _apply_decoder(decode_path, f if isinstance(f, dict) else {}, field=decode_field)
                f["decoded_field"] = decode_field
            except SystemExit:
                f["decoded"] = None
    emit_list(flows, emit_curl=args.emit_curl, json_array=args.json_array)
    if args.since_id is not None and max_id:
        print(json.dumps({"max_id": max_id, "count": len(flows), "total": total}, ensure_ascii=False), file=sys.stderr)


def cmd_packets_clear(args):
    """Clear flows across sessions. --all clears everything, --before-id N deletes old flows with id<N."""
    if args.all:
        res = _req("POST", "/flows/clear", {"mode": "all"})
        data = _ok(res)
        emit_obj({"cleared": True, "scope": "all", "deleted": data.get("deleted", 0) if isinstance(data, dict) else 0})
    elif args.before_id:
        res = _req("POST", "/flows/clear", {"mode": "before_id", "before_id": args.before_id})
        data = _ok(res)
        emit_obj({"cleared": True, "scope": "before_id", "before_id": args.before_id,
                  "deleted": data.get("deleted", 0) if isinstance(data, dict) else 0})
    else:
        _die_arg("packets clear requires --all or --before-id N")


def cmd_packets_export(args):
    """Export a single flow to curl/python-requests/postman/csv etc."""
    res = _req("GET", f"/flows/{args.id}")
    flow = _ok(res)
    fmt = args.format
    if fmt == "curl":
        content = build_curl(flow if isinstance(flow, dict) else {}, output_file=args.output or "")
    elif fmt == "python-requests":
        content = _flow_to_python_requests(flow if isinstance(flow, dict) else {}, output_file=args.output or "")
    elif fmt == "postman":
        content = json.dumps(_flow_to_postman(flow if isinstance(flow, dict) else {}, output_file=args.output or ""), ensure_ascii=False, indent=2)
    elif fmt == "csv":
        # Single-row CSV (with header)
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["id", "timestamp", "method", "host", "path", "url",
                    "status_code", "duration_ms", "size", "process_name", "pid", "protocol"])
        f = flow if isinstance(flow, dict) else {}
        w.writerow([f.get("id", ""), f.get("timestamp", ""), f.get("method", ""),
                    f.get("host", ""), f.get("path", ""), f.get("url", ""),
                    f.get("status_code", ""), f.get("duration_ms", ""), f.get("size", ""),
                    f.get("process_name", ""), f.get("pid", ""), f.get("protocol", "http")])
        content = buf.getvalue()
    else:  # json
        content = json.dumps(flow, ensure_ascii=False, indent=2)
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(content)
            emit_obj({"exported": True, "path": args.output, "format": fmt, "size": len(content)})
        except Exception as e:  # noqa: BLE001
            emit_obj({"exported": False, "error": str(e)})
    else:
        print(content)


def _flow_to_python_requests(flow: dict, output_file: str = "") -> str:
    """Convert a single flow to a python-requests script. Binary body uses base64.b64decode.

    On Windows, when output_file is specified, the binary body is written to <stem>_body.bin and
    the script reads it via open('<stem>_body.bin','rb'), avoiding a large inline base64 blob.
    """
    method = flow.get("method", "GET")
    url = flow.get("url") or flow.get("request_url") or ""
    headers = _parse_headers(flow.get("request_headers"))
    for k in list(headers.keys()):
        if k.lower() in ("host", "content-length", "connection"):
            del headers[k]
    body = flow.get("request_body") or ""
    is_windows = sys.platform.startswith("win")
    lines = ["import requests", "import base64", "",
             f"url = {url!r}", f"headers = {headers!r}", ""]
    if body:
        if body.startswith("base64:"):
            b64 = body[7:]
            if is_windows and output_file:
                # Windows + file mode: write <stem>_body.bin, script reads it via open()
                stem = os.path.splitext(output_file)[0]
                bin_path = f"{stem}_body.bin"
                try:
                    import base64 as _b64
                    raw = _b64.b64decode(b64)
                    with open(bin_path, "wb") as bf:
                        bf.write(raw)
                except Exception:  # noqa: BLE001
                    pass
                lines.append(f"with open({bin_path!r}, 'rb') as _f:  # binary body read from external file")
                lines.append(f"    data = _f.read()")
            else:
                lines.append(f"data = base64.b64decode({b64!r})  # binary body")
            lines.append(f"resp = requests.{method.lower()}(url, headers=headers, data=data, timeout=30, verify=False)")
        else:
            # Try JSON
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


def _flow_to_postman(flow: dict, output_file: str = "") -> dict:
    """Convert a single flow to a Postman Collection v2.1 single item. Binary body uses raw + base64 marker.

    On Windows, when output_file is specified, the binary body is written to <stem>_body.bin and
    Postman references the file in file mode.
    """
    method = flow.get("method", "GET")
    url = flow.get("url") or flow.get("request_url") or ""
    headers = []
    for k, v in _parse_headers(flow.get("request_headers")).items():
        if k.lower() in ("host", "content-length", "connection"):
            continue
        headers.append({"key": k, "value": str(v), "type": "text"})
    body = flow.get("request_body") or ""
    is_windows = sys.platform.startswith("win")
    item = {
        "name": f"{method} {url[:80]}",
        "request": {
            "method": method,
            "header": headers,
            "url": {"raw": url, "protocol": url.split("://")[0] if "://" in url else "",
                    "host": [url.split("://")[-1].split("/")[0]] if "://" in url else [url],
                    "path": url.split("://")[-1].split("/")[1:] if "://" in url and "/" in url.split("://")[-1] else []},
        },
    }
    if body:
        if body.startswith("base64:"):
            if is_windows and output_file:
                # Windows + file mode: write <stem>_body.bin, Postman references it in file mode
                stem = os.path.splitext(output_file)[0]
                bin_path = f"{stem}_body.bin"
                try:
                    import base64 as _b64
                    raw = _b64.b64decode(body[7:])
                    with open(bin_path, "wb") as bf:
                        bf.write(raw)
                except Exception:  # noqa: BLE001
                    pass
                item["request"]["body"] = {
                    "mode": "file",
                    "file": {"src": bin_path},
                    "description": "binary body read from external file (Windows compatibility)",
                }
            else:
                # Postman raw mode does not support binary; use a base64 string + note
                item["request"]["body"] = {
                    "mode": "raw",
                    "raw": body[7:],
                    "options": {"raw": {"language": "text"}},
                    "description": "base64-encoded binary body, decode before send",
                }
        else:
            item["request"]["body"] = {"mode": "raw", "raw": body,
                                        "options": {"raw": {"language": "json"}}}
    return {"collection": {"info": {"name": "Telnix Export", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"}, "item": [item]}}


def cmd_packets_stats(args):
    by = getattr(args, "by", "") or ""
    # content_type/process/group endpoint goes through the backend /flows/stats (cross-session full stats, no active session needed)
    if by in ("content_type", "process"):
        res = _req("GET", f"/flows/stats?group_by={by}")
        data = _ok(res)
        emit_obj(data)
        return
    # --by endpoint: use server-side aggregation (design fix: moved from client-side processing)
    if by == "endpoint":
        sid = _get_session(args) if getattr(args, "session", None) else None
        res = _req("GET", f"/flows/endpoint-stats?session_id={sid or ''}&limit=2000")
        data = _ok(res)
        emit_obj({"by": "endpoint", "endpoints": data.get("endpoints", [])})
        return
    sid = _get_session(args)
    res = _req("GET", f"/sessions/{sid}/stats")
    data = _ok(res)
    # --metrics: attach size/duration distribution on top of existing grouping
    metrics = (getattr(args, "metrics", "") or "").lower()
    if metrics:
        flows = _fetch_flows_for_analysis(args, default_limit=2000)
        data["metrics"] = {
            "size": _percentiles([f.get("size") or 0 for f in flows if isinstance(f, dict)]),
            "duration_ms": _percentiles([f.get("duration_ms") or 0 for f in flows if isinstance(f, dict)]),
        }
    emit_obj(data)


def cmd_packets_overview(args):
    """Multi-dimensional aggregate stats overview (CoolUI dashboard data source, cross-session full)."""
    res = _req("GET", "/flows/overview")
    data = _ok(res)
    emit_obj(data)


def _percentiles(values: list) -> dict:
    """Compute p50/p95/max/min."""
    if not values:
        return {"p50": 0, "p95": 0, "max": 0, "min": 0, "count": 0}
    s = sorted(values)
    n = len(s)
    return {
        "p50": s[n // 2],
        "p95": s[min(n - 1, int(n * 0.95))],
        "max": s[-1],
        "min": s[0],
        "count": n,
    }


def _path_template(path: str, keep_query: bool = False) -> tuple[str, list[str]]:
    """Normalize a path template: replace numbers, UUIDs, and long hex segments with {id}.
    Returns (template, query_keys). When keep_query=True the template keeps the ?k1&k2 form.
    """
    import re
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
    # Separate path and query
    raw_path, _, query_str = path.partition("?")
    segs = raw_path.split("/")
    out = []
    for seg in segs:
        if not seg:
            out.append("")
            continue
        # Pure number
        if rx_num.match(seg):
            out.append("{id}")
        # UUID
        elif rx_uuid.match(seg):
            out.append("{uuid}")
        # Long hex (>=16)
        elif rx_hex.match(seg):
            out.append("{hex}")
        # Long base64-ish (>=20, alphanumeric)
        elif len(seg) >= 24 and rx_token.match(seg):
            out.append("{token}")
        else:
            out.append(seg)
    tpl = "/".join(out)
    # Deduplicate query keys
    query_keys = []
    if query_str:
        for pair in query_str.split("&"):
            k = pair.split("=", 1)[0]
            if k and k not in query_keys:
                query_keys.append(k)
        if keep_query and query_keys:
            tpl = f"{tpl}?{'&'.join(query_keys)}"
    return tpl, query_keys


def cmd_packets_diff(args):
    """Compare a specified field of two flows and emit a unified diff."""
    import difflib
    res1 = _req("GET", f"/flows/{args.id1}")
    f1 = _ok(res1)
    res2 = _req("GET", f"/flows/{args.id2}")
    f2 = _ok(res2)
    field = args.field
    v1 = (f1.get(field) if isinstance(f1, dict) else "") or ""
    v2 = (f2.get(field) if isinstance(f2, dict) else "") or ""
    # Try to pretty-print JSON fields to make the diff clearer
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
    diff = list(difflib.unified_diff(
        lines1, lines2,
        fromfile=f"#{args.id1}.{field}", tofile=f"#{args.id2}.{field}",
        lineterm=""))
    out = {
        "id1": args.id1, "id2": args.id2, "field": field,
        "diff": "\n".join(diff),
        "identical": not diff,
    }
    emit_obj(out)


def _fetch_flows_for_analysis(args, default_limit: int = 5000) -> list[dict]:
    """Shared fetch logic for endpoints/timeline/stats: supports --session N, --limit 0 (unlimited), and --host filtering."""
    limit = getattr(args, "limit", default_limit) or default_limit
    # --limit 0 means unlimited, but the backend needs a large number; use 50000 as the practical cap
    if limit == 0:
        limit = 50000
    session_id = getattr(args, "session", 0) or 0
    if session_id:
        params = [f"limit={limit}", "offset=0"]
        if getattr(args, "host", ""):
            params.append(f"host={urllib.parse.quote(args.host)}")
        res = _req("GET", f"/sessions/{session_id}/flows?{'&'.join(params)}")
        data = _ok(res)
        return data.get("flows", []) if isinstance(data, dict) else data
    else:
        params = [f"limit={limit}", "offset=0"]
        if getattr(args, "host", ""):
            params.append(f"host={urllib.parse.quote(args.host)}")
        res = _req("GET", f"/flows/all?{'&'.join(params)}")
        data = _ok(res)
        return data.get("flows", []) if isinstance(data, dict) else data


def cmd_packets_endpoints(args):
    """Extract unique endpoints (path-template normalized, draw an API map).
    Supports --session N to look at a specific session, --limit 0 to pull everything, --keep-query to
    keep query parameter names, and --sample-strategy first/last/random to control the sample_ids strategy.
    """
    flows = _fetch_flows_for_analysis(args, default_limit=2000)
    keep_query = getattr(args, "keep_query", False)
    sample_strategy = getattr(args, "sample_strategy", "first") or "first"
    # Group by endpoint first, collecting all flow ids (not truncated)
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
                                         "_all_ids": [],
                                         "query_keys": qkeys})
        ep["count"] += 1
        sc = f.get("status_code")
        if sc and sc not in ep["status_set"]:
            ep["status_set"].append(sc)
        ep["_all_ids"].append(f.get("id"))
        for qk in qkeys:
            if qk not in ep["query_keys"]:
                ep["query_keys"].append(qk)
    # Sampling strategy: first (first 3, default) / last (last 3) / random (random 3)
    import random as _random
    for ep in endpoints.values():
        all_ids = ep.pop("_all_ids", [])
        if sample_strategy == "last":
            ep["sample_ids"] = all_ids[-3:]
        elif sample_strategy == "random":
            ep["sample_ids"] = _random.sample(all_ids, min(3, len(all_ids))) if all_ids else []
        else:  # first
            ep["sample_ids"] = all_ids[:3]
    out = list(endpoints.values())
    out.sort(key=lambda x: -x["count"])
    if args.json_array:
        print(json.dumps(out, ensure_ascii=False))
    else:
        for ep in out:
            print(json.dumps(ep, ensure_ascii=False))


def cmd_packets_timeline(args):
    """Flow timeline: sorted by time, marking segments with large gaps.
    Supports --session N to look at a specific session, --limit 0 to pull everything.
    """
    flows = _fetch_flows_for_analysis(args, default_limit=2000)
    # Ascending by time
    items = []
    for f in flows:
        if not isinstance(f, dict):
            continue
        items.append({
            "id": f.get("id"),
            "ts": f.get("timestamp"),
            "method": f.get("method"),
            "host": f.get("host"),
            "path": f.get("path"),
            "status": f.get("status_code"),
            "duration_ms": f.get("duration_ms"),
        })
    items.sort(key=lambda x: x.get("ts") or "")
    # Parse timestamps to compute gaps
    from datetime import datetime
    prev_dt = None
    segment = 0
    for it in items:
        try:
            dt = datetime.fromisoformat(it["ts"].replace("Z", "+00:00")) if it.get("ts") else None
        except Exception:  # noqa: BLE001
            dt = None
        if dt and prev_dt:
            gap = (dt - prev_dt).total_seconds()
            if gap > args.gap:
                segment += 1
                it["segment_break"] = True
                it["gap_seconds"] = round(gap, 2)
        prev_dt = dt or prev_dt
        it["segment"] = segment
    emit_obj({"timeline": items, "count": len(items), "segments": segment + 1, "gap_threshold": args.gap})


def cmd_packets_watch(args):
    """Targeted tail: block and emit new flows matching the filter expression (NDJSON); exit with Ctrl+C.
    Internally uses --since-id polling + client-side filter, more convenient than the agent writing its own poll loop.
    """
    sid = _get_session(args) if not args.session else args.session
    filters = parse_match(args.filter)["filters"]
    # Initialize last_max_id: pull the current max id once, only watch flows after it
    res = _req("GET", f"/sessions/{sid}/flows?limit=1&offset=0")
    data = _ok(res)
    init_flows = data.get("flows", []) if isinstance(data, dict) else data
    last_max_id = max((f.get("id") or 0 for f in init_flows), default=0) if init_flows else 0
    print(json.dumps({"watch": True, "session": sid, "filter": args.filter,
                      "waiting": True, "last_id": last_max_id}, ensure_ascii=False), file=sys.stderr)
    interval = max(0.2, float(args.interval or 1.0))
    try:
        while True:
            res = _req("GET", f"/sessions/{sid}/flows?limit=200&offset=0&since_id={last_max_id}")
            data = _ok(res)
            flows = data.get("flows", []) if isinstance(data, dict) else data
            for f in flows:
                fid = f.get("id")
                if fid and fid > last_max_id:
                    last_max_id = fid
                if flow_matches(f, filters):
                    print(json.dumps(f, ensure_ascii=False))
            time.sleep(interval)
    except KeyboardInterrupt:
        print(json.dumps({"watch": "stopped", "last_id": last_max_id}, ensure_ascii=False), file=sys.stderr)


def cmd_packets_trace(args):
    """Request dependency-chain trace: extract string values from the specified flow's response body
    (JSON field values or regex-matched token/session_id/uid, etc.), then search for them in the
    request_headers/request_body of subsequent flows.
    Emits the dependency chain [{source_flow, target_flow, field, value}] (NDJSON).

    With --all, scans across sessions (/flows/all?since_id=N); otherwise scans only the current session.
    """
    # 1. Pull the source flow
    res = _req("GET", f"/flows/{args.id}")
    src = _ok(res)
    if not isinstance(src, dict):
        _die_arg("Cannot fetch source flow")

    # 2. Extract string values from the response (--min-length filters short strings to reduce false positives)
    min_len = getattr(args, "min_length", 0) or 0
    values_to_track = _extract_trace_values(src.get("response_body") or "", min_length=min_len)
    if not values_to_track:
        emit_obj({"traced": True, "source_flow": args.id, "dependencies": [],
                  "hint": "Source flow response has no trackable strings"})
        return

    # 3. Pull subsequent flows: --all uses /flows/all across sessions; otherwise /sessions/{sid}/flows in the current session
    limit = getattr(args, "limit", 500) or 500
    if getattr(args, "all", False):
        # Cross-session scan
        res = _req("GET", f"/flows/all?limit={limit}&offset=0&since_id={args.id}")
    else:
        # Current-session scan
        sid = _get_session(args)
        res = _req("GET", f"/sessions/{sid}/flows?limit={limit}&offset=0&since_id={args.id}")
    data = _ok(res)
    flows = data.get("flows", []) if isinstance(data, dict) else data

    # 4. Client-side string search: look in the request_headers/request_body of subsequent flows
    deps = []
    for f in flows:
        if not isinstance(f, dict):
            continue
        fid = f.get("id")
        if not fid or fid == args.id:
            continue
        req_headers = f.get("request_headers") or ""
        req_body = f.get("request_body") or ""
        if not isinstance(req_headers, str):
            req_headers = json.dumps(req_headers, ensure_ascii=False)
        if not isinstance(req_body, str):
            req_body = json.dumps(req_body, ensure_ascii=False)
        search_text = req_headers + "\n" + req_body
        for v in values_to_track:
            if v.get("value") and v["value"] in search_text:
                deps.append({
                    "source_flow": args.id,
                    "target_flow": fid,
                    "field": v.get("field", ""),
                    "value": v["value"],
                })
    # NDJSON output
    for d in deps:
        print(json.dumps(d, ensure_ascii=False))


def _extract_trace_values(body: str, min_length: int = 4) -> list[dict]:
    """Extract trackable string values from a response body: JSON field values + regex-matched token/session_id/uid, etc.

    min_length filters short strings to reduce false positives (default 4, raise with --min-length).
    """
    out = []
    seen = set()
    if not body or body.startswith("base64:"):
        return out  # binary body cannot be directly extracted as strings
    # 1. JSON field values (leaf strings only, length >= min_length to filter short strings and reduce false positives)
    try:
        obj = json.loads(body)
        for path, val in _walk_json_leaves(obj):
            if isinstance(val, str) and len(val) >= min_length and val not in seen:
                seen.add(val)
                out.append({"field": path, "value": val})
    except Exception:  # noqa: BLE001
        pass
    # 2. Regex-match common token patterns (extractable even when not JSON)
    import re
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


def _walk_json_leaves(obj, prefix: str = ""):
    """Recursively walk JSON, yielding (path, leaf_value). Leaves are non-dict/list types."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            yield from _walk_json_leaves(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{prefix}[{i}]"
            yield from _walk_json_leaves(v, p)
    else:
        yield prefix, obj


def cmd_packets_analyze(args):
    """Signature field auto-detection: pull multiple requests to the same endpoint, parse the JSON body to
    find all leaf fields, and compare values of the same field across requests - fixed length + restricted
    charset + different every time = a suspicious signature field.
    Emits [{field_path, lengths, charsets, varies, sample_values, suspicion_score(0-1)}] (NDJSON).

    With --all, scans across sessions: the first ID is the source, and subsequent flows are pulled from /flows/all?since_id=N.
    Otherwise only the explicitly specified --ids are pulled (fetched one by one via /flows/{fid}, already cross-session).
    """
    flow_ids = getattr(args, "ids", None) or []
    use_all = getattr(args, "all", False)

    # --all mode: requires at least 1 source ID, pull subsequent flows from /flows/all
    if use_all:
        if not flow_ids:
            _die_arg("analyze --all requires at least 1 source flow ID")
        src_id = flow_ids[0]
        limit = getattr(args, "limit", 500) or 500
        res = _req("GET", f"/flows/all?limit={limit}&offset=0&since_id={src_id}")
        data = _ok(res)
        flows = data.get("flows", []) if isinstance(data, dict) else data
        # Ensure the source flow is also included
        try:
            res_src = _req("GET", f"/flows/{src_id}")
            src_flow = _ok(res_src)
            if isinstance(src_flow, dict):
                flows = [src_flow] + [f for f in flows if f.get("id") != src_id]
        except SystemExit:
            pass
    else:
        if len(flow_ids) < 2:
            _die_arg("analyze requires at least 2 flow IDs (use --find-signature for signature field detection)")
        # Pull all flows (a single failure does not block the others)
        flows = []
        for fid in flow_ids:
            try:
                res = _req("GET", f"/flows/{fid}")
                flow = _ok(res)
                if isinstance(flow, dict):
                    flows.append(flow)
            except SystemExit:
                continue
    if len(flows) < 2:
        _die_arg("Fewer than 2 flows fetched successfully; cannot compare")

    # Collect {field_path: [values]} across all flows
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

    # Analyze each field
    findings = []
    for path, vals in field_values.items():
        if len(vals) < 2:
            continue  # appeared only once; cannot compare
        str_vals = [str(v) for v in vals]
        lengths = sorted(set(len(v) for v in str_vals))
        charsets = sorted(set(_classify_charset(v) for v in str_vals))
        varies = len(set(str_vals)) > 1
        sample_values = str_vals[:5]
        # Suspicion score: varies is a necessary condition; add fixed length + restricted charset
        score = 0.0
        if varies:
            score = 0.34  # necessary-condition base score
            if len(lengths) == 1:
                score += 0.33  # fixed length
            if len(charsets) == 1 and charsets[0] in ("hex", "base64", "alphanumeric"):
                score += 0.33  # restricted charset
        findings.append({
            "field_path": path,
            "lengths": lengths,
            "charsets": charsets,
            "varies": varies,
            "sample_values": sample_values,
            "suspicion_score": round(min(1.0, score), 2),
        })
    # Sort by suspicion score descending
    findings.sort(key=lambda x: -x["suspicion_score"])
    for f in findings:
        print(json.dumps(f, ensure_ascii=False))


def _classify_charset(s: str) -> str:
    """Classify a string's charset (by strictest match, for signature field detection)."""
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
    # Pure number first (strictest)
    if _classify_charset._rx_num.match(s):
        return "numeric"
    # hex (0-9a-f, must contain at least one letter)
    if _classify_charset._rx_hex.match(s) and _classify_charset._rx_hex_needed.search(s):
        return "hex"
    # base64 charset (strict base64 only when it contains +/=)
    if _classify_charset._rx_b64.match(s):
        return "base64"
    # alphanumeric + underscore + hyphen
    if _classify_charset._rx_alnum.match(s):
        return "alphanumeric"
    # Pure letters
    if _classify_charset._rx_alpha.match(s):
        return "alpha"
    return "mixed"


def cmd_intercept_export(args):
    """Export all rules to a JSON file."""
    res = _req("GET", "/auto-reply/rules")
    rules = _ok(res)
    rules = rules if isinstance(rules, list) else []
    try:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({"rules": rules, "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, f,
                      ensure_ascii=False, indent=2)
        emit_obj({"exported": True, "path": args.output, "count": len(rules)})
    except Exception as e:  # noqa: BLE001
        emit_obj({"exported": False, "error": str(e)})


def cmd_intercept_import(args):
    """Import rules from a JSON file. merge=append, replace=clear first then import.
    Emits each import result (success/failure + reason) so the agent can locate failed rules.
    """
    if not os.path.isfile(args.file):
        _die_arg(f"File not found: {args.file}")
    try:
        with open(args.file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        _die_arg(f"Read failed: {e}")
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        _die_arg("Invalid file format: missing rules array")
    # replace mode: clear all existing rules first
    deleted = 0
    if args.mode == "replace":
        res = _req("GET", "/auto-reply/rules")
        existing = _ok(res)
        existing = existing if isinstance(existing, list) else []
        existing_ids = [r.get("id") for r in existing if isinstance(r, dict) and r.get("id")]
        if existing_ids:
            res = _req("POST", "/auto-reply/rules/batch-delete", {"ids": existing_ids})
            _ok(res)
            deleted = len(existing_ids)
    # Import one by one, recording each result
    created = 0
    results = []
    for idx, r in enumerate(rules):
        if not isinstance(r, dict):
            results.append({"index": idx, "ok": False, "error": "not an object"})
            continue
        body = {k: v for k, v in r.items() if k != "id"}
        body["enabled"] = r.get("enabled", True)
        try:
            res = _req("POST", "/auto-reply/rules", body)
            created_r = _ok(res)
            created += 1
            results.append({"index": idx, "ok": True, "rule_id": created_r.get("id") if isinstance(created_r, dict) else None,
                            "pattern": body.get("pattern", "")})
        except SystemExit as e:  # noqa: BLE001  _ok failure calls sys.exit
            results.append({"index": idx, "ok": False, "error": "API call failed",
                            "pattern": body.get("pattern", "")})
        except Exception as e:  # noqa: BLE001
            results.append({"index": idx, "ok": False, "error": str(e),
                            "pattern": body.get("pattern", "")})
    emit_obj({"imported": True, "mode": args.mode, "created": created, "deleted_old": deleted,
              "failed": len(rules) - created, "total_in_file": len(rules),
              **({} if getattr(args, "quiet", False) else {"results": results})})


def cmd_intercept_toggle(args):
    """Enable/disable a rule (without deleting). Supports a single rule or batch --all."""
    if getattr(args, "all", False):
        # Batch operation
        res = _req("GET", "/auto-reply/rules")
        rules = _ok(res)
        rules = rules if isinstance(rules, list) else []
        ids = [r.get("id") for r in rules if isinstance(r, dict) and r.get("id")]
        if not ids:
            emit_obj({"toggled": 0, "total": 0, "hint": "No rules"})
            return
        # --enable / --disable decide the target state
        if args.enable:
            enabled = True
        elif args.disable:
            enabled = False
        else:
            _die_arg("Batch toggle requires --enable or --disable")
        res = _req("POST", "/auto-reply/rules/batch-update", {"ids": ids, "enabled": enabled})
        _ok(res)
        emit_obj({"toggled": len(ids), "total": len(ids), "enabled": enabled})
        return
    # Single
    if not args.id:
        _die_arg("intercept toggle requires <rule_id> or --all")
    res = _req("GET", "/auto-reply/rules")
    rules = _ok(res)
    rules = rules if isinstance(rules, list) else []
    target = None
    for r in rules:
        if isinstance(r, dict) and str(r.get("id")) == str(args.id):
            target = r
            break
    if not target:
        _die_arg(f"Rule not found: {args.id}")
    if args.enable:
        new_enabled = True
    elif args.disable:
        new_enabled = False
    else:
        # Toggle the current state
        new_enabled = not target.get("enabled", True)
    body = dict(target)
    body["enabled"] = new_enabled
    # Drop id (the PUT path includes it)
    body.pop("id", None)
    res = _req("PUT", f"/auto-reply/rules/{args.id}", body)
    _ok(res)
    emit_obj({"toggled": True, "rule_id": args.id, "enabled": new_enabled})


def cmd_intercept_update(args):
    """Modify an existing rule (without delete+recreate). Supports --match/--action/--enable/--disable/--note."""
    if not args.id:
        _die_arg("intercept update requires <rule_id>")
    res = _req("GET", "/auto-reply/rules")
    rules = _ok(res)
    rules = rules if isinstance(rules, list) else []
    target = None
    for r in rules:
        if isinstance(r, dict) and str(r.get("id")) == str(args.id):
            target = r
            break
    if not target:
        _die_arg(f"Rule not found: {args.id}")
    body = dict(target)
    body.pop("id", None)
    updated_fields = []
    if args.match:
        m = parse_match(args.match)
        body["pattern"] = m["pattern"]
        body["match_mode"] = m["match_mode"]
        updated_fields.extend(["pattern", "match_mode"])
    if args.action:
        a = parse_action(args.action)
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
    if args.note:
        body["note"] = args.note
        updated_fields.append("note")
    if args.enable:
        body["enabled"] = True
        updated_fields.append("enabled")
    elif args.disable:
        body["enabled"] = False
        updated_fields.append("enabled")
    res = _req("PUT", f"/auto-reply/rules/{args.id}", body)
    _ok(res)
    emit_obj({"updated": True, "rule_id": args.id, "fields": updated_fields})


def cmd_processes(args):
    """Process list (optional connection snapshot/process tree). Backend /processes/snapshot extension.

    Subcommands (optional):
        processes                       list processes with network connections (default)
        processes ignore --pid N --name X   ignore a process (when pid is empty, ignore by name; can add multiple)
        processes unignore <row_id>     un-ignore a process
        processes ignored               list ignored processes
        processes ignore-host <pattern> add a host wildcard to ignore (e.g. *.example.com)
        processes unignore-host <id>    un-ignore a host
        processes ignored-hosts         list ignored hosts
    """
    action = getattr(args, "action", "") or ""
    if action == "ignore":
        body = {"pid": args.pid, "name": args.name or ""}
        if body["pid"] is None and not body["name"]:
            _die_arg("processes ignore requires --pid or --name")
        if body["pid"] is None:
            body["pid"] = None  # explicit null, ignore by name
        res = _req("POST", "/processes/ignore", body)
        emit_obj(_ok(res))
        return
    if action == "unignore":
        if not args.row_id:
            _die_arg("processes unignore requires <row_id>")
        res = _req("DELETE", f"/processes/ignore/{args.row_id}")
        emit_obj(_ok(res))
        return
    if action == "ignored":
        res = _req("GET", "/processes/ignored")
        data = _ok(res)
        items = data if isinstance(data, list) else data.get("items", data.get("processes", []))
        emit_list(items, json_array=getattr(args, "json_array", False))
        return
    if action == "ignore-host":
        if not args.host:
            _die_arg("processes ignore-host requires <host>")
        res = _req("POST", "/processes/ignore-host", {"host": args.host})
        emit_obj(_ok(res))
        return
    if action == "unignore-host":
        if not args.row_id:
            _die_arg("processes unignore-host requires <id>")
        res = _req("DELETE", f"/processes/ignore-host/{args.row_id}")
        emit_obj(_ok(res))
        return
    if action == "ignored-hosts":
        res = _req("GET", "/processes/ignored-hosts")
        data = _ok(res)
        items = data if isinstance(data, list) else data.get("items", data.get("hosts", []))
        emit_list(items, json_array=getattr(args, "json_array", False))
        return
    # Default: list processes (kept for compatibility; runs original logic when no subcommand)
    params = {}
    if getattr(args, "with_connections", False):
        params["with_connections"] = "1"
    if getattr(args, "tree", False):
        params["tree"] = "1"
    if getattr(args, "name", ""):
        params["name"] = args.name
    if getattr(args, "include_listen", False):
        params["include_listen"] = "1"
    qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    url = "/processes/snapshot" + (f"?{qs}" if qs else "")
    try:
        res = _req("GET", url)
        data = _ok(res)
    except Exception:  # noqa: BLE001
        # Fall back to plain /processes
        res = _req("GET", "/processes")
        data = _ok(res)
    procs = data if isinstance(data, list) else data.get("processes", [])
    if getattr(args, "name", ""):
        nl = args.name.lower()
        procs = [p for p in procs if isinstance(p, dict) and nl in (p.get("name") or "").lower()]
    emit_list(procs, json_array=getattr(args, "json_array", False))


def _agent_backup_path() -> str:
    """Path of the agent workspace backup file (system temp dir, retained across restarts)."""
    import tempfile
    return os.path.join(tempfile.gettempdir(), "telnix_agent_workspace_backup.json")


def _get_agent_ignore_processes() -> list[str]:
    """Get agent auto-ignore process list from settings (with hardcoded fallback).

    Design fix: moved from hardcoded AGENT_IGNORE_PROCESSES to settings_store
    for user configurability.
    """
    try:
        from . import settings_store
        procs = settings_store.get_setting("agent_ignore_processes", [])
        if isinstance(procs, list) and procs:
            return procs
    except Exception:  # noqa: BLE001
        pass
    # 默认值（fallback）
    return ["TRAE SOLO CN.exe"]


def cmd_agent(args):
    """Agent workspace mode: start saves current state and disables rules/focus/breakpoints; end restores; status queries.

    Scenario: before taking over a session, the agent runs `agent start` to temporarily turn off all
    factors that affect capture/interception (auto-reply rules, focus mode, breakpoints), and adds the
    agent's own process to the ignore list (to avoid capturing its own traffic). After finishing, run
    `agent end` to restore the user's original state.

    Backup file: `telnix_agent_workspace_backup.json` in the system temp dir, retained across processes.
    Repeated start will error (to avoid overwriting an un-restored backup); end without start also errors.
    """
    action = args.action
    backup_path = _agent_backup_path()

    if action == "start":
        # 1. Guard against overwrite: an existing backup must be ended first
        if os.path.exists(backup_path):
            _die_arg("An unrestored agent workspace backup already exists. Run `agent end` to restore it before `agent start`")

        # 2. Save the current snapshot (rules + focus + breakpoints)
        res = _req("GET", "/snapshot")
        snapshot = _ok(res)

        # 3. Save the current ignored-process list and add the agent's own process to the ignore list
        ignored_res = _req("GET", "/processes/ignored")
        ignored_before = _ok(ignored_res) or []
        # Record process names that were already ignored (used during end to identify which ones the agent added)
        # Field name is process_name (not name)
        ignored_names_before = set()
        for item in ignored_before:
            if isinstance(item, dict):
                n = item.get("process_name") or item.get("name")
                if n:
                    ignored_names_before.add(n.lower())

        agent_added_ignored = []
        for proc_name in _get_agent_ignore_processes():
            if proc_name.lower() not in ignored_names_before:
                try:
                    _ok(_req("POST", "/processes/ignore", {"name": proc_name}))
                    agent_added_ignored.append(proc_name)
                except Exception:  # noqa: BLE001
                    pass

        # Record agent-added ignored processes in the backup; they will be removed on end
        snapshot["agent_added_ignored_processes"] = agent_added_ignored

        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

        # 4. Disable all rules
        rules = snapshot.get("rules", []) or []
        rule_ids = [r.get("id") for r in rules if r.get("id")]
        rules_disabled = 0
        if rule_ids:
            r = _req("POST", "/auto-reply/rules/batch-update",
                     {"ids": rule_ids, "enabled": False})
            _ok(r)
            rules_disabled = len(rule_ids)

        # 5. Turn off focus
        _req("POST", "/focus", {
            "enabled": False, "pids": [], "hosts": [],
            "methods": [], "status_codes": [], "content_types": [],
        })
        _ok(_req("POST", "/focus", {"enabled": False, "pids": []}))

        # 6. Turn off breakpoints (request + response)
        _ok(_req("POST", "/breakpoint/request", {"enabled": False}))
        _ok(_req("POST", "/breakpoint/response", {"enabled": False}))

        emit_obj({
            "agent_workspace": "started",
            "backup_path": backup_path,
            "rules_disabled": rules_disabled,
            "focus_cleared": True,
            "breakpoint_cleared": True,
            "ignored_processes_added": agent_added_ignored,
            "exported_at": snapshot.get("exported_at"),
            "hint": "Workspace cleared: rules disabled, focus off, breakpoints off, agent process added to ignore list. "
                    "Run `agent end` to restore the original state when done.",
        })

    elif action == "end":
        if not os.path.exists(backup_path):
            _die_arg("No unrestored agent workspace backup (already ended or never started)")

        with open(backup_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)

        # 1. Restore rules + focus + breakpoint in one POST /snapshot (clear_rules=true clears before import,
        # to avoid leftover rules created during the agent session)
        restore_body = {
            "rules": snapshot.get("rules", []) or [],
            "focus": snapshot.get("focus", {}) or {},
            "breakpoint": snapshot.get("breakpoint", {}) or {},
            "clear_rules": True,
        }
        res = _req("POST", "/snapshot", restore_body)
        result = _ok(res)

        # 2. Remove the ignored processes added on agent start (keep the ones the user originally ignored)
        agent_added = snapshot.get("agent_added_ignored_processes", []) or []
        ignored_removed = 0
        if agent_added:
            # Fetch the current ignore list, match agent-added process names by row_id and delete
            # Field name is process_name (not name)
            try:
                cur_ignored = _ok(_req("GET", "/processes/ignored")) or []
                added_lower = {n.lower() for n in agent_added}
                for item in cur_ignored:
                    if not isinstance(item, dict):
                        continue
                    n = (item.get("process_name") or item.get("name") or "").lower()
                    if n in added_lower:
                        try:
                            _ok(_req("DELETE", f"/processes/ignore/{item['id']}"))
                            ignored_removed += 1
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001
                pass

        # 3. Delete the backup file
        try:
            os.remove(backup_path)
        except OSError:
            pass

        emit_obj({
            "agent_workspace": "ended",
            "restored": True,
            "rules_restored": result.get("rules_imported", 0),
            "focus_set": result.get("focus_set", False),
            "breakpoint_set": result.get("breakpoint_set", False),
            "ignored_processes_removed": ignored_removed,
            "system_proxy_cleared": False,
            "hint": (
                "Workspace restored to the state before agent start. "
                "Note: auto-reply rules require Telnix to be running to take effect, so the system proxy was not cleared and Telnix was not quit. "
                "Ask the user whether to close Telnix; once the user agrees, run `system quit` (the backend cleans up the system proxy on exit)."
            ),
        })

    elif action == "status":
        if os.path.exists(backup_path):
            try:
                with open(backup_path, "r", encoding="utf-8") as f:
                    snapshot = json.load(f)
                rules_count = len(snapshot.get("rules", []) or [])
                bp = snapshot.get("breakpoint", {}) or {}
                focus = snapshot.get("focus", {}) or {}
                emit_obj({
                    "agent_workspace": "active",
                    "backup_path": backup_path,
                    "rules_in_backup": rules_count,
                    "focus_was_enabled": bool(focus.get("enabled")),
                    "break_on_request_was_on": bool(bp.get("break_on_request")),
                    "break_on_response_was_on": bool(bp.get("break_on_response")),
                    "exported_at": snapshot.get("exported_at"),
                    "hint": "Workspace cleared. Run `agent end` to restore.",
                })
            except Exception as e:  # noqa: BLE001
                _die_arg(f"Backup file is corrupted and cannot be read: {e}")
        else:
            emit_obj({
                "agent_workspace": "inactive",
                "hint": "No active agent workspace (run `agent start` to begin).",
            })


def cmd_settings_get(args):
    """Read all settings or a single key."""
    res = _req("GET", "/settings")
    data = _ok(res)
    if args.key:
        data = {"key": args.key, "value": data.get(args.key)}
    emit_obj(data)
    if not args.json and not args.key:
        print("[Telnix] All current settings (key=value):", file=sys.stderr)
        if isinstance(data, dict):
            for k in sorted(data.keys()):
                print(f"  {k} = {data[k]}", file=sys.stderr)
    elif not args.json and args.key:
        print(f"[Telnix] {args.key} = {data.get('value')}", file=sys.stderr)


def cmd_settings_set(args):
    """Write a single setting item. bool/list/dict are auto-deserialized."""
    import json as _json
    value: Any = args.value
    # Try JSON deserialization (supports true/false/null/number/list/dict)
    try:
        parsed = _json.loads(args.value)
        if isinstance(parsed, (bool, int, float, list, dict)) or parsed is None:
            value = parsed
    except (ValueError, TypeError):
        pass  # keep as string
    body = {args.key: value}
    res = _req("PUT", "/settings", body=body)
    data = _ok(res)
    emit_obj(data)
    if not args.json:
        print(f"[Telnix] Setting updated: {args.key} = {value!r}", file=sys.stderr)
        print("[Telnix] Note: some settings (proxy engine, breakpoints, etc.) require a backend restart to take effect. "
              "Run `telnix system restart` to restart.", file=sys.stderr)


def cmd_settings_engine(args):
    """View/switch the proxy engine."""
    VALID = {"builtin", "async", "mitmproxy"}
    # Fetch current state first
    res = _req("GET", "/settings")
    data = _ok(res)
    current = data.get("proxy_engine") or "builtin"
    mitm_available = bool(data.get("mitmproxy_available"))

    if not args.name:
        # View only
        emit_obj({
            "proxy_engine": current,
            "mitmproxy_available": mitm_available,
            "available_engines": sorted(VALID),
            "hint": "Switch engine: telnix settings engine <name>; run telnix system restart after switching",
        })
        if not args.json:
            print(f"[Telnix] Current proxy engine: {current}", file=sys.stderr)
            print(f"[Telnix] mitmproxy available: {'yes' if mitm_available else 'no'}", file=sys.stderr)
            print("[Telnix] Available engines: builtin (default threaded) / async (asyncio) "
                  "/ mitmproxy (bundled as dependency)", file=sys.stderr)
            print("[Telnix] Switch example: telnix settings engine async", file=sys.stderr)
            print("[Telnix] After switching run: telnix system restart", file=sys.stderr)
        return

    name = args.name.lower().strip()
    if name not in VALID:
        _die_arg(f"Unsupported proxy engine: {name} (options: {', '.join(sorted(VALID))})")

    # mitmproxy engine requires installation first
    if name == "mitmproxy" and not mitm_available:
        emit_obj({
            "ok": False,
            "error": f"mitmproxy is not installed; cannot switch to this engine",
            "hint": "mitmproxy is bundled as a dependency; if unavailable, reinstall Telnix dependencies",
        })
        if not args.json:
            print(f"[Telnix] Error: mitmproxy is not installed; cannot switch to this engine", file=sys.stderr)
            print("[Telnix] Fix: reinstall Telnix dependencies to restore mitmproxy, then switch the engine",
                  file=sys.stderr)
        sys.exit(1)

    # Write setting
    res = _req("PUT", "/settings", body={"proxy_engine": name})
    data = _ok(res)
    emit_obj({
        "ok": True,
        "proxy_engine": name,
        "previous": current,
        "mitmproxy_available": mitm_available,
        "hint": "Requires backend restart to take effect: telnix system restart",
    })
    if not args.json:
        print(f"[Telnix] Proxy engine switched: {current} -> {name}", file=sys.stderr)
    print(f"[Telnix] mitmproxy available: {'yes' if mitm_available else 'no'}", file=sys.stderr)
    print("[Telnix] Important: requires backend restart to take effect. Run: telnix system restart", file=sys.stderr)


def cmd_tools(args):
    """代理工具：No Caching / Force CORS / Block List / Allow List"""
    sub = getattr(args, "sub", None)
    if sub == "status" or sub is None:
        res = _req("GET", "/proxy-tools")
        data = _ok(res)
        emit_obj(data)
    elif sub == "no-cache":
        enabled = args.on if hasattr(args, "on") else None
        if enabled is None:
            res = _req("GET", "/proxy-tools")
            data = _ok(res)
            print(f"no_caching: {'on' if data.get('no_caching') else 'off'}")
        else:
            val = enabled in ("on", "true", "1", "yes")
            res = _req("PUT", "/proxy-tools", {"no_caching": val})
            data = _ok(res)
            emit_obj(data)
    elif sub == "force-cors":
        enabled = args.on if hasattr(args, "on") else None
        if enabled is None:
            res = _req("GET", "/proxy-tools")
            data = _ok(res)
            print(f"force_cors: {'on' if data.get('force_cors') else 'off'}")
        else:
            val = enabled in ("on", "true", "1", "yes")
            res = _req("PUT", "/proxy-tools", {"force_cors": val})
            data = _ok(res)
            emit_obj(data)
    elif sub == "block-list":
        action = args.action
        if action == "list":
            res = _req("GET", "/proxy-tools")
            data = _ok(res)
            rules = data.get("block_list", [])
            print(f"block_list_enabled: {'on' if data.get('block_list_enabled') else 'off'}")
            for i, r in enumerate(rules):
                if isinstance(r, dict):
                    print(f"  [{i}] {r.get('mode', 'wildcard')}: {r.get('pattern', '')}")
                else:
                    print(f"  [{i}] wildcard: {r}")
        elif action == "on":
            res = _req("PUT", "/proxy-tools", {"block_list_enabled": True})
            data = _ok(res)
            emit_obj(data)
        elif action == "off":
            res = _req("PUT", "/proxy-tools", {"block_list_enabled": False})
            data = _ok(res)
            emit_obj(data)
        elif action == "add":
            pattern = args.pattern
            mode = args.mode
            res = _req("POST", "/proxy-tools/block-list", {"pattern": pattern, "mode": mode})
            data = _ok(res)
            emit_obj(data)
        elif action == "del":
            res = _req("DELETE", f"/proxy-tools/block-list/{args.index}")
            data = _ok(res)
            emit_obj(data)
    elif sub == "allow-list":
        action = args.action
        if action == "list":
            res = _req("GET", "/proxy-tools")
            data = _ok(res)
            rules = data.get("allow_list", [])
            print(f"allow_list_enabled: {'on' if data.get('allow_list_enabled') else 'off'}")
            for i, r in enumerate(rules):
                if isinstance(r, dict):
                    print(f"  [{i}] {r.get('mode', 'wildcard')}: {r.get('pattern', '')}")
                else:
                    print(f"  [{i}] wildcard: {r}")
        elif action == "on":
            res = _req("PUT", "/proxy-tools", {"allow_list_enabled": True})
            data = _ok(res)
            emit_obj(data)
        elif action == "off":
            res = _req("PUT", "/proxy-tools", {"allow_list_enabled": False})
            data = _ok(res)
            emit_obj(data)
        elif action == "add":
            pattern = args.pattern
            mode = args.mode
            res = _req("POST", "/proxy-tools/allow-list", {"pattern": pattern, "mode": mode})
            data = _ok(res)
            emit_obj(data)
        elif action == "del":
            res = _req("DELETE", f"/proxy-tools/allow-list/{args.index}")
            data = _ok(res)
            emit_obj(data)
    elif sub == "map-local":
        action = args.action
        if action == "list":
            res = _req("GET", "/proxy-tools")
            data = _ok(res)
            rules = data.get("map_local_rules", [])
            print(f"map_local_enabled: {'on' if data.get('map_local_enabled') else 'off'}")
            for i, r in enumerate(rules):
                if isinstance(r, dict):
                    status = f" status={r['status']}" if r.get("status") else ""
                    print(f"  [{i}] {r.get('mode', 'wildcard')}: {r.get('pattern', '')} -> {r.get('file_path', '')}{status}")
        elif action == "on":
            res = _req("PUT", "/proxy-tools", {"map_local_enabled": True})
            data = _ok(res)
            emit_obj(data)
        elif action == "off":
            res = _req("PUT", "/proxy-tools", {"map_local_enabled": False})
            data = _ok(res)
            emit_obj(data)
        elif action == "add":
            body = {"pattern": args.pattern, "mode": args.mode, "file_path": args.file_path}
            if args.status:
                body["status"] = args.status
            res = _req("POST", "/proxy-tools/map-local", body)
            data = _ok(res)
            emit_obj(data)
        elif action == "del":
            res = _req("DELETE", f"/proxy-tools/map-local/{args.index}")
            data = _ok(res)
            emit_obj(data)
    elif sub == "map-remote":
        action = args.action
        if action == "list":
            res = _req("GET", "/proxy-tools")
            data = _ok(res)
            rules = data.get("map_remote_rules", [])
            print(f"map_remote_enabled: {'on' if data.get('map_remote_enabled') else 'off'}")
            for i, r in enumerate(rules):
                if isinstance(r, dict):
                    print(f"  [{i}] {r.get('mode', 'wildcard')}: {r.get('pattern', '')} -> {r.get('target_url', '')}")
        elif action == "on":
            res = _req("PUT", "/proxy-tools", {"map_remote_enabled": True})
            data = _ok(res)
            emit_obj(data)
        elif action == "off":
            res = _req("PUT", "/proxy-tools", {"map_remote_enabled": False})
            data = _ok(res)
            emit_obj(data)
        elif action == "add":
            res = _req("POST", "/proxy-tools/map-remote", {
                "pattern": args.pattern, "mode": args.mode, "target_url": args.target_url})
            data = _ok(res)
            emit_obj(data)
        elif action == "del":
            res = _req("DELETE", f"/proxy-tools/map-remote/{args.index}")
            data = _ok(res)
            emit_obj(data)
    elif sub == "mirror":
        action = args.action
        if action == "list":
            res = _req("GET", "/proxy-tools")
            data = _ok(res)
            rules = data.get("mirror_rules", [])
            print(f"mirror_enabled: {'on' if data.get('mirror_enabled') else 'off'}")
            for i, r in enumerate(rules):
                if isinstance(r, dict):
                    print(f"  [{i}] {r.get('mode', 'wildcard')}: {r.get('pattern', '')} -> {r.get('save_dir', '')}")
        elif action == "on":
            res = _req("PUT", "/proxy-tools", {"mirror_enabled": True})
            data = _ok(res)
            emit_obj(data)
        elif action == "off":
            res = _req("PUT", "/proxy-tools", {"mirror_enabled": False})
            data = _ok(res)
            emit_obj(data)
        elif action == "add":
            res = _req("POST", "/proxy-tools/mirror", {
                "pattern": args.pattern, "mode": args.mode, "save_dir": args.save_dir})
            data = _ok(res)
            emit_obj(data)
        elif action == "del":
            res = _req("DELETE", f"/proxy-tools/mirror/{args.index}")
            data = _ok(res)
            emit_obj(data)


def cmd_system(args):
    """System control: restart / quit / restart-as-admin.

    - restart: restart front/backend via os.execv in-process
    - quit: exit Telnix (clears system proxy + sys.exit)
    - restart-as-admin: restart as administrator/root (for features requiring admin such as TCP/UDP capture)

    Cross-platform support:
    - Windows: UAC elevation (ShellExecuteW runas)
    - macOS: osascript with administrator privileges
    - Linux: pkexec (GUI popup) or sudo (CLI)
    - firewall-allow / firewall-status: Windows only (delegated to backend); skipped on non-Windows
    """
    action = args.action
    if action == "restart":
        res = _req("POST", "/system/restart")
        _ok(res)
        emit_obj({"restarting": True})
    elif action == "quit":
        res = _req("POST", "/system/quit")
        _ok(res)
        emit_obj({"quitting": True})
    elif action == "restart-as-admin":
        # Cross-platform elevation: Windows UAC / macOS osascript / Linux pkexec/sudo
        # Goes through a GUI user-confirmation flow: create a pending request -> long-poll for response
        # User accepts -> backend shows the elevation prompt -> CLI receives accepted
        # User rejects -> CLI receives rejected
        # No response within 60s -> CLI retries once; still no response -> timeout failure
        res = _req("POST", "/system/request-admin-restart", timeout=10.0)
        data = _ok(res)
        rid = data.get("request_id")
        if not rid:
            _die_arg("Backend did not return request_id")
        # Long polling: up to 3 retries (60s each), covering a 3-minute window
        final_status = None
        final_msg = None
        for _ in range(3):
            r = _req("GET", f"/system/admin-request/{rid}/wait", timeout=65.0)
            if r.get("code") != 0:
                # Request does not exist or has been processed
                _ok(r)  # _ok will die and print the error
                return
            d = r.get("data") or {}
            status = d.get("status")
            if status == "accepted":
                final_status = "accepted"
                final_msg = r.get("msg") or "User approved; restarting as administrator"
                break
            elif status == "rejected":
                final_status = "rejected"
                final_msg = r.get("msg") or "User rejected the admin restart request"
                break
            # status == "pending", continue to next round
        if final_status is None:
            _die_arg("Timed out waiting for user response (no response within 3 minutes)")
        if final_status == "accepted":
            emit_obj({"restarting": True, "as_admin": True, "approved": True, "message": final_msg})
        else:
            # User rejected: return a structured error so the agent can recognize it
            err_obj = {
                "ok": False,
                "error": final_msg,
                "rejected_by_user": True,
                "hint": "User rejected the admin restart request. You can restart manually in the GUI, or retry after the user agrees.",
            }
            print(json.dumps(err_obj, ensure_ascii=False), file=sys.stderr)
            sys.exit(1)
    elif action == "firewall-allow":
        # Allow ports 8888 (proxy) and 18901 (API) through Windows Firewall (delegated to backend)
        res = _req("POST", "/system/firewall-allow", timeout=30)
        data = _ok(res)
        emit_obj(data)
        if isinstance(data, dict) and not data.get("firewall_allow"):
            sys.exit(1)
    elif action == "firewall-status":
        # Query whether Telnix-related firewall rules exist (delegated to backend)
        res = _req("GET", "/system/firewall-status", timeout=15)
        data = _ok(res)
        emit_obj(data)
    elif action == "windivert-warning-status":
        # Query WinDivert risk-prompt status (needed/ack/message)
        res = _req("GET", "/system/windivert-warning")
        data = _ok(res)
        emit_obj(data)
    elif action == "windivert-warning-ack":
        # Mark the WinDivert risk prompt as acknowledged (will not be shown again)
        res = _req("POST", "/system/windivert-warning/ack")
        data = _ok(res)
        emit_obj(data)
        if not args.json:
            print("[Telnix] WinDivert risk prompt acknowledged; will not be shown again", file=sys.stderr)
    elif action == "platform-capabilities":
        # Query current platform capability info (cross-platform feature support)
        # Used by agent/CLI to determine which features are available and what privileges are required
        res = _req("GET", "/system/platform-capabilities")
        data = _ok(res)
        emit_obj(data)
        if not args.json:
            caps = data.get("capabilities", {}) if isinstance(data, dict) else {}
            print(f"[Telnix] Platform: {data.get('platform')} . "
                  f"{'administrator' if data.get('is_admin') else 'non-administrator/root'}", file=sys.stderr)
            for name, info in caps.items():
                supported = info.get("supported") if isinstance(info, dict) else False
                backend = info.get("backend", "none") if isinstance(info, dict) else "none"
                status_str = "v" if supported else "x"
                print(f"  [{status_str}] {name}: {backend}", file=sys.stderr)
    else:
        _die_arg("system requires: restart | quit | restart-as-admin | firewall-allow | "
                 "firewall-status | "
                 "windivert-warning-status | windivert-warning-ack | "
                 "platform-capabilities")


def cmd_intercept_add(args):
    m = parse_match(args.match)
    a = parse_action(args.action)
    note = args.note or args.name or f"CLI: {args.action}"
    if args.dry_run:
        sid = _get_session(args)
        res = _req("GET", f"/sessions/{sid}/flows?limit=500&offset=0")
        data = _ok(res)
        flows = data.get("flows", []) if isinstance(data, dict) else data
        matched = [f for f in flows if flow_matches(f, m["filters"])]
        would_create = {
            "pattern": m["pattern"],
            "match_mode": m["match_mode"],
            "action": a.get("action"),
            "modify_rules": a.get("modify_rules", []),
            "mock_status": a.get("mock_status"),
            "mock_body": a.get("mock_body", ""),
            "mock_headers": a.get("mock_headers"),
            "note": note,
        }
        # Attach filter fields (only non-empty ones, so the agent can confirm the backend will apply these filters)
        for fk in ("method_filter", "status_filter", "pid_filter", "process_filter"):
            if m.get(fk):
                would_create[fk] = m[fk]
        emit_obj({
            "dry_run": True,
            "would_create": would_create,
            "matched_count": len(matched),
            "matched_flows": [{"id": f.get("id"), "method": f.get("method"), "host": f.get("host"),
                               "path": f.get("path"), "url": f.get("url")} for f in matched[:20]],
        })
        return
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
        # §4.1 Filter fields: the proxy layer's find_matching_rule validates these (effective only when non-empty)
        "method_filter": m.get("method_filter", ""),
        "status_filter": m.get("status_filter", ""),
        "pid_filter": m.get("pid_filter", ""),
        "process_filter": m.get("process_filter", ""),
    }
    # §3.1 --idempotent: prefer the backend dedicated endpoint (single request + server-side comparison, more efficient than client-side traversal)
    # On failure (e.g. old backend without the endpoint) falls back to client-side traversal for compatibility
    if getattr(args, "idempotent", False):
        try:
            # Prefer the backend POST /auto-reply/rules/idempotent endpoint
            idem_res = _req("POST", "/auto-reply/rules/idempotent", rule)
            idem_data = _ok(idem_res)
            out = {
                "created": idem_data.get("created", False),
                "idempotent": idem_data.get("idempotent", not idem_data.get("created", False)),
                "rule_id": idem_data.get("rule_id"),
                "pattern": m["pattern"],
                "note": note,
            }
            if idem_data.get("hint"):
                out["hint"] = idem_data["hint"]
            emit_obj(out)
            return
        except Exception:  # noqa: BLE001
            # Fallback: old backend without idempotent endpoint, use client-side traversal
            try:
                list_res = _req("GET", "/auto-reply/rules")
                existing = _ok(list_res)
                existing = existing if isinstance(existing, list) else []
                import json as _json
                new_sig = _json.dumps(rule.get("modify_rules", []), sort_keys=True)
                for r in existing:
                    if not isinstance(r, dict):
                        continue
                    if (r.get("pattern") == rule["pattern"]
                            and r.get("match_mode") == rule["match_mode"]
                            and r.get("action") == rule["action"]
                            and _json.dumps(r.get("modify_rules", []), sort_keys=True) == new_sig):
                        out = {"created": False, "idempotent": True,
                               "rule_id": r.get("id"), "pattern": m["pattern"], "note": note,
                               "hint": "An identical rule already exists; not created again (client-side traversal fallback)"}
                        emit_obj(out)
                        return
            except Exception:  # noqa: BLE001
                pass  # On query failure, proceed to create normally
    res = _req("POST", "/auto-reply/rules", rule)
    created = _ok(res)
    out = {"created": True, "rule_id": created.get("id"), "pattern": m["pattern"], "note": note}
    for fk in ("method_filter", "status_filter", "pid_filter", "process_filter"):
        if m.get(fk):
            out[fk] = m[fk]
    emit_obj(out)


def cmd_intercept_list(args):
    res = _req("GET", "/auto-reply/rules")
    rules = _ok(res)
    rules = rules if isinstance(rules, list) else []
    # Add a rule_id alias uniformly (backend field is id; docs use rule_id so the agent can extract it easily)
    for r in rules:
        if isinstance(r, dict) and "rule_id" not in r:
            r["rule_id"] = r.get("id")
        # §4.1 Empty filter fields output null instead of empty string, so the agent doesn't need to special-case method_filter != ""
        if isinstance(r, dict):
            for fk in ("method_filter", "status_filter", "pid_filter", "process_filter"):
                if r.get(fk) == "":
                    r[fk] = None
    # §4.1 intercept list outputs hit_count/last_hit_at/last_hit_flow_id by default (already returned by backend)
    # --with-stats is a semantic flag kept for compatibility (stats are output even without it)
    emit_list(rules, json_array=args.json_array)


def cmd_intercept_hits(args):
    """§4.1 Show a rule's hit statistics + last-hit flow details.

    The backend only stores last_hit_flow_id (single); there is no full hit-history table.
    Output: {rule_id, hit_count, last_hit_at, last_hit_flow_id, last_hit_flow: {...}|null}
    """
    rule_id = args.id
    if not rule_id:
        _die_arg("intercept hits requires <rule_id>")
    res = _req("GET", "/auto-reply/rules")
    rules = _ok(res)
    rules = rules if isinstance(rules, list) else []
    rule = None
    for r in rules:
        if isinstance(r, dict) and (r.get("id") == rule_id or r.get("rule_id") == rule_id):
            rule = r
            break
    if not rule:
        _die_arg(f"Rule not found: {rule_id}")
    out = {
        "rule_id": rule.get("id"),
        "pattern": rule.get("pattern"),
        "action": rule.get("action"),
        "hit_count": rule.get("hit_count", 0),
        "last_hit_at": rule.get("last_hit_at", ""),
        "last_hit_flow_id": rule.get("last_hit_flow_id"),
    }
    # If there is a last-hit flow_id, fetch that flow's details
    last_fid = rule.get("last_hit_flow_id")
    if last_fid:
        try:
            flow_res = _req("GET", f"/flows/{last_fid}")
            flow_data = _ok(flow_res)
            out["last_hit_flow"] = flow_data
        except Exception:  # noqa: BLE001
            out["last_hit_flow"] = None
    else:
        out["last_hit_flow"] = None
    emit_obj(out)


def cmd_intercept_del(args):
    if args.ids:
        ids = [x.strip() for x in args.ids.split(",") if x.strip()]
        res = _req("POST", "/auto-reply/rules/batch-delete", {"ids": ids})
        _ok(res)
        emit_obj({"deleted": len(ids)})
    elif args.id:
        res = _req("DELETE", f"/auto-reply/rules/{args.id}")
        _ok(res)
        emit_obj({"deleted": 1, "id": args.id})
    else:
        _die_arg("Requires <id> or --ids")


def cmd_intercept_template(args):
    """§3.17 Rule template library CLI wrapper: list lists templates, apply applies a template to create a rule."""
    action = getattr(args, "action", "") or ""
    if action == "list":
        res = _req("GET", "/templates")
        data = _ok(res)
        data = data if isinstance(data, list) else []
        emit_list(data, json_array=getattr(args, "json_array", False))
        return
    if action == "apply":
        name = args.name
        pattern = args.match
        if not name:
            _die_arg("template apply requires <name>")
        if not pattern:
            _die_arg("template apply requires --match")
        body: dict = {
            "pattern": pattern,
            "match_mode": args.match_mode,
            "note": args.note or "",
            "enabled": not args.disabled,
        }
        for fk in ("method_filter", "status_filter", "pid_filter", "process_filter"):
            v = getattr(args, fk, "") or ""
            if v:
                body[fk] = v
        res = _req("POST", f"/templates/{name}/apply", body)
        data = _ok(res)
        emit_obj(data)
        return
    _die_arg("intercept template requires: list | apply <name>")


def cmd_replay(args):
    body = {}
    if args.body:
        body["body"] = args.body
    if args.host:
        body["host"] = args.host
    if args.port:
        body["port"] = args.port
    if args.method:
        body["method"] = args.method
    if args.url:
        body["url"] = args.url
    if args.header:
        headers = {}
        for h in args.header:
            if ":" in h:
                k, v = h.split(":", 1)
                headers[k.strip()] = v.strip()
        body["headers"] = headers
    if args.fuzz:
        body["fuzz"] = args.fuzz
    repeat = getattr(args, "repeat", 1) or 1
    parallel = getattr(args, "parallel", 1) or 1
    compare = getattr(args, "compare", False)
    interval_ms = getattr(args, "interval_ms", 0) or 0
    server_side = getattr(args, "server_side", False)
    # --timeout N overrides the default timeout (default: 120s with body, 30s without body)
    user_timeout = getattr(args, "timeout", 0) or 0

    def _req_timeout() -> float:
        if user_timeout > 0:
            return float(user_timeout)
        return 120.0 if body else 30.0

    # --fuzz-file multi-field combination fuzz takes precedence over --fuzz (mutually exclusive; --fuzz-file wins)
    fuzz_file = getattr(args, "fuzz_file", "") or ""
    if fuzz_file:
        _replay_fuzz_file(args, body)
        return

    if repeat <= 1:
        if body:
            res = _req("POST", f"/flows/{args.id}/replay", body, timeout=_req_timeout())
        else:
            res = _req("POST", f"/flows/{args.id}/replay", timeout=_req_timeout())
        data = _ok(res)
        emit_obj(data)
        return

    # --server-side: offload batch replay to backend /repeat endpoint (unified concurrency/interval/stats)
    if server_side:
        rp_body = {
            "count": repeat,
            "concurrency": parallel,
            "interval_ms": interval_ms,
            "override": body if body else None,
        }
        # 服务端可能跑较久，按 count * 单次最大耗时估算超时
        srv_timeout = max(_req_timeout() * repeat / max(parallel, 1), 60.0)
        res = _req("POST", f"/flows/{args.id}/repeat", rp_body, timeout=srv_timeout)
        data = _ok(res)
        emit_obj(data)
        return

    # Batch replay (client-side)
    if parallel > 1:
        # Concurrent mode: use ThreadPoolExecutor to replay N times; each entry outputs index/status/duration/size
        from concurrent.futures import ThreadPoolExecutor
        import time as _time

        def _do_replay(idx: int) -> dict:
            try:
                if body:
                    res = _req("POST", f"/flows/{args.id}/replay", body, timeout=_req_timeout())
                else:
                    res = _req("POST", f"/flows/{args.id}/replay", timeout=_req_timeout())
                data = _ok(res)
                return {"index": idx, "ok": True,
                        "status": data.get("status_code") if isinstance(data, dict) else None,
                        "duration": data.get("duration_ms") if isinstance(data, dict) else None,
                        "size": data.get("size") if isinstance(data, dict) else None,
                        "data": data}
            except SystemExit:  # _ok calls sys.exit on failure; catch inside the thread to avoid cascading
                return {"index": idx, "ok": False, "error": "API call failed"}
            except Exception as e:  # noqa: BLE001
                return {"index": idx, "ok": False, "error": str(e)}

        results = []
        interval_s = interval_ms / 1000.0
        with ThreadPoolExecutor(max_workers=parallel) as ex:
            futures = []
            for i in range(repeat):
                futures.append(ex.submit(_do_replay, i))
                if interval_s > 0:
                    _time.sleep(interval_s)
            for fut in futures:
                results.append(fut.result())
        # Sort by index (concurrent completion order may vary)
        results.sort(key=lambda x: x.get("index", 0))
    else:
        # Serial mode (preserves original behavior)
        import time as _time
        interval_s = interval_ms / 1000.0
        results = []
        for i in range(repeat):
            try:
                if body:
                    res = _req("POST", f"/flows/{args.id}/replay", body, timeout=_req_timeout())
                else:
                    res = _req("POST", f"/flows/{args.id}/replay", timeout=_req_timeout())
                data = _ok(res)
                results.append({"index": i, "ok": True, "data": data})
            except Exception as e:  # noqa: BLE001
                results.append({"index": i, "ok": False, "error": str(e)})
            if interval_s > 0 and i < repeat - 1:
                _time.sleep(interval_s)

    out = {"replayed": True, "id": args.id, "count": repeat, "parallel": parallel,
           "interval_ms": interval_ms, "results": results}
    # --compare: compare response differences across runs (using the first run as baseline)
    if compare and len(results) >= 2:
        import difflib
        bodies = []
        for r in results:
            d = r.get("data") if r.get("ok") else {}
            rb = d.get("response_body") if isinstance(d, dict) else ""
            try:
                rb = json.dumps(json.loads(rb), ensure_ascii=False, indent=2, sort_keys=True) if rb else ""
            except Exception:  # noqa: BLE001
                pass
            bodies.append(rb.splitlines(keepends=False) if isinstance(rb, str) else [])
        # Compare subsequent runs against the first as baseline
        diffs = []
        for i in range(1, len(bodies)):
            diff = list(difflib.unified_diff(bodies[0], bodies[i],
                                              fromfile=f"run0", tofile=f"run{i}", lineterm=""))
            diffs.append({"run": i, "identical": not diff, "diff": "\n".join(diff)})
        out["compare"] = diffs
    emit_obj(out)


def _replay_fuzz_file(args, base_body: dict):
    """Multi-field combination fuzz: load {field: [values...]} from payloads.json,
    generate combinations in cartesian/zip mode, replace the corresponding fields in the JSON body each time, then replay.
    Mutually exclusive with --fuzz (single-field numeric range); --fuzz-file takes precedence.
    """
    if not os.path.isfile(args.fuzz_file):
        _die_arg(f"File not found: {args.fuzz_file}")
    try:
        with open(args.fuzz_file, "r", encoding="utf-8") as f:
            payloads = json.load(f)
    except Exception as e:  # noqa: BLE001
        _die_arg(f"Failed to read payloads.json: {e}")
    if not isinstance(payloads, dict) or not payloads:
        _die_arg("Invalid payloads.json format: expected {field: [values...]}")

    mode = (getattr(args, "mode", "") or "cartesian").lower()
    keys = list(payloads.keys())
    value_lists = [payloads[k] if isinstance(payloads[k], list) else [payloads[k]] for k in keys]

    # Generate combinations
    if mode == "zip":
        combos = list(zip(*value_lists))  # uses the shortest length
    else:  # cartesian
        import itertools
        combos = list(itertools.product(*value_lists))

    if not combos:
        _die_arg("No valid combinations in payloads.json")

    # Fetch the original flow's request_body as template (--body takes precedence)
    res = _req("GET", f"/flows/{args.id}")
    flow = _ok(res)
    if not isinstance(flow, dict):
        _die_arg("Cannot get flow details")
    orig_body = base_body.get("body") or flow.get("request_body") or ""
    if not orig_body:
        _die_arg("Original flow has no request_body; cannot replace fields")
    try:
        body_obj = json.loads(orig_body)
    except Exception as e:  # noqa: BLE001
        _die_arg(f"Original request_body is not JSON: {e}")
    if not isinstance(body_obj, dict):
        _die_arg("Original request_body is not a JSON object; cannot replace fields")

    import copy
    user_timeout = getattr(args, "timeout", 0) or 0
    req_timeout = float(user_timeout) if user_timeout > 0 else 120.0
    results = []
    for idx, combo in enumerate(combos):
        # Deep-copy and replace fields
        new_body = copy.deepcopy(body_obj)
        for k, v in zip(keys, combo):
            _set_json_path(new_body, k, v)
        replay_body = dict(base_body)
        replay_body["body"] = json.dumps(new_body, ensure_ascii=False)
        try:
            res = _req("POST", f"/flows/{args.id}/replay", replay_body, timeout=req_timeout)
            data = _ok(res)
            results.append({
                "index": idx, "ok": True,
                "fields": dict(zip(keys, combo)),
                "status": data.get("status_code") if isinstance(data, dict) else None,
                "duration": data.get("duration_ms") if isinstance(data, dict) else None,
                "size": data.get("size") if isinstance(data, dict) else None,
            })
        except SystemExit:
            results.append({"index": idx, "ok": False, "error": "API call failed",
                            "fields": dict(zip(keys, combo))})
        except Exception as e:  # noqa: BLE001
            results.append({"index": idx, "ok": False, "error": str(e),
                            "fields": dict(zip(keys, combo))})
    emit_obj({
        "fuzz_file": args.fuzz_file, "mode": mode, "count": len(combos),
        "fields": keys, "id": args.id, "results": results,
    })


def _set_json_path(obj: dict, path: str, value: Any) -> None:
    """Support dotted path a.b.c for setting JSON fields (auto-creates intermediate nodes)."""
    if "." not in path:
        obj[path] = value
        return
    cur = obj
    parts = path.split(".")
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def cmd_export(args):
    sid = _get_session(args)
    res = _req("POST", f"/export/{sid}", {"format": args.format}, timeout=60)
    data = _ok(res)
    content = data.get("content") if isinstance(data, dict) else data
    # When -o is not specified, derive the default filename from format
    out = args.output
    if not out:
        ext_map = {"har": "har", "json": "json", "csv": "csv",
                   "python-requests": "py", "postman": "json", "curl": "sh",
                   "pcap": "pcap"}
        ext = ext_map.get(args.format, "txt")
        out = f"telnix_export.{ext}"
    try:
        # pcap 格式：后端返回 base64 编码的二进制，需解码后以二进制写入
        if args.format == "pcap" and isinstance(content, str):
            import base64
            pcap_bytes = base64.b64decode(content)
            with open(out, "wb") as f:
                f.write(pcap_bytes)
        elif isinstance(content, str):
            with open(out, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(content, f, ensure_ascii=False, indent=2)
        emit_obj({"exported": True, "path": out, "format": args.format})
    except Exception as e:  # noqa: BLE001
        emit_obj({"exported": False, "error": str(e)})


def cmd_trigger(args):
    """触发式捕获配置。"""
    action = getattr(args, "trigger_action", "")
    if action == "set":
        dsl = getattr(args, "dsl", "")
        res = _req("PUT", "/capture/trigger", {"dsl": dsl})
    elif action == "reset":
        res = _req("POST", "/capture/trigger/reset")
    elif action == "clear":
        res = _req("PUT", "/capture/trigger", {"dsl": ""})
    else:  # get/status
        res = _req("GET", "/capture/trigger")
    data = _ok(res)
    emit_obj(data)


def cmd_heatmap(args):
    """流量热力图聚合数据。"""
    params = {
        "group_by": getattr(args, "by", "host") or "host",
        "bucket_seconds": getattr(args, "bucket", 60),
        "max_buckets": getattr(args, "max_buckets", 120),
        "top_n": getattr(args, "top", 20),
    }
    host = getattr(args, "host", "")
    process = getattr(args, "process", "")
    if host:
        params["host"] = host
    if process:
        params["process"] = process
    res = _req("GET", "/flows/heatmap", params)
    data = _ok(res)
    emit_obj(data)


def cmd_topology(args):
    """网络拓扑数据。"""
    params = {"max_nodes": getattr(args, "max_nodes", 100)}
    host = getattr(args, "host", "")
    process = getattr(args, "process", "")
    if host:
        params["host"] = host
    if process:
        params["process"] = process
    res = _req("GET", "/flows/topology", params)
    data = _ok(res)
    emit_obj(data)


def cmd_send(args):
    """Send from scratch: construct and send an HTTP request (Composer feature).

    Does not depend on an existing flow; constructs the request directly. Supports --method/--url/--header/--body/--timeout.
    Outputs the response status_code/headers/body/duration.
    """
    method = (args.method or "GET").upper()
    url = (args.url or "").strip()
    if not url:
        _die_arg("--url is required")
    if not url.startswith(("http://", "https://")):
        _die_arg("--url must start with http:// or https://")
    body = {}
    headers = {}
    if args.header:
        for h in args.header:
            if ":" in h:
                k, v = h.split(":", 1)
                headers[k.strip()] = v.strip()
    if headers:
        body["headers"] = headers
    if args.body or args.body_file:
        if args.body_file:
            try:
                with open(args.body_file, "r", encoding="utf-8") as f:
                    body["body"] = f.read()
            except Exception as e:  # noqa: BLE001
                _die_arg(f"Failed to read --body-file: {e}")
        else:
            body["body"] = args.body
    body["method"] = method
    body["url"] = url
    body["timeout"] = float(args.timeout or 30)

    # --emit-curl: only output the curl command, do not send
    if getattr(args, "emit_curl", False):
        parts = ["curl", "-X", method]
        for k, v in headers.items():
            parts += ["-H", f"{k}: {v}"]
        if body.get("body"):
            parts += ["--data-raw", body["body"]]
        parts.append('"' + url + '"')
        print(" ".join(parts))
        return

    timeout = float(args.timeout or 30) + 5  # Give the backend an extra 5s to process
    res = _req("POST", "/send", body, timeout=timeout)
    data = _ok(res)
    if getattr(args, "headers_only", False):
        # Only output response headers
        emit_obj({
            "status_code": data.get("status_code"),
            "reason": data.get("reason", ""),
            "headers": data.get("response_headers", {}),
            "size": data.get("size", 0),
            "duration_ms": data.get("duration_ms", 0),
        })
    elif getattr(args, "body_only", False):
        # Only output the response body (plain text, not JSON-wrapped)
        print(data.get("response_body", ""))
    else:
        emit_obj(data)


def cmd_replay_batch(args):
    """Timed replay: fetch all flows of the given session, sort by timestamp, and replay them as a batch.
    - --preserve-timing: sleep according to original intervals then replay (tests server rate limiting/risk control), serial
    - Without --preserve-timing: replay immediately back-to-back; can combine with --parallel for concurrency
    - --filter: client-side filter expression; only matching flows are replayed
    Outputs index/flow_id/status/duration per replay (NDJSON).
    """
    if not args.session:
        _die_arg("replay-batch requires --session N")
    # Fetch all flows in the session
    res = _req("GET", f"/sessions/{args.session}/flows?limit=50000&offset=0")
    data = _ok(res)
    flows = data.get("flows", []) if isinstance(data, dict) else data
    if not flows:
        emit_obj({"replayed": True, "session": args.session, "count": 0,
                  "filtered_count": 0, "results": []})
        return
    flows = [f for f in flows if isinstance(f, dict)]
    total_before = len(flows)
    # §4.3 Client-side filter: --filter expression (uses parse_match + flow_matches)
    filter_expr = getattr(args, "filter", "") or ""
    if filter_expr:
        filters = parse_match(filter_expr)["filters"]
        flows = [f for f in flows if flow_matches(f, filters)]
    filtered_count = len(flows)
    if not flows:
        emit_obj({"replayed": True, "session": args.session, "count": 0,
                  "total_before_filter": total_before, "filtered_count": 0,
                  "results": []})
        return
    # Sort by timestamp ascending
    flows.sort(key=lambda f: f.get("timestamp") or "")

    preserve_timing = getattr(args, "preserve_timing", False)
    parallel = getattr(args, "parallel", 1) or 1

    def _replay_one(flow: dict, idx: int) -> dict:
        fid = flow.get("id")
        if not fid:
            return {"index": idx, "ok": False, "error": "no flow id"}
        try:
            res = _req("POST", f"/flows/{fid}/replay", timeout=120)
            data = _ok(res)
            return {"index": idx, "flow_id": fid, "ok": True,
                    "status": data.get("status_code") if isinstance(data, dict) else None,
                    "duration": data.get("duration_ms") if isinstance(data, dict) else None}
        except SystemExit:
            return {"index": idx, "flow_id": fid, "ok": False, "error": "API call failed"}
        except Exception as e:  # noqa: BLE001
            return {"index": idx, "flow_id": fid, "ok": False, "error": str(e)}

    if preserve_timing:
        # Preserve original intervals, serial replay, streaming output
        from datetime import datetime
        prev_ts = None
        for idx, f in enumerate(flows):
            ts_str = f.get("timestamp")
            if ts_str and prev_ts:
                try:
                    cur = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    prev = datetime.fromisoformat(prev_ts.replace("Z", "+00:00"))
                    delta = (cur - prev).total_seconds()
                    if delta > 0:
                        time.sleep(min(delta, 60))  # Sleep at most 60s per step to avoid hanging
                except Exception:  # noqa: BLE001
                    pass
            r = _replay_one(f, idx)
            print(json.dumps(r, ensure_ascii=False))
            prev_ts = ts_str
    elif parallel > 1:
        # §3.2 Concurrent immediate replay; use as_completed for true streaming (output as each completes, not blocking on earlier ones)
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=parallel) as ex:
            futures = {ex.submit(_replay_one, f, idx): idx for idx, f in enumerate(flows)}
            for fut in as_completed(futures):
                try:
                    r = fut.result()
                    print(json.dumps(r, ensure_ascii=False), flush=True)
                except Exception as e:  # noqa: BLE001
                    idx = futures.get(fut, -1)
                    print(json.dumps({"index": idx, "ok": False, "error": str(e)},
                                     ensure_ascii=False), flush=True)
    else:
        # Serial immediate replay, streaming output
        for idx, f in enumerate(flows):
            r = _replay_one(f, idx)
            print(json.dumps(r, ensure_ascii=False))
    # §4.3 Summary info (to stderr, so it doesn't interfere with stdout's NDJSON stream)
    summary = {"replayed": True, "session": args.session,
               "count": filtered_count, "filtered_count": filtered_count}
    if filter_expr:
        summary["total_before_filter"] = total_before
        summary["filter"] = filter_expr
    print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)


def cmd_proxy(args):
    if args.action == "status":
        res = _req("GET", "/status")
        data = _ok(res)
        emit_obj({"system_proxy_on": data.get("system_proxy_on"),
                  "proxy_host": data.get("proxy_host"), "proxy_port": data.get("proxy_port")})
    elif args.action == "on":
        res = _req("POST", "/system/enable-proxy")
        _ok(res)
        emit_obj({"system_proxy_on": True})
    elif args.action == "off":
        res = _req("POST", "/system/clear-proxy")
        _ok(res)
        emit_obj({"system_proxy_on": False})
    else:
        _die_arg("proxy requires: status | on | off")


def cmd_raw(args):
    if args.action == "status":
        res = _req("GET", "/raw/status")
        data = _ok(res)
        backend = data.get("backend", "windivert")
        is_admin = data.get("is_admin", False)
        if backend == "windivert":
            # Windows: check pydivert and administrator privileges
            if not data.get("pydivert_installed"):
                data["hint"] = "Run: python -m telnix.cli raw install"
            elif not is_admin:
                data["hint"] = "Administrator privileges required. Restart Telnix as administrator"
        else:
            # Unix: check root privileges
            if not is_admin:
                data["hint"] = "Root privileges required. Start Telnix with sudo"
        emit_obj(data)
    elif args.action == "start":
        body = {}
        if args.pid:
            body["pid_filter"] = [int(p) for p in args.pid.split(",") if p.strip()]
        if args.port:
            body["port_filter"] = [int(p) for p in args.port.split(",") if p.strip()]
        if args.bpf:
            body["filter_str"] = args.bpf
        # On first enable with an unacknowledged WinDivert risk prompt, automatically trigger a top-most native desktop popup
        res = _req_with_windivert_ack("POST", "/raw/start", body, timeout=10)
        data = _ok(res)
        emit_obj(data)
    elif args.action == "stop":
        res = _req("POST", "/raw/stop")
        data = _ok(res)
        emit_obj(data)
    else:
        _die_arg("raw requires: status | install | start | stop")


def cmd_cert(args):
    if args.action == "status":
        res = _req("GET", "/cert/status")
        emit_obj(_ok(res))
    elif args.action == "install":
        res = _req("POST", "/cert/install")
        emit_obj(_ok(res))
    elif args.action == "remove":
        res = _req("POST", "/cert/remove")
        emit_obj(_ok(res))
    else:
        _die_arg("cert requires: status | install | remove")


def cmd_transparent_proxy(args):
    """Transparent proxy control (cross-platform NETWORK-layer redirection; requires administrator/root privileges).

    Redirects outbound HTTP(80)/HTTPS(443) traffic to the local proxy so apps can be captured without proxy configuration.
    Platform support:
    - Windows: WinDivert NETWORK-layer interception (requires administrator + pydivert)
    - Linux: iptables NAT REDIRECT (requires root)
    - macOS: pf rdr (requires root)
    """
    if args.action == "status":
        res = _req("GET", "/transparent-proxy/status")
        data = _ok(res)
        # Give a clear hint when not elevated
        if isinstance(data, dict):
            if not data.get("running") and data.get("last_error"):
                le = data.get("last_error", "")
                if "administrator" in le.lower() or "admin" in le.lower() or "root" in le.lower() or "privilege" in le.lower():
                    data["hint"] = "Administrator/root privileges required. Run: python -m telnix.cli system restart-as-admin"
        emit_obj(data)
    elif args.action == "start":
        # On first enable with an unacknowledged WinDivert risk prompt (Windows only), automatically trigger a top-most native desktop popup
        res = _req_with_windivert_ack("POST", "/transparent-proxy/start")
        data = _ok(res)
        emit_obj(data)
    elif args.action == "stop":
        res = _req("POST", "/transparent-proxy/stop")
        data = _ok(res)
        emit_obj(data)
    else:
        _die_arg("transparent-proxy requires: status | start | stop")


def cmd_dns_hijack(args):
    """DNS hijack control (cross-platform: modifies A records in local DNS responses).

    Platform support:
    - Windows: WinDivert intercepts UDP 53 response packets and modifies A records (requires administrator + pydivert)
    - Linux: iptables NAT redirects UDP 53 to a local DNS server (requires root)
    - macOS: pf rdr redirects UDP 53 to a local DNS server (requires root)

    Subcommands:
    - status: view running state, rules, stats, and recent logs
    - start: start hijacking (can take initial rules via --rules and --default-ip)
    - stop: stop hijacking
    - rules: view/update rules (--set updates; without --set only views)
    - clear-log: clear the hijack log and stat counters
    """
    if args.action == "status":
        res = _req("GET", "/dns-hijack/status")
        data = _ok(res)
        # Give a clear hint when not elevated
        if isinstance(data, dict):
            if not data.get("running") and data.get("last_error"):
                le = data.get("last_error", "")
                if "administrator" in le.lower() or "admin" in le.lower() or "root" in le.lower() or "privilege" in le.lower():
                    data["hint"] = "Administrator/root privileges required. Run: python -m telnix.cli system restart-as-admin"
        emit_obj(data)
    elif args.action == "start":
        # On first enable with an unacknowledged WinDivert risk prompt (Windows only), automatically trigger a top-most native desktop popup
        body: dict = {}
        if args.rules:
            # Parse the "domain=ip,domain=ip,..." format
            try:
                rules_dict: dict = {}
                for pair in args.rules.split(","):
                    pair = pair.strip()
                    if not pair or "=" not in pair:
                        continue
                    k, v = pair.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k and v:
                        rules_dict[k] = v
                body["rules"] = rules_dict
            except Exception as e:  # noqa: BLE001
                _die_arg(f"Invalid --rules format (expected domain=ip,domain=ip): {e}")
        if args.default_ip:
            body["default_ip"] = args.default_ip.strip()
        res = _req_with_windivert_ack("POST", "/dns-hijack/start", body=body or None)
        data = _ok(res)
        emit_obj(data)
    elif args.action == "stop":
        res = _req("POST", "/dns-hijack/stop")
        data = _ok(res)
        emit_obj(data)
    elif args.action == "rules":
        if args.set_rules:
            # Update rules
            try:
                rules_dict = {}
                for pair in args.set_rules.split(","):
                    pair = pair.strip()
                    if not pair or "=" not in pair:
                        continue
                    k, v = pair.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k and v:
                        rules_dict[k] = v
                body = {"rules": rules_dict, "default_ip": args.default_ip.strip() if args.default_ip else ""}
            except Exception as e:  # noqa: BLE001
                _die_arg(f"Invalid --set format (expected domain=ip,domain=ip): {e}")
            res = _req("PUT", "/dns-hijack/rules", body=body)
            data = _ok(res)
            emit_obj(data)
        else:
            # View current rules only
            res = _req("GET", "/dns-hijack/status")
            data = _ok(res)
            if isinstance(data, dict):
                emit_obj({
                    "rules": data.get("rules", {}),
                    "default_ip": data.get("default_ip", ""),
                })
            else:
                emit_obj(data)
    elif args.action == "clear-log":
        res = _req("POST", "/dns-hijack/clear-log")
        data = _ok(res)
        emit_obj(data)
    else:
        _die_arg("dns-hijack requires: status | start | stop | rules | clear-log")


def cmd_auto_reply(args):
    """Auto-reply rule management (list/get/create/enable/disable/delete).

    Complementary to the intercept command: auto-reply create focuses on Python script rules,
    and supports --script-path to load script content from a local .py file, which is convenient for the agent.
    """
    sub = getattr(args, "sub", "")
    if sub == "list":
        res = _req("GET", "/auto-reply/rules")
        rules = _ok(res)
        rules = rules if isinstance(rules, list) else []
        for r in rules:
            if isinstance(r, dict) and "rule_id" not in r:
                r["rule_id"] = r.get("id")
        emit_list(rules, json_array=getattr(args, "json_array", False))
    elif sub == "get":
        res = _req("GET", "/auto-reply/rules")
        rules = _ok(res)
        rules = rules if isinstance(rules, list) else []
        target = None
        for r in rules:
            if isinstance(r, dict) and str(r.get("id")) == str(args.id):
                target = r
                break
        if not target:
            _die_arg(f"Rule not found: {args.id}")
        emit_obj(target)
    elif sub == "create":
        if not args.pattern:
            _die_arg("auto-reply create requires --pattern")
        if not args.action_type:
            _die_arg("auto-reply create requires --action")
        body: dict = {
            "enabled": not getattr(args, "disabled", False),
            "match_mode": args.match_mode,
            "pattern": args.pattern,
            "action": args.action_type,
            "note": args.note or "",
            "method_filter": args.method_filter or "",
            "status_filter": args.status_filter or "",
            "pid_filter": args.pid_filter or "",
            "process_filter": args.process_filter or "",
        }
        if args.action_type == "script":
            # script action: modify_rules is the Python script source (str)
            if args.script_path:
                if not os.path.isfile(args.script_path):
                    _die_arg(f"Script file not found: {args.script_path}")
                try:
                    with open(args.script_path, "r", encoding="utf-8") as f:
                        source = f.read()
                except OSError as e:
                    _die_arg(f"Failed to read script file: {e}")
                body["modify_rules"] = source
            elif args.script:
                body["modify_rules"] = args.script
            else:
                _die_arg("action=script requires --script-path or --script")
            body["mock_status"] = None
            body["mock_headers"] = {}
            body["mock_body"] = ""
        else:
            # Non-script actions: parse action_spec with parse_action
            if not args.action_spec:
                _die_arg(f"action={args.action_type} requires --action-spec (e.g. 'set-json key value')")
            try:
                a = parse_action(args.action_spec)
            except ValueError as e:
                _die_arg(str(e))
            if a.get("action"):
                body["action"] = a["action"]
            body["mock_status"] = a.get("mock_status")
            body["mock_headers"] = a.get("mock_headers")
            body["mock_body"] = a.get("mock_body", "")
            body["modify_rules"] = a.get("modify_rules", [])
        res = _req("POST", "/auto-reply/rules", body)
        created = _ok(res)
        emit_obj({"created": True,
                  "rule_id": created.get("id") if isinstance(created, dict) else None,
                  "pattern": args.pattern, "action": body["action"]})
    elif sub == "enable":
        res = _req("PUT", f"/auto-reply/rules/{args.id}", {"enabled": True})
        _ok(res)
        emit_obj({"rule_id": args.id, "enabled": True})
    elif sub == "disable":
        res = _req("PUT", f"/auto-reply/rules/{args.id}", {"enabled": False})
        _ok(res)
        emit_obj({"rule_id": args.id, "enabled": False})
    elif sub == "delete":
        res = _req("DELETE", f"/auto-reply/rules/{args.id}")
        _ok(res)
        emit_obj({"deleted": True, "rule_id": args.id})
    elif sub == "test-script":
        # Test a Python script (does not create a rule; runs the worker subprocess with mock data)
        # The agent can use this command to validate script logic before creating a rule
        if args.script_path:
            if not os.path.isfile(args.script_path):
                _die_arg(f"Script file not found: {args.script_path}")
            try:
                with open(args.script_path, "r", encoding="utf-8") as f:
                    script_source = f.read()
            except OSError as e:
                _die_arg(f"Failed to read script file: {e}")
        elif args.script:
            script_source = args.script
        else:
            _die_arg("test-script requires --script-path or --script")
            return
        # Parse mock headers JSON
        try:
            mock_headers = json.loads(args.mock_headers) if args.mock_headers else {}
        except json.JSONDecodeError as e:
            _die_arg(f"Failed to parse --mock-headers JSON: {e}")
            return
        body = {
            "script": script_source,
            "mock_request": {
                "host": args.mock_host,
                "path": args.mock_path,
                "method": args.mock_method,
                "scheme": args.mock_scheme,
                "http_version": args.mock_http_version,
                "headers": mock_headers,
                "body": args.mock_body or "",
            },
        }
        # Optional: mock_response (if provided, on_response is also called)
        if args.mock_resp_status is not None:
            try:
                resp_headers = json.loads(args.mock_resp_headers) if args.mock_resp_headers else {}
            except json.JSONDecodeError as e:
                _die_arg(f"Failed to parse --mock-resp-headers JSON: {e}")
                return
            body["mock_response"] = {
                "status_code": int(args.mock_resp_status),
                "headers": resp_headers,
                "body": args.mock_resp_body or "",
            }
        res = _req("POST", "/auto-reply/test-script", body, timeout=15.0)
        emit_obj(_ok(res))
    else:
        _die_arg(f"Unknown auto-reply action: {sub}")


def cmd_log_tail(args):
    params = f"?limit={args.limit or 100}&level={args.level or ''}&category={args.category or ''}"
    res = _req("GET", f"/logs{params}")
    data = _ok(res)
    logs = data if isinstance(data, list) else (data.get("items") or data.get("logs") or data.get("entries") or [])
    for lg in logs:
        print(json.dumps(lg, ensure_ascii=False))


def cmd_log_clear(args):
    res = _req("DELETE", "/logs")
    _ok(res)
    emit_obj({"cleared": True})


def cmd_log_export(args):
    params = []
    if args.level:
        params.append(f"level={urllib.parse.quote(args.level)}")
    if args.category:
        params.append(f"category={urllib.parse.quote(args.category)}")
    if args.keyword:
        params.append(f"keyword={urllib.parse.quote(args.keyword)}")
    qs = "&".join(params)
    url = "/logs/export" + (f"?{qs}" if qs else "")
    # /logs/export returns a PlainTextResponse (JSONL); _req's json.loads would fail
    # Use httpx directly to get the raw text
    full_url = f"{BASE_URL}/api{url}"
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0), trust_env=False) as client:
            resp = client.get(full_url, headers={"Accept": "application/x-jsonlines"})
            content = resp.text
    except Exception as e:  # noqa: BLE001
        _die_conn(f"Failed to export logs: {e}")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(content)
        emit_obj({"exported": True, "path": args.output, "size": len(content)})
    else:
        print(content, end="")


def cmd_focus(args):
    if args.action == "status":
        res = _req("GET", "/focus")
        emit_obj(_ok(res))
    elif args.action == "on":
        body = {"enabled": True, "pids": [], "process_names": [], "include_children": not args.no_children}
        if args.pid:
            body["pids"] = [int(p) for p in args.pid.split(",") if p.strip()]
        if args.name:
            body["process_names"] = [n.strip() for n in args.name.split(",") if n.strip()]
        if args.host:
            # Host wildcard list (* -> .*, ? -> .), cross-category OR match: records if pid/host matches either
            body["hosts"] = [h.strip() for h in args.host.split(",") if h.strip()]
        if not body["pids"] and not body["process_names"] and not body.get("hosts"):
            _die_arg("focus on requires --pid or --name or --host")
        res = _req("POST", "/focus", body)
        emit_obj(_ok(res))
    elif args.action == "off":
        res = _req("POST", "/focus", {"enabled": False, "pids": []})
        emit_obj(_ok(res))
    else:
        _die_arg("focus requires: status | on | off")


def cmd_breakpoint(args):
    """Breakpoint control: status / on / off / timeout / release / drop.

    Timeout mechanism: set --timeout N when enabling a breakpoint; if not released within N seconds, it auto-releases,
    to avoid permanent connection blocking when the agent forgets to release.
    """
    if args.action == "status":
        res = _req("GET", "/breakpoint/status")
        emit_obj(_ok(res))
    elif args.action == "on":
        bp_type = args.type or "request"
        body = {"enabled": True}
        if args.timeout is not None and args.timeout > 0:
            body["timeout"] = args.timeout
        path = f"/breakpoint/{bp_type}"
        res = _req("POST", path, body)
        data = _ok(res)
        out = {"break_on_" + bp_type: True}
        if args.timeout is not None and args.timeout > 0:
            out["timeout_seconds"] = args.timeout
            out["hint"] = f"Breakpoint {bp_type} enabled; auto-releases after {args.timeout}s without release"
        out.update(data)
        emit_obj(out)
    elif args.action == "off":
        bp_type = args.type or "request"
        res = _req("POST", f"/breakpoint/{bp_type}", {"enabled": False})
        emit_obj(_ok(res))
    elif args.action == "timeout":
        if args.timeout is None:
            _die_arg("breakpoint timeout requires --timeout N")
        res = _req("POST", "/breakpoint/timeout", {"timeout": args.timeout})
        emit_obj(_ok(res))
    elif args.action in ("release", "drop"):
        action = "release" if args.action == "release" else "drop"
        if getattr(args, "all", False):
            # Batch release/drop all pending breakpoints
            res = _req("GET", "/breakpoint/status")
            data = _ok(res)
            pending = data.get("pending", []) if isinstance(data, dict) else []
            ids = []
            for p in pending:
                if isinstance(p, dict):
                    fid = p.get("flow_id")
                else:
                    fid = p
                if fid:
                    ids.append(int(fid))
            if not ids:
                emit_obj({"released": 0, "total": 0, "hint": "No pending breakpoints"})
                return
            res = _req("POST", "/flows/batch-release", {"ids": ids, "action": action})
            data = _ok(res)
            emit_obj({"action": action, "total": len(ids),
                      "released": data.get("released", 0) if isinstance(data, dict) else 0})
        elif args.id:
            res = _req("POST", f"/flows/{args.id}/release", {"action": action})
            emit_obj(_ok(res))
        else:
            _die_arg(f"breakpoint {action} requires <flow_id> or --all")
    else:
        _die_arg("breakpoint requires: status | on | off | timeout | release | drop")


def cmd_cookies(args):
    """Cookie management: list / clear-host / clear-all (aggregated from Cookie/Set-Cookie headers in flows)."""
    action = getattr(args, "action", None)
    if action == "list":
        qs = ""
        if getattr(args, "host", ""):
            qs = f"?host={urllib.parse.quote(args.host)}"
        res = _req("GET", f"/cookies{qs}")
        data = _ok(res)
        if getattr(args, "json", False):
            emit_obj(data)
            return
        hosts = data.get("hosts", []) if isinstance(data, dict) else []
        if isinstance(data, dict):
            print(f"total_hosts={data.get('total_hosts', 0)} total_cookies={data.get('total_cookies', 0)}")
        for host_entry in hosts:
            host = host_entry.get("host", "") if isinstance(host_entry, dict) else ""
            cookies = (host_entry.get("cookies", []) if isinstance(host_entry, dict) else []) or []
            print(f"[{host}] ({len(cookies)} cookies)")
            for c in cookies:
                if not isinstance(c, dict):
                    continue
                name = c.get("name", "")
                value = c.get("value", "")
                attrs = []
                if c.get("domain"):
                    attrs.append(f"domain={c['domain']}")
                if c.get("path"):
                    attrs.append(f"path={c['path']}")
                if c.get("secure"):
                    attrs.append("secure")
                if c.get("httponly"):
                    attrs.append("httponly")
                if c.get("samesite"):
                    attrs.append(f"samesite={c['samesite']}")
                if c.get("expires"):
                    attrs.append(f"expires={c['expires']}")
                print(f"  {name}={value}  {' '.join(attrs)}")
    elif action == "clear-host":
        host = args.host
        res = _req("DELETE", f"/cookies/{urllib.parse.quote(host)}")
        data = _ok(res)
        emit_obj(data if isinstance(data, dict) else {"cleared": True, "host": host})
        if not getattr(args, "json", False):
            print(f"[cookies] cleared host: {host}", file=sys.stderr)
    elif action == "clear-all":
        res = _req("DELETE", "/cookies")
        data = _ok(res)
        emit_obj(data if isinstance(data, dict) else {"cleared": True})
        if not getattr(args, "json", False):
            print("[cookies] cleared all cookies", file=sys.stderr)
    else:
        _die_arg("cookies requires: list | clear-host | clear-all")


def cmd_site_map(args):
    """Site map tree (aggregated from flows)."""
    res = _req("GET", "/site-map")
    data = _ok(res)
    if getattr(args, "json", False):
        emit_obj(data)
        return
    tree = data if isinstance(data, list) else (data.get("tree") or data.get("hosts") or [])
    host_filter = getattr(args, "host", "") or ""

    def print_node(node, depth, is_host=False):
        indent = "  " * depth
        if is_host or depth == 0:
            label = node.get("host", "") if isinstance(node, dict) else ""
            print(f"{indent}{label}")
        else:
            path = node.get("path", "") if isinstance(node, dict) else ""
            method = node.get("method", "") if isinstance(node, dict) else ""
            status = node.get("status_code", "") if isinstance(node, dict) else ""
            leaf_info = ""
            if method or status:
                leaf_info = f"  [{method} {status}]"
            print(f"{indent}{path}{leaf_info}")
        children = (node.get("children", []) if isinstance(node, dict) else []) or []
        for child in children:
            print_node(child, depth + 1)

    for root in tree:
        if not isinstance(root, dict):
            continue
        if host_filter and root.get("host", "") != host_filter:
            continue
        print_node(root, 0, is_host=True)


def cmd_record_replay(args):
    """Record/replay management: list / create / show / delete / replay / start-record / stop-record / status."""
    action = getattr(args, "action", None)
    if action == "list":
        res = _req("GET", "/record-scripts")
        data = _ok(res)
        if getattr(args, "json", False):
            emit_obj(data)
            return
        items = data if isinstance(data, list) else (data.get("items") or data.get("scripts") or [])
        emit_list(items)
    elif action == "create":
        name = args.name
        flow_ids = []
        for part in str(args.flow_ids or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                flow_ids.append(int(part))
            except ValueError:
                _die_arg(f"Invalid flow id: {part}")
        body: dict = {"name": name, "flow_ids": flow_ids}
        if getattr(args, "note", ""):
            body["note"] = args.note
        res = _req("POST", "/record-scripts", body)
        data = _ok(res)
        emit_obj(data)
    elif action == "show":
        res = _req("GET", f"/record-scripts/{args.script_id}")
        data = _ok(res)
        emit_obj(data)
    elif action == "delete":
        res = _req("DELETE", f"/record-scripts/{args.script_id}")
        data = _ok(res)
        emit_obj(data if isinstance(data, dict) else {"deleted": True, "script_id": args.script_id})
    elif action == "replay":
        res = _req("POST", f"/record-scripts/{args.script_id}/replay")
        data = _ok(res)
        if getattr(args, "json", False):
            emit_obj(data)
            return
        if isinstance(data, dict):
            total = data.get("total", 0)
            replayed = data.get("replayed", 0)
            success = data.get("success", 0)
            fail = data.get("fail", 0)
            dist = data.get("status_distribution", {}) or {}
            print(f"replayed: {replayed}/{total}  success={success}  fail={fail}")
            if dist:
                print("status_distribution:")
                for code, cnt in dist.items():
                    print(f"  {code}: {cnt}")
        emit_obj(data)
    elif action == "start-record":
        res = _req("POST", "/record-scripts/start-recording")
        data = _ok(res)
        emit_obj(data)
    elif action == "stop-record":
        body: dict = {}
        if getattr(args, "name", ""):
            body["name"] = args.name
        if getattr(args, "note", ""):
            body["note"] = args.note
        res = _req("POST", "/record-scripts/stop-recording", body if body else None)
        data = _ok(res)
        emit_obj(data)
    elif action == "status":
        res = _req("GET", "/record-scripts/recording-status")
        data = _ok(res)
        emit_obj(data)
    else:
        _die_arg("record-replay requires: list | create | show | delete | replay | start-record | stop-record | status")


# ---------- argparse ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="telnix-cli",
        description="Telnix Agent CLI - capture/interception control tool for AI agents",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # status
    sub.add_parser("status", aliases=["st"], help="backend status").set_defaults(func=cmd_status)

    # capture
    sp = sub.add_parser("capture", aliases=["cap"], help="capture control")
    sp_sub = sp.add_subparsers(dest="sub", required=True)
    sp_start = sp_sub.add_parser("start", aliases=["st"], help="start capture")
    sp_start.add_argument("-f", "--filter", default="", help="capture filter (not yet supported; recorded only)")
    sp_start.add_argument("--max-duration", type=int, default=0, help="suggested max capture duration in seconds (agent safeguard hint)")
    sp_start.add_argument("--auto-stop", type=int, default=0, help="actually auto-stop capture after N seconds (background thread timed stop; agent doesn't need to sleep+stop)")
    sp_start.add_argument("-L", "--layer", choices=["http", "tcp", "all"], default="http",
                          help="capture layer: http=HTTP proxy only (default), tcp=TCP/UDP only (cross-platform network-layer capture), all=both")
    sp_start.add_argument("--pid", default="", help="TCP/UDP mode: filter by PID, comma-separated")
    sp_start.add_argument("-p", "--port", default="", help="TCP/UDP mode: filter by port, comma-separated")
    sp_start.add_argument("--bpf", default="", help="TCP/UDP mode: capture filter string (Windows: WinDivert filter; Unix: port filtering via --port)")
    sp_start.set_defaults(func=cmd_capture_start)
    sp_stop = sp_sub.add_parser("stop", aliases=["sp"], help="stop capture")
    sp_stop.add_argument("-L", "--layer", choices=["http", "tcp", "all"], default="http",
                         help="which layer to stop: default http; all stops TCP/UDP as well")
    sp_stop.set_defaults(func=cmd_capture_stop)
    sp_sub.add_parser("clear", aliases=["clr"], help="clear current session traffic").set_defaults(func=cmd_capture_clear)
    sp_sub.add_parser("pause", aliases=["pz"], help="pause capture (session retained; proxy still running)").set_defaults(func=cmd_capture_pause)
    sp_sub.add_parser("resume", aliases=["rs"], help="resume capture recording").set_defaults(func=cmd_capture_resume)

    # packets
    sp = sub.add_parser("packets", aliases=["pkts"], help="traffic query")
    sp_sub = sp.add_subparsers(dest="sub", required=True)
    sp_list = sp_sub.add_parser("list", aliases=["ls"], help="traffic list (NDJSON)")
    sp_list.add_argument("-s", "--session", type=int, default=0, help="session ID (default: current active session)")
    sp_list.add_argument("-n", "--limit", type=int, default=100, help="max number of entries to return")
    sp_list.add_argument("--since-id", type=int, default=None, help="incremental query: only return flows with id > N (non-blocking)")
    sp_list.add_argument("--tail", action="store_true", help="streaming tail (blocking; Ctrl+C to exit)")
    sp_list.add_argument("-f", "--filter", default="", help="filter expression: key op value && ...")
    sp_list.add_argument("--filter-host", default="", help="shortcut: filter by host")
    sp_list.add_argument("--filter-status", default="", help="shortcut: filter by status code")
    sp_list.add_argument("--filter-method", default="", help="shortcut: filter by method")
    sp_list.add_argument("-P", "--protocol", default="", help="protocol filter: http|tcp|udp|ws|dns")
    sp_list.add_argument("-c", "--emit-curl", action="store_true", help="attach a replayable curl command to each flow")
    sp_list.add_argument("-J", "--json-array", action="store_true", help="output a JSON array instead of NDJSON")
    sp_list.add_argument("-d", "--decode", default="", help="batch-load decoder plugins; each flow outputs a decoded field (see §3.3 decoder plugins)")
    sp_list.add_argument("--decode-field", default="response_body",
                         help="decoder target field: request_body|response_body (default response_body)")
    sp_list.add_argument("-T", "--tag", default="", help="filter by tag (only return flows with the specified tag)")
    sp_list.add_argument("--has-tags", action="store_true", help="only return flows that have tags")
    sp_list.set_defaults(func=cmd_packets_list)

    sp_get = sp_sub.add_parser("get", help="traffic details")
    sp_get.add_argument("id", type=int, help="traffic ID")
    sp_get.add_argument("-c", "--emit-curl", action="store_true", help="attach a curl command")
    sp_get.add_argument("--hex", action="store_true", help="output as hex dump")
    sp_get.add_argument("--field", default="response_body", help="hex mode: request_body|response_body|raw_data")
    sp_get.add_argument("--offset", type=int, default=0, help="hex mode: start offset")
    sp_get.add_argument("--length", type=int, default=0, help="hex mode: length (0=all)")
    sp_get.add_argument("-d", "--decode", default="", help="load a Python decoder plugin to decode the body (see §3.3 decoder plugins)")
    sp_get.add_argument("--decode-field", default="response_body",
                        help="decoder target field: request_body|response_body (default response_body)")
    sp_get.set_defaults(func=cmd_packets_get)

    sp_del = sp_sub.add_parser("delete", aliases=["del"], help="delete traffic")
    sp_del.add_argument("id", type=int, nargs="?", default=0, help="traffic ID")
    sp_del.add_argument("--ids", default="", help="batch delete, comma-separated")
    sp_del.set_defaults(func=cmd_packets_delete)

    sp_search = sp_sub.add_parser("search", aliases=["find"], help="search traffic across bodies (current session)")
    sp_search.add_argument("--body-regex", default="", help="regex (matches request_body/response_body/url/path)")
    sp_search.add_argument("--binary-hex", default="", help="binary content search (hex string)")
    sp_search.add_argument("--header-regex", default="",
                           help="request/response header regex match (AND with body-regex)")
    sp_search.add_argument("-X", "--method", default="", help="exact HTTP method match (case-insensitive)")
    sp_search.add_argument("--status", type=int, default=None, help="exact status code match")
    sp_search.add_argument("--pid", type=int, default=None, help="exact process PID match")
    sp_search.add_argument("--process", default="", help="exact process name match")
    sp_search.add_argument("-n", "--limit", type=int, default=200, help="max number of entries to return")
    sp_search.add_argument("-J", "--json-array", action="store_true", help="output a JSON array")
    sp_search.add_argument("-a", "--all", action="store_true", help="search across all sessions (session_id=0)")
    sp_search.add_argument("--offset", default="", help="binary search byte range START:END (e.g. 0:1024 searches only the first 1KB)")
    sp_search.set_defaults(func=cmd_packets_search)

    sp_list_all = sp_sub.add_parser("list-all", aliases=["all"], help="query all traffic across sessions (NDJSON)")
    sp_list_all.add_argument("-n", "--limit", type=int, default=100, help="max number of entries to return")
    sp_list_all.add_argument("--offset", type=int, default=0, help="pagination offset")
    sp_list_all.add_argument("--since-id", type=int, default=None, help="incremental query: only return flows with id > N")
    sp_list_all.add_argument("-H", "--host", default="", help="filter by host")
    sp_list_all.add_argument("--process", default="", help="filter by process name")
    sp_list_all.add_argument("--status", type=int, default=0, help="filter by status code")
    sp_list_all.add_argument("-X", "--method", default="", help="filter by method")
    sp_list_all.add_argument("-P", "--protocol", default="", help="filter by protocol: http|tcp|udp|ws|dns")
    sp_list_all.add_argument("-f", "--filter", default="", help="client-side expression filter: key op value && ...")
    sp_list_all.add_argument("--filter-path", default="", help="filter by path (backend SQL LIKE)")
    sp_list_all.add_argument("--filter-url", default="", help="filter by url (backend SQL LIKE)")
    sp_list_all.add_argument("-c", "--emit-curl", action="store_true", help="attach a curl command to each flow")
    sp_list_all.add_argument("-J", "--json-array", action="store_true", help="output a JSON array")
    sp_list_all.add_argument("-d", "--decode", default="", help="batch-load decoder plugins; each flow outputs a decoded field")
    sp_list_all.add_argument("--decode-field", default="response_body",
                             help="decoder target field: request_body|response_body (default response_body)")
    sp_list_all.add_argument("-T", "--tag", default="", help="filter by tag (only return flows with the specified tag)")
    sp_list_all.add_argument("--has-tags", action="store_true", help="only return flows that have tags")
    sp_list_all.set_defaults(func=cmd_packets_list_all)

    sp_clear = sp_sub.add_parser("clear", aliases=["clr"], help="clear traffic across sessions")
    sp_clear.add_argument("-a", "--all", action="store_true", help="clear all historical traffic")
    sp_clear.add_argument("--before-id", type=int, default=0, help="delete old flows with id < N")
    sp_clear.set_defaults(func=cmd_packets_clear)

    sp_export = sp_sub.add_parser("export", aliases=["exp"], help="single flow export (curl/python-requests/postman/json/csv)")
    sp_export.add_argument("id", type=int, help="traffic ID")
    sp_export.add_argument("-F", "--format", choices=["curl", "python-requests", "postman", "json", "csv"],
                           default="curl", help="export format")
    sp_export.add_argument("-o", "--output", default="", help="output file path (stdout if not specified)")
    sp_export.set_defaults(func=cmd_packets_export)

    # §3.1 Traffic tag subcommand
    sp_tag = sp_sub.add_parser("tag", help="traffic tag management: --add/--remove/--clear/--note/--clear-note/--list")
    sp_tag.add_argument("id", type=int, nargs="?", default=0, help="traffic ID (can be omitted with --list)")
    sp_tag.add_argument("--add", default="", help="add a tag (append to the comma-separated tags list)")
    sp_tag.add_argument("--remove", default="", help="remove a tag")
    sp_tag.add_argument("--clear", action="store_true", help="clear all tags")
    sp_tag.add_argument("--note", default=None, help="set tag note (tag_note)")
    sp_tag.add_argument("--clear-note", action="store_true", help="clear tag note (explicitly set tag_note to null)")
    sp_tag.add_argument("--list", action="store_true", help="list all global tags and the flow count per tag")
    sp_tag.set_defaults(func=cmd_packets_tag)

    sp_stats = sp_sub.add_parser("stats", help="traffic group statistics")
    sp_stats.add_argument("-s", "--session", type=int, default=0, help="session ID (default: current active session)")
    sp_stats.add_argument("--by", choices=["host", "method", "status", "protocol", "endpoint",
                                           "content_type", "process"], default="",
                          help="group by dimension (default: all). endpoint does path template normalization; content_type/process goes through backend /flows/stats")
    sp_stats.add_argument("--metrics", default="", help="extra metrics: size,duration (e.g. --metrics size,duration outputs p50/p95/max)")
    sp_stats.add_argument("-n", "--limit", type=int, default=2000, help="upper limit on flows to analyze (0=unlimited; actually 50000)")
    sp_stats.add_argument("--keep-query", action="store_true", help="endpoint normalization keeps query parameter names")
    sp_stats.set_defaults(func=cmd_packets_stats)

    sp_overview = sp_sub.add_parser("overview", aliases=["ov"], help="multi-dimensional aggregate stats overview (CoolUI dashboard data source; cross-session full)")
    sp_overview.set_defaults(func=cmd_packets_overview)

    sp_diff = sp_sub.add_parser("diff", help="compare request/response fields of two flows (unified diff)")
    sp_diff.add_argument("id1", type=int, help="first flow ID")
    sp_diff.add_argument("id2", type=int, help="second flow ID")
    sp_diff.add_argument("--field", default="response_body",
                         help="field to compare: request_body|response_body|request_headers|response_headers|url")
    sp_diff.set_defaults(func=cmd_packets_diff)

    sp_endpoints = sp_sub.add_parser("endpoints", aliases=["eps"], help="unique endpoint extraction (path template normalization; draw an API map)")
    sp_endpoints.add_argument("-H", "--host", default="", help="filter by host")
    sp_endpoints.add_argument("-s", "--session", type=int, default=0, help="only a specific session (default: across all sessions)")
    sp_endpoints.add_argument("-n", "--limit", type=int, default=2000, help="upper limit on flows to analyze (0=unlimited; actually 50000)")
    sp_endpoints.add_argument("--keep-query", action="store_true", help="keep query parameter names (dropped by default)")
    sp_endpoints.add_argument("--sample-strategy", choices=["first", "last", "random"], default="first",
                              help="sample_ids strategy: first=first 3 (default), last=last 3, random=random 3")
    sp_endpoints.add_argument("-J", "--json-array", action="store_true", help="output a JSON array")
    sp_endpoints.set_defaults(func=cmd_packets_endpoints)

    sp_timeline = sp_sub.add_parser("timeline", aliases=["tl"], help="traffic timeline (sorted by time; marks large gaps)")
    sp_timeline.add_argument("-H", "--host", default="", help="filter by host")
    sp_timeline.add_argument("-s", "--session", type=int, default=0, help="only a specific session (default: across all sessions)")
    sp_timeline.add_argument("--gap", type=float, default=1.0, help="gaps longer than N seconds are marked as section separators")
    sp_timeline.add_argument("-n", "--limit", type=int, default=2000, help="upper limit on flows to analyze (0=unlimited; actually 50000)")
    sp_timeline.set_defaults(func=cmd_packets_timeline)

    sp_watch = sp_sub.add_parser("watch", aliases=["w"], help="directed tail: only output matching new flows (blocking; Ctrl+C to exit)")
    sp_watch.add_argument("-f", "--filter", required=True, help="filter expression: host~=api.x.com && method=POST")
    sp_watch.add_argument("-s", "--session", type=int, default=0, help="session ID (default: current active session)")
    sp_watch.add_argument("-i", "--interval", type=float, default=1.0, help="polling interval (seconds)")
    sp_watch.set_defaults(func=cmd_packets_watch)

    sp_trace = sp_sub.add_parser("trace", aliases=["tr"], help="request dependency chain trace: extract string values from a response and search for them in subsequent request flows")
    sp_trace.add_argument("id", type=int, help="source flow ID")
    sp_trace.add_argument("-n", "--limit", type=int, default=500, help="upper limit on subsequent flows to scan")
    sp_trace.add_argument("-a", "--all", action="store_true", help="cross-session scan (/flows/all); default scans only the current session")
    sp_trace.add_argument("-s", "--session", type=int, default=0, help="session ID (default: current active session; only effective when --all is not specified)")
    sp_trace.add_argument("--min-length", type=int, default=4, help="only trace string values with length >= N (reduces short-string false positives; default 4)")
    sp_trace.set_defaults(func=cmd_packets_trace)

    sp_analyze = sp_sub.add_parser("analyze", aliases=["az"], help="signature field auto-detection (multi-flow comparison; finds suspicious signature/token fields)")
    sp_analyze.add_argument("ids", type=int, nargs="*", help="flow ID list (at least 2 in normal mode; at least 1 as source in --all mode)")
    sp_analyze.add_argument("--find-signature", action="store_true",
                            help="run signature field detection (enabled by default; flag for explicit declaration)")
    sp_analyze.add_argument("-a", "--all", action="store_true", help="cross-session scan: use the first ID as source and pull subsequent flows from /flows/all")
    sp_analyze.add_argument("-n", "--limit", type=int, default=500, help="upper limit on subsequent flows to scan in --all mode")
    sp_analyze.set_defaults(func=cmd_packets_analyze)

    # intercept
    sp = sub.add_parser("intercept", aliases=["itcp"], help="intercept/modify rules")
    sp_sub = sp.add_subparsers(dest="sub", required=True)
    sp_add = sp_sub.add_parser("add", help="add an intercept rule")
    sp_add.add_argument("--match", required=True, help="match expression: host~=x && method=POST && path~=/api/*")
    sp_add.add_argument("--action", required=True,
                        help="action: set-json k v | set-request-header K V | mock CODE BODY | replace-bytes off:hex | ...")
    sp_add.add_argument("-N", "--name", default="", help="rule name (written to note)")
    sp_add.add_argument("--note", default="", help="note")
    sp_add.add_argument("-s", "--session", type=int, default=0, help="session ID (for dry-run)")
    sp_add.add_argument("--dry-run", action="store_true", help="preview: list flows that would match; do not create the rule")
    sp_add.add_argument("--idempotent", action="store_true",
                        help="idempotent create: if an identical pattern+action+modify_rules rule already exists, do not create again; return the existing rule_id")
    sp_add.set_defaults(func=cmd_intercept_add)

    sp_list = sp_sub.add_parser("list", aliases=["ls"], help="rule list (NDJSON; includes hit_count stats)")
    sp_list.add_argument("-J", "--json-array", action="store_true", help="output a JSON array")
    sp_list.add_argument("--with-stats", action="store_true",
                         help="semantic flag: explicitly request hit stats (hit_count/last_hit_at/last_hit_flow_id are output by default)")
    sp_list.set_defaults(func=cmd_intercept_list)

    sp_hits = sp_sub.add_parser("hits", help="show a rule's hit stats + last-hit flow details (§4.1)")
    sp_hits.add_argument("id", help="rule ID")
    sp_hits.set_defaults(func=cmd_intercept_hits)

    sp_del = sp_sub.add_parser("del", help="delete a rule")
    sp_del.add_argument("id", nargs="?", default="", help="rule ID")
    sp_del.add_argument("--ids", default="", help="batch delete, comma-separated")
    sp_del.set_defaults(func=cmd_intercept_del)

    sp_toggle = sp_sub.add_parser("toggle", aliases=["tgl"], help="enable/disable a rule (does not delete)")
    sp_toggle.add_argument("id", nargs="?", default="", help="rule ID (toggle a single rule)")
    sp_toggle.add_argument("-a", "--all", action="store_true", help="batch operate on all rules")
    sp_toggle.add_argument("--enable", action="store_true", help="force enable")
    sp_toggle.add_argument("--disable", action="store_true", help="force disable")
    sp_toggle.set_defaults(func=cmd_intercept_toggle)

    sp_update = sp_sub.add_parser("update", aliases=["upd"], help="modify an existing rule (without delete+recreate)")
    sp_update.add_argument("id", help="rule ID")
    sp_update.add_argument("--match", default="", help="new match expression: host~=x && method=POST")
    sp_update.add_argument("--action", default="", help="new action: set-json k v | mock CODE BODY | ...")
    sp_update.add_argument("--note", default="", help="new note")
    sp_update.add_argument("--enable", action="store_true", help="enable at the same time")
    sp_update.add_argument("--disable", action="store_true", help="disable at the same time")
    sp_update.set_defaults(func=cmd_intercept_update)

    sp_export_rules = sp_sub.add_parser("export", aliases=["exp"], help="export all rules to a JSON file")
    sp_export_rules.add_argument("-o", "--output", default="telnix_rules.json", help="output file path")
    sp_export_rules.set_defaults(func=cmd_intercept_export)

    sp_import_rules = sp_sub.add_parser("import", aliases=["imp"], help="import rules from a JSON file")
    sp_import_rules.add_argument("file", help="rules JSON file path")
    sp_import_rules.add_argument("-m", "--mode", choices=["merge", "replace"], default="merge",
                                 help="merge=append (default), replace=clear first then import")
    sp_import_rules.add_argument("--quiet", action="store_true",
                                 help="only output the summary, not the results array (concise output for large batch imports)")
    sp_import_rules.set_defaults(func=cmd_intercept_import)

    # §3.17 Rule template library CLI wrapper
    sp_tpl = sp_sub.add_parser("template", aliases=["tpl"], help="rule template library (list/apply)")
    sp_tpl_sub = sp_tpl.add_subparsers(dest="action")
    sp_tpl_list = sp_tpl_sub.add_parser("list", help="list all built-in templates")
    sp_tpl_list.add_argument("-J", "--json-array", action="store_true", help="output a JSON array")
    sp_tpl_list.set_defaults(action="list")
    sp_tpl_apply = sp_tpl_sub.add_parser("apply", help="apply a template to create a rule")
    sp_tpl_apply.add_argument("name", help="template name (e.g. mock-404/unlock-vip)")
    sp_tpl_apply.add_argument("--match", required=True, help="URL match pattern (required)")
    sp_tpl_apply.add_argument("--match-mode", choices=["wildcard", "exact", "regex"],
                              default="wildcard", help="match mode (default wildcard)")
    sp_tpl_apply.add_argument("--note", default="", help="rule note")
    sp_tpl_apply.add_argument("--disabled", action="store_true", help="create in disabled state")
    sp_tpl_apply.add_argument("--method-filter", default="", help="method filter (comma-separated)")
    sp_tpl_apply.add_argument("--status-filter", default="", help="status code filter (comma-separated)")
    sp_tpl_apply.add_argument("--pid-filter", default="", help="PID filter")
    sp_tpl_apply.add_argument("--process-filter", default="", help="process name filter")
    sp_tpl_apply.set_defaults(action="apply")
    sp_tpl.set_defaults(func=cmd_intercept_template)

    # replay
    sp = sub.add_parser("replay", aliases=["rep"], help="replay traffic")
    sp.add_argument("id", type=int, help="traffic ID")
    sp.add_argument("-b", "--body", default="", help="override request body")
    sp.add_argument("-u", "--url", default="", help="override URL")
    sp.add_argument("-H", "--host", default="", help="redirect to another host")
    sp.add_argument("-p", "--port", type=int, default=0, help="redirect port")
    sp.add_argument("-X", "--method", default="", help="override HTTP method")
    sp.add_argument("--header", action="append", help="override/add request header, format K:V (can be used multiple times)")
    sp.add_argument("--fuzz", default="", help="batch fuzz: key=start..end (numeric field in JSON body)")
    sp.add_argument("--fuzz-file", default="",
                    help="multi-field combination fuzz: payloads.json path, format {field: [values...]} (mutually exclusive with --fuzz; --fuzz-file takes precedence)")
    sp.add_argument("-m", "--mode", choices=["cartesian", "zip"], default="cartesian",
                    help="--fuzz-file combination mode: cartesian=cartesian product (default), zip=pair by shortest length")
    sp.add_argument("-r", "--repeat", type=int, default=1, help="repeat replay count (default 1)")
    sp.add_argument("--parallel", type=int, default=1, help="concurrent replay threads (default 1=serial; >1 uses ThreadPoolExecutor)")
    sp.add_argument("--interval-ms", type=int, default=0, dest="interval_ms",
                    help="delay between replay submissions in milliseconds (default 0=no delay; serial mode sleeps between iterations, parallel mode sleeps between submissions)")
    sp.add_argument("--server-side", action="store_true", dest="server_side",
                    help="offload batch replay to backend /repeat endpoint (unified concurrency/interval/stats; ignores --compare)")
    sp.add_argument("--compare", action="store_true", help="compare response differences across replays (using the first as baseline; outputs diff)")
    sp.add_argument("--timeout", type=int, default=0, help="HTTP request timeout seconds (default: 120s with body, 30s without body; increase for slow endpoints)")
    sp.set_defaults(func=cmd_replay)

    # send: send from scratch (Composer)
    sp = sub.add_parser("send", help="send from scratch (Composer): construct and send an HTTP request")
    sp.add_argument("-X", "--method", default="GET",
                    choices=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
                    help="HTTP method (default GET)")
    sp.add_argument("-u", "--url", required=True, help="request URL (must start with http:// or https://)")
    sp.add_argument("--header", action="append", default=[],
                    help="request header, format K:V (can be used multiple times)")
    sp.add_argument("-b", "--body", default="", help="request body (mutually exclusive with --body-file)")
    sp.add_argument("--body-file", default="",
                    help="read request body from file (mutually exclusive with --body; suitable for large body or binary)")
    sp.add_argument("--timeout", type=int, default=30,
                    help="HTTP request timeout seconds (default 30)")
    sp.add_argument("--headers-only", action="store_true",
                    help="only output response headers (no body)")
    sp.add_argument("--body-only", action="store_true",
                    help="only output response body (plain text, not JSON-wrapped; suitable for piping)")
    sp.add_argument("-c", "--emit-curl", action="store_true",
                    help="only output the equivalent curl command; do not actually send")
    sp.set_defaults(func=cmd_send)

    # replay-batch
    sp = sub.add_parser("replay-batch", aliases=["rep-batch"], help="timed replay: replay a session as a batch (optionally preserve original intervals)")
    sp.add_argument("-s", "--session", type=int, required=True, help="session ID")
    sp.add_argument("--preserve-timing", action="store_true",
                    help="sleep according to original intervals then replay (tests server rate limiting/risk control; forces serial)")
    sp.add_argument("--parallel", type=int, default=1,
                    help="concurrent threads (default 1=serial; ignored with --preserve-timing)")
    sp.add_argument("-f", "--filter", default="",
                    help="client-side filter expression (e.g. 'host~=api.x.com && method=POST'); only matching flows are replayed")
    sp.set_defaults(func=cmd_replay_batch)

    # processes (no subcommand=list processes; subcommand=ignore management)
    sp = sub.add_parser("processes", aliases=["procs"], help="process list / ignore process and host management")
    sp.add_argument("action", nargs="?", default="",
                    choices=["", "ignore", "unignore", "ignored", "ignore-host", "unignore-host", "ignored-hosts"],
                    help="subcommand: ignore/unignore/ignored/ignore-host/unignore-host/ignored-hosts (omitted=list processes)")
    sp.add_argument("row_id", type=int, nargs="?", default=0, help="row ID (for unignore/unignore-host)")
    sp.add_argument("--pid", type=int, default=None, help="ignore: ignore by PID (choose one of or combine with --name)")
    sp.add_argument("-N", "--name", default="", help="process name filter / ignore: ignore by name")
    sp.add_argument("-H", "--host", default="", help="ignore-host: host wildcard (e.g. *.example.com)")
    sp.add_argument("--with-connections", action="store_true", help="attach each process's current TCP connections")
    sp.add_argument("--tree", action="store_true", help="output as a process tree (find parent-child relationships)")
    sp.add_argument("--include-listen", action="store_true",
                    help="include LISTEN-state connections (skipped by default; only ESTABLISHED)")
    sp.add_argument("-J", "--json-array", action="store_true", help="output a JSON array")
    sp.set_defaults(func=cmd_processes)

    # export
    sp = sub.add_parser("export", aliases=["exp"], help="export session")
    sp.add_argument("-s", "--session", type=int, default=0, help="session ID")
    sp.add_argument("-F", "--format", choices=["har", "json", "python-requests", "postman", "curl", "csv", "pcap"],
                    default="har", help="export format")
    sp.add_argument("-o", "--output", default="", help="output file path (derives filename from format if not specified)")
    sp.set_defaults(func=cmd_export)

    # trigger（触发式捕获）
    sp = sub.add_parser("trigger", help="trigger capture (set/reset/clear/get)")
    sp.add_argument("trigger_action", choices=["set", "reset", "clear", "get"],
                    nargs="?", default="get", help="action")
    sp.add_argument("-d", "--dsl", default="", help="trigger DSL, e.g. 'host=example.com & status>=500'")
    sp.set_defaults(func=cmd_trigger)

    # heatmap（流量热力图）
    sp = sub.add_parser("heatmap", help="traffic heatmap (2D: time bucket x dimension)")
    sp.add_argument("-b", "--by", default="host",
                    choices=["host", "process", "method", "status_range", "ip_region"],
                    help="group by dimension")
    sp.add_argument("--bucket", type=int, default=60, help="bucket size in seconds (default 60)")
    sp.add_argument("--max-buckets", type=int, default=120, help="max time buckets (default 120)")
    sp.add_argument("--top", type=int, default=20, help="top N dimensions (default 20)")
    sp.add_argument("--host", default="", help="filter by host (fuzzy)")
    sp.add_argument("--process", default="", help="filter by process (fuzzy)")
    sp.set_defaults(func=cmd_heatmap)

    # topology（网络拓扑图）
    sp = sub.add_parser("topology", help="network topology (process -> IP -> host)")
    sp.add_argument("--max-nodes", type=int, default=100, help="max nodes (default 100)")
    sp.add_argument("--host", default="", help="filter by host (fuzzy)")
    sp.add_argument("--process", default="", help="filter by process (fuzzy)")
    sp.set_defaults(func=cmd_topology)

    # sessions
    sp = sub.add_parser("sessions", aliases=["sess"], help="session management (list / show / delete / create / rename)")
    sp.add_argument("action", choices=["list", "show", "delete", "create", "rename"], help="action")
    sp.add_argument("id", type=int, nargs="?", default=0, help="session ID (for show/delete/rename)")
    sp.add_argument("-J", "--json-array", action="store_true", help="list outputs a JSON array")
    sp.add_argument("--name", default="", help="session name (for create/rename)")
    sp.add_argument("--color", default="", help="session color hex (for create/rename, e.g. #FF5733)")
    sp.set_defaults(func=cmd_sessions)

    # proxy
    sp = sub.add_parser("proxy", aliases=["pxy"], help="system proxy control")
    sp.add_argument("action", choices=["status", "on", "off"], help="action")
    sp.set_defaults(func=cmd_proxy)

    # raw (TCP/UDP)
    sp = sub.add_parser("raw", help="TCP/UDP raw capture (cross-platform: Windows WinDivert / Linux AF_PACKET / macOS BPF)")
    sp.add_argument("action", choices=["status", "start", "stop"], help="action")
    sp.add_argument("--pid", default="", help="filter by PID, comma-separated")
    sp.add_argument("-p", "--port", default="", help="filter by port, comma-separated")
    sp.add_argument("--bpf", default="", help="capture filter string (Windows: WinDivert filter; Unix: port filtering via --port)")
    sp.set_defaults(func=cmd_raw)

    # cert
    sp = sub.add_parser("cert", help="certificate management")
    sp.add_argument("action", choices=["status", "install", "remove"], help="action: status|install|remove")
    sp.set_defaults(func=cmd_cert)

    # transparent-proxy (cross-platform NETWORK-layer redirection; requires administrator/root privileges)
    sp = sub.add_parser("transparent-proxy", aliases=["tp"],
                        help="transparent proxy control (cross-platform: Windows WinDivert / Linux iptables / macOS pf; requires administrator/root privileges)")
    sp.add_argument("action", choices=["status", "start", "stop"],
                    help="action: status=view state (running/redirected packet count/NAT table/error), "
                         "start=start (requires administrator/root privileges), stop=stop")
    sp.set_defaults(func=cmd_transparent_proxy)

    # dns-hijack (cross-platform DNS response tampering; requires administrator/root privileges)
    sp = sub.add_parser("dns-hijack", aliases=["dns"],
                        help="DNS hijack control (cross-platform: Windows WinDivert / Linux iptables+local DNS / macOS pf+local DNS; requires administrator/root privileges)")
    sp.add_argument("action", choices=["status", "start", "stop", "rules", "clear-log"],
                    help="action: status=view state/rules/stats/logs, start=start (can take --rules/--default-ip), "
                         "stop=stop, rules=view/update rules (--set to update), clear-log=clear logs")
    sp.add_argument("--rules", default="",
                    help="initial rules at startup, format: domain=ip,domain=ip (supports *.example.com wildcard)")
    sp.add_argument("--default-ip", default="",
                    help="default hijack IP (A-record queries that don't match any rule return this IP)")
    sp.add_argument("--set", dest="set_rules", default="",
                    help="update rules (only effective with the rules action), format: domain=ip,domain=ip")
    sp.set_defaults(func=cmd_dns_hijack)

    # auto-reply (auto-reply rule management: list/get/create/enable/disable/delete)
    sp = sub.add_parser("auto-reply", aliases=["ar"],
                        help="auto-reply rule management (list/get/create/enable/disable/delete; create supports --script-path)")
    sp_sub = sp.add_subparsers(dest="sub", required=True)
    sp_ar_list = sp_sub.add_parser("list", aliases=["ls"], help="list all rules (NDJSON; includes hit stats)")
    sp_ar_list.add_argument("-J", "--json-array", action="store_true", help="output a JSON array")
    sp_ar_list.set_defaults(func=cmd_auto_reply)
    sp_ar_get = sp_sub.add_parser("get", help="view rule details")
    sp_ar_get.add_argument("id", help="rule ID")
    sp_ar_get.set_defaults(func=cmd_auto_reply)
    sp_ar_create = sp_sub.add_parser("create", aliases=["new"],
                                     help="create a rule (supports --script-path to load a Python script from a .py file)")
    sp_ar_create.add_argument("--pattern", required=True,
                              help="URL match pattern (e.g. *api.example.com*/v1/*)")
    sp_ar_create.add_argument("--action", required=True, dest="action_type",
                              choices=["script", "mock", "modify_response", "modify_request", "mock_request"],
                              help="action type: script=Python script, mock=fake response, modify_response=modify response, "
                                   "modify_request=modify request, mock_request=hardcode request and forward")
    sp_ar_create.add_argument("--script-path", default="",
                              help="action=script: load script content from a local .py file (agent-friendly; mutually exclusive with --script)")
    sp_ar_create.add_argument("--script", default="",
                              help="action=script: inline Python script source (mutually exclusive with --script-path)")
    sp_ar_create.add_argument("--action-spec", default="",
                              help="non-script actions: action spec string (e.g. 'set-json key value' / 'mock 200 {}'), "
                                   "reuses the intercept add syntax")
    sp_ar_create.add_argument("--match-mode", choices=["wildcard", "exact", "regex"],
                              default="wildcard", help="match mode (default wildcard)")
    sp_ar_create.add_argument("--note", default="", help="rule note")
    sp_ar_create.add_argument("--method-filter", default="", help="method filter (comma-separated)")
    sp_ar_create.add_argument("--status-filter", default="", help="status code filter (comma-separated)")
    sp_ar_create.add_argument("--pid-filter", default="", help="PID filter")
    sp_ar_create.add_argument("--process-filter", default="", help="process name filter")
    sp_ar_create.add_argument("--disabled", action="store_true", help="create in disabled state")
    sp_ar_create.set_defaults(func=cmd_auto_reply)
    sp_ar_enable = sp_sub.add_parser("enable", aliases=["en"], help="enable a rule")
    sp_ar_enable.add_argument("id", help="rule ID")
    sp_ar_enable.set_defaults(func=cmd_auto_reply)
    sp_ar_disable = sp_sub.add_parser("disable", aliases=["dis"], help="disable a rule")
    sp_ar_disable.add_argument("id", help="rule ID")
    sp_ar_disable.set_defaults(func=cmd_auto_reply)
    sp_ar_delete = sp_sub.add_parser("delete", aliases=["del"], help="delete a rule")
    sp_ar_delete.add_argument("id", help="rule ID")
    sp_ar_delete.set_defaults(func=cmd_auto_reply)
    # test-script: test Python script execution (does not create a rule; agent-friendly)
    # Usage: auto-reply test-script --script-path ./my_hook.py
    #        auto-reply test-script --script "def on_request(ctx): ..." --mock-resp-status 200
    sp_ar_test = sp_sub.add_parser("test-script", aliases=["test"],
        help="test Python script execution (does not create a rule; runs the worker subprocess with mock data)")
    sp_ar_test.add_argument("--script-path", default="",
        help="load script from a local .py file (mutually exclusive with --script)")
    sp_ar_test.add_argument("--script", default="",
        help="inline Python script source (mutually exclusive with --script-path)")
    sp_ar_test.add_argument("--mock-host", default="api.example.com", help="mock request host")
    sp_ar_test.add_argument("--mock-path", default="/v1/user", help="mock request path")
    sp_ar_test.add_argument("--mock-method", default="GET",
        choices=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
        help="mock request method")
    sp_ar_test.add_argument("--mock-scheme", default="https", help="mock request scheme")
    sp_ar_test.add_argument("--mock-http-version", default="HTTP/1.1", help="mock HTTP version")
    sp_ar_test.add_argument("--mock-headers", default="",
        help="mock request headers (JSON object string, e.g. '{\"User-Agent\":\"test\"}')")
    sp_ar_test.add_argument("--mock-body", default="", help="mock request body string")
    # Optional: mock response (if provided, on_response is also called)
    sp_ar_test.add_argument("--mock-resp-status", type=int, default=None,
        help="mock response status code (if provided, the on_response hook is also tested)")
    sp_ar_test.add_argument("--mock-resp-headers", default="",
        help="mock response headers (JSON object string)")
    sp_ar_test.add_argument("--mock-resp-body", default="", help="mock response body string")
    sp_ar_test.set_defaults(func=cmd_auto_reply)

    # log
    sp = sub.add_parser("log", help="log management")
    sp_sub = sp.add_subparsers(dest="sub", required=True)
    sp_tail = sp_sub.add_parser("tail", help="view logs (NDJSON)")
    sp_tail.add_argument("-l", "--level", default="", help="level filter: DEBUG/INFO/WARNING/ERROR")
    sp_tail.add_argument("-C", "--category", default="", help="category filter: proxy/ai/settings/raw")
    sp_tail.add_argument("-n", "--limit", type=int, default=100, help="number of entries")
    sp_tail.set_defaults(func=cmd_log_tail)
    sp_clear = sp_sub.add_parser("clear", aliases=["clr"], help="clear logs")
    sp_clear.set_defaults(func=cmd_log_clear)
    sp_export = sp_sub.add_parser("export", aliases=["exp"], help="export logs as a JSONL file")
    sp_export.add_argument("-o", "--output", default="", help="output file path (stdout if not specified)")
    sp_export.add_argument("-l", "--level", default="", help="level filter")
    sp_export.add_argument("-C", "--category", default="", help="category filter")
    sp_export.add_argument("-k", "--keyword", default="", help="keyword filter")
    sp_export.set_defaults(func=cmd_log_export)

    # system
    sp = sub.add_parser("system", aliases=["sys"], help="system control (restart/quit/admin restart/firewall allow/WinDivert risk prompt/platform capabilities)")
    sp.add_argument("action",
                    choices=["restart", "quit", "restart-as-admin", "firewall-allow", "firewall-status",
                             "windivert-warning-status", "windivert-warning-ack",
                             "platform-capabilities"],
                    help="action: restart=restart front/backend, quit=exit Telnix, restart-as-admin=restart as administrator (UAC/sudo elevation), "
                         "firewall-allow=allow ports 8888/18901 through firewall (required for phone capture), firewall-status=view allow-rule status, "
                         "windivert-warning-status=query WinDivert risk-prompt status, windivert-warning-ack=acknowledge WinDivert risk prompt (will not be shown again), "
                         "platform-capabilities=query current platform's supported features and privilege requirements (cross-platform compatibility query)")
    sp.add_argument("-j", "--json", action="store_true", help="output JSON only; silence stderr hints (agent-friendly)")
    sp.set_defaults(func=cmd_system)

    # settings (includes proxy engine switching)
    sp = sub.add_parser("settings", aliases=["set"], help="settings management (get/set/proxy-engine)")
    sp_sub = sp.add_subparsers(dest="sub_action", required=True)
    sp_get = sp_sub.add_parser("get", help="read all settings (NDJSON)")
    sp_get.add_argument("-k", "--key", default="", help="read only a specific key")
    sp_get.add_argument("-j", "--json", action="store_true", help="output JSON only; silence stderr hints (agent-friendly)")
    sp_get.set_defaults(func=cmd_settings_get)
    sp_set = sp_sub.add_parser("set", help="write a single setting item (--key/--value)")
    sp_set.add_argument("-k", "--key", required=True, help="setting key")
    sp_set.add_argument("-v", "--value", required=True, help="setting value (bool/list/dict are auto-deserialized)")
    sp_set.add_argument("-j", "--json", action="store_true", help="output JSON only; silence stderr hints (agent-friendly)")
    sp_set.set_defaults(func=cmd_settings_set)
    sp_engine = sp_sub.add_parser("engine", help="view/switch proxy engine (builtin/async/mitmproxy)")
    sp_engine.add_argument("name", nargs="?", default="",
                          help="engine name: builtin (default threaded) / async (asyncio) / mitmproxy (requires pip install mitmproxy); "
                               "omitted = view current engine only")
    sp_engine.add_argument("-j", "--json", action="store_true", help="output JSON only; silence stderr hints (agent-friendly)")
    sp_engine.set_defaults(func=cmd_settings_engine)

    # tools — 代理工具
    sp = sub.add_parser("tools", aliases=["tool"], help="proxy tools (no-cache, force-cors, block-list, allow-list)")
    sp_sub = sp.add_subparsers(dest="sub", required=False)
    # tools status
    sp_sub.add_parser("status", help="show proxy tools status")
    # tools no-cache [on|off]
    sp_nc = sp_sub.add_parser("no-cache", aliases=["noc"], help="toggle no-caching")
    sp_nc.add_argument("on", nargs="?", choices=["on", "off"], help="enable/disable")
    # tools force-cors [on|off]
    sp_fc = sp_sub.add_parser("force-cors", aliases=["cors"], help="toggle force-cors")
    sp_fc.add_argument("on", nargs="?", choices=["on", "off"], help="enable/disable")
    # tools block-list <list|on|off|add|del>
    sp_bl = sp_sub.add_parser("block-list", aliases=["block"], help="manage block list")
    sp_bl_sub = sp_bl.add_subparsers(dest="action", required=True)
    sp_bl_sub.add_parser("list", help="list block rules")
    sp_bl_sub.add_parser("on", help="enable block list")
    sp_bl_sub.add_parser("off", help="disable block list")
    sp_bl_add = sp_bl_sub.add_parser("add", help="add block rule")
    sp_bl_add.add_argument("pattern", help="pattern to match")
    sp_bl_add.add_argument("-m", "--mode", default="wildcard", choices=["wildcard", "exact", "regex"], help="match mode")
    sp_bl_del = sp_bl_sub.add_parser("del", help="delete block rule by index")
    sp_bl_del.add_argument("index", type=int, help="rule index")
    # tools allow-list <list|on|off|add|del>
    sp_al = sp_sub.add_parser("allow-list", aliases=["allow"], help="manage allow list")
    sp_al_sub = sp_al.add_subparsers(dest="action", required=True)
    sp_al_sub.add_parser("list", help="list allow rules")
    sp_al_sub.add_parser("on", help="enable allow list")
    sp_al_sub.add_parser("off", help="disable allow list")
    sp_al_add = sp_al_sub.add_parser("add", help="add allow rule")
    sp_al_add.add_argument("pattern", help="pattern to match")
    sp_al_add.add_argument("-m", "--mode", default="wildcard", choices=["wildcard", "exact", "regex"], help="match mode")
    sp_al_del = sp_al_sub.add_parser("del", help="delete allow rule by index")
    sp_al_del.add_argument("index", type=int, help="rule index")
    # tools map-local <list|on|off|add|del>
    sp_ml = sp_sub.add_parser("map-local", aliases=["ml"], help="manage map local (serve local file for matched requests)")
    sp_ml_sub = sp_ml.add_subparsers(dest="action", required=True)
    sp_ml_sub.add_parser("list", help="list map local rules")
    sp_ml_sub.add_parser("on", help="enable map local")
    sp_ml_sub.add_parser("off", help="disable map local")
    sp_ml_add = sp_ml_sub.add_parser("add", help="add map local rule")
    sp_ml_add.add_argument("pattern", help="pattern to match")
    sp_ml_add.add_argument("file_path", help="local file path to serve")
    sp_ml_add.add_argument("-m", "--mode", default="wildcard", choices=["wildcard", "exact", "regex"], help="match mode")
    sp_ml_add.add_argument("--status", type=int, default=0, help="HTTP status code (0=default 200)")
    sp_ml_del = sp_ml_sub.add_parser("del", help="delete map local rule by index")
    sp_ml_del.add_argument("index", type=int, help="rule index")
    # tools map-remote <list|on|off|add|del>
    sp_mr = sp_sub.add_parser("map-remote", aliases=["mr"], help="manage map remote (redirect matched requests to another URL)")
    sp_mr_sub = sp_mr.add_subparsers(dest="action", required=True)
    sp_mr_sub.add_parser("list", help="list map remote rules")
    sp_mr_sub.add_parser("on", help="enable map remote")
    sp_mr_sub.add_parser("off", help="disable map remote")
    sp_mr_add = sp_mr_sub.add_parser("add", help="add map remote rule")
    sp_mr_add.add_argument("pattern", help="pattern to match")
    sp_mr_add.add_argument("target_url", help="target URL to redirect to")
    sp_mr_add.add_argument("-m", "--mode", default="wildcard", choices=["wildcard", "exact", "regex"], help="match mode")
    sp_mr_del = sp_mr_sub.add_parser("del", help="delete map remote rule by index")
    sp_mr_del.add_argument("index", type=int, help="rule index")
    # tools mirror <list|on|off|add|del>
    sp_mir = sp_sub.add_parser("mirror", aliases=["mir"], help="manage mirror (auto-save matched responses to local dir)")
    sp_mir_sub = sp_mir.add_subparsers(dest="action", required=True)
    sp_mir_sub.add_parser("list", help="list mirror rules")
    sp_mir_sub.add_parser("on", help="enable mirror")
    sp_mir_sub.add_parser("off", help="disable mirror")
    sp_mir_add = sp_mir_sub.add_parser("add", help="add mirror rule")
    sp_mir_add.add_argument("pattern", help="pattern to match")
    sp_mir_add.add_argument("save_dir", help="directory to save responses")
    sp_mir_add.add_argument("-m", "--mode", default="wildcard", choices=["wildcard", "exact", "regex"], help="match mode")
    sp_mir_del = sp_mir_sub.add_parser("del", help="delete mirror rule by index")
    sp_mir_del.add_argument("index", type=int, help="rule index")
    sp.set_defaults(func=cmd_tools)

    # agent workspace mode
    sp = sub.add_parser("agent", help="agent workspace mode: temporarily clear the workspace and keep a backup of the original state; restore afterwards")
    sp.add_argument("action", choices=["start", "end", "status"],
                    help="start=save current rules/focus/breakpoints and disable them, end=restore original state from backup, status=query workspace state")
    sp.set_defaults(func=cmd_agent)

    # focus
    sp = sub.add_parser("focus", help="focus mode (only capture specified process/host; cross-category OR match)")
    sp.add_argument("action", choices=["status", "on", "off"], help="action")
    sp.add_argument("--pid", default="", help="by PID, comma-separated")
    sp.add_argument("-N", "--name", default="", help="by process name, comma-separated (use this when PIDs change)")
    sp.add_argument("-H", "--host", default="", help="by host wildcard, comma-separated (e.g. *.example.com; cross-category OR match)")
    sp.add_argument("--no-children", action="store_true", help="do not auto-include child processes")
    sp.set_defaults(func=cmd_focus)

    # breakpoint
    sp = sub.add_parser("breakpoint", aliases=["bp"], help="breakpoint control (supports timeout auto-release / batch release)")
    sp.add_argument("action", choices=["status", "on", "off", "timeout", "release", "drop"],
                    help="action: status|on|off|timeout|release|drop")
    sp.add_argument("id", type=int, nargs="?", default=0, help="flow ID (for release/drop)")
    sp.add_argument("--type", choices=["request", "response"], default="", help="breakpoint type (for on/off; default request)")
    sp.add_argument("--timeout", type=float, default=None, help="timeout seconds: set this value on on; auto-releases after N seconds without release; required for the timeout command")
    sp.add_argument("-a", "--all", action="store_true", help="release/drop batch operate on all pending breakpoints")
    sp.set_defaults(func=cmd_breakpoint)

    # cookies
    sp = sub.add_parser("cookies", aliases=["cookie"], help="cookie management (aggregated from Cookie/Set-Cookie headers in flows)")
    sp_sub = sp.add_subparsers(dest="action", required=True)
    sp_list = sp_sub.add_parser("list", aliases=["ls"], help="list cookies grouped by host")
    sp_list.add_argument("--host", default="", help="filter by host")
    sp_list.add_argument("-j", "--json", action="store_true", help="output raw JSON")
    sp_list.set_defaults(func=cmd_cookies)
    sp_ch = sp_sub.add_parser("clear-host", help="clear cookies for a host")
    sp_ch.add_argument("host", help="host to clear")
    sp_ch.add_argument("-j", "--json", action="store_true", help="output raw JSON")
    sp_ch.set_defaults(func=cmd_cookies)
    sp_ca = sp_sub.add_parser("clear-all", help="clear all cookies")
    sp_ca.add_argument("-j", "--json", action="store_true", help="output raw JSON")
    sp_ca.set_defaults(func=cmd_cookies)

    # site-map
    sp = sub.add_parser("site-map", aliases=["smap"], help="site map tree (aggregated from flows)")
    sp.add_argument("--host", default="", help="filter by host")
    sp.add_argument("-j", "--json", action="store_true", help="output raw JSON")
    sp.set_defaults(func=cmd_site_map)

    # record-replay
    sp = sub.add_parser("record-replay", aliases=["rr"], help="traffic record/replay management")
    sp_sub = sp.add_subparsers(dest="action", required=True)
    sp_list = sp_sub.add_parser("list", aliases=["ls"], help="list record scripts (NDJSON)")
    sp_list.add_argument("-j", "--json", action="store_true", help="output raw JSON")
    sp_list.set_defaults(func=cmd_record_replay)
    sp_create = sp_sub.add_parser("create", help="create a record script from given flow ids")
    sp_create.add_argument("name", help="script name")
    sp_create.add_argument("--flow-ids", default="", help="comma-separated flow ids, e.g. 1,2,3")
    sp_create.add_argument("--note", default="", help="optional note")
    sp_create.add_argument("-j", "--json", action="store_true", help="output raw JSON")
    sp_create.set_defaults(func=cmd_record_replay)
    sp_show = sp_sub.add_parser("show", help="show record script details (with flows)")
    sp_show.add_argument("script_id", help="script id (hex string)")
    sp_show.add_argument("-j", "--json", action="store_true", help="output raw JSON")
    sp_show.set_defaults(func=cmd_record_replay)
    sp_del = sp_sub.add_parser("delete", aliases=["del"], help="delete a record script")
    sp_del.add_argument("script_id", help="script id (hex string)")
    sp_del.add_argument("-j", "--json", action="store_true", help="output raw JSON")
    sp_del.set_defaults(func=cmd_record_replay)
    sp_rp = sp_sub.add_parser("replay", help="replay a record script")
    sp_rp.add_argument("script_id", help="script id (hex string)")
    sp_rp.add_argument("-j", "--json", action="store_true", help="output raw JSON")
    sp_rp.set_defaults(func=cmd_record_replay)
    sp_sr = sp_sub.add_parser("start-record", help="start recording traffic")
    sp_sr.add_argument("-j", "--json", action="store_true", help="output raw JSON")
    sp_sr.set_defaults(func=cmd_record_replay)
    sp_stop = sp_sub.add_parser("stop-record", help="stop recording traffic")
    sp_stop.add_argument("--name", default="", help="optional script name for the recorded session")
    sp_stop.add_argument("--note", default="", help="optional note")
    sp_stop.add_argument("-j", "--json", action="store_true", help="output raw JSON")
    sp_stop.set_defaults(func=cmd_record_replay)
    sp_st = sp_sub.add_parser("status", help="query recording status")
    sp_st.add_argument("-j", "--json", action="store_true", help="output raw JSON")
    sp_st.set_defaults(func=cmd_record_replay)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help(sys.stderr)
        sys.exit(3)
    args.func(args)


if __name__ == "__main__":
    main()

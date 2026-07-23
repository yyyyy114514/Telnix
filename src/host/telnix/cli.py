"""Telnix Agent CLI —— 给 AI agent 用的命令行抓包/拦截控制工具。

设计原则（对 agent 友好）：
1. 输出机器可读：列表默认 NDJSON（一行一个 JSON 对象），单对象输出紧凑 JSON。
2. 非交互、无 TTY 假设：零分页、零确认、零彩色 TUI。
3. 会话化：capture start 返回 session_id，后续命令带 --session 续命。
4. 防翻车：capture start 支持 --max-duration / --auto-stop。
5. 声明式拦截：intercept add --match 'expr' --action 'spec'，不写脚本。
6. --dry-run：拦截规则预览（列出会命中的流量，不真创建规则）。
7. --emit-curl：每条 HTTP 流量直接出可重放 curl 命令。
8. --since-id：非阻塞增量查询，替代 --tail 做轮询。

用法：
    python -m telnix.cli <subcommand> [options]

子命令：
    status                              后端状态（JSON）
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

匹配表达式（--match）：
    key op value [ && key op value ...]
    key:   host | method | path | url | status | pid | process
    op:    =（精确） ~=（通配符/包含） != >= <= > <
    示例:  'host~=api.example.com && method=POST && path~=/api/v1/*'

动作规范（--action）：
    改响应：
    set-json key value            改响应体 JSON 字段（全局搜索同名 key）
    set-json-path path value      改响应体 JSON 字段（精确路径）
    remove-json key               删除响应体 JSON 字段（全局）
    remove-json-path path         删除响应体 JSON 字段（精确路径）
    replace-header K V            改响应头
    replace-bytes offset:hex      二进制偏移替换响应体
    replace-bytes-regex regex hex 正则替换响应体字节
    mock CODE BODY                直接返回 CODE + BODY（不走服务器）
    status CODE                   直接返回 CODE（空 body）
    drop                          模拟失败（503 空 body）
    mock-request BODY [CTYPE]     写死请求 body，走真实服务器返回真实响应

    改请求：
    set-request-header K V        改请求头
    set-request-json key value    改请求体 JSON 字段
    set-request-json-path path v  改请求体 JSON 字段（精确路径）
    remove-request-json key       删除请求体 JSON 字段
    set-request-body-hex hex      替换整个请求体为 hex 字节
    replace-request-bytes off:hex 二进制偏移替换请求体

    Python 脚本（复杂逻辑用，独立 worker 子进程运行）：
    script '<source>'             内联脚本（单引号包裹）
    script file <path>            从文件读取脚本
    脚本定义 on_request(ctx)/on_response(ctx)，可用 ctx.set_* 修改或返回 {drop/mock}。

所有命令退出码：0 成功，1 业务错误，2 连接错误，3 参数错误。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_HOST = "127.0.0.1"
# 与 config.DEFAULT_PORT 保持一致（避免 CLI 默认连旧端口 18899）
try:
    from .config import DEFAULT_PORT as _CFG_PORT
    DEFAULT_PORT = _CFG_PORT
except Exception:  # noqa: BLE001
    DEFAULT_PORT = 18901
BASE_URL = os.environ.get("TELNIX_API", f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")


# ---------- HTTP ----------

def _req(method: str, path: str, body: Any = None, timeout: float = 30.0) -> dict:
    """调用后端 API，返回 {code, data, msg}。"""
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
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            return {"code": e.code, "msg": f"HTTP {e.code}: {e.reason}", "data": None}
    except urllib.error.URLError as e:
        _die_conn(f"无法连接后端 {BASE_URL}: {e.reason}")
    except Exception as e:  # noqa: BLE001
        _die_conn(f"连接异常: {e}")


def _ok(res: dict) -> Any:
    """提取 data，失败则 die。"""
    if res.get("code") == 0:
        return res.get("data")
    msg = res.get("msg") or "未知错误"
    hint = _hint_for_error(msg)
    err_obj = {"ok": False, "error": msg}
    if hint:
        err_obj["hint"] = hint
    print(json.dumps(err_obj, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)


# ---------- 错误提示 ----------

def _hint_for_error(msg: str) -> str | None:
    """根据错误信息生成 agent 可操作的修复建议。"""
    msg_l = msg.lower()
    if "无法连接" in msg or "connection" in msg_l:
        return "后端未启动？运行: cd src\\host && python -m telnix"
    if "证书" in msg or "cert" in msg_l:
        return "HTTPS 解密需要证书: python -m telnix.cli cert install"
    if "pydivert" in msg_l:
        return "TCP/UDP 抓包需要: pip install pydivert（并用管理员身份运行）"
    if "管理员" in msg or "admin" in msg_l:
        return "请用管理员身份重启 Telnix"
    if "会话" in msg and "不存在" in msg:
        return "先运行: python -m telnix.cli capture start"
    if "not found" in msg_l or "找不到" in msg:
        return "后端可能未重启，旧进程缺少新路由。重启后端: python -m telnix.cli restart 或手动重启"
    return None


# ---------- 输出 ----------

def emit_obj(obj: Any) -> None:
    """单对象：紧凑 JSON 一行。"""
    print(json.dumps(obj, ensure_ascii=False))


def emit_list(items: list[Any], emit_curl: bool = False, json_array: bool = False) -> None:
    """列表：默认 NDJSON，--json-array 时输出 JSON 数组。"""
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


# ---------- curl 构建 ----------

def _parse_headers(raw_h) -> dict:
    """健壮解析 headers（JSON 字符串或 HTTP 文本格式）。"""
    headers = {}
    if not raw_h or not isinstance(raw_h, str):
        return headers
    # 优先尝试 JSON
    try:
        h_obj = json.loads(raw_h)
        if isinstance(h_obj, dict):
            return {str(k): str(v) for k, v in h_obj.items()}
    except Exception:  # noqa: BLE001
        pass
    # 文本格式：按行 split(":", 1) 避免切到含 : 的值
    for line in raw_h.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    return headers


def build_curl(flow: dict, output_file: str = "") -> str:
    """从流量构造可重放的 curl 命令。二进制 body 在 Windows 上用临时文件方案。

    - output_file 指定时（如 req.sh）：同时生成 <stem>_body.bin，curl 用 --data-binary @<stem>_body.bin
    - output_file 为空时：Windows 用 PowerShell 解码到 body.bin；非 Windows 保持 Unix 管道
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
            # 二进制 body：根据输出模式和平台选择方案
            b64 = body[7:]
            is_windows = sys.platform.startswith("win")
            if output_file:
                # 文件模式：把 base64 解码写入 <stem>_body.bin，curl 脚本引用该文件
                stem = os.path.splitext(output_file)[0]
                bin_path = f"{stem}_body.bin"
                try:
                    import base64 as _b64
                    raw = _b64.b64decode(b64)
                    with open(bin_path, "wb") as bf:
                        bf.write(raw)
                except Exception:  # noqa: BLE001
                    pass  # 解码失败仍输出脚本，执行时报错便于定位
                parts += ["--data-binary", f"@{bin_path}"]
            else:
                # stdout 模式：无 -o 时无法写外部文件，回退到 Unix 管道
                if is_windows:
                    # Windows 上 base64 -d 不可用，打印警告提示用 -o
                    print("警告：Windows 上未指定 -o，二进制 body 使用 Unix 管道（echo|base64 -d）"
                          "可能不兼容，建议使用 -o 写入文件以生成 _body.bin", file=sys.stderr)
                parts = ['echo', f'"{b64}"', '|', 'base64', '-d', '|'] + parts + ["--data-binary", "@-"]
        else:
            b = body.replace("'", "'\\''")
            parts += ["-d", f"'{b}'"]
    parts.append(f'"{url}"')
    return " ".join(parts)


# ---------- 匹配表达式解析 ----------

def parse_match(expr: str) -> dict:
    """解析匹配表达式，返回 {pattern, match_mode, filters}。"""
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
            _die_arg(f"无法解析匹配条件: {tok}")
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
    # 提取 method/status/pid/process 到独立 filter 字段（逗号分隔多值），
    # 由 cmd_intercept_add 写入 rule 的 method_filter/status_filter/pid_filter/process_filter，
    # 代理层 find_matching_rule 会校验这些字段（§4.1 陷阱已修复）。
    # 仅收集 op 为 =/~=/!= 的条件；数值比较（>=/<=/>/<）保留在 filters 里只做客户端过滤。
    filter_fields = {"method": [], "status": [], "pid": [], "process": []}
    for k, op, v in filters:
        if k in filter_fields and op in ("=", "~=", "!="):
            # != 暂不支持后端过滤（需 negative match），只对 =/~= 收集
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
    """客户端过滤（用于 dry-run / packets list --filter）。"""
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


def _wildcard_to_regex(pat: str):
    import re
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


# ---------- 动作解析 ----------

def parse_action(spec: str) -> dict:
    """解析动作规范，返回后端规则字段。"""
    parts = _split_action(spec)
    if not parts:
        _die_arg("动作规范不能为空")
    name = parts[0].lower()
    args = parts[1:]

    # ---- 改响应 ----
    if name == "replace-header":
        if len(args) < 2:
            _die_arg("replace-header 需要: K V")
        return {
            "action": "modify_response",
            "modify_rules": [{"target": "response_header", "op": "replace", "key": args[0], "value": args[1]}],
        }
    if name == "set-json":
        if len(args) < 2:
            _die_arg("set-json 需要: key value")
        return {
            "action": "modify_response",
            "modify_rules": [{"target": "response_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}],
        }
    if name == "set-json-path":
        if len(args) < 2:
            _die_arg("set-json-path 需要: path value")
        return {
            "action": "modify_response",
            "modify_rules": [{"target": "response_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}],
        }
    if name == "remove-json":
        if len(args) < 1:
            _die_arg("remove-json 需要: key")
        return {
            "action": "modify_response",
            "modify_rules": [{"target": "response_body", "op": "remove", "key": args[0]}],
        }
    if name == "remove-json-path":
        if len(args) < 1:
            _die_arg("remove-json-path 需要: path")
        return {
            "action": "modify_response",
            "modify_rules": [{"target": "response_body", "op": "remove", "key": args[0]}],
        }
    if name == "replace-bytes":
        # replace-bytes offset:hex
        if len(args) < 1:
            _die_arg("replace-bytes 需要: offset:hex")
        return {
            "action": "modify_response",
            "modify_rules": [{"target": "response_body", "op": "replace-bytes", "key": "", "value": args[0]}],
        }
    if name == "replace-bytes-regex":
        if len(args) < 2:
            _die_arg("replace-bytes-regex 需要: regex hex")
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
        # 写死请求内容（mock 请求 body），走真实服务器返回真实响应
        # 用法: mock-request '<json body>' 或 mock-request '<json body>' 'application/json'
        body = args[0] if args else ""
        ctype = args[1] if len(args) > 1 else "application/json"
        return {
            "action": "mock_request",
            "mock_body": body,
            "mock_headers": {"Content-Type": ctype},
        }

    # ---- 改请求 ----
    if name == "set-request-header":
        if len(args) < 2:
            _die_arg("set-request-header 需要: K V")
        return {
            "action": "modify_request",
            "modify_rules": [{"target": "request_header", "op": "replace", "key": args[0], "value": args[1]}],
        }
    if name == "set-request-json":
        if len(args) < 2:
            _die_arg("set-request-json 需要: key value")
        return {
            "action": "modify_request",
            "modify_rules": [{"target": "request_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}],
        }
    if name == "set-request-json-path":
        if len(args) < 2:
            _die_arg("set-request-json-path 需要: path value")
        return {
            "action": "modify_request",
            "modify_rules": [{"target": "request_body", "op": "replace", "key": args[0], "value": _auto_type(args[1])}],
        }
    if name == "remove-request-json":
        if len(args) < 1:
            _die_arg("remove-request-json 需要: key")
        return {
            "action": "modify_request",
            "modify_rules": [{"target": "request_body", "op": "remove", "key": args[0]}],
        }
    if name == "set-request-body-hex":
        if len(args) < 1:
            _die_arg("set-request-body-hex 需要: hex")
        return {
            "action": "modify_request",
            "modify_rules": [{"target": "request_body", "op": "replace", "key": "", "value": _hex_to_b64(args[0])}],
        }
    if name == "replace-request-bytes":
        if len(args) < 1:
            _die_arg("replace-request-bytes 需要: offset:hex")
        return {
            "action": "modify_request",
            "modify_rules": [{"target": "request_body", "op": "replace-bytes", "key": "", "value": args[0]}],
        }

    # ---- 时序动作 ----
    if name == "delay":
        # delay N：响应阶段 sleep N 毫秒（测前端超时/重试逻辑）
        if len(args) < 1:
            _die_arg("delay 需要: N（毫秒）")
        try:
            val = int(args[0])
        except ValueError:
            _die_arg(f"delay 参数需为整数毫秒: {args[0]}")
        return {
            "action": "modify_response",
            "modify_rules": [{"target": "delay", "op": "sleep", "value": val}],
        }
    if name == "delay-request":
        # delay-request N：请求阶段 sleep N 毫秒（测服务端限流/风控）
        if len(args) < 1:
            _die_arg("delay-request 需要: N（毫秒）")
        try:
            val = int(args[0])
        except ValueError:
            _die_arg(f"delay-request 参数需为整数毫秒: {args[0]}")
        return {
            "action": "modify_request",
            "modify_rules": [{"target": "delay-request", "op": "sleep", "value": val}],
        }

    # ---- Python 脚本 ----
    if name == "script":
        # script '<inline source>' 或 script-file <path>
        # 内联脚本用单引号包裹（shlex 解析后为单个参数）
        if len(args) < 1:
            _die_arg("script 需要: '<inline source>' 或 script-file <path>")
        # 注意：args[0] 可能是 "file" 子命令
        if args[0] == "file" and len(args) >= 2:
            path = args[1]
            try:
                with open(path, "r", encoding="utf-8") as f:
                    source = f.read()
            except OSError as e:
                _die_arg(f"读取脚本文件失败: {e}")
        else:
            source = args[0]
        return {"action": "script", "modify_rules": source}

    _die_arg(f"未知动作: {name}（支持: set-json/set-json-path/remove-json/replace-header/replace-bytes/replace-bytes-regex/mock/status/drop/mock-request/set-request-header/set-request-json/set-request-json-path/remove-request-json/set-request-body-hex/replace-request-bytes/delay/delay-request/script）")
    return {}


def _split_action(spec: str) -> list[str]:
    import shlex
    try:
        return shlex.split(spec)
    except ValueError:
        return spec.split()


def _auto_type(v: str) -> Any:
    """自动识别数字/布尔/null，否则字符串。"""
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
    """hex 字符串转 base64: 前缀字符串。"""
    import base64
    raw = bytes.fromhex(hex_str.replace(" ", "").replace("0x", ""))
    return "base64:" + base64.b64encode(raw).decode("ascii")


# ---------- 退出 ----------

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


# ---------- 子命令 ----------

def cmd_status(args):
    res = _req("GET", "/status")
    emit_obj(res.get("data") or res)


def cmd_capture_start(args):
    body: dict = {}
    # --auto-stop N：传给后端，由后端常驻进程负责定时停止（agent CLI 退出也不影响）
    auto_stop = getattr(args, "auto_stop", 0) or 0
    if auto_stop and auto_stop > 0:
        body["auto_stop_seconds"] = float(auto_stop)
    res = _req("POST", "/capture/start", body, timeout=10)
    data = _ok(res)
    out = {"session_id": data.get("session_id"), "capturing": True}
    if args.max_duration and data.get("session_id"):
        out["auto_stop_after"] = args.max_duration
        out["hint"] = f"建议 {args.max_duration}s 后运行: python -m telnix.cli capture stop"
    if data.get("auto_stop_seconds"):
        out["auto_stop_seconds"] = data["auto_stop_seconds"]
        out["hint"] = f"后端将在 {data['auto_stop_seconds']}s 后自动停止抓包"
    # 如果指定 --layer tcp/all，启动 TCP/UDP 后端
    if args.layer in ("tcp", "all"):
        raw_body = {}
        if args.pid:
            raw_body["pid_filter"] = [int(p) for p in args.pid.split(",") if p.strip()]
        if args.port:
            raw_body["port_filter"] = [int(p) for p in args.port.split(",") if p.strip()]
        if args.bpf:
            raw_body["filter_str"] = args.bpf
        raw_res = _req("POST", "/raw/start", raw_body, timeout=10)
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
    # 同时停 TCP/UDP 后端
    if args.layer in ("tcp", "all"):
        _req("POST", "/raw/stop")
    emit_obj({"capturing": False})


def cmd_capture_clear(args):
    res = _req("POST", "/capture/clear")
    _ok(res)
    emit_obj({"cleared": True})


def cmd_capture_pause(args):
    """暂停抓包（会话保留，代理仍跑，区别于 stop）。"""
    res = _req("POST", "/capture/pause")
    data = _ok(res)
    emit_obj({"paused": True, "session_id": data.get("session_id"),
              "hint": "会话保留，代理仍跑。resume 恢复，stop 真正停止"})


def cmd_capture_resume(args):
    """恢复抓包记录。"""
    res = _req("POST", "/capture/resume")
    data = _ok(res)
    emit_obj({"resumed": True, "session_id": data.get("session_id")})


def cmd_sessions(args):
    """sessions 管理：list / show / delete。"""
    action = args.action
    if action == "list":
        res = _req("GET", "/sessions")
        data = _ok(res)
        sessions = data if isinstance(data, list) else data.get("sessions", [])
        emit_list(sessions, json_array=getattr(args, "json_array", False))
    elif action == "show":
        if not args.id:
            _die_arg("sessions show 需要 <id>")
        res = _req("GET", f"/sessions/{args.id}")
        emit_obj(_ok(res))
    elif action == "delete":
        if not args.id:
            _die_arg("sessions delete 需要 <id>")
        res = _req("DELETE", f"/sessions/{args.id}")
        emit_obj(_ok(res))
    else:
        _die_arg("sessions 需要: list | show | delete")


def _get_session(args) -> int:
    sid_val = getattr(args, "session", 0)
    if sid_val:
        return int(sid_val)
    # §2.2 TELNIX_SESSION 环境变量：agent 脚本固定一个 session 时可设此变量避免每条命令带 --session
    env_session = os.environ.get("TELNIX_SESSION", "").strip()
    if env_session:
        try:
            return int(env_session)
        except ValueError:
            _die_arg(f"TELNIX_SESSION 环境变量值无效: {env_session}（应为整数 session_id）")
    res = _req("GET", "/status")
    data = _ok(res)
    sid = data.get("session_id")
    if not sid:
        _die_arg("无活动会话，请先 capture start 或用 --session 指定（或设 TELNIX_SESSION 环境变量）")
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
    # §3.1 标签过滤
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
    # 客户端表达式过滤
    filters = []
    if args.filter:
        filters = parse_match(args.filter)["filters"]
        flows = [f for f in flows if flow_matches(f, filters)]
    if args.tail:
        _tail_flows(sid, flows, filters, args)
    else:
        # 批量解码器：每条流量输出 decoded 字段
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
        # 增量查询时输出 max_id 到 stderr 供 agent 记录（仅在显式 --since-id 时输出，避免污染普通 list）
        if args.since_id is not None and max_id:
            print(json.dumps({"max_id": max_id, "count": len(flows)}, ensure_ascii=False), file=sys.stderr)


def _tail_flows(sid: int, initial: list, filters: list, args):
    """流式追包：每隔 1s 拉取新流量，输出新流量。Ctrl+C 退出。"""
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
        # hex dump 模式
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
    # --decode plugin.py：加载自定义解码器对 body 解码
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
    """加载 Python 解码器插件，对 flow 指定字段解码。

    解码器接口：
        def decode(data: bytes, flow: dict) -> dict:
            return {"messages": [...], "fields": {...}}
    data 是 field（默认 response_body，可传 request_body）解码后的原始字节（base64: 前缀自动处理）。
    返回值会被原样输出到 flow["decoded"] 字段。
    """
    import importlib.util
    import base64 as _b64
    if not os.path.isfile(plugin_path):
        err_obj = {"ok": False, "error": f"解码器文件不存在: {plugin_path}"}
        print(json.dumps(err_obj, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    try:
        spec = importlib.util.spec_from_file_location("telnix_decoder", plugin_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("无法加载模块 spec")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        err_obj = {"ok": False, "error": f"解码器加载失败: {e}"}
        print(json.dumps(err_obj, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    if not hasattr(mod, "decode") or not callable(mod.decode):
        err_obj = {"ok": False, "error": "解码器缺少 def decode(data: bytes, flow: dict) -> dict 函数"}
        print(json.dumps(err_obj, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    # 取指定字段原始字节（默认 response_body，支持 request_body）
    body = flow.get(field) or ""
    if isinstance(body, str) and body.startswith("base64:"):
        try:
            data = _b64.b64decode(body[7:])
        except Exception as e:  # noqa: BLE001
            err_obj = {"ok": False, "error": f"base64 解码失败: {e}"}
            print(json.dumps(err_obj, ensure_ascii=False), file=sys.stderr)
            sys.exit(1)
    elif isinstance(body, str):
        data = body.encode("utf-8", errors="replace")
    elif isinstance(body, bytes):
        data = body
    else:
        data = b""
    try:
        result = mod.decode(data, flow)
        if not isinstance(result, dict):
            return {"value": result}
        return result
    except Exception as e:  # noqa: BLE001
        err_obj = {"ok": False, "error": f"解码器执行失败: {e}"}
        print(json.dumps(err_obj, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


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
        _die_arg("需要 <id> 或 --ids")


def cmd_packets_search(args):
    # --all 跨会话搜索（session_id=0）；否则当前会话
    if getattr(args, "all", False):
        sid = 0
    else:
        sid = _get_session(args)
    body = {"session_id": sid}
    if args.body_regex:
        body["body_regex"] = args.body_regex
    if args.binary_hex:
        body["binary_hex"] = args.binary_hex
    # §3.1 多条件组合搜索（与 body_regex/binary_hex 是 AND 关系）
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
    # §3.14 hex 偏移范围搜索：解析 "START:END" 格式
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
            _die_arg(f"--offset 格式无效：{offset_str}（应为 START:END，如 0:1024）")
    body["limit"] = args.limit
    res = _req("POST", "/flows/search", body)
    data = _ok(res)
    flows = data.get("matches", []) if isinstance(data, dict) else data
    emit_list(flows, json_array=args.json_array)


def cmd_packets_tag(args):
    """§3.1 流量标签管理：--add/--remove/--clear 操作 tags，--note 设置备注，--clear-note 清除备注。

    用法：
        packets tag <id> --add analyzed
        packets tag <id> --remove suspicious
        packets tag <id> --clear
        packets tag <id> --note "备注内容"
        packets tag <id> --clear-note
        packets tag <id> --add analyzed --note "已分析"
        packets tag --list              # 列出全局所有标签及每标签的 flow 数（§4.2）
    """
    # §4.2 全局标签列表
    if getattr(args, "list", False):
        res = _req("GET", "/flows/tags")
        data = _ok(res)
        tags = data.get("tags", []) if isinstance(data, dict) else data
        emit_list(tags, json_array=getattr(args, "json_array", False))
        return
    flow_id = args.id
    if not flow_id:
        _die_arg("tag 需要 <id> 或 --list")
    # 先拉当前 flow 获取现有 tags
    res = _req("GET", f"/flows/{flow_id}")
    flow = _ok(res)
    if not isinstance(flow, dict):
        _die_arg("无法获取 flow")

    existing_tags_str = flow.get("tags") or ""
    existing_tags = [t.strip() for t in existing_tags_str.split(",") if t.strip()]
    note = getattr(args, "note", None)
    clear_note = getattr(args, "clear_note", False)

    if getattr(args, "clear", False):
        # 清空所有标签
        new_tags = []
    elif getattr(args, "add", ""):
        # 添加标签（去重）
        add_tag = args.add.strip()
        if add_tag and add_tag not in existing_tags:
            existing_tags.append(add_tag)
        new_tags = existing_tags
    elif getattr(args, "remove", ""):
        # 移除标签
        rm_tag = args.remove.strip()
        new_tags = [t for t in existing_tags if t != rm_tag]
    else:
        # 无操作参数：只更新 note（若有）或显示当前标签
        if note is None and not clear_note:
            emit_obj({"flow_id": flow_id, "tags": existing_tags_str,
                      "tag_note": flow.get("tag_note") or ""})
            return
        new_tags = existing_tags

    new_tags_str = ",".join(new_tags)
    # 调 PATCH /flows/{id}/tags
    body = {"tags": new_tags_str}
    if note is not None:
        body["tag_note"] = note
    elif clear_note:
        # --clear-note 显式清空备注（传空字符串覆盖）
        body["tag_note"] = ""
    res = _req("PATCH", f"/flows/{flow_id}/tags", body)
    data = _ok(res)
    if isinstance(data, dict):
        emit_obj(data)
    else:
        emit_obj({"flow_id": flow_id, "tags": new_tags_str,
                  "tag_note": body.get("tag_note", flow.get("tag_note") or "")})


def cmd_packets_list_all(args):
    """跨会话查询所有流量（不依赖活动会话）。"""
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
    # §3.1 标签过滤
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
    # 客户端表达式过滤
    if args.filter:
        filters = parse_match(args.filter)["filters"]
        flows = [f for f in flows if flow_matches(f, filters)]
    # 批量解码器
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
    """跨会话清理流量。--all 清空全部，--before-id N 删除 id<N 的旧流量。"""
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
        _die_arg("packets clear 需要 --all 或 --before-id N")


def cmd_packets_export(args):
    """单 flow 导出为 curl/python-requests/postman/csv 等格式。"""
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
        # 单行 CSV（带表头）
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
    """单 flow 转 python-requests 脚本。二进制 body 用 base64.b64decode。

    Windows 上若指定 output_file，则把二进制 body 写到 <stem>_body.bin，
    脚本里改用 open('<stem>_body.bin','rb') 读取，避免内联大段 base64。
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
                # Windows + 文件模式：写 <stem>_body.bin，脚本用 open() 读取
                stem = os.path.splitext(output_file)[0]
                bin_path = f"{stem}_body.bin"
                try:
                    import base64 as _b64
                    raw = _b64.b64decode(b64)
                    with open(bin_path, "wb") as bf:
                        bf.write(raw)
                except Exception:  # noqa: BLE001
                    pass
                lines.append(f"with open({bin_path!r}, 'rb') as _f:  # 二进制 body 从外部文件读取")
                lines.append(f"    data = _f.read()")
            else:
                lines.append(f"data = base64.b64decode({b64!r})  # 二进制 body")
            lines.append(f"resp = requests.{method.lower()}(url, headers=headers, data=data, timeout=30, verify=False)")
        else:
            # 尝试 JSON
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
    """单 flow 转 Postman Collection v2.1 单 item。二进制 body 用 raw + base64 标记。

    Windows 上若指定 output_file，则把二进制 body 写到 <stem>_body.bin，
    Postman 用 file 模式引用该文件。
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
                # Windows + 文件模式：写 <stem>_body.bin，Postman file 模式引用
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
                    "description": "binary body 从外部文件读取（Windows 兼容）",
                }
            else:
                # Postman raw 模式不支持二进制，用 base64 字符串 + 说明
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
    # content_type/process 分组走后端 /flows/stats（跨会话全量统计，不依赖活动会话）
    if by in ("content_type", "process"):
        res = _req("GET", f"/flows/stats?group_by={by}")
        data = _ok(res)
        emit_obj(data)
        return
    sid = _get_session(args)
    res = _req("GET", f"/sessions/{sid}/stats")
    data = _ok(res)
    # --by endpoint：path 模板归一化分组
    if by == "endpoint":
        # 拉流量做 path 归一化（支持 --limit 0 拉全量）
        flows = _fetch_flows_for_analysis(args, default_limit=2000)
        endpoints: dict[str, dict] = {}
        for f in flows:
            if not isinstance(f, dict):
                continue
            tpl, qkeys = _path_template(f.get("path") or "",
                                         keep_query=getattr(args, "keep_query", False))
            method = f.get("method") or "-"
            key = f"{method} {f.get('host') or ''}{tpl}"
            ep = endpoints.setdefault(key, {"method": method, "host": f.get("host"),
                                             "path_template": tpl, "count": 0,
                                             "status_set": [], "sample_ids": [],
                                             "query_keys": qkeys})
            ep["count"] += 1
            sc = f.get("status_code")
            if sc and sc not in ep["status_set"]:
                ep["status_set"].append(sc)
            if len(ep["sample_ids"]) < 3:
                ep["sample_ids"].append(f.get("id"))
            for qk in qkeys:
                if qk not in ep["query_keys"]:
                    ep["query_keys"].append(qk)
        out = {"by": "endpoint", "endpoints": list(endpoints.values())}
        # --metrics size,duration
        metrics = (getattr(args, "metrics", "") or "").lower()
        if metrics:
            for ep in out["endpoints"]:
                ids = ep["sample_ids"]
                sizes, durs = [], []
                for f in flows:
                    if f.get("id") in ids:
                        if f.get("size") is not None:
                            sizes.append(f["size"])
                        if f.get("duration_ms") is not None:
                            durs.append(f["duration_ms"])
                ep["size"] = _percentiles(sizes)
                ep["duration_ms"] = _percentiles(durs)
        emit_obj(out)
        return
    # --metrics：在原有分组基础上附加 size/duration 分布
    metrics = (getattr(args, "metrics", "") or "").lower()
    if metrics:
        flows = _fetch_flows_for_analysis(args, default_limit=2000)
        data["metrics"] = {
            "size": _percentiles([f.get("size") or 0 for f in flows if isinstance(f, dict)]),
            "duration_ms": _percentiles([f.get("duration_ms") or 0 for f in flows if isinstance(f, dict)]),
        }
    emit_obj(data)


def cmd_packets_overview(args):
    """多维聚合统计概览（CoolUI 仪表盘数据源，跨会话全量）。"""
    res = _req("GET", "/flows/overview")
    data = _ok(res)
    emit_obj(data)


def _percentiles(values: list) -> dict:
    """计算 p50/p95/max/min。"""
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
    """path 模板归一化：把数字、UUID、长 hex 段替换为 {id}。
    返回 (template, query_keys)。keep_query=True 时 template 保留 ?k1&k2 形式。
    """
    import re
    if not path:
        return "/", []
    # 分离 path 和 query
    raw_path, _, query_str = path.partition("?")
    segs = raw_path.split("/")
    out = []
    for seg in segs:
        if not seg:
            out.append("")
            continue
        # 纯数字
        if re.match(r"^\d+$", seg):
            out.append("{id}")
        # UUID
        elif re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", seg):
            out.append("{uuid}")
        # 长 hex（>=16）
        elif re.match(r"^[0-9a-fA-F]{16,}$", seg):
            out.append("{hex}")
        # 长 base64-ish（>=20，含字母数字）
        elif len(seg) >= 24 and re.match(r"^[A-Za-z0-9_-]+$", seg):
            out.append("{token}")
        else:
            out.append(seg)
    tpl = "/".join(out)
    # query keys 去重
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
    """对比两条流量的指定字段，输出 unified diff。"""
    import difflib
    res1 = _req("GET", f"/flows/{args.id1}")
    f1 = _ok(res1)
    res2 = _req("GET", f"/flows/{args.id2}")
    f2 = _ok(res2)
    field = args.field
    v1 = (f1.get(field) if isinstance(f1, dict) else "") or ""
    v2 = (f2.get(field) if isinstance(f2, dict) else "") or ""
    # JSON 字段尝试格式化便于 diff
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
    """endpoints/timeline/stats 公共拉取逻辑：支持 --session N、--limit 0（不限）、--host 过滤。"""
    limit = getattr(args, "limit", default_limit) or default_limit
    # --limit 0 表示不限，但后端需要一个大数；用 50000 作为实际上限
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
    """唯一 endpoint 提取（path 模板归一化，画 API 地图）。
    支持 --session N 只看指定会话，--limit 0 拉全量，--keep-query 保留 query 参数名，
    --sample-strategy first/last/random 控制 sample_ids 采样策略。
    """
    flows = _fetch_flows_for_analysis(args, default_limit=2000)
    keep_query = getattr(args, "keep_query", False)
    sample_strategy = getattr(args, "sample_strategy", "first") or "first"
    # 先按 endpoint 分组，收集所有 flow id（不截断）
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
    # 采样策略：first（前 3，默认）/ last（后 3）/ random（随机 3）
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
    """流量时间线：按时间排序，标注大间隔段落。
    支持 --session N 只看指定会话，--limit 0 拉全量。
    """
    flows = _fetch_flows_for_analysis(args, default_limit=2000)
    # 按时间升序
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
    # 解析时间戳算间隔
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
    """定向 tail：阻塞输出匹配过滤表达式的新流量（NDJSON），Ctrl+C 退出。
    内部用 --since-id 轮询 + 客户端 filter，比 agent 自己写轮询方便。
    """
    sid = _get_session(args) if not args.session else args.session
    filters = parse_match(args.filter)["filters"]
    # 初始化 last_max_id：拉一次当前最大 id，只看之后的新流量
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
    """请求依赖链 trace：从指定 flow 的响应 body 提取字符串值（JSON 字段值或正则匹配的
    token/session_id/uid 等），在后续流量的 request_headers/request_body 里搜索，
    输出依赖链 [{source_flow, target_flow, field, value}]（NDJSON）。

    --all 时跨会话扫描（/flows/all?since_id=N），否则只在当前会话内扫描。
    """
    # 1. 拉源 flow
    res = _req("GET", f"/flows/{args.id}")
    src = _ok(res)
    if not isinstance(src, dict):
        _die_arg("无法获取源 flow")

    # 2. 提取响应中的字符串值（--min-length 过滤短串减少误报）
    min_len = getattr(args, "min_length", 0) or 0
    values_to_track = _extract_trace_values(src.get("response_body") or "", min_length=min_len)
    if not values_to_track:
        emit_obj({"traced": True, "source_flow": args.id, "dependencies": [],
                  "hint": "源 flow 响应无可追踪字符串"})
        return

    # 3. 拉后续流量：--all 跨会话用 /flows/all，否则当前会话 /sessions/{sid}/flows
    limit = getattr(args, "limit", 500) or 500
    if getattr(args, "all", False):
        # 跨会话扫描
        res = _req("GET", f"/flows/all?limit={limit}&offset=0&since_id={args.id}")
    else:
        # 当前会话扫描
        sid = _get_session(args)
        res = _req("GET", f"/sessions/{sid}/flows?limit={limit}&offset=0&since_id={args.id}")
    data = _ok(res)
    flows = data.get("flows", []) if isinstance(data, dict) else data

    # 4. 客户端字符串搜索：在后续流量的 request_headers/request_body 里找
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
    # NDJSON 输出
    for d in deps:
        print(json.dumps(d, ensure_ascii=False))


def _extract_trace_values(body: str, min_length: int = 4) -> list[dict]:
    """从响应 body 提取可追踪字符串值：JSON 字段值 + 正则匹配 token/session_id/uid 等。

    min_length 过滤短串减少误报（默认 4，可用 --min-length 提高）。
    """
    out = []
    seen = set()
    if not body or body.startswith("base64:"):
        return out  # 二进制 body 无法直接提取字符串
    # 1. JSON 字段值（仅叶子字符串，长度 >= min_length 过滤短串减少误报）
    try:
        obj = json.loads(body)
        for path, val in _walk_json_leaves(obj):
            if isinstance(val, str) and len(val) >= min_length and val not in seen:
                seen.add(val)
                out.append({"field": path, "value": val})
    except Exception:  # noqa: BLE001
        pass
    # 2. 正则匹配常见 token 模式（即便非 JSON 也能提取）
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
    """递归遍历 JSON，输出 (path, leaf_value)。叶子为非 dict/list 类型。"""
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
    """签名字段自动检测：拉取多条同接口请求，解析 JSON body 找所有叶子字段，
    对比同字段在多次请求中的值——长度固定+字符集受限+每次都不同=可疑签名字段。
    输出 [{field_path, lengths, charsets, varies, sample_values, suspicion_score(0-1)}]（NDJSON）。

    --all 时跨会话扫描：以第一个 ID 为源，从 /flows/all?since_id=N 拉后续流量。
    否则只拉显式指定的 --ids（按 /flows/{fid} 逐条获取，已跨会话）。
    """
    flow_ids = getattr(args, "ids", None) or []
    use_all = getattr(args, "all", False)

    # --all 模式：需要至少 1 个源 ID，从 /flows/all 拉后续流量
    if use_all:
        if not flow_ids:
            _die_arg("analyze --all 至少需要 1 个源 flow ID")
        src_id = flow_ids[0]
        limit = getattr(args, "limit", 500) or 500
        res = _req("GET", f"/flows/all?limit={limit}&offset=0&since_id={src_id}")
        data = _ok(res)
        flows = data.get("flows", []) if isinstance(data, dict) else data
        # 确保源 flow 也包含在内
        try:
            res_src = _req("GET", f"/flows/{src_id}")
            src_flow = _ok(res_src)
            if isinstance(src_flow, dict):
                flows = [src_flow] + [f for f in flows if f.get("id") != src_id]
        except SystemExit:
            pass
    else:
        if len(flow_ids) < 2:
            _die_arg("analyze 至少需要 2 个 flow ID（用 --find-signature 做签名字段检测）")
        # 拉所有 flow（单条失败不阻塞其他）
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
        _die_arg("成功拉取的 flow 不足 2 条，无法对比")

    # 收集所有 flow 的 {field_path: [values]}
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

    # 分析每个字段
    findings = []
    for path, vals in field_values.items():
        if len(vals) < 2:
            continue  # 只出现一次无法对比
        str_vals = [str(v) for v in vals]
        lengths = sorted(set(len(v) for v in str_vals))
        charsets = sorted(set(_classify_charset(v) for v in str_vals))
        varies = len(set(str_vals)) > 1
        sample_values = str_vals[:5]
        # 可疑评分：varies 是必要条件，叠加 length 固定 + charset 受限
        score = 0.0
        if varies:
            score = 0.34  # 必要条件基础分
            if len(lengths) == 1:
                score += 0.33  # 长度固定
            if len(charsets) == 1 and charsets[0] in ("hex", "base64", "alphanumeric"):
                score += 0.33  # 字符集受限
        findings.append({
            "field_path": path,
            "lengths": lengths,
            "charsets": charsets,
            "varies": varies,
            "sample_values": sample_values,
            "suspicion_score": round(min(1.0, score), 2),
        })
    # 按可疑分数倒序输出
    findings.sort(key=lambda x: -x["suspicion_score"])
    for f in findings:
        print(json.dumps(f, ensure_ascii=False))


def _classify_charset(s: str) -> str:
    """分类字符串字符集（按最严格匹配，用于签名字段检测）。"""
    if not s:
        return "empty"
    import re
    # 纯数字优先（最严格）
    if re.match(r"^\d+$", s):
        return "numeric"
    # hex（0-9a-f，至少含一个字母）
    if re.match(r"^[0-9a-fA-F]+$", s) and re.search(r"[a-fA-F]", s):
        return "hex"
    # base64 字符集（含 +/= 才算严格 base64）
    if re.match(r"^[A-Za-z0-9+/=]+$", s):
        return "base64"
    # 字母数字下划线短横
    if re.match(r"^[A-Za-z0-9_\-]+$", s):
        return "alphanumeric"
    # 纯字母
    if re.match(r"^[A-Za-z]+$", s):
        return "alpha"
    return "mixed"


def cmd_intercept_export(args):
    """导出所有规则到 JSON 文件。"""
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
    """从 JSON 文件导入规则。merge=追加，replace=先清空再导入。
    输出每条导入结果（成功/失败+原因），便于 agent 定位失败规则。
    """
    if not os.path.isfile(args.file):
        _die_arg(f"文件不存在: {args.file}")
    try:
        with open(args.file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        _die_arg(f"读取失败: {e}")
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        _die_arg("文件格式错误：缺少 rules 数组")
    # replace 模式：先清空所有现有规则
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
    # 逐条导入，记录每条结果
    created = 0
    results = []
    for idx, r in enumerate(rules):
        if not isinstance(r, dict):
            results.append({"index": idx, "ok": False, "error": "非对象"})
            continue
        body = {k: v for k, v in r.items() if k != "id"}
        body["enabled"] = r.get("enabled", True)
        try:
            res = _req("POST", "/auto-reply/rules", body)
            created_r = _ok(res)
            created += 1
            results.append({"index": idx, "ok": True, "rule_id": created_r.get("id") if isinstance(created_r, dict) else None,
                            "pattern": body.get("pattern", "")})
        except SystemExit as e:  # noqa: BLE001  _ok 失败会 sys.exit
            results.append({"index": idx, "ok": False, "error": "API 调用失败",
                            "pattern": body.get("pattern", "")})
        except Exception as e:  # noqa: BLE001
            results.append({"index": idx, "ok": False, "error": str(e),
                            "pattern": body.get("pattern", "")})
    emit_obj({"imported": True, "mode": args.mode, "created": created, "deleted_old": deleted,
              "failed": len(rules) - created, "total_in_file": len(rules),
              **({} if getattr(args, "quiet", False) else {"results": results})})


def cmd_intercept_toggle(args):
    """启用/禁用规则（不删除）。支持单条或批量 --all。"""
    if getattr(args, "all", False):
        # 批量操作
        res = _req("GET", "/auto-reply/rules")
        rules = _ok(res)
        rules = rules if isinstance(rules, list) else []
        ids = [r.get("id") for r in rules if isinstance(r, dict) and r.get("id")]
        if not ids:
            emit_obj({"toggled": 0, "total": 0, "hint": "无规则"})
            return
        # --enable / --disable 决定目标状态
        if args.enable:
            enabled = True
        elif args.disable:
            enabled = False
        else:
            _die_arg("批量 toggle 需要 --enable 或 --disable")
        res = _req("POST", "/auto-reply/rules/batch-update", {"ids": ids, "enabled": enabled})
        _ok(res)
        emit_obj({"toggled": len(ids), "total": len(ids), "enabled": enabled})
        return
    # 单条
    if not args.id:
        _die_arg("intercept toggle 需要 <rule_id> 或 --all")
    res = _req("GET", "/auto-reply/rules")
    rules = _ok(res)
    rules = rules if isinstance(rules, list) else []
    target = None
    for r in rules:
        if isinstance(r, dict) and str(r.get("id")) == str(args.id):
            target = r
            break
    if not target:
        _die_arg(f"规则不存在: {args.id}")
    if args.enable:
        new_enabled = True
    elif args.disable:
        new_enabled = False
    else:
        # 切换当前状态
        new_enabled = not target.get("enabled", True)
    body = dict(target)
    body["enabled"] = new_enabled
    # 去掉 id（PUT 路径里带）
    body.pop("id", None)
    res = _req("PUT", f"/auto-reply/rules/{args.id}", body)
    _ok(res)
    emit_obj({"toggled": True, "rule_id": args.id, "enabled": new_enabled})


def cmd_intercept_update(args):
    """修改现有规则（不删除重建）。支持 --match/--action/--enable/--disable/--note。"""
    if not args.id:
        _die_arg("intercept update 需要 <rule_id>")
    res = _req("GET", "/auto-reply/rules")
    rules = _ok(res)
    rules = rules if isinstance(rules, list) else []
    target = None
    for r in rules:
        if isinstance(r, dict) and str(r.get("id")) == str(args.id):
            target = r
            break
    if not target:
        _die_arg(f"规则不存在: {args.id}")
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
    """进程列表（可选连接快照/进程树）。后端 /processes/snapshot 扩展。

    子命令（可选）：
        processes                       列出有网络连接的进程（默认）
        processes ignore --pid N --name X   忽略进程（pid 为空时按名称忽略，可添加多个）
        processes unignore <row_id>     取消忽略进程
        processes ignored               列出已忽略进程
        processes ignore-host <pattern> 添加忽略 host 通配符（如 *.example.com）
        processes unignore-host <id>    取消忽略 host
        processes ignored-hosts         列出已忽略 host
    """
    action = getattr(args, "action", "") or ""
    if action == "ignore":
        body = {"pid": args.pid, "name": args.name or ""}
        if body["pid"] is None and not body["name"]:
            _die_arg("processes ignore 需要 --pid 或 --name")
        if body["pid"] is None:
            body["pid"] = None  # 显式 null，按名称忽略
        res = _req("POST", "/processes/ignore", body)
        emit_obj(_ok(res))
        return
    if action == "unignore":
        if not args.row_id:
            _die_arg("processes unignore 需要 <row_id>")
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
            _die_arg("processes ignore-host 需要 <host>")
        res = _req("POST", "/processes/ignore-host", {"host": args.host})
        emit_obj(_ok(res))
        return
    if action == "unignore-host":
        if not args.row_id:
            _die_arg("processes unignore-host 需要 <id>")
        res = _req("DELETE", f"/processes/ignore-host/{args.row_id}")
        emit_obj(_ok(res))
        return
    if action == "ignored-hosts":
        res = _req("GET", "/processes/ignored-hosts")
        data = _ok(res)
        items = data if isinstance(data, list) else data.get("items", data.get("hosts", []))
        emit_list(items, json_array=getattr(args, "json_array", False))
        return
    # 默认：列出进程（保持兼容，无子命令时走原逻辑）
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
        # 回退到普通 /processes
        res = _req("GET", "/processes")
        data = _ok(res)
    procs = data if isinstance(data, list) else data.get("processes", [])
    if getattr(args, "name", ""):
        nl = args.name.lower()
        procs = [p for p in procs if isinstance(p, dict) and nl in (p.get("name") or "").lower()]
    emit_list(procs, json_array=getattr(args, "json_array", False))


def _agent_backup_path() -> str:
    """agent 工作区备份文件路径（系统临时目录，跨重启保留）。"""
    import tempfile
    return os.path.join(tempfile.gettempdir(), "telnix_agent_workspace_backup.json")


# agent start 时自动加入忽略列表的进程（防止 agent 抓自己的包）
AGENT_IGNORE_PROCESSES = ["TRAE SOLO CN.exe"]


def cmd_agent(args):
    """agent 工作模式：start 保存原状并禁用规则/focus/断点；end 恢复；status 查询。

    场景：agent 接管会话前先 `agent start`，临时关闭所有影响抓包/拦截的因素
    （自动回复规则、专注模式、断点），并把 agent 自身进程加入忽略列表（防止抓自己包），
    做完事再 `agent end` 恢复用户原状。

    备份文件：系统临时目录下 `telnix_agent_workspace_backup.json`，跨进程保留。
    重复 start 会报错（避免覆盖未恢复的备份）；未 start 就 end 也会报错。
    """
    action = args.action
    backup_path = _agent_backup_path()

    if action == "start":
        # 1. 防覆盖：已有备份必须先 end
        if os.path.exists(backup_path):
            _die_arg("已有未恢复的 agent 工作区备份，请先 `agent end` 恢复后再 `agent start`")

        # 2. 保存当前 snapshot（规则 + focus + 断点）
        res = _req("GET", "/snapshot")
        snapshot = _ok(res)

        # 3. 保存当前忽略进程列表，并添加 agent 自身进程到忽略列表
        ignored_res = _req("GET", "/processes/ignored")
        ignored_before = _ok(ignored_res) or []
        # 记录原本就忽略的进程名（用于 end 时识别哪些是 agent 加的）
        # 字段名是 process_name（不是 name）
        ignored_names_before = set()
        for item in ignored_before:
            if isinstance(item, dict):
                n = item.get("process_name") or item.get("name")
                if n:
                    ignored_names_before.add(n.lower())

        agent_added_ignored = []
        for proc_name in AGENT_IGNORE_PROCESSES:
            if proc_name.lower() not in ignored_names_before:
                try:
                    _ok(_req("POST", "/processes/ignore", {"name": proc_name}))
                    agent_added_ignored.append(proc_name)
                except Exception:  # noqa: BLE001
                    pass

        # 把 agent 新增的忽略进程记入备份，end 时移除
        snapshot["agent_added_ignored_processes"] = agent_added_ignored

        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

        # 4. 禁用所有规则
        rules = snapshot.get("rules", []) or []
        rule_ids = [r.get("id") for r in rules if r.get("id")]
        rules_disabled = 0
        if rule_ids:
            r = _req("POST", "/auto-reply/rules/batch-update",
                     {"ids": rule_ids, "enabled": False})
            _ok(r)
            rules_disabled = len(rule_ids)

        # 5. 关闭 focus
        _req("POST", "/focus", {
            "enabled": False, "pids": [], "hosts": [],
            "methods": [], "status_codes": [], "content_types": [],
        })
        _ok(_req("POST", "/focus", {"enabled": False, "pids": []}))

        # 6. 关闭断点（请求 + 响应）
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
            "hint": "工作区已清空：规则已禁用、focus 已关闭、断点已关闭、agent 进程已加入忽略列表。"
                    "完成工作后请调 `agent end` 恢复原状。",
        })

    elif action == "end":
        if not os.path.exists(backup_path):
            _die_arg("没有未恢复的 agent 工作区备份（可能已 agent end 或从未 agent start）")

        with open(backup_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)

        # 1. 一次 POST /snapshot 恢复 rules + focus + breakpoint（clear_rules=true 先清空再导入，
        # 避免 agent 工作期间新建的规则残留）
        restore_body = {
            "rules": snapshot.get("rules", []) or [],
            "focus": snapshot.get("focus", {}) or {},
            "breakpoint": snapshot.get("breakpoint", {}) or {},
            "clear_rules": True,
        }
        res = _req("POST", "/snapshot", restore_body)
        result = _ok(res)

        # 2. 移除 agent start 时添加的忽略进程（保留用户原本就忽略的）
        agent_added = snapshot.get("agent_added_ignored_processes", []) or []
        ignored_removed = 0
        if agent_added:
            # 拉取当前忽略列表，匹配 agent 添加的进程名找 row_id 删除
            # 字段名是 process_name（不是 name）
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

        # 3. 删除备份文件
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
                "工作区已恢复到 agent start 之前的状态。"
                "注意：自动修改规则需要 Telnix 运行才生效，因此系统代理未关闭、Telnix 未退出。"
                "请询问用户是否关闭 Telnix；用户同意后再调 `system quit`（后端退出时会自动清理系统代理）。"
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
                    "hint": "工作区已清空，调 `agent end` 恢复。",
                })
            except Exception as e:  # noqa: BLE001
                _die_arg(f"备份文件损坏，无法读取: {e}")
        else:
            emit_obj({
                "agent_workspace": "inactive",
                "hint": "没有活跃的 agent 工作区（可调 `agent start` 开始）。",
            })


def cmd_settings_get(args):
    """读取所有设置或单个 key。"""
    res = _req("GET", "/settings")
    data = _ok(res)
    if args.key:
        data = {"key": args.key, "value": data.get(args.key)}
    emit_obj(data)
    if not args.json and not args.key:
        print("[Telnix] 当前全部设置（key=value）：", file=sys.stderr)
        if isinstance(data, dict):
            for k in sorted(data.keys()):
                print(f"  {k} = {data[k]}", file=sys.stderr)
    elif not args.json and args.key:
        print(f"[Telnix] {args.key} = {data.get('value')}", file=sys.stderr)


def cmd_settings_set(args):
    """写入单个设置项。bool/list/dict 自动反序列化。"""
    import json as _json
    value: Any = args.value
    # 尝试 JSON 反序列化（支持 true/false/null/数字/list/dict）
    try:
        parsed = _json.loads(args.value)
        if isinstance(parsed, (bool, int, float, list, dict)) or parsed is None:
            value = parsed
    except (ValueError, TypeError):
        pass  # 保持字符串
    body = {args.key: value}
    res = _req("PUT", "/settings", body=body)
    data = _ok(res)
    emit_obj(data)
    if not args.json:
        print(f"[Telnix] 设置已更新：{args.key} = {value!r}", file=sys.stderr)
        print("[Telnix] 提示：代理引擎、断点等部分设置需重启后端才生效。"
              "可执行 `telnix system restart` 重启。", file=sys.stderr)


def cmd_settings_engine(args):
    """查看/切换代理引擎。"""
    VALID = {"builtin", "async", "mitmproxy"}
    # 先取当前状态
    res = _req("GET", "/settings")
    data = _ok(res)
    current = data.get("proxy_engine") or "builtin"
    mitm_available = bool(data.get("mitmproxy_available"))

    if not args.name:
        # 仅查看
        emit_obj({
            "proxy_engine": current,
            "mitmproxy_available": mitm_available,
            "available_engines": sorted(VALID),
            "hint": "切换引擎: telnix settings engine <name>; 切换后需执行 telnix system restart",
        })
        if not args.json:
            print(f"[Telnix] 当前代理引擎: {current}", file=sys.stderr)
            print(f"[Telnix] mitmproxy 可用: {'是' if mitm_available else '否'}"
                  f"（未安装可执行 telnix system install-dep）", file=sys.stderr)
            print("[Telnix] 可选引擎: builtin（默认线程）/ async（asyncio）"
                  "/ mitmproxy（需 pip install mitmproxy）", file=sys.stderr)
            print("[Telnix] 切换示例: telnix settings engine async", file=sys.stderr)
            print("[Telnix] 切换后需执行: telnix system restart", file=sys.stderr)
        return

    name = args.name.lower().strip()
    if name not in VALID:
        _die_arg(f"不支持的代理引擎: {name}（可选: {', '.join(sorted(VALID))}）")

    # mitmproxy 引擎需先确认已安装
    if name == "mitmproxy" and not mitm_available:
        emit_obj({
            "ok": False,
            "error": f"mitmproxy 未安装，无法切换到该引擎",
            "hint": "先执行 `telnix system install-dep` 安装 mitmproxy，再切换引擎",
        })
        if not args.json:
            print(f"[Telnix] 错误：mitmproxy 未安装，无法切换到该引擎", file=sys.stderr)
            print("[Telnix] 修复建议：先执行 `telnix system install-dep` 安装 mitmproxy，再切换引擎",
                  file=sys.stderr)
        sys.exit(1)

    # 写入设置
    res = _req("PUT", "/settings", body={"proxy_engine": name})
    data = _ok(res)
    emit_obj({
        "ok": True,
        "proxy_engine": name,
        "previous": current,
        "mitmproxy_available": mitm_available,
        "hint": "需重启后端才生效：telnix system restart",
    })
    if not args.json:
        print(f"[Telnix] 代理引擎已切换: {current} → {name}", file=sys.stderr)
        print(f"[Telnix] mitmproxy 可用: {'是' if mitm_available else '否'}", file=sys.stderr)
        print("[Telnix] 重要：需重启后端才生效，执行: telnix system restart", file=sys.stderr)


def cmd_system(args):
    """系统控制：restart / quit / restart-as-admin。

    - restart：同进程内 os.execv 重启前后端
    - quit：退出 Telnix（清系统代理 + sys.exit）
    - restart-as-admin：以管理员身份重启（UAC 提权，用于 TCP/UDP 抓包等需管理员的功能）

    跨平台说明：restart-as-admin / firewall-allow / firewall-status 是 Windows 专属功能
    （UAC 提权 / netsh 防火墙），非 Windows 平台打印"不支持"并跳过。
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
        # Windows 专属：UAC 提权。非 Windows 平台打印"不支持"并退出
        if not sys.platform.startswith("win"):
            print("[Telnix] restart-as-admin 仅 Windows 支持（UAC 提权），"
                  "macOS/Linux 请用 sudo 手动以 root 身份运行", file=sys.stderr)
            sys.exit(1)
        # 走 GUI 用户确认流程：创建 pending 请求 → 长轮询等待响应
        # 用户同意 → 后端真正弹 UAC 提权 → CLI 收到 accepted
        # 用户拒绝 → CLI 收到 rejected
        # 60s 内无响应 → CLI 自动重试一次，仍无响应则超时失败
        res = _req("POST", "/system/request-admin-restart", timeout=10.0)
        data = _ok(res)
        rid = data.get("request_id")
        if not rid:
            _die_arg("后端未返回 request_id")
        # 长轮询：最多重试 3 次（每次 60s），覆盖 3 分钟窗口
        final_status = None
        final_msg = None
        for _ in range(3):
            r = _req("GET", f"/system/admin-request/{rid}/wait", timeout=65.0)
            if r.get("code") != 0:
                # 请求不存在或已处理
                _ok(r)  # _ok 会 die 并打印错误
                return
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
            # status == "pending"，继续下一轮
        if final_status is None:
            _die_arg("等待用户响应超时（3 分钟无响应）")
        if final_status == "accepted":
            emit_obj({"restarting": True, "as_admin": True, "approved": True, "message": final_msg})
        else:
            # 用户拒绝：返回结构化错误，方便 agent 识别
            err_obj = {
                "ok": False,
                "error": final_msg,
                "rejected_by_user": True,
                "hint": "用户拒绝了管理员重启请求。可在 GUI 中手动重启，或请用户同意后重试",
            }
            print(json.dumps(err_obj, ensure_ascii=False), file=sys.stderr)
            sys.exit(1)
    elif action == "firewall-allow":
        # 防火墙放行 8888（代理）和 18901（API）端口，手机抓包必备
        # 需要 administrator 权限，失败时给出清晰提示
        # Windows 专属：用 netsh 配置 Windows 防火墙。非 Windows 平台打印"不支持"
        if not sys.platform.startswith("win"):
            print("[Telnix] firewall-allow 仅 Windows 支持（netsh 防火墙），"
                  "macOS/Linux 请用 ufw/firewall-cmd/系统偏好设置手动放行端口", file=sys.stderr)
            sys.exit(1)
        import subprocess
        rules = [
            ("Telnix-Proxy-8888", 8888, "TCP"),
            ("Telnix-API-18901", 18901, "TCP"),
        ]
        results = []
        failed = False
        for name, port, proto in rules:
            cmd = [
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={name}",
                f"dir=in", f"action=allow", f"protocol={proto}",
                f"localport={port}",
            ]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    results.append({"rule": name, "port": port, "ok": True})
                else:
                    results.append({"rule": name, "port": port, "ok": False,
                                    "error": (r.stderr or r.stdout or "").strip()})
                    failed = True
            except Exception as e:  # noqa: BLE001
                results.append({"rule": name, "port": port, "ok": False, "error": str(e)})
                failed = True
        emit_obj({
            "firewall_allow": not failed,
            "rules": results,
            "hint": "防火墙规则已添加" if not failed else
                    "部分规则添加失败。请以管理员身份运行：telnix system firewall-allow",
        })
        if failed:
            sys.exit(1)
    elif action == "firewall-status":
        # 查询 Telnix 相关防火墙规则是否存在
        # Windows 专属：netsh 防火墙查询。非 Windows 平台打印"不支持"
        if not sys.platform.startswith("win"):
            print("[Telnix] firewall-status 仅 Windows 支持（netsh 防火墙），"
                  "macOS/Linux 请用 ufw status/firewall-cmd --list-ports 查看防火墙状态", file=sys.stderr)
            sys.exit(1)
        import subprocess
        cmd = ["netsh", "advfirewall", "firewall", "show", "rule",
               "name=Telnix-Proxy-8888"]
        try:
            r1 = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except Exception:  # noqa: BLE001
            r1 = None
        cmd = ["netsh", "advfirewall", "firewall", "show", "rule",
               "name=Telnix-API-18901"]
        try:
            r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except Exception:  # noqa: BLE001
            r2 = None
        proxy_ok = bool(r1 and r1.returncode == 0 and "Telnix-Proxy-8888" in (r1.stdout or ""))
        api_ok = bool(r2 and r2.returncode == 0 and "Telnix-API-18901" in (r2.stdout or ""))
        emit_obj({
            "proxy_8888_allowed": proxy_ok,
            "api_18901_allowed": api_ok,
            "hint": "若 false 表示规则未添加，手机连不上。运行 `telnix system firewall-allow` 添加（需管理员权限）",
        })
    elif action == "install-dep":
        # 触发 pip 安装可选依赖（如 mitmproxy）
        # 异步任务：本命令返回 status=running 后需轮询 install-dep-status 查询进度
        pkg = getattr(args, "package", None) or "mitmproxy"
        res = _req("POST", "/system/install-dep", body={"package": pkg}, timeout=30)
        data = _ok(res)
        emit_obj(data)
        if not args.json:
            print(f"[Telnix] 安装任务已启动，使用 `telnix system install-dep-status` 查询进度",
                  file=sys.stderr)
    elif action == "install-dep-status":
        # 查询 install-dep 任务状态
        res = _req("GET", "/system/install-dep/status")
        data = _ok(res)
        emit_obj(data)
        if not args.json:
            status = data.get("status")
            pkg = data.get("package") or ""
            if status == "running":
                print(f"[Telnix] {pkg} 安装中…", file=sys.stderr)
            elif status == "success":
                extra = ""
                if data.get("mitmproxy_available"):
                    extra = "（已可切换为代理引擎）"
                elif data.get("note"):
                    extra = f"（{data['note']}）"
                print(f"[Telnix] {pkg} 安装成功{extra}，重启 Telnix 后生效", file=sys.stderr)
            elif status == "failed":
                print(f"[Telnix] {pkg} 安装失败（return_code={data.get('return_code')}）",
                      file=sys.stderr)
    else:
        _die_arg("system 需要: restart | quit | restart-as-admin | firewall-allow | "
                 "firewall-status | install-dep | install-dep-status")


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
        # 附带 filter 字段（非空才显示，让 agent 确认后端会做这些过滤）
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
        # §4.1 过滤字段：代理层 find_matching_rule 会校验（非空才生效）
        "method_filter": m.get("method_filter", ""),
        "status_filter": m.get("status_filter", ""),
        "pid_filter": m.get("pid_filter", ""),
        "process_filter": m.get("process_filter", ""),
    }
    # §3.1 --idempotent：优先调后端专用端点（单次请求+服务端比对，比客户端遍历高效）
    # 失败时（如后端旧版本不支持端点）降级为客户端遍历保持兼容
    if getattr(args, "idempotent", False):
        try:
            # 优先用后端 POST /auto-reply/rules/idempotent 端点
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
            # 降级：后端旧版本无 idempotent 端点，用客户端遍历
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
                               "hint": "已存在相同规则，未重复创建（客户端遍历降级）"}
                        emit_obj(out)
                        return
            except Exception:  # noqa: BLE001
                pass  # 查询失败则正常创建
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
    # 统一加 rule_id 别名（后端字段是 id，文档统一用 rule_id 方便 agent 提取）
    for r in rules:
        if isinstance(r, dict) and "rule_id" not in r:
            r["rule_id"] = r.get("id")
        # §4.1 空 filter 字段输出 null 而非空字符串，避免 agent 判断 method_filter != "" 的特判
        if isinstance(r, dict):
            for fk in ("method_filter", "status_filter", "pid_filter", "process_filter"):
                if r.get(fk) == "":
                    r[fk] = None
    # §4.1 intercept list 默认输出含 hit_count/last_hit_at/last_hit_flow_id（后端已返回）
    # --with-stats 是语义化 flag，保持兼容（不加也输出统计字段）
    emit_list(rules, json_array=args.json_array)


def cmd_intercept_hits(args):
    """§4.1 显示某规则的命中统计 + 最后命中的流量详情。

    后端只存 last_hit_flow_id（单个），无完整命中历史表。
    输出：{rule_id, hit_count, last_hit_at, last_hit_flow_id, last_hit_flow: {...}|null}
    """
    rule_id = args.id
    if not rule_id:
        _die_arg("intercept hits 需要 <rule_id>")
    res = _req("GET", "/auto-reply/rules")
    rules = _ok(res)
    rules = rules if isinstance(rules, list) else []
    rule = None
    for r in rules:
        if isinstance(r, dict) and (r.get("id") == rule_id or r.get("rule_id") == rule_id):
            rule = r
            break
    if not rule:
        _die_arg(f"规则不存在: {rule_id}")
    out = {
        "rule_id": rule.get("id"),
        "pattern": rule.get("pattern"),
        "action": rule.get("action"),
        "hit_count": rule.get("hit_count", 0),
        "last_hit_at": rule.get("last_hit_at", ""),
        "last_hit_flow_id": rule.get("last_hit_flow_id"),
    }
    # 如果有最后命中的 flow_id，获取该流量详情
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
        _die_arg("需要 <id> 或 --ids")


def cmd_intercept_template(args):
    """§3.17 规则模板库 CLI 封装：list 列出模板，apply 应用模板创建规则。"""
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
            _die_arg("template apply 需要 <name>")
        if not pattern:
            _die_arg("template apply 需要 --match")
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
    _die_arg("intercept template 需要: list | apply <name>")


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
    # --timeout N 覆盖默认超时（默认：带 body 120s，无 body 30s）
    user_timeout = getattr(args, "timeout", 0) or 0

    def _req_timeout() -> float:
        if user_timeout > 0:
            return float(user_timeout)
        return 120.0 if body else 30.0

    # --fuzz-file 多字段组合 fuzz 优先于 --fuzz（互斥，优先用 --fuzz-file）
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

    # 批量重放
    if parallel > 1:
        # 并发模式：用 ThreadPoolExecutor 并发重放 N 次，每条输出 index/status/duration/size
        from concurrent.futures import ThreadPoolExecutor

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
            except SystemExit:  # _ok 失败会 sys.exit，线程里捕获避免连坐
                return {"index": idx, "ok": False, "error": "API 调用失败"}
            except Exception as e:  # noqa: BLE001
                return {"index": idx, "ok": False, "error": str(e)}

        results = []
        with ThreadPoolExecutor(max_workers=parallel) as ex:
            futures = [ex.submit(_do_replay, i) for i in range(repeat)]
            for fut in futures:
                results.append(fut.result())
        # 按 index 排序（并发完成顺序可能乱）
        results.sort(key=lambda x: x.get("index", 0))
    else:
        # 串行模式（保持原行为）
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

    out = {"replayed": True, "id": args.id, "count": repeat, "parallel": parallel, "results": results}
    # --compare：对比多次响应差异（以第一次为基准）
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
        # 以第一次为基准对比后续
        diffs = []
        for i in range(1, len(bodies)):
            diff = list(difflib.unified_diff(bodies[0], bodies[i],
                                              fromfile=f"run0", tofile=f"run{i}", lineterm=""))
            diffs.append({"run": i, "identical": not diff, "diff": "\n".join(diff)})
        out["compare"] = diffs
    emit_obj(out)


def _replay_fuzz_file(args, base_body: dict):
    """多字段组合 fuzz：从 payloads.json 加载 {field: [values...]}，
    按 cartesian/zip 模式生成组合，每次替换 JSON body 中对应字段后发起重放。
    与 --fuzz（单字段数值范围）互斥，优先用 --fuzz-file。
    """
    if not os.path.isfile(args.fuzz_file):
        _die_arg(f"文件不存在: {args.fuzz_file}")
    try:
        with open(args.fuzz_file, "r", encoding="utf-8") as f:
            payloads = json.load(f)
    except Exception as e:  # noqa: BLE001
        _die_arg(f"读取 payloads.json 失败: {e}")
    if not isinstance(payloads, dict) or not payloads:
        _die_arg("payloads.json 格式错误：应为 {field: [values...]}")

    mode = (getattr(args, "mode", "") or "cartesian").lower()
    keys = list(payloads.keys())
    value_lists = [payloads[k] if isinstance(payloads[k], list) else [payloads[k]] for k in keys]

    # 生成组合
    if mode == "zip":
        combos = list(zip(*value_lists))  # 取最短长度
    else:  # cartesian
        import itertools
        combos = list(itertools.product(*value_lists))

    if not combos:
        _die_arg("payloads.json 中无有效组合")

    # 拉取原 flow 的 request_body 作为模板（--body 优先）
    res = _req("GET", f"/flows/{args.id}")
    flow = _ok(res)
    if not isinstance(flow, dict):
        _die_arg("无法获取 flow 详情")
    orig_body = base_body.get("body") or flow.get("request_body") or ""
    if not orig_body:
        _die_arg("原 flow 无 request_body，无法做字段替换")
    try:
        body_obj = json.loads(orig_body)
    except Exception as e:  # noqa: BLE001
        _die_arg(f"原 request_body 非 JSON: {e}")
    if not isinstance(body_obj, dict):
        _die_arg("原 request_body 不是 JSON 对象，无法做字段替换")

    import copy
    user_timeout = getattr(args, "timeout", 0) or 0
    req_timeout = float(user_timeout) if user_timeout > 0 else 120.0
    results = []
    for idx, combo in enumerate(combos):
        # 深拷贝并替换字段
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
            results.append({"index": idx, "ok": False, "error": "API 调用失败",
                            "fields": dict(zip(keys, combo))})
        except Exception as e:  # noqa: BLE001
            results.append({"index": idx, "ok": False, "error": str(e),
                            "fields": dict(zip(keys, combo))})
    emit_obj({
        "fuzz_file": args.fuzz_file, "mode": mode, "count": len(combos),
        "fields": keys, "id": args.id, "results": results,
    })


def _set_json_path(obj: dict, path: str, value: Any) -> None:
    """支持点分路径 a.b.c 设置 JSON 字段（自动创建中间节点）。"""
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
    # 不指定 -o 时按 format 推导默认文件名
    out = args.output
    if not out:
        ext_map = {"har": "har", "json": "json", "csv": "csv",
                   "python-requests": "py", "postman": "json", "curl": "sh"}
        ext = ext_map.get(args.format, "txt")
        out = f"telnix_export.{ext}"
    try:
        if isinstance(content, str):
            with open(out, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(content, f, ensure_ascii=False, indent=2)
        emit_obj({"exported": True, "path": out, "format": args.format})
    except Exception as e:  # noqa: BLE001
        emit_obj({"exported": False, "error": str(e)})


def cmd_send(args):
    """从零发包：构造 HTTP 请求并发送（Composer 功能）。

    不依赖已有 flow，直接构造请求。支持 --method/--url/--header/--body/--timeout。
    输出响应的 status_code/headers/body/duration。
    """
    method = (args.method or "GET").upper()
    url = (args.url or "").strip()
    if not url:
        _die_arg("--url 必填")
    if not url.startswith(("http://", "https://")):
        _die_arg("--url 必须以 http:// 或 https:// 开头")
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
                _die_arg(f"读取 --body-file 失败: {e}")
        else:
            body["body"] = args.body
    body["method"] = method
    body["url"] = url
    body["timeout"] = float(args.timeout or 30)

    # --emit-curl：只输出 curl 命令不发送
    if getattr(args, "emit_curl", False):
        parts = ["curl", "-X", method]
        for k, v in headers.items():
            parts += ["-H", f"{k}: {v}"]
        if body.get("body"):
            parts += ["--data-raw", body["body"]]
        parts.append('"' + url + '"')
        print(" ".join(parts))
        return

    timeout = float(args.timeout or 30) + 5  # 多给 5s 给后端处理
    res = _req("POST", "/send", body, timeout=timeout)
    data = _ok(res)
    if getattr(args, "headers_only", False):
        # 只输出响应头
        emit_obj({
            "status_code": data.get("status_code"),
            "reason": data.get("reason", ""),
            "headers": data.get("response_headers", {}),
            "size": data.get("size", 0),
            "duration_ms": data.get("duration_ms", 0),
        })
    elif getattr(args, "body_only", False):
        # 只输出响应体（纯文本，不 JSON 包裹）
        print(data.get("response_body", ""))
    else:
        emit_obj(data)


def cmd_replay_batch(args):
    """时序回放：拉取指定 session 全部流量，按 timestamp 排序后整批重放。
    - --preserve-timing：按原始时间间隔 sleep 后重放（测服务端限流/风控），串行
    - 无 --preserve-timing：立即连续重放，可配合 --parallel 并发
    - --filter：客户端过滤表达式，只重放匹配的流量
    输出每条重放结果的 index/flow_id/status/duration（NDJSON）。
    """
    if not args.session:
        _die_arg("replay-batch 需要 --session N")
    # 拉取 session 所有流量
    res = _req("GET", f"/sessions/{args.session}/flows?limit=50000&offset=0")
    data = _ok(res)
    flows = data.get("flows", []) if isinstance(data, dict) else data
    if not flows:
        emit_obj({"replayed": True, "session": args.session, "count": 0,
                  "filtered_count": 0, "results": []})
        return
    flows = [f for f in flows if isinstance(f, dict)]
    total_before = len(flows)
    # §4.3 客户端过滤：--filter 表达式（用 parse_match + flow_matches）
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
    # 按 timestamp 升序排序
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
            return {"index": idx, "flow_id": fid, "ok": False, "error": "API 调用失败"}
        except Exception as e:  # noqa: BLE001
            return {"index": idx, "flow_id": fid, "ok": False, "error": str(e)}

    if preserve_timing:
        # 保留原始时间间隔，串行重放，流式输出
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
                        time.sleep(min(delta, 60))  # 单次最多 sleep 60s 防卡死
                except Exception:  # noqa: BLE001
                    pass
            r = _replay_one(f, idx)
            print(json.dumps(r, ensure_ascii=False))
            prev_ts = ts_str
    elif parallel > 1:
        # §3.2 并发立即重放，用 as_completed 真流式输出（完成一个输出一个，不阻塞等前面的）
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
        # 串行立即重放，流式输出
        for idx, f in enumerate(flows):
            r = _replay_one(f, idx)
            print(json.dumps(r, ensure_ascii=False))
    # §4.3 汇总信息（输出到 stderr，不干扰 stdout 的 NDJSON 流）
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
        _die_arg("proxy 需要: status | on | off")


def cmd_raw(args):
    if args.action == "status":
        res = _req("GET", "/raw/status")
        data = _ok(res)
        if not data.get("pydivert_installed"):
            data["hint"] = "运行: python -m telnix.cli raw install"
        elif not data.get("is_admin"):
            data["hint"] = "需要管理员权限。请用管理员身份重启 Telnix"
        emit_obj(data)
    elif args.action == "install":
        # 调后端 pip install pydivert（驱动 WinDivert64.sys 随包附带）
        res = _req("POST", "/raw/install-pydivert", timeout=180)
        data = _ok(res)
        emit_obj(data)
    elif args.action == "start":
        body = {}
        if args.pid:
            body["pid_filter"] = [int(p) for p in args.pid.split(",") if p.strip()]
        if args.port:
            body["port_filter"] = [int(p) for p in args.port.split(",") if p.strip()]
        if args.bpf:
            body["filter_str"] = args.bpf
        res = _req("POST", "/raw/start", body, timeout=10)
        data = _ok(res)
        emit_obj(data)
    elif args.action == "stop":
        res = _req("POST", "/raw/stop")
        data = _ok(res)
        emit_obj(data)
    else:
        _die_arg("raw 需要: status | install | start | stop")


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
        _die_arg("cert 需要: status | install | remove")


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
    # /logs/export 返回 PlainTextResponse（JSONL），_req 的 json.loads 会失败
    # 直接用 urllib 取原始文本
    import urllib.request as _ur
    full_url = f"{BASE_URL}/api{url}"
    req = _ur.Request(full_url, headers={"Accept": "application/x-jsonlines"})
    try:
        with _ur.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        _die_conn(f"导出日志失败: {e}")
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
            # host 通配符列表（* → .*, ? → .），跨类 OR 匹配：pid/host 满足任一即记录
            body["hosts"] = [h.strip() for h in args.host.split(",") if h.strip()]
        if not body["pids"] and not body["process_names"] and not body.get("hosts"):
            _die_arg("focus on 需要 --pid 或 --name 或 --host")
        res = _req("POST", "/focus", body)
        emit_obj(_ok(res))
    elif args.action == "off":
        res = _req("POST", "/focus", {"enabled": False, "pids": []})
        emit_obj(_ok(res))
    else:
        _die_arg("focus 需要: status | on | off")


def cmd_breakpoint(args):
    """断点控制：status / on / off / timeout / release / drop。

    超时机制：开启断点时设 --timeout N，N 秒未放行自动 release，避免 agent 忘了放行导致连接永久阻塞。
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
            out["hint"] = f"断点 {bp_type} 已开启，{args.timeout}s 未放行自动 release"
        out.update(data)
        emit_obj(out)
    elif args.action == "off":
        bp_type = args.type or "request"
        res = _req("POST", f"/breakpoint/{bp_type}", {"enabled": False})
        emit_obj(_ok(res))
    elif args.action == "timeout":
        if args.timeout is None:
            _die_arg("breakpoint timeout 需要 --timeout N")
        res = _req("POST", "/breakpoint/timeout", {"timeout": args.timeout})
        emit_obj(_ok(res))
    elif args.action in ("release", "drop"):
        action = "release" if args.action == "release" else "drop"
        if getattr(args, "all", False):
            # 批量放行/丢弃所有 pending 断点
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
                emit_obj({"released": 0, "total": 0, "hint": "无 pending 断点"})
                return
            res = _req("POST", "/flows/batch-release", {"ids": ids, "action": action})
            data = _ok(res)
            emit_obj({"action": action, "total": len(ids),
                      "released": data.get("released", 0) if isinstance(data, dict) else 0})
        elif args.id:
            res = _req("POST", f"/flows/{args.id}/release", {"action": action})
            emit_obj(_ok(res))
        else:
            _die_arg(f"breakpoint {action} 需要 <flow_id> 或 --all")
    else:
        _die_arg("breakpoint 需要: status | on | off | timeout | release | drop")


# ---------- argparse ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="telnix-cli",
        description="Telnix Agent CLI —— 给 AI agent 用的抓包/拦截控制工具",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # status
    sub.add_parser("status", help="后端状态").set_defaults(func=cmd_status)

    # capture
    sp = sub.add_parser("capture", help="抓包控制")
    sp_sub = sp.add_subparsers(dest="sub", required=True)
    sp_start = sp_sub.add_parser("start", help="开始抓包")
    sp_start.add_argument("--filter", default="", help="捕获过滤（暂不支持，仅记录）")
    sp_start.add_argument("--max-duration", type=int, default=0, help="建议最大抓包时长（秒，agent 防翻车提示）")
    sp_start.add_argument("--auto-stop", type=int, default=0, help="N 秒后真正自动停止抓包（后台线程定时 stop，agent 不用自己 sleep+stop）")
    sp_start.add_argument("--layer", choices=["http", "tcp", "all"], default="http",
                          help="抓包层：http=仅HTTP代理（默认），tcp=仅TCP/UDP（WinDivert），all=两者都抓")
    sp_start.add_argument("--pid", default="", help="TCP/UDP 模式：按 PID 过滤，逗号分隔")
    sp_start.add_argument("--port", default="", help="TCP/UDP 模式：按端口过滤，逗号分隔")
    sp_start.add_argument("--bpf", default="", help="TCP/UDP 模式：WinDivert filter 字符串")
    sp_start.set_defaults(func=cmd_capture_start)
    sp_stop = sp_sub.add_parser("stop", help="停止抓包")
    sp_stop.add_argument("--layer", choices=["http", "tcp", "all"], default="http",
                         help="停止哪层：默认 http，all 同时停 TCP/UDP")
    sp_stop.set_defaults(func=cmd_capture_stop)
    sp_sub.add_parser("clear", help="清空当前会话流量").set_defaults(func=cmd_capture_clear)
    sp_sub.add_parser("pause", help="暂停抓包（会话保留，代理仍跑）").set_defaults(func=cmd_capture_pause)
    sp_sub.add_parser("resume", help="恢复抓包记录").set_defaults(func=cmd_capture_resume)

    # packets
    sp = sub.add_parser("packets", help="流量查询")
    sp_sub = sp.add_subparsers(dest="sub", required=True)
    sp_list = sp_sub.add_parser("list", help="流量列表（NDJSON）")
    sp_list.add_argument("--session", type=int, default=0, help="会话 ID（默认当前活动会话）")
    sp_list.add_argument("--limit", type=int, default=100, help="最多返回条数")
    sp_list.add_argument("--since-id", type=int, default=None, help="增量查询：只返回 id > N 的流量（非阻塞）")
    sp_list.add_argument("--tail", action="store_true", help="流式追包（阻塞，Ctrl+C 退出）")
    sp_list.add_argument("--filter", default="", help="过滤表达式: key op value && ...")
    sp_list.add_argument("--filter-host", default="", help="快捷: 按主机过滤")
    sp_list.add_argument("--filter-status", default="", help="快捷: 按状态码过滤")
    sp_list.add_argument("--filter-method", default="", help="快捷: 按方法过滤")
    sp_list.add_argument("--protocol", default="", help="协议过滤: http|tcp|udp|ws|dns")
    sp_list.add_argument("--emit-curl", action="store_true", help="每条流量附带可重放 curl 命令")
    sp_list.add_argument("--json-array", action="store_true", help="输出 JSON 数组而非 NDJSON")
    sp_list.add_argument("--decode", default="", help="批量加载解码器插件，每条流量输出 decoded 字段（见 §3.3 解码器插件）")
    sp_list.add_argument("--decode-field", default="response_body",
                         help="解码器作用字段: request_body|response_body（默认 response_body）")
    sp_list.add_argument("--tag", default="", help="按标签过滤（只返回带指定标签的流量）")
    sp_list.add_argument("--has-tags", action="store_true", help="只返回有标签的流量")
    sp_list.set_defaults(func=cmd_packets_list)

    sp_get = sp_sub.add_parser("get", help="流量详情")
    sp_get.add_argument("id", type=int, help="流量 ID")
    sp_get.add_argument("--emit-curl", action="store_true", help="附带 curl 命令")
    sp_get.add_argument("--hex", action="store_true", help="输出 hex dump 格式")
    sp_get.add_argument("--field", default="response_body", help="hex 模式: request_body|response_body|raw_data")
    sp_get.add_argument("--offset", type=int, default=0, help="hex 模式: 起始偏移")
    sp_get.add_argument("--length", type=int, default=0, help="hex 模式: 长度（0=全部）")
    sp_get.add_argument("--decode", default="", help="加载 Python 解码器插件对 body 解码（见 §3.3 解码器插件）")
    sp_get.add_argument("--decode-field", default="response_body",
                        help="解码器作用字段: request_body|response_body（默认 response_body）")
    sp_get.set_defaults(func=cmd_packets_get)

    sp_del = sp_sub.add_parser("delete", help="删除流量")
    sp_del.add_argument("id", type=int, nargs="?", default=0, help="流量 ID")
    sp_del.add_argument("--ids", default="", help="批量删除，逗号分隔")
    sp_del.set_defaults(func=cmd_packets_delete)

    sp_search = sp_sub.add_parser("search", help="跨 body 搜索流量（当前会话）")
    sp_search.add_argument("--body-regex", default="", help="正则表达式（匹配 request_body/response_body/url/path）")
    sp_search.add_argument("--binary-hex", default="", help="二进制内容搜索（hex 字符串）")
    sp_search.add_argument("--header-regex", default="",
                           help="请求/响应头正则匹配（与 body-regex 是 AND 关系）")
    sp_search.add_argument("--method", default="", help="精确匹配 HTTP 方法（不区分大小写）")
    sp_search.add_argument("--status", type=int, default=None, help="精确匹配状态码")
    sp_search.add_argument("--pid", type=int, default=None, help="精确匹配进程 PID")
    sp_search.add_argument("--process", default="", help="精确匹配进程名")
    sp_search.add_argument("--limit", type=int, default=200, help="最多返回条数")
    sp_search.add_argument("--json-array", action="store_true", help="输出 JSON 数组")
    sp_search.add_argument("--all", action="store_true", help="跨所有会话搜索（session_id=0）")
    sp_search.add_argument("--offset", default="", help="二进制搜索字节范围 START:END（如 0:1024 只搜前 1KB）")
    sp_search.set_defaults(func=cmd_packets_search)

    sp_list_all = sp_sub.add_parser("list-all", help="跨会话查询所有流量（NDJSON）")
    sp_list_all.add_argument("--limit", type=int, default=100, help="最多返回条数")
    sp_list_all.add_argument("--offset", type=int, default=0, help="分页偏移")
    sp_list_all.add_argument("--since-id", type=int, default=None, help="增量查询：只返回 id > N 的流量")
    sp_list_all.add_argument("--host", default="", help="按 host 过滤")
    sp_list_all.add_argument("--process", default="", help="按进程名过滤")
    sp_list_all.add_argument("--status", type=int, default=0, help="按状态码过滤")
    sp_list_all.add_argument("--method", default="", help="按方法过滤")
    sp_list_all.add_argument("--protocol", default="", help="按协议过滤: http|tcp|udp|ws|dns")
    sp_list_all.add_argument("--filter", default="", help="客户端表达式过滤: key op value && ...")
    sp_list_all.add_argument("--filter-path", default="", help="按 path 过滤（后端 SQL LIKE）")
    sp_list_all.add_argument("--filter-url", default="", help="按 url 过滤（后端 SQL LIKE）")
    sp_list_all.add_argument("--emit-curl", action="store_true", help="每条流量附带 curl 命令")
    sp_list_all.add_argument("--json-array", action="store_true", help="输出 JSON 数组")
    sp_list_all.add_argument("--decode", default="", help="批量加载解码器插件，每条流量输出 decoded 字段")
    sp_list_all.add_argument("--decode-field", default="response_body",
                             help="解码器作用字段: request_body|response_body（默认 response_body）")
    sp_list_all.add_argument("--tag", default="", help="按标签过滤（只返回带指定标签的流量）")
    sp_list_all.add_argument("--has-tags", action="store_true", help="只返回有标签的流量")
    sp_list_all.set_defaults(func=cmd_packets_list_all)

    sp_clear = sp_sub.add_parser("clear", help="跨会话清理流量")
    sp_clear.add_argument("--all", action="store_true", help="清空全部历史流量")
    sp_clear.add_argument("--before-id", type=int, default=0, help="删除 id < N 的旧流量")
    sp_clear.set_defaults(func=cmd_packets_clear)

    sp_export = sp_sub.add_parser("export", help="单 flow 导出（curl/python-requests/postman/json/csv）")
    sp_export.add_argument("id", type=int, help="流量 ID")
    sp_export.add_argument("--format", choices=["curl", "python-requests", "postman", "json", "csv"],
                           default="curl", help="导出格式")
    sp_export.add_argument("-o", "--output", default="", help="输出文件路径（不指定则 stdout）")
    sp_export.set_defaults(func=cmd_packets_export)

    # §3.1 流量标签 tag 子命令
    sp_tag = sp_sub.add_parser("tag", help="流量标签管理：--add/--remove/--clear/--note/--clear-note/--list")
    sp_tag.add_argument("id", type=int, nargs="?", default=0, help="流量 ID（--list 时可省略）")
    sp_tag.add_argument("--add", default="", help="添加标签（逗号分隔的 tags 列表中追加一项）")
    sp_tag.add_argument("--remove", default="", help="移除标签")
    sp_tag.add_argument("--clear", action="store_true", help="清空所有标签")
    sp_tag.add_argument("--note", default=None, help="设置标签备注（tag_note）")
    sp_tag.add_argument("--clear-note", action="store_true", help="清除标签备注（显式置空 tag_note）")
    sp_tag.add_argument("--list", action="store_true", help="列出全局所有标签及每标签的 flow 数")
    sp_tag.set_defaults(func=cmd_packets_tag)

    sp_stats = sp_sub.add_parser("stats", help="流量分组统计")
    sp_stats.add_argument("--session", type=int, default=0, help="会话 ID（默认当前活动会话）")
    sp_stats.add_argument("--by", choices=["host", "method", "status", "protocol", "endpoint",
                                           "content_type", "process"], default="",
                          help="按维度分组（默认全部）。endpoint 做 path 模板归一化；content_type/process 走后端 /flows/stats")
    sp_stats.add_argument("--metrics", default="", help="附加指标: size,duration（如 --metrics size,duration 输出 p50/p95/max）")
    sp_stats.add_argument("--limit", type=int, default=2000, help="分析流量条数上限（0=不限，实际 50000）")
    sp_stats.add_argument("--keep-query", action="store_true", help="endpoint 归一化保留 query 参数名")
    sp_stats.set_defaults(func=cmd_packets_stats)

    sp_overview = sp_sub.add_parser("overview", help="多维聚合统计概览（CoolUI 仪表盘数据源，跨会话全量）")
    sp_overview.set_defaults(func=cmd_packets_overview)

    sp_diff = sp_sub.add_parser("diff", help="对比两条流量的请求/响应字段（unified diff）")
    sp_diff.add_argument("id1", type=int, help="第一条流量 ID")
    sp_diff.add_argument("id2", type=int, help="第二条流量 ID")
    sp_diff.add_argument("--field", default="response_body",
                         help="对比字段: request_body|response_body|request_headers|response_headers|url")
    sp_diff.set_defaults(func=cmd_packets_diff)

    sp_endpoints = sp_sub.add_parser("endpoints", help="唯一 endpoint 提取（path 模板归一化，画 API 地图）")
    sp_endpoints.add_argument("--host", default="", help="按 host 过滤")
    sp_endpoints.add_argument("--session", type=int, default=0, help="只看指定会话（默认跨所有会话）")
    sp_endpoints.add_argument("--limit", type=int, default=2000, help="分析流量条数上限（0=不限，实际 50000）")
    sp_endpoints.add_argument("--keep-query", action="store_true", help="保留 query 参数名（默认丢弃 query）")
    sp_endpoints.add_argument("--sample-strategy", choices=["first", "last", "random"], default="first",
                              help="sample_ids 采样策略: first=前 3 个（默认），last=后 3 个，random=随机 3 个")
    sp_endpoints.add_argument("--json-array", action="store_true", help="输出 JSON 数组")
    sp_endpoints.set_defaults(func=cmd_packets_endpoints)

    sp_timeline = sp_sub.add_parser("timeline", help="流量时间线（按时间排序，标注大间隔）")
    sp_timeline.add_argument("--host", default="", help="按 host 过滤")
    sp_timeline.add_argument("--session", type=int, default=0, help="只看指定会话（默认跨所有会话）")
    sp_timeline.add_argument("--gap", type=float, default=1.0, help="间隔超过 N 秒标为段落分隔")
    sp_timeline.add_argument("--limit", type=int, default=2000, help="分析流量条数上限（0=不限，实际 50000）")
    sp_timeline.set_defaults(func=cmd_packets_timeline)

    sp_watch = sp_sub.add_parser("watch", help="定向 tail：只输出匹配的新流量（阻塞，Ctrl+C 退出）")
    sp_watch.add_argument("--filter", required=True, help="过滤表达式: host~=api.x.com && method=POST")
    sp_watch.add_argument("--session", type=int, default=0, help="会话 ID（默认当前活动会话）")
    sp_watch.add_argument("--interval", type=float, default=1.0, help="轮询间隔（秒）")
    sp_watch.set_defaults(func=cmd_packets_watch)

    sp_trace = sp_sub.add_parser("trace", help="请求依赖链 trace：从响应提取字符串值，在后续流量请求里搜索")
    sp_trace.add_argument("id", type=int, help="源 flow ID")
    sp_trace.add_argument("--limit", type=int, default=500, help="扫描后续流量条数上限")
    sp_trace.add_argument("--all", action="store_true", help="跨会话扫描（/flows/all），默认只在当前会话内扫描")
    sp_trace.add_argument("--session", type=int, default=0, help="会话 ID（默认当前活动会话，仅 --all 未指定时生效）")
    sp_trace.add_argument("--min-length", type=int, default=4, help="只追踪长度 >= N 的字符串值（减少短串误报，默认 4）")
    sp_trace.set_defaults(func=cmd_packets_trace)

    sp_analyze = sp_sub.add_parser("analyze", help="签名字段自动检测（多 flow 对比，找可疑签名/ token 字段）")
    sp_analyze.add_argument("ids", type=int, nargs="*", help="flow ID 列表（普通模式至少 2 个；--all 模式至少 1 个作源）")
    sp_analyze.add_argument("--find-signature", action="store_true",
                            help="执行签名字段检测（默认即启用，flag 用于显式声明）")
    sp_analyze.add_argument("--all", action="store_true", help="跨会话扫描：以第一个 ID 为源从 /flows/all 拉后续流量")
    sp_analyze.add_argument("--limit", type=int, default=500, help="--all 模式下扫描后续流量条数上限")
    sp_analyze.set_defaults(func=cmd_packets_analyze)

    # intercept
    sp = sub.add_parser("intercept", help="拦截/改包规则")
    sp_sub = sp.add_subparsers(dest="sub", required=True)
    sp_add = sp_sub.add_parser("add", help="添加拦截规则")
    sp_add.add_argument("--match", required=True, help="匹配表达式: host~=x && method=POST && path~=/api/*")
    sp_add.add_argument("--action", required=True,
                        help="动作: set-json k v | set-request-header K V | mock CODE BODY | replace-bytes off:hex | ...")
    sp_add.add_argument("--name", default="", help="规则名（写入备注）")
    sp_add.add_argument("--note", default="", help="备注")
    sp_add.add_argument("--session", type=int, default=0, help="会话 ID（dry-run 用）")
    sp_add.add_argument("--dry-run", action="store_true", help="预览：列出会命中的流量，不创建规则")
    sp_add.add_argument("--idempotent", action="store_true",
                        help="幂等创建：已存在相同 pattern+action+modify_rules 的规则时不重复创建，返回现有 rule_id")
    sp_add.set_defaults(func=cmd_intercept_add)

    sp_list = sp_sub.add_parser("list", help="规则列表（NDJSON，含 hit_count 命中统计）")
    sp_list.add_argument("--json-array", action="store_true", help="输出 JSON 数组")
    sp_list.add_argument("--with-stats", action="store_true",
                         help="语义化 flag：显式要求含命中统计（默认已输出 hit_count/last_hit_at/last_hit_flow_id）")
    sp_list.set_defaults(func=cmd_intercept_list)

    sp_hits = sp_sub.add_parser("hits", help="显示某规则的命中统计 + 最后命中的流量详情（§4.1）")
    sp_hits.add_argument("id", help="规则 ID")
    sp_hits.set_defaults(func=cmd_intercept_hits)

    sp_del = sp_sub.add_parser("del", help="删除规则")
    sp_del.add_argument("id", nargs="?", default="", help="规则 ID")
    sp_del.add_argument("--ids", default="", help="批量删除，逗号分隔")
    sp_del.set_defaults(func=cmd_intercept_del)

    sp_toggle = sp_sub.add_parser("toggle", help="启用/禁用规则（不删除）")
    sp_toggle.add_argument("id", nargs="?", default="", help="规则 ID（单条切换）")
    sp_toggle.add_argument("--all", action="store_true", help="批量操作所有规则")
    sp_toggle.add_argument("--enable", action="store_true", help="强制启用")
    sp_toggle.add_argument("--disable", action="store_true", help="强制禁用")
    sp_toggle.set_defaults(func=cmd_intercept_toggle)

    sp_update = sp_sub.add_parser("update", help="修改现有规则（不删除重建）")
    sp_update.add_argument("id", help="规则 ID")
    sp_update.add_argument("--match", default="", help="新匹配表达式: host~=x && method=POST")
    sp_update.add_argument("--action", default="", help="新动作: set-json k v | mock CODE BODY | ...")
    sp_update.add_argument("--note", default="", help="新备注")
    sp_update.add_argument("--enable", action="store_true", help="同时启用")
    sp_update.add_argument("--disable", action="store_true", help="同时禁用")
    sp_update.set_defaults(func=cmd_intercept_update)

    sp_export_rules = sp_sub.add_parser("export", help="导出所有规则到 JSON 文件")
    sp_export_rules.add_argument("-o", "--output", default="telnix_rules.json", help="输出文件路径")
    sp_export_rules.set_defaults(func=cmd_intercept_export)

    sp_import_rules = sp_sub.add_parser("import", help="从 JSON 文件导入规则")
    sp_import_rules.add_argument("file", help="规则 JSON 文件路径")
    sp_import_rules.add_argument("--mode", choices=["merge", "replace"], default="merge",
                                 help="merge=追加（默认），replace=先清空再导入")
    sp_import_rules.add_argument("--quiet", action="store_true",
                                 help="只输出汇总不输出 results 数组（大批量导入时精简输出）")
    sp_import_rules.set_defaults(func=cmd_intercept_import)

    # §3.17 规则模板库 CLI 封装
    sp_tpl = sp_sub.add_parser("template", help="规则模板库（list/apply）")
    sp_tpl_sub = sp_tpl.add_subparsers(dest="action")
    sp_tpl_list = sp_tpl_sub.add_parser("list", help="列出所有内置模板")
    sp_tpl_list.add_argument("--json-array", action="store_true", help="输出 JSON 数组")
    sp_tpl_list.set_defaults(action="list")
    sp_tpl_apply = sp_tpl_sub.add_parser("apply", help="应用模板创建规则")
    sp_tpl_apply.add_argument("name", help="模板名（如 mock-404/unlock-vip）")
    sp_tpl_apply.add_argument("--match", required=True, help="URL 匹配 pattern（必填）")
    sp_tpl_apply.add_argument("--match-mode", choices=["wildcard", "exact", "regex"],
                              default="wildcard", help="匹配模式（默认 wildcard）")
    sp_tpl_apply.add_argument("--note", default="", help="规则备注")
    sp_tpl_apply.add_argument("--disabled", action="store_true", help="创建为禁用状态")
    sp_tpl_apply.add_argument("--method-filter", default="", help="方法过滤（逗号分隔）")
    sp_tpl_apply.add_argument("--status-filter", default="", help="状态码过滤（逗号分隔）")
    sp_tpl_apply.add_argument("--pid-filter", default="", help="PID 过滤")
    sp_tpl_apply.add_argument("--process-filter", default="", help="进程名过滤")
    sp_tpl_apply.set_defaults(action="apply")
    sp_tpl.set_defaults(func=cmd_intercept_template)

    # replay
    sp = sub.add_parser("replay", help="重放流量")
    sp.add_argument("id", type=int, help="流量 ID")
    sp.add_argument("--body", default="", help="覆盖请求体")
    sp.add_argument("--url", default="", help="覆盖 URL")
    sp.add_argument("--host", default="", help="重定向到其他 host")
    sp.add_argument("--port", type=int, default=0, help="重定向端口")
    sp.add_argument("--method", default="", help="覆盖 HTTP 方法")
    sp.add_argument("--header", action="append", help="覆盖/新增请求头，格式 K:V（可多次）")
    sp.add_argument("--fuzz", default="", help="批量 fuzz: key=start..end（JSON body 数值字段）")
    sp.add_argument("--fuzz-file", default="",
                    help="多字段组合 fuzz: payloads.json 路径，格式 {field: [values...]}（与 --fuzz 互斥，优先用 --fuzz-file）")
    sp.add_argument("--mode", choices=["cartesian", "zip"], default="cartesian",
                    help="--fuzz-file 组合模式: cartesian=笛卡尔积（默认），zip=按最短长度配对")
    sp.add_argument("--repeat", type=int, default=1, help="重复重放次数（默认 1）")
    sp.add_argument("--parallel", type=int, default=1, help="并发重放线程数（默认 1=串行，>1 用 ThreadPoolExecutor 并发）")
    sp.add_argument("--compare", action="store_true", help="重放多次时对比响应差异（以第一次为基准，输出 diff）")
    sp.add_argument("--timeout", type=int, default=0, help="HTTP 请求超时秒数（默认：带 body 120s，无 body 30s；慢接口可调大）")
    sp.set_defaults(func=cmd_replay)

    # send：从零发包（Composer）
    sp = sub.add_parser("send", help="从零发包（Composer）：构造 HTTP 请求并发送")
    sp.add_argument("--method", default="GET",
                    choices=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
                    help="HTTP 方法（默认 GET）")
    sp.add_argument("--url", required=True, help="请求 URL（必须以 http:// 或 https:// 开头）")
    sp.add_argument("--header", action="append", default=[],
                    help="请求头，格式 K:V（可多次）")
    sp.add_argument("--body", default="", help="请求体（与 --body-file 互斥）")
    sp.add_argument("--body-file", default="",
                    help="从文件读取请求体（与 --body 互斥，适合大 body 或二进制）")
    sp.add_argument("--timeout", type=int, default=30,
                    help="HTTP 请求超时秒数（默认 30）")
    sp.add_argument("--headers-only", action="store_true",
                    help="只输出响应头（不输出 body）")
    sp.add_argument("--body-only", action="store_true",
                    help="只输出响应体（纯文本，不 JSON 包裹，适合管道处理）")
    sp.add_argument("--emit-curl", action="store_true",
                    help="只输出等价的 curl 命令，不实际发送")
    sp.set_defaults(func=cmd_send)

    # replay-batch
    sp = sub.add_parser("replay-batch", help="时序回放：按 session 整批重放（可选保留原始时间间隔）")
    sp.add_argument("--session", type=int, required=True, help="会话 ID")
    sp.add_argument("--preserve-timing", action="store_true",
                    help="按原始时间间隔 sleep 后重放（测服务端限流/风控，强制串行）")
    sp.add_argument("--parallel", type=int, default=1,
                    help="并发线程数（默认 1=串行；--preserve-timing 时无效）")
    sp.add_argument("--filter", default="",
                    help="客户端过滤表达式（如 'host~=api.x.com && method=POST'），只重放匹配的流量")
    sp.set_defaults(func=cmd_replay_batch)

    # processes（无子命令=列出进程；子命令=忽略管理）
    sp = sub.add_parser("processes", help="进程列表 / 忽略进程·host 管理")
    sp.add_argument("action", nargs="?", default="",
                    choices=["", "ignore", "unignore", "ignored", "ignore-host", "unignore-host", "ignored-hosts"],
                    help="子命令: ignore/unignore/ignored/ignore-host/unignore-host/ignored-hosts（省略=列出进程）")
    sp.add_argument("row_id", type=int, nargs="?", default=0, help="行 ID（unignore/unignore-host 用）")
    sp.add_argument("--pid", type=int, default=None, help="ignore: 按 PID 忽略（与 --name 二选一或组合）")
    sp.add_argument("--name", default="", help="进程名过滤 / ignore: 按名称忽略")
    sp.add_argument("--host", default="", help="ignore-host: host 通配符（如 *.example.com）")
    sp.add_argument("--with-connections", action="store_true", help="附带每个进程的当前 TCP 连接")
    sp.add_argument("--tree", action="store_true", help="按进程树输出（找父子关系）")
    sp.add_argument("--include-listen", action="store_true",
                    help="包含 LISTEN 状态连接（默认跳过，只看 ESTABLISHED）")
    sp.add_argument("--json-array", action="store_true", help="输出 JSON 数组")
    sp.set_defaults(func=cmd_processes)

    # export
    sp = sub.add_parser("export", help="导出会话")
    sp.add_argument("--session", type=int, default=0, help="会话 ID")
    sp.add_argument("--format", choices=["har", "json", "python-requests", "postman", "curl", "csv"],
                    default="har", help="导出格式")
    sp.add_argument("-o", "--output", default="", help="输出文件路径（不指定则按格式推导文件名）")
    sp.set_defaults(func=cmd_export)

    # sessions
    sp = sub.add_parser("sessions", help="会话管理（list / show / delete）")
    sp.add_argument("action", choices=["list", "show", "delete"], help="操作")
    sp.add_argument("id", type=int, nargs="?", default=0, help="会话 ID（show/delete 用）")
    sp.add_argument("--json-array", action="store_true", help="list 输出 JSON 数组")
    sp.set_defaults(func=cmd_sessions)

    # proxy
    sp = sub.add_parser("proxy", help="系统代理控制")
    sp.add_argument("action", choices=["status", "on", "off"], help="操作")
    sp.set_defaults(func=cmd_proxy)

    # raw (TCP/UDP)
    sp = sub.add_parser("raw", help="TCP/UDP 原始抓包（WinDivert）")
    sp.add_argument("action", choices=["status", "install", "start", "stop"], help="操作")
    sp.add_argument("--pid", default="", help="按 PID 过滤，逗号分隔")
    sp.add_argument("--port", default="", help="按端口过滤，逗号分隔")
    sp.add_argument("--bpf", default="", help="WinDivert filter 字符串（如 'tcp and dst port 80'）")
    sp.set_defaults(func=cmd_raw)

    # cert
    sp = sub.add_parser("cert", help="证书管理")
    sp.add_argument("action", choices=["status", "install", "remove"], help="操作: status|install|remove")
    sp.set_defaults(func=cmd_cert)

    # log
    sp = sub.add_parser("log", help="日志管理")
    sp_sub = sp.add_subparsers(dest="sub", required=True)
    sp_tail = sp_sub.add_parser("tail", help="查看日志（NDJSON）")
    sp_tail.add_argument("--level", default="", help="级别过滤: DEBUG/INFO/WARNING/ERROR")
    sp_tail.add_argument("--category", default="", help="分类过滤: proxy/ai/settings/raw")
    sp_tail.add_argument("--limit", type=int, default=100, help="条数")
    sp_tail.set_defaults(func=cmd_log_tail)
    sp_clear = sp_sub.add_parser("clear", help="清空日志")
    sp_clear.set_defaults(func=cmd_log_clear)
    sp_export = sp_sub.add_parser("export", help="导出日志为 JSONL 文件")
    sp_export.add_argument("-o", "--output", default="", help="输出文件路径（不指定则 stdout）")
    sp_export.add_argument("--level", default="", help="级别过滤")
    sp_export.add_argument("--category", default="", help="分类过滤")
    sp_export.add_argument("--keyword", default="", help="关键词过滤")
    sp_export.set_defaults(func=cmd_log_export)

    # system
    sp = sub.add_parser("system", help="系统控制（重启/退出/管理员重启/防火墙放行/可选依赖安装）")
    sp.add_argument("action",
                    choices=["restart", "quit", "restart-as-admin", "firewall-allow", "firewall-status",
                             "install-dep", "install-dep-status"],
                    help="操作: restart=重启前后端，quit=退出 Telnix，restart-as-admin=以管理员身份重启（UAC 提权），"
                         "firewall-allow=防火墙放行 8888/18901 端口（手机抓包必备），firewall-status=查看放行规则状态，"
                         "install-dep=pip 安装可选依赖（如 mitmproxy），install-dep-status=查询安装任务状态")
    sp.add_argument("--package", default="mitmproxy",
                   help="install-dep 时指定包名（默认 mitmproxy）")
    sp.add_argument("--json", action="store_true", help="仅输出 JSON，stderr 提示信息静默（agent 友好）")
    sp.set_defaults(func=cmd_system)

    # settings（含代理引擎切换）
    sp = sub.add_parser("settings", help="设置管理（get/set/proxy-engine）")
    sp_sub = sp.add_subparsers(dest="sub_action", required=True)
    sp_get = sp_sub.add_parser("get", help="读取所有设置（NDJSON）")
    sp_get.add_argument("-k", "--key", default="", help="只读指定 key")
    sp_get.add_argument("--json", action="store_true", help="仅输出 JSON，stderr 提示信息静默（agent 友好）")
    sp_get.set_defaults(func=cmd_settings_get)
    sp_set = sp_sub.add_parser("set", help="写入单个设置项（--key/--value）")
    sp_set.add_argument("-k", "--key", required=True, help="设置项 key")
    sp_set.add_argument("-v", "--value", required=True, help="设置项 value（bool/list/dict 会自动反序列化）")
    sp_set.add_argument("--json", action="store_true", help="仅输出 JSON，stderr 提示信息静默（agent 友好）")
    sp_set.set_defaults(func=cmd_settings_set)
    sp_engine = sp_sub.add_parser("engine", help="查看/切换代理引擎（builtin/async/mitmproxy）")
    sp_engine.add_argument("name", nargs="?", default="",
                          help="引擎名：builtin（默认线程）/ async（asyncio）/ mitmproxy（需 pip install mitmproxy）；"
                               "省略则仅查看当前引擎")
    sp_engine.add_argument("--json", action="store_true", help="仅输出 JSON，stderr 提示信息静默（agent 友好）")
    sp_engine.set_defaults(func=cmd_settings_engine)

    # agent 工作模式
    sp = sub.add_parser("agent", help="agent 工作模式：临时清空工作区并保留原状备份，事后恢复")
    sp.add_argument("action", choices=["start", "end", "status"],
                    help="start=保存当前规则/focus/断点并禁用，end=从备份恢复原状，status=查询工作区状态")
    sp.set_defaults(func=cmd_agent)

    # focus
    sp = sub.add_parser("focus", help="专注模式（只抓指定进程/host，跨类 OR 匹配）")
    sp.add_argument("action", choices=["status", "on", "off"], help="操作")
    sp.add_argument("--pid", default="", help="按 PID，逗号分隔")
    sp.add_argument("--name", default="", help="按进程名，逗号分隔（PID 会变时用这个）")
    sp.add_argument("--host", default="", help="按 host 通配符，逗号分隔（如 *.example.com，跨类 OR 匹配）")
    sp.add_argument("--no-children", action="store_true", help="不自动包含子进程")
    sp.set_defaults(func=cmd_focus)

    # breakpoint
    sp = sub.add_parser("breakpoint", help="断点控制（支持超时自动放行/批量 release）")
    sp.add_argument("action", choices=["status", "on", "off", "timeout", "release", "drop"],
                    help="操作: status|on|off|timeout|release|drop")
    sp.add_argument("id", type=int, nargs="?", default=0, help="flow ID（release/drop 用）")
    sp.add_argument("--type", choices=["request", "response"], default="", help="断点类型（on/off 用，默认 request）")
    sp.add_argument("--timeout", type=float, default=None, help="超时秒数：on 时设此值，N 秒未放行自动 release；timeout 命令必填")
    sp.add_argument("--all", action="store_true", help="release/drop 批量操作所有 pending 断点")
    sp.set_defaults(func=cmd_breakpoint)

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

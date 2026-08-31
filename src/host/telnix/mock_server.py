"""独立 Mock HTTP 服务器。

监听单独端口（默认 18902），根据 settings.json 中 ``mock_rules`` 键下的规则
返回预设响应。不依赖主 FastAPI 应用，使用 Python 标准库 http.server 实现。

规则数据结构（存储在 settings.json 的 ``mock_rules`` 列表）：
    {
        "id": str,
        "enabled": bool,
        "method": str,            # HTTP 方法，如 "GET"
        "path": str,              # 匹配路径
        "match_mode": "exact" | "prefix" | "regex",
        "status_code": int,
        "headers": dict,          # 自定义响应头
        "body": str,              # 响应体
        "content_type": str,      # Content-Type（headers 未指定时使用）
        "delay_ms": int,          # 延迟响应毫秒数
        "note": str
    }

匹配规则：
- exact / prefix 模式按 URL path（不含 query）匹配；
- regex 模式按完整 path+query 匹配（re.search）；
- 第一个匹配的启用规则生效。
"""

import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import settings_store

DEFAULT_PORT = 18902
_MOCK_RULES_KEY = "mock_rules"
_MOCK_MULTI_MATCH_KEY = "mock_multi_match_rules"
_MAX_LOGS = 100

# 模块级请求日志（最近 100 条，存内存）
_logs_lock = threading.Lock()
_logs: "deque[dict]" = deque(maxlen=_MAX_LOGS)


# ---------- 规则读写 ----------

def _get_rules() -> list[dict]:
    """从 settings.json 读取 mock 规则列表。"""
    data = settings_store.get_setting(_MOCK_RULES_KEY, [])
    if isinstance(data, list):
        return data
    return []


def _save_rules(rules: list[dict]) -> None:
    """写回 mock 规则列表到 settings.json。"""
    settings_store.set_setting(_MOCK_RULES_KEY, rules)


def get_multi_match_rules() -> list[dict]:
    """从 settings.json 读取多条件匹配规则列表。"""
    data = settings_store.get_setting(_MOCK_MULTI_MATCH_KEY, [])
    return data if isinstance(data, list) else []


def save_multi_match_rules(rules: list[dict]) -> None:
    """写回多条件匹配规则列表到 settings.json。"""
    settings_store.set_setting(_MOCK_MULTI_MATCH_KEY, rules)


def render_template(text: str, variables: dict[str, str]) -> tuple[str, list[str]]:
    """把 ``{{var}}`` 占位符替换为变量值，返回 (渲染结果, 未定义变量名列表)。"""
    import string

    errors: list[str] = []

    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:
            errors.append(key)
            return ""

    try:
        result = string.Template(text or "").safe_substitute(_SafeDict(variables))
    except Exception:  # noqa: BLE001
        return text or "", errors
    # 同时支持 {{var}} 形式
    def _repl(m: "re.Match[str]") -> str:
        key = m.group(1).strip()
        if key in variables:
            return str(variables[key])
        errors.append(key)
        return m.group(0)

    result = re.sub(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", _repl, result)
    return result, errors


def _cond_match(cond: dict, ctx: dict) -> bool:
    """评估单个多条件匹配条件（所有字段均须满足才算命中）。"""
    field = (cond.get("field") or "").lower()
    op = (cond.get("operator") or "equals")
    if field == "header":
        target = str(ctx["headers"].get((cond.get("header_name") or "").lower(), ""))
    else:
        target = str(ctx.get(field, ""))
    value = str(cond.get("value") or "")
    if op == "equals":
        return target == value
    if op == "contains":
        return value in target
    if op == "startsWith":
        return target.startswith(value)
    if op == "endsWith":
        return target.endswith(value)
    if op == "regex":
        try:
            return re.search(value, target) is not None
        except re.error:
            return False
    if op == "exists":
        return bool(target)
    if op == "notExists":
        return not target
    return False


def _match_multi_rule(rule: dict, ctx: dict) -> bool:
    """规则的所有 conditions 全部满足才命中。"""
    conditions = rule.get("conditions") or []
    if not conditions:
        return False
    return all(_cond_match(c, ctx) for c in conditions)


def _match_path(path: str, pattern: str, mode: str) -> bool:
    """按 match_mode 匹配 path 与 pattern。

    exact: 完全相等；prefix: path 以 pattern 开头；regex: re.search。

    性能优化：regex 模式预编译缓存。
    """
    if not pattern:
        return False
    mode = (mode or "exact").lower()
    if mode == "regex":
        # 预编译缓存
        if not hasattr(_match_path, "_rx_cache"):
            _match_path._rx_cache = {}
        cache = _match_path._rx_cache
        compiled = cache.get(pattern)
        if compiled is None:
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
            except re.error:
                cache[pattern] = False
                return False
            cache[pattern] = compiled
        if compiled is False:
            return False
        return compiled.search(path or "") is not None
    if mode == "prefix":
        return (path or "").startswith(pattern)
    # exact（默认）
    return (path or "") == pattern


# ---------- MockServer ----------

class MockServer:
    """独立 Mock HTTP 服务器，后台线程运行。

    通过 ``get_mock_server()`` 获取单例。``port`` 为可变属性，可在 ``start()``
    前修改以切换监听端口；如需在运行中切换端口，先 ``stop()`` 再改端口再 ``start()``。
    """

    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._count_lock = threading.Lock()
        self._request_count = 0

    def is_running(self) -> bool:
        """服务器是否正在运行。"""
        return self._server is not None

    def get_request_count(self) -> int:
        """累计处理的请求数。"""
        with self._count_lock:
            return self._request_count

    def start(self) -> bool:
        """启动服务器（后台线程）。已在运行则返回 False。"""
        with self._lock:
            if self._server is not None:
                return False
            handler_cls = _make_handler(self)
            self._server = ThreadingHTTPServer(
                ("0.0.0.0", self.port), handler_cls
            )
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                daemon=True,
                name="mock-server",
            )
            self._thread.start()
            return True

    def stop(self) -> bool:
        """停止服务器。未运行则返回 False。"""
        with self._lock:
            if self._server is None:
                return False
            srv = self._server
            self._server = None
            self._thread = None
        # shutdown 会阻塞到 serve_forever 循环退出，放在锁外避免死锁
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass
        return True

    # ---------- 供 handler 回调 ----------

    def _match(self, method: str, path: str) -> dict | None:
        """根据 method + path 匹配第一个启用的规则，无匹配返回 None。"""
        rules = _get_rules()
        method_u = (method or "").upper()
        for r in rules:
            if not r.get("enabled"):
                continue
            rmethod = (r.get("method") or "").upper()
            if rmethod and rmethod != method_u:
                continue
            rpath = r.get("path") or ""
            mode = r.get("match_mode") or "exact"
            if _match_path(path, rpath, mode):
                return r
        return None

    def _match_multi(self, ctx: dict) -> dict | None:
        """多条件匹配：按 priority 降序尝试启用的规则，返回首个命中者。"""
        rules = [r for r in get_multi_match_rules() if r.get("enabled")]
        rules.sort(key=lambda r: int(r.get("priority") or 0), reverse=True)
        for r in rules:
            if _match_multi_rule(r, ctx):
                return r
        return None

    def _log_request(self, entry: dict) -> None:
        """记录一条请求日志到内存环形缓冲。"""
        with _logs_lock:
            _logs.append(entry)

    def _incr_count(self) -> None:
        with self._count_lock:
            self._request_count += 1


# ---------- 请求处理器 ----------

def _make_handler(server: "MockServer"):
    """构造一个绑定了 MockServer 实例的 handler 类。"""

    class MockHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _read_body(self) -> bytes:
            """读取请求体（即使不用也要消费，避免 keep-alive 错乱）。"""
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except (TypeError, ValueError):
                length = 0
            if length > 0:
                try:
                    return self.rfile.read(length)
                except Exception:  # noqa: BLE001
                    return b""
            return b""

        def _handle(self):
            from datetime import datetime
            t0 = time.time()
            server._incr_count()
            # 消费请求体
            raw_body = self._read_body()

            sp = urlsplit(self.path)
            path_only = sp.path or "/"
            full_path = self.path
            method = self.command

            # 构建多条件匹配上下文
            try:
                body_text = raw_body.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                body_text = ""
            ctx = {
                "method": method,
                "path": path_only,
                "host": self.headers.get("Host", ""),
                "query": sp.query or "",
                "body": body_text,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }

            # 多条件规则优先（有 priority），未命中再走简单规则
            rule = server._match_multi(ctx)
            if rule is not None:
                resp = rule.get("mock_response") or {}
                status = int(resp.get("status_code") or 200)
                headers = resp.get("headers") or {}
                if not isinstance(headers, dict):
                    headers = {}
                body_str = str(resp.get("body") or "")
                if resp.get("template_mode"):
                    variables: dict[str, str] = {}
                    # 从 query / JSON body 提取变量值
                    from urllib.parse import parse_qs
                    for k, vals in parse_qs(sp.query or "").items():
                        if vals:
                            variables[k] = vals[0]
                    try:
                        import json as _json
                        obj = _json.loads(body_text)
                        if isinstance(obj, dict):
                            for k, v in obj.items():
                                variables.setdefault(str(k), str(v))
                    except Exception:  # noqa: BLE001
                        pass
                    body_str, _errs = render_template(body_str, variables)
                body_bytes = body_str.encode("utf-8")
                try:
                    delay_ms = int(resp.get("delay_ms") or 0)
                except (TypeError, ValueError):
                    delay_ms = 0
            else:
                rule = server._match(method, path_only)
                if rule is None:
                    body = b"No mock rule matched"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except Exception:  # noqa: BLE001
                        pass
                    server._log_request({
                        "timestamp": datetime.now().isoformat(),
                        "method": method,
                        "path": full_path,
                        "matched_rule_id": None,
                        "matched_rule_note": "",
                        "status_code": 404,
                        "duration_ms": int((time.time() - t0) * 1000),
                    })
                    return

                # 延迟响应
                try:
                    delay_ms = int(rule.get("delay_ms") or 0)
                except (TypeError, ValueError):
                    delay_ms = 0

                try:
                    status = int(rule.get("status_code") or 200)
                except (TypeError, ValueError):
                    status = 200
                headers = rule.get("headers") or {}
                if not isinstance(headers, dict):
                    headers = {}
                body_bytes = (rule.get("body") or "").encode("utf-8")

            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)

            self.send_response(status)
            has_ct = any(k.lower() == "content-type" for k in headers)
            if not has_ct:
                ct = rule.get("content_type") or "text/plain; charset=utf-8"
                self.send_header("Content-Type", ct)
            for k, v in headers.items():
                self.send_header(k, str(v))
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header("Connection", "close")
            self.end_headers()
            if method != "HEAD":
                try:
                    self.wfile.write(body_bytes)
                except Exception:  # noqa: BLE001
                    pass

            server._log_request({
                "timestamp": datetime.now().isoformat(),
                "method": method,
                "path": full_path,
                "matched_rule_id": rule.get("id"),
                "matched_rule_note": rule.get("note") or "",
                "status_code": status,
                "duration_ms": int((time.time() - t0) * 1000),
            })

        def log_message(self, fmt, *args):  # noqa: A003
            """抑制默认的 stderr 日志输出。"""
            pass

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle
        do_DELETE = _handle
        do_PATCH = _handle
        do_HEAD = _handle
        do_OPTIONS = _handle

    return MockHandler


# ---------- 单例 ----------

_mock_server_instance: MockServer | None = None
_singleton_lock = threading.Lock()


def get_mock_server() -> MockServer:
    """获取 MockServer 单例（懒创建，默认端口 18902）。"""
    global _mock_server_instance
    with _singleton_lock:
        if _mock_server_instance is None:
            _mock_server_instance = MockServer(DEFAULT_PORT)
        return _mock_server_instance


# ---------- 请求日志 ----------

def get_mock_logs() -> list:
    """获取最近的请求日志（按时间倒序，最新在前）。"""
    with _logs_lock:
        return list(reversed(_logs))


def clear_mock_logs() -> None:
    """清空请求日志。"""
    with _logs_lock:
        _logs.clear()

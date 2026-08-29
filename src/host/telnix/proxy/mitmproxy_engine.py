"""mitmproxy engine integration: an optional high-performance proxy engine.

mitmproxy is a mature HTTPS proxy library with built-in SSL interception,
HTTP/2 support, upstream proxy and other capabilities. This module wraps it as
MitmproxyEngine, inheriting the built-in ProxyServer to stay compatible with the
interface the API layer depends on (capturing / session_id / breakpoint /
ssl_bump etc.), and overrides start()/stop() to run mitmproxy's asyncio event
loop in a dedicated thread.

When mitmproxy is not installed, the module can still be imported normally
(MITMPROXY_AVAILABLE=False); calling start() raises RuntimeError so the upper
layer falls back to the built-in engine.

Features:
- Traffic recording to DB (request/response hook)
- SSL interception (handled automatically by mitmproxy, reusing Telnix root cert)
- Upstream proxy (Clash integration: gets upstream proxy address from clash/client.py)
- Breakpoints (reuses BreakpointManager, with a dedicated thread pool + explicit
  timeout to avoid thread pool exhaustion)
- Auto-modify rules (mock / modify_request / modify_response, reusing server.py logic)
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import socket
import threading
import time
from collections import OrderedDict
from datetime import datetime
from urllib.parse import urlsplit

# ---------- mitmproxy 可选导入 ----------
# mitmproxy 是大型依赖（~50MB），不强制安装。未安装时模块仍可导入，
# MITMPROXY_AVAILABLE=False，调用 start() 时抛出 RuntimeError 让上层回退。
# 注意：
# 1. pyOpenSSL 与 cryptography 版本不兼容时，mitmproxy 导入链会抛
#    AttributeError（如 _lib.GEN_EMAIL 缺失），需视为"不可用"而非崩溃。
# 2. ProxyServer 类在不同 mitmproxy 版本位置不同，且 10.1+ 已移除该类
#    （DumpMaster 自行管理 server，无需手动创建）。MitmProxyServer 仅作为
#    可选导入，导入失败不影响 MITMPROXY_AVAILABLE。
try:
    import mitmproxy  # noqa: F401
    from mitmproxy import options
    from mitmproxy.tools.dump import DumpMaster
    MITMPROXY_AVAILABLE = True
    # ProxyServer 可选导入：mitmproxy 10.0 有 mitmproxy.proxy.ProxyServer，
    # 10.1+ 移除（DumpMaster 自行管理），11+ 有 mitmproxy.proxy.server.ProxyServer。
    # 导入失败不影响引擎可用性（start() 里会跳过手动设置）。
    MitmProxyServer = None
    for _import_path in (
        "mitmproxy.proxy.server",   # 11+
        "mitmproxy.proxy",          # 10.0
    ):
        try:
            import importlib
            _mod = importlib.import_module(_import_path)
            if hasattr(_mod, "ProxyServer"):
                MitmProxyServer = _mod.ProxyServer
                break
        except Exception:  # noqa: BLE001  # 扩大捕获范围，兼容 pyOpenSSL 版本不匹配
            continue
except Exception as _mitm_err:  # noqa: BLE001
    MITMPROXY_AVAILABLE = False
    MitmProxyServer = None  # type: ignore[assignment,misc]
    _mitm_err_type = type(_mitm_err).__name__
    print(f"[Telnix] mitmproxy engine unavailable ({_mitm_err_type}: {_mitm_err}). "
          f"Falling back to built-in engine. To enable, run pip install --upgrade mitmproxy pyOpenSSL cryptography.")

from .server import (
    ProxyServer,
    Headers,
    _truncate_for_record,
    _to_bytes,
    _parse_json,
    _apply_modify_request,
    _apply_modify_response,
)
from .. import db, logger
from ..ip_region import lookup as ip_region_lookup


# 断点等待专用线程池：避免耗尽 mitmproxy 默认 executor（min(32, cpu+4)）
# 断点等待是长时阻塞操作，混用默认 executor 会导致所有 async hook 排队卡死
_BREAKPOINT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=16, thread_name_prefix="mitm-bp"
)
# 断点默认超时（秒）：避免 agent 忘记 release 导致线程永久占用
_BREAKPOINT_DEFAULT_TIMEOUT = 300.0


# IP 属地查询缓存：避免同一 IP 重复查询（10min TTL，最多 500 条）
_IP_REGION_CACHE: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
_IP_REGION_TTL = 600.0
_IP_REGION_MAX = 500


def _cached_ip_region_lookup(ip: str) -> str:
    """IP region lookup cache with TTL (LRU + 10min expiry)."""
    now = time.monotonic()
    cached = _IP_REGION_CACHE.get(ip)
    if cached is not None:
        region, ts = cached
        if now - ts < _IP_REGION_TTL:
            _IP_REGION_CACHE.move_to_end(ip)
            return region
    try:
        region = ip_region_lookup(ip) or ""
    except Exception:  # noqa: BLE001
        region = ""
    _IP_REGION_CACHE[ip] = (region, now)
    _IP_REGION_CACHE.move_to_end(ip)
    while len(_IP_REGION_CACHE) > _IP_REGION_MAX:
        _IP_REGION_CACHE.popitem(last=False)
    return region


# ---------- 工具函数 ----------

def _mitm_headers_to_dict(hdrs) -> dict:
    """Convert mitmproxy multidict headers to a plain dict (first value kept for duplicate names)."""
    d = {}
    try:
        for k, v in hdrs.items(multi=True):
            if k not in d:
                d[k] = v
    except Exception:  # noqa: BLE001
        # 旧版 mitmproxy 不支持 multi 参数
        for k, v in hdrs.items():
            if k not in d:
                d[k] = v
    return d


def _write_dict_to_mitm_headers(hdrs, d: dict):
    """Convert dict to mitmproxy headers (clears then populates)."""
    hdrs.clear()
    for k, v in d.items():
        hdrs.add(k, str(v))


def _write_headers_to_mitm_headers(hdrs, h: Headers):
    """Convert Headers object to mitmproxy headers (clears then populates)."""
    hdrs.clear()
    for k, v in h._items:  # noqa: SLF001
        hdrs.add(k, v)


# ---------- 流量记录 addon ----------

class RecordingAddon:
    """mitmproxy addon: records traffic to DB + applies auto-reply rules + breakpoint support.

    All hooks are async coroutines; breakpoint waiting runs non-blocking via
    asyncio.to_thread to avoid blocking the mitmproxy event loop and starving
    other connections.
    """

    def __init__(self, engine: "MitmproxyEngine"):
        self.engine = engine

    def _should_record(self) -> bool:
        """Whether traffic should be recorded (capturing and has a session)."""
        return self.engine.capturing and self.engine.session_id is not None

    def _is_ignored(self, flow) -> bool:
        """Check whether ignore rules are hit (host wildcard + PID/process name)."""
        try:
            host = flow.request.host
            # 尝试获取客户端 PID/进程名（mitmproxy flow.client_conn.peername）
            pid, proc_name = self._get_client_pid(flow)
            return self.engine.is_ignored(pid, proc_name, host)
        except Exception:  # noqa: BLE001
            return False

    def _get_client_pid(self, flow) -> tuple[int | None, str | None]:
        """Get client PID and process name from a mitmproxy flow.

        mitmproxy's flow.client_conn.peername is (ip, port); reverse-lookup the
        PID via process_lookup (consistent with the built-in engine). Results are
        cached in flow.metadata to avoid repeated TCP table scans.
        """
        if flow.metadata.get("telnix_pid_cached"):
            return (flow.metadata.get("telnix_pid"),
                    flow.metadata.get("telnix_proc_name"))
        try:
            client_addr = flow.client_conn.peername
            if client_addr and self.engine.process_lookup:
                pid, proc_name = self.engine.process_lookup.lookup(client_addr)
                flow.metadata["telnix_pid"] = pid
                flow.metadata["telnix_proc_name"] = proc_name
                flow.metadata["telnix_pid_cached"] = True
                return pid, proc_name
        except Exception:  # noqa: BLE001
            pass
        flow.metadata["telnix_pid"] = None
        flow.metadata["telnix_proc_name"] = None
        flow.metadata["telnix_pid_cached"] = True
        return None, None

    def _get_server_conn_info(self, flow) -> tuple[str, str, str]:
        """Get server connection info from a mitmproxy flow.

        Returns (remote_ip, http_version, cert_info_json).
        - remote_ip: server IP (used for IP region lookup)
        - http_version: HTTP version (h2 / http/1.1 etc.)
        - cert_info_json: certificate info JSON (empty string means none)
        """
        remote_ip = ""
        http_version = ""
        cert_info_json = ""
        try:
            server_conn = flow.server_conn
            if server_conn and server_conn.peername:
                remote_ip = server_conn.peername[0] if isinstance(server_conn.peername, tuple) else str(server_conn.peername)
            # HTTP 版本
            if flow.response is not None:
                http_version = getattr(flow.response, "http_version", "") or ""
            if not http_version and flow.request is not None:
                http_version = getattr(flow.request, "http_version", "") or ""
        except Exception:  # noqa: BLE001
            pass
        # IP 属地查询：通过模块级 TTL-LRU 缓存避免同一 IP 重复查询
        ip_region = ""
        if remote_ip:
            ip_region = _cached_ip_region_lookup(remote_ip)
        return remote_ip, ip_region, http_version, cert_info_json

    # ---------- 请求 hook ----------

    async def request(self, flow):
        """Request phase: match rules + mock/modify_request + request breakpoint."""
        if not self._should_record():
            return
        if self._is_ignored(flow):
            return

        url = flow.request.url
        method = flow.request.method

        # 匹配自动回复规则（请求阶段，status_code=None）
        rule = self._match_rule(url, method=method, status_code=None, flow=flow)
        if rule:
            try:
                await asyncio.to_thread(db.increment_rule_hit, rule.get("id"), None)
            except Exception:  # noqa: BLE001
                pass
            # 标记该规则已在请求阶段计过命中，响应阶段不再重复计数
            flow.metadata["telnix_rule_counted"] = True
            action = rule.get("action")
            if action == "mock":
                # mock：直接返回伪造响应，不转发到服务器
                await self._handle_mock(flow, rule)
                return
            if action == "modify_request":
                self._apply_modify_request(flow, rule)
            if action == "mock_request":
                # mock_request：用预设请求转发到目标服务器，返回真实响应
                self._handle_mock_request(flow, rule)
                return
            if action == "script":
                # script：调用用户脚本 on_request（转发前），可改请求/返回 mock/drop
                self._handle_script_request(flow, rule)
                return

        # 请求断点：同步插入 DB 获取 flow_id，非阻塞等待用户放行
        if self.engine.breakpoint.should_break_request():
            await self._handle_request_breakpoint(flow)

    # ---------- 响应 hook ----------

    async def response(self, flow):
        """Response phase: match modify_response rules + response breakpoint + record to DB."""
        if not self._should_record():
            return
        if self._is_ignored(flow):
            return
        if flow.response is None:
            return

        # 如果请求阶段已因 mock 记录过完整 flow，跳过
        if flow.metadata.get("telnix_mock_recorded"):
            return

        url = flow.request.url
        method = flow.request.method
        status = flow.response.status_code

        # mock_request 已在请求 hook 用预设请求转发，响应按真实响应记录，
        # 不再二次匹配 modify_response/script（与自研引擎行为一致）
        if flow.metadata.get("telnix_mock_request"):
            rule = None
        else:
            # 响应阶段重新匹配（让带 status_filter 的 modify_response 规则生效）
            rule = self._match_rule(url, method=method, status_code=status, flow=flow)

        if rule and rule.get("action") == "modify_response":
            # 仅当请求阶段未计过该规则命中时才计数，避免与请求阶段重复统计
            # （带 status_filter 的规则只在响应阶段匹配，此处会补计一次）
            if not flow.metadata.get("telnix_rule_counted"):
                try:
                    await asyncio.to_thread(db.increment_rule_hit,
                                            rule.get("id"),
                                            flow.metadata.get("telnix_flow_id"))
                except Exception:  # noqa: BLE001
                    pass
            self._apply_modify_response(flow, rule)
        elif rule and rule.get("action") == "script":
            # script：调用用户脚本 on_response（返回客户端前），可改响应/返回 mock/drop
            if not flow.metadata.get("telnix_rule_counted"):
                try:
                    await asyncio.to_thread(db.increment_rule_hit,
                                            rule.get("id"),
                                            flow.metadata.get("telnix_flow_id"))
                except Exception:  # noqa: BLE001
                    pass
            stopped = await self._handle_script_response(flow, rule)
            if stopped:
                return

        # 响应断点
        if self.engine.breakpoint.should_break_response():
            await self._handle_response_breakpoint(flow)
            return

        # 普通记录：fire-and-forget 异步写入完整 flow（请求 + 响应）
        # 用 create_task 包装让 DB 写入不阻塞响应发送给客户端
        asyncio.create_task(asyncio.to_thread(self._record_full_flow, flow))

    # ---------- 错误 hook ----------

    async def error(self, flow):
        """Record on request failure (flows with no response)."""
        if not self._should_record():
            return
        if self._is_ignored(flow):
            return
        if flow.metadata.get("telnix_recorded") or \
           flow.metadata.get("telnix_mock_recorded"):
            return
        # 构建失败 flow（status=0 表示连接失败）
        flow_dict = self._build_flow_dict(flow, force_status=0)
        if flow_dict is not None:
            db.insert_flow_async(flow_dict)
            flow.metadata["telnix_recorded"] = True

    # ---------- 规则匹配 ----------

    def _match_rule(self, url, method=None, status_code=None, flow=None):
        """Match auto-reply rules, reusing the logic from auto_reply.rules.

        Passes in flow to get pid/process_name so rules with PID filter can take effect.
        """
        try:
            from ..auto_reply.rules import find_matching_rule
            pid = None
            process_name = None
            if flow is not None:
                pid, process_name = self._get_client_pid(flow)
            return find_matching_rule(
                url, method=method, status_code=status_code,
                pid=pid, process_name=process_name)
        except Exception:  # noqa: BLE001
            return None

    # ---------- mock ----------

    async def _handle_mock(self, flow, rule):
        """mock rule: directly returns a forged response without forwarding to the server."""
        status = int(rule.get("mock_status") or 200)
        headers = _parse_json(rule.get("mock_headers"))
        body = _to_bytes(rule.get("mock_body") or "")
        try:
            from mitmproxy import http as mitm_http
            flow.response = mitm_http.Response.make(
                status_code=status,
                content=body,
                headers=headers,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"Mock response creation failed: {e}", "")
        # 记录完整 flow（请求 + mock 响应）：用 asyncio.to_thread 避免同步属地查询阻塞事件循环
        if self._should_record():
            flow_dict = await asyncio.to_thread(
                self._build_flow_dict,
                flow, mock_status=status,
                mock_headers=headers, mock_body=body)
            if flow_dict is not None:
                db.insert_flow_async(flow_dict)
                flow.metadata["telnix_mock_recorded"] = True

    # ---------- 修改请求 ----------

    def _apply_modify_request(self, flow, rule):
        """Apply modify_request rule (reuses the modification logic from server.py)."""
        try:
            h = Headers()
            for k, v in flow.request.headers.items():
                h.add(k, v)
            body = flow.request.content or b""
            h, body = _apply_modify_request(h, body, rule)
            _write_headers_to_mitm_headers(flow.request.headers, h)
            flow.request.content = body
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"modify_request apply failed: {e}", "")

    # ---------- 修改响应 ----------

    def _apply_modify_response(self, flow, rule):
        """Apply modify_response rule (reuses server.py modification logic)."""
        try:
            h = Headers()
            for k, v in flow.response.headers.items():
                h.add(k, v)
            body = flow.response.content or b""
            status = flow.response.status_code
            status, h, body = _apply_modify_response(status, h, body, rule)
            flow.response.status_code = status
            _write_headers_to_mitm_headers(flow.response.headers, h)
            flow.response.content = body
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"modify_response apply failed: {e}", "")

    # ---------- mock_request ----------

    def _handle_mock_request(self, flow, rule):
        """mock_request rule: forward a preset method/url/headers/body to the target server and return the real response.

        Semantically consistent with the built-in engine: the request content is
        fixed but actually sent to the server and the real response is retrieved.
        Directly rewrites flow.request so mitmproxy forwards it naturally (the
        response hook handles recording).
        """
        try:
            m_method = (rule.get("mock_method") or "GET").upper()
            m_url = rule.get("mock_url") or flow.request.url
            sp = urlsplit(m_url)
            m_scheme = sp.scheme or flow.request.scheme or "https"
            m_host = sp.hostname or flow.request.host
            m_path = sp.path or "/"
            if sp.query:
                m_path = f"{m_path}?{sp.query}"
            m_headers = _parse_json(rule.get("mock_headers")) or {}
            m_body = _to_bytes(rule.get("mock_body") or "")
            hdrs = dict(m_headers)
            # 确保 Host 头存在（mitmproxy 需要正确的 Host 才能路由）
            if not any(k.lower() == "host" for k in hdrs) and m_host:
                hdrs["Host"] = m_host
            flow.request.method = m_method
            if m_url:
                flow.request.url = m_url
            _write_dict_to_mitm_headers(flow.request.headers, hdrs)
            flow.request.content = m_body
            flow.metadata["telnix_mock_request"] = True
            logger.info("mitmproxy", "mock_request: forward with preset request",
                        f"method={m_method}, url={m_url}, host={m_host}, body_len={len(m_body)}")
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"mock_request apply failed: {e}", "")

    # ---------- script（请求阶段） ----------

    def _handle_script_request(self, flow, rule):
        """script rule: call the user script's on_request, apply the result to the request, or mock/drop."""
        script = rule.get("modify_rules") or ""
        if isinstance(script, list):
            script = ""
        if not script.strip():
            logger.warning("mitmproxy", f"Script rule content is empty: {rule.get('id')}", "")
            return
        try:
            pid, process_name = self._get_client_pid(flow)
            from ..auto_reply.script_runner import (
                call_script_request, build_ctx)
            ctx = build_ctx(
                host=flow.request.host, path=flow.request.path,
                method=flow.request.method, url=flow.request.url,
                scheme=flow.request.scheme, pid=pid,
                process_name=process_name or "",
                session_id=self.engine.session_id,
                request_headers=_mitm_headers_to_dict(flow.request.headers),
                request_body=flow.request.content or b"",
            )
            resp = call_script_request(rule["id"], script, ctx)
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"script on_request exception: {rule.get('id')}", str(e))
            return
        if resp is None:
            logger.warning("mitmproxy", f"script on_request call failed: {rule.get('id')}",
                           "Script unavailable, request forwarded as-is")
            return
        action = resp.get("action", "continue")
        if action == "drop":
            logger.info("mitmproxy", f"script drop request: {flow.request.url}", "")
            flow.kill()
            return
        if action == "mock":
            self._set_mock_response(flow, resp, rule.get("id"))
            return
        # continue：应用请求头/体修改
        try:
            if resp.get("request_headers"):
                _write_dict_to_mitm_headers(
                    flow.request.headers, resp["request_headers"])
            if resp.get("request_body_b64"):
                try:
                    flow.request.content = base64.b64decode(resp["request_body_b64"])
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"script apply request modification failed: {rule.get('id')}", str(e))

    # ---------- script（响应阶段） ----------

    async def _handle_script_response(self, flow, rule) -> bool:
        """script rule: call the user script's on_response. Returns True if dropped (response hook should abort recording)."""
        script = rule.get("modify_rules") or ""
        if isinstance(script, list):
            script = ""
        if not script.strip():
            logger.warning("mitmproxy", f"Script rule content is empty: {rule.get('id')}", "")
            return False
        try:
            pid, process_name = self._get_client_pid(flow)
            from ..auto_reply.script_runner import (
                call_script_response, build_ctx)
            ctx = build_ctx(
                host=flow.request.host, path=flow.request.path,
                method=flow.request.method, url=flow.request.url,
                scheme=flow.request.scheme, pid=pid,
                process_name=process_name or "",
                session_id=self.engine.session_id,
                request_headers=_mitm_headers_to_dict(flow.request.headers),
                request_body=flow.request.content or b"",
                status_code=flow.response.status_code,
                response_headers=_mitm_headers_to_dict(flow.response.headers),
                response_body=flow.response.content or b"",
            )
            resp = call_script_response(rule["id"], script, ctx)
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"script on_response exception: {rule.get('id')}", str(e))
            return False
        if resp is None:
            logger.warning("mitmproxy", f"script on_response call failed: {rule.get('id')}",
                           "Script unavailable, response returned as-is")
            return False
        action = resp.get("action", "continue")
        if action == "drop":
            logger.info("mitmproxy", f"script drop response: {flow.request.url}", "")
            flow.kill()
            return True
        if action == "mock":
            self._set_mock_response(flow, resp, rule.get("id"))
            return False
        # continue：应用响应头/体/状态码修改
        try:
            if resp.get("response_headers"):
                _write_dict_to_mitm_headers(
                    flow.response.headers, resp["response_headers"])
            if resp.get("response_body_b64"):
                try:
                    flow.response.content = base64.b64decode(resp["response_body_b64"])
                except Exception:  # noqa: BLE001
                    pass
            if resp.get("status_code") is not None:
                try:
                    flow.response.status_code = int(resp["status_code"])
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"script apply response modification failed: {rule.get('id')}", str(e))
        return False

    def _set_mock_response(self, flow, resp: dict, rule_id):
        """Overwrite flow.response with the script-returned mock response and record the full flow."""
        try:
            from mitmproxy import http as mitm_http
            status = int(resp.get("mock_status") or 200)
            headers = _parse_json(resp.get("mock_headers")) or {}
            body = b""
            if resp.get("mock_body_b64"):
                try:
                    body = base64.b64decode(resp["mock_body_b64"])
                except Exception:  # noqa: BLE001
                    pass
            flow.response = mitm_http.Response.make(
                status_code=status, content=body, headers=headers)
            if self._should_record():
                # 同步构造 mock flow dict 并异步写入 DB（不阻塞事件循环）
                flow_dict = self._build_flow_dict(
                    flow, mock_status=status, mock_headers=headers, mock_body=body)
                if flow_dict is not None:
                    db.insert_flow_async(flow_dict)
                    flow.metadata["telnix_mock_recorded"] = True
            logger.info("mitmproxy", f"script mock response: {flow.request.url} -> {status}", "")
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"script mock response failed: {rule_id}", str(e))

    # ---------- 请求断点 ----------

    async def _handle_request_breakpoint(self, flow):
        """Request breakpoint: synchronously insert into DB to get flow_id, then non-blocking wait for user release.

        Uses a dedicated thread pool (_BREAKPOINT_EXECUTOR) to avoid exhausting
        mitmproxy's default executor, and sets a default timeout (300s) to prevent
        threads from being held forever when the agent forgets to release.
        """
        flow_id = await asyncio.to_thread(self._insert_flow_sync, flow)
        if flow_id is None:
            return
        flow.metadata["telnix_flow_id"] = flow_id
        await asyncio.to_thread(db.update_flow_breakpoint, flow_id, "pending_request")
        # 非阻塞等待：在专用线程池中执行 Event.wait，不阻塞 mitmproxy 事件循环
        # 显式超时避免线程池耗尽（原实现默认 0=永不超时，会导致所有 async hook 卡死）
        loop = asyncio.get_running_loop()
        action = await loop.run_in_executor(
            _BREAKPOINT_EXECUTOR,
            lambda: self.engine.breakpoint.wait_for_release(
                flow_id, timeout=_BREAKPOINT_DEFAULT_TIMEOUT))
        if action == "drop":
            flow.kill()
            await asyncio.to_thread(db.update_flow_breakpoint, flow_id, None)
            return
        # 读取用户修改后的请求并应用
        modified = await asyncio.to_thread(db.get_flow, flow_id)
        if modified:
            self._apply_request_modifications(flow, modified)
        await asyncio.to_thread(db.update_flow_breakpoint, flow_id, None)
        # 标记请求已记录（不阻止响应阶段更新响应字段）
        # 原实现设 telnix_recorded=True 会导致 _record_full_flow 跳过响应更新
        flow.metadata["telnix_request_recorded"] = True

    # ---------- 响应断点 ----------

    async def _handle_response_breakpoint(self, flow):
        """Response breakpoint: update DB response fields, then non-blocking wait for user release."""
        flow_id = flow.metadata.get("telnix_flow_id")
        if flow_id is None:
            # 请求阶段未插入（可能请求断点未开），现在同步插入完整 flow
            flow_id = await asyncio.to_thread(self._insert_flow_sync, flow, True)
            if flow_id is None:
                return
            flow.metadata["telnix_flow_id"] = flow_id
        else:
            # 请求阶段已插入，更新响应字段
            await asyncio.to_thread(self._update_flow_response_sync, flow_id, flow)
        await asyncio.to_thread(db.update_flow_breakpoint, flow_id, "pending_response")
        loop = asyncio.get_running_loop()
        action = await loop.run_in_executor(
            _BREAKPOINT_EXECUTOR,
            lambda: self.engine.breakpoint.wait_for_release(
                flow_id, timeout=_BREAKPOINT_DEFAULT_TIMEOUT))
        if action == "drop":
            flow.kill()
            await asyncio.to_thread(db.update_flow_breakpoint, flow_id, None)
            return
        modified = await asyncio.to_thread(db.get_flow, flow_id)
        if modified:
            self._apply_response_modifications(flow, modified)
        await asyncio.to_thread(db.update_flow_breakpoint, flow_id, None)
        # 响应断点放行后标记完整记录已完成
        flow.metadata["telnix_recorded"] = True

    # ---------- DB 记录 ----------

    def _record_full_flow(self, flow):
        """Record the full flow (request + response) to DB.

        - If not recorded in the request phase (no telnix_flow_id), async-insert the full flow
        - If already recorded in the request phase (telnix_flow_id present after request
          breakpoint release), only update the response fields to avoid losing them
        """
        # 请求断点放行后已有 flow_id，但响应字段尚未写入 → 更新响应
        flow_id = flow.metadata.get("telnix_flow_id")
        if flow_id is not None and flow.metadata.get("telnix_request_recorded"):
            # 请求阶段已插入，现在更新响应字段
            self._update_flow_response_sync(flow_id, flow)
            flow.metadata["telnix_recorded"] = True
            return
        # 普通路径：异步插入完整 flow
        if flow.metadata.get("telnix_recorded"):
            return
        flow_dict = self._build_flow_dict(flow)
        if flow_dict is None:
            return
        db.insert_flow_async(flow_dict)
        flow.metadata["telnix_recorded"] = True

    def _insert_flow_sync(self, flow, with_response=False) -> int | None:
        """Synchronously insert a flow into the DB, returns flow_id (used in breakpoint scenarios)."""
        flow_dict = self._build_flow_dict(
            flow, include_response=with_response)
        if flow_dict is None:
            return None
        try:
            return db.insert_flow(flow_dict)
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"Failed to sync insert flow: {e}", "")
            return None

    def _update_flow_response_sync(self, flow_id, flow):
        """Synchronously update response fields to DB (must be visible immediately before breakpoint release)."""
        try:
            status = flow.response.status_code
            resp_headers = json.dumps(
                _mitm_headers_to_dict(flow.response.headers))
            resp_body = _truncate_for_record(flow.response.content or b"")
            duration = self._calc_duration(flow)
            size = len(flow.response.content or b"")
            db.update_flow_response(
                flow_id, status, resp_headers, resp_body, duration, size)
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"Failed to sync update response: {e}", "")

    # ---------- flow dict 构建 ----------

    def _build_flow_dict(self, flow, mock_status=None, mock_headers=None,
                         mock_body=None, force_status=None,
                         include_response=True) -> dict | None:
        """Build a DB flow dict from a mitmproxy flow.

        Fills in fields: pid / process_name / remote_ip / ip_region / http_version /
        cert_info / breakpoint_status, aligned with the fields recorded by the
        built-in engine.
        """
        try:
            req = flow.request
            scheme = req.scheme or "http"
            host = req.host or ""
            path = req.path or "/"
            method = req.method or "GET"
            url = req.url or f"{scheme}://{host}{path}"
            req_headers = json.dumps(_mitm_headers_to_dict(req.headers))
            req_body = _truncate_for_record(req.content or b"")

            # 获取客户端 PID/进程名
            pid, process_name = self._get_client_pid(flow)
            # 获取服务器连接信息（remote_ip, ip_region, http_version, cert_info）
            remote_ip, ip_region, http_version, cert_info_json = \
                self._get_server_conn_info(flow)

            flow_dict = {
                "session_id": self.engine.session_id,
                "timestamp": datetime.now().isoformat(),
                "pid": pid,
                "process_name": process_name,
                "method": method,
                "url": url,
                "scheme": scheme,
                "host": host,
                "path": path,
                "request_headers": req_headers,
                "request_body": req_body,
                "remote_ip": remote_ip or None,
                "ip_region": ip_region or None,
                "http_version": http_version or None,
                "cert_info": cert_info_json or None,
            }

            # mock 响应
            if mock_status is not None:
                flow_dict["status_code"] = mock_status
                flow_dict["response_headers"] = json.dumps(mock_headers or {})
                flow_dict["response_body"] = _truncate_for_record(mock_body or b"")
                flow_dict["duration_ms"] = 0
                flow_dict["size"] = len(mock_body or b"")
            # 错误（连接失败）
            elif force_status is not None:
                flow_dict["status_code"] = force_status
                flow_dict["response_headers"] = "{}"
                flow_dict["response_body"] = ""
                flow_dict["duration_ms"] = self._calc_duration(flow)
                flow_dict["size"] = 0
            # 真实响应
            elif include_response and flow.response is not None:
                resp = flow.response
                flow_dict["status_code"] = resp.status_code
                flow_dict["response_headers"] = json.dumps(
                    _mitm_headers_to_dict(resp.headers))
                flow_dict["response_body"] = _truncate_for_record(
                    resp.content or b"")
                flow_dict["duration_ms"] = self._calc_duration(flow)
                flow_dict["size"] = len(resp.content or b"")

            return flow_dict
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"Failed to build flow dict: {e}", "")
            return None

    def _calc_duration(self, flow) -> int:
        """Calculate request duration (milliseconds)."""
        try:
            if flow.response and flow.request:
                return int(
                    (flow.response.timestamp_end -
                     flow.request.timestamp_start) * 1000)
        except Exception:  # noqa: BLE001
            pass
        return 0

    # ---------- 用户修改应用 ----------

    def _apply_request_modifications(self, flow, modified: dict):
        """Apply the user-modified request from DB to the mitmproxy flow."""
        try:
            if modified.get("method"):
                flow.request.method = modified["method"]
            if modified.get("url"):
                flow.request.url = modified["url"]
            if modified.get("request_headers"):
                hdrs = _parse_json(modified["request_headers"])
                _write_dict_to_mitm_headers(flow.request.headers, hdrs)
            if modified.get("request_body") is not None:
                flow.request.content = _to_bytes(modified["request_body"])
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"Failed to apply request modification: {e}", "")

    def _apply_response_modifications(self, flow, modified: dict):
        """Apply the user-modified response from DB to the mitmproxy flow."""
        if flow.response is None:
            return
        try:
            if modified.get("status_code"):
                flow.response.status_code = int(modified["status_code"])
            if modified.get("response_headers"):
                hdrs = _parse_json(modified["response_headers"])
                _write_dict_to_mitm_headers(flow.response.headers, hdrs)
            if modified.get("response_body") is not None:
                flow.response.content = _to_bytes(modified["response_body"])
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"Failed to apply response modification: {e}", "")


# ---------- mitmproxy 引擎 ----------

class MitmproxyEngine(ProxyServer):
    """mitmproxy proxy engine, compatible with the ProxyServer interface.

    Inherits ProxyServer to reuse properties and methods relied on by the API layer:
    - capturing / session_id: capture status (set by capture API)
    - breakpoint: BreakpointManager (operated by breakpoint API)
    - ssl_bump / cert_installed: certificate status (read by cert API)
    - host / port / _running: proxy status (read by status API)
    - is_ignored / refresh_ignored: ignore rules
    - _h2_pool / _pinning_suspected etc.: internal properties accessed by the API layer

    Overrides start()/stop() to run the mitmproxy event loop in a daemon thread.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8888,
                 ssl_bump=None):
        super().__init__(host=host, port=port, ssl_bump=ssl_bump)
        self._master: "DumpMaster | None" = None
        self._thread: threading.Thread | None = None
        self._opts = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _get_mitmproxy_mode(self) -> str:
        """Get the mitmproxy mode.

        - regular: connect directly to the target server
        - upstream:http://host:port: forward via Clash/Mihomo upstream proxy
        """
        try:
            from ..clash.client import get_upstream_proxy
            upstream = get_upstream_proxy()
            if upstream:
                host, port = upstream
                return f"upstream:http://{host}:{port}"
        except Exception:  # noqa: BLE001
            pass
        return "regular"

    def start(self):
        """Start the mitmproxy engine (runs the asyncio event loop in a daemon thread).

        mitmproxy has its own event loop; master.run() (blocking) runs in a
        dedicated thread. stop() calls master.shutdown() to notify the event loop
        to exit.
        """
        if not MITMPROXY_AVAILABLE:
            raise RuntimeError(
                "mitmproxy is not installed; cannot start the mitmproxy engine. "
                "Please run pip install mitmproxy to install it.")

        # 构建 mitmproxy 选项
        mode = self._get_mitmproxy_mode()
        kwargs = {
            "listen_host": self.host,
            "listen_port": self.port,
            "mode": [mode],
            "flow_detail": 0,  # 不在控制台打印流量详情
        }
        # 复用 Telnix 根证书：用户已安装 Telnix CA，
        # 让 mitmproxy 用同一张根证书签发叶证书，避免安装第二个 CA
        # 注意：不设置 confdir，让 mitmproxy 用默认配置目录管理叶证书缓存，
        # 避免与 Telnix 的 data/certs 混在一起导致每次 TLS 握手重新签发证书
        if self.ssl_bump:
            if hasattr(self.ssl_bump, "root_cert_path"):
                kwargs["ca_cert"] = self.ssl_bump.root_cert_path
            if hasattr(self.ssl_bump, "root_key_path"):
                kwargs["ca_key"] = self.ssl_bump.root_key_path

        try:
            self._opts = options.Options(**kwargs)
        except Exception as e:  # noqa: BLE001
            # 部分选项在不同 mitmproxy 版本可能不支持，逐个尝试
            logger.warning("mitmproxy", f"Option build failed, trying simplified options: {e}", "")
            self._opts = options.Options(
                listen_host=self.host,
                listen_port=self.port,
            )

        # 创建独立事件循环（DumpMaster 构造时调用 asyncio.get_running_loop()，
        # 但 start() 在同步线程调用无运行中的循环。显式创建并通过 loop 参数传入）
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        # 创建 DumpMaster（关闭 termlog/dumper 减少控制台输出）
        try:
            self._master = DumpMaster(
                self._opts, loop=self._loop,
                with_termlog=False, with_dumper=False)
        except TypeError:
            # 旧版 mitmproxy 无 loop/with_termlog/with_dumper 参数
            try:
                self._master = DumpMaster(
                    self._opts, with_termlog=False, with_dumper=False)
            except TypeError:
                self._master = DumpMaster(self._opts)

        # 显式设置 ProxyServer：仅当 MitmProxyServer 可用且 master.server 未初始化时
        # mitmproxy 10.1+ 的 DumpMaster 自行管理 server，无需手动设置
        if MitmProxyServer is not None:
            try:
                if getattr(self._master, "server", None) is None:
                    self._master.server = MitmProxyServer(self._opts)
            except Exception as e:  # noqa: BLE001
                logger.warning("mitmproxy", f"ProxyServer setup skipped: {e}", "")

        # 添加流量记录 addon
        self._master.addons.add(RecordingAddon(self))

        self._running = True
        self.refresh_ignored()

        # 在守护线程中运行 mitmproxy 事件循环（master.run 是协程，阻塞直到 shutdown）
        def _run():
            # 线程内绑定事件循环（与 DumpMaster 构造时传入的 loop 一致）
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._master.run())
            except Exception as e:  # noqa: BLE001
                logger.error("mitmproxy", f"Event loop exited with exception: {e}", "")
            finally:
                self._running = False

        self._thread = threading.Thread(
            target=_run, daemon=True, name="mitmproxy-engine")
        self._thread.start()
        logger.info("mitmproxy",
                    f"mitmproxy engine started: {self.host}:{self.port}, mode={mode}",
                    f"ca_cert={kwargs.get('ca_cert', '(mitmproxy default)')}")

    def stop(self):
        """Stop the mitmproxy engine: notify the event loop to exit + wait for the thread to finish.

        Uses call_soon_threadsafe to ensure shutdown runs inside the event loop
        thread, avoiding RuntimeError / resource leaks from cross-thread event
        loop operations.
        """
        self._running = False
        master = self._master
        loop = self._loop
        thread = self._thread

        if master is not None and loop is not None:
            try:
                # 通过 call_soon_threadsafe 在事件循环线程内执行 shutdown
                # 避免跨线程直接调用 master.shutdown() 的竞态
                loop.call_soon_threadsafe(master.shutdown)
            except Exception as e:  # noqa: BLE001
                logger.warning("mitmproxy", f"shutdown schedule failed: {e}", "")
                # 回退：直接调用（虽然不安全，但比什么都不做好）
                try:
                    master.shutdown()
                except Exception as e2:  # noqa: BLE001
                    logger.warning("mitmproxy", f"shutdown direct call failed: {e2}", "")

        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("mitmproxy", "Engine thread still running after 5s", "")
            self._thread = None

        # 关闭事件循环（确保线程已退出后再 close，避免 RuntimeError: Cannot close a running event loop）
        if loop is not None:
            try:
                # 取消所有剩余任务
                try:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                except Exception:  # noqa: BLE001
                    pass
                loop.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("mitmproxy", f"loop.close exception: {e}", "")
            self._loop = None

        self._master = None

        # 关闭继承自 ProxyServer 的连接池（虽然 mitmproxy 不用它们，但 API 层可能访问）
        try:
            self._conn_pool.close_all()
            self._h2_pool.close_all()
        except Exception:  # noqa: BLE001
            pass

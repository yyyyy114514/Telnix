"""mitmproxy 引擎集成：作为可选的高性能代理引擎。

mitmproxy 是成熟的 HTTPS 代理库，自带 SSL 拦截、HTTP/2 支持、上游代理等能力。
本模块将其封装为 MitmproxyEngine，继承内置 ProxyServer 以兼容 API 层依赖的接口
（capturing / session_id / breakpoint / ssl_bump 等属性），覆写 start()/stop()
在独立线程中运行 mitmproxy 的 asyncio 事件循环。

mitmproxy 未安装时模块仍可正常导入（MITMPROXY_AVAILABLE=False），调用 start()
时抛出 RuntimeError 让上层回退到内置引擎。

功能：
- 流量记录到 DB（request/response hook）
- SSL 拦截（mitmproxy 自动处理，复用 Telnix 根证书）
- 上游代理（Clash 集成：从 clash/client.py 获取上游代理地址）
- 断点（复用 BreakpointManager，通过 async hook + asyncio.to_thread 非阻塞等待）
- 自动修改规则（mock / modify_request / modify_response，复用 server.py 的修改逻辑）
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime

# ---------- mitmproxy 可选导入 ----------
# mitmproxy 是大型依赖（~50MB），不强制安装。未安装时模块仍可导入，
# MITMPROXY_AVAILABLE=False，调用 start() 时抛出 RuntimeError 让上层回退。
try:
    import mitmproxy  # noqa: F401
    from mitmproxy import options
    from mitmproxy.tools.dump import DumpMaster
    from mitmproxy.proxy import ProxyServer as MitmProxyServer
    MITMPROXY_AVAILABLE = True
except ImportError:
    MITMPROXY_AVAILABLE = False
    print("[Telnix] mitmproxy 未安装，mitmproxy 引擎不可用。"
          "可执行 pip install mitmproxy 启用。")

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


# ---------- 工具函数 ----------

def _mitm_headers_to_dict(hdrs) -> dict:
    """mitmproxy multidict headers → 普通 dict（同名头保留首个值）。"""
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
    """dict → mitmproxy headers（清空后填充）。"""
    hdrs.clear()
    for k, v in d.items():
        hdrs.add(k, str(v))


def _write_headers_to_mitm_headers(hdrs, h: Headers):
    """Headers 对象 → mitmproxy headers（清空后填充）。"""
    hdrs.clear()
    for k, v in h._items:  # noqa: SLF001
        hdrs.add(k, v)


# ---------- 流量记录 addon ----------

class RecordingAddon:
    """mitmproxy addon：记录流量到 DB + 应用自动回复规则 + 断点支持。

    所有 hook 为 async 协程，断点等待通过 asyncio.to_thread 非阻塞执行，
    避免 mitmproxy 事件循环被阻塞导致其他连接无法处理。
    """

    def __init__(self, engine: "MitmproxyEngine"):
        self.engine = engine

    def _should_record(self) -> bool:
        """是否应记录流量（抓包中且有会话）。"""
        return self.engine.capturing and self.engine.session_id is not None

    def _is_ignored(self, flow) -> bool:
        """检查是否命中忽略规则（host 通配符）。"""
        try:
            host = flow.request.host
            return self.engine.is_ignored(None, None, host)
        except Exception:  # noqa: BLE001
            return False

    # ---------- 请求 hook ----------

    async def request(self, flow):
        """请求阶段：匹配规则 + mock/modify_request + 请求断点。"""
        if not self._should_record():
            return
        if self._is_ignored(flow):
            return

        url = flow.request.url
        method = flow.request.method

        # 匹配自动回复规则（请求阶段，status_code=None）
        rule = self._match_rule(url, method=method, status_code=None)
        if rule:
            try:
                db.increment_rule_hit(rule.get("id"), None)
            except Exception:  # noqa: BLE001
                pass
            action = rule.get("action")
            if action == "mock":
                # mock：直接返回伪造响应，不转发到服务器
                self._handle_mock(flow, rule)
                return
            if action == "modify_request":
                self._apply_modify_request(flow, rule)

        # 请求断点：同步插入 DB 获取 flow_id，非阻塞等待用户放行
        if self.engine.breakpoint.should_break_request():
            await self._handle_request_breakpoint(flow)

    # ---------- 响应 hook ----------

    async def response(self, flow):
        """响应阶段：匹配 modify_response 规则 + 响应断点 + 记录到 DB。"""
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

        # 响应阶段重新匹配（让带 status_filter 的 modify_response 规则生效）
        rule = self._match_rule(url, method=method, status_code=status)
        if rule and rule.get("action") == "modify_response":
            try:
                db.increment_rule_hit(rule.get("id"),
                                      flow.metadata.get("telnix_flow_id"))
            except Exception:  # noqa: BLE001
                pass
            self._apply_modify_response(flow, rule)

        # 响应断点
        if self.engine.breakpoint.should_break_response():
            await self._handle_response_breakpoint(flow)
            return

        # 普通记录：异步写入完整 flow（请求 + 响应）
        self._record_full_flow(flow)

    # ---------- 错误 hook ----------

    async def error(self, flow):
        """请求失败时记录（无响应的流量）。"""
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

    def _match_rule(self, url, method=None, status_code=None):
        """匹配自动回复规则，复用 auto_reply.rules 的逻辑。"""
        try:
            from ..auto_reply.rules import find_matching_rule
            return find_matching_rule(
                url, method=method, status_code=status_code)
        except Exception:  # noqa: BLE001
            return None

    # ---------- mock ----------

    def _handle_mock(self, flow, rule):
        """mock 规则：直接返回伪造响应，不转发到服务器。"""
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
            logger.warning("mitmproxy", f"mock 响应创建失败: {e}", "")
        # 记录完整 flow（请求 + mock 响应）
        if self._should_record():
            flow_dict = self._build_flow_dict(
                flow, mock_status=status,
                mock_headers=headers, mock_body=body)
            if flow_dict is not None:
                db.insert_flow_async(flow_dict)
                flow.metadata["telnix_mock_recorded"] = True

    # ---------- 修改请求 ----------

    def _apply_modify_request(self, flow, rule):
        """应用 modify_request 规则（复用 server.py 的修改逻辑）。"""
        try:
            h = Headers()
            for k, v in flow.request.headers.items():
                h.add(k, v)
            body = flow.request.content or b""
            h, body = _apply_modify_request(h, body, rule)
            _write_headers_to_mitm_headers(flow.request.headers, h)
            flow.request.content = body
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"modify_request 应用失败: {e}", "")

    # ---------- 修改响应 ----------

    def _apply_modify_response(self, flow, rule):
        """应用 modify_response 规则（复用 server.py 的修改逻辑）。"""
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
            logger.warning("mitmproxy", f"modify_response 应用失败: {e}", "")

    # ---------- 请求断点 ----------

    async def _handle_request_breakpoint(self, flow):
        """请求断点：同步插入 DB 获取 flow_id，非阻塞等待用户放行。"""
        flow_id = self._insert_flow_sync(flow)
        if flow_id is None:
            return
        flow.metadata["telnix_flow_id"] = flow_id
        db.update_flow_breakpoint(flow_id, "pending_request")
        # 非阻塞等待：在独立线程中执行 Event.wait，不阻塞 mitmproxy 事件循环
        action = await asyncio.to_thread(
            self.engine.breakpoint.wait_for_release, flow_id)
        if action == "drop":
            flow.kill()
            db.update_flow_breakpoint(flow_id, None)
            return
        # 读取用户修改后的请求并应用
        modified = db.get_flow(flow_id)
        if modified:
            self._apply_request_modifications(flow, modified)
        db.update_flow_breakpoint(flow_id, None)
        flow.metadata["telnix_recorded"] = True

    # ---------- 响应断点 ----------

    async def _handle_response_breakpoint(self, flow):
        """响应断点：更新 DB 响应字段，非阻塞等待用户放行。"""
        flow_id = flow.metadata.get("telnix_flow_id")
        if flow_id is None:
            # 请求阶段未插入（可能请求断点未开），现在同步插入完整 flow
            flow_id = self._insert_flow_sync(flow, with_response=True)
            if flow_id is None:
                return
            flow.metadata["telnix_flow_id"] = flow_id
        else:
            # 请求阶段已插入，更新响应字段
            self._update_flow_response_sync(flow_id, flow)
        db.update_flow_breakpoint(flow_id, "pending_response")
        action = await asyncio.to_thread(
            self.engine.breakpoint.wait_for_release, flow_id)
        if action == "drop":
            flow.kill()
            db.update_flow_breakpoint(flow_id, None)
            return
        modified = db.get_flow(flow_id)
        if modified:
            self._apply_response_modifications(flow, modified)
        db.update_flow_breakpoint(flow_id, None)

    # ---------- DB 记录 ----------

    def _record_full_flow(self, flow):
        """异步记录完整 flow（请求 + 响应）到 DB。"""
        if flow.metadata.get("telnix_recorded"):
            return
        flow_dict = self._build_flow_dict(flow)
        if flow_dict is None:
            return
        db.insert_flow_async(flow_dict)
        flow.metadata["telnix_recorded"] = True

    def _insert_flow_sync(self, flow, with_response=False) -> int | None:
        """同步插入 flow 到 DB，返回 flow_id（断点场景用）。"""
        flow_dict = self._build_flow_dict(
            flow, include_response=with_response)
        if flow_dict is None:
            return None
        try:
            return db.insert_flow(flow_dict)
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"同步插入 flow 失败: {e}", "")
            return None

    def _update_flow_response_sync(self, flow_id, flow):
        """同步更新响应字段到 DB（断点放行前需要立即可见）。"""
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
            logger.warning("mitmproxy", f"同步更新响应失败: {e}", "")

    # ---------- flow dict 构建 ----------

    def _build_flow_dict(self, flow, mock_status=None, mock_headers=None,
                         mock_body=None, force_status=None,
                         include_response=True) -> dict | None:
        """从 mitmproxy flow 构建 DB flow dict。"""
        try:
            req = flow.request
            scheme = req.scheme or "http"
            host = req.host or ""
            path = req.path or "/"
            method = req.method or "GET"
            url = req.url or f"{scheme}://{host}{path}"
            req_headers = json.dumps(_mitm_headers_to_dict(req.headers))
            req_body = _truncate_for_record(req.content or b"")

            flow_dict = {
                "session_id": self.engine.session_id,
                "timestamp": datetime.now().isoformat(),
                "pid": None,
                "process_name": None,
                "method": method,
                "url": url,
                "scheme": scheme,
                "host": host,
                "path": path,
                "request_headers": req_headers,
                "request_body": req_body,
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
            logger.warning("mitmproxy", f"构建 flow dict 失败: {e}", "")
            return None

    def _calc_duration(self, flow) -> int:
        """计算请求耗时（毫秒）。"""
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
        """将 DB 中用户修改后的请求应用到 mitmproxy flow。"""
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
            logger.warning("mitmproxy", f"应用请求修改失败: {e}", "")

    def _apply_response_modifications(self, flow, modified: dict):
        """将 DB 中用户修改后的响应应用到 mitmproxy flow。"""
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
            logger.warning("mitmproxy", f"应用响应修改失败: {e}", "")


# ---------- mitmproxy 引擎 ----------

class MitmproxyEngine(ProxyServer):
    """mitmproxy 代理引擎，兼容 ProxyServer 接口。

    继承 ProxyServer 以复用 API 层依赖的属性和方法：
    - capturing / session_id：抓包状态（capture API 设置）
    - breakpoint：BreakpointManager（断点 API 操作）
    - ssl_bump / cert_installed：证书状态（cert API 读取）
    - host / port / _running：代理状态（status API 读取）
    - is_ignored / refresh_ignored：忽略规则
    - _h2_pool / _pinning_suspected 等：API 层访问的内部属性

    覆写 start()/stop() 以在守护线程中运行 mitmproxy 事件循环。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8888,
                 ssl_bump=None):
        super().__init__(host=host, port=port, ssl_bump=ssl_bump)
        self._master: "DumpMaster | None" = None
        self._thread: threading.Thread | None = None
        self._opts = None

    def _get_mitmproxy_mode(self) -> str:
        """获取 mitmproxy mode。

        - regular：直连目标服务器
        - upstream:http://host:port：通过 Clash/Mihomo 上游代理转发
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
        """启动 mitmproxy 引擎（在守护线程中运行 asyncio 事件循环）。

        mitmproxy 有自己的事件循环，在独立线程中运行 master.run()（阻塞），
        stop() 时调用 master.shutdown() 通知事件循环退出。
        """
        if not MITMPROXY_AVAILABLE:
            raise RuntimeError(
                "mitmproxy 未安装，无法启动 mitmproxy 引擎。"
                "请执行 pip install mitmproxy 安装。")

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
        if self.ssl_bump:
            if hasattr(self.ssl_bump, "root_cert_path"):
                kwargs["ca_cert"] = self.ssl_bump.root_cert_path
            if hasattr(self.ssl_bump, "root_key_path"):
                kwargs["ca_key"] = self.ssl_bump.root_key_path
            if hasattr(self.ssl_bump, "cert_dir"):
                kwargs["confdir"] = self.ssl_bump.cert_dir

        try:
            self._opts = options.Options(**kwargs)
        except Exception as e:  # noqa: BLE001
            # 部分选项在不同 mitmproxy 版本可能不支持，逐个尝试
            logger.warning("mitmproxy", f"选项构建失败，尝试精简选项: {e}", "")
            self._opts = options.Options(
                listen_host=self.host,
                listen_port=self.port,
            )

        # 创建 DumpMaster（关闭 termlog/dumper 减少控制台输出）
        try:
            self._master = DumpMaster(
                self._opts, with_termlog=False, with_dumper=False)
        except TypeError:
            # 旧版 mitmproxy 无 with_termlog/with_dumper 参数
            self._master = DumpMaster(self._opts)

        # 显式设置 ProxyServer（部分 mitmproxy 版本需要手动设置）
        try:
            if getattr(self._master, "server", None) is None:
                self._master.server = MitmProxyServer(self._opts)
        except Exception as e:  # noqa: BLE001
            logger.warning("mitmproxy", f"ProxyServer 设置跳过: {e}", "")

        # 添加流量记录 addon
        self._master.addons.add(RecordingAddon(self))

        self._running = True
        self.refresh_ignored()

        # 在守护线程中运行 mitmproxy 事件循环（master.run 阻塞直到 shutdown）
        def _run():
            try:
                self._master.run()
            except Exception as e:  # noqa: BLE001
                logger.error("mitmproxy", f"事件循环异常退出: {e}", "")
            finally:
                self._running = False

        self._thread = threading.Thread(
            target=_run, daemon=True, name="mitmproxy-engine")
        self._thread.start()
        logger.info("mitmproxy",
                    f"mitmproxy 引擎已启动: {self.host}:{self.port}, mode={mode}",
                    f"ca_cert={kwargs.get('ca_cert', '(mitmproxy 默认)')}")

    def stop(self):
        """停止 mitmproxy 引擎：通知事件循环退出 + 等待线程结束。"""
        self._running = False
        if self._master is not None:
            try:
                self._master.shutdown()
            except Exception as e:  # noqa: BLE001
                logger.warning("mitmproxy", f"shutdown 异常: {e}", "")
            self._master = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        # 关闭继承自 ProxyServer 的连接池（虽然 mitmproxy 不用它们，但 API 层可能访问）
        try:
            self._conn_pool.close_all()
            self._h2_pool.close_all()
        except Exception:  # noqa: BLE001
            pass

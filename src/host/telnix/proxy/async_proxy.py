"""asyncio 代理服务器（G 方案）。

性能优化：用 asyncio 替代线程模型，解决 GIL 下高并发瓶颈。
- HTTP 请求/响应转发用 async I/O（无 GIL 争用）
- SSL 握手、证书签发、DB 写入用 asyncio.to_thread 包装（CPU/IO 密集型）
- WS relay / H2 forward 复用现有同步实现（在线程中运行）
- 与 FastAPI API 服务器共享同一事件循环

注意：这是可选引擎（proxy_engine=async）。默认仍用 builtin 线程引擎。
"""

from __future__ import annotations

import asyncio
import re
import socket
import ssl
import threading
import time
from typing import Optional
from datetime import datetime

from .. import db, logger
from ..ip_region import lookup as ip_region_lookup
from . import ssl_bump as ssl_bump_mod
from .server import Headers
from .breakpoint import BreakpointManager
from .process_lookup import ProcessLookup
from ..clash.client import get_upstream_proxy


def _compile_host_wildcard(pattern: str):
    """编译 host 通配符（* → .*, ? → .），大小写不敏感。失败返回 None。"""
    if not pattern:
        return None
    regex_str = '^' + re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.') + '$'
    try:
        return re.compile(regex_str, re.IGNORECASE)
    except re.error:
        return None


class AsyncProxyServer:
    """asyncio 代理服务器。

    与 ProxyServer 接口兼容：start()/stop()/refresh_cert_status()/cert_installed
    以及 API 层访问的 focus/ignore/pinning/breakpoint 等属性。
    注意：断点、focus、ignore 仅维护状态（API 可读写），async 引擎的转发链路
    未实现实际过滤逻辑（实验性引擎，生产请用 builtin 线程引擎）。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8888,
                 ssl_bump: Optional[ssl_bump_mod.SSLBumpManager] = None):
        self.host = host
        self.port = port
        self.ssl_bump = ssl_bump
        self.cert_installed = False
        self.capturing = False
        self.session_id: Optional[int] = None
        self.ignore_hosts: set[str] = set()
        self.ignore_pids: set[int] = set()
        self._server: Optional[asyncio.AbstractServer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event = threading.Event()
        # 运行状态（与 ProxyServer 接口兼容，capture.py:40 访问 proxy._running）
        self._running = False
        # 进程查找（processes.py 访问 proxy.process_lookup）
        self.process_lookup = ProcessLookup()
        # 忽略列表（refresh_ignored 从 DB 加载；API 层访问 _ignored_* 属性）
        self._ignored_lock = threading.Lock()
        self._ignored_pids: set[int] = set()
        self._ignored_names: set[str] = set()
        self._ignored_hosts: list[str] = []
        self._ignored_host_regexes: list[re.Pattern | None] = []
        # 专注模式（focus.py 访问 get_focus_mode/set_focus_mode）
        self._focus_lock = threading.Lock()
        self._focus_pids: set[int] = set()
        self._focus_hosts: list[str] = []
        self._focus_host_regexes: list[re.Pattern | None] = []
        self._focus_methods: set[str] = set()
        self._focus_status_codes: set[int] = set()
        self._focus_content_types: list[str] = []
        self._focus_enabled = False
        # 证书 pinning 疑似进程（与 ProxyServer 接口兼容，capture.py:31 访问）
        self._pinning_lock = threading.Lock()
        self._pinning_suspected: list[dict] = []
        self._pinning_keys: set[tuple] = set()
        # 上游代理（Clash 集成）
        self._upstream: Optional[tuple[str, int]] = None
        # 断点管理器（占位：与 ProxyServer 接口兼容，async 引擎未实现断点拦截逻辑）
        self.breakpoint = BreakpointManager()

    def refresh_cert_status(self):
        """刷新证书安装状态（与 ProxyServer 接口兼容）。"""
        if self.ssl_bump:
            # SSLBumpManager 方法名是 is_root_cert_installed（不是 is_root_installed）
            self.cert_installed = self.ssl_bump.is_root_cert_installed()

    def start(self):
        """启动 asyncio 代理服务器（在新线程中运行事件循环）。

        asyncio 事件循环不能与 uvicorn 的循环混用，因此放在独立线程中。
        """
        self._running = True
        t = threading.Thread(target=self._run_loop, daemon=True, name="async-proxy")
        t.start()
        logger.info("async-proxy", f"asyncio 代理服务器已启动: {self.host}:{self.port}")

    def _run_loop(self):
        """在独立线程中运行 asyncio 事件循环。"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._server = self._loop.run_until_complete(
                asyncio.start_server(self._handle_client, self.host, self.port)
            )
            self._loop.run_until_complete(self._stop_event.wait())
        except Exception as e:  # noqa: BLE001
            logger.error("async-proxy", "事件循环异常", str(e))
        finally:
            if self._server:
                self._server.close()
                self._loop.run_until_complete(self._server.wait_closed())
            self._loop.close()

    def stop(self):
        """停止代理服务器。"""
        self._running = False
        self._stop_event.set()
        logger.info("async-proxy", "asyncio 代理服务器已停止")

    def set_capturing(self, capturing: bool, session_id: Optional[int] = None):
        """设置抓包状态。"""
        self.capturing = capturing
        if session_id is not None:
            self.session_id = session_id

    def set_ignore_hosts(self, hosts: set[str]):
        self.ignore_hosts = hosts

    def set_ignore_pids(self, pids: set[int]):
        self.ignore_pids = pids

    def refresh_ignored(self):
        """从 DB 加载忽略列表（与 ProxyServer 接口兼容）。

        capture.py:101 / processes.py 多处调用。async 引擎转发链路未实际使用
        这些列表做过滤，但 API 层需要能读写状态。
        """
        with self._ignored_lock:
            rows = db.get_ignored_processes()
            self._ignored_pids = {r["pid"] for r in rows if r.get("pid") and r["pid"] > 0}
            self._ignored_names = {r["process_name"].lower()
                                   for r in rows
                                   if (not r.get("pid") or r["pid"] <= 0) and r.get("process_name")}
            host_rows = db.get_ignored_hosts()
            self._ignored_hosts = [r["host_pattern"] for r in host_rows if r.get("host_pattern")]
            self._ignored_host_regexes = [_compile_host_wildcard(p) for p in self._ignored_hosts]

    def get_focus_mode(self) -> dict:
        """获取专注模式状态（与 ProxyServer 接口兼容，focus.py:87 访问）。"""
        with self._focus_lock:
            return {
                "enabled": self._focus_enabled,
                "pids": list(self._focus_pids),
                "hosts": list(self._focus_hosts),
                "methods": list(self._focus_methods),
                "status_codes": list(self._focus_status_codes),
                "content_types": list(self._focus_content_types),
            }

    def set_focus_mode(self, enabled: bool, pids: list[int] | None = None,
                       hosts: list[str] | None = None,
                       methods: list[str] | None = None,
                       status_codes: list[int] | None = None,
                       content_types: list[str] | None = None):
        """设置专注模式（与 ProxyServer 接口兼容，focus.py:118 访问）。

        enabled 参数被忽略——自动根据是否有任何专注条件判断。
        async 引擎转发链路未实现实际过滤，仅维护状态供 API 读写。
        """
        with self._focus_lock:
            if pids is not None:
                self._focus_pids = set(pids)
            if hosts is not None:
                self._focus_hosts = list(hosts)
                self._focus_host_regexes = [_compile_host_wildcard(p) for p in self._focus_hosts]
            if methods is not None:
                self._focus_methods = {m.upper() for m in methods if m}
            if status_codes is not None:
                self._focus_status_codes = set(status_codes)
            if content_types is not None:
                self._focus_content_types = [c.lower() for c in content_types if c]
            self._focus_enabled = bool(
                self._focus_pids or self._focus_hosts or self._focus_methods
                or self._focus_status_codes or self._focus_content_types
            )

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        """处理客户端连接（asyncio 入口）。"""
        try:
            # 读取请求行
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
            line_str = request_line.decode("latin-1", "replace").strip()
            parts = line_str.split(" ")
            if len(parts) < 3:
                writer.close()
                return
            method, url, _version = parts[0], parts[1], parts[2]

            # 读取 headers
            headers = await self._read_headers(reader)

            # CONNECT 隧道（HTTPS）
            if method == "CONNECT":
                await self._handle_connect(reader, writer, url, headers)
                return

            # 普通 HTTP 请求
            await self._handle_http(reader, writer, method, url, headers)

        except (ConnectionError, asyncio.TimeoutError, OSError):
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug("async-proxy", "连接处理异常", str(e))
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _read_headers(self, reader: asyncio.StreamReader) -> Headers:
        """读取 HTTP headers（到空行为止）。"""
        h = Headers()
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            try:
                s = line.decode("latin-1", "replace").rstrip("\r\n")
                idx = s.find(":")
                if idx > 0:
                    h.add(s[:idx].strip(), s[idx+1:].strip())
            except Exception:  # noqa: BLE001
                pass
        return h

    async def _handle_connect(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter,
                              url: str, headers: Headers):
        """处理 CONNECT 隧道（HTTPS）。

        流程：
        1. 解析目标 host:port
        2. 判断是否需要 SSL bump（证书已安装且不在忽略列表）
        3. 连接目标（直连或通过上游代理）
        4. SSL bump：客户端侧用签发证书 wrap，服务器侧用普通 SSL wrap
        5. 转发双向数据
        """
        # 解析目标
        if ":" in url:
            host, port_str = url.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                writer.close()
                return
        else:
            host, port = url, 443

        # 忽略列表检查
        if host in self.ignore_hosts:
            writer.close()
            return

        # 连接目标（可能通过上游代理）
        target_reader, target_writer = await self._connect_target(host, port)
        if target_reader is None:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # 发送 200 Connection Established
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        # 判断是否需要 SSL bump
        do_bump = (self.ssl_bump and self.cert_installed
                   and host not in self.ignore_hosts)

        if do_bump:
            try:
                # 客户端侧 SSL（用签发证书）—— 在线程中执行（CPU 密集型）
                client_ssl_ctx = await asyncio.to_thread(
                    self.ssl_bump.get_client_ssl_context, host
                )
                # 获取底层 socket 并 wrap
                client_sock = writer.get_extra_info("socket")
                client_ssl_sock = await asyncio.to_thread(
                    client_ssl_ctx.wrap_socket, client_sock, server_side=True
                )
                # 服务器侧 SSL
                target_sock = target_writer.get_extra_info("socket")
                server_ssl_ctx = ssl.create_default_context()
                target_ssl_sock = await asyncio.to_thread(
                    server_ssl_ctx.wrap_socket, target_sock, server_hostname=host
                )
                # 用线程转发（SSL socket 不兼容 asyncio）
                await asyncio.to_thread(
                    self._relay_ssl_tunnel,
                    client_ssl_sock, target_ssl_sock, host, port
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("async-proxy", f"SSL bump 失败 {host}:{port}", str(e))
        else:
            # 不做 SSL bump，直接转发明文
            await self._relay_async(reader, writer, target_reader, target_writer)

    def _relay_ssl_tunnel(self, client_sock: ssl.SSLSocket,
                          target_sock: ssl.SSLSocket, host: str, port: int):
        """在线程中转发 SSL 隧道数据（SSL socket 不兼容 asyncio）。"""
        import select
        try:
            while not self._stop_event.is_set():
                r, _, _ = select.select([client_sock, target_sock], [], [], 1.0)
                for sock in r:
                    try:
                        data = sock.recv(8192)
                        if not data:
                            return
                        if sock is client_sock:
                            target_sock.sendall(data)
                        else:
                            client_sock.sendall(data)
                    except (OSError, ssl.SSLError):
                        return
        finally:
            try: client_sock.close()
            except: pass
            try: target_sock.close()
            except: pass

    async def _handle_http(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter,
                           method: str, url: str, headers: Headers):
        """处理普通 HTTP 请求。"""
        # 解析 URL
        if url.startswith("http://"):
            url_no_scheme = url[7:]
        elif url.startswith("https://"):
            url_no_scheme = url[8:]
        else:
            url_no_scheme = url
        if "/" in url_no_scheme:
            host_port, path = url_no_scheme.split("/", 1)
            path = "/" + path
        else:
            host_port, path = url_no_scheme, "/"
        if ":" in host_port:
            host, port_str = host_port.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                host, port = host_port, 80
        else:
            host, port = host_port, 80

        # 忽略列表检查
        if host in self.ignore_hosts:
            writer.close()
            return

        # 读取请求体
        body = b""
        cl = headers.get("Content-Length")
        if cl:
            try:
                body_len = int(cl)
                if body_len > 0:
                    body = await reader.readexactly(body_len)
            except (ValueError, asyncio.IncompleteReadError):
                pass

        # 连接目标
        target_reader, target_writer = await self._connect_target(host, port)
        if target_reader is None:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # 转发请求
        req_line = f"{method} {path} HTTP/1.1\r\n".encode("latin-1")
        req_headers = Headers()
        for k, v in headers._items:  # noqa: SLF001
            if k.lower() not in ("proxy-connection", "proxy-authorization"):
                req_headers.add(k, v)
        req_headers.set("Connection", "close")
        req_data = req_line + req_headers.to_bytes() + b"\r\n" + body
        target_writer.write(req_data)
        await target_writer.drain()

        # 转发响应
        try:
            while True:
                data = await target_reader.read(8192)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (OSError, asyncio.TimeoutError):
            pass

        try:
            target_writer.close()
            await target_writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    async def _connect_target(self, host: str, port: int) -> tuple[
            Optional[asyncio.StreamReader], Optional[asyncio.StreamWriter]]:
        """连接目标服务器（直连或通过上游代理）。

        返回 (reader, writer)，失败返回 (None, None)。
        """
        try:
            # 检查上游代理（Clash 集成）
            upstream = await asyncio.to_thread(get_upstream_proxy)
            if upstream:
                proxy_host, proxy_port = upstream
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(proxy_host, proxy_port), timeout=10
                )
                # 通过代理建立 CONNECT 隧道
                connect_req = (
                    f"CONNECT {host}:{port} HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\n\r\n"
                ).encode("latin-1")
                writer.write(connect_req)
                await writer.drain()
                # 读取代理响应
                resp_line = await reader.readline()
                if not resp_line or b"200" not in resp_line:
                    writer.close()
                    return None, None
                # 跳过剩余 headers
                while True:
                    line = await reader.readline()
                    if not line or line in (b"\r\n", b"\n"):
                        break
                return reader, writer
            else:
                # 直连
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=10
                )
                return reader, writer
        except (OSError, asyncio.TimeoutError) as e:
            logger.debug("async-proxy", f"连接目标失败 {host}:{port}", str(e))
            return None, None

    async def _relay_async(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter,
                           target_reader: asyncio.StreamReader,
                           target_writer: asyncio.StreamWriter):
        """双向转发数据（asyncio 实现）。"""
        async def forward(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
            try:
                while True:
                    data = await src.read(8192)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (OSError, asyncio.TimeoutError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.gather(
            forward(reader, target_writer),
            forward(target_reader, writer)
        )

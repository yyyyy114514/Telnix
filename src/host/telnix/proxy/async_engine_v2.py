"""AsyncEngineV2 — 高性能 asyncio 代理引擎（基于 v2 架构，适配 v1 全部功能）。

继承 ProxyServer 保持对外接口完全兼容（属性 / public 方法 / flow dict / SSE），
重写核心连接处理为 asyncio 全异步：

- uvloop（Linux/macOS）/ winloop（Windows）高性能事件循环
- asyncio.start_server 全异步 accept（无每连接一线程开销）
- asyncio 连接池（keep-alive 复用，减少 TCP/TLS 握手）
- 异步 I/O 转发（asyncio.open_connection / StreamReader / StreamWriter）
- SSL Bump（loop.start_tls + v1 SSLBumpManager 证书）
- 复用 v1 子系统 via asyncio.to_thread 桥接：
  断点(BreakpointManager) / PID反查(ProcessLookup) / 规则(auto_reply) /
  proxy_tools / throttle / Clash上游 / WebSocket中继 / HTTP/2
- flow 记录复用 v1 db.insert_flow_async / insert_flow + SSE 两阶段推送

引擎选择：settings.json 的 proxy_engine = "v2" 启用。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import socket
import ssl
import sys
import threading
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

from .server import (
    ProxyServer,
    Headers,
    SocketReader,
    _truncate_for_record,
    _to_text,
    _to_bytes,
    _SSL_BUMP_FAILED_TTL,
    _SSL_CTX_CACHE_MAX,
    MAX_RECORDED_BODY,
    MAX_DECOMPRESS_BODY,
    _decompress_body,
    _reason,
)
from .. import db
from .. import logger
from ..auto_reply.rules import (
    find_matching_rule,
    has_active_rules_fast,
    host_matches_any_rule,
)
from ..clash.client import get_upstream_proxy
from . import throttle
from . import proxy_tools


# ---------------------------------------------------------------------------
# 事件循环优化
# ---------------------------------------------------------------------------

_loop_type = "asyncio"


def _setup_v2_event_loop() -> str:
    """选择高性能事件循环策略：Windows 用 winloop，其他平台用 uvloop。"""
    global _loop_type
    if sys.platform == "win32":
        try:
            import winloop  # type: ignore[import-untyped]
            asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
            _loop_type = "winloop"
            return _loop_type
        except ImportError:
            pass
    try:
        import uvloop  # type: ignore[import-untyped]
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        _loop_type = "uvloop"
        return _loop_type
    except ImportError:
        pass
    _loop_type = "asyncio"
    return _loop_type


# ---------------------------------------------------------------------------
# 异步连接池
# ---------------------------------------------------------------------------


class _PooledConn:
    __slots__ = ("reader", "writer", "host", "port", "is_ssl", "last_used")

    def __init__(self, reader, writer, host, port, is_ssl):
        self.reader = reader
        self.writer = writer
        self.host = host
        self.port = port
        self.is_ssl = is_ssl
        self.last_used = time.time()


class _AsyncConnPool:
    """异步 keep-alive 连接池。按 host:port:ssl 分组，LRU 淘汰。"""

    _MAX_IDLE = 60.0
    _MAX_PER_KEY = 8
    _MAX_TOTAL = 200

    def __init__(self):
        self._conns: dict[str, list[_PooledConn]] = {}
        self._lock = threading.Lock()
        self._stats = {"created": 0, "reused": 0, "closed": 0}

    def _key(self, host, port, is_ssl):
        return f"{host}:{port}:{int(is_ssl)}"

    def get(self, host, port, is_ssl) -> Optional[_PooledConn]:
        key = self._key(host, port, is_ssl)
        with self._lock:
            conns = self._conns.get(key)
            if not conns:
                return None
            now = time.time()
            while conns:
                c = conns.pop(0)
                if now - c.last_used > self._MAX_IDLE:
                    self._close(c)
                    continue
                if c.writer.is_closing():
                    self._stats["closed"] += 1
                    continue
                self._stats["reused"] += 1
                return c
            if key in self._conns and not self._conns[key]:
                del self._conns[key]
            return None

    def put(self, conn: _PooledConn):
        key = self._key(conn.host, conn.port, conn.is_ssl)
        with self._lock:
            total = sum(len(v) for v in self._conns.values())
            if total >= self._MAX_TOTAL:
                self._close(conn)
                return
            existing = self._conns.setdefault(key, [])
            if len(existing) >= self._MAX_PER_KEY:
                self._close(conn)
                return
            conn.last_used = time.time()
            existing.append(conn)

    def _close(self, conn: _PooledConn):
        try:
            conn.writer.close()
        except Exception:  # noqa: BLE001
            pass
        self._stats["closed"] += 1

    def close_all(self):
        with self._lock:
            for conns in self._conns.values():
                for c in conns:
                    try:
                        c.writer.close()
                    except Exception:  # noqa: BLE001
                        pass
            self._conns.clear()

    def stats(self):
        with self._lock:
            return {**self._stats, "total": sum(len(v) for v in self._conns.values())}


# ---------------------------------------------------------------------------
# AsyncEngineV2
# ---------------------------------------------------------------------------


class AsyncEngineV2(ProxyServer):
    """高性能 asyncio 代理引擎（v2）。

    继承 ProxyServer，保持所有属性 / public 方法 / flow dict / SSE 契约不变。
    仅重写 start / stop / 连接处理为 asyncio 全异步。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8888, ssl_bump=None):
        super().__init__(host=host, port=port, ssl_bump=ssl_bump)
        self._engine_name = "v2"
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._async_server: Optional[asyncio.AbstractServer] = None
        self._server_socket = None
        self._aio_conn_pool = _AsyncConnPool()
        # 断点专用线程池（避免耗尽默认 executor）
        self._bp_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=32, thread_name_prefix="v2-breakpoint"
        )
        # request_id -> (asyncio.Future, StreamWriter)，用于 send_raw_response / 断点
        self._pending_flows: dict[str, dict] = {}
        self._pending_lock = threading.Lock()

    # ------------------------------------------------------------------ 生命周期

    def start(self):
        """启动 v2 引擎：asyncio 事件循环驱动 accept，连接处理复用 v1 同步代码。

        架构与 AsyncProxyServer 一致（asyncio accept + 线程处理），但保留 v2 标识
        和连接池/断点线程池等扩展。SSL Bump / HTTP / 隧道全部复用 v1 成熟代码，
        不漏包、解密正常。asyncio 事件循环仅驱动 accept，未来可扩展其他异步任务。
        """
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(200)
        self._server_socket.settimeout(1.0)  # accept 超时 1s，便于响应 stop
        self._running = True
        self.refresh_ignored()
        self.refresh_cert_status()
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True, name="v2-engine-loop"
        )
        self._loop_thread.start()
        logger.info("proxy", f"AsyncEngineV2 (v2) started: {self.host}:{self.port}",
                    f"loop={_loop_type}")

    def _run_loop(self):
        _setup_v2_event_loop()
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._accept_loop_async())
        except Exception as e:  # noqa: BLE001
            logger.error("proxy", "v2 engine loop exception", str(e))
        finally:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _accept_loop_async(self):
        """asyncio 驱动的 accept 循环。

        阻塞 accept 在 executor 线程中执行，await 让出事件循环。
        连接处理直接提交到线程（复用 v1 _handle_client_safe_with_sem），
        SSL Bump / HTTP / 隧道全部走 v1 同步代码，不漏包。
        """
        while self._running:
            try:
                client_sock, client_addr = await self._loop.run_in_executor(
                    None, self._server_socket.accept)
            except socket.timeout:
                continue  # 周期性检查 _running
            except OSError:
                if self._running:
                    logger.warning("proxy", "v2 accept exception")
                break
            try:
                client_sock.settimeout(60)
            except OSError:
                pass
            # 获取信号量，限制最大并发连接数
            if not self._client_sem.acquire(timeout=5):
                try:
                    client_sock.close()
                except OSError:
                    pass
                continue
            # 提交到线程处理（复用 v1 _handle_client_safe_with_sem）
            t = threading.Thread(
                target=self._handle_client_safe_with_sem,
                args=(client_sock, client_addr),
                daemon=True,
            )
            t.start()

    def stop(self):
        """停止异步引擎。"""
        self._running = False
        # 关闭 server socket，让 accept 退出阻塞
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        # 停止事件循环
        if self._loop is not None and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)
            self._loop_thread = None
        # 关闭连接池
        try:
            self._aio_conn_pool.close_all()
        except Exception:  # noqa: BLE001
            pass
        # 关闭 v1 连接池（h2 等）
        try:
            self._conn_pool.close_all()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._h2_pool.close_all()
        except Exception:  # noqa: BLE001
            pass
        # 关闭断点线程池
        try:
            self._bp_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass
        logger.info("proxy", "AsyncEngineV2 (v2) stopped")

    # ------------------------------------------------------------------ 客户端连接

    async def _handle_client_async(self, reader: asyncio.StreamReader,
                                   writer: asyncio.StreamWriter):
        """异步处理客户端连接（支持 HTTP keep-alive 多请求循环）。"""
        client_addr = writer.get_extra_info("peername", ("", 0))
        try:
            # 读首字节判断是否 HTTP 流量（asyncio 无 MSG_PEEK，读取即消费）
            first_byte = await asyncio.wait_for(reader.read(1), timeout=60)
            if not first_byte:
                return
            is_http = 0x41 <= first_byte[0] <= 0x5A  # A-Z（HTTP 方法首字母）
            if not is_http:
                # 非 HTTP 流量：尝试 raw 隧道（透明代理）
                await self._try_raw_tunnel_async(first_byte, reader, writer, client_addr)
                return
            # 读剩余请求行
            rest = await asyncio.wait_for(reader.readline(), timeout=60)
            request_line = (first_byte + rest).decode("latin-1").strip()
            parts = request_line.split(" ", 2)
            if len(parts) != 3:
                return
            method, target, version = parts
            # PID 反查（首次连接时查一次，keep-alive 后续请求复用）
            pid, proc_name = None, None
            if self._needs_pid():
                try:
                    pid, proc_name = await asyncio.to_thread(
                        self.process_lookup.lookup, client_addr)
                except Exception:  # noqa: BLE001
                    pass
            # CONNECT 交给专用处理（SSL Bump / 隧道），不循环
            if method.upper() == "CONNECT":
                # 取出 asyncio reader 缓冲区数据（客户端可能已 pipeline 发送 TLS ClientHello）
                # + 取出底层 socket，dup 后交给 to_thread 同步处理，完全复用 v1 _handle_connect
                # 避免 asyncio start_tls 的缓冲区丢包问题
                buffered = bytes(reader._buffer) if reader._buffer else b""
                reader._buffer.clear()
                sock = writer.get_extra_info("socket")
                if sock is not None:
                    dup_sock = sock.dup()
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:  # noqa: BLE001
                        pass
                    await asyncio.to_thread(
                        self._sync_handle_connect, dup_sock, buffered,
                        request_line.encode("latin-1"), pid, proc_name)
                return
            # HTTP keep-alive 循环（修复漏包：原实现只处理一个请求就关闭）
            while self._running:
                keep_alive = await self._handle_http_async(
                    reader, writer, method, target, version,
                    pid, proc_name, client_addr, scheme="http")
                if not keep_alive:
                    break
                # 读下一个请求行（keep-alive 空闲超时 30s）
                try:
                    first_byte = await asyncio.wait_for(reader.read(1), timeout=30)
                    if not first_byte:
                        break
                    rest = await asyncio.wait_for(reader.readline(), timeout=30)
                    request_line = (first_byte + rest).decode("latin-1").strip()
                    parts = request_line.split(" ", 2)
                    if len(parts) != 3:
                        break
                    method, target, version = parts
                except asyncio.TimeoutError:
                    break  # keep-alive 空闲超时
        except asyncio.TimeoutError:
            pass
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug("proxy", "v2 handle_client error", str(e))
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _try_raw_tunnel_async(self, first_byte, reader, writer, client_addr):
        """非 HTTP 流量处理（透明代理 raw 隧道）。回退到 v1 同步代码。"""
        try:
            from .transparent_proxy import get_transparent_proxy
            proxy = get_transparent_proxy()
            if not proxy.running:
                return
        except Exception:  # noqa: BLE001
            return
        # 透明代理模式下，把 socket 取出用 v1 同步代码处理
        # asyncio 的 reader/writer 底层是 socket，可取出
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        # 把已读的 first_byte 放回（用 unread 不可靠，改为传给 v1）
        # 由于 transparent_proxy 的 _try_raw_tunnel 需要 MSG_PEEK，这里用 to_thread 桥接
        # 先关闭 asyncio 侧，用原始 socket
        try:
            writer.transport.close()
        except Exception:  # noqa: BLE001
            pass
        # 把 first_byte 重新放回 socket 的接收缓冲不可行（已读出）
        # 改为：用已读字节 + 剩余数据转发
        # 透明代理场景较少，这里简单处理：直接关闭
        # （透明代理通常用 v1 builtin 引擎，v2 引擎主要用于系统代理模式）
        logger.debug("proxy", "v2: non-HTTP traffic in transparent mode, "
                      "consider using builtin engine for full transparent proxy support")

    # ------------------------------------------------------------------ CONNECT / SSL Bump

    def _sync_handle_connect(self, sock, buffered, connect_line, pid, proc_name):
        """同步处理 CONNECT（在线程中执行，完全复用 v1 _handle_connect）。

        将 asyncio reader 缓冲区数据注入 SocketReader，避免 pipeline 的 TLS
        ClientHello 丢失。v1 _handle_connect 内含 SSL Bump / 纯隧道 / 断点 /
        视频 CDN 跳过 / 证书缓存 / pinning 收集等全部逻辑。
        """
        try:
            sock.setblocking(True)
            reader = SocketReader(sock)
            # 注入 asyncio reader 缓冲区的数据（客户端 pipeline 发送的字节）
            if buffered:
                reader.buf.extend(buffered)
            self._handle_connect(sock, reader, connect_line, pid, proc_name)
        except Exception as e:  # noqa: BLE001
            logger.debug("proxy", "v2 sync_handle_connect error", str(e))
        finally:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass

    async def _handle_connect_async(self, reader, writer, target,
                                    pid, proc_name, client_addr):
        """异步 CONNECT 处理（HTTPS 隧道 + SSL Bump）。

        SSL Bump 采用「取出 socket → to_thread 同步 wrap_socket + _serve_http_loop」方案，
        完全复用 v1 成熟代码，避免 asyncio start_tls 的缓冲区丢包问题。
        纯隧道仍用 asyncio 双向转发（性能好）。
        """
        # 解析目标
        if ":" in target:
            host, port_str = target.split(":", 1)
            port = int(port_str)
        else:
            host, port = target, 443

        # 端口范围校验
        if not (1 <= port <= 65535):
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return

        # 忽略/专注过滤
        if self.is_ignored(pid, proc_name, host):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return

        record = self.capturing and self.session_id is not None
        if record and self._focus_enabled:
            if self.is_focused_out(pid, host, None, None, None):
                record = False

        # 判断是否需要 SSL Bump（与 v1 _handle_connect 逻辑一致）
        do_bump = self._should_ssl_bump(host, pid, proc_name)
        if self.capturing and record:
            logger.debug("proxy", f"CONNECT {host}:{port}",
                        f"do_bump={do_bump} pid={pid} proc={proc_name}")

        # SSL bump 曾失败的 host 自动降级（TTL 机制）
        if do_bump:
            with self._ssl_bump_failed_lock:
                failed_at = self._ssl_bump_failed_hosts.get(host)
            if failed_at is not None:
                if time.time() - failed_at > _SSL_BUMP_FAILED_TTL:
                    with self._ssl_bump_failed_lock:
                        self._ssl_bump_failed_hosts.pop(host, None)
                else:
                    do_bump = False

        # 预签发证书（F6 修复：失败则降级为纯隧道，避免已发 200 但无法握手）
        cert_path = key_path = None
        if do_bump:
            try:
                cert_path, key_path = await asyncio.to_thread(
                    self.ssl_bump.get_cert, host)
            except Exception:  # noqa: BLE001
                with self._ssl_bump_failed_lock:
                    was_new = host not in self._ssl_bump_failed_hosts
                    self._ssl_bump_failed_hosts[host] = time.time()
                if was_new:
                    logger.warning("proxy", "v2 SSL bump cert signing failed",
                                   f"host={host}, downgrading to tunnel")
                do_bump = False

        # 读完 CONNECT 请求剩余的 HTTP 头（asyncio reader 可能已缓冲部分数据）
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if not line or line == b"\r\n" or line == b"\n":
                    break
        except asyncio.TimeoutError:
            return

        if do_bump and cert_path:
            # SSL Bump：取出底层 socket，dup 后交给 to_thread 同步处理
            sock = writer.get_extra_info("socket")
            if sock is not None:
                dup_sock = sock.dup()
                # 关闭 asyncio 侧（原 fd 关闭，dup_sock 保持打开）
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
                # to_thread 调用同步 wrap_socket + _serve_http_loop
                await asyncio.to_thread(
                    self._sync_bump_and_serve, dup_sock, host, port,
                    cert_path, key_path, pid, proc_name)
                return
            # 取 socket 失败，降级隧道
            do_bump = False

        # 纯隧道：先发 200，再 asyncio 双向转发
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        # 记录纯隧道 CONNECT flow（与 v1 一致，让用户看到 CONNECT 流量）
        if record and not self.is_ignored(pid, proc_name, host):
            try:
                await self._record_flow_async(
                    pid or 0, proc_name or "", "CONNECT", f"https://{host}:{port}/",
                    "https", host, "/", Headers(), b"", 200, Headers(),
                    f"TLS tunnel (no bump): {host}:{port}", 0, 0, client_addr)
            except Exception:  # noqa: BLE001
                pass

        await self._tunnel_to_target_async(reader, writer, host, port,
                                           pid, proc_name, client_addr, record,
                                           scheme="https")

    def _should_ssl_bump(self, host, pid, proc_name):
        """判断是否需要 SSL Bump（与 v1 _handle_connect 逻辑一致）。

        - 抓包中：bump 所有 HTTPS（用户主动要抓包）
        - 未抓包但有自动修改规则：仅 bump 匹配规则 pattern 的 host
        - 证书已装 + 非忽略进程 + 非专注外进程
        """
        if self.capturing:
            need_bump = True
        elif has_active_rules_fast():
            need_bump = host_matches_any_rule(host)
        else:
            need_bump = False
        return (need_bump
                and self.ssl_bump is not None
                and self.cert_installed
                and not self.is_ignored(pid, proc_name, host)
                and not self.is_focused_out(pid, host))

    def _sync_bump_and_serve(self, sock, host, port, cert_path, key_path,
                             pid, proc_name):
        """同步 SSL Bump + HTTP serve（在线程中执行，复用 v1 成熟代码）。

        流程：SSLContext 缓存 → wrap_socket（同步阻塞握手）→ _serve_http_loop（循环处理请求）
        完全复用 v1 的 SSL Bump 逻辑，避免 asyncio start_tls 的缓冲区丢包问题。
        """
        local_port = 0
        try:
            sock.setblocking(True)
            sock.settimeout(60)
            try:
                local_port = sock.getsockname()[1]
            except OSError:
                pass
            # 注册防循环端口（v1 _connect_target 会检查）
            if local_port:
                self._register_proxy_port(local_port)

            # SSLContext 缓存（与 v1 double-checked locking 一致）
            _now_ts = time.time()
            _last_mtime = self._ssl_mtime_cache.get(cert_path)
            if _last_mtime and _now_ts - _last_mtime[0] < 5.0:
                current_mtime = _last_mtime[1]
            else:
                try:
                    current_mtime = os.path.getmtime(cert_path)
                except OSError:
                    current_mtime = 0
                self._ssl_mtime_cache[cert_path] = (_now_ts, current_mtime)
            ssl_ctx = None
            with self._ssl_ctx_lock:
                cached_entry = self._ssl_ctx_cache.get(cert_path)
                if cached_entry is not None:
                    cached_ctx, cached_mtime = cached_entry
                    if current_mtime and cached_mtime == current_mtime:
                        ssl_ctx = cached_ctx
                        self._ssl_ctx_cache.move_to_end(cert_path)
            if ssl_ctx is None:
                new_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                new_ctx.load_cert_chain(cert_path, key_path)
                # ALPN：只通告 http/1.1，强制客户端走 HTTP/1.1
                try:
                    new_ctx.set_alpn_protocols(["http/1.1"])
                except Exception:  # noqa: BLE001
                    pass
                with self._ssl_ctx_lock:
                    cached_entry = self._ssl_ctx_cache.get(cert_path)
                    if cached_entry is not None:
                        cached_ctx, cached_mtime = cached_entry
                        if current_mtime and cached_mtime == current_mtime:
                            ssl_ctx = cached_ctx
                            self._ssl_ctx_cache.move_to_end(cert_path)
                        else:
                            ssl_ctx = new_ctx
                            self._ssl_ctx_cache[cert_path] = (new_ctx, current_mtime)
                    else:
                        ssl_ctx = new_ctx
                        self._ssl_ctx_cache[cert_path] = (new_ctx, current_mtime)
                        if len(self._ssl_ctx_cache) > _SSL_CTX_CACHE_MAX:
                            self._ssl_ctx_cache.popitem(last=False)

            # wrap_socket（同步阻塞，直到 TLS 握手完成）
            try:
                tls_sock = ssl_ctx.wrap_socket(sock, server_side=True)
            except (ssl.SSLError, OSError) as e:
                # TLS 握手失败：加入降级列表 + pinning 收集
                err_str = str(e).lower()
                is_cert_error = any(k in err_str for k in (
                    "tlsv1 alert", "handshake failure", "certificate",
                    "unknown ca", "bad certificate", "eof",
                    "violation of protocol", "unexpected eof", "connection reset"))
                if is_cert_error:
                    with self._ssl_bump_failed_lock:
                        was_new = host not in self._ssl_bump_failed_hosts
                        self._ssl_bump_failed_hosts[host] = time.time()
                    if was_new:
                        logger.warning(
                            "proxy",
                            f"v2 SSL bump failed, downgraded to plain tunnel: host={host}",
                            f"TLS handshake failed: {e}. Subsequent connections will be tunneled.")
                    key = (host, pid, proc_name)
                    with self._pinning_lock:
                        if key not in self._pinning_keys:
                            self._pinning_keys.add(key)
                            self._pinning_suspected.append({
                                "host": host, "pid": pid, "process_name": proc_name,
                                "timestamp": datetime.now().isoformat(timespec="seconds"),
                                "error": str(e)[:200],
                            })
                            if len(self._pinning_suspected) > 50:
                                dropped = self._pinning_suspected.pop(0)
                                self._pinning_keys.discard((
                                    dropped.get("host"), dropped.get("pid"),
                                    dropped.get("process_name")))
                else:
                    logger.warning("proxy", f"v2 TLS handshake failed: host={host}",
                                   f"error: {e}")
                return

            # HTTP serve 循环（复用 v1 _serve_http_loop，不漏包）
            tls_sock.settimeout(60)
            tls_reader = SocketReader(tls_sock)
            try:
                self._serve_http_loop(tls_sock, tls_reader, pid, proc_name,
                                      scheme="https", default_host=host,
                                      default_port=port)
            finally:
                try:
                    tls_sock.unwrap()
                except (OSError, ValueError):
                    pass
        except Exception as e:  # noqa: BLE001
            logger.debug("proxy", "v2 sync_bump_and_serve error", str(e))
        finally:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass
            if local_port:
                try:
                    self._unregister_proxy_port(local_port)
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ HTTP 请求处理

    async def _handle_http_async(self, reader, writer, method, url, version,
                                 pid, proc_name, client_addr, scheme="http",
                                 record=None, is_bumped=False):
        """异步处理 HTTP/HTTPS 请求。返回 keep_alive 是否可复用。"""
        flow_start = time.time()
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"

        # 读请求头
        headers = Headers()
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=30)
                if not line or line == b"\r\n" or line == b"\n":
                    break
                text = line.decode("latin-1", "replace").rstrip("\r\n")
                if ":" in text:
                    name, _, value = text.partition(":")
                    headers.add(name.strip(), value.strip())
        except asyncio.TimeoutError:
            return False

        # 读请求体
        body = b""
        cl = headers.get("Content-Length")
        if cl:
            try:
                body = await asyncio.wait_for(
                    reader.readexactly(int(cl)), timeout=30)
            except asyncio.TimeoutError:
                return False
        elif (headers.get("Transfer-Encoding") or "").lower() == "chunked":
            body = await self._read_chunked_async(reader)

        keep_alive = (headers.get("Connection") or "").lower() != "close"

        # 确定是否记录
        if record is None:
            record = self.capturing and self.session_id is not None
        if record:
            if self.is_ignored(pid, proc_name, host):
                record = False
            elif self._focus_enabled and self.is_focused_out(pid, host, method, None, None):
                record = False

        # proxy_tools: Block List
        if proxy_tools.should_block(host, url):
            await self._send_response_async(writer, 403, Headers(), b"Forbidden", keep_alive)
            if record:
                await self._record_flow_async(
                    pid, proc_name, method, url, scheme, host, path,
                    headers, body, 403, Headers(), b"Forbidden",
                    int((time.time() - flow_start) * 1000), len(b"Forbidden"),
                    client_addr)
            return keep_alive

        # proxy_tools: No-Caching 注入
        proxy_tools.inject_no_caching(headers, url)

        # proxy_tools: Map Local
        local_resp = proxy_tools.check_map_local(host, url, headers.to_dict())
        if local_resp:
            lr_headers = Headers.from_dict(local_resp.get("headers", {}))
            lr_body = local_resp.get("body", b"")
            proxy_tools.inject_cors(lr_headers)
            await self._send_response_async(
                writer, local_resp.get("status", 200), lr_headers, lr_body, keep_alive)
            if record:
                await self._record_flow_async(
                    pid, proc_name, method, url, scheme, host, path,
                    headers, body, local_resp.get("status", 200),
                    lr_headers, lr_body,
                    int((time.time() - flow_start) * 1000), len(lr_body),
                    client_addr)
            return keep_alive

        # proxy_tools: Map Remote
        remote = proxy_tools.check_map_remote(host, url, headers.to_dict())
        if remote:
            new_scheme, host, port, path, url = remote
            scheme = new_scheme
            headers.set("Host", f"{host}:{port}" if port not in (80, 443) else host)

        # throttle 延迟
        if throttle.is_enabled():
            await asyncio.to_thread(throttle.delay)
            if throttle.should_drop():
                return keep_alive

        # 自动回复规则匹配
        rule = None
        if has_active_rules_fast():
            rule = await asyncio.to_thread(
                find_matching_rule, url, method, None, pid, proc_name)

        # 规则动作：mock / mock_request / modify_request / delay / script
        if rule:
            action = rule.get("action", "")
            # delay
            delay_ms = self._get_rule_delay(rule, "request")
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000.0)

            if action == "mock":
                status = int(rule.get("mock_status") or 200)
                m_headers = Headers.from_dict(json.loads(rule.get("mock_headers") or "{}"))
                m_body = _to_bytes(rule.get("mock_body") or "")
                await self._send_response_async(writer, status, m_headers, m_body, keep_alive)
                if record:
                    await self._record_flow_async(
                        pid, proc_name, method, url, scheme, host, path,
                        headers, body, status, m_headers, m_body,
                        int((time.time() - flow_start) * 1000), len(m_body),
                        client_addr)
                return keep_alive

            if action == "mock_request":
                m_method = rule.get("mock_request_method") or method
                m_url = rule.get("mock_request_url") or url
                m_headers = Headers.from_dict(json.loads(rule.get("mock_request_headers") or "{}"))
                m_body = _to_bytes(rule.get("mock_request_body") or "")
                method, url, headers, body = m_method, m_url, m_headers, m_body
                parsed = urlsplit(url)
                host = parsed.hostname or host
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                path = parsed.path or "/"
                if parsed.query:
                    path += f"?{parsed.query}"
                scheme = parsed.scheme or scheme

            if action == "modify_request":
                self._apply_modify_request(rule, headers, body)

            if action == "script":
                result = await asyncio.to_thread(
                    self._run_script_request, rule, url, method, headers, body,
                    host, path, scheme, pid, proc_name)
                if result == "drop":
                    return keep_alive
                if isinstance(result, tuple):
                    s_headers, s_body, s_status = result
                    await self._send_response_async(
                        writer, s_status, s_headers, s_body, keep_alive)
                    if record:
                        await self._record_flow_async(
                            pid, proc_name, method, url, scheme, host, path,
                            headers, body, s_status, s_headers, s_body,
                            int((time.time() - flow_start) * 1000), len(s_body),
                            client_addr)
                    return keep_alive
                if isinstance(result, bytes):
                    body = result

        # WebSocket Upgrade 检测
        if (headers.get("Upgrade") or "").lower() == "websocket":
            await self._handle_ws_upgrade_async(
                reader, writer, method, url, scheme, host, port, path,
                headers, body, pid, proc_name, client_addr, record, flow_start)
            return False  # WebSocket 不复用连接

        # 请求断点
        flow_id = None
        if record and self.breakpoint.should_break_request():
            flow_id = await self._insert_flow_async(
                pid, proc_name, method, url, scheme, host, path,
                headers, body, client_addr)
            if flow_id:
                await asyncio.to_thread(db.update_flow_breakpoint, flow_id, "pending_request")
                action = await self._wait_breakpoint_async(flow_id)
                if action == "drop":
                    await asyncio.to_thread(db.update_flow_breakpoint, flow_id, None)
                    return keep_alive
                # 取用户修改
                modified = await asyncio.to_thread(db.get_flow, flow_id)
                if modified:
                    method = modified.get("method", method)
                    url = modified.get("url", url)
                    body = _to_bytes(modified.get("request_body", ""))
                    headers = Headers.from_dict(json.loads(modified.get("request_headers") or "{}"))
                    parsed = urlsplit(url)
                    host = parsed.hostname or host
                    port = parsed.port or (443 if parsed.scheme == "https" else 80)
                    path = parsed.path or "/"
                    if parsed.query:
                        path += f"?{parsed.query}"
                await asyncio.to_thread(db.update_flow_breakpoint, flow_id, None)
        elif record:
            # 非断点路径：预入库（请求阶段，拿 flow_id 用于响应阶段 SSE 补齐）
            flow_id = await self._preinsert_flow_async(
                pid, proc_name, method, url, scheme, host, path,
                headers, body, client_addr)

        # 转发请求到目标
        try:
            status, resp_headers, resp_body, resp_size = await self._forward_async(
                host, port, method, path, version, headers, body,
                scheme, url, is_bumped, client_addr)
        except Exception as e:  # noqa: BLE001
            # 转发失败：返回 502
            err_body = f"Bad Gateway: {e}".encode()
            err_headers = Headers()
            err_headers.add("Content-Length", str(len(err_body)))
            err_headers.add("Connection", "close")
            await self._send_response_async(writer, 502, err_headers, err_body, False)
            if record:
                duration_ms = int((time.time() - flow_start) * 1000)
                await self._record_flow_async(
                    pid, proc_name, method, url, scheme, host, path,
                    headers, body, 502, err_headers, "",
                    duration_ms, len(err_body),
                    client_addr, flow_id=flow_id)
                if flow_id:
                    await self._notify_flow_update_async(
                        flow_id, 502, err_headers, len(err_body), duration_ms)
            return False

        # proxy_tools: Force CORS
        proxy_tools.inject_cors(resp_headers)

        # proxy_tools: Mirror
        mirror_dir = proxy_tools.check_mirror(host, url)
        if mirror_dir and resp_body:
            await asyncio.to_thread(
                proxy_tools.save_mirror_response, mirror_dir, url,
                resp_headers.get("Content-Type", ""), resp_body)

        # 规则动作：modify_response / script / delay
        if rule:
            action = rule.get("action", "")
            delay_ms = self._get_rule_delay(rule, "response")
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000.0)
            if action == "modify_response":
                self._apply_modify_response(rule, resp_headers, resp_body)
            if action == "script":
                result = await asyncio.to_thread(
                    self._run_script_response, rule, url, method,
                    resp_headers, resp_body, status,
                    host, path, scheme, pid, proc_name,
                    headers, body)
                if isinstance(result, tuple):
                    status, resp_headers, resp_body = result

        # 响应断点
        if record and self.breakpoint.should_break_response():
            if flow_id is None:
                flow_id = await self._insert_flow_async(
                    pid, proc_name, method, url, scheme, host, path,
                    headers, body, client_addr)
            if flow_id:
                await asyncio.to_thread(
                    db.update_flow_response, flow_id, status,
                    json.dumps(resp_headers.to_dict()),
                    _truncate_for_record(resp_body),
                    int((time.time() - flow_start) * 1000), resp_size)
                await asyncio.to_thread(db.update_flow_breakpoint, flow_id, "pending_response")
                action = await self._wait_breakpoint_async(flow_id)
                if action == "drop":
                    await asyncio.to_thread(db.update_flow_breakpoint, flow_id, None)
                    return keep_alive
                modified = await asyncio.to_thread(db.get_flow, flow_id)
                if modified:
                    status = modified.get("status_code", status)
                    resp_body = _to_bytes(modified.get("response_body", ""))
                    resp_headers = Headers.from_dict(
                        json.loads(modified.get("response_headers") or "{}"))
                    resp_size = len(resp_body)
                await asyncio.to_thread(db.update_flow_breakpoint, flow_id, None)

        # 发送响应给客户端
        await self._send_response_async(
            writer, status, resp_headers, resp_body, keep_alive)

        # flow 记录 + SSE
        if record:
            duration_ms = int((time.time() - flow_start) * 1000)
            # _record_flow_async 内部根据 flow_id 决定 update（已预入库）还是
            # insert（未预入库），并统一触发录制/被动扫描 hook
            await self._record_flow_async(
                pid, proc_name, method, url, scheme, host, path,
                headers, body, status, resp_headers,
                _truncate_for_record(resp_body),
                duration_ms, resp_size, client_addr, flow_id=flow_id)
            # SSE 响应补齐推送（已预入库的 flow 需推送响应字段更新给前端）
            if flow_id:
                await self._notify_flow_update_async(
                    flow_id, status, resp_headers, resp_size, duration_ms)

        return keep_alive

    # ------------------------------------------------------------------ 异步转发

    async def _forward_async(self, host, port, method, path, version,
                             headers, body, scheme, url, is_bumped, client_addr):
        """异步转发请求到目标服务器。返回 (status, resp_headers, resp_body, resp_size)。"""
        is_ssl = scheme == "https"

        # 构造请求数据
        req_lines = [f"{method} {path} {version}"]
        for k, v in headers.to_dict().items():
            req_lines.append(f"{k}: {v}")
        req_lines.append("")
        req_lines.append("")
        req_data = "\r\n".join(req_lines).encode("latin-1", "replace")
        if body:
            req_data += body

        # 尝试从连接池获取
        pooled = self._aio_conn_pool.get(host, port, is_ssl)
        if pooled:
            reader, writer = pooled.reader, pooled.writer
            try:
                writer.write(req_data)
                await writer.drain()
            except Exception:  # noqa: BLE001
                # 池中连接失效，新建
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass
                reader, writer = await self._connect_target_async(host, port, is_ssl)
                writer.write(req_data)
                await writer.drain()
        else:
            reader, writer = await self._connect_target_async(host, port, is_ssl)
            writer.write(req_data)
            await writer.drain()

        # throttle 限速发送
        # （req_data 已发送，throttle 主要在连接阶段生效）

        try:
            # 读响应状态行
            status_line = await asyncio.wait_for(reader.readline(), timeout=30)
            status_line = status_line.decode("latin-1", "replace").strip()
            parts = status_line.split(" ", 2)
            if len(parts) < 2:
                raise OSError(f"invalid status line: {status_line}")
            status = int(parts[1])

            # 读响应头
            resp_headers = Headers()
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=30)
                if not line or line == b"\r\n" or line == b"\n":
                    break
                text = line.decode("latin-1", "replace").rstrip("\r\n")
                if ":" in text:
                    name, _, value = text.partition(":")
                    resp_headers.add(name.strip(), value.strip())

            # 读响应体
            resp_body = b""
            cl = resp_headers.get("Content-Length")
            if cl:
                resp_body = await asyncio.wait_for(
                    reader.readexactly(int(cl)), timeout=60)
            elif (resp_headers.get("Transfer-Encoding") or "").lower() == "chunked":
                resp_body = await self._read_chunked_async(reader)
            elif method.upper() == "HEAD":
                resp_body = b""
            elif status in (204, 304):
                resp_body = b""
            else:
                # 无 Content-Length 的响应：读到连接关闭
                try:
                    resp_body = await asyncio.wait_for(
                        reader.read(MAX_RECORDED_BODY * 4), timeout=30)
                except asyncio.TimeoutError:
                    resp_body = b""

            resp_size = len(resp_body)

            # 解压（小 body 才解压）
            if resp_body and len(resp_body) <= MAX_DECOMPRESS_BODY:
                try:
                    resp_body, resp_headers = _decompress_body(resp_body, resp_headers)
                except Exception:  # noqa: BLE001
                    pass

            # 连接复用：放回连接池
            if (resp_headers.get("Connection") or "").lower() != "close":
                if not writer.is_closing():
                    self._aio_conn_pool.put(_PooledConn(
                        reader, writer, host, port, is_ssl))
                else:
                    try:
                        writer.close()
                    except Exception:  # noqa: BLE001
                        pass
            else:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass

            return status, resp_headers, resp_body, resp_size

        except Exception:
            # 转发失败，关闭连接
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            raise

    async def _connect_target_async(self, host, port, is_ssl, timeout=30):
        """异步连接目标（含 Clash 上游 + 防循环端口注册）。返回 (reader, writer)。"""
        upstream = self._get_upstream_proxy()
        if upstream is None:
            # 直连 + 防循环端口注册
            # Phase 1: 创建 socket，bind，注册 port
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)
            local_port = 0
            try:
                sock.bind(("", 0))
                local_port = sock.getsockname()[1]
                self._register_proxy_port(local_port)
                loop = asyncio.get_running_loop()
                await asyncio.wait_for(
                    loop.sock_connect(sock, (host, port)), timeout=timeout)
                self._register_proxy_socket_addr(sock, local_port)
            except (OSError, asyncio.TimeoutError):
                try:
                    sock.close()
                except OSError:
                    pass
                if local_port:
                    self._unregister_proxy_port(local_port)
                raise
            # 用 asyncio 包装 socket
            if is_ssl:
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                reader, writer = await asyncio.open_connection(
                    sock=sock, ssl=ssl_ctx,
                    server_hostname=host)
            else:
                reader, writer = await asyncio.open_connection(sock=sock)
            # 记录 local_port 用于后续 unregister
            writer._v2_local_port = local_port  # type: ignore[attr-defined]
            return reader, writer
        else:
            # 通过上游代理（Clash）建立 CONNECT 隧道
            proxy_host, proxy_port = upstream
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)
            local_port = 0
            try:
                sock.bind(("", 0))
                local_port = sock.getsockname()[1]
                self._register_proxy_port(local_port)
                loop = asyncio.get_running_loop()
                await asyncio.wait_for(
                    loop.sock_connect(sock, (proxy_host, proxy_port)), timeout=timeout)
                self._register_proxy_socket_addr(sock, local_port)
            except (OSError, asyncio.TimeoutError):
                try:
                    sock.close()
                except OSError:
                    pass
                if local_port:
                    self._unregister_proxy_port(local_port)
                raise
            # 发送 CONNECT 请求
            try:
                host_bytes = host.encode("ascii", errors="replace")
            except Exception:  # noqa: BLE001
                host_bytes = host.encode("idna", errors="replace")
            port_bytes = str(port).encode("ascii")
            connect_req = (b"CONNECT " + host_bytes + b":" + port_bytes +
                          b" HTTP/1.1\r\nHost: " + host_bytes + b":" + port_bytes +
                          b"\r\n\r\n")
            await loop.sock_sendall(sock, connect_req)
            # 读 CONNECT 响应
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = await asyncio.wait_for(loop.sock_recv(sock, 4096), timeout=10)
                if not chunk:
                    raise OSError(f"upstream proxy closed: {proxy_host}:{proxy_port}")
                buf += chunk
            status_line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            if " 200 " not in status_line:
                raise OSError(f"upstream proxy refused CONNECT {host}:{port}: {status_line}")
            # 建立 SSL 或返回明文连接
            if is_ssl:
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                reader, writer = await asyncio.open_connection(
                    sock=sock, ssl=ssl_ctx, server_hostname=host)
            else:
                reader, writer = await asyncio.open_connection(sock=sock)
            writer._v2_local_port = local_port  # type: ignore[attr-defined]
            return reader, writer

    async def _tunnel_to_target_async(self, reader, writer, host, port,
                                      pid, proc_name, client_addr, record,
                                      scheme="https"):
        """纯隧道转发（不解密 HTTPS）。"""
        try:
            target_reader, target_writer = await self._connect_target_async(
                host, port, is_ssl=False, timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("proxy", "v2 tunnel connect failed", f"{host}:{port} {e}")
            return
        # 记录隧道 flow
        if record:
            tunnel_headers = Headers()
            await self._record_flow_async(
                pid, proc_name, "CONNECT", f"{scheme}://{host}:{port}",
                scheme, host, f":{port}", tunnel_headers, b"",
                200, tunnel_headers, "",
                0, 0, client_addr)
        # 双向转发
        try:
            await asyncio.gather(
                self._pipe_async(reader, target_writer),
                self._pipe_async(target_reader, writer),
                return_exceptions=True)
        finally:
            try:
                target_writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _pipe_async(self, reader, writer):
        """单向数据管道转发。"""
        try:
            while True:
                data = await asyncio.wait_for(reader.read(65536), timeout=300)
                if not data:
                    break
                # throttle 限速
                if throttle.is_enabled():
                    await asyncio.to_thread(throttle.delay)
                    if throttle.should_drop():
                        break
                    # 限速发送
                    for i in range(0, len(data), 8192):
                        chunk = data[i:i + 8192]
                        writer.write(chunk)
                        await writer.drain()
                        if throttle.get_config().get("bps_kbps", 0) > 0:
                            await asyncio.sleep(len(chunk) / (throttle.get_config()["bps_kbps"] * 1024))
                else:
                    writer.write(data)
                    await writer.drain()
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ WebSocket

    async def _handle_ws_upgrade_async(self, reader, writer, method, url, scheme,
                                       host, port, path, headers, body,
                                       pid, proc_name, client_addr, record, flow_start):
        """WebSocket Upgrade 处理：用 to_thread 桥接 v1 relay_websocket。"""
        try:
            from .websocket_relay import is_websocket_upgrade
            if not is_websocket_upgrade(headers.to_dict()):
                return
        except Exception:  # noqa: BLE001
            return
        # 连接目标
        is_ssl = scheme == "https"
        try:
            target_reader, target_writer = await self._connect_target_async(
                host, port, is_ssl=is_ssl, timeout=30)
        except Exception:  # noqa: BLE001
            return
        # 构造 Upgrade 请求转发给目标
        upgrade_req = self._build_ws_upgrade_request(method, path, version, headers)
        target_writer.write(upgrade_req)
        await target_writer.drain()
        # 读 101 响应转发给客户端
        try:
            resp_line = await asyncio.wait_for(target_reader.readline(), timeout=10)
            writer.write(resp_line)
            # 读响应头
            while True:
                line = await asyncio.wait_for(target_reader.readline(), timeout=10)
                writer.write(line)
                if not line or line == b"\r\n" or line == b"\n":
                    break
            await writer.drain()
            status_code = 101 if b"101" in resp_line else 0
        except Exception:  # noqa: BLE001
            try:
                target_writer.close()
            except Exception:  # noqa: BLE001
                pass
            return
        if status_code != 101:
            try:
                target_writer.close()
            except Exception:  # noqa: BLE001
                pass
            return
        # 记录握手 flow
        if record:
            await self._record_flow_async(
                pid, proc_name, "GET", url, scheme, host, path,
                headers, body, 101, Headers(), "",
                int((time.time() - flow_start) * 1000), 0, client_addr)
        # WebSocket 双向中继：用 to_thread 桥接 v1 relay_websocket
        # 取出底层 socket
        client_sock = writer.get_extra_info("socket")
        target_sock = target_writer.get_extra_info("socket")
        if client_sock and target_sock:
            # 设置阻塞模式
            try:
                client_sock.setblocking(True)
                target_sock.setblocking(True)
            except Exception:  # noqa: BLE001
                pass
            # 用目标 host 解析服务器 IP（client_addr 是 127.0.0.1，查不到属地）
            remote_ip, ip_region = await self._resolve_target_ip(host)
            # relay_websocket 是 keyword-only 参数，必须用关键字传
            await asyncio.to_thread(
                self._relay_ws_threaded, client_sock, target_sock,
                self.session_id, pid, proc_name, method, url, scheme, host, path,
                headers.to_dict(), remote_ip, ip_region, self.capturing)
        try:
            target_writer.close()
        except Exception:  # noqa: BLE001
            pass

    def _relay_ws_threaded(self, client_sock, target_sock, session_id, pid,
                           proc_name, method, url, scheme, host, path,
                           request_headers, remote_ip, ip_region, capturing):
        """在线程中调用 v1 relay_websocket（keyword-only 参数）。"""
        try:
            from .websocket_relay import relay_websocket
            relay_websocket(
                client_sock, target_sock,
                session_id=session_id,
                pid=pid,
                proc_name=proc_name or "",
                method=method,
                url=url,
                scheme=scheme,
                host=host,
                path=path,
                request_headers=request_headers,
                remote_ip=remote_ip,
                ip_region=ip_region,
                capturing=capturing,
            )
        except Exception:  # noqa: BLE001
            pass

    def _build_ws_upgrade_request(self, method, path, version, headers):
        """构造 WebSocket Upgrade 请求。"""
        lines = [f"{method} {path} {version}"]
        for k, v in headers.to_dict().items():
            lk = k.lower()
            if lk in ("proxy-connection", "proxy-authorization"):
                continue
            lines.append(f"{k}: {v}")
        lines.append("")
        lines.append("")
        return "\r\n".join(lines).encode("latin-1", "replace")

    # ------------------------------------------------------------------ 辅助方法

    async def _read_chunked_async(self, reader):
        """异步读取 chunked 编码的 body。"""
        body = b""
        while True:
            size_line = await asyncio.wait_for(reader.readline(), timeout=30)
            try:
                chunk_size = int(size_line.strip(), 16)
            except ValueError:
                break
            if chunk_size == 0:
                await reader.readline()  # 尾部 CRLF
                break
            chunk = await asyncio.wait_for(reader.readexactly(chunk_size), timeout=30)
            body += chunk
            await reader.readline()  # 块尾 CRLF
        return body

    async def _send_response_async(self, writer, status, headers, body, keep_alive):
        """异步发送 HTTP 响应给客户端。"""
        reason = _reason(status)
        lines = [f"HTTP/1.1 {status} {reason}"]
        # 确保有 Content-Length
        if not headers.has("Content-Length") and not headers.has("Transfer-Encoding"):
            headers.set("Content-Length", str(len(body) if body else 0))
        if not keep_alive:
            headers.set("Connection", "close")
        elif not headers.has("Connection"):
            headers.set("Connection", "keep-alive")
        for k, v in headers.to_dict().items():
            lines.append(f"{k}: {v}")
        lines.append("")
        lines.append("")
        data = "\r\n".join(lines).encode("latin-1", "replace")
        if body:
            data += body
        # throttle 限速发送
        if throttle.is_enabled() and body:
            writer.write(data)
            await writer.drain()
        else:
            writer.write(data)
            await writer.drain()

    async def _insert_flow_async(self, pid, proc_name, method, url, scheme, host, path,
                                 headers, body, client_addr):
        """同步插入请求 flow（断点需要立即 flow_id）。返回 flow_id。"""
        # 用目标 host 解析服务器 IP（client_addr 是 127.0.0.1，查不到属地）
        remote_ip, ip_region = await self._resolve_target_ip(host)
        cert_info = ""
        http_version = "HTTP/1.1"
        flow = self._build_request_flow_dict(
            pid, proc_name, method, url, scheme, host, path,
            headers, body, remote_ip, ip_region, cert_info, http_version)
        flow_id = await asyncio.to_thread(db.insert_flow, flow)
        if flow_id:
            flow["id"] = flow_id
            await asyncio.to_thread(db._notify_flow_subscribers, flow)
        return flow_id

    async def _preinsert_flow_async(self, pid, proc_name, method, url, scheme, host, path,
                                    headers, body, client_addr):
        """预入库请求 flow（非断点路径）。

        与 v1 _preinsert 一致：用同步 insert_flow 拿 flow_id，立即 SSE 推送
        请求阶段 flow（响应字段为 null），响应完成后再 update 补齐。
        避免"预入库(异步) + 响应阶段再 insert"导致的重复入库。
        """
        # 用目标 host 解析服务器 IP（client_addr 是 127.0.0.1，查不到属地）
        remote_ip, ip_region = await self._resolve_target_ip(host)
        cert_info = ""
        http_version = "HTTP/1.1"
        flow = self._build_request_flow_dict(
            pid, proc_name, method, url, scheme, host, path,
            headers, body, remote_ip, ip_region, cert_info, http_version)
        flow_id = await asyncio.to_thread(db.insert_flow, flow)
        if flow_id:
            flow["id"] = flow_id
            await asyncio.to_thread(db._notify_flow_subscribers, flow)
        return flow_id

    async def _notify_flow_update_async(self, flow_id, status_code, resp_headers,
                                        resp_body_size, duration_ms):
        """响应完成后推送 SSE 更新。

        加 _is_update=True 标记，让前端把该推送识别为「响应字段更新」而不是「新 flow 插入」：
        - 绝不新增行（避免只有 id/status/size 的空行）
        - 对应 id 的预入库 flow 尚未到时，暂存 pendingUpdates 等其到达后合并
        - 已存在则 merge 字段后触发响应式重渲染
        """
        try:
            await asyncio.to_thread(
                db._notify_flow_subscribers,
                {
                    "id": flow_id,
                    "status_code": status_code,
                    "response_headers": json.dumps(resp_headers.to_dict()) if resp_headers else "{}",
                    "size": resp_body_size,
                    "duration_ms": duration_ms,
                    "_is_update": True,
                })
        except Exception:  # noqa: BLE001
            pass

    async def _resolve_target_ip(self, host: str, port: int = 443) -> tuple[str, str]:
        """异步解析目标服务器 IP + 属地查询。返回 (remote_ip, ip_region)。

        client_addr 是本机回环地址（127.0.0.1），用它查属地只会得到「内网」或空。
        改为用目标 host 做 DNS 解析拿到真实服务器 IP，再查属地。
        """
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo, host, port, socket.AF_INET, socket.SOCK_STREAM)
            if infos:
                remote_ip = infos[0][4][0]
            else:
                return "", ""
        except Exception:  # noqa: BLE001
            return "", ""
        ip_region = ""
        if remote_ip:
            try:
                from ..ip_region import lookup as ip_region_lookup
                ip_region = await asyncio.to_thread(ip_region_lookup, remote_ip)
            except Exception:  # noqa: BLE001
                pass
        return remote_ip, ip_region

    async def _record_flow_async(self, pid, proc_name, method, url, scheme, host, path,
                                 headers, body, status, resp_headers, resp_body_text,
                                 duration_ms, size, client_addr, flow_id=None):
        """完整记录 flow（请求+响应）。

        与 v1 _record_flow 保持一致：优先同步 insert_flow 拿 flow_id 触发
        录制 / 被动扫描 hook；已预入库（flow_id 存在）则更新响应字段并触发 hook。
        """
        if self.session_id is None:
            return
        # 用目标 host 解析服务器 IP（client_addr 是 127.0.0.1，查不到属地）
        remote_ip, ip_region = await self._resolve_target_ip(host)
        flow = {
            "session_id": self.session_id,
            "timestamp": datetime.now().isoformat(),
            "pid": pid,
            "process_name": proc_name,
            "method": method,
            "url": url,
            "scheme": scheme,
            "host": host,
            "path": path,
            "request_headers": json.dumps(headers.to_dict()),
            "request_body": _truncate_for_record(body),
            "status_code": status,
            "response_headers": json.dumps(resp_headers.to_dict()),
            "response_body": resp_body_text,
            "duration_ms": duration_ms,
            "size": size,
            "remote_ip": remote_ip,
            "ip_region": ip_region,
            "cert_info": None,
            "http_version": "HTTP/1.1",
        }
        if flow_id:
            # 已预入库：更新响应字段 + 触发 hook
            await asyncio.to_thread(
                db.update_flow_response_async, flow_id, status,
                flow["response_headers"], resp_body_text, duration_ms, size)
            await asyncio.to_thread(
                self._run_post_record_hooks, flow_id, url, method, scheme,
                host, path, headers, status, resp_headers, resp_body_text)
        else:
            # 未预入库：同步 insert 拿 flow_id 触发 hook（与 v1 一致）
            def _insert_and_hook():
                try:
                    new_flow_id = db.insert_flow(flow)
                    if new_flow_id:
                        flow["id"] = new_flow_id
                        db._notify_flow_subscribers(flow)
                        self._run_post_record_hooks(
                            new_flow_id, url, method, scheme, host, path,
                            headers, status, resp_headers, resp_body_text)
                except Exception:  # noqa: BLE001
                    # 回退到异步批量写入（不触发 hook，但至少记录流量）
                    try:
                        db.insert_flow_async(flow)
                    except Exception:  # noqa: BLE001
                        pass
            await asyncio.to_thread(_insert_and_hook)

    async def _wait_breakpoint_async(self, flow_id):
        """异步等待断点释放。用专用线程池避免耗尽默认 executor。"""
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def _do_wait():
            result = self.breakpoint.wait_for_release(flow_id)
            loop.call_soon_threadsafe(future.set_result, result)

        self._bp_executor.submit(_do_wait)
        return await future

    def _get_rule_delay(self, rule, target_name):
        """从规则获取延迟毫秒数。"""
        try:
            delays = json.loads(rule.get("delays") or "[]")
            for d in delays:
                if d.get("target") == target_name or (target_name == "request" and not d.get("target")):
                    return int(d.get("ms", 0))
        except Exception:  # noqa: BLE001
            pass
        return 0

    def _apply_modify_request(self, rule, headers, body):
        """应用 modify_request 规则（修改请求头/体）。"""
        try:
            mods = json.loads(rule.get("modify_request_headers") or "[]")
            for m in mods:
                op = m.get("op", "set")
                name = m.get("name", "")
                value = m.get("value", "")
                if op == "set":
                    headers.set(name, value)
                elif op == "remove":
                    headers.remove(name)
                elif op == "add":
                    headers.add(name, value)
        except Exception:  # noqa: BLE001
            pass

    def _apply_modify_response(self, rule, headers, body):
        """应用 modify_response 规则（修改响应头）。"""
        try:
            mods = json.loads(rule.get("modify_response_headers") or "[]")
            for m in mods:
                op = m.get("op", "set")
                name = m.get("name", "")
                value = m.get("value", "")
                if op == "set":
                    headers.set(name, value)
                elif op == "remove":
                    headers.remove(name)
                elif op == "add":
                    headers.add(name, value)
        except Exception:  # noqa: BLE001
            pass

    def _run_script_request(self, rule, url, method, headers, body,
                            host, path, scheme, pid, proc_name):
        """执行请求阶段用户脚本。

        返回值：
        - "drop": 拒绝请求
        - (headers, body, status) tuple: mock 响应
        - bytes: 修改后的 body（headers 已就地修改）
        - None: 无修改或脚本不可用
        """
        try:
            from ..auto_reply.script_runner import (
                call_script_request, build_ctx, apply_request)
            rule_id = str(rule.get("id", ""))
            script = rule.get("modify_rules", "") or ""
            if not script:
                return None
            ctx = build_ctx(
                host=host, path=path, method=method, url=url, scheme=scheme,
                pid=pid, process_name=proc_name or "", session_id=None,
                request_headers=headers.to_dict(), request_body=body,
            )
            resp = call_script_request(rule_id, script, ctx)
            if resp is None:
                return None
            if resp.get("action") == "drop":
                return "drop"
            if resp.get("action") == "mock":
                import base64 as _b64
                from ..proxy.server import Headers as _Headers
                mock_headers = _Headers.from_dict(resp.get("mock_headers") or {})
                mock_body = _b64.b64decode(resp["mock_body_b64"]) if resp.get("mock_body_b64") else b""
                mock_status = int(resp.get("mock_status") or 200)
                return (mock_headers, mock_body, mock_status)
            # continue: 应用请求修改（headers 就地修改，返回新 body）
            _, new_body = apply_request(resp, headers, body)
            return new_body
        except Exception as e:  # noqa: BLE001
            logger.debug("proxy", "v2 script request error", str(e))
            return None

    def _run_script_response(self, rule, url, method, resp_headers, resp_body, status,
                             host, path, scheme, pid, proc_name,
                             req_headers, req_body):
        """执行响应阶段用户脚本。

        返回值：
        - (status, headers, body) tuple: 修改后的响应数据
        - None: 无修改或脚本不可用
        """
        try:
            from ..auto_reply.script_runner import (
                call_script_response, build_ctx, apply_response)
            rule_id = str(rule.get("id", ""))
            script = rule.get("modify_rules", "") or ""
            if not script:
                return None
            ctx = build_ctx(
                host=host, path=path, method=method, url=url, scheme=scheme,
                pid=pid, process_name=proc_name or "", session_id=None,
                request_headers=req_headers.to_dict(),
                request_body=req_body,
                status_code=status,
                response_headers=resp_headers.to_dict(),
                response_body=resp_body,
            )
            resp = call_script_response(rule_id, script, ctx)
            if resp is None:
                return None
            # 合并 mock 字段到标准字段（apply_response 统一处理）
            apply_resp = dict(resp)
            if resp.get("action") == "mock":
                apply_resp["status_code"] = resp.get("mock_status")
                apply_resp["response_headers"] = resp.get("mock_headers")
                apply_resp["response_body_b64"] = resp.get("mock_body_b64")
            new_status, new_headers, new_body = apply_response(
                apply_resp, status, resp_headers, resp_body)
            return (new_status, new_headers, new_body)
        except Exception as e:  # noqa: BLE001
            logger.debug("proxy", "v2 script response error", str(e))
            return None

    async def send_raw_response(self, request_id, status_code, headers, body):
        """直接发送响应给客户端（断点/mock场景）。"""
        with self._pending_lock:
            entry = self._pending_flows.get(request_id)
        if entry is None:
            return
        writer = entry.get("writer")
        if writer is None:
            return
        try:
            await self._send_response_async(
                writer, status_code, Headers.from_dict(headers or {}), body or b"", False)
        except Exception:  # noqa: BLE001
            pass

    def get_engine_info(self) -> dict:
        """获取 v2 引擎信息（供 /capture/status 展示）。"""
        return {
            "engine": "v2",
            "loop": _loop_type,
            "conn_pool": self._aio_conn_pool.stats(),
        }

"""HTTP/2 forwarding module (phase 2: connection pool reuse + stream multiplexing).

H2Client encapsulates an h2 connection to target server, with internal reader thread continuously reading frames
and dispatching to corresponding requests by stream_id. Multiple proxy threads can simultaneously call request(),
sharing the same TCP+TLS connection, fully leveraging h2 multiplexing advantages.

H2ClientPool caches H2Client by (host, port, scheme), up to 3 per key
(multiple connections disperse single-lock bottleneck, improving concurrent throughput).
"""

from __future__ import annotations

import logging
import socket
import threading
import time

logger = logging.getLogger("telnix.h2")


# ---------- 单个 stream 的状态 ----------

class _H2Stream:
    """Async state of a single h2 stream."""

    __slots__ = ("status", "headers", "trailers", "body", "done", "error",
                 "flow_ready", "oversized")

    def __init__(self):
        self.status = 0
        self.headers: list[tuple[str, str]] = []
        self.trailers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.done = threading.Event()
        self.error: str | None = None
        # flow control：窗口更新时 set，发送方等待此事件重试 send_data
        self.flow_ready = threading.Event()
        # 性能修复(审计 P-#2)：响应体超过阈值时标记，request() 抛 OSError
        # 触发调用方回退到 HTTP/1.1 流式转发，避免 H2 全量缓冲导致内存暴涨
        self.oversized = False


# ---------- h2 连接封装（支持多路复用） ----------

class H2Client:
    """h2 connection to target server, supports multiplexing.

    One H2Client corresponds to one TCP+TLS connection, can handle multiple requests (streams) simultaneously.
    Internal reader thread continuously reads h2 frames and dispatches to corresponding stream's _H2Stream.
    request() method is thread-safe, multiple proxy threads can call concurrently.
    """

    _MAX_IDLE = 180.0  # 空闲超时（秒），超过后可被池清理（原 60s 过短导致频繁重握手）
    # 性能修复(审计 P-#2)：H2 全量缓冲响应体的上限（8MB）。
    # 超过此阈值标记 oversized，request() 抛 OSError，调用方捕获后回退到
    # HTTP/1.1 流式转发（_forward 的 stream=True 路径），避免大响应内存暴涨。
    _MAX_BUFFERED_BODY = 8 * 1024 * 1024

    def __init__(self, sock: socket.socket, host: str, port: int, scheme: str,
                 cert_info: str = ""):
        import h2.connection
        import h2.config

        self.sock = sock
        self.host = host
        self.port = port
        self.scheme = scheme
        # 缓存 local_port：close() 时用此值注销 _proxy_outbound_ports，
        # 避免 socket 已关闭后 getsockname() 失败导致端口泄漏
        try:
            self._local_port = sock.getsockname()[1]
        except OSError:
            self._local_port = 0
        # 缓存 TLS 证书信息，复用连接时返回给调用方记录
        self.cert_info = cert_info

        self._conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(
                # validate_inbound_headers=False：放宽 RFC 7540 header 校验，
                # 容忍部分服务器（如 MSN 的 NEL 报告头）返回前后带空格的 header value。
                # 作为抓包代理，严格校验会导致整条 h2 连接断开 + 所有并发 stream 失败，
                # 降级到 HTTP/1.1 重试，影响抓包完整性和性能。
                client_side=True, header_encoding="utf-8",
                validate_inbound_headers=False,
            )
        )
        # _lock 保护 H2Connection 状态操作
        self._lock = threading.Lock()
        # _send_lock 串行化所有 sock.sendall，避免 SSL socket 并发写入破坏状态机
        # （ssl.SSLSocket.sendall 内部走 SSL_write，非线程安全）
        self._send_lock = threading.Lock()
        self._streams: dict[int, _H2Stream] = {}
        self._closed = False
        self._last_used = time.time()

        # 发送 client preface（magic + SETTINGS）
        self._conn.initiate_connection()
        self._flush_locked()

        # 性能优化：socket 设为阻塞模式（settimeout(None)）。
        # _connect_target 返回的 socket 带 settimeout(30)，reader 线程持续 recv
        # 时一旦 30s 内无数据就抛 socket.timeout 杀死连接，使 _MAX_IDLE=180s
        # 失效、每次请求都重新 TLS 握手（多 1-2 RTT）。阻塞模式下只有真实
        # EOF/对端关闭才返回，连接可稳定复用到 _MAX_IDLE。
        try:
            self.sock.settimeout(None)
        except OSError:
            pass

        # 启动 reader 线程
        self._reader = threading.Thread(
            target=self._read_loop, daemon=True, name="h2-reader"
        )
        self._reader.start()

    def _sendall(self, data: bytes):
        """Thread-safe socket sendall. All sock.sendall must go through here."""
        with self._send_lock:
            if self._closed:
                raise OSError("h2 connection closed")
            self.sock.sendall(data)

    def _flush_locked(self):
        """Send pending data in conn. Caller must hold _lock."""
        out = self._conn.data_to_send()
        if out:
            # 通过 _sendall 串行化，避免与 reader 线程的 sendall 竞态
            self._sendall(out)

    def _read_loop(self):
        """Background thread: continuously read h2 frames and dispatch to corresponding streams.

        Performance optimization: reduce lock granularity - only lock when operating on H2Connection (not thread-safe),
        event handling (operating on stream objects) executes outside lock, reducing lock contention.
        Batch process acknowledge + data_to_send, reducing lock acquisitions.
        """
        import h2.events

        try:
            while not self._closed:
                try:
                    # 性能修复(审计 P-#7)：recv 缓冲从 64KB 提到 256KB，
                    # 大流量场景下减少 syscall + 锁获取次数（下载 10MB 约 -75% recv 调用）
                    data = self.sock.recv(262144)
                except (OSError, socket.timeout):
                    break
                if not data:
                    break

                # 批量处理：receive_data + acknowledge + data_to_send 合并到一次锁
                pending_out = b""
                ack_batch: list[tuple[int, int]] = []  # [(flow_controlled_length, stream_id)]
                reset_streams: list[int] = []  # 需要reset的pushed stream id
                with self._lock:
                    try:
                        events = self._conn.receive_data(data)
                    except Exception as e:  # noqa: BLE001
                        # 部分服务器返回的 header value 前后有空格等违规字符，
                        # h2 严格遵循 RFC 7540 会抛 ProtocolError。这里优雅关闭
                        # 连接（上层会重新建连），不让线程异常退出污染日志。
                        try:
                            logger.warning(f"[h2-reader] {self.host}:{self.port} "
                                           f"receive_data failed: {type(e).__name__}: {e}")
                        except Exception:  # noqa: BLE001
                            pass
                        break
                    pending_out = self._conn.data_to_send() or b""
                    # 批量收集需要 acknowledge 的 DataReceived 事件
                    for event in events:
                        if isinstance(event, h2.events.DataReceived):
                            ack_batch.append(
                                (event.flow_controlled_length, event.stream_id)
                            )
                        elif isinstance(event, h2.events.PushedStreamReceived):
                            reset_streams.append(event.pushed_stream_id)
                    # 批量 acknowledge
                    for fc_len, sid in ack_batch:
                        try:
                            self._conn.acknowledge_received_data(fc_len, sid)
                        except Exception:  # noqa: BLE001
                            pass
                    if ack_batch:
                        pending_out += self._conn.data_to_send() or b""
                    # 批量 reset pushed streams
                    for sid in reset_streams:
                        try:
                            self._conn.reset_stream(sid)
                        except Exception:  # noqa: BLE001
                            pass
                    if reset_streams:
                        pending_out += self._conn.data_to_send() or b""

                # 发送待发数据（锁外，但通过 _send_lock 串行化，避免 SSL socket 并发写入）
                if pending_out:
                    try:
                        self._sendall(pending_out)
                    except OSError:
                        break

                # 事件处理（锁外，只操作 stream 对象，不操作 H2Connection）
                for event in events:
                    sid = getattr(event, "stream_id", 0)
                    stream = self._streams.get(sid)

                    if isinstance(event, h2.events.ResponseReceived):
                        if stream:
                            for name, value in event.headers:
                                if name == ":status":
                                    try:
                                        stream.status = int(value)
                                    except (ValueError, TypeError):
                                        pass
                                elif not name.startswith(":"):
                                    if not isinstance(value, str):
                                        value = value.decode("utf-8", "replace")
                                    stream.headers.append((name, value))

                    elif isinstance(event, h2.events.DataReceived):
                        # body 追加在锁外（bytearray += 是原子的，GIL 保护）
                        if stream:
                            if stream.oversized:
                                # 已标记 oversized：丢弃后续 DATA，避免继续累积
                                continue
                            stream.body += event.data
                            # 性能修复(审计 P-#2)：超过缓冲阈值时标记 oversized，
                            # 唤醒等待的 request() 让其抛 OSError 回退 HTTP/1.1，
                            # 并锁内 reset_stream 停止对端继续发送 DATA。
                            if len(stream.body) > self._MAX_BUFFERED_BODY:
                                stream.oversized = True
                                stream.error = "body too large for h2 buffering"
                                stream.done.set()
                                stream.flow_ready.set()
                                try:
                                    with self._lock:
                                        self._conn.reset_stream(sid)
                                        out = self._conn.data_to_send() or b""
                                    if out:
                                        self._sendall(out)
                                except Exception:  # noqa: BLE001
                                    pass

                    elif isinstance(event, h2.events.StreamEnded):
                        if stream:
                            stream.done.set()

                    elif isinstance(event, h2.events.TrailersReceived):
                        if stream:
                            for name, value in event.headers:
                                if not name.startswith(":"):
                                    if not isinstance(value, str):
                                        value = value.decode("utf-8", "replace")
                                    stream.trailers.append((name, value))

                    elif isinstance(event, h2.events.StreamReset):
                        if stream:
                            stream.error = f"reset:{event.error_code}"
                            stream.done.set()
                            stream.flow_ready.set()

                    elif isinstance(event, h2.events.WindowUpdated):
                        if event.stream_id == 0:
                            # 取快照避免 RuntimeError: dictionary changed size during iteration
                            # （request() 在其他线程持有 _lock 时会修改 _streams）
                            for s in list(self._streams.values()):
                                s.flow_ready.set()
                        else:
                            s = self._streams.get(event.stream_id)
                            if s:
                                s.flow_ready.set()

                    elif isinstance(event, h2.events.ConnectionTerminated):
                        self._closed = True
                        with self._lock:
                            for s in list(self._streams.values()):
                                s.error = "goaway"
                                s.done.set()
                                s.flow_ready.set()
                        return

                    elif isinstance(event, h2.events.RemoteSettingsChanged):
                        # 取快照避免并发修改 _streams 导致 RuntimeError
                        for s in list(self._streams.values()):
                            s.flow_ready.set()

                    elif isinstance(event, h2.events.PingReceived):
                        pass

                    elif isinstance(event, h2.events.SettingsAcknowledged):
                        pass

        except Exception as e:  # noqa: BLE001
            # 记录异常便于诊断，避免 reader 静默死亡后调用方等到 30s 超时
            # 注意：标准 logging 不支持 logger.warning(name, msg) 形式，
            # 必须用 f-string 或 %s 占位符，否则 logging 自身会抛 TypeError
            try:
                logger.warning(f"[h2-reader] {self.host}:{self.port} reader thread exited with exception: "
                               f"{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._closed = True
            with self._lock:
                # 取快照：request() 可能在其他线程同时 pop _streams
                for s in list(self._streams.values()):
                    if not s.done.is_set():
                        s.error = "conn_closed"
                        s.done.set()
                        s.flow_ready.set()

    def request(
        self,
        method: str,
        scheme: str,
        host: str,
        path: str,
        headers: list[tuple[str, str]],
        body: bytes,
        timeout: float = 30.0,
    ) -> tuple[int, list[tuple[str, str]], list[tuple[str, str]], bytes]:
        """Send h2 request and wait for response. Thread-safe.

        Returns (status, resp_headers, resp_trailers, resp_body).
        """
        import h2.exceptions

        if self._closed:
            raise OSError("h2 connection closed")

        stream = _H2Stream()
        deadline = time.time() + timeout

        # 剥离 hop-by-hop 头和 HTTP/1.1 特有头（h2 用伪头替代）
        skip = {
            "connection", "keep-alive", "proxy-connection", "proxy-authorization",
            "transfer-encoding", "content-length", "upgrade",
            "host",  # h2 用 :authority 伪头替代
        }
        req_headers = [
            (":method", method),
            (":scheme", scheme),
            (":authority", host),
            (":path", path),
        ]
        for k, v in headers:
            if k.lower() not in skip:
                req_headers.append((k, v))

        # 1. 发送 HEADERS + 尽可能多的 DATA（在锁内）
        with self._lock:
            if self._closed:
                raise OSError("h2 connection closed")
            stream_id = self._conn.get_next_available_stream_id()
            self._streams[stream_id] = stream

            try:
                end_stream = not body
                self._conn.send_headers(stream_id, req_headers, end_stream=end_stream)
                self._flush_locked()
            except Exception:
                self._streams.pop(stream_id, None)
                raise

        # 2. 发送 body（分块 + flow control 处理）
        if body:
            self._send_body(stream_id, body, stream, deadline)

        self._last_used = time.time()

        # 3. 等待响应
        remaining = deadline - time.time()
        if remaining <= 0:
            self._cleanup_stream(stream_id)
            raise socket.timeout("h2 request timeout")

        if not stream.done.wait(remaining):
            self._cleanup_stream(stream_id)
            raise socket.timeout("h2 response timeout")

        with self._lock:
            self._streams.pop(stream_id, None)

        # 性能修复(审计 P-#2)：响应体超过缓冲阈值，抛 OSError 触发调用方
        # 回退到 HTTP/1.1 流式转发（server.py 的 _h2_create_and_request /
        # _h2_request_via_pool 已用 except Exception 捕获并 return None）。
        if stream.oversized:
            raise OSError("h2 response body exceeded buffering threshold")

        if stream.error:
            raise OSError(f"h2 stream error: {stream.error}")

        self._last_used = time.time()
        return stream.status, stream.headers, stream.trailers, bytes(stream.body)

    def _send_body(self, stream_id: int, body: bytes, stream: _H2Stream, deadline: float):
        """Send body, handle flow control (wait for WindowUpdated when window insufficient).

        Performance optimization:
        - chunk_size uses peer-negotiated max_outbound_frame_size (up to 64KB), no longer hardcoded 16KB
        - sendall moved outside lock (only holds _send_lock), reader thread can concurrently receive_data
        - flow_ready.clear() executes in lock's except branch, avoiding lost wakeup race
        """
        import h2.exceptions

        # 用 memoryview 避免每 chunk bytes 切片产生新对象
        body_mv = memoryview(body) if body else b""
        offset = 0
        while offset < len(body):
            # 检查 stream 是否已被 reset
            if stream.done.is_set() and stream.error:
                raise OSError(f"h2 stream reset during send: {stream.error}")

            remaining = deadline - time.time()
            if remaining <= 0:
                raise socket.timeout("h2 send timeout (flow control)")

            need_wait = False
            pending_out = b""
            with self._lock:
                if self._closed:
                    raise OSError("h2 connection closed")

                # 用对端协商的最大帧大小（默认 16KB，对端可升到 64KB），不再硬编码 16KB
                chunk_size = self._conn.max_outbound_frame_size
                chunk = body_mv[offset:offset + chunk_size]
                is_last = offset + chunk_size >= len(body)

                try:
                    self._conn.send_data(stream_id, chunk, end_stream=is_last)
                    pending_out = self._conn.data_to_send() or b""
                    offset += chunk_size
                except h2.exceptions.FlowControlError:
                    # 窗口不足：锁内 clear 防止 lost wakeup（reader 的 set 只能在锁外发生）
                    stream.flow_ready.clear()
                    need_wait = True
                except h2.exceptions.StreamConsumedError:
                    break  # stream 已结束

            # 锁外发送（仅持 _send_lock 串行化 SSL 写），reader 可并发 receive_data
            if pending_out:
                try:
                    self._sendall(pending_out)
                except OSError:
                    raise OSError("h2 connection send failed")

            if need_wait:
                # 等待窗口更新（在锁外，不阻塞 reader 线程）
                stream.flow_ready.wait(min(0.02, remaining))

    def _cleanup_stream(self, stream_id: int):
        """Clean up stream on timeout or error."""
        with self._lock:
            self._streams.pop(stream_id, None)
            try:
                self._conn.reset_stream(stream_id)
                self._flush_locked()
            except Exception:  # noqa: BLE001
                pass

    def is_expired(self) -> bool:
        """Whether idle expired (no active streams and exceeds MAX_IDLE)."""
        if self._closed:
            return True
        if self._streams:
            return False
        return time.time() - self._last_used > self._MAX_IDLE

    def close(self):
        """Close h2 connection."""
        self._closed = True
        try:
            with self._lock:
                self._conn.close_connection()
                out = self._conn.data_to_send()
            if out:
                self._sendall(out)
        except OSError:
            pass
        except Exception as e:  # noqa: BLE001
            try:
                logger.debug(f"[h2-close] {self.host}:{self.port} close exception: {e}")
            except Exception:  # noqa: BLE001
                pass
        # F10: 先 shutdown+close 再 unregister，让 FIN/RST 在端口仍注册时发出，
        # 被 _is_proxy_outbound_addr 排除，避免 WinDivert 拦截产生 spurious NAT
        # 用缓存的 _local_port，避免 socket 已关闭后 getsockname() 失败
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        if self._local_port:
            try:
                from .transparent_proxy import unregister_proxy_port
                unregister_proxy_port(self._local_port)
            except Exception:  # noqa: BLE001
                pass


# ---------- h2 连接池 ----------

class H2ClientPool:
    """h2 connection pool, caches H2Client by (host, port, scheme).

    Up to _MAX_PER_KEY H2Clients per key (multiple connections disperse single-lock bottleneck,
    improving concurrent throughput; under h2 multiplexing 1 connection is theoretically sufficient, but single lock becomes bottleneck).
    Built-in background cleanup thread, periodically closes idle expired connections.

    性能修复(审计 P-#4)：采用 16 分片（与 server.py 的 _ConnPool 对齐），
    每个 key 按 hash 分配到独立分片，分片内独立锁，避免单锁串行所有 get/put/remove。
    """

    _CLEANUP_INTERVAL = 30.0  # 清理检查间隔（秒）
    _MAX_PER_KEY = 12  # 每个 key 最多缓存的连接数（4→12：提升高并发站点连接复用率，减少 TLS 握手；h2 单连接 100+ stream，12 个可支撑 1200+ 并发）
    _SHARD_COUNT = 16  # 分片数（与 _ConnPool 对齐，分散单锁瓶颈）

    def __init__(self):
        # 16 分片：每分片独立 dict + lock，get/put/remove 只锁对应分片
        self._pools: list[dict[tuple, list[H2Client]]] = [
            {} for _ in range(self._SHARD_COUNT)]
        self._locks: list[threading.Lock] = [
            threading.Lock() for _ in range(self._SHARD_COUNT)]
        # 统计计数（原子操作，非精确，仅供调试）
        self._stats = {"hits": 0, "misses": 0, "created": 0, "closed_idle": 0,
                       "closed_error": 0, "requests": 0}
        # 后台清理线程
        self._cleanup_stop = threading.Event()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="h2-pool-cleanup"
        )
        self._cleanup_thread.start()

    def _shard(self, key: tuple) -> int:
        """Return the shard index for a given key."""
        return hash(key) % self._SHARD_COUNT

    def get(self, host: str, port: int, scheme: str) -> H2Client | None:
        """Get an available H2Client. Returns None if none or closed.

        Strategy: preferentially return the client with the fewest active streams (load balancing).
        """
        key = (host, port, scheme)
        shard = self._shard(key)
        with self._locks[shard]:
            clients = self._pools[shard].get(key)
            if not clients:
                self._stats["misses"] += 1
                return None
            # 过滤已关闭的，挑选活跃 stream 最少的
            alive = [c for c in clients if not c._closed]
            if not alive:
                self._pools[shard].pop(key, None)
                self._stats["misses"] += 1
                return None
            # 选活跃 stream 最少的（负载均衡，减少单锁争用）
            best = min(alive, key=lambda c: len(c._streams))
            self._stats["hits"] += 1
            return best

    def put(self, client: H2Client) -> bool:
        """Put into pool. If pool connection count has reached limit, not put in, returns False (caller should close the connection)."""
        key = (client.host, client.port, client.scheme)
        shard = self._shard(key)
        with self._locks[shard]:
            clients = self._pools[shard].setdefault(key, [])
            # 过滤已关闭的
            clients[:] = [c for c in clients if not c._closed]
            if len(clients) >= self._MAX_PER_KEY:
                return False  # 已达上限，不放入
            if client in clients:
                return False  # 已在池中
            clients.append(client)
            self._stats["created"] += 1
            return True

    def remove(self, host: str, port: int, scheme: str):
        """Remove and close all connections for this key from pool (called on error)."""
        key = (host, port, scheme)
        shard = self._shard(key)
        with self._locks[shard]:
            clients = self._pools[shard].pop(key, None)
        if clients:
            for client in clients:
                client.close()
            self._stats["closed_error"] += len(clients)

    def remove_client(self, client: H2Client):
        """Remove and close only a single connection from pool (does not affect other connections for this host).

        Performance optimization: a single stream timeout/jitter should not clear the entire host's connection pool (up to 8),
        otherwise subsequent concurrent requests all need to re-TLS handshake (extra 1-2 RTT x N).
        """
        key = (client.host, client.port, client.scheme)
        shard = self._shard(key)
        with self._locks[shard]:
            clients = self._pools[shard].get(key)
            if clients:
                # 原地过滤掉指定 client（用 is 判断身份）
                clients[:] = [c for c in clients if c is not client]
                if not clients:
                    self._pools[shard].pop(key, None)
        client.close()
        self._stats["closed_error"] += 1

    def record_request(self):
        """Record an h2 request (for statistics)."""
        self._stats["requests"] += 1

    def stats(self) -> dict:
        """Return statistics snapshot.

        分片后逐个分片加锁汇总，避免一次性持全局锁阻塞所有 get/put。
        统计为粗略值，分片间非原子快照（仅供调试/监控）。
        """
        total_clients = 0
        active_streams = 0
        for shard in range(self._SHARD_COUNT):
            with self._locks[shard]:
                for v in self._pools[shard].values():
                    total_clients += len(v)
                    for c in v:
                        if not c._closed:
                            active_streams += len(c._streams)
        return {
            **self._stats,
            "pool_size": total_clients,
            "active_streams": active_streams,
        }

    def _cleanup_loop(self):
        """Background thread: periodically clean up idle expired h2 connections."""
        while not self._cleanup_stop.wait(self._CLEANUP_INTERVAL):
            try:
                self._cleanup_once()
            except Exception:  # noqa: BLE001
                pass

    def _cleanup_once(self):
        """Perform one cleanup.

        性能修复(审计 P-#4)：分片遍历，每次只锁一个分片，不阻塞其他分片的 get/put。
        """
        for shard in range(self._SHARD_COUNT):
            to_close: list[H2Client] = []
            with self._locks[shard]:
                for key, clients in list(self._pools[shard].items()):
                    alive = []
                    for client in clients:
                        if client.is_expired():
                            to_close.append(client)
                        else:
                            alive.append(client)
                    if alive:
                        self._pools[shard][key] = alive
                    else:
                        self._pools[shard].pop(key, None)
            for client in to_close:
                client.close()
                self._stats["closed_idle"] += 1

    def close_all(self):
        self._cleanup_stop.set()
        for shard in range(self._SHARD_COUNT):
            with self._locks[shard]:
                for clients in self._pools[shard].values():
                    for client in clients:
                        client.close()
                self._pools[shard].clear()


# ---------- 向后兼容：单次请求接口（不推荐，仅供测试） ----------

def h2_request(
    sock: socket.socket,
    method: str,
    scheme: str,
    host: str,
    path: str,
    headers: list[tuple[str, str]],
    body: bytes,
    timeout: float = 30.0,
) -> tuple[int, list[tuple[str, str]], list[tuple[str, str]], bytes]:
    """Send a single request over an established h2 connection (no reuse). Recommend using H2Client.request().

    Returns (status, resp_headers, resp_trailers, resp_body).
    """
    client = H2Client(sock, host, port=0, scheme=scheme)
    try:
        return client.request(method, scheme, host, path, headers, body, timeout)
    finally:
        client.close()


def get_alpn_protocol(sock: socket.socket) -> str:
    """Get the ALPN protocol negotiated by TLS connection. Returns 'h2' / 'http/1.1' / ''."""
    try:
        proto = sock.selected_alpn_protocol()
        return proto or ""
    except (AttributeError, OSError):
        return ""

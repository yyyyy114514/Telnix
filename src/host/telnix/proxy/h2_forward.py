"""HTTP/2 转发模块（第二阶段：连接池复用 + stream 多路复用）。

H2Client 封装一个到目标服务器的 h2 连接，内部有 reader 线程持续读取帧
并按 stream_id 分发到对应请求。多个代理线程可同时调用 request()，
共享同一个 TCP+TLS 连接，充分发挥 h2 多路复用优势。

H2ClientPool 按 (host, port, scheme) 缓存 H2Client，每个 key 最多 1 个
（h2 多路复用下 1 个连接即可并发）。
"""

from __future__ import annotations

import socket
import threading
import time


# ---------- 单个 stream 的状态 ----------

class _H2Stream:
    """单个 h2 stream 的异步状态。"""

    __slots__ = ("status", "headers", "trailers", "body", "done", "error", "flow_ready")

    def __init__(self):
        self.status = 0
        self.headers: list[tuple[str, str]] = []
        self.trailers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.done = threading.Event()
        self.error: str | None = None
        # flow control：窗口更新时 set，发送方等待此事件重试 send_data
        self.flow_ready = threading.Event()


# ---------- h2 连接封装（支持多路复用） ----------

class H2Client:
    """到目标服务器的 h2 连接，支持多路复用。

    一个 H2Client 对应一个 TCP+TLS 连接，可同时处理多个请求（stream）。
    内部 reader 线程持续读取 h2 帧并分发到对应 stream 的 _H2Stream。
    request() 方法线程安全，多个代理线程可并发调用。
    """

    _MAX_IDLE = 60.0  # 空闲超时（秒），超过后可被池清理

    def __init__(self, sock: socket.socket, host: str, port: int, scheme: str):
        import h2.connection
        import h2.config

        self.sock = sock
        self.host = host
        self.port = port
        self.scheme = scheme

        self._conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(
                client_side=True, header_encoding="utf-8"
            )
        )
        self._lock = threading.Lock()
        self._streams: dict[int, _H2Stream] = {}
        self._closed = False
        self._last_used = time.time()

        # 发送 client preface（magic + SETTINGS）
        self._conn.initiate_connection()
        self._flush_locked()

        # 启动 reader 线程
        self._reader = threading.Thread(
            target=self._read_loop, daemon=True, name="h2-reader"
        )
        self._reader.start()

    def _flush_locked(self):
        """发送 conn 中待发的数据。调用方需持有 _lock。"""
        out = self._conn.data_to_send()
        if out:
            self.sock.sendall(out)

    def _read_loop(self):
        """后台线程：持续读取 h2 帧并分发到对应 stream。

        性能优化：缩小锁粒度——只在操作 H2Connection（非线程安全）时加锁，
        事件处理（操作 stream 对象）在锁外执行，减少锁争用。
        批量处理 acknowledge + data_to_send，减少锁获取次数。
        """
        import h2.events

        try:
            while not self._closed:
                try:
                    data = self.sock.recv(65536)
                except (OSError, socket.timeout):
                    break
                if not data:
                    break

                # 批量处理：receive_data + acknowledge + data_to_send 合并到一次锁
                pending_out = b""
                ack_batch: list[tuple[int, int]] = []  # [(flow_controlled_length, stream_id)]
                reset_streams: list[int] = []  # 需要reset的pushed stream id
                with self._lock:
                    events = self._conn.receive_data(data)
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

                # 发送待发数据（锁外，避免 sendall 阻塞时持锁）
                if pending_out:
                    try:
                        self.sock.sendall(pending_out)
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
                            stream.body += event.data

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
                            for s in self._streams.values():
                                s.flow_ready.set()
                        else:
                            s = self._streams.get(event.stream_id)
                            if s:
                                s.flow_ready.set()

                    elif isinstance(event, h2.events.ConnectionTerminated):
                        self._closed = True
                        with self._lock:
                            for s in self._streams.values():
                                s.error = "goaway"
                                s.done.set()
                                s.flow_ready.set()
                        return

                    elif isinstance(event, h2.events.RemoteSettingsChanged):
                        for s in self._streams.values():
                            s.flow_ready.set()

                    elif isinstance(event, h2.events.PingReceived):
                        pass

                    elif isinstance(event, h2.events.SettingsAcknowledged):
                        pass

        except Exception:  # noqa: BLE001
            pass
        finally:
            self._closed = True
            with self._lock:
                for s in self._streams.values():
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
        """发送 h2 请求并等待响应。线程安全。

        返回 (status, resp_headers, resp_trailers, resp_body)。
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

        if stream.error:
            raise OSError(f"h2 stream error: {stream.error}")

        self._last_used = time.time()
        return stream.status, stream.headers, stream.trailers, bytes(stream.body)

    def _send_body(self, stream_id: int, body: bytes, stream: _H2Stream, deadline: float):
        """发送 body，处理 flow control（窗口不足时等待 WindowUpdated）。"""
        import h2.exceptions

        offset = 0
        while offset < len(body):
            # 检查 stream 是否已被 reset
            if stream.done.is_set() and stream.error:
                raise OSError(f"h2 stream reset during send: {stream.error}")

            remaining = deadline - time.time()
            if remaining <= 0:
                raise socket.timeout("h2 send timeout (flow control)")

            with self._lock:
                if self._closed:
                    raise OSError("h2 connection closed")

                chunk_size = min(self._conn.max_outbound_frame_size, 16384)
                chunk = body[offset:offset + chunk_size]
                is_last = offset + chunk_size >= len(body)

                try:
                    self._conn.send_data(stream_id, chunk, end_stream=is_last)
                    self._flush_locked()
                    offset += chunk_size
                    continue  # 发送成功，继续下一块
                except h2.exceptions.FlowControlError:
                    pass  # 窗口不足，需要等待
                except h2.exceptions.StreamConsumedError:
                    break  # stream 已结束

            # 等待窗口更新（在锁外，不阻塞 reader 线程）
            # 性能优化：100ms 轮询粒度（原 1 秒太粗，大 body 发送延迟明显）
            stream.flow_ready.clear()
            stream.flow_ready.wait(min(0.1, remaining))

    def _cleanup_stream(self, stream_id: int):
        """超时或出错时清理 stream。"""
        with self._lock:
            self._streams.pop(stream_id, None)
            try:
                self._conn.reset_stream(stream_id)
                self._flush_locked()
            except Exception:  # noqa: BLE001
                pass

    def is_expired(self) -> bool:
        """是否空闲过期（无活跃 stream 且超过 MAX_IDLE）。"""
        if self._closed:
            return True
        if self._streams:
            return False
        return time.time() - self._last_used > self._MAX_IDLE

    def close(self):
        """关闭 h2 连接。"""
        self._closed = True
        try:
            with self._lock:
                self._conn.close_connection()
                out = self._conn.data_to_send()
            if out:
                self.sock.sendall(out)
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------- h2 连接池 ----------

class H2ClientPool:
    """h2 连接池，按 (host, port, scheme) 缓存 H2Client。

    每个 key 最多 1 个 H2Client（h2 多路复用下 1 个连接即可并发）。
    内置后台清理线程，定期关闭空闲过期的连接。
    """

    _CLEANUP_INTERVAL = 30.0  # 清理检查间隔（秒）

    def __init__(self):
        self._pool: dict[tuple, H2Client] = {}
        self._lock = threading.Lock()
        # 统计计数（原子操作，非精确，仅供调试）
        self._stats = {"hits": 0, "misses": 0, "created": 0, "closed_idle": 0,
                       "closed_error": 0, "requests": 0}
        # 后台清理线程
        self._cleanup_stop = threading.Event()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="h2-pool-cleanup"
        )
        self._cleanup_thread.start()

    def get(self, host: str, port: int, scheme: str) -> H2Client | None:
        """获取可用的 H2Client。没有或已关闭返回 None。"""
        key = (host, port, scheme)
        with self._lock:
            client = self._pool.get(key)
            if client is None or client._closed:
                self._pool.pop(key, None)
                self._stats["misses"] += 1
                return None
            self._stats["hits"] += 1
            return client

    def put(self, client: H2Client) -> bool:
        """放入池。如果池中已有可用连接，不替换，返回 False（调用方应 close 该连接）。

        不 close 旧连接，避免正在被其他线程使用的连接被意外关闭。
        """
        key = (client.host, client.port, client.scheme)
        with self._lock:
            existing = self._pool.get(key)
            if existing is not None and not existing._closed:
                return False  # 池中已有可用连接，不替换
            self._pool[key] = client
            self._stats["created"] += 1
            return True

    def remove(self, host: str, port: int, scheme: str):
        """从池中移除并关闭连接（出错时调用）。"""
        key = (host, port, scheme)
        with self._lock:
            client = self._pool.pop(key, None)
        if client:
            client.close()
            self._stats["closed_error"] += 1

    def record_request(self):
        """记录一次 h2 请求（统计用）。"""
        self._stats["requests"] += 1

    def stats(self) -> dict:
        """返回统计信息快照。"""
        with self._lock:
            return {
                **self._stats,
                "pool_size": len(self._pool),
                "active_streams": sum(
                    len(c._streams) for c in self._pool.values() if not c._closed
                ),
            }

    def _cleanup_loop(self):
        """后台线程：定期清理空闲过期的 h2 连接。"""
        while not self._cleanup_stop.wait(self._CLEANUP_INTERVAL):
            try:
                self._cleanup_once()
            except Exception:  # noqa: BLE001
                pass

    def _cleanup_once(self):
        """执行一次清理。"""
        to_close: list[H2Client] = []
        with self._lock:
            for key, client in list(self._pool.items()):
                if client.is_expired():
                    self._pool.pop(key, None)
                    to_close.append(client)
        for client in to_close:
            client.close()
            self._stats["closed_idle"] += 1

    def close_all(self):
        self._cleanup_stop.set()
        with self._lock:
            for client in self._pool.values():
                client.close()
            self._pool.clear()


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
    """在已建立的 h2 连接上发送单个请求（不复用）。推荐使用 H2Client.request()。

    返回 (status, resp_headers, resp_trailers, resp_body)。
    """
    client = H2Client(sock, host, port=0, scheme=scheme)
    try:
        return client.request(method, scheme, host, path, headers, body, timeout)
    finally:
        client.close()


def get_alpn_protocol(sock: socket.socket) -> str:
    """获取 TLS 连接协商的 ALPN 协议。返回 'h2' / 'http/1.1' / ''。"""
    try:
        proto = sock.selected_alpn_protocol()
        return proto or ""
    except (AttributeError, OSError):
        return ""

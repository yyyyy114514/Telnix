"""HTTP/2 转发模块（第二阶段：连接池复用 + stream 多路复用）。

H2Client 封装一个到目标服务器的 h2 连接，内部有 reader 线程持续读取帧
并按 stream_id 分发到对应请求。多个代理线程可同时调用 request()，
共享同一个 TCP+TLS 连接，充分发挥 h2 多路复用优势。

H2ClientPool 按 (host, port, scheme) 缓存 H2Client，每个 key 最多 3 个
（多连接分散单锁瓶颈，提升并发吞吐）。
"""

from __future__ import annotations

import logging
import socket
import threading
import time

logger = logging.getLogger("telnix.h2")


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

    _MAX_IDLE = 180.0  # 空闲超时（秒），超过后可被池清理（原 60s 过短导致频繁重握手）

    def __init__(self, sock: socket.socket, host: str, port: int, scheme: str,
                 cert_info: str = ""):
        import h2.connection
        import h2.config

        self.sock = sock
        self.host = host
        self.port = port
        self.scheme = scheme
        # 缓存 TLS 证书信息，复用连接时返回给调用方记录
        self.cert_info = cert_info

        self._conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(
                client_side=True, header_encoding="utf-8"
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
        """线程安全的 socket sendall。所有 sock.sendall 必须经过此处。"""
        with self._send_lock:
            if self._closed:
                raise OSError("h2 connection closed")
            self.sock.sendall(data)

    def _flush_locked(self):
        """发送 conn 中待发的数据。调用方需持有 _lock。"""
        out = self._conn.data_to_send()
        if out:
            # 通过 _sendall 串行化，避免与 reader 线程的 sendall 竞态
            self._sendall(out)

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

        except Exception as e:  # noqa: BLE001
            # 记录异常便于诊断，避免 reader 静默死亡后调用方等到 30s 超时
            logger.warning("h2-reader", f"h2 reader 线程异常退出: {type(e).__name__}: {e}")
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
        """发送 body，处理 flow control（窗口不足时等待 WindowUpdated）。

        性能优化：
        - chunk_size 用对端协商的 max_outbound_frame_size（可达 64KB），不再硬编码 16KB
        - sendall 移到锁外（仅持 _send_lock），reader 线程可并发 receive_data
        - flow_ready.clear() 在锁内 except 分支执行，避免 lost wakeup 竞态
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
                self._sendall(out)
        except OSError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug("h2-close", f"close 异常: {e}")
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

    每个 key 最多 _MAX_PER_KEY 个 H2Client（多连接分散单锁瓶颈，
    提升并发吞吐；h2 多路复用下 1 个连接理论够用，但单锁会成为瓶颈）。
    内置后台清理线程，定期关闭空闲过期的连接。
    """

    _CLEANUP_INTERVAL = 30.0  # 清理检查间隔（秒）
    _MAX_PER_KEY = 8  # 每个 key 最多缓存的连接数（3→8，降低高并发锁争用）

    def __init__(self):
        # value 改为 list，支持每 key 多个 client
        self._pool: dict[tuple, list[H2Client]] = {}
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
        """获取可用的 H2Client。没有或已关闭返回 None。

        策略：优先返回活跃 stream 最少的 client（负载均衡）。
        """
        key = (host, port, scheme)
        with self._lock:
            clients = self._pool.get(key)
            if not clients:
                self._stats["misses"] += 1
                return None
            # 过滤已关闭的，挑选活跃 stream 最少的
            alive = [c for c in clients if not c._closed]
            if not alive:
                self._pool.pop(key, None)
                self._stats["misses"] += 1
                return None
            # 选活跃 stream 最少的（负载均衡，减少单锁争用）
            best = min(alive, key=lambda c: len(c._streams))
            self._stats["hits"] += 1
            return best

    def put(self, client: H2Client) -> bool:
        """放入池。如果池中连接数已达上限，不放入，返回 False（调用方应 close 该连接）。"""
        key = (client.host, client.port, client.scheme)
        with self._lock:
            clients = self._pool.setdefault(key, [])
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
        """从池中移除并关闭所有该 key 的连接（出错时调用）。"""
        key = (host, port, scheme)
        with self._lock:
            clients = self._pool.pop(key, None)
        if clients:
            for client in clients:
                client.close()
            self._stats["closed_error"] += len(clients)

    def remove_client(self, client: H2Client):
        """只从池中移除并关闭单个连接（不影响该 host 的其他连接）。

        性能优化：单个 stream 超时/抖动不应清空整个 host 的连接池（最多 8 个），
        否则后续并发请求都要重新 TLS 握手（多 1-2 RTT × N）。
        """
        key = (client.host, client.port, client.scheme)
        with self._lock:
            clients = self._pool.get(key)
            if clients:
                # 原地过滤掉指定 client（用 is 判断身份）
                clients[:] = [c for c in clients if c is not client]
                if not clients:
                    self._pool.pop(key, None)
        client.close()
        self._stats["closed_error"] += 1

    def record_request(self):
        """记录一次 h2 请求（统计用）。"""
        self._stats["requests"] += 1

    def stats(self) -> dict:
        """返回统计信息快照。"""
        with self._lock:
            total_clients = sum(len(v) for v in self._pool.values())
            active_streams = sum(
                len(c._streams) for v in self._pool.values() for c in v if not c._closed
            )
            return {
                **self._stats,
                "pool_size": total_clients,
                "active_streams": active_streams,
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
            for key, clients in list(self._pool.items()):
                alive = []
                for client in clients:
                    if client.is_expired():
                        to_close.append(client)
                    else:
                        alive.append(client)
                if alive:
                    self._pool[key] = alive
                else:
                    self._pool.pop(key, None)
        for client in to_close:
            client.close()
            self._stats["closed_idle"] += 1

    def close_all(self):
        self._cleanup_stop.set()
        with self._lock:
            for clients in self._pool.values():
                for client in clients:
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

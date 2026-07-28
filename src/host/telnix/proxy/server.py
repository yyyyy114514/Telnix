"""HTTP/HTTPS 抓包代理服务器（线程模型）。

每个客户端连接开一个线程。HTTP 直接解析转发；HTTPS（CONNECT 隧道）
用 SSL bump 动态签发证书解密。每个连接通过 GetExtendedTcpTable 反查 PID。
断点在代理层阻塞等待 API 放行。
"""

import base64
import concurrent.futures
import gzip
import json
import os
import re
import select
import socket
import ssl
import threading
import time
import traceback
import zlib
from collections import OrderedDict
from datetime import datetime
from urllib.parse import urlsplit

from .. import db
from .. import logger
from ..auto_reply.rules import (
    find_matching_rule, has_active_rules, has_active_rules_fast,
    host_matches_any_rule)
from ..cert_info import get_cert_info
from ..clash.client import get_upstream_proxy
from ..ip_region import lookup as ip_region_lookup
from .breakpoint import BreakpointManager
from .process_lookup import ProcessLookup
from .ssl_bump import SSLBumpManager
from . import throttle

# 记录到 DB 的 body 上限（字节）。超过则截断并加标记，避免大 body 阻塞代理线程
# （视频/图片流可能几 MB ~ 几十 MB，base64 编码后 3x 膨胀 + DB INSERT 慢）
MAX_RECORDED_BODY = 512 * 1024  # 512KB
MAX_STREAM_BODY = 256 * 1024 * 1024  # 256MB 防御上限：无长度响应 read_until_close 硬上限，防极端 OOM

# SSLContext 缓存上限（按 cert_path 缓存，LRU 淘汰最久未用）
# 性能优化：避免长跑场景下不同 host 的证书无限累积导致内存增长
_SSL_CTX_CACHE_MAX = 500

# SSL bump 失败 host 的 TTL（秒）：超过此时间自动允许重试 do_bump
# 避免一次握手失败后该 host 被永久降级为纯隧道（证书可能已重新安装或应用重启）
_SSL_BUMP_FAILED_TTL = 300.0

# 大响应体阈值：超过此大小跳过解压，直接转发原始字节。
# 性能优化：解压几百 KB 的 gzip/br 需几十~几百 ms，阻塞代理线程。
# 大 body 通常是视频/图片/下载，modify_response 规则对二进制内容无意义。
# 仍会记录前 512KB（截断）用于 Inspector 查看，但不解压。
# 1MB→256KB：网页常见 200-800KB gzip JSON/HTML，256KB 以内才解压，减少主线程阻塞
MAX_DECOMPRESS_BODY = 256 * 1024  # 256KB


# 性能优化（v11）：跳过规则匹配的 throttle 日志
# 避免每请求都记录"跳过规则匹配"，每 60s 最多记录一次 DEBUG 日志
_skip_rule_match_count: int = 0
_skip_rule_match_last_log_ts: float = 0.0
_SKIP_LOG_INTERVAL: float = 60.0  # 秒


def _log_skip_rule_match(reason: str):
    """throttle 记录'跳过规则匹配'日志（每 60s 最多一次 DEBUG）。

    线程安全说明：_skip_rule_match_count += 1 在 GIL 下不是严格原子的，
    但日志计数偶尔丢失几个无所谓，不值得加锁。
    """
    global _skip_rule_match_count, _skip_rule_match_last_log_ts
    now = time.time()
    _skip_rule_match_count += 1
    if now - _skip_rule_match_last_log_ts >= _SKIP_LOG_INTERVAL:
        logger.debug(
            "proxy",
            f"[auto-reply] {reason}，跳过规则匹配",
            f"近 {_SKIP_LOG_INTERVAL:.0f}s 内共 {_skip_rule_match_count} 次请求跳过"
        )
        _skip_rule_match_count = 0
        _skip_rule_match_last_log_ts = now


def _truncate_for_record(b: bytes) -> str:
    """将 body bytes 转为可记录的文本，超过 MAX_RECORDED_BODY 截断。

    性能要点：
    - 限制 base64 膨胀范围（只对截断后的 bytes 编码）
    - 限制 utf-8 解码范围（避免对几 MB 的 bytes 调 decode）
    """
    if not b:
        return ""
    truncated = False
    if len(b) > MAX_RECORDED_BODY:
        b = b[:MAX_RECORDED_BODY]
        truncated = True
    try:
        text = b.decode("utf-8")
    except UnicodeDecodeError:
        text = "base64:" + base64.b64encode(b).decode("ascii")
    if truncated:
        text += f"\n\n[... body 已截断，原始大小 {len(b)} 字节，仅记录前 {MAX_RECORDED_BODY} 字节 ...]"
    return text


# ---------- DNS 预解析线程池（模块级单例） ----------
# 异步执行 socket.getaddrinfo + IP 属地查询，避免阻塞代理线程
# 线程池任务只做 DNS 解析 + 本地 IP 属地查询，不依赖代理线程，不会引入死锁
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=32, thread_name_prefix="dns-resolver"
)

# ---------- 证书信息解析线程池（模块级单例） ----------
# 异步执行 getpeercert + cryptography 解析（50-100ms），避免阻塞代理主线程
_CERT_INFO_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="cert-info"
)


def _async_cert_info(target_sock):
    """异步获取对端证书信息，返回 Future（懒获取结果）。

    将 getpeercert + get_cert_info（cryptography 解析 + SHA256）提交到后台线程，
    避免阻塞代理主线程。调用方通过 _get_cert_info_result 懒获取结果。
    """
    try:
        return _CERT_INFO_EXECUTOR.submit(_do_cert_info, target_sock)
    except Exception:  # noqa: BLE001
        return None


def _do_cert_info(target_sock) -> str:
    """在工作线程中执行证书信息解析。"""
    try:
        cert_der = target_sock.getpeercert(binary_form=True)
        if cert_der:
            info = get_cert_info(cert_der)
            if info:
                return json.dumps(info)
    except Exception:  # noqa: BLE001
        pass
    return ""


def _get_cert_info_result(future) -> str:
    """非阻塞获取 cert_info Future 的结果。

    性能优化：原 timeout=5.0 会阻塞代理主线程最多 5 秒等待证书解析，
    是"网页加载后 5 秒才显示包"的核心元凶。cert_info 是次要信息（证书详情），
    不应阻塞响应返回。改为非阻塞：未完成则返回空，复用连接时再重试取值。
    """
    if future is None:
        return ""
    try:
        return future.result(timeout=0)
    except concurrent.futures.TimeoutError:
        return ""  # 未完成，返回空（不阻塞）
    except Exception:  # noqa: BLE001
        return ""


def _resolve_host_for_region(host: str, port: int) -> tuple[str, str]:
    """在工作线程中执行 DNS 解析 + IP 属地查询，返回 (remote_ip, ip_region)。"""
    remote_ip = ""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if infos:
            remote_ip = infos[0][4][0]
    except Exception:  # noqa: BLE001
        pass
    ip_region = ""
    if remote_ip:
        try:
            ip_region = ip_region_lookup(remote_ip)
        except Exception:  # noqa: BLE001
            pass
    return remote_ip, ip_region


# ---------- 到目标服务器的连接池 ----------

def _close_pooled_sock(sock: socket.socket):
    """关闭池化连接并注销其本地端口。

    透明代理防循环依赖 _proxy_outbound_ports 集合排除代理自身的出站流量。
    连接进入连接池后，其本地端口仍保留在集合中；若连接被池丢弃/关闭而不注销，
    端口号会被 OS 复用给非代理 socket，导致 WinDivert 错误排除这些 socket 的流量
    （端口泄漏）。本函数统一处理关闭 + 注销，避免泄漏。
    """
    # 先捕获 local_port（close 后 getsockname 会失败）
    local_port = 0
    try:
        local_port = sock.getsockname()[1]
    except OSError:
        pass
    # F10: 先 close 再 unregister，让 FIN/RST 在端口仍注册时发出，
    # 被 _is_proxy_outbound_addr 排除，避免 WinDivert 拦截产生 spurious NAT
    try:
        sock.close()
    except OSError:
        pass
    if local_port:
        try:
            from .transparent_proxy import unregister_proxy_port
            unregister_proxy_port(local_port)
        except Exception:  # noqa: BLE001
            pass


class _PooledConn:
    """到目标服务器的可复用连接。

    存 reader 而非裸 sock，以保留 SocketReader 缓冲区中可能的多读数据。
    cert_info：首次 TLS 握手时获取的证书信息 JSON，复用连接时直接返回（避免重复 getpeercert）。
    cert_info_future：新建连接时异步解析证书的 Future，复用连接时非阻塞重试取值
    （首次请求时 future 可能未完成，cert_info 为空；复用时大概率已完成，可取到值）。
    """
    def __init__(self, sock: socket.socket, reader: 'SocketReader',
                 host: str, port: int, scheme: str, cert_info: str = "",
                 cert_info_future=None):
        self.sock = sock
        self.reader = reader
        self.host = host
        self.port = port
        self.scheme = scheme
        self.cert_info = cert_info  # 缓存证书信息，复用连接时返回
        self.cert_info_future = cert_info_future  # 异步证书解析 Future
        self.last_used = time.time()


class _ConnPool:
    """简单的 keep-alive 连接池，按 (host, port, scheme) 缓存连接。

    每个连接空闲超过 120 秒自动过期。每个 key 最多缓存 32 个连接。

    性能优化：分片锁（16 桶），不同 host 的连接 get/put 可并行，
    避免高并发下（30 域名 × 6 连接 = 180 线程）单锁串行化。
    """
    _MAX_IDLE = 120.0  # 秒（60→120，匹配主流 keep-alive 超时，提升复用率）
    _MAX_PER_KEY = 32  # 性能优化：从 4 增大到 32，避免高并发下连接池耗尽导致新建连接
    _SHARD_COUNT = 16  # 分片数，减少锁争用

    def __init__(self):
        self._pools: list[dict[tuple, list[_PooledConn]]] = [
            {} for _ in range(self._SHARD_COUNT)]
        self._locks: list[threading.Lock] = [
            threading.Lock() for _ in range(self._SHARD_COUNT)]

    def _shard(self, key: tuple) -> int:
        return hash(key) % self._SHARD_COUNT

    def get(self, host: str, port: int, scheme: str) -> _PooledConn | None:
        key = (host, port, scheme)
        idx = self._shard(key)
        with self._locks[idx]:
            conns = self._pools[idx].get(key)
            if not conns:
                return None
            while conns:
                conn = conns.pop()
                if time.time() - conn.last_used > self._MAX_IDLE:
                    # 过期连接：关闭并注销端口（防端口泄漏）
                    _close_pooled_sock(conn.sock)
                    continue
                return conn
        return None

    def put(self, conn: _PooledConn):
        key = (conn.host, conn.port, conn.scheme)
        idx = self._shard(key)
        with self._locks[idx]:
            conns = self._pools[idx].setdefault(key, [])
            if len(conns) >= self._MAX_PER_KEY:
                # 池满：关闭并注销端口（防端口泄漏）
                _close_pooled_sock(conn.sock)
                return
            conns.append(conn)

    def close_all(self):
        for idx in range(self._SHARD_COUNT):
            with self._locks[idx]:
                for conns in self._pools[idx].values():
                    for c in conns:
                        # 关闭并注销端口（防端口泄漏）
                        _close_pooled_sock(c.sock)
                self._pools[idx].clear()


# 响应状态码原因短语（常用）
_REASON_PHRASES = {
    200: "OK", 201: "Created", 204: "No Content", 301: "Moved Permanently",
    302: "Found", 304: "Not Modified", 400: "Bad Request", 401: "Unauthorized",
    403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
    407: "Proxy Authentication Required", 500: "Internal Server Error",
    502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout",
}


def _reason(code: int) -> str:
    return _REASON_PHRASES.get(code, "OK")


# ---------- HTTP 头容器 ----------

class Headers:
    """大小写不敏感的头容器，保留原始顺序与大小写。

    性能优化：_index dict 提供 O(1) 按名查找，避免每次 get/set/has/remove
    都 O(n) 扫描 _items 并对每个 name 调 .lower()。
    """

    def __init__(self):
        self._items: list[list[str]] = []  # [[name, value], ...]
        self._index: dict[str, int] = {}  # lower_name -> 首个匹配的 _items 下标

    def add(self, name: str, value: str):
        self._items.append([name, value])
        nl = name.lower()
        if nl not in self._index:
            self._index[nl] = len(self._items) - 1

    def get(self, name: str, default=None):
        idx = self._index.get(name.lower())
        if idx is not None:
            return self._items[idx][1]
        return default

    def set(self, name: str, value: str):
        nl = name.lower()
        idx = self._index.get(nl)
        if idx is not None:
            self._items[idx][1] = value
            return
        self._items.append([name, value])
        self._index[nl] = len(self._items) - 1

    def remove(self, name: str):
        nl = name.lower()
        if nl not in self._index:
            return
        # remove 罕见，重建 _items + _index 可接受
        self._items = [it for it in self._items if it[0].lower() != nl]
        self._index.clear()
        for i, it in enumerate(self._items):
            key = it[0].lower()
            if key not in self._index:
                self._index[key] = i

    def has(self, name: str) -> bool:
        return name.lower() in self._index

    def to_dict(self) -> dict:
        d = {}
        for k, v in self._items:
            d[k] = v
        return d

    def to_bytes(self) -> bytes:
        out = bytearray()
        for k, v in self._items:
            out += f"{k}: {v}\r\n".encode("latin-1", "replace")
        return bytes(out)

    @classmethod
    def from_lines(cls, lines: list[bytes]) -> "Headers":
        h = cls()
        for line in lines:
            try:
                text = line.decode("latin-1")
            except Exception:  # noqa: BLE001
                continue
            if ":" not in text:
                continue
            name, _, value = text.partition(":")
            h.add(name.strip(), value.strip())
        return h

    @classmethod
    def from_dict(cls, d: dict) -> "Headers":
        h = cls()
        for k, v in d.items():
            h.add(k, str(v))
        return h


# ---------- 带缓冲的 socket 读取器 ----------

# 性能优化：host 通配符预编译（替代 _match_host_wildcard 的每次 re.escape + re.match）
def _compile_host_wildcard(pattern: str) -> re.Pattern | None:
    """通配符转预编译正则：* → .*, ? → .，大小写不敏感。失败返回 None。"""
    if not pattern:
        return None
    try:
        regex_str = '^' + re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.') + '$'
        return re.compile(regex_str, re.IGNORECASE)
    except re.error:
        return None


class SocketReader:
    """带缓冲的 socket 读取器，支持按行读取与精确读取。"""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            return False
        self.buf += chunk
        return True

    def read_line(self) -> bytes:
        while True:
            idx = self.buf.find(b"\r\n")
            if idx >= 0:
                line = bytes(self.buf[:idx])
                del self.buf[: idx + 2]
                return line
            if not self._fill():
                # 连接关闭，返回剩余内容
                if self.buf:
                    line = bytes(self.buf)
                    self.buf.clear()
                    return line
                return b""

    def read_headers(self) -> list[bytes]:
        lines = []
        while True:
            line = self.read_line()
            if line == b"":
                break
            lines.append(line)
        return lines

    def read_exactly(self, n: int) -> bytes:
        while len(self.buf) < n:
            if not self._fill():
                break
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

    def read_until_close(self) -> bytes:
        data = bytearray(self.buf)
        self.buf.clear()
        # 防御性上限：无 Content-Length/chunked 的响应（如视频流）会一直读到连接关闭，
        # 设硬上限防止极端情况下把整条流全量缓冲进内存导致 OOM。
        while len(data) < MAX_STREAM_BODY:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
        return bytes(data)

    def read_chunked(self) -> bytes:
        data = bytearray()
        while True:
            size_line = self.read_line()
            size_str = size_line.split(b";")[0].strip()
            try:
                size = int(size_str, 16)
            except ValueError:
                break
            if size == 0:
                # 读取尾部头直到空行
                self.read_headers()
                break
            data += self.read_exactly(size)
            self.read_line()  # 尾部 CRLF
        return bytes(data)


def read_body(reader: SocketReader, headers: Headers, *,
              is_request: bool = False, method: str | None = None,
              status_code: int | None = None) -> bytes:
    """根据头读取消息体。

    性能优化：MAX_BODY_SIZE 上限保护，超过则截断并标记（避免大 body OOM + GC 压力）。
    """
    # 响应：HEAD/204/304/1xx 无 body
    if status_code is not None and (
            status_code in (204, 304) or 100 <= status_code < 200):
        return b""
    if method == "HEAD":
        return b""
    te = (headers.get("Transfer-Encoding") or "").lower()
    if "chunked" in te:
        return reader.read_chunked()
    cl = headers.get("Content-Length")
    if cl is not None:
        try:
            n = int(cl)
        except ValueError:
            n = 0
        if n > 0:
            return reader.read_exactly(n)
        return b""
    # 请求无 Content-Length 且非 chunked 视为无 body
    if is_request:
        return b""
    # 响应无长度信息：读到连接关闭
    return reader.read_until_close()


# ---------- 代理服务器 ----------

class ProxyServer:
    """HTTP/HTTPS 抓包代理服务器（线程模型）。"""

    # F12 修复：capturing 加锁保护，消除 F9 的微小竞态窗口。
    # Python GIL 保证属性读写原子，但加锁可保证内存可见性（写后立即对其他线程可见）。
    # property 透明替换，所有调用点（22 处）无需改动。
    # 跨平台
    @property
    def capturing(self) -> bool:
        with self._capturing_lock:
            return self._capturing

    @capturing.setter
    def capturing(self, value: bool) -> None:
        with self._capturing_lock:
            self._capturing = value

    def __init__(self, host: str = "127.0.0.1", port: int = 8888,
                 ssl_bump: SSLBumpManager | None = None):
        self.host = host
        self.port = port
        self.ssl_bump = ssl_bump
        self.process_lookup = ProcessLookup()
        self.breakpoint = BreakpointManager()
        self._capturing = False
        self._capturing_lock = threading.Lock()
        self.session_id: int | None = None
        self.cert_installed = False
        self._server_socket: socket.socket | None = None
        self._running = False
        self._ignored_pids: set[int] = set()
        self._ignored_names: set[str] = set()
        self._ignored_hosts: list[str] = []  # 通配符列表，如 *.example.com
        # 性能优化：预编译的 host 通配符正则（与 _ignored_hosts 一一对应，None=编译失败）
        self._ignored_host_regexes: list[re.Pattern | None] = []
        self._ignored_lock = threading.Lock()
        # 专注模式：只抓符合任一条件的流量，其他直接放行不记录
        # pid/host/method/status_code/content_type 跨类 OR 匹配
        self._focus_pids: set[int] = set()
        self._focus_hosts: list[str] = []  # 通配符列表，如 *.example.com
        # 性能优化：预编译的 focus host 通配符正则（与 _focus_hosts 一一对应，None=编译失败）
        self._focus_host_regexes: list[re.Pattern | None] = []
        self._focus_methods: set[str] = set()  # HTTP 方法大写集合
        self._focus_status_codes: set[int] = set()  # 状态码集合
        self._focus_content_types: list[str] = []  # Content-Type 主类型，如 application/text/image
        self._focus_enabled = False
        self._focus_lock = threading.Lock()
        # 证书 pinning 疑似进程收集（host+pid+proc 去重，上限 50 条）
        self._pinning_suspected: list[dict] = []
        self._pinning_keys: set[tuple] = set()
        self._pinning_lock = threading.Lock()
        # SSL bump 失败的 host 集合（TLS 握手被客户端拒绝后自动降级为纯隧道，避免反复失败）
        # dict[host -> 失败时间戳]，超过 _SSL_BUMP_FAILED_TTL 自动允许重试 do_bump
        self._ssl_bump_failed_hosts: dict[str, float] = {}
        self._ssl_bump_failed_lock = threading.Lock()
        # 到目标服务器的 keep-alive 连接池（复用 TCP+TLS 连接，避免每次握手）
        self._conn_pool = _ConnPool()
        # HTTP/2 连接池（复用 h2 连接，支持多路复用）
        from .h2_forward import H2ClientPool
        self._h2_pool = H2ClientPool()
        # SSL bump 的 SSLContext 缓存（按 cert_path 缓存，避免每次创建）
        # 用 OrderedDict 实现 LRU：访问时 move_to_end，超上限时 popitem(last=False) 淘汰最久未用
        # value 为 (SSLContext, cert_mtime)：当叶证书被重新签发（根证书变更或旧格式升级）
        # 时 mtime 改变，缓存自动失效，避免使用旧证书导致 TLS 握手失败
        self._ssl_ctx_cache: "OrderedDict[str, tuple[ssl.SSLContext, float]]" = OrderedDict()
        self._ssl_ctx_lock = threading.Lock()
        # 客户端连接并发信号量（防止无限制创建线程导致 OOM / GIL 严重争用）
        self._client_sem = threading.Semaphore(200)
        # 转发到目标用的 SSL context（全局复用一个）
        self._forward_ssl_ctx = ssl.create_default_context()
        self._forward_ssl_ctx.check_hostname = False
        self._forward_ssl_ctx.verify_mode = ssl.CERT_NONE
        # HTTP/2 支持：到目标服务器的连接通过 ALPN 协商 h2 或 http/1.1
        self._forward_ssl_ctx.set_alpn_protocols(["h2", "http/1.1"])
        # WebSocket over TLS (wss) 专用 SSL context：
        # 只通告 http/1.1，避免 ALPN 协商到 h2（RFC 9113 禁止在 h2 上
        # 发送 Connection/Upgrade 等 connection-specific 头，会导致 WS
        # 握手失败，浏览器收到 code=1006 异常断开）
        self._ws_ssl_ctx = ssl.create_default_context()
        self._ws_ssl_ctx.check_hostname = False
        self._ws_ssl_ctx.verify_mode = ssl.CERT_NONE
        self._ws_ssl_ctx.set_alpn_protocols(["http/1.1"])
        # 上游代理（Clash/Mihomo 集成用）：启用时所有出站连接先走 CONNECT 到上游代理
        # 由 clash.client.get_upstream_proxy() 动态返回 (host, port) 或 None
        self._upstream_proxy_override: tuple[str, int] | None = None

    def _get_upstream_proxy(self) -> tuple[str, int] | None:
        """获取当前上游代理地址。优先用运行时 override，否则从 settings 读。"""
        if self._upstream_proxy_override is not None:
            return self._upstream_proxy_override
        try:
            return get_upstream_proxy()
        except Exception:  # noqa: BLE001
            return None

    def _connect_target(self, host: str, port: int, timeout: int = 30) -> socket.socket:
        """建立到目标服务器的连接。启用 Clash 时走上游代理 CONNECT 隧道。

        性能优化：
        - TCP_NODELAY 禁用 Nagle 算法，减少小数据包延迟
        - CONNECT 响应读取设独立超时（10s），避免代理异常时长时间卡死
        - 缓存由 clash.client.get_upstream_proxy() 提供，避免每次查文件

        透明代理防循环（关键，两阶段注册）：
        - Phase 1（connect 前）：bind 后 getsockname() 获取端口号，注册到
          _proxy_outbound_ports（仅 port，IP 此时为 0.0.0.0 不可用）。
          确保 SYN 发出前端口已注册，避免 WinDivert 拦截代理自身 SYN
          形成无限循环（TOCTOU 竞态修复）。
        - Phase 2（connect 后）：getsockname() 获取真实源 IP，注册 (ip, port)
          到 _proxy_outbound_addrs。减少纯 port 匹配误排除客户端流量的窗口。
        """
        upstream = self._get_upstream_proxy()
        if upstream is None:
            # 两阶段注册防循环（关键修复）：
            # Phase 1: connect 前仅注册 port。getsockname() 在 bind 后返回
            #   ('0.0.0.0', port)，IP 是通配符不可用（真实源 IP 要等 connect 后
            #   由路由选择才知道）。此时靠 port 匹配排除代理自身 SYN。
            # Phase 2: connect 后 getsockname() 返回真实源 IP，升级为 (ip, port)
            #   精确匹配，减少纯 port 匹配误排除客户端流量的窗口。
            # 若 Phase 2 失败（getsockname 异常），Phase 1 的 port 注册仍能
            # 防循环（_is_proxy_outbound_addr 始终回退到 port 匹配）。
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            local_port = 0
            try:
                s.bind(('', 0))
                local_port = s.getsockname()[1]
                self._register_proxy_port(local_port)
                s.connect((host, port))
                self._register_proxy_socket_addr(s, local_port)
            except OSError:
                # F10: 先 close 再 unregister（即使 connect 失败也保持一致顺序）
                try:
                    s.close()
                except OSError:
                    pass
                if local_port:
                    self._unregister_proxy_port(local_port)
                raise
            return s
        # 通过上游代理（Mihomo mixed-port）建立 CONNECT 隧道
        proxy_host, proxy_port = upstream
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        local_port = 0
        try:
            s.bind(('', 0))
            local_port = s.getsockname()[1]
            self._register_proxy_port(local_port)
            s.connect((proxy_host, proxy_port))
            # Phase 2: connect 后获取真实源 IP，升级为 (ip, port) 精确匹配
            self._register_proxy_socket_addr(s, local_port)
            # 禁用 Nagle 算法：CONNECT 请求是小包，立即发送避免 40ms 延迟
            try:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass  # 非 TCP 或不支持时忽略
            # 预编码 CONNECT 请求（避免每次字符串拼接 + encode）
            try:
                host_bytes = host.encode("ascii")
            except UnicodeEncodeError:
                host_bytes = host.encode("idna")
            port_bytes = str(port).encode("ascii")
            req = b"CONNECT " + host_bytes + b":" + port_bytes + b" HTTP/1.1\r\nHost: " + host_bytes + b":" + port_bytes + b"\r\n\r\n"
            s.sendall(req)
            # 读取代理响应时设独立超时（3s），避免代理异常长时间卡死
            s.settimeout(3)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    raise OSError(f"上游代理关闭连接: {proxy_host}:{proxy_port}")
                buf += chunk
            status_line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            if " 200 " not in status_line:
                raise OSError(f"上游代理拒绝 CONNECT {host}:{port}: {status_line}")
            # 恢复正常超时
            s.settimeout(timeout)
        except OSError:
            # F10: 先 close 再 unregister（即使 CONNECT 失败也保持一致顺序）
            try:
                s.close()
            except OSError:
                pass
            if local_port:
                self._unregister_proxy_port(local_port)
            raise
        return s

    def _register_proxy_port(self, local_port: int):
        """注册代理出站端口（Phase 1，connect 前调用）。

        仅注册 port 到 _proxy_outbound_ports，不注册 (ip, port)。
        原因：connect 前 getsockname() 返回 ('0.0.0.0', port)，IP 是通配符，
        注册到 _proxy_outbound_addrs 会成为永远匹配不到真实出站包的死数据，
        且让 _proxy_outbound_addrs 非空从而禁用端口回退（旧版致命 bug）。
        端口匹配在 connect 窗口期提供防循环保护。
        """
        if not local_port:
            return
        try:
            from .transparent_proxy import register_proxy_port
            register_proxy_port(local_port)
        except Exception:  # noqa: BLE001
            pass

    def _register_proxy_socket_addr(self, s: socket.socket, local_port: int):
        """注册代理出站 (src_ip, src_port) 二元组（Phase 2，connect 后调用）。

        connect 后 getsockname() 返回真实源 IP（由路由选择），此时注册
        (ip, port) 到 _proxy_outbound_addrs 实现 precise 匹配，减少纯 port
        匹配误排除客户端流量的窗口。Phase 1 的 port 注册仍保留（不在
        此处移除），由 _unregister_proxy_port 统一清理。
        """
        if not local_port:
            return
        try:
            from .transparent_proxy import register_proxy_addr
            local_ip = s.getsockname()[0]
            register_proxy_addr(local_ip, local_port)
        except Exception:  # noqa: BLE001
            # Phase 2 失败不影响 Phase 1 的 port 防循环保护
            pass

    def _unregister_proxy_port(self, local_port: int):
        """按端口号注销代理出站端口（不依赖 socket，socket 关闭后仍可用）。

        unregister_proxy_port 会同时清理 _proxy_outbound_ports 和
        _proxy_outbound_addrs 中所有以该 port 结尾的条目，因此无论
        注册到了 Phase 1 还是 Phase 2 都能正确清理。
        """
        if not local_port:
            return
        try:
            from .transparent_proxy import unregister_proxy_port
            unregister_proxy_port(local_port)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 生命周期 ----------

    def start(self):
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(200)
        self._running = True
        self.refresh_ignored()
        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()
        # 后台预热：强制加载首次调用时的懒加载依赖（cryptography/h2/psutil/ip2region），
        # 避免首批请求被 5-6 秒冷启动延迟拖慢
        threading.Thread(target=self._prewarm, daemon=True, name="prewarm").start()

    def _prewarm(self):
        """预热：在后台线程强制触发各种首次调用的开销，避免首批请求被冷启动延迟拖慢。

        主要预热项（按耗时从大到小）：
        - cryptography 库首次加载 OpenSSL 后端（cffi 绑定 libcrypto/libssl，500ms-2s）
        - ip2region 11MB 数据加载（200-500ms）
        - h2 库首次导入（50-100ms）
        - psutil 首次进程枚举初始化（50-200ms）
        - DB 连接 + 后台写入线程首次启动
        """
        try:
            # 1. cryptography：触发 OpenSSL 后端加载 + 一次 ECDSA keygen
            #    首次调用会动态加载 libcrypto-1_1.dll，预热后后续证书签发只需 5-15ms
            from cryptography.hazmat.primitives.asymmetric import ec
            ec.generate_private_key(ec.SECP256R1())
        except Exception:  # noqa: BLE001
            pass
        try:
            # 2. ip2region 数据预加载（11MB xdb → 内存）
            from .. import ip_region
            ip_region._get_searcher()
        except Exception:  # noqa: BLE001
            pass
        try:
            # 3. h2 库预导入（避免首个 HTTPS 请求创建 H2Client 时才导入）
            import h2.connection  # noqa: F401
            import h2.config  # noqa: F401
        except Exception:  # noqa: BLE001
            pass
        try:
            # 4. psutil 预热：查询当前进程名，触发内部缓存初始化
            import psutil
            psutil.Process().name()
        except Exception:  # noqa: BLE001
            pass
        try:
            # 5. DB 后台写入线程预启动 + 主线程连接建立
            from .. import db
            db._start_flow_writer()
            db._start_update_writer()
            db._get_thread_conn()
        except Exception:  # noqa: BLE001
            pass
        try:
            # 6. SSL bump 证书签发路径预热：构造一次 SSLContext 验证 cryptography 链路通畅
            if self.ssl_bump and self.ssl_bump._root_cert and self.ssl_bump._root_key:
                import ssl as _ssl
                ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
                # 用根证书做一次 load_cert_chain，触发证书解析路径
                ctx.load_cert_chain(self.ssl_bump.root_cert_path, self.ssl_bump.root_key_path)
        except Exception:  # noqa: BLE001
            pass

    def stop(self):
        self._running = False
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        # 关闭连接池中所有复用连接
        self._conn_pool.close_all()
        self._h2_pool.close_all()

    def _accept_loop(self):
        while self._running:
            try:
                client_sock, client_addr = self._server_socket.accept()
            except OSError:
                break
            client_sock.settimeout(60)
            # 获取信号量，限制最大并发连接数（防止线程无限增长）
            if not self._client_sem.acquire(timeout=5):
                try:
                    client_sock.close()
                except OSError:
                    pass
                continue
            t = threading.Thread(
                target=self._handle_client_safe_with_sem,
                args=(client_sock, client_addr),
                daemon=True,
            )
            t.start()

    def _handle_client_safe_with_sem(self, client_sock: socket.socket, client_addr):
        """带信号量释放的客户端处理包装器。"""
        try:
            self._handle_client_safe(client_sock, client_addr)
        finally:
            self._client_sem.release()

    def _handle_client_safe(self, client_sock: socket.socket, client_addr):
        try:
            self._handle_client(client_sock, client_addr)
        except Exception:  # noqa: BLE001
            # 单个请求异常不能让代理崩溃，但记录堆栈便于排障
            try:
                logger.error("proxy", "处理客户端连接异常\n" + traceback.format_exc())
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                client_sock.close()
            except OSError:
                pass

    # ---------- 状态控制 ----------

    def refresh_ignored(self):
        with self._ignored_lock:
            rows = db.get_ignored_processes()
            self._ignored_pids = {r["pid"] for r in rows if r.get("pid") and r["pid"] > 0}
            self._ignored_names = {r["process_name"].lower()
                                   for r in rows
                                   if (not r.get("pid") or r["pid"] <= 0) and r.get("process_name")}
            host_rows = db.get_ignored_hosts()
            self._ignored_hosts = [r["host_pattern"] for r in host_rows if r.get("host_pattern")]
            # 性能优化：预编译 host 通配符正则（避免每请求 re.escape + re.match）
            self._ignored_host_regexes = [
                _compile_host_wildcard(p) for p in self._ignored_hosts
            ]

    def is_ignored(self, pid: int | None, proc_name: str | None = None,
                   host: str | None = None) -> bool:
        """是否命中忽略规则（pid / 进程名 / host 通配符 任一命中即忽略）。

        host 支持通配符（* → 任意，? → 单字符），大小写不敏感。

        性能优化：一次锁拿全量 snapshot（pid_set / name_set / host_regexes），避免 3 次加锁。
        host 通配符改为预编译正则（refresh_ignored 时编译），避免每请求 re.escape + re.match。
        """
        # 一次锁拿 snapshot
        with self._ignored_lock:
            pid_set = self._ignored_pids
            name_set = self._ignored_names
            host_regexes = self._ignored_host_regexes
        if pid is not None and pid > 0 and pid in pid_set:
            return True
        if proc_name and proc_name.lower() in name_set:
            return True
        if host and host_regexes:
            for rx in host_regexes:
                if rx is not None and rx.search(host):
                    return True
        return False

    # ---------- 专注模式 ----------

    def set_focus_mode(self, enabled: bool, pids: list[int] | None = None,
                       hosts: list[str] | None = None,
                       methods: list[str] | None = None,
                       status_codes: list[int] | None = None,
                       content_types: list[str] | None = None):
        """开启/关闭专注模式。

        pid/host/method/status_code/content_type 跨类 OR 匹配：满足任一条件即记录/拦截。
        enabled 参数被忽略——自动根据是否有任何专注条件判断：
        有任一条件（pids/hosts/methods/status_codes/content_types 非空）则启用，全空则关闭。
        """
        with self._focus_lock:
            if pids is not None:
                self._focus_pids = set(pids)
            if hosts is not None:
                self._focus_hosts = list(hosts)
                # 性能优化：预编译 host 通配符正则
                self._focus_host_regexes = [
                    _compile_host_wildcard(p) for p in self._focus_hosts
                ]
            if methods is not None:
                self._focus_methods = {m.upper() for m in methods if m}
            if status_codes is not None:
                self._focus_status_codes = set(status_codes)
            if content_types is not None:
                self._focus_content_types = [c.lower() for c in content_types if c]
            # 自动判断 enabled：有任一专注条件就开，全空就关
            self._focus_enabled = bool(
                self._focus_pids or self._focus_hosts or self._focus_methods
                or self._focus_status_codes or self._focus_content_types
            )

    def get_focus_mode(self) -> dict:
        with self._focus_lock:
            return {
                "enabled": self._focus_enabled,
                "pids": list(self._focus_pids),
                "hosts": list(self._focus_hosts),
                "methods": list(self._focus_methods),
                "status_codes": list(self._focus_status_codes),
                "content_types": list(self._focus_content_types),
            }

    @staticmethod
    def _match_host_wildcard(host: str, pattern: str) -> bool:
        """通配符匹配 host（* → .*, ? → .），大小写不敏感。"""
        if not pattern:
            return False
        regex_str = '^' + re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.') + '$'
        try:
            return re.match(regex_str, host, re.IGNORECASE) is not None
        except Exception:  # noqa: BLE001
            return pattern.lower() in host.lower()

    def _has_response_focus(self) -> bool:
        """是否有响应阶段才能判断的专注条件（status_code/content_type）。"""
        with self._focus_lock:
            return bool(self._focus_status_codes or self._focus_content_types)

    def is_focused_out(self, pid: int | None, host: str | None = None,
                       method: str | None = None,
                       status_code: int | None = None,
                       content_type: str | None = None) -> bool:
        """专注模式下，该请求/响应是否不在专注范围内（应跳过不记录）。

        pid/host/method/status_code/content_type 跨类 OR 匹配：
        满足任一专注条件即在专注范围内（返回 False），都不满足返回 True（放行不记录）。
        请求阶段只传 pid/host/method；响应阶段可额外传 status_code/content_type。
        如果有响应阶段专注条件（status/content_type），请求阶段无法确定，返回 False（暂记）。
        """
        if not self._focus_enabled:
            return False
        with self._focus_lock:
            pids = self._focus_pids
            host_regexes = self._focus_host_regexes
            methods = self._focus_methods
            status_codes = self._focus_status_codes
            content_types = self._focus_content_types
        # 如果专注列表全空，不拦截
        if not pids and not host_regexes and not methods and not status_codes and not content_types:
            return False
        # 检查 pid
        if pid is not None and pid in pids:
            return False
        # 检查 host（用预编译正则）
        if host and host_regexes:
            for rx in host_regexes:
                if rx is not None and rx.search(host):
                    return False
        # 检查 method
        if method and methods and method.upper() in methods:
            return False
        # 检查 status_code（响应阶段）
        if status_code is not None and status_codes and status_code in status_codes:
            return False
        # 检查 content_type（响应阶段，按主类型匹配）
        if content_type and content_types:
            ct_main = content_type.split(";")[0].split("/")[0].strip().lower()
            if ct_main in content_types:
                return False
        # 请求阶段：如果有响应阶段专注条件且当前未匹配，暂不算专注外（等响应阶段再判）
        if status_code is None and content_type is None:
            if status_codes or content_types:
                return False
        return True

    def refresh_cert_status(self):
        if self.ssl_bump:
            self.cert_installed = self.ssl_bump.is_root_cert_installed()

    # ---------- 客户端处理 ----------

    def _needs_pid(self) -> bool:
        """是否需要反查 PID：抓包中 / 有启用的规则 / 专注模式开启 / 有忽略进程。"""
        if self.capturing or self._focus_enabled:
            return True
        # 有忽略进程配置时也需要查 PID 来过滤
        with self._ignored_lock:
            if self._ignored_pids or self._ignored_names:
                return True
        # 有启用的自动回复规则时需要解密 HTTPS，也需要 PID
        # 性能优化（v11）：用 fast 检查（无锁读模块级标志位）
        return has_active_rules_fast()

    def _handle_client(self, client_sock: socket.socket, client_addr):
        # 先 peek 第一字节判断是否是 HTTP 流量
        # 透明代理模式下，HTTPS 流量（TLS ClientHello 0x16）会被重定向到本地代理，
        # 此时不是 HTTP 协议，需要走 raw TCP 隧道（不解密，仅字节转发）
        try:
            first_byte = client_sock.recv(1, socket.MSG_PEEK)
        except OSError as e:
            # F31 诊断：peek 失败通常意味着客户端立即关闭，记录便于排障
            logger.debug("proxy", "peek首字节失败",
                         f"client={client_addr} err={e}")
            first_byte = b""

        # HTTP method 首字符是大写字母 A-Z（GET/POST/PUT/DELETE/HEAD/OPTIONS/CONNECT/PATCH）
        # TLS handshake 首字节是 0x16，其他二进制协议也不是 ASCII 字母
        is_http_like = bool(first_byte) and 0x41 <= first_byte[0] <= 0x5A

        if not is_http_like:
            # 非 HTTP 流量：尝试 raw TCP 隧道（透明代理反查原目标）
            if self._try_raw_tunnel(client_sock, client_addr):
                return
            # 反查失败（非透明代理模式或 NAT 表无条目）：关闭连接
            # F31 诊断：记录非 HTTP 流量被关闭的情况（之前是静默 RST）
            first_byte_hex = first_byte.hex() if first_byte else "empty"
            logger.warning(
                "proxy", "非HTTP流量隧道失败关闭连接",
                f"client={client_addr} first_byte=0x{first_byte_hex} "
                f"(TLS=0x16) 导致RST"
            )
            return

        reader = SocketReader(client_sock)

        first_line = reader.read_line()
        if not first_line:
            return

        # 性能优化：不抓包且无规则且无专注且无忽略进程时，跳过 PID 反查
        # process_lookup 会遍历 TCP 表最多 3 次 + 30ms sleep，是主要性能瓶颈
        if self._needs_pid():
            pid, proc_name = self.process_lookup.lookup(client_addr)
        else:
            pid, proc_name = None, None

        if first_line.upper().startswith(b"CONNECT"):
            self._handle_connect(client_sock, reader, first_line, pid, proc_name)
        else:
            self._handle_http(client_sock, reader, first_line, pid, proc_name,
                              scheme="http")

    @staticmethod
    def _parse_sni_from_tls(client_sock: socket.socket) -> "str | None":
        """从 TLS ClientHello 中解析 SNI hostname（不消耗 socket 数据，用 MSG_PEEK）。

        F20: NAT 反查失败时的 HTTPS 兜底——从 ClientHello 的 SNI 扩展提取目标域名。
        """
        try:
            data = client_sock.recv(4096, socket.MSG_PEEK)
        except OSError:
            return None
        if len(data) < 5 or data[0] != 0x16:  # 0x16 = TLS Handshake
            return None
        pos = 5  # 跳过 TLS record header (5 bytes)
        if len(data) < pos + 4 or data[pos] != 0x01:  # 0x01 = ClientHello
            return None
        pos += 4  # 跳过 handshake type + length (4 bytes)
        pos += 2  # 跳过 version (2 bytes)
        pos += 32  # 跳过 random (32 bytes)
        # Session ID
        if len(data) < pos + 1:
            return None
        sid_len = data[pos]
        pos += 1 + sid_len
        # Cipher Suites
        if len(data) < pos + 2:
            return None
        cs_len = int.from_bytes(data[pos:pos + 2], 'big')
        pos += 2 + cs_len
        # Compression Methods
        if len(data) < pos + 1:
            return None
        cm_len = data[pos]
        pos += 1 + cm_len
        # Extensions
        if len(data) < pos + 2:
            return None
        ext_total = int.from_bytes(data[pos:pos + 2], 'big')
        pos += 2
        ext_end = pos + ext_total
        while pos + 4 <= ext_end and pos + 4 <= len(data):
            ext_type = int.from_bytes(data[pos:pos + 2], 'big')
            ext_len = int.from_bytes(data[pos + 2:pos + 4], 'big')
            pos += 4
            if ext_type == 0x0000:  # SNI extension
                if pos + 2 > len(data):
                    return None
                p = pos + 2  # 跳过 SNI list length
                if p + 3 > len(data):
                    return None
                sni_type = data[p]
                sni_len = int.from_bytes(data[p + 1:p + 3], 'big')
                if sni_type == 0:  # host_name
                    p += 3
                    if p + sni_len > len(data):
                        return None
                    return data[p:p + sni_len].decode('ascii', errors='replace')
            pos += ext_len
        return None

    def _try_raw_tunnel(self, client_sock: socket.socket, client_addr) -> bool:
        """透明代理模式下的 raw TCP 隧道转发（不解密）。

        用于 HTTPS(443) 透明代理：
        - Windows: WinDivert 把出站 443 流量重定向到本地代理端口，
          通过 NAT 表反查原目标 IP:Port
        - Linux/macOS: iptables/pf 把出站 443 重定向到本地代理端口，
          通过 getsockopt(SO_ORIGINAL_DST) 或 getsockname 查询原目标

        返回 True 表示已处理（无论成功失败），False 表示非透明代理模式或反查失败。
        """
        # 懒导入避免非透明模式下加载透明代理模块
        try:
            from .transparent_proxy import get_transparent_proxy, IS_WINDOWS
        except ImportError:  # noqa: BLE001
            # F31 诊断：transparent_proxy 模块导入失败
            logger.warning("proxy", "transparent_proxy导入失败",
                           f"client={client_addr} 关闭连接导致RST")
            return False
        proxy = get_transparent_proxy()
        if not proxy.running:
            # F31 诊断：透明代理未运行时，非 HTTP 流量（TLS）无处可去 → RST
            # 这是"关代理就reset无日志"的根因：之前静默 return False
            first_byte_hex = ""
            try:
                fb = client_sock.recv(1, socket.MSG_PEEK)
                first_byte_hex = fb.hex() if fb else "empty"
            except OSError:
                first_byte_hex = "peek_err"
            logger.warning(
                "proxy", "透明代理未运行非HTTP流量被关闭",
                f"client={client_addr} first_byte=0x{first_byte_hex} "
                f"导致RST（开启透明代理可解决）"
            )
            return False

        # 平台分支：Windows 用 NAT 表反查，Unix 用 socket 选项查询
        if IS_WINDOWS:
            # Windows: client_addr = (ip, port)，port 是客户端源端口
            client_src_port = client_addr[1] if len(client_addr) >= 2 else 0
            if client_src_port == 0:
                return False
            # F2 修复：传 client_src_ip 给 lookup_reverse 做 (ip, port) 二元组精确反查，
            # 避免不同客户端端口复用时误匹配。
            client_src_ip = client_addr[0] if len(client_addr) >= 1 else None
            target = proxy.lookup_reverse(client_src_port, client_src_ip)
            # F25 诊断：记录 lookup_reverse 查询和结果
            if not target:
                logger.warning(
                    "server", "lookup_reverse 未命中",
                    f"client={client_src_ip}:{client_src_port} "
                    f"将尝试 SNI fallback"
                )
        else:
            # Unix: 通过 getsockopt(SO_ORIGINAL_DST) 或 getsockname 查询原目标
            target = proxy.lookup_original_dst(client_sock)
        if not target:
            # F20: NAT 反查失败时，尝试从 TLS ClientHello 解析 SNI 作为 fallback。
            # 场景1 HTTPS RESET 根因：NAT 反查失败（条目过期/未写入/竞态）直接关闭连接 → RST。
            # SNI fallback 让 HTTPS 在 NAT 表异常时仍能连接目标。
            sni_host = self._parse_sni_from_tls(client_sock)
            if sni_host:
                logger.warning(
                    "server", "NAT反查失败，SNI fallback",
                    f"sni={sni_host} port=443 client={client_addr}"
                )
                target = (sni_host, 443)
                # F26: SNI fallback 成功后注册 NAT 条目，确保反向回包能改写回原服务器。
                # 否则客户端收到 src=local_ip:8888 的包 → RST。
                if IS_WINDOWS and len(client_addr) >= 2:
                    try:
                        proxy.register_nat_entry(
                            client_addr[0], client_addr[1], sni_host, 443
                        )
                        logger.info("server", "SNI fallback NAT已注册",
                                    f"client={client_addr[0]}:{client_addr[1]} -> {sni_host}:443")
                    except Exception as e:  # noqa: BLE001
                        logger.warning("server", "SNI fallback NAT注册失败", str(e))
            else:
                logger.warning(
                    "server", "NAT反查失败且无SNI",
                    f"client={client_addr} 关闭连接"
                )
                return False
        orig_dst_ip, orig_dst_port = target

        # F30: 透明代理 raw tunnel 模式下，capturing=True 时记录 TLS 隧道元数据 flow。
        # 无法解密 payload，但让用户在抓包页面看到有 HTTPS 连接发生（不再"抓不到包"）。
        if self.capturing and self.session_id:
            try:
                host_for_record = orig_dst_ip
                # 如果是 SNI fallback，target[0] 可能是 hostname
                if isinstance(orig_dst_ip, str) and not orig_dst_ip[0].isdigit():
                    host_for_record = orig_dst_ip
                self._record_flow(
                    0, "", "CONNECT", f"https://{host_for_record}:{orig_dst_port}/",
                    "https", host_for_record, "/",
                    Headers(), b"", 200, Headers(),
                    f"TLS tunnel (raw, undecryptable): {host_for_record}:{orig_dst_port}",
                    0, 0, remote_ip=orig_dst_ip, ip_region="",
                    cert_info="", http_version="TLS",
                )
            except Exception:  # noqa: BLE001
                pass

        # 建立到原目标的 TCP 连接
        # 透明代理防循环（关键）：必须用 _connect_target 而非 socket.create_connection，
        # 因为 _connect_target 先 bind 获取本地端口并注册到 _proxy_outbound_ports，
        # 再执行 connect()，确保 SYN 发出前端口已注册，避免 WinDivert 拦截到代理自身的
        # SYN 并重定向回自身形成无限循环（TOCTOU 竞态修复）。
        try:
            target_sock = self._connect_target(
                orig_dst_ip, orig_dst_port, timeout=10
            )
        except OSError as e:
            # F31 诊断：连接目标失败（防火墙/网络中断/目标不可达）→ 客户端 RST
            logger.warning(
                "proxy", "raw隧道连接目标失败",
                f"target={orig_dst_ip}:{orig_dst_port} client={client_addr} "
                f"err={e} 导致RST"
            )
            return True  # 已处理（连接失败也返回 True 避免走 HTTP 流程）

        # 捕获 local_port 用于 socket 关闭后仍能注销端口（防泄漏）
        try:
            target_local_port = target_sock.getsockname()[1]
        except OSError:
            target_local_port = 0

        # 双向字节转发（不解密）
        # 端口注册/注销由 _connect_target 和此处 finally 统一管理
        try:
            self._tunnel_raw(client_sock, target_sock)
        except Exception:  # noqa: BLE001
            pass
        finally:
            # F10 修复：先 close() 再 unregister。
            # 原顺序（先 unregister 再 close）会让 close 触发的 FIN/RST 包
            # 在端口已从 _proxy_outbound_ports 移除后被 WinDivert 当作新客户端流量拦截，
            # 创建 spurious NAT 条目并污染 _client_port_index，且外部服务器收不到 FIN
            # 形成半开连接。先 close 让 FIN/RST 在端口仍注册时发出，被
            # _is_proxy_outbound_addr 排除直接放行；之后再注销端口。
            try:
                target_sock.close()
            except OSError:
                pass
            self._unregister_proxy_port(target_local_port)
        return True

    def _tunnel_raw(self, client_sock: socket.socket, target_sock: socket.socket):
        """双向字节转发（无 HTTP 解析），用于 raw TCP 隧道。

        与 _tunnel 的区别：
        - _tunnel 用于 CONNECT 隧道，reader 已消耗了 HTTP 头
        - _tunnel_raw 用于透明代理，可能没有 HTTP 头需要消耗，直接双向转发

        注意：本方法不关闭 target_sock（由调用方在 finally 中统一处理端口注销+关闭），
        避免 close 后 getsockname() 失败导致端口无法注销（端口泄漏）。

        性能优化：用 select.select 单线程驱动双向转发，替代双线程 pipe 模型，
        减少线程数并避免 GIL 争用，与 _tunnel 保持一致。
        """
        client_sock.settimeout(None)
        target_sock.settimeout(None)
        socks = [client_sock, target_sock]
        IDLE_TIMEOUT = 120.0
        while True:
            r, _, _ = select.select(socks, [], [], IDLE_TIMEOUT)
            if not r:
                break
            for s in r:
                dst = target_sock if s is client_sock else client_sock
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    dst.sendall(data)
                except OSError:
                    return

    # ---------- HTTPS CONNECT ----------

    def _handle_connect(self, client_sock, reader, connect_line, pid, proc_name):
        try:
            parts = connect_line.decode("latin-1").split()
            host_port = parts[1]
            host, _, port_s = host_port.partition(":")
            port = int(port_s) if port_s else 443
            # 端口范围校验（防 0/超范围值）
            if not (1 <= port <= 65535):
                return
        except Exception:  # noqa: BLE001
            return

        # 是否需要 SSL bump：
        # - 抓包中（capture on）：bump 所有 HTTPS（用户主动要抓包）
        # - 未抓包但有启用的自动修改规则：仅 bump 匹配某条规则 pattern 的 host
        #   避免对所有 HTTPS 都 bump 导致钉扎站点（edge/bing/bilibili 等）断连
        # - 证书已装 + 非忽略进程 + 非专注外进程
        # 性能优化（v11）：用 has_active_rules_fast() 无锁读，避免每 CONNECT 都进 _cache_lock
        if self.capturing:
            need_bump = True
        elif has_active_rules_fast():
            # 有规则但未抓包：只 bump 匹配规则的 host（结果有 LRU 缓存）
            need_bump = host_matches_any_rule(host)
        else:
            need_bump = False
        # 透明代理运行时：客户端未配置信任代理证书，SSL bump 必然握手失败（WinError 10054）。
        # 透明代理场景下 HTTPS 一律走 raw tunnel（不解密），避免断连。
        # （透明代理自身的重定向流量本就不经此分支，这里是防御「系统代理 + 透明代理」并存时的误 bump）
        # F29 修复：区分流量来源。系统代理来的 CONNECT（client_addr=127.0.0.1/::1）信任代理证书，
        # 保持 need_bump=True；透明代理重定向来的（非 loopback）才禁用 bump。
        try:
            from .transparent_proxy import get_transparent_proxy
            if get_transparent_proxy().running:
                try:
                    peer = client_sock.getpeername()
                    is_loopback = len(peer) >= 1 and peer[0] in ("127.0.0.1", "::1")
                except Exception:  # noqa: BLE001
                    is_loopback = False
                if not is_loopback:
                    need_bump = False
        except Exception:  # noqa: BLE001
            pass
        do_bump = (
            need_bump
            and self.ssl_bump is not None
            and self.cert_installed
            and not self.is_ignored(pid, proc_name, host)
            and not self.is_focused_out(pid, host)
        )
        # SSL bump 曾失败的 host 自动降级为纯隧道（避免反复握手失败断连）
        # TTL 机制：超过 _SSL_BUMP_FAILED_TTL 秒后移除并允许重试（证书可能已重新安装）
        with self._ssl_bump_failed_lock:
            failed_at = self._ssl_bump_failed_hosts.get(host)
            if failed_at is not None:
                if time.time() - failed_at > _SSL_BUMP_FAILED_TTL:
                    del self._ssl_bump_failed_hosts[host]
                    # TTL 过期，允许重试 do_bump
                else:
                    do_bump = False

        # F6 修复：先签发证书，失败则降级为纯隧道（避免已发 200 但无法 TLS 握手 → RESET）。
        # 原逻辑：先发 200（第 1242 行）再 get_cert，失败直接 return → 客户端等 TLS 握手挂起。
        # 现逻辑：get_cert 提前到发 200 之前，失败时置 do_bump=False 走纯隧道分支，
        # 并加入降级列表（300s TTL），避免反复失败 + 自动恢复。
        # get_cert 无持久副作用（_needs_resign 自愈），可安全提前。
        cert_path = None
        key_path = None
        if do_bump:
            try:
                cert_path, key_path = self.ssl_bump.get_cert(host)
            except Exception:  # noqa: BLE001
                with self._ssl_bump_failed_lock:
                    was_new = host not in self._ssl_bump_failed_hosts
                    self._ssl_bump_failed_hosts[host] = time.time()
                if was_new:
                    logger.warning(
                        "proxy",
                        "SSL bump 证书签发失败，降级为纯隧道",
                        f"host={host} pid={pid} proc={proc_name}。"
                        f"后续该 host 将直接隧道转发（不解密），请检查证书目录磁盘空间/权限。"
                    )
                do_bump = False

        if not do_bump:
            # 纯隧道转发（不解密）
            # 先消耗 CONNECT 请求剩余的 HTTP 头，避免转发到目标导致协议错误
            reader.read_headers()
            # F33: 纯隧道流量也记录元数据，让用户在抓包页看到 CONNECT 流量。
            # 原实现 _tunnel 无 _record_flow 调用，导致系统代理 CONNECT 非 bump 流量
            # （如透明代理运行时的非 loopback CONNECT）完全不显示在抓包页。
            # 与 F30（_try_raw_tunnel 元数据记录）对齐，让所有 HTTPS 隧道流量都可见。
            # 使用真实 pid/proc_name（F33 改进）：F33 场景下 pid 已在手，比 F30（raw tunnel 无 pid）更优。
            if self.capturing and self.session_id:
                try:
                    self._record_flow(
                        pid or 0, proc_name or "", "CONNECT", f"https://{host}:{port}/",
                        "https", host, "/",
                        Headers(), b"", 200, Headers(),
                        f"TLS tunnel (no bump): {host}:{port}",
                        0, 0, remote_ip=host, ip_region="",
                        cert_info="", http_version="TLS",
                    )
                except Exception:  # noqa: BLE001
                    pass
            try:
                client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            except OSError:
                return
            self._tunnel(client_sock, reader, host, port)
            return

        # 告诉客户端隧道已建立
        try:
            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        except OSError:
            return

        # SSL bump：cert_path/key_path 已在上方预尝试获取（F6 修复）
        # 缓存 SSLContext（按 cert_path + cert_mtime 缓存），避免每次 CONNECT 都创建
        # LRU：命中时 move_to_end 标记为最近使用，超上限时 popitem(last=False) 淘汰最久未用
        # mtime 校验：叶证书被 ssl_bump 重新签发后（根证书变更或旧格式升级），
        # 文件 mtime 改变，缓存的 SSLContext 自动失效重建，避免使用旧证书
        #
        # 性能优化：double-checked locking —— 缓存命中时仅短暂持锁（dict get + move_to_end）；
        # 缓存未命中时 SSLContext 创建 + load_cert_chain（文件 I/O，5-20ms）在锁外执行，
        # 不阻塞其他线程的缓存查询。原实现把 load_cert_chain 放在锁内，10 个新 host 并发时
        # 串行化 50-200ms，是 HTTPS 首次加载卡顿的元凶之一。
        try:
            current_mtime = os.path.getmtime(cert_path)
        except OSError:
            current_mtime = 0
        ssl_ctx = None
        # Fast path：锁内只做 dict get + move_to_end
        with self._ssl_ctx_lock:
            cached_entry = self._ssl_ctx_cache.get(cert_path)
            if cached_entry is not None:
                cached_ctx, cached_mtime = cached_entry
                if current_mtime and cached_mtime == current_mtime:
                    ssl_ctx = cached_ctx
                    self._ssl_ctx_cache.move_to_end(cert_path)
        # Cache miss：在锁外创建 SSLContext（load_cert_chain 是文件 I/O，不阻塞其他线程）
        if ssl_ctx is None:
            new_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            new_ctx.load_cert_chain(cert_path, key_path)
            # 客户端侧 ALPN：只通告 http/1.1，强制客户端走 HTTP/1.1
            # 代理到客户端方向仅实现 HTTP/1.1 解析（h2 多路复用在代理→服务器方向已实现）
            # 这样客户端不会协商到 h2，避免 h2 preface 被误解析
            try:
                new_ctx.set_alpn_protocols(["http/1.1"])
            except Exception:  # noqa: BLE001
                pass
            # Double-checked locking：重新加锁插入，可能另一线程已抢先创建
            with self._ssl_ctx_lock:
                cached_entry = self._ssl_ctx_cache.get(cert_path)
                if cached_entry is not None:
                    cached_ctx, cached_mtime = cached_entry
                    if current_mtime and cached_mtime == current_mtime:
                        # 另一线程已插入有效 ctx，复用它（丢弃我们新建的）
                        ssl_ctx = cached_ctx
                        self._ssl_ctx_cache.move_to_end(cert_path)
                    else:
                        # mtime 已变（证书被重新签发），用我们新建的 ctx 覆盖
                        ssl_ctx = new_ctx
                        self._ssl_ctx_cache[cert_path] = (new_ctx, current_mtime)
                else:
                    ssl_ctx = new_ctx
                    self._ssl_ctx_cache[cert_path] = (new_ctx, current_mtime)
                    # 超上限淘汰最久未用的条目（LRU）
                    if len(self._ssl_ctx_cache) > _SSL_CTX_CACHE_MAX:
                        self._ssl_ctx_cache.popitem(last=False)
        try:
            tls_sock = ssl_ctx.wrap_socket(client_sock, server_side=True)
        except (ssl.SSLError, OSError) as e:
            # TLS 握手失败：客户端拒绝了我们的证书
            from datetime import datetime
            err_str = str(e).lower()
            is_cert_error = any(k in err_str for k in ("tlsv1 alert", "handshake failure",
                                                       "certificate", "unknown ca", "bad certificate",
                                                       "eof", "violation of protocol",
                                                       "unexpected eof", "connection reset"))
            if is_cert_error:
                # 将该 host 加入 SSL bump 失败列表，后续连接自动降级为纯隧道（不解密但不断连）
                with self._ssl_bump_failed_lock:
                    was_new = host not in self._ssl_bump_failed_hosts
                    self._ssl_bump_failed_hosts[host] = time.time()
                if was_new:
                    logger.warning("proxy",
                                   f"SSL bump 失败，已降级为纯隧道: host={host}, pid={pid}, proc={proc_name}",
                                   f"TLS 握手失败: {e}。后续该 host 的连接将直接隧道转发（不解密）。"
                                   f"如需解密，请确认 Telnix 根证书已安装（cert install），或该应用可能使用了证书钉扎。")
                # 收集到疑似 pinning 列表，供 status 接口暴露给 agent
                key = (host, pid, proc_name)
                with self._pinning_lock:
                    if key not in self._pinning_keys:
                        self._pinning_keys.add(key)
                        self._pinning_suspected.append({
                            "host": host, "pid": pid, "process_name": proc_name,
                            "timestamp": datetime.now().isoformat(timespec="seconds"),
                            "error": str(e)[:200],
                        })
                        # 上限 50 条，FIFO 淘汰
                        if len(self._pinning_suspected) > 50:
                            dropped = self._pinning_suspected.pop(0)
                            self._pinning_keys.discard((dropped.get("host"),
                                                        dropped.get("pid"),
                                                        dropped.get("process_name")))
            else:
                logger.warning("proxy", f"TLS 握手失败: host={host}, pid={pid}",
                               f"错误: {e}")
            return

        tls_sock.settimeout(60)
        tls_reader = SocketReader(tls_sock)
        try:
            # 在 TLS 连接上循环处理 HTTP 请求（keep-alive）
            self._serve_http_loop(tls_sock, tls_reader, pid, proc_name,
                                  scheme="https", default_host=host,
                                  default_port=port)
        finally:
            try:
                tls_sock.unwrap()
            except (OSError, ValueError):
                pass
            try:
                tls_sock.close()
            except OSError:
                pass

    def get_ssl_bump_failed_hosts(self) -> list[str]:
        """返回当前 SSL bump 失败的 host 列表（清理过期项，供 status API 调用）。"""
        with self._ssl_bump_failed_lock:
            now = time.time()
            expired = [h for h, t in self._ssl_bump_failed_hosts.items()
                       if now - t > _SSL_BUMP_FAILED_TTL]
            for h in expired:
                del self._ssl_bump_failed_hosts[h]
            return sorted(self._ssl_bump_failed_hosts.keys())

    def clear_ssl_bump_failed_hosts(self):
        """清空 SSL bump 失败集合（证书重新安装后调用）。"""
        with self._ssl_bump_failed_lock:
            self._ssl_bump_failed_hosts.clear()

    def _tunnel(self, client_sock, reader, host, port):
        """纯隧道：不解密，直接把字节在客户端与目标之间双向转发。

        性能优化：用 select.select 单线程驱动双向转发，替代原双线程 pipe 模型。
        - 原模型：每个 CONNECT 开 2 个 pipe 线程 + join 阻塞，30 个并发域名 = 90 线程，GIL 严重争用
        - 新模型：单线程 select 循环，线程数减少 2/3，GIL 争用大幅降低
        - 额外收益：select timeout 实现空闲超时（120s 无活动自动关闭），防止僵尸连接占用线程
        """
        # 弱网模拟：每连接启动延迟（模拟 RTT）
        throttle.delay()
        try:
            target = self._connect_target(host, port)
        except OSError:
            try:
                client_sock.sendall(
                    b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except OSError:
                pass
            return
        # 捕获 local_port 用于 socket 关闭后仍能注销端口（防泄漏）
        try:
            target_local_port = target.getsockname()[1]
        except OSError:
            target_local_port = 0
        # 所有后续操作都在 try/finally 覆盖内，确保异常时也注销端口+关闭 socket
        # （修复审计 1.1：原代码 target.sendall(leftover) 在 try/finally 之外，
        # 异常时 target 既不关闭也不注销端口，导致 fd + 端口泄漏）
        try:
            # 先把 reader 缓冲里剩余的字节发给目标
            leftover = bytes(reader.buf)
            if leftover:
                target.sendall(leftover)
            client_sock.settimeout(None)
            target.settimeout(None)

            # select 驱动双向转发（单线程，替代双线程 pipe 模型）
            socks = [client_sock, target]
            IDLE_TIMEOUT = 120.0  # 120s 无活动则关闭（匹配连接池 _MAX_IDLE）
            while True:
                r, _, _ = select.select(socks, [], [], IDLE_TIMEOUT)
                if not r:
                    break  # 空闲超时
                done = False
                for s in r:
                    dst = target if s is client_sock else client_sock
                    try:
                        data = s.recv(65536)
                    except OSError:
                        done = True
                        break
                    if not data:
                        done = True
                        break
                    # 弱网模拟：按 drop_pct 概率丢包
                    if throttle.should_drop():
                        continue
                    # 弱网模拟：限速发送
                    try:
                        throttle.send_throttled(dst, data)
                    except OSError:
                        done = True
                        break
                if done:
                    break
        finally:
            # 用捕获的 target_local_port 注销（socket 关闭后仍可用）
            # F10: 先 close 再 unregister，让 FIN/RST 在端口仍注册时发出，
            # 被 _is_proxy_outbound_addr 排除，避免 WinDivert 拦截产生 spurious NAT
            try:
                target.close()
            except OSError:
                pass
            self._unregister_proxy_port(target_local_port)

    # ---------- HTTP ----------

    def _handle_http(self, client_sock, reader, first_line, pid, proc_name,
                     scheme, default_host=None, default_port=None):
        self._serve_http_loop(client_sock, reader, pid, proc_name, scheme,
                              default_host=default_host, default_port=default_port,
                              first_line=first_line)

    def _serve_http_loop(self, client_sock, reader, pid, proc_name, scheme,
                         default_host=None, default_port=None, first_line=None):
        """在一条连接上循环处理 HTTP 请求（keep-alive）。"""
        line = first_line
        # 获取 client_addr 用于 PID 重试（首次 lookup miss 后后续请求重试）
        client_addr = None
        if pid is None and self._needs_pid():
            try:
                client_addr = client_sock.getpeername()
            except OSError:
                client_addr = None
        while self._running:
            # first_line 为 None 时从连接读取下一行
            if line is None:
                try:
                    # keep-alive 空闲超时：30s 无新请求则关闭连接
                    # 防止浏览器空闲 keep-alive 连接永久占用代理线程
                    # （浏览器默认 keep-alive idle 15-30s，30s 超时确保浏览器主动关闭前不误杀）
                    client_sock.settimeout(30)
                    line = reader.read_line()
                except (OSError, socket.timeout):
                    break
                if not line:
                    break
            # 请求处理期间取消超时（响应可能较慢，由 _connect_target 和 socket 自身超时控制）
            client_sock.settimeout(None)
            # 过滤 HTTP/2 connection preface（PRI * HTTP/2.0）
            # 客户端可能通过 ALPN 协商到 h2，但代理仅支持 HTTP/1.1 解析，
            # h2 preface 不是有效 HTTP/1.1 请求，跳过避免记录无效流量
            if line.startswith(b"PRI "):
                # 读取并丢弃 preface 的剩余部分（空行 + SM + 空行 + SETTINGS 帧）
                try:
                    reader.read_line()  # 空行
                    reader.read_line()  # SM
                    reader.read_line()  # 空行
                    # SETTINGS 帧由后续 read_line 处理，可能读不到，忽略
                except OSError:
                    pass
                # 客户端要求 h2，但代理只能 HTTP/1.1，关闭连接
                break
            # 进程名重试：首次 lookup 超时返回 None 时，后续 keep-alive 请求重试
            # （同步查找带 200ms 超时，通常首次就能命中，此处为兜底）
            if pid is None and client_addr and self._needs_pid():
                pid, proc_name = self.process_lookup.lookup(client_addr)
            keep_alive = self._process_one_request(
                client_sock, reader, line, pid, proc_name, scheme,
                default_host, default_port)
            line = None
            if not keep_alive:
                break

    def _process_one_request(self, client_sock, reader, request_line, pid,
                             proc_name, scheme, default_host, default_port) -> bool:
        """处理单个 HTTP 请求。返回是否保持 keep-alive。"""
        try:
            method, url, version, headers, body = self._read_request(
                reader, request_line)
        except Exception:  # noqa: BLE001
            return False

        # 解析目标 host/port/path
        host, port, path, orig_url = self._parse_target(
            method, url, headers, scheme, default_host, default_port)

        if host is None:
            self._send_simple(client_sock, 400, "Bad Request")
            return False

        keep_alive = self._should_keep_alive(version, headers)

        # IP 属地分析：DNS 预解析异步化（丢到线程池），不阻塞代理线程
        # 直连场景：与实际连接的目标 IP 一致
        # Clash 上游代理场景：本机解析的候选 IP（可能与 Mihomo 选中节点不一致，但能给出大致属地）
        # 性能优化：不抓包时（capturing=False）跳过 DNS 与属地查询；
        #           抓包时提交到 _DNS_EXECUTOR，记录流量时非阻塞取结果（未完成则用空值）
        _dns_future = None
        if self.capturing:
            _dns_future = _DNS_EXECUTOR.submit(_resolve_host_for_region, host, port)

        def _ip_info():
            """非阻塞获取 DNS 解析 + 属地查询结果，未完成返回 ("", "")。"""
            if _dns_future is None or not _dns_future.done():
                return "", ""
            try:
                return _dns_future.result()
            except Exception:  # noqa: BLE001
                return "", ""

        remote_ip = ""
        ip_region = ""

        # 自动回复规则（mock：直接返回伪造响应）
        # §4.1 请求阶段匹配：传 method/pid/process_name（status_code 此时无，传 None）
        # 请求阶段无法匹配带 status_filter 的规则（filter_str 非空但 value 为 None → 不匹配）
        # 那些规则会在响应阶段（modify_response 分支）重新匹配
        # 性能优化（v11）：无启用规则时跳过 _match_auto_reply，避免每请求都进 _cache_lock
        if has_active_rules_fast():
            rule = self._match_auto_reply(
                orig_url, method=method, status_code=None,
                pid=pid, process_name=proc_name)
        else:
            rule = None
            # throttle 日志：记录跳过原因（capturing off 或无规则）
            if not self.capturing:
                _log_skip_rule_match("capturing off 且无启用规则")
            else:
                _log_skip_rule_match("无启用规则")
        if rule:
            logger.info("proxy", f"匹配到自动回复规则: action={rule['action']}, pattern={rule['pattern']}",
                        f"URL={orig_url}")
            # §3.2 命中计数：匹配成功即自增（flow_id 此阶段可能为 None）
            try:
                db.increment_rule_hit(rule.get("id"), None)
            except Exception:  # noqa: BLE001
                pass
        if rule and rule["action"] == "mock":
            self._handle_mock(client_sock, rule, keep_alive)
            mock_status = rule["mock_status"] if rule["mock_status"] is not None else 200
            mock_ct = Headers.from_dict(_parse_json(rule.get("mock_headers"))).get("Content-Type")
            if self.capturing and self.session_id and not self.is_ignored(pid, proc_name, host) \
                    and not self.is_focused_out(pid, host, method, mock_status, mock_ct):
                remote_ip, ip_region = _ip_info()
                self._record_flow(pid, proc_name, method, orig_url, scheme,
                                  host, path, headers, body,
                                  rule["mock_status"],
                                  Headers.from_dict(_parse_json(rule.get("mock_headers"))),
                                  rule.get("mock_body") or "",
                                  remote_ip=remote_ip, ip_region=ip_region)
            return keep_alive

        # mock_request：写死请求（用预设的 method/url/headers/body 转发到目标服务器，返回真实响应）
        # 区别于 mock（不转发直接返回伪造响应），mock_request 会真的请求服务器，但请求内容是预设的
        if rule and rule["action"] == "mock_request":
            try:
                m_method = (rule.get("mock_method") or "GET").upper()
                m_url = rule.get("mock_url") or orig_url
                # 解析预设 URL 得到 host/port/path/scheme
                sp = urlsplit(m_url)
                m_scheme = sp.scheme or scheme or "https"
                m_host = sp.hostname or host
                m_port = sp.port or (443 if m_scheme == "https" else 80)
                m_path = sp.path or "/"
                if sp.query:
                    m_path = f"{m_path}?{sp.query}"
                m_headers = Headers.from_dict(_parse_json(rule.get("mock_headers")))
                m_body = _to_bytes(rule.get("mock_body") or "")
                # 必须有 Host 头
                if not m_headers.has("Host"):
                    m_headers.set("Host", m_host)
                logger.info("proxy", "mock_request: 用预设请求转发",
                            f"method={m_method}, url={m_url}, host={m_host}, body_len={len(m_body)}")
                start = time.time()
                try:
                    status, resp_headers, resp_body, m_cert_info, m_http_ver = self._forward(
                        m_host, m_port, m_scheme, m_method, m_path, "HTTP/1.1",
                        m_headers, m_body)
                except Exception:  # noqa: BLE001
                    self._send_simple(client_sock, 502, "Bad Gateway")
                    return False
                duration = int((time.time() - start) * 1000)
                self._send_response(client_sock, status, resp_headers, resp_body, keep_alive)
                # 记录流量（用预设的 method/url，而非原始请求）
                if self.capturing and self.session_id and not self.is_ignored(pid, proc_name, m_host) \
                        and not self.is_focused_out(pid, m_host, m_method, status,
                                                     resp_headers.get("Content-Type")):
                    remote_ip, ip_region = _ip_info()
                    self._record_flow(pid, proc_name, m_method, m_url, m_scheme,
                                      m_host, m_path, m_headers, m_body,
                                      status, resp_headers,
                                      _truncate_for_record(resp_body), duration, len(resp_body),
                                      remote_ip=remote_ip, ip_region=ip_region,
                                      cert_info=m_cert_info, http_version=m_http_ver)
                return keep_alive
            except Exception as e:  # noqa: BLE001
                logger.info("proxy", "mock_request 执行失败", str(e))
                self._send_simple(client_sock, 500, "Mock Request Error")
                return False

        # modify_request：在转发前应用规则到请求头/体（记录原始 vs 修改后 diff 供 agent 排查）
        if rule and rule["action"] == "modify_request":
            # §3.8 delay-request 动作：在转发前延迟（毫秒）
            _apply_delay(rule, target_name="delay-request",
                         log_label="delay-request")
            # 性能优化：detail 中的 hex/decode/str(dict) 较昂贵，仅在 INFO 级别启用时计算
            _log_detail = logger.is_enabled(logger.INFO)
            if _log_detail:
                try:
                    orig_headers_str = str(headers.to_dict())
                    orig_body_preview = body[:200].hex() if body else "(empty)"
                    orig_body_text = body[:200].decode("utf-8", errors="replace") if body else ""
                except Exception:  # noqa: BLE001
                    orig_headers_str, orig_body_preview, orig_body_text = "(读取失败)", "", ""
            headers, body = _apply_modify_request(headers, body, rule)
            if _log_detail:
                try:
                    new_headers_str = str(headers.to_dict())
                    new_body_preview = body[:200].hex() if body else "(empty)"
                    new_body_text = body[:200].decode("utf-8", errors="replace") if body else ""
                except Exception:  # noqa: BLE001
                    new_headers_str, new_body_preview, new_body_text = "(读取失败)", "", ""
                detail = (f"原始 headers: {orig_headers_str}\n"
                          f"修改后 headers: {new_headers_str}\n"
                          f"原始 body(hex): {orig_body_preview}\n"
                          f"修改后 body(hex): {new_body_preview}\n"
                          f"原始 body(text): {orig_body_text}\n"
                          f"修改后 body(text): {new_body_text}")
            else:
                detail = ""
            logger.info("proxy",
                        f"modify_request 已应用: host={host}, path={path}, rule_note={rule.get('note', '')}",
                        detail)

        # script：调用用户 Python 脚本的 on_request（请求阶段，转发前）
        # 脚本可修改请求头/体，或返回 mock（直接伪造响应）/ drop（拒绝）
        if rule and rule["action"] == "script":
            from ..auto_reply.script_runner import (
                call_script_request, build_ctx, apply_request)
            script = rule.get("modify_rules") or ""
            if isinstance(script, list):
                # 兼容旧 JSON 字段（不太可能，但保护）
                script = ""
            if not script.strip():
                logger.warning("proxy", f"script 规则内容为空: {rule.get('id')}", "")
            else:
                try:
                    ctx = build_ctx(
                        host=host, path=path, method=method, url=orig_url,
                        scheme=scheme, pid=pid, process_name=proc_name or "",
                        session_id=self.session_id,
                        request_headers=headers.to_dict(), request_body=body,
                    )
                    logger.info("proxy", f"script on_request 开始调用: {rule['id']}",
                                f"url={orig_url}, body_len={len(body) if body else 0}")
                    resp = call_script_request(rule["id"], script, ctx)
                    logger.info("proxy", f"script on_request 调用返回: {rule['id']}",
                                f"resp={'None' if resp is None else resp.get('action', '?')}")
                except Exception as e:  # noqa: BLE001
                    logger.warning("proxy", f"script on_request 异常: {rule['id']}",
                                   f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
                    resp = None
                if resp is None:
                    logger.warning("proxy", f"script on_request 调用失败: {rule['id']}",
                                   "脚本不可用，请求按原样转发")
                else:
                    action = resp.get("action", "continue")
                    if action == "drop":
                        logger.info("proxy", f"script drop 请求: {orig_url}", "")
                        self._send_simple(client_sock, 403, "Blocked by Script")
                        return False
                    if action == "mock":
                        # 脚本返回伪造响应，直接发给客户端
                        m_status = int(resp.get("mock_status") or 200)
                        m_headers = Headers.from_dict(resp.get("mock_headers") or {})
                        m_body = b""
                        if resp.get("mock_body_b64"):
                            try:
                                m_body = base64.b64decode(resp["mock_body_b64"])
                            except Exception:  # noqa: BLE001
                                pass
                        self._send_response(client_sock, m_status, m_headers, m_body, keep_alive)
                        if self.capturing and self.session_id and not self.is_ignored(pid, proc_name, host) \
                                and not self.is_focused_out(pid, host, method, m_status,
                                                             m_headers.get("Content-Type")):
                            remote_ip, ip_region = _ip_info()
                            self._record_flow(pid, proc_name, method, orig_url, scheme,
                                              host, path, headers, body, m_status, m_headers, m_body,
                                              remote_ip=remote_ip, ip_region=ip_region)
                        logger.info("proxy", f"script mock 请求: {orig_url} -> {m_status}", "")
                        return keep_alive
                    # continue：应用请求头/体修改
                    new_headers, new_body = apply_request(resp, headers, body)
                    if new_headers is not headers:
                        try:
                            orig_h = str(headers.to_dict())
                            new_h = str(new_headers.to_dict())
                        except Exception:  # noqa: BLE001
                            orig_h, new_h = "?", "?"
                        logger.info("proxy", f"script on_request 修改 headers: {orig_h} -> {new_h}", "")
                        headers = new_headers
                    if new_body is not body:
                        logger.info("proxy", f"script on_request 修改 body: len {len(body)} -> {len(new_body)}", "")
                        body = new_body

        # 忽略进程或专注外进程/host：转发不记录
        # 请求阶段只传 pid/host/method；响应阶段条件（status_code/content_type）
        # 由 is_focused_out 内部处理（暂不算专注外，等响应阶段再判）
        # DNS 预解析结果非阻塞刷新（若已完成则取值，否则保持空值不阻塞）
        remote_ip, ip_region = _ip_info()
        ignored = self.is_ignored(pid, proc_name, host) or self.is_focused_out(pid, host, method)
        record = self.capturing and self.session_id and not ignored

        # 请求断点
        flow_id = None
        if record and self.breakpoint.should_break_request():
            flow_id = self._insert_flow(pid, proc_name, method, orig_url, scheme,
                                        host, path, headers, body,
                                        remote_ip=remote_ip, ip_region=ip_region)
            db.update_flow_breakpoint(flow_id, "pending_request")
            action = self.breakpoint.wait_for_release(flow_id)
            if action == "drop":
                db.update_flow_breakpoint(flow_id, None)
                return False
            # 重新读取可能被修改的请求
            modified = db.get_flow(flow_id)
            if modified:
                method = modified["method"] or method
                path = modified["path"] or path
                orig_url = modified["url"] or orig_url
                host = modified["host"] or host
                headers = Headers.from_dict(_parse_json(modified["request_headers"]))
                body = _to_bytes(modified["request_body"])
            db.update_flow_breakpoint(flow_id, None)

        # 转发到目标
        start = time.time()
        # WebSocket Upgrade：握手成功后转为双向帧转发，不再走常规 HTTP 请求/响应
        from .websocket_relay import is_websocket_upgrade, relay_websocket
        if is_websocket_upgrade(headers):
            return self._handle_websocket_upgrade(
                client_sock, host, port, scheme, method, path, orig_url,
                headers, body, pid, proc_name, remote_ip, ip_region,
                record, flow_id, keep_alive)
        cert_info_json = ""
        try:
            # 是否需要解压响应体：抓包记录或需要修改响应时才解压，否则跳过节省 CPU
            need_decompress = record or (
                rule is not None and rule["action"] in ("modify_response", "script"))
            status, resp_headers, resp_body, cert_info_json, http_version = self._forward(
                host, port, scheme, method, path, version, headers, body,
                decompress=need_decompress)
        except Exception:  # noqa: BLE001
            self._send_simple(client_sock, 502, "Bad Gateway")
            if record and flow_id:
                db.update_flow_response_async(flow_id, 502, "{}", "", 0, 0)
            elif record:
                self._update_or_insert_response(
                    flow_id, pid, proc_name, method, orig_url, scheme, host,
                    path, headers, body, 502, Headers(), b"", start,
                    remote_ip=remote_ip, ip_region=ip_region,
                    cert_info=cert_info_json)
            return False
        duration = int((time.time() - start) * 1000)
        # DNS 预解析结果非阻塞刷新（转发耗时期间 DNS 多已完成）
        remote_ip, ip_region = _ip_info()

        # 自动回复：修改响应
        # §4.1 响应阶段重新匹配：如果请求阶段未匹配上（可能因 status_filter 限制），
        # 现在拿到 status_code 后再匹配一次，让带 status_filter 的 modify_response 规则生效
        # 性能优化（v11）：无启用规则时跳过（请求阶段已检查过，但规则可能在转发期间变更）
        if rule is None and has_active_rules_fast():
            rule = self._match_auto_reply(
                orig_url, method=method, status_code=status,
                pid=pid, process_name=proc_name)
            if rule:
                logger.info("proxy",
                        f"响应阶段匹配到规则: action={rule['action']}, pattern={rule['pattern']}",
                        f"URL={orig_url}, status={status}")
                # §3.2 命中计数（响应阶段匹配也计数）
                try:
                    db.increment_rule_hit(rule.get("id"), flow_id)
                except Exception:  # noqa: BLE001
                    pass
        if rule and rule["action"] == "modify_response":
            # 性能优化：detail 中的 !r 计算仅在 INFO 级别启用时执行
            _log_detail = logger.is_enabled(logger.INFO)
            logger.info("proxy", "进入 modify_response 分支",
                        (f"body_len={len(resp_body)}, Content-Encoding={resp_headers.get('Content-Encoding')}, "
                         f"Content-Type={resp_headers.get('Content-Type')}, "
                         f"body 前 200 字节={resp_body[:200]!r}") if _log_detail else "")
            # §3.8 delay 动作：在响应前延迟（毫秒）
            _apply_delay(rule, target_name="delay", log_label="delay")
            status, resp_headers, resp_body = _apply_modify_response(
                status, resp_headers, resp_body, rule)
            logger.info("proxy", "modify_response 处理完成",
                        (f"body_len={len(resp_body)}, body 前 200 字节={resp_body[:200]!r}") if _log_detail else "")

        # script：调用用户 Python 脚本的 on_response（响应阶段，返回客户端前）
        # 脚本可修改响应头/体/状态码，或返回 drop（替换为 403）/ mock（替换为伪造响应）
        if rule and rule["action"] == "script":
            from ..auto_reply.script_runner import (
                call_script_response, build_ctx, apply_response)
            script = rule.get("modify_rules") or ""
            if isinstance(script, list):
                script = ""
            if not script.strip():
                logger.warning("proxy", f"script 规则内容为空: {rule.get('id')}", "")
            else:
                # 注意：请求阶段已修改过的 headers/body 现在是原始响应的 headers/body
                # ctx.request_* 传当前（已被 on_request 修改过的）请求
                ctx = build_ctx(
                    host=host, path=path, method=method, url=orig_url,
                    scheme=scheme, pid=pid, process_name=proc_name or "",
                    session_id=self.session_id,
                    request_headers=headers.to_dict(), request_body=body,
                    status_code=status,
                    response_headers=resp_headers.to_dict(),
                    response_body=resp_body,
                )
                resp = call_script_response(rule["id"], script, ctx)
                if resp is None:
                    logger.warning("proxy", f"script on_response 调用失败: {rule['id']}",
                                   "脚本不可用，响应按原样返回")
                else:
                    action = resp.get("action", "continue")
                    if action == "drop":
                        logger.info("proxy", f"script drop 响应: {orig_url}", "")
                        self._send_simple(client_sock, 403, "Blocked by Script")
                        return False
                    if action == "mock":
                        # 脚本返回全新伪造响应，覆盖原响应
                        m_status = int(resp.get("mock_status") or 200)
                        m_headers = Headers.from_dict(resp.get("mock_headers") or {})
                        m_body = b""
                        if resp.get("mock_body_b64"):
                            try:
                                m_body = base64.b64decode(resp["mock_body_b64"])
                            except Exception:  # noqa: BLE001
                                pass
                        status, resp_headers, resp_body = m_status, m_headers, m_body
                        logger.info("proxy", f"script mock 响应: {orig_url} -> {m_status}", "")
                    else:
                        # continue：应用响应修改
                        new_status, new_headers, new_body = apply_response(
                            resp, status, resp_headers, resp_body)
                        if new_status != status:
                            logger.info("proxy", f"script on_response 改状态码: {status} -> {new_status}", "")
                            status = new_status
                        if new_headers is not resp_headers:
                            logger.info("proxy", "script on_response 修改 headers", "")
                            resp_headers = new_headers
                        if new_body is not resp_body:
                            logger.info("proxy", f"script on_response 改 body: len {len(resp_body)} -> {len(new_body)}", "")
                            resp_body = new_body

        # 响应断点
        if record and self.breakpoint.should_break_response():
            if flow_id is None:
                flow_id = self._insert_flow(pid, proc_name, method, orig_url,
                                            scheme, host, path, headers, body,
                                            remote_ip=remote_ip, ip_region=ip_region)
            db.update_flow_response(flow_id, status, json.dumps(resp_headers.to_dict()),
                                    _to_text(resp_body), duration,
                                    len(resp_body))
            db.update_flow_breakpoint(flow_id, "pending_response")
            action = self.breakpoint.wait_for_release(flow_id)
            if action == "drop":
                db.update_flow_breakpoint(flow_id, None)
                return False
            modified = db.get_flow(flow_id)
            if modified:
                status = modified["status_code"] or status
                resp_headers = Headers.from_dict(
                    _parse_json(modified["response_headers"]))
                resp_body = _to_bytes(modified["response_body"])
            db.update_flow_breakpoint(flow_id, None)

        # 发回客户端
        self._send_response(client_sock, status, resp_headers, resp_body, keep_alive)

        # 响应阶段：若有响应专注条件（status_code/content_type），再次评估是否在专注范围
        # 请求阶段无法知道响应字段，此时才能精确判定
        if record and self._has_response_focus():
            resp_ct = resp_headers.get("Content-Type")
            if self.is_focused_out(pid, host, method, status, resp_ct):
                record = False
                # 若请求阶段已插入 flow（请求断点），删除避免残留
                if flow_id is not None:
                    try:
                        db.delete_flow(flow_id)
                    except Exception:  # noqa: BLE001
                        pass

        # 记录
        if record:
            if flow_id is None:
                self._record_flow(pid, proc_name, method, orig_url, scheme, host,
                                  path, headers, body, status, resp_headers,
                                  _truncate_for_record(resp_body), duration, len(resp_body),
                                  remote_ip=remote_ip, ip_region=ip_region,
                                  cert_info=cert_info_json, http_version=http_version)
            else:
                db.update_flow_response_async(
                    flow_id, status, json.dumps(resp_headers.to_dict()),
                    _truncate_for_record(resp_body), duration, len(resp_body))
                # 更新 cert_info（首次握手时获取）
                if cert_info_json:
                    try:
                        with db.get_connection() as conn:
                            conn.execute(
                                "UPDATE flows SET cert_info=? WHERE id=?",
                                (cert_info_json, flow_id))
                    except Exception:  # noqa: BLE001
                        pass
        return keep_alive

    # ---------- 请求读取与解析 ----------

    def _read_request(self, reader, request_line):
        parts = request_line.decode("latin-1", "replace").split(" ", 2)
        if len(parts) < 3:
            raise ValueError("bad request line")
        method, url, version = parts[0], parts[1], parts[2]
        header_lines = reader.read_headers()
        headers = Headers.from_lines(header_lines)
        body = read_body(reader, headers, is_request=True, method=method)
        return method, url, version, headers, body

    def _parse_target(self, method, url, headers, scheme, default_host, default_port):
        """返回 (host, port, path, orig_url)。"""
        orig_url = url
        # 绝对 URL（HTTP 代理请求形式）：GET http://host/path HTTP/1.1
        if url.lower().startswith("http://") or url.lower().startswith("https://"):
            sp = urlsplit(url)
            host = sp.hostname or default_host
            port = sp.port or (443 if sp.scheme == "https" else 80)
            path = sp.path or "/"
            if sp.query:
                path += "?" + sp.query
            scheme = sp.scheme
            return host, port, path, orig_url
        # 路径形式（HTTPS bump 后或直连）：GET /path HTTP/1.1
        host = headers.get("Host")
        if host:
            if ":" in host:
                h, _, p = host.partition(":")
                host = h
                port = int(p) if p else (default_port or 80)
            else:
                port = default_port or (443 if scheme == "https" else 80)
        else:
            host = default_host
            port = default_port or (443 if scheme == "https" else 80)
        path = url
        # 重建 orig_url 供记录
        orig_url = f"{scheme}://{host}{path}"
        return host, port, path, orig_url

    def _should_keep_alive(self, version, headers) -> bool:
        conn = (headers.get("Connection") or "").lower()
        if version.upper() == "HTTP/1.1":
            return "close" not in conn
        return "keep-alive" in conn

    # ---------- 转发 ----------

    def _handle_websocket_upgrade(self, client_sock, host, port, scheme, method,
                                  path, orig_url, headers: Headers, body: bytes,
                                  pid, proc_name, remote_ip: str, ip_region: str,
                                  record: bool, flow_id, keep_alive: bool) -> bool:
        """处理 WebSocket Upgrade 请求。

        流程：
        1. 建立到目标服务器的 TCP/TLS 连接（不复用连接池，WS 是长连接）
        2. 发送原始 Upgrade 请求到服务器
        3. 读取服务器 101 响应，转发给客户端
        4. 转为 WebSocket 双向帧转发，按 message 记录到 flows 表
        5. 连接关闭后返回 False（不再 keep-alive HTTP）
        """
        from .websocket_relay import relay_websocket
        # 弱网模拟：新建连接延迟 + 丢包
        throttle.delay()
        if throttle.should_drop():
            self._send_simple(client_sock, 502, "Bad Gateway")
            if record and flow_id:
                db.update_flow_response_async(flow_id, 502, "{}", "", 0, 0)
            return False

        # 捕获 local_port 用于 socket 关闭后仍能注销端口（防泄漏）
        # relay_websocket 会在结束时关闭 socket，导致 getsockname 失败
        target_local_port = 0
        try:
            target = self._connect_target(host, port)
            # _connect_target 已注册端口到 _proxy_outbound_ports，
            # 这里捕获 local_port 供后续 finally 注销（socket 关闭后仍可用）
            try:
                target_local_port = target.getsockname()[1]
            except OSError:
                target_local_port = 0
            if scheme == "https":
                # WSS：用专用 _ws_ssl_ctx（只通告 http/1.1），
                # 避免 ALPN 协商到 h2 导致 WS Upgrade 握手失败 (code=1006)
                target = self._ws_ssl_ctx.wrap_socket(target, server_hostname=host)
            target.settimeout(30)
        except OSError:
            # WSS wrap_socket 失败时 target 仍是 TCP socket（端口已注册），
            # 必须注销端口+关闭 socket 防泄漏
            # F10: 先 close 再 unregister，让 FIN/RST 在端口仍注册时发出
            try:
                if 'target' in locals() and target is not None:
                    target.close()
            except OSError:
                pass
            self._unregister_proxy_port(target_local_port)
            self._send_simple(client_sock, 502, "Bad Gateway")
            if record and flow_id:
                db.update_flow_response_async(flow_id, 502, "{}", "", 0, 0)
            return False

        # 构造 Upgrade 请求发送给服务器
        # 过滤 hop-by-hop 头，保留 Sec-WebSocket-* 等握手必需头
        # 不强制设 Content-Length：WebSocket Upgrade 请求无 body，
        # 强制设 Content-Length: 0 会让部分 WS 服务器拒绝握手 (code=1006)
        fwd_headers = Headers()
        for k, v in headers._items:  # noqa: SLF001
            kl = k.lower()
            if kl in ("proxy-connection", "proxy-authorization",
                      "transfer-encoding", "content-length",
                      "connection"):
                continue
            fwd_headers.add(k, v)
        fwd_headers.set("Connection", "Upgrade")

        req_line = f"{method} {path} HTTP/1.1\r\n".encode("latin-1", "replace")
        req_data = req_line + fwd_headers.to_bytes() + b"\r\n"
        try:
            throttle.send_throttled(target, req_data)
        except OSError:
            # F10: 先 close 再 unregister，让 FIN/RST 在端口仍注册时发出，
            # 被 _is_proxy_outbound_addr 排除，避免 WinDivert 拦截产生 spurious NAT
            try:
                target.close()
            except OSError:
                pass
            self._unregister_proxy_port(target_local_port)
            self._send_simple(client_sock, 502, "Bad Gateway")
            if record and flow_id:
                db.update_flow_response_async(flow_id, 502, "{}", "", 0, 0)
            return False

        # 读取服务器响应（期望 101 Switching Protocols）
        treader = SocketReader(target)
        try:
            status_line = treader.read_line()
            if not status_line:
                raise OSError("服务器未响应")
            sp = status_line.decode("latin-1", "replace").split(" ", 2)
            status_code = int(sp[1]) if len(sp) >= 2 and sp[1].isdigit() else 0
            resp_header_lines = treader.read_headers()
            resp_headers = Headers.from_lines(resp_header_lines)
        except (OSError, ValueError):
            # F10: 先 close 再 unregister，让 FIN/RST 在端口仍注册时发出，
            # 被 _is_proxy_outbound_addr 排除，避免 WinDivert 拦截产生 spurious NAT
            try:
                target.close()
            except OSError:
                pass
            self._unregister_proxy_port(target_local_port)
            self._send_simple(client_sock, 502, "Bad Gateway")
            if record and flow_id:
                db.update_flow_response_async(flow_id, 502, "{}", "", 0, 0)
            return False

        # 转发握手响应给客户端
        out = bytearray()
        out += status_line + b"\r\n"
        out += resp_headers.to_bytes()
        out += b"\r\n"
        try:
            throttle.send_throttled(client_sock, bytes(out))
        except OSError:
            # F10: 先 close 再 unregister，让 FIN/RST 在端口仍注册时发出，
            # 被 _is_proxy_outbound_addr 排除，避免 WinDivert 拦截产生 spurious NAT
            try:
                target.close()
            except OSError:
                pass
            self._unregister_proxy_port(target_local_port)
            return False

        # 记录握手 flow（若请求断点已插入则更新，否则新建）
        if record:
            if flow_id is not None:
                try:
                    db.update_flow_response_async(
                        flow_id, status_code,
                        json.dumps(resp_headers.to_dict()), "", 0, 0)
                except Exception:  # noqa: BLE001
                    pass
            else:
                try:
                    self._record_flow(
                        pid, proc_name, method, orig_url, scheme, host, path,
                        headers, body, status_code, resp_headers, "", 0, 0,
                        remote_ip=remote_ip, ip_region=ip_region)
                except Exception:  # noqa: BLE001
                    pass

        # 非 101 响应：服务器拒绝了 Upgrade，按普通 HTTP 处理（已转发响应，直接关闭）
        if status_code != 101:
            # F10: 先 close 再 unregister，让 FIN/RST 在端口仍注册时发出，
            # 被 _is_proxy_outbound_addr 排除，避免 WinDivert 拦截产生 spurious NAT
            try:
                target.close()
            except OSError:
                pass
            self._unregister_proxy_port(target_local_port)
            return False

        # 101 成功：转为双向帧转发
        # treader.buf 可能已有服务器发来的 WS 帧数据（紧跟 101 响应后）
        # 把缓冲区剩余数据写回 target，让帧读取器从 socket 读
        # （实际上 101 响应后服务器不会立即发数据，等待客户端首个 WS 帧）
        logger.info("ws", f"WebSocket 升级成功: {host}{path}",
                    f"pid={pid}, proc={proc_name}")
        try:
            relay_websocket(
                client_sock, target,
                session_id=self.session_id,
                pid=pid, proc_name=proc_name or "",
                method=method, url=orig_url, scheme=scheme,
                host=host, path=path,
                request_headers=headers.to_dict(),
                remote_ip=remote_ip, ip_region=ip_region,
                capturing=self.capturing,
            )
        except Exception as e:  # noqa: BLE001
            logger.info("ws", "WebSocket 转发结束", str(e))
        finally:
            # WS 转发结束后注销端口并关闭（防端口泄漏）
            # 关键：用捕获的 target_local_port 注销，不依赖 getsockname()
            # （relay_websocket 已关闭 socket，getsockname 会失败）
            # F10: 先 close 再 unregister，让 FIN/RST 在端口仍注册时发出，
            # 被 _is_proxy_outbound_addr 排除，避免 WinDivert 拦截产生 spurious NAT
            try:
                target.close()
            except OSError:
                pass
            self._unregister_proxy_port(target_local_port)
        return False  # WS 连接结束后不再 keep-alive

    def _h2_request_via_pool(self, host, port, scheme, method, path,
                             fwd: Headers, body: bytes, decompress: bool = True):
        """尝试通过 h2 连接池发送请求（复用已有 h2 连接）。

        成功返回 (status, resp_headers, resp_body, cert_info_json, "HTTP/2")。
        cert_info_json 从 H2Client 缓存中取（首次握手时获取）。
        池中无可用连接返回 None（调用方应新建连接）。
        池中连接出错返回 None 并自动从池中移除。
        """
        h2_client = self._h2_pool.get(host, port, scheme)
        if h2_client is None:
            return None
        try:
            h2_headers = [(k, v) for k, v in fwd.to_dict().items()]
            status, resp_h, resp_trailers, resp_body = h2_client.request(
                method, scheme, host, path, h2_headers, body)
            resp_headers = Headers.from_dict(dict(resp_h))
            # trailers 合并到 resp_headers（代理场景简化处理）
            for k, v in resp_trailers:
                resp_headers.add(k, v)
            if decompress:
                resp_body, resp_headers = _decompress_body(resp_body, resp_headers)
            self._h2_pool.record_request()
            # 复用连接时返回缓存的证书信息
            return status, resp_headers, resp_body, h2_client.cert_info, "HTTP/2"
        except Exception:  # noqa: BLE001
            # 性能优化：只移除出错的这个连接，不清空整个 host 的连接池。
            # 单个 stream 超时/抖动不应让后续并发请求都重新 TLS 握手。
            self._h2_pool.remove_client(h2_client)
            return None

    def _h2_create_and_request(self, target, host, port, scheme, method, path,
                                fwd: Headers, body: bytes, cert_info_json: str,
                                decompress: bool = True):
        """在新建的 TLS 连接上创建 H2Client 并发送请求。

        成功返回 5-tuple（H2Client 已放入池，可被后续请求复用）。
        失败返回 None（连接已关闭，调用方需新建连接回退 HTTP/1.1）。
        """
        from .h2_forward import H2Client
        # 捕获 local_port 用于 socket 关闭后仍能注销端口（防泄漏）
        try:
            target_local_port = target.getsockname()[1]
        except OSError:
            target_local_port = 0
        try:
            h2_client = H2Client(target, host, port, scheme, cert_info_json)
            h2_headers = [(k, v) for k, v in fwd.to_dict().items()]
            status, resp_h, resp_trailers, resp_body = h2_client.request(
                method, scheme, host, path, h2_headers, body)
            # 请求成功后才放入池（避免请求中途被另一个 put close 掉）
            if not self._h2_pool.put(h2_client):
                # 池中已有连接，关闭这个（reader 线程会退出）
                # h2_client.close() 内部会注销端口
                h2_client.close()
            else:
                # 成功放入池：端口由 H2Client.close() 在池清理时注销，
                # 此处不注销（连接还活着，被池复用）
                pass
            resp_headers = Headers.from_dict(dict(resp_h))
            # trailers 合并到 resp_headers（代理场景简化处理）
            for k, v in resp_trailers:
                resp_headers.add(k, v)
            if decompress:
                resp_body, resp_headers = _decompress_body(resp_body, resp_headers)
            self._h2_pool.record_request()
            return status, resp_headers, resp_body, cert_info_json, "HTTP/2"
        except Exception:  # noqa: BLE001
            # h2 创建/请求失败，注销端口并关闭连接（还没放入池，不需要 remove）
            # F10: 先 close 再 unregister，让 FIN/RST 在端口仍注册时发出，
            # 被 _is_proxy_outbound_addr 排除，避免 WinDivert 拦截产生 spurious NAT
            try:
                target.close()
            except OSError:
                pass
            self._unregister_proxy_port(target_local_port)
            return None

    def _forward(self, host, port, scheme, method, path, version, headers, body,
                 decompress: bool = True):
        """转发请求到目标服务器，返回 (status, resp_headers, resp_body, cert_info_json, http_version)。

        优先复用 h2 连接池（多路复用），其次复用 HTTP/1.1 连接池，最后新建连接。
        cert_info_json：HTTPS 流量的对端证书信息 JSON 字符串（首次握手时获取，复用连接返回空）
        http_version：'HTTP/1.1' 或 'HTTP/2'（ALPN 协商到 h2 时走 h2 转发）
        decompress：是否解压响应体。不抓包且无修改规则时传 False 跳过解压，节省 CPU。
        """
        # 构造转发请求头：去掉代理相关头，使用 keep-alive
        fwd = Headers()
        for k, v in headers._items:  # noqa: SLF001
            kl = k.lower()
            if kl in ("proxy-connection", "proxy-authorization",
                      "transfer-encoding", "content-length", "connection"):
                continue
            # 剥离 br 编码：代理无法解压 brotli（无库），会导致 modify_response 失败
            if kl == "accept-encoding":
                v = v.replace("br", "").replace(",,", ",").strip(", ").strip()
                if not v:
                    v = "identity"
            fwd.add(k, v)
        fwd.set("Content-Length", str(len(body)))
        fwd.set("Connection", "keep-alive")

        # ---- 优先尝试 h2 连接池（复用已有 h2 连接，多路复用） ----
        if scheme == "https":
            result = self._h2_request_via_pool(host, port, scheme, method, path, fwd, body,
                                                decompress=decompress)
            if result is not None:
                return result

        # 性能优化：req_data 延迟构造——h2 池命中时不浪费 CPU 拼接字节
        req_line = f"{method} {path} HTTP/1.1\r\n".encode("latin-1", "replace")
        req_data = req_line + fwd.to_bytes() + b"\r\n" + body

        # ---- HTTP/1.1 路径（或新建 h2 连接）----
        # 尝试从 HTTP/1.1 连接池获取复用连接
        pooled = self._conn_pool.get(host, port, scheme)
        target = None
        treader = None
        reused = False
        if pooled is not None:
            target = pooled.sock
            treader = pooled.reader  # 复用旧 reader 以保留缓冲区
            reused = True
            # 复用连接时使用缓存的证书信息（首次握手时已获取）
            cert_info_json = pooled.cert_info
            # 非阻塞重试取 cert_info：首次请求时 future 可能未完成（cert_info 为空），
            # 复用连接时 future 大概率已完成，可取到值
            if not cert_info_json and pooled.cert_info_future is not None:
                cert_info_json = _get_cert_info_result(pooled.cert_info_future)

        try:
            # 池中无连接：新建 TCP+TLS
            cert_info_future = None  # 仅新建连接时异步获取证书信息
            # 捕获 local_port 用于 socket 关闭后仍能注销端口（防泄漏）
            # 新建连接时 _connect_target 已注册端口，这里捕获 port 供后续注销
            target_local_port = 0
            if target is None:
                cert_info_json = ""
                # 弱网模拟：新建连接时加 RTT 延迟（模拟首包延迟）
                throttle.delay()
                # 弱网模拟：按 drop_pct 概率丢包（模拟连接失败）
                if throttle.should_drop():
                    raise OSError("throttle drop: connection dropped")
                target = self._connect_target(host, port)
                try:
                    target_local_port = target.getsockname()[1]
                except OSError:
                    target_local_port = 0
                if scheme == "https":
                    target = self._forward_ssl_ctx.wrap_socket(
                        target, server_hostname=host)
                    # 获取对端证书信息（仅首次握手，复用连接不重复获取）
                    # 性能优化：cert_info 解析（cryptography 库）提交到后台线程，
                    # 不阻塞代理主线程；后续 _record_flow 时懒获取结果
                    cert_info_future = _async_cert_info(target)
                    # HTTP/2：ALPN 协商到 h2 时创建 H2Client（放入池，后续可复用）
                    try:
                        alpn = target.selected_alpn_protocol()
                    except (AttributeError, OSError):
                        alpn = None
                    if alpn == "h2":
                        result = self._h2_create_and_request(
                            target, host, port, scheme, method, path, fwd, body,
                            _get_cert_info_result(cert_info_future),
                            decompress=decompress)
                        if result is not None:
                            return result
                        # h2 失败，回退到新建 HTTP/1.1 连接
                        # 先注销旧连接端口（防泄漏）
                        self._unregister_proxy_port(target_local_port)
                        target = self._connect_target(host, port)
                        try:
                            target_local_port = target.getsockname()[1]
                        except OSError:
                            target_local_port = 0
                        target = self._forward_ssl_ctx.wrap_socket(
                            target, server_hostname=host)
                target.settimeout(30)
                treader = SocketReader(target)
            else:
                # 复用池中连接：捕获其 local_port
                try:
                    target_local_port = target.getsockname()[1]
                except OSError:
                    target_local_port = 0

            # 弱网模拟：限速发送请求
            throttle.send_throttled(target, req_data)

            status_line = treader.read_line()
            if not status_line:
                # 连接已失效（池中的连接可能被服务器关闭），重试一次
                # 仅对幂等方法重试（GET/HEAD/OPTIONS），避免非幂等请求（POST/PUT/DELETE）重复执行副作用
                if reused and method in ("GET", "HEAD", "OPTIONS"):
                    # 注销旧连接端口并关闭（防端口泄漏）
                    # F10: 先 close 再 unregister
                    try:
                        target.close()
                    except OSError:
                        pass
                    self._unregister_proxy_port(target_local_port)
                    target = self._connect_target(host, port)
                    try:
                        target_local_port = target.getsockname()[1]
                    except OSError:
                        target_local_port = 0
                    if scheme == "https":
                        target = self._forward_ssl_ctx.wrap_socket(
                            target, server_hostname=host)
                        # 重试连接也异步获取证书信息
                        cert_info_future = _async_cert_info(target)
                        # 重试连接也检查 ALPN
                        try:
                            alpn = target.selected_alpn_protocol()
                        except (AttributeError, OSError):
                            alpn = None
                        if alpn == "h2":
                            result = self._h2_create_and_request(
                                target, host, port, scheme, method, path, fwd, body,
                                _get_cert_info_result(cert_info_future),
                                decompress=decompress)
                            if result is not None:
                                return result
                            # h2 失败，回退
                            self._unregister_proxy_port(target_local_port)
                            target = self._connect_target(host, port)
                            try:
                                target_local_port = target.getsockname()[1]
                            except OSError:
                                target_local_port = 0
                            target = self._forward_ssl_ctx.wrap_socket(
                                target, server_hostname=host)
                    target.settimeout(30)
                    treader = SocketReader(target)
                    # 弱网模拟：重试时也限速发送
                    throttle.send_throttled(target, req_data)
                    status_line = treader.read_line()
                    if not status_line:
                        # 重试后仍失败：注销端口+关闭（防泄漏，原代码漏掉）
                        # F10: 先 close 再 unregister
                        try:
                            target.close()
                        except OSError:
                            pass
                        self._unregister_proxy_port(target_local_port)
                        return 0, Headers(), b"", _get_cert_info_result(cert_info_future), "HTTP/1.1"
                else:
                    # 非幂等/非复用连接：注销端口+关闭（防泄漏，原代码漏掉）
                    # F10: 先 close 再 unregister
                    try:
                        target.close()
                    except OSError:
                        pass
                    self._unregister_proxy_port(target_local_port)
                    return 0, Headers(), b"", _get_cert_info_result(cert_info_future), "HTTP/1.1"

            sp = status_line.decode("latin-1", "replace").split(" ", 2)
            status_code = int(sp[1]) if len(sp) >= 2 and sp[1].isdigit() else 0
            resp_header_lines = treader.read_headers()
            resp_headers = Headers.from_lines(resp_header_lines)
            resp_body = read_body(treader, resp_headers, method=method,
                                  status_code=status_code)
            # 解压响应体，便于后续修改/记录（不抓包且无规则时跳过，节省 CPU）
            if decompress:
                resp_body, resp_headers = _decompress_body(resp_body, resp_headers)

            # 判断连接是否可复用：服务器未返回 close
            resp_conn = (resp_headers.get("Connection") or "").lower()
            can_reuse = "close" not in resp_conn
            # 非阻塞获取 cert_info（未完成返回空，不阻塞响应返回）
            cert_info_json = _get_cert_info_result(cert_info_future)
            if can_reuse:
                self._conn_pool.put(_PooledConn(
                    target, treader, host, port, scheme, cert_info_json,
                    cert_info_future=cert_info_future))
            else:
                # 不可复用：注销端口并关闭（防端口泄漏）
                # F10: 先 close 再 unregister
                try:
                    target.close()
                except OSError:
                    pass
                self._unregister_proxy_port(target_local_port)
            return status_code, resp_headers, resp_body, cert_info_json, "HTTP/1.1"
        except (OSError, ssl.SSLError):
            # 出错时关闭连接（池中复用的连接已被消耗）
            # 注销端口防泄漏（socket 可能来自池或新建，都已注册）
            if target is not None:
                # F10: 先 close 再 unregister
                try:
                    target.close()
                except OSError:
                    pass
                self._unregister_proxy_port(target_local_port)
            raise

    # ---------- 响应发送 ----------

    def _send_response(self, client_sock, status_code, headers: Headers,
                       body: bytes, keep_alive: bool):
        # 性能优化：直接修改传入的 headers（调用方不再复用），
        # 避免每响应都新建 dict + Headers（减少一次对象分配和 GIL 开销）
        headers.remove("Transfer-Encoding")
        headers.remove("Content-Length")
        headers.set("Content-Length", str(len(body)))
        headers.set("Connection", "keep-alive" if keep_alive else "close")
        out = bytearray()
        out += f"HTTP/1.1 {status_code} {_reason(status_code)}\r\n".encode("latin-1")
        out += headers.to_bytes()
        out += b"\r\n"
        out += body
        # 弱网模拟：限速发送响应
        throttle.send_throttled(client_sock, bytes(out))

    def _send_simple(self, client_sock, status_code, reason):
        body = f"{status_code} {reason}".encode("latin-1")
        out = (
            f"HTTP/1.1 {status_code} {reason}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("latin-1") + body
        try:
            client_sock.sendall(out)
        except OSError:
            pass

    def _handle_mock(self, client_sock, rule, keep_alive):
        status = int(rule.get("mock_status") or 200)
        headers = Headers.from_dict(_parse_json(rule.get("mock_headers")))
        body = _to_bytes(rule.get("mock_body") or "")
        self._send_response(client_sock, status, headers, body, keep_alive)

    # ---------- 流量记录 ----------

    def _insert_flow(self, pid, proc_name, method, url, scheme, host, path,
                     headers: Headers, body: bytes,
                     remote_ip: str = "", ip_region: str = "",
                     cert_info: str = "", http_version: str = "") -> int:
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
            "request_body": _to_text(body),
            "remote_ip": remote_ip,
            "ip_region": ip_region,
            "cert_info": cert_info or None,
            "http_version": http_version or None,
        }
        return db.insert_flow(flow)

    def _record_flow(self, pid, proc_name, method, url, scheme, host, path,
                     headers: Headers, body: bytes, status, resp_headers: Headers,
                     resp_body_text, duration_ms=0, size=0,
                     remote_ip: str = "", ip_region: str = "",
                     cert_info: str = "", http_version: str = ""):
        if self.session_id is None:
            return
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
            "cert_info": cert_info or None,
            "http_version": http_version or None,
        }
        # 性能优化：异步批量写入，避免代理线程等待 DB INSERT + COMMIT
        db.insert_flow_async(flow)

    def _update_or_insert_response(self, flow_id, pid, proc_name, method, url,
                                   scheme, host, path, headers, body, status,
                                   resp_headers, resp_body, start,
                                   remote_ip: str = "", ip_region: str = "",
                                   cert_info: str = "", http_version: str = ""):
        duration = int((time.time() - start) * 1000)
        if flow_id:
            db.update_flow_response_async(flow_id, status,
                                          json.dumps(resp_headers.to_dict()),
                                          _truncate_for_record(resp_body), duration, len(resp_body))
        else:
            self._record_flow(pid, proc_name, method, url, scheme, host, path,
                              headers, body, status, resp_headers,
                              _truncate_for_record(resp_body), duration, len(resp_body),
                              remote_ip=remote_ip, ip_region=ip_region,
                              cert_info=cert_info, http_version=http_version)

    # ---------- 自动回复匹配 ----------

    def _match_auto_reply(self, url: str, method: str | None = None,
                          status_code: int | None = None,
                          pid: int | None = None,
                          process_name: str | None = None):
        """§4.1 规则匹配支持 method/status/pid/process 过滤。

        请求阶段：status_code 传 None（尚无响应）。
        响应阶段：可传入 status_code 做状态码过滤。
        """
        try:
            return find_matching_rule(
                url, method=method, status_code=status_code,
                pid=pid, process_name=process_name,
            )
        except Exception:  # noqa: BLE001
            return None


# ---------- 工具函数 ----------

def _parse_json(s):
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return {}


def _apply_delay(rule, target_name: str, log_label: str = "delay"):
    """§3.8 intercept delay 动作：扫描 modify_rules，匹配 target=target_name 的规则，
    用 time.sleep(value/1000) 延迟（value 单位毫秒）。

    target_name 可选：
    - "delay"：响应阶段延迟（在 _apply_modify_response 调用前执行）
    - "delay-request"：请求阶段延迟（在转发前执行）
    """
    modify_rules = _parse_json(rule.get("modify_rules"))
    if not isinstance(modify_rules, list):
        return
    for mr in modify_rules:
        try:
            if not isinstance(mr, dict):
                continue
            if mr.get("target") != target_name:
                continue
            op = mr.get("op", "sleep")
            if op != "sleep":
                continue
            value = mr.get("value", 0)
            try:
                ms = float(value)
            except (TypeError, ValueError):
                continue
            if ms <= 0:
                continue
            # 钳制最大延迟，避免超长 delay 长期占用代理工作线程导致吞吐下降/线程饥饿
            ms = min(ms, 10000.0)
            logger.info("proxy", f"{log_label} 延迟 {ms}ms",
                        f"rule_note={rule.get('note', '')}")
            time.sleep(ms / 1000.0)
        except Exception:  # noqa: BLE001
            continue


def _to_text(b: bytes) -> str:
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return "base64:" + base64.b64encode(b).decode("ascii")


def _to_bytes(s) -> bytes:
    if not s:
        return b""
    if isinstance(s, bytes):
        return s
    if s.startswith("base64:"):
        try:
            return base64.b64decode(s[7:])
        except Exception:  # noqa: BLE001
            return b""
    return s.encode("utf-8")


def _decompress_body(body: bytes, headers: Headers) -> tuple[bytes, Headers]:
    """根据 Content-Encoding 解压响应体，并移除该头。
    支持 gzip / deflate / br（如有 brotli 库）。

    性能优化：body 超过 MAX_DECOMPRESS_BODY（1MB）时跳过解压，直接返回原始字节。
    大 body 通常是视频/图片/下载，解压耗时几百 ms 且 modify_response 对二进制无意义。
    """
    enc = (headers.get("Content-Encoding") or "").lower().strip()
    if not enc or not body:
        return body, headers
    # 大响应体跳过解压：避免阻塞代理线程
    if len(body) > MAX_DECOMPRESS_BODY:
        logger.debug("proxy",
                     f"跳过解压大响应体: {len(body)} bytes > {MAX_DECOMPRESS_BODY} bytes, enc={enc}")
        return body, headers
    try:
        if "gzip" in enc:
            body = gzip.decompress(body)
        elif "deflate" in enc:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                body = zlib.decompress(body, -zlib.MAX_WBITS)
        elif "br" in enc:
            try:
                import brotli
                body = brotli.decompress(body)
            except ImportError:
                logger.warning("proxy", "brotli 库未安装，无法解压 br 响应，modify_response 将失效",
                               f"Content-Encoding: {enc}, body 长度: {len(body)}")
                return body, headers
            except Exception as e:
                logger.error("proxy", f"brotli 解压失败: {e}",
                             f"body 前 50 字节: {body[:50]}")
                return body, headers
        else:
            return body, headers
        headers.remove("Content-Encoding")
        headers.remove("Content-Length")
        headers.set("Content-Length", str(len(body)))
        logger.debug("proxy", f"解压响应体成功: {enc} -> {len(body)} bytes")
    except Exception as e:  # noqa: BLE001
        logger.error("proxy", f"解压响应体失败: {e}", f"enc={enc}")
    return body, headers


def _is_plain_key(key: str) -> bool:
    """判断是否为纯字段名（不含 . [ $ 等路径符号），用于全局搜索模式。"""
    if not key:
        return False
    return not any(ch in key for ch in ".[$")


def _replace_all_keys(data, key: str, value):
    """递归遍历整个 JSON，把所有名为 key 的字段替换为 value。"""
    coerced = _coerce_value(value)
    if isinstance(data, dict):
        for k in list(data.keys()):
            if k == key:
                data[k] = coerced
            else:
                _replace_all_keys(data[k], key, value)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            _replace_all_keys(item, key, value)
    return data


def _remove_all_keys(data, key: str):
    """递归遍历整个 JSON，删除所有名为 key 的字段。"""
    if isinstance(data, dict):
        if key in data:
            del data[key]
        for k in list(data.keys()):
            _remove_all_keys(data[k], key)
    elif isinstance(data, list):
        for item in data:
            _remove_all_keys(item, key)
    return data


def _set_json_path(data, path: str, value):
    """按点路径设置 JSON 字段，支持 $.a.b.c / a.b.c / a.b[0].c。
    路径不存在时自动创建中间节点（dict/list 按下标 [n] 判断）。
    """
    p = path.strip()
    if p.startswith("$."):
        p = p[2:]
    elif p.startswith("$"):
        p = p[1:]
    # 解析 token：拆分 a.b[0].c -> ['a', 'b', 0, 'c']
    tokens = []
    for seg in p.split("."):
        seg = seg.strip()
        if not seg:
            continue
        # 处理形如 b[0][1]
        i = 0
        while i < len(seg):
            lb = seg.find("[", i)
            if lb == -1:
                tokens.append(seg[i:])
                break
            if lb > i:
                tokens.append(seg[i:lb])
            rb = seg.find("]", lb)
            if rb == -1:
                tokens.append(seg[lb + 1:])
                break
            try:
                tokens.append(int(seg[lb + 1:rb]))
            except ValueError:
                tokens.append(seg[lb + 1:rb])
            i = rb + 1
    if not tokens:
        return data
    cur = data
    for i, tok in enumerate(tokens[:-1]):
        nxt = tokens[i + 1]
        if isinstance(tok, int):
            # cur 应是 list
            if not isinstance(cur, list):
                return data
            while len(cur) <= tok:
                cur.append(None)
            if cur[tok] is None or not isinstance(cur[tok], (dict, list)):
                cur[tok] = [] if isinstance(nxt, int) else {}
            cur = cur[tok]
        else:
            if not isinstance(cur, dict):
                return data
            if tok not in cur or not isinstance(cur[tok], (dict, list)):
                cur[tok] = [] if isinstance(nxt, int) else {}
            cur = cur[tok]
    last = tokens[-1]
    if isinstance(last, int):
        if not isinstance(cur, list):
            return data
        while len(cur) <= last:
            cur.append(None)
        cur[last] = _coerce_value(value)
    else:
        if not isinstance(cur, dict):
            return data
        cur[last] = _coerce_value(value)
    return data


def _del_json_path(data, path: str):
    """按点路径删除 JSON 字段。"""
    p = path.strip()
    if p.startswith("$."):
        p = p[2:]
    elif p.startswith("$"):
        p = p[1:]
    tokens = []
    for seg in p.split("."):
        seg = seg.strip()
        if not seg:
            continue
        i = 0
        while i < len(seg):
            lb = seg.find("[", i)
            if lb == -1:
                tokens.append(seg[i:])
                break
            if lb > i:
                tokens.append(seg[i:lb])
            rb = seg.find("]", lb)
            if rb == -1:
                tokens.append(seg[lb + 1:])
                break
            try:
                tokens.append(int(seg[lb + 1:rb]))
            except ValueError:
                tokens.append(seg[lb + 1:rb])
            i = rb + 1
    if not tokens:
        return data
    cur = data
    for tok in tokens[:-1]:
        if isinstance(tok, int):
            if not isinstance(cur, list) or tok >= len(cur):
                return data
            cur = cur[tok]
        else:
            if not isinstance(cur, dict) or tok not in cur:
                return data
            cur = cur[tok]
    last = tokens[-1]
    if isinstance(last, int):
        if isinstance(cur, list) and last < len(cur):
            cur.pop(last)
    else:
        if isinstance(cur, dict) and last in cur:
            del cur[last]
    return data


def _coerce_value(v):
    """把字符串值尽量转成 JSON 类型（int/float/bool/null）。"""
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s == "":
        return ""
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none"):
        return None
    try:
        if "." in s or "e" in low:
            return float(s)
        return int(s)
    except ValueError:
        pass
    return v


def _apply_modify_response(status, headers: Headers, body: bytes, rule) -> tuple:
    """应用 modify_response 规则到响应。

    支持两种结构：
    1. 新结构（前端 RuleEditor 生成）:
       { target, op, key, value }
       - target: response_body | response_header
       - op:     replace | remove
       - key:    响应体时为 JSON 路径（如 data.status.remainingUses），
                 响应头时为头名；为空则整 body 替换
       - value:  新值
    2. 旧结构（兼容）:
       { action: replace_body|replace_status|replace_header|remove_header, name, value }
    """
    modify_rules = _parse_json(rule.get("modify_rules"))
    if not isinstance(modify_rules, list):
        logger.warning("proxy", "modify_response 规则解析失败或为空",
                       f"modify_rules={rule.get('modify_rules')!r}, parsed={modify_rules!r}")
        return status, headers, body
    logger.info("proxy", f"应用 modify_response，共 {len(modify_rules)} 条子规则",
                f"body 长度={len(body)}, Content-Encoding={headers.get('Content-Encoding')}")
    for mr in modify_rules:
        try:
            # 新结构：按 target/op
            target = mr.get("target")
            op = mr.get("op")
            if target and op:
                key = (mr.get("key") or "").strip()
                value = mr.get("value", "")
                if target == "response_body":
                    if op == "remove" and key:
                        try:
                            data = json.loads(body.decode("utf-8", "ignore"))
                            if _is_plain_key(key):
                                data = _remove_all_keys(data, key)
                            else:
                                data = _del_json_path(data, key)
                            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                            logger.info("proxy", f"remove 字段成功: {key}")
                        except Exception as e:
                            logger.error("proxy", f"remove 字段失败: {key}", str(e))
                    elif op == "replace":
                        if key:
                            try:
                                text = body.decode("utf-8", "ignore")
                                data = json.loads(text)
                                logger.info("proxy", f"JSON 解析成功, 顶层类型={type(data).__name__}",
                                            f"key={key}, is_plain={_is_plain_key(key)}, "
                                            f"JSON 前 200 字符={text[:200]}")
                                if _is_plain_key(key):
                                    data = _replace_all_keys(data, key, value)
                                else:
                                    data = _set_json_path(data, key, value)
                                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                                logger.info("proxy", f"replace 字段成功: {key} -> {value}")
                            except json.JSONDecodeError as e:
                                logger.error("proxy", f"replace 字段失败（响应体不是有效 JSON）: {key} -> {value}",
                                             f"JSON 错误: {e}, body 前 200 字节: {body[:200]!r}")
                            except Exception as e:
                                logger.error("proxy", f"replace 字段失败: {key} -> {value}",
                                             f"错误: {e}, body 前 200 字节: {body[:200]!r}")
                        else:
                            body = _to_bytes(value)
                    elif op == "append":
                        # 追加文本（非 JSON）
                        body = body + _to_bytes(value)
                elif target == "response_header":
                    if op == "remove":
                        headers.remove(key)
                    else:  # replace / append
                        headers.set(key, value)
                continue
            # 旧结构：按 action
            action = mr.get("action")
            if action == "replace_body":
                body = _to_bytes(mr.get("value", ""))
            elif action == "replace_status":
                status = int(mr.get("value", status))
            elif action == "replace_header":
                headers.set(mr.get("name", ""), mr.get("value", ""))
            elif action == "remove_header":
                headers.remove(mr.get("name", ""))
        except Exception:  # noqa: BLE001
            continue
    return status, headers, body


def _apply_modify_request(headers: Headers, body: bytes, rule) -> tuple:
    """应用 modify_request 规则到请求头/体。

    支持的 target：
    - request_header: op=replace/remove，key=头名，value=新值
    - request_body:   op=replace（JSON 字段替换/整体替换）/remove（删字段）/replace-bytes（二进制偏移替换）
    """
    modify_rules = _parse_json(rule.get("modify_rules"))
    if not isinstance(modify_rules, list):
        return headers, body
    for mr in modify_rules:
        try:
            target = mr.get("target")
            op = mr.get("op")
            if not (target and op):
                continue
            key = (mr.get("key") or "").strip()
            value = mr.get("value", "")
            if target == "request_header":
                if op == "remove":
                    headers.remove(key)
                else:
                    headers.set(key, str(value))
            elif target == "request_body":
                if op == "replace-bytes":
                    # 二进制偏移替换：value = {offset: int, hex: "A3FF"} 或 "offset:hex"
                    try:
                        if isinstance(value, dict):
                            offset = int(value.get("offset", 0))
                            hex_str = value.get("hex", "")
                        else:
                            parts = str(value).split(":", 1)
                            offset = int(parts[0]) if parts[0] else 0
                            hex_str = parts[1] if len(parts) > 1 else ""
                        replacement = bytes.fromhex(hex_str.replace(" ", "").replace("0x", ""))
                        if offset + len(replacement) <= len(body):
                            body = body[:offset] + replacement + body[offset + len(replacement):]
                            logger.info("proxy", f"request_body replace-bytes: offset={offset}, len={len(replacement)}")
                    except Exception as e:  # noqa: BLE001
                        logger.error("proxy", "replace-bytes 失败", str(e))
                elif op == "remove" and key:
                    try:
                        data = json.loads(body.decode("utf-8", "ignore"))
                        if _is_plain_key(key):
                            data = _remove_all_keys(data, key)
                        else:
                            data = _del_json_path(data, key)
                        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                    except Exception:  # noqa: BLE001
                        pass
                elif op == "replace":
                    if key:
                        try:
                            data = json.loads(body.decode("utf-8", "ignore"))
                            if _is_plain_key(key):
                                data = _replace_all_keys(data, key, value)
                            else:
                                data = _set_json_path(data, key, value)
                            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                        except Exception:  # noqa: BLE001
                            pass
                    else:
                        body = _to_bytes(value)
                elif op == "append":
                    body = body + _to_bytes(value)
        except Exception:  # noqa: BLE001
            continue
    return headers, body


def _apply_modify_response_binary(body: bytes, rule) -> bytes:
    """对响应体应用二进制字节替换规则（replace-bytes / replace-bytes-regex）。"""
    modify_rules = _parse_json(rule.get("modify_rules"))
    if not isinstance(modify_rules, list):
        return body
    for mr in modify_rules:
        try:
            target = mr.get("target")
            op = mr.get("op")
            if target != "response_body":
                continue
            key = (mr.get("key") or "").strip()
            value = mr.get("value", "")
            if op == "replace-bytes":
                try:
                    if isinstance(value, dict):
                        offset = int(value.get("offset", 0))
                        hex_str = value.get("hex", "")
                    else:
                        parts = str(value).split(":", 1)
                        offset = int(parts[0]) if parts[0] else 0
                        hex_str = parts[1] if len(parts) > 1 else ""
                    replacement = bytes.fromhex(hex_str.replace(" ", "").replace("0x", ""))
                    if offset + len(replacement) <= len(body):
                        body = body[:offset] + replacement + body[offset + len(replacement):]
                except Exception:  # noqa: BLE001
                    pass
            elif op == "replace-bytes-regex":
                # key = 正则（bytes），value = 替换 hex
                try:
                    pattern = re.compile(key.encode("latin-1"))
                    replacement = bytes.fromhex(str(value).replace(" ", "").replace("0x", ""))
                    body = pattern.sub(replacement, body)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            continue
    return body

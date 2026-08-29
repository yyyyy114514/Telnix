"""HTTP/HTTPS capture proxy server (thread-based model).

One thread per client connection. HTTP is parsed and forwarded directly; HTTPS
(CONNECT tunnel) is decrypted via SSL bump with dynamically issued certificates.
Each connection reverse-looks-up the PID via GetExtendedTcpTable.
Breakpoints block at the proxy layer waiting for API release.
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
from ..ip_region import lookup as ip_region_lookup, lookup_with_asn as ip_region_lookup_with_asn
from .breakpoint import BreakpointManager
from .process_lookup import ProcessLookup
from .ssl_bump import SSLBumpManager
from . import throttle
from . import proxy_tools
from ..auto_reply.hit_tracker import record_hit as record_rule_hit


def _get_proxy_threshold(key: str, default: int) -> int:
    """Read proxy threshold from settings_store (allows runtime configuration).

    Design fix: moved from hardcoded constants to settings_store.
    """
    try:
        from .. import settings_store
        v = settings_store.get_setting(key, default)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    except Exception:  # noqa: BLE001
        pass
    return default


# 记录到 DB 的 body 上限（字节）。超过则截断并加标记，避免大 body 阻塞代理线程
# （视频/图片流可能几 MB ~ 几十 MB，base64 编码后 3x 膨胀 + DB INSERT 慢）
MAX_RECORDED_BODY = _get_proxy_threshold("max_recorded_body", 512 * 1024)  # 512KB
MAX_STREAM_BODY = _get_proxy_threshold("max_stream_body", 256 * 1024 * 1024)  # 256MB

# SSLContext 缓存上限（按 cert_path 缓存，LRU 淘汰最久未用）
# 性能优化：避免长跑场景下不同 host 的证书无限累积导致内存增长
_SSL_CTX_CACHE_MAX = _get_proxy_threshold("ssl_ctx_cache_max", 150)

# SSL bump 失败 host 的 TTL（秒）：超过此时间自动允许重试 do_bump
# 避免一次握手失败后该 host 被永久降级为纯隧道（证书可能已重新安装或应用重启）
_SSL_BUMP_FAILED_TTL = _get_proxy_threshold("ssl_bump_failed_ttl", 300.0)

# 大响应体阈值：超过此大小跳过解压，直接转发原始字节。
# 性能优化：解压几百 KB 的 gzip/br 需几十~几百 ms，阻塞代理线程。
# 大 body 通常是视频/图片/下载，modify_response 规则对二进制内容无意义。
# 仍会记录前 512KB（截断）用于 Inspector 查看，但不解压。
MAX_DECOMPRESS_BODY = _get_proxy_threshold("max_decompress_body", 256 * 1024)  # 256KB
# 解压炸弹防御：限制解压后的最大输出。gzip/deflate 压缩比可达 1000x，
# 256KB 压缩输入可解出数百 MB。超过此上限视为解压炸弹，放弃解压返回原始字节。
MAX_DECOMPRESS_OUTPUT = _get_proxy_threshold("max_decompress_output", 64 * 1024 * 1024)  # 64MB


# 性能优化（v11）：跳过规则匹配的 throttle 日志
# 避免每请求都记录"跳过规则匹配"，每 60s 最多记录一次 DEBUG 日志
_skip_rule_match_count: int = 0
_skip_rule_match_last_log_ts: float = 0.0
_SKIP_LOG_INTERVAL: float = 60.0  # 秒


def _log_skip_rule_match(reason: str):
    """Throttle logging of 'skipped rule matching' (at most one DEBUG per 60s).

    Thread-safety note: _skip_rule_match_count += 1 is not strictly atomic under GIL,
    but occasionally losing a few log counts doesn't matter and isn't worth a lock.
    """
    global _skip_rule_match_count, _skip_rule_match_last_log_ts
    now = time.time()
    _skip_rule_match_count += 1
    if now - _skip_rule_match_last_log_ts >= _SKIP_LOG_INTERVAL:
        logger.debug(
            "proxy",
            f"[auto-reply] {reason}, skipped rule matching",
            f"Total {_skip_rule_match_count} requests skipped in last {_SKIP_LOG_INTERVAL:.0f}s"
        )
        _skip_rule_match_count = 0
        _skip_rule_match_last_log_ts = now


def _truncate_for_record(b: bytes) -> str:
    """Convert body bytes to recordable text, truncating beyond MAX_RECORDED_BODY.

    Performance notes:
    - Limit base64 expansion scope (only encode the truncated bytes)
    - Limit utf-8 decode scope (avoid calling decode on multi-MB bytes)
    """
    if not b:
        return ""
    original_size = len(b)
    truncated = False
    if len(b) > MAX_RECORDED_BODY:
        b = b[:MAX_RECORDED_BODY]
        truncated = True
    try:
        text = b.decode("utf-8")
        if truncated:
            text += f"\n\n[... body truncated, original size {original_size} bytes, only first {MAX_RECORDED_BODY} bytes recorded ...]"
        return text
    except UnicodeDecodeError:
        # 二进制内容（图片/视频等）：base64 编码
        # 注意：不要在 base64 字符串后追加文本标记，否则会破坏 base64 解码
        # 导致前端 data URL 无效、图片无法渲染
        return "base64:" + base64.b64encode(b).decode("ascii")


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
    """Asynchronously fetch peer certificate info, returns a Future (lazy result).

    Submits getpeercert + get_cert_info (cryptography parsing + SHA256) to a background
    thread to avoid blocking the proxy main thread. Callers lazily retrieve the result
    via _get_cert_info_result.
    """
    try:
        return _CERT_INFO_EXECUTOR.submit(_do_cert_info, target_sock)
    except Exception:  # noqa: BLE001
        return None


def _do_cert_info(target_sock) -> str:
    """Perform certificate info parsing in a worker thread."""
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
    """Non-blocking retrieval of the cert_info Future result.

    Performance: the original timeout=5.0 would block the proxy main thread for up
    to 5 seconds waiting for certificate parsing — the core culprit behind "packets
    appear 5 seconds after page load". cert_info is secondary info (certificate
    details) and should not block the response. Now non-blocking: returns empty if
    not done; retries on connection reuse.
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
    """Perform DNS resolution + IP region lookup in a worker thread, returns (remote_ip, ip_region)."""
    remote_ip = ""
    try:
        # 限制为 IPv4（ip2region_v4.xdb 仅支持 IPv4；getaddrinfo 默认可能返回 IPv6 地址，
        # 导致 _is_private_ip 将 IPv6 格式当作"内网"处理，属地始终为空）
        infos = socket.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
        if infos:
            remote_ip = infos[0][4][0]
    except Exception:  # noqa: BLE001
        pass
    ip_region = ""
    if remote_ip:
        try:
            # 优先使用带 ASN 的属地查询；ASN 数据文件缺失时自动降级为普通属地
            ip_region = ip_region_lookup_with_asn(remote_ip)
        except Exception:  # noqa: BLE001
            try:
                ip_region = ip_region_lookup(remote_ip)
            except Exception:  # noqa: BLE001
                pass
    return remote_ip, ip_region


# ---------- 到目标服务器的连接池 ----------

def _close_pooled_sock(sock: socket.socket):
    """Close a pooled connection and unregister its local port.

    Transparent proxy anti-loop relies on the _proxy_outbound_ports set to exclude
    the proxy's own outbound traffic. After a connection enters the pool, its local
    port remains in the set; if the connection is discarded/closed without unregistering,
    the port number may be reused by the OS for a non-proxy socket, causing WinDivert
    to incorrectly exclude that socket's traffic (port leak). This function unifies
    close + unregister to avoid leaks.
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
    """A reusable connection to the target server.

    Stores the reader rather than the raw sock, to preserve any over-read data in
    the SocketReader buffer.
    cert_info: certificate info JSON obtained during the first TLS handshake; returned
    directly on connection reuse (avoids repeated getpeercert).
    cert_info_future: Future for async certificate parsing on new connections; retried
    non-blockingly on reuse (on first request the future may be incomplete and
    cert_info is empty; on reuse it's likely done and the value can be retrieved).
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
    """Simple keep-alive connection pool, caches connections by (host, port, scheme).

    Each connection auto-expires after 120 seconds idle. Each key caches at most
    32 connections.

    Performance: sharded locks (16 buckets); connections of different hosts can
    get/put in parallel, avoiding single-lock serialization under high concurrency
    (30 domains × 6 connections = 180 threads).
    """
    _MAX_IDLE = 120.0  # 秒（60→120，匹配主流 keep-alive 超时，提升复用率）
    _MAX_PER_KEY = 64  # 性能优化：从 32 增大到 64，避免高并发下连接池耗尽导致新建连接
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
    """Case-insensitive header container preserving original order and case.

    Performance: the _index dict provides O(1) lookup by name, avoiding O(n)
    scans of _items with .lower() on each name for every get/set/has/remove.
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
    """Compile a wildcard pattern to a precompiled regex: * → .*, ? → ., case-insensitive. Returns None on failure."""
    if not pattern:
        return None
    try:
        regex_str = '^' + re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.') + '$'
        return re.compile(regex_str, re.IGNORECASE)
    except re.error:
        return None


class SocketReader:
    """Buffered socket reader supporting line-based and exact-length reads."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()

    def _fill(self):
        chunk = self.sock.recv(262144)
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
                chunk = self.sock.recv(262144)
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
    """Read the message body based on headers.

    Performance: MAX_BODY_SIZE upper bound protection; truncates and marks beyond it
    (avoids large body OOM + GC pressure).
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


# 流式媒体内容类型前缀：命中则直接流式转发，避免 read_body 全量缓冲
# 导致播放器等待首字节超时（视频/音频/HLS/DASH 等）
_STREAMING_CONTENT_TYPE_PREFIXES = (
    "video/",
    "audio/",
    "application/octet-stream",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
)


def _is_streaming_content(headers: Headers, status_code: int) -> bool:
    """判断响应是否应流式转发给客户端（视频/音频/大媒体）。

    流式转发跳过 read_body 全量缓冲，直接把目标返回的字节块转发给客户端，
    避免播放器等待首字节超时。命中条件（任一即可）：
    - Content-Type 为 video/ audio/ application/octet-stream
      application/vnd.apple.mpegurl application/x-mpegURL
    - Transfer-Encoding 为 chunked 且无 Content-Length
    - Content-Type 为流式媒体且存在 Accept-Ranges 头（支持 Range 请求的可拖动媒体）
    """
    # HEAD/204/304/1xx 无 body，无需流式
    if status_code is not None and (
            status_code in (204, 304) or 100 <= status_code < 200):
        return False
    ct = (headers.get("Content-Type") or "").lower()
    if ct.startswith(_STREAMING_CONTENT_TYPE_PREFIXES):
        return True
    te = (headers.get("Transfer-Encoding") or "").lower()
    if "chunked" in te and headers.get("Content-Length") is None:
        return True
    # Accept-Ranges 需结合 Content-Type 判断，否则普通响应（带 Accept-Ranges: bytes）
    # 也会被误判为流式，导致 body 无法记录
    if headers.has("Accept-Ranges") and ct.startswith(_STREAMING_CONTENT_TYPE_PREFIXES):
        return True
    return False


# 流式媒体请求的 URL 后缀（命中则跳过 h2，强制走 HTTP/1.1 流式转发）
_MEDIA_URL_EXTS = (
    ".mp4", ".m3u8", ".ts", ".flv", ".webm", ".ogg", ".mpd", ".m4s",
    ".mkv", ".avi", ".mov", ".wav", ".mp3", ".aac", ".m4a", ".m4v",
    ".fmp4", ".cmfv", ".dash",
)

def _is_media_request(headers: Headers, path: str, host: str = "") -> bool:
    """判断请求是否可能是流式媒体（视频/音频），应在请求阶段跳过 h2 全量缓冲。

    h2 的 request() 方法会全量缓冲响应体（stream.body += event.data），
    对视频/音频等流式内容会导致播放器首字节超时。
    命中条件的请求强制走 HTTP/1.1 路径（已有 _stream_to_client 流式转发）。

    注意：不再基于 host 黑名单判断（已移除 _MEDIA_HOSTS 设计），
    确保所有 HTTPS 流量都能被 SSL Bump 解密。仅通过 Accept/Range/URL 后缀
    判断流式媒体，避免误伤正常站点的解密。
    """
    # Accept 头检查
    accept = (headers.get("Accept") or "").lower()
    if "video/" in accept or "audio/" in accept:
        return True
    # Range 请求常用于视频拖动进度
    if headers.get("Range"):
        return True
    # URL 后缀检查（去掉 query string）
    path_lower = path.lower().split("?")[0]
    if path_lower.endswith(_MEDIA_URL_EXTS):
        return True
    return False


# ---------- 代理服务器 ----------

class ProxyServer:
    """HTTP/HTTPS capture proxy server (thread-based model)."""

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
        # 性能修复(审计 P-#5)：mtime 检查节流缓存，cert_path -> (last_check_ts, mtime)
        # 5s 内复用上次 mtime，避免每次 CONNECT 都 os.path.getmtime 系统调用
        self._ssl_mtime_cache: dict[str, tuple[float, float]] = {}
        # 客户端连接并发信号量（防止无限制创建线程导致 OOM / GIL 严重争用）
        # 设计修复：从硬编码 500 改为从 settings_store 读取，支持用户调整
        client_sem_limit = _get_proxy_threshold("client_sem_limit", 500)
        self._client_sem = threading.Semaphore(client_sem_limit)
        # 请求阶段预入库线程池：将同步 DB 写从转发关键路径剥离，
        # 代理线程提交后立即继续，响应阶段再取回 flow_id 做更新。
        # SQLite WAL 模式同一时刻仅允许 1 个 writer，过多 worker 反而因锁等待
        # 增加延迟；2 个 worker 足够（1 个正在写，1 个就绪），减少锁争用。
        # 设计修复：从硬编码 2 改为从 settings_store 读取，支持用户调整
        preinsert_workers = _get_proxy_threshold("preinsert_workers", 2)
        self._preinsert_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=preinsert_workers, thread_name_prefix="flow-preinsert")
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
        """Get the current upstream proxy address. Prefers runtime override, otherwise reads from settings."""
        if self._upstream_proxy_override is not None:
            return self._upstream_proxy_override
        try:
            return get_upstream_proxy()
        except Exception:  # noqa: BLE001
            return None

    def _connect_target(self, host: str, port: int, timeout: int = 30) -> socket.socket:
        """Establish a connection to the target server. When Clash is enabled, goes through
        the upstream proxy via a CONNECT tunnel.

        Performance optimizations:
        - TCP_NODELAY disables Nagle's algorithm, reducing small-packet latency
        - CONNECT response reading uses a separate timeout (10s), avoiding long hangs
          when the proxy is misbehaving
        - Caching is provided by clash.client.get_upstream_proxy(), avoiding file reads each time

        Transparent proxy anti-loop (key, two-phase registration):
        - Phase 1 (before connect): after bind, getsockname() obtains the port number and
          registers it in _proxy_outbound_ports (port only; IP is 0.0.0.0 and unusable at
          this point). Ensures the port is registered before SYN is sent, preventing WinDivert
          from intercepting the proxy's own SYN and forming an infinite loop (TOCTOU race fix).
        - Phase 2 (after connect): getsockname() obtains the real source IP and registers
          (ip, port) in _proxy_outbound_addrs. Reduces the window where pure port matching
          incorrectly excludes client traffic.
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
                    raise OSError(f"Upstream proxy closed connection: {proxy_host}:{proxy_port}")
                buf += chunk
            status_line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            if " 200 " not in status_line:
                raise OSError(f"Upstream proxy refused CONNECT {host}:{port}: {status_line}")
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
        """Register a proxy outbound port (Phase 1, called before connect).

        Only registers the port into _proxy_outbound_ports, not (ip, port).
        Reason: before connect, getsockname() returns ('0.0.0.0', port); the IP is
        a wildcard, and registering it in _proxy_outbound_addrs would create dead
        data that never matches real outbound packets, and would make
        _proxy_outbound_addrs non-empty thus disabling port fallback (a fatal bug
        in the old version). Port matching provides anti-loop protection during
        the connect window.
        """
        if not local_port:
            return
        try:
            from .transparent_proxy import register_proxy_port
            register_proxy_port(local_port)
        except Exception:  # noqa: BLE001
            pass

    def _register_proxy_socket_addr(self, s: socket.socket, local_port: int):
        """Register the proxy outbound (src_ip, src_port) tuple (Phase 2, called after connect).

        After connect, getsockname() returns the real source IP (chosen by routing);
        at this point registering (ip, port) into _proxy_outbound_addrs enables precise
        matching, reducing the window where pure port matching incorrectly excludes
        client traffic. The Phase 1 port registration is retained (not removed here);
        it is cleaned up uniformly by _unregister_proxy_port.
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
        """Unregister a proxy outbound port by port number (does not depend on socket;
        usable after socket close).

        unregister_proxy_port clears both _proxy_outbound_ports and all entries in
        _proxy_outbound_addrs ending with that port, so it correctly cleans up
        regardless of whether Phase 1 or Phase 2 registration occurred.
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
        """Prewarm: force-trigger various first-call costs in a background thread to avoid
        slowing down the first batch of requests with cold-start latency.

        Main prewarm items (from most to least expensive):
        - cryptography library first-load OpenSSL backend (cffi binding libcrypto/libssl, 500ms-2s)
        - ip2region 11MB data load (200-500ms)
        - h2 library first import (50-100ms)
        - psutil first process enumeration init (50-200ms)
        - DB connection + background writer thread first start
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
            # 触发 s.search(ip) 首次调用，避免首批抓包请求 IP 属地查询冷启动
            try:
                ip_region.lookup("8.8.8.8")
            except Exception:  # noqa: BLE001
                pass
            # 预加载 GeoLite2-ASN.mmdb（若存在），避免首批请求 ASN 查询冷启动
            try:
                ip_region._get_asn_reader()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
        try:
            # 2.1 触发系统 DNS 解析器首次初始化（Windows 5-20ms），首批请求不再承担
            socket.getaddrinfo("localhost", 80)
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
        # 关闭预入库线程池（不等待任务完成，避免 stop 被阻塞）
        try:
            self._preinsert_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass

    def _accept_loop(self):
        while self._running:
            try:
                client_sock, client_addr = self._server_socket.accept()
            except OSError:
                break
            client_sock.settimeout(60)
            # 获取信号量，限制最大并发连接数（防止线程无限增长）
            if not self._client_sem.acquire(timeout=5):
                logger.warning("proxy", "client_sem full, rejecting connection",
                               f"client={client_addr}, concurrent_limit=200")
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
        """Client handling wrapper with semaphore release."""
        try:
            self._handle_client_safe(client_sock, client_addr)
        finally:
            self._client_sem.release()

    def _handle_client_safe(self, client_sock: socket.socket, client_addr):
        try:
            self._handle_client(client_sock, client_addr)
        except (ssl.SSLEOFError, ssl.SSLError, BrokenPipeError,
                ConnectionResetError, ConnectionAbortedError, OSError) as e:
            # 客户端主动断开（刷新/跳转/关页面）是良性错误，降级为 DEBUG 避免日志噪音
            try:
                logger.debug("proxy", f"Client disconnected: {type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            # 单个请求异常不能让代理崩溃，但记录堆栈便于排障
            try:
                logger.error("proxy", "Client connection handling exception\n" + traceback.format_exc())
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
        """Whether any ignore rule is hit (pid / process name / host wildcard — any hit means ignored).

        host supports wildcards (* → any, ? → single char), case-insensitive.

        Performance: takes a full snapshot under one lock (pid_set / name_set / host_regexes),
        avoiding 3 lock acquisitions. Host wildcards are precompiled regexes (compiled in
        refresh_ignored), avoiding re.escape + re.match on every request.
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
        """Enable/disable focus mode.

        pid/host/method/status_code/content_type are cross-category OR-matched: satisfying
        any condition means record/intercept.
        The enabled parameter is ignored — automatically determined by whether any focus
        condition exists: if any condition is non-empty (pids/hosts/methods/status_codes/
        content_types) it's enabled; all empty means disabled.
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
        """Wildcard match host (* → .*, ? → .), case-insensitive."""
        if not pattern:
            return False
        regex_str = '^' + re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.') + '$'
        try:
            return re.match(regex_str, host, re.IGNORECASE) is not None
        except Exception:  # noqa: BLE001
            return pattern.lower() in host.lower()

    def _has_response_focus(self) -> bool:
        """Whether there are focus conditions only determinable at the response phase (status_code/content_type)."""
        with self._focus_lock:
            return bool(self._focus_status_codes or self._focus_content_types)

    def is_focused_out(self, pid: int | None, host: str | None = None,
                       method: str | None = None,
                       status_code: int | None = None,
                       content_type: str | None = None) -> bool:
        """In focus mode, whether this request/response is outside the focus scope (should skip recording).

        pid/host/method/status_code/content_type are cross-category OR-matched:
        satisfying any focus condition means within scope (return False); satisfying none
        returns True (passthrough, not recorded).
        Request phase only passes pid/host/method; response phase may additionally pass
        status_code/content_type.
        If there are response-phase focus conditions (status/content_type), the request
        phase cannot determine yet, returns False (record temporarily).
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
        """Whether PID reverse-lookup is needed: capturing / has active rules / focus mode on / has ignored processes."""
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
            logger.debug("proxy", "peek first byte failed",
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
                "proxy", "Non-HTTP traffic tunnel failed, closing connection",
                f"client={client_addr} first_byte=0x{first_byte_hex} "
                f"(TLS=0x16) caused RST"
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
    def _parse_sni_from_tls(client_sock: socket.socket, timeout: float = 0.3) -> "str | None":
        """Parse the SNI hostname from a TLS ClientHello (does not consume socket data; uses MSG_PEEK).

        F20: HTTPS fallback when NAT reverse-lookup fails — extracts the target domain
        from the SNI extension of the ClientHello.
        """
        import time
        import select
        try:
            deadline = time.monotonic() + timeout
            data = b""
            while True:
                try:
                    chunk = client_sock.recv(65536, socket.MSG_PEEK)
                except OSError:
                    return None
                if not chunk:
                    return None
                data = chunk
                if data[0] != 0x16:  # 非 TLS Handshake
                    return None
                if len(data) < 5:
                    if time.monotonic() >= deadline:
                        return None
                    select.select([client_sock], [], [], 0.05)
                    continue
                # TLS record 头 data[3:5] = record 长度（record 头之后的握手数据长度）
                rec_len = int.from_bytes(data[3:5], 'big')
                needed = 5 + rec_len
                if len(data) >= needed:
                    break
                if time.monotonic() >= deadline:
                    break  # 超时仍残缺：用已有数据尽力解析
                select.select([client_sock], [], [], 0.05)
            if len(data) < 5 or data[0] != 0x16:
                return None
        except OSError:
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
        """Raw TCP tunnel forwarding in transparent proxy mode (no decryption).

        Used for HTTPS(443) transparent proxy:
        - Windows: WinDivert redirects outbound 443 traffic to the local proxy port;
          reverse-looks-up the original target IP:Port via the NAT table
        - Linux/macOS: iptables/pf redirects outbound 443 to the local proxy port;
          queries the original target via getsockopt(SO_ORIGINAL_DST) or getsockname

        Returns True if handled (regardless of success/failure), False if not in
        transparent proxy mode or reverse-lookup failed.
        """
        # 懒导入避免非透明模式下加载透明代理模块
        try:
            from .transparent_proxy import get_transparent_proxy, IS_WINDOWS
        except ImportError:  # noqa: BLE001
            # F31 诊断：transparent_proxy 模块导入失败
            logger.warning("proxy", "transparent_proxy import failed",
                           f"client={client_addr} closing connection caused RST")
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
                "proxy", "Transparent proxy not running, non-HTTP traffic closed",
                f"client={client_addr} first_byte=0x{first_byte_hex} "
                f"caused RST (enable transparent proxy to fix)"
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
                    "server", "lookup_reverse miss",
                    f"client={client_src_ip}:{client_src_port} "
                    f"will try SNI fallback"
                )
        else:
            # Unix: 通过 getsockopt(SO_ORIGINAL_DST) 或 getsockname 查询原目标
            target = proxy.lookup_original_dst(client_sock)
        if not target:
            # F20: NAT 反查失败时，尝试从 TLS ClientHello 解析 SNI 作为 fallback。
            # 场景1 HTTPS RESET 根因：NAT 反查失败（条目过期/未写入/竞态）直接关闭连接 → RST。
            # SNI fallback 让 HTTPS 在 NAT 表异常时仍能连接目标。
            sni_host = self._parse_sni_from_tls(client_sock)
            if sni_host and isinstance(sni_host, str):
                logger.warning(
                    "server", "NAT lookup failed, SNI fallback",
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
                        logger.info("server", "SNI fallback NAT registered",
                                    f"client={client_addr[0]}:{client_addr[1]} -> {sni_host}:443")
                    except Exception as e:  # noqa: BLE001
                        logger.warning("server", "SNI fallback NAT registration failed", str(e))
            else:
                logger.warning(
                    "server", "NAT lookup failed and no SNI",
                    f"client={client_addr} closing connection"
                )
                return False
        orig_dst_ip, orig_dst_port = target

        # F30: 透明代理 raw tunnel 模式下，capturing=True 时记录 TLS 隧道元数据 flow。
        # 无法解密 payload，但让用户在抓包页面看到有 HTTPS 连接发生（不再"抓不到包"）。
        if self.capturing and self.session_id:
            try:
                host_for_record = orig_dst_ip
                # 如果是 SNI fallback，target[0] 已是 hostname
                if isinstance(orig_dst_ip, str) and not orig_dst_ip[0].isdigit():
                    host_for_record = orig_dst_ip
                else:
                    # F35: 透明无系统代理时 NAT 反查几乎都命中（返回纯 IP），导致
                    # host/url 全是 IP。主动 PEEK 解析 ClientHello 的 SNI，用真实域名
                    # 覆盖 IP（SNI 优先、IP 兜底），使抓包页 host/url 显示真实域名。
                    # 仅 PEEK，不消耗数据，后续 _connect_target 仍可正常读取。
                    try:
                        sni_host = self._parse_sni_from_tls(client_sock)
                        if sni_host and isinstance(sni_host, str) and not sni_host[0].isdigit():
                            host_for_record = sni_host
                    except Exception:  # noqa: BLE001
                        pass
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
                "proxy", "raw tunnel connect target failed",
                f"target={orig_dst_ip}:{orig_dst_port} client={client_addr} "
                f"err={e} caused RST"
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
        """Bidirectional byte forwarding (no HTTP parsing), used for raw TCP tunnels.

        Differences from _tunnel:
        - _tunnel is for CONNECT tunnels, where the reader has already consumed the HTTP headers
        - _tunnel_raw is for transparent proxy, where there may be no HTTP headers to consume;
          it directly forwards bidirectionally

        Note: this method does not close target_sock (the caller handles port unregister + close
        uniformly in a finally block), to avoid getsockname() failing after close which would
        prevent port unregistration (port leak).

        Performance: uses select.select to drive bidirectional forwarding in a single thread,
        replacing the two-thread pipe model, reducing thread count and GIL contention,
        consistent with _tunnel.
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
                    data = s.recv(262144)
                except OSError:
                    return
                if not data:
                    return
                try:
                    dst.sendall(data)
                except OSError:
                    return

    def _should_ssl_bump(self, client_sock, host: str, pid: int, proc_name: str) -> tuple:
        """Determine SSL bump decision for a CONNECT request.

        Design fix: extracted from _handle_connect for testability and clarity.
        Returns (need_bump, do_bump, cert_path, key_path).

        The decision snapshots all relevant state at request time to avoid
        inconsistencies from concurrent state changes during processing.
        """
        # Phase 1: Determine if user wants to bump this host
        if self.capturing:
            need_bump = True
        elif has_active_rules_fast():
            need_bump = host_matches_any_rule(host)
        else:
            need_bump = False

        # Transparent proxy运行时：客户端未配置信任代理证书，SSL bump 必然握手失败
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

        # Phase 2: Determine if we CAN bump (all conditions must be met)
        do_bump = (
            need_bump
            and self.ssl_bump is not None
            and self.cert_installed
            and not self.is_ignored(pid, proc_name, host)
            and not self.is_focused_out(pid, host)
        )

        # Phase 3: Check if this host was previously bumped and failed
        with self._ssl_bump_failed_lock:
            failed_at = self._ssl_bump_failed_hosts.get(host)
            if failed_at is not None:
                if time.time() - failed_at > _SSL_BUMP_FAILED_TTL:
                    del self._ssl_bump_failed_hosts[host]
                else:
                    do_bump = False

        # Phase 4: Try to issue certificate if bumping
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
                        "SSL bump cert signing failed, downgrading to plain tunnel",
                        f"host={host} pid={pid} proc={proc_name}. "
                        f"Subsequent connections to this host will be tunneled directly (not decrypted). "
                        f"Check cert dir disk space/permissions."
                    )
                do_bump = False

        return need_bump, do_bump, cert_path, key_path

    # ---------- HTTPS CONNECT ----------

    def _handle_connect(self, client_sock, reader, connect_line, pid, proc_name):
        try:
            parts = connect_line.decode("latin-1").split()
            host_port = parts[1]
            host, _, port_s = host_port.partition(":")
            port = int(port_s) if port_s else 443
            # 端口范围校验（防 0/超范围值）
            if not (1 <= port <= 65535):
                self._send_simple(client_sock, 400, "Bad Request")
                return
        except Exception:  # noqa: BLE001
            return

        # 是否需要 SSL bump：
        # - 抓包中（capture on）：bump 所有 HTTPS（用户主动要抓包）
        # - 未抓包但有启用的自动修改规则：仅 bump 匹配某条规则 pattern 的 host
        #   避免对所有 HTTPS 都 bump 导致钉扎站点（edge/bing/bilibili 等）断连
        # - 证书已装 + 非忽略进程 + 非专注外进程
        # 性能优化（v11）：用 has_active_rules_fast() 无锁读，避免每 CONNECT 都进 _cache_lock
        # 设计修复：提取为独立函数 _should_ssl_bump()，便于测试和复用
        need_bump, do_bump, cert_path, key_path = self._should_ssl_bump(
            client_sock, host, pid, proc_name
        )

        if not do_bump:
            # 纯隧道转发（不解密）
            # 先消耗 CONNECT 请求剩余的 HTTP 头，避免转发到目标导致协议错误
            reader.read_headers()
            # F33: 纯隧道流量也记录元数据，让用户在抓包页看到 CONNECT 流量。
            # 原实现 _tunnel 无 _record_flow 调用，导致系统代理 CONNECT 非 bump 流量
            # （如透明代理运行时的非 loopback CONNECT）完全不显示在抓包页。
            # 与 F30（_try_raw_tunnel 元数据记录）对齐，让所有 HTTPS 隧道流量都可见。
            # 使用真实 pid/proc_name（F33 改进）：F33 场景下 pid 已在手，比 F30（raw tunnel 无 pid）更优。
            # 注意：被忽略的进程/host 不记录（与 API 注释"without capture"一致）
            if self.capturing and self.session_id \
                    and not self.is_ignored(pid, proc_name, host):
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
        # 性能修复(审计 P-#5)：mtime 检查节流——5s 内复用上次 mtime，
        # 避免每次 CONNECT 都 os.path.getmtime 系统调用（证书重签是低频事件）。
        # _ssl_mtime_cache 读写无需加锁，最坏情况是多查一次 mtime，无正确性问题。
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
                                   f"SSL bump failed, downgraded to plain tunnel: host={host}, pid={pid}, proc={proc_name}",
                                   f"TLS handshake failed: {e}. Subsequent connections to this host will be tunneled directly (not decrypted). "
                                   f"To decrypt, ensure the Telnix root cert is installed (cert install), or the app may use cert pinning.")
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
                logger.warning("proxy", f"TLS handshake failed: host={host}, pid={pid}",
                               f"error: {e}")
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
        """Return the current list of SSL bump failed hosts (cleans expired entries; for status API)."""
        with self._ssl_bump_failed_lock:
            now = time.time()
            expired = [h for h, t in self._ssl_bump_failed_hosts.items()
                       if now - t > _SSL_BUMP_FAILED_TTL]
            for h in expired:
                del self._ssl_bump_failed_hosts[h]
            return sorted(self._ssl_bump_failed_hosts.keys())

    def clear_ssl_bump_failed_hosts(self):
        """Clear the SSL bump failed set (called after the certificate is reinstalled)."""
        with self._ssl_bump_failed_lock:
            self._ssl_bump_failed_hosts.clear()

    def _tunnel(self, client_sock, reader, host, port):
        """Pure tunnel: no decryption; bidirectionally forwards bytes between client and target.

        Performance: uses select.select to drive bidirectional forwarding in a single thread,
        replacing the original two-thread pipe model.
        - Original model: each CONNECT opened 2 pipe threads + blocking join; 30 concurrent
          domains = 90 threads with severe GIL contention
        - New model: single-threaded select loop; thread count reduced by 2/3, GIL contention
          greatly reduced
        - Extra benefit: select timeout implements idle timeout (auto-close after 120s of
          inactivity), preventing zombie connections from holding threads
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
                try:
                    target.sendall(leftover)
                except OSError:
                    return  # finally 会关闭 target
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
                        data = s.recv(262144)
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
        """Loop processing HTTP requests on one connection (keep-alive)."""
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
        """Process a single HTTP request. Returns whether to keep-alive."""
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

        # 代理工具：黑白名单检查（转发前阻断）
        if proxy_tools.should_block(host, orig_url):
            self._send_simple(client_sock, 403, "Blocked by Telnix")
            return False

        # 代理工具：No Caching — 注入 no-cache 请求头
        # 流式媒体（视频/音频）请求跳过注入，避免破坏 CDN 缓存导致分片回源
        proxy_tools.inject_no_caching(headers, orig_url)

        # 延迟规则：请求阶段延迟
        try:
            from ..api.delay import check_delay as _check_delay
            _req_delay_ms = _check_delay(host, orig_url, "request")
            if _req_delay_ms > 0:
                time.sleep(_req_delay_ms / 1000.0)
        except Exception:  # noqa: BLE001
            pass

        # 代理工具：Map Local — 命中则直接返回本地文件，不转发
        map_local_result = proxy_tools.check_map_local(host, orig_url, headers.to_dict())
        if map_local_result is not None:
            ml_status = map_local_result["status"]
            ml_headers = Headers.from_dict(map_local_result["headers"])
            ml_body = map_local_result["body"]
            proxy_tools.inject_cors(ml_headers)
            self._send_response(client_sock, ml_status, ml_headers, ml_body, keep_alive)
            logger.info("proxy", f"Map Local hit: {orig_url} -> {ml_status} ({len(ml_body)} bytes)")
            if self.capturing and self.session_id and not self.is_ignored(pid, proc_name, host) \
                    and not self.is_focused_out(pid, host, method, ml_status,
                                                 ml_headers.get("Content-Type")):
                remote_ip, ip_region = _ip_info()
                self._record_flow(pid, proc_name, method, orig_url, scheme,
                                  host, path, headers, body,
                                  ml_status, ml_headers, _to_text(ml_body),
                                  remote_ip=remote_ip, ip_region=ip_region,
                                  size=len(ml_body))
            return keep_alive

        # 代理工具：Map Remote — 命中则改写目标 host/port/scheme/path
        map_remote_result = proxy_tools.check_map_remote(host, orig_url, headers.to_dict())
        if map_remote_result is not None:
            new_scheme, new_host, new_port, new_path, new_url = map_remote_result
            logger.info("proxy", f"Map Remote hit: {orig_url} -> {new_url}")
            scheme = new_scheme
            host = new_host
            port = new_port
            path = new_path
            orig_url = new_url
            headers.set("Host", host + (f":{port}" if port not in (80, 443) else ""))

        # IP 属地分析：DNS 预解析异步化（丢到线程池），不阻塞代理线程
        # 直连场景：与实际连接的目标 IP 一致
        # Clash 上游代理场景：本机解析的候选 IP（可能与 Mihomo 选中节点不一致，但能给出大致属地）
        # 性能优化：不抓包时（capturing=False）跳过 DNS 与属地查询；
        #           抓包时提交到 _DNS_EXECUTOR，记录流量时非阻塞取结果（未完成则用空值）
        _dns_future = None
        if self.capturing:
            _dns_future = _DNS_EXECUTOR.submit(_resolve_host_for_region, host, port)

        def _ip_info():
            """Retrieve DNS resolution + region lookup result.

            性能优化：原实现纯非阻塞（done() 为 False 就返回空），高并发时 DNS 线程池
            排队导致大量 flow 在记录时 future 未完成 → 属地始终为空。
            改为短暂等待（_record_flow 在响应完成后调用，此时 DNS 早已提交，
            转发耗时期间 DNS 多已完成），兼顾属地显示率与转发线程尾延迟。
            超时从 200ms 收紧到 50ms：绝大多数场景 future 已完成，命中超时的
            慢 DNS 场景下也不再让转发线程被长时间阻塞。
            """
            if _dns_future is None:
                return "", ""
            try:
                return _dns_future.result(timeout=0.05)
            except concurrent.futures.TimeoutError:
                return "", ""
            except Exception:  # noqa: BLE001
                return "", ""

        remote_ip = ""
        ip_region = ""

        # 自动回复规则（mock：直接返回伪造响应）
        # §4.1 请求阶段匹配：传 method/pid/process_name（status_code 此时无，传 None）
        # 请求阶段无法匹配带 status_filter 的规则（filter_str 非空但 value 为 None → 不匹配）
        # 那些规则会在响应阶段（modify_response 分支）重新匹配
        # 性能优化（v11）：无启用规则时跳过 _match_auto_reply，避免每请求都进 _cache_lock
        rule_hit_start = time.time()  # 命中追踪计时起点
        if has_active_rules_fast():
            rule = self._match_auto_reply(
                orig_url, method=method, status_code=None,
                pid=pid, process_name=proc_name)
        else:
            rule = None
            # throttle 日志：记录跳过原因（capturing off 或无规则）
            if not self.capturing:
                _log_skip_rule_match("capturing off and no active rules")
            else:
                _log_skip_rule_match("no active rules")
        if rule:
            # 计算匹配耗时（毫秒）
            match_duration_ms = (time.time() - rule_hit_start) * 1000
            logger.info("proxy", f"Matched auto-reply rule: action={rule['action']}, pattern={rule['pattern']}",
                        f"URL={orig_url}")
            # 记录实时命中追踪（耗时热力图 + 时间线）
            rule_id = rule.get("id")
            rule_name = rule.get("note") or rule.get("pattern", "")
            try:
                record_rule_hit(rule_id, rule_name, match_duration_ms)
            except Exception:  # noqa: BLE001
                pass
            # §3.2 命中计数：匹配成功即自增（flow_id 此阶段可能为 None）
            # 性能修复(审计 P-#1)：下沉到 _preinsert_executor 异步执行，
            # 避免在代理转发线程内同步触发 SQLite fsync 阻塞吞吐。
            try:
                self._preinsert_executor.submit(db.increment_rule_hit, rule.get("id"), None)
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
                logger.info("proxy", "mock_request: forward with preset request",
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
                logger.info("proxy", "mock_request execution failed", str(e))
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
                    orig_headers_str, orig_body_preview, orig_body_text = "(read failed)", "", ""
            headers, body = _apply_modify_request(headers, body, rule)
            if _log_detail:
                try:
                    new_headers_str = str(headers.to_dict())
                    new_body_preview = body[:200].hex() if body else "(empty)"
                    new_body_text = body[:200].decode("utf-8", errors="replace") if body else ""
                except Exception:  # noqa: BLE001
                    new_headers_str, new_body_preview, new_body_text = "(read failed)", "", ""
                detail = (f"original headers: {orig_headers_str}\n"
                          f"modified headers: {new_headers_str}\n"
                          f"original body(hex): {orig_body_preview}\n"
                          f"modified body(hex): {new_body_preview}\n"
                          f"original body(text): {orig_body_text}\n"
                          f"modified body(text): {new_body_text}")
            else:
                detail = ""
            logger.info("proxy",
                        f"modify_request applied: host={host}, path={path}, rule_note={rule.get('note', '')}",
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
                logger.warning("proxy", f"Script rule content is empty: {rule.get('id')}", "")
            else:
                try:
                    ctx = build_ctx(
                        host=host, path=path, method=method, url=orig_url,
                        scheme=scheme, pid=pid, process_name=proc_name or "",
                        session_id=self.session_id,
                        request_headers=headers.to_dict(), request_body=body,
                    )
                    logger.info("proxy", f"script on_request start call: {rule['id']}",
                                f"url={orig_url}, body_len={len(body) if body else 0}")
                    resp = call_script_request(rule["id"], script, ctx)
                    logger.info("proxy", f"script on_request call returned: {rule['id']}",
                                f"resp={'None' if resp is None else resp.get('action', '?')}")
                except Exception as e:  # noqa: BLE001
                    logger.warning("proxy", f"script on_request exception: {rule['id']}",
                                   f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
                    resp = None
                if resp is None:
                    logger.warning("proxy", f"script on_request call failed: {rule['id']}",
                                   "Script unavailable, request forwarded as-is")
                else:
                    action = resp.get("action", "continue")
                    if action == "drop":
                        logger.info("proxy", f"script drop request: {orig_url}", "")
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
                        logger.info("proxy", f"script mock request: {orig_url} -> {m_status}", "")
                        return keep_alive
                    # continue：应用请求头/体修改
                    new_headers, new_body = apply_request(resp, headers, body)
                    if new_headers is not headers:
                        try:
                            orig_h = str(headers.to_dict())
                            new_h = str(new_headers.to_dict())
                        except Exception:  # noqa: BLE001
                            orig_h, new_h = "?", "?"
                        logger.info("proxy", f"script on_request modified headers: {orig_h} -> {new_h}", "")
                        headers = new_headers
                    if new_body is not body:
                        logger.info("proxy", f"script on_request modified body: len {len(body)} -> {len(new_body)}", "")
                        body = new_body

        # 忽略进程或专注外进程/host：转发不记录
        # 请求阶段只传 pid/host/method；响应阶段条件（status_code/content_type）
        # 由 is_focused_out 内部处理（暂不算专注外，等响应阶段再判）
        # DNS 预解析结果非阻塞刷新（若已完成则取值，否则保持空值不阻塞）
        remote_ip, ip_region = _ip_info()
        ignored = self.is_ignored(pid, proc_name, host) or self.is_focused_out(pid, host, method)
        record = self.capturing and self.session_id and not ignored

        # 触发式捕获：若 trigger 启用且未触发，请求阶段不预入库，
        # 响应阶段的 _record_flow 会根据完整条件（含 status_code）决定是否触发。
        # 已触发则正常 record。
        _trigger_armed = False
        if record:
            try:
                from ..trigger import get_trigger_manager
                _tm = get_trigger_manager()
                if _tm.enabled and not _tm.triggered:
                    _trigger_armed = True  # 标记：跳过预入库，但响应阶段仍检查触发
            except Exception:  # noqa: BLE001
                pass

        # 请求断点
        flow_id = None
        preinsert_state = None
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
                # 从修改后的 URL 重新解析 scheme 和 port，避免断点改了
                # scheme/port（如 https→http、443→8080）后转发仍用原值
                if orig_url.lower().startswith("http://") or orig_url.lower().startswith("https://"):
                    _sp = urlsplit(orig_url)
                    if _sp.scheme:
                        scheme = _sp.scheme
                    if _sp.port:
                        port = _sp.port
                    elif _sp.scheme == "https":
                        port = 443
                    elif _sp.scheme == "http":
                        port = 80
                headers = Headers.from_dict(_parse_json(modified["request_headers"]))
                body = _to_bytes(modified["request_body"])
            db.update_flow_breakpoint(flow_id, None)

        # 请求阶段预入库：让用户立即看到"请求已发出"的行（实时出包）
        # 性能修复：线程池异步预入库，避免同步 SQLite 写阻塞转发线程。
        # 响应阶段等待预入库完成（最多15ms），超时则同步插入兜底；不再 cancel 预入库
        # （cancel 导致快速响应走 _record_flow 回退路径，反而延迟出包）。
        # 仅在非断点场景预入库（断点场景已在上面 _insert_flow，需要同步 id）
        if record and flow_id is None and not _trigger_armed:
            try:
                preinsert_state = {"lock": threading.Lock(), "cancelled": False,
                                   "flow_id": None, "done": threading.Event()}
                self._preinsert_executor.submit(
                    self._preinsert_flow, preinsert_state,
                    pid, proc_name, method, orig_url, scheme,
                    host, path, headers, body,
                    remote_ip=remote_ip, ip_region=ip_region)
            except Exception:  # noqa: BLE001
                preinsert_state = None

        # 转发到目标
        start = time.time()
        # WebSocket Upgrade：握手成功后转为双向帧转发，不再走常规 HTTP 请求/响应
        from .websocket_relay import is_websocket_upgrade, relay_websocket
        if is_websocket_upgrade(headers):
            # WebSocket 由 websocket_relay 自行记录完整消息，这里取回预入库 flow_id
            if preinsert_state is not None:
                with preinsert_state["lock"]:
                    fid = preinsert_state["flow_id"]
                if fid is None:
                    # WS 握手需要立即继续，等待最多5ms，超时则标记取消
                    preinsert_state["done"].wait(timeout=0.005)
                    with preinsert_state["lock"]:
                        fid = preinsert_state["flow_id"]
                        if fid is None:
                            preinsert_state["cancelled"] = True
                if fid is not None:
                    flow_id = fid
                preinsert_state = None
            return self._handle_websocket_upgrade(
                client_sock, host, port, scheme, method, path, orig_url,
                headers, body, pid, proc_name, remote_ip, ip_region,
                record, flow_id, keep_alive)
        cert_info_json = ""
        timing_info = {}
        try:
            # 是否需要解压响应体：抓包记录或需要修改响应时才解压，否则跳过节省 CPU
            need_decompress = record or (
                rule is not None and rule["action"] in ("modify_response", "script"))
            t_forward_start = time.perf_counter()
            status, resp_headers, resp_body, cert_info_json, http_version = self._forward(
                host, port, scheme, method, path, version, headers, body,
                decompress=need_decompress, client_sock=client_sock)
            t_forward_end = time.perf_counter()
            # timing 分解：_forward 耗时 ≈ connect+ssl+server_processing（含首字节）
            # total 由调用方算（duration_ms）
            timing_info = {
                "forward_ms": int((t_forward_end - t_forward_start) * 1000),
            }
        except Exception:  # noqa: BLE001
            self._send_simple(client_sock, 502, "Bad Gateway")
            # 转发异常：等待预入库完成或同步插入兜底
            if preinsert_state is not None:
                with preinsert_state["lock"]:
                    fid = preinsert_state["flow_id"]
                if fid is None:
                    preinsert_state["done"].wait(timeout=0.015)
                    with preinsert_state["lock"]:
                        fid = preinsert_state["flow_id"]
                if fid is not None:
                    flow_id = fid
                else:
                    with preinsert_state["lock"]:
                        preinsert_state["synced"] = True
                    try:
                        flow_id = self._insert_flow(
                            pid, proc_name, method, orig_url, scheme, host, path,
                            headers, body,
                            remote_ip=remote_ip, ip_region=ip_region,
                            cert_info=cert_info_json, http_version=http_version,
                            push_sse=True)
                    except Exception:  # noqa: BLE001
                        flow_id = None
                preinsert_state = None
            if record and flow_id:
                db.update_flow_response_async(flow_id, 502, "{}", "", 0, 0)
                # 实时出包：推送 502 状态的 SSE 更新（预入库 flow 补齐失败状态）
                self._notify_flow_update(flow_id, 502, Headers(), b"", int((time.time() - start) * 1000))
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

        # 流式响应：body 已由 _forward 直接流式发送给客户端（resp_body is None），
        # 跳过解压 / body 修改 / mirror / 断点 / _send_response，仅记录元数据后返回。
        if resp_body is None:
            # 取回异步预入库的 flow_id
            if preinsert_state is not None:
                with preinsert_state["lock"]:
                    fid = preinsert_state["flow_id"]
                if fid is None:
                    preinsert_state["done"].wait(timeout=0.015)
                    with preinsert_state["lock"]:
                        fid = preinsert_state["flow_id"]
                if fid is not None:
                    flow_id = fid
                else:
                    with preinsert_state["lock"]:
                        preinsert_state["synced"] = True
                    try:
                        flow_id = self._insert_flow(
                            pid, proc_name, method, orig_url, scheme, host, path,
                            headers, body,
                            remote_ip=remote_ip, ip_region=ip_region,
                            cert_info=cert_info_json, http_version=http_version,
                            push_sse=True)
                    except Exception:  # noqa: BLE001
                        flow_id = None
                preinsert_state = None
            if record:
                resp_headers_json = json.dumps(resp_headers.to_dict())
                if flow_id is None:
                    # 无预入库：插入完整 flow，但 body 为空（已流式转发，不入库大媒体）
                    self._record_flow(pid, proc_name, method, orig_url, scheme, host,
                                      path, headers, body, status, resp_headers,
                                      "", duration, 0,
                                      remote_ip=remote_ip, ip_region=ip_region,
                                      cert_info=cert_info_json, http_version=http_version)
                else:
                    # 有预入库：补齐响应字段（body 为空，size=0）
                    db.update_flow_response_async(
                        flow_id, status, resp_headers_json, "", duration, 0)
                    if cert_info_json:
                        try:
                            db.update_flow_cert_info_async(flow_id, cert_info_json)
                        except Exception:  # noqa: BLE001
                            pass
                    # 实时出包：推送 SSE 更新补齐预入库 flow 的响应字段
                    # WebSocket 流式转发，body 已转发不入库，传空 bytes
                    self._notify_flow_update(flow_id, status, resp_headers, b"", duration)
                    # 录制 hook（流式响应 body 已转发，仅录制 flow_id；被动扫描跳过无 body）
                    try:
                        from ..api.record_replay import add_flow_to_recording as _add_to_recording
                        _add_to_recording(flow_id)
                    except Exception:  # noqa: BLE001
                        pass
            return keep_alive

        # 延迟规则：响应阶段延迟
        try:
            from ..api.delay import check_delay as _check_delay
            _resp_delay_ms = _check_delay(host, orig_url, "response")
            if _resp_delay_ms > 0:
                time.sleep(_resp_delay_ms / 1000.0)
        except Exception:  # noqa: BLE001
            pass

        # 代理工具：Force CORS — 给响应注入 CORS 头
        proxy_tools.inject_cors(resp_headers)

        # 代理工具：Mirror — 将匹配的响应保存到本地目录
        mirror_dir = proxy_tools.check_mirror(host, orig_url)
        if mirror_dir:
            saved = proxy_tools.save_mirror_response(
                mirror_dir, orig_url, resp_headers.get("Content-Type") or "", resp_body)
            if saved:
                logger.info("proxy", f"Mirror saved: {orig_url} -> {saved}")

        # 取回异步预入库的 flow_id。等待最多 15ms 让预入库完成，
        # 若仍未完成则同步插入兜底（不再 cancel 预入库，避免快速响应延迟出包）。
        if preinsert_state is not None:
            with preinsert_state["lock"]:
                fid = preinsert_state["flow_id"]
            if fid is None:
                # 等待 worker 完成（最多15ms）
                preinsert_state["done"].wait(timeout=0.015)
                with preinsert_state["lock"]:
                    fid = preinsert_state["flow_id"]
            if fid is not None:
                flow_id = fid
            else:
                # 超时兜底：同步插入，标记 synced 让 worker 跳过重复行
                with preinsert_state["lock"]:
                    preinsert_state["synced"] = True
                try:
                    flow_id = self._insert_flow(
                        pid, proc_name, method, orig_url, scheme, host, path,
                        headers, body,
                        remote_ip=remote_ip, ip_region=ip_region,
                        cert_info=cert_info_json, http_version=http_version,
                        push_sse=True)
                except Exception:  # noqa: BLE001
                    flow_id = None
            preinsert_state = None

        # 自动回复：修改响应
        # §4.1 响应阶段重新匹配：如果请求阶段未匹配上（可能因 status_filter 限制），
        # 现在拿到 status_code 后再匹配一次，让带 status_filter 的 modify_response 规则生效
        # 性能优化（v11）：无启用规则时跳过（请求阶段已检查过，但规则可能在转发期间变更）
        rule_hit_start = time.time()  # 命中追踪计时起点
        if rule is None and has_active_rules_fast():
            rule = self._match_auto_reply(
                orig_url, method=method, status_code=status,
                pid=pid, process_name=proc_name)
            if rule:
                # 计算匹配耗时（毫秒）
                match_duration_ms = (time.time() - rule_hit_start) * 1000
                logger.info("proxy",
                        f"Response phase matched rule: action={rule['action']}, pattern={rule['pattern']}",
                        f"URL={orig_url}, status={status}")
                # 记录实时命中追踪（耗时热力图 + 时间线）
                rule_id = rule.get("id")
                rule_name = rule.get("note") or rule.get("pattern", "")
                try:
                    record_rule_hit(rule_id, rule_name, match_duration_ms, flow_id=flow_id)
                except Exception:  # noqa: BLE001
                    pass
                # §3.2 命中计数（响应阶段匹配也计数）
                # 性能修复(审计 P-#1)：下沉到 _preinsert_executor 异步执行，
                # 避免在代理转发线程内同步触发 SQLite fsync 阻塞吞吐。
                try:
                    self._preinsert_executor.submit(db.increment_rule_hit, rule.get("id"), flow_id)
                except Exception:  # noqa: BLE001
                    pass
        if rule and rule["action"] == "modify_response":
            # 性能优化：detail 中的 !r 计算仅在 INFO 级别启用时执行
            _log_detail = logger.is_enabled(logger.INFO)
            logger.info("proxy", "entering modify_response branch",
                        (f"body_len={len(resp_body)}, Content-Encoding={resp_headers.get('Content-Encoding')}, "
                         f"Content-Type={resp_headers.get('Content-Type')}, "
                         f"body first 200 bytes={resp_body[:200]!r}") if _log_detail else "")
            # §3.8 delay 动作：在响应前延迟（毫秒）
            _apply_delay(rule, target_name="delay", log_label="delay")
            status, resp_headers, resp_body = _apply_modify_response(
                status, resp_headers, resp_body, rule)
            logger.info("proxy", "modify_response done",
                        (f"body_len={len(resp_body)}, body first 200 bytes={resp_body[:200]!r}") if _log_detail else "")

        # script：调用用户 Python 脚本的 on_response（响应阶段，返回客户端前）
        # 脚本可修改响应头/体/状态码，或返回 drop（替换为 403）/ mock（替换为伪造响应）
        if rule and rule["action"] == "script":
            from ..auto_reply.script_runner import (
                call_script_response, build_ctx, apply_response)
            script = rule.get("modify_rules") or ""
            if isinstance(script, list):
                script = ""
            if not script.strip():
                logger.warning("proxy", f"Script rule content is empty: {rule.get('id')}", "")
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
                    logger.warning("proxy", f"script on_response call failed: {rule['id']}",
                                   "Script unavailable, response returned as-is")
                else:
                    action = resp.get("action", "continue")
                    if action == "drop":
                        logger.info("proxy", f"script drop response: {orig_url}", "")
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
                        logger.info("proxy", f"script mock response: {orig_url} -> {m_status}", "")
                    else:
                        # continue：应用响应修改
                        new_status, new_headers, new_body = apply_response(
                            resp, status, resp_headers, resp_body)
                        if new_status != status:
                            logger.info("proxy", f"script on_response changed status: {status} -> {new_status}", "")
                            status = new_status
                        if new_headers is not resp_headers:
                            logger.info("proxy", "script on_response modified headers", "")
                            resp_headers = new_headers
                        if new_body is not resp_body:
                            logger.info("proxy", f"script on_response modified body: len {len(resp_body)} -> {len(new_body)}", "")
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
                                  cert_info=cert_info_json, http_version=http_version,
                                  timing=timing_info or None)
            else:
                db.update_flow_response_async(
                    flow_id, status, json.dumps(resp_headers.to_dict()),
                    _truncate_for_record(resp_body), duration, len(resp_body))
                # 更新 cert_info（首次握手时获取）
                # 性能修复(审计 B1-#1)：原为代理线程内同步新建连接单条 UPDATE，
                # 绕过批写队列；改为入异步 update 队列与响应字段合并提交
                if cert_info_json:
                    try:
                        db.update_flow_cert_info_async(flow_id, cert_info_json)
                    except Exception:  # noqa: BLE001
                        pass
                # 实时出包：响应完成后推送 SSE 更新，让前端补齐预入库 flow 的响应字段
                # （请求阶段已推送 status=null 的 lite flow，这里推送带 status/headers/body 的更新）
                self._notify_flow_update(flow_id, status, resp_headers, resp_body, duration)
                # 录制 / 被动扫描 hook（正常响应路径）
                self._run_post_record_hooks(flow_id, orig_url, method, scheme, host,
                                            path, headers, status, resp_headers, resp_body)
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
        """Returns (host, port, path, orig_url)."""
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
        """Handle a WebSocket Upgrade request.

        Flow:
        1. Establish a TCP/TLS connection to the target server (does not reuse the pool;
           WS is a long-lived connection)
        2. Send the original Upgrade request to the server
        3. Read the server's 101 response and forward it to the client
        4. Switch to bidirectional WebSocket frame relay, recording per message to the flows table
        5. After the connection closes, return False (no HTTP keep-alive)
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
                raise OSError("Server not responding")
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
        logger.info("ws", f"WebSocket upgrade success: {host}{path}",
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
            logger.info("ws", "WebSocket forwarding ended", str(e))
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
        """Try to send a request via the h2 connection pool (reusing an existing h2 connection).

        On success returns (status, resp_headers, resp_body, cert_info_json, "HTTP/2").
        cert_info_json is taken from the H2Client cache (obtained during the first handshake).
        Returns None if no available connection in the pool (caller should create a new one).
        Returns None and auto-removes from the pool if a pooled connection errors.
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
            # 流式媒体检测：H2 已由 h2_client.request 全量缓冲（多路复用不消耗连接），
            # 真正的 H2 流式转发（边读 DATA 帧边转发）是未来优化项，暂不实现。
            # 此处对流式内容跳过解压（视频/音频解压无意义且浪费 CPU），body 仍完整返回。
            h2_streaming = _is_streaming_content(resp_headers, status)
            if decompress and not h2_streaming:
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
        """Create an H2Client on a newly established TLS connection and send a request.

        On success returns a 5-tuple (H2Client has been put into the pool, can be reused
        by subsequent requests).
        On failure returns None (connection has been closed; caller needs to create a new
        connection and fall back to HTTP/1.1).
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
            # 流式媒体检测：H2 已由 h2_client.request 全量缓冲（多路复用不消耗连接），
            # 真正的 H2 流式转发（边读 DATA 帧边转发）是未来优化项，暂不实现。
            # 此处对流式内容跳过解压（视频/音频解压无意义且浪费 CPU），body 仍完整返回。
            h2_streaming = _is_streaming_content(resp_headers, status)
            if decompress and not h2_streaming:
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
                 decompress: bool = True, client_sock=None, stream: bool = False):
        """Forward a request to the target server; returns (status, resp_headers, resp_body, cert_info_json, http_version).

        Prefers reusing the h2 connection pool (multiplexing), then the HTTP/1.1 pool,
        and finally creates a new connection.
        cert_info_json: peer certificate info JSON string for HTTPS traffic (obtained on
        first handshake; empty on connection reuse)
        http_version: 'HTTP/1.1' or 'HTTP/2' (uses h2 forwarding when ALPN negotiates h2)
        decompress: whether to decompress the response body. Pass False to skip decompression
        when not capturing and no modify rules, saving CPU.
        client_sock: optional client socket; when provided and the response is detected as
        streaming media (video/audio), the body is streamed directly to the client without
        buffering, and resp_body is returned as None to signal the caller to skip
        _send_response / body recording.
        stream: force streaming when client_sock is provided (auto-detection via
        _is_streaming_content still applies if False).
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
        # 流式媒体请求跳过 h2（h2 全量缓冲会导致视频播放首字节超时），
        # 强制走 HTTP/1.1 路径（已有 _stream_to_client 逐块流式转发）
        if scheme == "https" and not _is_media_request(fwd, path, host):
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
                    if alpn == "h2" and not _is_media_request(fwd, path, host):
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
                        # 使用仅通告 http/1.1 的独立 SSL context，避免新连接再次协商到 h2
                        if not hasattr(self, "_fallback_ssl_ctx"):
                            ctx = ssl.create_default_context()
                            ctx.check_hostname = False
                            ctx.verify_mode = ssl.CERT_NONE
                            ctx.set_alpn_protocols(["http/1.1"])
                            self._fallback_ssl_ctx = ctx
                        target = self._fallback_ssl_ctx.wrap_socket(
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
                        if alpn == "h2" and not _is_media_request(fwd, path, host):
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
                            # 使用仅通告 http/1.1 的独立 SSL context，避免新连接再次协商到 h2
                            if not hasattr(self, "_fallback_ssl_ctx"):
                                ctx = ssl.create_default_context()
                                ctx.check_hostname = False
                                ctx.verify_mode = ssl.CERT_NONE
                                ctx.set_alpn_protocols(["http/1.1"])
                                self._fallback_ssl_ctx = ctx
                            target = self._fallback_ssl_ctx.wrap_socket(
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
            # 流式转发：视频/音频/大媒体响应直接转发给客户端，避免 read_body 全量
            # 缓冲导致播放器等待首字节超时。仅 HTTP/1.1 路径支持真流式（H2 见上方分支）。
            # HEAD 请求响应无 body，不能流式（否则会阻塞等待不存在的 body）。
            if client_sock is not None and method != "HEAD" and (
                    stream or _is_streaming_content(resp_headers, status_code)):
                cert_info_json = _get_cert_info_result(cert_info_future)
                # 客户端连接是否可保持：依据客户端请求版本/Connection 头
                stream_keep_alive = self._should_keep_alive(version, headers)
                # 先发响应头，再逐块流式转发 body
                self._stream_to_client(client_sock, target, treader,
                                       status_code, resp_headers, stream_keep_alive)
                # 流式响应消耗目标连接：不归还连接池，直接关闭并注销端口（防泄漏）
                # F10: 先 close 再 unregister，让 FIN/RST 在端口仍注册时发出
                try:
                    target.close()
                except OSError:
                    pass
                self._unregister_proxy_port(target_local_port)
                # resp_body=None 表示已直接流式发送给客户端，调用方据此跳过
                # _send_response / 解压 / body 修改 / body 记录
                return (status_code, resp_headers, None, cert_info_json, "HTTP/1.1")
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

    def _stream_to_client(self, client_sock, target_sock, reader: SocketReader,
                          status_code, resp_headers: Headers,
                          keep_alive: bool) -> int:
        """流式转发响应体到客户端（不缓冲到内存）。

        先立即下发 HTTP 状态行 + 响应头，再逐块/逐 chunk 把目标返回的字节
        转发给客户端，避免 read_body 全量缓冲导致播放器首字节超时。
        返回已转发的 body 字节数。客户端断开（SSLEOFError/OSError）静默处理。
        """
        te = (resp_headers.get("Transfer-Encoding") or "").lower()
        cl = resp_headers.get("Content-Length")
        target_chunked = "chunked" in te
        # 客户端 framing：目标 chunked 或无长度信息时用 chunked 下发，
        # 保证客户端能识别响应边界（HTTP/1.1 客户端必须支持 chunked）
        client_chunked = target_chunked or cl is None

        # 构造转发响应头（保留原始顺序与大小写，去掉长度/连接相关头后重设）
        out_headers = Headers()
        for k, v in resp_headers._items:  # noqa: SLF001
            kl = k.lower()
            if kl in ("content-length", "connection", "transfer-encoding"):
                continue
            out_headers.add(k, v)
        if client_chunked:
            out_headers.set("Transfer-Encoding", "chunked")
        else:
            out_headers.set("Content-Length", cl)
        out_headers.set("Connection", "keep-alive" if keep_alive else "close")

        head = bytearray()
        head += f"HTTP/1.1 {status_code} {_reason(status_code)}\r\n".encode("latin-1")
        head += out_headers.to_bytes()
        head += b"\r\n"

        total = 0
        try:
            # 弱网模拟：限速下发响应头（禁用 throttle 时直接 sendall，减少函数调用）
            throttle.send_throttled(client_sock, bytes(head))
            if target_chunked:
                # 目标用 chunked 编码：逐块读取并重新编码为 chunked 帧转发
                terminated = False
                while True:
                    size_line = reader.read_line()
                    if not size_line:
                        break
                    size_str = size_line.split(b";")[0].strip()
                    try:
                        size = int(size_str, 16)
                    except ValueError:
                        break
                    if size == 0:
                        # 终止块：读取并丢弃尾部头，下发终止帧
                        reader.read_headers()
                        throttle.send_throttled(client_sock, b"0\r\n\r\n")
                        terminated = True
                        break
                    chunk = reader.read_exactly(size)
                    if not chunk:
                        break
                    total += len(chunk)
                    # 用实际读取长度编码 chunked 帧（连接断开时 read_exactly
                    # 可能返回少于 size 的字节，用 len(chunk) 避免帧损坏）
                    throttle.send_throttled(
                        client_sock, b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    reader.read_line()  # 尾部 CRLF
                # 异常退出（目标早断）：补发终止帧，避免客户端连接挂起
                if not terminated:
                    try:
                        throttle.send_throttled(client_sock, b"0\r\n\r\n")
                    except (ssl.SSLEOFError, ssl.SSLError, OSError, ConnectionError):
                        pass
            elif cl is not None:
                # 有 Content-Length：按 256KB 块读取并立即转发
                try:
                    remaining = int(cl)
                except ValueError:
                    remaining = 0
                while remaining > 0:
                    want = min(262144, remaining)
                    block = reader.read_exactly(want)
                    if not block:
                        break
                    total += len(block)
                    throttle.send_throttled(client_sock, block)
                    remaining -= len(block)
            else:
                # 无长度信息：读到目标关闭，按 chunked 帧转发
                # 先排空 reader 缓冲区（读响应头时可能已预读部分 body）
                if reader.buf:
                    block = bytes(reader.buf)
                    del reader.buf[:]
                    total += len(block)
                    throttle.send_throttled(
                        client_sock, b"%x\r\n" % len(block) + block + b"\r\n")
                while True:
                    block = target_sock.recv(262144)
                    if not block:
                        break
                    total += len(block)
                    throttle.send_throttled(
                        client_sock, b"%x\r\n" % len(block) + block + b"\r\n")
                # 发送终止块（在 try 内，断开时静默）
                throttle.send_throttled(client_sock, b"0\r\n\r\n")
        except (ssl.SSLEOFError, ssl.SSLError, OSError, ConnectionError):
            # 客户端断开（刷新/跳转/关页面/拖动进度）是良性的，静默忽略
            pass
        return total

    def _send_response(self, client_sock, status_code, headers: Headers,
                       body: bytes, keep_alive: bool):
        # 性能优化：直接修改传入的 headers（调用方不再复用），
        # 避免每响应都新建 dict + Headers（减少一次对象分配和 GIL 开销）
        headers.remove("Transfer-Encoding")
        headers.remove("Content-Length")
        headers.set("Content-Length", str(len(body)))
        headers.set("Connection", "keep-alive" if keep_alive else "close")
        # 先发头部，再单独发 body——避免 bytearray 拼接 + bytes(out) 两次内存拷贝
        head = bytearray()
        head += f"HTTP/1.1 {status_code} {_reason(status_code)}\r\n".encode("latin-1")
        head += headers.to_bytes()
        head += b"\r\n"
        try:
            client_sock.sendall(bytes(head))
            if body:
                throttle.send_throttled(client_sock, body)
        except (ssl.SSLEOFError, ssl.SSLError, OSError, ConnectionError):
            pass

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

    def _build_request_flow_dict(self, pid, proc_name, method, url, scheme, host, path,
                                 headers: Headers, body: bytes,
                                 remote_ip: str = "", ip_region: str = "",
                                 cert_info: str = "", http_version: str = "") -> dict:
        """Build the request-phase flow dict (used by sync/async insert paths)."""
        return {
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

    def _insert_flow(self, pid, proc_name, method, url, scheme, host, path,
                     headers: Headers, body: bytes,
                     remote_ip: str = "", ip_region: str = "",
                     cert_info: str = "", http_version: str = "",
                     push_sse: bool = True) -> int:
        """Synchronously insert a request-phase flow. Breakpoints need immediate id."""
        flow = self._build_request_flow_dict(
            pid, proc_name, method, url, scheme, host, path,
            headers, body, remote_ip, ip_region, cert_info, http_version)
        flow_id = db.insert_flow(flow)
        # 实时出包：请求阶段预入库后立即 SSE 推送，让前端即时看到"请求已发出"
        # 响应字段（status_code/response_headers/response_body/duration_ms/size）为 null，
        # 响应完成后再通过 _notify_flow_update 推送补齐
        if flow_id and push_sse:
            try:
                flow["id"] = flow_id
                db._notify_flow_subscribers(flow)
            except Exception:  # noqa: BLE001
                pass
        return flow_id

    def _preinsert_flow(self, state: dict, pid, proc_name, method, url, scheme, host, path,
                        headers: Headers, body: bytes,
                        remote_ip: str = "", ip_region: str = "",
                        cert_info: str = "", http_version: str = ""):
        """Background pre-insert for non-breakpoint capture.

        Runs in a thread-pool worker so the proxy thread is not blocked by SQLite.
        The response phase waits up to 15ms for completion via state["done"] Event;
        if still not done, it does a synchronous insert as fallback.
        We never cancel/delete a preinsert (that caused fast responses to miss
        the real-time SSE push and fall back to delayed _record_flow).

        性能修复(审计 P-#2)：不取消预入库，worker 始终尝试 INSERT+SSE 推送。
        如果响应阶段已同步插入（state["flow_id"] 已被设置），则跳过避免重复。
        """
        # 构造一次，DB 插入 + SSE 推送复用
        flow = self._build_request_flow_dict(
            pid, proc_name, method, url, scheme, host, path,
            headers, body, remote_ip, ip_region, cert_info, http_version)
        try:
            with state["lock"]:
                # 如果响应阶段已同步插入（synced 标志），跳过
                if state.get("synced"):
                    state["done"].set()
                    return
            flow_id = db.insert_flow(flow)
        except Exception:  # noqa: BLE001
            flow_id = None
        with state["lock"]:
            if flow_id is None:
                state["flow_id"] = None
                state["done"].set()
                return
            # 如果响应阶段已同步插入，删除我们的重复行
            if state.get("synced"):
                try:
                    db.delete_flow(flow_id)
                except Exception:  # noqa: BLE001
                    pass
                state["done"].set()
                return
            state["flow_id"] = flow_id
        # SSE 推送（请求阶段 lite flow，status=null）
        try:
            flow["id"] = flow_id
            db._notify_flow_subscribers(flow)
        except Exception:  # noqa: BLE001
            pass
        finally:
            state["done"].set()

    def _notify_flow_update(self, flow_id, status_code, resp_headers: Headers,
                            resp_body, duration_ms: int):
        """响应完成后推送 SSE 更新，让前端补齐预入库 flow 的响应字段。

        与 _insert_flow 的请求阶段 SSE 推送配合：请求阶段推送 lite flow（status=null），
        响应完成后再推送一次（带 status/headers/body/duration + _is_update 标记）。
        前端根据 _is_update 标记判断：
        - 绝不当成新 flow 插入（避免产生只有 id/status/size 的空行）
        - 若对应 id 的预入库 flow 尚未到达（时序竞态），暂存 pendingUpdates，等预入库到达后合并
        - 若已存在则 merge 字段并触发响应式重渲染
        """
        try:
            flow = {
                "id": flow_id,
                "status_code": status_code,
                "response_headers": json.dumps(resp_headers.to_dict()) if resp_headers else "{}",
                "response_body": resp_body,
                "duration_ms": duration_ms,
                "_is_update": True,
            }
            db._notify_flow_subscribers(flow)
        except Exception:  # noqa: BLE001
            pass

    def _record_flow(self, pid, proc_name, method, url, scheme, host, path,
                     headers: Headers, body: bytes, status, resp_headers: Headers,
                     resp_body_text, duration_ms=0, size=0,
                     remote_ip: str = "", ip_region: str = "",
                     cert_info: str = "", http_version: str = "",
                     timing: dict | None = None):
        if self.session_id is None:
            return
        # 触发式捕获：若 trigger 启用且未触发，检查是否匹配条件
        try:
            from ..trigger import get_trigger_manager
            _tm = get_trigger_manager()
            if _tm.enabled and not _tm.triggered:
                _check = {
                    "host": host, "method": method, "status_code": status,
                    "path": path, "process_name": proc_name, "url": url,
                    "protocol": "http",
                }
                if not _tm.should_record(_check):
                    return  # 未触发，不记录
        except Exception:  # noqa: BLE001
            pass
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
            "timing": json.dumps(timing) if timing else None,
        }
        # 在线程池中同步插入 + 触发 hooks（录制 + 被动扫描）
        # 不阻塞代理线程，同时确保 flow_id 可用于 hooks
        # 之前用 db.insert_flow_async（异步批量写入），拿不到 flow_id，
        # 导致录制 hook 和被动扫描 hook 无法触发（前端"录制中.0"、被动扫描无数据）
        def _insert_and_hook():
            try:
                flow_id = db.insert_flow(flow)
                if flow_id:
                    flow["id"] = flow_id
                    db._notify_flow_subscribers(flow)
                    self._run_post_record_hooks(
                        flow_id, url, method, scheme, host, path,
                        headers, status, resp_headers, resp_body_text)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._preinsert_executor.submit(_insert_and_hook)
        except Exception:  # noqa: BLE001
            # 线程池满或关闭时，回退到异步批量写入（不触发 hooks，但至少记录流量）
            db.insert_flow_async(flow)

    def _run_post_record_hooks(self, flow_id, url, method, scheme, host, path,
                               headers, status, resp_headers, resp_body):
        """响应记录完成后触发的后置 hook：录制 / 被动扫描。

        在所有响应记录路径（正常、流式、502 异常）统一调用，避免漏触发。
        """
        if not flow_id:
            return
        # 录制 hook：将 flow 加入录制集
        try:
            from ..api.record_replay import add_flow_to_recording as _add_to_recording
            _add_to_recording(flow_id)
        except Exception:  # noqa: BLE001
            pass

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
            self._run_post_record_hooks(flow_id, url, method, scheme, host, path,
                                        headers, status, resp_headers, resp_body)
        else:
            self._record_flow(pid, proc_name, method, url, scheme, host, path,
                              headers, body, status, resp_headers,
                              _truncate_for_record(resp_body), duration, len(resp_body),
                              remote_ip=remote_ip, ip_region=ip_region,
                              cert_info=cert_info, http_version=http_version)
            # 新插入的 flow 无法拿到 flow_id（异步插入），跳过 hooks；
            # 这些流量在 _record_flow 异步落库后不会被 hook，属于已知限制。

    # ---------- 自动回复匹配 ----------

    def _match_auto_reply(self, url: str, method: str | None = None,
                          status_code: int | None = None,
                          pid: int | None = None,
                          process_name: str | None = None):
        """§4.1 Rule matching supports method/status/pid/process filtering.

        Request phase: status_code is None (no response yet).
        Response phase: status_code may be passed for status code filtering.
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
    """§3.8 intercept delay action: scans modify_rules, matches rules with target=target_name,
    delays via time.sleep(value/1000) (value in milliseconds).

    target_name options:
    - "delay": response phase delay (executed before _apply_modify_response)
    - "delay-request": request phase delay (executed before forwarding)
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
            logger.info("proxy", f"{log_label} delay {ms}ms",
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


def _bounded_zlib_decompress(body: bytes, wbits: int) -> bytes:
    """增量解压 zlib/gzip 流，输出超过 MAX_DECOMPRESS_OUTPUT 时抛出异常（防解压炸弹）。"""
    d = zlib.decompressobj(wbits)
    out = bytearray()
    # 分块喂入并检查累计输出，避免一次性解出巨量数据
    chunk = 64 * 1024
    for i in range(0, len(body), chunk):
        out += d.decompress(body[i:i + chunk], MAX_DECOMPRESS_OUTPUT - len(out) + 1)
        if len(out) > MAX_DECOMPRESS_OUTPUT:
            raise ValueError(f"decompressed output exceeds limit {MAX_DECOMPRESS_OUTPUT}")
    out += d.flush()
    if len(out) > MAX_DECOMPRESS_OUTPUT:
        raise ValueError(f"decompressed output exceeds limit {MAX_DECOMPRESS_OUTPUT}")
    return bytes(out)


def _decompress_body(body: bytes, headers: Headers) -> tuple[bytes, Headers]:
    """Decompress the response body according to Content-Encoding and remove that header.
    Supports gzip / deflate / br (if the brotli library is available).

    Performance: when body exceeds MAX_DECOMPRESS_BODY (1MB), skips decompression and
    returns the raw bytes. Large bodies are usually video/image/download; decompression
    takes hundreds of ms and modify_response is meaningless for binary content.

    Security: decompressed output is capped at MAX_DECOMPRESS_OUTPUT to defend against
    decompression bombs (a tiny gzip input expanding to hundreds of MB).
    """
    enc = (headers.get("Content-Encoding") or "").lower().strip()
    if not enc or not body:
        return body, headers
    # 大响应体跳过解压：避免阻塞代理线程
    if len(body) > MAX_DECOMPRESS_BODY:
        logger.debug("proxy",
                     f"Skipped decompressing large response body: {len(body)} bytes > {MAX_DECOMPRESS_BODY} bytes, enc={enc}")
        return body, headers
    try:
        if "gzip" in enc:
            # gzip 格式：wbits = MAX_WBITS | 16
            body = _bounded_zlib_decompress(body, zlib.MAX_WBITS | 16)
        elif "deflate" in enc:
            try:
                body = _bounded_zlib_decompress(body, zlib.MAX_WBITS)
            except zlib.error:
                # 格式不符（raw deflate 无 zlib 头）才回退；炸弹(ValueError)会向上抛出并放弃解压
                body = _bounded_zlib_decompress(body, -zlib.MAX_WBITS)
        elif "br" in enc:
            try:
                import brotli
                body = brotli.decompress(body)
                if len(body) > MAX_DECOMPRESS_OUTPUT:
                    raise ValueError(f"brotli decompressed output exceeds limit {MAX_DECOMPRESS_OUTPUT}")
            except ImportError:
                logger.warning("proxy", "brotli library not installed, cannot decompress br response, modify_response will not work",
                               f"Content-Encoding: {enc}, body length: {len(body)}")
                return body, headers
            except Exception as e:
                logger.error("proxy", f"brotli decompress failed: {e}",
                             f"body first 50 bytes: {body[:50]}")
                return body, headers
        else:
            return body, headers
        headers.remove("Content-Encoding")
        headers.remove("Content-Length")
        headers.set("Content-Length", str(len(body)))
        logger.debug("proxy", f"Decompressed response body: {enc} -> {len(body)} bytes")
    except Exception as e:  # noqa: BLE001
        logger.error("proxy", f"Decompress response body failed: {e}", f"enc={enc}")
    return body, headers


def _is_plain_key(key: str) -> bool:
    """Determine whether the key is a plain field name (no path symbols like . [ $), for global search mode."""
    if not key:
        return False
    return not any(ch in key for ch in ".[$")


def _replace_all_keys(data, key: str, value):
    """Recursively traverse the entire JSON, replacing all fields named key with value."""
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
    """Recursively traverse the entire JSON, deleting all fields named key."""
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
    """Set a JSON field by dot path, supporting $.a.b.c / a.b.c / a.b[0].c.
    Auto-creates intermediate nodes when the path doesn't exist (dict/list decided by [n] index).
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
    """Delete a JSON field by dot path."""
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
    """Best-effort coercion of a string value into a JSON type (int/float/bool/null)."""
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
    """Apply modify_response rule to the response.

    Supports two structures:
    1. New structure (generated by the frontend RuleEditor):
       { target, op, key, value }
       - target: response_body | response_header
       - op:     replace | remove
       - key:    For response body, a JSON path (e.g. data.status.remainingUses);
                 for response header, the header name; empty means replace the whole body
       - value:  the new value
    2. Old structure (compatibility):
       { action: replace_body|replace_status|replace_header|remove_header, name, value }
    """
    modify_rules = _parse_json(rule.get("modify_rules"))
    if not isinstance(modify_rules, list):
        logger.warning("proxy", "modify_response rule parse failed or empty",
                       f"modify_rules={rule.get('modify_rules')!r}, parsed={modify_rules!r}")
        return status, headers, body
    logger.info("proxy", f"Applying modify_response, {len(modify_rules)} sub-rules total",
                f"body length={len(body)}, Content-Encoding={headers.get('Content-Encoding')}")
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
                            logger.info("proxy", f"remove field success: {key}")
                        except Exception as e:
                            logger.error("proxy", f"remove field failed: {key}", str(e))
                    elif op == "replace":
                        if key:
                            try:
                                text = body.decode("utf-8", "ignore")
                                data = json.loads(text)
                                logger.info("proxy", f"JSON parse success, top-level type={type(data).__name__}",
                                            f"key={key}, is_plain={_is_plain_key(key)}, "
                                            f"JSON first 200 chars={text[:200]}")
                                if _is_plain_key(key):
                                    data = _replace_all_keys(data, key, value)
                                else:
                                    data = _set_json_path(data, key, value)
                                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                                logger.info("proxy", f"replace field success: {key} -> {value}")
                            except json.JSONDecodeError as e:
                                logger.error("proxy", f"replace field failed (response body is not valid JSON): {key} -> {value}",
                                             f"JSON error: {e}, body first 200 bytes: {body[:200]!r}")
                            except Exception as e:
                                logger.error("proxy", f"replace field failed: {key} -> {value}",
                                             f"error: {e}, body first 200 bytes: {body[:200]!r}")
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
    """Apply modify_request rule to request headers/body.

    Supported targets:
    - request_header: op=replace/remove, key=header name, value=new value
    - request_body:   op=replace (JSON field replacement / full replacement) / remove (delete field) / replace-bytes (binary offset replacement)
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
                        logger.error("proxy", "replace-bytes failed", str(e))
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
    """Apply binary byte replacement rules (replace-bytes / replace-bytes-regex) to the response body."""
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

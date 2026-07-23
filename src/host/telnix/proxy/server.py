"""HTTP/HTTPS 抓包代理服务器（线程模型）。

每个客户端连接开一个线程。HTTP 直接解析转发；HTTPS（CONNECT 隧道）
用 SSL bump 动态签发证书解密。每个连接通过 GetExtendedTcpTable 反查 PID。
断点在代理层阻塞等待 API 放行。
"""

import json
import os
import re
import socket
import ssl
import threading
import time
import concurrent.futures
from datetime import datetime

# 记录到 DB 的 body 上限（字节）。超过则截断并加标记，避免大 body 阻塞代理线程
# （视频/图片流可能几 MB ~ 几十 MB，base64 编码后 3x 膨胀 + DB INSERT 慢）
MAX_RECORDED_BODY = 512 * 1024  # 512KB

# 大响应体阈值：超过此大小跳过解压，直接转发原始字节。
# 性能优化：解压几 MB 的 gzip/br 需几百 ms，阻塞代理线程。
# 大 body 通常是视频/图片/下载，modify_response 规则对二进制内容无意义。
# 仍会记录前 512KB（截断）用于 Inspector 查看，但不解压。
MAX_DECOMPRESS_BODY = 1024 * 1024  # 1MB


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
        import base64
        text = "base64:" + base64.b64encode(b).decode("ascii")
    if truncated:
        text += f"\n\n[... body 已截断，原始大小 {len(b)} 字节，仅记录前 {MAX_RECORDED_BODY} 字节 ...]"
    return text
from urllib.parse import urlsplit

from .. import db
from .breakpoint import BreakpointManager
from .process_lookup import ProcessLookup
from .ssl_bump import SSLBumpManager
from . import throttle


# ---------- DNS 预解析线程池（模块级单例） ----------
# 异步执行 socket.getaddrinfo + IP 属地查询，避免阻塞代理线程
# 线程池任务只做 DNS 解析 + 本地 IP 属地查询，不依赖代理线程，不会引入死锁
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="dns-resolver"
)


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
            from ..ip_region import lookup
            ip_region = lookup(remote_ip)
        except Exception:  # noqa: BLE001
            pass
    return remote_ip, ip_region


# ---------- 到目标服务器的连接池 ----------
class _PooledConn:
    """到目标服务器的可复用连接。

    存 reader 而非裸 sock，以保留 SocketReader 缓冲区中可能的多读数据。
    """
    def __init__(self, sock: socket.socket, reader: 'SocketReader',
                 host: str, port: int, scheme: str):
        self.sock = sock
        self.reader = reader
        self.host = host
        self.port = port
        self.scheme = scheme
        self.last_used = time.time()


class _ConnPool:
    """简单的 keep-alive 连接池，按 (host, port, scheme) 缓存连接。

    每个连接空闲超过 30 秒自动过期。每次最多缓存 4 个连接 per key。
    """
    _MAX_IDLE = 60.0  # 秒（增大以提升复用率）
    _MAX_PER_KEY = 32  # 性能优化：从 4 增大到 32，避免高并发下连接池耗尽导致新建连接

    def __init__(self):
        self._pool: dict[tuple, list[_PooledConn]] = {}
        self._lock = threading.Lock()

    def get(self, host: str, port: int, scheme: str) -> _PooledConn | None:
        key = (host, port, scheme)
        with self._lock:
            conns = self._pool.get(key)
            if not conns:
                return None
            while conns:
                conn = conns.pop()
                if time.time() - conn.last_used > self._MAX_IDLE:
                    try:
                        conn.sock.close()
                    except OSError:
                        pass
                    continue
                return conn
        return None

    def put(self, conn: _PooledConn):
        key = (conn.host, conn.port, conn.scheme)
        with self._lock:
            conns = self._pool.setdefault(key, [])
            if len(conns) >= self._MAX_PER_KEY:
                try:
                    conn.sock.close()
                except OSError:
                    pass
                return
            conns.append(conn)

    def close_all(self):
        with self._lock:
            for conns in self._pool.values():
                for c in conns:
                    try:
                        c.sock.close()
                    except OSError:
                        pass
            self._pool.clear()


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
    """大小写不敏感的头容器，保留原始顺序与大小写。"""

    def __init__(self):
        self._items: list[list[str]] = []  # [[name, value], ...]

    def add(self, name: str, value: str):
        self._items.append([name, value])

    def get(self, name: str, default=None):
        nl = name.lower()
        for k, v in self._items:
            if k.lower() == nl:
                return v
        return default

    def set(self, name: str, value: str):
        nl = name.lower()
        for it in self._items:
            if it[0].lower() == nl:
                it[1] = value
                return
        self._items.append([name, value])

    def remove(self, name: str):
        nl = name.lower()
        self._items = [it for it in self._items if it[0].lower() != nl]

    def has(self, name: str) -> bool:
        return self.get(name) is not None

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
        while True:
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

    def __init__(self, host: str = "127.0.0.1", port: int = 8888,
                 ssl_bump: SSLBumpManager | None = None):
        self.host = host
        self.port = port
        self.ssl_bump = ssl_bump
        self.process_lookup = ProcessLookup()
        self.breakpoint = BreakpointManager()
        self.capturing = False
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
        # 到目标服务器的 keep-alive 连接池（复用 TCP+TLS 连接，避免每次握手）
        self._conn_pool = _ConnPool()
        # HTTP/2 连接池（复用 h2 连接，支持多路复用）
        from .h2_forward import H2ClientPool
        self._h2_pool = H2ClientPool()
        # SSL bump 的 SSLContext 缓存（按 cert_path 缓存，避免每次创建）
        self._ssl_ctx_cache: dict[str, ssl.SSLContext] = {}
        self._ssl_ctx_lock = threading.Lock()
        # 转发到目标用的 SSL context（全局复用一个）
        self._forward_ssl_ctx = ssl.create_default_context()
        self._forward_ssl_ctx.check_hostname = False
        self._forward_ssl_ctx.verify_mode = ssl.CERT_NONE
        # HTTP/2 支持：到目标服务器的连接通过 ALPN 协商 h2 或 http/1.1
        self._forward_ssl_ctx.set_alpn_protocols(["h2", "http/1.1"])
        # 上游代理（Clash/Mihomo 集成用）：启用时所有出站连接先走 CONNECT 到上游代理
        # 由 clash.client.get_upstream_proxy() 动态返回 (host, port) 或 None
        self._upstream_proxy_override: tuple[str, int] | None = None

    def _get_upstream_proxy(self) -> tuple[str, int] | None:
        """获取当前上游代理地址。优先用运行时 override，否则从 settings 读。"""
        if self._upstream_proxy_override is not None:
            return self._upstream_proxy_override
        try:
            from ..clash.client import get_upstream_proxy
            return get_upstream_proxy()
        except Exception:  # noqa: BLE001
            return None

    def _connect_target(self, host: str, port: int, timeout: int = 30) -> socket.socket:
        """建立到目标服务器的连接。启用 Clash 时走上游代理 CONNECT 隧道。

        性能优化：
        - TCP_NODELAY 禁用 Nagle 算法，减少小数据包延迟
        - CONNECT 响应读取设独立超时（10s），避免代理异常时长时间卡死
        - 缓存由 clash.client.get_upstream_proxy() 提供，避免每次查文件
        """
        upstream = self._get_upstream_proxy()
        if upstream is None:
            return socket.create_connection((host, port), timeout=timeout)
        # 通过上游代理（Mihomo mixed-port）建立 CONNECT 隧道
        proxy_host, proxy_port = upstream
        s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
        try:
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
            # 读取代理响应时设独立超时（10s），避免代理异常长时间卡死
            s.settimeout(10)
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
            # 透传代理响应后的残余数据（通常无，但保险起见留在 socket 缓冲里）
        except OSError:
            try:
                s.close()
            except OSError:
                pass
            raise
        return s

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
            t = threading.Thread(
                target=self._handle_client_safe,
                args=(client_sock, client_addr),
                daemon=True,
            )
            t.start()

    def _handle_client_safe(self, client_sock: socket.socket, client_addr):
        try:
            self._handle_client(client_sock, client_addr)
        except Exception:  # noqa: BLE001
            # 单个请求异常不能让代理崩溃
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
        import re
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
        from ..auto_reply.rules import has_active_rules
        return has_active_rules()

    def _handle_client(self, client_sock: socket.socket, client_addr):
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

    # ---------- HTTPS CONNECT ----------

    def _handle_connect(self, client_sock, reader, connect_line, pid, proc_name):
        try:
            parts = connect_line.decode("latin-1").split()
            host_port = parts[1]
            host, _, port_s = host_port.partition(":")
            port = int(port_s) if port_s else 443
        except Exception:  # noqa: BLE001
            return

        # 是否需要 SSL bump：
        # - 抓包中（capture on）：bump 所有 HTTPS（用户主动要抓包）
        # - 未抓包但有启用的自动修改规则：仅 bump 匹配某条规则 pattern 的 host
        #   避免对所有 HTTPS 都 bump 导致钉扎站点（edge/bing/bilibili 等）断连
        # - 证书已装 + 非忽略进程 + 非专注外进程
        from ..auto_reply.rules import has_active_rules, host_matches_any_rule
        if self.capturing:
            need_bump = True
        elif has_active_rules():
            # 有规则但未抓包：只 bump 匹配规则的 host
            need_bump = host_matches_any_rule(host)
        else:
            need_bump = False
        do_bump = (
            need_bump
            and self.ssl_bump is not None
            and self.cert_installed
            and not self.is_ignored(pid, proc_name, host)
            and not self.is_focused_out(pid, host)
        )

        if not do_bump:
            # 纯隧道转发（不解密）
            # 先消耗 CONNECT 请求剩余的 HTTP 头，避免转发到目标导致协议错误
            reader.read_headers()
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

        # SSL bump：用动态签发的证书与客户端建立 TLS
        try:
            cert_path, key_path = self.ssl_bump.get_cert(host)
        except Exception:  # noqa: BLE001
            return
        # 缓存 SSLContext（按 cert_path），避免每次 CONNECT 都创建
        with self._ssl_ctx_lock:
            ssl_ctx = self._ssl_ctx_cache.get(cert_path)
            if ssl_ctx is None:
                ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ssl_ctx.load_cert_chain(cert_path, key_path)
                # 客户端侧 ALPN：只通告 http/1.1，强制客户端走 HTTP/1.1
                # 代理到客户端方向仅实现 HTTP/1.1 解析（h2 多路复用在代理→服务器方向已实现）
                # 这样客户端不会协商到 h2，避免 h2 preface 被误解析
                try:
                    ssl_ctx.set_alpn_protocols(["http/1.1"])
                except Exception:  # noqa: BLE001
                    pass
                self._ssl_ctx_cache[cert_path] = ssl_ctx
        try:
            tls_sock = ssl_ctx.wrap_socket(client_sock, server_side=True)
        except (ssl.SSLError, OSError) as e:
            # 证书钉扎（pinning）诊断：客户端拒绝我们的证书，通常是 pinning
            from .. import logger
            from datetime import datetime
            err_str = str(e).lower()
            is_pinning = any(k in err_str for k in ("tlsv1 alert", "handshake failure",
                                                    "certificate", "unknown ca", "bad certificate"))
            if is_pinning:
                logger.warning("proxy",
                               f"可能检测到证书钉扎（cert pinning）: host={host}, pid={pid}, proc={proc_name}",
                               f"TLS 握手失败: {e}。客户端可能在校验服务器证书，SSL bump 无法绕过。")
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
            except OSError:
                pass
            try:
                tls_sock.close()
            except OSError:
                pass

    def _tunnel(self, client_sock, reader, host, port):
        """纯隧道：不解密，直接把字节在客户端与目标之间双向转发。"""
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
        # 先把 reader 缓冲里剩余的字节发给目标
        leftover = bytes(reader.buf)
        if leftover:
            target.sendall(leftover)
        client_sock.settimeout(None)
        target.settimeout(None)

        def pipe(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    # 弱网模拟：按 drop_pct 概率丢包
                    if throttle.should_drop():
                        continue
                    # 弱网模拟：限速发送
                    throttle.send_throttled(dst, data)
            except OSError:
                pass
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        t1 = threading.Thread(target=pipe, args=(client_sock, target), daemon=True)
        t2 = threading.Thread(target=pipe, args=(target, client_sock), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        try:
            target.close()
        except OSError:
            pass

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
        while self._running:
            # first_line 为 None 时从连接读取下一行
            if line is None:
                try:
                    line = reader.read_line()
                except OSError:
                    break
                if not line:
                    break
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
        rule = self._match_auto_reply(
            orig_url, method=method, status_code=None,
            pid=pid, process_name=proc_name)
        if rule:
            from .. import logger
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
            from .. import logger
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
            from .. import logger
            # §3.8 delay-request 动作：在转发前延迟（毫秒）
            _apply_delay(rule, target_name="delay-request",
                         log_label="delay-request")
            # 记录原始值（前 200 字节），便于 agent 对比规则是否生效
            try:
                orig_headers_str = str(headers.to_dict())
                orig_body_preview = body[:200].hex() if body else "(empty)"
                orig_body_text = body[:200].decode("utf-8", errors="replace") if body else ""
            except Exception:  # noqa: BLE001
                orig_headers_str, orig_body_preview, orig_body_text = "(读取失败)", "", ""
            headers, body = _apply_modify_request(headers, body, rule)
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
            logger.info("proxy",
                        f"modify_request 已应用: host={host}, path={path}, rule_note={rule.get('note', '')}",
                        detail)

        # script：调用用户 Python 脚本的 on_request（请求阶段，转发前）
        # 脚本可修改请求头/体，或返回 mock（直接伪造响应）/ drop（拒绝）
        if rule and rule["action"] == "script":
            from .. import logger
            from ..auto_reply.script_runner import (
                call_script_request, build_ctx, apply_request)
            import base64
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
                    import traceback
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
        from .websocket_relay import is_websocket_upgrade, relay_websocket, compute_accept_key
        if is_websocket_upgrade(headers):
            return self._handle_websocket_upgrade(
                client_sock, host, port, scheme, method, path, orig_url,
                headers, body, pid, proc_name, remote_ip, ip_region,
                record, flow_id, keep_alive)
        try:
            status, resp_headers, resp_body, cert_info_json, http_version = self._forward(
                host, port, scheme, method, path, version, headers, body)
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
        if rule is None:
            rule = self._match_auto_reply(
                orig_url, method=method, status_code=status,
                pid=pid, process_name=proc_name)
            if rule:
                from .. import logger
                logger.info("proxy",
                            f"响应阶段匹配到规则: action={rule['action']}, pattern={rule['pattern']}",
                            f"URL={orig_url}, status={status}")
                # §3.2 命中计数（响应阶段匹配也计数）
                try:
                    db.increment_rule_hit(rule.get("id"), flow_id)
                except Exception:  # noqa: BLE001
                    pass
        if rule and rule["action"] == "modify_response":
            from .. import logger
            logger.info("proxy", "进入 modify_response 分支",
                        f"body_len={len(resp_body)}, Content-Encoding={resp_headers.get('Content-Encoding')}, "
                        f"Content-Type={resp_headers.get('Content-Type')}, "
                        f"body 前 200 字节={resp_body[:200]!r}")
            # §3.8 delay 动作：在响应前延迟（毫秒）
            _apply_delay(rule, target_name="delay", log_label="delay")
            status, resp_headers, resp_body = _apply_modify_response(
                status, resp_headers, resp_body, rule)
            logger.info("proxy", "modify_response 处理完成",
                        f"body_len={len(resp_body)}, body 前 200 字节={resp_body[:200]!r}")

        # script：调用用户 Python 脚本的 on_response（响应阶段，返回客户端前）
        # 脚本可修改响应头/体/状态码，或返回 drop（替换为 403）/ mock（替换为伪造响应）
        if rule and rule["action"] == "script":
            from .. import logger
            from ..auto_reply.script_runner import (
                call_script_response, build_ctx, apply_response)
            import base64
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

        try:
            target = self._connect_target(host, port)
            if scheme == "https":
                target = self._forward_ssl_ctx.wrap_socket(target, server_hostname=host)
            target.settimeout(30)
        except OSError:
            self._send_simple(client_sock, 502, "Bad Gateway")
            if record and flow_id:
                db.update_flow_response_async(flow_id, 502, "{}", "", 0, 0)
            return False

        # 构造 Upgrade 请求发送给服务器
        fwd_headers = Headers()
        for k, v in headers._items:  # noqa: SLF001
            kl = k.lower()
            if kl in ("proxy-connection", "proxy-authorization",
                      "transfer-encoding", "content-length"):
                continue
            fwd_headers.add(k, v)
        fwd_headers.set("Connection", "Upgrade")
        fwd_headers.set("Content-Length", "0")

        req_line = f"{method} {path} HTTP/1.1\r\n".encode("latin-1", "replace")
        req_data = req_line + fwd_headers.to_bytes() + b"\r\n"
        try:
            throttle.send_throttled(target, req_data)
        except OSError:
            try:
                target.close()
            except OSError:
                pass
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
            try:
                target.close()
            except OSError:
                pass
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
            try:
                target.close()
            except OSError:
                pass
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
            try:
                target.close()
            except OSError:
                pass
            return False

        # 101 成功：转为双向帧转发
        # treader.buf 可能已有服务器发来的 WS 帧数据（紧跟 101 响应后）
        # 把缓冲区剩余数据写回 target，让帧读取器从 socket 读
        # （实际上 101 响应后服务器不会立即发数据，等待客户端首个 WS 帧）
        from .. import logger
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
        return False  # WS 连接结束后不再 keep-alive

    def _h2_request_via_pool(self, host, port, scheme, method, path,
                             fwd: Headers, body: bytes):
        """尝试通过 h2 连接池发送请求（复用已有 h2 连接）。

        成功返回 (status, resp_headers, resp_body, "", "HTTP/2")。
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
            resp_body, resp_headers = _decompress_body(resp_body, resp_headers)
            self._h2_pool.record_request()
            return status, resp_headers, resp_body, "", "HTTP/2"
        except Exception:  # noqa: BLE001
            # h2 连接出错，从池中移除，让调用方新建连接
            self._h2_pool.remove(host, port, scheme)
            return None

    def _h2_create_and_request(self, target, host, port, scheme, method, path,
                                fwd: Headers, body: bytes, cert_info_json: str):
        """在新建的 TLS 连接上创建 H2Client 并发送请求。

        成功返回 5-tuple（H2Client 已放入池，可被后续请求复用）。
        失败返回 None（连接已关闭，调用方需新建连接回退 HTTP/1.1）。
        """
        from .h2_forward import H2Client
        try:
            h2_client = H2Client(target, host, port, scheme)
            h2_headers = [(k, v) for k, v in fwd.to_dict().items()]
            status, resp_h, resp_trailers, resp_body = h2_client.request(
                method, scheme, host, path, h2_headers, body)
            # 请求成功后才放入池（避免请求中途被另一个 put close 掉）
            if not self._h2_pool.put(h2_client):
                # 池中已有连接，关闭这个（reader 线程会退出）
                h2_client.close()
            resp_headers = Headers.from_dict(dict(resp_h))
            # trailers 合并到 resp_headers（代理场景简化处理）
            for k, v in resp_trailers:
                resp_headers.add(k, v)
            resp_body, resp_headers = _decompress_body(resp_body, resp_headers)
            self._h2_pool.record_request()
            return status, resp_headers, resp_body, cert_info_json, "HTTP/2"
        except Exception:  # noqa: BLE001
            # h2 创建/请求失败，直接关闭连接（还没放入池，不需要 remove）
            try:
                target.close()
            except OSError:
                pass
            return None

    def _forward(self, host, port, scheme, method, path, version, headers, body):
        """转发请求到目标服务器，返回 (status, resp_headers, resp_body, cert_info_json, http_version)。

        优先复用 h2 连接池（多路复用），其次复用 HTTP/1.1 连接池，最后新建连接。
        cert_info_json：HTTPS 流量的对端证书信息 JSON 字符串（首次握手时获取，复用连接返回空）
        http_version：'HTTP/1.1' 或 'HTTP/2'（ALPN 协商到 h2 时走 h2 转发）
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

        req_line = f"{method} {path} HTTP/1.1\r\n".encode("latin-1", "replace")
        req_data = req_line + fwd.to_bytes() + b"\r\n" + body

        # ---- 优先尝试 h2 连接池（复用已有 h2 连接，多路复用） ----
        if scheme == "https":
            result = self._h2_request_via_pool(host, port, scheme, method, path, fwd, body)
            if result is not None:
                return result

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

        try:
            # 池中无连接：新建 TCP+TLS
            cert_info_json = ""
            if target is None:
                # 弱网模拟：新建连接时加 RTT 延迟（模拟首包延迟）
                throttle.delay()
                # 弱网模拟：按 drop_pct 概率丢包（模拟连接失败）
                if throttle.should_drop():
                    raise OSError("throttle drop: connection dropped")
                target = self._connect_target(host, port)
                if scheme == "https":
                    target = self._forward_ssl_ctx.wrap_socket(
                        target, server_hostname=host)
                    # 获取对端证书信息（仅首次握手，复用连接不重复获取）
                    try:
                        cert_der = target.getpeercert(binary_form=True)
                        if cert_der:
                            from ..cert_info import get_cert_info
                            info = get_cert_info(cert_der)
                            if info:
                                cert_info_json = json.dumps(info)
                    except Exception:  # noqa: BLE001
                        pass
                    # HTTP/2：ALPN 协商到 h2 时创建 H2Client（放入池，后续可复用）
                    try:
                        alpn = target.selected_alpn_protocol()
                    except (AttributeError, OSError):
                        alpn = None
                    if alpn == "h2":
                        result = self._h2_create_and_request(
                            target, host, port, scheme, method, path, fwd, body, cert_info_json)
                        if result is not None:
                            return result
                        # h2 失败，回退到新建 HTTP/1.1 连接
                        target = self._connect_target(host, port)
                        target = self._forward_ssl_ctx.wrap_socket(
                            target, server_hostname=host)
                target.settimeout(30)
                treader = SocketReader(target)

            # 弱网模拟：限速发送请求
            throttle.send_throttled(target, req_data)

            status_line = treader.read_line()
            if not status_line:
                # 连接已失效（池中的连接可能被服务器关闭），重试一次
                if reused:
                    try:
                        target.close()
                    except OSError:
                        pass
                    target = self._connect_target(host, port)
                    if scheme == "https":
                        target = self._forward_ssl_ctx.wrap_socket(
                            target, server_hostname=host)
                        try:
                            cert_der = target.getpeercert(binary_form=True)
                            if cert_der:
                                from ..cert_info import get_cert_info
                                info = get_cert_info(cert_der)
                                if info:
                                    cert_info_json = json.dumps(info)
                        except Exception:  # noqa: BLE001
                            pass
                        # 重试连接也检查 ALPN
                        try:
                            alpn = target.selected_alpn_protocol()
                        except (AttributeError, OSError):
                            alpn = None
                        if alpn == "h2":
                            result = self._h2_create_and_request(
                                target, host, port, scheme, method, path, fwd, body, cert_info_json)
                            if result is not None:
                                return result
                            # h2 失败，回退
                            target = self._connect_target(host, port)
                            target = self._forward_ssl_ctx.wrap_socket(
                                target, server_hostname=host)
                    target.settimeout(30)
                    treader = SocketReader(target)
                    # 弱网模拟：重试时也限速发送
                    throttle.send_throttled(target, req_data)
                    status_line = treader.read_line()
                    if not status_line:
                        return 0, Headers(), b"", cert_info_json, "HTTP/1.1"
                else:
                    return 0, Headers(), b"", cert_info_json, "HTTP/1.1"

            sp = status_line.decode("latin-1", "replace").split(" ", 2)
            status_code = int(sp[1]) if len(sp) >= 2 and sp[1].isdigit() else 0
            resp_header_lines = treader.read_headers()
            resp_headers = Headers.from_lines(resp_header_lines)
            resp_body = read_body(treader, resp_headers, method=method,
                                  status_code=status_code)
            # 解压响应体，便于后续修改/记录
            resp_body, resp_headers = _decompress_body(resp_body, resp_headers)

            # 判断连接是否可复用：服务器未返回 close
            resp_conn = (resp_headers.get("Connection") or "").lower()
            can_reuse = "close" not in resp_conn
            if can_reuse:
                self._conn_pool.put(_PooledConn(target, treader, host, port, scheme))
            else:
                try:
                    target.close()
                except OSError:
                    pass
            return status_code, resp_headers, resp_body, cert_info_json, "HTTP/1.1"
        except (OSError, ssl.SSLError):
            # 出错时关闭连接（池中复用的连接已被消耗）
            try:
                if target is not None:
                    target.close()
            except OSError:
                pass
            raise

    # ---------- 响应发送 ----------

    def _send_response(self, client_sock, status_code, headers: Headers,
                       body: bytes, keep_alive: bool):
        headers = Headers.from_dict(headers.to_dict())
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
            from ..auto_reply.rules import find_matching_rule
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
            from .. import logger
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
        import base64
        return "base64:" + base64.b64encode(b).decode("ascii")


def _to_bytes(s) -> bytes:
    if not s:
        return b""
    if isinstance(s, bytes):
        return s
    if s.startswith("base64:"):
        import base64
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
    from .. import logger
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
            import gzip
            body = gzip.decompress(body)
        elif "deflate" in enc:
            import zlib
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
    from .. import logger
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
    from .. import logger
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
    from .. import logger
    modify_rules = _parse_json(rule.get("modify_rules"))
    if not isinstance(modify_rules, list):
        return body
    import re
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

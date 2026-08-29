"""DNS hijack backend - Unix cross-platform implementation (local DNS server + iptables/pf redirect).

Replaces Windows WinDivert DNS hijack on macOS / Linux:
- Starts a local DNS server (listening on 127.0.0.1:5354)
- Uses iptables/pf to redirect UDP 53 to local DNS server port
- DNS server resolves queries, returns fake_ip when matching rules, otherwise forwards to upstream DNS

Core differences from Windows version:
- Windows: WinDivert intercepts UDP 53 response packets, modifies A record RDATA
- Unix: Local DNS server directly constructs responses (cleaner, no need to modify original response packets)

Advantages:
- No dependency on WinDivert kernel driver
- Local DNS server can cache query results (performance improvement)
- Rule matching logic is simpler (directly construct response)

Limitations:
- Need to modify system DNS config or use iptables/pf to redirect port 53
- Only hijacks UDP DNS (TCP DNS not handled for now, most clients use UDP)
"""

from __future__ import annotations

import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

from .. import logger

IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_UNIX = IS_LINUX or IS_MACOS

# 单条规则通配符数量上限（防止 ReDoS）
# 实测：20 个 *a* 交替的通配符 + 200 字符域名即可触发 >3s 卡顿；
# 8 个通配符在最坏输入下 <0.1ms，覆盖所有合理用例（典型 1-2 个）。
_MAX_WILDCARDS_PER_PATTERN = 8

# 本地 DNS 服务器监听端口（用 5354 避免与系统 DNS 冲突）
_LOCAL_DNS_PORT = 5354

# 上游 DNS 服务器列表（用于转发未匹配规则的查询）
_UPSTREAM_DNS = ["8.8.8.8", "1.1.1.1", "114.114.114.114"]

# pf anchor 文件路径（DNS 劫持用）
_PF_DNS_ANCHOR_FILE = "/etc/pf.anchors/telnix-dns"
_PF_DNS_ANCHOR_NAME = "telnix-dns"

# 复用 dns_hijack.py 的日志和统计模块
try:
    from .dns_hijack import (
        _HIJACK_LOG, _HIJACK_LOG_LOCK, _STATS, _STATS_LOCK,
        _log_hijack, get_hijack_log, get_stats, _reset_stats,
    )
except ImportError:
    # dns_hijack.py 导入失败时使用本地备份（不应该发生）
    _HIJACK_LOG = deque(maxlen=200)
    _HIJACK_LOG_LOCK = threading.Lock()
    _STATS = {
        "total_packets": 0,
        "hijacked_packets": 0,
        "skipped_no_match": 0,
        "errors": 0,
    }
    _STATS_LOCK = threading.Lock()

    def _log_hijack(domain, original_ips, new_ip, src):
        entry = {
            "ts": datetime.now().isoformat(),
            "domain": domain,
            "original_ips": original_ips,
            "new_ip": new_ip,
            "dns_server": src,
        }
        with _HIJACK_LOG_LOCK:
            _HIJACK_LOG.append(entry)

    def get_hijack_log():
        with _HIJACK_LOG_LOCK:
            return list(_HIJACK_LOG)

    def get_stats():
        with _STATS_LOCK:
            return dict(_STATS)

    def _reset_stats():
        with _STATS_LOCK:
            for k in _STATS:
                _STATS[k] = 0


class LocalDnsHijacker:
    """Unix platform DNS hijack backend (local DNS server + iptables/pf redirect).

    External interface consistent with Windows DnsHijacker class:
    - start() / stop() / status()
    - set_rules() / get_rules()
    - running / last_error properties
    """

    def __init__(self):
        self._running = False
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._last_error: str = ""
        self._rules: dict[str, str] = {}
        # 预编译通配符正则：[(compiled_regex, ip), ...]（仅含含 * 或 ? 的规则）
        self._compiled_patterns: list[tuple[re.Pattern, str]] = []
        self._default_ip: str = ""
        self._enabled: bool = True
        self._lock = threading.RLock()
        # 已添加的 iptables 规则
        self._iptables_rules: list[tuple[str, ...]] = []
        # pf 是否已加载
        self._pf_loaded = False
        # DNS 查询缓存（域名+qtype → (响应包, 过期时间)）
        self._cache: dict[tuple[str, int], tuple[bytes, float]] = {}
        self._cache_lock = threading.Lock()
        self._cache_ttl = 60.0  # 缓存 60 秒

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> str:
        return self._last_error

    def set_rules(self, rules: dict[str, str], default_ip: str = ""):
        """Update hijack rules (interface consistent with Windows version)."""
        with self._lock:
            self._rules = {
                k.strip().lower(): v.strip()
                for k, v in rules.items()
                if k.strip() and v.strip()
            }
            self._default_ip = default_ip.strip()
            # 预编译含通配符的规则为正则（性能优化：避免每个 DNS 包都重新编译）
            self._compiled_patterns = []
            for pattern, ip in self._rules.items():
                if '*' in pattern or '?' in pattern:
                    # 防止 ReDoS：通配符过多会导致正则引擎指数级回溯
                    # （20 个 *a* + 200 字符域名实测 >3s 卡顿）
                    if pattern.count('*') + pattern.count('?') > _MAX_WILDCARDS_PER_PATTERN:
                        logger.warning("dns_hijack", "Too many rule wildcards, skipped compilation",
                                       f"pattern={pattern!r} count={pattern.count('*') + pattern.count('?')}"
                                       f" max={_MAX_WILDCARDS_PER_PATTERN}")
                        continue
                    regex_str = '^' + re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.') + '$'
                    try:
                        self._compiled_patterns.append((re.compile(regex_str, re.IGNORECASE), ip))
                    except re.error:
                        pass

    def get_rules(self) -> tuple[dict[str, str], str]:
        with self._lock:
            return dict(self._rules), self._default_ip

    def _match_rule(self, domain: str) -> str | None:
        """Match domain, return hijack IP or None (logic consistent with Windows version)."""
        d = domain.strip().lower()
        if not d:
            return None
        with self._lock:
            # 1. 精确匹配（O(1)，最快）
            if d in self._rules:
                return self._rules[d]
            # 2. 通配符匹配（预编译正则，支持 *baidu* / *.baidu.com 等任意通配符）
            for regex, ip in self._compiled_patterns:
                if regex.match(d):
                    return ip
            # 3. 默认 IP（如果设置了）
            if self._default_ip:
                return self._default_ip
        return None

    def _is_admin(self) -> bool:
        try:
            return os.geteuid() == 0
        except AttributeError:
            return False

    def start(self) -> tuple[bool, str]:
        """Start DNS hijack."""
        if not IS_UNIX:
            return False, "LocalDnsHijacker only supports Linux/macOS"
        if self._running:
            return True, "Already running"
        if not self._is_admin():
            msg = "DNS hijack requires root privileges (iptables/pf requires root)"
            self._last_error = msg
            logger.error("dns_hijack", "Unix DNS hijack requires root", "Please start Telnix with sudo")
            return False, msg

        try:
            # Start local DNS server
            if not self._start_dns_server():
                return False, self._last_error

            # Add iptables/pf redirect rules
            if IS_LINUX:
                if not self._setup_iptables_redirect():
                    self._stop_dns_server()
                    return False, self._last_error
            else:
                if not self._setup_pf_redirect():
                    self._stop_dns_server()
                    return False, self._last_error

            self._running = True
            backend = "iptables+local_dns" if IS_LINUX else "pf+local_dns"
            logger.info("dns_hijack", f"DNS hijack started ({backend})",
                        f"local_dns=127.0.0.1:{_LOCAL_DNS_PORT}")
            return True, f"DNS hijack started ({backend})"
        except Exception as e:  # noqa: BLE001
            self._last_error = str(e)
            logger.error("dns_hijack", "Unix DNS hijack start failed", str(e))
            self._cleanup_rules()
            self._stop_dns_server()
            return False, f"Start failed: {e}"

    def _start_dns_server(self) -> bool:
        """Start local DNS server (listening on 127.0.0.1:5354)."""
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("127.0.0.1", _LOCAL_DNS_PORT))
            self._sock.settimeout(1.0)  # 1 秒超时，便于响应 stop
        except OSError as e:
            self._last_error = f"Local DNS server start failed: {e} (port {_LOCAL_DNS_PORT} may be occupied)"
            return False
        # 启动 DNS 服务器线程
        self._thread = threading.Thread(target=self._dns_loop, daemon=True, name="local-dns")
        self._thread.start()
        return True

    def _setup_iptables_redirect(self) -> bool:
        """Linux: use iptables NAT to redirect UDP 53 to local DNS port."""
        self._iptables_rules = []
        # OUTPUT 链：本机出站 UDP 53 重定向
        rule = (
            "iptables", "-t", "nat", "-A", "OUTPUT",
            "-p", "udp",
            "--dport", "53",
            "!", "-d", "127.0.0.0/8",
            "-j", "REDIRECT",
            "--to-port", str(_LOCAL_DNS_PORT),
        )
        try:
            result = subprocess.run(rule, capture_output=True, timeout=5)
            if result.returncode != 0:
                self._last_error = f"iptables add rule failed: {result.stderr.decode('utf-8', errors='replace')}"
                self._cleanup_rules()
                return False
            self._iptables_rules.append(rule)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            self._last_error = f"iptables unavailable: {e}"
            return False
        return True

    def _setup_pf_redirect(self) -> bool:
        """macOS: use pf rdr to redirect UDP 53 to local DNS port."""
        # Write anchor rule file
        pf_rules = (
            f"# Telnix DNS hijack rules (auto-generated, do not edit manually)\n"
            f"rdr pass on lo0 proto udp from any to any port 53 -> 127.0.0.1 port {_LOCAL_DNS_PORT}\n"
        )
        try:
            with open(_PF_DNS_ANCHOR_FILE, "w", encoding="utf-8") as f:
                f.write(pf_rules)
        except OSError as e:
            self._last_error = f"Failed to write pf anchor file: {e}"
            return False

        # 加载 anchor
        try:
            result = subprocess.run(
                ["pfctl", "-a", _PF_DNS_ANCHOR_NAME, "-f", _PF_DNS_ANCHOR_FILE],
                capture_output=True, timeout=5,
            )
            if result.returncode != 0:
                self._last_error = f"pfctl load anchor failed: {result.stderr.decode('utf-8', errors='replace')}"
                self._cleanup_rules()
                return False
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            self._last_error = f"pfctl unavailable: {e}"
            return False

        # Check if main config references the anchor
        try:
            result = subprocess.run(
                ["pfctl", "-s", "rules"],
                capture_output=True, timeout=5,
            )
            rules_output = result.stdout.decode("utf-8", errors="replace")
            if f'anchor "{_PF_DNS_ANCHOR_NAME}"' not in rules_output:
                self._last_error = (
                    f"pf main config does not reference anchor '{_PF_DNS_ANCHOR_NAME}'."
                    f"Please add to /etc/pf.conf: anchor \"{_PF_DNS_ANCHOR_NAME}\","
                    f"then run: sudo pfctl -f /etc/pf.conf"
                )
                self._cleanup_rules()
                return False
        except subprocess.TimeoutExpired:
            self._last_error = "pfctl query rules timeout"
            self._cleanup_rules()
            return False

        self._pf_loaded = True
        return True

    def _dns_loop(self):
        """Local DNS server main loop."""
        while self._running:
            try:
                try:
                    data, client_addr = self._sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError as e:
                    if self._running:
                        logger.error("dns_hijack", "Local DNS server recv failed", str(e))
                    break
                # 处理查询（在主循环中同步处理，避免线程开销）
                try:
                    self._handle_dns_query(data, client_addr)
                except Exception as e:  # noqa: BLE001
                    with _STATS_LOCK:
                        _STATS["errors"] += 1
                    if self._running:
                        logger.error("dns_hijack", "DNS query handling exception", str(e))
            except Exception as e:  # noqa: BLE001
                if self._running:
                    logger.error("dns_hijack", "DNS loop exception", str(e))
                    time.sleep(0.1)

    def _handle_dns_query(self, data: bytes, client_addr: tuple):
        """Handle a single DNS query."""
        with _STATS_LOCK:
            _STATS["total_packets"] += 1

        if len(data) < 12:
            with _STATS_LOCK:
                _STATS["errors"] += 1
            return

        # 解析 DNS 查询
        try:
            query_info = self._parse_dns_query(data)
        except Exception as e:  # noqa: BLE001
            with _STATS_LOCK:
                _STATS["errors"] += 1
            return

        domain = query_info["domain"]
        qtype = query_info["qtype"]

        # 匹配规则
        fake_ip = self._match_rule(domain)

        if fake_ip and qtype == 1:  # A 记录查询 + 命中规则
            # 构造 A 记录响应
            response = self._build_a_response(data, query_info, fake_ip)
            if response:
                self._sock.sendto(response, client_addr)
                with _STATS_LOCK:
                    _STATS["hijacked_packets"] += 1
                _log_hijack(domain, [], fake_ip, f"local:{_LOCAL_DNS_PORT}")
                return

        if fake_ip and qtype == 28:  # AAAA 查询 + 命中规则 → 返回空响应强制 IPv4
            response = self._build_empty_response(data, query_info)
            if response:
                self._sock.sendto(response, client_addr)
                with _STATS_LOCK:
                    _STATS["hijacked_packets"] += 1
                _log_hijack(domain, [], fake_ip, f"local:{_LOCAL_DNS_PORT} (AAAA->empty)")
                return

        # 未命中规则：转发给上游 DNS 服务器
        with _STATS_LOCK:
            _STATS["skipped_no_match"] += 1
        self._forward_to_upstream(data, client_addr, query_info)

    def _parse_dns_query(self, data: bytes) -> dict:
        """Parse DNS query packet, extract domain and qtype."""
        # DNS header: ID(2) + FLAGS(2) + QDCOUNT(2) + ANCOUNT(2) + NSCOUNT(2) + ARCOUNT(2)
        # Query: QNAME(变长) + QTYPE(2) + QCLASS(2)
        offset = 12
        # QNAME 是 label 序列，每个 label 前有长度字节，以 0 结尾
        labels = []
        while offset < len(data):
            label_len = data[offset]
            if label_len == 0:
                offset += 1
                break
            offset += 1
            labels.append(data[offset:offset + label_len].decode("ascii", errors="replace"))
            offset += label_len
        domain = ".".join(labels)
        # QTYPE(2) + QCLASS(2)
        if offset + 4 > len(data):
            return {"domain": domain, "qtype": 0, "qclass": 0, "query_end": offset}
        qtype, qclass = struct.unpack("!HH", data[offset:offset + 4])
        return {
            "domain": domain,
            "qtype": qtype,
            "qclass": qclass,
            "query_end": offset + 4,
        }

    def _build_a_response(self, query: bytes, query_info: dict, ip: str) -> bytes:
        """Build A record response packet."""
        # 截断查询包至 Question 段末尾，丢弃可能的 Additional 段（如 EDNS0 OPT）。
        # 否则 ARCOUNT=0 但包体仍含 Additional 数据，造成 DNS 包不一致。
        query_end = query_info.get("query_end", len(query))
        response = bytearray(query[:query_end])
        # 修改 FLAGS：QR=1（响应），RD=1（递归），AA=0，TC=0，RA=1
        # byte 2: QR(1) + Opcode(4) + AA(1) + TC(1) + RD(1) = 0x81 (response + RD)
        response[2] = 0x81
        # byte 3: RA(1) + Z(3) + RCODE(4) = 0x80 (RA + no error)
        response[3] = 0x80
        # ANCOUNT = 1（1 个回答）
        struct.pack_into("!H", response, 6, 1)
        # NSCOUNT = 0
        struct.pack_into("!H", response, 8, 0)
        # ARCOUNT = 0
        struct.pack_into("!H", response, 10, 0)
        # 追加 Answer 段
        # Answer: NAME(指针 0xC00C) + TYPE(2, A=1) + CLASS(2, IN=1) + TTL(4) + RDLENGTH(2, 4) + RDATA(4, IP)
        answer = struct.pack("!HHHIH", 0xC00C, 1, 1, 60, 4) + socket.inet_aton(ip)
        response.extend(answer)
        return bytes(response)

    def _build_empty_response(self, query: bytes, query_info: dict) -> bytes:
        """Build empty response (used for AAAA to force IPv4 fallback)."""
        # 截断查询包至 Question 段末尾，丢弃可能的 Additional 段
        query_end = query_info.get("query_end", len(query))
        response = bytearray(query[:query_end])
        response[2] = 0x81
        response[3] = 0x80
        # ANCOUNT = 0, NSCOUNT = 0, ARCOUNT = 0
        struct.pack_into("!H", response, 6, 0)
        struct.pack_into("!H", response, 8, 0)
        struct.pack_into("!H", response, 10, 0)
        return bytes(response)

    @staticmethod
    def _rewrite_response_id(query: bytes, response: bytes) -> bytes:
        """Replace the DNS transaction id of upstream response with current query's id.

        The cached response packet was returned by upstream during the first query, and its TXID belongs to that first client.
        Directly reusing it would cause TXID mismatch for subsequent clients, and the response would be discarded by the resolver. Here we only rewrite
        the first 2 bytes of id in the header, the rest (including compression pointers) remain consistent.
        """
        if len(query) < 2 or len(response) < 2:
            return response
        out = bytearray(response)
        out[0] = query[0]
        out[1] = query[1]
        return bytes(out)

    def _forward_to_upstream(self, query: bytes, client_addr: tuple, query_info: dict):
        """Forward query to upstream DNS server and return response."""
        # 缓存检查
        cache_key = (query_info["domain"], query_info["qtype"])
        now = time.time()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                response, expire = cached
                if now < expire:
                    try:
                        # 缓存只保存上游解析出的答案 RDATA，回送前用当前客户端的
                        # 查询 transaction id 重写响应头，避免不同客户端收到错误的 TXID
                        # 而被解析器丢弃（表现为间歇性解析失败）
                        self._sock.sendto(
                            self._rewrite_response_id(query, response), client_addr
                        )
                        return
                    except OSError:
                        pass
                else:
                    self._cache.pop(cache_key, None)

        # 转发给上游
        for upstream in _UPSTREAM_DNS:
            try:
                forward_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                forward_sock.settimeout(2.0)
                try:
                    forward_sock.sendto(query, (upstream, 53))
                    response, _ = forward_sock.recvfrom(4096)
                    # 返回给客户端（用该客户端的查询 id）
                    self._sock.sendto(
                        self._rewrite_response_id(query, response), client_addr
                    )
                    # 缓存（只缓存答案数据，回送时再用各自的查询 id 重写）
                    with self._cache_lock:
                        self._cache[cache_key] = (response, now + self._cache_ttl)
                        # 清理过期缓存（每 100 次添加清理一次）
                        if len(self._cache) > 1000:
                            expired_keys = [k for k, (_, exp) in self._cache.items() if now >= exp]
                            for k in expired_keys:
                                self._cache.pop(k, None)
                    return
                finally:
                    forward_sock.close()
            except (OSError, socket.timeout):
                continue
        # 所有上游都失败：返回 SERVFAIL
        servfail = bytearray(query)
        servfail[2] = 0x81
        servfail[3] = 0x82  # RA + SERVFAIL
        struct.pack_into("!H", servfail, 6, 0)
        try:
            self._sock.sendto(bytes(servfail), client_addr)
        except OSError:
            pass

    def stop(self) -> tuple[bool, str]:
        """Stop DNS hijack. Returns (success, msg) consistent with Windows version interface."""
        if not self._running:
            return True, "Not running"
        self._running = False
        self._cleanup_rules()
        self._stop_dns_server()
        backend = "iptables+local_dns" if IS_LINUX else "pf+local_dns"
        logger.info("dns_hijack", f"DNS hijack stopped ({backend})")
        return True, "DNS hijack stopped"

    def _stop_dns_server(self):
        """Stop local DNS server."""
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            self._sock = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def _cleanup_rules(self):
        """Clean up iptables/pf rules."""
        # Linux: 删除 iptables 规则
        for rule in self._iptables_rules:
            del_rule = list(rule)
            if "-A" in del_rule:
                idx = del_rule.index("-A")
                del_rule[idx] = "-D"
            try:
                subprocess.run(del_rule, capture_output=True, timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
        self._iptables_rules.clear()

        # macOS: 卸载 pf anchor
        if self._pf_loaded:
            try:
                subprocess.run(
                    ["pfctl", "-a", _PF_DNS_ANCHOR_NAME, "-d"],
                    capture_output=True, timeout=5,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass
            self._pf_loaded = False

        # 删除 anchor 文件
        try:
            if os.path.exists(_PF_DNS_ANCHOR_FILE):
                os.unlink(_PF_DNS_ANCHOR_FILE)
        except OSError:
            pass

    def status(self) -> dict:
        """Return DNS hijack status (fields consistent with Windows version)."""
        return {
            "running": self._running,
            "last_error": self._last_error,
            "rules": dict(self._rules),
            "default_ip": self._default_ip,
            "stats": get_stats(),
            "log": get_hijack_log(),
            "is_admin": self._is_admin(),
            "is_windows": False,
            "backend": "iptables+local_dns" if IS_LINUX else ("pf+local_dns" if IS_MACOS else "none"),
            "supported": IS_UNIX,  # Unix 平台 Linux/macOS 均支持
        }


def is_unix_dns_hijack_available() -> bool:
    """Check if Unix DNS hijack backend is available."""
    if not IS_UNIX:
        return False
    try:
        if IS_LINUX:
            subprocess.run(["iptables", "--version"], capture_output=True, check=True, timeout=5)
        else:
            subprocess.run(["pfctl", "-s", "info"], capture_output=True, check=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def get_unix_dns_hijack_status() -> dict:
    """Return Unix DNS hijack backend status."""
    try:
        is_admin = os.geteuid() == 0
    except AttributeError:
        is_admin = False
    return {
        "running": False,
        "supported": True,
        "backend": "iptables+local_dns" if IS_LINUX else ("pf+local_dns" if IS_MACOS else "none"),
        "is_admin": is_admin,
        "hint": ("Ready" if is_admin else "Root privileges required") if IS_UNIX else "Unsupported platform",
    }

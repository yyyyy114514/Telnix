"""DNS 劫持后端 - Unix 跨平台实现（本地 DNS 服务器 + iptables/pf 重定向）。

在 macOS / Linux 上替代 Windows 的 WinDivert DNS 劫持：
- 启动一个本地 DNS 服务器（监听 127.0.0.1:5354）
- 用 iptables/pf 把 UDP 53 重定向到本地 DNS 服务器端口
- DNS 服务器解析查询，匹配规则时返回 fake_ip，否则转发给上游 DNS

与 Windows 版本的核心差异：
- Windows: WinDivert 拦截 UDP 53 响应包，修改 A 记录 RDATA
- Unix: 本地 DNS 服务器直接构造响应（更干净，无需修改原响应包）

优势：
- 不依赖 WinDivert 内核驱动
- 本地 DNS 服务器可以缓存查询结果（性能提升）
- 规则匹配逻辑更简单（直接构造响应）

限制：
- 需要修改系统 DNS 配置或用 iptables/pf 重定向 53 端口
- 仅劫持 UDP DNS（TCP DNS 暂不处理，绝大多数客户端用 UDP）
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
    """Unix 平台 DNS 劫持后端（本地 DNS 服务器 + iptables/pf 重定向）。

    对外接口与 Windows 的 DnsHijacker 类保持一致：
    - start() / stop() / status()
    - set_rules() / get_rules()
    - running / last_error 属性
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
        """更新劫持规则（与 Windows 版本接口一致）。"""
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
                        logger.warning("dns_hijack", "规则通配符过多，已跳过编译",
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
        """匹配域名，返回劫持 IP 或 None（与 Windows 版本逻辑一致）。"""
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
        """启动 DNS 劫持。"""
        if not IS_UNIX:
            return False, "LocalDnsHijacker 仅支持 Linux/macOS"
        if self._running:
            return True, "已在运行"
        if not self._is_admin():
            msg = "DNS 劫持需要 root 权限（iptables/pf 需要 root）"
            self._last_error = msg
            logger.error("dns_hijack", "Unix DNS 劫持需要 root", "请用 sudo 启动 Telnix")
            return False, msg

        try:
            # 启动本地 DNS 服务器
            if not self._start_dns_server():
                return False, self._last_error

            # 添加 iptables/pf 重定向规则
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
            logger.info("dns_hijack", f"DNS 劫持已启动 ({backend})",
                        f"local_dns=127.0.0.1:{_LOCAL_DNS_PORT}")
            return True, f"DNS 劫持已启动（{backend}）"
        except Exception as e:  # noqa: BLE001
            self._last_error = str(e)
            logger.error("dns_hijack", "Unix DNS 劫持启动失败", str(e))
            self._cleanup_rules()
            self._stop_dns_server()
            return False, f"启动失败: {e}"

    def _start_dns_server(self) -> bool:
        """启动本地 DNS 服务器（监听 127.0.0.1:5354）。"""
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("127.0.0.1", _LOCAL_DNS_PORT))
            self._sock.settimeout(1.0)  # 1 秒超时，便于响应 stop
        except OSError as e:
            self._last_error = f"本地 DNS 服务器启动失败: {e}（端口 {_LOCAL_DNS_PORT} 可能被占用）"
            return False
        # 启动 DNS 服务器线程
        self._thread = threading.Thread(target=self._dns_loop, daemon=True, name="local-dns")
        self._thread.start()
        return True

    def _setup_iptables_redirect(self) -> bool:
        """Linux: 用 iptables NAT 把 UDP 53 重定向到本地 DNS 端口。"""
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
                self._last_error = f"iptables 添加规则失败: {result.stderr.decode('utf-8', errors='replace')}"
                self._cleanup_rules()
                return False
            self._iptables_rules.append(rule)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            self._last_error = f"iptables 不可用: {e}"
            return False
        return True

    def _setup_pf_redirect(self) -> bool:
        """macOS: 用 pf rdr 把 UDP 53 重定向到本地 DNS 端口。"""
        # 写入 anchor 规则文件
        pf_rules = (
            f"# Telnix DNS 劫持规则（自动生成，请勿手动编辑）\n"
            f"rdr pass on lo0 proto udp from any to any port 53 -> 127.0.0.1 port {_LOCAL_DNS_PORT}\n"
        )
        try:
            with open(_PF_DNS_ANCHOR_FILE, "w", encoding="utf-8") as f:
                f.write(pf_rules)
        except OSError as e:
            self._last_error = f"写入 pf anchor 文件失败: {e}"
            return False

        # 加载 anchor
        try:
            result = subprocess.run(
                ["pfctl", "-a", _PF_DNS_ANCHOR_NAME, "-f", _PF_DNS_ANCHOR_FILE],
                capture_output=True, timeout=5,
            )
            if result.returncode != 0:
                self._last_error = f"pfctl 加载 anchor 失败: {result.stderr.decode('utf-8', errors='replace')}"
                self._cleanup_rules()
                return False
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            self._last_error = f"pfctl 不可用: {e}"
            return False

        # 检查主配置是否引用 anchor
        try:
            result = subprocess.run(
                ["pfctl", "-s", "rules"],
                capture_output=True, timeout=5,
            )
            rules_output = result.stdout.decode("utf-8", errors="replace")
            if f'anchor "{_PF_DNS_ANCHOR_NAME}"' not in rules_output:
                self._last_error = (
                    f"pf 主配置未引用 anchor '{_PF_DNS_ANCHOR_NAME}'。"
                    f"请在 /etc/pf.conf 中添加：anchor \"{_PF_DNS_ANCHOR_NAME}\"，"
                    f"然后运行：sudo pfctl -f /etc/pf.conf"
                )
                self._cleanup_rules()
                return False
        except subprocess.TimeoutExpired:
            self._last_error = "pfctl 查询规则超时"
            self._cleanup_rules()
            return False

        self._pf_loaded = True
        return True

    def _dns_loop(self):
        """本地 DNS 服务器主循环。"""
        while self._running:
            try:
                try:
                    data, client_addr = self._sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError as e:
                    if self._running:
                        logger.error("dns_hijack", "本地 DNS 服务器 recv 失败", str(e))
                    break
                # 处理查询（在主循环中同步处理，避免线程开销）
                try:
                    self._handle_dns_query(data, client_addr)
                except Exception as e:  # noqa: BLE001
                    with _STATS_LOCK:
                        _STATS["errors"] += 1
                    if self._running:
                        logger.error("dns_hijack", "DNS 查询处理异常", str(e))
            except Exception as e:  # noqa: BLE001
                if self._running:
                    logger.error("dns_hijack", "DNS 循环异常", str(e))
                    time.sleep(0.1)

    def _handle_dns_query(self, data: bytes, client_addr: tuple):
        """处理单个 DNS 查询。"""
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
        """解析 DNS 查询包，提取 domain 和 qtype。"""
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
        """构造 A 记录响应包。"""
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
        """构造空响应（用于 AAAA 强制 IPv4 回退）。"""
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
        """把上游响应的 DNS transaction id 替换为当前查询的 id。

        缓存的响应包是首次查询时上游返回的，其 TXID 属于首次那台客户端。
        直接复用会导致后续客户端 TXID 不匹配、响应被解析器丢弃。这里只改写
        头部前 2 字节的 id，其余（含压缩指针）保持一致即可。
        """
        if len(query) < 2 or len(response) < 2:
            return response
        out = bytearray(response)
        out[0] = query[0]
        out[1] = query[1]
        return bytes(out)

    def _forward_to_upstream(self, query: bytes, client_addr: tuple, query_info: dict):
        """转发查询给上游 DNS 服务器并返回响应。"""
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
        """停止 DNS 劫持。返回 (success, msg) 与 Windows 版本接口一致。"""
        if not self._running:
            return True, "未在运行"
        self._running = False
        self._cleanup_rules()
        self._stop_dns_server()
        backend = "iptables+local_dns" if IS_LINUX else "pf+local_dns"
        logger.info("dns_hijack", f"DNS 劫持已停止 ({backend})")
        return True, "DNS 劫持已停止"

    def _stop_dns_server(self):
        """停止本地 DNS 服务器。"""
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
        """清理 iptables/pf 规则。"""
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
        """返回 DNS 劫持状态（与 Windows 版本字段保持一致）。"""
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
    """检查 Unix DNS 劫持后端是否可用。"""
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
    """返回 Unix DNS 劫持后端状态。"""
    try:
        is_admin = os.geteuid() == 0
    except AttributeError:
        is_admin = False
    return {
        "running": False,
        "supported": True,
        "backend": "iptables+local_dns" if IS_LINUX else ("pf+local_dns" if IS_MACOS else "none"),
        "is_admin": is_admin,
        "hint": ("就绪" if is_admin else "需要 root 权限") if IS_UNIX else "不支持的平台",
    }

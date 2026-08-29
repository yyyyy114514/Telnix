"""DNS hijack backend - cross-platform implementation.

Working modes:
- Windows: WinDivert intercepts DNS response packets on UDP port 53 at network layer, modifies A record IP
- Linux: iptables NAT redirects UDP 53 to local DNS server (127.0.0.1:5354)
- macOS: pf rdr redirects UDP 53 to local DNS server (127.0.0.1:5354)

Cross-platform notes:
- Windows: WinDivert intercept mode, modifies A record RDATA of original response packet
- Unix: Local DNS server directly constructs response (cleaner, no need to modify original response packet)

DoH/DoT support:
- detect_doh_request() checks flow for DNS-over-HTTPS indicators
- Common DoH providers: cloudflare-dns.com, dns.google, dns.quad9.net, one.one.one.one, dns.adguard.com
- DoT (DNS-over-TLS): port 853 direct connections to known DoT servers
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import struct
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

from .. import logger

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_UNIX = IS_LINUX or IS_MACOS

# ============ DoH/DoT 检测相关常量 ============

# 已知 DoH 服务器域名列表
DOH_SERVER_DOMAINS: frozenset[str] = frozenset({
    # Cloudflare
    "cloudflare-dns.com",
    "one.one.one.one",
    "cloudflare-dns.org",
    # Google
    "dns.google",
    "dns.google.com",
    # Quad9
    "dns.quad9.net",
    "dns9.quad9.net",
    # AdGuard
    "dns.adguard.com",
    "dns.adguard-dns.com",
    "family.adguard-dns.com",
    # NextDNS
    "dns.nextdns.io",
    "anycast.dns.nextdns.io",
    # OpenDNS
    "doh.opendns.com",
    "doh64.secure.internode.on.net",
    # CleanBrowsing
    "doh.cleanbrowsing.org",
    "security-filter-dns.cleanbrowsing.org",
    "family-filter-dns.cleanbrowsing.org",
    # Mullvad
    "dns.mullvad.net",
    # ControlD
    "dns.controld.com",
    "doh.privacy.xyz",
    # Quad101
    "dns.twnic.tw",
    # etc.
})

# 已知 DoT 服务器域名列表（用于检测 DoT 流量）
DOT_SERVER_DOMAINS: frozenset[str] = frozenset({
    "cloudflare-dns.com",
    "one.one.one.one",
    "dns.google",
    "dns.quad9.net",
    "dns.adguard.com",
    "dns.nextdns.io",
    "dns.mullvad.net",
    "dns.twnic.tw",
    "doh.cleanbrowsing.org",
})

# DoH 相关 HTTP 头（用于检测 DoH 请求）
DOH_HTTP_HEADERS: frozenset[str] = frozenset({
    "accept",
    "content-type",
    "host",
    "doh",
    "x-doh",
})

# DoH 内容类型
DOH_CONTENT_TYPES: frozenset[str] = frozenset({
    "application/dns-message",
    "application/dns-json",
    "application/manifest+json",
    "application/x-google-protobuf",
})

# 单条规则通配符数量上限（防止 ReDoS）
# 实测：20 个 *a* 交替的通配符 + 200 字符域名即可触发 >3s 卡顿；
# 8 个通配符在最坏输入下 <0.1ms，覆盖所有合理用例（典型 1-2 个）。
_MAX_WILDCARDS_PER_PATTERN = 8


# 命中日志环形缓冲（前端可拉取最近 N 条）
_HIJACK_LOG: deque = deque(maxlen=200)
_HIJACK_LOG_LOCK = threading.Lock()

# 统计计数器
_STATS = {
    "total_packets": 0,        # 总共截获的 DNS 响应包
    "hijacked_packets": 0,     # 实际修改的包
    "skipped_no_match": 0,     # 未匹配规则的包
    "errors": 0,               # 解析/修改失败次数
}
_STATS_LOCK = threading.Lock()


def _log_hijack(domain: str, original_ips: list, new_ip: str, src: str):
    """Record a hijack log entry."""
    entry = {
        "ts": datetime.now().isoformat(),
        "domain": domain,
        "original_ips": original_ips,
        "new_ip": new_ip,
        "dns_server": src,
    }
    with _HIJACK_LOG_LOCK:
        _HIJACK_LOG.append(entry)


def get_hijack_log() -> list:
    with _HIJACK_LOG_LOCK:
        return list(_HIJACK_LOG)


def get_stats() -> dict:
    with _STATS_LOCK:
        return dict(_STATS)


def _reset_stats():
    with _STATS_LOCK:
        for k in _STATS:
            _STATS[k] = 0


# ============ DoH/DoT 检测函数 ============

def detect_doh_request(flow: dict) -> dict:
    """检测流量是否为 DoH/DoT 请求。

    Args:
        flow: 流量字典，包含 host, url, headers, sni, method 等字段

    Returns:
        检测结果字典：
        {
            "is_doh": bool,           # 是否为 DoH 请求
            "is_dot": bool,           # 是否为 DoT 请求
            "provider": str | None,   # DoH 提供商名称
            "reason": str,            # 检测依据
            "confidence": str,        # confidence: "high", "medium", "low"
        }
    """
    result = {
        "is_doh": False,
        "is_dot": False,
        "provider": None,
        "reason": "",
        "confidence": "none",
    }

    # 提取关键信息
    host = (flow.get("host") or "").lower()
    url = (flow.get("url") or "").lower()
    method = (flow.get("method") or "").upper()
    sni = (flow.get("sni") or "").lower()
    headers = flow.get("headers") or {}
    if isinstance(headers, dict):
        headers = {k.lower(): v for k, v in headers.items()}

    # 检查是否为 HTTPS 请求（DoH 使用 HTTPS）
    is_https = flow.get("scheme") == "https" or flow.get("port") == 443
    port = flow.get("port")

    # ========== 优先检测 DoT (DNS over TLS) ==========
    # DoT 通常是直接连接到 853 端口的 TLS 连接，特征明确
    if port == 853:
        result["is_dot"] = True
        if sni in DOT_SERVER_DOMAINS:
            result["provider"] = sni
            result["reason"] = f"SNI matches DoT provider: {sni}"
            result["confidence"] = "high"
        else:
            result["reason"] = "Port 853 suggests DoT"
            result["confidence"] = "medium"
        return result

    # ========== 检测 DoH (DNS over HTTPS) ==========
    # 方法1: 检查 SNI 是否为已知 DoH 域名
    for doh_domain in DOH_SERVER_DOMAINS:
        if sni == doh_domain or (sni and sni.endswith(f".{doh_domain}")):
            result["is_doh"] = True
            result["provider"] = doh_domain
            result["reason"] = f"SNI matches DoH provider: {doh_domain}"
            result["confidence"] = "high"
            return result

    # 方法2: 检查 Host header 是否为已知 DoH 域名
    for doh_domain in DOH_SERVER_DOMAINS:
        if host == doh_domain or host.endswith(f".{doh_domain}"):
            # 进一步检查 HTTP headers 是否符合 DoH 特征
            content_type = headers.get("content-type", "")
            accept = headers.get("accept", "")

            if any(ct in content_type for ct in DOH_CONTENT_TYPES):
                result["is_doh"] = True
                result["provider"] = doh_domain
                result["reason"] = f"Host + Content-Type matches DoH: {doh_domain}"
                result["confidence"] = "high"
                return result

            if "application/dns" in content_type or "application/dns" in accept:
                result["is_doh"] = True
                result["provider"] = doh_domain
                result["reason"] = f"Host + DNS Content-Type matches DoH: {doh_domain}"
                result["confidence"] = "high"
                return result

            # DoH GET 请求特征（Google 格式）
            if "resolve" in url or "dns-query" in url:
                result["is_doh"] = True
                result["provider"] = doh_domain
                result["reason"] = f"Host + URL path matches DoH: {doh_domain}"
                result["confidence"] = "medium"
                return result

    # 方法3: 检查 URL 路径
    doh_path_patterns = [
        "/dns-query",
        "/dns-query/",
        "/resolve",
        "/api/dns",
        "/doh/query",
        "/doh/submit",
    ]
    for pattern in doh_path_patterns:
        if pattern in url and is_https:
            result["is_doh"] = True
            result["reason"] = f"URL path matches DoH pattern: {pattern}"
            result["confidence"] = "medium"
            return result

    # 方法4: 检查 HTTP headers 中的 DoH 特征
    # Chrome/Edge 使用 X-HTTP-Method-Override 或特殊的 Accept 头
    if "accept" in headers:
        accept_lower = headers["accept"].lower()
        if "application/dns" in accept_lower or "application/x-google-protobuf" in accept_lower:
            result["is_doh"] = True
            result["reason"] = "Accept header suggests DoH"
            result["confidence"] = "medium"
            return result

    # 方法5: 检查 Content-Type
    if "content-type" in headers:
        content_type_lower = headers["content-type"].lower()
        if any(ct in content_type_lower for ct in DOH_CONTENT_TYPES):
            result["is_doh"] = True
            result["reason"] = "Content-Type suggests DoH"
            result["confidence"] = "medium"
            return result

    # ========== 兜底检测 DoT (基于 SNI) ==========
    # DoT 可以不依赖端口（通过 SNI 识别）
    if sni in DOT_SERVER_DOMAINS:
        result["is_dot"] = True
        result["provider"] = sni
        result["reason"] = f"SNI matches DoT provider: {sni}"
        result["confidence"] = "medium"  # 降级为 medium 因为没有明确端口
        return result

    return result


def detect_doh_in_flows(flows: list[dict]) -> dict:
    """检测一批流量中的 DoH/DoT 请求。

    Args:
        flows: 流量字典列表

    Returns:
        {
            "doh_count": int,        # DoH 请求数量
            "dot_count": int,        # DoT 请求数量
            "doh_flows": list,      # DoH 流量详情
            "dot_flows": list,      # DoT 流量详情
            "doh_providers": dict,  # 各提供商数量统计
        }
    """
    result = {
        "doh_count": 0,
        "dot_count": 0,
        "doh_flows": [],
        "dot_flows": [],
        "doh_providers": {},
    }

    for flow in flows:
        detection = detect_doh_request(flow)

        if detection["is_doh"]:
            result["doh_count"] += 1
            provider = detection.get("provider") or "unknown"
            result["doh_providers"][provider] = result["doh_providers"].get(provider, 0) + 1
            result["doh_flows"].append({
                "id": flow.get("id"),
                "url": flow.get("url"),
                "method": flow.get("method"),
                "sni": flow.get("sni"),
                "host": flow.get("host"),
                **detection,
            })

        if detection["is_dot"]:
            result["dot_count"] += 1
            provider = detection.get("provider") or "unknown"
            result["dot_flows"].append({
                "id": flow.get("id"),
                "url": flow.get("url"),
                "method": flow.get("method"),
                "sni": flow.get("sni"),
                "port": flow.get("port"),
                **detection,
            })

    return result


def get_doh_status() -> dict:
    """获取 DoH 检测状态和统计信息。

    Returns:
        {
            "doh_enabled": bool,           # 检测到 DoH 流量
            "dot_enabled": bool,           # 检测到 DoT 流量
            "known_providers": list,       # 已知 DoH 提供商列表
            "recent_detection": dict,      # 最近一次检测结果（如果有）
        }
    """
    return {
        "doh_enabled": False,  # 将在流量检测时动态更新
        "dot_enabled": False,
        "known_providers": sorted(DOH_SERVER_DOMAINS),
        "known_dot_providers": sorted(DOT_SERVER_DOMAINS),
        "supported_doh_content_types": sorted(DOH_CONTENT_TYPES),
    }


class DnsHijacker:
    """DNS hijack backend."""

    def __init__(self):
        self._running = False
        self._thread: threading.Thread | None = None
        self._divert = None
        self._last_error: str = ""
        # 规则：domain → fake_ip（支持精确匹配和通配符 * ?）
        self._rules: dict[str, str] = {}
        # 预编译通配符正则：[(compiled_regex, ip), ...]（仅含含 * 或 ? 的规则）
        self._compiled_patterns: list[tuple[re.Pattern, str]] = []
        # 默认劫持 IP（所有未在 _rules 中但匹配的域名都返回此 IP）
        # 空字符串表示只劫持 _rules 中明确列出的域名
        self._default_ip: str = ""
        self._enabled: bool = True  # 总开关（启动时为 True，stop 时为 False）
        self._lock = threading.RLock()
        # 本机 IP 集合缓存（避免每次包都查询）
        self._local_ips: set[str] = set()
        self._local_ips_ts: float = 0.0
        # admin 检查结果缓存（降低 status API 频繁调用时的开销）
        self._admin_cache: bool | None = None
        self._admin_cache_ts: float = 0.0

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> str:
        return self._last_error

    def set_rules(self, rules: dict[str, str], default_ip: str = ""):
        """Update hijack rules.

        rules: {domain: fake_ip}, domain supports wildcards * and ?
        - Exact match: baidu.com
        - Suffix wildcard: *.baidu.com (matches mbd.baidu.com, www.baidu.com)
        - Any position wildcard: *baidu* (matches mbd.baidu.com, baidu.com, tieba.baidu.com)
        default_ip: default hijack IP (all A record queries not matching rules return this IP)
        """
        with self._lock:
            # 规范化规则：domain 转小写，去前后空白
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
                    # 通配符转正则：* → .*, ? → .，锚定首尾
                    regex_str = '^' + re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.') + '$'
                    try:
                        self._compiled_patterns.append((re.compile(regex_str, re.IGNORECASE), ip))
                    except re.error:
                        pass

    def get_rules(self) -> tuple[dict[str, str], str]:
        with self._lock:
            return dict(self._rules), self._default_ip

    def _match_rule(self, domain: str) -> str | None:
        """Match domain, return hijack IP or None."""
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

    def _refresh_local_ips(self):
        """Refresh local IP set (used to determine packet direction)."""
        now = time.time()
        if now - self._local_ips_ts < 30:
            return
        ips: set[str] = set()
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None):
                ip = info[4][0]
                if ":" not in ip:  # 仅 IPv4
                    ips.add(ip)
        except Exception:  # noqa: BLE001
            pass
        ips.add("127.0.0.1")
        self._local_ips = ips
        self._local_ips_ts = now

    def _is_local_ip(self, ip: str) -> bool:
        self._refresh_local_ips()
        return ip in self._local_ips

    def _is_admin(self) -> bool:
        now = time.time()
        if self._admin_cache is not None and now - self._admin_cache_ts < 30:
            return self._admin_cache
        result = False
        if not IS_WINDOWS:
            try:
                result = os.geteuid() == 0  # type: ignore[attr-defined]
            except AttributeError:
                result = False
        else:
            try:
                import ctypes
                result = bool(ctypes.windll.shell32.IsUserAnAdmin())
            except Exception:
                result = False
        self._admin_cache = result
        self._admin_cache_ts = now
        return result

    def start(self) -> tuple[bool, str]:
        """Start DNS hijack (Windows only). Returns (success, msg).

        Note: Non-Windows platforms should not call this method. start_hijack() will automatically route to
        LocalDnsHijacker (dns_hijack_local.py), no need for caller to care about platform.
        """
        if not IS_WINDOWS:
            # Defensive check: non-Windows platforms should not reach here (start_hijack routes to LocalDnsHijacker)
            logger.error("dns_hijack", "DnsHijacker.start() called on non-Windows platform",
                         "Should be routed to LocalDnsHijacker by start_hijack(), please check caller")
            return False, "Internal error: Windows backend called on non-Windows platform"
        try:
            import pydivert  # type: ignore  # noqa: F401
        except ImportError:
            msg = "pydivert not installed, please run: pip install pydivert"
            logger.error("dns_hijack", "pydivert not installed", msg)
            return False, msg
        if self._running:
            return True, "Already running"
        try:
            import pydivert  # type: ignore
            if not self._is_admin():
                msg = "DNS hijack requires administrator privileges"
                logger.error("dns_hijack", "Insufficient privileges", msg)
                return False, msg
            # WinDivert filter：拦截 UDP 53 和 TCP 53 端口的包
            # UDP 53：标准 DNS 查询/响应（系统 nslookup、curl 等）
            # TCP 53：DNS over TCP（大响应、区域传送）
            # 注意：DoH (DNS over HTTPS) 走 TCP 443，不在此拦截范围。
            #   浏览器默认启用 DoH 时会绕过 DNS 劫持。
            #   解决方案：在设置中关闭浏览器的"安全 DNS / Secure DNS"功能。
            # flags=0：拦截模式，recv 取出的包不再流转，必须 send 才会继续传递
            self._divert = pydivert.WinDivert(
                "(udp and (udp.SrcPort == 53 or udp.DstPort == 53)) "
                "or (tcp and (tcp.SrcPort == 53 or tcp.DstPort == 53))",
                flags=0,
            )
            self._divert.open()
            self._running = True
            self._enabled = True
            _reset_stats()
            self._thread = threading.Thread(target=self._capture_loop, daemon=True, name="dns-hijack")
            self._thread.start()
            rules, default_ip = self.get_rules()
            logger.info("dns_hijack", "DNS hijack started",
                        f"rules={len(rules)} rules, default_ip={default_ip or '(none)'}")
            return True, "DNS hijack started"
        except Exception as e:  # noqa: BLE001
            err_msg = str(e)
            self._last_error = err_msg
            logger.error("dns_hijack", "WinDivert start failed", err_msg)
            self._divert = None
            self._running = False
            low = err_msg.lower()
            if "找不到" in err_msg or "not found" in low:
                return False, "WinDivert driver file missing, please ensure pydivert is installed"
            if "access is denied" in low or "拒绝访问" in low:
                return False, "Insufficient privileges, please run Telnix as administrator"
            if "签名" in err_msg or "sign" in low:
                return False, "WinDivert driver loading blocked, possibly by antivirus, please add to whitelist"
            return False, f"Start failed: {err_msg}"

    def stop(self) -> tuple[bool, str]:
        """Stop DNS hijack.

        First _running=False, then use shutdown to unblock recv, then close.
        Avoids directly closing which would cause worker thread to permanently block on recv.
        Platform: Windows
        """
        if not self._running:
            return True, "Not running"
        self._running = False
        self._enabled = False
        if self._divert:
            # F7 修复：先 shutdown 让 recv() 立即返回 None 或抛异常。
            # 注意：pydivert Python 层未暴露 shutdown（DLL 层有 WinDivertShutdown），
            # 此调用会抛 AttributeError 被 try/except 吞掉（no-op，保留文档意图，
            # 未来 pydivert 若暴露则自动生效）。对齐 transparent_proxy.py 的模式。
            try:
                self._divert.shutdown()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            # 等待工作线程退出（recv 应已解除阻塞或在 close 后解除）
            if self._thread:
                self._thread.join(timeout=3)
                if self._thread.is_alive():
                    logger.warning("dns_hijack", "Worker thread still running on stop",
                                   "possible blocking recv")
                self._thread = None
            # 最后关闭句柄（join 之后，避免 close-during-blocking-recv 未定义行为）
            try:
                self._divert.close()
            except Exception:  # noqa: BLE001
                pass
            self._divert = None
        logger.info("dns_hijack", "DNS hijack stopped",
                    f"stats={get_stats()}")
        return True, "DNS hijack stopped"

    def _capture_loop(self):
        """Capture main loop: recv -> determine if hijack needed -> modify -> send.

        Supports both UDP 53 and TCP 53 DNS transports:
        - UDP 53: DNS payload is directly IP payload
        - TCP 53: DNS payload has 2-byte length prefix (RFC 1035 §4.2.2)
        """
        import pydivert  # type: ignore
        global _STATS
        while self._running:
            try:
                packet = self._divert.recv()
                if packet is None:
                    continue
                ip_hdr = packet.ipv4
                if ip_hdr is None:
                    self._divert.send(packet)
                    continue

                # 判断是 UDP 还是 TCP DNS
                is_tcp = packet.tcp is not None
                is_udp = packet.udp is not None
                if not is_tcp and not is_udp:
                    self._divert.send(packet)
                    continue

                if is_udp:
                    transport_hdr = packet.udp
                else:
                    transport_hdr = packet.tcp
                src_port = transport_hdr.src_port
                dst_port = transport_hdr.dst_port

                # 只处理 DNS 响应包（src_port=53，从远端 DNS 服务器到本机）
                if src_port != 53:
                    # DNS 查询包（dst_port=53）原样放行
                    self._divert.send(packet)
                    continue
                # 判断方向：响应包的目标是本机
                dst_ip = ip_hdr.dst_addr
                if not self._is_local_ip(dst_ip):
                    # 不是发往本机的包，原样放行
                    self._divert.send(packet)
                    continue
                with _STATS_LOCK:
                    _STATS["total_packets"] += 1

                payload = packet.payload or b""
                # TCP DNS: 前面有 2 字节长度前缀（RFC 1035 §4.2.2）
                tcp_dns_prefix = b""
                if is_tcp and len(payload) >= 2:
                    tcp_dns_prefix = payload[:2]
                    payload = payload[2:]
                if len(payload) < 12:
                    # 不完整的 DNS 包
                    self._divert.send(packet)
                    continue

                try:
                    new_payload, hijacked, domain, original_ips, new_ip = self._maybe_hijack(payload)
                    if hijacked and new_payload:
                        # 修改包 payload 并重算校验和
                        if is_tcp:
                            # TCP DNS: 重新计算长度前缀
                            new_len = len(new_payload)
                            packet.payload = struct.pack(">H", new_len) + new_payload
                        else:
                            packet.payload = new_payload
                        # F4 修复：pydivert 3.x 正确 API 是 Packet.recalculate_checksums()
                        # 原代码用 packet.recalc_checksums()（不存在）+ _manual_recalc（三层 fallback 全错），
                        # 仅因 WinDivert.send() 默认 recalculate_checksum=True 兜底才"能工作"。
                        # 即便此处失败，下方 self._divert.send(packet) 仍会兜底重算（幂等）。
                        # Platform: Windows
                        packet.recalculate_checksums()
                        with _STATS_LOCK:
                            _STATS["hijacked_packets"] += 1
                        _log_hijack(domain or "?", original_ips, new_ip, ip_hdr.src_addr)
                        logger.info("dns_hijack", "Hijacking DNS response",
                                    f"domain={domain} {original_ips} -> {new_ip}")
                    else:
                        with _STATS_LOCK:
                            _STATS["skipped_no_match"] += 1
                except Exception as e:  # noqa: BLE001
                    with _STATS_LOCK:
                        _STATS["errors"] += 1
                    logger.error("dns_hijack", "Hijack processing failed", str(e))
                # 不论是否修改，都要 send（否则会断网）
                self._divert.send(packet)
            except Exception as e:  # noqa: BLE001
                if self._running:
                    logger.error("dns_hijack", "Hijack loop exception", str(e))
                    time.sleep(0.05)

    def _maybe_hijack(self, data: bytes) -> tuple:
        """Parse DNS response, modify A record according to rules.

        Returns (new_payload, hijacked, domain, original_ips, new_ip)
        """
        if len(data) < 12:
            return data, False, "", [], ""
        # DNS header：12 字节
        # ID(2) FLAGS(2) QDCOUNT(2) ANCOUNT(2) NSCOUNT(2) ARCOUNT(2)
        flags = struct.unpack(">H", data[2:4])[0]
        qdcount = struct.unpack(">H", data[4:6])[0]
        ancount = struct.unpack(">H", data[6:8])[0]
        # QR=1 表示响应
        is_response = (flags & 0x8000) != 0
        if not is_response or qdcount == 0:
            return data, False, "", [], ""

        offset = 12
        # 解析 Question 段（取第一个查询的域名）
        domain, offset = self._parse_name(data, offset)
        if not domain:
            return data, False, "", [], ""
        # 读取 QTYPE(2) + QCLASS(2)
        if offset + 4 > len(data):
            return data, False, domain, [], ""
        qtype = struct.unpack(">H", data[offset:offset+2])[0]
        offset += 4
        # 跳过剩余的 Question（如果有）
        for _ in range(qdcount - 1):
            _, offset = self._parse_name(data, offset)
            offset += 4

        # 匹配规则
        new_ip = self._match_rule(domain)
        if not new_ip:
            return data, False, domain, [], ""

        # 校验 IP 合法性
        try:
            ip_bytes = socket.inet_aton(new_ip)
        except OSError:
            logger.warning("dns_hijack", "Invalid rule IP", f"domain={domain} ip={new_ip}")
            return data, False, domain, [], ""

        # AAAA 查询（IPv6）：清空 Answer 段，强制浏览器回退到 IPv4 A 记录查询
        # 浏览器优先用 IPv6，若不处理 AAAA 则劫持无效（会直接用真实 IPv6 地址访问）
        if qtype == 28:
            new_data = bytearray(data[:offset])
            struct.pack_into(">H", new_data, 6, 0)   # ancount = 0
            struct.pack_into(">H", new_data, 8, 0)   # nscount = 0
            struct.pack_into(">H", new_data, 10, 0) # arcount = 0
            return bytes(new_data), True, domain, ["AAAA"], new_ip

        # 遍历 Answer 段，找到所有 A 记录（TYPE=1）并修改 RDATA
        new_data = bytearray(data)
        original_ips: list[str] = []
        modified = False
        for i in range(ancount):
            if offset >= len(new_data):
                break
            # Answer 中的 NAME 可能是指针（压缩），先尝试解析
            name_start = offset
            ans_name, offset = self._parse_name(bytes(new_data), offset)
            if offset + 10 > len(new_data):
                break
            rtype = struct.unpack(">H", new_data[offset:offset+2])[0]
            rclass = struct.unpack(">H", new_data[offset+2:offset+4])[0]
            ttl = struct.unpack(">I", new_data[offset+4:offset+8])[0]
            rdlength = struct.unpack(">H", new_data[offset+8:offset+10])[0]
            rdata_offset = offset + 10
            if rdata_offset + rdlength > len(new_data):
                break
            # TYPE=1 是 A 记录，CLASS=1 是 IN
            if rtype == 1 and rclass == 1 and rdlength == 4:
                # 修改 A 记录的 IP
                old_ip_bytes = bytes(new_data[rdata_offset:rdata_offset+4])
                old_ip = socket.inet_ntoa(old_ip_bytes)
                original_ips.append(old_ip)
                # 覆盖 IP 地址
                new_data[rdata_offset:rdata_offset+4] = ip_bytes
                modified = True
            offset = rdata_offset + rdlength

        if not modified:
            # Answer 段中没有 A 记录（可能是 CNAME 等）
            # 此时构造一个新的 A 记录应答太复杂，简单跳过
            return data, False, domain, [], ""

        return bytes(new_data), True, domain, original_ips, new_ip

    def _parse_name(self, data: bytes, offset: int) -> tuple[str, int]:
        """Parse DNS domain name (supports compression pointers).

        Returns (domain, new_offset): new_offset points to position after NAME
        """
        labels: list[str] = []
        pos = offset
        jumped = False
        jump_pos = 0
        iterations = 0  # 防止死循环
        while pos < len(data) and iterations < 100:
            iterations += 1
            length = data[pos]
            if length == 0:
                pos += 1
                if not jumped:
                    jump_pos = pos
                break
            if (length & 0xC0) == 0xC0:
                # 指针
                if pos + 1 >= len(data):
                    break
                if not jumped:
                    jump_pos = pos + 2
                ptr = ((length & 0x3F) << 8) | data[pos + 1]
                pos = ptr
                jumped = True
                continue
            else:
                # 标签
                pos += 1
                if pos + length > len(data):
                    break
                labels.append(data[pos:pos+length].decode("ascii", errors="replace"))
                pos += length
        domain = ".".join(labels)
        final_offset = jump_pos if jumped and jump_pos > 0 else pos
        return domain.lower(), final_offset


# 单例
# 类型注解：Unix 平台上 _dns_hijacker 可能是 LocalDnsHijacker 实例（duck typing）
# 用 Any 避免循环依赖，运行时通过 duck typing 调用 start/stop/running/last_error
_dns_hijacker: "DnsHijacker | Any | None" = None
_singleton_lock = threading.Lock()


def get_hijacker() -> "DnsHijacker | Any":
    """Get DNS hijack singleton.

    Returns DnsHijacker instance on Windows, LocalDnsHijacker instance on Unix.
    """
    global _dns_hijacker
    with _singleton_lock:
        if _dns_hijacker is None:
            if IS_WINDOWS:
                _dns_hijacker = DnsHijacker()
            else:
                # Unix 平台：延迟导入 LocalDnsHijacker
                from .dns_hijack_local import LocalDnsHijacker
                _dns_hijacker = LocalDnsHijacker()
        return _dns_hijacker


def start_hijack(rules: dict[str, str], default_ip: str = "") -> tuple[bool, str]:
    """Start DNS hijack.

    Platform support:
    - Windows: WinDivert intercept mode (requires pydivert + administrator privileges)
    - Linux: iptables NAT + local DNS server (requires root)
    - macOS: pf rdr + local DNS server (requires root)

    rules: {domain: fake_ip} dict
    default_ip: default hijack IP (all A record queries not matching rules return this IP)
    """
    h = get_hijacker()
    h.set_rules(rules, default_ip)
    return h.start()


def stop_hijack() -> tuple[bool, str]:
    """Stop DNS hijack."""
    h = get_hijacker()
    return h.stop()


def update_rules(rules: dict[str, str], default_ip: str = "") -> tuple[bool, str]:
    """Update rules (runtime hot update)."""
    h = get_hijacker()
    h.set_rules(rules, default_ip)
    return True, f"Updated {len(rules)} rules"


def hijack_status() -> dict:
    """Get current status.

    Platform support:
    - Windows: WinDivert backend status
    - Linux: iptables + local DNS server status
    - macOS: pf + local DNS server status
    """
    h = get_hijacker()
    rules, default_ip = h.get_rules()
    # Unix 平台：h.status() 已包含 backend 字段
    if not IS_WINDOWS:
        status = h.status() if hasattr(h, "status") else {}
        return {
            "running": h.running,
            "last_error": h.last_error,
            "rules": rules,
            "default_ip": default_ip,
            "stats": get_stats(),
            "log": get_hijack_log(),
            "is_admin": h._is_admin() if hasattr(h, "_is_admin") else False,
            "is_windows": False,
            **status,
        }
    # Windows 平台：补全 backend 字段，与 Unix 分支对齐（前端可统一读取 backend 判断后端类型）
    return {
        "running": h.running,
        "last_error": h.last_error,
        "rules": rules,
        "default_ip": default_ip,
        "stats": get_stats(),
        "log": get_hijack_log(),
        "is_admin": h._is_admin(),
        "is_windows": True,
        "backend": "windivert",
        "supported": True,
    }


def clear_log():
    """Clear hijack log."""
    with _HIJACK_LOG_LOCK:
        _HIJACK_LOG.clear()
    with _STATS_LOCK:
        for k in _STATS:
            _STATS[k] = 0
    return True

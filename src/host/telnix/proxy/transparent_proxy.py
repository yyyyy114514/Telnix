"""Transparent proxy mode (simplified) - cross-platform NETWORK layer redirection.

Working principle:
- Windows: WinDivert intercepts outbound TCP packets with dstPort=80/443, rewrites addresses to local proxy
- Linux: iptables NAT REDIRECT redirects outbound TCP 80/443 to local proxy port
- macOS: pf rdr redirects outbound TCP 80/443 to local proxy port

Simplification scope:
- HTTP(80) goes through proxy for normal parsing; HTTPS(443) uses raw TCP tunnel (no decryption, end-to-end TLS)
- Outbound connections only, no inbound handling
- Only works when explicitly enabled by user (setting transparent_proxy=True)
- Coexists with system proxy: system proxy handles configured clients, transparent proxy handles proxy-unaware clients

Stealth advantages:
- Applications need no proxy configuration, fully transparent to HTTP traffic
- Does not modify system registry (does not write ProxyServer/ProxyEnable)
- Browser has no proxy awareness, hard to detect via conventional means

Cross-platform notes:
- Windows: WinDivert NETWORK layer interception + userspace NAT table
- Unix: iptables/pf kernel-space NAT, reverse traffic handled automatically (no NAT table needed)
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time
from typing import Any, Optional

from .. import logger

# 平台判断：WinDivert/pydivert 仅 Windows 可用
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_UNIX = IS_LINUX or IS_MACOS
if IS_WINDOWS:
    import ctypes
    try:
        import pydivert  # type: ignore
    except ImportError:
        pydivert = None  # type: ignore
else:
    ctypes = None  # type: ignore[assignment]
    pydivert = None  # type: ignore[assignment]


# 透明代理本地监听端口（与 HTTP 代理共用 8888）
_LOCAL_HOST = "127.0.0.1"
_LOCAL_PORT = 8888

# 重定向使用的 loopback 地址（127.0.0.0/8 范围内，非 127.0.0.1）。
# F34 修复：原方案把出站包 dst 改写为本机真实 IP（如 192.168.6.117），但 Windows
# TCP 栈对 src_ip == dst_ip == 本机真实 IP 的包走 "TCP loopback fast path"，绕过
# NDIS 层（WinDivert 钩子位置），导致代理回包（SYN-ACK 等）永不被 WinDivert 拦截，
# 反向 NAT 无法执行 → TCP 握手无法完成 → 全超时。
# 改用 127.0.0.2 作为双向重定向目标（src 和 dst 都改写为 127.0.0.2），使包走
# 127.0.0.0/8 loopback adapter（WinDivert 可拦截），同时不与系统代理（127.0.0.1）
# 冲突。filter 中已有的 ip.DstAddr != 127.0.0.1 排除系统代理流量，不影响 127.0.0.2。
_REDIRECT_LOOPBACK_ADDR = "127.0.0.2"

# 重定向目标端口：HTTP(80) + HTTPS(443)
# HTTP 走代理正常解析；HTTPS 仅做 raw TCP 隧道转发（不解密），由 server.py 在
# _handle_client 入口判断首字节非 HTTP method 时调用 lookup_reverse 反查原目标。
_REDIRECT_DST_PORTS = {80, 443}

# NAT 表条目 TTL（秒）：超过此时间未活动的条目被清理
# 300s 覆盖常见 keep-alive 间隔（HTTP/2 默认 15s，HTTP/1.1 默认 60s，部分长连接 120s）
# 原 60s 在长连接空闲超 60s 后反向回包未改写 → 客户端 RST
_NAT_TTL = 300.0


# 代理自身出站连接的本地端口集合（WinDivert 应跳过这些端口，避免无限重定向循环）
# ProxyServer._connect_target 创建 socket 后注册端口，连接关闭后注销
_proxy_outbound_ports: set[int] = set()
_proxy_outbound_lock = threading.Lock()

# 代理自身出站连接的 (src_ip, src_port) 二元组集合（F11 修复）：
# 用于精确匹配代理自身出站流量，避免纯端口匹配在端口复用时错误排除正常客户端流量。
_proxy_outbound_addrs: set[tuple[str, int]] = set()


def _detect_local_ip() -> str:
    """Detect the local non-loopback IPv4 address (used as transparent proxy redirect target).

    The transparent proxy must rewrite the destination address of outbound packets to
    "the machine's real IP" rather than 127.0.0.1: WinDivert intercepts outbound packets
    whose src is the client's real IP (e.g. 192.168.x.x). If dst is rewritten to 127.0.0.1,
    Windows will drop the packet due to loopback anti-spoofing (dst=127.0.0.1 but src is not
    127.0.0.1), and the proxy will never receive the connection → full timeout, no logs.
    After redirecting to the machine's real IP, the packet becomes "sent from local to local"
    (src/dst are both the local IP), and Windows delivers it normally to the proxy listening on 0.0.0.0.

    Detection strategy (multiple fallbacks to avoid silent fallback to 127.0.0.1 due to single-point failure):
    1. UDP connect to public IP (8.8.8.8 / 114.114.114.114) lets the OS choose the outbound NIC
    2. socket.getaddrinfo(gethostname()) enumerates local IPs resolved from the hostname
    3. Returns empty string when no non-loopback IP is found, letting the caller decide it's
       unsupported (no longer silently falls back to 127.0.0.1)
    """
    import socket
    # 策略 1：UDP connect 公网 IP（不会真正发包）
    for probe_dst in ("8.8.8.8", "114.114.114.114", "223.5.5.5"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe_dst, 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            s.close()
    # 策略 2：getaddrinfo(gethostname()) 枚举本机 IP
    try:
        hostname = socket.gethostname()
        for fam, _, _, _, sockaddr in socket.getaddrinfo(hostname, None):
            if fam != socket.AF_INET:
                continue
            ip = sockaddr[0]
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except OSError:
        pass
    # 策略 3：找不到非回环 IP，返回空串让调用方判定为不支持
    # （不再静默回退 127.0.0.1，否则会导致"全超时无日志"的隐蔽失败）
    return ""


def register_proxy_port(port: int):
    """Register the proxy's own outbound connection local port (legacy interface, called by server.py)."""
    with _proxy_outbound_lock:
        _proxy_outbound_ports.add(port)


def unregister_proxy_port(port: int):
    """Unregister the proxy's own outbound connection local port."""
    with _proxy_outbound_lock:
        _proxy_outbound_ports.discard(port)
        # 同步清理可能存在的 (any_ip, port) 条目（best-effort，无法精确匹配 ip）
        for ip_port in list(_proxy_outbound_addrs):
            if ip_port[1] == port:
                _proxy_outbound_addrs.discard(ip_port)


def register_proxy_addr(src_ip: str, src_port: int):
    """Register the (src_ip, src_port) tuple of the proxy's own outbound connection (exact match, F11 fix)."""
    with _proxy_outbound_lock:
        _proxy_outbound_ports.add(src_port)
        _proxy_outbound_addrs.add((src_ip, src_port))


def unregister_proxy_addr(src_ip: str, src_port: int):
    """Unregister the (src_ip, src_port) tuple of the proxy's own outbound connection."""
    with _proxy_outbound_lock:
        _proxy_outbound_ports.discard(src_port)
        _proxy_outbound_addrs.discard((src_ip, src_port))


def _is_proxy_outbound_port(port: int) -> bool:
    """Check whether the port belongs to the proxy's own outbound connection (legacy interface)."""
    with _proxy_outbound_lock:
        return port in _proxy_outbound_ports


def _is_proxy_outbound_addr(src_ip: str, src_port: int) -> bool:
    """Check whether (src_ip, src_port) belongs to the proxy's own outbound connection.

    Matching strategy (two-phase registration):
    1. Prefer (ip, port) exact match — registered by register_proxy_addr after connect() completes
    2. Fall back to pure port match — registered by register_proxy_port before connect() (TOCTOU window)

    Port fallback is always enabled (no longer only when _proxy_outbound_addrs is empty):
    The proxy's outbound socket only registers port before connect() (getsockname() returns 0.0.0.0
    at this point, real source IP unavailable), and upgrades to (ip, port) after connect(). SYN
    packets sent during the connect window must be excluded via port matching, otherwise WinDivert
    intercepts the proxy's own SYN, forming an infinite redirect loop (root cause of full timeout with no logs).

    Original implementation flaw: when _proxy_outbound_addrs was non-empty, port fallback was
    completely disabled, but _register_proxy_socket got 0.0.0.0 from getsockname before connect,
    registering dead data ('0.0.0.0', port) that real outbound packets' src_ip could never match,
    while port fallback was disabled → proxy's own traffic was not excluded at all → infinite loop.
    """
    with _proxy_outbound_lock:
        if (src_ip, src_port) in _proxy_outbound_addrs:
            return True
        return src_port in _proxy_outbound_ports


class TransparentProxy:
    """Transparent proxy: uses WinDivert NETWORK layer to redirect HTTP(80) traffic to local proxy.

    Implementation notes:
    - NETWORK layer capture: full IP/TCP headers available
    - Modify destination IP/Port to local proxy, recompute checksums and send
    - Maintain NAT table: original (src_ip:port, dst_ip:80) <-> (127.0.0.1:port, 127.0.0.1:8888)
    - Reverse traffic (proxy response): look up NAT table, rewrite src from local back to original server IP:80
    - Traffic not on the proxy's own port is passed through directly (does not affect other network activity)

    Key anti-loop: when the proxy connects to the target server itself, its outbound packets also
    match the WinDivert filter (dst port 80/443, non-loopback). Excluded via the _proxy_outbound_ports
    set to avoid infinite redirection causing full timeout.

    NAT table design:
    - key = (src_ip, src_port, dst_ip, dst_port) 4-tuple (recorded on outbound)
    - Reverse lookup key = (orig dst_ip, 80, orig src_ip, orig src_port) (reverse 4-tuple)
    - Each entry has a last_seen timestamp, TTL 300s auto-cleanup
    - TCP FIN/RST immediately cleans up the corresponding entry
    """

    def __init__(self, local_port: int = _LOCAL_PORT):
        self.local_port = local_port
        # 重定向目标地址（本机真实 IP）。运行时由 start() 通过 _detect_local_ip() 覆盖，
        # 避免硬编码 127.0.0.1 触发 Windows loopback 反欺骗丢包。
        self._local_host = _LOCAL_HOST
        self._divert: Optional["pydivert.WinDivert"] = None  # type: ignore
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_error: str = ""
        # NAT 表：{forward_key: (orig_src_ip, orig_src_port, orig_dst_ip, orig_dst_port, last_seen, reverse_key)}
        # forward_key = (src_ip, src_port, dst_ip, dst_port)
        # 第 6 个字段 reverse_key 用于 O(1) 反向索引清理（替代原 O(n) 扫描 _nat_reverse）
        self._nat_table: dict = {}
        # 反向索引：{reverse_key: forward_key}
        # reverse_key = (orig_dst_ip, orig_dst_port, orig_src_ip, orig_src_port)
        self._nat_reverse: dict = {}
        # 性能优化：(client_ip, client_src_port) → forward_key 索引，使 lookup_reverse O(1)。
        # 必须用 (ip, port) 二元组键而非纯 port（F2/F11 修复）：
        # 反向过滤器 tcp.SrcPort == local_port 会匹配所有代理→客户端的回包（含普通系统代理客户端），
        # 仅按 port 索引会在端口复用时把普通客户端的响应错误改写成外部服务器 IP:port，
        # 导致普通系统代理流量被间歇性污染（典型 flaky 现象）。
        self._client_port_index: dict[tuple[str, int], tuple] = {}
        # 代理自身出站 socket 的 (src_ip, src_port) 集合（F11 修复）：
        # 仅按 port 排除代理自身流量会在客户端临时端口与代理端口碰撞时错误排除正常客户端流量。
        # 用 (ip, port) 二元组精确匹配：代理自身出站的 src_ip 永远是 local_host，
        # 客户端的 src_ip 是其本机 IP（多数情况下与 local_host 相同，但 LAN 客户端不同）。
        # _proxy_outbound_ports 仍保留（server.py 已使用），但本类内部判断改用 _proxy_outbound_addrs。
        self._nat_lock = threading.Lock()
        # 统计
        self._redirected_count = 0
        self._passed_count = 0
        # Platform: Windows; send 失败计数（F2 诊断埋点：暴露被静默吞的 PermissionError(13,'段已解除锁定',None,158)）
        self._send_fail_count = 0
        # 反向 NAT 未命中计数（F25 诊断埋点）
        self._nat_miss_count = 0
        # 出站重定向注册计数（F38 诊断埋点，低速率日志用）
        self._diag_out_count = 0
        # 上次清理时间
        self._last_cleanup = time.monotonic()
        # QUIC(UDP/443) 拦截句柄（F39）：丢弃出站 UDP/443（及 80）包，强制浏览器
        # 从 HTTP/3(QUIC) 回退到 TCP/HTTPS。否则现代浏览器默认走 QUIC(UDP)，
        # 而本代理仅拦截 TCP(dst 80/443)，UDP 流量完全绕过 → 浏览器流量抓不到
        # （系统软件多用 TCP/TLS 故仍可被捕获，形成"系统软件能抓、浏览器抓不到"的 asymmetry）。
        self._quic_divert: Optional["pydivert.WinDivert"] = None  # type: ignore
        self._quic_thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> str:
        return self._last_error

    def status(self) -> dict:
        """Return transparent proxy status."""
        with self._nat_lock:
            nat_size = len(self._nat_table)
        return {
            "running": self._running,
            "redirected_count": self._redirected_count,
            "passed_count": self._passed_count,
            "send_fail_count": self._send_fail_count,  # F2 诊断埋点
            "nat_table_size": nat_size,
            "last_error": self._last_error,
            "local_port": self.local_port,
            "redirect_ports": list(_REDIRECT_DST_PORTS),
        }

    def lookup_reverse(self, client_src_port: int, client_src_ip: Optional[str] = None) -> Optional[tuple]:
        """Reverse-lookup the original target for the proxy server in raw tunnel mode.

        Args:
            client_src_port - the source port when the client connects to the local proxy
                (equivalent to the src_port of the original outbound packet, uniquely identifies a NAT entry)
            client_src_ip - the source IP when the client connects to the local proxy (optional, F2 fix).
                If provided, uses the (client_src_ip, client_src_port) tuple for exact reverse lookup,
                avoiding mismatches when different clients reuse ports; if None, falls back to pure port match
                (compatible with old behavior, but may mismatch when ports are reused).

        Returns: (orig_dst_ip, orig_dst_port) or None (no match)

        Performance optimization: O(1) lookup via the _client_port_index dict,
        replacing the original O(n) traversal of _nat_table (significant performance gap when NAT table is large).
        """
        with self._nat_lock:
            # 优先用 (ip, port) 二元组精确匹配
            if client_src_ip is not None:
                forward_key = self._client_port_index.get((client_src_ip, client_src_port))
                if forward_key is not None:
                    entry = self._nat_table.get(forward_key)
                    if entry is not None:
                        return entry[2], entry[3]
                # 二元组未命中：可能是旧版客户端未传 ip，或端口复用，回退到纯 port 匹配
                # （扫描 _client_port_index 中所有以该 port 结尾的 key）
                # 仅当确实存在以该 port 结尾但 ip 不同的条目时才回退，
                # 避免对正常未匹配的查询做无谓扫描
                for (ip, port), fk in self._client_port_index.items():
                    if port == client_src_port:
                        entry = self._nat_table.get(fk)
                        if entry is not None:
                            return entry[2], entry[3]
                return None
            # 旧路径：纯 port 匹配（仅当调用方未传 ip）
            for (ip, port), fk in self._client_port_index.items():
                if port == client_src_port:
                    entry = self._nat_table.get(fk)
                    if entry is not None:
                        return entry[2], entry[3]
            return None

    def register_nat_entry(self, client_ip: str, client_port: int,
                            orig_dst_ip: str, orig_dst_port: int) -> None:
        """F26: Actively register a NAT entry for SNI fallback.

        When lookup_reverse fails but SNI fallback successfully connects to the target, call this method
        to register a NAT entry, ensuring the reverse response can be correctly rewritten back to the original server IP:port.
        Otherwise the client receives a packet with src=local_ip:8888 → RST.
        """
        forward_key = (client_ip, client_port, orig_dst_ip, orig_dst_port)
        reverse_key = (orig_dst_ip, orig_dst_port, client_ip, client_port)
        now = time.monotonic()
        with self._nat_lock:
            self._nat_table[forward_key] = (
                client_ip, client_port, orig_dst_ip, orig_dst_port, now, reverse_key
            )
            self._nat_reverse[reverse_key] = forward_key
            # F35: 索引 key 必须用改写后的 loopback 地址（127.0.0.2），与出站分支
            # （transparent_proxy.py:559）及反向查找（:577，dst_ip=127.0.0.2）保持一致。
            # 原实现用真实 client_ip 作 key，导致 SNI fallback 注册的 NAT 条目在反向
            # 改写时永远命不中（key 是 (real_ip,port) 而非 (127.0.0.2,port)）→ 回包被丢弃。
            # nat_table / forward_key 内部仍保留真实 client_ip，供改写响应 dst 使用。
            # F38: 双键注册（与出站分支一致），覆盖 F34 src 改写是否生效两种情况
            self._client_port_index[(_REDIRECT_LOOPBACK_ADDR, client_port)] = forward_key
            self._client_port_index[(client_ip, client_port)] = forward_key

    def _is_admin(self) -> bool:
        """Check administrator privileges.

        Windows: ctypes.windll.shell32.IsUserAnAdmin()
        Unix: os.geteuid() == 0 (root)
        """
        if not IS_WINDOWS:
            try:
                return os.geteuid() == 0
            except AttributeError:
                return False
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            return False

    def start(self) -> bool:
        """Start the transparent proxy. Returns True on success, False on failure.

        Platform support:
        - Windows: WinDivert NETWORK layer interception (requires pydivert + administrator privileges)
        - Linux: iptables NAT REDIRECT (requires root + iptables)
        - macOS: pf rdr (requires root + pfctl)
        """
        if not IS_WINDOWS:
            self._last_error = "TransparentProxy only supports Windows; use UnixTransparentProxy on Unix"
            return False
        if self._running:
            return True
        if not self._is_admin():
            self._last_error = "Administrator privileges required (WinDivert requirement)"
            return False
        if pydivert is None:
            self._last_error = "pydivert not installed, please run: pip install pydivert"
            return False
        try:
            # filter：出站 TCP dst port in {80,443}，或反向 src port = local_port
            # 排除 loopback 避免回环噪音
            # NETWORK 层（默认）：能拿到 IP+TCP 头
            ports_str = " or ".join(
                f"tcp.DstPort == {p}" for p in _REDIRECT_DST_PORTS
            )
            # 重定向目标：必须用本机真实 IP（非 127.0.0.1）。
            # 原因：被 WinDivert 拦截的出站包 src 是客户端真实 IP（如 192.168.x.x），
            # 若把 dst 改写成 127.0.0.1，Windows 会因 loopback 反欺骗
            # （dst=127.0.0.1 但 src 非 127.0.0.1）直接丢弃该包，代理永远收不到连接，
            # 表现为「全超时、无日志」。改成重定向到本机真实 IP 后，包变成
            # 「本机发往本机」（src/dst 同为本机 IP），Windows 正常投递给监听 0.0.0.0 的代理。
            self._local_host = _detect_local_ip()
            # F1 修复：探测失败（无默认路由/隔离网络/防火墙挡 8.8.8.8）时不再静默回退 127.0.0.1，
            # 否则会导致"全超时无日志"的隐蔽失败。明确拒绝启动并报告清晰错误。
            if not self._local_host or self._local_host == "127.0.0.1":
                self._last_error = (
                    "Cannot detect a non-loopback IPv4 address on this machine (UDP connect to "
                    "8.8.8.8/114.114.114.114/223.5.5.5 all failed, and getaddrinfo(hostname) returned no non-loopback IP). "
                    "Transparent proxy cannot work: if the redirect target is 127.0.0.1, it will be dropped by "
                    "Windows loopback anti-spoofing, causing a full timeout with no logs. Please check the default route/NIC/firewall and retry."
                )
                logger.error("transparent", "Transparent proxy start failed: cannot detect non-loopback local IP", self._last_error)
                return False
            # 反向 filter：源端口 == local_port 且目的非 loopback。
            # 代理→客户端的回包专用此端口，唯一标识；而出站分支只匹配 dst port 80/443，
            # 不会误匹配。不再限定 src ip 为 127.0.0.1（监听 0.0.0.0 时回包 src 是本机真实 IP）。
            # F3b 修复（P0-002 根因 C）：reverse_str 加 `and ip.DstAddr != 127.0.0.1`。
            # 原因：系统代理（非透明代理）客户端连 127.0.0.1:8888 时，代理回包 src=8888→dst=127.0.0.1。
            # 该 loopback 包被 `tcp.SrcPort == 8888` 匹配后无谓拦截进 _loop，高流量下
            # WinDivert 驱动段锁定级联失败，send 持续抛 PermissionError 被静默吞（根因 B），
            # 改写包未注入网络 → 客户端全 RESET。
            # 调研证实：WinDivert NETWORK 层默认拦截 loopback，`outbound` 关键字不能排除 loopback
            # （loopback 包同时 is_outbound，见 pydivert tests/test_windivert.py:110-113 test_echo）。
            # 加 `ip.DstAddr != 127.0.0.1` 后：系统代理回包(dst=127.0.0.1)被排除；
            # 透明代理回包(dst=本机真实 IP)不受影响。
            # Platform: Windows
            # F10 修复（回归修正）：原用 `not loopback` 关键字，但 WinDivert 1.3.0 驱动不支持
            # `loopback` 关键字（check_filter 通过但 WinDivertOpen 抛 WinError 87 参数错误）。
            # 改用 `ip.DstAddr != ::1` 排除 IPv6 ::1 loopback，与 F3b 的 `ip.DstAddr != 127.0.0.1` 并列。
            reverse_str = f"tcp.SrcPort == {self.local_port} and ip.DstAddr != 127.0.0.1 and ip.DstAddr != ::1"
            outbound_str = (
                f"({ports_str}) and ip.DstAddr != 127.0.0.1 and ip.DstAddr != ::1"
            )
            # F3a 修复：加 outbound 限定，避免入站到本机 80/443 的流量被误劫持。
            # 原过滤器无方向限定，外部客户端访问本机 IIS/Apache/dev-server 的 SYN
            # 也会匹配 outbound_str（dst port 80/443, dst_ip 非 127.0.0.1）并被重定向，
            # 导致本机 80/443 服务完全不可用且产生混乱的 NAT 条目。
            # outbound 关键字限定仅匹配本机发出的包，与透明代理"截获出站流量"的语义一致。
            # 反向分支也加 outbound：代理→客户端的回包对内核而言是 outbound（本机发出）。
            # 注意：outbound 不能排除 loopback（见 F3b 注释），故 reverse_str 额外加 dst 限定。
            # F3b 排除 IPv4 127.0.0.1，F10 排除 IPv6 ::1，两者并列覆盖所有 loopback 地址。
            # Platform: Windows
            filter_str = f"outbound and (({outbound_str}) or ({reverse_str}))"
            self._divert = pydivert.WinDivert(filter_str)
            self._divert.open()
            self._running = True
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="transparent-proxy"
            )
            self._thread.start()
            # F39: 启用 QUIC(UDP/443) 拦截，强制浏览器回退 TCP/HTTPS 以便透明捕获。
            # 另开一个 WinDivert 句柄，filter 命中出站 UDP/443(及80) 包后直接丢弃（不重注入），
            # 浏览器 QUIC 握手失败即回退到 TCP 443，被主句柄拦截重定向到代理。
            try:
                quic_filter = "outbound and (udp.DstPort == 443 or udp.DstPort == 80)"
                self._quic_divert = pydivert.WinDivert(quic_filter)
                self._quic_divert.open()
                self._quic_thread = threading.Thread(
                    target=self._quic_drop_loop, daemon=True, name="transparent-quic-block"
                )
                self._quic_thread.start()
                logger.info(
                    "transparent", "QUIC(UDP/443) interception enabled",
                    "Browsers forced to fall back to TCP/HTTPS, transparent proxy can capture browser traffic"
                )
            except Exception as e:  # noqa: BLE001
                # QUIC 拦截失败不影响 TCP 透明代理（仅浏览器 QUIC 流量可能绕过）
                logger.warning(
                    "transparent", "QUIC interception start failed (does not affect TCP transparent proxy)", str(e)
                )
                if self._quic_divert is not None:
                    try:
                        self._quic_divert.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._quic_divert = None
            logger.info(
                "transparent", "Transparent proxy started",
                f"redirect ports={list(_REDIRECT_DST_PORTS)} -> {_REDIRECT_LOOPBACK_ADDR}:{self.local_port} "
                f"(local_ip={self._local_host})",
            )
            return True
        except Exception as e:  # noqa: BLE001
            self._last_error = str(e)
            logger.error("transparent", "Transparent proxy start failed", str(e))
            # 清理已构造但 open 失败的 divert 对象
            if self._divert is not None:
                try:
                    self._divert.close()  # type: ignore
                except Exception:  # noqa: BLE001
                    pass
            self._divert = None
            return False

    def stop(self):
        """Stop the transparent proxy.

        First sets _running=False, then uses shutdown to unblock recv, then closes.
        Avoids directly closing which would leave the worker thread permanently blocked on recv.
        Platform: Windows
        """
        self._running = False
        if self._divert:
            # 先 shutdown 让 recv() 立即返回 None 或抛异常
            # F15 修正：pydivert Python 层（1.x/3.x）从未暴露 shutdown() 方法，
            # 此调用会抛 AttributeError 被 try/except 吞掉（no-op）。
            # 真正解除阻塞靠 join 等待 + close 后 recv 抛 OSError。
            # 保留 try/except 兜底：未来 pydivert 若暴露 shutdown 则自动生效。
            try:
                self._divert.shutdown()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            # 等待工作线程退出（recv 应已解除阻塞）
            if self._thread:
                self._thread.join(timeout=3)
                if self._thread.is_alive():
                    logger.warning("transparent", "Worker thread still running on stop",
                                   "possible blocking recv")
                self._thread = None
            # 最后关闭句柄
            try:
                self._divert.close()
            except Exception:  # noqa: BLE001
                pass
            self._divert = None
        # F39: 关闭 QUIC(UDP/443) 拦截句柄（close 会让 _quic_drop_loop 的 recv 抛异常并退出）
        if self._quic_divert is not None:
            try:
                self._quic_divert.close()
            except Exception:  # noqa: BLE001
                pass
            self._quic_divert = None
        self._quic_thread = None
        with self._nat_lock:
            self._nat_table.clear()
            self._nat_reverse.clear()
            # 同时清空 O(1) (ip, port) 二元组索引，避免 stop 后残留 stale 条目
            # （下次 start 时 _client_port_index 应为空，否则 lookup_reverse 可能命中已失效的旧条目）
            self._client_port_index.clear()
        # 清空代理出站端口集合：停止时所有注册的端口都不再有效，
        # 避免跨重启的 stale 端口注册累积导致 WinDivert 错误排除正常流量。
        # （连接未关闭的端口会被 OS 回收，端口复用后不应再被排除）
        global _proxy_outbound_ports
        with _proxy_outbound_lock:
            _proxy_outbound_ports.clear()
            _proxy_outbound_addrs.clear()
        logger.info("transparent", "Transparent proxy stopped")

    def _loop(self):
        """Main loop: receive packet -> determine direction -> rewrite addresses -> re-inject.

        Outbound packets (dst port in {80}, non-loopback):
            - Record NAT: forward_key -> (src_ip, src_port, dst_ip, dst_port, last_seen)
            - Reverse index: reverse_key -> forward_key
            - dst_ip -> 127.0.0.1, dst_port -> local_port
        Reverse packets (src port = local_port, src/dst ip = 127.0.0.1):
            - Look up reverse index, find forward_key
            - src_ip -> original dst_ip, src_port -> 80
            - Clean up NAT entry on FIN/RST
        """
        while self._running:
            try:
                packet = self._divert.recv()  # type: ignore
                if packet is None:
                    continue
                # 周期性清理过期 NAT 条目（每 30s 一次）
                # F32 修复：移到分支判断前，避免反向未命中 continue 跳过清理。
                # 原位置在 send 之后，导致反向未命中频繁时 NAT 表无法及时清理。
                now = time.monotonic()
                if now - self._last_cleanup > 30.0:
                    self._cleanup_nat()
                    self._last_cleanup = now
                # 仅处理 IPv4（IPv6 透明重定向暂不支持，直接放行）
                ip_hdr = packet.ipv4
                if ip_hdr is None or packet.tcp is None:
                    try:
                        self._divert.send(packet)  # type: ignore
                    except Exception as e:  # noqa: BLE001  # F11 诊断埋点：暴露 IPv6/非TCP send 失败
                        self._send_fail_count += 1
                        if self._send_fail_count == 1 or self._send_fail_count % 100 == 0:
                            raw_len = len(packet.raw) if packet.raw else 0
                            logger.warning(
                                "transparent", "IPv6/non-TCP send failed",
                                f"count={self._send_fail_count} repr={e!r} raw_len={raw_len}"
                            )
                    continue

                tcp_hdr = packet.tcp
                src_ip = ip_hdr.src_addr
                dst_ip = ip_hdr.dst_addr
                src_port = tcp_hdr.src_port
                dst_port = tcp_hdr.dst_port

                # 出站：dst port in {80, 443}, 非 loopback（filter 已保证 dst != 127.0.0.1）
                # 关键：排除代理自身的出站连接（代理连接目标服务器时也会产生 dst port 80/443 的包）
                # F11 修复：用 (src_ip, src_port) 二元组精确匹配，避免纯 port 匹配
                # 在客户端临时端口与代理出站端口碰撞时错误排除正常客户端流量。
                branch = "other"  # F16 诊断：记录分支类型
                if dst_port in _REDIRECT_DST_PORTS and dst_ip != "127.0.0.1" and not _is_proxy_outbound_addr(src_ip, src_port):
                    branch = "outbound"
                    forward_key = (src_ip, src_port, dst_ip, dst_port)
                    reverse_key = (dst_ip, dst_port, src_ip, src_port)
                    now = time.monotonic()
                    with self._nat_lock:
                        # NAT 表条目格式：(src_ip, src_port, dst_ip, dst_port, last_seen, reverse_key)
                        # 第 6 字段 reverse_key 用于 O(1) 反向索引清理
                        self._nat_table[forward_key] = (
                            src_ip, src_port, dst_ip, dst_port, now, reverse_key
                        )
                        self._nat_reverse[reverse_key] = forward_key
                        # 维护 O(1) (client_ip, client_src_port) → forward_key 索引（F2 修复）
                        # F38: 双键注册 —— 同时以「真实客户端 IP(src_ip)」与「改写后的
                        # loopback 地址(127.0.0.2)」为索引键。原因：反向回包（代理→客户端）
                        # 的 dst 取决于 F34 的 src 改写是否生效：
                        #   - 生效：代理回包 dst=127.0.0.2:port（精确键 (127.0.0.2,port) 命中）
                        #   - 未生效：代理回包 dst=真实客户端IP:port（精确键 (src_ip,port) 命中）
                        # 两种 key 都注册，无论哪种情况反向查找都能命中，彻底消除
                        # "反向NAT未命中丢弃包" 中因 dst_ip 与索引键不一致导致的 miss。
                        self._client_port_index[(_REDIRECT_LOOPBACK_ADDR, src_port)] = forward_key
                        self._client_port_index[(src_ip, src_port)] = forward_key
                        # F38 诊断：低速率确认出站重定向与 NAT 注册确实发生
                        # （若此日志长期不出现却仍有反向NAT未命中，说明出站分支根本未被命中，
                        #  问题在 filter/重定向而非 NAT 表）。
                        self._diag_out_count += 1
                        if self._diag_out_count % 200 == 1:
                            logger.info(
                                "transparent", "Outbound redirect NAT registered",
                                f"src={src_ip}:{src_port} dst={dst_ip}:{dst_port} "
                                f"nat_size={len(self._nat_table)}"
                            )
                    # F34: 同时改写 src 和 dst 为 127.0.0.2，使包走 127.0.0.0/8 loopback
                    # adapter（WinDivert 可拦截）。原方案仅改 dst 为本机真实 IP，导致
                    # src==dst==本机IP 走 TCP fast path 绕过 WinDivert，反向 NAT 失效。
                        ip_hdr.src_addr = _REDIRECT_LOOPBACK_ADDR
                    ip_hdr.dst_addr = _REDIRECT_LOOPBACK_ADDR
                    tcp_hdr.dst_port = self.local_port
                    # F36: 不再在用户态调用 packet.recalculate_checksums()。
                    # 该调用会访问包的缓冲区，而 WinDivert 在 send 前可能已在内核侧
                    # 回收该缓冲区，导致 PermissionError(13,'段已解除锁定',158)，
                    # 且随后的 self._divert.send 因同一失效缓冲区重试 5 次均失败 →
                    # 改写后的 SYN 包永久丢失 → 浏览器连接超时（"浏览器都不行"）。
                    # 改由 send(recalculate_checksum=True) 在内核态重算校验和，
                    # 不触碰用户态缓冲区，避免该错误。
                    self._redirected_count += 1
                # 反向：src port = local_port（代理回包专用端口，唯一标识代理→客户端的回包）
                # 不再限定 src ip 必须为 127.0.0.1：代理监听 0.0.0.0 时回包 src 是本机真实 IP；
                # 且出站分支只匹配 dst port 80/443，不会误匹配本分支。
                elif src_port == self.local_port:
                    branch = "reverse"
                    # F2 修复：用 (dst_ip, dst_port) 二元组反查，避免纯 port 索引
                    # 在端口复用时把普通系统代理客户端的响应错误改写成外部服务器 IP:port。
                    # dst_ip/dst_port 是反向包的目的（即客户端的 IP/端口）。
                    with self._nat_lock:
                        forward_key = self._client_port_index.get((dst_ip, dst_port))
                        entry = self._nat_table.get(forward_key) if forward_key else None
                        # F38: 精确键 (dst_ip, dst_port) 未命中时，按端口扫描索引，
                        # 优先选取 orig_src_ip == dst_ip（真实客户端 IP 与反向回包 dst 一致）
                        # 的条目。覆盖 F34 src 改写未生效、回包 dst 为真实客户端 IP 而索引
                        # 仅以 127.0.0.2 注册时精确键 miss 的场景。
                        if entry is None:
                            for (idx_ip, idx_port), fk in self._client_port_index.items():
                                if idx_port != dst_port:
                                    continue
                                e = self._nat_table.get(fk)
                                if e is None:
                                    continue
                                if e[0] == dst_ip:
                                    forward_key, entry = fk, e
                                    break
                    if entry:
                        orig_src_ip, orig_src_port, orig_dst_ip, orig_dst_port, _, stored_reverse_key = entry
                        # 改写 src -> 原服务器（让客户端以为回包来自原目标服务器）
                        ip_hdr.src_addr = orig_dst_ip
                        tcp_hdr.src_port = orig_dst_port
                        # F34: 同时改写 dst -> 原客户端真实 IP:port
                        # 代理回包的 dst 是 127.0.0.2:client_port（改写后的地址），
                        # 需恢复为原客户端 IP:port（如 192.168.6.117:client_port），
                        # 否则客户端收到的包 dst 不匹配其 socket → 静默丢弃。
                        ip_hdr.dst_addr = orig_src_ip
                        tcp_hdr.dst_port = orig_src_port
                        # F36: 同出站分支，校验和交由 send(recalculate_checksum=True) 内核态重算
                        self._redirected_count += 1
                        # 反向包命中时刷新 last_seen，避免长连接 60s 后被清理导致隧道断裂
                        now = time.monotonic()
                        # F37: NAT 条目清理改为"仅 RST 才删，FIN 只刷新 last_seen"。
                        # 原实现在收到代理→客户端的 FIN 时立即删除条目，但 HTTP 等协议常出现
                        # "客户端发完请求先 FIN、服务器响应稍后到达"的时序：响应回包到达时
                        # 条目已被删 → 反向 NAT 未命中 → 回包被丢弃 → 连接异常/重传。
                        # 改为 FIN 不删（视作活动、刷新 last_seen），仅 RST（连接确已中断）才删，
                        # 条目最终由 TTL(300s) 或 RST 清理，避免误删导致的反向丢包。
                        # 兼容不同 pydivert 版本：有些版本无 flags 属性，用 fin/rst 布尔属性
                        if hasattr(tcp_hdr, 'flags'):
                            flags = tcp_hdr.flags
                            is_rst = bool(flags & 0x04)
                        else:
                            is_rst = bool(getattr(tcp_hdr, 'rst', False))
                        with self._nat_lock:
                            # F38: FIN 与 RST 均只刷新 last_seen，不删除条目、不清理索引。
                            # 原实现在 RST 时删除条目并清理索引，但代理可能在 RST 后仍有
                            # 服务端数据段要回传给客户端，删除后这些回包反向 NAT 未命中
                            # 被丢弃 → 连接假死/重传（典型症状："浏览器都不行"）。
                            # 保留条目与索引，最终由 _cleanup_nat(TTL=300s) 或 stop() 统一清理。
                            # 仅当条目仍存在时刷新 last_seen：避免在释放锁→改写头部→重新
                            # 持锁的窗口内被 _cleanup_nat 删除后又重添加，导致 _nat_table
                            # 与 _client_port_index 不一致（索引未重建，后续精确查找 miss）。
                            if forward_key in self._nat_table:
                                self._nat_table[forward_key] = (
                                    orig_src_ip, orig_src_port,
                                    orig_dst_ip, orig_dst_port, now, stored_reverse_key,
                                )
                    else:
                        # 反向 NAT 未命中：丢弃包（不 send），避免客户端收到 src=本机IP:8888
                        # 的包导致内核 RST。原实现"原样放行"会让客户端收到 src 不匹配的包 → RST。
                        # 丢弃后客户端超时重传，重传的出站包会建立新 NAT 条目，反向包能正确命中。
                        # F25 诊断日志：揭示反向 NAT 未命中的根因（采样率 1/100 避免日志爆炸）
                        self._nat_miss_count = getattr(self, '_nat_miss_count', 0) + 1
                        if self._nat_miss_count == 1 or self._nat_miss_count % 100 == 0:
                            with self._nat_lock:
                                nat_size = len(self._nat_table)
                                idx_size = len(self._client_port_index)
                                sample_keys = list(self._client_port_index.keys())[:5]
                            logger.warning(
                                "transparent", "Reverse NAT miss, packet dropped",
                                f"count={self._nat_miss_count} "
                                f"lookup=(dst={dst_ip}:{dst_port}) "
                                f"nat_size={nat_size} idx_size={idx_size} "
                                f"sample_keys={sample_keys}"
                            )
                        continue  # 跳过 send，丢弃包
                else:
                    # filter 已限定，理论上不会到这里
                    self._passed_count += 1

                # F16+F17 修复：send 失败时增强诊断 + 指数退避重试。
                # 根因（Top 1）：WinDivert 1.3.0 驱动段锁定级联失败，send 抛
                # PermissionError(13,'段已解除锁定',None,158)，改写后的 SYN 包永久丢失 →
                # 客户端连接超时 → 代理收不到 HTTP 请求 → "抓不到 HTTP 包"。
                # 重试安全：send 失败时包未注入网络栈，重试不会产生重复包。
                # F32 增强：重试次数 3→5，退避 0.5ms/1ms/4ms/16ms（原 1ms/4ms/16ms/64ms 在高负载下累积延迟过大）。
                # Platform: Windows
                send_ok = False
                for attempt in range(5):
                    try:
                        self._divert.send(packet)  # type: ignore
                        send_ok = True
                        break
                    except Exception as e:  # noqa: BLE001
                        if attempt < 4:
                            time.sleep(0.0005 * (2 ** attempt))  # 0.5ms, 1ms, 2ms, 4ms
                            continue
                        # 5 次均失败：记录增强诊断日志
                        self._send_fail_count += 1
                        if self._send_fail_count == 1 or self._send_fail_count % 100 == 0:
                            raw_len = len(packet.raw) if packet.raw else 0
                            try:
                                direction = packet.direction
                            except Exception:  # noqa: BLE001
                                direction = "?"
                            try:
                                is_lb = packet.is_loopback
                            except Exception:  # noqa: BLE001
                                is_lb = "?"
                            logger.warning(
                                "transparent", "send failed",
                                f"count={self._send_fail_count} repr={e!r} branch={branch} "
                                f"src={src_ip}:{src_port} dst={dst_ip}:{dst_port} "
                                f"dir={direction} loopback={is_lb} raw_len={raw_len}"
                            )
                if not send_ok:
                    pass  # 包丢失，等待客户端超时重传

            except Exception as e:  # noqa: BLE001
                if self._running:
                    logger.warning("transparent", "Capture loop exception", str(e))
                    time.sleep(0.01)

    def _quic_drop_loop(self):
        """F39: QUIC (UDP/443) drop loop.

        Receives outbound UDP/443(80) packets from a dedicated WinDivert handle and **does not re-inject** them
        (i.e. drops them), causing the browser's QUIC handshake to fail and fall back to TCP/HTTPS
        (intercepted and redirected to the proxy by the main handle).
        Only drops, does not rewrite, so it does not touch any NAT table or checksums.
        """
        while self._running and self._quic_divert is not None:
            try:
                pkt = self._quic_divert.recv()
            except Exception:  # noqa: BLE001
                # 句柄被 close() 后 recv 抛异常 → 根据 _running 退出
                if self._running:
                    time.sleep(0.01)
                else:
                    break
                continue
            if pkt is None:
                continue
            # 直接丢弃：不调用 send()，包不会被注入网络栈
            # （WinDivert 默认不重注入已 recv 的包，故无需额外操作）

    def _recalc_checksums(self, packet):
        """Recompute packet checksums, compatible with different pydivert versions.

        F4 fix: the original fallback chain ended with `pass` silently swallowing errors,
        causing packets with rewritten IP/TCP headers but unchanged checksums to be silently
        dropped by the receiver, manifesting as a "full timeout with no logs" hidden failure.
        Now all fallback failures log a warning for easier troubleshooting.

        F12 fix: pydivert 3.x renamed the method from `recalc_checksums()` to
        `recalculate_checksums()`. The old code's three fallbacks all used non-existent API names
        (`packet.recalc_checksums`, `pydivert.WinDivertHelper`,
        `self._divert.recalc_checksums`), causing every rewritten packet to fall into the warning
        branch without actually recomputing checksums. Now probes both old and new method names by existence.

        Note: WinDivert.send(packet) defaults to `recalculate_checksum=True` which recomputes
        again, so even if this fails, sending still covers it — but explicit recomputation exposes errors earlier.
        """
        # 优先 pydivert 3.x 新名，回退 pydivert 2.x 旧名
        for attr in ("recalculate_checksums", "recalc_checksums"):
            method = getattr(packet, attr, None)
            if method is None:
                continue
            try:
                method()
                return
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "transparent", "Checksum recalc exception",
                    f"packet.{attr}() raised: {e!r}"
                )
                return
        # 兜底：packet 上两个方法都不存在（极旧版或非 pydivert 包）
        logger.warning(
            "transparent", "Checksum recalc failed",
            "Cannot find recalculate_checksums / recalc_checksums method on packet, "
            "falling back to WinDivert.send() default recalculate_checksum=True"
        )

    def _find_by_client_port(self, client_src_port: int):
        """Reverse-lookup NAT entry by the client's original src_port.

        Simplified reverse lookup: assumes that at any given moment a client_src_port corresponds
        to only one connection (Windows client ephemeral port range is 49152-65535, collision probability is very low).
        A stricter implementation should use the (src_ip, src_port) tuple for reverse lookup.
        """
        for forward_key, entry in self._nat_table.items():
            if forward_key[1] == client_src_port:
                return forward_key, entry
        return None

    def _cleanup_nat(self):
        """Clean up expired NAT entries (TTL 300s of inactivity).

        Prevents the NAT table from growing unboundedly without breaking in-flight connections
        (active connections update last_seen).

        Performance optimization: uses list(self._nat_table) to copy only the key list (lighter than items()),
        and synchronously cleans up _client_port_index and _nat_reverse to reduce memory copy overhead on large tables.
        F6 fix: the reverse index uses O(1) direct pop (replacing the original O(n) scan of _nat_reverse),
        eliminating the O(n²) performance bottleneck of periodic cleanup on large NAT tables.
        """
        now = time.monotonic()
        expired_keys = []
        with self._nat_lock:
            # 仅复制 key 列表，避免复制整个 items 元组
            for forward_key in list(self._nat_table):
                entry = self._nat_table[forward_key]
                last_seen = entry[4]
                if now - last_seen > _NAT_TTL:
                    expired_keys.append(forward_key)
            for fk in expired_keys:
                entry = self._nat_table.pop(fk, None)
                if entry is None:
                    continue
                # fk = (src_ip, src_port, dst_ip, dst_port) — 用 (ip, port) 二元组键
                # F38: 双键弹出（真实客户端 IP 与 127.0.0.2 两种索引键都要清理）
                self._client_port_index.pop((fk[0], fk[1]), None)
                self._client_port_index.pop((_REDIRECT_LOOPBACK_ADDR, fk[1]), None)
                # O(1) 反向索引清理：entry[5] 是创建时存储的 reverse_key
                if len(entry) >= 6:
                    self._nat_reverse.pop(entry[5], None)
        if expired_keys:
            logger.info("transparent", "NAT table expired entries cleaned",
                        f"cleaned {len(expired_keys)} entries, {len(self._nat_table)} remaining")


# ---------- 单例管理 ----------
# 类型注解：Unix 平台上 _instance 可能是 UnixTransparentProxy 实例（duck typing）
# 用 Any 避免循环依赖，运行时通过 duck typing 调用 start/stop/running/last_error/status
_instance: "TransparentProxy | Any" = None

# 代理实际监听地址（由 __main__ 在创建代理后写入）。
# 透明代理要求其为 0.0.0.0（或本机真实 IP），否则 WinDivert 把流量重定向到本机 IP 后
# 监听 127.0.0.1 的代理收不到包。默认 "127.0.0.1"（最严格，但透明代理不能工作）。
proxy_listen_host: str = "127.0.0.1"
_lock = threading.Lock()


def get_transparent_proxy() -> "TransparentProxy | Any":
    """Get the transparent proxy singleton (lazy initialization).

    Returns a TransparentProxy instance on Windows, and a UnixTransparentProxy instance on Unix.
    """
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                if IS_WINDOWS:
                    _instance = TransparentProxy()
                else:
                    # Unix 平台：延迟导入 UnixTransparentProxy
                    from .transparent_proxy_unix import UnixTransparentProxy
                    _instance = UnixTransparentProxy()
    return _instance


def start_transparent_proxy() -> tuple[bool, str]:
    """Start the transparent proxy. Returns (success, msg).

    Platform support:
    - Windows: WinDivert (requires pydivert + administrator privileges)
    - Linux: iptables NAT REDIRECT (requires root)
    - macOS: pf rdr (requires root)
    """
    # 透明代理要求代理监听 0.0.0.0（WinDivert 把流量重定向到本机真实 IP）。
    # 若代理仅监听 127.0.0.1，重定向到本机 IP 的包会被丢弃，表现为「全超时、无日志」。
    # 常见原因：未在启动前开启透明代理（__main__ 会强制 0.0.0.0），或没开「允许局域网连接」。
    if proxy_listen_host == "127.0.0.1":
        return False, (
            "Transparent proxy requires the proxy to listen on 0.0.0.0: currently the proxy only listens on 127.0.0.1, "
            "traffic redirected to the local IP cannot be received. Please enable \"Allow LAN device connections\" "
            "in settings and restart Telnix, or enable transparent proxy before starting and then restart."
        )
    # F19: 撤销 F18 互斥。F18 互斥导致场景3（抓不到HTTP）和场景4（RESET）：
    # 开抓包→停透明代理→NAT表清空→已建立连接回包未改写→RESET；
    # 且 raw_capture 排除 8888 端口→系统代理流量抓不到。
    # raw_capture 用 SNIFF 模式（flags=1，只读不拦截），不阻止 transparent_proxy 的包流。
    # F17 的 send 重试机制足以应对双 handle 下的瞬时段锁定。
    proxy = get_transparent_proxy()
    if proxy.running:
        return True, "Already running"
    if proxy.start():
        backend = "WinDivert" if IS_WINDOWS else ("iptables" if IS_LINUX else "pf")
        return True, f"Transparent proxy started ({backend})"
    return False, proxy.last_error or "Start failed"


def stop_transparent_proxy() -> tuple[bool, str]:
    """Stop the transparent proxy."""
    proxy = get_transparent_proxy()
    if not proxy.running:
        return True, "Not running"
    proxy.stop()
    return True, "Transparent proxy stopped"


def transparent_proxy_status() -> dict:
    """Return transparent proxy status.

    Platform support:
    - Windows: WinDivert backend status
    - Linux: iptables backend status (requires root)
    - macOS: pf backend status (requires root)

    Cross-platform field alignment: all platforms return running/supported/is_admin/backend/hint,
    so the frontend can uniformly read these fields to determine UI state (admin label, backend label, elevation button visibility, etc.).
    """
    if not IS_WINDOWS:
        # Unix 平台：返回后端可用性 + 运行状态
        try:
            is_admin = os.geteuid() == 0
        except AttributeError:
            is_admin = False
        backend = "iptables" if IS_LINUX else ("pf" if IS_MACOS else "none")
        running = bool(_instance and _instance.running)
        if running:
            hint = "Ready (running)"
        elif not is_admin:
            hint = "Root privileges required. Please start Telnix with sudo"
        else:
            hint = "Ready"
        return {
            "running": running,
            "supported": True,
            "is_admin": is_admin,
            "backend": backend,
            "hint": hint,
            **(_instance.status() if _instance else {}),
        }
    # Windows 平台：补全跨平台字段（is_admin/backend/hint），与 Unix 分支对齐
    proxy = get_transparent_proxy()
    is_admin = proxy._is_admin()
    running = proxy.running
    if running:
        hint = "Ready (running)"
    elif not is_admin:
        hint = "Administrator privileges required. Please restart Telnix as administrator"
    else:
        hint = "Ready"
    return {
        "running": running,
        "supported": True,
        "is_admin": is_admin,
        "backend": "windivert",
        "hint": hint,
        **proxy.status(),
    }


def lookup_original_dst(sock: socket.socket) -> Optional[tuple[str, int]]:
    """Query the original destination address before transparent proxy redirection (cross-platform).

    Windows: reverse-lookup via NAT table (client_src_port) - this interface does not apply, use lookup_reverse
    Linux: getsockopt(SOL_IP, SO_ORIGINAL_DST)
    macOS: getsockname (pf rdr puts the original target into the socket's local address)

    Returns: (orig_dst_ip, orig_dst_port) or None (query failed/unsupported)
    """
    if not IS_WINDOWS and _instance is not None:
        # Unix 平台：委托给 UnixTransparentProxy.lookup_original_dst
        return _instance.lookup_original_dst(sock)
    # Windows 平台：此接口不适用（Windows 用 NAT 表反查 client_src_port）
    return None


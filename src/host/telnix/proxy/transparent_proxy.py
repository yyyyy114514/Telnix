"""透明代理模式（简化版）- 跨平台 NETWORK 层重定向。

工作原理：
- Windows: WinDivert 拦截出站 TCP dstPort=80/443 的包，改写地址到本地代理
- Linux: iptables NAT REDIRECT 把出站 TCP 80/443 重定向到本地代理端口
- macOS: pf rdr 把出站 TCP 80/443 重定向到本地代理端口

简化范围：
- HTTP(80) 走代理正常解析；HTTPS(443) 走 raw TCP 隧道（不解密，端到端 TLS）
- 仅出站连接，不处理入站
- 仅在用户显式启用时工作（设置 transparent_proxy=True）
- 与系统代理并存：系统代理处理已配置的客户端，透明代理处理无代理感知的客户端

隐蔽性优势：
- 应用无需配置代理，对 HTTP 流量完全透明
- 不修改系统注册表（不写 ProxyServer/ProxyEnable）
- 浏览器无代理感知，难以通过常规手段探测

跨平台说明：
- Windows: WinDivert NETWORK 层拦截 + 用户态 NAT 表
- Unix: iptables/pf 内核态 NAT，反向流量自动处理（无需 NAT 表）
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
    """探测本机非回环 IPv4 地址（用于透明代理重定向目标）。

    透明代理必须把出站包的目的地址改写成「本机真实 IP」而非 127.0.0.1：
    WinDivert 拦截到的出站包 src 是客户端真实 IP（如 192.168.x.x），若把 dst
    改成 127.0.0.1，Windows 会因 loopback 反欺骗（dst=127.0.0.1 但 src 非
    127.0.0.1）直接丢弃该包，代理永远收不到连接 → 全超时、无日志。改成重定向
    到本机真实 IP 后，包变成「本机发往本机」（src/dst 同为本机 IP），Windows
    正常投递给监听 0.0.0.0 的代理。

    探测策略（多重 fallback，避免单点失败导致静默回退 127.0.0.1）：
    1. UDP connect 公网 IP（8.8.8.8 / 114.114.114.114）让 OS 选择出口网卡
    2. socket.getaddrinfo(gethostname()) 枚举解析得到的本机 IP
    3. 仍找不到非回环 IP 时返回空串，让调用方判定为不支持（不再静默回退 127.0.0.1）
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
    """注册代理自身的出站连接本地端口（兼容旧接口，供 server.py 调用）。"""
    with _proxy_outbound_lock:
        _proxy_outbound_ports.add(port)


def unregister_proxy_port(port: int):
    """注销代理自身的出站连接本地端口。"""
    with _proxy_outbound_lock:
        _proxy_outbound_ports.discard(port)
        # 同步清理可能存在的 (any_ip, port) 条目（best-effort，无法精确匹配 ip）
        for ip_port in list(_proxy_outbound_addrs):
            if ip_port[1] == port:
                _proxy_outbound_addrs.discard(ip_port)


def register_proxy_addr(src_ip: str, src_port: int):
    """注册代理自身出站连接的 (src_ip, src_port) 二元组（精确匹配，F11 修复）。"""
    with _proxy_outbound_lock:
        _proxy_outbound_ports.add(src_port)
        _proxy_outbound_addrs.add((src_ip, src_port))


def unregister_proxy_addr(src_ip: str, src_port: int):
    """注销代理自身出站连接的 (src_ip, src_port) 二元组。"""
    with _proxy_outbound_lock:
        _proxy_outbound_ports.discard(src_port)
        _proxy_outbound_addrs.discard((src_ip, src_port))


def _is_proxy_outbound_port(port: int) -> bool:
    """检查端口是否属于代理自身的出站连接（兼容旧接口）。"""
    with _proxy_outbound_lock:
        return port in _proxy_outbound_ports


def _is_proxy_outbound_addr(src_ip: str, src_port: int) -> bool:
    """检查 (src_ip, src_port) 是否属于代理自身的出站连接。

    匹配策略（两阶段注册）：
    1. 优先 (ip, port) 精确匹配 —— connect() 完成后由 register_proxy_addr 注册
    2. 回退到纯 port 匹配 —— connect() 前由 register_proxy_port 注册（TOCTOU 窗口期）

    端口回退始终启用（不再仅当 _proxy_outbound_addrs 为空时）：
    代理出站 socket 在 connect() 前仅注册 port（getsockname() 此时返回 0.0.0.0，
    无法获取真实源 IP），connect() 后才升级为 (ip, port)。connect 窗口期内
    发出的 SYN 包必须靠 port 匹配排除，否则 WinDivert 拦截代理自身 SYN
    形成无限重定向循环（全超时无日志的根因）。

    原实现缺陷：当 _proxy_outbound_addrs 非空时完全禁用端口回退，
    但 _register_proxy_socket 在 connect 前 getsockname 得到 0.0.0.0，
    注册的是 ('0.0.0.0', port) 死数据，真实出站包 src_ip 永远匹配不上，
    端口回退又被禁用 → 代理自身流量完全未被排除 → 无限循环。
    """
    with _proxy_outbound_lock:
        if (src_ip, src_port) in _proxy_outbound_addrs:
            return True
        return src_port in _proxy_outbound_ports


class TransparentProxy:
    """透明代理：用 WinDivert NETWORK 层重定向 HTTP(80) 流量到本地代理。

    实现要点：
    - NETWORK 层抓包：能拿到完整 IP/TCP 头
    - 修改目的 IP/Port 为本地代理，重新计算校验和后 send
    - 维护 NAT 表：原 (src_ip:port, dst_ip:80) <-> (127.0.0.1:port, 127.0.0.1:8888)
    - 反向流量（代理回包）：查 NAT 表，把 src 从本地改回原服务器 IP:80
    - 非本代理端口的流量直接放行（不影响其他网络活动）

    关键防循环：代理自身连接目标服务器时，其出站包也匹配 WinDivert filter
    （dst port 80/443, 非 loopback）。通过 _proxy_outbound_ports 集合排除
    代理自身的出站连接，避免无限重定向导致全部 timeout。

    NAT 表设计：
    - key = (src_ip, src_port, dst_ip, dst_port) 四元组（出站时记录）
    - 反向查找 key = (原 dst_ip, 80, 原 src_ip, 原 src_port)（反向四元组）
    - 每个条目带 last_seen 时间戳，TTL 300s 自动清理
    - TCP FIN/RST 收到时立即清理对应条目
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
        # 上次清理时间
        self._last_cleanup = time.monotonic()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> str:
        return self._last_error

    def status(self) -> dict:
        """返回透明代理状态。"""
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
        """供代理服务器在 raw tunnel 模式下反查原目标。

        参数：
            client_src_port - 客户端连接到本地代理时的源端口
                （等价于原出站包的 src_port，唯一标识一条 NAT 条目）
            client_src_ip - 客户端连接到本地代理时的源 IP（可选，F2 修复）。
                若提供，则用 (client_src_ip, client_src_port) 二元组精确反查，
                避免不同客户端端口复用时的误匹配；若为 None 则回退到纯 port 匹配
                （兼容旧行为，但端口复用时可能误匹配）。

        返回：(orig_dst_ip, orig_dst_port) 或 None（无匹配）

        性能优化：通过 _client_port_index 字典实现 O(1) 查找，
        替代原 O(n) 遍历 _nat_table（NAT 表大时性能差距显著）。
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
        """F26: 为 SNI fallback 主动注册 NAT 条目。

        当 lookup_reverse 失败但 SNI fallback 成功连接目标后，调用此方法
        注册 NAT 条目，确保反向回包能被正确改写回原服务器 IP:port。
        否则客户端收到 src=local_ip:8888 的包 → RST。
        """
        forward_key = (client_ip, client_port, orig_dst_ip, orig_dst_port)
        reverse_key = (orig_dst_ip, orig_dst_port, client_ip, client_port)
        now = time.monotonic()
        with self._nat_lock:
            self._nat_table[forward_key] = (
                client_ip, client_port, orig_dst_ip, orig_dst_port, now, reverse_key
            )
            self._nat_reverse[reverse_key] = forward_key
            self._client_port_index[(client_ip, client_port)] = forward_key

    def _is_admin(self) -> bool:
        """检查管理员权限。

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
        """启动透明代理。返回 True 成功，False 失败。

        平台支持：
        - Windows: WinDivert NETWORK 层拦截（需 pydivert + 管理员权限）
        - Linux: iptables NAT REDIRECT（需 root + iptables）
        - macOS: pf rdr（需 root + pfctl）
        """
        if not IS_WINDOWS:
            self._last_error = "TransparentProxy 类仅支持 Windows，Unix 请用 UnixTransparentProxy"
            return False
        if self._running:
            return True
        if not self._is_admin():
            self._last_error = "需要管理员权限（WinDivert 要求）"
            return False
        if pydivert is None:
            self._last_error = "pydivert 未安装，请运行: pip install pydivert"
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
                    "无法探测本机非回环 IPv4 地址（UDP connect 8.8.8.8/114.114.114.114/223.5.5.5 "
                    "均失败，且 getaddrinfo(hostname) 未返回非回环 IP）。"
                    "透明代理无法工作：重定向目标若是 127.0.0.1 会被 Windows loopback "
                    "反欺骗丢弃，导致全超时无日志。请检查默认路由/网卡/防火墙后重试。"
                )
                logger.error("transparent", "透明代理启动失败：无法探测本机非回环 IP", self._last_error)
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
            logger.info(
                "transparent", "透明代理已启动",
                f"redirect ports={list(_REDIRECT_DST_PORTS)} -> {_REDIRECT_LOOPBACK_ADDR}:{self.local_port} "
                f"(local_ip={self._local_host})",
            )
            return True
        except Exception as e:  # noqa: BLE001
            self._last_error = str(e)
            logger.error("transparent", "透明代理启动失败", str(e))
            # 清理已构造但 open 失败的 divert 对象
            if self._divert is not None:
                try:
                    self._divert.close()  # type: ignore
                except Exception:  # noqa: BLE001
                    pass
            self._divert = None
            return False

    def stop(self):
        """停止透明代理。

        先 _running=False，再用 shutdown 解除 recv 阻塞，再 close。
        避免直接 close 导致工作线程永久阻塞在 recv。
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
                    logger.warning("transparent", "停止时工作线程仍在运行",
                                   "可能存在阻塞 recv")
                self._thread = None
            # 最后关闭句柄
            try:
                self._divert.close()
            except Exception:  # noqa: BLE001
                pass
            self._divert = None
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
        logger.info("transparent", "透明代理已停止")

    def _loop(self):
        """主循环：接收包 -> 判断方向 -> 改写地址 -> 重新注入。

        出站包（dst port in {80}, 非 loopback）：
            - 记录 NAT: forward_key -> (src_ip, src_port, dst_ip, dst_port, last_seen)
            - 反向索引: reverse_key -> forward_key
            - dst_ip -> 127.0.0.1, dst_port -> local_port
        反向包（src port = local_port, src/dst ip = 127.0.0.1）：
            - 查反向索引，找 forward_key
            - src_ip -> 原 dst_ip, src_port -> 80
            - FIN/RST 时清理 NAT 条目
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
                                "transparent", "IPv6/非TCP send 失败",
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
                        # F34:索引用改写后的 loopback 地址（127.0.0.2），因为代理收到的
                        # client_addr 是改写后的 src（127.0.0.2:src_port），lookup_reverse
                        # 和 register_nat_entry 都以此 key 查找。
                        self._client_port_index[(_REDIRECT_LOOPBACK_ADDR, src_port)] = forward_key
                    # F34: 同时改写 src 和 dst 为 127.0.0.2，使包走 127.0.0.0/8 loopback
                    # adapter（WinDivert 可拦截）。原方案仅改 dst 为本机真实 IP，导致
                    # src==dst==本机IP 走 TCP fast path 绕过 WinDivert，反向 NAT 失效。
                    ip_hdr.src_addr = _REDIRECT_LOOPBACK_ADDR
                    ip_hdr.dst_addr = _REDIRECT_LOOPBACK_ADDR
                    tcp_hdr.dst_port = self.local_port
                    self._recalc_checksums(packet)
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
                        self._recalc_checksums(packet)
                        self._redirected_count += 1
                        # 反向包命中时刷新 last_seen，避免长连接 60s 后被清理导致隧道断裂
                        now = time.monotonic()
                        # TCP FIN/RST 时清理 NAT 条目
                        # 兼容不同 pydivert 版本：有些版本无 flags 属性，用 fin/rst 布尔属性
                        if hasattr(tcp_hdr, 'flags'):
                            flags = tcp_hdr.flags
                            is_close = bool((flags & 0x01) or (flags & 0x04))
                        else:
                            is_close = bool(getattr(tcp_hdr, 'fin', False) or getattr(tcp_hdr, 'rst', False))
                        with self._nat_lock:
                            if is_close:
                                self._nat_table.pop(forward_key, None)
                                # O(1) 反向索引清理（替代原 O(n) 扫描 _nat_reverse）
                                self._nat_reverse.pop(stored_reverse_key, None)
                                self._client_port_index.pop((dst_ip, dst_port), None)
                            else:
                                # 更新 last_seen（保持长连接 NAT 条目不过期）
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
                                "transparent", "反向NAT未命中丢弃包",
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
                # F32 增强：重试次数 3→5，退避 1ms/4ms/16ms/64ms（原 1ms/4ms 在高负载下不足）。
                # Platform: Windows
                send_ok = False
                for attempt in range(5):
                    try:
                        self._divert.send(packet)  # type: ignore
                        send_ok = True
                        break
                    except Exception as e:  # noqa: BLE001
                        if attempt < 4:
                            time.sleep(0.001 * (4 ** attempt))  # 1ms, 4ms, 16ms, 64ms
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
                                "transparent", "send 失败",
                                f"count={self._send_fail_count} repr={e!r} branch={branch} "
                                f"src={src_ip}:{src_port} dst={dst_ip}:{dst_port} "
                                f"dir={direction} loopback={is_lb} raw_len={raw_len}"
                            )
                if not send_ok:
                    pass  # 包丢失，等待客户端超时重传

            except Exception as e:  # noqa: BLE001
                if self._running:
                    logger.warning("transparent", "抓包循环异常", str(e))
                    time.sleep(0.01)

    def _recalc_checksums(self, packet):
        """重算包校验和，兼容不同 pydivert 版本。

        F4 修复：原实现 fallback 链最终 `pass` 静默吞错，导致改写了 IP/TCP 头
        但校验和未更新时包被接收方静默丢弃，表现为"全超时无日志"的隐蔽失败。
        现在所有 fallback 失败时记录 warning 日志，便于排查。

        F12 修复：pydivert 3.x 把方法名从 `recalc_checksums()` 改为
        `recalculate_checksums()`。旧代码三个 fallback 全用了不存在的 API 名
        （`packet.recalc_checksums`、`pydivert.WinDivertHelper`、
        `self._divert.recalc_checksums`），导致每次改写包都落到 warning 分支，
        实际未重算校验和。现按存在性顺序探测新旧两个方法名。

        备注：WinDivert.send(packet) 默认 `recalculate_checksum=True` 会再重算
        一次，所以即便这里失败，发送时仍会兜底——但显式重算让错误能更早暴露。
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
                    "transparent", "校验和重算异常",
                    f"packet.{attr}() 抛异常: {e!r}"
                )
                return
        # 兜底：packet 上两个方法都不存在（极旧版或非 pydivert 包）
        logger.warning(
            "transparent", "校验和重算失败",
            "packet 上找不到 recalculate_checksums / recalc_checksums 方法，"
            "依赖 WinDivert.send() 默认 recalculate_checksum=True 兜底"
        )

    def _find_by_client_port(self, client_src_port: int):
        """通过客户端原 src_port 反查 NAT 条目。

        简化反查：假设同一时刻同一 client_src_port 只对应一条连接
        （Windows 客户端临时端口范围 49152-65535，碰撞概率极低）。
        更严格实现应用 (src_ip, src_port) 二元组反查。
        """
        for forward_key, entry in self._nat_table.items():
            if forward_key[1] == client_src_port:
                return forward_key, entry
        return None

    def _cleanup_nat(self):
        """清理过期 NAT 条目（TTL 300s 未活动）。

        避免 NAT 表无限增长，同时不破坏在途连接（活跃连接会更新 last_seen）。

        性能优化：用 list(self._nat_table) 仅复制 key 列表（比 items() 轻量），
        并同步清理 _client_port_index 与 _nat_reverse，减少大表时的内存复制开销。
        F6 修复：反向索引用 O(1) 直接 pop（替代原 O(n) 扫描 _nat_reverse），
        消除大 NAT 表下周期清理的 O(n²) 性能瓶颈。
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
                self._client_port_index.pop((fk[0], fk[1]), None)
                # O(1) 反向索引清理：entry[5] 是创建时存储的 reverse_key
                if len(entry) >= 6:
                    self._nat_reverse.pop(entry[5], None)
        if expired_keys:
            logger.info("transparent", "NAT 表清理过期条目",
                        f"清理 {len(expired_keys)} 条，剩余 {len(self._nat_table)}")


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
    """获取透明代理单例（懒初始化）。

    Windows 平台返回 TransparentProxy 实例，Unix 平台返回 UnixTransparentProxy 实例。
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
    """启动透明代理。返回 (success, msg)。

    平台支持：
    - Windows: WinDivert（需 pydivert + 管理员权限）
    - Linux: iptables NAT REDIRECT（需 root）
    - macOS: pf rdr（需 root）
    """
    # 透明代理要求代理监听 0.0.0.0（WinDivert 把流量重定向到本机真实 IP）。
    # 若代理仅监听 127.0.0.1，重定向到本机 IP 的包会被丢弃，表现为「全超时、无日志」。
    # 常见原因：未在启动前开启透明代理（__main__ 会强制 0.0.0.0），或没开「允许局域网连接」。
    if proxy_listen_host == "127.0.0.1":
        return False, (
            "透明代理需要代理监听 0.0.0.0：当前代理仅监听 127.0.0.1，"
            "重定向到本机 IP 的流量无法被接收。请到设置开启「允许局域网设备连接」"
            "后重启 Telnix，或在启动前开启透明代理后再重启。"
        )
    # F19: 撤销 F18 互斥。F18 互斥导致场景3（抓不到HTTP）和场景4（RESET）：
    # 开抓包→停透明代理→NAT表清空→已建立连接回包未改写→RESET；
    # 且 raw_capture 排除 8888 端口→系统代理流量抓不到。
    # raw_capture 用 SNIFF 模式（flags=1，只读不拦截），不阻止 transparent_proxy 的包流。
    # F17 的 send 重试机制足以应对双 handle 下的瞬时段锁定。
    proxy = get_transparent_proxy()
    if proxy.running:
        return True, "已在运行"
    if proxy.start():
        backend = "WinDivert" if IS_WINDOWS else ("iptables" if IS_LINUX else "pf")
        return True, f"透明代理已启动（{backend}）"
    return False, proxy.last_error or "启动失败"


def stop_transparent_proxy() -> tuple[bool, str]:
    """停止透明代理。"""
    proxy = get_transparent_proxy()
    if not proxy.running:
        return True, "未在运行"
    proxy.stop()
    return True, "透明代理已停止"


def transparent_proxy_status() -> dict:
    """返回透明代理状态。

    平台支持：
    - Windows: WinDivert 后端状态
    - Linux: iptables 后端状态（需 root）
    - macOS: pf 后端状态（需 root）

    跨平台字段对齐：所有平台都返回 running/supported/is_admin/backend/hint，
    前端可统一读取这些字段判断 UI 状态（管理员标签、后端标签、提权按钮可见性等）。
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
            hint = "就绪（运行中）"
        elif not is_admin:
            hint = "需要 root 权限。请用 sudo 启动 Telnix"
        else:
            hint = "就绪"
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
        hint = "就绪（运行中）"
    elif not is_admin:
        hint = "需要管理员权限。请用管理员身份重启 Telnix"
    else:
        hint = "就绪"
    return {
        "running": running,
        "supported": True,
        "is_admin": is_admin,
        "backend": "windivert",
        "hint": hint,
        **proxy.status(),
    }


def lookup_original_dst(sock: socket.socket) -> Optional[tuple[str, int]]:
    """查询透明代理重定向前的原目标地址（跨平台）。

    Windows: 通过 NAT 表反查（client_src_port）- 此接口不适用，请用 lookup_reverse
    Linux: getsockopt(SOL_IP, SO_ORIGINAL_DST)
    macOS: getsockname（pf rdr 把原目标放到 socket 本地地址）

    返回：(orig_dst_ip, orig_dst_port) 或 None（查询失败/不支持）
    """
    if not IS_WINDOWS and _instance is not None:
        # Unix 平台：委托给 UnixTransparentProxy.lookup_original_dst
        return _instance.lookup_original_dst(sock)
    # Windows 平台：此接口不适用（Windows 用 NAT 表反查 client_src_port）
    return None


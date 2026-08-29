"""TCP/UDP raw capture backend (cross-platform).

Independent of the HTTP proxy backend; captures at the network layer, supporting
non-HTTP protocols (Steam P2P, protobuf, etc.).

Platform support:
- Windows: WinDivert kernel driver (requires pydivert + admin privileges)
  On first run, checks whether pydivert is installed; if not, prompts: pip install pydivert
  Also requires WinDivert64.sys (bundled with pydivert)
- Linux: AF_PACKET raw socket (requires root / CAP_NET_RAW; pure stdlib, no external deps)
- macOS: BPF device (requires root; pure stdlib, no external deps)

The actual capture logic for non-Windows platforms is implemented in the
UnixRawCapture class in raw_capture_unix.py. The RawCapture class in this module
is Windows-only, but start()/stop()/raw_capture_status() provide a unified
external interface; cross-platform routing is handled in start_raw_capture().

Captured packets are stored in the flows table with protocol=tcp/udp; the
raw_data field stores base64-encoded raw bytes.
"""

from __future__ import annotations

import base64
import os
import queue
import socket
import struct
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime

from .. import db, logger
from ..ip_region import lookup as _ip_region_lookup

# 平台判断：WinDivert/pydivert 仅 Windows 可用
IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import ctypes
else:
    # 非 Windows 平台：ctypes.windll 不存在，置 None 占位
    ctypes = None  # type: ignore[assignment]


# ---------- 性能优化：进程级 LRU 缓存 ----------
# PID 反查：五元组 → pid，3 秒 TTL（连接建立后端口稳定）
# IP 属地：ip → region，10 分钟 TTL（属地不变）
# 进程名：pid → name，5 秒 TTL（进程退出后 PID 可复用）

_PID_CACHE: OrderedDict = OrderedDict()  # key: tuple → (ts, pid)
_PID_CACHE_TTL = 3.0
_PID_CACHE_MAX = 2000
_PID_CACHE_LOCK = threading.Lock()

_PROC_NAME_CACHE: OrderedDict = OrderedDict()  # key: pid → (ts, name)
_PROC_NAME_CACHE_TTL = 5.0
_PROC_NAME_CACHE_MAX = 500
_PROC_NAME_LOCK = threading.Lock()

_IP_REGION_CACHE: OrderedDict = OrderedDict()  # key: ip → (ts, region)
_IP_REGION_CACHE_TTL = 600.0  # 10 分钟
_IP_REGION_CACHE_MAX = 500
_IP_REGION_LOCK = threading.Lock()

# lookup 锁已移除：_list_tcp_owner_rows / _list_udp_owner_rows 读快照安全，
# 多 worker 可并发读表，仅缓存写入用 _PID_CACHE_LOCK 保护即可

# 本机 IP 集合缓存（避免每个包都做 socket.getaddrinfo DNS 查询）
_LOCAL_IP_CACHE: frozenset[str] | None = None
_LOCAL_IP_CACHE_TS: float = 0.0
_LOCAL_IP_CACHE_TTL = 60.0
_LOCAL_IP_CACHE_LOCK = threading.Lock()

# 抓包队列满时的丢包计数器（模块级，便于跨线程读取统计）
_DROPPED_PACKETS = 0
_DROPPED_PACKETS_LOCK = threading.Lock()

# P1 监控统计（模块级，供 API 层轮询）
# 滑动窗口：最近 N 秒的带宽/pps 数据（环形缓冲区）
_BW_WINDOW_SEC = 30
_BW_WINDOW_SIZE = 30  # 1 秒一个桶
_BW_BUCKETS: list[dict] = []  # [{"ts": float, "pkt_count": int, "bytes": int}, ...]
_BW_BUCKETS_LOCK = threading.Lock()

# 协议分布（每分钟重置）
_PROTO_COUNTS = {"tcp": 0, "udp": 0, "dns": 0, "http": 0, "https": 0}
_PROTO_COUNTS_LOCK = threading.Lock()
_PROTO_COUNTS_SINCE = time.time()

# 活跃连接表（用于连接监控表格）
_ACTIVE_CONNECTIONS: dict[str, dict] = {}  # key: f"{proto}:{local_ip}:{local_port}:{remote_ip}:{remote_port}"
_ACTIVE_CONNECTIONS_LOCK = threading.Lock()
_ACTIVE_CONNECTIONS_TTL = 30.0  # 30 秒无活动则移除

# 增量进程统计（避免每次 get_raw_stats 遍历 3000+ 连接）
# key: proc_name → {"count": int, "pkts": int, "bytes": int}
_PROC_STATS: dict[str, dict] = {}
_PROC_STATS_LOCK = threading.Lock()
_PROC_STATS_MAX_TOP = 10  # 只维护 Top N


def _record_packet(proto: str, size: int, local_ip: str, local_port: int, remote_ip: str, remote_port: int, pid: int | None, proc_name: str | None):
    """Record a packet for statistics (called from enrich loop)."""
    now = time.time()
    proc_key = proc_name or "(unknown)"
    with _PROC_STATS_LOCK:
        if proc_key not in _PROC_STATS:
            _PROC_STATS[proc_key] = {"count": 0, "pkts": 0, "bytes": 0}
        _PROC_STATS[proc_key]["count"] += 1
        _PROC_STATS[proc_key]["pkts"] += 1
        _PROC_STATS[proc_key]["bytes"] += size
    global _DROPPED_PACKETS
    # 更新滑动窗口
    with _BW_BUCKETS_LOCK:
        _BW_BUCKETS.append({
            "ts": now,
            "pkt_count": 1,
            "bytes": size,
            "proto": proto,
        })
        # 保留最近 _BW_WINDOW_SEC 秒的数据
        cutoff = now - _BW_WINDOW_SEC
        while _BW_BUCKETS and _BW_BUCKETS[0]["ts"] < cutoff:
            _BW_BUCKETS.pop(0)
    # 更新协议分布
    with _PROTO_COUNTS_LOCK:
        _PROTO_COUNTS[proto] = _PROTO_COUNTS.get(proto, 0) + 1
        # 每分钟重置
        if now - _PROTO_COUNTS_SINCE > 60:
            for k in _PROTO_COUNTS:
                _PROTO_COUNTS[k] = 0
    # 更新活跃连接
    conn_key = f"{proto}:{local_ip}:{local_port}:{remote_ip}:{remote_port}"
    with _ACTIVE_CONNECTIONS_LOCK:
        _ACTIVE_CONNECTIONS[conn_key] = {
            "proto": proto,
            "local_ip": local_ip,
            "local_port": local_port,
            "remote_ip": remote_ip,
            "remote_port": remote_port,
            "pid": pid,
            "proc_name": proc_name or "",
            "last_seen": now,
            "pkt_count": 1,
            "bytes": size,
        }


def _touch_connection(proto: str, local_ip: str, local_port: int, remote_ip: str, remote_port: int, pid: int | None, proc_name: str | None, size: int):
    """Touch an existing connection to update its last_seen time."""
    conn_key = f"{proto}:{local_ip}:{local_port}:{remote_ip}:{remote_port}"
    proc_key = proc_name or "(unknown)"
    with _PROC_STATS_LOCK:
        if proc_key not in _PROC_STATS:
            _PROC_STATS[proc_key] = {"count": 0, "pkts": 0, "bytes": 0}
        _PROC_STATS[proc_key]["count"] += 1
        _PROC_STATS[proc_key]["pkts"] += 1
        _PROC_STATS[proc_key]["bytes"] += size
    with _ACTIVE_CONNECTIONS_LOCK:
        if conn_key in _ACTIVE_CONNECTIONS:
            _ACTIVE_CONNECTIONS[conn_key]["last_seen"] = time.time()
            _ACTIVE_CONNECTIONS[conn_key]["pkt_count"] += 1
            _ACTIVE_CONNECTIONS[conn_key]["bytes"] += size


def _get_raw_stats() -> dict:
    """Get current capture statistics."""
    global _DROPPED_PACKETS
    now = time.time()
    # 滑动窗口统计
    with _BW_BUCKETS_LOCK:
        # 计算最近窗口内的 pps 和带宽
        cutoff = now - _BW_WINDOW_SEC
        recent = [b for b in _BW_BUCKETS if b["ts"] >= cutoff]
        total_pkts = sum(b["pkt_count"] for b in recent)
        total_bytes = sum(b["bytes"] for b in recent)
        pps = total_pkts / _BW_WINDOW_SEC if _BW_WINDOW_SEC > 0 else 0
        bps = total_bytes * 8 / _BW_WINDOW_SEC if _BW_WINDOW_SEC > 0 else 0  # bits per second
        mbps = bps / 1_000_000
        # 趋势数据（每秒一个点）
        trend = []
        for i in range(_BW_WINDOW_SEC):
            t = now - i
            bucket_pkts = sum(b["pkt_count"] for b in recent if t - 1 <= b["ts"] < t + 0.001)
            bucket_bytes = sum(b["bytes"] for b in recent if t - 1 <= b["ts"] < t + 0.001)
            trend.insert(0, {
                "ts": int(t),
                "pps": bucket_pkts,
                "mbps": round(bucket_bytes * 8 / 1_000_000, 4),
            })
        trend = trend[-20:]  # 只返回最近 20 秒用于图表显示
    # 丢包统计
    with _DROPPED_PACKETS_LOCK:
        dropped = _DROPPED_PACKETS
    # 协议分布
    with _PROTO_COUNTS_LOCK:
        proto_counts = dict(_PROTO_COUNTS)
    # 活跃连接
    with _ACTIVE_CONNECTIONS_LOCK:
        # 清理过期连接
        expire_cutoff = now - _ACTIVE_CONNECTIONS_TTL
        expired_keys = [k for k, v in _ACTIVE_CONNECTIONS.items() if v["last_seen"] < expire_cutoff]
        for k in expired_keys:
            _ACTIVE_CONNECTIONS.pop(k, None)
        # 构建连接列表
        connections = list(_ACTIVE_CONNECTIONS.values())
        # Top 5 进程排序（使用增量统计，O(1)）
        with _PROC_STATS_LOCK:
            top_processes = sorted(_PROC_STATS.items(), key=lambda x: x[1]["pkts"], reverse=True)[:5]
        top_process_list = [{"name": name, **stats} for name, stats in top_processes]
    return {
        "pps": round(pps, 2),
        "mbps": round(mbps, 4),
        "total_packets": total_pkts,
        "total_bytes": total_bytes,
        "dropped_packets": dropped,
        "proto_distribution": proto_counts,
        "tcp_count": proto_counts.get("tcp", 0),
        "udp_count": proto_counts.get("udp", 0),
        "dns_count": proto_counts.get("dns", 0),
        "http_count": proto_counts.get("http", 0),
        "https_count": proto_counts.get("https", 0),
        "trend": trend,
        "active_connections": connections,
        "top_processes": top_process_list,
        "timestamp": now,
    }


def _reset_raw_stats():
    """Reset all statistics counters."""
    global _DROPPED_PACKETS, _PROTO_COUNTS, _PROTO_COUNTS_SINCE
    with _BW_BUCKETS_LOCK:
        _BW_BUCKETS.clear()
    with _DROPPED_PACKETS_LOCK:
        _DROPPED_PACKETS = 0
    with _PROTO_COUNTS_LOCK:
        for k in _PROTO_COUNTS:
            _PROTO_COUNTS[k] = 0
        _PROTO_COUNTS_SINCE = time.time()
    with _ACTIVE_CONNECTIONS_LOCK:
        _ACTIVE_CONNECTIONS.clear()


# 为了兼容旧代码的导入方式，保留 _RAW_STATS
_RAW_STATS = {}

# 忽略规则缓存（与 server.py 保持同步，5 秒刷新）
_IGNORED_PIDS: set[int] = set()
_IGNORED_NAMES: set[str] = set()
_IGNORED_HOST_REGEXES: list = []
_IGNORED_LOCK = threading.Lock()
_IGNORED_LAST_REFRESH = 0.0
_IGNORED_REFRESH_INTERVAL = 5.0


def _refresh_ignored():
    """Refresh ignore rules from DB (cached, 5s TTL)."""
    global _IGNORED_PIDS, _IGNORED_NAMES, _IGNORED_HOST_REGEXES, _IGNORED_LAST_REFRESH
    now = time.time()
    if now - _IGNORED_LAST_REFRESH < _IGNORED_REFRESH_INTERVAL:
        return
    _IGNORED_LAST_REFRESH = now
    rows = db.get_ignored_processes()
    pids = {r["pid"] for r in rows if r.get("pid") and r["pid"] > 0}
    names = {r["process_name"].lower()
             for r in rows
             if (not r.get("pid") or r["pid"] <= 0) and r.get("process_name")}
    host_rows = db.get_ignored_hosts()
    hosts = [r["host_pattern"] for r in host_rows if r.get("host_pattern")]
    # 预编译通配符正则
    import re as _re
    host_rx = []
    for p in hosts:
        try:
            escaped = p.replace(".", r"\.").replace("*", ".*").replace("?", ".")
            host_rx.append(_re.compile(escaped, _re.IGNORECASE))
        except Exception:  # noqa: BLE001
            host_rx.append(None)
    with _IGNORED_LOCK:
        _IGNORED_PIDS = pids
        _IGNORED_NAMES = names
        _IGNORED_HOST_REGEXES = host_rx


def _is_ignored(pid: int | None, proc_name: str | None, host: str | None) -> bool:
    """Check if flow should be ignored per user rules (pid / process name / host wildcard)."""
    _refresh_ignored()
    with _IGNORED_LOCK:
        pid_set = _IGNORED_PIDS
        name_set = _IGNORED_NAMES
        host_rx = _IGNORED_HOST_REGEXES
    if pid is not None and pid > 0 and pid in pid_set:
        return True
    if proc_name and proc_name.lower() in name_set:
        return True
    if host and host_rx:
        for rx in host_rx:
            if rx is not None and rx.search(host):
                return True
    return False


def _extract_path(url: str, host: str | None, scheme: str | None) -> str:
    """Robustly extract the path (including query/fragment) from a full URL.

    The old implementation used ``url.split(host, 1)[-1]``, which would mis-truncate
    when the host string reappears in the path (e.g. host is 'api' and path
    contains '/api/v1/api'). Here we trim by the exact ``scheme://host`` prefix.
    """
    if not url:
        return ""
    prefix = ""
    if scheme:
        prefix += f"{scheme}://"
    if host:
        prefix += host
    if prefix and url.startswith(prefix):
        rest = url[len(prefix):]
        return rest if rest.startswith("/") else "/" + rest
    # 无法按前缀拆解时，退化为取最后一个 '/' 之后部分
    return url.rsplit("/", 1)[-1]


class RawCapture:
    """WinDivert capture backend."""

    def __init__(self, session_id: int):
        self.session_id = session_id
        self._running = False
        self._thread: threading.Thread | None = None
        self._divert = None
        # 最近一次启动失败的错误详情（前端展示用）
        self._last_error: str = ""
        # 默认 filter：最简形式，仅抓 tcp/udp，端口排除在代码层做
        # 注意：filter 中 tcp.SrcPort != X 在 UDP 包上会让整个 filter 评估为 false，
        # 导致 UDP 包被丢弃。loopback 和端口排除都改在代码层判断。
        self._filter = "tcp or udp"
        # Telnix 自身端口，代码层排除避免回环噪音
        self._self_ports = {8888, 18901}
        self._pid_filter: set[int] | None = None  # None=不过滤
        self._port_filter: set[int] | None = None
        # 性能优化：抓包线程只解包入队，PID/进程名/IP属地/DNS解析在 worker 线程池异步完成
        # 队列满时丢包保平安（避免反压导致抓包线程卡顿）
        self._enrich_queue: queue.Queue = queue.Queue(maxsize=20000)
        self._enrich_threads: list[threading.Thread] = []
        # worker 数自适应 CPU 核数（4→8）：高流量（2000+ pps）时 4 个易成瓶颈
        self._enrich_worker_count = min(8, os.cpu_count() or 4)

    def set_pid_filter(self, pids: set[int] | None):
        self._pid_filter = pids

    def set_port_filter(self, ports: set[int] | None):
        self._port_filter = ports

    def set_filter(self, filter_str: str):
        """Set the WinDivert filter string. Empty string restores the default filter."""
        if filter_str:
            self._filter = filter_str
        else:
            # 恢复默认（最简 filter，端口排除在代码层）
            self._filter = "tcp or udp"

    def start(self) -> bool:
        """Start capture (Windows-only). Returns True on success, False on failure (driver not installed, etc.).

        Note: non-Windows platforms should not call this method. start_raw_capture()
        automatically routes to UnixRawCapture (raw_capture_unix.py); the caller
        does not need to care about the platform.
        """
        if not IS_WINDOWS:
            # 防御性检查：非 Windows 平台不应走到这里（start_raw_capture 会路由到 UnixRawCapture）
            logger.error("raw", "RawCapture.start() called on non-Windows platform",
                         "Should be routed to UnixRawCapture by start_raw_capture(), check the caller")
            self._last_error = "Internal error: Windows backend called on a non-Windows platform"
            return False
        try:
            import pydivert  # type: ignore  # noqa: F401
        except ImportError:
            logger.error("raw", "pydivert not installed",
                         "Please run: pip install pydivert")
            return False
        if self._running:
            return True
        try:
            import pydivert  # type: ignore
            # 检查管理员权限
            if not self._is_admin():
                logger.error("raw", "WinDivert requires admin privileges",
                             "Please run Telnix as administrator")
                return False
            # 使用 SNIFF 模式：只嗅探不拦截，包会正常流转不会断网
            # WINDIVERT_FLAG_SNIFF = 1
            # F24 历史：曾尝试设 priority=-100 让 SNIFF handle 先于 transparent_proxy(priority=0)
            # 看到改写前的原始包（dst_port=80/443），但实测 priority=-100 会导致 WinDivert
            # 抓不到任何包（驱动层段锁定级联失败）。已回退到 priority=0（默认）。
            # F28 兼容方案：raw_capture 与 transparent_proxy 都用 priority=0，无论 raw_capture
            # 先看到改写前的包（dst=80/443）还是改写后的包（dst=8888），都能正确解析：
            # - 改写前（dst=80，出站）：走 HTTP 解析
            # - 改写前（dst=443，出站）：走 TLS SNI 解析
            # - 改写后（dst=8888，HTTP，出站）：走 HTTP 解析
            # - 改写后（dst=8888，TLS，出站）：先检查 payload[0]==0x16 走 TLS SNI，否则 HTTP
            # _self_ports 排除逻辑（F28）：排除 src_port in _self_ports（即 src_port=8888 代理回包、
            # src_port=18901 API 响应回包）和 dst_port=18901（API 请求），
            # 不排除 dst_port=8888（改写后的入站包），确保重定向流量能被抓到。
            # Platform: Windows
            self._divert = pydivert.WinDivert(self._filter, flags=1)
            self._divert.open()
            self._running = True
            self._thread = threading.Thread(target=self._capture_loop, daemon=True)
            self._thread.start()
            # 启动 enrich worker 线程池（PID/进程名/IP属地/DNS 解析异步化）
            for i in range(self._enrich_worker_count):
                t = threading.Thread(target=self._enrich_loop, daemon=True, name=f"raw-enrich-{i}")
                t.start()
                self._enrich_threads.append(t)
            logger.info("raw", "TCP/UDP capture started", f"filter={self._filter}, workers={self._enrich_worker_count}")
            return True
        except Exception as e:  # noqa: BLE001
            err_msg = str(e)
            logger.error("raw", "WinDivert start failed", err_msg)
            # 把异常详情存到实例上，让 start_raw_capture 能带回到前端
            self._last_error = err_msg
            # 检测常见失败原因，给前端更友好的提示
            low = err_msg.lower()
            if "找不到" in err_msg or "not found" in low or "找不到指定的模块" in err_msg:
                logger.error("raw", "WinDivert driver file missing",
                             "Ensure WinDivert64.sys is in the same directory as python.exe, or pydivert is properly installed")
            elif "access is denied" in low or "拒绝访问" in err_msg or "权限" in err_msg:
                logger.error("raw", "Insufficient permissions",
                             "Please run Telnix as administrator")
            elif "签名" in err_msg or "sign" in low or "数字签名" in err_msg or "加载失败" in err_msg:
                # 杀软拦截通常表现为驱动加载失败 / 签名问题
                logger.error("raw", "WinDivert driver load blocked",
                             "Antivirus (360/Huorong/Windows Defender) may be blocking. Add Telnix directory and WinDivert64.sys to antivirus whitelist and retry")
            self._divert = None
            return False

    def stop(self):
        """Stop capture.

        First sets _running=False, then uses shutdown to unblock recv, then closes.
        Avoids directly closing which would leave worker threads blocked forever
        in recv.
        Platform: Windows
        """
        global _DROPPED_PACKETS
        self._running = False
        if self._divert:
            # F14 修复：对齐 transparent_proxy.py / dns_hijack.py 的 stop 模式。
            # 先 shutdown 让 recv() 立即返回 None 或抛异常。
            # 注意：pydivert Python 层未暴露 shutdown（DLL 层有 WinDivertShutdown），
            # 此调用会抛 AttributeError 被 try/except 吞掉（no-op，保留文档意图，
            # 未来 pydivert 若暴露则自动生效）。
            try:
                self._divert.shutdown()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            # 等待工作线程退出（recv 应已解除阻塞或在 close 后解除）
            if self._thread:
                self._thread.join(timeout=3)
                if self._thread.is_alive():
                    logger.warning("raw", "Worker thread still running on stop",
                                   "possible blocking recv")
                self._thread = None
            # 最后关闭句柄（join 之后，避免 close-during-blocking-recv 未定义行为）
            try:
                self._divert.close()
            except Exception:  # noqa: BLE001
                pass
            self._divert = None
        # 清空队列唤醒 worker，等待退出
        for _ in range(len(self._enrich_threads)):
            try:
                self._enrich_queue.put_nowait(None)
            except queue.Full:
                pass
        for t in self._enrich_threads:
            t.join(timeout=2)
        self._enrich_threads.clear()
        # 输出最终丢包统计
        with _DROPPED_PACKETS_LOCK:
            total_dropped = _DROPPED_PACKETS
            _DROPPED_PACKETS = 0
        if total_dropped > 0:
            logger.warning("raw", "Capture stopped, final drop stats", f"total dropped: {total_dropped}")
        logger.info("raw", "TCP/UDP capture stopped")

    @property
    def running(self) -> bool:
        return self._running

    def _is_admin(self) -> bool:
        """Check for admin privileges.

        Windows: uses ctypes.windll.shell32.IsUserAnAdmin().
        Non-Windows: uses os.geteuid() == 0 to check for root (unified POSIX interface).
        """
        if not IS_WINDOWS:
            # POSIX 平台：euid == 0 即 root
            try:
                return os.geteuid() == 0
            except AttributeError:  # noqa: BLE001
                return False
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            return False

    def _capture_loop(self):
        """Capture main loop (lightweight: only unpack + basic filter + enqueue; no slow ops like PID/process name/IP region).

        Performance optimization: the capture thread does minimal work (unpack +
        loopback/port filter + build raw dict + enqueue). Slow operations like PID
        reverse-lookup, process name query, IP region lookup, and DNS parsing are
        all moved to _enrich_loop worker threads. This boosts capture-thread
        throughput 5-10x, avoiding packet drops at 1000 pps due to full-table PID
        scan blocking.
        """
        global _DROPPED_PACKETS
        while self._running:
            try:
                packet = self._divert.recv()
                if packet is None:
                    continue
                # pydivert 2.x API: packet.ipv4 / packet.ipv6 / packet.tcp / packet.udp
                ip_hdr = packet.ipv4 or packet.ipv6
                if ip_hdr is None:
                    continue
                src_ip = ip_hdr.src_addr
                dst_ip = ip_hdr.dst_addr
                protocol = getattr(ip_hdr, "protocol", getattr(ip_hdr, "next_hdr", None))  # Platform: Windows; IPv4=protocol, IPv6=next_hdr

                # 代码层排除 loopback（filter 层无法可靠排除 IPv6 loopback）
                # 透明代理会把重定向包改写为 127.0.0.2↔127.0.0.2（127.0.0.0/8 整个段），
                # 这类纯本机 loopback 流量已由 server.py 处理，若在此被当作真实流量解析
                # 会因方向误判而生成 tcp://127.0.0.2:8888 之类的怪异条目，故整段跳过。
                if src_ip == "::1" or src_ip.startswith("127.") or dst_ip == "::1" or dst_ip.startswith("127."):
                    continue

                if protocol == 6:  # TCP
                    tcp_hdr = packet.tcp
                    if tcp_hdr is None:
                        continue
                    src_port = tcp_hdr.src_port
                    dst_port = tcp_hdr.dst_port
                    proto_name = "tcp"
                elif protocol == 17:  # UDP
                    udp_hdr = packet.udp
                    if udp_hdr is None:
                        continue
                    src_port = udp_hdr.src_port
                    dst_port = udp_hdr.dst_port
                    proto_name = "udp"
                else:
                    continue

                # 代码层排除 Telnix 自身端口（避免回环噪音）
                # F28: 只排除 src_port（代理回包）和 API 端口 18901，
                # 不排除 dst_port=8888（透明代理改写后的入站包，payload 仍是原始 HTTP/TLS）。
                # 这样无论 raw_capture 看到改写前(dst=80/443)还是改写后(dst=8888)的包，都能抓到。
                if src_port in self._self_ports or dst_port == 18901:
                    continue

                # 端口过滤
                if self._port_filter:
                    if src_port not in self._port_filter and dst_port not in self._port_filter:
                        continue

                payload = packet.payload or b""

                # 判断方向：本机发出的包（src 是本机）算请求，否则算响应
                is_outbound = self._is_local_ip(src_ip)
                remote_ip = dst_ip if is_outbound else src_ip
                remote_port = dst_port if is_outbound else src_port
                local_port = src_port if is_outbound else dst_port

                # F27 诊断：统计 80/443/8888 包数量
                # P1-1 修复：降频至每 5000 包打印一次（原每 50 包在捕获热路径同步
                # 构造大 f-string 阻塞 recv，高流量下造成丢包）。每 5000 包一次影响可忽略。
                if dst_port in (80, 443, 8888) or src_port in (80, 443, 8888):
                    self._http_pkt_count = getattr(self, '_http_pkt_count', 0) + 1
                    _cnt = self._http_pkt_count
                    if _cnt == 1 or _cnt % 5000 == 0:
                        logger.info(
                            "raw", "HTTP/HTTPS packet stats",
                            f"count={_cnt} "
                            f"src={src_ip}:{src_port} dst={dst_ip}:{dst_port} "
                            f"outbound={is_outbound} payload_len={len(payload)}"
                        )

                # 构造 raw dict（不含 pid/proc_name/ip_region），入队交给 enrich worker
                raw = {
                    "proto_name": proto_name,
                    "src_ip": src_ip, "dst_ip": dst_ip,
                    "src_port": src_port, "dst_port": dst_port,
                    "payload": payload,
                    "is_outbound": is_outbound,
                    "remote_ip": remote_ip, "remote_port": remote_port,
                    "local_port": local_port,
                    "timestamp": datetime.now().isoformat(),
                }
                try:
                    self._enrich_queue.put_nowait(raw)
                except queue.Full:
                    # 队列满丢包保平安，避免反压阻塞抓包线程；累计计数并周期性告警
                    with _DROPPED_PACKETS_LOCK:
                        _DROPPED_PACKETS += 1
                        if _DROPPED_PACKETS % 100 == 0:
                            logger.warning("raw", "Capture queue full, dropping packets", f"total dropped: {_DROPPED_PACKETS}")

                # SNIFF 模式无需 send，包已正常流转

            except Exception as e:  # noqa: BLE001
                if self._running:
                    logger.error("raw", "Capture loop exception", str(e))
                    time.sleep(0.1)

    def _enrich_loop(self):
        """enrich worker: dequeue raw dict, fill in PID/process name/IP region/DNS, write to DB."""
        from .process_lookup import _list_tcp_owner_rows, _list_udp_owner_rows
        while self._running:
            try:
                raw = self._enrich_queue.get(timeout=1)
            except queue.Empty:
                continue
            if raw is None:  # 停止信号
                break
            try:
                self._enrich_one(raw, _list_tcp_owner_rows, _list_udp_owner_rows)
            except Exception as e:  # noqa: BLE001
                if self._running:
                    logger.error("raw", "enrich exception", str(e))

    def _enrich_one(self, raw: dict, list_tcp_fn, list_udp_fn):
        """Process a single raw dict: PID reverse-lookup -> process name -> PID filter -> DNS parse -> IP region -> write to DB."""
        proto_name = raw["proto_name"]
        src_ip, dst_ip = raw["src_ip"], raw["dst_ip"]
        src_port, dst_port = raw["src_port"], raw["dst_port"]
        payload = raw["payload"]
        is_outbound = raw["is_outbound"]
        remote_ip = raw["remote_ip"]
        remote_port = raw["remote_port"]
        local_port = raw["local_port"]
        timestamp = raw["timestamp"]

        # PID 反查（带 LRU 缓存）
        if proto_name == "tcp":
            pid = self._lookup_pid_cached(src_ip, src_port, dst_ip, dst_port, list_tcp_fn, is_udp=False)
        else:
            pid = self._lookup_pid_cached(src_ip, src_port, dst_ip, dst_port, list_udp_fn, is_udp=True)

        # PID 过滤
        if self._pid_filter and pid not in self._pid_filter:
            return

        proc_name = ""
        if pid:
            proc_name = self._proc_name_cached(pid)

        # DNS 解析：UDP 端口 53 的包尝试解析为 DNS
        dns_info = None
        if proto_name == "udp" and (src_port == 53 or dst_port == 53) and payload:
            try:
                from .dns_parser import parse_dns
                dns_info = parse_dns(payload)
            except Exception:  # noqa: BLE001
                dns_info = None

        if dns_info:
            q = dns_info.get("questions") or [{}]
            qname = q[0].get("qname", "") if q else ""
            qtype = q[0].get("qtype_name", "") if q else ""
            if dns_info.get("is_response"):
                answers = dns_info.get("answers") or []
                a_values = []
                for a in answers[:5]:
                    if a.get("type") in (1, 28):
                        a_values.append(a.get("rdata", ""))
                ans_str = ", ".join(a_values) if a_values else dns_info.get("rcode_name", "")
                path = f"DNS {qname} {qtype} -> {ans_str}"
                method = "DNS-RESP"
            else:
                path = f"DNS {qname} {qtype}"
                method = "DNS-QUERY"
            host = qname or remote_ip
            dns_summary = self._dns_summary(dns_info)
            flow = {
                "session_id": self.session_id,
                "timestamp": timestamp,
                "pid": pid,
                "process_name": proc_name,
                "method": method,
                "url": f"udp://{remote_ip}:{remote_port}",
                "scheme": "dns",
                "host": host,
                "path": path,
                "request_headers": "{}",
                "request_body": dns_summary if not dns_info.get("is_response") else "",
                "status_code": 0 if dns_info.get("rcode") == 0 else (dns_info.get("rcode") or None),
                "response_headers": "{}",
                "response_body": dns_summary if dns_info.get("is_response") else "",
                "duration_ms": 0,
                "size": len(payload),
                "protocol": "dns",
                "raw_data": "base64:" + base64.b64encode(payload).decode("ascii") if payload else None,
                "src_port": src_port,
                "dst_port": dst_port,
                "remote_ip": remote_ip,
                "ip_region": self._ip_region_cached(remote_ip) if remote_ip else "",
            }
        elif proto_name == "tcp" and payload and (dst_port in (80, 443, 8888) or src_port in (80, 443, 8888)):
            # F21+F22+F23: HTTP/TLS/WS 载荷解析
            # - 80 端口: HTTP 请求/响应、WebSocket 升级请求/响应
            # - 443 端口: TLS ClientHello SNI 提取（仅出站首包）
            # - 8888 端口: 透明代理改写后的入站包（payload 仍是原始 HTTP/TLS，F28）
            parsed = None  # (method, url, host, status_code, body_text, protocol, scheme)
            try:
                if dst_port in (80, 8888) or src_port in (80, 8888):
                    # F21: HTTP 解析（8888 端口可能是透明代理改写后的 HTTP 包）
                    # F28: 8888 端口先检查是否是 TLS ClientHello（原始 443 改写后）
                    if dst_port == 8888 and is_outbound and len(payload) >= 5 and payload[0] == 0x16:
                        # F22: TLS ClientHello SNI 解析（443 改写为 8888）
                        sni = self._parse_tls_sni(payload)
                        if sni:
                            parsed = ("TLS", f"https://{sni}/", sni, None,
                                      f"TLS ClientHello SNI={sni}", "https", "https")
                    else:
                        http_info = self._parse_http_payload(payload, is_outbound)
                        if http_info:
                            method, url, host, status_code, body_text = http_info
                            # F23: WebSocket 升级检测
                            if method in ("GET",) and is_outbound:
                                ws_check = self._parse_ws_upgrade(payload)
                                if ws_check:
                                    ws_key, ws_version = ws_check
                                    parsed = (method, url, host, None,
                                              f"WebSocket Upgrade: key={ws_key} v={ws_version}",
                                              "websocket", "ws")
                            elif not is_outbound and status_code == 101:
                                parsed = ("RESP", url, host, 101,
                                          "WebSocket Upgrade Ack", "websocket", "ws")
                            else:
                                parsed = (method, url, host, status_code, body_text, "http", "http")
                elif dst_port == 443 and is_outbound:
                    # F22: TLS ClientHello SNI 解析
                    sni = self._parse_tls_sni(payload)
                    if sni:
                        parsed = ("TLS", f"https://{sni}/", sni, None,
                                  f"TLS ClientHello SNI={sni}", "https", "https")
            except Exception:  # noqa: BLE001
                parsed = None
            if parsed:
                method, url, host, status_code, body_text, protocol, scheme = parsed
                flow = {
                    "session_id": self.session_id,
                    "timestamp": timestamp,
                    "pid": pid,
                    "process_name": proc_name,
                    "method": method,
                    "url": url,
                    "scheme": scheme,
                    "host": host or remote_ip,
                    "path": _extract_path(url, host, scheme),
                    "request_headers": "{}",
                    "request_body": body_text if is_outbound else "",
                    "status_code": status_code,
                    "response_headers": "{}",
                    "response_body": body_text if not is_outbound else "",
                    "duration_ms": 0,
                    "size": len(payload),
                    "protocol": protocol,
                    "raw_data": "base64:" + base64.b64encode(payload).decode("ascii") if payload else None,
                    "src_port": src_port,
                    "dst_port": dst_port,
                    "remote_ip": remote_ip,
                    "ip_region": self._ip_region_cached(remote_ip) if remote_ip else "",
                }
            else:
                flow = {
                    "session_id": self.session_id,
                    "timestamp": timestamp,
                    "pid": pid,
                    "process_name": proc_name,
                    "method": "SEND" if is_outbound else "RECV",
                    "url": f"{proto_name}://{remote_ip}:{remote_port}",
                    "scheme": proto_name,
                    "host": remote_ip,
                    "path": f"/{proto_name}/{local_port}->{remote_port}",
                    "request_headers": "{}",
                    "request_body": "base64:" + base64.b64encode(payload).decode("ascii") if payload and is_outbound else "",
                    "status_code": None,
                    "response_headers": "{}",
                    "response_body": "base64:" + base64.b64encode(payload).decode("ascii") if payload and not is_outbound else "",
                    "duration_ms": 0,
                    "size": len(payload),
                    "protocol": proto_name,
                    "raw_data": "base64:" + base64.b64encode(payload).decode("ascii") if payload else None,
                    "src_port": src_port,
                    "dst_port": dst_port,
                    "remote_ip": remote_ip,
                    "ip_region": self._ip_region_cached(remote_ip) if remote_ip else "",
                }
        else:
            flow = {
                "session_id": self.session_id,
                "timestamp": timestamp,
                "pid": pid,
                "process_name": proc_name,
                "method": "SEND" if is_outbound else "RECV",
                "url": f"{proto_name}://{remote_ip}:{remote_port}",
                "scheme": proto_name,
                "host": remote_ip,
                "path": f"/{proto_name}/{local_port}->{remote_port}",
                "request_headers": "{}",
                "request_body": "base64:" + base64.b64encode(payload).decode("ascii") if payload and is_outbound else "",
                "status_code": None,
                "response_headers": "{}",
                "response_body": "base64:" + base64.b64encode(payload).decode("ascii") if payload and not is_outbound else "",
                "duration_ms": 0,
                "size": len(payload),
                "protocol": proto_name,
                "raw_data": "base64:" + base64.b64encode(payload).decode("ascii") if payload else None,
                "src_port": src_port,
                "dst_port": dst_port,
                "remote_ip": remote_ip,
                "ip_region": self._ip_region_cached(remote_ip) if remote_ip else "",
            }
        # 忽略检查（与 ProxyServer 逻辑一致）：DNS 包 host=qname（真实域名），HTTP 包 host 已是域名，直接用
        # 纯二进制 TCP/UDP 包 host=remote_ip（无域名），此时仅靠 PID/进程名过滤
        _flow_host = flow.get("host", "")
        if _is_ignored(pid, proc_name, _flow_host):
            return
        # P1 监控统计：记录包信息用于实时监控
        _record_packet(
            proto=flow["protocol"],
            size=len(payload),
            local_ip=src_ip,
            local_port=src_port,
            remote_ip=remote_ip,
            remote_port=remote_port,
            pid=pid,
            proc_name=proc_name,
        )
        db.insert_flow_async(flow)

    @staticmethod
    def _parse_http_payload(payload: bytes, is_outbound: bool):
        """Parse HTTP request/response payload. Returns (method, url, host, status_code, body_text) or None.

        F21: enables raw_capture to recognize HTTP traffic on port 80, stored as protocol="http".
        """
        if not payload:
            return None
        # 尝试解码（HTTP 是文本协议）
        try:
            text = payload.decode('latin-1', errors='replace')
        except Exception:  # noqa: BLE001
            return None
        # 按 CRLF 分割首行
        parts = text.split('\r\n', 1)
        first_line = parts[0]
        rest = parts[1] if len(parts) > 1 else ''
        if is_outbound:
            # HTTP 请求：GET /path HTTP/1.1
            # 首字符是大写字母 A-Z
            if not first_line or not ('A' <= first_line[0] <= 'Z'):
                return None
            tokens = first_line.split(' ')
            if len(tokens) < 3:
                return None
            method = tokens[0]
            path = tokens[1]
            # 从 Host 头提取 host
            host = ''
            for line in rest.split('\r\n'):
                if line.lower().startswith('host:'):
                    host = line[5:].strip()
                    break
            url = f"http://{host}{path}" if host else f"http://{path}"
            # 提取 body（\r\n\r\n 之后）
            body = rest.split('\r\n\r\n', 1)[-1] if '\r\n\r\n' in rest else ''
            body_text = body[:2000] if body else ''
            return (method, url, host, None, body_text)
        else:
            # HTTP 响应：HTTP/1.1 200 OK
            if not first_line.startswith('HTTP/'):
                return None
            tokens = first_line.split(' ', 2)
            status_code = int(tokens[1]) if len(tokens) >= 2 and tokens[1].isdigit() else None
            # 提取 body
            body = rest.split('\r\n\r\n', 1)[-1] if '\r\n\r\n' in rest else ''
            body_text = body[:2000] if body else ''
            return ('RESP', '', '', status_code, body_text)

    @staticmethod
    def _parse_ws_upgrade(payload: bytes):
        """Detect WebSocket upgrade request. Returns (sec_websocket_key, sec_websocket_version) or None.

        F23: a WebSocket upgrade request is an HTTP GET + Upgrade: websocket header.
        """
        try:
            text = payload.decode('latin-1', errors='replace')
        except Exception:  # noqa: BLE001
            return None
        if not text.startswith('GET '):
            return None
        headers_lower = {}
        for line in text.split('\r\n')[1:]:
            if ':' in line:
                k, _, v = line.partition(':')
                headers_lower[k.strip().lower()] = v.strip()
        if headers_lower.get('upgrade', '').lower() != 'websocket':
            return None
        return (headers_lower.get('sec-websocket-key', ''),
                headers_lower.get('sec-websocket-version', ''))

    @staticmethod
    def _parse_tls_sni(payload: bytes):
        """Extract SNI hostname from TLS ClientHello. Returns hostname or None.

        F22: enables port 443 traffic to be stored as protocol="https", showing the target domain.
        """
        if len(payload) < 5 or payload[0] != 0x16:  # 0x16 = TLS Handshake
            return None
        pos = 5  # 跳过 TLS record header (5 bytes)
        if len(payload) < pos + 4 or payload[pos] != 0x01:  # 0x01 = ClientHello
            return None
        pos += 4  # handshake type + length (4 bytes)
        pos += 2  # version (2 bytes)
        pos += 32  # random (32 bytes)
        # Session ID
        if len(payload) < pos + 1:
            return None
        sid_len = payload[pos]
        pos += 1 + sid_len
        # Cipher Suites
        if len(payload) < pos + 2:
            return None
        cs_len = int.from_bytes(payload[pos:pos + 2], 'big')
        pos += 2 + cs_len
        # Compression Methods
        if len(payload) < pos + 1:
            return None
        cm_len = payload[pos]
        pos += 1 + cm_len
        # Extensions
        if len(payload) < pos + 2:
            return None
        ext_total = int.from_bytes(payload[pos:pos + 2], 'big')
        pos += 2
        ext_end = pos + ext_total
        while pos + 4 <= ext_end and pos + 4 <= len(payload):
            ext_type = int.from_bytes(payload[pos:pos + 2], 'big')
            ext_len = int.from_bytes(payload[pos + 2:pos + 4], 'big')
            pos += 4
            if ext_type == 0x0000:  # SNI extension
                if pos + 2 > len(payload):
                    return None
                p = pos + 2  # SNI list length
                if p + 3 > len(payload):
                    return None
                sni_type = payload[p]
                sni_len = int.from_bytes(payload[p + 1:p + 3], 'big')
                if sni_type == 0:  # host_name
                    p += 3
                    if p + sni_len > len(payload):
                        return None
                    return payload[p:p + sni_len].decode('ascii', errors='replace')
            pos += ext_len
        return None

    def _lookup_pid_cached(self, src_ip, src_port, dst_ip, dst_port, list_rows_fn, is_udp: bool = False) -> int | None:
        """PID reverse-lookup with LRU cache.

        Performance optimization:
        - TCP: 5-tuple -> pid, 3-second TTL (subsequent packets of the same connection hit the cache)
        - UDP: (local_ip, local_port) -> pid, 3-second TTL
        - lookup lock prevents concurrent full-table scans
        """
        if is_udp:
            src_is_local = self._is_local_ip(src_ip)
            local_ip = src_ip if src_is_local else dst_ip
            local_port = src_port if src_is_local else dst_port
            cache_key = ("u", local_ip, local_port)
        else:
            cache_key = ("t", src_ip, src_port, dst_ip, dst_port)

        now = time.time()
        # 1. 查缓存（持锁一次即可，双检锁在已持锁时无意义）
        with _PID_CACHE_LOCK:
            item = _PID_CACHE.get(cache_key)
            if item is not None:
                ts, pid = item
                if now - ts < _PID_CACHE_TTL:
                    _PID_CACHE.move_to_end(cache_key)
                    return pid
                _PID_CACHE.pop(cache_key, None)

        # 2. 真正查表（表是只读快照，并发安全）
        pid = None
        try:
            if is_udp:
                for (lip, lport, p) in list_rows_fn():
                    if lport == local_port and (lip == local_ip or lip == "0.0.0.0"):
                        pid = p
                        break
            else:
                for (lip, lport, rip, rport, p, _state) in list_rows_fn():
                    if lip == src_ip and lport == src_port and rip == dst_ip and rport == dst_port:
                        pid = p
                        break
                    if lip == dst_ip and lport == dst_port and rip == src_ip and rport == src_port:
                        pid = p
                        break
        except Exception:  # noqa: BLE001
            pid = None

        # 4. 写缓存
        with _PID_CACHE_LOCK:
            _PID_CACHE[cache_key] = (now, pid)
            _PID_CACHE.move_to_end(cache_key)
            if len(_PID_CACHE) > _PID_CACHE_MAX:
                _PID_CACHE.popitem(last=False)

        return pid

    def _proc_name_cached(self, pid: int) -> str:
        """Process name query with LRU cache, 5-second TTL."""
        now = time.time()
        with _PROC_NAME_LOCK:
            item = _PROC_NAME_CACHE.get(pid)
            if item is not None:
                ts, name = item
                if now - ts < _PROC_NAME_CACHE_TTL:
                    _PROC_NAME_CACHE.move_to_end(pid)
                    return name
                _PROC_NAME_CACHE.pop(pid, None)

        try:
            import psutil
            name = psutil.Process(pid).name()
        except Exception:  # noqa: BLE001
            name = ""

        with _PROC_NAME_LOCK:
            _PROC_NAME_CACHE[pid] = (now, name)
            _PROC_NAME_CACHE.move_to_end(pid)
            if len(_PROC_NAME_CACHE) > _PROC_NAME_CACHE_MAX:
                _PROC_NAME_CACHE.popitem(last=False)
        return name

    def _ip_region_cached(self, ip: str) -> str:
        """IP region lookup with LRU cache, 10-minute TTL (region does not change)."""
        now = time.time()
        with _IP_REGION_LOCK:
            item = _IP_REGION_CACHE.get(ip)
            if item is not None:
                ts, region = item
                if now - ts < _IP_REGION_CACHE_TTL:
                    _IP_REGION_CACHE.move_to_end(ip)
                    return region
                _IP_REGION_CACHE.pop(ip, None)

        region = _ip_region_lookup(ip) or ""

        with _IP_REGION_LOCK:
            _IP_REGION_CACHE[ip] = (now, region)
            _IP_REGION_CACHE.move_to_end(ip)
            if len(_IP_REGION_CACHE) > _IP_REGION_CACHE_MAX:
                _IP_REGION_CACHE.popitem(last=False)
        return region

    def _lookup_pid(self, src_ip, src_port, dst_ip, dst_port, list_rows_fn) -> int | None:
        """Look up PID by 5-tuple in the TCP table (legacy interface; internally uses the cached version)."""
        return self._lookup_pid_cached(src_ip, src_port, dst_ip, dst_port, list_rows_fn, is_udp=False)

    def _lookup_udp_pid(self, src_ip, src_port, dst_ip, dst_port, list_rows_fn) -> int | None:
        """Look up PID in the UDP table by local_ip+local_port (legacy interface, internally uses the cached version)."""
        return self._lookup_pid_cached(src_ip, src_port, dst_ip, dst_port, list_rows_fn, is_udp=True)

    def _is_local_ip(self, ip: str) -> bool:
        """Check whether the IP is a local IP."""
        if ip == "::1" or ip.startswith("127."):
            return True
        global _LOCAL_IP_CACHE, _LOCAL_IP_CACHE_TS
        now = time.time()
        local_ips = _LOCAL_IP_CACHE
        if local_ips is None or now - _LOCAL_IP_CACHE_TS > _LOCAL_IP_CACHE_TTL:
            with _LOCAL_IP_CACHE_LOCK:
                if _LOCAL_IP_CACHE is None or now - _LOCAL_IP_CACHE_TS > _LOCAL_IP_CACHE_TTL:
                    ips: set[str] = set()
                    try:
                        hostname = socket.gethostname()
                        for addr in socket.getaddrinfo(hostname, None):
                            ips.add(addr[4][0])
                    except Exception:  # noqa: BLE001
                        pass
                    _LOCAL_IP_CACHE = frozenset(ips)
                    _LOCAL_IP_CACHE_TS = now
                local_ips = _LOCAL_IP_CACHE
        return ip in local_ips

    @staticmethod
    def _dns_summary(dns_info: dict) -> str:
        """Format DNS parse results as readable text (stored in request_body/response_body)."""
        import json as _json
        return _json.dumps(dns_info, ensure_ascii=False, indent=2)


# 全局单例
# 类型注解：Unix 平台上 _raw_capture 可能是 UnixRawCapture 实例（duck typing）
# 用 Any 避免循环依赖，运行时通过 duck typing 调用 start/stop/running/last_error
_raw_capture: "RawCapture | Any" = None


def get_raw_capture() -> RawCapture | None:
    return _raw_capture


def start_raw_capture(session_id: int, pid_filter: set[int] | None = None,
                      port_filter: set[int] | None = None,
                      filter_str: str = "") -> tuple[bool, str]:
    """Start TCP/UDP capture. Returns (success, message).

    Platform support:
    - Windows: WinDivert SNIFF mode (requires pydivert + admin privileges)
    - Linux: AF_PACKET raw socket (requires root / CAP_NET_RAW)
    - macOS: BPF device (requires root)
    """
    global _raw_capture
    if _raw_capture and _raw_capture.running:
        return False, "Already running"

    # F19: 撤销 F18 互斥。raw_capture 用 SNIFF 模式（flags=1，只读不拦截），
    # 不阻止 transparent_proxy 的包流。F17 的 send 重试机制足以应对瞬时段锁定。
    if not IS_WINDOWS:
        # 非 Windows 平台：使用 Unix 跨平台抓包后端（AF_PACKET / BPF）
        return _start_unix_raw_capture(session_id, pid_filter, port_filter, filter_str)

    # Windows 平台：使用 WinDivert
    # 检查 pydivert 是否可用（已加入 requirements.txt，正常情况下不会缺失）
    try:
        import pydivert  # type: ignore  # noqa: F401
    except ImportError:
        return False, ("pydivert is not installed. This dependency is declared in requirements.txt; "
                       "please run pip install -r requirements.txt to install all dependencies.")
    _raw_capture = RawCapture(session_id)
    if pid_filter:
        _raw_capture.set_pid_filter(pid_filter)
    if port_filter:
        _raw_capture.set_port_filter(port_filter)
    if filter_str:
        _raw_capture.set_filter(filter_str)
    ok = _raw_capture.start()
    if not ok:
        # 根据异常详情生成更精准的提示，帮助用户定位是杀软拦截还是其他原因
        err_detail = (_raw_capture._last_error or "").lower()
        if "签名" in err_detail or "sign" in err_detail or "加载失败" in err_detail or "驱动" in err_detail:
            msg = ("Start failed: WinDivert driver load was blocked. "
                   "Common cause: antivirus (360/Huorong/Windows Defender) flagged it as a vulnerable driver. "
                   "Please disable antivirus or add the Telnix directory + WinDivert64.sys to the whitelist and retry.")
        elif "拒绝访问" in err_detail or "access is denied" in err_detail or "权限" in err_detail:
            msg = ("Start failed: insufficient privileges. Please run Telnix as administrator "
                   "(click the 'Restart as Admin' button below).")
        elif "找不到" in err_detail or "not found" in err_detail or "找不到指定的模块" in err_detail:
            msg = ("Start failed: WinDivert driver file missing. "
                   "Ensure you have run pip install pydivert and that WinDivert64.sys is in the same directory as python.exe.")
        else:
            msg = ("Start failed. Possible causes: 1) not running as administrator; "
                   "2) WinDivert driver file missing; 3) pydivert not properly installed; "
                   "4) antivirus blocking driver load. "
                   "Please restart Telnix as administrator and ensure pip install pydivert has been run; "
                   "if it still fails, try disabling antivirus.")
        _raw_capture = None
        return False, msg
    return True, "TCP/UDP capture started (WinDivert)"


def _start_unix_raw_capture(session_id: int, pid_filter: set[int] | None = None,
                            port_filter: set[int] | None = None,
                            filter_str: str = "") -> tuple[bool, str]:
    """Start TCP/UDP capture on Unix platforms (Linux/macOS).

    Uses AF_PACKET (Linux) or BPF (macOS) instead of WinDivert.
    Requires root privileges.
    """
    global _raw_capture
    try:
        from .raw_capture_unix import UnixRawCapture, IS_UNIX
    except ImportError as e:
        return False, f"Failed to load Unix capture backend: {e}"
    if not IS_UNIX:
        return False, "TCP/UDP capture is not supported on this platform"
    _raw_capture = UnixRawCapture(session_id)
    if pid_filter:
        _raw_capture.set_pid_filter(pid_filter)
    if port_filter:
        _raw_capture.set_port_filter(port_filter)
    if filter_str:
        # Unix 后端忽略 WinDivert filter 字符串（端口过滤通过 port_filter 参数）
        _raw_capture.set_filter(filter_str)
    ok = _raw_capture.start()
    if not ok:
        err_detail = _raw_capture.last_error or ""
        if "权限" in err_detail or "root" in err_detail or "CAP_NET_RAW" in err_detail:
            msg = ("Start failed: root privileges required. Please start Telnix with sudo:\n"
                   "  sudo ./start.sh --no-browser\n"
                   "or: sudo python3 -m telnix --no-browser")
        elif "BPF" in err_detail or "/dev/bpf" in err_detail:
            msg = ("Start failed: no available BPF device. Please confirm you are running as root, "
                   "and check whether /dev/bpfN devices are occupied by other capture tools.")
        elif "AF_PACKET" in err_detail:
            msg = ("Start failed: AF_PACKET creation failed. Please confirm you are running as root, "
                   "and check whether the kernel supports AF_PACKET (standard Linux kernels all support it).")
        else:
            msg = f"Start failed: {err_detail or 'unknown reason'}"
        _raw_capture = None
        return False, msg
    backend = "AF_PACKET" if sys.platform.startswith("linux") else "BPF"
    return True, f"TCP/UDP capture started ({backend})"


def stop_raw_capture() -> tuple[bool, str]:
    """Stop TCP/UDP capture."""
    global _raw_capture
    if not _raw_capture or not _raw_capture.running:
        return False, "Not running"
    _raw_capture.stop()
    _raw_capture = None
    return True, "Stopped"


def raw_capture_status() -> dict:
    """Return TCP/UDP capture status.

    Platform support:
    - Windows: check pydivert + admin privileges
    - Linux: check root (AF_PACKET backend, no external dependencies)
    - macOS: check root (BPF backend, no external dependencies)
    """
    running = bool(_raw_capture and _raw_capture.running)
    # 非 Windows 平台：返回 Unix 后端状态
    if not IS_WINDOWS:
        try:
            is_admin = os.geteuid() == 0
        except AttributeError:  # noqa: BLE001
            is_admin = False
        backend = "af_packet" if sys.platform.startswith("linux") else (
            "bpf" if sys.platform == "darwin" else "none"
        )
        if running:
            hint = "Ready (running)"
        elif not is_admin:
            hint = "Root privileges required. Please start Telnix with sudo"
        else:
            hint = "Ready"
        return {
            "running": running,
            "pydivert_installed": False,  # 兼容字段，非 Windows 不用 pydivert
            "is_admin": is_admin,
            "backend": backend,
            "supported": True,
            "hint": hint,
        }
    # Windows 平台：检查 pydivert
    try:
        import pydivert  # type: ignore  # noqa: F401
        pydivert_installed = True
    except ImportError:
        pydivert_installed = False
    # 检查管理员权限
    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        is_admin = False
    return {
        "running": running,
        "pydivert_installed": pydivert_installed,
        "is_admin": is_admin,
        "backend": "windivert",
        "supported": True,
        "hint": _get_hint(pydivert_installed, is_admin),
    }


def _get_hint(pydivert_installed: bool, is_admin: bool) -> str:
    """Generate status hint."""
    if not pydivert_installed:
        return "pydivert not installed. Please run: pip install pydivert"
    if not is_admin:
        return "Administrator privileges required. Please restart Telnix as administrator"
    return "Ready"

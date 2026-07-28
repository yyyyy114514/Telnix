"""TCP/UDP 原始抓包后端（跨平台）。

独立于 HTTP 代理后端，通过网络层抓包，支持非 HTTP 协议（Steam P2P、protobuf 等）。

平台支持：
- Windows: WinDivert 内核驱动（需 pydivert + 管理员权限）
  首次运行时检测 pydivert 是否安装，未安装则提示：pip install pydivert
  并需要 WinDivert64.sys（pydivert 自带）
- Linux: AF_PACKET raw socket（需 root / CAP_NET_RAW，纯 stdlib 无外部依赖）
- macOS: BPF 设备（需 root，纯 stdlib 无外部依赖）

非 Windows 平台的实际抓包逻辑在 raw_capture_unix.py 的 UnixRawCapture 类中实现，
本模块的 RawCapture 类仅 Windows 使用，但 start()/stop()/raw_capture_status()
统一对外，跨平台路由在 start_raw_capture() 中完成。

抓到的包以 protocol=tcp/udp 存入 flows 表，raw_data 字段存 base64 编码的原始字节。
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


def _extract_path(url: str, host: str | None, scheme: str | None) -> str:
    """从完整 url 中稳健地提取 path（含 query/fragment）。

    旧实现用 ``url.split(host, 1)[-1]``，当 host 字符串在 path 中再次出现时
    会误截断（例如 host 为 'api'，path 含 '/api/v1/api'）。此处改为按
    ``scheme://host`` 前缀精确裁剪。
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
    """WinDivert 抓包后端。"""

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
        # 4 个 worker：2 个在高流量时容易成为瓶颈（1000 pps 时每 worker 500 pps）
        self._enrich_worker_count = 4

    def set_pid_filter(self, pids: set[int] | None):
        self._pid_filter = pids

    def set_port_filter(self, ports: set[int] | None):
        self._port_filter = ports

    def set_filter(self, filter_str: str):
        """设置 WinDivert filter 字符串。空字符串恢复默认 filter。"""
        if filter_str:
            self._filter = filter_str
        else:
            # 恢复默认（最简 filter，端口排除在代码层）
            self._filter = "tcp or udp"

    def start(self) -> bool:
        """启动抓包（Windows 专用）。返回 True 成功，False 失败（驱动未装等）。

        注意：非 Windows 平台不应调用此方法。start_raw_capture() 会自动路由到
        UnixRawCapture（raw_capture_unix.py），无需调用方关心平台。
        """
        if not IS_WINDOWS:
            # 防御性检查：非 Windows 平台不应走到这里（start_raw_capture 会路由到 UnixRawCapture）
            logger.error("raw", "RawCapture.start() 在非 Windows 平台被调用",
                         "应由 start_raw_capture() 路由到 UnixRawCapture，请检查调用方")
            self._last_error = "内部错误：Windows 后端在非 Windows 平台被调用"
            return False
        try:
            import pydivert  # type: ignore  # noqa: F401
        except ImportError:
            logger.error("raw", "pydivert 未安装",
                         "请运行: pip install pydivert")
            return False
        if self._running:
            return True
        try:
            import pydivert  # type: ignore
            # 检查管理员权限
            if not self._is_admin():
                logger.error("raw", "WinDivert 需要管理员权限",
                             "请用管理员身份运行 Telnix")
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
            logger.info("raw", "TCP/UDP 抓包已启动", f"filter={self._filter}, workers={self._enrich_worker_count}")
            return True
        except Exception as e:  # noqa: BLE001
            err_msg = str(e)
            logger.error("raw", "WinDivert 启动失败", err_msg)
            # 把异常详情存到实例上，让 start_raw_capture 能带回到前端
            self._last_error = err_msg
            # 检测常见失败原因，给前端更友好的提示
            low = err_msg.lower()
            if "找不到" in err_msg or "not found" in low or "找不到指定的模块" in err_msg:
                logger.error("raw", "WinDivert 驱动文件缺失",
                             "请确保 WinDivert64.sys 与 python.exe 同目录，或 pydivert 已正确安装")
            elif "access is denied" in low or "拒绝访问" in err_msg or "权限" in err_msg:
                logger.error("raw", "权限不足",
                             "请用管理员身份运行 Telnix")
            elif "签名" in err_msg or "sign" in low or "数字签名" in err_msg or "加载失败" in err_msg:
                # 杀软拦截通常表现为驱动加载失败 / 签名问题
                logger.error("raw", "WinDivert 驱动加载被拦截",
                             "可能是杀毒软件（360/火绒/Windows Defender）拦截，请将 Telnix 目录和 WinDivert64.sys 加入杀软白名单后重试")
            self._divert = None
            return False

    def stop(self):
        """停止抓包。

        先 _running=False，再用 shutdown 解除 recv 阻塞，再 close。
        避免直接 close 导致工作线程永久阻塞在 recv。
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
                    logger.warning("raw", "停止时工作线程仍在运行",
                                   "可能存在阻塞 recv")
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
            logger.warning("raw", "抓包停止，最终丢包统计", f"累计丢包数: {total_dropped}")
        logger.info("raw", "TCP/UDP 抓包已停止")

    @property
    def running(self) -> bool:
        return self._running

    def _is_admin(self) -> bool:
        """检查是否管理员权限。

        Windows：用 ctypes.windll.shell32.IsUserAnAdmin()。
        非 Windows：用 os.geteuid() == 0 判断 root（POSIX 系统统一接口）。
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
        """抓包主循环（轻量：只解包+基本过滤+入队，不做 PID/进程名/IP属地等慢操作）。

        性能优化：抓包线程只做最小工作（解包+loopback/端口过滤+构造raw dict+入队），
        PID 反查、进程名查询、IP 属地查询、DNS 解析等慢操作全部移到 _enrich_loop worker 线程。
        这样抓包线程吞吐量提升 5-10x，避免 1000 pps 时因 PID 全表扫描阻塞丢包。
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
                if dst_port in (80, 443, 8888) or src_port in (80, 443, 8888):
                    self._http_pkt_count = getattr(self, '_http_pkt_count', 0) + 1
                    if self._http_pkt_count == 1 or self._http_pkt_count % 50 == 0:
                        logger.info(
                            "raw", "HTTP/HTTPS包统计",
                            f"count={self._http_pkt_count} "
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
                            logger.warning("raw", "抓包队列满丢包", f"累计丢包数: {_DROPPED_PACKETS}")

                # SNIFF 模式无需 send，包已正常流转

            except Exception as e:  # noqa: BLE001
                if self._running:
                    logger.error("raw", "抓包循环异常", str(e))
                    time.sleep(0.1)

    def _enrich_loop(self):
        """enrich worker：从队列取 raw dict，补充 PID/进程名/IP属地/DNS，写库。"""
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
                    logger.error("raw", "enrich 异常", str(e))

    def _enrich_one(self, raw: dict, list_tcp_fn, list_udp_fn):
        """处理单个 raw dict：PID 反查 → 进程名 → PID 过滤 → DNS 解析 → IP 属地 → 写库。"""
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
        db.insert_flow_async(flow)

    @staticmethod
    def _parse_http_payload(payload: bytes, is_outbound: bool):
        """解析 HTTP 请求/响应载荷。返回 (method, url, host, status_code, body_text) 或 None。

        F21: 让 raw_capture 能识别 80 端口的 HTTP 流量，存为 protocol="http"。
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
        """检测 WebSocket 升级请求。返回 (sec_websocket_key, sec_websocket_version) 或 None。

        F23: WebSocket 升级请求是 HTTP GET + Upgrade: websocket 头。
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
        """从 TLS ClientHello 提取 SNI hostname。返回 hostname 或 None。

        F22: 让 443 端口流量能存为 protocol="https"，显示目标域名。
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
        """带 LRU 缓存的 PID 反查。

        性能优化：
        - TCP: 五元组 → pid，3 秒 TTL（同一连接的后续包命中缓存）
        - UDP: (local_ip, local_port) → pid，3 秒 TTL
        - lookup 锁防止全表扫描并发
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
        """带 LRU 缓存的进程名查询，5 秒 TTL。"""
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
        """带 LRU 缓存的 IP 属地查询，10 分钟 TTL（属地不变）。"""
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
        """在 TCP 表里按五元组查 PID（保留旧接口，内部走缓存版本）。"""
        return self._lookup_pid_cached(src_ip, src_port, dst_ip, dst_port, list_rows_fn, is_udp=False)

    def _lookup_udp_pid(self, src_ip, src_port, dst_ip, dst_port, list_rows_fn) -> int | None:
        """在 UDP 表里按 local_ip+local_port 查 PID（保留旧接口，内部走缓存版本）。"""
        return self._lookup_pid_cached(src_ip, src_port, dst_ip, dst_port, list_rows_fn, is_udp=True)

    def _is_local_ip(self, ip: str) -> bool:
        """判断是否本机 IP。"""
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
        """把 DNS 解析结果格式化为可读文本（存到 request_body/response_body）。"""
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
    """启动 TCP/UDP 抓包。返回 (成功, 消息)。

    平台支持：
    - Windows: WinDivert SNIFF 模式（需 pydivert + 管理员权限）
    - Linux: AF_PACKET raw socket（需 root / CAP_NET_RAW）
    - macOS: BPF 设备（需 root）
    """
    global _raw_capture
    if _raw_capture and _raw_capture.running:
        return False, "已在运行中"

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
        return False, ("pydivert 未安装。该依赖已在 requirements.txt 中声明，"
                       "请执行 pip install -r requirements.txt 完整安装依赖。")
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
            msg = ("启动失败：WinDivert 驱动加载被拦截。"
                   "常见原因是杀毒软件（360/火绒/Windows Defender）将其识别为漏洞驱动。"
                   "请关闭杀毒软件或将 Telnix 目录 + WinDivert64.sys 加入白名单后重试。")
        elif "拒绝访问" in err_detail or "access is denied" in err_detail or "权限" in err_detail:
            msg = ("启动失败：权限不足。请用管理员身份运行 Telnix "
                   "（点击下方「管理员重启」按钮）。")
        elif "找不到" in err_detail or "not found" in err_detail or "找不到指定的模块" in err_detail:
            msg = ("启动失败：WinDivert 驱动文件缺失。"
                   "请确保已运行 pip install pydivert，且 WinDivert64.sys 与 python.exe 同目录。")
        else:
            msg = ("启动失败。可能原因：1) 未用管理员身份运行；"
                   "2) WinDivert 驱动文件缺失；3) pydivert 未正确安装；"
                   "4) 杀毒软件拦截驱动加载。"
                   "请用管理员身份重启 Telnix，并确保已运行 pip install pydivert；"
                   "若仍失败请尝试关闭杀毒软件。")
        _raw_capture = None
        return False, msg
    return True, "TCP/UDP 抓包已启动（WinDivert）"


def _start_unix_raw_capture(session_id: int, pid_filter: set[int] | None = None,
                            port_filter: set[int] | None = None,
                            filter_str: str = "") -> tuple[bool, str]:
    """Unix 平台（Linux/macOS）启动 TCP/UDP 抓包。

    使用 AF_PACKET (Linux) 或 BPF (macOS) 替代 WinDivert。
    需要 root 权限。
    """
    global _raw_capture
    try:
        from .raw_capture_unix import UnixRawCapture, IS_UNIX
    except ImportError as e:
        return False, f"加载 Unix 抓包后端失败: {e}"
    if not IS_UNIX:
        return False, "当前平台不支持 TCP/UDP 抓包"
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
            msg = ("启动失败：需要 root 权限。请用 sudo 启动 Telnix：\n"
                   "  sudo ./start.sh --no-browser\n"
                   "或：sudo python3 -m telnix --no-browser")
        elif "BPF" in err_detail or "/dev/bpf" in err_detail:
            msg = ("启动失败：找不到可用的 BPF 设备。请确认以 root 身份运行，"
                   "并检查 /dev/bpfN 设备是否被其他抓包工具占用。")
        elif "AF_PACKET" in err_detail:
            msg = ("启动失败：AF_PACKET 创建失败。请确认以 root 身份运行，"
                   "并检查内核是否支持 AF_PACKET（标准 Linux 内核均支持）。")
        else:
            msg = f"启动失败：{err_detail or '未知原因'}"
        _raw_capture = None
        return False, msg
    backend = "AF_PACKET" if sys.platform.startswith("linux") else "BPF"
    return True, f"TCP/UDP 抓包已启动（{backend}）"


def stop_raw_capture() -> tuple[bool, str]:
    """停止 TCP/UDP 抓包。"""
    global _raw_capture
    if not _raw_capture or not _raw_capture.running:
        return False, "未在运行"
    _raw_capture.stop()
    _raw_capture = None
    return True, "已停止"


def raw_capture_status() -> dict:
    """返回 TCP/UDP 抓包状态。

    平台支持：
    - Windows: 检查 pydivert + 管理员权限
    - Linux: 检查 root（AF_PACKET 后端，无外部依赖）
    - macOS: 检查 root（BPF 后端，无外部依赖）
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
            hint = "就绪（运行中）"
        elif not is_admin:
            hint = "需要 root 权限。请用 sudo 启动 Telnix"
        else:
            hint = "就绪"
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
    """生成状态提示。"""
    if not pydivert_installed:
        return "未安装 pydivert。请运行: pip install pydivert"
    if not is_admin:
        return "需要管理员权限。请用管理员身份重启 Telnix"
    return "就绪"

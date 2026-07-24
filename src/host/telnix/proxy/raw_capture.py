"""TCP/UDP 原始抓包后端（WinDivert）。

独立于 HTTP 代理后端，通过网络层抓包，支持非 HTTP 协议（Steam P2P、protobuf 等）。
需要管理员权限 + WinDivert 驱动。

首次运行时检测 pydivert 是否安装，未安装则提示用户：
    pip install pydivert
并需要 WinDivert64.sys（pydivert 自带）。

抓到的包以 protocol=tcp/udp 存入 flows 表，raw_data 字段存 base64 编码的原始字节。

跨平台说明：WinDivert 是 Windows 专属内核驱动，macOS/Linux 上 pydivert 无法导入。
非 Windows 平台调用 start()/stop()/raw_capture_status() 会返回"不支持"错误，
但不影响其他功能（HTTP 代理、SSL bump 等）正常运行。
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
        """启动抓包。返回 True 成功，False 失败（驱动未装等）。

        非 Windows 平台：WinDivert 是 Windows 专属驱动，直接返回失败并打印"不支持"。
        """
        if not IS_WINDOWS:
            # 非 Windows 平台：WinDivert 不可用，降级返回失败
            logger.error("raw", "TCP/UDP 抓包不支持当前平台",
                         "WinDivert 是 Windows 专属驱动，macOS/Linux 无法使用此功能")
            self._last_error = "当前平台不支持 TCP/UDP 抓包（仅 Windows 可用）"
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
        """停止抓包。"""
        global _DROPPED_PACKETS
        self._running = False
        if self._divert:
            try:
                self._divert.close()
            except Exception:  # noqa: BLE001
                pass
            self._divert = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
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
                protocol = ip_hdr.protocol  # 6=TCP, 17=UDP

                # 代码层排除 loopback（filter 层无法可靠排除 IPv6 loopback）
                if src_ip in ("127.0.0.1", "::1") or dst_ip in ("127.0.0.1", "::1"):
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
                if src_port in self._self_ports or dst_port in self._self_ports:
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
        # 1. 查缓存
        with _PID_CACHE_LOCK:
            item = _PID_CACHE.get(cache_key)
            if item is not None:
                ts, pid = item
                if now - ts < _PID_CACHE_TTL:
                    _PID_CACHE.move_to_end(cache_key)
                    return pid
                _PID_CACHE.pop(cache_key, None)

        # 2. 双检锁：拿锁后再查一次（不再持 _LOOKUP_LOCK，允许多 worker 并发读表快照）
        with _PID_CACHE_LOCK:
            item = _PID_CACHE.get(cache_key)
            if item is not None:
                ts, pid = item
                if now - ts < _PID_CACHE_TTL:
                    _PID_CACHE.move_to_end(cache_key)
                    return pid
                _PID_CACHE.pop(cache_key, None)

        # 3. 真正查表（表是只读快照，并发安全）
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
        if ip in ("127.0.0.1", "::1"):
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
_raw_capture: RawCapture | None = None


def get_raw_capture() -> RawCapture | None:
    return _raw_capture


def start_raw_capture(session_id: int, pid_filter: set[int] | None = None,
                      port_filter: set[int] | None = None,
                      filter_str: str = "") -> tuple[bool, str]:
    """启动 TCP/UDP 抓包。返回 (成功, 消息)。

    非 Windows 平台：WinDivert 不可用，直接返回"不支持"错误。
    """
    global _raw_capture
    if _raw_capture and _raw_capture.running:
        return False, "已在运行中"
    # 非 Windows 平台：WinDivert 不可用，降级返回错误
    if not IS_WINDOWS:
        return False, ("当前平台不支持 TCP/UDP 抓包（WinDivert 是 Windows 专属驱动）。"
                       "macOS/Linux 上可使用 HTTP 代理抓包功能。")
    # 检查 pydivert 是否可用
    try:
        import pydivert  # type: ignore  # noqa: F401
    except ImportError:
        return False, ("pydivert 未安装。请运行: pip install pydivert。"
                       "首次使用需安装此依赖。")
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
    return True, "TCP/UDP 抓包已启动"


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

    非 Windows 平台：pydivert_installed=False，is_admin 用 os.geteuid() 判断，
    hint 提示当前平台不支持。
    """
    running = bool(_raw_capture and _raw_capture.running)
    # 非 Windows 平台：WinDivert 不可用，直接返回不支持状态
    if not IS_WINDOWS:
        # POSIX 平台：用 os.geteuid() == 0 判断 root
        try:
            is_admin = os.geteuid() == 0
        except AttributeError:  # noqa: BLE001
            is_admin = False
        return {
            "running": running,
            "pydivert_installed": False,
            "is_admin": is_admin,
            "hint": "当前平台不支持 TCP/UDP 抓包（WinDivert 仅 Windows 可用）",
        }
    # 检查 pydivert 是否安装
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
        "hint": _get_hint(pydivert_installed, is_admin),
    }


def _get_hint(pydivert_installed: bool, is_admin: bool) -> str:
    """生成状态提示。"""
    if not pydivert_installed:
        return "未安装 pydivert。请运行: pip install pydivert"
    if not is_admin:
        return "需要管理员权限。请用管理员身份重启 Telnix"
    return "就绪"

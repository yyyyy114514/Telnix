"""TCP/UDP 原始抓包后端（WinDivert）。

独立于 HTTP 代理后端，通过网络层抓包，支持非 HTTP 协议（Steam P2P、protobuf 等）。
需要管理员权限 + WinDivert 驱动。

首次运行时检测 pydivert 是否安装，未安装则提示用户：
    pip install pydivert
并需要 WinDivert64.sys（pydivert 自带）。

抓到的包以 protocol=tcp/udp 存入 flows 表，raw_data 字段存 base64 编码的原始字节。
"""

from __future__ import annotations

import base64
import ctypes
import os
import socket
import struct
import threading
import time
from datetime import datetime

from .. import db, logger


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
        # OpenNet 自身端口，代码层排除避免回环噪音
        self._self_ports = {8888, 18901}
        self._pid_filter: set[int] | None = None  # None=不过滤
        self._port_filter: set[int] | None = None

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
        """启动抓包。返回 True 成功，False 失败（驱动未装等）。"""
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
                             "请用管理员身份运行 OpenNet")
                return False
            # 使用 SNIFF 模式：只嗅探不拦截，包会正常流转不会断网
            # WINDIVERT_FLAG_SNIFF = 1
            self._divert = pydivert.WinDivert(self._filter, flags=1)
            self._divert.open()
            self._running = True
            self._thread = threading.Thread(target=self._capture_loop, daemon=True)
            self._thread.start()
            logger.info("raw", "TCP/UDP 抓包已启动", f"filter={self._filter}")
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
                             "请用管理员身份运行 OpenNet")
            elif "签名" in err_msg or "sign" in low or "数字签名" in err_msg or "加载失败" in err_msg:
                # 杀软拦截通常表现为驱动加载失败 / 签名问题
                logger.error("raw", "WinDivert 驱动加载被拦截",
                             "可能是杀毒软件（360/火绒/Windows Defender）拦截，请将 OpenNet 目录和 WinDivert64.sys 加入杀软白名单后重试")
            self._divert = None
            return False

    def stop(self):
        """停止抓包。"""
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
        logger.info("raw", "TCP/UDP 抓包已停止")

    @property
    def running(self) -> bool:
        return self._running

    def _is_admin(self) -> bool:
        """检查是否管理员权限（Windows）。"""
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            return False

    def _capture_loop(self):
        """抓包主循环。"""
        import pydivert  # type: ignore
        from .process_lookup import _list_tcp_owner_rows, _list_udp_owner_rows

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

                # 代码层排除 OpenNet 自身端口（避免回环噪音）
                if src_port in self._self_ports or dst_port in self._self_ports:
                    continue

                # 端口过滤
                if self._port_filter:
                    if src_port not in self._port_filter and dst_port not in self._port_filter:
                        continue

                # PID 反查：TCP 用五元组匹配，UDP 按 local_ip+local_port 匹配
                if proto_name == "tcp":
                    pid = self._lookup_pid(src_ip, src_port, dst_ip, dst_port, _list_tcp_owner_rows)
                else:
                    pid = self._lookup_udp_pid(src_ip, src_port, dst_ip, dst_port, _list_udp_owner_rows)
                proc_name = ""
                if pid:
                    try:
                        import psutil
                        proc_name = psutil.Process(pid).name()
                    except Exception:  # noqa: BLE001
                        proc_name = ""

                # PID 过滤
                if self._pid_filter and pid not in self._pid_filter:
                    continue

                # payload
                payload = packet.payload or b""

                # 存入数据库（每条包独立存储，不做流重组）
                # 判断方向：本机发出的包（src 是本机）算请求，否则算响应
                is_outbound = self._is_local_ip(src_ip)
                # 对端 = 出站时是 dst，入站时是 src；url/host 都指向对端
                remote_ip = dst_ip if is_outbound else src_ip
                remote_port = dst_port if is_outbound else src_port
                local_port = src_port if is_outbound else dst_port

                # DNS 解析：UDP 端口 53 的包尝试解析为 DNS
                # 解析成功后把域名/类型/应答 IP 写到 path 字段，方便在列表里直接看到
                dns_info = None
                if proto_name == "udp" and (src_port == 53 or dst_port == 53) and payload:
                    try:
                        from .dns_parser import parse_dns
                        dns_info = parse_dns(payload)
                    except Exception:  # noqa: BLE001
                        dns_info = None

                if dns_info:
                    # DNS 包：path 显示「DNS 域名 类型」便于识别
                    q = dns_info.get("questions") or [{}]
                    qname = q[0].get("qname", "") if q else ""
                    qtype = q[0].get("qtype_name", "") if q else ""
                    if dns_info.get("is_response"):
                        # 响应：把应答 IP 拼到 path
                        answers = dns_info.get("answers") or []
                        a_values = []
                        for a in answers[:5]:  # 最多显示 5 条
                            if a.get("type") in (1, 28):  # A/AAAA
                                a_values.append(a.get("rdata", ""))
                        ans_str = ", ".join(a_values) if a_values else dns_info.get("rcode_name", "")
                        path = f"DNS {qname} {qtype} -> {ans_str}"
                        method = "DNS-RESP"
                    else:
                        path = f"DNS {qname} {qtype}"
                        method = "DNS-QUERY"
                    # host 字段存域名，方便搜索
                    host = qname or remote_ip
                    # raw_data 仍存原始字节，额外把解析结果存 request_body/response_body
                    dns_summary = self._dns_summary(dns_info)
                    flow = {
                        "session_id": self.session_id,
                        "timestamp": datetime.now().isoformat(),
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
                    }
                else:
                    flow = {
                        "session_id": self.session_id,
                        "timestamp": datetime.now().isoformat(),
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
                    }
                db.insert_flow(flow)

                # SNIFF 模式无需 send，包已正常流转

            except Exception as e:  # noqa: BLE001
                if self._running:
                    logger.error("raw", "抓包循环异常", str(e))
                    time.sleep(0.1)

    def _lookup_pid(self, src_ip, src_port, dst_ip, dst_port, list_rows_fn) -> int | None:
        """在 TCP 表里按五元组查 PID。"""
        try:
            for (lip, lport, rip, rport, pid, _state) in list_rows_fn():
                if lip == src_ip and lport == src_port and rip == dst_ip and rport == dst_port:
                    return pid
                # 反向（收到的包）
                if lip == dst_ip and lport == dst_port and rip == src_ip and rport == src_port:
                    return pid
        except Exception:  # noqa: BLE001
            pass
        return None

    def _lookup_udp_pid(self, src_ip, src_port, dst_ip, dst_port, list_rows_fn) -> int | None:
        """在 UDP 表里按 local_ip+local_port 查 PID。

        UDP 是无连接的，UDP 表只有 local 端信息。
        - 本机发出的包：src_ip+src_port 是本机 UDP socket 的 local
        - 收到的入站包：dst_ip+dst_port 是本机 UDP socket 的 local
        本机 socket 可能绑定 0.0.0.0，所以 0.0.0.0:port 匹配任意本机 IP。
        """
        try:
            # 判断哪个端是本机：出站包 src 是本机，入站包 dst 是本机
            src_is_local = self._is_local_ip(src_ip)
            local_ip = src_ip if src_is_local else dst_ip
            local_port = src_port if src_is_local else dst_port
            for (lip, lport, pid) in list_rows_fn():
                # 0.0.0.0 绑定匹配所有本机 IP
                if lport == local_port and (lip == local_ip or lip == "0.0.0.0"):
                    return pid
        except Exception:  # noqa: BLE001
            pass
        return None

    def _is_local_ip(self, ip: str) -> bool:
        """判断是否本机 IP。"""
        if ip in ("127.0.0.1", "::1"):
            return True
        try:
            hostname = socket.gethostname()
            local_ips = socket.getaddrinfo(hostname, None)
            for addr in local_ips:
                if ip in addr[4][0]:
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

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
    """启动 TCP/UDP 抓包。返回 (成功, 消息)。"""
    global _raw_capture
    if _raw_capture and _raw_capture.running:
        return False, "已在运行中"
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
                   "请关闭杀毒软件或将 OpenNet 目录 + WinDivert64.sys 加入白名单后重试。")
        elif "拒绝访问" in err_detail or "access is denied" in err_detail or "权限" in err_detail:
            msg = ("启动失败：权限不足。请用管理员身份运行 OpenNet "
                   "（点击下方「管理员重启」按钮）。")
        elif "找不到" in err_detail or "not found" in err_detail or "找不到指定的模块" in err_detail:
            msg = ("启动失败：WinDivert 驱动文件缺失。"
                   "请确保已运行 pip install pydivert，且 WinDivert64.sys 与 python.exe 同目录。")
        else:
            msg = ("启动失败。可能原因：1) 未用管理员身份运行；"
                   "2) WinDivert 驱动文件缺失；3) pydivert 未正确安装；"
                   "4) 杀毒软件拦截驱动加载。"
                   "请用管理员身份重启 OpenNet，并确保已运行 pip install pydivert；"
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
    """返回 TCP/UDP 抓包状态。"""
    running = bool(_raw_capture and _raw_capture.running)
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
        return "需要管理员权限。请用管理员身份重启 OpenNet"
    return "就绪"

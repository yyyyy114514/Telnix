"""透明代理模式（简化版）- WinDivert NETWORK 层重定向。

工作原理：
- WinDivert 拦截出站 TCP dstPort=80 的包
- 修改目的地址为 127.0.0.1:8888（本地 HTTP 代理端口）
- 同时修改源地址为 127.0.0.1（让回包走代理）
- 代理收到 HTTP 请求时，从 Host 头识别真实目标，转发到原服务器
- 代理回包时，WinDivert 拦截反向流量，把 src 改回原 IP:80

简化范围：
- HTTP(80) 走代理正常解析；HTTPS(443) 走 raw TCP 隧道（不解密，端到端 TLS）
- 仅出站连接，不处理入站
- 仅在用户显式启用时工作（设置 transparent_proxy=True）
- 与系统代理并存：系统代理处理已配置的客户端，透明代理处理无代理感知的客户端

隐蔽性优势：
- 应用无需配置代理，对 HTTP 流量完全透明
- 不修改系统注册表（不写 ProxyServer/ProxyEnable）
- 浏览器无代理感知，难以通过常规手段探测
"""
from __future__ import annotations

import os
import socket
import struct
import threading
import time
from typing import Optional

from .. import logger

# Windows 专属：WinDivert 仅 Windows 可用
IS_WINDOWS = os.name == "nt"
if IS_WINDOWS:
    import ctypes
    import pydivert  # type: ignore


# 透明代理本地监听端口（与 HTTP 代理共用 8888）
_LOCAL_HOST = "127.0.0.1"
_LOCAL_PORT = 8888

# 重定向目标端口：HTTP(80) + HTTPS(443)
# HTTP 走代理正常解析；HTTPS 仅做 raw TCP 隧道转发（不解密），由 server.py 在
# _handle_client 入口判断首字节非 HTTP method 时调用 lookup_reverse 反查原目标。
_REDIRECT_DST_PORTS = {80, 443}

# NAT 表条目 TTL（秒）：超过此时间未活动的条目被清理
_NAT_TTL = 60.0


class TransparentProxy:
    """透明代理：用 WinDivert NETWORK 层重定向 HTTP(80) 流量到本地代理。

    实现要点：
    - NETWORK 层抓包：能拿到完整 IP/TCP 头
    - 修改目的 IP/Port 为本地代理，重新计算校验和后 send
    - 维护 NAT 表：原 (src_ip:port, dst_ip:80) <-> (127.0.0.1:port, 127.0.0.1:8888)
    - 反向流量（代理回包）：查 NAT 表，把 src 从本地改回原服务器 IP:80
    - 非本代理端口的流量直接放行（不影响其他网络活动）

    NAT 表设计：
    - key = (src_ip, src_port, dst_ip, dst_port) 四元组（出站时记录）
    - 反向查找 key = (原 dst_ip, 80, 原 src_ip, 原 src_port)（反向四元组）
    - 每个条目带 last_seen 时间戳，TTL 60s 自动清理
    - TCP FIN/RST 收到时立即清理对应条目
    """

    def __init__(self, local_port: int = _LOCAL_PORT):
        self.local_port = local_port
        self._divert: Optional["pydivert.WinDivert"] = None  # type: ignore
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_error: str = ""
        # NAT 表：{forward_key: (orig_src_ip, orig_src_port, orig_dst_ip, orig_dst_port, last_seen)}
        # forward_key = (src_ip, src_port, dst_ip, dst_port)
        self._nat_table: dict = {}
        # 反向索引：{reverse_key: forward_key}
        # reverse_key = (orig_dst_ip, orig_dst_port, orig_src_ip, orig_src_port)
        self._nat_reverse: dict = {}
        self._nat_lock = threading.Lock()
        # 统计
        self._redirected_count = 0
        self._passed_count = 0
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
            "nat_table_size": nat_size,
            "last_error": self._last_error,
            "local_port": self.local_port,
            "redirect_ports": list(_REDIRECT_DST_PORTS),
        }

    def lookup_reverse(self, client_src_port: int) -> Optional[tuple]:
        """供代理服务器在 raw tunnel 模式下反查原目标。

        参数：client_src_port - 客户端连接到本地代理时的源端口
        （等价于原出站包的 src_port，唯一标识一条 NAT 条目）

        返回：(orig_dst_ip, orig_dst_port) 或 None（无匹配）
        """
        with self._nat_lock:
            for forward_key, entry in self._nat_table.items():
                # forward_key = (src_ip, src_port, dst_ip, dst_port)
                if forward_key[1] == client_src_port:
                    # entry = (orig_src_ip, orig_src_port, orig_dst_ip, orig_dst_port, last_seen)
                    return entry[2], entry[3]
        return None

    def _is_admin(self) -> bool:
        """检查管理员权限。"""
        if not IS_WINDOWS:
            return False
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            return False

    def start(self) -> bool:
        """启动透明代理。返回 True 成功，False 失败。"""
        if not IS_WINDOWS:
            self._last_error = "透明代理仅 Windows 可用（依赖 WinDivert）"
            return False
        if self._running:
            return True
        if not self._is_admin():
            self._last_error = "需要管理员权限（WinDivert 要求）"
            return False
        try:
            # filter：出站 TCP dst port in {80}，或反向 src port = local_port
            # 排除 loopback 避免回环噪音
            # NETWORK 层（默认）：能拿到 IP+TCP 头
            ports_str = " or ".join(
                f"tcp.DstPort == {p}" for p in _REDIRECT_DST_PORTS
            )
            reverse_str = (
                f"tcp.SrcPort == {self.local_port} and "
                f"ip.SrcAddress == 127.0.0.1 and ip.DstAddress == 127.0.0.1"
            )
            outbound_str = (
                f"({ports_str}) and ip.DstAddress != 127.0.0.1"
            )
            filter_str = f"({outbound_str}) or ({reverse_str})"
            self._divert = pydivert.WinDivert(filter_str)  # type: ignore
            self._divert.open()
            self._running = True
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="transparent-proxy"
            )
            self._thread.start()
            logger.info(
                "transparent", "透明代理已启动",
                f"redirect ports={list(_REDIRECT_DST_PORTS)} -> 127.0.0.1:{self.local_port}",
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
        """
        self._running = False
        if self._divert:
            # 先 shutdown 让 recv() 立即返回 None 或抛异常
            try:
                # pydivert 暴露 WinDivertShutdown，等价于 C API WinDivertShutdown(handle, WINDIVERT_SHUTDOWN_ALL)
                self._divert.shutdown()  # type: ignore
            except Exception:  # noqa: BLE001
                # 旧版 pydivert 无 shutdown 方法时回退到直接 close
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
                # 仅处理 IPv4（IPv6 透明重定向暂不支持，直接放行）
                ip_hdr = packet.ipv4
                if ip_hdr is None or packet.tcp is None:
                    try:
                        self._divert.send(packet)  # type: ignore
                    except Exception:  # noqa: BLE001
                        pass
                    continue

                tcp_hdr = packet.tcp
                src_ip = ip_hdr.src_addr
                dst_ip = ip_hdr.dst_addr
                src_port = tcp_hdr.src_port
                dst_port = tcp_hdr.dst_port

                # 出站：dst port in {80}, 非 loopback（filter 已保证 dst != 127.0.0.1）
                if dst_port in _REDIRECT_DST_PORTS and dst_ip != "127.0.0.1":
                    forward_key = (src_ip, src_port, dst_ip, dst_port)
                    reverse_key = (dst_ip, dst_port, src_ip, src_port)
                    now = time.monotonic()
                    with self._nat_lock:
                        self._nat_table[forward_key] = (
                            src_ip, src_port, dst_ip, dst_port, now
                        )
                        self._nat_reverse[reverse_key] = forward_key
                    # 改写 dst -> 本地代理
                    ip_hdr.dst_addr = _LOCAL_HOST
                    tcp_hdr.dst_port = self.local_port
                    packet.recalc_checksums()
                    self._redirected_count += 1
                # 反向：src port = local_port, src/dst ip = 127.0.0.1
                elif (src_port == self.local_port and src_ip == "127.0.0.1"
                      and dst_ip == "127.0.0.1"):
                    # 反向查找：reverse_key 由原出站包的 (dst_ip, dst_port, src_ip, src_port) 构成
                    # 此时 dst_port 是原客户端源端口
                    reverse_key = (
                        # 反向包的源端口是 local_port（代理端口），但查找需要按原出站包的反向四元组
                        # 原 forward: (src_ip=X, src_port=Y, dst_ip=Z, dst_port=80)
                        # 原 reverse_key = (Z, 80, X, Y)
                        # 反向包的 src=127.0.0.1:local_port, dst=127.0.0.1:Y
                        # 我们不知道 X 和 Z，只能通过 Y 反查
                        # 简化：用 dst_port（客户端原 src_port Y）作为反查主键
                        dst_port,  # Y - 原客户端 src_port
                    )
                    # 改用简化反查：按 dst_port 索引（Y 唯一标识一条连接）
                    with self._nat_lock:
                        entry = self._find_by_client_port(dst_port)
                    if entry:
                        forward_key, (orig_src_ip, orig_src_port,
                                      orig_dst_ip, orig_dst_port, _) = entry
                        # 改写 src -> 原服务器
                        ip_hdr.src_addr = orig_dst_ip
                        tcp_hdr.src_port = orig_dst_port
                        packet.recalc_checksums()
                        self._redirected_count += 1
                        # 反向包命中时刷新 last_seen，避免长连接 60s 后被清理导致隧道断裂
                        now = time.monotonic()
                        # TCP FIN/RST 时清理 NAT 条目
                        flags = tcp_hdr.flags
                        # FIN=0x01, RST=0x04
                        is_close = bool((flags & 0x01) or (flags & 0x04))
                        with self._nat_lock:
                            if is_close:
                                self._nat_table.pop(forward_key, None)
                                # 反向索引也清理（遍历找到对应 key）
                                for rk, fk in list(self._nat_reverse.items()):
                                    if fk == forward_key:
                                        del self._nat_reverse[rk]
                                        break
                            else:
                                # 更新 last_seen（保持长连接 NAT 条目不过期）
                                self._nat_table[forward_key] = (
                                    orig_src_ip, orig_src_port,
                                    orig_dst_ip, orig_dst_port, now,
                                )
                    else:
                        # 无 NAT 条目（已过期或非本代理流量），直接放行
                        pass
                else:
                    # filter 已限定，理论上不会到这里
                    self._passed_count += 1

                try:
                    self._divert.send(packet)  # type: ignore
                except Exception:  # noqa: BLE001
                    pass

                # 周期性清理过期 NAT 条目（每 30s 一次，避免每次包都触发）
                now = time.monotonic()
                if now - self._last_cleanup > 30.0:
                    self._cleanup_nat()
                    self._last_cleanup = now

            except Exception as e:  # noqa: BLE001
                if self._running:
                    logger.warning("transparent", "抓包循环异常", str(e))
                    time.sleep(0.01)

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
        """清理过期 NAT 条目（TTL 60s 未活动）。

        避免 NAT 表无限增长，同时不破坏在途连接（活跃连接会更新 last_seen）。
        """
        now = time.monotonic()
        expired_keys = []
        with self._nat_lock:
            for forward_key, entry in list(self._nat_table.items()):
                last_seen = entry[4]
                if now - last_seen > _NAT_TTL:
                    expired_keys.append(forward_key)
            for fk in expired_keys:
                del self._nat_table[fk]
                # 同步清理反向索引
                for rk, fk2 in list(self._nat_reverse.items()):
                    if fk2 == fk:
                        del self._nat_reverse[rk]
                        break
        if expired_keys:
            logger.info("transparent", "NAT 表清理过期条目",
                        f"清理 {len(expired_keys)} 条，剩余 {len(self._nat_table)}")


# ---------- 单例管理 ----------

_instance: Optional[TransparentProxy] = None
_lock = threading.Lock()


def get_transparent_proxy() -> TransparentProxy:
    """获取透明代理单例（懒初始化）。"""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = TransparentProxy()
    return _instance


def start_transparent_proxy() -> tuple[bool, str]:
    """启动透明代理。返回 (success, msg)。"""
    proxy = get_transparent_proxy()
    if proxy.running:
        return True, "已在运行"
    if proxy.start():
        return True, "透明代理已启动"
    return False, proxy.last_error or "启动失败"


def stop_transparent_proxy() -> tuple[bool, str]:
    """停止透明代理。"""
    proxy = get_transparent_proxy()
    if not proxy.running:
        return True, "未在运行"
    proxy.stop()
    return True, "透明代理已停止"


def transparent_proxy_status() -> dict:
    """返回透明代理状态。"""
    if not IS_WINDOWS:
        return {
            "running": False,
            "supported": False,
            "hint": "透明代理仅 Windows 可用",
        }
    return {
        "supported": True,
        **get_transparent_proxy().status(),
    }


"""Transparent proxy backend - cross-platform Unix implementation (Linux iptables / macOS pf).

Replaces the Windows WinDivert transparent proxy on macOS / Linux:
- Linux: uses iptables NAT REDIRECT rules to redirect outbound TCP 80/443 to the local proxy port
  - Inserts iptables rules on start, deletes them on stop
  - After the proxy server accepts, uses getsockopt(SO_ORIGINAL_DST) to get the original target
  - Advantages: iptables is the standard Linux firewall, excellent performance (kernel-space NAT)
  - Limitations: requires root; only supports TCP (UDP transparent proxy requires TPROXY, which is complex)
- macOS: uses pf (packet filter) rdr rules for redirection
  - On start, writes rules to /etc/pf.anchors/telnix and loads them into pf
  - After the proxy server accepts, uses getsockname to get the local address (pf puts the original target in the local address)
  - Limitations: requires root; macOS pf configuration is relatively complex

Core differences from the Windows version:
- Windows: WinDivert intercepts+rewrites packets at the NETWORK layer, needs to maintain a NAT table for reverse traffic mapping
- Unix: iptables/pf does NAT in kernel space, reverse traffic is handled automatically, userspace only needs to query the original target

Performance notes:
- iptables REDIRECT has excellent performance (kernel-space NAT, zero userspace overhead)
- pf rdr is also very performant on macOS
- Equivalent to the Windows WinDivert interception mode: transparent redirection + original dst query
"""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import threading
from typing import Optional

from .. import logger

IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_UNIX = IS_LINUX or IS_MACOS

# 与 Windows 版本一致的重定向端口
_REDIRECT_DST_PORTS = {80, 443}

# Linux SO_ORIGINAL_DST 常量（用于查询 iptables REDIRECT 后的原目标）
SO_ORIGINAL_DST = 80

# macOS pf anchor 文件路径
_PF_ANCHOR_FILE = "/etc/pf.anchors/telnix"
_PF_ANCHOR_NAME = "telnix"


class UnixTransparentProxy:
    """Unix-platform transparent proxy backend.

    External interface is consistent with the Windows TransparentProxy class:
    - start() / stop() / status()
    - lookup_reverse(client_src_port) - reverse-lookup original target

    Differences from the Windows version:
    - No pydivert; uses iptables/pf for kernel-space NAT
    - No need to maintain a NAT table (kernel handles reverse traffic automatically)
    - lookup_reverse is implemented via getsockopt(SO_ORIGINAL_DST) (Linux)
      or getsockname (macOS)
    """

    def __init__(self, local_port: int = 8888):
        self.local_port = local_port
        self._running = False
        self._last_error: str = ""
        self._thread: threading.Thread | None = None
        # 已添加的 iptables 规则（停止时需要删除）
        self._iptables_rules: list[tuple[str, ...]] = []
        # pf 配置是否已加载
        self._pf_loaded = False
        # 统计（与 Windows 版本字段保持一致）
        self._redirected_count = 0
        self._passed_count = 0

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> str:
        return self._last_error

    def status(self) -> dict:
        """Return transparent proxy status (fields consistent with the Windows version)."""
        return {
            "running": self._running,
            "redirected_count": self._redirected_count,
            "passed_count": self._passed_count,
            "nat_table_size": 0,  # Unix 内核态 NAT，无用户态 NAT 表
            "last_error": self._last_error,
            "local_port": self.local_port,
            "redirect_ports": list(_REDIRECT_DST_PORTS),
            "backend": "iptables" if IS_LINUX else ("pf" if IS_MACOS else "none"),
        }

    def lookup_reverse(self, client_src_port: int) -> Optional[tuple]:
        """Reverse-lookup the original target for the proxy server in raw tunnel mode.

        The Unix implementation has the same interface as Windows, but the query mechanism differs:
        - Linux: after iptables REDIRECT, the accepted socket can use
          getsockopt(SOL_IP, SO_ORIGINAL_DST) to get the original target
        - macOS: after pf rdr, getsockname of the accepted socket returns the original target

        Note: this method on Unix does not query via client_src_port (because no NAT table is maintained),
        but requires the caller to pass an already-accepted socket fd.
        For interface compatibility, returns None here; actual query is done via lookup_original_dst(sock).
        """
        # Windows 版本通过 NAT 表反查，Unix 版本通过 socket 选项查询
        # 此方法保留接口兼容性，但 Unix 上应使用 lookup_original_dst
        return None

    def lookup_original_dst(self, sock: socket.socket) -> Optional[tuple[str, int]]:
        """Query the original target address before iptables/pf redirection.

        Linux: getsockopt(SOL_IP, SO_ORIGINAL_DST)
        macOS: getsockname (pf rdr puts the original target into the socket's local address)

        Returns: (orig_dst_ip, orig_dst_port) or None (query failed)
        """
        if IS_LINUX:
            return self._lookup_original_dst_linux(sock)
        elif IS_MACOS:
            return self._lookup_original_dst_macos(sock)
        return None

    def _lookup_original_dst_linux(self, sock: socket.socket) -> Optional[tuple[str, int]]:
        """Linux: query original target via SO_ORIGINAL_DST."""
        try:
            # SO_ORIGINAL_DST 返回 sockaddr_in 结构：family(2) + port(2) + addr(4) + padding(8)
            data = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
            if len(data) < 8:
                return None
            # 解析端口（网络字节序）和 IP
            port = struct.unpack("!H", data[2:4])[0]
            ip = socket.inet_ntoa(data[4:8])
            if port == 0 or ip == "0.0.0.0":
                return None
            return ip, port
        except OSError as e:
            logger.error("transparent", "SO_ORIGINAL_DST lookup failed", str(e))
            return None

    def _lookup_original_dst_macos(self, sock: socket.socket) -> Optional[tuple[str, int]]:
        """macOS: after pf rdr, getsockname returns the original target."""
        try:
            # pf rdr 把原目标地址放到 socket 的本地地址
            # accept 后 getsockname 返回的是原目标，不是 127.0.0.1:8888
            ip, port = sock.getsockname()
            if port == self.local_port and ip in ("127.0.0.1", "0.0.0.0"):
                # 没有被 pf rdr 处理（直连到代理端口）
                return None
            return ip, port
        except OSError as e:
            logger.error("transparent", "macOS getsockname lookup failed", str(e))
            return None

    def _is_admin(self) -> bool:
        """Check root privileges."""
        try:
            return os.geteuid() == 0
        except AttributeError:
            return False

    def start(self) -> bool:
        """Start the transparent proxy."""
        if not IS_UNIX:
            self._last_error = "UnixTransparentProxy only supports Linux/macOS"
            return False
        if self._running:
            return True
        if not self._is_admin():
            self._last_error = "Root privileges required (iptables/pf requires root)"
            logger.error("transparent", "Unix transparent proxy requires root", "Please start Telnix with sudo")
            return False
        try:
            if IS_LINUX:
                ok = self._start_linux()
            else:
                ok = self._start_macos()
            if not ok:
                return False
            self._running = True
            # 启动一个监控线程（保持与 Windows 版本接口一致，实际无工作）
            self._thread = threading.Thread(target=self._monitor_loop, daemon=True,
                                            name="transparent-unix")
            self._thread.start()
            backend = "iptables" if IS_LINUX else "pf"
            logger.info("transparent", f"Transparent proxy started ({backend})",
                        f"redirect ports={list(_REDIRECT_DST_PORTS)} -> 127.0.0.1:{self.local_port}")
            return True
        except Exception as e:  # noqa: BLE001
            self._last_error = str(e)
            logger.error("transparent", "Unix transparent proxy start failed", str(e))
            self._cleanup_rules()
            return False

    def _start_linux(self) -> bool:
        """Linux: use iptables NAT REDIRECT to redirect 80/443 to the local proxy port."""
        self._iptables_rules = []
        # 检查 iptables 是否可用
        try:
            subprocess.run(["iptables", "--version"], capture_output=True, check=True, timeout=5)
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            self._last_error = f"iptables unavailable: {e} (please install the iptables package)"
            return False

        # 添加 OUTPUT 链 REDIRECT 规则（仅本机出站流量）
        # 排除本地回环和 Telnix 自身端口
        for port in _REDIRECT_DST_PORTS:
            # -t nat: NAT 表
            # -A OUTPUT: 添加到 OUTPUT 链（本机出站）
            # -p tcp: 仅 TCP
            # --dport <port>: 目的端口
            # ! -d 127.0.0.0/8: 排除本地回环
            # -j REDIRECT --to-port <local_port>: 重定向到本地代理端口
            rule = (
                "iptables", "-t", "nat", "-A", "OUTPUT",
                "-p", "tcp",
                "--dport", str(port),
                "!", "-d", "127.0.0.0/8",
                "-j", "REDIRECT",
                "--to-port", str(self.local_port),
            )
            try:
                result = subprocess.run(rule, capture_output=True, timeout=5)
                if result.returncode != 0:
                    self._last_error = f"iptables failed to add rule: {result.stderr.decode('utf-8', errors='replace')}"
                    self._cleanup_rules()
                    return False
                self._iptables_rules.append(rule)
            except subprocess.TimeoutExpired:
                self._last_error = f"iptables timed out adding rule (port={port})"
                self._cleanup_rules()
                return False
        return True

    def _start_macos(self) -> bool:
        """macOS: use pf rdr rules to redirect 80/443 to the local proxy port."""
        # 检查 pfctl 是否可用
        try:
            subprocess.run(["pfctl", "-s", "info"], capture_output=True, check=True, timeout=5)
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            self._last_error = f"pfctl unavailable: {e} (macOS should ship with pf)"
            return False

        # 生成 pf 规则文件
        # rdr pass on lo0 proto tcp from any to any port {80, 443} -> 127.0.0.1 port 8888
        ports_str = ", ".join(str(p) for p in sorted(_REDIRECT_DST_PORTS))
        pf_rules = (
            f"# Telnix transparent proxy rules (auto-generated, do not edit manually)\n"
            f"rdr pass on lo0 proto tcp from any to any port {{ {ports_str} }} -> 127.0.0.1 port {self.local_port}\n"
        )

        # 写入 anchor 文件
        try:
            with open(_PF_ANCHOR_FILE, "w", encoding="utf-8") as f:
                f.write(pf_rules)
        except OSError as e:
            self._last_error = f"Failed to write pf anchor file: {e} (need root to write /etc/pf.anchors/)"
            return False

        # 加载 anchor 到 pf
        # pfctl -a telnix -f /etc/pf.anchors/telnix
        try:
            result = subprocess.run(
                ["pfctl", "-a", _PF_ANCHOR_NAME, "-f", _PF_ANCHOR_FILE],
                capture_output=True, timeout=5,
            )
            if result.returncode != 0:
                self._last_error = f"pfctl failed to load anchor: {result.stderr.decode('utf-8', errors='replace')}"
                self._cleanup_rules()
                return False
        except subprocess.TimeoutExpired:
            self._last_error = "pfctl timed out loading anchor"
            self._cleanup_rules()
            return False

        # 把 anchor 加入 pf 主配置
        # echo "anchor \"telnix\"" | pfctl -ef -
        # 但这样会覆盖现有 pf 配置，不安全
        # 更好的方法：用 pfctl -a telnix -f 加载 anchor，然后确保主配置引用了该 anchor
        # macOS 默认 pf.conf 不引用我们的 anchor，需要用户手动添加或我们用其他方式
        # 简化实现：直接用 pfctl -f 加载完整规则（仅 Telnix 的规则）
        # 这样会覆盖用户现有 pf 配置，不安全
        # 最佳实践：用 anchor，但需要确保主配置引用
        # 这里采用折中方案：检查主配置是否引用 anchor，没有则提示用户手动添加
        try:
            result = subprocess.run(
                ["pfctl", "-s", "rules"],
                capture_output=True, timeout=5,
            )
            rules_output = result.stdout.decode("utf-8", errors="replace")
            if f'anchor "{_PF_ANCHOR_NAME}"' not in rules_output:
                # 主配置未引用 anchor，提示用户手动添加
                self._last_error = (
                    f"pf main config does not reference anchor '{_PF_ANCHOR_NAME}'. "
                    f"Please add a line to /etc/pf.conf: anchor \"{_PF_ANCHOR_NAME}\", "
                    f"then run: sudo pfctl -f /etc/pf.conf"
                )
                self._cleanup_rules()
                return False
        except subprocess.TimeoutExpired:
            self._last_error = "pfctl timed out querying rules"
            self._cleanup_rules()
            return False

        self._pf_loaded = True
        return True

    def _monitor_loop(self):
        """Monitor loop (kept for interface consistency with the Windows version, actually does no work).

        The Windows version maintains NAT table cleanup here; the Unix version's NAT is in kernel space and needs no cleanup.
        """
        while self._running:
            # 仅做周期性统计输出
            import time
            time.sleep(30)

    def stop(self):
        """Stop the transparent proxy and clean up iptables/pf rules."""
        self._running = False
        self._cleanup_rules()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        backend = "iptables" if IS_LINUX else "pf"
        logger.info("transparent", f"Transparent proxy stopped ({backend})")

    def _cleanup_rules(self):
        """Clean up the iptables/pf rules that were added."""
        # Linux: 删除 iptables 规则（用 -D 替代 -A）
        for rule in self._iptables_rules:
            # 把 -A 改为 -D
            del_rule = list(rule)
            if "-A" in del_rule:
                idx = del_rule.index("-A")
                del_rule[idx] = "-D"
            try:
                subprocess.run(del_rule, capture_output=True, timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass  # 清理失败不阻塞停止流程
        self._iptables_rules.clear()

        # macOS: 卸载 pf anchor
        if self._pf_loaded:
            try:
                subprocess.run(
                    ["pfctl", "-a", _PF_ANCHOR_NAME, "-d"],
                    capture_output=True, timeout=5,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass
            self._pf_loaded = False

        # 删除 anchor 文件
        try:
            if os.path.exists(_PF_ANCHOR_FILE):
                os.unlink(_PF_ANCHOR_FILE)
        except OSError:
            pass


def is_unix_transparent_proxy_available() -> bool:
    """Check whether the Unix transparent proxy backend is available."""
    if not IS_UNIX:
        return False
    # 检查所需工具是否可用
    try:
        if IS_LINUX:
            subprocess.run(["iptables", "--version"], capture_output=True, check=True, timeout=5)
        else:
            subprocess.run(["pfctl", "-s", "info"], capture_output=True, check=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def get_unix_transparent_proxy_status() -> dict:
    """Return the Unix transparent proxy backend status."""
    try:
        is_admin = os.geteuid() == 0
    except AttributeError:
        is_admin = False
    return {
        "running": False,
        "supported": True,
        "backend": "iptables" if IS_LINUX else ("pf" if IS_MACOS else "none"),
        "is_admin": is_admin,
        "hint": ("Ready" if is_admin else "Root privileges required") if IS_UNIX else "Unsupported platform",
    }

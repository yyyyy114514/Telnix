"""透明代理后端 - Unix 跨平台实现（Linux iptables / macOS pf）。

在 macOS / Linux 上替代 Windows 的 WinDivert 透明代理：
- Linux: 用 iptables NAT REDIRECT 规则把出站 TCP 80/443 重定向到本地代理端口
  - 启动时插入 iptables 规则，停止时删除
  - 代理服务器 accept 后用 getsockopt(SO_ORIGINAL_DST) 获取原目标
  - 优势：iptables 是 Linux 标准防火墙，性能优秀（内核态 NAT）
  - 限制：需要 root；仅支持 TCP（UDP 透明代理需要 TPROXY，复杂度高）
- macOS: 用 pf (packet filter) rdr 规则做重定向
  - 启动时把规则写入 /etc/pf.anchors/telnix 并加载到 pf
  - 代理服务器 accept 后用 getsockname 获取本地地址（pf 把原目标放在本地地址）
  - 限制：需要 root；macOS pf 配置较复杂

与 Windows 版本的核心差异：
- Windows: WinDivert 在 NETWORK 层拦截+改写包，需要维护 NAT 表做反向流量映射
- Unix: iptables/pf 在内核态做 NAT，反向流量自动处理，用户态只需查询原目标

性能说明：
- iptables REDIRECT 性能优秀（内核态 NAT，零用户态开销）
- pf rdr 在 macOS 上性能也很好
- 与 Windows WinDivert 拦截模式等价：透明重定向 + 原 dst 查询
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
    """Unix 平台透明代理后端。

    对外接口与 Windows 的 TransparentProxy 类保持一致：
    - start() / stop() / status()
    - lookup_reverse(client_src_port) - 反查原目标

    与 Windows 版本的差异：
    - 没有 pydivert，用 iptables/pf 在内核态做 NAT
    - 不需要维护 NAT 表（内核自动处理反向流量）
    - lookup_reverse 通过 getsockopt(SO_ORIGINAL_DST) 查询（Linux）
      或 getsockname（macOS）实现
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
        """返回透明代理状态（与 Windows 版本字段保持一致）。"""
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
        """供代理服务器在 raw tunnel 模式下反查原目标。

        Unix 实现的接口与 Windows 一致，但查询机制不同：
        - Linux: 通过 iptables REDIRECT 后，accept 的 socket 可用
          getsockopt(SOL_IP, SO_ORIGINAL_DST) 获取原目标
        - macOS: pf rdr 后，accept 的 socket 的 getsockname 返回原目标

        注意：此方法在 Unix 上不通过 client_src_port 查询（因为不维护 NAT 表），
        而是要求调用方传入已 accept 的 socket fd。
        为保持接口兼容，这里返回 None，实际查询通过 lookup_original_dst(sock) 完成。
        """
        # Windows 版本通过 NAT 表反查，Unix 版本通过 socket 选项查询
        # 此方法保留接口兼容性，但 Unix 上应使用 lookup_original_dst
        return None

    def lookup_original_dst(self, sock: socket.socket) -> Optional[tuple[str, int]]:
        """查询 iptables/pf 重定向前的原目标地址。

        Linux: getsockopt(SOL_IP, SO_ORIGINAL_DST)
        macOS: getsockname（pf rdr 把原目标放到 socket 的本地地址）

        返回：(orig_dst_ip, orig_dst_port) 或 None（查询失败）
        """
        if IS_LINUX:
            return self._lookup_original_dst_linux(sock)
        elif IS_MACOS:
            return self._lookup_original_dst_macos(sock)
        return None

    def _lookup_original_dst_linux(self, sock: socket.socket) -> Optional[tuple[str, int]]:
        """Linux: 用 SO_ORIGINAL_DST 查询原目标。"""
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
            logger.error("transparent", "SO_ORIGINAL_DST 查询失败", str(e))
            return None

    def _lookup_original_dst_macos(self, sock: socket.socket) -> Optional[tuple[str, int]]:
        """macOS: pf rdr 后 getsockname 返回原目标。"""
        try:
            # pf rdr 把原目标地址放到 socket 的本地地址
            # accept 后 getsockname 返回的是原目标，不是 127.0.0.1:8888
            ip, port = sock.getsockname()
            if port == self.local_port and ip in ("127.0.0.1", "0.0.0.0"):
                # 没有被 pf rdr 处理（直连到代理端口）
                return None
            return ip, port
        except OSError as e:
            logger.error("transparent", "macOS getsockname 查询失败", str(e))
            return None

    def _is_admin(self) -> bool:
        """检查 root 权限。"""
        try:
            return os.geteuid() == 0
        except AttributeError:
            return False

    def start(self) -> bool:
        """启动透明代理。"""
        if not IS_UNIX:
            self._last_error = "UnixTransparentProxy 仅支持 Linux/macOS"
            return False
        if self._running:
            return True
        if not self._is_admin():
            self._last_error = "需要 root 权限（iptables/pf 需要 root）"
            logger.error("transparent", "Unix 透明代理需要 root", "请用 sudo 启动 Telnix")
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
            logger.info("transparent", f"透明代理已启动 ({backend})",
                        f"redirect ports={list(_REDIRECT_DST_PORTS)} -> 127.0.0.1:{self.local_port}")
            return True
        except Exception as e:  # noqa: BLE001
            self._last_error = str(e)
            logger.error("transparent", "Unix 透明代理启动失败", str(e))
            self._cleanup_rules()
            return False

    def _start_linux(self) -> bool:
        """Linux: 用 iptables NAT REDIRECT 重定向 80/443 到本地代理端口。"""
        self._iptables_rules = []
        # 检查 iptables 是否可用
        try:
            subprocess.run(["iptables", "--version"], capture_output=True, check=True, timeout=5)
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            self._last_error = f"iptables 不可用: {e}（请安装 iptables 包）"
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
                    self._last_error = f"iptables 添加规则失败: {result.stderr.decode('utf-8', errors='replace')}"
                    self._cleanup_rules()
                    return False
                self._iptables_rules.append(rule)
            except subprocess.TimeoutExpired:
                self._last_error = f"iptables 添加规则超时（port={port}）"
                self._cleanup_rules()
                return False
        return True

    def _start_macos(self) -> bool:
        """macOS: 用 pf rdr 规则重定向 80/443 到本地代理端口。"""
        # 检查 pfctl 是否可用
        try:
            subprocess.run(["pfctl", "-s", "info"], capture_output=True, check=True, timeout=5)
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            self._last_error = f"pfctl 不可用: {e}（macOS 应自带 pf）"
            return False

        # 生成 pf 规则文件
        # rdr pass on lo0 proto tcp from any to any port {80, 443} -> 127.0.0.1 port 8888
        ports_str = ", ".join(str(p) for p in sorted(_REDIRECT_DST_PORTS))
        pf_rules = (
            f"# Telnix 透明代理规则（自动生成，请勿手动编辑）\n"
            f"rdr pass on lo0 proto tcp from any to any port {{ {ports_str} }} -> 127.0.0.1 port {self.local_port}\n"
        )

        # 写入 anchor 文件
        try:
            with open(_PF_ANCHOR_FILE, "w", encoding="utf-8") as f:
                f.write(pf_rules)
        except OSError as e:
            self._last_error = f"写入 pf anchor 文件失败: {e}（需要 root 写 /etc/pf.anchors/）"
            return False

        # 加载 anchor 到 pf
        # pfctl -a telnix -f /etc/pf.anchors/telnix
        try:
            result = subprocess.run(
                ["pfctl", "-a", _PF_ANCHOR_NAME, "-f", _PF_ANCHOR_FILE],
                capture_output=True, timeout=5,
            )
            if result.returncode != 0:
                self._last_error = f"pfctl 加载 anchor 失败: {result.stderr.decode('utf-8', errors='replace')}"
                self._cleanup_rules()
                return False
        except subprocess.TimeoutExpired:
            self._last_error = "pfctl 加载 anchor 超时"
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
                    f"pf 主配置未引用 anchor '{_PF_ANCHOR_NAME}'。"
                    f"请在 /etc/pf.conf 中添加一行：anchor \"{_PF_ANCHOR_NAME}\"，"
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

    def _monitor_loop(self):
        """监控循环（保持与 Windows 版本接口一致，实际无工作）。

        Windows 版本在这里维护 NAT 表清理，Unix 版本 NAT 在内核态，无需清理。
        """
        while self._running:
            # 仅做周期性统计输出
            import time
            time.sleep(30)

    def stop(self):
        """停止透明代理，清理 iptables/pf 规则。"""
        self._running = False
        self._cleanup_rules()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        backend = "iptables" if IS_LINUX else "pf"
        logger.info("transparent", f"透明代理已停止 ({backend})")

    def _cleanup_rules(self):
        """清理已添加的 iptables/pf 规则。"""
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
    """检查 Unix 透明代理后端是否可用。"""
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
    """返回 Unix 透明代理后端状态。"""
    try:
        is_admin = os.geteuid() == 0
    except AttributeError:
        is_admin = False
    return {
        "running": False,
        "supported": True,
        "backend": "iptables" if IS_LINUX else ("pf" if IS_MACOS else "none"),
        "is_admin": is_admin,
        "hint": ("就绪" if is_admin else "需要 root 权限") if IS_UNIX else "不支持的平台",
    }

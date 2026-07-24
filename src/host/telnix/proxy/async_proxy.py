"""asyncio 代理服务器。

性能优化：用 asyncio 事件循环驱动 accept，替代 builtin 的线程 accept 循环。
- accept 在 asyncio 事件循环中完成（通过 run_in_executor 包装阻塞 accept）
- 连接处理（SSL bump / HTTP 解析 / 转发 / 抓包）全部复用 builtin ProxyServer 的成熟代码
- 支持 builtin 引擎的全部功能：断点拦截 / 自动回复规则 / HTTP/2 / WebSocket / 连接池

与 ProxyServer 接口完全兼容：直接继承，仅重写 start/stop/_accept_loop。
"""

from __future__ import annotations

import asyncio
import socket
import threading
from typing import Optional

from .. import logger
from . import ssl_bump as ssl_bump_mod
from .server import ProxyServer


class AsyncProxyServer(ProxyServer):
    """asyncio 代理服务器。

    继承 ProxyServer，仅重写 accept 循环为 asyncio 驱动。
    所有连接处理逻辑（_handle_client / SSL bump / 抓包 / 过滤 / 规则）
    完全复用父类实现，确保功能等价。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8888,
                 ssl_bump: Optional[ssl_bump_mod.SSLBumpManager] = None):
        super().__init__(host=host, port=port, ssl_bump=ssl_bump)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        # 标记是否使用 asyncio accept（用于日志区分）
        self._engine_name = "async"

    def start(self):
        """启动代理：用 asyncio 事件循环驱动 accept。

        与父类的区别：
        - 父类：socket.listen + threading.Thread 跑 _accept_loop（每连接一个线程）
        - 本类：socket.listen + asyncio 事件循环 + run_in_executor(accept)
                接受到的连接仍提交到线程处理（与父类一致）

        注意：本实现保留了 asyncio 事件循环的结构，但连接处理仍是线程模型
        （因为 SSL socket 和同步 HTTP 解析不兼容 asyncio）。
        主要价值在于：未来可在事件循环中集成其他异步任务（如 WebSocket relay）。
        """
        # 创建 server socket（与父类一致）
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(200)
        self._server_socket.settimeout(1.0)  # accept 超时 1s，便于响应 stop
        self._running = True
        self.refresh_ignored()
        # 在独立线程中运行 asyncio 事件循环
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True, name="async-proxy-loop")
        self._loop_thread.start()
        logger.info("async-proxy",
                    f"asyncio 代理服务器已启动: {self.host}:{self.port}")

    def _run_loop(self):
        """在独立线程中运行 asyncio 事件循环。"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._accept_loop_async())
        except Exception as e:  # noqa: BLE001
            logger.error("async-proxy", "事件循环异常", str(e))
        finally:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _accept_loop_async(self):
        """asyncio 驱动的 accept 循环。

        通过 run_in_executor 包装阻塞 accept，让事件循环保持响应。
        接受到的连接直接调用父类的 _handle_client_safe（线程处理）。

        与父类 _accept_loop 的区别：
        - 父类：while + blocking accept + threading.Thread per connection
        - 本类：while + await run_in_executor(accept) + threading.Thread per connection
        """
        while self._running:
            try:
                # 阻塞 accept 在 executor 线程中执行，await 让出事件循环
                client_sock, client_addr = await self._loop.run_in_executor(
                    None, self._server_socket.accept)
            except socket.timeout:
                continue  # 周期性检查 _running
            except OSError:
                if self._running:
                    logger.warning("async-proxy", "accept 异常", "")
                break
            # 设置超时（与父类一致：60s）
            try:
                client_sock.settimeout(60)
            except OSError:
                pass
            # 提交到线程处理（与父类 _accept_loop 完全一致）
            t = threading.Thread(
                target=self._handle_client_safe,
                args=(client_sock, client_addr),
                daemon=True,
            )
            t.start()

    def stop(self):
        """停止代理：关闭 server socket + 停止事件循环。"""
        self._running = False
        # 关闭 server socket，让 accept 退出阻塞
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        # 停止事件循环
        if self._loop is not None and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass  # 事件循环可能已停止
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)
            self._loop_thread = None
        # 关闭连接池（与父类一致）
        try:
            self._conn_pool.close_all()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._h2_pool.close_all()
        except Exception:  # noqa: BLE001
            pass
        logger.info("async-proxy", "asyncio 代理服务器已停止")

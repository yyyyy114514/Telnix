"""asyncio proxy server.

Performance optimization: use asyncio event loop to drive accept, replacing builtin's thread accept loop.
- accept is done in asyncio event loop (via run_in_executor wrapping blocking accept)
- Connection handling (SSL bump / HTTP parsing / forwarding / packet capture) all reuse builtin ProxyServer's mature code
- Supports all builtin engine features: breakpoint interception / auto-reply rules / HTTP/2 / WebSocket / connection pool

Fully compatible with ProxyServer interface: direct inheritance, only overrides start/stop/_accept_loop.
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
    """asyncio proxy server.

    Inherits ProxyServer, only overrides accept loop to be asyncio-driven.
    All connection handling logic (_handle_client / SSL bump / capture / filtering / rules)
    fully reuses parent class implementation, ensuring functional equivalence.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8888,
                 ssl_bump: Optional[ssl_bump_mod.SSLBumpManager] = None):
        super().__init__(host=host, port=port, ssl_bump=ssl_bump)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        # 标记是否使用 asyncio accept（用于日志区分）
        self._engine_name = "async"

    def start(self):
        """Start proxy: use asyncio event loop to drive accept.

        Differences from parent class:
        - Parent: socket.listen + threading.Thread runs _accept_loop (one thread per connection)
        - This class: socket.listen + asyncio event loop + run_in_executor(accept)
                Accepted connections are still submitted to thread handling (same as parent)

        Note: This implementation retains the asyncio event loop structure, but connection handling is still thread-based
        (because SSL sockets and synchronous HTTP parsing are incompatible with asyncio).
        Main value: other async tasks (such as WebSocket relay) can be integrated in the event loop in the future.
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
                    f"asyncio proxy server started: {self.host}:{self.port}")

    def _run_loop(self):
        """Run asyncio event loop in a separate thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._accept_loop_async())
        except Exception as e:  # noqa: BLE001
            logger.error("async-proxy", "Event loop exception", str(e))
        finally:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _accept_loop_async(self):
        """asyncio-driven accept loop.

        Wraps blocking accept via run_in_executor to keep event loop responsive.
        Accepted connections directly call parent class _handle_client_safe (thread handling).

        Differences from parent class _accept_loop:
        - Parent: while + blocking accept + threading.Thread per connection
        - This class: while + await run_in_executor(accept) + threading.Thread per connection
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
                    logger.warning("async-proxy", "accept exception", "")
                break
            # 设置超时（与父类一致：60s）
            try:
                client_sock.settimeout(60)
            except OSError:
                pass
            # 获取信号量，限制最大并发连接数（与父类一致）
            if not self._client_sem.acquire(timeout=5):
                try:
                    client_sock.close()
                except OSError:
                    pass
                continue
            # 提交到线程处理（与父类 _accept_loop 完全一致）
            t = threading.Thread(
                target=self._handle_client_safe_with_sem,
                args=(client_sock, client_addr),
                daemon=True,
            )
            t.start()

    def stop(self):
        """Stop proxy: close server socket + stop event loop."""
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
        logger.info("async-proxy", "asyncio proxy server stopped")

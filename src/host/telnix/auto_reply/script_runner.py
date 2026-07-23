"""Python 脚本自动修改 - 主进程管理器。

管理 worker 子进程的生命周期：
- 启动：把规则脚本写入临时文件，spawn `python -m telnix.auto_reply.script_worker <tmp>`
- 调用：通过 stdin/stdout JSON 行协议同步调用 on_request/on_response
- 超时：单次调用超过 TIMEOUT 秒，杀 worker 重启
- 重启：worker 崩溃或退出后下次调用自动重启
- 重新加载：规则脚本变更后调用 reload() 重启 worker

设计要点：
- 一个规则对应一个 ScriptRunner 实例（各自独立 worker，脚本互不影响）
- 同步调用（proxy 在请求处理线程中直接 call，不并发）
- worker stdout 严格逐行 JSON，stderr 写入日志文件供排查
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import Optional

from .. import logger
from ..config import get_data_dir


# 单次调用 worker 的超时（秒）。超时杀 worker 重启。
CALL_TIMEOUT = 5.0

# worker 启动超时（秒）：启动后应立即写一行 {"ready": true}
STARTUP_TIMEOUT = 3.0

# 全局锁：每个 runner 实例自己的锁（避免多线程同时调用同一 worker）
# 同一规则的多次调用串行，不同规则可并行（各自独立 worker）


class ScriptRunner:
    """单条脚本规则的 worker 管理器。

    一个 ScriptRunner 实例对应一条 action=script 的规则。
    proxy 在匹配到该规则时，调用 run_request / run_response。
    """

    def __init__(self, rule_id: str, script: str):
        self.rule_id = rule_id
        self.script = script
        self._proc: Optional[subprocess.Popen] = None
        self._script_path: Optional[str] = None
        self._lock = threading.Lock()
        self._stderr_log: Optional[str] = None
        self._last_error: Optional[str] = None  # 最近的脚本错误（供 UI 展示）

    def reload(self, script: str):
        """更新脚本内容并重启 worker。"""
        with self._lock:
            self.script = script
            self._kill_worker_locked()
            self._last_error = None

    def run_request(self, ctx: dict) -> Optional[dict]:
        """调用 on_request。返回 worker 响应 dict 或 None（失败）。"""
        return self._call({"type": "request", "ctx": ctx})

    def run_response(self, ctx: dict) -> Optional[dict]:
        """调用 on_response。返回 worker 响应 dict 或 None（失败）。"""
        return self._call({"type": "response", "ctx": ctx})

    def get_last_error(self) -> Optional[str]:
        return self._last_error

    def stop(self):
        """停止 worker（规则删除/禁用时调用）。"""
        with self._lock:
            self._kill_worker_locked()

    # ---------- 内部 ----------

    def _call(self, req: dict) -> Optional[dict]:
        """同步调用 worker：写一行 JSON，读一行响应，超时控制。"""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                # worker 未启动或已退出，重启
                if not self._start_worker_locked():
                    return None
            assert self._proc is not None
            assert self._proc.stdin is not None
            assert self._proc.stdout is not None

            line = json.dumps(req, ensure_ascii=False) + "\n"
            try:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self._last_error = f"写入 worker 失败: {e}"
                self._kill_worker_locked()
                return None

            # 读响应（带超时）
            # subprocess 的 stdout 是阻塞 file，无法直接 timeout。
            # 用线程读取 + Event 等待的方式实现超时。
            return self._read_response_with_timeout()

    def _read_response_with_timeout(self) -> Optional[dict]:
        """从 worker stdout 读一行 JSON，超时杀进程。"""
        assert self._proc is not None
        assert self._proc.stdout is not None

        result: list[Optional[str]] = [None]
        done = threading.Event()

        def _reader():
            try:
                result[0] = self._proc.stdout.readline()  # type: ignore
            except Exception:  # noqa: BLE001
                result[0] = None
            finally:
                done.set()

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        if not done.wait(CALL_TIMEOUT):
            # 超时
            self._last_error = f"脚本执行超过 {CALL_TIMEOUT}s 超时"
            self._kill_worker_locked()
            return None

        line = result[0]
        if not line:
            # worker 已退出
            self._last_error = "worker 进程意外退出"
            self._kill_worker_locked()
            return None

        try:
            resp = json.loads(line)
        except json.JSONDecodeError as e:
            self._last_error = f"worker 响应 JSON 解析失败: {e}"
            self._kill_worker_locked()
            return None

        if resp.get("error"):
            self._last_error = resp["error"]
            logger.warning(
                "script", f"脚本规则 {self.rule_id} 执行错误",
                resp.get("error", "") + "\n" + resp.get("traceback", ""),
            )
        return resp

    def _start_worker_locked(self) -> bool:
        """启动 worker 子进程。调用方需持锁。"""
        # 写脚本到临时文件
        if self._script_path and os.path.exists(self._script_path):
            try:
                os.remove(self._script_path)
            except OSError:
                pass
        scripts_dir = os.path.join(get_data_dir(), "scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        fd, self._script_path = tempfile.mkstemp(
            suffix=".py", prefix=f"rule_{self.rule_id}_", dir=scripts_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.script)
        except OSError as e:
            self._last_error = f"脚本写入失败: {e}"
            return False

        # stderr 日志文件（追加，便于排查脚本崩溃）
        log_path = os.path.join(scripts_dir, f"rule_{self.rule_id}.stderr.log")
        self._stderr_log = log_path
        try:
            stderr_fp = open(log_path, "ab", buffering=0)
        except OSError:
            stderr_fp = subprocess.PIPE

        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "telnix.auto_reply.script_worker", self._script_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_fp,
                # 文本模式，按行读写
                text=True,
                encoding="utf-8",
                bufsize=1,  # 行缓冲
                # 避免继承 proxy 的 socket handle
                close_fds=True,
            )
        except OSError as e:
            self._last_error = f"启动 worker 失败: {e}"
            return False

        # 等待 ready 信号
        if not self._wait_ready():
            return False
        return True

    def _wait_ready(self) -> bool:
        """等待 worker 启动信号 {"ready": true} 或错误。"""
        assert self._proc is not None
        assert self._proc.stdout is not None

        result: list[Optional[str]] = [None]
        done = threading.Event()

        def _reader():
            try:
                result[0] = self._proc.stdout.readline()  # type: ignore
            except Exception:  # noqa: BLE001
                result[0] = None
            finally:
                done.set()

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        if not done.wait(STARTUP_TIMEOUT):
            self._last_error = f"worker 启动超过 {STARTUP_TIMEOUT}s"
            self._kill_worker_locked()
            return False

        line = result[0]
        if not line:
            self._last_error = "worker 启动时退出（脚本加载失败？查看 stderr 日志）"
            self._kill_worker_locked()
            return False

        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            self._last_error = f"worker 启动响应解析失败: {e}"
            self._kill_worker_locked()
            return False

        if msg.get("error"):
            self._last_error = msg["error"]
            logger.warning(
                "script", f"脚本规则 {self.rule_id} 加载失败",
                msg.get("error", "") + "\n" + msg.get("traceback", ""),
            )
            self._kill_worker_locked()
            return False

        if not msg.get("ready"):
            self._last_error = f"worker 启动响应异常: {msg}"
            self._kill_worker_locked()
            return False

        self._last_error = None
        return True

    def _kill_worker_locked(self):
        """杀掉 worker 进程并清理。调用方需持锁。"""
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=0.5)
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._proc = None


# ---------- 全局 runner 注册表 ----------
# rule_id -> ScriptRunner，proxy 在请求处理时按 rule_id 查找
_runners: dict[str, ScriptRunner] = {}
_runners_lock = threading.Lock()


def get_runner(rule_id: str, script: str) -> ScriptRunner:
    """获取（或创建）某条脚本规则的 runner。

    如果已有 runner 但脚本内容变了，自动 reload。
    """
    with _runners_lock:
        r = _runners.get(rule_id)
        if r is None:
            r = ScriptRunner(rule_id, script)
            _runners[rule_id] = r
        elif r.script != script:
            r.reload(script)
        return r


def remove_runner(rule_id: str):
    """规则删除/禁用时调用，停止并清理 runner。"""
    with _runners_lock:
        r = _runners.pop(rule_id, None)
    if r is not None:
        r.stop()


def reload_runner(rule_id: str, script: str):
    """规则更新时调用，重启 worker 加载新脚本。"""
    with _runners_lock:
        r = _runners.get(rule_id)
        if r is None:
            r = ScriptRunner(rule_id, script)
            _runners[rule_id] = r
        else:
            r.reload(script)


def stop_all():
    """进程退出时调用，清理所有 worker。"""
    with _runners_lock:
        runners = list(_runners.values())
        _runners.clear()
    for r in runners:
        r.stop()


def get_runner_error(rule_id: str) -> Optional[str]:
    """获取某条脚本规则的最近错误（供 UI 展示）。"""
    with _runners_lock:
        r = _runners.get(rule_id)
        return r.get_last_error() if r else None


# ---------- 便捷调用入口 ----------
# proxy server 调用这两个函数即可，无需关心 runner 管理

def call_script_request(rule_id: str, script: str, ctx: dict) -> Optional[dict]:
    """请求阶段调用脚本。返回 worker 响应或 None（脚本不可用）。"""
    try:
        runner = get_runner(rule_id, script)
        return runner.run_request(ctx)
    except Exception as e:  # noqa: BLE001
        logger.warning("script", f"调用脚本 on_request 异常 rule={rule_id}", str(e))
        return None


def call_script_response(rule_id: str, script: str, ctx: dict) -> Optional[dict]:
    """响应阶段调用脚本。返回 worker 响应或 None。"""
    try:
        runner = get_runner(rule_id, script)
        return runner.run_response(ctx)
    except Exception as e:  # noqa: BLE001
        logger.warning("script", f"调用脚本 on_response 异常 rule={rule_id}", str(e))
        return None


def _b64encode(b: bytes) -> str:
    if not b:
        return ""
    return base64.b64encode(b).decode("ascii")


def build_ctx(
    *,
    host: str,
    path: str,
    method: str,
    url: str,
    scheme: str,
    pid: Optional[int],
    process_name: str,
    session_id: Optional[int],
    request_headers: dict,
    request_body: bytes,
    # 响应阶段独有
    status_code: Optional[int] = None,
    response_headers: Optional[dict] = None,
    response_body: Optional[bytes] = None,
) -> dict:
    """构造传给 worker 的 ctx dict。"""
    ctx = {
        "host": host,
        "path": path,
        "method": method,
        "url": url,
        "scheme": scheme,
        "pid": pid,
        "process_name": process_name or "",
        "session_id": session_id,
        "request_headers": {k: str(v) for k, v in (request_headers or {}).items()},
        "request_body_b64": _b64encode(request_body or b""),
    }
    if status_code is not None:
        ctx["status_code"] = status_code
        ctx["response_headers"] = {k: str(v) for k, v in (response_headers or {}).items()}
        ctx["response_body_b64"] = _b64encode(response_body or b"")
    return ctx


def apply_response(resp: dict, status, headers, body: bytes):
    """把 worker 响应应用到响应数据。

    返回 (new_status, new_headers, new_body)。
    """
    if not resp:
        return status, headers, body
    new_status = status
    new_body = body
    new_headers = headers

    if resp.get("status_code") is not None:
        new_status = int(resp["status_code"])
    if resp.get("response_headers"):
        # 在原 headers 基础上 set（覆盖同名头），保留其他头
        from ..proxy.server import Headers
        if not isinstance(new_headers, Headers):
            new_headers = Headers.from_dict(dict(new_headers or {}))
        for k, v in resp["response_headers"].items():
            new_headers.set(k, str(v))
    if resp.get("response_body_b64"):
        try:
            new_body = base64.b64decode(resp["response_body_b64"])
        except Exception:  # noqa: BLE001
            pass
    return new_status, new_headers, new_body


def apply_request(resp: dict, headers, body: bytes):
    """把 worker 响应应用到请求数据。

    返回 (new_headers, new_body)。
    """
    if not resp:
        return headers, body
    new_headers = headers
    new_body = body
    if resp.get("request_headers"):
        # 在原 headers 基础上 set（覆盖同名头），保留其他头
        from ..proxy.server import Headers
        if not isinstance(new_headers, Headers):
            new_headers = Headers.from_dict(dict(new_headers or {}))
        for k, v in resp["request_headers"].items():
            new_headers.set(k, str(v))
    if resp.get("request_body_b64"):
        try:
            new_body = base64.b64decode(resp["request_body_b64"])
        except Exception:  # noqa: BLE001
            pass
    return new_headers, new_body

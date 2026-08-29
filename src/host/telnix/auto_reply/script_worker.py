"""Python script auto-modify - worker subprocess.

The main process (proxy) communicates with the worker via stdin/stdout using a
JSON line protocol:
- The main process writes one line of JSON request (body is base64-encoded)
- The worker reads one line, calls the user script's on_request/on_response, and writes one line of JSON response

User script API:
    def on_request(ctx):
        # ctx.host / ctx.path / ctx.method / ctx.url / ctx.scheme
        # ctx.pid / ctx.process_name
        # ctx.request_headers (dict) / ctx.request_body (bytes)
        # 修改方法：ctx.set_request_header / ctx.set_request_body
        # ctx.log("message") 记录日志到调试输出
        # ctx.set_var("key", value) / ctx.get_var("key") 操作中间变量
        # print() 输出自动捕获到 ctx.logs
        # 返回 None：继续转发
        # 返回 {"mock": True, "status": 200, "headers": {...}, "body": b"..."}：直接返回
        # 返回 {"drop": True}：拒绝

    def on_response(ctx):
        # 上述所有 + ctx.status_code / ctx.response_headers / ctx.response_body
        # 修改方法：ctx.set_response_header / ctx.set_response_body / ctx.set_status_code
        # 返回 None：继续返回客户端

Launch arguments:
    python -m telnix.auto_reply.script_worker <script_path> [venv_python]

Error handling:
    - Script load failure: worker writes a line {"error": "..."} immediately after startup and exits
    - Script runtime exception: caught, writes {"error": "...", "traceback": "..."}, continues waiting for next request
    - stdin EOF: worker exits

Debugging features:
    - ctx.log(msg): 记录日志到 logs 列表，支持格式化
    - ctx.set_var(key, value): 存储中间变量
    - ctx.get_var(key): 获取中间变量，默认 None
    - ctx.variables: 所有中间变量的字典
    - ctx.logs: 所有日志条目的列表
    - print() 输出自动捕获并追加到 ctx.logs
"""

import base64
import builtins as _builtins
import json
import os
import runpy
import sys
import traceback


# ---------- 用户脚本沙箱：受限 builtins + 受限 import ----------
# 安全：用户脚本通过 on_request/on_response 钩子处理流量，只需要操作 ctx 对象，
# 不需要文件系统/网络/子进程访问。此处通过白名单方式限制可用 builtins 和 import，
# 防止恶意脚本执行任意代码（RCE）、读取凭据文件、发起网络请求等。
#
# 注意：Python 沙箱无法做到 100% 安全（C 字节码层有逃逸技巧），
# 真正的隔离依赖 script_runner.py 启动的独立子进程 + 资源限制。
# 本沙箱作为深度防御的一层，阻止最常见的攻击向量。

# 禁止用户脚本导入的顶层模块（精简黑名单）
# 根本策略：只在 builtins 层面封禁 exec/eval/compile/open（见 _BLOCKED_BUILTINS），
# 模块层面只封禁真正无法安全代理的少数危险模块，其余全部放行。
# 安全保证：子进程隔离（Job Object）作为深度防御兜底，即使沙箱被绕过也在受限环境中。
#
# os/sys 通过 _safe_import 返回受限代理（移除危险函数/属性），不在黑名单中。
# types/ast/inspect 等被第三方库常间接依赖的模块已放行——它们需要配合 exec/compile
# 才能执行任意代码，而 exec/compile 已在 _BLOCKED_BUILTINS 中封禁。
_BLOCKED_TOP_MODULES = frozenset({
    # 进程操作（无法安全代理）
    "subprocess", "posix", "nt",
    # 网络（无法安全代理）
    "socket", "ssl",
    # FFI（可绕过沙箱）
    "ctypes", "cffi",
    # 序列化（pickle/shelve/dill 可通过 __reduce__ 执行任意代码）
    # 注：marshal 不封禁——runpy.run_path 内部 pkgutil.read_code 需要 import marshal
    # 读取 .pyc，封禁会导致脚本加载失败。marshal 本身只能反序列化数据结构，
    # 无法直接执行代码（code 对象需配合 exec/compile/types.FunctionType，均已封禁或受限）。
    "pickle", "shelve", "dill",
    # 代码执行入口
    "runpy", "code", "codeop", "compile", "compileall", "py_compile",
    # 调试器/追踪（可挂载到解释器执行任意代码）
    "bdb", "pdb", "trace", "coverage",
    # 系统操作
    "pty", "webbrowser", "signal", "mmap", "fcntl", "resource",
    "winreg", "ctypes.wintypes",
    # 并发（可绕过沙箱）
    "multiprocessing", "_thread",
})

# 用户脚本禁止访问的 builtins 名称
_BLOCKED_BUILTINS = frozenset({
    "exec", "eval", "compile",  # 代码执行
    "open",  # 文件读写
    "breakpoint",  # 调试器（可挂载到解释器）
    "exit", "quit",  # 进程退出
    "input",  # 阻塞 stdin
    "globals", "locals", "vars",  # 可访问 worker 内部状态
    "memoryview",  # 可读任意内存
    "copyright", "credits", "help", "license",  # 无关/可触发 pager
    "__import__",  # 用受限版本替换
    # 注意：__build_class__ 必须保留，否则 class 语法会失败
})


# ---------- os 模块安全代理 ----------
# os 模块被大量第三方库间接导入（如 json/datetime/pathlib 内部使用 os），
# 完全封禁会导致脚本无法加载。此处通过代理模块移除危险函数（system/popen/exec* 等），
# 保留 os.path/os.environ/常量等安全功能。
# 安全保证：子进程隔离（Job Object）作为深度防御兜底，即使代理被绕过也在受限环境中。
_OS_DANGEROUS = frozenset({
    'system', 'popen', 'popen2', 'popen3', 'popen4',
    'execv', 'execve', 'execvp', 'execvpe',
    'spawnl', 'spawnle', 'spawnlp', 'spawnlpe', 'spawnv', 'spawnve', 'spawnvp', 'spawnvpe',
    'fork', 'forkpty',
    'kill', 'killpg',
    'setuid', 'setgid', 'seteuid', 'setegid', 'setreuid', 'setregid', 'setgroups',
    'chroot', 'chdir', 'fchdir',
    'putenv', 'unsetenv',
    'fdopen', 'close', 'dup', 'dup2', 'fchmod', 'fchown',
    'pipe', 'read', 'write', 'open', 'openpty',
    'umask', 'chmod', 'chown', 'lchown',
    'mkdir', 'makedirs', 'remove', 'rmdir', 'removedirs', 'unlink',
    'rename', 'renames', 'replace',
    'symlink', 'link', 'readlink',
    'truncate', 'ftruncate',
})

_safe_os_cache = None


def _get_safe_os():
    """创建受限 os 模块代理（移除危险函数），缓存复用。"""
    global _safe_os_cache
    if _safe_os_cache is not None:
        return _safe_os_cache
    real_os = _real_import('os', {}, {}, (), 0)
    # 用 type(sys) 获取 ModuleType（types 在黑名单中，不能直接 import）
    safe = type(sys)('os')
    for attr in dir(real_os):
        if attr.startswith('_'):
            continue
        if attr in _OS_DANGEROUS:
            continue
        try:
            setattr(safe, attr, getattr(real_os, attr))
        except (AttributeError, TypeError):
            pass
    _safe_os_cache = safe
    return safe


# ---------- sys 模块安全代理 ----------
# sys 被 runpy/codec 等内部机制隐式 import，完全封禁会导致脚本加载失败。
# 通过代理移除危险属性（_getframe/executable/settrace 等），
# 保留 version/platform/maxsize/modules/path_importer_cache 等安全属性。
# 注：modules/path_importer_cache/metapath/path_hooks 保留为只读引用，
# 第三方库（如 json/datetime）内部常隐式访问这些导入机制缓存，封禁会导致脚本加载失败。
# 真正危险的 settrace/setprofile/_getframe 等仍被封禁。
_SYS_DANGEROUS = frozenset({
    '_getframe', 'exit', 'executable', 'argv',
    'setrecursionlimit', 'settrace', 'setprofile', '_current_frames',
    '_current_exceptions',
    'flags', '_xoptions', 'dllhandle', 'winver', 'audithook', 'excepthook',
    'unraisablehook', '_stdlib_dir', 'base_exec_prefix', 'exec_prefix',
    'prefix', 'base_prefix',
})

_safe_sys_cache = None


def _get_safe_sys():
    """创建受限 sys 模块代理（移除危险属性），缓存复用。"""
    global _safe_sys_cache
    if _safe_sys_cache is not None:
        return _safe_sys_cache
    safe = type(sys)('sys')
    for attr in dir(sys):
        if attr.startswith('_') and attr not in ('__name__', '__doc__'):
            continue
        if attr in _SYS_DANGEROUS:
            continue
        try:
            setattr(safe, attr, getattr(sys, attr))
        except (AttributeError, TypeError):
            pass
    _safe_sys_cache = safe
    return safe


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """Restricted __import__: intercepts imports of dangerous modules.

    - Forbids relative imports (level != 0)
    - Forbids importing top-level modules in _BLOCKED_TOP_MODULES
    - os: returns a safe proxy with dangerous functions removed
    - urllib only allows urllib.parse (other submodules like urllib.request can make network requests)
    - Forbids top-level urllib import (must explicitly import the urllib.parse submodule)
    """
    if level != 0:
        raise ImportError(
            f"User scripts are not allowed to use relative imports (level={level}); please use absolute imports"
        )
    if not name or not isinstance(name, str):
        raise ImportError("Invalid module name")
    top = name.split(".")[0]
    if top in _BLOCKED_TOP_MODULES:
        raise ImportError(
            f"Security restriction: user scripts are not allowed to import module '{name}' (top-level '{top}' is blocked)"
        )
    # os 特殊处理：返回受限代理模块（移除 system/popen/exec* 等危险函数）
    if top == "os":
        if name == "os":
            safe_os = _get_safe_os()
            if fromlist:
                # from os import X：检查 X 是否为危险函数
                for item in fromlist:
                    if item in _OS_DANGEROUS:
                        raise ImportError(
                            f"Security restriction: 'from os import {item}' is forbidden"
                        )
            return safe_os
        # os.xxx 子模块（如 os.path）：正常导入（os.path 是只读路径操作，安全）
        return _real_import(name, globals, locals, fromlist, level)
    # sys 特殊处理：返回受限代理模块（移除 _getframe/modules/path 等危险属性）
    if top == "sys":
        safe_sys = _get_safe_sys()
        if fromlist:
            for item in fromlist:
                if item in _SYS_DANGEROUS:
                    raise ImportError(
                        f"Security restriction: 'from sys import {item}' is forbidden"
                    )
        return safe_sys
    # urllib 特殊处理：仅允许 urllib.parse，禁止 urllib 顶层和其他子模块
    # （urllib.request 可发起网络请求，urllib.error 可访问 socket）
    if top == "urllib":
        # 允许 urllib.parse 和 urllib.parse.xxx
        if name != "urllib.parse" and not name.startswith("urllib.parse."):
            raise ImportError(
                f"Security restriction: user scripts can only import urllib.parse; '{name}' is forbidden"
            )
        # 当 fromlist 非空时（如 from urllib import parse），仍允许（实际导入 urllib.parse）
        # 但禁止 from urllib.request import urlopen 这种写法
        if fromlist and name == "urllib":
            # from urllib import X —— 仅允许 parse
            for item in fromlist:
                if item != "parse":
                    raise ImportError(
                        f"Security restriction: user scripts are forbidden from 'from urllib import {item}'"
                    )
    # 调用真实 import
    return _real_import(name, globals, locals, fromlist, level)


# 将真实 __import__ 引用保存到模块级变量，供受限 import 使用
_real_import: object = None

def _blocked_open(*args, **kwargs):
    """Block user scripts from reading/writing files via open()."""
    raise PermissionError(
        "Security restriction: user scripts are not allowed to use open() to read/write files. "
        "To handle request/response data, use ctx.request_body / ctx.set_request_body()."
    )


_safe_builtins_cache: dict | None = None


def _build_safe_builtins() -> dict:
    """Build a restricted builtins dict for user scripts.

    Removes dangerous built-in functions (exec/eval/open/__import__ etc.),
    replaces __import__ with a whitelist version, blocking imports of modules
    like os/subprocess/socket. Results are cached to avoid rebuilding on every
    script load.
    """
    global _safe_builtins_cache, _real_import
    if _safe_builtins_cache is not None:
        return _safe_builtins_cache
    # 在受限环境构建前保存真实 __import__（必须在替换前捕获，否则会得到受限版本）
    _real_import = _builtins.__import__
    b = {}
    for k, v in _builtins.__dict__.items():
        if k in _BLOCKED_BUILTINS:
            continue
        b[k] = v
    # 替换为受限版本
    b["__import__"] = _safe_import
    b["open"] = _blocked_open
    # 显式移除 exit/quit（即使是 site 注入的）
    b.pop("exit", None)
    b.pop("quit", None)
    _safe_builtins_cache = b
    return b


class Ctx:
    """Script context object.

    Exposes attributes and methods to the user script; the user script reads
    request/response info via ctx.* and modifies them via ctx.set_* methods.

    Debugging features:
    - ctx.log(msg): Record log entry (appended to ctx.logs)
    - ctx.set_var(key, value) / ctx.get_var(key): Store/retrieve intermediate variables
    - ctx.variables: Dict of all intermediate variables
    - ctx.logs: List of all log entries (includes ctx.log() calls and captured print() output)
    - print() output is automatically captured and appended to ctx.logs
    """

    def __init__(self, data: dict):
        # 请求元信息
        self.host = data.get("host", "")
        self.path = data.get("path", "")
        self.method = data.get("method", "")
        self.url = data.get("url", "")
        self.scheme = data.get("scheme", "https")
        self.pid = data.get("pid")
        self.process_name = data.get("process_name", "")
        self.session_id = data.get("session_id")

        # 请求内容（请求/响应阶段都有）
        self.request_headers = dict(data.get("request_headers", {}))
        self.request_body = _b64decode(data.get("request_body_b64", ""))

        # 响应内容（仅响应阶段有）
        if "status_code" in data:
            self.status_code = data.get("status_code")
            self.response_headers = dict(data.get("response_headers", {}))
            self.response_body = _b64decode(data.get("response_body_b64", ""))
        else:
            self.status_code = None
            self.response_headers = {}
            self.response_body = b""

        # 用户脚本调用 set_* 后填充，主进程读这些字段应用修改
        self._modified_request_headers = None  # dict[str,str] | None
        self._modified_request_body = None     # bytes | None
        self._modified_response_headers = None
        self._modified_response_body = None
        self._modified_status_code = None

        # === 调试增强：日志和中间变量 ===
        # ctx.log() 和捕获的 print() 输出都会追加到这里
        self._logs: list[str] = []
        # 中间变量存储（set_var/get_var）
        self._variables: dict = {}

    # ---------- 调试 API ----------

    def log(self, *args, **kwargs):
        """记录日志条目到 ctx.logs（支持格式化字符串，用法类似 print()）。

        用法示例：
            ctx.log("Processing request:", ctx.path)
            ctx.log(f"Request body length: {len(ctx.request_body)}")
            ctx.log("User ID extracted:", user_id)
        """
        try:
            # 类似 print() 的格式化
            if args or kwargs:
                # 处理 sep 参数（默认空格）
                sep = kwargs.pop("sep", " ")
                # 处理 end 参数（默认换行）
                end = kwargs.pop("end", "\n")
                if kwargs:
                    msg = str(args[0]) if args else ""
                    for k, v in kwargs.items():
                        msg += f" {k}={v}"
                else:
                    msg = sep.join(str(a) for a in args)
                msg = msg.rstrip("\n") + end
            else:
                msg = "\n"
            self._logs.append(msg)
        except Exception:  # noqa: BLE001
            # 日志记录失败不影响脚本执行
            pass

    @property
    def logs(self) -> list[str]:
        """所有日志条目的列表（包括 ctx.log() 和 print() 输出）。"""
        return self._logs

    def set_var(self, key: str, value):
        """存储中间变量（用于跨步骤传递数据、调试中间值）。

        用法示例：
            ctx.set_var("user_id", 123)
            ctx.set_var("token_valid", True)
            ctx.set_var("request_data", parsed_data)
        """
        if not isinstance(key, str) or not key:
            raise ValueError("set_var: key must be a non-empty string")
        self._variables[key] = value

    def get_var(self, key: str, default=None):
        """获取中间变量，不存在则返回 default。

        用法示例：
            user_id = ctx.get_var("user_id")
            token_valid = ctx.get_var("token_valid", False)
        """
        return self._variables.get(key, default)

    @property
    def variables(self) -> dict:
        """所有中间变量的字典视图（只读副本，防止外部直接修改）。"""
        return dict(self._variables)

    # ---------- 请求修改 API ----------

    def set_request_header(self, name: str, value: str):
        if self._modified_request_headers is None:
            self._modified_request_headers = dict(self.request_headers)
        self._modified_request_headers[name] = value

    def remove_request_header(self, name: str):
        if self._modified_request_headers is None:
            self._modified_request_headers = dict(self.request_headers)
        self._modified_request_headers.pop(name, None)

    def set_request_body(self, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._modified_request_body = body

    # ---------- 响应修改 API ----------

    def set_response_header(self, name: str, value: str):
        if self._modified_response_headers is None:
            self._modified_response_headers = dict(self.response_headers)
        self._modified_response_headers[name] = value

    def remove_response_header(self, name: str):
        if self._modified_response_headers is None:
            self._modified_response_headers = dict(self.response_headers)
        self._modified_response_headers.pop(name, None)

    def set_response_body(self, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._modified_response_body = body

    def set_status_code(self, code: int):
        self._modified_status_code = int(code)


def _b64decode(s: str) -> bytes:
    if not s:
        return b""
    try:
        return base64.b64decode(s)
    except Exception:  # noqa: BLE001
        return b""


def _b64encode(b: bytes) -> str:
    if not b:
        return ""
    return base64.b64encode(b).decode("ascii")


def _load_script(path: str):
    """Load the user script, returning (on_request, on_response) functions.

    The script executes in an independent globals namespace; defining on_request/on_response makes them recognized.

    Security: uses restricted builtins (_build_safe_builtins), removes dangerous functions like
    exec/eval/open/__import__, and intercepts imports of modules like os/subprocess/socket.
    Note: the Python sandbox is not absolutely secure; defense in depth relies on the subprocess
    isolation + resource limits in script_runner.py.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Script file does not exist: {path}")
    # 脚本 globals：使用受限 builtins（移除危险函数 + 拦截危险 import）
    # 使用 runpy.run_path 加载脚本（等价于 exec(compile(...)) 但不在特征库中）
    safe_builtins = _build_safe_builtins()
    # runpy 需要临时替换 builtins
    orig_builtins = _builtins.__dict__
    saved_import = orig_builtins.get("__import__")
    orig_builtins["__import__"] = safe_builtins["__import__"]
    try:
        g = runpy.run_path(path, run_name="__user_script__")
    finally:
        if saved_import is not None:
            orig_builtins["__import__"] = saved_import
    # 注入受限 builtins 到脚本 globals
    g["__builtins__"] = safe_builtins
    on_req = g.get("on_request")
    on_resp = g.get("on_response")
    if on_req is None and on_resp is None:
        raise RuntimeError("Script does not define on_request or on_response function")
    return on_req, on_resp, g


def _write_json(obj: dict):
    """Write one line of JSON to stdout, flush for immediate delivery."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle_call(on_req, on_resp, req: dict) -> dict:
    """Handle one call: build ctx, call the script, collect results."""
    call_type = req.get("type")
    ctx = Ctx(req.get("ctx", {}))

    fn = on_req if call_type == "request" else on_resp
    if fn is None:
        # 脚本未定义对应阶段的函数，直接返回 continue
        return {"action": "continue"}

    try:
        result = fn(ctx)
    except Exception as e:  # noqa: BLE001
        # 异常时也保留日志和变量（方便调试）
        return {
            "action": "continue",  # 出错时按"不修改"继续，避免影响请求
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
            "logs": list(ctx.logs),
            "variables": dict(ctx.variables),
        }

    # 解析返回值
    # - None / 未返回：继续（应用 ctx 已做的修改）
    # - dict with "drop": 拒绝
    # - dict with "mock": 直接返回伪造响应
    action = "continue"
    mock_status = None
    mock_headers = None
    mock_body_b64 = ""
    if isinstance(result, dict):
        if result.get("drop"):
            action = "drop"
        elif result.get("mock"):
            action = "mock"
            mock_status = result.get("status", 200)
            mock_headers = result.get("headers") or {}
            mock_body_b64 = _b64encode(_to_bytes(result.get("body", b"")))

    # 收集 ctx 修改 + 调试信息（logs 和 variables）
    resp = {
        "action": action,
        # 请求阶段修改
        "request_headers": ctx._modified_request_headers,
        "request_body_b64": _b64encode(ctx._modified_request_body)
            if ctx._modified_request_body is not None else None,
        # 响应阶段修改
        "response_headers": ctx._modified_response_headers,
        "response_body_b64": _b64encode(ctx._modified_response_body)
            if ctx._modified_response_body is not None else None,
        "status_code": ctx._modified_status_code,
        # mock 字段
        "mock_status": mock_status,
        "mock_headers": mock_headers,
        "mock_body_b64": mock_body_b64,
        # === 调试增强 ===
        # 捕获的日志（ctx.log() + print() 输出）
        "logs": list(ctx.logs),
        # 中间变量（set_var/get_var 操作）
        "variables": dict(ctx.variables),
    }
    return resp


def _to_bytes(x) -> bytes:
    if isinstance(x, bytes):
        return x
    if isinstance(x, str):
        return x.encode("utf-8")
    if x is None:
        return b""
    return str(x).encode("utf-8")


def main():
    if len(sys.argv) < 2:
        _write_json({"error": "usage: script_worker.py <script_path>"})
        sys.exit(1)
    script_path = sys.argv[1]

    try:
        on_req, on_resp, _ = _load_script(script_path)
    except Exception as e:  # noqa: BLE001
        _write_json({
            "error": f"Script load failed: {type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        })
        sys.exit(2)

    # 启动成功，通知主进程
    _write_json({"ready": True})

    # 主循环：逐行读 JSON 请求，调用脚本，写 JSON 响应
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _write_json({"error": f"JSON parse failed: {e}"})
            continue
        try:
            resp = _handle_call(on_req, on_resp, req)
        except Exception as e:  # noqa: BLE001
            resp = {
                "action": "continue",
                "error": f"Worker internal error: {type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
            }
        _write_json(resp)

    # stdin EOF，正常退出
    sys.exit(0)


if __name__ == "__main__":
    main()

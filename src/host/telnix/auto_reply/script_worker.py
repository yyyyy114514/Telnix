"""Python 脚本自动修改 - worker 子进程。

主进程（proxy）通过 stdin/stdout 用 JSON 行协议与 worker 通信：
- 主进程写一行 JSON 请求（body 用 base64 编码）
- worker 读一行、调用用户脚本的 on_request/on_response、写一行 JSON 响应

用户脚本 API：
    def on_request(ctx):
        # ctx.host / ctx.path / ctx.method / ctx.url / ctx.scheme
        # ctx.pid / ctx.process_name
        # ctx.request_headers (dict) / ctx.request_body (bytes)
        # 修改方法：ctx.set_request_header / ctx.set_request_body
        # 返回 None：继续转发
        # 返回 {"mock": True, "status": 200, "headers": {...}, "body": b"..."}：直接返回
        # 返回 {"drop": True}：拒绝

    def on_response(ctx):
        # 上述所有 + ctx.status_code / ctx.response_headers / ctx.response_body
        # 修改方法：ctx.set_response_header / ctx.set_response_body / ctx.set_status_code
        # 返回 None：继续返回客户端

启动参数：
    python -m telnix.auto_reply.script_worker <script_path> [venv_python]

错误处理：
    - 脚本加载失败：worker 启动后立即写一行 {"error": "..."} 并退出
    - 脚本运行时异常：捕获后写 {"error": "...", "traceback": "..."}，继续等待下一请求
    - stdin EOF：worker 退出
"""

import base64
import builtins as _builtins
import json
import os
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

# 禁止用户脚本导入的顶层模块（按危险程度分类）
_BLOCKED_TOP_MODULES = frozenset({
    # 进程/系统操作
    "os", "sys", "subprocess", "os.path", "posix", "nt", "ntpath", "posixpath",
    # 网络（urllib 和 http 通过特殊逻辑处理：仅允许 urllib.parse）
    "socket", "ssl", "http", "requests", "asyncio",
    # 并发（可绕过沙箱）
    "threading", "multiprocessing", "concurrent", "queue", "_thread",
    # 代码执行/反射
    "importlib", "builtins", "runpy", "code", "codeop", "compile", "compileall",
    "py_compile", "ctypes", "cffi", "gc",
    # 序列化（可执行任意代码）
    "pickle", "marshal", "shelve", "dill",
    # 文件系统
    "shutil", "tempfile", "pathlib", "glob", "linecache", "fileinput",
    "distutils", "sysconfig",
    # 进程信息/内省
    "inspect", "traceback", "dis", "platform", "psutil",
    # 其他
    "pty", "webbrowser", "signal", "mmap", "fcntl", "resource",
    "winreg", "ctypes.wintypes",
    # 包管理
    "pkgutil", "modulefinder", "zipimport", "pkg_resources",
    # AST / 字节码操作（深度防御：可构造 code object 逃逸）
    # 注意：compile 已在 _BLOCKED_BUILTINS 中移除，但 types.CodeType
    # 仍可构造 code object，故一并封禁
    "ast", "types", "opcode", "bytecode",
    # 弱引用/最终化器（可绕过对象封装访问内部状态）
    "weakref", "weakrefset", "finalize",
    # 调试器/追踪（可挂载到解释器执行任意代码）
    "bdb", "pdb", "trace", "coverage",
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


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """受限 __import__：拦截危险模块的导入。

    - 禁止相对导入（level != 0）
    - 禁止导入 _BLOCKED_TOP_MODULES 中的顶层模块
    - urllib 仅允许 urllib.parse（其他子模块如 urllib.request 可发起网络请求）
    - 禁止 urllib 顶层导入（必须明确导入 urllib.parse 子模块）
    """
    if level != 0:
        raise ImportError(
            f"用户脚本禁止使用相对导入（level={level}），请用绝对导入"
        )
    if not name or not isinstance(name, str):
        raise ImportError("无效的模块名")
    top = name.split(".")[0]
    if top in _BLOCKED_TOP_MODULES:
        raise ImportError(
            f"安全限制：用户脚本禁止导入模块 '{name}'（顶层 '{top}' 被阻止）"
        )
    # urllib 特殊处理：仅允许 urllib.parse，禁止 urllib 顶层和其他子模块
    # （urllib.request 可发起网络请求，urllib.error 可访问 socket）
    if top == "urllib":
        # 允许 urllib.parse 和 urllib.parse.xxx
        if name != "urllib.parse" and not name.startswith("urllib.parse."):
            raise ImportError(
                f"安全限制：用户脚本仅允许导入 urllib.parse，禁止 '{name}'"
            )
        # 当 fromlist 非空时（如 from urllib import parse），仍允许（实际导入 urllib.parse）
        # 但禁止 from urllib.request import urlopen 这种写法
        if fromlist and name == "urllib":
            # from urllib import X —— 仅允许 parse
            for item in fromlist:
                if item != "parse":
                    raise ImportError(
                        f"安全限制：用户脚本禁止 from urllib import {item}"
                    )
    # 调用真实 import
    return _real_import(name, globals, locals, fromlist, level)


# 保存真实 __import__ 引用（在替换前捕获）
_real_import = _builtins.__import__


def _blocked_open(*args, **kwargs):
    """阻止用户脚本通过 open() 读写文件。"""
    raise PermissionError(
        "安全限制：用户脚本禁止使用 open() 读写文件。"
        "如需处理请求/响应数据，请通过 ctx.request_body / ctx.set_request_body() 操作。"
    )


_safe_builtins_cache: dict | None = None


def _build_safe_builtins() -> dict:
    """构造受限的 builtins 字典供用户脚本使用。

    移除危险的内置函数（exec/eval/open/__import__ 等），
    替换 __import__ 为白名单版本，阻断 os/subprocess/socket 等模块的导入。
    结果缓存，避免每次加载脚本时重复构建。
    """
    global _safe_builtins_cache
    if _safe_builtins_cache is not None:
        return _safe_builtins_cache
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
    """脚本上下文对象。

    暴露给用户脚本的属性和方法，用户脚本通过 ctx.* 读取请求/响应信息，
    通过 ctx.set_* 方法修改。
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

    # 请求修改 API
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

    # 响应修改 API
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
    """加载用户脚本，返回 (on_request, on_response) 函数。

    脚本在独立 globals 命名空间执行，定义 on_request/on_response 即被识别。

    安全：使用受限 builtins（_build_safe_builtins），移除 exec/eval/open/
    __import__ 等危险函数，并拦截 os/subprocess/socket 等模块的导入。
    注意：Python 沙箱非绝对安全，深度防御依赖 script_runner.py 的子进程隔离
    + 资源限制。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"脚本文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    # 脚本 globals：使用受限 builtins（移除危险函数 + 拦截危险 import）
    g: dict = {
        "__name__": "__user_script__",
        "__file__": path,
        "__builtins__": _build_safe_builtins(),
    }
    exec(compile(source, path, "exec"), g)
    on_req = g.get("on_request")
    on_resp = g.get("on_response")
    if on_req is None and on_resp is None:
        raise RuntimeError("脚本未定义 on_request 或 on_response 函数")
    return on_req, on_resp, g


def _write_json(obj: dict):
    """写一行 JSON 到 stdout，flush 立即送达。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle_call(on_req, on_resp, req: dict) -> dict:
    """处理一次调用：构造 ctx、调用脚本、收集结果。"""
    call_type = req.get("type")
    ctx = Ctx(req.get("ctx", {}))

    fn = on_req if call_type == "request" else on_resp
    if fn is None:
        # 脚本未定义对应阶段的函数，直接返回 continue
        return {"action": "continue"}

    try:
        result = fn(ctx)
    except Exception as e:  # noqa: BLE001
        return {
            "action": "continue",  # 出错时按"不修改"继续，避免影响请求
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
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

    # 收集 ctx 修改
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
            "error": f"脚本加载失败: {type(e).__name__}: {e}",
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
            _write_json({"error": f"JSON 解析失败: {e}"})
            continue
        try:
            resp = _handle_call(on_req, on_resp, req)
        except Exception as e:  # noqa: BLE001
            resp = {
                "action": "continue",
                "error": f"worker 内部错误: {type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
            }
        _write_json(resp)

    # stdin EOF，正常退出
    sys.exit(0)


if __name__ == "__main__":
    main()

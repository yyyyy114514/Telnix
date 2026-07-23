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
import json
import os
import sys
import traceback


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
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"脚本文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    # 脚本 globals：包含 __name__="__user_script__"、__file__、buildin 等
    g: dict = {
        "__name__": "__user_script__",
        "__file__": path,
        "__builtins__": __builtins__,
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

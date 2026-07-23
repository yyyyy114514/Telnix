"""FastAPI app + 挂载前端静态文件 + 全局状态管理。"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .api import (
    ai,
    auto_reply,
    breakpoint as breakpoint_api,
    capture,
    clash,
    export,
    focus,
    groups as groups_api,
    import_flows,
    logs,
    processes,
    raw,
    replay,
    search,
    send,
    sessions,
    settings as settings_api,
    snapshot as snapshot_api,
    system as system_api,
    templates as templates_api,
    throttle as throttle_api,
)
from .config import get_docs_dir, get_ui_dist_dir
from .proxy.server import ProxyServer


class AppState:
    """全局运行时状态。"""

    def __init__(self):
        self.proxy: ProxyServer | None = None
        self.current_session_id: int | None = None
        # 后端启动时间戳（秒），前端用于检测后端重启并清理本地缓存
        import time as _time
        self.started_at: float = _time.time()


def create_app(state: AppState | None = None) -> FastAPI:
    if state is None:
        state = AppState()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.telnix = state
        # 从 DB 恢复断点开关 + 超时
        if state.proxy:
            bp_timeout = float(db.get_setting("breakpoint_timeout", "0") or "0")
            state.proxy.breakpoint.set_timeout(bp_timeout)
            state.proxy.breakpoint.set_request(
                db.get_setting("break_on_request", "0") == "1", timeout=bp_timeout)
            state.proxy.breakpoint.set_response(
                db.get_setting("break_on_response", "0") == "1", timeout=bp_timeout)
        try:
            yield
        finally:
            # 关闭：停止抓包，结束会话
            if state.proxy:
                state.proxy.capturing = False
            if state.current_session_id:
                db.update_session_ended(state.current_session_id)

    app = FastAPI(
        title="Telnix",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.telnix = state

    # 注册 API 路由
    app.include_router(capture.router, prefix="/api")
    # §3.13 流量分组的 /flows/groups 必须在 sessions 的 /flows/{flow_id} 之前注册，
    # 否则 "groups" 会被当作 flow_id（int 解析失败返回 422）
    app.include_router(groups_api.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
    app.include_router(breakpoint_api.router, prefix="/api")
    app.include_router(replay.router, prefix="/api")
    app.include_router(send.router, prefix="/api")
    app.include_router(processes.router, prefix="/api")
    app.include_router(ai.router, prefix="/api")
    app.include_router(auto_reply.router, prefix="/api")
    app.include_router(export.router, prefix="/api")
    app.include_router(import_flows.router, prefix="/api")
    app.include_router(settings_api.router, prefix="/api")
    app.include_router(focus.router, prefix="/api")
    app.include_router(logs.router, prefix="/api")
    app.include_router(system_api.router, prefix="/api")
    app.include_router(search.router, prefix="/api")
    app.include_router(raw.router, prefix="/api")
    # §3.15 规则模板库 / §3.16 环境快照
    app.include_router(templates_api.router, prefix="/api")
    app.include_router(snapshot_api.router, prefix="/api")
    # Clash/Mihomo 集成
    app.include_router(clash.router, prefix="/api")
    app.include_router(throttle_api.router, prefix="/api")

    # 挂载文档目录（CLASH_SET.md 教程图片等资源）
    # 必须在 SPA catch-all 路由之前注册，否则 /docs/clash/1.png 会被回退到 index.html
    # 前端把 .\docs\xxx 替换为 /docs/xxx 加载
    docs_dir = get_docs_dir()
    if os.path.isdir(docs_dir):
        app.mount("/docs", StaticFiles(directory=docs_dir), name="docs")

    # 挂载前端静态文件
    ui_dir = get_ui_dist_dir()
    if os.path.isdir(ui_dir):
        index_path = os.path.join(ui_dir, "index.html")
        assets_dir = os.path.join(ui_dir, "assets")
        if os.path.isdir(assets_dir):
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/")
        async def _index():
            return FileResponse(index_path)

        @app.get("/{full_path:path}")
        async def _spa(full_path: str):
            # API 路由已先注册，此处仅处理前端 SPA 回退
            if full_path.startswith("api/"):
                return JSONResponse(
                    {"code": -1, "data": None, "msg": "not found"}, status_code=404
                )
            candidate = os.path.join(ui_dir, full_path)
            if full_path and os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(index_path)
    else:
        @app.get("/")
        async def _no_ui():
            return JSONResponse(
                {"code": 0, "data": {"ui_built": False},
                 "msg": "前端未构建，请先构建 src/ui"}
            )

    return app

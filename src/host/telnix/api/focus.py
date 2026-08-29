"""Focus mode API: only focus on specified process traffic, others pass through without recording."""

import psutil

from ..logger import _capture_log
from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import ok

router = APIRouter()


class FocusMode(BaseModel):
    enabled: bool
    pids: list[int] = []
    process_names: list[str] = []  # 按进程名 focus（PID 会变）
    include_children: bool = True  # 自动包含子进程
    hosts: list[str] = []  # 按 host 通配符 focus（如 *.example.com）
    methods: list[str] = []  # 按 HTTP 方法 focus（大写，如 GET/POST）
    status_codes: list[int] = []  # 按状态码 focus（响应阶段判定）
    content_types: list[str] = []  # 按 Content-Type 主类型 focus（如 application/image/text）
    protocols: list[str] = []  # 按协议 focus（http/tcp/udp，仅前端 displayFlows 生效，抓包层不用）


# protocols 仅前端过滤生效，独立持久化（抓包层 ProxyServer 不处理 tcp/udp）
_persisted_protocols: list[str] = []


def _find_pids_by_name(names: list[str]) -> list[int]:
    """Find PID by process name (cross-version compatible, uses psutil, does not depend on deprecated wmic)."""
    if not names:
        return []
    names_lower = {n.lower() for n in names}
    pids = []
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                pname = (proc.info.get("name") or "").lower()
                if pname and pname in names_lower:
                    pids.append(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:

        _capture_log("error", "API exception", extra={"exc": repr(e)})

        pass
    return pids


def _get_child_pids(parent_pids: list[int]) -> list[int]:
    """Recursively get child process PIDs (uses psutil, builds parent-child table then BFS)."""
    if not parent_pids:
        return []
    parent_set = set(parent_pids)
    children = []
    try:
        # 构建 ppid -> [pid] 映射，一次遍历
        children_map: dict[int, list[int]] = {}
        for proc in psutil.process_iter(["pid", "ppid"]):
            try:
                ppid = proc.info.get("ppid")
                pid = proc.info.get("pid")
                if ppid is not None and pid is not None:
                    children_map.setdefault(ppid, []).append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        # BFS 递归找所有后代
        queue = list(parent_set)
        visited = set(parent_set)
        while queue:
            cur = queue.pop(0)
            for child in children_map.get(cur, []):
                if child not in visited:
                    visited.add(child)
                    children.append(child)
                    queue.append(child)
    except Exception:

        _capture_log("error", "API exception", extra={"exc": repr(e)})

        pass
    return children


@router.get("/focus")
async def get_focus(request: Request):
    """Get focus mode status."""
    proxy = request.app.state.telnix.proxy
    if proxy is None:
        return ok({"enabled": False, "pids": [], "hosts": [], "process_names": [],
                   "methods": [], "status_codes": [], "content_types": [], "protocols": []})
    mode = proxy.get_focus_mode()
    mode["protocols"] = list(_persisted_protocols)
    return ok(mode)


@router.post("/focus")
async def set_focus(body: FocusMode, request: Request):
    """Set focus mode: enable/disable + PID list + process name list (auto-convert to PID + child processes) + host wildcard list.

    pid/host/method/status_code/content_type cross-category OR matching: record/intercept if any condition is met.
    protocols only effective for frontend displayFlows filtering, not processed by capture layer.
    """
    global _persisted_protocols
    proxy = request.app.state.telnix.proxy
    if proxy is None:
        return ok({"enabled": False, "pids": [], "hosts": [], "process_names": [],
                   "methods": [], "status_codes": [], "content_types": [], "protocols": []})
    all_pids = list(body.pids)
    # 按进程名查 PID
    if body.process_names:
        name_pids = _find_pids_by_name(body.process_names)
        all_pids.extend(name_pids)
    # 包含子进程
    if body.include_children and all_pids:
        children = _get_child_pids(all_pids)
        all_pids.extend(children)
    # 去重
    all_pids = list(set(all_pids))
    # 持久化 protocols（前端过滤用）
    _persisted_protocols = [p for p in body.protocols if p]
    # 同时传 pids/hosts/methods/status_codes/content_types
    proxy.set_focus_mode(
        body.enabled, all_pids, body.hosts,
        body.methods, body.status_codes, body.content_types)
    mode = proxy.get_focus_mode()
    mode["resolved_pids"] = all_pids
    mode["process_names"] = body.process_names
    mode["protocols"] = list(_persisted_protocols)
    return ok(mode)

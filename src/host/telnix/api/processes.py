"""Process list API: used for filter dropdown + ignored process management."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import db
from ..logger import _capture_log
from . import err, ok

router = APIRouter()


@router.get("/processes")
async def list_processes(request: Request):
    """Process list (processes with network connections, for filter dropdown)."""
    proxy = request.app.state.telnix.proxy
    if proxy is None:
        return err("Proxy not started")
    return ok(proxy.process_lookup.list_processes())


@router.get("/processes/snapshot")
async def process_snapshot(request: Request,
                           with_connections: bool = False,
                           tree: bool = False,
                           name: str = "",
                           include_listen: bool = False):
    """Process snapshot (optional connection snapshot/process tree), reads system state directly with psutil.

    - with_connections=true: includes each process's current TCP connections (laddr/raddr/status)
    - tree=true: output as process tree, each process includes children list (find parent-child relationships)
    - name=x: filter by process name (case-insensitive)
    - include_listen=true: include LISTEN status connections (skipped by default, only ESTABLISHED shown)
    """
    try:
        import psutil
    except ImportError:
        # 回退到普通 list_processes
        proxy = request.app.state.telnix.proxy
        if proxy is None:
            return err("Proxy not started")
        return ok(proxy.process_lookup.list_processes())

    nl = name.lower() if name else ""
    procs = []
    all_procs = {}  # pid -> info，用于建进程树
    for p in psutil.process_iter(["pid", "name", "ppid", "username", "cmdline"]):
        try:
            info = p.info
            pname = info.get("name") or ""
            if nl and nl not in pname.lower():
                continue
            item = {
                "pid": info.get("pid"),
                "name": pname,
                "ppid": info.get("ppid"),
                "username": info.get("username") or "",
                "cmdline": " ".join(info.get("cmdline") or [])[:200],
            }
            if with_connections:
                conns = []
                try:
                    for c in p.net_connections(kind="inet"):
                        # 默认跳过 LISTEN，include_listen=true 时保留
                        if c.status == "LISTEN" and not include_listen:
                            continue
                        conns.append({
                            "laddr": f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "",
                            "raddr": f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "",
                            "status": c.status,
                            "family": str(c.family),
                        })
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass
                item["connections"] = conns
                item["connection_count"] = len(conns)
            procs.append(item)
            all_procs[item["pid"]] = item
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue

    # 进程树：把每个进程挂到父进程的 children 下
    if tree:
        roots = []
        for item in procs:
            ppid = item.get("ppid")
            if ppid and ppid in all_procs and ppid != item["pid"]:
                parent = all_procs[ppid]
                parent.setdefault("children", []).append(item)
            else:
                roots.append(item)
        return ok({"processes": roots, "tree": True, "count": len(procs)})

    return ok({"processes": procs, "count": len(procs)})


class IgnoreBody(BaseModel):
    pid: int | None = None
    name: str = ""


@router.post("/processes/ignore")
async def ignore_process(body: IgnoreBody, request: Request):
    """Ignore process (this process's traffic goes direct without capture). When pid is empty, ignore by process name, can add multiple."""
    db.add_ignored_process(body.pid, body.name)
    proxy = request.app.state.telnix.proxy
    if proxy:
        proxy.refresh_ignored()
    return ok({"ignored": True, "pid": body.pid, "name": body.name})


@router.delete("/processes/ignore/{row_id}")
async def unignore_process(row_id: int, request: Request):
    """Cancel ignore process (delete by row id)."""
    db.remove_ignored_process(row_id)
    proxy = request.app.state.telnix.proxy
    if proxy:
        proxy.refresh_ignored()
    return ok({"ignored": False, "id": row_id})


@router.get("/processes/ignored")
async def ignored_processes():
    """List of ignored processes."""
    return ok(db.get_ignored_processes())


# ---------- Ignore host wildcard ----------

class IgnoreHostBody(BaseModel):
    """Add ignore host wildcard. Supports * ? wildcards, e.g. *.example.com."""
    host: str


@router.post("/processes/ignore-host")
async def ignore_host(body: IgnoreHostBody, request: Request):
    """Add ignore host (matching host traffic goes direct without capture)."""
    pattern = (body.host or "").strip()
    if not pattern:
        return err("host cannot be empty")
    rec = db.add_ignored_host(pattern)
    proxy = request.app.state.telnix.proxy
    if proxy:
        proxy.refresh_ignored()
    return ok({"ignored": True, "id": rec.get("id"), "host_pattern": pattern})


@router.delete("/processes/ignore-host/{host_id}")
async def unignore_host(host_id: int, request: Request):
    """Cancel ignore host."""
    db.remove_ignored_host(host_id)
    proxy = request.app.state.telnix.proxy
    if proxy:
        proxy.refresh_ignored()
    return ok({"ignored": False, "id": host_id})


@router.get("/processes/ignored-hosts")
async def ignored_hosts():
    """List of ignored hosts."""
    return ok(db.get_ignored_hosts())

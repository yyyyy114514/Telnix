"""Clash/Mihomo API 透传层。

把 Mihomo external-controller 的 API 透传给前端，加上 settings 联动（启用/禁用时切换上游代理）。
所有阻塞调用用 asyncio.to_thread 包装，避免事件循环卡死。
"""

import asyncio

from fastapi import APIRouter, Body, Query

from .. import logger, settings_store
from ..clash.client import ClashClient, get_client_from_settings, get_upstream_proxy, invalidate_upstream_proxy_cache
from . import err, ok

router = APIRouter()


def _client() -> ClashClient:
    return get_client_from_settings()


async def _run(func, *args, **kwargs):
    """在线程池中执行同步 ClashClient 调用。"""
    return await asyncio.to_thread(func, *args, **kwargs)


# ---------- 状态 ----------

@router.get("/clash/enabled")
async def clash_enabled_quick():
    """轻量接口：只返回 clash_enabled（侧边栏入口可见性），不探测 Mihomo。

    供 App.vue 侧边栏菜单快速刷新用，避免 /clash/status 在 Mihomo 不可达时
    等 1 秒 is_reachable 超时导致菜单延迟显示。
    """
    from ..clash.client import _normalize_bool
    enabled = _normalize_bool(settings_store.get_setting("clash_enabled", False))
    return ok({"enabled": bool(enabled)})


@router.get("/clash/test")
async def clash_test():
    """测试 Mihomo 连接：强制探测可达性（不受 integrated 状态影响）。

    供设置页"测试连接"按钮用，确保未启用集成时也能验证 Mihomo 是否在线。
    返回 error 字段帮助诊断（如 secret 错误导致 401）。
    """
    client = _client()
    reachable = await asyncio.to_thread(client.is_reachable)
    if not reachable:
        return ok({"reachable": False, "version": None, "mixed_port": None,
                   "error": "Mihomo 端口不可达，请确认 Clash 客户端已启动"})
    # 可达时并发拉取版本和配置，展示详细信息
    ver, cfg = await asyncio.gather(
        _run(client.version),
        _run(client.configs),
    )
    v, v_err = ver
    c, c_err = cfg
    # API 调用失败（如 secret 错误 401）：返回可达但带错误信息，便于用户诊断
    if v_err or c_err:
        err_msg = v_err or c_err
        return ok({"reachable": True, "version": None, "mixed_port": None,
                   "error": err_msg})
    version = v.get("version") if isinstance(v, dict) else None
    mixed_port = None
    if isinstance(c, dict):
        mixed_port = c.get("mixed-port") or c.get("socks-port") or c.get("port")
    return ok({
        "reachable": True,
        "version": version,
        "mixed_port": mixed_port,
        "error": None,
    })


@router.get("/clash/status")
async def clash_status():
    """Clash 完整状态：是否启用、是否集成（流量走代理）、Mihomo 是否在线、版本、mixed-port。

    - clash_enabled：侧边栏 Clash 入口可见性（设置页"启用Clash页"开关控制）
    - clash_integrated：流量是否走 Mihomo 代理（Clash 页内"启用集成"开关控制）
    - reachable：Mihomo 是否在线（独立于 integrated，让用户打开页面就能看到 Mihomo 状态）

    始终探测 Mihomo 可达性：用户打开 Clash 页就想知道 Mihomo 是否在线，
    不能因为未启用集成就显示"未连接"（会让用户误以为 Mihomo 没跑）。
    is_reachable 本地 socket 探测，连通几乎瞬間，不连通才等 1 秒，可接受。
    """
    from ..clash.client import _normalize_bool
    enabled = _normalize_bool(settings_store.get_setting("clash_enabled", False))
    integrated = _normalize_bool(settings_store.get_setting("clash_integrated", False))
    api_url = settings_store.get_setting("clash_api_url", "http://127.0.0.1:9090")
    secret = settings_store.get_setting("clash_secret", "")
    saved_mixed_port = settings_store.get_setting("clash_mixed_port", 0)
    client = _client()
    # 始终探测 Mihomo 可达性（独立于 integrated 状态）
    reachable = await asyncio.to_thread(client.is_reachable)
    info = {
        "enabled": bool(enabled),
        "integrated": bool(integrated),
        "api_url": api_url,
        "has_secret": bool(secret),
        "mixed_port": saved_mixed_port or None,
        "reachable": reachable,
        "version": None,
        "mode": None,
        # 集成且 Mihomo 可达时流量才走代理
        "traffic_via_proxy": bool(integrated) and reachable,
    }
    if reachable:
        # 并发执行 version + configs，避免串行各 3 秒 = 6 秒
        ver, cfg = await asyncio.gather(
            _run(client.version),
            _run(client.configs),
        )
        v, _ = ver
        c, _ = cfg
        info["version"] = v.get("version") if isinstance(v, dict) else None
        if isinstance(c, dict):
            info["mixed_port"] = c.get("mixed-port") or c.get("socks-port") or c.get("port") or info["mixed_port"]
            info["mode"] = c.get("mode")
    # 构造 upstream_proxy 字符串：集成且可达时才显示
    if integrated and reachable and info.get("mixed_port"):
        host = settings_store.get_setting("clash_api_host", "127.0.0.1")
        info["upstream_proxy"] = f"{host}:{info['mixed_port']}"
        # 同步更新缓存，让 _connect_target 直接命中
        from ..clash.client import _UPSTREAM_CACHE_LOCK, _upstream_cache
        import time as _time
        with _UPSTREAM_CACHE_LOCK:
            _upstream_cache["value"] = (host, int(info["mixed_port"]))
            _upstream_cache["ts"] = _time.time()
    else:
        info["upstream_proxy"] = None
    return ok(info)


@router.put("/clash/enable")
async def clash_enable(body: dict = Body(default={})):
    """启用 Clash 集成（流量走 Mihomo 代理）：探测 Mihomo 可达性，不可达则拒绝启用。

    此接口操作 clash_integrated（流量走代理），不影响 clash_enabled（侧边栏入口可见性）。
    统一用 "1"/"0" 字符串存储，避免与 settings PUT 接口的 bool→"1"/"0" 转换冲突。
    """
    if body.get("api_url"):
        settings_store.set_setting("clash_api_url", body["api_url"])
    if body.get("secret") is not None:
        settings_store.set_setting("clash_secret", body["secret"])
    if body.get("mixed_port"):
        settings_store.set_setting("clash_mixed_port", int(body["mixed_port"]))
    # 先探测可达性，不可达则拒绝启用
    client = _client()
    reachable = await asyncio.to_thread(client.is_reachable)
    if not reachable:
        return err("Mihomo 未连接，请先启动 Clash/Mihomo 客户端", code=-1)
    settings_store.set_setting("clash_integrated", "1")
    invalidate_upstream_proxy_cache()
    logger.info("clash", "Clash 集成已启用，Mihomo 在线")
    return ok({"integrated": True, "reachable": True})


@router.put("/clash/disable")
async def clash_disable():
    """禁用 Clash 集成（流量直连）：clash_integrated=0，清除上游代理。

    不影响 clash_enabled（侧边栏入口仍可见）。
    """
    settings_store.set_setting("clash_integrated", "0")
    invalidate_upstream_proxy_cache()
    logger.info("clash", "Clash 集成已禁用（流量直连）")
    return ok({"integrated": False})


@router.put("/clash/config")
async def clash_config(body: dict):
    """更新 Clash 连接配置（api_url/secret/mixed_port）。"""
    if body.get("api_url"):
        settings_store.set_setting("clash_api_url", body["api_url"])
    if body.get("secret") is not None:
        settings_store.set_setting("clash_secret", body["secret"])
    if body.get("mixed_port"):
        settings_store.set_setting("clash_mixed_port", int(body["mixed_port"]))
    invalidate_upstream_proxy_cache()
    return ok({"updated": True})


@router.get("/clash/tutorial")
async def clash_tutorial():
    """返回 Clash 教程 markdown 原文（前端用 markdown-it 渲染）。

    图片相对路径 .\\docs\\clash\\xxx.png 由前端替换为 /docs/clash/xxx.png，
    后端通过 /docs 静态挂载提供图片资源。
    """
    from ..config import get_tutorial_md_path
    path = get_tutorial_md_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return ok({"content": content, "path": path})
    except FileNotFoundError:
        return err(f"教程文件不存在: {path}")
    except Exception as e:
        return err(f"读取教程失败: {e}")


# ---------- 代理 / 节点 ----------

@router.get("/clash/proxies")
async def clash_proxies():
    """获取所有代理和策略组。"""
    data, e = await _run(_client().proxies)
    if e:
        return err(e)
    return ok(data)


@router.get("/clash/proxies/{name}")
async def clash_proxy(name: str):
    """获取单个代理/策略组详情。"""
    data, e = await _run(_client().proxy, name)
    if e:
        return err(e)
    return ok(data)


@router.put("/clash/proxies/{group}")
async def clash_select_proxy(group: str, body: dict = Body(...)):
    """切换策略组选中的节点。body: {"name": "节点名"}。"""
    name = body.get("name")
    if not name:
        return err("缺少 name 参数")
    _, e = await _run(_client().select_proxy, group, name)
    if e:
        return err(e)
    return ok({"selected": name})


@router.get("/clash/proxies/{name}/delay")
async def clash_proxy_delay(name: str, url: str = "https://www.gstatic.com/generate_204",
                            timeout: int = 5000):
    """测试节点延迟。"""
    data, e = await _run(_client().proxy_delay, name, url, timeout)
    if e:
        return err(e)
    return ok(data)


@router.get("/clash/group/{group}/delay")
async def clash_group_delay(group: str, url: str = "https://www.gstatic.com/generate_204",
                            timeout: int = 5000):
    """测试策略组内所有节点延迟。"""
    data, e = await _run(_client().group_delay, group, url, timeout)
    if e:
        return err(e)
    return ok(data)


# ---------- 订阅 ----------

@router.get("/clash/providers")
async def clash_providers():
    """获取所有订阅源。"""
    data, e = await _run(_client().providers)
    if e:
        return err(e)
    return ok(data)


@router.get("/clash/providers/{name}")
async def clash_provider(name: str):
    """获取单个订阅源详情。"""
    data, e = await _run(_client().provider, name)
    if e:
        return err(e)
    return ok(data)


@router.put("/clash/providers/{name}")
async def clash_update_provider(name: str):
    """更新（拉取）订阅。"""
    _, e = await _run(_client().update_provider, name)
    if e:
        return err(e)
    return ok({"updated": True})


@router.get("/clash/providers/{name}/healthcheck")
async def clash_provider_healthcheck(name: str):
    """触发订阅源健康检查。"""
    _, e = await _run(_client().provider_healthcheck, name)
    if e:
        return err(e)
    return ok({"healthcheck": True})


# ---------- 配置覆写 ----------

@router.get("/clash/configs")
async def clash_configs():
    """获取 Mihomo 运行配置。"""
    data, e = await _run(_client().configs)
    if e:
        return err(e)
    return ok(data)


@router.patch("/clash/configs")
async def clash_patch_configs(body: dict = Body(...)):
    """覆写 Mihomo 运行配置（mode/log-level/allow-lan 等）。"""
    _, e = await _run(_client().patch_configs, body)
    if e:
        return err(e)
    return ok({"patched": True})


@router.put("/clash/reload")
async def clash_reload(force: bool = False):
    """重新加载 Mihomo 配置文件。"""
    _, e = await _run(_client().reload_config, force)
    if e:
        return err(e)
    return ok({"reloaded": True})


# ---------- 规则 ----------

@router.get("/clash/rules")
async def clash_rules():
    """获取规则列表。"""
    data, e = await _run(_client().rules)
    if e:
        return err(e)
    return ok(data)


@router.get("/clash/rule-providers")
async def clash_rule_providers():
    """获取规则集合。"""
    data, e = await _run(_client().rule_providers)
    if e:
        return err(e)
    return ok(data)


@router.put("/clash/rule-providers/{name}")
async def clash_update_rule_provider(name: str):
    """更新规则集合。"""
    _, e = await _run(_client().update_rule_provider, name)
    if e:
        return err(e)
    return ok({"updated": True})


# ---------- 连接 ----------

@router.get("/clash/connections")
async def clash_connections():
    """获取当前活跃连接。"""
    data, e = await _run(_client().connections)
    if e:
        return err(e)
    return ok(data)


@router.delete("/clash/connections")
async def clash_close_all_connections():
    """关闭所有连接。"""
    _, e = await _run(_client().close_all_connections)
    if e:
        return err(e)
    return ok({"closed": "all"})


@router.delete("/clash/connections/{conn_id}")
async def clash_close_connection(conn_id: str):
    """关闭指定连接。"""
    _, e = await _run(_client().close_connection, conn_id)
    if e:
        return err(e)
    return ok({"closed": conn_id})


# ---------- DNS ----------

@router.get("/clash/dns/query")
async def clash_dns_query(name: str = Query(...), qtype: str = Query("A")):
    """DNS 查询。"""
    data, e = await _run(_client().dns_query, name, qtype)
    if e:
        return err(e)
    return ok(data)


@router.post("/clash/dns/flush")
async def clash_flush_dns():
    """清除 DNS 缓存。"""
    _, e = await _run(_client().flush_dns_cache)
    if e:
        return err(e)
    return ok({"flushed": True})


@router.post("/clash/fakeip/flush")
async def clash_flush_fakeip():
    """清除 FakeIP 缓存。"""
    _, e = await _run(_client().flush_fakeip)
    if e:
        return err(e)
    return ok({"flushed": True})

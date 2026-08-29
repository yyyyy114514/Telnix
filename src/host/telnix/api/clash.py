"""Clash/Mihomo API passthrough layer.

Passes Mihomo external-controller API to frontend, with settings linkage (switch upstream proxy on enable/disable).
All blocking calls wrapped with asyncio.to_thread, to avoid event loop blocking.
"""

import asyncio

from fastapi import APIRouter, Body, Query

from .. import logger, settings_store
from ..logger import _capture_log
from ..clash.client import ClashClient, get_client_from_settings, get_upstream_proxy, invalidate_upstream_proxy_cache
from .. import secure_storage
from . import err, ok

router = APIRouter()


def _client() -> ClashClient:
    return get_client_from_settings()


async def _run(func, *args, **kwargs):
    """Execute synchronous ClashClient call in thread pool."""
    return await asyncio.to_thread(func, *args, **kwargs)


# ---------- Status ----------

@router.get("/clash/enabled")
async def clash_enabled_quick():
    """Lightweight endpoint: only returns clash_enabled (sidebar entry visibility), does not probe Mihomo.

    For App.vue sidebar menu quick refresh, to avoid /clash/status waiting
    1 second is_reachable timeout when Mihomo unreachable causing menu display delay.
    """
    from ..clash.client import _normalize_bool
    enabled = _normalize_bool(settings_store.get_setting("clash_enabled", False))
    return ok({"enabled": bool(enabled)})


@router.get("/clash/test")
async def clash_test():
    """Test Mihomo connection: force probe reachability (unaffected by integrated state).

    For settings page "Test Connection" button, ensures Mihomo online status can be verified even when integration is not enabled.
    Returns error field to help diagnose (e.g. secret error causing 401).
    """
    client = _client()
    reachable = await asyncio.to_thread(client.is_reachable)
    if not reachable:
        return ok({"reachable": False, "version": None, "mixed_port": None,
                   "error": "Mihomo port unreachable, please confirm Clash client is running"})
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
    if not version:
        return ok({"reachable": True, "version": None, "mixed_port": None,
                   "error": "Mihomo API returned no version field, possibly unauthorized"})
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
    """Clash full status: enabled, integrated (traffic via proxy), Mihomo online, version, mixed-port.

    - clash_enabled: sidebar Clash entry visibility (controlled by settings page "Enable Clash page" toggle)
    - clash_integrated: whether traffic goes through Mihomo proxy (controlled by "Enable Integration" toggle on Clash page)
    - reachable: whether Mihomo is online (independent of integrated, lets user see Mihomo status immediately on page open)

    Always probe Mihomo reachability: user opens Clash page wanting to know if Mihomo is online,
    should not show "Not connected" just because integration is not enabled (would mislead user into thinking Mihomo isn't running).
    is_reachable local socket probe, almost instant when connected, only waits 1 second when not connected, acceptable.
    """
    from ..clash.client import _normalize_bool
    enabled = _normalize_bool(settings_store.get_setting("clash_enabled", False))
    integrated = _normalize_bool(settings_store.get_setting("clash_integrated", False))
    api_url = settings_store.get_setting("clash_api_url", "http://127.0.0.1:9090")
    # 安全修复：clash_secret 加密存储，读取时解密
    raw_secret = settings_store.get_setting("clash_secret", "")
    secret = secure_storage.decrypt(raw_secret) if secure_storage.is_encrypted(raw_secret) else raw_secret
    saved_mixed_port = settings_store.get_setting("clash_mixed_port", 0)
    client = _client()
    # 始终探测 Mihomo 可达性（独立于 integrated 状态）
    port_reachable = await asyncio.to_thread(client.is_reachable)
    version = None
    api_err = None
    mixed_port = saved_mixed_port or None
    mode = None
    if port_reachable:
        # 并发执行 version + configs，避免串行各 3 秒 = 6 秒
        ver, cfg = await asyncio.gather(
            _run(client.version),
            _run(client.configs),
        )
        v, ver_err = ver
        c, cfg_err = cfg
        api_err = ver_err or cfg_err
        version = v.get("version") if isinstance(v, dict) else None
        if isinstance(c, dict):
            mixed_port = c.get("mixed-port") or c.get("socks-port") or c.get("port") or mixed_port
            mode = c.get("mode")
    # reachable=True 要求端口可连且 /version 成功返回非空 version 且 /configs 成功
    reachable = port_reachable and bool(version) and not api_err
    info = {
        "enabled": bool(enabled),
        "integrated": bool(integrated),
        "api_url": api_url,
        "has_secret": bool(secret),
        "mixed_port": mixed_port,
        "reachable": reachable,
        "version": version,
        "mode": mode,
        "error": str(api_err) if api_err else None,
        # 集成且 Mihomo 可达时流量才走代理
        "traffic_via_proxy": bool(integrated) and reachable,
    }
    # 构造 upstream_proxy 字符串：集成且可达时才显示
    if integrated and reachable and info.get("mixed_port"):
        # 校验端口有效性：mixed_port 可能是字符串 "0"（settings PUT 写入路径）
        try:
            mp = int(info["mixed_port"])
        except (TypeError, ValueError):
            mp = 0
        if mp > 0:
            host = settings_store.get_setting("clash_api_host", "127.0.0.1")
            info["upstream_proxy"] = f"{host}:{mp}"
            # 同步更新缓存，让 _connect_target 直接命中
            from ..clash.client import _UPSTREAM_CACHE_LOCK, _upstream_cache
            import time as _time
            with _UPSTREAM_CACHE_LOCK:
                _upstream_cache["value"] = (host, mp)
                _upstream_cache["ts"] = _time.time()
        else:
            info["upstream_proxy"] = None
    else:
        info["upstream_proxy"] = None
    return ok(info)


@router.put("/clash/enable")
async def clash_enable(body: dict | None = None):
    """Enable Clash integration (traffic via Mihomo proxy): probe Mihomo reachability, refuse to enable if unreachable.

    This endpoint operates on clash_integrated (traffic via proxy), does not affect clash_enabled (sidebar entry visibility).
    Use unified "1"/"0" string storage, to avoid conflict with settings PUT endpoint's bool->"1"/"0" conversion.
    """
    body = body or {}
    if body.get("api_url"):
        settings_store.set_setting("clash_api_url", body["api_url"])
    if body.get("secret") is not None:
        # 安全修复：clash_secret 加密存储
        settings_store.set_setting("clash_secret", secure_storage.encrypt(str(body["secret"])))
    if body.get("mixed_port"):
        settings_store.set_setting("clash_mixed_port", int(body["mixed_port"]))
    # 先探测 API 可用性：要求 TCP 端口可连且 /version 和 /configs 均成功返回
    client = _client()
    port_reachable = await asyncio.to_thread(client.is_reachable)
    if not port_reachable:
        return err("Mihomo 外部控制器未正确连接，请确认 Clash/Mihomo 客户端已运行", code=-1)
    ver, cfg = await asyncio.gather(
        _run(client.version),
        _run(client.configs),
    )
    v, ver_err = ver
    c, cfg_err = cfg
    version = v.get("version") if isinstance(v, dict) else None
    api_err = ver_err or cfg_err
    if api_err or not version:
        err_msg = api_err or "无法获取 Mihomo 版本"
        return err(f"Mihomo 外部控制器未正确连接：{err_msg}", code=-1)
    settings_store.set_setting("clash_integrated", "1")
    invalidate_upstream_proxy_cache()
    logger.info("clash", "Clash integration enabled, Mihomo online")
    return ok({"integrated": True, "reachable": True})


@router.put("/clash/disable")
async def clash_disable():
    """Disable Clash integration (traffic direct): clash_integrated=0, clear upstream proxy.

    Does not affect clash_enabled (sidebar entry still visible).
    """
    settings_store.set_setting("clash_integrated", "0")
    invalidate_upstream_proxy_cache()
    logger.info("clash", "Clash integration disabled (traffic direct)")
    return ok({"integrated": False})


@router.put("/clash/config")
async def clash_config(body: dict):
    """Update Clash connection config (api_url/secret/mixed_port)."""
    if body.get("api_url"):
        settings_store.set_setting("clash_api_url", body["api_url"])
    if body.get("secret") is not None:
        # 安全修复：clash_secret 加密存储
        settings_store.set_setting("clash_secret", secure_storage.encrypt(str(body["secret"])))
    if body.get("mixed_port"):
        settings_store.set_setting("clash_mixed_port", int(body["mixed_port"]))
    invalidate_upstream_proxy_cache()
    return ok({"updated": True})


@router.get("/clash/tutorial")
async def clash_tutorial():
    """Return Clash tutorial markdown original text (frontend renders with markdown-it).

    Image relative paths .\\docs\\clash\\xxx.png are replaced by frontend with /docs/clash/xxx.png,
    backend serves image resources via /docs static mount.
    """
    from ..config import get_tutorial_md_path
    path = get_tutorial_md_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return ok({"content": content, "path": path})
    except FileNotFoundError:
        return err(f"Tutorial file not found: {path}")
    except Exception as e:
        return err(f"Failed to read tutorial: {e}")


# ---------- Proxy / Node ----------

@router.get("/clash/proxies")
async def clash_proxies():
    """Get all proxies and policy groups."""
    data, e = await _run(_client().proxies)
    if e:
        return err(e)
    return ok(data)


@router.get("/clash/proxies/{name}")
async def clash_proxy(name: str):
    """Get single proxy/policy group details."""
    data, e = await _run(_client().proxy, name)
    if e:
        return err(e)
    return ok(data)


@router.put("/clash/proxies/{group}")
async def clash_select_proxy(group: str, body: dict = Body(...)):
    """Switch node selected by policy group. body: {"name": "node name"}."""
    name = body.get("name")
    if not name:
        return err("Missing name parameter")
    _, e = await _run(_client().select_proxy, group, name)
    if e:
        return err(e)
    return ok({"selected": name})


@router.get("/clash/proxies/{name}/delay")
async def clash_proxy_delay(name: str, url: str = "https://www.gstatic.com/generate_204",
                            timeout: int = 5000):
    """Test node delay."""
    data, e = await _run(_client().proxy_delay, name, url, timeout)
    if e:
        return err(e)
    return ok(data)


@router.get("/clash/group/{group}/delay")
async def clash_group_delay(group: str, url: str = "https://www.gstatic.com/generate_204",
                            timeout: int = 5000):
    """Test delay for all nodes in policy group."""
    data, e = await _run(_client().group_delay, group, url, timeout)
    if e:
        return err(e)
    return ok(data)


# ---------- Subscription ----------

@router.get("/clash/providers")
async def clash_providers():
    """Get all subscription sources."""
    data, e = await _run(_client().providers)
    if e:
        return err(e)
    return ok(data)


@router.get("/clash/providers/{name}")
async def clash_provider(name: str):
    """Get single subscription source details."""
    data, e = await _run(_client().provider, name)
    if e:
        return err(e)
    return ok(data)


@router.put("/clash/providers/{name}")
async def clash_update_provider(name: str):
    """Update (pull) subscription."""
    _, e = await _run(_client().update_provider, name)
    if e:
        return err(e)
    return ok({"updated": True})


@router.get("/clash/providers/{name}/healthcheck")
async def clash_provider_healthcheck(name: str):
    """Trigger subscription source health check."""
    _, e = await _run(_client().provider_healthcheck, name)
    if e:
        return err(e)
    return ok({"healthcheck": True})


# ---------- Config override ----------

@router.get("/clash/configs")
async def clash_configs():
    """Get Mihomo runtime config."""
    data, e = await _run(_client().configs)
    if e:
        return err(e)
    return ok(data)


@router.patch("/clash/configs")
async def clash_patch_configs(body: dict = Body(...)):
    """Override Mihomo runtime config (mode/log-level/allow-lan etc.)."""
    _, e = await _run(_client().patch_configs, body)
    if e:
        return err(e)
    return ok({"patched": True})


@router.put("/clash/reload")
async def clash_reload(force: bool = False):
    """Reload Mihomo config file."""
    _, e = await _run(_client().reload_config, force)
    if e:
        return err(e)
    return ok({"reloaded": True})


# ---------- Rules ----------

@router.get("/clash/rules")
async def clash_rules():
    """Get rule list."""
    data, e = await _run(_client().rules)
    if e:
        return err(e)
    return ok(data)


@router.get("/clash/rule-providers")
async def clash_rule_providers():
    """Get rule sets."""
    data, e = await _run(_client().rule_providers)
    if e:
        return err(e)
    return ok(data)


@router.put("/clash/rule-providers/{name}")
async def clash_update_rule_provider(name: str):
    """Update rule set."""
    _, e = await _run(_client().update_rule_provider, name)
    if e:
        return err(e)
    return ok({"updated": True})


# ---------- Connections ----------

@router.get("/clash/connections")
async def clash_connections():
    """Get currently active connections."""
    data, e = await _run(_client().connections)
    if e:
        return err(e)
    return ok(data)


@router.delete("/clash/connections")
async def clash_close_all_connections():
    """Close all connections."""
    _, e = await _run(_client().close_all_connections)
    if e:
        return err(e)
    return ok({"closed": "all"})


@router.delete("/clash/connections/{conn_id}")
async def clash_close_connection(conn_id: str):
    """Close specified connection."""
    _, e = await _run(_client().close_connection, conn_id)
    if e:
        return err(e)
    return ok({"closed": conn_id})


# ---------- DNS ----------

@router.get("/clash/dns/query")
async def clash_dns_query(name: str = Query(...), qtype: str = Query("A")):
    """DNS query."""
    data, e = await _run(_client().dns_query, name, qtype)
    if e:
        return err(e)
    return ok(data)


@router.post("/clash/dns/flush")
async def clash_flush_dns():
    """Clear DNS cache."""
    _, e = await _run(_client().flush_dns_cache)
    if e:
        return err(e)
    return ok({"flushed": True})


@router.post("/clash/fakeip/flush")
async def clash_flush_fakeip():
    """Clear FakeIP cache."""
    _, e = await _run(_client().flush_fakeip)
    if e:
        return err(e)
    return ok({"flushed": True})

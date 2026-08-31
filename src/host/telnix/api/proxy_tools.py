"""代理工具 API：No Caching / Force CORS / Block List / Allow List / Map Local / Map Remote / Mirror。

提供：
- 工具开关与规则 CRUD
- 规则命中统计（hit_count / last_hits）
- 规则导入/导出（JSON 格式）
"""
import json
import os
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..proxy import proxy_tools
from ..logger import _capture_log
from . import ok, err

router = APIRouter()


def _get_allowed_base_dirs():
    """Return list of root directories allowed for file access."""
    from ..config import get_cert_dir, get_data_dir
    dirs = [get_data_dir(), get_cert_dir()]
    # 可选：UI dist 目录
    try:
        from ..config import get_ui_dist_dir
        ui_dir = get_ui_dist_dir()
        if ui_dir and os.path.isdir(ui_dir):
            dirs.append(ui_dir)
    except Exception:
        pass
    return [os.path.realpath(d) for d in dirs if os.path.isdir(d)]


@router.get("/proxy-tools")
async def proxy_tools_status():
    """获取代理工具配置。"""
    return ok(proxy_tools.get_config())


@router.put("/proxy-tools")
async def proxy_tools_update(body: dict):
    """批量更新代理工具配置。

    可选字段：no_caching, force_cors, block_list_enabled, allow_list_enabled,
              block_list (array), allow_list (array)
    """
    try:
        return ok(proxy_tools.update_config(body))
    except Exception as e:
        return err(str(e))


@router.post("/proxy-tools/block-list")
async def block_list_add(body: dict):
    """添加黑名单规则。

    body: {pattern: str, mode: "wildcard"|"exact"|"regex"}
    """
    pattern = (body.get("pattern") or "").strip()
    mode = body.get("mode", "wildcard")
    if not pattern:
        return err("pattern is required")
    if mode not in ("wildcard", "exact", "regex"):
        return err("mode must be wildcard, exact, or regex")
    cfg = proxy_tools.get_config()
    lst = cfg["block_list"]
    lst.append({"pattern": pattern, "mode": mode})
    return ok(proxy_tools.update_config({"block_list": lst}))


@router.delete("/proxy-tools/block-list/{index}")
async def block_list_delete(index: int):
    """删除黑名单规则（按索引）。"""
    cfg = proxy_tools.get_config()
    lst = cfg["block_list"]
    if index < 0 or index >= len(lst):
        return err("index out of range")
    lst.pop(index)
    return ok(proxy_tools.update_config({"block_list": lst}))


@router.post("/proxy-tools/allow-list")
async def allow_list_add(body: dict):
    """添加白名单规则。

    body: {pattern: str, mode: "wildcard"|"exact"|"regex"}
    """
    pattern = (body.get("pattern") or "").strip()
    mode = body.get("mode", "wildcard")
    if not pattern:
        return err("pattern is required")
    if mode not in ("wildcard", "exact", "regex"):
        return err("mode must be wildcard, exact, or regex")
    cfg = proxy_tools.get_config()
    lst = cfg["allow_list"]
    lst.append({"pattern": pattern, "mode": mode})
    return ok(proxy_tools.update_config({"allow_list": lst}))


@router.delete("/proxy-tools/allow-list/{index}")
async def allow_list_delete(index: int):
    """删除白名单规则（按索引）。"""
    cfg = proxy_tools.get_config()
    lst = cfg["allow_list"]
    if index < 0 or index >= len(lst):
        return err("index out of range")
    lst.pop(index)
    return ok(proxy_tools.update_config({"allow_list": lst}))


# ---------- Map Local ----------

@router.post("/proxy-tools/map-local")
async def map_local_add(body: dict):
    """添加 Map Local 规则。

    body: {pattern: str, mode: "wildcard"|"exact"|"regex", file_path: str,
           status?: int, content_type?: str, headers?: {key: pattern}}
    """
    import os
    pattern = (body.get("pattern") or "").strip()
    mode = body.get("mode", "wildcard")
    file_path = (body.get("file_path") or "").strip()
    if not pattern:
        return err("pattern is required")
    if not file_path:
        return err("file_path is required")
    if mode not in ("wildcard", "exact", "regex"):
        return err("mode must be wildcard, exact, or regex")
    # 安全修复：路径白名单校验
    try:
        # 解析真实路径
        real_path = os.path.realpath(file_path)
        allowed_dirs = _get_allowed_base_dirs()
        if not any(real_path == d or real_path.startswith(d + os.sep) for d in allowed_dirs):
            return err("file_path is outside the allowed directories (data_dir/cert_dir/ui_dir)")
        if not os.path.isfile(real_path):
            return err(f"file_path does not exist or is not a file: {file_path}")
    except Exception as e:  # noqa: BLE001
        return err(f"Invalid file_path: {e}")
    rule = {"pattern": pattern, "mode": mode, "file_path": file_path}
    # Header 条件（支持 {"X-Token": "*abc*"} 格式）
    rule_headers = body.get("headers")
    if rule_headers and isinstance(rule_headers, dict):
        rule["headers"] = rule_headers
    if "status" in body and body["status"] is not None:
        try:
            rule["status"] = int(body["status"])
        except (TypeError, ValueError):
            pass
    if "content_type" in body and body["content_type"]:
        rule["content_type"] = str(body["content_type"])
    cfg = proxy_tools.get_config()
    lst = cfg["map_local_rules"]
    lst.append(rule)
    return ok(proxy_tools.update_config({"map_local_rules": lst}))


@router.delete("/proxy-tools/map-local/{index}")
async def map_local_delete(index: int):
    """删除 Map Local 规则（按索引）。"""
    cfg = proxy_tools.get_config()
    lst = cfg["map_local_rules"]
    if index < 0 or index >= len(lst):
        return err("index out of range")
    lst.pop(index)
    return ok(proxy_tools.update_config({"map_local_rules": lst}))


# ---------- Map Remote ----------

@router.post("/proxy-tools/map-remote")
async def map_remote_add(body: dict):
    """添加 Map Remote 规则。

    body: {pattern: str, mode: "wildcard"|"exact"|"regex", target_url: str,
           headers?: {key: pattern}}
    """
    from urllib.parse import urlparse
    pattern = (body.get("pattern") or "").strip()
    mode = body.get("mode", "wildcard")
    target_url = (body.get("target_url") or "").strip()
    if not pattern:
        return err("pattern is required")
    if not target_url:
        return err("target_url is required")
    if mode not in ("wildcard", "exact", "regex"):
        return err("mode must be wildcard, exact, or regex")
    # 安全修复：SSRF 校验，参照 send.py 的 _resolve_safe_target 实现
    try:
        parsed = urlparse(target_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not host:
            return err("target_url must have a valid host")
        # SSRF 校验
        from .send import _resolve_safe_target
        resolved_ip = _resolve_safe_target(host, port)
        if resolved_ip is None:
            return err("target_url points to internal/private/loopback address (SSRF risk)")
    except Exception as e:  # noqa: BLE001
        return err(f"Invalid target_url: {e}")
    rule = {"pattern": pattern, "mode": mode, "target_url": target_url}
    # Header 条件
    rule_headers = body.get("headers")
    if rule_headers and isinstance(rule_headers, dict):
        rule["headers"] = rule_headers
    cfg = proxy_tools.get_config()
    lst = cfg["map_remote_rules"]
    lst.append(rule)
    return ok(proxy_tools.update_config({"map_remote_rules": lst}))


@router.delete("/proxy-tools/map-remote/{index}")
async def map_remote_delete(index: int):
    """删除 Map Remote 规则（按索引）。"""
    cfg = proxy_tools.get_config()
    lst = cfg["map_remote_rules"]
    if index < 0 or index >= len(lst):
        return err("index out of range")
    lst.pop(index)
    return ok(proxy_tools.update_config({"map_remote_rules": lst}))


# ---------- Mirror ----------

@router.post("/proxy-tools/mirror")
async def mirror_add(body: dict):
    """添加 Mirror 规则。

    body: {pattern: str, mode: "wildcard"|"exact"|"regex", save_dir: str}
    """
    pattern = (body.get("pattern") or "").strip()
    mode = body.get("mode", "wildcard")
    save_dir = (body.get("save_dir") or "").strip()
    if not pattern:
        return err("pattern is required")
    if not save_dir:
        return err("save_dir is required")
    if mode not in ("wildcard", "exact", "regex"):
        return err("mode must be wildcard, exact, or regex")
    rule = {"pattern": pattern, "mode": mode, "save_dir": save_dir}
    cfg = proxy_tools.get_config()
    lst = cfg["mirror_rules"]
    lst.append(rule)
    return ok(proxy_tools.update_config({"mirror_rules": lst}))


@router.delete("/proxy-tools/mirror/{index}")
async def mirror_delete(index: int):
    """删除 Mirror 规则（按索引）。"""
    cfg = proxy_tools.get_config()
    lst = cfg["mirror_rules"]
    if index < 0 or index >= len(lst):
        return err("index out of range")
    lst.pop(index)
    return ok(proxy_tools.update_config({"mirror_rules": lst}))


# ---------- 命中统计 ----------

@router.get("/proxy-tools/stats")
async def proxy_tools_stats():
    """获取所有规则的命中统计。

    返回结构:
    {
      "map_local_rules": {
        "0": {"hit_count": 10, "last_hits": [{"ts": 123..., "url": "...", "resolved_path": "..."}]},
        "1": {"hit_count": 5, "last_hits": [...]},
      },
      "map_remote_rules": {...}
    }
    """
    return ok(proxy_tools.get_hit_stats())


@router.post("/proxy-tools/stats/clear")
async def proxy_tools_stats_clear(body: dict = None):
    """清除命中统计。

    body: {rule_type?: "map_local_rules"|"map_remote_rules", rule_index?: int}
    - 只有 rule_type: 清除该类型所有规则
    - 只有 rule_index: 无效（需要 rule_type）
    - 全为空: 清除全部
    """
    rule_type = None
    rule_index = None
    if body:
        rule_type = body.get("rule_type")
        rule_index = body.get("rule_index")
    proxy_tools.clear_hit_stats(rule_type, rule_index)
    return ok({"cleared": True})


# ---------- 规则导入/导出 ----------

@router.get("/proxy-tools/export")
async def proxy_tools_export():
    """导出所有 Map Local / Map Remote / Mirror 规则为 JSON 文件。"""
    cfg = proxy_tools.get_config()
    export_data = {
        "version": 1,
        "exported_at": __import__("datetime").datetime.now().isoformat(),
        "rules": {
            "map_local": cfg.get("map_local_rules", []),
            "map_remote": cfg.get("map_remote_rules", []),
            "mirror": cfg.get("mirror_rules", []),
        },
    }
    json_str = json.dumps(export_data, ensure_ascii=False, indent=2)
    return StreamingResponse(
        iter([json_str]),
        media_type="application/json",
        headers={
            "Content-Disposition": "attachment; filename=telnix_proxy_rules.json",
        },
    )


@router.post("/proxy-tools/import")
async def proxy_tools_import(body: dict):
    """导入规则。

    body: {
      rules: {
        "map_local": [...],
        "map_remote": [...],
        "mirror": [...]
      },
      mode: "replace" | "merge" | "skip_conflict"
    }

    mode 说明：
    - replace: 替换所有现有规则
    - merge: 追加到现有规则
    - skip_conflict: 追加但跳过重复 pattern

    返回: {imported: int, skipped: int, conflicts: []}
    """
    rules_data = body.get("rules", {})
    mode = body.get("mode", "merge")

    cfg = proxy_tools.get_config()
    conflicts = []
    imported = {"map_local": 0, "map_remote": 0, "mirror": 0}
    skipped = {"map_local": 0, "map_remote": 0, "mirror": 0}

    for rule_type in ("map_local", "map_remote", "mirror"):
        source_rules = rules_data.get(rule_type, [])
        if not source_rules:
            continue

        # 转换规则类型名称
        cfg_key = rule_type + "_rules"
        existing = cfg.get(cfg_key, [])

        if mode == "replace":
            # 替换模式：直接替换
            pass
        elif mode in ("merge", "skip_conflict"):
            # 合并模式：检查重复
            for new_rule in source_rules:
                pattern = new_rule.get("pattern", "")
                mode_val = new_rule.get("mode", "wildcard")
                # 检查是否已存在相同 pattern + mode 的规则
                is_duplicate = any(
                    r.get("pattern") == pattern and r.get("mode") == mode_val
                    for r in existing
                )
                if is_duplicate:
                    conflicts.append({"rule_type": rule_type, "pattern": pattern, "mode": mode_val})
                    if mode == "skip_conflict":
                        skipped[rule_type] += 1
                        continue
                existing.append(new_rule)
                imported[rule_type] += 1

        if mode == "replace":
            cfg[cfg_key] = source_rules
            imported[rule_type] = len(source_rules)

    # 保存配置
    update_body = {}
    for rule_type in ("map_local", "map_remote", "mirror"):
        cfg_key = rule_type + "_rules"
        update_body[cfg_key] = cfg[cfg_key]

    proxy_tools.update_config(update_body)

    total_imported = sum(imported.values())
    total_skipped = sum(skipped.values())
    return ok({
        "imported": total_imported,
        "skipped": total_skipped,
        "conflicts": conflicts,
        "details": imported,
    })


@router.post("/proxy-tools/validate-import")
async def proxy_tools_validate_import(body: dict):
    """预览导入规则，检测冲突（不实际导入）。

    body: {rules: {...}}

    返回: {valid: bool, conflicts: [], warnings: []}
    """
    rules_data = body.get("rules", {})
    cfg = proxy_tools.get_config()
    conflicts = []
    warnings = []

    for rule_type in ("map_local", "map_remote", "mirror"):
        source_rules = rules_data.get(rule_type, [])
        cfg_key = rule_type + "_rules"
        existing = cfg.get(cfg_key, [])

        for new_rule in source_rules:
            pattern = new_rule.get("pattern", "")
            mode_val = new_rule.get("mode", "wildcard")

            # 检查与现有规则冲突
            for r in existing:
                if r.get("pattern") == pattern and r.get("mode") == mode_val:
                    conflicts.append({
                        "rule_type": rule_type,
                        "pattern": pattern,
                        "mode": mode_val,
                        "existing_target": r.get("file_path") or r.get("target_url") or r.get("save_dir", ""),
                    })

            # 检查规则格式警告
            if not pattern:
                warnings.append({"rule_type": rule_type, "message": "空 pattern 将被跳过"})

            if rule_type in ("map_local", "mirror"):
                file_path = new_rule.get("file_path") or new_rule.get("save_dir", "")
                if file_path and not os.path.exists(file_path):
                    warnings.append({
                        "rule_type": rule_type,
                        "pattern": pattern,
                        "message": f"文件/目录不存在: {file_path}",
                    })

    return ok({
        "valid": len(conflicts) == 0,
        "conflicts": conflicts,
        "warnings": warnings,
    })

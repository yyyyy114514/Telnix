"""AI Provider 抽象基类定义。

所有 AI Provider（DeepSeek/OpenAI/Anthropic/Gemini/Ollama）必须实现此接口，
以提供统一的调用方式和一致的错误处理。
"""

import ssl
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import httpx

from ... import db, logger


@dataclass
class ChatResult:
    """Chat API 返回结果。"""
    ok: bool
    result: str = ""
    tool_results: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""
    # 扩展字段（某些 provider 可能返回额外信息）
    extra: dict = field(default_factory=dict)


@dataclass
class ModelInfo:
    """模型信息。"""
    id: str
    name: str
    input_price: float = 0.0  # 每百万 token 价格（RMB）
    output_price: float = 0.0


class BaseAIProvider(ABC):
    """AI Provider 抽象基类。

    所有支持的 AI 服务必须继承此类并实现其方法。
    """

    name: str = "Unknown"
    service_key: str = ""  # 用于设置存储的键名（如 "deepseek"）

    # Provider 支持的模型列表（子类覆盖）
    MODELS: list[ModelInfo] = []

    def __init__(self, api_key: str = "", **kwargs):
        """初始化 Provider。

        Args:
            api_key: API 密钥
            **kwargs: 其他配置参数（如 endpoint, base_url 等）
        """
        self.api_key = api_key
        self._config = kwargs

    @property
    def default_model(self) -> str:
        """默认模型 ID。"""
        if self.MODELS:
            return self.MODELS[0].id
        return ""

    @property
    def available_models(self) -> list[str]:
        """可用模型 ID 列表。"""
        return [m.id for m in self.MODELS]

    def get_model_info(self, model_id: str) -> Optional[ModelInfo]:
        """获取模型信息。"""
        for m in self.MODELS:
            if m.id == model_id:
                return m
        return None

    def _create_client(self) -> httpx.Client:
        """创建 httpx 客户端。"""
        if os.environ.get("TELNIX_AI_INSECURE") == "1":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return httpx.Client(verify=ctx, timeout=180, trust_env=False)
        return httpx.Client(timeout=180, trust_env=False)

    def _record_usage(self, chat_id: int, model: str,
                      input_tokens: int, output_tokens: int) -> None:
        """记录 AI 使用量。"""
        model_info = self.get_model_info(model)
        if model_info:
            input_price = model_info.input_price
            output_price = model_info.output_price
        else:
            input_price = output_price = 0.0

        total_cost = (
            input_tokens / 1_000_000 * input_price +
            output_tokens / 1_000_000 * output_price
        )

        db.record_ai_usage(
            chat_id=chat_id,
            service=self.service_key,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_cost=total_cost,
        )

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        chat_id: int = 0,
    ) -> ChatResult:
        """发送聊天消息（非流式）。

        Args:
            messages: 消息列表，格式为 [{"role": "user", "content": "..."}]
            model: 模型 ID（None 使用默认模型）
            tools: 工具定义列表
            chat_id: 会话 ID（用于记录用量）

        Returns:
            ChatResult 对象
        """
        pass

    @abstractmethod
    async def stream_chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        chat_id: int = 0,
    ) -> AsyncIterator[str]:
        """发送聊天消息（流式）。

        Args:
            messages: 消息列表
            model: 模型 ID
            tools: 工具定义列表
            chat_id: 会话 ID

        Yields:
            流式文本片段
        """
        yield ""

    def _get_api_key_from_settings(self, key_name: str) -> str:
        """从设置中获取并解密 API Key。"""
        raw_key = db.get_setting(key_name, "")
        try:
            from ... import secure_storage
            if secure_storage.is_encrypted(raw_key):
                return secure_storage.decrypt(raw_key)
        except ImportError:
            pass
        return raw_key

    def _get_endpoint_from_settings(self, key_name: str) -> str:
        """从设置中获取自定义端点。"""
        return db.get_setting(key_name, "")


# ---------- 工具执行相关 ----------

SENSITIVE_KEYS = (
    "authorization", "cookie", "set-cookie", "token", "api_key",
    "apikey", "secret", "password", "x-api-key",
)


def redact_sensitive(obj: Any, depth: int = 0) -> Any:
    """递归脱敏敏感键。"""
    if depth > 6:
        return obj
    if isinstance(obj, dict):
        return {
            k: ("***" if k.lower() in SENSITIVE_KEYS else redact_sensitive(v, depth + 1))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_sensitive(v, depth + 1) for v in obj]
    return obj


def execute_tool(name: str, arguments: dict) -> str:
    """执行工具调用。"""
    import json

    if name == "create_auto_reply_rule":
        url_pattern = arguments.get("url_pattern", "")
        field_key = arguments.get("field_key", "")
        field_value = arguments.get("field_value", "")
        note = arguments.get("note", "") or ""
        modify_target = str(arguments.get("modify_target", "response")).lower().strip()
        if modify_target not in ("request", "response"):
            modify_target = "response"
        is_request = modify_target == "request"
        action = "modify_request" if is_request else "modify_response"
        body_target = "request_body" if is_request else "response_body"
        modify_rules = json.dumps([{
            "target": body_target,
            "op": "replace",
            "key": field_key,
            "value": field_value,
        }])
        rule_id = db.add_rule({
            "enabled": 1,
            "match_mode": "wildcard",
            "pattern": url_pattern,
            "action": action,
            "modify_rules": modify_rules,
            "note": note,
        })
        try:
            from ...auto_reply.rules import invalidate_cache
            invalidate_cache()
        except Exception:  # noqa: BLE001
            pass
        target_label = "request body" if is_request else "response body"
        logger.info("ai", f"AI created auto-modify rule ({action}): {url_pattern} -> {field_key}={field_value}",
                    f"rule_id={rule_id}, note={note}")
        return (f"Created auto-modify rule (ID: {rule_id}): URL pattern '{url_pattern}', "
                f"replace {target_label} field '{field_key}' with '{field_value}'")

    elif name == "create_script_rule":
        url_pattern = arguments.get("url_pattern", "")
        script = arguments.get("script", "")
        note = arguments.get("note", "") or ""
        modify_target = str(arguments.get("modify_target", "response")).lower().strip()
        if modify_target not in ("request", "response"):
            modify_target = "response"
        rule_id = db.add_rule({
            "enabled": 1,
            "match_mode": "wildcard",
            "pattern": url_pattern,
            "action": "script",
            "modify_rules": script,
            "note": note,
        })
        try:
            from ...auto_reply.rules import invalidate_cache
            invalidate_cache()
        except Exception:  # noqa: BLE001
            pass
        target_label = "request" if modify_target == "request" else "response"
        return (f"Created script rule (ID: {rule_id}): URL pattern '{url_pattern}', "
                f"modify {target_label} with custom Python script.")

    elif name == "list_auto_reply_rules":
        rules = db.get_rules()
        if not rules:
            return json.dumps({"count": 0, "rules": []}, ensure_ascii=False)
        rule_list = [{
            "rule_id": r["id"],
            "enabled": bool(r["enabled"]),
            "match_mode": r["match_mode"],
            "pattern": r["pattern"],
            "action": r["action"],
            "note": r.get("note", ""),
        } for r in rules]
        return json.dumps({"count": len(rule_list), "rules": rule_list}, ensure_ascii=False)

    elif name == "delete_auto_reply_rule":
        rule_id_raw = arguments.get("rule_id", "")
        ids = [s.strip() for s in str(rule_id_raw).split(",") if s.strip()]
        if not ids:
            return "No rule ID provided for deletion"
        results = []
        for rule_id in ids:
            existing = db.get_rule(rule_id)
            if not existing:
                rules = db.get_rules()
                all_ids = [r["id"] for r in rules]
                results.append({"rule_id": rule_id, "ok": False, "error": "Rule does not exist", "current_ids": all_ids})
                continue
            db.delete_rule(rule_id)
            still = db.get_rule(rule_id)
            results.append({"rule_id": rule_id, "ok": not still, "pattern": existing.get("pattern", "")})
            if not still:
                logger.info("ai", f"AI deleted rule {rule_id}")
        try:
            from ...auto_reply.rules import invalidate_cache
            invalidate_cache()
        except Exception:  # noqa: BLE001
            pass
        return json.dumps({"results": results}, ensure_ascii=False)

    elif name == "delete_all_auto_reply_rules":
        rules = db.get_rules()
        if not rules:
            return json.dumps({"deleted": 0, "msg": "No rules currently exist"}, ensure_ascii=False)
        count = 0
        for r in rules:
            try:
                db.delete_rule(r["id"])
                count += 1
            except Exception:  # noqa: BLE001
                pass
        try:
            from ...auto_reply.rules import invalidate_cache
            invalidate_cache()
        except Exception:  # noqa: BLE001
            pass
        logger.info("ai", f"AI deleted all rules, total {count}")
        return json.dumps({"deleted": count, "msg": f"Deleted {count} rule(s)"})

    elif name == "dns_hijack_stats":
        try:
            from ...proxy.dns_hijack import get_stats, get_hijack_log
            stats = get_stats()
            log = get_hijack_log()
            return json.dumps({
                "stats": stats,
                "log_count": len(log),
                "log": log[-50:],
            }, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    elif name == "query_traffic":
        try:
            protocol = arguments.get("protocol", "http")
            host = arguments.get("host", None)
            limit = int(arguments.get("limit", 10000))
            session_id = db.get_current_session_id() or 0
            flows = db.get_flows(
                session_id, limit=limit, offset=0,
                host=host, protocol=protocol if protocol != "http" else None,
            )
            out = [{
                "id": f.get("id"),
                "protocol": f.get("protocol", "http"),
                "method": f.get("method", ""),
                "url": f.get("url", ""),
                "host": f.get("host", ""),
                "status_code": f.get("status_code"),
                "size": f.get("size", 0),
                "process_name": f.get("process_name", ""),
                "pid": f.get("pid"),
                "timestamp": f.get("timestamp", ""),
            } for f in flows[:limit]]
            return json.dumps({"count": len(out), "flows": out}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    elif name == "send_request":
        try:
            url = arguments.get("url", "")
            method = arguments.get("method", "GET").upper()
            extra_headers = arguments.get("headers", "")
            body = arguments.get("body", "")
            headers = {}
            if extra_headers:
                try:
                    headers.update(json.loads(extra_headers))
                except Exception:  # noqa: BLE001
                    pass
            client = httpx.Client(timeout=15.0)
            resp = client.request(method, url, headers=headers, content=body if body else None)
            result = {
                "status_code": resp.status_code,
                "headers": dict(resp.headers),
                "body": resp.text[:5000] if resp.text else "",
            }
            client.close()
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    elif name == "global_analysis":
        try:
            focus = arguments.get("focus", "all")
            session_id = db.get_current_session_id() or 0
            http_flows = db.get_flows(session_id, limit=10000, offset=0, protocol=None)
            tcp_flows = db.get_flows(session_id, limit=5000, offset=0, protocol="tcp")
            udp_flows = db.get_flows(session_id, limit=5000, offset=0, protocol="udp")
            dns_data = {"stats": {}, "log_count": 0}
            try:
                from ...proxy.dns_hijack import get_stats, get_hijack_log
                dns_data = {"stats": get_stats(), "log": get_hijack_log()[-20:], "log_count": len(get_hijack_log())}
            except Exception:  # noqa: BLE001
                pass
            proto_counts = {}
            for f in http_flows:
                p = f.get("protocol", "http")
                proto_counts[p] = proto_counts.get(p, 0) + 1
            for f in tcp_flows:
                p = f.get("protocol", "tcp")
                proto_counts[p] = proto_counts.get(p, 0) + 1
            for f in udp_flows:
                p = f.get("protocol", "udp")
                proto_counts[p] = proto_counts.get(p, 0) + 1
            report = {
                "session_id": session_id,
                "focus": focus,
                "summary": {
                    "total_flows": len(http_flows) + len(tcp_flows) + len(udp_flows),
                    "http_flows": len(http_flows),
                    "tcp_flows": len(tcp_flows),
                    "udp_flows": len(udp_flows),
                    "protocol_distribution": proto_counts,
                    "dns_hijack_log_count": dns_data.get("log_count", 0),
                },
                "recent_http": [
                    {"id": f.get("id"), "method": f.get("method", ""), "url": f.get("url", ""),
                     "host": f.get("host", ""), "status": f.get("status_code"), "size": f.get("size", 0)}
                    for f in http_flows[:20]
                ],
            }
            return json.dumps(report, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    return f"Unknown tool: {name}"

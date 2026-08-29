"""AI analysis module: supports multiple AI services (Claude, Claude, GPT-4o, Gemini, Ollama).

Features:
- Multi-service support with unified interface
- SSE streaming responses
- Token usage tracking and cost estimation
"""

import json
import os
import ssl
from dataclasses import dataclass
from typing import Optional

import httpx

from .. import db, logger

# Default API endpoints
DEFAULT_DEEPSEEK_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

# Supported AI services
AI_SERVICES = {
    "deepseek": {
        "name": "DeepSeek",
        "default_model": "deepseek-v4-flash",
        "supported_models": ["deepseek-v4-flash", "deepseek-v4-pro"],
        "api_type": "openai_compatible",
        "pricing": {  # RMB per 1M tokens (input/output)
            "deepseek-v4-flash": (0.1, 0.5),
            "deepseek-v4-pro": (2.0, 8.0),
        },
    },
    "anthropic": {
        "name": "Claude (Anthropic)",
        "default_model": "claude-3-5-sonnet-20241022",
        "supported_models": ["claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"],
        "api_type": "anthropic",
        "api_url": "https://api.anthropic.com/v1/messages",
        "pricing": {
            "claude-3-5-sonnet-20241022": (11.0, 32.0),
            "claude-3-5-haiku-20241022": (0.8, 4.0),
            "claude-3-opus-20240229": (90.0, 270.0),
        },
    },
    "openai": {
        "name": "GPT (OpenAI)",
        "default_model": "gpt-4o",
        "supported_models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
        "api_type": "openai",
        "api_url": "https://api.openai.com/v1/chat/completions",
        "pricing": {
            "gpt-4o": (15.0, 60.0),
            "gpt-4o-mini": (0.75, 3.0),
            "gpt-4-turbo": (30.0, 90.0),
            "gpt-3.5-turbo": (0.5, 1.5),
        },
    },
    "gemini": {
        "name": "Gemini (Google)",
        "default_model": "gemini-1.5-pro",
        "supported_models": ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash-exp"],
        "api_type": "google",
        "api_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "pricing": {  # Free tier up to limits, then per 1M tokens
            "gemini-1.5-pro": (3.5, 10.5),
            "gemini-1.5-flash": (0.075, 0.3),
            "gemini-2.0-flash-exp": (0.0, 0.0),  # Free tier
        },
    },
    "ollama": {
        "name": "Ollama (Local)",
        "default_model": "llama3.1",
        "supported_models": [],  # Dynamic, depends on installed models
        "api_type": "ollama",
        "api_url": DEFAULT_OLLAMA_URL,
        "pricing": (0.0, 0.0),  # Local, no cost
    },
}


@dataclass
class AIUsageRecord:
    """AI usage record for tracking costs."""
    chat_id: int
    service: str
    model: str
    input_tokens: int
    output_tokens: int
    total_cost: float  # RMB


def _get_current_service() -> str:
    """Get the currently selected AI service."""
    return db.get_setting("ai_service", "deepseek")


def _get_service_config(service: str) -> dict:
    """Get configuration for a specific AI service."""
    config = AI_SERVICES.get(service, AI_SERVICES["deepseek"])
    # Get API key from settings
    key_map = {
        "deepseek": "deepseek_api_key",
        "anthropic": "anthropic_api_key",
        "openai": "openai_api_key",
        "gemini": "gemini_api_key",
        "ollama": "ollama_api_key",
    }
    key_name = key_map.get(service, "")
    raw_key = db.get_setting(key_name, "")
    # Decrypt if encrypted
    from .. import secure_storage
    api_key = secure_storage.decrypt(raw_key) if secure_storage.is_encrypted(raw_key) else raw_key

    # Get custom endpoint if configured
    endpoint_key = f"{service}_endpoint"
    endpoint = db.get_setting(endpoint_key, "")

    return {
        **config,
        "api_key": api_key,
        "endpoint": endpoint,
    }


def _get_model(service: str) -> str:
    """Get the user-selected model for a service."""
    model_key = f"{service}_model"
    default = AI_SERVICES.get(service, {}).get("default_model", "")
    return db.get_setting(model_key, default)


def _record_usage(chat_id: int, service: str, model: str,
                  input_tokens: int, output_tokens: int) -> AIUsageRecord:
    """Record AI usage and calculate cost."""
    pricing = AI_SERVICES.get(service, {}).get("pricing", {})
    model_pricing = pricing.get(model, (0, 0))
    if isinstance(model_pricing, tuple):
        input_price, output_price = model_pricing
    else:
        input_price, output_price = 0, 0

    total_cost = (input_tokens / 1_000_000 * input_price +
                  output_tokens / 1_000_000 * output_price)

    record = AIUsageRecord(
        chat_id=chat_id,
        service=service,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_cost=total_cost,
    )

    # Store in database
    db.record_ai_usage(
        chat_id=chat_id,
        service=service,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_cost=total_cost,
    )

    return record


def get_ai_usage_stats() -> dict:
    """Get AI usage statistics (today, this month, all time)."""
    return db.get_ai_usage_stats()


# ---------- Tool definitions ----------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_auto_reply_rule",
            "description": (
                "Create an auto-modify rule to modify a JSON field in the HTTP request or response body matching a URL. "
                "Call this tool when the user asks to modify a request or response field value of an interface. "
                "By default modifies the response (modify_target='response'); when the user says 'change request' / 'tamper request' / 'forge request parameters', pass modify_target='request'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url_pattern": {
                        "type": "string",
                        "description": (
                            "URL match pattern, supports wildcard *. Example: *steamstart.top*"
                        ),
                    },
                    "field_key": {
                        "type": "string",
                        "description": (
                            "The JSON field name to modify. You can fill in the field name directly (e.g. remainingUses), "
                            "which will be globally searched and replaced; or fill in the full path (e.g. data.status.remainingUses) for precise targeting."
                        ),
                    },
                    "field_value": {
                        "type": "string",
                        "description": "The replacement value. Numbers like 99999, strings as plain text without quotes.",
                    },
                    "modify_target": {
                        "type": "string",
                        "enum": ["response", "request"],
                        "description": "Modify target: 'response' (default) = modify response body field; 'request' = modify request body field (tamper before forwarding, used to forge request parameters to test server-side validation). When the user does not explicitly say request or response, default to 'response'.",
                    },
                    "note": {
                        "type": "string",
                        "description": "(Required) The note/comment for the rule, briefly describing what this rule does, for future identification. Example: 'Modify the remaining count returned by login to 99999'.",
                    },
                },
                "required": ["url_pattern", "field_key", "field_value", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_script_rule",
            "description": (
                "Create an auto-modify rule with a custom Python script to modify the HTTP request or response. "
                "Use this when the user wants a complex modification that can't be done with simple field replacement "
                "(e.g. conditional logic, multi-field updates, computed values, body rewriting). "
                "The script runs in a sandboxed subprocess and must define an on_request(ctx) and/or on_response(ctx) function."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url_pattern": {
                        "type": "string",
                        "description": "URL wildcard pattern to match (e.g. '*.example.com/api/*')",
                    },
                    "script": {
                        "type": "string",
                        "description": "Python script source code. Must define on_request(ctx) and/or on_response(ctx).",
                    },
                    "modify_target": {
                        "type": "string",
                        "enum": ["request", "response"],
                        "description": "Whether to modify request or response. Default: response.",
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional note for this rule",
                    },
                },
                "required": ["url_pattern", "script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_auto_reply_rules",
            "description": "List all current auto-modify rules, including enabled status and match patterns.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_auto_reply_rule",
            "description": "Delete auto-modify rules by specified ID. Supports a single ID or multiple IDs (comma-separated).",
            "parameters": {
                "type": "object",
                "properties": {
                    "rule_id": {
                        "type": "string",
                        "description": "The rule ID(s) to delete. Multiple IDs comma-separated, e.g. 'abc123,def456'.",
                    },
                },
                "required": ["rule_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_all_auto_reply_rules",
            "description": "Delete all auto-modify rules (clear all at once). Call this tool when the user says 'delete all rules' / 'clear rules'.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dns_hijack_stats",
            "description": "Query DNS hijack statistics and logs.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_traffic",
            "description": "Query captured traffic flows with filtering.",
            "parameters": {
                "type": "object",
                "properties": {
                    "protocol": {
                        "type": "string",
                        "enum": ["http", "tcp", "udp"],
                        "description": "Protocol filter: 'http' (default), 'tcp', or 'udp'",
                    },
                    "host": {"type": "string", "description": "Filter by host name (substring match)"},
                    "limit": {"type": "integer", "description": "Max number of results (default: 10000)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_request",
            "description": "Send an HTTP request to test or probe a target.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to send the request to"},
                    "method": {"type": "string", "description": "HTTP method: GET (default), POST, PUT, DELETE"},
                    "headers": {"type": "string", "description": "Extra headers as JSON string"},
                    "body": {"type": "string", "description": "Request body (for POST/PUT)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "global_analysis",
            "description": "Generate a comprehensive analysis report from all captured traffic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "enum": ["overview", "security", "performance", "all"],
                        "description": "Analysis focus: 'overview' (default), 'security', 'performance', or 'all'",
                    },
                },
                "required": [],
            },
        },
    },
]


def _parse_dsml_tool_calls(text: str) -> list[dict]:
    """Parse DSML-format tool calls from Claude text output."""
    import re
    results: list[dict] = []
    invoke_re = re.compile(
        r'<｜｜DSML｜｜invoke\s+name="([^"]+)">(.*?)</｜｜DSML｜｜invoke>',
        re.DOTALL,
    )
    param_re = re.compile(
        r'<｜｜DSML｜｜parameter\s+name="([^"]+)"[^>]*>(.*?)</｜｜DSML｜｜parameter>',
        re.DOTALL,
    )
    for m in invoke_re.finditer(text):
        name = m.group(1).strip()
        body = m.group(2)
        arguments: dict = {}
        for pm in param_re.finditer(body):
            pname = pm.group(1).strip()
            pval = pm.group(2).strip()
            arguments[pname] = pval
        results.append({"name": name, "arguments": arguments})
    return results


def _strip_dsml(text: str) -> str:
    """Remove DSML tag blocks from text, keeping other text."""
    import re
    cleaned = re.sub(
        r'<｜｜DSML｜｜tool_calls>.*?</｜｜DSML｜｜tool_calls>',
        '', text, flags=re.DOTALL,
    )
    cleaned = re.sub(
        r'<｜｜DSML｜｜invoke\s+name="[^"]+">.*?</｜｜DSML｜｜invoke>',
        '', cleaned, flags=re.DOTALL,
    )
    return cleaned.strip()


def _execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool call, returning the result text."""
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
            from ..auto_reply.rules import invalidate_cache
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
            from ..auto_reply.rules import invalidate_cache
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
            from ..auto_reply.rules import invalidate_cache
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
            from ..auto_reply.rules import invalidate_cache
            invalidate_cache()
        except Exception:  # noqa: BLE001
            pass
        logger.info("ai", f"AI deleted all rules, total {count}")
        return json.dumps({"deleted": count, "msg": f"Deleted {count} rule(s)"})

    elif name == "dns_hijack_stats":
        try:
            from ..proxy.dns_hijack import get_stats, get_hijack_log
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
                from ..proxy.dns_hijack import get_stats, get_hijack_log
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


# ---------- System prompt ----------

SYSTEM_PROMPT = (
    "You are a network traffic analysis assistant. The user will provide HTTP/HTTPS captured traffic data. "
    "You need to analyze the purpose, anomalies, key parameters, and authentication information of the traffic in Chinese. "
    "The user may follow up with details about this traffic; please answer based on the provided traffic context.\n\n"
    "You also have Agent capabilities to help the user manage auto-modify rules:\n"
    "- When the user says 'help me set up auto-modify to change xxx field to yyy', call create_auto_reply_rule\n"
    "- When the user asks for complex modifications, call create_script_rule\n"
    "- When the user asks 'list rules', call list_auto_reply_rules\n"
    "- When the user says 'delete rule xxx', call delete_auto_reply_rule\n"
    "- When the user says 'delete all rules', call delete_all_auto_reply_rules\n"
    "- When the user asks about DNS hijack stats, call dns_hijack_stats\n"
    "- When the user asks to query traffic, call query_traffic\n"
    "- When the user asks to send a request, call send_request\n"
    "- When the user asks for global analysis, call global_analysis\n\n"
    "**Important guidelines**:\n"
    "1. When creating a rule, you must fill in the note field\n"
    "2. For rule queries/deletes, you must call list_auto_reply_rules first\n"
    "3. Output in Markdown format.\n"
)


def _build_flow_context(flows: list[dict]) -> str:
    """Build traffic context text."""
    if not flows:
        return "(No associated traffic data for this conversation)"
    snippets = []
    for i, f in enumerate(flows, 1):
        req_headers = _safe_json(f.get("request_headers"))
        resp_headers = _safe_json(f.get("response_headers"))
        snippets.append(
            f"### Flow {i}\n"
            f"- Request: {f.get('method', '')} {f.get('url', '')}\n"
            f"- Process: {f.get('process_name', '')} (PID {f.get('pid', '')})\n"
            f"- Request headers: {json.dumps(req_headers, ensure_ascii=False)}\n"
            f"- Request body: {(f.get('request_body') or '')[:2000]}\n"
            f"- Status code: {f.get('status_code', '')}\n"
            f"- Response headers: {json.dumps(resp_headers, ensure_ascii=False)}\n"
            f"- Response body: {(f.get('response_body') or '')[:4000]}\n"
        )
    return "\n".join(snippets)


def _safe_json(s):
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return {}


def _build_analyze_messages(flows: list[dict]) -> list[dict]:
    """Build the message list for the first analysis."""
    flow_ctx = _build_flow_context(flows)
    if flows:
        content = (
            f"Please analyze the following captured traffic:\n\n{flow_ctx}\n\n"
            "Please analyze:\n1. The purpose and meaning of each flow\n"
            "2. Whether there are anomalous or suspicious requests\n"
            "3. Key parameters, Tokens, authentication information\n"
            "4. Summarize the overall behavior"
        )
    else:
        content = "The user started a free conversation with no associated traffic. Please greet them and ask what help they need."
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def _build_chat_messages(history: list[dict], flow_context: str, user_message: str) -> list[dict]:
    """Build the multi-turn conversation message list."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"The following is the analyzed traffic context; subsequent Q&A is based on it:\n\n{flow_context}"},
    ]
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})
    return messages


# ---------- API calls ----------

_SENSITIVE_KEYS = (
    "authorization", "cookie", "set-cookie", "token", "api_key",
    "apikey", "secret", "password", "x-api-key",
)


def _redact_sensitive(obj, depth: int = 0):
    """Recursively mask sensitive keys and credential fragments for logging."""
    if depth > 6:
        return obj
    if isinstance(obj, dict):
        return {
            k: ("***" if k.lower() in _SENSITIVE_KEYS else _redact_sensitive(v, depth + 1))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_sensitive(v, depth + 1) for v in obj]
    if isinstance(obj, str):
        import re
        return re.sub(
            r'(?i)(authorization|cookie|token|api[_-]?key)\s*[:=]\s*\S+',
            r'\1: ***', obj,
        )
    return obj


def _create_client() -> httpx.Client:
    """Create an httpx client with proper SSL settings."""
    if os.environ.get("TELNIX_AI_INSECURE") == "1":
        logger.warning("ai", "TELNIX_AI_INSECURE=1 enabled, SSL verification disabled")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return httpx.Client(verify=ctx, timeout=180, trust_env=False)
    return httpx.Client(timeout=180, trust_env=False)


def _call_openai_compatible_api(messages: list[dict], service: str,
                                model: str, api_key: str,
                                base_url: str = None,
                                use_tools: bool = False) -> dict:
    """Call OpenAI-compatible API (Claude, OpenAI, Ollama)."""
    config = _get_service_config(service)
    url = base_url or config.get("endpoint") or config.get("api_url", DEFAULT_DEEPSEEK_URL)

    # For Claude, use default URL if no custom endpoint
    if service == "deepseek" and not base_url and not config.get("endpoint"):
        url = DEFAULT_DEEPSEEK_URL

    # For Ollama, use default URL if no custom endpoint
    if service == "ollama" and not base_url and not config.get("endpoint"):
        url = DEFAULT_OLLAMA_URL

    headers = {
        "Content-Type": "application/json",
    }

    # Add auth header for OpenAI-compatible APIs
    if service in ("deepseek", "openai"):
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
    }

    # Ollama doesn't support tools, OpenAI-compatible services do
    if use_tools and service != "ollama":
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"

    try:
        logger.info("ai", f"Calling {service} API, model={model}, messages={len(messages)}, tools={use_tools}")
        with _create_client() as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

            # Extract usage info
            usage = data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

            message = data.get("choices", [{}])[0].get("message", {})

            # Handle tool calls
            tool_calls = message.get("tool_calls", [])
            content_text = message.get("content", "") or ""

            # Parse DSML tool calls if present
            if not tool_calls and "<｜｜DSML｜｜" in content_text:
                parsed = _parse_dsml_tool_calls(content_text)
                if parsed:
                    for i, p in enumerate(parsed):
                        tool_calls.append({
                            "id": f"dsml_{i}",
                            "function": {
                                "name": p["name"],
                                "arguments": json.dumps(p["arguments"], ensure_ascii=False),
                            },
                        })
                    content_text = _strip_dsml(content_text)
                    message["content"] = content_text

            if tool_calls:
                results = []
                messages.append(message)
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except Exception:  # noqa: BLE001
                        args = {}
                    logger.info("ai", f"AI calling tool: {name}", f"args: {_redact_sensitive(args)}")
                    result = _execute_tool(name, args)
                    results.append({"name": name, "result": result})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result,
                    })

                # Second API call with tool results
                logger.info("ai", "Tool calls completed, requesting API again")
                resp2 = client.post(url, headers=headers, json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                })
                resp2.raise_for_status()
                data2 = resp2.json()
                result_text = data2.get("choices", [{}])[0].get("message", {}).get("content", "")
                result_text = _strip_dsml(result_text)
                logger.info("ai", "API call succeeded (with tool calls)")

                # Get usage from second call
                usage2 = data2.get("usage", {})
                input_tokens += usage2.get("prompt_tokens", 0)
                output_tokens += usage2.get("completion_tokens", 0)

                return {
                    "ok": True,
                    "result": result_text,
                    "tool_results": results,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }

            result = content_text or ""
            result = _strip_dsml(result)
            logger.info("ai", f"API call succeeded, reply length={len(result)}")

            return {
                "ok": True,
                "result": result,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }

    except httpx.HTTPStatusError as e:
        logger.error("ai", f"{service} API returned error: {e.response.status_code}",
                     e.response.text[:500])
        return {"ok": False, "error": f"API returned {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:  # noqa: BLE001
        logger.error("ai", f"{service} API request failed: {e}", str(e))
        return {"ok": False, "error": f"Request failed: {e}"}


def _call_anthropic_api(messages: list[dict], api_key: str, model: str,
                        use_tools: bool = False) -> dict:
    """Call Anthropic Claude API."""
    url = "https://api.anthropic.com/v1/messages"

    # Convert messages format for Anthropic
    anthropic_messages = []
    system_content = ""
    for msg in messages:
        if msg["role"] == "system":
            system_content += msg["content"] + "\n\n"
        elif msg["role"] == "user":
            anthropic_messages.append({"role": "user", "content": msg["content"]})
        elif msg["role"] == "assistant":
            anthropic_messages.append({"role": "assistant", "content": msg["content"]})
        elif msg["role"] == "tool":
            anthropic_messages.append({
                "role": "user",
                "content": f"[Tool result: {msg.get('name', 'unknown')}]\n{msg['content']}"
            })

    payload = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": 4096,
    }
    if system_content:
        payload["system"] = system_content

    if use_tools:
        # Convert our tools to Anthropic format
        payload["tools"] = [
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "input_schema": t["function"].get("parameters", {}),
            }
            for t in TOOLS
        ]

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-dangerous-direct-browser-access": "true",
    }

    try:
        logger.info("ai", f"Calling Anthropic API, model={model}")
        with _create_client() as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

            usage = data.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)

            content = data.get("content", [])
            if isinstance(content, list):
                # Handle stop reason
                stop_reason = data.get("stop_reason", "")
                result_text = ""
                for block in content:
                    if block.get("type") == "text":
                        result_text += block.get("text", "")
                    elif block.get("type") == "tool_use":
                        # Handle tool use
                        pass
            else:
                result_text = str(content)

            # Check for tool use in response
            tool_uses = [b for b in content if b.get("type") == "tool_use"] if isinstance(content, list) else []
            if tool_uses:
                results = []
                messages.append({"role": "assistant", "content": content})
                for tool_use in tool_uses:
                    name = tool_use.get("name", "")
                    input_json = tool_use.get("input", {})
                    logger.info("ai", f"AI calling tool: {name}", f"args: {_redact_sensitive(input_json)}")
                    result = _execute_tool(name, input_json)
                    results.append({"name": name, "result": result})
                    messages.append({
                        "role": "user",
                        "content": f"<result_of_tool_call>\n{result}\n</result_of_tool_call>"
                    })

                # Second call with tool results
                resp2 = client.post(url, headers=headers, json={
                    "model": model,
                    "messages": anthropic_messages + [
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": "Continue with the tool results provided above."}
                    ],
                    "max_tokens": 4096,
                })
                resp2.raise_for_status()
                data2 = resp2.json()
                content2 = data2.get("content", [])
                if isinstance(content2, list):
                    for block in content2:
                        if block.get("type") == "text":
                            result_text += block.get("text", "")
                usage2 = data2.get("usage", {})
                input_tokens += usage2.get("input_tokens", 0)
                output_tokens += usage2.get("output_tokens", 0)

                return {
                    "ok": True,
                    "result": result_text,
                    "tool_results": results,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }

            logger.info("ai", f"API call succeeded, reply length={len(result_text)}")
            return {
                "ok": True,
                "result": result_text,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }

    except httpx.HTTPStatusError as e:
        logger.error("ai", f"Anthropic API returned error: {e.response.status_code}",
                     e.response.text[:500])
        return {"ok": False, "error": f"API returned {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:  # noqa: BLE001
        logger.error("ai", f"Anthropic API request failed: {e}", str(e))
        return {"ok": False, "error": f"Request failed: {e}"}


def _call_gemini_api(messages: list[dict], api_key: str, model: str,
                    use_tools: bool = False) -> dict:
    """Call Google Gemini API."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    # Convert messages format for Gemini
    contents = []
    system_instruction = ""
    for msg in messages:
        if msg["role"] == "system":
            system_instruction = msg["content"]
        elif msg["role"] == "user":
            contents.append({"role": "user", "parts": [{"text": msg["content"]}]})
        elif msg["role"] == "assistant":
            contents.append({"role": "model", "parts": [{"text": msg["content"]}]})
        elif msg["role"] == "tool":
            contents.append({
                "role": "user",
                "parts": [{"text": f"[Tool result]\n{msg['content']}"}]
            })

    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0.7,
            "topP": 0.95,
            "topK": 40,
            "maxOutputTokens": 8192,
        },
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    if use_tools:
        # Convert tools to Gemini format (simplified)
        payload["tools"] = [{"functionDeclarations": [
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "parameters": t["function"].get("parameters", {}),
            }
            for t in TOOLS
        ]}]

    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        url = f"{url}?key={api_key}"

    try:
        logger.info("ai", f"Calling Gemini API, model={model}")
        with _create_client() as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

            # Gemini doesn't provide token counts in the same way
            input_tokens = 0
            output_tokens = 0

            candidates = data.get("candidates", [])
            if candidates:
                content = candidates[0].get("content", {})
                parts = content.get("parts", [])
                result_text = ""
                for part in parts:
                    if "text" in part:
                        result_text += part["text"]
                    elif "functionCall" in part:
                        # Handle function call
                        fc = part["functionCall"]
                        name = fc.get("name", "")
                        args = fc.get("args", {})
                        logger.info("ai", f"AI calling tool: {name}", f"args: {_redact_sensitive(args)}")
                        result = _execute_tool(name, args)

                        # Continue with function response
                        resp2 = client.post(url, headers=headers, json={
                            "contents": contents + [{
                                "role": "model",
                                "parts": [part]
                            }, {
                                "role": "user",
                                "parts": [{"functionResponse": {
                                    "name": name,
                                    "response": {"result": result}
                                }}]
                            }],
                            "generationConfig": payload["generationConfig"],
                        })
                        resp2.raise_for_status()
                        data2 = resp2.json()
                        candidates2 = data2.get("candidates", [])
                        if candidates2:
                            parts2 = candidates2[0].get("content", {}).get("parts", [])
                            for p2 in parts2:
                                if "text" in p2:
                                    result_text += p2["text"]

                return {
                    "ok": True,
                    "result": result_text,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }

            return {"ok": False, "error": "No response from Gemini"}

    except httpx.HTTPStatusError as e:
        logger.error("ai", f"Gemini API returned error: {e.response.status_code}",
                     e.response.text[:500])
        return {"ok": False, "error": f"API returned {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:  # noqa: BLE001
        logger.error("ai", f"Gemini API request failed: {e}", str(e))
        return {"ok": False, "error": f"Request failed: {e}"}


def _call_api(messages: list[dict], use_tools: bool = False) -> dict:
    """Call the configured AI service API."""
    service = _get_current_service()
    config = _get_service_config(service)
    api_key = config.get("api_key", "")
    model = _get_model(service)

    if not api_key and service != "ollama":
        return {"ok": False, "error": f"{config['name']} API key not configured"}

    api_type = config.get("api_type", "openai_compatible")

    if api_type == "anthropic":
        return _call_anthropic_api(messages, api_key, model, use_tools)
    elif api_type == "google":
        return _call_gemini_api(messages, api_key, model, use_tools)
    else:
        # OpenAI-compatible (Claude, OpenAI, Ollama)
        return _call_openai_compatible_api(messages, service, model, api_key, use_tools=use_tools)


def analyze_flows(flow_ids: list[int]) -> dict:
    """Analyze the specified flow list."""
    if flow_ids:
        by_id = db.get_flows_by_ids(flow_ids)
        flows = [by_id[i] for i in flow_ids if i in by_id]
    else:
        flows = []
    messages = _build_analyze_messages(flows)
    return _call_api(messages, use_tools=True)


def chat(history: list[dict], flow_context: str, user_message: str) -> dict:
    """Multi-turn conversation."""
    messages = _build_chat_messages(history, flow_context, user_message)
    return _call_api(messages, use_tools=True)


def get_available_models() -> dict:
    """Get available models for each service."""
    result = {}
    for service, config in AI_SERVICES.items():
        # Check if API key is configured
        key_map = {
            "deepseek": "deepseek_api_key",
            "anthropic": "anthropic_api_key",
            "openai": "openai_api_key",
            "gemini": "gemini_api_key",
            "ollama": "ollama_api_key",
        }
        key_name = key_map.get(service, "")
        raw_key = db.get_setting(key_name, "")
        from .. import secure_storage
        has_key = bool(secure_storage.decrypt(raw_key) if secure_storage.is_encrypted(raw_key) else raw_key)

        # Get selected model for this service
        selected_model = _get_model(service) or config.get("default_model", "")

        result[service] = {
            "name": config["name"],
            "models": config.get("supported_models", []),
            "default_model": config.get("default_model", ""),
            "selected_model": selected_model,
            "has_api_key": has_key,
            "pricing": config.get("pricing", {}),
        }

    return result


def get_service_status() -> dict:
    """Get status of all AI services."""
    current = _get_current_service()
    available = get_available_models()

    return {
        "current_service": current,
        "services": available,
    }

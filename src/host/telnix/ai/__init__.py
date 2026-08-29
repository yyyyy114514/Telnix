"""AI 分析模块：支持多 AI 服务（Claude/DeepSeek/GPT/Gemini/Ollama）。

特性：
- 多服务支持，统一接口
- SSE 流式响应
- Token 使用量追踪与成本估算
"""

import json
from typing import AsyncIterator, Optional

from .providers import (
    get_provider,
    get_current_provider,
    BaseAIProvider,
    ChatResult,
    execute_tool,
    redact_sensitive,
)
from .providers.deepseek import DeepSeekProvider
from .providers.openai_ import OpenAIProvider
from .providers.anthropic import AnthropicProvider
from .providers.gemini import GeminiProvider
from .providers.ollama import OllamaProvider

from .. import db, logger


# ---------- 服务配置 ----------

AI_SERVICES = {
    "deepseek": {
        "name": "DeepSeek",
        "default_model": "deepseek-chat",
        "supported_models": ["deepseek-chat", "deepseek-reasoner"],
        "pricing": {
            "deepseek-chat": (0.1, 0.5),
            "deepseek-reasoner": (0.1, 2.0),
        },
    },
    "anthropic": {
        "name": "Claude (Anthropic)",
        "default_model": "claude-3-5-sonnet-20241022",
        "supported_models": ["claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"],
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
        "pricing": {
            "gemini-1.5-pro": (3.5, 10.5),
            "gemini-1.5-flash": (0.075, 0.3),
            "gemini-2.0-flash-exp": (0.0, 0.0),
        },
    },
    "ollama": {
        "name": "Ollama (本地)",
        "default_model": "llama3.1",
        "supported_models": [],  # 动态获取
        "pricing": {},
    },
}


# ---------- 工具定义 ----------

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
                        "description": "Modify target: 'response' (default) = modify response body field; 'request' = modify request body field.",
                    },
                    "note": {
                        "type": "string",
                        "description": "(Required) The note/comment for the rule.",
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
                "Use this when the user wants a complex modification that can't be done with simple field replacement."
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
                        "description": "The rule ID(s) to delete. Multiple IDs comma-separated.",
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
            "description": "Delete all auto-modify rules (clear all at once).",
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
                    "host": {"type": "string", "description": "Filter by host name"},
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


# ---------- System Prompt ----------

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


# ---------- 辅助函数 ----------

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


# ---------- API 封装（向后兼容） ----------

def _get_current_service() -> str:
    """Get the currently selected AI service."""
    return db.get_setting("ai_service", "deepseek")


def _get_model(service: str) -> str:
    """Get the user-selected model for a service."""
    model_key = f"{service}_model"
    default = AI_SERVICES.get(service, {}).get("default_model", "")
    return db.get_setting(model_key, default)


async def _call_api_async(messages: list[dict], use_tools: bool = False,
                          chat_id: int = 0) -> dict:
    """Call the configured AI service API (async version using new provider architecture)."""
    service = _get_current_service()
    provider = get_provider(service)

    model = _get_model(service)
    tools = TOOLS if use_tools else None

    result = await provider.chat(messages, model=model, tools=tools, chat_id=chat_id)

    return {
        "ok": result.ok,
        "result": result.result,
        "tool_results": result.tool_results,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "error": result.error,
    }


# ---------- 公开 API（向后兼容） ----------

def analyze_flows(flow_ids: list[int]) -> dict:
    """Analyze the specified flow list (sync wrapper)."""
    import asyncio

    if flow_ids:
        by_id = db.get_flows_by_ids(flow_ids)
        flows = [by_id[i] for i in flow_ids if i in by_id]
    else:
        flows = []
    messages = _build_analyze_messages(flows)

    # 使用新架构调用
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_call_api_async(messages, use_tools=True))
        loop.close()
        return result
    except Exception as e:
        logger.error("ai", f"analyze_flows failed: {e}")
        return {"ok": False, "error": str(e)}


def chat(history: list[dict], flow_context: str, user_message: str) -> dict:
    """Multi-turn conversation (sync wrapper)."""
    import asyncio

    messages = _build_chat_messages(history, flow_context, user_message)

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_call_api_async(messages, use_tools=True))
        loop.close()
        return result
    except Exception as e:
        logger.error("ai", f"chat failed: {e}")
        return {"ok": False, "error": str(e)}


def get_available_models() -> dict:
    """Get available models for each service."""
    result = {}
    for service, config in AI_SERVICES.items():
        # 检查 API Key 是否配置
        key_map = {
            "deepseek": "deepseek_api_key",
            "anthropic": "anthropic_api_key",
            "openai": "openai_api_key",
            "gemini": "gemini_api_key",
            "ollama": "ollama_api_key",
        }
        key_name = key_map.get(service, "")
        raw_key = db.get_setting(key_name, "")
        try:
            from .. import secure_storage
            has_key = bool(
                secure_storage.decrypt(raw_key) if secure_storage.is_encrypted(raw_key) else raw_key
            )
        except ImportError:
            has_key = bool(raw_key)

        # 获取选中模型
        selected_model = _get_model(service) or config.get("default_model", "")

        # 获取 Provider 的模型列表
        try:
            provider = get_provider(service)
            provider_models = provider.available_models
        except ValueError:
            provider_models = config.get("supported_models", [])

        result[service] = {
            "name": config["name"],
            "models": provider_models,
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


def get_ai_usage_stats() -> dict:
    """Get AI usage statistics (today, this month, all time)."""
    return db.get_ai_usage_stats()


# ---------- 新架构：直接使用 Provider ----------

def get_provider_instance(service: Optional[str] = None) -> BaseAIProvider:
    """获取 Provider 实例。

    Args:
        service: 服务名称，None 则使用当前选中服务

    Returns:
        Provider 实例
    """
    if service is None:
        service = _get_current_service()
    return get_provider(service)


async def chat_async(
    messages: list[dict],
    service: Optional[str] = None,
    model: Optional[str] = None,
    tools: Optional[list[dict]] = None,
    chat_id: int = 0,
) -> ChatResult:
    """异步聊天（使用新架构）。

    Args:
        messages: 消息列表
        service: 服务名称，None 使用当前选中服务
        model: 模型 ID，None 使用默认模型
        tools: 工具定义
        chat_id: 会话 ID（用于记录用量）

    Returns:
        ChatResult 对象
    """
    if service is None:
        service = _get_current_service()

    provider = get_provider(service)

    if model is None:
        model = _get_model(service)

    return await provider.chat(messages, model=model, tools=tools, chat_id=chat_id)


async def stream_chat_async(
    messages: list[dict],
    service: Optional[str] = None,
    model: Optional[str] = None,
    tools: Optional[list[dict]] = None,
    chat_id: int = 0,
) -> AsyncIterator[str]:
    """异步流式聊天（使用新架构）。

    Args:
        messages: 消息列表
        service: 服务名称
        model: 模型 ID
        tools: 工具定义
        chat_id: 会话 ID

    Yields:
        流式文本片段
    """
    if service is None:
        service = _get_current_service()

    provider = get_provider(service)

    if model is None:
        model = _get_model(service)

    async for chunk in provider.stream_chat(messages, model=model, tools=tools, chat_id=chat_id):
        yield chunk


async def analyze_flows_async(flow_ids: list[int], service: Optional[str] = None) -> ChatResult:
    """异步分析流量（使用新架构）。

    Args:
        flow_ids: 流量 ID 列表
        service: 服务名称

    Returns:
        ChatResult 对象
    """
    if flow_ids:
        by_id = db.get_flows_by_ids(flow_ids)
        flows = [by_id[i] for i in flow_ids if i in by_id]
    else:
        flows = []
    messages = _build_analyze_messages(flows)
    return await chat_async(messages, service=service, tools=TOOLS)


# ---------- 导出 ----------

__all__ = [
    # 向后兼容
    "AI_SERVICES",
    "TOOLS",
    "SYSTEM_PROMPT",
    "analyze_flows",
    "chat",
    "get_available_models",
    "get_service_status",
    "get_ai_usage_stats",
    # 新架构
    "get_provider",
    "get_current_provider",
    "get_provider_instance",
    "BaseAIProvider",
    "ChatResult",
    "chat_async",
    "stream_chat_async",
    "analyze_flows_async",
    # Provider 类
    "DeepSeekProvider",
    "OpenAIProvider",
    "AnthropicProvider",
    "GeminiProvider",
    "OllamaProvider",
    # 工具函数
    "execute_tool",
    "redact_sensitive",
]

"""DeepSeek API calls: traffic analysis + multi-turn conversation + Agent tool calls.

Supports:
- Traffic analysis (first analysis creates a chat record)
- Multi-turn conversation (with traffic context)
- Agent capability: AI can call tools to create auto-modify rules (modify request/response)
- Direct conversation without traffic
"""

import json
import os
import ssl

import httpx

from .. import db, logger

API_URL = "https://api.deepseek.com/chat/completions"
# 支持的模型列表（其他模型已弃用）
SUPPORTED_MODELS = ("deepseek-v4-flash", "deepseek-v4-pro")
DEFAULT_MODEL = "deepseek-v4-flash"


def _get_model() -> str:
    """Read the user-selected model from settings, default deepseek-v4-flash."""
    m = db.get_setting("deepseek_model", DEFAULT_MODEL)
    if m not in SUPPORTED_MODELS:
        m = DEFAULT_MODEL
    return m

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
                        "description": "URL match pattern, supports wildcard *. Example: *steamstart.top*",
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
            "description": "Delete all auto-modify rules (clear all at once). Call this tool when the user says 'delete all rules' / 'clear rules', do not call delete_auto_reply_rule one by one.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _parse_dsml_tool_calls(text: str) -> list[dict]:
    """Parse DSML-format tool calls from DeepSeek text output.

    Format example:
      <｜｜DSML｜｜tool_calls>
      <｜｜DSML｜｜invoke name="delete_auto_reply_rule">
      <｜｜DSML｜｜parameter name="rule_id" string="true">53354dfe</｜｜DSML｜｜parameter>
      </｜｜DSML｜｜invoke>
      </｜｜DSML｜｜tool_calls>

    Returns a list of [{"name": "...", "arguments": {...}}].
    """
    import re
    results: list[dict] = []
    # 匹配每个 invoke 块
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
    # 移除整个 tool_calls 块
    cleaned = re.sub(
        r'<｜｜DSML｜｜tool_calls>.*?</｜｜DSML｜｜tool_calls>',
        '',
        text,
        flags=re.DOTALL,
    )
    # 移除可能残留的单独 invoke 块
    cleaned = re.sub(
        r'<｜｜DSML｜｜invoke\s+name="[^"]+">.*?</｜｜DSML｜｜invoke>',
        '',
        cleaned,
        flags=re.DOTALL,
    )
    return cleaned.strip()


def _execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool call, returning the result text."""
    if name == "create_auto_reply_rule":
        url_pattern = arguments.get("url_pattern", "")
        field_key = arguments.get("field_key", "")
        field_value = arguments.get("field_value", "")
        note = arguments.get("note", "") or ""
        # modify_target: 'response'（默认）或 'request'
        # 'response' -> action=modify_response, target=response_body
        # 'request'  -> action=modify_request,  target=request_body
        modify_target = str(arguments.get("modify_target", "response")).lower().strip()
        if modify_target not in ("request", "response"):
            modify_target = "response"
        is_request = modify_target == "request"
        action = "modify_request" if is_request else "modify_response"
        body_target = "request_body" if is_request else "response_body"
        modify_rules = json.dumps([
            {
                "target": body_target,
                "op": "replace",
                "key": field_key,
                "value": field_value,
            }
        ])
        rule_id = db.add_rule({
            "enabled": 1,
            "match_mode": "wildcard",
            "pattern": url_pattern,
            "action": action,
            "modify_rules": modify_rules,
            "note": note,
        })
        # 清除规则缓存
        try:
            from ..auto_reply.rules import invalidate_cache
            invalidate_cache()
        except Exception:  # noqa: BLE001
            pass
        target_label = "request body" if is_request else "response body"
        logger.info("ai", f"AI created auto-modify rule ({action}): {url_pattern} -> {field_key}={field_value}",
                     f"rule_id={rule_id}, note={note}")
        return (f"Created auto-modify rule (ID: {rule_id}, action: {action}): URL pattern '{url_pattern}', "
                f"replace {target_label} field '{field_key}' with '{field_value}', note: {note}")

    elif name == "list_auto_reply_rules":
        rules = db.get_rules()
        if not rules:
            return json.dumps({"count": 0, "rules": []}, ensure_ascii=False)
        # 返回结构化 JSON，让 AI 能准确提取 rule_id
        rule_list = []
        for r in rules:
            rule_list.append({
                "rule_id": r["id"],
                "enabled": bool(r["enabled"]),
                "match_mode": r["match_mode"],
                "pattern": r["pattern"],
                "action": r["action"],
                "note": r.get("note", ""),
            })
        return json.dumps({"count": len(rule_list), "rules": rule_list}, ensure_ascii=False)

    elif name == "delete_auto_reply_rule":
        rule_id_raw = arguments.get("rule_id", "")
        # 支持逗号分隔的多个 ID
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
            if still:
                results.append({"rule_id": rule_id, "ok": False, "error": "Still exists after deletion"})
            else:
                results.append({"rule_id": rule_id, "ok": True, "pattern": existing.get("pattern", "")})
                logger.info("ai", f"AI deleted rule {rule_id}", f"pattern={existing.get('pattern')}")
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
        # 验证
        remaining = db.get_rules()
        return json.dumps({
            "deleted": count,
            "remaining": len(remaining),
            "msg": f"Deleted {count} rule(s)" + (f", {len(remaining)} rule(s) not deleted" if remaining else ""),
        }, ensure_ascii=False)

    return f"Unknown tool: {name}"


# ---------- 消息构建 ----------

SYSTEM_PROMPT = (
    "You are a network traffic analysis assistant. The user will provide HTTP/HTTPS captured traffic data. "
    "You need to analyze the purpose, anomalies, key parameters, and authentication information of the traffic in Chinese. "
    "The user may follow up with details about this traffic; please answer based on the provided traffic context.\n\n"
    "You also have Agent capabilities to help the user manage auto-modify rules (modify request or response):\n"
    "- When the user says 'help me set up auto-modify to change xxx field to yyy' / 'change xxx in the response', call the create_auto_reply_rule tool (modify_target='response' or omit)\n"
    "- When the user says 'change xxx in the request' / 'tamper request parameters' / 'forge request fields', call the create_auto_reply_rule tool with modify_target='request'\n"
    "- When the user asks 'what auto-modify rules are there' / 'list rules', **you must call the list_auto_reply_rules tool first** to check actual rules; do not answer from memory\n"
    "- When the user says 'delete rule xxx', call the delete_auto_reply_rule tool (supports comma-separated multiple IDs)\n"
    "- When the user says 'delete all rules' / 'clear rules', **directly call the delete_all_auto_reply_rules tool**; do not list then delete one by one\n\n"
    "**Important guidelines**:\n"
    "1. When creating a rule, you must fill in the note field (comment), briefly describing what this rule does\n"
    "2. For any rule query/delete operation, you must call the list_auto_reply_rules tool first to get the real rule list; never answer 'no rules' based on historical memory\n"
    "3. When deleting a rule, use the real rule_id returned by list; do not use old IDs from memory\n"
    "4. When deleting all rules, use delete_all_auto_reply_rules; do not loop calling delete_auto_reply_rule\n"
    "5. list_auto_reply_rules returns JSON; the rule_id field of each item in the rules array is the real ID\n"
    "6. When the user says 'change request', be sure to pass modify_target='request'; when not explicitly stated, default to changing the response (omit or pass 'response')\n\n"
    "After calling a tool, tell the user the operation result in Chinese. Output in Markdown."
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


def _build_chat_messages(history: list[dict], flow_context: str,
                         user_message: str) -> list[dict]:
    """Build the multi-turn conversation message list (with traffic context + history)."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"The following is the analyzed traffic context; subsequent Q&A is based on it:\n\n{flow_context}"},
    ]
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})
    return messages


def _safe_json(s):
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return {}


# ---------- API 调用 ----------

# S6 修复：日志脱敏——AI 工具调用的 args 可能包含授权头/Token/Cookie 等敏感信息，
# 直接记录会经 /api/logs 与 /api/logs/export 泄露。对敏感键与字符串中的凭据片段做脱敏。
_SENSITIVE_KEYS = (
    "authorization", "cookie", "set-cookie", "token", "api_key",
    "apikey", "secret", "password", "x-api-key",
)


def _redact_sensitive(obj, depth: int = 0):
    """递归屏蔽敏感键值与字符串中的凭据片段，用于日志脱敏。"""
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
    """Create an httpx client to call the DeepSeek official API.

    Security: DeepSeek is a public HTTPS service; certificate verification is enabled by
    default to prevent man-in-the-middle attacks from intercepting and stealing the Bearer
    API Key in request headers. Only when the environment variable
    TELNIX_DEEPSEEK_INSECURE=1 is explicitly set (self-signed CA / debug proxy environment)
    is verification temporarily disabled; not recommended for production.
    """
    if os.environ.get("TELNIX_DEEPSEEK_INSECURE") == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return httpx.Client(verify=ctx, timeout=120)
    return httpx.Client(timeout=120)


def _call_api(messages: list[dict], use_tools: bool = False) -> dict:
    """Call the DeepSeek API, returns {ok, result/error, tool_results?}."""
    api_key = db.get_setting("deepseek_api_key", "")
    if not api_key:
        return {"ok": False, "error": "DeepSeek API key not configured"}
    try:
        logger.info("ai", f"Calling DeepSeek API, messages={len(messages)}, tools={use_tools}")
        model = _get_model()
        with _create_client() as client:
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
            }
            if use_tools:
                payload["tools"] = TOOLS
                payload["tool_choice"] = "auto"
            resp = client.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            message = data.get("choices", [{}])[0].get("message", {})

            # 处理工具调用
            tool_calls = message.get("tool_calls", [])
            # 兼容 DeepSeek 偶尔把 tool 调用写在文本里的 DSML 格式
            content_text = message.get("content", "") or ""
            if not tool_calls and "<｜｜DSML｜｜" in content_text:
                parsed = _parse_dsml_tool_calls(content_text)
                if parsed:
                    logger.info("ai", f"Parsed {len(parsed)} tool calls from DSML text")
                    # 构造标准 tool_calls 结构
                    for i, p in enumerate(parsed):
                        tool_calls.append({
                            "id": f"dsml_{i}",
                            "function": {
                                "name": p["name"],
                                "arguments": json.dumps(p["arguments"], ensure_ascii=False),
                            },
                        })
                    # 从展示给用户的 content 中移除 DSML 标签，避免用户看到原始标签
                    cleaned = _strip_dsml(content_text)
                    message["content"] = cleaned
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
                # 再次调用 API，让它根据工具结果生成回复
                logger.info("ai", "Tool calls completed, requesting API again to generate reply")
                resp2 = client.post(
                    API_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                    },
                )
                resp2.raise_for_status()
                data2 = resp2.json()
                result_text = (
                    data2.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                logger.info("ai", "API call succeeded (with tool calls)")
                # 第二次 API 返回的 content 也可能含 DSML 标签，需清理
                result_text = _strip_dsml(result_text)
                return {
                    "ok": True,
                    "result": result_text,
                    "tool_results": results,
                }

            result = message.get("content", "") or ""
            # 无 tool_calls 时的回复也可能含 DSML 标签，统一清理
            result = _strip_dsml(result)
            logger.info("ai", f"API call succeeded, reply length={len(result)}")
            return {"ok": True, "result": result}
    except httpx.HTTPStatusError as e:
        logger.error("ai", f"DeepSeek API returned error: {e.response.status_code}",
                     e.response.text[:500])
        return {"ok": False, "error": f"DeepSeek returned {e.response.status_code}: "
                                      f"{e.response.text[:200]}"}
    except Exception as e:  # noqa: BLE001
        logger.error("ai", f"DeepSeek API request failed: {e}", str(e))
        return {"ok": False, "error": f"Request failed: {e}"}


def analyze_flows(flow_ids: list[int]) -> dict:
    """Analyze the specified flow list, returns {ok, result/error}. Supports empty list (free conversation)."""
    flows = [db.get_flow(fid) for fid in flow_ids] if flow_ids else []
    flows = [f for f in flows if f]
    messages = _build_analyze_messages(flows)
    return _call_api(messages, use_tools=True)


def chat(history: list[dict], flow_context: str, user_message: str) -> dict:
    """Multi-turn conversation: answer user questions based on history and traffic context."""
    messages = _build_chat_messages(history, flow_context, user_message)
    return _call_api(messages, use_tools=True)

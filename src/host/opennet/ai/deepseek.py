"""DeepSeek API 调用：分析抓包流量 + 多轮对话 + Agent 工具调用。

支持：
- 流量分析（首次分析创建聊天记录）
- 多轮对话（带流量上下文）
- Agent 能力：AI 可调用工具创建自动修改规则（修改请求/响应）
- 无流量直接对话
"""

import json
import ssl

import httpx

from .. import db, logger

API_URL = "https://api.deepseek.com/chat/completions"
# 支持的模型列表（其他模型已弃用）
SUPPORTED_MODELS = ("deepseek-v4-flash", "deepseek-v4-pro")
DEFAULT_MODEL = "deepseek-v4-flash"


def _get_model() -> str:
    """从设置读取用户选择的模型，默认 deepseek-v4-flash。"""
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
                "创建一条自动修改规则，用于修改匹配 URL 的 HTTP 请求或响应体中的 JSON 字段。"
                "当用户要求修改某个接口的请求或响应字段值时调用此工具。"
                "默认修改响应（modify_target='response'）；当用户说「改请求」「篡改请求」「伪造请求参数」时传 modify_target='request'。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url_pattern": {
                        "type": "string",
                        "description": "URL 匹配模式，支持通配符 *。例如 *steamstart.top*",
                    },
                    "field_key": {
                        "type": "string",
                        "description": (
                            "要修改的 JSON 字段名。可直接填字段名（如 remainingUses），"
                            "会全局搜索替换；也可填完整路径（如 data.status.remainingUses）精确定位。"
                        ),
                    },
                    "field_value": {
                        "type": "string",
                        "description": "替换后的值。数字填 99999，字符串填不含引号的文本。",
                    },
                    "modify_target": {
                        "type": "string",
                        "enum": ["response", "request"],
                        "description": "修改目标：'response'（默认）=修改响应体字段；'request'=修改请求体字段（转发前篡改，用于伪造请求参数测试服务端校验）。用户没明确说改请求还是响应时默认 'response'。",
                    },
                    "note": {
                        "type": "string",
                        "description": "（必填）规则的备注说明，简要描述这条规则是干什么的，方便用户日后识别。例如「修改登录返回的剩余次数为 99999」。",
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
            "description": "列出当前所有自动修改规则，包括启用状态和匹配模式。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_auto_reply_rule",
            "description": "删除指定 ID 的自动修改规则。可传单个 ID 或多个 ID（用逗号分隔）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "rule_id": {
                        "type": "string",
                        "description": "要删除的规则 ID。多个 ID 用逗号分隔，如 'abc123,def456'。",
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
            "description": "删除所有自动修改规则（一键清空）。当用户说「删除所有规则」「清空规则」时调用此工具，不要逐个调用 delete_auto_reply_rule。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _parse_dsml_tool_calls(text: str) -> list[dict]:
    """从 DeepSeek 文本输出中解析 DSML 格式的工具调用。

    格式示例：
      <｜｜DSML｜｜tool_calls>
      <｜｜DSML｜｜invoke name="delete_auto_reply_rule">
      <｜｜DSML｜｜parameter name="rule_id" string="true">53354dfe</｜｜DSML｜｜parameter>
      </｜｜DSML｜｜invoke>
      </｜｜DSML｜｜tool_calls>

    返回 [{"name": "...", "arguments": {...}}] 列表。
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
    """移除文本中的 DSML 标签块，保留其他文字。"""
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
    """执行工具调用，返回结果文本。"""
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
        target_label = "请求体" if is_request else "响应体"
        logger.info("ai", f"AI 创建自动修改规则({action}): {url_pattern} -> {field_key}={field_value}",
                     f"rule_id={rule_id}, note={note}")
        return (f"已创建自动修改规则（ID: {rule_id}，动作: {action}）：URL 模式 '{url_pattern}'，"
                f"将{target_label}字段 '{field_key}' 替换为 '{field_value}'，备注：{note}")

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
            return "未提供要删除的规则 ID"
        results = []
        for rule_id in ids:
            existing = db.get_rule(rule_id)
            if not existing:
                rules = db.get_rules()
                all_ids = [r["id"] for r in rules]
                results.append({"rule_id": rule_id, "ok": False, "error": "规则不存在", "current_ids": all_ids})
                continue
            db.delete_rule(rule_id)
            still = db.get_rule(rule_id)
            if still:
                results.append({"rule_id": rule_id, "ok": False, "error": "删除后仍存在"})
            else:
                results.append({"rule_id": rule_id, "ok": True, "pattern": existing.get("pattern", "")})
                logger.info("ai", f"AI 删除规则 {rule_id}", f"pattern={existing.get('pattern')}")
        try:
            from ..auto_reply.rules import invalidate_cache
            invalidate_cache()
        except Exception:  # noqa: BLE001
            pass
        return json.dumps({"results": results}, ensure_ascii=False)

    elif name == "delete_all_auto_reply_rules":
        rules = db.get_rules()
        if not rules:
            return json.dumps({"deleted": 0, "msg": "当前没有任何规则"}, ensure_ascii=False)
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
        logger.info("ai", f"AI 一键删除所有规则，共 {count} 条")
        # 验证
        remaining = db.get_rules()
        return json.dumps({
            "deleted": count,
            "remaining": len(remaining),
            "msg": f"已删除 {count} 条规则" + (f"，剩余 {len(remaining)} 条未删除" if remaining else ""),
        }, ensure_ascii=False)

    return f"未知工具: {name}"


# ---------- 消息构建 ----------

SYSTEM_PROMPT = (
    "你是网络流量分析助手。用户会提供 HTTP/HTTPS 抓包流量数据，"
    "你需要用中文分析流量的目的、异常、关键参数和鉴权信息。"
    "后续用户可能就这些流量追问细节，请基于提供的流量上下文回答。\n\n"
    "你还具有 Agent 能力，可以帮用户管理自动修改规则（修改请求或响应）：\n"
    "- 当用户说「帮我设置自动修改把 xxx 字段改成 yyy」「改响应里的 xxx」时，调用 create_auto_reply_rule 工具（modify_target='response' 或不传）\n"
    "- 当用户说「改请求里的 xxx」「篡改请求参数」「伪造请求字段」时，调用 create_auto_reply_rule 工具并传 modify_target='request'\n"
    "- 当用户问「有哪些自动修改规则」「列出规则」时，**必须先调用 list_auto_reply_rules 工具**查看实际规则，不能凭印象回答\n"
    "- 当用户说「删除规则 xxx」时，调用 delete_auto_reply_rule 工具（支持逗号分隔多个 ID）\n"
    "- 当用户说「删除所有规则」「清空规则」时，**直接调用 delete_all_auto_reply_rules 工具**，不要先 list 再逐个删除\n\n"
    "**重要准则**：\n"
    "1. 创建规则时必须填写 note 字段（备注），简要描述这条规则是干什么的\n"
    "2. 任何涉及规则查询/删除的操作，必须先调用 list_auto_reply_rules 工具获取真实规则列表，严禁凭历史印象回答「没有规则」\n"
    "3. 删除规则时用 list 返回的真实 rule_id，不要用记忆中的旧 id\n"
    "4. 删除所有规则时用 delete_all_auto_reply_rules，不要循环调用 delete_auto_reply_rule\n"
    "5. list_auto_reply_rules 返回 JSON，其中 rules 数组每项的 rule_id 字段是真实 ID\n"
    "6. 用户说「改请求」时务必传 modify_target='request'；没明确说改请求还是响应时默认改响应（不传或传 'response'）\n\n"
    "调用工具后，用中文告诉用户操作结果。用 Markdown 输出。"
)


def _build_flow_context(flows: list[dict]) -> str:
    """构建流量上下文文本。"""
    if not flows:
        return "（本次对话无关联流量数据）"
    snippets = []
    for i, f in enumerate(flows, 1):
        req_headers = _safe_json(f.get("request_headers"))
        resp_headers = _safe_json(f.get("response_headers"))
        snippets.append(
            f"### 流量 {i}\n"
            f"- 请求：{f.get('method', '')} {f.get('url', '')}\n"
            f"- 进程：{f.get('process_name', '')} (PID {f.get('pid', '')})\n"
            f"- 请求头：{json.dumps(req_headers, ensure_ascii=False)}\n"
            f"- 请求体：{(f.get('request_body') or '')[:2000]}\n"
            f"- 状态码：{f.get('status_code', '')}\n"
            f"- 响应头：{json.dumps(resp_headers, ensure_ascii=False)}\n"
            f"- 响应体：{(f.get('response_body') or '')[:4000]}\n"
        )
    return "\n".join(snippets)


def _build_analyze_messages(flows: list[dict]) -> list[dict]:
    """构建首次分析的消息列表。"""
    flow_ctx = _build_flow_context(flows)
    if flows:
        content = (
            f"请分析以下抓包流量：\n\n{flow_ctx}\n\n"
            "请分析：\n1. 每个流量的目的与含义\n"
            "2. 是否存在异常或可疑请求\n"
            "3. 关键参数、Token、鉴权信息\n"
            "4. 总结整体行为"
        )
    else:
        content = "用户发起了自由对话，没有关联流量。请打招呼并询问用户需要什么帮助。"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def _build_chat_messages(history: list[dict], flow_context: str,
                         user_message: str) -> list[dict]:
    """构建多轮对话消息列表（含流量上下文 + 历史记录）。"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"以下是分析的流量上下文，后续问答都基于此：\n\n{flow_context}"},
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

def _create_client() -> httpx.Client:
    """创建 httpx 客户端，跳过 SSL 验证（代理环境下的证书问题）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return httpx.Client(verify=ctx, timeout=120)


def _call_api(messages: list[dict], use_tools: bool = False) -> dict:
    """调用 DeepSeek API，返回 {ok, result/error, tool_results?}。"""
    api_key = db.get_setting("deepseek_api_key", "")
    if not api_key:
        return {"ok": False, "error": "未配置 DeepSeek API key"}
    try:
        logger.info("ai", f"调用 DeepSeek API, messages={len(messages)} 条, tools={use_tools}")
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
                    logger.info("ai", f"从 DSML 文本解析到 {len(parsed)} 个工具调用")
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
                    logger.info("ai", f"AI 调用工具: {name}", f"参数: {args}")
                    result = _execute_tool(name, args)
                    results.append({"name": name, "result": result})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result,
                    })
                # 再次调用 API，让它根据工具结果生成回复
                logger.info("ai", "工具调用完成，再次请求 API 生成回复")
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
                logger.info("ai", "API 调用成功（含工具调用）")
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
            logger.info("ai", f"API 调用成功, 回复长度={len(result)}")
            return {"ok": True, "result": result}
    except httpx.HTTPStatusError as e:
        logger.error("ai", f"DeepSeek API 返回错误: {e.response.status_code}",
                     e.response.text[:500])
        return {"ok": False, "error": f"DeepSeek 返回 {e.response.status_code}: "
                                      f"{e.response.text[:200]}"}
    except Exception as e:  # noqa: BLE001
        logger.error("ai", f"DeepSeek API 请求失败: {e}", str(e))
        return {"ok": False, "error": f"请求失败: {e}"}


def analyze_flows(flow_ids: list[int]) -> dict:
    """分析指定流量列表，返回 {ok, result/error}。支持空列表（自由对话）。"""
    flows = [db.get_flow(fid) for fid in flow_ids] if flow_ids else []
    flows = [f for f in flows if f]
    messages = _build_analyze_messages(flows)
    return _call_api(messages, use_tools=True)


def chat(history: list[dict], flow_context: str, user_message: str) -> dict:
    """多轮对话：基于历史记录和流量上下文回答用户问题。"""
    messages = _build_chat_messages(history, flow_context, user_message)
    return _call_api(messages, use_tools=True)

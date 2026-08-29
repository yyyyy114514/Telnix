"""Anthropic Claude Provider 实现。"""

import json
from typing import AsyncIterator, Optional

import httpx

from .base import (
    BaseAIProvider,
    ChatResult,
    ModelInfo,
    execute_tool,
    redact_sensitive,
)
from ... import logger


# Claude 工具定义转换为 Anthropic 格式
def _convert_tools_for_anthropic(tools: list[dict]) -> list[dict]:
    """将通用工具定义转换为 Anthropic 格式。"""
    result = []
    for t in tools:
        fn = t.get("function", {})
        result.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {}),
        })
    return result


def _convert_messages_for_anthropic(messages: list[dict]) -> tuple[list[dict], str]:
    """将消息列表转换为 Anthropic 格式。

    Returns:
        (anthropic_messages, system_content)
    """
    anthropic_messages = []
    system_content = ""

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            system_content += content + "\n\n"
        elif role == "user":
            anthropic_messages.append({"role": "user", "content": content})
        elif role == "assistant":
            anthropic_messages.append({"role": "assistant", "content": content})
        elif role == "tool":
            anthropic_messages.append({
                "role": "user",
                "content": f"[Tool result: {msg.get('name', 'unknown')}]\n{content}"
            })

    return anthropic_messages, system_content


class AnthropicProvider(BaseAIProvider):
    """Anthropic Claude Provider。"""

    name = "Claude (Anthropic)"
    service_key = "anthropic"

    MODELS = [
        ModelInfo("claude-3-5-sonnet-20241022", "Claude 3.5 Sonnet", 11.0, 32.0),
        ModelInfo("claude-3-5-haiku-20241022", "Claude 3.5 Haiku", 0.8, 4.0),
        ModelInfo("claude-3-opus-20240229", "Claude 3 Opus", 90.0, 270.0),
    ]

    BASE_URL = "https://api.anthropic.com/v1"

    async def chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        chat_id: int = 0,
    ) -> ChatResult:
        """发送聊天消息。"""
        model = model or self.default_model
        api_key = self.api_key or self._get_api_key_from_settings("anthropic_api_key")

        if not api_key:
            return ChatResult(ok=False, error=f"{self.name} API key not configured")

        url = f"{self.BASE_URL}/messages"
        anthropic_messages, system_content = _convert_messages_for_anthropic(messages)

        payload: dict = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": 4096,
        }

        if system_content:
            payload["system"] = system_content

        anthropic_tools = None
        if tools:
            anthropic_tools = _convert_tools_for_anthropic(tools)
            payload["tools"] = anthropic_tools

        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-dangerous-direct-browser-access": "true",
        }

        try:
            logger.info("ai", f"Calling {self.name} API, model={model}")
            with self._create_client() as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()

            usage = data.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)

            content = data.get("content", [])
            result_text = ""
            tool_uses = []

            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        result_text += block.get("text", "")
                    elif block.get("type") == "tool_use":
                        tool_uses.append(block)

            # 处理工具调用
            if tool_uses:
                return await self._handle_tool_calls(
                    url, headers, model, anthropic_messages, content,
                    system_content, tool_uses, input_tokens, output_tokens, chat_id
                )

            logger.info("ai", f"API call succeeded, reply length={len(result_text)}")

            if chat_id > 0 and (input_tokens > 0 or output_tokens > 0):
                self._record_usage(chat_id, model, input_tokens, output_tokens)

            return ChatResult(
                ok=True,
                result=result_text,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        except httpx.HTTPStatusError as e:
            logger.error("ai", f"{self.name} API error: {e.response.status_code}", e.response.text[:500])
            return ChatResult(ok=False, error=f"API returned {e.response.status_code}: {e.response.text[:200]}")
        except Exception as e:
            logger.error("ai", f"{self.name} request failed: {e}")
            return ChatResult(ok=False, error=f"Request failed: {e}")

    async def stream_chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        chat_id: int = 0,
    ) -> AsyncIterator[str]:
        """流式聊天。"""
        model = model or self.default_model
        api_key = self.api_key or self._get_api_key_from_settings("anthropic_api_key")

        if not api_key:
            yield f"Error: {self.name} API key not configured"
            return

        url = f"{self.BASE_URL}/messages"
        anthropic_messages, system_content = _convert_messages_for_anthropic(messages)

        payload: dict = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": 4096,
            "stream": True,
        }

        if system_content:
            payload["system"] = system_content

        anthropic_tools = None
        if tools:
            anthropic_tools = _convert_tools_for_anthropic(tools)
            payload["tools"] = anthropic_tools

        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-dangerous-direct-browser-access": "true",
        }

        try:
            with self._create_client() as client:
                with client.stream("POST", url, headers=headers, json=payload) as resp:
                    resp.raise_for_status()
                    full_content = ""
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                                event_type = data.get("type", "")

                                if event_type == "content_block_delta":
                                    delta = data.get("delta", {})
                                    if delta.get("type") == "text_delta":
                                        text = delta.get("text", "")
                                        full_content += text
                                        yield text
                                elif event_type == "message_delta":
                                    usage = data.get("usage", {})
                                    if chat_id > 0:
                                        output_tokens = usage.get("output_tokens", 0)
                                        self._record_usage(chat_id, model, 0, output_tokens)
                            except json.JSONDecodeError:
                                continue

                    logger.info("ai", f"Stream completed, total length={len(full_content)}")

        except Exception as e:
            logger.error("ai", f"{self.name} stream failed: {e}")
            yield f"Error: {e}"

    async def _handle_tool_calls(
        self,
        url: str,
        headers: dict,
        model: str,
        anthropic_messages: list[dict],
        content: list,
        system_content: str,
        tool_uses: list[dict],
        input_tokens: int,
        output_tokens: int,
        chat_id: int,
    ) -> ChatResult:
        """处理工具调用。"""
        results = []

        for tool_use in tool_uses:
            name = tool_use.get("name", "")
            input_json = tool_use.get("input", {})
            logger.info("ai", f"AI calling tool: {name}", f"args: {redact_sensitive(input_json)}")
            result = execute_tool(name, input_json)
            results.append({"name": name, "result": result})
            anthropic_messages.append({
                "role": "user",
                "content": f"<result_of_tool_call>\n{result}\n</result_of_tool_call>"
            })

        logger.info("ai", "Tool calls completed, requesting API again")

        payload = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": 4096,
            "stream": False,
        }
        if system_content:
            payload["system"] = system_content

        try:
            with self._create_client() as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()

            usage = data.get("usage", {})
            input_tokens += usage.get("input_tokens", 0)
            output_tokens += usage.get("output_tokens", 0)

            result_content = data.get("content", [])
            result_text = ""
            if isinstance(result_content, list):
                for block in result_content:
                    if block.get("type") == "text":
                        result_text += block.get("text", "")

            logger.info("ai", "API call succeeded (with tool calls)")

            if chat_id > 0:
                self._record_usage(chat_id, model, input_tokens, output_tokens)

            return ChatResult(
                ok=True,
                result=result_text,
                tool_results=results,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        except httpx.HTTPStatusError as e:
            logger.error("ai", f"{self.name} API error: {e.response.status_code}")
            return ChatResult(ok=False, error=f"API returned {e.response.status_code}")
        except Exception as e:
            logger.error("ai", f"{self.name} request failed: {e}")
            return ChatResult(ok=False, error=f"Request failed: {e}")

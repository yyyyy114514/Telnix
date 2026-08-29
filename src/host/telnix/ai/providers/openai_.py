"""OpenAI Provider 实现。"""

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


class OpenAIProvider(BaseAIProvider):
    """OpenAI GPT Provider。"""

    name = "OpenAI GPT"
    service_key = "openai"

    MODELS = [
        ModelInfo("gpt-4o", "GPT-4o", 15.0, 60.0),
        ModelInfo("gpt-4o-mini", "GPT-4o Mini", 0.75, 3.0),
        ModelInfo("gpt-4-turbo", "GPT-4 Turbo", 30.0, 90.0),
        ModelInfo("gpt-3.5-turbo", "GPT-3.5 Turbo", 0.5, 1.5),
    ]

    BASE_URL = "https://api.openai.com/v1"

    async def chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        chat_id: int = 0,
    ) -> ChatResult:
        """发送聊天消息。"""
        model = model or self.default_model
        api_key = self.api_key or self._get_api_key_from_settings("openai_api_key")

        if not api_key:
            return ChatResult(ok=False, error=f"{self.name} API key not configured")

        url = f"{self.BASE_URL}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            logger.info("ai", f"Calling {self.name} API, model={model}")
            with self._create_client() as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()

            usage = data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

            message = data.get("choices", [{}])[0].get("message", {})
            content_text = message.get("content", "") or ""

            # 处理工具调用
            tool_calls = message.get("tool_calls", [])
            if tool_calls:
                return await self._handle_tool_calls(
                    url, headers, model, messages, message,
                    tool_calls, input_tokens, output_tokens, chat_id
                )

            logger.info("ai", f"API call succeeded, reply length={len(content_text)}")

            # 记录用量
            if chat_id > 0 and (input_tokens > 0 or output_tokens > 0):
                self._record_usage(chat_id, model, input_tokens, output_tokens)

            return ChatResult(
                ok=True,
                result=content_text,
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
        api_key = self.api_key or self._get_api_key_from_settings("openai_api_key")

        if not api_key:
            yield f"Error: {self.name} API key not configured"
            return

        url = f"{self.BASE_URL}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            with self._create_client() as client:
                with client.stream("POST", url, headers=headers, json=payload) as resp:
                    resp.raise_for_status()
                    full_content = ""
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if delta:
                                full_content += delta
                                yield delta
                        except json.JSONDecodeError:
                            continue

                    logger.info("ai", f"Stream completed, total length={len(full_content)}")

                    if chat_id > 0:
                        estimated_tokens = len(full_content) // 4
                        self._record_usage(chat_id, model, 0, estimated_tokens)

        except Exception as e:
            logger.error("ai", f"{self.name} stream failed: {e}")
            yield f"Error: {e}"

    async def _handle_tool_calls(
        self,
        url: str,
        headers: dict,
        model: str,
        messages: list[dict],
        assistant_message: dict,
        tool_calls: list[dict],
        input_tokens: int,
        output_tokens: int,
        chat_id: int,
    ) -> ChatResult:
        """处理工具调用。"""
        messages = messages + [assistant_message]
        results = []

        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            logger.info("ai", f"AI calling tool: {name}", f"args: {redact_sensitive(args)}")
            result = execute_tool(name, args)
            results.append({"name": name, "result": result})
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": result,
            })

        logger.info("ai", "Tool calls completed, requesting API again")
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }

        try:
            with self._create_client() as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()

            usage = data.get("usage", {})
            input_tokens += usage.get("prompt_tokens", 0)
            output_tokens += usage.get("completion_tokens", 0)

            result_text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
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

"""Google Gemini Provider 实现。"""

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


class GeminiProvider(BaseAIProvider):
    """Google Gemini Provider。"""

    name = "Google Gemini"
    service_key = "gemini"

    MODELS = [
        ModelInfo("gemini-1.5-pro", "Gemini 1.5 Pro", 3.5, 10.5),
        ModelInfo("gemini-1.5-flash", "Gemini 1.5 Flash", 0.075, 0.3),
        ModelInfo("gemini-2.0-flash-exp", "Gemini 2.0 Flash (Exp)", 0.0, 0.0),
    ]

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

    async def chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        chat_id: int = 0,
    ) -> ChatResult:
        """发送聊天消息。"""
        model = model or self.default_model
        api_key = self.api_key or self._get_api_key_from_settings("gemini_api_key")

        if not api_key:
            return ChatResult(ok=False, error=f"{self.name} API key not configured")

        contents = []
        system_instruction = ""

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system_instruction = content
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": content}]})
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": content}]})
            elif role == "tool":
                contents.append({
                    "role": "user",
                    "parts": [{"text": f"[Tool result]\n{content}"}]
                })

        payload: dict = {
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

        # 转换工具
        gemini_tools = None
        if tools:
            gemini_tools = [{"functionDeclarations": [
                {
                    "name": t["function"]["name"],
                    "description": t["function"]["description"],
                    "parameters": t["function"].get("parameters", {}),
                }
                for t in tools
            ]}]
            payload["tools"] = gemini_tools

        url = f"{self.BASE_URL}/{model}:generateContent?key={api_key}"
        headers = {"Content-Type": "application/json"}

        try:
            logger.info("ai", f"Calling {self.name} API, model={model}")
            with self._create_client() as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()

            # Gemini 不返回精确 token 计数
            input_tokens = 0
            output_tokens = 0

            candidates = data.get("candidates", [])
            if not candidates:
                return ChatResult(ok=False, error="No response from Gemini")

            content = candidates[0].get("content", {})
            parts = content.get("parts", [])
            result_text = ""
            function_calls = []

            for part in parts:
                if "text" in part:
                    result_text += part["text"]
                elif "functionCall" in part:
                    function_calls.append(part["functionCall"])

            # 处理函数调用
            if function_calls:
                return await self._handle_function_calls(
                    url, headers, model, contents, payload,
                    function_calls, input_tokens, output_tokens, chat_id
                )

            logger.info("ai", f"API call succeeded, reply length={len(result_text)}")

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
        api_key = self.api_key or self._get_api_key_from_settings("gemini_api_key")

        if not api_key:
            yield f"Error: {self.name} API key not configured"
            return

        contents = []
        system_instruction = ""

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system_instruction = content
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": content}]})
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": content}]})

        payload: dict = {
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

        url = f"{self.BASE_URL}/{model}:streamGenerateContent?key={api_key}&alt=sse"
        headers = {"Content-Type": "application/json"}

        try:
            with self._create_client() as client:
                with client.stream("POST", url, headers=headers, json=payload) as resp:
                    resp.raise_for_status()
                    full_content = ""
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        try:
                            data = json.loads(data_str)
                            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                            for part in parts:
                                if "text" in part:
                                    text = part["text"]
                                    full_content += text
                                    yield text
                        except json.JSONDecodeError:
                            continue

                    logger.info("ai", f"Stream completed, total length={len(full_content)}")

        except Exception as e:
            logger.error("ai", f"{self.name} stream failed: {e}")
            yield f"Error: {e}"

    async def _handle_function_calls(
        self,
        url: str,
        headers: dict,
        model: str,
        contents: list,
        base_payload: dict,
        function_calls: list[dict],
        input_tokens: int,
        output_tokens: int,
        chat_id: int,
    ) -> ChatResult:
        """处理函数调用。"""
        results = []

        for fc in function_calls:
            name = fc.get("name", "")
            args = fc.get("args", {})
            logger.info("ai", f"AI calling tool: {name}", f"args: {redact_sensitive(args)}")
            result = execute_tool(name, args)
            results.append({"name": name, "result": result})
            contents.append({
                "role": "model",
                "parts": [{"functionCall": {"name": name, "args": args}}]
            })
            contents.append({
                "role": "user",
                "parts": [{"functionResponse": {"name": name, "response": {"result": result}}}]
            })

        logger.info("ai", "Function calls completed, requesting API again")

        payload = {
            "contents": contents,
            "generationConfig": base_payload.get("generationConfig", {}),
        }

        try:
            with self._create_client() as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()

            candidates = data.get("candidates", [])
            if not candidates:
                return ChatResult(ok=False, error="No response from Gemini after function calls")

            parts = candidates[0].get("content", {}).get("parts", [])
            result_text = ""
            for part in parts:
                if "text" in part:
                    result_text += part["text"]

            logger.info("ai", "API call succeeded (with function calls)")

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

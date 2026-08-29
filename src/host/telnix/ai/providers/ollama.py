"""Ollama (本地模型) Provider 实现。"""

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
from ... import db, logger


class OllamaProvider(BaseAIProvider):
    """Ollama 本地模型 Provider。"""

    name = "Ollama (本地)"
    service_key = "ollama"

    # Ollama 模型列表为空，因为模型是动态的
    MODELS: list[ModelInfo] = []

    BASE_URL = "http://127.0.0.1:11434"

    def __init__(self, api_key: str = "", **kwargs):
        super().__init__(api_key, **kwargs)
        self._cached_models: list[ModelInfo] = []

    @property
    def default_model(self) -> str:
        """从设置获取默认模型。"""
        return db.get_setting("ollama_model", "llama3.1")

    @property
    def available_models(self) -> list[str]:
        """获取可用模型列表。"""
        return [m.id for m in self._get_models_from_api()]

    def _get_models_from_api(self) -> list[ModelInfo]:
        """从 Ollama API 获取模型列表。"""
        if self._cached_models:
            return self._cached_models

        endpoint = self._get_endpoint_from_settings("ollama_endpoint") or self.BASE_URL
        url = f"{endpoint}/api/tags"

        try:
            with self._create_client() as client:
                resp = client.get(url, timeout=5.0)
                resp.raise_for_status()
                data = resp.json()

            models = []
            for m in data.get("models", []):
                models.append(ModelInfo(
                    id=m.get("name", ""),
                    name=m.get("name", ""),
                    input_price=0.0,
                    output_price=0.0,
                ))

            self._cached_models = models
            return models

        except Exception as e:
            logger.warning("ai", f"Failed to fetch Ollama models: {e}")
            return self._cached_models

    async def chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        chat_id: int = 0,
    ) -> ChatResult:
        """发送聊天消息。"""
        model = model or self.default_model
        endpoint = self._get_endpoint_from_settings("ollama_endpoint") or self.BASE_URL

        # Ollama 使用不同的消息格式
        ollama_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            # Ollama 支持 user/assistant/system
            if role == "system":
                role = "system"
            elif role == "tool":
                role = "user"  # Ollama 不直接支持 tool role
                content = f"[Tool result]\n{content}"
            else:
                role = role if role in ("user", "assistant") else "user"

            ollama_messages.append({"role": role, "content": content})

        payload = {
            "model": model,
            "messages": ollama_messages,
            "stream": False,
        }

        url = f"{endpoint}/api/chat"

        try:
            logger.info("ai", f"Calling {self.name} API, model={model}")
            with self._create_client() as client:
                resp = client.post(url, json=payload, timeout=120)
                resp.raise_for_status()
                data = resp.json()

            message = data.get("message", {})
            result_text = message.get("content", "")

            # Ollama 不返回 token 统计
            input_tokens = 0
            output_tokens = 0

            # 检查 done 状态
            done = data.get("done", True)
            if not done:
                # 模型仍在生成，可能需要继续获取
                pass

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
        endpoint = self._get_endpoint_from_settings("ollama_endpoint") or self.BASE_URL

        ollama_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                role = "system"
            elif role == "tool":
                role = "user"
                content = f"[Tool result]\n{content}"
            else:
                role = role if role in ("user", "assistant") else "user"

            ollama_messages.append({"role": role, "content": content})

        payload = {
            "model": model,
            "messages": ollama_messages,
            "stream": True,
        }

        url = f"{endpoint}/api/chat"

        try:
            with self._create_client() as client:
                with client.stream("POST", url, json=payload, timeout=120) as resp:
                    resp.raise_for_status()
                    full_content = ""
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                            message = data.get("message", {})
                            delta = message.get("content", "")
                            if delta:
                                full_content += delta
                                yield delta
                        except json.JSONDecodeError:
                            continue

                    logger.info("ai", f"Stream completed, total length={len(full_content)}")

        except Exception as e:
            logger.error("ai", f"{self.name} stream failed: {e}")
            yield f"Error: {e}"

    def get_service_status(self) -> dict:
        """检查 Ollama 服务状态。"""
        endpoint = self._get_endpoint_from_settings("ollama_endpoint") or self.BASE_URL
        url = f"{endpoint}/"

        try:
            with self._create_client() as client:
                resp = client.get(url, timeout=5.0)
                resp.raise_for_status()
                return {"ok": True, "status": "running"}
        except Exception as e:
            return {"ok": False, "status": "error", "error": str(e)}

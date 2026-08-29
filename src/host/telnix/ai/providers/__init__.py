"""AI Provider 统一导出模块。"""

from .base import (
    BaseAIProvider,
    ChatResult,
    ModelInfo,
    execute_tool,
    redact_sensitive,
)
from .claude import DeepSeekProvider
from .openai_ import OpenAIProvider
from .anthropic import AnthropicProvider
from .gemini import GeminiProvider
from .ollama import OllamaProvider


def get_provider(service: str, api_key: str = "", **kwargs) -> BaseAIProvider:
    """Provider 工厂：根据服务名称返回对应的 Provider 实例。"""
    providers = {
        "deepseek": DeepSeekProvider,
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
        "gemini": GeminiProvider,
        "ollama": OllamaProvider,
    }
    provider_cls = providers.get(service.lower())
    if not provider_cls:
        available = ", ".join(providers.keys())
        raise ValueError(f"Unsupported AI service: {service!r}. Available: {available}")
    return provider_cls(api_key=api_key, **kwargs)


def get_current_provider() -> BaseAIProvider:
    """获取当前选定的 AI Provider。"""
    from ... import db
    service = db.get_setting("ai_service", "deepseek")
    return get_provider(service)


__all__ = [
    "BaseAIProvider", "ChatResult", "ModelInfo",
    "get_provider", "get_current_provider",
    "DeepSeekProvider", "OpenAIProvider", "AnthropicProvider", "GeminiProvider", "OllamaProvider",
    "execute_tool", "redact_sensitive",
]

# src/utils/llm_provider/__init__.py

"""
LLM Provider Package.

Exports the abstract base class and all concrete provider implementations.
"""

from .base import LLMProvider
from .anthropic import AnthropicProvider
from .openai import OpenAIProvider
from .gemini import GeminiProvider

__all__ = [
    "LLMProvider",
    "AnthropicProvider",
    "OpenAIProvider",
    "GeminiProvider",
]


def create_provider(sdk_type, api_key, base_url, model_id):
    """
    Factory function to create a provider instance based on SDK type.
    
    Args:
        sdk_type: One of "anthropic", "openai", "gemini", "ai studio", "nvidia"
        api_key: API key for the provider
        base_url: Base URL (optional)
        model_id: Model identifier
    
    Returns:
        LLMProvider instance
    """
    sdk = sdk_type.lower()
    if sdk in ["ai studio", "gemini"]:
        return GeminiProvider(api_key, base_url, model_id)
    elif sdk in ["openai", "nvidia"]:
        return OpenAIProvider(api_key, base_url, model_id)
    else:
        return AnthropicProvider(api_key, base_url, model_id)
##
# @file src/utils/llm_provider/__init__.py
# @date 2026/08/05
# 
# @brief LLM Provider Package.
# Exports the abstract base class and all concrete provider implementations.
#

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

##
# @brief Factory function to create a provider instance based on SDK type.
# 
# @param sdk_type: One of "anthropic", "openai", "gemini", "ai studio", "nvidia".
# @param api_key: API key for the provider.
# @param base_url: Base URL (optional).
# @param model_id: Model identifier.
#
# @return LLMProvider instance.
#
# @todo Need to use SDK/API type (ResponseAPI, AnthropicAPI, InteractionsAPI ...) to create LLM provider;
# Not user providers name anymore.
# 
def create_provider(sdk_type, api_key, base_url, model_id):
    sdk = sdk_type.lower()
    if sdk in ["ai studio", "gemini"]:
        return GeminiProvider(api_key, base_url, model_id)
    elif sdk in ["openai", "nvidia"]:
        return OpenAIProvider(api_key, base_url, model_id)
    else:
        return AnthropicProvider(api_key, base_url, model_id)
# End-def
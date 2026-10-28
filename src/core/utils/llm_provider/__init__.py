##
 # @file src/core/utils/llm_provider/__init__.py
 # @date 2026/08/05
 #
 # @brief LLM provider selection package.
 # Exports the shared provider interface and concrete Anthropic/OpenAI adapters,
 # and chooses the adapter matching the configured SDK type.
 #

from .base import LLMProvider
from .anthropic import AnthropicProvider
from .openai import OpenAIProvider

__all__ = [
    "LLMProvider",
    "AnthropicProvider",
    "OpenAIProvider",
]

##
 # @brief Factory function to create a provider instance based on SDK type.
 # 
 # @param sdk_type Protocol / SDK type: "Message" (Anthropic) or "Chat Completions" (OpenAI).
 # @param api_key API key for the provider.
 # @param base_url Base URL (optional).
 # @param model_id Model identifier.
 #
 # @return LLMProvider instance.
 #
def create_provider(sdk_type, api_key, base_url, model_id):
    sdk = str(sdk_type).strip().lower()
    if sdk in ["message", "messages", "anthropic"]:
        return AnthropicProvider(api_key, base_url, model_id)
    elif sdk in ["chat completions", "chat completion", "chat_completions", "openai", "nvidia"]:
        return OpenAIProvider(api_key, base_url, model_id)
    elif sdk in ["responses", "response"]:
        raise ValueError("SDK_TYPE='Responses' (OpenAI Responses API) is not supported yet.")
    elif sdk in ["interactions", "interaction"]:
        raise ValueError("SDK_TYPE='Interactions' (Gemini Interactions API) is not supported yet.")
    elif sdk in ["generate content", "generatecontent", "ai studio", "gemini", "google"]:
        raise ValueError(
            f"Google/Gemini native SDK support has been removed. "
            f"Please use SDK_TYPE='Chat Completions' with Google's OpenAI-compatible endpoint instead."
        )
    else:
        raise ValueError(
            f"Unsupported SDK_TYPE: '{sdk_type}'. Supported types: 'Message' (Anthropic), 'Chat Completions' (OpenAI)."
        )
# End-def
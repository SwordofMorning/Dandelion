##
 # @file src/utils/safe_llm/__init__.py
 # @date 2026/08/05
 # 
 # @brief Safe LLM Client Package.
 # Provides a thread-safe wrapper around LLM providers with routing and fallback capabilities.
 #

from .safe_llm import SafeLLMClient

__all__ = [
    "SafeLLMClient",
]
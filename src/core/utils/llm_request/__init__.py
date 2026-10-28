##
 # @file src/core/utils/llm_request/__init__.py
 # @date 2026/08/05
 #
 # @brief Shared LLM request client package.
 # Exports SafeLLMClient as the common entry point for provider calls, request
 # logging, and task-based SubAgent model routing with retry and fallback.
 #

from .llm_request import SafeLLMClient

__all__ = [
    "SafeLLMClient",
]
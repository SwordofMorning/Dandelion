# src/utils/safe_llm/__init__.py

"""
Safe LLM Client Package.

Provides a thread-safe wrapper around LLM providers with routing and fallback capabilities.
"""

from .safe_llm import SafeLLMClient

__all__ = [
    "SafeLLMClient",
]
# src/utils/__init__.py

"""
Utils Package - Refactored structure.

This package provides utilities for the Regent agent.
All submodules are organized into focused subpackages.
"""

# CLI utilities
from .cli import CLIPrinter, InteractiveCLI, cli

# Config utilities
from .config import load_api_config

# LLM Provider utilities
from .llm_provider import (
    LLMProvider,
    AnthropicProvider,
    OpenAIProvider,
    GeminiProvider,
    create_provider,
)

# Logging utilities
from .logging import AgentLogger, SessionManager

# Routing utilities
from .routing import (
    RateLimiter,
    RateLimitConfig,
    UsageWindow,
    ModelRegistry,
    RegistryModelSpec,
    RoutingPolicy,
)

# Safe LLM Client
from .safe_llm import SafeLLMClient

__all__ = [
    # CLI
    "CLIPrinter",
    "InteractiveCLI",
    "cli",
    # Config
    "load_api_config",
    # LLM Provider
    "LLMProvider",
    "AnthropicProvider",
    "OpenAIProvider",
    "GeminiProvider",
    "create_provider",
    # Logging
    "AgentLogger",
    "SessionManager",
    # Routing
    "RateLimiter",
    "RateLimitConfig",
    "UsageWindow",
    "ModelRegistry",
    "RegistryModelSpec",
    "RoutingPolicy",
    # Safe LLM
    "SafeLLMClient",
]
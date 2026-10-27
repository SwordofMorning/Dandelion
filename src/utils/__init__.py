##
 # @file src/utils/__init__.py
 # @date 2026/08/04
 # 
 # @brief Provides utilities for the Dandelion agent.
 #

from importlib import import_module

# Importing core helpers must not initialize the terminal frontend.
_EXPORTS = {
    "CLIPrinter": (".cli", "CLIPrinter"),
    "InteractiveCLI": (".cli", "InteractiveCLI"),
    "cli_printer": (".cli", "cli_printer"),
    "load_api_config": (".config", "load_api_config"),
    "LLMProvider": (".llm_provider", "LLMProvider"),
    "AnthropicProvider": (".llm_provider", "AnthropicProvider"),
    "OpenAIProvider": (".llm_provider", "OpenAIProvider"),
    "create_provider": (".llm_provider", "create_provider"),
    "AgentLogger": (".logging", "AgentLogger"),
    "SessionManager": (".logging", "SessionManager"),
    "ModelRegistry": (".routing", "ModelRegistry"),
    "RegistryModelSpec": (".routing", "RegistryModelSpec"),
    "RoutingPolicy": (".routing", "RoutingPolicy"),
    "SafeLLMClient": (".llm_request", "SafeLLMClient"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module, __name__), attribute)
    globals()[name] = value
    return value

__all__ = [
    # CLI
    "CLIPrinter",
    "InteractiveCLI",
    "cli_printer",
    # Config
    "load_api_config",
    # LLM Provider
    "LLMProvider",
    "AnthropicProvider",
    "OpenAIProvider",
    "create_provider",
    # Logging
    "AgentLogger",
    "SessionManager",
    # Routing
    "ModelRegistry",
    "RegistryModelSpec",
    "RoutingPolicy",
    # Safe LLM
    "SafeLLMClient",
]

##
 # @file src/core/utils/__init__.py
 # @date 2026/08/04
 #
 # @brief Shared Core utility exports.
 # Exposes configuration, provider, logging and routing helpers through lazy imports,
 # so callers can use shared services without initializing either frontend.
 #

from importlib import import_module

# Importing core helpers must not initialize the terminal frontend.
_EXPORTS = {
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


##
 # @brief Resolve utilities without eagerly importing a frontend.
 # @param name Public utility name.
 # @return Lazily imported utility.
 #
def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    # End-if
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module, __name__), attribute)
    globals()[name] = value
    return value
# End-def

__all__ = [
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

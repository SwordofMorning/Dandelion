##
 # @file src/core/__init__.py
 # @date 2026/08/06
 # 
 # @brief Core Package.
 # Provides agent (LLM request), memory, skill and prompt builder.
 #

from importlib import import_module

_EXPORTS = {
    "MemoryManager": ".memory",
    "SkillManager": ".skill",
    "PromptBuilder": ".sysprompt",
    "ContextCompressor": ".compress",
    "CompressCondition": ".compress",
    "is_context_overflow_error": ".compress",
    "extract_usage_tokens": ".compress",
    "MyAgent": ".agent",
    "run_agent_loop": ".runner",
}

__all__ = list(_EXPORTS)


##
 # @brief Resolve a public Core helper without loading either frontend.
 # @param name Public helper name.
 # @return Lazily imported helper.
 #
def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    # End-if
    value = getattr(import_module(_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
# End-def

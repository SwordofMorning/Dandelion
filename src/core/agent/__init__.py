##
 # @file src/core/agent/__init__.py
 # @date 2026/10/28
 #
 # @brief Main-agent execution and context package.
 # Groups the agent step, shared turn runner, tool factory, prompt builder,
 # persistent memory, skill catalog and context compaction implementations.
 #
 # @note Public helpers are loaded on demand without importing a frontend.
 #

from importlib import import_module

_EXPORTS = {
    "MyAgent": ".agent",
    "strip_dynamic_context": ".agent",
    "MemoryManager": ".memory",
    "SkillManager": ".skill",
    "PromptBuilder": ".sysprompt",
    "ContextCompressor": ".compress",
    "CompressCondition": ".compress",
    "is_context_overflow_error": ".compress",
    "extract_usage_tokens": ".compress",
    "run_agent_loop": ".runner",
    "build_tools": ".tools",
}

__all__ = list(_EXPORTS)

##
 # @brief Resolve a public agent helper without eager package initialization.
 # @param name Exported helper name.
 # @return Imported class or function.
 #
def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    # End-if
    value = getattr(import_module(_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
# End-def

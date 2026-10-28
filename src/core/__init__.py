##
 # @file src/core/__init__.py
 # @date 2026/08/06
 # 
 # @brief Shared agent execution package.
 # Exposes the agent, prompt, memory and compaction helpers through lazy exports.
 # Keeps execution code, subagents, tools and utilities independent of frontends.
 #
 # @note Importing Core does not initialize the CLI or Remote service.
 #

from importlib import import_module

_EXPORTS = {
    "MemoryManager": ".agent.memory",
    "SkillManager": ".agent.skill",
    "PromptBuilder": ".agent.sysprompt",
    "ContextCompressor": ".agent.compress",
    "CompressCondition": ".agent.compress",
    "is_context_overflow_error": ".agent.compress",
    "extract_usage_tokens": ".agent.compress",
    "MyAgent": ".agent",
    "run_agent_loop": ".agent.runner",
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

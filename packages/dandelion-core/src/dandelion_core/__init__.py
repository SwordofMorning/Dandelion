##
 # @file packages/dandelion-core/src/dandelion_core/__init__.py
 # @date 2026/11/28
 #
 # @brief Standalone Dandelion Core execution package.
 # Exposes the abstract runtime and host bases, sync wire protocol, main agent,
 # prompt builders, memory, skill catalog, compaction, pruning, and turn runner.
 #

from .runtime import BaseRuntime, DefaultRuntime, Runtime
from .sync_protocol import (
    REQUIRED_FILES,
    OPTIONAL_FILES,
    ALLOWED_FILES,
    CHUNK_SIZE,
    key_digest,
    snapshot_id,
)
from .agent import (
    MyAgent,
    MemoryManager,
    SkillManager,
    BasePromptBuilder,
    DefaultPromptBuilder,
    PromptBuilder,
    ContextCompressor,
    CompressCondition,
    is_context_overflow_error,
    extract_usage_tokens,
    ContextPruner,
    block_field,
    validate_history_protocol,
    run_agent_loop,
    TurnInterrupt,
    MODE_STOP,
    MODE_ROLLBACK,
)
from .host import BaseAgentHost

__all__ = [
    "BaseRuntime",
    "DefaultRuntime",
    "Runtime",
    "BaseAgentHost",
    "MyAgent",
    "MemoryManager",
    "SkillManager",
    "BasePromptBuilder",
    "DefaultPromptBuilder",
    "PromptBuilder",
    "ContextCompressor",
    "CompressCondition",
    "is_context_overflow_error",
    "extract_usage_tokens",
    "ContextPruner",
    "block_field",
    "validate_history_protocol",
    "run_agent_loop",
    "TurnInterrupt",
    "MODE_STOP",
    "MODE_ROLLBACK",
    "REQUIRED_FILES",
    "OPTIONAL_FILES",
    "ALLOWED_FILES",
    "CHUNK_SIZE",
    "key_digest",
    "snapshot_id",
]

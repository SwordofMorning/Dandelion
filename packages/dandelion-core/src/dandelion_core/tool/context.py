##
 # @file packages/dandelion-core/src/dandelion_core/tool/context.py
 # @date 2026/11/28
 #
 # @brief Typed dependency bundle passed to hot-plugged and built-in tools.
 # Encapsulates host identity, workspace root, parsed configuration, operator
 # config directory, runtime adapter, session/skill/memory managers, optional
 # MainAgent reference, protected directories, and host-specific extras.
 #

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


##
 # @brief Immutable dependency bundle passed to BaseTool.create_instance().
 #
@dataclass(frozen=True)
class ToolContext:
    host: str = "core"
    workspace_dir: str = "."
    config: Mapping[str, Any] = field(default_factory=dict)
    config_dir: Optional[str] = None
    runtime: Any = None
    session_manager: Any = None
    skill_manager: Any = None
    memory_manager: Any = None
    agent: Any = None
    protected_dirs: tuple = ()
    extras: Mapping[str, Any] = field(default_factory=dict)

    ##
     # @brief Return the normalized [Tool:<name>] / [Tool.<name>] config section.
     #
     # @param name Tool identifier or section suffix (case-insensitive).
     #
     # @return Shallow dict copy of the section options, or empty dict when absent.
     #
    def tool_config(self, name: str) -> dict:
        if not isinstance(self.config, Mapping):
            return {}
        # End-if
        sections = self.config.get("TOOL_CONFIG")
        if not isinstance(sections, Mapping):
            return {}
        # End-if
        key = str(name or "").strip().lower()
        if not key:
            return {}
        # End-if
        direct = sections.get(key)
        if isinstance(direct, Mapping):
            return dict(direct)
        # End-if
        for sec_key, sec_val in sections.items():
            if str(sec_key).strip().lower() == key and isinstance(sec_val, Mapping):
                return dict(sec_val)
            # End-if
        # End-for
        return {}
    # End-def
# End-class

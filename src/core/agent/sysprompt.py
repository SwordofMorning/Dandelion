##
 # @file src/core/agent/sysprompt.py
 # @date 2026/08/06
 #
 # @brief Abstract base system-prompt builder.
 # Defines shared environment detection, skills catalog, language policy,
 # memory/pruning instructions, and dynamic task-state rendering, while
 # requiring concrete frontends (CLI and Remote) to subclass BasePromptBuilder.
 #

import platform
import shutil


##
 # @brief Abstract base class for constructing static system prompts and dynamic context blocks.
 #
class BasePromptBuilder:
    ##
     # @brief Constructor.
     #
     # @param memory_manager MemoryManager (memories index + injection).
     # @param skill_manager SkillManager (skills catalog).
     # @param config Flat config dict from load_api_config.
     # @param workspace_dir Workspace root directory.
     # @param session_manager SessionManager (session-scoped task state), optional.
     # @param tool_names Optional iterable of registered tool names for section filtering.
     #
     # @return None.
     #
    def __init__(self, memory_manager, skill_manager, config, workspace_dir=".",
                 session_manager=None, tool_names=None):
        self.memory = memory_manager
        self.skill = skill_manager
        self.config = config
        self.workspace_dir = workspace_dir
        self.session_manager = session_manager
        self.tool_names = frozenset(tool_names) if tool_names is not None else None

        self.os_name = platform.system()
        self.has_pwsh = shutil.which("powershell") is not None
        self.has_bash = shutil.which("bash") is not None

        if self.os_name == "Windows" and self.has_pwsh:
            self.terminal_hint = "Windows Environment. Primary shell is 'powershell'. Avoid linux-specific arguments like 'ls -la'."
        elif self.os_name == "Windows" and self.has_bash:
            self.terminal_hint = "Windows Environment but using 'bash' (Git Bash/MSYS). Use standard unix commands."
        else:
            self.terminal_hint = f"{self.os_name} Environment. Primary shell is 'bash'."
        # End-if
    # End-def

    ##
     # @brief Check whether the caller exposes a named tool.
     #
     # @param name Tool name to check.
     #
     # @return True when the tool is available.
     #
    def _has_tool(self, name):
        return self.tool_names is None or name in self.tool_names
    # End-def

    ##
     # @brief Build the Skills Catalog section when load_skill is available.
     #
     # @return Skills section string, or empty string.
     #
    def _build_skills_section(self):
        if not self._has_tool("load_skill"):
            return ""
        # End-if
        catalog = self.skill.get_catalog()
        return (
            f"Available Skills:\n{catalog}\n"
            "Use the 'load_skill' tool to fetch the full content of a skill when you need specific formats or rules."
        )
    # End-def

    ##
     # @brief Build the shared ASCII internal storage Language Policy section.
     #
     # @return Language policy section string.
     #
    @staticmethod
    def _build_language_section():
        return (
            "Language Policy:\n"
            "1. User-facing replies and final deliverables (documents, reports) MAY use the user's language (e.g., Chinese).\n"
            "2. INTERNAL ARTIFACTS MUST BE ENGLISH/ASCII ONLY, including: tool inputs for 'remember', 'forget' and 'update_state' "
            "(name, description, tags, content, scope, target, todos, completed), global memory files under llm/memory/, "
            "session memory files under .log/sess_*/memory/, the per-tier MEMORY.md indexes, "
            "task_state.json under .log/sess_*/task_state.json, artifact filenames, and any intermediate storage.\n"
            "3. Rationale: internal keyword retrieval splits on ASCII whitespace; non-ASCII (Chinese) text breaks matching. "
            "If the user speaks Chinese, translate internal state/memory content into English before storing.\n"
            "4. Tools enforce this strictly: if 'remember' or 'update_state' returns an ASCII-only error, "
            "translate the offending values to English and re-submit."
        )
    # End-def

    ##
     # @brief Build the shared Memory & Context Management guide section.
     #
     # @return Memory and context management section string.
     #
    def _build_memory_section(self):
        lines = [
            "Memory & Context Management:",
            "1. You have a persistent two-tier memory system (global tier + session tier, up to 500 index entries per tier).",
            "2. Relevant memories and the current task state are auto-injected by your runtime as a [Dandelion Context] block appended to the latest user message. It is reference data - not something the user typed.",
            "3. Use 'remember' (scope='global' or 'session') to persist important facts; use 'forget' to remove obsolete, superseded, or merged memories so your memory stays clean and self-organized; use 'update_state' to keep target/todos/completed current.",
            "4. Update your memory whenever necessary to record key engineering paths, decisions, and code status before context compression or pruning.",
        ]
        if self._has_tool("prune_context"):
            lines.append(
                "5. Use 'prune_context' after finishing a large file inspection or sub-problem (once key takeaways are saved via 'remember') to fold verbose tool outputs and keep history.log lean."
            )
        # End-if
        return "\n".join(lines)
    # End-def

    ##
     # @brief Optional hook for subclasses to append extra dynamic context blocks.
     #
     # @return Extra dynamic context string, or empty string.
     #
    def render_extra_dynamic_context(self):
        return ""
    # End-def

    ##
     # @brief Build the base system prompt sections (overridden by CLI and Remote subclasses).
     #
     # @return Static system prompt string.
     #
    def build(self):
        sections = [
            "You are Dandelion, a professional coding and management agent.",
            f"Environment Info:\n{self.terminal_hint}",
        ]
        skills_sec = self._build_skills_section()
        if skills_sec:
            sections.append(skills_sec)
        # End-if
        sections.append(self._build_language_section())
        sections.append(self._build_memory_section())
        return "\n\n".join(sections)
    # End-def

    ##
     # @brief Render the Task State block text (Attention Anchor).
     #
     # @param state Task state dict loaded from task_state.json.
     # @param session_hint Optional " (session: xxx)" suffix.
     #
     # @return Rendered multi-line block text.
     #
    @staticmethod
    def render_task_state_text(state, session_hint=""):
        ##
         # @brief Normalize a task-state list field into a comma-separated string.
         #
         # @param value Raw task-state field value.
         #
         # @return Normalized text representation.
         #
        def _coerce_list(value):
            if value is None:
                return ""
            # End-if
            if isinstance(value, list):
                return ", ".join(str(x) for x in value if x is not None)
            # End-if
            return str(value)
        # End-def

        return (
            "## Current Task State (Attention Anchor)"
            f"{session_hint}\n"
            f"- Target: {state.get('target', 'None')}\n"
            f"- Pending TODOs: {_coerce_list(state.get('todos'))}\n"
            f"- Completed: {_coerce_list(state.get('completed'))}\n"
            "(You must frequently use the 'update_state' tool to keep this updated)"
        )
    # End-def
# End-class

# Alias kept for external imports referencing PromptBuilder.
PromptBuilder = BasePromptBuilder

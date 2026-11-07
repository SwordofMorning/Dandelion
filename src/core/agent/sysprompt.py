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
     # @brief Build the shared Language Policy section for memory and internal state.
     #
     # @return Language policy section string.
     #
    @staticmethod
    def _build_language_section():
        return (
            "Language Policy:\n"
            "1. User-facing replies and final deliverables (documents, reports) MAY use the user's language (e.g., Chinese).\n"
            "2. For 'remember' and 'forget', 'name' and 'scope' MUST be English/ASCII identifiers. "
            "'description', 'tags', and 'content' MAY be submitted in the user's natural language (UTF-8, e.g., Chinese) when vector memory is active.\n"
            "3. For 'update_state' (target, todos, completed), task_state.json under .log/sess_*/task_state.json, and artifact filenames, "
            "values MUST remain English/ASCII only.\n"
            "4. Fallback enforcement: if 'remember' or 'update_state' returns an error indicating vector memory is unavailable or requiring English/ASCII, "
            "translate the offending fields to English/ASCII and re-submit."
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
            "3. Use 'remember' (scope='global' or 'session'; re-using an existing 'name' in the same scope upserts/replaces the entry) to persist important facts; use 'forget' to remove obsolete or merged memories; use 'memory_search' to actively query global/session memories beyond the passive top-K digest; use 'update_state' to keep target/todos/completed current.",
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

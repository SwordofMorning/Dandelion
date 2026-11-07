##
 # @file src/remote/sysprompt.py
 # @date 2026/10/28
 #
 # @brief System-prompt builder for Dandelion Remote (DR).
 # Specializes BasePromptBuilder for an always-on remote coding companion with
 # unrestricted server access, interrupt-driven wakeups, autonomous communication
 # tools, pure-filesystem synced session digestion, and self-organizing memory.
 #

from src.core.agent.sysprompt import BasePromptBuilder


##
 # @brief Concrete system-prompt builder for Dandelion Remote.
 #
class RemotePromptBuilder(BasePromptBuilder):
    ##
     # @brief Constructor.
     #
     # @param memory_manager MemoryManager instance.
     # @param skill_manager SkillManager instance.
     # @param config Loaded configuration dictionary.
     # @param workspace_dir Server workspace directory.
     # @param session_manager SessionManager instance.
     # @param tool_names Optional iterable of registered tool names.
     # @param backups Optional BackupStore instance for synced session overview.
     #
     # @return None.
     #
    def __init__(self, memory_manager, skill_manager, config, workspace_dir=".",
                 session_manager=None, tool_names=None, backups=None):
        super().__init__(
            memory_manager=memory_manager,
            skill_manager=skill_manager,
            config=config,
            workspace_dir=workspace_dir,
            session_manager=session_manager,
            tool_names=tool_names,
        )
        self.backups = backups
    # End-def

    ##
     # @brief Render a live digest of recently synced CLI sessions for [Dandelion Context].
     #
     # @return Synced session overview string, or empty string when none exist.
     #
    def render_extra_dynamic_context(self):
        if self.backups is None:
            return ""
        # End-if
        try:
            return self.backups.render_sync_digest(limit=10)
        except Exception:
            return ""
        # End-try
    # End-def

    ##
     # @brief Build the static system prompt for Dandelion Remote by assembling
     #        'llm/sysprompt/remote.md' with shared skill, language, and memory sections.
     #
     # @return Static system prompt string.
     #
    def build(self):
        default_body = (
            "You are Dandelion Remote, a 24/7 autonomous coding and management agent.\n\n"
            "Environment Info:\n{terminal_hint}\n"
            "Workspace Root: {workspace_dir}"
        )
        sections = [self._load_sysprompt_file("remote.md", fallback=default_body)]

        skills_sec = self._build_skills_section()
        if skills_sec:
            sections.append(skills_sec)
        # End-if

        sections.append(self._build_language_section())
        sections.append(self._build_memory_section())
        return "\n\n".join(s for s in sections if s)
    # End-def
# End-class

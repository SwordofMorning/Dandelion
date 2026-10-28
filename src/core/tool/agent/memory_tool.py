##
 # @file src/core/tool/agent/memory_tool.py
 # @date 2026/08/13
 #
 # @brief Persistent memory-writing tool.
 # Validates model-facing memory fields and forwards writes to the global or
 # active-session memory tier through MemoryManager.
 #
 # @note Only allowed to write ASCII characters due to key words match.
 #
 # @note write memory: LLM -> MemoryTool
 # @note read memory: In sysprompt (user's msg append).
 #

from ..base_tool import BaseTool

##
 # @brief Memory Class.
 #
class MemoryTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param memory_manager Memory Manager Class.
     #
     # @see src/core/agent/memory.py
     #
    def __init__(self, memory_manager):
        super().__init__()
        self.memory = memory_manager
    # End-def

    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "remember"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return (
            "Save important facts, user preferences, or architectural decisions to memory. "
            "Memory is two-tier: use scope='global' (default) for project-wide knowledge "
            "preserved across sessions (coding style, architecture decisions, preferences), "
            "or scope='session' for facts that only apply to the current session branch. "
            "Use this when you learn something that should not be forgotten."
        )
    # End-def

    ##
     # @brief Return tool's schema.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique short name for the memory topic (e.g., 'coding_style')."},
                "description": {"type": "string", "description": "One sentence summary of what this memory is about."},
                "tags": {"type": "string", "description": "Comma separated tags (e.g., 'preference, python')."},
                "content": {"type": "string", "description": "The detailed content to remember."},
                "scope": {
                    "type": "string",
                    "enum": ["global", "session"],
                    "description": "Storage scope: 'global' (project-wide, default) or 'session' (current session branch only)."
                }
            },
            "required": ["name", "description", "content"]
        }
    # End-def

    ##
     # @brief Save memory entry.
     #
     # @param kwargs schema properties: name, description, tags, content, scope.
     #
     # @note ASCII-only enforcement: non-ASCII values are rejected because
     # internal keyword retrieval splits on ASCII whitespace.
     #
     # @return (success_bool, result_string)
     #
    def execute(self, **kwargs):
        name = kwargs.get("name")
        description = kwargs.get("description", "")
        tags = kwargs.get("tags", "general")
        content = kwargs.get("content", "")
        # Treat an explicit null scope the same as an omitted one (default global).
        scope = str(kwargs.get("scope") or "global").strip().lower()

        if not name or not content:
            return False, "Error: name and content are required."

        if scope not in ("global", "session"):
            return False, f"Error: scope must be 'global' or 'session', got '{scope}'."

        # ASCII-only enforcement (Language Policy): internal storage must stay
        # ASCII so keyword retrieval (space-split) keeps working. Chinese or
        # other non-ASCII values are rejected with a clear hint to translate.
        non_ascii = [
            v for v in (name, description, tags, content)
            if any(ord(ch) > 127 for ch in str(v or ""))
        ]
        if non_ascii:
            return False, (
                "Error: 'remember' stores ASCII-only content (non-ASCII text breaks "
                "keyword retrieval). Please translate the following values to English "
                f"and re-submit: {non_ascii}"
            )

        try:
            success = self.memory.write_memory(name, description, tags, content, scope=scope)
        except ValueError as e:
            # Reserved names (e.g. MEMORY -> MEMORY.md collision), sanitized-name
            # collisions, overlong names and unverifiable existing files surface
            # as a tool error instead of crashing the turn.
            return False, str(e)
        except OSError as e:
            # Disk-level failures (ENAMETOOLONG survivors, permission errors,
            # full disk) must never crash the agent turn.
            return False, f"Error: failed to write memory file: {e}"
        if success:
            # No content echo: the model just generated this content and the
            # [Dandelion Context] block refreshes at the next user turn, so
            # echoing description/tags/content would only duplicate sensitive
            # memory data into the persisted history.
            return True, (
                f"Successfully saved {scope} memory topic '{name}' "
                f"({len(content)} chars). It will appear in the dynamic "
                f"context on the next user turn."
            )
        # End-if
        return False, "Failed to save memory."
    # End-def
# End-class


##
 # @brief Persistent memory-removal tool.
 # Allows the Agent to forget obsolete, redundant, or merged memories from
 # the global or active-session tier.
 #
class ForgetMemoryTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param memory_manager MemoryManager instance.
     #
     # @return None.
     #
    def __init__(self, memory_manager):
        super().__init__()
        self.memory = memory_manager
    # End-def

    ##
     # @brief Return tool's name.
     #
     # @return Tool identifier string.
     #
    def get_name(self):
        return "forget"
    # End-def

    ##
     # @brief Return tool's description.
     #
     # @return Human-readable tool description.
     #
    def get_description(self):
        return (
            "Remove an obsolete, superseded, or merged memory topic from storage. "
            "Use scope='global' (default) for global project memories or scope='session' "
            "for current session memories. Use this to keep memory clean and self-organized."
        )
    # End-def

    ##
     # @brief Return tool's schema.
     #
     # @return JSON input schema dictionary.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact name of the memory topic to remove (as shown in [name] in the memory index)."
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "session"],
                    "description": "Storage scope: 'global' (default) or 'session'."
                }
            },
            "required": ["name"]
        }
    # End-def

    ##
     # @brief Delete a memory entry from the specified scope.
     #
     # @param kwargs Schema properties: name, scope.
     #
     # @return (success_bool, result_string) tuple.
     #
    def execute(self, **kwargs):
        name = str(kwargs.get("name") or "").strip()
        scope = str(kwargs.get("scope") or "global").strip().lower()

        if not name:
            return False, "Error: 'name' is required."
        # End-if
        if scope not in ("global", "session"):
            return False, f"Error: scope must be 'global' or 'session', got '{scope}'."
        # End-if

        try:
            removed = self.memory.delete_memory(name, scope=scope)
        except ValueError as e:
            return False, str(e)
        except OSError as e:
            return False, f"Error: failed to remove memory file: {e}"
        # End-try

        if removed:
            return True, f"Successfully forgot {scope} memory topic '{name}'."
        # End-if
        return False, f"Memory topic '{name}' was not found in {scope} scope."
    # End-def
# End-class
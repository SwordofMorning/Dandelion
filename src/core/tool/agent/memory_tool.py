##
 # @file src/core/tool/agent/memory_tool.py
 # @date 2026/08/13
 #
 # @brief Persistent memory-writing tool.
 # Validates model-facing memory fields and forwards writes to the global or
 # active-session memory tier through MemoryManager.
 #
 # @note Supports UTF-8 content when vector memory is active; falls back to
 #       requiring English/ASCII when vector memory is disabled or unavailable.
 #
 # @note write memory: LLM -> MemoryTool
 # @note read memory: In sysprompt (user's msg append).
 #

import json
from ..base_tool import BaseTool
from src.core.utils.embedding import EmbeddingError

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
     # @note Enforces ASCII for 'name' and 'scope'; allows UTF-8 for
     # 'description', 'tags', and 'content' when vector pre-embedding succeeds,
     # otherwise rejects non-ASCII asking the LLM to re-submit in English/ASCII.
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

        # @note Write-time UTF-8/ASCII fallback guard: 'name' and 'scope' must
        # always be ASCII identifiers. 'description', 'tags', and 'content' may
        # use UTF-8 (e.g. Chinese) ONLY when vector memory is active and
        # pre-embedding succeeds. If vector memory is disabled or fails at write
        # time, reject non-ASCII and ask the LLM to re-submit in English/ASCII
        # so keyword split mode works reliably. Do NOT remove this fallback!
        if any(ord(ch) > 127 for v in (name, scope) for ch in str(v or "")):
            return False, "Error: memory 'name' and 'scope' must be ASCII-only."
        # End-if
        precomputed_vec = None
        if any(ord(ch) > 127 for v in (description, tags, content) for ch in str(v or "")):
            try:
                precomputed_vec = self.memory.preembed_entry(name, description, content, tags)
            except EmbeddingError:
                return False, (
                    "Error: vector memory is disabled or unavailable (fallback split mode is active). "
                    "Please translate 'description', 'tags', and 'content' to English/ASCII and re-submit."
                )
            # End-try
        # End-if

        try:
            success = self.memory.write_memory(name, description, tags, content, scope=scope, precomputed_vector=precomputed_vec)
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


##
 # @brief Active semantic/keyword memory search tool.
 #
class MemorySearchTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param memory_manager MemoryManager instance.
     #
    def __init__(self, memory_manager):
        super().__init__()
        self.memory = memory_manager
    # End-def

    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "memory_search"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return (
            "Actively search global and/or session memories by semantic similarity "
            "(with automatic keyword fallback). Use this when you need to recall "
            "facts, preferences, or prior decisions not shown in the passive top-K digest."
        )
    # End-def

    ##
     # @brief Return tool's schema.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query or keywords."},
                "scope": {"type": "string", "enum": ["all", "global", "session"], "description": "Search scope (default: 'all')."},
                "top_k": {"type": "integer", "description": "Max results to return (1-10, default: 5)."},
            },
            "required": ["query"],
        }
    # End-def

    ##
     # @brief Execute memory search across global and/or session tiers.
     #
    def execute(self, **kwargs):
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return False, "Error: 'query' must be a non-empty string."
        # End-if
        scope = str(kwargs.get("scope") or "all").strip().lower()
        if scope not in ("all", "global", "session"):
            return False, f"Error: scope must be 'all', 'global', or 'session', got '{scope}'."
        # End-if
        try:
            top_k = min(10, max(1, int(kwargs.get("top_k", 5) or 5)))
        except (TypeError, ValueError):
            top_k = 5
        # End-try
        res = self.memory.search(query, scope=scope, top_k=top_k)
        hits = [
            {
                "name": h["name"],
                "scope": h["scope"],
                "description": h.get("description", ""),
                "tags": h.get("tags", ""),
                "score": h.get("score", 0.0),
                "body": h.get("body", "")[:2000],
            }
            for h in res.get("hits", [])
        ]
        return True, json.dumps({"backend": res.get("backend"), "reason": res.get("reason"), "hits": hits}, ensure_ascii=False)
    # End-def
# End-class

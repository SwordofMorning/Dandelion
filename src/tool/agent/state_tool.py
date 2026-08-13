##
 # @file src/tool/agent/state_tool.py
 # @date 2026/08/13
 # 
 # @brief Update Task State (Agent's Target).
 #

import os
import json
from ..base_tool import BaseTool

##
 # @brief State Update Class.
 #
class StateTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param workspace_dir Default to current directory if not explicitly provided.
     # @param session_manager Session manager; resolves the active session's
     # task_state.json dynamically.
     #
    def __init__(self, workspace_dir=None, session_manager=None):
        super().__init__(workspace_dir)
        self.session_manager = session_manager
    # End-def

    ##
     # @brief Resolve the active session's task_state.json dynamically.
     #
     # @note Session-scoped only: the legacy global state file
     # (llm/task/task_state.json) was removed, so there is NO fallback.
     # The session manager creates a blank file on demand
     # (ensure_task_state_file); None means no active session, and callers
     # must fail loudly instead of touching any global file.
     # Resolving dynamically makes `checkout` (session switch) work without
     # rebuilding the agent.
     #
     # @return Path to task_state.json, or None if no active session.
     #
    def _get_state_file(self):
        if self.session_manager is not None:
            return self.session_manager.ensure_task_state_file()
        return None

    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "update_state"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return (
            "Update the current task state, including the main target and TODO lists. "
            "This state is scoped to the current session branch "
            "(stored at .log/sess_<id>/task_state.json) and is injected into the "
            "conversation as part of the [Dandelion Context] block appended "
            "to the latest user message, to keep you focused on the current task."
        )
    # End-def

    ##
     # @brief Return tool's schema.
     #
    def get_schema(self):
        # All fields optional: execute() merges into existing state, so the
        # model can update a single field without rewriting the whole state.
        return {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "The overall goal of current session."},
                "todos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of pending sub-tasks."
                },
                "completed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of completed sub-tasks."
                }
            }
        }

    ##
     # @brief Load current task state.
     #
     # @note Falls back to defaults when the file is missing, malformed or
     # no active session exists; a corrupt file is reported loudly.
     #
     # @return State dict {target, todos, completed}.
     #
    def _load(self):
        state = {"target": "No specific target set.", "todos": [], "completed": []}
        state_file = self._get_state_file()
        if state_file is None:
            # No active session: execute() reports the error before writing;
            # nothing to read here.
            return state
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # Keep the loaded value only when it is a dict; otherwise the
                # file is malformed (e.g. manual edits) and execute would crash
                # on state["target"] updates.
                if isinstance(loaded, dict):
                    state = loaded
                else:
                    print(f"[-] Warning: task state at {state_file} is not a JSON "
                          "object; falling back to a fresh default state.")
            except Exception as e:
                # A corrupt state file must not break the tool, but the loss is
                # loud: the next successful update overwrites the corrupt file.
                print(f"[-] Warning: failed to parse task state at {state_file}: {e}")
        return state

    ##
     # @brief Write JSON atomically (tmp + os.replace).
     #
     # @param path Target file path.
     # @param state State dict to serialize.
     #
     # @note A crash mid-write never leaves a half-written task_state.json
     # (the attention anchor would be lost and silently reset to defaults).
     #
    @staticmethod
    def _atomic_write_json(path, state):
        """Write JSON atomically (tmp + os.replace) so a crash mid-write never
        leaves a half-written task_state.json (the attention anchor would be
        lost and silently reset to defaults on the next turn)."""
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)

    ##
     # @brief Check whether a value contains non-ASCII characters.
     #
     # @param value str or list of str.
     #
     # @return True if any non-ASCII char is found, False otherwise.
     #
    @staticmethod
    def _has_non_ascii(value):
        if isinstance(value, list):
            return any(ord(ch) > 127 for v in value for ch in str(v or ""))
        return any(ord(ch) > 127 for ch in str(value or ""))

    ##
     # @brief Safely render todos/completed as a single string.
     #
     # @param value null, list, or bare str.
     #
     # @return '' for null, ', '-joined strings for lists, str(value) otherwise.
     #
    @staticmethod
    def _coerce_list(value):
        if value is None:
            return ""
        if isinstance(value, list):
            return ", ".join(str(x) for x in value if x is not None)
        return str(value)

    ##
     # @brief Update the task state (target/todos/completed).
     #
     # @param kwargs schema properties: target, todos, completed (all optional).
     #
     # @note Merge semantics: only provided fields are updated, so partial
     # updates never wipe out the rest of the task state. ASCII-only
     # enforcement (Language Policy) rejects non-ASCII values with a hint.
     #
     # @return (success_bool, result_string) result_string echoes the merged
     # state on success so the model sees it immediately.
     #
    def execute(self, **kwargs):
        # Session-scoped only: without an active session there is no place to
        # persist task state. Fail loudly instead of writing a global file
        # (the legacy llm/task fallback was removed).
        if self.session_manager is None:
            return False, "Error: 'update_state' requires an active session (no session manager attached)."

        # Merge semantics: only provided fields are updated, so partial
        # updates never wipe out the rest of the task state.
        state = self._load()

        # ASCII-only enforcement (Language Policy): task_state.json is injected
        # into the system prompt and internal tooling splits on ASCII spaces,
        # so non-ASCII (e.g. Chinese) values are rejected with a hint to translate.
        for key in ("target", "todos", "completed"):
            if key in kwargs and kwargs.get(key) is not None and self._has_non_ascii(kwargs.get(key)):
                return False, (
                    f"Error: 'update_state' stores ASCII-only content for '{key}' "
                    "(non-ASCII text breaks internal handling). Please translate "
                    f"'{kwargs.get(key)}' to English and re-submit."
                )

        if "target" in kwargs and kwargs.get("target") is not None:
            state["target"] = kwargs.get("target")
        if "todos" in kwargs and kwargs.get("todos") is not None:
            state["todos"] = kwargs.get("todos")
        if "completed" in kwargs and kwargs.get("completed") is not None:
            state["completed"] = kwargs.get("completed")

        try:
            state_file = self._get_state_file()  # ensures the blank file exists
            self._atomic_write_json(state_file, state)
        except OSError as e:
            # Disk-level failures (state path corrupted into a directory, full
            # disk, permission errors) surface as a tool error instead of
            # crashing the whole agent turn.
            return False, f"Error: failed to write task state: {e}"
        # Echo the merged state back so the model sees it immediately in the
        # tool result (fresh region, cache-friendly). Mid-tool-loop the
        # [Dandelion Context] block is only refreshed at user turns, so
        # this echo is what keeps the updated anchor visible right after
        # update_state() without re-injecting into the messages prefix.
        return True, (
            "Task state updated:\n"
            f"- Target: {state.get('target', 'None')}\n"
            f"- Pending TODOs: {self._coerce_list(state.get('todos'))}\n"
            f"- Completed: {self._coerce_list(state.get('completed'))}"
        )

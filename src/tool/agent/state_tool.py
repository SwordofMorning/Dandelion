# src/tool/agent/state_tool.py

import os
import json
from ..base_tool import BaseTool

class StateTool(BaseTool):
    def __init__(self, workspace_dir=None, session_manager=None):
        super().__init__(workspace_dir)
        self.session_manager = session_manager
        # Legacy global path kept as a fallback for setups without a
        # session manager (e.g. standalone usage or tests).
        self.legacy_state_dir = os.path.join(self.workspace_dir, "llm/task")
        self.legacy_state_file = os.path.join(self.legacy_state_dir, "task_state.json")

    def _get_state_file(self):
        """Resolve the active session's task_state.json dynamically, so
        `checkout` (session switch) works without rebuilding the agent.

        Falls back to the legacy global file (llm/task/task_state.json) ONLY
        when no session manager is attached (standalone usage / tests). The
        fallback is loud on purpose: a global task_state must never be read
        or written silently, and production runs (main.py) always bind task
        state to a session via SessionManager.
        """
        if self.session_manager is not None:
            state_file = self.session_manager.get_task_state_file()
            if state_file:
                return state_file
        print("[-] Warning: StateTool has no session manager; using the legacy "
              "global state file (llm/task/task_state.json). Production runs "
              "(main.py) always bind task state to a session.")
        return self.legacy_state_file

    def _ensure_state_file(self):
        state_file = self._get_state_file()
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        if not os.path.exists(state_file):
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump({"target": "No specific target set.", "todos": [], "completed": []}, f)
        return state_file

    def get_name(self):
        return "update_state"

    def get_description(self):
        return (
            "Update the current task state, including the main target and TODO lists. "
            "This state is scoped to the current session branch "
            "(stored at .log/sess_<id>/task_state.json) and is injected into the "
            "system prompt on every turn to keep you focused."
        )

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

    def _load(self):
        state = {"target": "No specific target set.", "todos": [], "completed": []}
        state_file = self._get_state_file()
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # Keep the loaded value only when it is a dict; otherwise the
                # file is malformed (e.g. manual edits) and execute would crash
                # on state["target"] updates.
                if isinstance(loaded, dict):
                    state = loaded
            except Exception:
                pass
        return state

    @staticmethod
    def _has_non_ascii(value):
        """True if the value (str or list of str) contains non-ASCII chars."""
        if isinstance(value, list):
            return any(ord(ch) > 127 for v in value for ch in str(v or ""))
        return any(ord(ch) > 127 for ch in str(value or ""))

    def execute(self, **kwargs):
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

        state_file = self._ensure_state_file()
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        return True, "Task state updated successfully. It will be reflected in your next turn."

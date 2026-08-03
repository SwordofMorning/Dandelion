# src/tool/agent/state_tool.py

import os
import json
from ..base_tool import BaseTool

class StateTool(BaseTool):
    def __init__(self, workspace_dir=None):
        super().__init__(workspace_dir)
        self.state_dir = os.path.join(self.workspace_dir, "llm/task")
        self.state_file = os.path.join(self.state_dir, "task_state.json")
        os.makedirs(self.state_dir, exist_ok=True)
        
        if not os.path.exists(self.state_file):
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"target": "No specific target set.", "todos": [], "completed": []}, f)

    def get_name(self):
        return "update_state"

    def get_description(self):
        return (
            "Update the current task state, including the main target and TODO lists. "
            "Use this to manage your attention, plan steps, and mark them as completed. "
            "This state is injected into your system prompt on every turn to keep you focused."
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
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
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

        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        return True, "Task state updated successfully. It will be reflected in your next turn."
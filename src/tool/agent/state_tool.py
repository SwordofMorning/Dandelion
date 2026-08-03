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
            },
            "required": ["target", "todos", "completed"]
        }

    def execute(self, **kwargs):
        state = {
            "target": kwargs.get("target", ""),
            "todos": kwargs.get("todos", []),
            "completed": kwargs.get("completed", [])
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        return True, "Task state updated successfully. It will be reflected in your next turn."
# src/tool/agent/memory_tool.py

from ..base_tool import BaseTool

class MemoryTool(BaseTool):
    def __init__(self, memory_manager):
        super().__init__()
        self.memory = memory_manager

    def get_name(self):
        return "remember"

    def get_description(self):
        return (
            "Save important facts, user preferences, or architectural decisions to long-term memory. "
            "Memory is preserved across sessions. Use this when you learn something that should not be forgotten."
        )

    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique short name for the memory topic (e.g., 'coding_style')."},
                "description": {"type": "string", "description": "One sentence summary of what this memory is about."},
                "tags": {"type": "string", "description": "Comma separated tags (e.g., 'preference, python')."},
                "content": {"type": "string", "description": "The detailed content to remember."}
            },
            "required": ["name", "description", "content"]
        }

    def execute(self, **kwargs):
        name = kwargs.get("name")
        description = kwargs.get("description", "")
        tags = kwargs.get("tags", "general")
        content = kwargs.get("content", "")

        if not name or not content:
            return False, "Error: name and content are required."

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

        success = self.memory.write_memory(name, description, tags, content)
        if success:
            return True, f"Successfully saved memory topic '{name}'."
        return False, "Failed to save memory."
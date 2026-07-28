# src/tool/filesystem/edit_file_tool.py

import os
from ..base_tool import BaseTool

class EditFileTool(BaseTool):
    def __init__(self, workspace_dir=None):
        super().__init__(workspace_dir)

    def get_name(self):
        return "edit_file"

    def get_description(self):
        return (
            "Edit an existing file by replacing a specific exact text block with new text. "
            "This is much safer and faster than rewriting the entire file using write_file. "
            "You MUST provide the EXACT old text (including correct indentation and line breaks) "
            "as it currently appears in the file. It will replace ALL occurrences of the old text."
        )

    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file to edit."
                },
                "old_text": {
                    "type": "string",
                    "description": "The exact existing text block to be replaced. Must match exactly."
                },
                "new_text": {
                    "type": "string",
                    "description": "The new text block to insert in place of the old_text."
                }
            },
            "required": ["file_path", "old_text", "new_text"]
        }

    def execute(self, **kwargs):
        file_path = kwargs.get("file_path", "")
        old_text = kwargs.get("old_text", "")
        new_text = kwargs.get("new_text", "")

        if not file_path:
            return False, "Error: No file_path provided."
            
        if not old_text:
            return False, "Error: old_text cannot be empty."

        if not os.path.isabs(file_path):
            file_path = os.path.join(self.workspace_dir, file_path)
        file_path = os.path.abspath(file_path)

        # Security sandbox check
        if not self.check_workspace_permission(file_path, action_desc=f"EDIT File at '{file_path}'"):
            return False, (
                f"CRITICAL SECURITY BLOCK: Permission denied to edit file '{file_path}'. "
                f"STOP and acknowledge this restriction to the user."
            )

        if not os.path.exists(file_path):
            return False, f"Error: File not found at '{file_path}'."
            
        if not os.path.isfile(file_path):
            return False, f"Error: '{file_path}' is a directory, not a file."

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            if old_text not in content:
                # Provide helpful fallback if possible (e.g. whitespace mismatch)
                return False, (
                    f"Error: The specified old_text was not found in '{file_path}'. "
                    f"Please ensure indentation, spaces, and line breaks match exactly. "
                    f"Consider using read_file to check the exact content."
                )

            new_content = content.replace(old_text, new_text)

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            return True, f"Successfully edited file '{file_path}'."

        except UnicodeDecodeError:
            return False, f"Error: '{file_path}' could not be decoded as UTF-8."
        except Exception as e:
            return False, f"Error editing file: {e}"
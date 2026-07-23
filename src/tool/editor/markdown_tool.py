# src/tool/editor/markdown_tool.py

import os
from ..base_tool import BaseTool

class MarkdownTool(BaseTool):
    def __init__(self, workspace_dir=None):
        super().__init__(workspace_dir)

    def get_name(self):
        return "markdown_editor"

    def get_description(self):
        return (
            "Read, write, or append to a Markdown (.md) file. "
            "IMPORTANT: Before you write or append, you MUST call load_skill('markdown_style') "
            "if you haven't already, to ensure you comply with the project's strict format rules."
        )

    def get_schema(self):
        return {
            "type": "object", 
            "properties": {
                "action": {
                    "type": "string", 
                    "enum": ["read", "write", "append"],
                    "description": "Action to perform on the file."
                },
                "file_path": {
                    "type": "string", 
                    "description": "Absolute or relative path to the .md file."
                },
                "content": {
                    "type": "string", 
                    "description": "The markdown content to write or append (ignore if action is read)."
                }
            }, 
            "required": ["action", "file_path"]
        }

    def execute(self, **kwargs):
        action = kwargs.get("action")
        file_path = kwargs.get("file_path")
        content = kwargs.get("content", "")
        
        if not file_path.endswith(".md"):
            return False, "Error: Target file must have a .md extension."
            
        # SECURITY INJECTION: Check workspace permission
        if not self.check_workspace_permission(file_path, action_desc=f"{action.upper()} Markdown File"):
            # Cognitive Interrupt Error Message
            return False, (
                f"CRITICAL SECURITY BLOCK: The human user explicitly DENIED permission "
                f"to {action} the file '{file_path}'. STOP IMMEDIATELY. "
                f"Do not attempt any workarounds. Acknowledge this restriction to the user."
            )
            
        try:
            if action == "read":
                if not os.path.exists(file_path):
                    return False, f"Error: File not found at {file_path}"
                with open(file_path, "r", encoding="utf-8") as f:
                    return True, f.read()
                    
            elif action == "write":
                # Ensure directory exists
                os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)
                return True, f"Successfully written to {file_path}"
                
            elif action == "append":
                if not os.path.exists(file_path):
                    return False, f"Error: Cannot append. File not found at {file_path}"
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write("\n" + content)
                return True, f"Successfully appended to {file_path}"
                
            else:
                return False, f"Error: Unknown action '{action}'"
                
        except Exception as e:
            return False, f"Error performing {action} on {file_path}: {str(e)}"
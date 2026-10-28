##
 # @file src/core/tool/editor/markdown_tool.py
 # @date 2026/08/13
 #
 # @brief Workspace-bound Markdown file tool.
 # Reads, writes and appends Markdown files using paths relative to the workspace.
 # Applies shared path approval and guarded file-opening checks before access.
 #
 # @todo need to add some 'edit/modify' function.
 #

import os
from ..base_tool import BaseTool

##
 # @brief Markdown Class.
 #
class MarkdownTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param workspace_dir Default to current directory if not explicitly provided.
     #
    def __init__(self, workspace_dir=None):
        super().__init__(workspace_dir)
        self.workspace_dir = os.path.realpath(os.path.abspath(self.workspace_dir))
    # End-def

    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "markdown_editor"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return (
            "Read, write, or append to a Markdown (.md) file. "
            "IMPORTANT: Before you write or append, you MUST call load_skill('markdown_style') "
            "if you haven't already. "
            "WARNING: To avoid API timeouts, if generating large content (> 200 lines), "
            "use 'write' for the first chunk and 'append' for subsequent chunks iteratively."
        )
    # End-def

    ##
     # @brief Return tool's schema.
     #
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
    # End-def

    ##
     # @brief Run tool.
     #
     # @param kwargs schema properties.
     #
     # @return (success_bool, result_string)
     #
    def execute(self, **kwargs):
        action = kwargs.get("action")
        file_path = kwargs.get("file_path")
        content = kwargs.get("content", "")

        if not isinstance(file_path, str) or not file_path.endswith(".md"):
            return False, "Error: Target file must have a .md extension."
        # End-if

        if not os.path.isabs(file_path):
            file_path = os.path.join(self.workspace_dir, file_path)
        # End-if

        # SECURITY: interactive approval + fail-safe re-verify on resolved path.
        resolved, err = self._prepare_path(file_path, action_desc=f"{action.upper()} Markdown File")
        if err:
            return False, err
        # End-if

        try:
            if action == "read":
                if not os.path.exists(resolved):
                    return False, f"Error: File not found at {file_path}"
                # End-if
                with self._open_secure(resolved, "r") as f:
                    return True, f.read()
                # End-with

            elif action == "write":
                # Ensure directory exists
                os.makedirs(os.path.dirname(resolved), exist_ok=True)
                with self._open_secure(resolved, "w") as f:
                    f.write(content)
                # End-with
                return True, f"Successfully written to {file_path}"

            elif action == "append":
                if not os.path.exists(resolved):
                    return False, f"Error: Cannot append. File not found at {file_path}"
                # End-if
                with self._open_secure(resolved, "a") as f:
                    f.write("\n" + content)
                # End-with
                return True, f"Successfully appended to {file_path}"

            else:
                return False, f"Error: Unknown action '{action}'"
            # End-if

        except Exception as e:
            return False, f"Error performing {action} on {file_path}: {str(e)}"
        # End-try
    # End-def
# End-class

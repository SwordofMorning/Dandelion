##
 # @file src/tool/filesystem/edit_file_tool.py
 # @date 2026/08/13
 # 
 # @brief Edit File Tools.
 #

import os
from ..base_tool import BaseTool

##
 # @brief Edit File Class.
 #
class EditFileTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param workspace_dir Default to current directory if not explicitly provided.
     #
    def __init__(self, workspace_dir=None):
        super().__init__(workspace_dir)
    # End-def

    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "edit_file"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return (
            "Edit an existing file by replacing a specific exact text block with new text. "
            "This is much safer and faster than rewriting the entire file using write_file. "
            "You MUST provide the EXACT old text (including correct indentation and line breaks) "
            "as it currently appears in the file. It will replace ALL occurrences of the old text."
        )
    # End-def

    ##
     # @brief Return tool's schema.
     #
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
    # End-def

    ##
     # @brief Execute editing.
     #
     # @param kwargs schema properties: file_path, old_text, new_text.
     #
     # @return (success_bool, result_string)
     #
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

        # SECURITY: interactive approval + fail-safe re-verify on resolved path.
        resolved, err = self._prepare_path(file_path, action_desc=f"EDIT File at '{file_path}'")
        if err:
            return False, err
        # End-if

        if not os.path.exists(resolved):
            return False, f"Error: File not found at '{file_path}'."
            
        if not os.path.isfile(resolved):
            return False, f"Error: '{file_path}' is a directory, not a file."

        try:
            with self._open_secure(resolved, "r") as f:
                content = f.read()

            if old_text not in content:
                # Provide helpful fallback if possible (e.g. whitespace mismatch)
                return False, (
                    f"Error: The specified old_text was not found in '{file_path}'. "
                    f"Please ensure indentation, spaces, and line breaks match exactly. "
                    f"Consider using read_file to check the exact content."
                )
            # End-if

            # Re-verify before write-back: the read-close -> write-open gap is
            # the widest TOCTOU window in this tool; abort if the resolved
            # target changed since the initial check.
            resolved2, inside2 = self._resolved_within_workspace(file_path)
            if not inside2 or resolved2 != resolved:
                return False, (
                    f"CRITICAL SECURITY BLOCK: resolved path of '{file_path}' changed "
                    f"while editing. Aborting without write-back."
                )
            # End-if

            new_content = content.replace(old_text, new_text)

            with self._open_secure(resolved, "w") as f:
                f.write(new_content)

            return True, f"Successfully edited file '{file_path}'."
        # End-try

        except UnicodeDecodeError:
            return False, f"Error: '{file_path}' could not be decoded as UTF-8."
        except Exception as e:
            return False, f"Error editing file: {e}"
    # End-def execute
# End-class
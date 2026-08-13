##
 # @file src/tool/filesystem/write_file_tool.py
 # @date 2026/08/13
 # 
 # @brief Write File Tool.
 #

import os
from ..base_tool import BaseTool

# Maximum file size to write (5 MB)
_MAX_WRITE_BYTES = 5 * 1024 * 1024

##
 # @brief Write File Class.
 #
class WriteFileTool(BaseTool):
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
        return "write_file"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return (
            "Write content to a file. Creates the file if it does not exist, "
            "overwrites it if it does. Parent directories are created automatically. "
            "WARNING: To prevent API timeouts, do NOT write massive files (> 200 lines) in a single call. "
            "For large files, write the initial chunk first, then use 'edit_file' or alternative chunked methods."
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
                    "description": "Absolute or relative path to the target file."
                },
                "content": {
                    "type": "string",
                    "description": "The complete text content to write to the file."
                }
            },
            "required": ["file_path", "content"]
        }
    # End-def

    ##
     # @brief Execute file writing.
     #
     # @param kwargs schema properties: file_path, content.
     #
     # @return (success_bool, result_string)
     #
    def execute(self, **kwargs):
        file_path = kwargs.get("file_path", "")
        content = kwargs.get("content", "")

        if not file_path:
            return False, "Error: No file_path provided."

        if not os.path.isabs(file_path):
            file_path = os.path.join(self.workspace_dir, file_path)
        file_path = os.path.abspath(file_path)

        # SECURITY: interactive approval + fail-safe re-verify on resolved path.
        resolved, err = self._prepare_path(file_path, action_desc=f"WRITE File at '{file_path}'")
        if err:
            return False, err
        # End-if

        # Check content size
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > _MAX_WRITE_BYTES:
            return False, (
                f"Error: Content is too large ({content_bytes / 1024:.1f} KB). "
                f"Maximum allowed size is {_MAX_WRITE_BYTES / 1024:.0f} KB."
            )

        # Determine if this is a new file or overwrite
        file_existed = os.path.exists(resolved)

        try:
            # Ensure parent directories exist
            parent_dir = os.path.dirname(resolved)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            with self._open_secure(resolved, "w") as f:
                f.write(content)

            action_verb = "Updated" if file_existed else "Created"
            lines = content.count("\n") + (0 if content.endswith("\n") else 1)
            return True, (
                f"{action_verb} file: '{file_path}'\n"
                f"  Size: {content_bytes}B ({content_bytes / 1024:.1f} KB)\n"
                f"  Lines: {lines}"
            )
        # End-try

        except PermissionError:
            return False, f"Error: Permission denied when writing to '{file_path}'."
        except OSError as e:
            return False, f"Error writing file: {e}"
        except Exception as e:
            return False, f"Unexpected error writing file: {e}"
     # End-def execute
# End-class
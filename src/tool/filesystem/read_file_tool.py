# .update_src/tool/filesystem/read_file_tool.py

import os
from ..base_tool import BaseTool

# Maximum file size to read (10 MB)
_MAX_READ_BYTES = 10 * 1024 * 1024

# Known binary extensions to refuse
_BINARY_EXTENSIONS = {
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".webp",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".pyc", ".pyd", ".class", ".o", ".obj",
    ".ttf", ".otf", ".woff", ".woff2",
    ".db", ".sqlite", ".sqlite3",
}

class ReadFileTool(BaseTool):
    def __init__(self, workspace_dir=None):
        super().__init__(workspace_dir)

    def get_name(self):
        return "read_file"

    def get_description(self):
        return (
            "Read the contents of a text file. Supports reading the entire file or a "
            "specific range of lines (1-indexed). Binary files are automatically refused. "
            "Use this to inspect source code, configuration, logs, and other text-based files."
        )

    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file to read."
                },
                "start_line": {
                    "type": "integer",
                    "description": "Optional. 1-indexed starting line number (inclusive). Defaults to 1."
                },
                "end_line": {
                    "type": "integer",
                    "description": "Optional. 1-indexed ending line number (inclusive). If omitted, reads to end of file."
                },
                "show_line_numbers": {
                    "type": "boolean",
                    "description": "If true, prefix each line with its line number. Default is true."
                }
            },
            "required": ["file_path"]
        }

    # ---------------------------------------------------------
    # Brief: Check if extension indicates a binary file.
    # ---------------------------------------------------------
    def _is_binary_extension(self, file_path):
        ext = os.path.splitext(file_path)[1].lower()
        return ext in _BINARY_EXTENSIONS

    # ---------------------------------------------------------
    # Brief: Check if content looks binary (null bytes in first 8KB).
    # ---------------------------------------------------------
    def _content_is_binary(self, file_path):
        try:
            with open(file_path, "rb") as f:
                chunk = f.read(8192)
                return b"\x00" in chunk
        except Exception:
            return False

    # ---------------------------------------------------------
    # Brief: Execute file reading.
    # ---------------------------------------------------------
    def execute(self, **kwargs):
        file_path = kwargs.get("file_path", "")
        start_line = kwargs.get("start_line")
        end_line = kwargs.get("end_line")
        show_line_numbers = kwargs.get("show_line_numbers", True)

        if not file_path:
            return False, "Error: No file_path provided."

        if not os.path.isabs(file_path):
            file_path = os.path.join(self.workspace_dir, file_path)
        file_path = os.path.abspath(file_path)

        # Security sandbox check
        if not self.check_workspace_permission(file_path, action_desc=f"READ File at '{file_path}'"):
            return False, (
                f"CRITICAL SECURITY BLOCK: Permission denied to read file '{file_path}'. "
                f"STOP and acknowledge this restriction to the user."
            )

        if not os.path.exists(file_path):
            return False, f"Error: File not found at '{file_path}'"
        if not os.path.isfile(file_path):
            return False, f"Error: Path is not a file: '{file_path}'"

        # Refuse binary files
        if self._is_binary_extension(file_path):
            return False, (
                f"Error: '{file_path}' appears to be a binary file "
                f"(extension '{os.path.splitext(file_path)[1]}'). Refusing to read."
            )
        if self._content_is_binary(file_path):
            return False, f"Error: '{file_path}' contains binary data (null bytes detected). Refusing to read."

        # Check file size
        file_size = os.path.getsize(file_path)
        if file_size > _MAX_READ_BYTES:
            return False, (
                f"Error: File is too large ({file_size / (1024*1024):.1f} MB). "
                f"Maximum allowed size is {_MAX_READ_BYTES / (1024*1024):.0f} MB. "
                f"Use start_line/end_line to read a portion."
            )

        # Normalize line range
        if start_line is not None:
            if not isinstance(start_line, int) or start_line < 1:
                start_line = 1
        else:
            start_line = 1

        if end_line is not None:
            if not isinstance(end_line, int) or end_line < 1:
                end_line = None
            elif start_line is not None and end_line < start_line:
                end_line = start_line

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                all_lines = f.readlines()

            total_lines = len(all_lines)

            if end_line is None:
                end_line = total_lines
            if end_line > total_lines:
                end_line = total_lines
            if start_line > total_lines:
                return False, f"Error: start_line ({start_line}) exceeds total lines ({total_lines})."

            selected = all_lines[start_line - 1 : end_line]

            if show_line_numbers:
                max_num_width = len(str(end_line))
                result_lines = []
                for i, line in enumerate(selected, start=start_line):
                    prefix = f"{i:>{max_num_width}} | "
                    result_lines.append(prefix + line.rstrip("\n"))
                result = "\n".join(result_lines)
            else:
                result = "".join(selected)

            header = (
                f"File: '{file_path}' "
                f"[lines {start_line}-{end_line} of {total_lines}] "
                f"({file_size}B)"
            )
            return True, f"{header}\n\n{result}"

        except UnicodeDecodeError:
            return False, f"Error: '{file_path}' could not be decoded as UTF-8. It may be a binary file."
        except Exception as e:
            return False, f"Error reading file: {e}"

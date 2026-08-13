##
 # @file src/tool/filesystem/grep_search_tool.py
 # @date 2026/08/13
 # 
 # @brief Grep Search Tools.
 #

import os
import re
import fnmatch
from ..base_tool import BaseTool

# Maximum total lines to scan across all files (performance guard)
_MAX_SCAN_LINES = 100000

# Directories always skipped during recursive search
_SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".env", ".log", ".idea", ".vscode", "venv", ".venv"}

# Extensions considered "text" for searching
_TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".xml", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".md", ".rst", ".txt", ".tex", ".csv", ".tsv",
    ".sh", ".bash", ".zsh", ".ps1", ".psm1", ".bat", ".cmd",
    ".sql", ".r", ".m", ".jl", ".lua", ".vim", ".vimrc",
    ".dockerfile", ".makefile", ".cmake", ".gradle",
    ".env", ".gitignore", ".dockerignore",
}

# Extensions explicitly excluded (binary / large)
_SKIP_EXTENSIONS = {
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".pyc", ".pyd",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".ttf", ".otf", ".woff", ".woff2",
    ".db", ".sqlite", ".sqlite3",
    ".o", ".obj", ".a", ".lib", ".class",
}

##
 # @brief Grep Search Class.
 #
class GrepSearchTool(BaseTool):
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
        return "grep_search"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return (
            "Search for a text pattern (regular expression) across files in a directory. "
            "Returns matching lines with file path and line number. "
            "Recursively searches subdirectories, automatically skipping binary files "
            "and common noise directories (__pycache__, node_modules, .git, etc.). "
            "Use this to find function definitions, variable usages, configuration keys, "
            "or any text pattern in a codebase."
        )
    # End-def

    ##
     # @brief Return tool's schema.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The text or regular expression to search for. "
                                   "Examples: 'def main', 'import os', 'TODO', 'raise ValueError'."
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in. Defaults to workspace root."
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Optional glob pattern to filter files (e.g., '*.py', '*.md', 'test_*'). "
                                   "If omitted, searches all recognized text files."
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "If true, match case exactly. Default is false (case-insensitive)."
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of matching lines to return (1-200). Default is 50."
                }
            },
            "required": ["pattern"]
        }
    # End-def

    ##
     # @brief Determine if a file should be searched based on its extension.
     #
     # @param file_path Full path of the candidate file.
     # @param file_pattern Optional glob pattern; when set, only basenames
     # matching it are searched.
     #
     # @return True if the file should be searched, False otherwise.
     #
    def _should_search(self, file_path, file_pattern):
        ext = os.path.splitext(file_path)[1].lower()
        basename = os.path.basename(file_path).lower()

        # Explicit skip
        if ext in _SKIP_EXTENSIONS:
            return False

        # File-pattern filter (only applied if specified)
        if file_pattern:
            return fnmatch.fnmatch(basename, file_pattern)

        # Otherwise, search known text extensions + extensionless files
        if ext in _TEXT_EXTENSIONS:
            return True
        if ext == "":
            # Extensionless files: check if they're likely text
            return True

        return False
    # End-def

    ## 
     # @brief Walk directory tree and collect searchable file paths.
     #
     # @param root_path Directory to walk.
     # @param file_pattern Optional glob pattern forwarded to _should_search().
     #
     # @return List of searchable file paths (absolute).
     #
    def _collect_files(self, root_path, file_pattern):
        files = []
        for dirpath, dirnames, filenames in os.walk(root_path):
            # Prune skip directories in-place
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]

            for fname in filenames:
                full_path = os.path.join(dirpath, fname)
                if self._should_search(full_path, file_pattern):
                    files.append(full_path)
        return files
    # End-def

    ## 
     # @brief Execute grep search.
     #
     # @param kwargs schema properties: pattern, path, file_pattern,
     # case_sensitive, max_results.
     #
     # @return (success_bool, result_string)
     #
    def execute(self, **kwargs):
        pattern = kwargs.get("pattern", "")
        path = kwargs.get("path") or self.workspace_dir
        file_pattern = kwargs.get("file_pattern") or None
        case_sensitive = kwargs.get("case_sensitive", False)
        max_results = kwargs.get("max_results", 50)

        if not pattern:
            return False, "Error: No search pattern provided."

        # Clamp max_results
        if not isinstance(max_results, int) or max_results < 1:
            max_results = 1
        if max_results > 200:
            max_results = 200

        # Resolve search path
        if not os.path.isabs(path):
            path = os.path.join(self.workspace_dir, path)
        path = os.path.abspath(path)

        # Security sandbox check
        if not self.check_workspace_permission(path, action_desc=f"GREP Search in '{path}'"):
            return False, (
                f"CRITICAL SECURITY BLOCK: Permission denied to search in '{path}'. "
                f"STOP and acknowledge this restriction to the user."
            )

        if not os.path.exists(path):
            return False, f"Error: Directory not found at '{path}'"
        if not os.path.isdir(path):
            return False, f"Error: Path is not a directory: '{path}'"

        # Compile regex
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            regex = re.compile(pattern, flags)
        except re.error as e:
            return False, f"Error: Invalid regular expression pattern: {e}"

        # Collect files
        try:
            target_files = self._collect_files(path, file_pattern)
        except PermissionError:
            return False, f"Error: Permission denied while scanning directories in '{path}'."
        except Exception as e:
            return False, f"Error scanning directories: {e}"

        if not target_files:
            return False, (
                f"No searchable files found in '{path}'"
                + (f" matching '{file_pattern}'" if file_pattern else "")
                + "."
            )
        # End-if

        # Search
        results = []
        total_lines_scanned = 0
        truncated = False

        for file_path in target_files:
            if len(results) >= max_results or total_lines_scanned >= _MAX_SCAN_LINES:
                truncated = True
                break

            try:
                # Quick binary check on first chunk
                with open(file_path, "rb") as f:
                    head = f.read(4096)
                    if b"\x00" in head:
                        continue

                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    for line_no, line in enumerate(f, 1):
                        total_lines_scanned += 1

                        if len(results) >= max_results or total_lines_scanned > _MAX_SCAN_LINES:
                            truncated = True
                            break
                        # End-if

                        if regex.search(line):
                            # Make path relative to workspace for cleaner output
                            try:
                                display_path = os.path.relpath(file_path, self.workspace_dir)
                            except ValueError:
                                display_path = file_path

                            results.append({
                                "file": display_path,
                                "line": line_no,
                                "content": line.rstrip("\n")[:500]
                            })
                        # End-if
                    # End-for
                # End-with
            # End-try

            except (UnicodeDecodeError, PermissionError, OSError):
                continue
        # End-for

        # Build output
        if not results:
            flags_str = "" if case_sensitive else " (case-insensitive)"
            return True, (
                f"No matches found for pattern '{pattern}'{flags_str} "
                f"across {len(target_files)} files in '{path}'."
            )
        # End-if

        output_lines = [
            f"Search results for '{pattern}'"
            + ("" if case_sensitive else " (case-insensitive)")
            + f" in '{path}':",
            f"  Files searched: {len(target_files)}",
            f"  Matches found: {len(results)}",
            "",
        ]

        for r in results:
            output_lines.append(f"{r['file']}:{r['line']}: {r['content']}")

        if truncated:
            output_lines.append(f"\n[Results truncated at {max_results} matches or scan limit.]")

        return True, "\n".join(output_lines)
    # End-def execute
# End-class
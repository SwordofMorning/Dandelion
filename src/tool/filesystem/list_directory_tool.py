# .update_src/tool/filesystem/list_directory_tool.py

import os
import fnmatch
from ..base_tool import BaseTool

class ListDirectoryTool(BaseTool):
    def __init__(self, workspace_dir=None):
        super().__init__(workspace_dir)

    def get_name(self):
        return "list_directory"

    def get_description(self):
        return (
            "List the contents of a directory. "
            "Supports recursive listing up to a controlled depth and optional glob filtering. "
            "Hidden directories (.git, __pycache__, node_modules, .env) are automatically excluded. "
            "Use this to explore project structure before reading or editing files."
        )

    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the directory. Defaults to workspace root if omitted."
                },
                "recursive": {
                    "type": "boolean",
                    "description": "If true, recursively list subdirectories. Default is false."
                },
                "depth": {
                    "type": "integer",
                    "description": "Maximum recursion depth (1-5). Only used when recursive is true. Default is 3."
                },
                "filter_pattern": {
                    "type": "string",
                    "description": "Optional glob pattern to filter entries (e.g., '*.py', 'test_*', '*.md'). Applies to file names only."
                }
            },
            "required": []
        }

    # ---------------------------------------------------------
    # Internal: always-skipped directory names
    # ---------------------------------------------------------
    _SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".env", ".log"}

    # ---------------------------------------------------------
    # Brief: Recursively build a tree representation string.
    # ---------------------------------------------------------
    def _build_tree(self, root_path, max_depth, filter_pattern, current_depth):
        if current_depth > max_depth:
            return ""

        try:
            entries = sorted(os.scandir(root_path), key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            return "    " * (current_depth - 1) + "[Permission Denied]\n"
        except OSError as e:
            return "    " * (current_depth - 1) + f"[Error: {e}]\n"

        lines = []
        for entry in entries:
            name = entry.name

            if name.startswith("."):
                continue
            if name in self._SKIP_DIRS:
                continue

            if filter_pattern and not entry.is_dir():
                if not fnmatch.fnmatch(name, filter_pattern):
                    continue

            indent = "    " * (current_depth - 1)

            if entry.is_dir():
                lines.append(f"{indent}{name}/")
                sub = self._build_tree(entry.path, max_depth, filter_pattern, current_depth + 1)
                if sub:
                    lines.append(sub)
            else:
                try:
                    size = entry.stat().st_size
                    if size < 1024:
                        size_str = f"{size}B"
                    elif size < 1024 * 1024:
                        size_str = f"{size / 1024:.1f}KB"
                    else:
                        size_str = f"{size / (1024 * 1024):.1f}MB"
                    lines.append(f"{indent}{name}  ({size_str})")
                except OSError:
                    lines.append(f"{indent}{name}")

        return "\n".join(lines) + ("\n" if lines else "")

    # ---------------------------------------------------------
    # Brief: Execute directory listing.
    # ---------------------------------------------------------
    def execute(self, **kwargs):
        path = kwargs.get("path") or self.workspace_dir
        recursive = kwargs.get("recursive", False)
        depth = kwargs.get("depth", 3)
        filter_pattern = kwargs.get("filter_pattern") or None

        if not isinstance(depth, int) or depth < 1:
            depth = 1
        if depth > 5:
            depth = 5

        if not os.path.isabs(path):
            path = os.path.join(self.workspace_dir, path)
        path = os.path.abspath(path)

        if not self.check_workspace_permission(path, action_desc=f"LIST Directory at '{path}'"):
            return False, (
                f"CRITICAL SECURITY BLOCK: Permission denied to list directory '{path}'. "
                f"STOP and acknowledge this restriction to the user."
            )

        if not os.path.exists(path):
            return False, f"Error: Directory not found at '{path}'"
        if not os.path.isdir(path):
            return False, f"Error: Path is not a directory: '{path}'"

        try:
            if recursive:
                tree = self._build_tree(path, depth, filter_pattern, 1)
                header = f"Directory tree of '{path}' (depth={depth})"
                if filter_pattern:
                    header += f" [filter: {filter_pattern}]"
                result = f"{header}:\n\n{tree.rstrip()}" if tree else f"{header}:\n\n(no matching entries)"
            else:
                entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
                lines = []
                for entry in entries:
                    name = entry.name
                    if name.startswith("."):
                        continue
                    if name in self._SKIP_DIRS:
                        continue
                    if filter_pattern and not entry.is_dir():
                        if not fnmatch.fnmatch(name, filter_pattern):
                            continue
                    marker = "/" if entry.is_dir() else ""
                    lines.append(f"  {name}{marker}")
                result = f"Contents of '{path}':\n" + ("\n".join(lines) if lines else "  (empty)")

            return True, result
        except Exception as e:
            return False, f"Error listing directory: {e}"

##
 # @file packages/dandelion-core/src/dandelion_core/tool/base_tool.py
 # @date 2026/11/28
 #
 # @brief Abstract base tool interface and workspace access helpers.
 # Defines BaseTool(ABC) enforcing get_name, get_description, get_schema, and
 # execute on concrete tools, alongside shared output forwarding, workspace
 # sandbox checks, and symlink-safe file-opening helpers.
 #

from abc import ABC, abstractmethod
import os


##
 # @brief Abstract Base Tool Class.
 #
class BaseTool(ABC):
    ##
     # @brief Constructor.
     #
     # @param workspace_dir Default to current directory if not explicitly provided.
     #
     # @return None.
     #
    def __init__(self, workspace_dir=None):
        self.workspace_dir = workspace_dir if workspace_dir else os.getcwd()
    # End-def

    ##
     # @brief Emit a tool message through the host output adapter.
     #
     # @param text Message text.
     #
     # @return None.
     #
    def _emit(self, text):
        runtime = getattr(self, "runtime", None)
        if runtime is not None:
            runtime.emit(text)
        else:
            print(text)
        # End-if
    # End-def

    ##
     # @brief Return the registered tool name.
     #
     # @return Tool identifier string.
     #
    @abstractmethod
    def get_name(self):
        pass
    # End-def

    ##
     # @brief Return the model-facing tool description.
     #
     # @return Human-readable description string.
     #
    @abstractmethod
    def get_description(self):
        pass
    # End-def

    ##
     # @brief Return the JSON input schema dictionary for this tool.
     #
     # @return Schema dictionary containing type, properties, and required keys.
     #
    @abstractmethod
    def get_schema(self):
        pass
    # End-def

    ##
     # @brief Sandbox protect: verify whether target_path is inside workspace_dir.
     #
     # @param target_path File path the tool wants to access.
     # @param action_desc Human-readable action description for the approval prompt.
     #
     # @note When target_path escapes workspace_dir under sandbox=True, delegates
     # to runtime.approve() for host-controlled authorization.
     #
     # @return True if allowed (or inside workspace), False if denied.
     #
    def check_workspace_permission(self, target_path, action_desc="File Access"):
        runtime = getattr(self, "runtime", None)
        if runtime is not None and not getattr(runtime, "sandbox", True):
            return True
        # End-if

        if not self.workspace_dir:
            return True
        # End-if

        abs_target, is_inside = self._resolved_within_workspace(target_path)
        abs_workspace = os.path.realpath(self.workspace_dir)

        if is_inside:
            return True
        # End-if

        if runtime is not None:
            tool_name = self.get_name()
            desc = action_desc if action_desc.startswith("[") else f"[{tool_name}] {action_desc}"
            return bool(runtime.approve(desc, abs_target, abs_workspace))
        # End-if

        return False
    # End-def

    ##
     # @brief Resolve symlinks and test workspace containment without prompting.
     #
     # @param target_path File path to test.
     #
     # @return (resolved_path, is_inside) tuple.
     #
    def _resolved_within_workspace(self, target_path):
        if not self.workspace_dir:
            return os.path.realpath(target_path), True
        # End-if
        abs_workspace = os.path.realpath(self.workspace_dir)
        abs_target = os.path.realpath(target_path)
        try:
            # os.path.commonpath prevents prefix trickery like /work vs /workspace.
            return abs_target, os.path.commonpath([abs_workspace, abs_target]) == abs_workspace
        except ValueError:
            # On Windows, ValueError is raised when paths are on different drives.
            return abs_target, False
        # End-try
    # End-def

    ##
     # @brief Prepare a target path with sandbox boundary check and optional approval.
     #
     # @param file_path File path from the tool schema.
     # @param action_desc Human-readable action description for the prompt.
     #
     # @return (resolved_path, None) on success, or (None, error_message).
     #
    def _prepare_path(self, file_path, action_desc):
        runtime = getattr(self, "runtime", None)
        if runtime is not None and not getattr(runtime, "sandbox", True):
            return os.path.realpath(file_path), None
        # End-if

        resolved, inside = self._resolved_within_workspace(file_path)
        if inside:
            return resolved, None
        # End-if

        if not self.check_workspace_permission(resolved, action_desc=action_desc):
            return None, (
                f"CRITICAL SECURITY BLOCK: The human user explicitly DENIED permission "
                f"to access '{file_path}'. STOP IMMEDIATELY. "
                f"Do not attempt any workarounds. Acknowledge this restriction to the user."
            )
        # End-if
        return resolved, None
    # End-def

    ##
     # @brief Open a file with the final component protected against symlink swaps.
     #
     # @param path Resolved (realpath) file path that already passed permission checks.
     # @param mode 'r', 'w' or 'a', optionally suffixed with 'b' for binary mode.
     # @param encoding Text encoding (default utf-8; ignored in binary mode).
     # @param errors Text error handling policy passed to os.fdopen.
     #
     # @return Opened file object on success; raises OSError otherwise.
     #
    def _open_secure(self, path, mode, encoding="utf-8", errors=None):
        binary = "b" in mode
        flags = {
            "r": os.O_RDONLY,
            "w": os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            "a": os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        }[mode.replace("b", "")]
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags | nofollow)
        if binary:
            return os.fdopen(fd, mode)
        # End-if
        return os.fdopen(fd, mode, encoding=encoding, errors=errors)
    # End-def

    ##
     # @brief Execute the tool with validated keyword arguments.
     #
     # @param kwargs Tool input parameters matching get_schema().
     #
     # @return (success_bool, result_string_or_payload) tuple.
     #
    @abstractmethod
    def execute(self, **kwargs):
        pass
    # End-def
# End-class

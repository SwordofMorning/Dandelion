##
 # @file src/tool/base_tool.py
 # @date 2026/08/13
 # 
 # @brief Tool Class.
 # Provides abstract/virtual base tool class.
 #

import os

##
 # @brief Virtual Base Tool Class.
 #
class BaseTool:
    ##
     # @brief Constructor.
     #
     # @param workspace_dir Default to current directory if not explicitly provided.
     #
    def __init__(self, workspace_dir=None):
        self.workspace_dir = workspace_dir if workspace_dir else os.getcwd()
    # End-def

    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "base_tool"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return "Base tool interface."
    # End-def

    ##
     # @brief Return tool's schema.
     # like:
     # {
     #   type,
     #   properties(parameters),
     #   required
     # }
     #
    def get_schema(self):
        return {}
    # End-def

    ##
     # @brief Sandbox protect.
     #
     # @param target_path File path the tool wants to access.
     # @param action_desc Human-readable action description for the approval prompt.
     #
     # @note Check if target_path is strictly within workspace_dir.
     # If it escapes the workspace, prompt user for manual y/N approval.
     #
     # @return True if allowed (or inside workspace), False if denied by user.
     #
    def check_workspace_permission(self, target_path, action_desc="File Access"):
        # Relative path.
        if not self.workspace_dir:
            return True
        # End-if

        abs_target, is_inside = self._resolved_within_workspace(target_path)
        abs_workspace = os.path.realpath(self.workspace_dir)

        if is_inside:
            return True
        # End-if

        # Trigger Security Prompt (Outside Workspace).
        print(f"\n\033[33m[!] SECURITY ALERT: Tool '{self.get_name()}' is attempting to escape workspace.\033[0m")
        print(f"    Action   : {action_desc}")
        print(f"    Target   : {abs_target}")
        print(f"    Workspace: {abs_workspace}")

        # User input.
        while True:
            ans = input("    Allow this operation? [y/N]: ").strip().lower()
            if ans in ['y', 'yes']:
                print("    [+] User approved outside access.")
                return True
            elif ans in ['n', 'no', '']:
                print("    [-] User denied access.")
                return False
            else:
                print("    Please enter y or n.")
        # End-while
    # End-def check_workspace_permission

    ##
     # @brief Resolve symlinks and test workspace containment (no prompt).
     #
     # @param target_path File path to test.
     #
     # @note os.path.realpath() resolves symlinks so `ws/link -> /etc` cannot
     # bypass the boundary; for NEW files the parent chain is resolved while
     # the final filename is preserved.
     #
     # @return (resolved_path, is_inside) where resolved_path is the realpath
     # of target_path and is_inside is True only when it stays within
     # workspace_dir.
     #
    def _resolved_within_workspace(self, target_path):
        if not self.workspace_dir:
            return os.path.realpath(target_path), True
        abs_workspace = os.path.realpath(self.workspace_dir)
        abs_target = os.path.realpath(target_path)
        try:
            # os.path.commonpath prevents trickery like `/work` vs `/workspace`.
            return abs_target, os.path.commonpath([abs_workspace, abs_target]) == abs_workspace
        except ValueError:
            # In Windows, ValueError is raised if paths are on different drives.
            return abs_target, False
    # End-def

    ##
     # @brief Prepare a target path: interactive approval + fail-safe re-verify.
     #
     # @param file_path File path from the tool schema.
     # @param action_desc Human-readable action description for the prompt.
     #
     # @note Runs the interactive containment check first; on approval,
     # re-verifies the RESOLVED path without a second prompt. If the path
     # changed between check and resolution (TOCTOU), it fails safe.
     #
     # @return (resolved_path, None) on success, or (None, error_message).
     #
    def _prepare_path(self, file_path, action_desc):
        if not self.check_workspace_permission(file_path, action_desc=action_desc):
            return None, (
                f"CRITICAL SECURITY BLOCK: The human user explicitly DENIED permission "
                f"to access '{file_path}'. STOP IMMEDIATELY. "
                f"Do not attempt any workarounds. Acknowledge this restriction to the user."
            )
        # End-if
        resolved, inside = self._resolved_within_workspace(file_path)
        if not inside:
            return None, (
                f"CRITICAL SECURITY BLOCK: resolved path of '{file_path}' escapes "
                f"the workspace. Acknowledge this restriction to the user."
            )
        # End-if
        return resolved, None
    # End-def

    ##
     # @brief Open a file with the final component protected against symlink swaps.
     #
     # @param path Resolved (realpath) file path that already passed the
     # workspace containment check.
     # @param mode 'r', 'w' or 'a' (same semantics as builtin open); prefix
     # with 'b' for binary (e.g. 'rb').
     # @param encoding Text encoding (default utf-8; None for binary).
     # @param errors Text error handling policy (e.g. 'replace'), passed to
     # os.fdopen.
     #
     # @note O_NOFOLLOW (POSIX) refuses the open with ELOOP when the final
     # component is a symlink, so a swap after the permission check cannot
     # redirect the write outside the workspace. On Windows O_NOFOLLOW is
     # unavailable (getattr -> 0) and the resolved path is opened instead.
     #
     # @return File object on success; raises OSError otherwise.
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
        return os.fdopen(fd, mode, encoding=encoding, errors=errors)
    # End-def

    ##
     # @brief Run tool.
     #
     # @param kwargs schema properties.
     #
     # @return (success_bool, result_string)
     #
    def execute(self, **kwargs):
        return False, "Not implemented."
    # End-def
# End-class
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

        # Absolute path convert.
        abs_workspace = os.path.abspath(self.workspace_dir)
        abs_target = os.path.abspath(target_path)

        # Check if target is inside workspace securely (abs path).
        is_inside = False
        try:
            # os.path.commonpath prevents trickery like `/work` vs `/workspace`.
            common = os.path.commonpath([abs_workspace, abs_target])
            if common == abs_workspace:
                is_inside = True
        except ValueError:
            # In Windows, ValueError is raised if paths are on different drives.
            is_inside = False
        # End-try

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
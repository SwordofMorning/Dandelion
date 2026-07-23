# src/tool/base_tool.py
import os

class BaseTool:
    def __init__(self, workspace_dir=None):
        # Default to current directory if not explicitly provided
        self.workspace_dir = workspace_dir if workspace_dir else os.getcwd()

    def get_name(self):
        return "base_tool"
        
    def get_description(self):
        return "Base tool interface."
        
    def get_schema(self):
        return {}

    def check_workspace_permission(self, target_path, action_desc="File Access"):
        """
        Check if target_path is strictly within workspace_dir.
        If it escapes the workspace, prompt user for manual y/N approval.
        Returns:
            True if allowed (or inside workspace)
            False if denied by user
        """
        if not self.workspace_dir:
            return True
            
        abs_workspace = os.path.abspath(self.workspace_dir)
        abs_target = os.path.abspath(target_path)
        
        # Check if target is inside workspace securely
        is_inside = False
        try:
            # os.path.commonpath prevents trickery like /work vs /workspace
            common = os.path.commonpath([abs_workspace, abs_target])
            if common == abs_workspace:
                is_inside = True
        except ValueError:
            # In Windows, ValueError is raised if paths are on different drives
            is_inside = False
            
        if is_inside:
            return True
            
        # Trigger Security Prompt (Outside Workspace)
        print(f"\n\033[33m[!] SECURITY ALERT: Tool '{self.get_name()}' is attempting to escape workspace.\033[0m")
        print(f"    Action   : {action_desc}")
        print(f"    Target   : {abs_target}")
        print(f"    Workspace: {abs_workspace}")
        
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

    def execute(self, **kwargs):
        """
        Returns (success_bool, result_string)
        """
        return False, "Not implemented."
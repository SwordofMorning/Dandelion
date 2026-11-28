##
 # @file packages/dandelion-core/src/dandelion_core/tool/shell/bash_tool.py
 # @date 2026/11/28
 #
 # @brief Workspace-bound local shell command tool.
 # Uses quote-aware ShellCommandGuard validation before starting a shell
 # subprocess, enforces workspace sandbox approval on external paths, and
 # bounds execution time and captured output for the Agent.
 #
 # @note Shell selection: Bash is preferred when available (also on Windows,
 # e.g. Git Bash); PowerShell is used only as the fallback. The shell
 # environment is described in sysprompt so the LLM knows which one runs.
 #

import os
import shutil
import subprocess

from ..base_tool import BaseTool
from .shell_guard import ShellCommandGuard


##
 # @brief Shell Tool Class.
 #
class BashTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param workspace_dir Default to current directory if not explicitly provided.
     #
     # @return None.
     #
    def __init__(self, workspace_dir=None):
        super().__init__(workspace_dir)
        # Pin the command and path-check root without changing process CWD.
        self.workspace_dir = os.path.realpath(os.path.abspath(self.workspace_dir))
        self.has_bash = shutil.which("bash") is not None
        self.has_pwsh = shutil.which("powershell") is not None
    # End-def

    ##
     # @brief Return tool's name.
     #
     # @return Tool identifier string.
     #
    def get_name(self):
        return "bash"
    # End-def

    ##
     # @brief Return tool's description.
     #
     # @return Human-readable tool description.
     #
    def get_description(self):
        return "Run a shell command. Strict security rules apply."
    # End-def

    ##
     # @brief Return tool's schema.
     #
     # @return JSON Schema dictionary.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        }
    # End-def

    ##
     # @brief Quote-aware sandbox and command safety check.
     #
     # @param cmd Shell command from schema.
     #
     # @return (True, "") when permitted, or (False, reason) when blocked.
     #
    def _is_safe_command(self, cmd):
        runtime = getattr(self, "runtime", None)
        if runtime is not None and not getattr(runtime, "sandbox", True):
            return True, ""
        # End-if

        # 1. Quote-aware command segmentation & sensitive path boundary checks.
        is_safe, err_msg, candidate_paths = ShellCommandGuard.check_local_command(cmd)
        if not is_safe:
            return False, err_msg
        # End-if

        # 2. Workspace sandbox containment check on recognized path arguments.
        for token in candidate_paths:
            target = token if os.path.isabs(token) else os.path.join(self.workspace_dir, token)
            if not self.check_workspace_permission(target, action_desc=f"Shell Command on '{token}'"):
                deny_msg = (
                    f"CRITICAL SECURITY BLOCK: The human user explicitly DENIED access to '{token}'. "
                    f"YOU MUST STOP TRYING TO ACCESS OR EXPLORE THIS TARGET. "
                    f"Do not use dynamic shell evaluation like (Get-Item).Parent to bypass this. "
                    f"Apologize to the user and ask for new instructions."
                )
                return False, deny_msg
            # End-if
        # End-for

        return True, ""
    # End-def

    ##
     # @brief Execute shell command.
     #
     # @param kwargs Schema properties containing 'command'.
     #
     # @return (success_bool, result_string) tuple.
     #
    def execute(self, **kwargs):
        cmd = kwargs.get("command", "")
        if not cmd:
            return False, "Error: No command provided."
        # End-if

        is_safe, error_msg = self._is_safe_command(cmd)
        if not is_safe:
            return False, error_msg
        # End-if

        exec_array = []
        if self.has_bash:
            exec_array = ["bash", "-c", cmd]
        elif self.has_pwsh:
            exec_array = ["powershell", "-Command", cmd]
        else:
            return False, "Error: Neither bash nor powershell found."
        # End-if

        try:
            r = subprocess.run(
                exec_array,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=self.workspace_dir,
            )
            out = (r.stdout + r.stderr).strip()
            out_str = out[:50000] if out else "(no output)"
            if r.returncode == 0:
                return True, out_str
            else:
                return False, f"[Command Failed with code {r.returncode}]\n{out_str}"
            # End-if
        except subprocess.TimeoutExpired:
            return False, "Error: Timeout (120s)"
        except Exception as e:
            return False, f"Error: {str(e)}"
        # End-try
    # End-def
# End-class

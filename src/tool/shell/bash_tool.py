##
 # @file src/tool/shell/bash_tool.py
 # @date 2026/08/13
 # 
 # @brief Direct Shell Call.
 #
 # @note Environment specified in sysprompt, which allow LLm to know using PS on Windows.
 #

import subprocess
import shutil
import re
import shlex
from ..base_tool import BaseTool

##
 # @brief Shell Tool Class.
 #
class BashTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param workspace_dir Default to current directory if not explicitly provided.
     #
    def __init__(self, workspace_dir=None):
        super().__init__(workspace_dir)
        self.has_bash = shutil.which("bash") is not None
        self.has_pwsh = shutil.which("powershell") is not None
        
        # Blacklist of forbidden patterns for security.
        self.forbidden_patterns = [
            "../",
            "..\\",
            "/etc",
            "\\etc",
            ".env",
            "~/"
        ]
    # End-def

    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "bash"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return "Run a shell command. Strict security rules apply."
    # End-def

    ##
     # @brief Return tool's schema.
     #
    def get_schema(self):
        return {
            "type": "object", 
            "properties": {"command": {"type": "string"}}, 
            "required": ["command"]
        }
    # End-def

    ##
     # @brief Sandbox and command self check.
     #
     # @param cmd Shell command from schema.
     #
     # @return (True, "") | (False, "Why refuse this command").
     #
    def _is_safe_command(self, cmd):
        cmd_lower = cmd.lower()

        # 1. Fast Blacklist Check
        for pattern in self.forbidden_patterns:
            if pattern in cmd_lower:
                # Cognitive Interrupt
                err_msg = (
                    f"CRITICAL SECURITY BLOCK: Pattern '{pattern}' is forbidden. "
                    f"STOP IMMEDIATELY. Do not attempt to use workarounds, PowerShell expressions, "
                    f"or alternative paths to bypass this. Acknowledge the restriction to the user."
                )
                return False, err_msg
        # End-for

        # 2. Heuristic Path Extraction & Workspace Sandbox Check
        try:
            tokens = shlex.split(cmd, posix=False)
        except ValueError:
            tokens = cmd.split()
        # End-try

        for token in tokens:
            token = token.strip('"\'')
            is_path = False

            if re.match(r'^[a-zA-Z]:', token): 
                is_path = True
            elif '\\' in token or '/' in token:
                if re.match(r'^/[a-zA-Z?]$', token):
                    pass
                else:
                    is_path = True
            elif token == '..':
                is_path = True
            # End-if

            if is_path:
                if not self.check_workspace_permission(token, action_desc=f"Shell Command on '{token}'"):
                    # Reject LLM's Action
                    err_msg = (
                        f"CRITICAL SECURITY BLOCK: The human user explicitly DENIED access to '{token}'. "
                        f"YOU MUST STOP TRYING TO ACCESS OR EXPLORE THIS TARGET. "
                        f"Do not use dynamic shell evaluation like (Get-Item).Parent to bypass this. "
                        f"Apologize to the user and ask for new instructions."
                    )
                    return False, err_msg
                # End-if
            # End-if
        # End-for
   
        return True, ""
    # End-def _is_safe_command

    ##
     # @brief Execute shell command.
     #
     # @param kwargs schema properties.
     # 
     # @return (success_bool, result_string)
     #
    def execute(self, **kwargs):
        cmd = kwargs.get("command", "")
        if not cmd:
            return False, "Error: No command provided."

        # Security Check (Early Return style)
        is_safe, error_msg = self._is_safe_command(cmd)
        if not is_safe:
            return False, error_msg

        exec_array = []
        if self.has_bash:
            exec_array = ["bash", "-c", cmd]
        elif self.has_pwsh:
            exec_array = ["powershell", "-Command", cmd]
        else:
            return False, "Error: Neither bash nor powershell found."

        try:
            r = subprocess.run(exec_array, capture_output=True, text=True, timeout=120)
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
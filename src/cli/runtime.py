##
 # @file src/cli/runtime.py
 # @brief Adapt the terminal to the host-independent core runtime callbacks.
 #

from src.core.runtime import Runtime
from src.core.tool.base_tool import _APPROVAL_LOCK
from .interrupt import is_requested, request


##
 # @brief Serialize terminal approval prompts and support turn cancellation.
 #
 # @param action_desc Human-readable description of the requested operation.
 # @param abs_target Resolved absolute target path.
 # @param abs_workspace Resolved absolute workspace root.
 #
 # @return True for approval; False for denial or turn cancellation.
 #
def _approve(action_desc, abs_target, abs_workspace):
    with _APPROVAL_LOCK:
        print("\n\033[33m[!] SECURITY ALERT: Tool is attempting to escape workspace.\033[0m")
        print(f"    Action   : {action_desc}")
        print(f"    Target   : {abs_target}")
        print(f"    Workspace: {abs_workspace}")
        while True:
            answer = input("    Allow this operation? [y/N/C]: ").strip().lower()
            if answer in ("y", "yes"):
                print("    [+] User approved outside access.")
                return True
            # End-if
            if answer in ("c", "cancel"):
                request("tool_cancel")
                print("    [!] Canceled: this call is denied and the turn will stop and roll back.")
                return False
            # End-if
            if answer in ("n", "no", ""):
                print("    [-] User denied access.")
                return False
            # End-if
            print("    Please enter y, n or c.")
        # End-while
    # End-with
# End-def


##
 # @brief Build callbacks for terminal output, approvals and process stop requests.
 #
 # @return Runtime connected to the terminal and process stop flag.
 #
def cli_runtime():
    return Runtime(cancelled=is_requested, approve=_approve, echo=True)
# End-def

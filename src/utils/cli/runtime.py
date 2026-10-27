"""Terminal adapters for the host-independent agent runtime."""

from src.runtime import Runtime
from src.tool.base_tool import _APPROVAL_LOCK
from .interrupt import is_requested, request


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
            if answer in ("c", "cancel"):
                request("tool_cancel")
                print("    [!] Canceled: this call is denied and the turn will stop and roll back.")
                return False
            if answer in ("n", "no", ""):
                print("    [-] User denied access.")
                return False
            print("    Please enter y, n or c.")


def cli_runtime():
    return Runtime(cancelled=is_requested, approve=_approve, echo=True)

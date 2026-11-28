##
 # @file src/cli/runtime.py
 # @date 2026/10/27
 #
 # @brief Terminal runtime implementation inheriting Core BaseRuntime.
 # Connects Core output, turn interruption, sandbox enforcement and workspace
 # approvals to the terminal with colored status prefixes.
 #

import threading

from dandelion_core.agent.interrupt import TurnInterrupt
from dandelion_core.runtime import BaseRuntime

from .printer import CLIPrinter

_PRINTER = CLIPrinter()
_APPROVAL_LOCK = threading.Lock()
# Single process-wide turn interrupt shared by the shell and Core callbacks.
INTERRUPT = TurnInterrupt()


##
 # @brief Concrete terminal runtime adapter for interactive CLI execution.
 #
class CLIRuntime(BaseRuntime):
    ##
     # @brief Initialize the terminal runtime with shared interrupt and printer.
     #
     # @param interrupt Optional TurnInterrupt controller (defaults to module INTERRUPT).
     # @param printer Optional CLIPrinter instance for formatted terminal output.
     # @param echo Whether streaming LLM tokens echo directly to stdout.
     # @param sandbox Whether workspace boundary approval prompts are enforced.
     #
     # @return None.
     #
    def __init__(self, interrupt=None, printer=None, echo=True, sandbox=True):
        super().__init__(
            echo=echo,
            sandbox=sandbox,
            interrupt=INTERRUPT if interrupt is None else interrupt,
        )
        self.printer = _PRINTER if printer is None else printer
    # End-def

    ##
     # @brief Render Core runtime messages with CLIPrinter colors and prefixes.
     #
     # @param text Message text emitted by Core components.
     #
     # @return None.
     #
    def emit(self, text):
        msg = str(text)
        stripped = msg.lstrip()
        if stripped.startswith(("Token:", "Result length:")):
            self.printer.print(f"{self.printer.C_GRAY}{msg}{self.printer.C_RESET}", level="debug")
        elif stripped.startswith(("Tool requested:", "Media attached:")):
            self.printer.print(msg, level="info")
        else:
            print(msg)
        # End-if
    # End-def

    ##
     # @brief Serialize terminal approval prompts and support turn cancellation.
     #
     # @param action_desc Human-readable description of the requested operation.
     # @param abs_target Resolved absolute target path.
     # @param abs_workspace Resolved absolute workspace root.
     #
     # @return True for approval; False for denial or turn cancellation.
     #
    def approve(self, action_desc, abs_target, abs_workspace):
        with _APPROVAL_LOCK:
            print("\n\033[33m[!] SECURITY ALERT: Tool is attempting to escape workspace.\033[0m")
            print(f"    Action   : {action_desc}")
            print(f"    Target   : {abs_target}")
            print(f"    Workspace: {abs_workspace}")
            while True:
                answer = input("    Allow this operation? [y/N/C (pause turn: Stop/Rollback)]: ").strip().lower()
                if answer in ("y", "yes"):
                    print("    [+] User approved outside access.")
                    return True
                # End-if
                if answer in ("c", "cancel"):
                    self.interrupt.request("tool_cancel")
                    print("    [!] Canceled: this call is denied and the turn will pause at the step checkpoint.")
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
# End-class


##
 # @brief Build a terminal runtime bound to the shared process-wide interrupt.
 #
 # @return CLIRuntime instance connected to the terminal and INTERRUPT flag.
 #
def cli_runtime():
    return CLIRuntime(interrupt=INTERRUPT)
# End-def

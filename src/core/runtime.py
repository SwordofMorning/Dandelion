##
 # @file src/core/runtime.py
 # @date 2026/10/27
 #
 # @brief Host interaction callbacks shared by Core execution components.
 # Defines output, cancellation, workspace approval and stream-echo settings
 # used by main agents, subagents and tools without coupling them to a frontend.
 #
 # @note Unattended defaults deny workspace escape and disable stream echo;
 # each CLI or Remote host supplies the callbacks appropriate to its lifetime.
 #

from dataclasses import dataclass
from typing import Callable


##
 # @brief Supply the default inactive cancellation callback.
 #
 # @return False.
 #
def _not_cancelled():
    return False
# End-def


##
 # @brief Deny unattended workspace escape requests by default.
 #
 # @param action_desc Human-readable description of the requested operation.
 # @param abs_target Resolved absolute target path.
 # @param abs_workspace Resolved absolute workspace root.
 #
 # @return False.
 #
def _deny_approval(action_desc, abs_target, abs_workspace):
    return False
# End-def


##
 # @brief Keep host-owned output, cancellation, approval and stream echo callbacks.
 #
 # @param output Complete-message output callback.
 # @param cancelled Callback checking for a stop request.
 # @param approve Callback granting or denying a workspace escape.
 # @param echo Whether provider streams echo to the terminal.
 #
@dataclass
class Runtime:

    output: Callable[[str], None] = print
    cancelled: Callable[[], bool] = _not_cancelled
    approve: Callable[[str, str, str], bool] = _deny_approval
    echo: bool = False

    ##
     # @brief Send a complete text message to the configured output callback.
     #
     # @param text Text to persist or emit.
     #
     # @return None.
     #
    def emit(self, text):
        self.output(str(text))
    # End-def
# End-class

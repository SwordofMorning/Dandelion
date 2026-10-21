##
 # @file src/utils/cli/interrupt.py
 # @date 2026/09/15
 # 
 # @brief Process-wide stop flag for the Ctrl+C turn interrupt.
 #
 # @note Two producers, one consumer:
 #   - the CLI SIGINT handler (Ctrl+C while a turn is running),
 #   - the tools (the [C] option of the workspace-escape approval prompt).
 #   The CLI consumes the flag at its turn checkpoints, then either rolls the
 #   turn back or refuses the stop (see InteractiveCLI._try_stop).
 #
 # @note Single-process. Parallel SubAgents (src/subagent/dispatch.py) READ the
 #       flag from worker threads at their turn boundaries; writes come from
 #       the SIGINT handler or a tool. A plain bool in a dict is safe for this
 #       one-writer/many-readers use under the GIL, so no lock is required.
 #       The root step() only returns after every worker stopped, so the CLI
 #       never rolls back while a SubAgent is still running.
 #

# Module state. A dict is used so the fields stay mutable without module-level
# globals being rebound (setter functions remain the only writers).
_STATE = {"requested": False, "reason": ""}

##
 # @brief Request a stop of the running turn.
 #
 # @param reason Free-form tag ("sigint" / "tool_cancel") kept for messages.
 #
def request(reason=""):
    _STATE["requested"] = True
    _STATE["reason"] = str(reason or "")
# End-def

##
 # @brief Check whether a stop was requested and not consumed yet.
 #
 # @return True when a stop is pending.
 #
def is_requested():
    return bool(_STATE["requested"])
# End-def

##
 # @brief Tag of the pending (or last consumed) stop request.
 #
 # @return Reason string, "" when never set.
 #
def reason():
    return _STATE["reason"]
# End-def

##
 # @brief Clear the pending stop request.
 #
 # @note Called when a turn starts and after every consumption, so a request
 #       can never leak into the next turn.
 #
def clear():
    _STATE["requested"] = False
    _STATE["reason"] = ""
# End-def

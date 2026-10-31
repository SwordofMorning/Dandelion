##
 # @file src/core/agent/interrupt.py
 # @date 2026/10/29
 #
 # @brief Shared cooperative turn-interrupt controller for CLI and Remote hosts.
 # Uses independent lock-free boolean slots for 'rollback' and 'stop' so signal
 # handlers (Ctrl+C SIGINT) never deadlock and concurrent stop requests can
 # never overwrite a rollback request.
 #

MODE_STOP = "stop"
MODE_ROLLBACK = "rollback"


##
 # @brief Signal-safe and thread-safe cooperative interrupt state.
 #
class TurnInterrupt:
    ##
     # @brief Initialize independent stop and rollback flags.
     #
     # @return None.
     #
    def __init__(self):
        self._flags = {"stop": False, "rollback": False, "reason": ""}
    # End-def

    ##
     # @brief Record an interrupt request; 'rollback' and 'stop' write to separate slots.
     #
     # @param reason Free-form trigger tag ("sigint", "tool_cancel", "user_preempt").
     # @param mode Target interrupt mode ("rollback" or "stop").
     #
     # @return None.
     #
    def request(self, reason="", mode=MODE_ROLLBACK):
        if reason:
            self._flags["reason"] = str(reason)
        # End-if
        if str(mode).strip().lower() == MODE_STOP:
            self._flags["stop"] = True
        else:
            self._flags["rollback"] = True
        # End-if
    # End-def

    ##
     # @brief Check whether an unconsumed interrupt is pending.
     #
     # @return True when an interrupt has been requested.
     #
    def is_requested(self):
        return bool(self._flags["rollback"] or self._flags["stop"])
    # End-def

    ##
     # @brief Return the active interrupt mode ("rollback", "stop", or ""); rollback wins.
     #
     # @return Mode string.
     #
    def mode(self):
        if self._flags["rollback"]:
            return MODE_ROLLBACK
        # End-if
        if self._flags["stop"]:
            return MODE_STOP
        # End-if
        return ""
    # End-def

    ##
     # @brief Return the trigger reason of the pending interrupt.
     #
     # @return Reason string.
     #
    def reason(self):
        return self._flags["reason"]
    # End-def

    ##
     # @brief Reset the interrupt state at turn start or after consumption.
     #
     # @return None.
     #
    def clear(self):
        self._flags["rollback"] = False
        self._flags["stop"] = False
        self._flags["reason"] = ""
    # End-def

    ##
     # @brief Alias for threading.Event compatibility (requests a rollback cancel).
     #
     # @return None.
     #
    def set(self):
        self.request(reason="cancel", mode=MODE_ROLLBACK)
    # End-def

    ##
     # @brief Alias for threading.Event compatibility.
     #
     # @return True when an interrupt is pending.
     #
    def is_set(self):
        return self.is_requested()
    # End-def
# End-class

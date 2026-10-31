##
 # @file src/core/agent/interrupt.py
 # @date 2026/10/29
 #
 # @brief Shared cooperative turn-interrupt controller for CLI and Remote hosts.
 # Uses lock-free atomic tuple replacement so signal handlers (Ctrl+C SIGINT)
 # can never deadlock when interrupting the main thread mid-operation.
 #

MODE_STOP = "stop"
MODE_ROLLBACK = "rollback"


##
 # @brief Signal-safe and thread-safe cooperative interrupt state.
 #
class TurnInterrupt:
    ##
     # @brief Initialize an idle interrupt state tuple (requested, mode, reason).
     #
     # @return None.
     #
    def __init__(self):
        self._state = (False, "", "")
    # End-def

    ##
     # @brief Record an interrupt request; 'rollback' takes precedence over 'stop'.
     #
     # @param reason Free-form trigger tag ("sigint", "tool_cancel", "user_preempt").
     # @param mode Target interrupt mode ("rollback" or "stop").
     #
     # @return None.
     #
    def request(self, reason="", mode=MODE_ROLLBACK):
        target = MODE_STOP if str(mode).strip().lower() == MODE_STOP else MODE_ROLLBACK
        _, cur_mode, cur_reason = self._state
        next_mode = (
            MODE_ROLLBACK
            if (cur_mode == MODE_ROLLBACK or target == MODE_ROLLBACK)
            else MODE_STOP
        )
        next_reason = str(reason or cur_reason or "")
        self._state = (True, next_mode, next_reason)
    # End-def

    ##
     # @brief Check whether an unconsumed interrupt is pending.
     #
     # @return True when an interrupt has been requested.
     #
    def is_requested(self):
        return bool(self._state[0])
    # End-def

    ##
     # @brief Return the active interrupt mode ("rollback", "stop", or "").
     #
     # @return Mode string.
     #
    def mode(self):
        return self._state[1]
    # End-def

    ##
     # @brief Return the trigger reason of the pending interrupt.
     #
     # @return Reason string.
     #
    def reason(self):
        return self._state[2]
    # End-def

    ##
     # @brief Reset the interrupt state at turn start or after consumption.
     #
     # @return None.
     #
    def clear(self):
        self._state = (False, "", "")
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

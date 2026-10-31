##
 # @file src/core/agent/interrupt.py
 # @date 2026/10/29
 #
 # @brief Shared cooperative turn-interrupt controller for CLI and Remote hosts.
 # Tracks stop vs rollback requests across step checkpoints so active turns
 # can either roll back to the pre-commit SessionBackup or stop in place at
 # the current step boundary while preserving completed session progress.
 #

import threading

MODE_STOP = "stop"
MODE_ROLLBACK = "rollback"


##
 # @brief Thread-safe cooperative interrupt state for one agent or process.
 #
class TurnInterrupt:
    ##
     # @brief Initialize an idle interrupt state.
     #
     # @return None.
     #
    def __init__(self):
        self._lock = threading.Lock()
        self._requested = False
        self._mode = ""
        self._reason = ""
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
        with self._lock:
            self._requested = True
            if self._mode != MODE_ROLLBACK:
                self._mode = target
            # End-if
            if reason or not self._reason:
                self._reason = str(reason or "")
            # End-if
        # End-with
    # End-def

    ##
     # @brief Check whether an unconsumed interrupt is pending.
     #
     # @return True when an interrupt has been requested.
     #
    def is_requested(self):
        return bool(self._requested)
    # End-def

    ##
     # @brief Return the active interrupt mode ("rollback", "stop", or "").
     #
     # @return Mode string.
     #
    def mode(self):
        return self._mode
    # End-def

    ##
     # @brief Return the trigger reason of the pending interrupt.
     #
     # @return Reason string.
     #
    def reason(self):
        return self._reason
    # End-def

    ##
     # @brief Reset the interrupt state at turn start or after consumption.
     #
     # @return None.
     #
    def clear(self):
        with self._lock:
            self._requested = False
            self._mode = ""
            self._reason = ""
        # End-with
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

##
 # @file src/cli/interrupt.py
 # @date 2026/10/29
 #
 # @brief Cooperative terminal turn-interrupt state adapter.
 # Wraps the shared Core TurnInterrupt controller for the single-process CLI.
 #

from src.core.agent.interrupt import TurnInterrupt, MODE_STOP, MODE_ROLLBACK

_INTERRUPT = TurnInterrupt()


##
 # @brief Request an interrupt of the running CLI turn.
 #
 # @param reason Free-form tag ("sigint" / "tool_cancel") kept for messages.
 # @param mode Target interrupt mode ("rollback" or "stop").
 #
 # @return None.
 #
def request(reason="", mode=MODE_ROLLBACK):
    _INTERRUPT.request(reason=reason, mode=mode)
# End-def


##
 # @brief Check whether a stop was requested and not consumed yet.
 #
 # @return True when a stop is pending.
 #
def is_requested():
    return _INTERRUPT.is_requested()
# End-def


##
 # @brief Tag of the pending (or last consumed) stop request.
 #
 # @return Reason string, "" when never set.
 #
def reason():
    return _INTERRUPT.reason()
# End-def


##
 # @brief Mode of the pending interrupt ("rollback", "stop", or "").
 #
 # @return Mode string.
 #
def mode():
    return _INTERRUPT.mode()
# End-def


##
 # @brief Clear the pending stop request.
 #
 # @return None.
 #
def clear():
    _INTERRUPT.clear()
# End-def

##
 # @file packages/dandelion-core/src/dandelion_core/runtime.py
 # @date 2026/11/28
 #
 # @brief Host interaction runtime abstractions shared by Core execution components.
 # Defines BaseRuntime(ABC) for output, cooperative cancellation, workspace
 # approval, and stream-echo control, plus DefaultRuntime (aliased as Runtime)
 # for standalone and callback-injected execution without frontend coupling.
 #
 # @note Unattended defaults deny workspace escape and disable stream echo;
 # CLI and Remote frontends subclass BaseRuntime with host-specific policies.
 #

from abc import ABC, abstractmethod


##
 # @brief Abstract base class for host runtime adapters consumed by Core.
 #
class BaseRuntime(ABC):
    ##
     # @brief Initialize common runtime flags and cooperative interrupt controller.
     #
     # @param echo Whether provider streams echo directly to the terminal.
     # @param sandbox Whether workspace path and shell sandbox restrictions apply.
     # @param interrupt Optional TurnInterrupt controller; created lazily if None.
     #
     # @return None.
     #
    def __init__(self, *, echo=False, sandbox=True, interrupt=None):
        self.echo = bool(echo)
        self.sandbox = bool(sandbox)
        if interrupt is None:
            from .agent.interrupt import TurnInterrupt
            interrupt = TurnInterrupt()
        # End-if
        self.interrupt = interrupt
    # End-def

    ##
     # @brief Emit a complete status or diagnostic message to the host frontend.
     #
     # @param text Message text to render or forward.
     #
     # @return None.
     #
    @abstractmethod
    def emit(self, text) -> None:
        pass
    # End-def

    ##
     # @brief Decide whether an operation outside the workspace root is allowed.
     #
     # @param action_desc Human-readable description of the requested operation.
     # @param abs_target Resolved absolute target path.
     # @param abs_workspace Resolved absolute workspace root.
     #
     # @return True when permitted, False when denied.
     #
    @abstractmethod
    def approve(self, action_desc, abs_target, abs_workspace) -> bool:
        pass
    # End-def

    ##
     # @brief Check whether a cooperative stop or rollback interrupt is pending.
     #
     # @return True when an interrupt has been requested.
     #
    def cancelled(self) -> bool:
        return self.interrupt.is_requested()
    # End-def

    ##
     # @brief Request a cooperative turn stop or rollback via the interrupt slot.
     #
     # @param reason Free-form trigger label recorded for diagnostics.
     # @param mode Interrupt mode ("stop" or "rollback").
     #
     # @return None.
     #
    def request_stop(self, reason="stop", mode="stop") -> None:
        self.interrupt.request(reason, mode=mode)
    # End-def

    ##
     # @brief Clear any pending cooperative interrupt request.
     #
     # @return None.
     #
    def clear_stop(self) -> None:
        self.interrupt.clear()
    # End-def
# End-class


##
 # @brief Concrete callback-backed runtime implementation for default and legacy callers.
 #
class DefaultRuntime(BaseRuntime):
    ##
     # @brief Construct a concrete runtime from optional host callbacks.
     #
     # @param output Callable accepting one string for complete-message output.
     # @param cancelled Optional zero-argument predicate checking external cancellation.
     # @param approve Optional approval callback(action_desc, abs_target, abs_workspace).
     # @param echo Whether provider streams echo to the terminal.
     # @param sandbox Whether workspace path and shell sandbox restrictions apply.
     # @param interrupt Optional TurnInterrupt controller shared with the caller.
     #
     # @return None.
     #
    def __init__(self, output=print, cancelled=None, approve=None,
                 echo=False, sandbox=True, interrupt=None):
        super().__init__(echo=echo, sandbox=sandbox, interrupt=interrupt)
        self.output = output if output is not None else print
        self._cancelled_cb = cancelled
        self._approve_cb = approve
    # End-def

    ##
     # @brief Send a complete text message to the configured output callback.
     #
     # @param text Text to persist or emit.
     #
     # @return None.
     #
    def emit(self, text) -> None:
        self.output(str(text))
    # End-def

    ##
     # @brief Grant or deny a workspace escape request via the configured callback.
     #
     # @param action_desc Human-readable description of the requested operation.
     # @param abs_target Resolved absolute target path.
     # @param abs_workspace Resolved absolute workspace root.
     #
     # @return True when allowed; False by default when unattended.
     #
    def approve(self, action_desc, abs_target, abs_workspace) -> bool:
        if self._approve_cb is None:
            return False
        # End-if
        return bool(self._approve_cb(action_desc, abs_target, abs_workspace))
    # End-def

    ##
     # @brief Check external cancellation callback and internal TurnInterrupt state.
     #
     # @return True when either the injected callback or TurnInterrupt signals stop.
     #
    def cancelled(self) -> bool:
        if self._cancelled_cb is not None and bool(self._cancelled_cb()):
            return True
        # End-if
        return super().cancelled()
    # End-def
# End-class


# Public alias preserving backward compatibility for callers importing Runtime.
Runtime = DefaultRuntime

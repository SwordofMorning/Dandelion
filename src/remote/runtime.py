##
 # @file src/remote/runtime.py
 # @date 2026/11/28
 #
 # @brief Remote runtime implementation inheriting Core BaseRuntime.
 # Encapsulates Telegram progress forwarding, unrestricted server approvals,
 # non-echoing streaming, and per-conversation TurnInterrupt state.
 #

from dandelion_core.agent.interrupt import TurnInterrupt
from dandelion_core.runtime import BaseRuntime


##
 # @brief Concrete runtime adapter for Dandelion Remote conversation agents.
 #
class RemoteRuntime(BaseRuntime):
    ##
     # @brief Initialize an unrestricted Remote runtime bound to a session and hub.
     #
     # @param session_id Conversation identifier (e.g. 'telegram:123456').
     # @param hub Optional ChannelHub owning a Telegram transport for progress updates.
     # @param interrupt Optional TurnInterrupt controller for this conversation.
     # @param echo Whether streaming LLM tokens echo directly to stdout.
     # @param sandbox Whether workspace boundary approval prompts are enforced.
     #
     # @return None.
     #
    def __init__(self, session_id="", hub=None, interrupt=None, echo=False, sandbox=False):
        super().__init__(
            echo=echo,
            sandbox=sandbox,
            interrupt=interrupt if interrupt is not None else TurnInterrupt(),
        )
        self.session_id = str(session_id)
        self.hub = hub
    # End-def

    ##
     # @brief Forward Core step progress output to Telegram when active.
     #
     # @param text Message text emitted by Core components.
     #
     # @return None.
     #
    def emit(self, text):
        if self.session_id.startswith("telegram:") and self.hub is not None:
            tg = getattr(self.hub, "telegram", None)
            if tg is not None and hasattr(tg, "on_progress"):
                tg.on_progress(self.session_id.split(":", 1)[1], text)
            # End-if
        # End-if
    # End-def

    ##
     # @brief Auto-approve server tool operations without interactive prompts.
     #
     # @param action_desc Human-readable description of the requested operation.
     # @param abs_target Resolved absolute target path.
     # @param abs_workspace Resolved absolute workspace root.
     #
     # @return Always True.
     #
    def approve(self, action_desc, abs_target, abs_workspace):
        return True
    # End-def
# End-class

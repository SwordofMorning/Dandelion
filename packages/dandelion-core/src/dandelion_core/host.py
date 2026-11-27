##
 # @file packages/dandelion-core/src/dandelion_core/host.py
 # @date 2026/11/28
 #
 # @brief Abstract base host controller shared by CLI and Remote frontends.
 # Centralizes interrupted-tool history recovery, pre-turn backup capture,
 # cooperative turn stop, and turn rollback lifecycle helpers.
 #

from abc import ABC, abstractmethod
import json

from .agent.prune import block_field
from .utils.logging.backup import SessionBackup


##
 # @brief Abstract base class for concrete Dandelion agent host frontends.
 #
class BaseAgentHost(ABC):
    ##
     # @brief Initialize host references to the active agent and session manager.
     #
     # @param agent Optional MyAgent instance owned by the host.
     # @param session Optional SessionManager instance owned by the host.
     #
     # @return None.
     #
    def __init__(self, agent=None, session=None):
        self.agent = agent
        self.session = session
    # End-def

    ##
     # @brief Close orphaned persisted tool_use blocks without replaying side effects.
     #
     # @param agent Agent whose history and session manager are repaired in place.
     #
     # @return List of repaired tool_use identifiers.
     #
    @staticmethod
    def repair_interrupted_tools(agent) -> list:
        repaired = []
        index = 0
        while index < len(agent.history):
            message = agent.history[index]
            content = message.get("content", [])
            if message.get("role") != "assistant" or not isinstance(content, list):
                index += 1
                continue
            # End-if
            ids = list(dict.fromkeys(
                block_field(block, "id") for block in content
                if block_field(block, "type") == "tool_use" and block_field(block, "id")
            ))
            if not ids:
                index += 1
                continue
            # End-if
            following = agent.history[index + 1] if index + 1 < len(agent.history) else None
            results = (
                following.get("content", [])
                if following and following.get("role") == "user"
                else []
            )
            present = {
                block_field(block, "tool_use_id") for block in results
                if block_field(block, "type") == "tool_result"
            } if isinstance(results, list) else set()
            missing = [tool_id for tool_id in ids if tool_id not in present]
            if missing:
                interrupted = [{
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": json.dumps({
                        "status": "interrupted",
                        "side_effects_unknown": True,
                        "message": (
                            "Process stopped before the tool result was saved. "
                            "This call was not replayed. Inspect existing jobs/artifacts "
                            "before explicitly resubmitting; actions were not rolled back."
                        ),
                    }),
                } for tool_id in missing]
                if following and following.get("role") == "user" and isinstance(results, list):
                    following["content"] = results + interrupted
                else:
                    agent.history.insert(index + 1, {"role": "user", "content": interrupted})
                # End-if
                repaired.extend(missing)
            # End-if
            index += 1
        # End-while
        if repaired:
            agent.session.log_api_call("INTERRUPTED TOOL RECOVERY", {
                "tool_use_ids": repaired,
                "status": "interrupted",
                "side_effects_unknown": True,
            })
            agent.session.save_history(agent.history)
        # End-if
        return repaired
    # End-def

    ##
     # @brief Create and capture a pre-turn SessionBackup for the active session.
     #
     # @param session Optional SessionManager override; defaults to self.session.
     # @param note Free-form tag recorded in backup.json.
     # @param history_messages Optional message count recorded for diagnostics.
     #
     # @return Captured SessionBackup instance.
     #
    def capture_turn_backup(self, session=None, note="commit", history_messages=None) -> SessionBackup:
        target_session = session if session is not None else self.session
        session_dir = getattr(target_session, "current_session_dir", None) if target_session is not None else None
        backup = SessionBackup(session_dir)
        if history_messages is None and self.agent is not None and hasattr(self.agent, "history"):
            history_messages = len(self.agent.history)
        # End-if
        backup.create(note=note, history_messages=history_messages)
        return backup
    # End-def

    ##
     # @brief Stop the active agent turn at a clean history boundary.
     #
     # @param note Closing assistant marker appended when history ends on user.
     # @param agent Optional MyAgent override; defaults to self.agent.
     #
     # @return None.
     #
    def stop_active_turn(self, note="[Turn stopped by user.]", agent=None) -> None:
        target_agent = agent if agent is not None else self.agent
        if target_agent is not None:
            target_agent.stop_turn(note)
        # End-if
    # End-def

    ##
     # @brief Roll back the active agent turn to a previously captured SessionBackup.
     #
     # @param backup SessionBackup instance to restore (or None for current session).
     # @param trigger Reason tag recorded in the audit log.
     # @param agent Optional MyAgent override; defaults to self.agent.
     #
     # @return (success_bool, restored_items, failed_items) tuple.
     #
    def rollback_active_turn(self, backup=None, trigger="", agent=None):
        target_agent = agent if agent is not None else self.agent
        if target_agent is None:
            return False, [], ["no_active_agent"]
        # End-if
        return target_agent.rollback_turn(backup=backup, trigger=trigger)
    # End-def

    ##
     # @brief Execute the concrete host main loop.
     #
     # @return Host-defined exit status or None.
     #
    @abstractmethod
    def run(self):
        pass
    # End-def
# End-class

##
 # @file src/core/tool/agent/prune_tool.py
 # @date 2026/10/28
 #
 # @brief Agent-initiated context pruning tool.
 # Allows the Main Agent to invoke the ContextPruner SubAgent to fold or drop
 # obsolete tool outputs and completed turns from history.log safely.
 #

import json
from ..base_tool import BaseTool
from src.core.agent.prune import ContextPruner


##
 # @brief Tool enabling the Agent to proactively prune its conversation history.
 #
class PruneContextTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param agent Host MyAgent instance owning history, client and session.
     #
     # @return None.
     #
    def __init__(self, agent):
        super().__init__(workspace_dir=agent.workspace_dir)
        self.agent = agent
    # End-def

    ##
     # @brief Return the registered tool name.
     #
     # @return Tool identifier string.
     #
    def get_name(self):
        return "prune_context"
    # End-def

    ##
     # @brief Describe the tool capability to the model.
     #
     # @return Human-readable tool description.
     #
    def get_description(self):
        return (
            "Proactively prune obsolete conversation turns or fold large read_file/grep/bash "
            "outputs from history.log via a dedicated Context Pruning SubAgent. "
            "A full backup is archived automatically before any changes, and tool_use/tool_result "
            "pairings are strictly validated. Call this after finishing a large file inspection "
            "or sub-task (once key facts are saved with 'remember') to keep context lean."
        )
    # End-def

    ##
     # @brief Return the model-visible JSON input schema.
     #
     # @return JSON schema dictionary.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "focus_hint": {
                    "type": "string",
                    "description": "Brief note on which completed explorations or large tool outputs can now be folded or dropped."
                },
                "keep_recent_units": {
                    "type": "integer",
                    "minimum": 2,
                    "maximum": 20,
                    "description": "Number of most recent closed turn/tool units to keep untouched (default: 4)."
                }
            },
            "required": ["focus_hint"]
        }
    # End-def

    ##
     # @brief Run the pruning SubAgent on closed history units and persist if valid.
     #
     # @param kwargs Tool parameters: focus_hint, keep_recent_units.
     #
     # @return (success_bool, result_json_string) tuple.
     #
    def execute(self, **kwargs):
        focus_hint = str(kwargs.get("focus_hint") or "").strip()
        keep_recent = int(kwargs.get("keep_recent_units") or 4)
        pruner = ContextPruner(
            safe_client=self.agent.client,
            session_manager=self.agent.session,
            runtime=self.agent.runtime,
            config=self.agent.config,
        )
        new_history, stats = pruner.prune(
            self.agent.history,
            focus_hint=focus_hint,
            keep_recent_units=keep_recent,
            in_flight_tail=True,
            tag="prune",
        )
        if stats.get("status") == "ok":
            self.agent.history[:] = new_history
            self.agent.session.save_history(self.agent.history)
            return True, json.dumps(stats, ensure_ascii=False)
        # End-if
        return True, json.dumps(stats, ensure_ascii=False)
    # End-def
# End-class

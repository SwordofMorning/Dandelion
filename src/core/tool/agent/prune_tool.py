##
 # @file src/core/tool/agent/prune_tool.py
 # @date 2026/10/28
 #
 # @brief Agent-initiated context pruning and session rollover tool.
 # Allows the Main Agent to invoke its ContextPruner SubAgent to fold or drop
 # obsolete tool outputs and completed turns from history.log safely, and
 # optionally rotate into a fresh session branch ("Let LLM Decide").
 #
 # @note When rollover=True, the pruned history (including the in-flight
 # prune_context tool_use tail) and current task_state.json + session memory
 # are inherited into the newly created session directory BEFORE the caller
 # appends the tool_result, preserving strict tool_use/tool_result pairing
 # and per-turn SessionBackup isolation in the previous session.
 #

import datetime
import json
import os
import shutil

from ..base_tool import BaseTool


##
 # @brief Tool enabling the Agent to proactively prune or roll over its conversation session.
 #
class PruneContextTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param agent Host MyAgent instance owning history, pruner and session.
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
            "outputs from history.log via a dedicated Context Pruning SubAgent, and optionally "
            "roll over into a fresh session branch ('rollover=true'). A full backup is archived "
            "automatically before any changes, and tool_use/tool_result pairings are strictly "
            "validated. Call this after finishing a large inspection/sub-task or during a "
            "scheduled maintenance wakeup ('Let LLM Decide') to keep context lean."
        )
    # End-def

    ##
     # @brief Return the model-visible JSON input schema.
     #
     # @return JSON schema dictionary.
     #
    def get_schema(self):
        registry = getattr(getattr(self.agent, "client", None), "registry", None)
        aliases = registry.aliases() if registry is not None else []
        props = {
            "focus_hint": {
                "type": "string",
                "description": (
                    "Brief note on which completed explorations or large tool outputs "
                    "can now be folded or dropped."
                ),
            },
            "keep_recent_units": {
                "type": "integer",
                "minimum": 2,
                "maximum": 20,
                "description": "Number of most recent closed turn/tool units to keep untouched (default: 4).",
            },
            "rollover": {
                "type": "boolean",
                "description": (
                    "If true, rotate into a new session directory (inheriting pruned history, "
                    "task_state.json, and session memory) for daily or milestone rollover."
                ),
            },
            "session_name": {
                "type": "string",
                "description": "Optional ASCII name for the new session when rollover=true (default: dr_YYYY-MM-DD).",
            },
        }
        if aliases:
            props["model"] = {
                "type": "string",
                "enum": aliases,
                "description": "Optional lightweight SubAgent model alias to run the pruning pass.",
            }
        # End-if
        return {
            "type": "object",
            "properties": props,
            "required": ["focus_hint"],
        }
    # End-def

    ##
     # @brief Copy task_state.json and session memory entries into a rolled-over session directory.
     #
     # @param old_dir Previous session directory path.
     # @param new_dir Newly created session directory path.
     #
     # @return None.
     #
    @staticmethod
    def _inherit_session_state(old_dir, new_dir):
        if not old_dir or not new_dir or os.path.abspath(old_dir) == os.path.abspath(new_dir):
            return
        # End-if
        old_state = os.path.join(old_dir, "task_state.json")
        if os.path.isfile(old_state):
            try:
                shutil.copy2(old_state, os.path.join(new_dir, "task_state.json"))
            except OSError:
                pass
            # End-try
        # End-if
        old_mem = os.path.join(old_dir, "memory")
        new_mem = os.path.join(new_dir, "memory")
        if os.path.isdir(old_mem):
            os.makedirs(new_mem, exist_ok=True)
            for name in os.listdir(old_mem):
                src = os.path.join(old_mem, name)
                dst = os.path.join(new_mem, name)
                if os.path.isfile(src):
                    try:
                        shutil.copy2(src, dst)
                    except OSError:
                        pass
                    # End-try
                # End-if
            # End-for
        # End-if
    # End-def

    ##
     # @brief Run the pruning SubAgent on closed history units and optionally roll over the session.
     #
     # @param kwargs Tool parameters: focus_hint, keep_recent_units, rollover, session_name, model.
     #
     # @return (success_bool, result_json_string) tuple.
     #
    def execute(self, **kwargs):
        focus_hint = str(kwargs.get("focus_hint") or "").strip()
        rollover = bool(kwargs.get("rollover", False))
        default_keep = 6 if rollover else 4
        keep_recent = int(kwargs.get("keep_recent_units") or default_keep)
        model_alias = str(kwargs.get("model") or "").strip() or None
        raw_name = str(kwargs.get("session_name") or "").strip()

        if raw_name and not raw_name.isascii():
            return False, "Error: 'session_name' must contain ASCII characters only."
        # End-if

        tag = "daily_rollover" if rollover else "prune"
        new_history, stats = self.agent.pruner.prune(
            self.agent.history,
            focus_hint=focus_hint,
            keep_recent_units=keep_recent,
            in_flight_tail=True,
            tag=tag,
            model_alias=model_alias,
        )

        status = stats.get("status")
        if status in ("archive_failed", "llm_failed", "validation_failed"):
            return True, json.dumps(stats, ensure_ascii=False)
        # End-if

        active_history = new_history if status == "ok" else list(self.agent.history)
        if rollover:
            old_id = self.agent.session.current_session_id
            old_dir = self.agent.session.current_session_dir
            target_name = raw_name or f"dr_{datetime.date.today().isoformat()}"
            new_id = self.agent.session.create_session(name=target_name)
            new_dir = self.agent.session.current_session_dir
            self._inherit_session_state(old_dir, new_dir)
            self.agent.history[:] = active_history
            self.agent.session.save_history(self.agent.history)
            self.agent._invalidate_memories_cache()
            stats = dict(
                stats,
                rollover=True,
                previous_session_id=old_id,
                new_session_id=new_id,
                session_name=target_name,
            )
            return True, json.dumps(stats, ensure_ascii=False)
        # End-if

        if status == "ok":
            self.agent.history[:] = new_history
            self.agent.session.save_history(self.agent.history)
        # End-if
        return True, json.dumps(stats, ensure_ascii=False)
    # End-def
# End-class

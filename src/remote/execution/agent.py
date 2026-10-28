##
 # @file src/remote/execution/agent.py
 # @date 2026/10/27
 #
 # @brief Remote conversation agent assembly.
 # Builds an unrestricted Core agent with RemotePromptBuilder, full direct
 # shell/SSH/search/file/memory/pruning tools, SendMessageTool, and background
 # delegation tools.
 #

import hashlib
import json
import threading
from pathlib import Path

from ..sysprompt import RemotePromptBuilder
from ..tool import SubmitTaskTool, TaskControlTool, SendMessageTool

MAIN_DIRECT_TOOLS = (
    "bash",
    "ssh",
    "web_search",
    "read_file",
    "write_file",
    "edit_file",
    "list_directory",
    "grep_search",
    "markdown_editor",
    "load_skill",
    "get_time",
    "remember",
    "forget",
    "update_state",
    "prune_context",
)


##
 # @brief Close persisted tool calls without replaying unknown external actions.
 #
 # @param agent Agent whose runtime, session and history are used.
 #
 # @return Identifiers of repaired tool calls.
 #
def repair_interrupted_tools(agent):
    ##
     # @brief Read a block field from a mapping or SDK object.
     #
     # @param block Dictionary or SDK content block.
     # @param key Field name to read.
     # @param default Fallback value when the requested field is absent.
     #
     # @return Block field value, or the supplied default.
     #
    def field(block, key, default=None):
        return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)
    # End-def

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
            field(block, "id") for block in content
            if field(block, "type") == "tool_use" and field(block, "id")
        ))
        if not ids:
            index += 1
            continue
        # End-if
        following = agent.history[index + 1] if index + 1 < len(agent.history) else None
        results = following.get("content", []) if following and following.get("role") == "user" else []
        present = {
            field(block, "tool_use_id") for block in results
            if field(block, "type") == "tool_result"
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
                        "Server stopped before the tool result was saved. "
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
        agent.session.log_api_call("REMOTE INTERRUPTED TOOL RECOVERY", {
            "tool_use_ids": repaired,
            "status": "interrupted",
            "side_effects_unknown": True,
        })
        agent.session.save_history(agent.history)
    # End-if
    return repaired
# End-def


##
 # @brief Create a factory for isolated remote conversation agents.
 #
 # @param config Loaded model and routing configuration.
 # @param data_dir Private remote-service data directory.
 # @param workspace Workspace assigned to the task or remote session.
 # @param jobs Background job manager used by remote delegation tools.
 # @param hub Optional ChannelHub for SendMessageTool.
 # @param backups Optional shared BackupStore for synced session overview.
 #
 # @return Callable creating a core agent for one conversation.
 #
def make_agent_factory(config, data_dir, workspace, jobs, hub=None, backups=None):
    ##
     # @brief Build or restore one remote conversation agent.
     #
     # @param session_id Conversation identifier used to scope persisted records.
     #
     # @return Restored agent with unrestricted runtime, communication and delegation tools.
     #
    def create(session_id):
        from src.core.agent import MyAgent
        from src.core.agent.tools import build_tools
        from src.core.runtime import Runtime
        from src.core.utils.logging.session import SessionManager

        scope = hashlib.sha256(str(session_id).encode()).hexdigest()
        root = Path(data_dir) / "sessions" / scope
        session = SessionManager(log_dir=str(root))
        cancelled = threading.Event()
        runtime = Runtime(
            output=lambda text: None,
            cancelled=cancelled.is_set,
            approve=lambda action, target, ws: True,
            echo=False,
            sandbox=False,
        )

        ##
         # @brief Construct RemotePromptBuilder bound to the shared BackupStore.
         #
         # @param memory_mgr MemoryManager instance.
         # @param skill_mgr SkillManager instance.
         # @param cfg Configuration dictionary.
         # @param ws_dir Workspace root directory.
         # @param session_manager SessionManager instance.
         #
         # @return Configured RemotePromptBuilder instance.
         #
        def prompt_factory(memory_mgr, skill_mgr, cfg, ws_dir, session_manager=None):
            return RemotePromptBuilder(
                memory_mgr,
                skill_mgr,
                cfg,
                ws_dir,
                session_manager=session_manager,
                backups=backups,
            )
        # End-def

        ##
         # @brief Mount direct shell/SSH/search/file/memory/pruning tools plus Remote delegation and communication tools.
         #
         # @param agent Agent whose runtime, session and history are used.
         #
         # @return Mapping of tools available to the remote main agent.
         #
        def tools(agent):
            shared = build_tools(agent)

            ##
             # @brief Snapshot reference context without sharing mutable history.
             #
             # @return Bounded JSON snapshot of recent history.
             #
            def context():
                return json.dumps(agent.history[-8:], ensure_ascii=False, default=str)[-16000:]
            # End-def

            result = {
                name: shared[name]
                for name in MAIN_DIRECT_TOOLS
                if name in shared
            }
            result["submit_task"] = SubmitTaskTool(jobs, str(session_id), workspace, context)
            result["task_control"] = TaskControlTool(jobs, str(session_id))
            if hub is not None:
                result["send_message"] = SendMessageTool(hub, str(session_id))
            # End-if
            return result
        # End-def

        agent = MyAgent(
            config,
            session,
            workspace,
            runtime=runtime,
            tool_factory=tools,
            prompt_builder_cls=prompt_factory,
        )
        agent._remote_cancel_event = cancelled
        repair_interrupted_tools(agent)
        return agent
    # End-def
    return create
# End-def

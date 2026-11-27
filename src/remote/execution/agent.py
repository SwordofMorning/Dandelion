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
from pathlib import Path

from dandelion_core.host import BaseAgentHost

from ..runtime import RemoteRuntime
from ..sysprompt import RemotePromptBuilder
from ..tool import SubmitTaskTool, TaskControlTool, SendMessageTool, GoogleDriveTool

MAIN_DIRECT_TOOLS = (
    "bash",
    "ssh",
    "tavily_search",
    "read_file",
    "write_file",
    "edit_file",
    "list_directory",
    "grep_search",
    "markdown_editor",
    "read_image",
    "read_pdf",
    "load_skill",
    "get_time",
    "remember",
    "forget",
    "memory_search",
    "update_state",
    "prune_context",
    "plan_tool",
)

# Re-export shared interrupted tool recovery from Core BaseAgentHost.
repair_interrupted_tools = BaseAgentHost.repair_interrupted_tools


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
        from dandelion_core.agent import MyAgent
        from dandelion_core.agent.tools import build_tools
        from dandelion_core.utils.logging.session import SessionManager

        scope = hashlib.sha256(str(session_id).encode()).hexdigest()
        root = Path(data_dir) / "sessions" / scope
        session = SessionManager(log_dir=str(root))
        runtime = RemoteRuntime(session_id=session_id, hub=hub)

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
            if agent.pool.max_depth > 0 and agent.client.registry.get_all_specs():
                result["submit_task"] = SubmitTaskTool(
                    jobs, str(session_id), workspace, context, registry=agent.client.registry
                )
            # End-if
            result["task_control"] = TaskControlTool(jobs, str(session_id))
            result["google_drive"] = GoogleDriveTool(config, workspace, backups=backups)
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
        BaseAgentHost.repair_interrupted_tools(agent)
        return agent
    # End-def
    return create
# End-def

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
from mk.lib.paths import tool_dirs as resolve_tool_dirs

from ..runtime import RemoteRuntime
from ..sysprompt import RemotePromptBuilder
from ..tool import SubmitTaskTool, TaskControlTool, SendMessageTool, GoogleDriveTool

# Tools excluded from the Remote main agent (delegated to background workers
# via submit_task instead of synchronous inline execution). Any built-in or
# hot-plugged extension tool not in this denylist passes through automatically.
EXCLUDED_REMOTE_MAIN_TOOLS = frozenset({
    "spawn_subagent",
    "read_excel",
    "write_excel",
})

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
 # @param config_dir Optional operator config directory (.env) for SSH devices.yaml/known_hosts.
 # @param tool_dirs Optional ordered directories scanned for hot-plugged Remote tools.
 #
 # @return Callable creating a core agent for one conversation.
 #
def make_agent_factory(config, data_dir, workspace, jobs, hub=None, backups=None,
                       config_dir=None, tool_dirs=None):
    extra_tool_dirs = config.get("TOOL_DIRS") if isinstance(config, dict) else None
    remote_root = str(Path(__file__).resolve().parents[3])
    remote_tool_dirs = (
        list(tool_dirs)
        if tool_dirs is not None
        else resolve_tool_dirs("remote", extra_tool_dirs, root_dir=remote_root)
    )

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
         # @brief Mount direct shell/SSH/search/file/memory/pruning/extension tools plus Remote delegation and communication tools.
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
                name: tool
                for name, tool in shared.items()
                if name not in EXCLUDED_REMOTE_MAIN_TOOLS
            }
            pool_ts_reg = getattr(getattr(agent, "pool", None), "toolset_registry", None)
            if pool_ts_reg is not None and hasattr(jobs, "toolset_registry"):
                jobs.toolset_registry = pool_ts_reg
            # End-if
            if agent.pool.max_depth > 0 and agent.client.registry.get_all_specs():
                result["submit_task"] = SubmitTaskTool(
                    jobs,
                    str(session_id),
                    workspace,
                    context,
                    registry=agent.client.registry,
                    toolset_registry=pool_ts_reg,
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
            config_dir=config_dir,
            host="remote",
            tool_dirs=remote_tool_dirs,
        )
        BaseAgentHost.repair_interrupted_tools(agent)
        return agent
    # End-def
    return create
# End-def

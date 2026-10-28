##
 # @file src/core/agent/tools.py
 # @date 2026/10/27
 #
 # @brief Agent tool construction and delegation catalog assembly.
 # Creates filesystem, shell, memory, context-pruning, skill and media tools
 # for one agent or isolated worker pool.
 #

import os
from src.core.subagent import SubAgentPool
from src.core.agent.skill import SkillManager

from src.core.tool import (
    BashTool, LoadSkillTool, MarkdownTool,
    GrepSearchTool, WriteFileTool, ReadFileTool, ListDirectoryTool,
    EditFileTool, PlanTool, SpawnSubagentTool, WebSearchTool,
    ReadExcelTool, WriteExcelTool,
    StateTool, MemoryTool, ForgetMemoryTool, PruneContextTool,
    SSHTool, TimeTool,
    ReadPdfTool, ReadImageTool
)


##
 # @brief Build the worker tool catalog for a SubAgentPool without a full MyAgent.
 #
 # @param workspace_dir Workspace root directory.
 # @param config Loaded model and routing configuration.
 # @param skill_manager SkillManager instance.
 # @param session_manager SessionManager instance for media/session paths.
 # @param runtime Runtime callbacks bound to each tool.
 # @param host Optional media host object.
 #
 # @return Dictionary of worker-eligible tool instances and list of media tools.
 #
def _build_worker_catalog(workspace_dir, config, skill_manager, session_manager, runtime, host=None):
    bash = BashTool(workspace_dir=workspace_dir)
    skill_loader = LoadSkillTool(skill_manager)
    md_editor = MarkdownTool(workspace_dir=workspace_dir)
    read_excel_tool = ReadExcelTool(workspace_dir=workspace_dir)
    write_excel_tool = WriteExcelTool(workspace_dir=workspace_dir)
    grep_tool = GrepSearchTool(workspace_dir=workspace_dir)
    write_tool = WriteFileTool(workspace_dir=workspace_dir)
    read_tool = ReadFileTool(workspace_dir=workspace_dir)
    list_tool = ListDirectoryTool(workspace_dir=workspace_dir)
    edit_tool = EditFileTool(workspace_dir=workspace_dir)
    web_search_tool = WebSearchTool(workspace_dir=workspace_dir, config=config)
    time_tool = TimeTool(workspace_dir=workspace_dir, config=config)
    ssh_tool = SSHTool(workspace_dir=workspace_dir)

    all_tools = {
        bash.get_name(): bash,
        skill_loader.get_name(): skill_loader,
        md_editor.get_name(): md_editor,
        grep_tool.get_name(): grep_tool,
        write_tool.get_name(): write_tool,
        read_tool.get_name(): read_tool,
        list_tool.get_name(): list_tool,
        edit_tool.get_name(): edit_tool,
        web_search_tool.get_name(): web_search_tool,
        time_tool.get_name(): time_tool,
        read_excel_tool.get_name(): read_excel_tool,
        write_excel_tool.get_name(): write_excel_tool,
        ssh_tool.get_name(): ssh_tool,
    }

    media_exts = config.get("MEDIA_EXTS", []) or []
    media_limits = config.get("MEDIA_LIMITS", {}) or {}
    media_tools = []

    if ".pdf" in media_exts:
        read_pdf_tool = ReadPdfTool(
            workspace_dir=workspace_dir,
            host=host,
            session_dir_fn=lambda: session_manager.current_session_dir,
            media_limits=media_limits,
            file_exts=media_exts,
        )
        media_tools.append(read_pdf_tool)
        all_tools[read_pdf_tool.get_name()] = read_pdf_tool
    # End-if

    if any(ext != ".pdf" for ext in media_exts):
        read_image_tool = ReadImageTool(
            workspace_dir=workspace_dir,
            host=host,
            session_dir_fn=lambda: session_manager.current_session_dir,
            media_limits=media_limits,
            file_exts=media_exts,
        )
        media_tools.append(read_image_tool)
        all_tools[read_image_tool.get_name()] = read_image_tool
    # End-if

    for tool in all_tools.values():
        tool.runtime = runtime
    # End-for
    return all_tools, media_tools
# End-def


##
 # @brief Build an isolated SubAgentPool for a background job without constructing MyAgent.
 #
 # @param config Loaded model configuration.
 # @param client SafeLLMClient instance.
 # @param session_manager Isolated SessionManager for the worker job.
 # @param workspace_dir Target workspace directory.
 # @param runtime Worker-owned Runtime callbacks.
 #
 # @return Configured SubAgentPool instance.
 #
def build_worker_pool(config, client, session_manager, workspace_dir, runtime):
    skill_mgr = SkillManager(
        skill_dir=os.path.join(workspace_dir, "llm", "skill"),
        runtime=runtime,
    )
    all_tools, _ = _build_worker_catalog(
        workspace_dir=workspace_dir,
        config=config,
        skill_manager=skill_mgr,
        session_manager=session_manager,
        runtime=runtime,
        host=None,
    )
    return SubAgentPool(
        safe_client=client,
        logger=session_manager,
        config=config,
        all_tools=all_tools,
        max_depth=int(config.get("SUB_DEPTH", 1)),
        runtime=runtime,
    )
# End-def


##
 # @brief Build the full main-agent toolset and mount its synchronous SubAgentPool.
 #
 # @param agent MyAgent instance whose runtime, session and history are used.
 #
 # @return Main-agent tool dictionary; also assigns agent.pool.
 #
def build_tools(agent):
    all_tools, _ = _build_worker_catalog(
        workspace_dir=agent.workspace_dir,
        config=agent.config,
        skill_manager=agent.skill,
        session_manager=agent.session,
        runtime=agent.runtime,
        host=agent,
    )

    state_tool = StateTool(workspace_dir=agent.workspace_dir, session_manager=agent.session)
    memory_tool = MemoryTool(agent.memory)
    forget_tool = ForgetMemoryTool(agent.memory)
    prune_tool = PruneContextTool(agent)

    agent.pool = SubAgentPool(
        safe_client=agent.client,
        logger=agent.session,
        config=agent.config,
        all_tools=all_tools,
        max_depth=int(agent.config.get("SUB_DEPTH", 1)),
        runtime=agent.runtime,
    )

    plan_tool = PlanTool(agent.client, agent.config)
    spawn_subagent = SpawnSubagentTool(agent.pool) if agent.pool.max_depth > 0 else None
    agent.sub_concurrency = max(int(agent.config.get("SUB_CONCURRENCY", 4)), 1)

    tools = dict(all_tools)
    for extra in (plan_tool, state_tool, memory_tool, forget_tool, prune_tool):
        tools[extra.get_name()] = extra
    # End-for
    if spawn_subagent is not None:
        tools[spawn_subagent.get_name()] = spawn_subagent
    # End-if

    for tool in tools.values():
        tool.runtime = agent.runtime
    # End-for
    return tools
# End-def

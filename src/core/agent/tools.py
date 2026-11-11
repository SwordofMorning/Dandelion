##
 # @file src/core/agent/tools.py
 # @date 2026/10/27
 #
 # @brief Agent tool construction and delegation catalog assembly.
 # Creates filesystem, shell, memory, context-pruning, skill and media tools
 # for one agent or isolated worker pool, decoupling Main Agent media support
 # from per-SubAgent media capabilities.
 #

import os
from src.core.subagent import SubAgentPool
from src.core.agent.skill import SkillManager

from src.core.tool import (
    BashTool, LoadSkillTool, MarkdownTool,
    GrepSearchTool, WriteFileTool, ReadFileTool, ListDirectoryTool,
    EditFileTool, PlanTool, SpawnSubagentTool, WebSearchTool,
    ReadExcelTool, WriteExcelTool,
    StateTool, MemoryTool, ForgetMemoryTool, MemorySearchTool, PruneContextTool,
    SSHTool, TimeTool,
    ReadPdfTool, ReadImageTool
)


##
 # @brief Build worker and main-agent base tool catalogs.
 #
 # Worker catalog always includes ReadPdfTool and ReadImageTool templates so
 # SubAgent._rehost_media_tools() can enable or prune them per selected SubAgent
 # model_spec, even when the Main Agent itself is text-only.
 #
 # @return (worker_tools, main_base_tools) tuple.
 #
def _build_worker_catalog(workspace_dir, config, skill_manager, session_manager, runtime, host=None, memory_manager=None):
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

    common_tools = {
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
    if memory_manager is not None:
        ms_tool = MemorySearchTool(memory_manager)
        common_tools[ms_tool.get_name()] = ms_tool
    # End-if

    session_dir_fn = lambda: session_manager.current_session_dir
    media_exts = config.get("MEDIA_EXTS", []) or []
    media_limits = config.get("MEDIA_LIMITS", {}) or {}

    worker_tools = dict(common_tools)
    main_tools = dict(common_tools)

    worker_pdf = ReadPdfTool(
        workspace_dir=workspace_dir,
        host=host,
        session_dir_fn=session_dir_fn,
        media_limits=media_limits,
        file_exts=media_exts or None,
    )
    worker_image = ReadImageTool(
        workspace_dir=workspace_dir,
        host=host,
        session_dir_fn=session_dir_fn,
        media_limits=media_limits,
        file_exts=media_exts or None,
    )
    worker_tools[worker_pdf.get_name()] = worker_pdf
    worker_tools[worker_image.get_name()] = worker_image

    if ".pdf" in media_exts:
        main_tools[worker_pdf.get_name()] = worker_pdf
    # End-if
    if any(ext != ".pdf" for ext in media_exts):
        main_tools[worker_image.get_name()] = worker_image
    # End-if

    for tool in worker_tools.values():
        tool.runtime = runtime
    # End-for
    return worker_tools, main_tools
# End-def


##
 # @brief Build an isolated SubAgentPool for a background job without constructing MyAgent.
 #
def build_worker_pool(config, client, session_manager, workspace_dir, runtime, memory_manager=None):
    from src.core.agent.memory import MemoryManager
    if memory_manager is None:
        memory_manager = MemoryManager(
            os.path.join(workspace_dir, "llm", "memory"),
            session_manager=session_manager,
            safe_client=client,
            logger=session_manager,
            runtime=runtime,
            memory_config=config.get("MEMORY"),
        )
    # End-if
    skill_mgr = SkillManager(
        skill_dir=os.path.join(workspace_dir, "llm", "skill"),
        runtime=runtime,
    )
    worker_tools, _ = _build_worker_catalog(
        workspace_dir=workspace_dir,
        config=config,
        skill_manager=skill_mgr,
        session_manager=session_manager,
        runtime=runtime,
        host=None,
        memory_manager=memory_manager,
    )
    return SubAgentPool(
        safe_client=client,
        logger=session_manager,
        config=config,
        all_tools=worker_tools,
        max_depth=int(config.get("SUB_DEPTH", 1)),
        runtime=runtime,
    )
# End-def


##
 # @brief Build the full main-agent toolset and mount its synchronous SubAgentPool.
 #
def build_tools(agent):
    worker_tools, main_tools = _build_worker_catalog(
        workspace_dir=agent.workspace_dir,
        config=agent.config,
        skill_manager=agent.skill,
        session_manager=agent.session,
        runtime=agent.runtime,
        host=agent,
        memory_manager=agent.memory,
    )

    state_tool = StateTool(workspace_dir=agent.workspace_dir, session_manager=agent.session)
    memory_tool = MemoryTool(agent.memory)
    forget_tool = ForgetMemoryTool(agent.memory)
    prune_tool = PruneContextTool(agent)

    agent.pool = SubAgentPool(
        safe_client=agent.client,
        logger=agent.session,
        config=agent.config,
        all_tools=worker_tools,
        max_depth=int(agent.config.get("SUB_DEPTH", 1)),
        runtime=agent.runtime,
    )

    agent.sub_concurrency = max(int(agent.config.get("SUB_CONCURRENCY", 4)), 1)

    tools = dict(main_tools)
    for extra in (state_tool, memory_tool, forget_tool, prune_tool):
        tools[extra.get_name()] = extra
    # End-for

    has_subagents = (
        agent.pool.max_depth > 0
        and getattr(agent.client, "registry", None) is not None
        and bool(agent.client.registry.get_all_specs())
    )
    if has_subagents:
        plan_tool = PlanTool(agent.client.registry, caller_label="MainAgent")
        spawn_subagent = SpawnSubagentTool(agent.pool)
        tools[plan_tool.get_name()] = plan_tool
        tools[spawn_subagent.get_name()] = spawn_subagent
    # End-if

    for tool in tools.values():
        tool.runtime = agent.runtime
    # End-for
    return tools
# End-def

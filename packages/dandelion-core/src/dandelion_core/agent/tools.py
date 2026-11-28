##
 # @file packages/dandelion-core/src/dandelion_core/agent/tools.py
 # @date 2026/11/28
 #
 # @brief Agent tool construction, hot-plug discovery, and delegation catalog assembly.
 # Creates built-in shell, filesystem, memory, context-pruning, skill, and media
 # tools, merges zero-rebuild hot-plugged BaseTool extensions discovered via
 # ToolLoader, registers dynamic SubAgent toolsets into ToolsetRegistry, and
 # propagates sandbox protected_dirs onto every mounted tool.
 #

import os
from ..runtime import Runtime
from ..subagent import SubAgentPool
from ..subagent.registry import ToolsetRegistry
from .skill import SkillManager

from ..tool import (
    BashTool,
    LoadSkillTool,
    MarkdownTool,
    GrepSearchTool,
    WriteFileTool,
    ReadFileTool,
    ListDirectoryTool,
    EditFileTool,
    PlanTool,
    SpawnSubagentTool,
    WebSearchTool,
    ReadExcelTool,
    WriteExcelTool,
    StateTool,
    MemoryTool,
    ForgetMemoryTool,
    MemorySearchTool,
    PruneContextTool,
    SSHTool,
    TimeTool,
    ReadPdfTool,
    ReadImageTool,
    ToolContext,
    ToolLoader,
)


##
 # @brief Assemble built-in and hot-plugged worker/main tool catalogs with registry metadata.
 #
 # Worker catalog always includes ReadPdfTool and ReadImageTool templates so
 # SubAgent._rehost_media_tools() can enable or prune them per selected SubAgent
 # model_spec, even when the Main Agent itself is text-only.
 #
 # @param workspace_dir Workspace root directory.
 # @param config Loaded configuration dictionary.
 # @param skill_manager SkillManager instance.
 # @param session_manager SessionManager instance (optional in isolated probes).
 # @param runtime Runtime adapter instance.
 # @param host Host tag string ("cli", "remote", "core") or legacy media-host object.
 # @param memory_manager Optional MemoryManager instance.
 # @param config_dir Optional operator config directory (.env).
 # @param agent Optional MainAgent instance (media host and ToolContext.agent).
 # @param tool_dirs Optional ordered list of hot-plug plugin directories.
 # @param protected_dirs Optional iterable of sandbox-protected directory paths.
 # @param toolset_registry Optional ToolsetRegistry instance (mutated in place).
 #
 # @return (worker_tools, main_tools, toolset_registry, extension_tools, discovery_errors) tuple.
 #
def _assemble_catalog(
    workspace_dir,
    config,
    skill_manager,
    session_manager,
    runtime,
    host="core",
    memory_manager=None,
    config_dir=None,
    *,
    agent=None,
    tool_dirs=None,
    protected_dirs=None,
    toolset_registry=None,
):
    if host is None or isinstance(host, str):
        host_name = str(host or "core").strip().lower() or "core"
        media_host = agent
    else:
        media_host = host
        host_name = str(getattr(host, "host", "core") or "core").strip().lower() or "core"
    # End-if

    cfg = config if isinstance(config, dict) else (config or {})
    protected = tuple(protected_dirs or ())
    registry = toolset_registry if toolset_registry is not None else ToolsetRegistry()

    bash = BashTool(workspace_dir=workspace_dir)
    skill_loader = LoadSkillTool(skill_manager, workspace_dir=workspace_dir)
    md_editor = MarkdownTool(workspace_dir=workspace_dir)
    read_excel_tool = ReadExcelTool(workspace_dir=workspace_dir)
    write_excel_tool = WriteExcelTool(workspace_dir=workspace_dir)
    grep_tool = GrepSearchTool(workspace_dir=workspace_dir)
    write_tool = WriteFileTool(workspace_dir=workspace_dir)
    read_tool = ReadFileTool(workspace_dir=workspace_dir)
    list_tool = ListDirectoryTool(workspace_dir=workspace_dir)
    edit_tool = EditFileTool(workspace_dir=workspace_dir)
    web_search_tool = WebSearchTool(workspace_dir=workspace_dir, config=cfg)
    time_tool = TimeTool(workspace_dir=workspace_dir, config=cfg)
    ssh_tool = SSHTool(workspace_dir=workspace_dir, config_dir=config_dir)

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

    session_dir_fn = (
        (lambda: session_manager.current_session_dir)
        if session_manager is not None
        else None
    )
    media_exts = cfg.get("MEDIA_EXTS", []) or []
    media_limits = cfg.get("MEDIA_LIMITS", {}) or {}

    worker_tools = dict(common_tools)
    main_tools = dict(common_tools)

    worker_pdf = ReadPdfTool(
        workspace_dir=workspace_dir,
        host=media_host,
        session_dir_fn=session_dir_fn,
        media_limits=media_limits,
        file_exts=media_exts or None,
    )
    worker_image = ReadImageTool(
        workspace_dir=workspace_dir,
        host=media_host,
        session_dir_fn=session_dir_fn,
        media_limits=media_limits,
        file_exts=media_exts or None,
    )
    worker_tools[worker_pdf.get_name()] = worker_pdf
    worker_tools[worker_image.get_name()] = worker_image

    if ".pdf" in media_exts:
        main_tools[worker_pdf.get_name()] = worker_pdf
    # End-if
    if worker_image.supported_extensions() & set(media_exts):
        main_tools[worker_image.get_name()] = worker_image
    # End-if

    extension_tools = {}
    discovery_errors = []
    if tool_dirs:
        ctx = ToolContext(
            host=host_name,
            workspace_dir=workspace_dir,
            config=cfg,
            config_dir=config_dir,
            runtime=runtime,
            session_manager=session_manager,
            skill_manager=skill_manager,
            memory_manager=memory_manager,
            agent=media_host,
            protected_dirs=protected,
        )
        existing_names = set(worker_tools.keys()) | set(main_tools.keys())
        discovery = ToolLoader.discover(
            tool_dirs=tool_dirs,
            host=host_name,
            context=ctx,
            existing_tools=existing_names,
        )
        for name, tool in discovery.worker_tools.items():
            worker_tools[name] = tool
            registry.register_tool(name, discovery.toolset_bindings.get(name, ("full",)))
            extension_tools[name] = tool.get_description()
        # End-for
        for name, tool in discovery.main_tools.items():
            main_tools[name] = tool
            extension_tools[name] = tool.get_description()
        # End-for
        discovery_errors.extend(discovery.errors)
    # End-if

    for tool in worker_tools.values():
        tool.runtime = runtime
        tool.protected_dirs = protected
    # End-for
    for tool in main_tools.values():
        tool.runtime = runtime
        tool.protected_dirs = protected
    # End-for

    return worker_tools, main_tools, registry, extension_tools, discovery_errors
# End-def


##
 # @brief Build worker and main-agent base tool catalogs.
 #
 # @param workspace_dir Workspace root directory.
 # @param config Loaded configuration dictionary.
 # @param skill_manager SkillManager instance.
 # @param session_manager SessionManager instance.
 # @param runtime Runtime adapter instance.
 # @param host Host tag string ("cli", "remote", "core") or legacy media-host object.
 # @param memory_manager Optional MemoryManager instance.
 # @param config_dir Optional operator config directory (.env).
 # @param agent Optional MainAgent instance.
 # @param tool_dirs Optional ordered hot-plug plugin directories.
 # @param protected_dirs Optional iterable of sandbox-protected directories.
 # @param toolset_registry Optional ToolsetRegistry instance (mutated in place).
 #
 # @return (worker_tools, main_base_tools) tuple.
 #
def _build_worker_catalog(
    workspace_dir,
    config,
    skill_manager,
    session_manager,
    runtime,
    host="core",
    memory_manager=None,
    config_dir=None,
    *,
    agent=None,
    tool_dirs=None,
    protected_dirs=None,
    toolset_registry=None,
):
    worker_tools, main_tools, _, _, _ = _assemble_catalog(
        workspace_dir=workspace_dir,
        config=config,
        skill_manager=skill_manager,
        session_manager=session_manager,
        runtime=runtime,
        host=host,
        memory_manager=memory_manager,
        config_dir=config_dir,
        agent=agent,
        tool_dirs=tool_dirs,
        protected_dirs=protected_dirs,
        toolset_registry=toolset_registry,
    )
    return worker_tools, main_tools
# End-def


##
 # @brief Build an isolated SubAgentPool for a background job without constructing MyAgent.
 #
 # @param config Loaded configuration dictionary.
 # @param client SafeLLMClient instance (optional during lightweight toolset discovery).
 # @param session_manager SessionManager or logger (optional during lightweight discovery).
 # @param workspace_dir Workspace root directory.
 # @param runtime Runtime adapter (defaults to Runtime() when None).
 # @param memory_manager Optional MemoryManager instance.
 # @param config_dir Optional operator config directory (.env).
 # @param host Host tag string ("core", "cli", "remote").
 # @param tool_dirs Optional ordered hot-plug plugin directories.
 # @param protected_dirs Optional iterable of sandbox-protected directories.
 # @param toolset_registry Optional ToolsetRegistry instance.
 #
 # @return SubAgentPool bound to the discovered worker tools and ToolsetRegistry.
 #
def build_worker_pool(
    config,
    client=None,
    session_manager=None,
    workspace_dir=".",
    runtime=None,
    memory_manager=None,
    config_dir=None,
    *,
    host="core",
    tool_dirs=None,
    protected_dirs=None,
    toolset_registry=None,
):
    from .memory import MemoryManager

    cfg = config if isinstance(config, dict) else (config or {})
    effective_runtime = runtime if runtime is not None else Runtime()

    if memory_manager is None:
        memory_manager = MemoryManager(
            os.path.join(workspace_dir, "llm", "memory"),
            session_manager=session_manager,
            safe_client=client,
            logger=session_manager,
            runtime=effective_runtime,
            memory_config=cfg.get("MEMORY"),
        )
    # End-if
    skill_mgr = SkillManager(
        skill_dir=os.path.join(workspace_dir, "llm", "skill"),
        runtime=effective_runtime,
    )
    registry = toolset_registry if toolset_registry is not None else ToolsetRegistry()
    worker_tools, _, registry, extension_tools, discovery_errors = _assemble_catalog(
        workspace_dir=workspace_dir,
        config=cfg,
        skill_manager=skill_mgr,
        session_manager=session_manager,
        runtime=effective_runtime,
        host=host,
        memory_manager=memory_manager,
        config_dir=config_dir,
        agent=None,
        tool_dirs=tool_dirs,
        protected_dirs=protected_dirs,
        toolset_registry=registry,
    )
    pool = SubAgentPool(
        safe_client=client,
        logger=session_manager,
        config=cfg,
        all_tools=worker_tools,
        max_depth=int(cfg.get("SUB_DEPTH", 1)),
        runtime=effective_runtime,
        workspace_dir=workspace_dir,
        toolset_registry=registry,
    )
    pool.extension_tools = extension_tools
    pool.tool_load_errors = discovery_errors
    return pool
# End-def


##
 # @brief Build the full main-agent toolset and mount its synchronous SubAgentPool.
 #
 # @param agent Host MyAgent instance.
 # @param config_dir Optional operator config directory override.
 # @param toolset_registry Optional ToolsetRegistry instance.
 #
 # @return Mapping of tool_name -> BaseTool for the Main Agent.
 #
def build_tools(agent, config_dir=None, *, toolset_registry=None):
    effective_config_dir = config_dir if config_dir is not None else getattr(agent, "config_dir", None)
    host_name = getattr(agent, "host", "core")
    tool_dirs = getattr(agent, "tool_dirs", None)
    protected = tuple(getattr(agent, "protected_dirs", ()) or ())
    registry = toolset_registry if toolset_registry is not None else ToolsetRegistry()

    worker_tools, main_tools, registry, extension_tools, discovery_errors = _assemble_catalog(
        workspace_dir=agent.workspace_dir,
        config=agent.config,
        skill_manager=agent.skill,
        session_manager=agent.session,
        runtime=agent.runtime,
        host=host_name,
        memory_manager=agent.memory,
        config_dir=effective_config_dir,
        agent=agent,
        tool_dirs=tool_dirs,
        protected_dirs=protected,
        toolset_registry=registry,
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
        workspace_dir=agent.workspace_dir,
        toolset_registry=registry,
    )
    agent.toolset_registry = registry
    agent.extension_tools = dict(extension_tools)
    agent.tool_load_errors = list(discovery_errors)

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
        plan_tool = PlanTool(
            agent.client.registry,
            workspace_dir=agent.workspace_dir,
            caller_label="MainAgent",
            toolset_registry=registry,
        )
        spawn_subagent = SpawnSubagentTool(agent.pool)
        tools[plan_tool.get_name()] = plan_tool
        tools[spawn_subagent.get_name()] = spawn_subagent
    # End-if

    for tool in tools.values():
        tool.runtime = agent.runtime
        tool.protected_dirs = protected
    # End-for
    return tools
# End-def

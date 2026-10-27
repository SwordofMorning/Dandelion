"""Build fresh tool instances for one agent/session runtime.

Callers may independently restrict the main-agent tools and the worker catalog.
No mutable agent or session instances are shared between remote jobs.
"""

from src.subagent import SubAgentPool

from src.tool import (
    BashTool, LoadSkillTool, MarkdownTool,
    GrepSearchTool, WriteFileTool, ReadFileTool, ListDirectoryTool,
    EditFileTool, PlanTool, SpawnSubagentTool, WebSearchTool,
    ReadExcelTool, WriteExcelTool,
    StateTool, MemoryTool,
    SSHTool, TimeTool,
    ReadPdfTool, ReadImageTool
)


def _select_tools(tools, names):
    names = tuple(names)
    unknown = set(names) - tools.keys()
    if unknown:
        raise ValueError("Unknown or unavailable tools: " + ", ".join(sorted(unknown)))
    return {name: tools[name] for name in names}


def build_tools(agent, main_tool_names=None, worker_tool_names=None):
    """Return the CLI toolset by default; mount a separate worker catalog.

    Also assigns ``agent.pool`` for synchronous delegation. A background caller
    must construct a new agent/session rather than retaining the main pool.
    """
    # ----- @par 1. Create Tools Object -----

    # Pass BASE_DIR to all file-system related tools
    # Bash maintains its own command checking
    bash = BashTool(workspace_dir=agent.workspace_dir)
    skill_loader = LoadSkillTool(agent.skill)
    # Editor
    md_editor = MarkdownTool(workspace_dir=agent.workspace_dir)
    read_excel_tool = ReadExcelTool(workspace_dir=agent.workspace_dir)
    write_excel_tool = WriteExcelTool(workspace_dir=agent.workspace_dir)
    # FS
    grep_tool = GrepSearchTool(workspace_dir=agent.workspace_dir)
    write_tool = WriteFileTool(workspace_dir=agent.workspace_dir)
    read_tool = ReadFileTool(workspace_dir=agent.workspace_dir)
    list_tool = ListDirectoryTool(workspace_dir=agent.workspace_dir)
    edit_tool = EditFileTool(workspace_dir=agent.workspace_dir)
    # Others
    web_search_tool = WebSearchTool(workspace_dir=agent.workspace_dir, config=agent.config)
    time_tool = TimeTool(workspace_dir=agent.workspace_dir, config=agent.config)
    # Remote
    ssh_tool = SSHTool(workspace_dir=agent.workspace_dir)
    # Memory
    state_tool = StateTool(workspace_dir=agent.workspace_dir, session_manager=agent.session)
    memory_tool = MemoryTool(agent.memory)

    # Create full tools mapping for Orchestrator
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
        ssh_tool.get_name(): ssh_tool
    }

    # ----- @par 1-b. Multimodal (media) Tools -----

    # Registered ONLY when the active model declares media support through
    # its "file" whitelist. A text-only model never sees the media tools.
    media_exts = agent.config.get("MEDIA_EXTS", []) or []
    media_limits = agent.config.get("MEDIA_LIMITS", {}) or {}
    media_tools = []

    if ".pdf" in media_exts:
        read_pdf_tool = ReadPdfTool(
            workspace_dir=agent.workspace_dir,
            host=agent,
            session_dir_fn=lambda: agent.session.current_session_dir,
            media_limits=media_limits,
            file_exts=media_exts,
        )
        media_tools.append(read_pdf_tool)
        all_tools[read_pdf_tool.get_name()] = read_pdf_tool
    # End-if

    if any(ext != ".pdf" for ext in media_exts):
        read_image_tool = ReadImageTool(
            workspace_dir=agent.workspace_dir,
            host=agent,
            session_dir_fn=lambda: agent.session.current_session_dir,
            media_limits=media_limits,
            file_exts=media_exts,
        )
        media_tools.append(read_image_tool)
        all_tools[read_image_tool.get_name()] = read_image_tool
    # End-if

    # ----- @par 2. Subagent Pool and Tools -----

    if worker_tool_names is not None:
        all_tools = _select_tools(all_tools, worker_tool_names)
    for tool in all_tools.values():
        tool.runtime = agent.runtime

    agent.pool = SubAgentPool(
        safe_client=agent.client,
        logger=agent.session,
        config=agent.config,
        all_tools=all_tools,
        max_depth=int(agent.config.get("SUB_DEPTH", 1)),
        runtime=agent.runtime
    )

    # Decompose one descriptions to multi (or one) tasks.
    plan_tool = PlanTool(agent.client, agent.config)
    # Spawn a new subagent (only when SubAgents are enabled: SUB_DEPTH > 0).
    spawn_subagent = SpawnSubagentTool(agent.pool) if agent.pool.max_depth > 0 else None
    # Max SubAgents running at once for parallel spawn calls in one response.
    agent.sub_concurrency = max(int(agent.config.get("SUB_CONCURRENCY", 4)), 1)

    # ----- @par 3. Register Tools  -----

    # Added all tools to the registration list
    tool_list = [
        bash, skill_loader, md_editor,
        grep_tool, write_tool, read_tool, list_tool,
        edit_tool, plan_tool, web_search_tool,
        read_excel_tool, write_excel_tool,
        state_tool, memory_tool,
        ssh_tool, time_tool
    ]
    # Media tools join the main-agent toolset only when registered above.
    tool_list.extend(media_tools)
    if spawn_subagent is not None:
        tool_list.append(spawn_subagent)
    # End-if

    tools = {tool.get_name(): tool for tool in tool_list}
    if main_tool_names is not None:
        tools = _select_tools(tools, main_tool_names)
    for tool in tools.values():
        tool.runtime = agent.runtime
    return tools

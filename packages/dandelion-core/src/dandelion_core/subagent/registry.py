##
 # @file packages/dandelion-core/src/dandelion_core/subagent/registry.py
 # @date 2026/11/28
 #
 # @brief Named toolset catalog for delegated workers.
 # Provides the instance-scoped ToolsetRegistry used for dynamic hot-plugged
 # tool registration while keeping module-level TOOLSET_REGISTRY and
 # resolve_toolset() intact for backward compatibility.
 #

DEFAULT_TOOLSETS = {
    "minimal": ("read_file", "write_file", "list_directory", "get_time"),
    "filesystem": (
        "read_file",
        "write_file",
        "list_directory",
        "grep_search",
        "markdown_editor",
        "edit_file",
        "load_skill",
        "read_pdf",
        "read_image",
    ),
    "code_analysis": (
        "read_file",
        "grep_search",
        "list_directory",
        "bash",
        "ssh",
        "tavily_search",
        "memory_search",
    ),
    "data_processing": (
        "read_excel",
        "write_excel",
        "write_file",
        "markdown_editor",
        "load_skill",
    ),
    "full": (
        "bash",
        "ssh",
        "read_file",
        "write_file",
        "list_directory",
        "grep_search",
        "markdown_editor",
        "edit_file",
        "read_excel",
        "write_excel",
        "get_time",
        "load_skill",
        "tavily_search",
        "read_pdf",
        "read_image",
        "memory_search",
    ),
}

# Backward-compatible default view for existing imports and tests.
TOOLSET_REGISTRY = {k: list(v) for k, v in DEFAULT_TOOLSETS.items()}


##
 # @brief Instance-scoped registry mapping SubAgent toolset names to tool lists.
 #
class ToolsetRegistry:
    ##
     # @brief Constructor.
     #
     # @param base_toolsets Optional mapping of toolset_name -> iterable of tool names.
     #        Defaults to DEFAULT_TOOLSETS when omitted.
     #
     # @return None.
     #
    def __init__(self, base_toolsets=None):
        src = DEFAULT_TOOLSETS if base_toolsets is None else base_toolsets
        self._toolsets = {str(k): list(v) for k, v in src.items()}
    # End-def

    ##
     # @brief Register a tool name into one or more named toolsets.
     #
     # Creates custom toolsets on demand when they do not already exist.
     #
     # @param tool_name Registered tool identifier string.
     # @param toolsets Iterable (or single string) of target toolset names.
     #
     # @return None.
     #
    def register_tool(self, tool_name: str, toolsets=("full",)) -> None:
        name = str(tool_name or "").strip()
        if not name:
            return
        # End-if
        if isinstance(toolsets, str):
            targets = (toolsets,)
        else:
            targets = tuple(toolsets) if toolsets else ("full",)
        # End-if
        for ts in targets:
            ts_name = str(ts or "").strip()
            if not ts_name:
                continue
            # End-if
            members = self._toolsets.setdefault(ts_name, [])
            if name not in members:
                members.append(name)
            # End-if
        # End-for
    # End-def

    ##
     # @brief Return ordered registered toolset names.
     #
     # @return List of toolset name strings.
     #
    def names(self) -> list:
        return list(self._toolsets.keys())
    # End-def

    ##
     # @brief Check whether a toolset name is registered.
     #
     # @param toolset_name Toolset name to test.
     #
     # @return True if present, False otherwise.
     #
    def has(self, toolset_name: str) -> bool:
        return toolset_name in self._toolsets
    # End-def

    ##
     # @brief Return a copy of the ordered tool names belonging to a toolset.
     #
     # @param toolset_name Registered toolset name.
     #
     # @return List of tool name strings.
     #
    def members(self, toolset_name: str) -> list:
        return list(self._toolsets[toolset_name])
    # End-def

    ##
     # @brief Resolve a toolset name against mounted worker tools and parent bounds.
     #
     # @param toolset_name Target toolset name.
     # @param all_tools Mapping of tool_name -> BaseTool mounted in the worker pool.
     # @param parent_tools Optional set of logical tool names allowed by parent SubAgent.
     #
     # @return Filtered mapping of tool_name -> BaseTool.
     #
    def resolve(self, toolset_name: str, all_tools: dict, parent_tools: set = None) -> dict:
        if toolset_name not in self._toolsets:
            raise ValueError(
                f"Unknown toolset: '{toolset_name}'. "
                f"Available toolsets are: {self.names()}"
            )
        # End-if
        tool_names = self._toolsets[toolset_name]
        if parent_tools is not None:
            tool_names = [n for n in tool_names if n in parent_tools]
        # End-if
        resolved = {}
        for name in tool_names:
            if name in all_tools:
                resolved[name] = all_tools[name]
            # End-if
        # End-for
        return resolved
    # End-def

    ##
     # @brief Format registered toolsets as bullet lines for system prompts.
     #
     # @param available_tools Optional iterable restricting displayed tool names.
     #
     # @return Newline-joined toolset description lines.
     #
    def format_prompt_lines(self, available_tools=None) -> str:
        avail_set = set(available_tools) if available_tools is not None else None
        lines = []
        for ts_name, ts_tools in self._toolsets.items():
            visible = [t for t in ts_tools if avail_set is None or t in avail_set]
            if visible:
                lines.append(f"- '{ts_name}': {', '.join(visible)}")
            # End-if
        # End-for
        return "\n".join(lines)
    # End-def

    ##
     # @brief Return an independent deep copy of this ToolsetRegistry.
     #
     # @return New ToolsetRegistry instance.
     #
    def clone(self):
        return ToolsetRegistry(self._toolsets)
    # End-def

    ##
     # @brief Support membership tests (`name in registry`).
     #
     # @param toolset_name Toolset name to test.
     #
     # @return True when toolset_name is registered.
     #
    def __contains__(self, toolset_name: str) -> bool:
        return self.has(toolset_name)
    # End-def

    ##
     # @brief Iterate registered toolset names in declaration order.
     #
     # @return Iterator over toolset name strings.
     #
    def __iter__(self):
        return iter(self._toolsets.keys())
    # End-def
# End-class


##
 # @brief Parse from LLM's response to local toolset using module-level TOOLSET_REGISTRY.
 #
 # @param toolset_name Toolset name in TOOLSET_REGISTRY.
 # @param all_tools Mapping of all mounted worker tools.
 # @param parent_tools Optional parent toolset name set.
 #
 # @return Resolved mapping of tool_name -> BaseTool.
 #
def resolve_toolset(toolset_name: str, all_tools: dict, parent_tools: set = None) -> dict:
    # Let LLM know which toolsets actually exist.
    if toolset_name not in TOOLSET_REGISTRY:
        raise ValueError(
            f"Unknown toolset: '{toolset_name}'. "
            f"Available toolsets are: {list(TOOLSET_REGISTRY.keys())}"
        )
    # End-if

    tool_names = TOOLSET_REGISTRY[toolset_name]

    if parent_tools is not None:
        tool_names = [n for n in tool_names if n in parent_tools]
    # End-if

    resolved = {}
    for name in tool_names:
        if name in all_tools:
            resolved[name] = all_tools[name]
        # End-if
    # End-for

    return resolved
# End-def

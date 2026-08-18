##
 # @file src/subagent/registry.py
 # @date 2026/08/07
 # 
 # @brief The set of pre-defined tools that the management sub-agent can acquire.
 #

TOOLSET_REGISTRY = {
    "minimal": ["read_file", "write_file", "list_directory"],
    "filesystem": ["read_file", "write_file", "list_directory", "grep_search", "markdown_editor", "edit_file"],
    "code_analysis": ["read_file", "grep_search", "list_directory", "bash", "ssh"],
    "data_processing": ["read_excel", "write_excel", "write_file", "markdown_editor"],
    "full": ["bash", "ssh", "read_file", "write_file", "list_directory", "grep_search", "markdown_editor", "edit_file", "read_excel", "write_excel"]
}

##
 # @brief Parse from LLM's response to local toolset.
 #
 # @param toolset_name toolset name in TOOLSET_REGISTRY.
 # @param all_tools all tools.
 # @param parent_tools parent's toolset.
 #
 # @return toolset.
 #
def resolve_toolset(toolset_name: str, all_tools: dict, parent_tools: set = None) -> dict:
    # Let LLM know which tool are really existed.
    if toolset_name not in TOOLSET_REGISTRY:
        raise ValueError(
            f"Unknown toolset: '{toolset_name}'. "
            f"Available toolsets are: {list(TOOLSET_REGISTRY.keys())}"
        )

    tool_names = TOOLSET_REGISTRY[toolset_name]

    if parent_tools is not None:
        tool_names = [n for n in tool_names if n in parent_tools]

    resolved = {}
    for name in tool_names:
        if name in all_tools:
            resolved[name] = all_tools[name]

    return resolved
# End-def
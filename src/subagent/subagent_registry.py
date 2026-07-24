# src/tool/agent/registry.py

# Brief: The set of pre-defined tools that the management sub-agent can acquire.

TOOLSET_REGISTRY = {
    "minimal": ["read_file", "write_file", "list_directory"],
    "filesystem": ["read_file", "write_file", "list_directory", "grep_search", "markdown_editor", "edit_file"],
    "code_analysis": ["read_file", "grep_search", "list_directory", "bash"],
    "data_processing": ["read_weekly_report", "write_file", "markdown_editor"],
    "full": ["bash", "read_file", "write_file", "list_directory", "grep_search", "markdown_editor", "edit_file"]
}

def resolve_toolset(toolset_name: str, all_tools: dict, parent_tools: set = None) -> dict:
    tool_names = TOOLSET_REGISTRY.get(toolset_name)
    if tool_names is None:
        tool_names = TOOLSET_REGISTRY.get("minimal", [])
    
    if parent_tools is not None:
        tool_names = [n for n in tool_names if n in parent_tools]
        
    resolved = {}
    for name in tool_names:
        if name in all_tools:
            resolved[name] = all_tools[name]
            
    return resolved
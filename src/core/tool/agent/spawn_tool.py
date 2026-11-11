##
 # @file src/core/tool/agent/spawn_tool.py
 # @date 2026/08/13
 #
 # @brief Synchronous SubAgent delegation tools with LLM-driven model selection.
 # Exposes configured SubAgent names and descriptions directly in the tool
 # definition so the calling Agent LLM chooses the target model explicitly.
 #

from ..base_tool import BaseTool
from ...subagent.registry import TOOLSET_REGISTRY


##
 # @brief Build the 'model' property schema from a ModelRegistry.
 #
def _build_model_property(registry):
    aliases = registry.aliases() if registry else []
    prop = {
        "type": "string",
        "description": (
            "Name of the SubAgent model to execute this sub-task. "
            "Select according to the model descriptions in this tool."
        ),
    }
    if aliases:
        prop["enum"] = aliases
    # End-if
    return prop
# End-def


##
 # @brief Append configured SubAgent catalog lines to a base tool description.
 #
def _append_model_catalog(base_desc: str, registry) -> str:
    catalog = registry.format_catalog() if registry else ""
    if not catalog:
        return base_desc
    # End-if
    return f"{base_desc}\nAvailable SubAgent models:\n{catalog}"
# End-def


##
 # @brief Root SubAgent delegation tool (installed on Main Agent when SUB_DEPTH > 0).
 #
class SpawnSubagentTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param pool SubAgentPool instance.
     #
    def __init__(self, pool):
        super().__init__()
        self.pool = pool
        self.runtime = getattr(pool, "runtime", None)
    # End-def

    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "spawn_subagent"
    # End-def

    ##
     # @brief Return tool's description including configured SubAgent descriptions.
     #
    def get_description(self):
        base = (
            "Spawn a dedicated SubAgent using the selected 'model' to handle a specific "
            "sub-task in an isolated context and return a structured summary. "
            "Several calls in the same response run in parallel; give parallel SubAgents "
            "independent tasks that do not modify the same files."
        )
        registry = getattr(self.pool.safe_client, "registry", None)
        return _append_model_catalog(base, registry)
    # End-def

    ##
     # @brief Return tool's JSON input schema.
     #
    def get_schema(self):
        registry = getattr(self.pool.safe_client, "registry", None)
        return {
            "type": "object",
            "properties": {
                "model": _build_model_property(registry),
                "task_description": {
                    "type": "string",
                    "description": "Detailed natural-language description of the sub-task.",
                },
                "toolset": {
                    "type": "string",
                    "enum": list(TOOLSET_REGISTRY.keys()),
                    "description": "Name of the pre-defined toolset for this subagent.",
                },
                "role_prompt": {
                    "type": "string",
                    "description": "Custom system prompt defining the subagent's role and behavior.",
                },
                "expected_output_format": {
                    "type": "string",
                    "description": "Optional hint about the expected output format.",
                },
            },
            "required": ["model", "task_description", "toolset", "role_prompt"],
        }
    # End-def

    ##
     # @brief Execute SubAgent delegation at depth=1.
     #
    def execute(self, **kwargs):
        model = str(kwargs.get("model") or "").strip()
        task_desc = kwargs.get("task_description", "")
        toolset = kwargs.get("toolset", "minimal")
        role_prompt = kwargs.get("role_prompt", "")
        expected_format = kwargs.get("expected_output_format", "")

        if not model or not task_desc or not role_prompt:
            return False, "Error: model, task_description, and role_prompt are required."
        # End-if

        if expected_format:
            task_desc += f"\n\nExpected Output Format:\n{expected_format}"
        # End-if

        self._emit(
            f"\n[+] [SpawnSubagentTool] Spawning '{model}' for: {task_desc[:60]}..."
        )

        result = self.pool.create_and_run(
            role_prompt=role_prompt,
            toolset_name=toolset,
            task_description=task_desc,
            model=model,
            depth=1,
        )

        return result.status != "failed", result.to_context_string()
    # End-def
# End-class


##
 # @brief Restricted SubAgent delegation tool for nested SubAgents (depth < max_depth).
 #
class RestrictedSpawnTool(BaseTool):
    ##
     # @brief Constructor.
     #
    def __init__(self, pool, parent_subagent, current_depth, max_depth):
        super().__init__()
        self.pool = pool
        self.runtime = getattr(pool, "runtime", None)
        self.parent = parent_subagent
        self.current_depth = current_depth
        self.max_depth = max_depth
    # End-def

    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "spawn_subagent"
    # End-def

    ##
     # @brief Return tool's description including depth and SubAgent descriptions.
     #
    def get_description(self):
        base = (
            f"Spawn a nested SubAgent using the selected 'model' to handle a narrower "
            f"sub-task (current depth: {self.current_depth}/{self.max_depth}). "
            f"Consecutive calls in one response run in parallel when not already inside a "
            f"parallel worker; assign disjoint files to parallel SubAgents."
        )
        registry = getattr(self.pool.safe_client, "registry", None)
        return _append_model_catalog(base, registry)
    # End-def

    ##
     # @brief Return tool's JSON input schema.
     #
    def get_schema(self):
        registry = getattr(self.pool.safe_client, "registry", None)
        return {
            "type": "object",
            "properties": {
                "model": _build_model_property(registry),
                "task_description": {
                    "type": "string",
                    "description": "Detailed natural-language description of the sub-task.",
                },
                "toolset": {
                    "type": "string",
                    "enum": list(TOOLSET_REGISTRY.keys()),
                    "description": "Name of the pre-defined toolset for this nested subagent.",
                },
                "role_prompt": {
                    "type": "string",
                    "description": "Custom system prompt defining the nested subagent's role.",
                },
                "expected_output_format": {
                    "type": "string",
                    "description": "Optional hint about the expected output format.",
                },
            },
            "required": ["model", "task_description", "toolset", "role_prompt"],
        }
    # End-def

    ##
     # @brief Execute nested SubAgent delegation at depth = current_depth + 1.
     #
    def execute(self, **kwargs):
        if self.current_depth >= self.max_depth:
            return False, (
                f"Error: Maximum recursion depth ({self.max_depth}) reached. "
                f"You must complete this task yourself without further delegation."
            )
        # End-if

        model = str(kwargs.get("model") or "").strip()
        task_desc = kwargs.get("task_description", "")
        toolset = kwargs.get("toolset", "minimal")
        role_prompt = kwargs.get("role_prompt", "")
        expected_format = kwargs.get("expected_output_format", "")

        if not model or not task_desc or not role_prompt:
            return False, "Error: model, task_description, and role_prompt are required."
        # End-if

        if expected_format:
            task_desc += f"\n\nExpected Output Format:\n{expected_format}"
        # End-if

        parent_tools = set(getattr(self.parent, "logical_tool_names", self.parent.tools.keys()))

        self._emit(
            f"\n[+] [RestrictedSpawnTool] Spawning '{model}' (depth {self.current_depth + 1}) "
            f"for: {task_desc[:60]}..."
        )

        result = self.pool.create_and_run(
            role_prompt=role_prompt,
            toolset_name=toolset,
            task_description=task_desc,
            model=model,
            depth=self.current_depth + 1,
            parent_tools=parent_tools,
        )

        if hasattr(self.parent, "sub_results"):
            self.parent.sub_results.append(result)
        # End-if

        return result.status != "failed", result.to_context_string()
    # End-def
# End-class

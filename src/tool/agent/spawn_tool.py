##
 # @file src/tool/agent/spawn_tool.py
 # @date 2026/08/13
 # 
 # @brief fork() a new subagent like threads.
 #
 # @note Spawn call chain:
 #   Root agent (src/core/agent.py)
 #     -> SpawnSubagentTool(pool)               # depth=0, no parent
 #       -> pool.create_and_run(role_prompt, toolset_name, task_description, depth=0)
 #         -> resolve_toolset(toolset_name, all_tools, parent_tools=None)
 #         -> SubAgent.run(task_description)    # subagent tool loop
 #           -> depth < max_depth: inject RestrictedSpawnTool
 #              (src/subagent/subagent.py)      # recursive delegation
 #             -> RestrictedSpawnTool.execute()
 #               -> pool.create_and_run(..., depth+1, parent_tools=parent toolset)
 #                 -> SubSubAgent ...          # recursion capped by max_depth
 #               -> parent.sub_results.append(result)
 #           -> SubAgentResult.to_context_string()  # tool result back to LLM
 #

from ..base_tool import BaseTool
from ...subagent.registry import TOOLSET_REGISTRY

##
 # @brief Fork Subagent Class.
 #
class SpawnSubagentTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param pool Subagent pool
     #
     # @see src/subagent/pool.py
     #
    def __init__(self, pool):
        super().__init__()
        self.pool = pool
    # End-def

    def get_name(self):
        return "spawn_subagent"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return (
            "Spawn a dedicated SubAgent to handle a specific sub-task. "
            "The SubAgent runs in an isolated context and returns a structured summary. "
            "Use this for complex, self-contained sub-tasks to keep the main context clean."
        )
    # End-def

    ##
     # @brief Return tool's schema.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "task_description": {
                    "type": "string",
                    "description": "Detailed natural-language description of the sub-task."
                },
                "toolset": {
                    "type": "string",
                    "enum": list(TOOLSET_REGISTRY.keys()),
                    "description": "Name of the pre-defined toolset for this subagent."
                },
                "role_prompt": {
                    "type": "string",
                    "description": "Custom system prompt defining the subagent's role and behavior."
                },
                "expected_output_format": {
                    "type": "string",
                    "description": "Optional hint about the expected output format."
                }
            },
            "required": ["task_description", "toolset", "role_prompt"]
        }
    # End-def

    ##
     # @brief Execute spawn.
     #
     # @param kwargs schema properties: task_description, toolset,
     # role_prompt, expected_output_format.
     #
     # @return (success_bool, result_string)
     #
    def execute(self, **kwargs):
        task_desc = kwargs.get("task_description", "")
        toolset = kwargs.get("toolset", "minimal")
        role_prompt = kwargs.get("role_prompt", "")
        expected_format = kwargs.get("expected_output_format", "")
        
        if not task_desc or not role_prompt:
            return False, "Error: task_description and role_prompt are required."
            
        if expected_format:
            task_desc += f"\n\nExpected Output Format:\n{expected_format}"
            
        print(f"\n[+] [SpawnSubagentTool] Spawning SubAgent for: {task_desc[:60]}...")
        
        result = self.pool.create_and_run(
            role_prompt=role_prompt,
            toolset_name=toolset,
            task_description=task_desc,
            depth=0
        )
        
        return True, result.to_context_string()
    # End-def

##
 # @brief Restricted Fork Class (for recursive subagents).
 #
 # @note Two spawn classes exist by design:
 # - SpawnSubagentTool: installed in the ROOT agent (src/core/agent.py),
 #   spawns depth-0 subagents without parent restrictions.
 # - RestrictedSpawnTool: installed in SUBAGENTS only when
 #   depth < max_depth (src/subagent/subagent.py), so recursive delegation
 #   is depth-limited and parent-aware.
 #
 # @see src/subagent/subagent.py (installer), src/subagent/pool.py
 #
class RestrictedSpawnTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param pool Subagent pool.
     # @param parent_subagent Parent subagent instance (owner of this tool).
     # @param current_depth Current recursion depth of the parent.
     # @param max_depth Maximum allowed recursion depth.
     #
    def __init__(self, pool, parent_subagent, current_depth, max_depth):
        super().__init__()
        self.pool = pool
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
     # @brief Return tool's description.
     #
    def get_description(self):
        return (
            f"Spawn a SubSubAgent to handle a specialized sub-task. "
            f"Current depth: {self.current_depth}/{self.max_depth}. "
            f"You may only delegate tasks that are narrower in scope than your own."
        )
    # End-def

    ##
     # @brief Return tool's schema.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "task_description": {
                    "type": "string",
                    "description": "Detailed natural-language description of the sub-task."
                },
                "toolset": {
                    "type": "string",
                    "enum": list(TOOLSET_REGISTRY.keys()),
                    "description": "Name of the pre-defined toolset for this subagent."
                },
                "role_prompt": {
                    "type": "string",
                    "description": "Custom system prompt defining the subagent's role and behavior."
                },
                "expected_output_format": {
                    "type": "string",
                    "description": "Optional hint about the expected output format."
                }
            },
            "required": ["task_description", "toolset", "role_prompt"]
        }
    # End-def

    ##
     # @brief Execute restricted spawn.
     #
     # @param kwargs schema properties: task_description, toolset,
     # role_prompt, expected_output_format.
     #
     # @note Enforces the recursion depth limit (current_depth >= max_depth
     # is rejected) and passes the parent's toolset as parent_tools so the
     # child can only use a subset of the parent's tools.
     #
     # @return (success_bool, result_string)
     #
    def execute(self, **kwargs):
        if self.current_depth >= self.max_depth:
            return False, (
                f"Error: Maximum recursion depth ({self.max_depth}) reached. "
                f"You must complete this task yourself without further delegation."
            )

        task_desc = kwargs.get("task_description", "")
        toolset = kwargs.get("toolset", "minimal")
        role_prompt = kwargs.get("role_prompt", "")
        expected_format = kwargs.get("expected_output_format", "")
        
        if not task_desc or not role_prompt:
            return False, "Error: task_description and role_prompt are required."
            
        if expected_format:
            task_desc += f"\n\nExpected Output Format:\n{expected_format}"
            
        parent_tools = set(self.parent.tools.keys())
        
        print(f"\n[+] [RestrictedSpawnTool] Spawning SubSubAgent (depth {self.current_depth+1}) for: {task_desc[:60]}...")
        
        result = self.pool.create_and_run(
            role_prompt=role_prompt,
            toolset_name=toolset,
            task_description=task_desc,
            depth=self.current_depth + 1,
            parent_tools=parent_tools
        )
        
        if hasattr(self.parent, "sub_results"):
            self.parent.sub_results.append(result)
            
        return True, result.to_context_string()
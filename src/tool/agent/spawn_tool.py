# src/tool/agent/spawn_tool.py

# Brief: Provides a full version of the Main Agent and a limited version of the spawn tool for sub-agents.

from ..base_tool import BaseTool
from .subagent_registry import TOOLSET_REGISTRY

class SpawnSubagentTool(BaseTool):
    def __init__(self, orchestrator):
        super().__init__()
        self.orchestrator = orchestrator

    def get_name(self):
        return "spawn_subagent"

    def get_description(self):
        return (
            "Spawn a dedicated SubAgent to handle a specific sub-task. "
            "The SubAgent runs in an isolated context and returns a structured summary. "
            "Use this for complex, self-contained sub-tasks to keep the main context clean."
        )

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

    def execute(self, **kwargs):
        task_desc = kwargs.get("task_description", "")
        toolset = kwargs.get("toolset", "minimal")
        role_prompt = kwargs.get("role_prompt", "")
        
        if not task_desc or not role_prompt:
            return False, "Error: task_description and role_prompt are required."
            
        print(f"\n[+] [SpawnSubagentTool] Spawning SubAgent for: {task_desc[:60]}...")
        
        result = self.orchestrator.create_and_run(
            role_prompt=role_prompt,
            toolset_name=toolset,
            task_description=task_desc,
            depth=0
        )
        
        return True, result.to_context_string()

class RestrictedSpawnTool(BaseTool):
    def __init__(self, orchestrator, parent_subagent, current_depth, max_depth):
        super().__init__()
        self.orchestrator = orchestrator
        self.parent = parent_subagent
        self.current_depth = current_depth
        self.max_depth = max_depth

    def get_name(self):
        return "spawn_subagent"

    def get_description(self):
        return (
            f"Spawn a SubSubAgent to handle a specialized sub-task. "
            f"Current depth: {self.current_depth}/{self.max_depth}. "
            f"You may only delegate tasks that are narrower in scope than your own."
        )

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
                }
            },
            "required": ["task_description", "toolset", "role_prompt"]
        }

    def execute(self, **kwargs):
        if self.current_depth >= self.max_depth:
            return False, (
                f"Error: Maximum recursion depth ({self.max_depth}) reached. "
                f"You must complete this task yourself without further delegation."
            )

        task_desc = kwargs.get("task_description", "")
        toolset = kwargs.get("toolset", "minimal")
        role_prompt = kwargs.get("role_prompt", "")
        
        if not task_desc or not role_prompt:
            return False, "Error: task_description and role_prompt are required."
            
        parent_tools = set(self.parent.tools.keys())
        
        print(f"\n[+] [RestrictedSpawnTool] Spawning SubSubAgent (depth {self.current_depth+1}) for: {task_desc[:60]}...")
        
        result = self.orchestrator.create_and_run(
            role_prompt=role_prompt,
            toolset_name=toolset,
            task_description=task_desc,
            depth=self.current_depth + 1,
            parent_tools=parent_tools
        )
        
        if hasattr(self.parent, "sub_results"):
            self.parent.sub_results.append(result)
            
        return True, result.to_context_string()
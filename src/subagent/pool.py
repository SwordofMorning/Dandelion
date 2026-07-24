# src/subagent/pool.py

# Brief: Control all subagent.py
# A global singleton, held by MyAgent, used to maintaining and managing all derived SubAgents. 
# When you need to create a new SubAgent, this scheduler assigns an ID, mounts specific toolsets,
# and tracks their execution status.

import uuid
from .subagent import SubAgent
from .result import SubAgentResult
from .registry import resolve_toolset

class SubAgentPool:
    def __init__(self, safe_client, logger, config, all_tools, max_depth=3):
        self.safe_client = safe_client
        self.logger = logger
        self.config = config
        self.all_tools = all_tools
        self.max_depth = max_depth
        self.completed_results = []
        
    def create_and_run(
        self,
        role_prompt: str,
        toolset_name: str,
        task_description: str,
        depth: int = 0,
        parent_tools: set = None
    ) -> SubAgentResult:
        tools = resolve_toolset(toolset_name, self.all_tools, parent_tools)
        subagent_id = f"sa-{uuid.uuid4().hex[:8]}"
        
        subagent = SubAgent(
            safe_client=self.safe_client,
            logger=self.logger,
            config=self.config,
            pool=self,
            role_prompt=role_prompt,
            tools=tools,
            depth=depth,
            max_depth=self.max_depth,
            subagent_id=subagent_id
        )
        
        result = subagent.run(task_description)
        self.completed_results.append(result)
        
        return result
        
    def get_summary(self) -> str:
        if not self.completed_results:
            return "(No SubAgents executed)"
            
        lines = ["=== SubAgent Execution Summary ==="]
        for r in self.completed_results:
            status_mark = "+" if r.status == "success" else "!"
            lines.append(
                f"  [{status_mark}] {r.subagent_id}: {r.task_description[:60]}... "
                f"({r.tool_calls_made} calls, depth={r.depth_reached})"
            )
        return "\n".join(lines)
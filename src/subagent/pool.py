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
        subagent_id = f"sa-{uuid.uuid4().hex[:8]}"
        
        # Tool Error
        try:
            tools = resolve_toolset(toolset_name, self.all_tools, parent_tools)
        except ValueError as e:
            err_msg = str(e)
            self.logger.log_api_call(f"SUBAGENT:{subagent_id} INIT ERROR", {"error": err_msg})
            result = SubAgentResult(
                subagent_id=subagent_id,
                task_description=task_description,
                status="failed",
                summary="Failed to initialize SubAgent. Invalid toolset.",
                depth_reached=depth,
                error_message=err_msg
            )
            self.completed_results.append(result)
            return result
        
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
        
        # Runtime Error (like HTTP)
        try:
            result = subagent.run(task_description)
        except Exception as e:
            err_msg = f"Unexpected error during execution: {str(e)}"
            self.logger.log_api_call(f"SUBAGENT:{subagent_id} EXEC ERROR", {"error": err_msg})
            result = SubAgentResult(
                subagent_id=subagent_id,
                task_description=task_description,
                status="failed",
                summary="SubAgent execution crashed.",
                depth_reached=depth,
                error_message=err_msg
            )
            
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
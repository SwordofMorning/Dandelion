# src/subagent/subagent.py

# Brief: Inherits from the original ISubAgent with recursive deep.
# A SubAgent which could do some jobs, like a thread.
# When call run(), a new LLM Context (Session/Message) were created.

import time
from .i_subagent import ISubAgent
from .result import SubAgentResult
from ..tool.agent.spawn_tool import RestrictedSpawnTool

class SubAgent(ISubAgent):
    def __init__(
        self,
        safe_client,
        logger,
        config,
        pool,
        role_prompt,
        tools,
        depth=0,
        max_depth=3,
        subagent_id=None
    ):
        super().__init__(safe_client, logger, tools, config)
        self.pool = pool
        self.depth = depth
        self.max_depth = max_depth
        self.subagent_id = subagent_id or f"subagent-{id(self):x}"
        self.sub_results = []
        
        self.system_prompt = self._build_system_prompt(role_prompt)
        
        if self.depth < self.max_depth:
            self.tools["spawn_subagent"] = RestrictedSpawnTool(
                pool=pool,
                parent_subagent=self,
                current_depth=self.depth,
                max_depth=self.max_depth
            )
            self._refresh_tool_schemas()
            
    def _build_system_prompt(self, role_prompt):
        if self.depth == 0:
            depth_info = (
                "You are a dedicated SubAgent. "
                "Complete the sub-task using available tools. "
                "You may delegate narrow sub-tasks to SubSubAgents using 'spawn_subagent'. "
                "Always return a concise, well-structured summary of your findings."
            )
        elif self.depth < self.max_depth:
            remaining = self.max_depth - self.depth
            depth_info = (
                f"You are a SubAgent at depth {self.depth}/{self.max_depth}. "
                f"You may delegate sub-tasks up to {remaining} more level(s). "
                "Return a concise summary of your findings."
            )
        else:
            depth_info = (
                f"You are at maximum recursion depth ({self.max_depth}). "
                "You MUST complete the task yourself. Do not attempt to delegate."
            )
            
        return f"{role_prompt}\n\n{depth_info}"
        
    def _refresh_tool_schemas(self):
        self.tool_schemas = [
            {
                "name": t.get_name(),
                "description": t.get_description(),
                "input_schema": t.get_schema()
            }
            for t in self.tools.values()
        ]

    def run(self, task_description) -> SubAgentResult:
        start_time = time.time()
        tool_calls_made = 0
        max_depth_reached = self.depth
        
        print(f"\n[*] [SubAgent:{self.subagent_id}] Spawned (depth={self.depth})")
        print(f"    Task: {task_description[:80]}...")
        
        messages = [{"role": "user", "content": task_description}]
        
        for turn in range(30):
            payload = {
                "tools": self.tool_schemas,
                "messages": messages,
                "max_tokens": int(self.config.get("MAX_TOKENS", 8000)),
                "system": self.system_prompt
            }
            
            self.logger.log_api_call(
                f"SUBAGENT:{self.subagent_id} PRE-CALL (Turn {turn+1}, depth={self.depth})",
                payload
            )
            
            resp, err = self.client.safe_stream_request(payload)
            
            if err:
                return SubAgentResult(
                    subagent_id=self.subagent_id,
                    task_description=task_description,
                    status="failed",
                    summary="",
                    tool_calls_made=tool_calls_made,
                    depth_reached=max_depth_reached,
                    error_message=str(err)
                )
                
            self.logger.log_api_call(
                f"SUBAGENT:{self.subagent_id} POST-CALL (Turn {turn+1}, depth={self.depth})",
                resp if resp else {"error": err}
            )
   
            messages.append({"role": "assistant", "content": resp.content})
            
            if resp.stop_reason != "tool_use":
                break
                
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    tool_calls_made += 1
                    handler = self.tools.get(block.name)
                    if handler:
                        success, output = handler.execute(**block.input)
                        if block.name == "spawn_subagent" and success:
                            max_depth_reached = max(max_depth_reached, self.depth + 1)
                    else:
                        success, output = False, f"Unknown tool: {block.name}"
                        
                    print(f"    [>] [{self.subagent_id}] tool {block.name}: {'Success' if success else 'Failed'}")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output
                    })
                    
            messages.append({"role": "user", "content": results})
            
        final_text = self._extract_final_summary(messages)
        elapsed = time.time() - start_time
        print(f"[*] [SubAgent:{self.subagent_id}] Completed in {elapsed:.1f}s "
              f"({tool_calls_made} tool calls, depth={max_depth_reached})")
              
        return SubAgentResult(
            subagent_id=self.subagent_id,
            task_description=task_description,
            status="success" if final_text else "partial_success",
            summary=final_text or "SubAgent completed without generating a summary.",
            tool_calls_made=tool_calls_made,
            depth_reached=max_depth_reached,
            tokens_used=0,
            sub_results=self.sub_results
        )
        
    def _extract_final_summary(self, messages):
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                text = self.client.extract_text(msg["content"])
                if text and len(text.strip()) > 20:
                    return text.strip()
        return ""
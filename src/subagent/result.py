##
 # @file src/subagent/result.py
 # @date 2026/08/07
 # 
 # @brief Define the structured results to be returned to the parent Agent.
 # Messages struct between Agents.
 #

import json
from dataclasses import dataclass, field, asdict
from typing import List

@dataclass
class SubAgentResult:
    # ----- @par 1. Members -----

    subagent_id: str
    task_description: str
    status: str
    summary: str
    artifacts: List[str] = field(default_factory=list)
    tool_calls_made: int = 0
    tokens_used: int = 0
    depth_reached: int = 0
    error_message: str = ""
    sub_results: List['SubAgentResult'] = field(default_factory=list)

    # ----- @par 2. Type Convert -----

    ##
     # @brief Serialize this result into a JSON string.
     #
     # @note No current caller (reserved for debugging / persistence);
     # the production path is to_context_string() via spawn_tool.
     #
     # @return JSON string with ensure_ascii=True.
     #
    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=True, indent=2)
    # End-def

    ##
     # @brief Render a compact multi-line text summary for the parent agent.
     #
     # @note Called by SpawnSubagentTool / RestrictedSpawnTool
     # (src/tool/agent/spawn_tool.py) as the tool output fed back into the
     # parent agent's context (tool_result).
     #
     # @return Multi-line string: id / status / task / summary / artifacts /
     #         error / tool calls / depth reached.
     #
    def to_context_string(self) -> str:
        lines = [
            f"[SubAgent Result - {self.subagent_id}]",
            f"Status: {self.status}",
            f"Task: {self.task_description[:200]}",
            f"Summary: {self.summary[:2000]}",
        ]
        if self.artifacts:
            lines.append(f"Artifacts: {', '.join(self.artifacts)}")
        if self.error_message:
            lines.append(f"Error: {self.error_message}")
        lines.append(f"Tool Calls: {self.tool_calls_made}")
        lines.append(f"Depth Reached: {self.depth_reached}")
        return "\n".join(lines)
    # End-def
# End-class
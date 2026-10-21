##
 # @file src/subagent/result.py
 # @date 2026/08/07
 # 
 # @brief Define the structured results to be returned to the parent Agent.
 # Messages struct between Agents.
 #

from dataclasses import dataclass, field
from typing import List

# Budget of the text fed back to the parent (tool_result). Keeps one large
# SubAgent answer, or a deep result tree, from flooding the parent context.
CONTEXT_SUMMARY_CHARS = 6000
NESTED_SUMMARY_CHARS = 300
MAX_NESTED_LINES = 20

##
 # @brief Result of one SubAgent run.
 #
 # @note status values:
 #   success   - finished with a final answer
 #   partial   - stopped without a usable final answer (turn limit / no summary)
 #   cancelled - stopped by a user interrupt (Ctrl+C)
 #   failed    - could not start, crashed, or the LLM request failed
 #
@dataclass
class SubAgentResult:
    # ----- @par 1. Members -----

    subagent_id: str
    task_description: str
    status: str
    summary: str
    artifacts: List[str] = field(default_factory=list)
    tool_calls_made: int = 0
    depth_reached: int = 1
    duration_seconds: float = 0.0
    model_alias: str = ""
    error_message: str = ""
    sub_results: List['SubAgentResult'] = field(default_factory=list)

    # ----- @par 2. Type Convert -----

    ##
     # @brief Render a compact, bounded summary for the parent agent.
     #
     # @note Called by SpawnSubagentTool / RestrictedSpawnTool
     # (src/tool/agent/spawn_tool.py); the text becomes the tool_result.
     #
     # @return Multi-line string: header, execution facts, artifacts, error,
     #         output (truncated to CONTEXT_SUMMARY_CHARS) and the nested tree.
     #
    def to_context_string(self) -> str:
        lines = [
            f"[SubAgent Result | ID: {self.subagent_id} | Status: {self.status.upper()}]",
            f"- Task: {_clip(self.task_description, 300)}",
            f"- Execution: {self.tool_calls_made} tool calls in {self.duration_seconds:.1f}s"
            f" (depth reached: {self.depth_reached}"
            + (f", model: {self.model_alias}" if self.model_alias else "") + ")",
        ]
        if self.artifacts:
            lines.append(f"- Created/Modified Files: {', '.join(self.artifacts)}")
        # End-if
        if self.error_message:
            lines.append(f"- Error: {_clip(self.error_message, 1000)}")
        # End-if
        lines.append("- Output:")
        lines.append(_clip(self.summary.strip(), CONTEXT_SUMMARY_CHARS) or "(No content produced)")

        nested = []
        _collect_nested(self.sub_results, 1, nested)
        if nested:
            lines.append("- Nested SubAgent Results:")
            lines.extend(nested[:MAX_NESTED_LINES])
            if len(nested) > MAX_NESTED_LINES:
                lines.append(f"  ... {len(nested) - MAX_NESTED_LINES} more nested result(s) omitted")
            # End-if
        # End-if
        return "\n".join(lines)
    # End-def
# End-class

##
 # @brief Truncate text with an explicit marker.
 #
def _clip(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    # End-if
    return text[:limit] + f" ... [truncated, {len(text) - limit} more chars]"
# End-def

##
 # @brief Flatten the nested result tree into indented one-line summaries.
 #
def _collect_nested(results, level, out):
    for sub in results or []:
        flat = " ".join((sub.summary or sub.error_message or "").split())
        files = f" | files: {', '.join(sub.artifacts)}" if sub.artifacts else ""
        out.append(f"{'  ' * level}* [{sub.subagent_id}] {sub.status.upper()}: "
                   f"{_clip(flat, NESTED_SUMMARY_CHARS)}{files}")
        _collect_nested(sub.sub_results, level + 1, out)
    # End-for
# End-def

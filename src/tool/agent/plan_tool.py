##
 # @file src/tool/agent/plan_tool.py
 # @date 2026/08/13
 # 
 # @brief  Use LLM to split the task.
 # 
 # @note This tool does not perform any specific actions. 
 # It calls a separate LLM (without polluting the main thread's memory) 
 # to translate complex natural language tasks into a structured JSON task array.
 #

import json
from ..base_tool import BaseTool

PLAN_SYSTEM_PROMPT = """\
You are a Task Decomposition Expert. Your job is to analyze a complex task and break it into subtasks.

Rules:
1. Identify independent sub-problems that can be solved separately.
2. Determine dependencies: does Subtask B need the output of Subtask A?
3. Assign appropriate toolsets and roles to each subtask.
4. Identify which subtasks can run in parallel.
5. Output a valid JSON TaskPlan and NOTHING ELSE.

Available toolsets:
- minimal: read_file, write_file, list_directory
- filesystem: read_file, write_file, list_directory, grep_search, markdown_editor, edit_file
- code_analysis: read_file, grep_search, list_directory, bash
- data_processing: read_weekly_report, write_file, markdown_editor

Output strictly the following JSON structure:
{
  "overall_goal": "...",
  "subtasks": [
    {
      "id": "task-1",
      "description": "...",
      "depends_on": [],
      "toolset": "minimal|filesystem|code_analysis|data_processing|full",
      "role_prompt": "You are a specialized agent... (write a complete and detailed system prompt defining the subagent's role here)",
      "expected_output": "...",
      "priority": 5
    }
  ],
  "execution_strategy": "sequential|parallel|mixed",
  "integration_notes": "..."
}
"""

##
 # @brief Plan Class.
 #
class PlanTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param safe_client Request LLM.
     # @param config User's config in `.env`.
     #
     # @see src/utils/safe_llm/safe_llm.py
     #
    def __init__(self, safe_client, config):
        super().__init__()
        self.safe_client = safe_client
        self.config = config
    # End-def
    
    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "plan_tool"
    # End-def
    
    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return (
            "Analyze a complex task and produce a structured TaskPlan with subtasks, "
            "dependencies, and recommended toolsets. Use this BEFORE spawning subagents "
            "for multi-step tasks."
        )
    # End-def
    
    ##
     # @brief Return tool's schema.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "complex_task": {
                    "type": "string",
                    "description": "The full description of the complex task to decompose."
                },
                "max_subtasks": {
                    "type": "integer",
                    "description": "Maximum number of subtasks to generate (default: 6)."
                }
            },
            "required": ["complex_task"]
        }
    # End-def
    
    ##
     # @brief Sandbox protect.
     #
     # @return rue if allowed (or inside workspace), False if denied by user.
     #
    def execute(self, **kwargs):
        complex_task = kwargs.get("complex_task", "")
        max_subtasks = kwargs.get("max_subtasks", 6)
        
        if not complex_task:
            return False, "Error: complex_task is required."
            
        messages = [
            {
                "role": "user",
                "content": (
                    f"Please decompose the following complex task into at most {max_subtasks} subtasks:\n\n"
                    f"{complex_task}\n\n"
                    "Remember: output ONLY the JSON TaskPlan, no other text."
                )
            }
        ]
        
        payload = {
            "messages": messages,
            "max_tokens": int(self.config.get("MAX_TOKENS", 4000)),
            "system": PLAN_SYSTEM_PROMPT
        }
        
        print("\n[+] [PlanTool] Generating TaskPlan...")

        resp, err = self.safe_client.route_request(
            payload=payload,
            task_description=complex_task,
            toolset_name="planning",
            depth=0,
            stream=True
        )

        if err:
            return False, f"Decomposition failed: {err}"
            
        text = self.safe_client.extract_text(resp.content)
        
        json_str = text.strip()
        if json_str.startswith("```"):
            lines = json_str.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            json_str = "\n".join(lines)
            
        try:
            plan = json.loads(json_str)
            required_keys = ["overall_goal", "subtasks", "execution_strategy"]
            for key in required_keys:
                if key not in plan:
                    return False, f"Invalid TaskPlan: missing key '{key}'"
            return True, json.dumps(plan, ensure_ascii=True, indent=2)
        except json.JSONDecodeError as e:
            return False, f"Failed to parse TaskPlan JSON: {e}\n\nRaw output:\n{text[:500]}"
    # End-def
# End-class
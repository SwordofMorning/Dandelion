# src/subagent/jp/reporter.py
from ..subagent import SubAgent
from ...tool.excel.read_excel_tool import ReadExcelTool

class ReporterSubAgent(SubAgent):
    def __init__(self, safe_client, logger, config, workspace_dir):
        tools = {
            "read_weekly_report": ReadExcelTool(workspace_dir=workspace_dir)
        }
        
        role_prompt = (
            "You are a professional data analyst specializing in weekly report summarization. "
            "Your task is to parse the provided Excel file using the 'read_weekly_report' tool, "
            "analyze the '本周工作' (This Week's Work) section, and produce a concise, "
            "well-structured summary in Markdown format. "
            "Focus on: project names, key accomplishments, and any blockers or next steps. "
            "Output only the final Markdown summary."
        )
        
        super().__init__(
            safe_client=safe_client,
            logger=logger,
            config=config,
            pool=None, 
            role_prompt=role_prompt,
            tools=tools,
            depth=0,
            max_depth=0,
            routing_context={
                "task_description": "summarize weekly report",
                "toolset_name": "data_processing",
                "depth": 0
            }
        )
    
    def execute(self, file_path):
        task_desc = f"Analyze and summarize the weekly report at: {file_path}"
        # run() will return a SubAgentResult object
        result = self.run(task_desc)
        return result
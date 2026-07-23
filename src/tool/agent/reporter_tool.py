# src/tool/agent/reporter_tool.py
from ..base_tool import BaseTool
from ...subagent.jp.reporter import ReporterSubAgent

class ReporterTool(BaseTool):
    def __init__(self, safe_client, logger, config, workspace_dir=None):
        # ReporterTool doesn't do file I/O itself, but it needs to pass workspace_dir to subagent
        super().__init__(workspace_dir)
        self.safe_client = safe_client
        self.logger = logger
        self.config = config

    def get_name(self):
        return "summarize_weekly_report"

    def get_description(self):
        return "Spawn a dedicated SubAgent to read and summarize a SINGLE weekly report Excel file."

    def get_schema(self):
        return {
            "type": "object", 
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the Excel file."}
            }, 
            "required": ["file_path"]
        }

    def execute(self, **kwargs):
        file_path = kwargs.get("file_path", "")
        if not file_path:
            return False, "Error: No file_path provided."
            
        print(f"\n[+] [ReporterTool] Spawning ReporterSubAgent for: {file_path}")
        
        # Pass workspace_dir down to SubAgent (so SubAgent's ReadExcelTool inherits it)
        subagent = ReporterSubAgent(self.safe_client, self.logger, self.config, self.workspace_dir)
        
        task_desc = f"Please read the weekly report at '{file_path}' and generate a professional summary."
        
        # Run SubAgent synchronously
        summary_result = subagent.run(task_desc)
        
        return True, summary_result
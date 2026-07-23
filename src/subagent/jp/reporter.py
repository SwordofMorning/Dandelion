# src/subagent/jp/reporter.py

from ..base_subagent import BaseSubAgent
from ...tool.excel.read_excel_tool import ReadExcelTool

class ReporterSubAgent(BaseSubAgent):
    def __init__(self, safe_client, logger, config, workspace_dir=None):
        # Only inject the EXCEL tool for security. No shell access here.
        tools = {
            "read_weekly_report": ReadExcelTool(workspace_dir=workspace_dir)
        }
        super().__init__(safe_client, logger, tools, config)
        
        # Override System Prompt for specific role
        self.system_prompt = (
            "You are an expert Data Analyst SubAgent.\n"
            "Your task is to use the provided tool to read a specific weekly report Excel file, "
            "then summarize the core work done by the person in a concise, professional format.\n"
            "Do not ask for further instructions. Return the final markdown summary immediately after getting data."
        )
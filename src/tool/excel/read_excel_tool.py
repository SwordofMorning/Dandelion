# src/tool/excel/read_excel_tool.py

import os
import re
import json
from ..base_tool import BaseTool

# Optional dependencies check (C-style)
try:
    import pandas as pd
    import openpyxl
    HAS_EXCEL_LIBS = True
except ImportError:
    HAS_EXCEL_LIBS = False

class ReadExcelTool(BaseTool):
    def __init__(self, workspace_dir=None):
        super().__init__(workspace_dir)

    def get_name(self):
        return "read_weekly_report"

    def get_description(self):
        return "Parse a weekly report Excel file and extract items (Name, Period, Project, Content) in JSON format."

    def get_schema(self):
        return {
            "type": "object", 
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the Excel file."}
            }, 
            "required": ["file_path"]
        }

    # ---------------------------------------------------------
    # Brief: Extract name from filename if missing inside sheet.
    # Input: filename (str)
    # Output: name (str)
    # ---------------------------------------------------------
    def _extract_name_from_filename(self, filename):
        name_part = os.path.splitext(filename)[0]
        tokens = re.split(r'[-_\s]+', name_part)
        for token in tokens:
            token = token.strip()
            if not token or token in ["周报", "副本", "第", "工作周报"]:
                continue
            if re.search(r'\d', token): 
                continue
            return token
        return "Unknown_Person"

    # ---------------------------------------------------------
    # Brief: Execute the Excel parsing logic.
    # Input: kwargs containing 'file_path'
    # Return: (Success: bool, Result: str)
    #         Success=True  -> Result is JSON array string.
    #         Success=False -> Result is error reason.
    # ---------------------------------------------------------
    def execute(self, **kwargs):
        if not HAS_EXCEL_LIBS:
            return False, "Error: Missing required libraries. Run: pip install pandas openpyxl"

        # 1. First get the file_path
        file_path = kwargs.get("file_path", "")
        if not file_path:
            return False, "Error: No file_path provided."

        # 2. SECURITY INJECTION: Check workspace permission AFTER we have the path
        if not self.check_workspace_permission(file_path, action_desc="READ Excel File"):
            return False, f"Error: Permission denied by user to access '{file_path}' outside workspace."

        # 3. Check if file actually exists
        if not os.path.exists(file_path):
            return False, f"Error: File not found at {file_path}"

        period_name = os.path.basename(os.path.dirname(file_path))
        
        try:
            wb = openpyxl.load_workbook(file_path, data_only=True)
            sheet = wb.active
            rows = list(sheet.iter_rows(values_only=True))
        except Exception as e:
            return False, f"Error loading workbook: {str(e)}"

        reporter = None
        work_content_start_row = None
        work_content_end_row = None

        # Phase 1: Fuzzy scan for boundaries
        for i, row in enumerate(rows):
            row_str = [str(cell).strip() if cell is not None else "" for cell in row]
            
            if not reporter:
                for idx, cell in enumerate(row_str):
                    if "汇报人" in cell:
                        split_char = "：" if "：" in cell else ":"
                        if split_char in cell:
                            parts = cell.split(split_char)
                            if len(parts) > 1 and parts[1].strip():
                                reporter = parts[1].strip()
                                break
                        elif idx + 1 < len(row_str) and row_str[idx + 1]:
                            reporter = str(row_str[idx + 1]).strip()
                            break

            if work_content_start_row is None:
                if any("本周工作" in cell or ("工作内容" in cell and "下周" not in cell) for cell in row_str):
                    work_content_start_row = i
                    continue

            if work_content_start_row is not None and i > work_content_start_row:
                if any(any(term in cell for term in ["下周工作", "下周起止", "下周计划", "后续工作"]) for cell in row_str):
                    work_content_end_row = i
                    break

        # Fallback boundary
        if work_content_start_row is not None and work_content_end_row is None:
            for i in range(work_content_start_row + 1, len(rows)):
                row_str = [str(cell).strip() if cell is not None else "" for cell in rows[i]]
                if any(any(term in cell for term in ["遇及问题", "问题", "所需支持", "困难"]) for cell in row_str):
                    work_content_end_row = i
                    break
            if work_content_end_row is None:
                work_content_end_row = len(rows)

        if not reporter or reporter == "None" or reporter.strip() == "":
            reporter = self._extract_name_from_filename(os.path.basename(file_path))

        if work_content_start_row is None:
            return False, "Error: Could not find '本周工作' starting row."

        # Phase 2: Precise column matching
        content_rows = rows[work_content_start_row+1 : work_content_end_row]
        content_col_idx = None
        project_col_idx = None
        item_rows = content_rows

        for r_idx, r in enumerate(content_rows):
            r_str = [str(c).strip() if c is not None else "" for c in r]
            if "具体工作内容" in r_str or "工作内容" in r_str:
                content_col_idx = r_str.index("具体工作内容") if "具体工作内容" in r_str else r_str.index("工作内容")
                
                if "项目名称" in r_str:
                    project_col_idx = r_str.index("项目名称")
                elif "项目" in r_str:
                    project_col_idx = r_str.index("项目")
                else:
                    project_col_idx = 0
                    
                item_rows = content_rows[r_idx+1:]
                break

        if content_col_idx is None:
            content_col_idx = 2
            project_col_idx = 1

        # Phase 3: Extract items
        extracted_items = []
        current_project = "通用/其他"

        for r in item_rows:
            if len(r) == 0:
                continue
                
            proj_val = r[project_col_idx] if project_col_idx < len(r) else None
            if proj_val is not None:
                proj_str = str(proj_val).strip()
                if proj_str and proj_str.lower() != "none" and proj_str not in ["项目名称", "项目", "具体工作内容", "工作内容"]:
                    current_project = proj_str
                    
            content_val = r[content_col_idx] if content_col_idx < len(r) else None
            if content_val is not None:
                val_str = str(content_val).strip()
                if not val_str or val_str.lower() == "none" or val_str in ["具体工作内容", "工作内容"] or val_str.startswith("下周"):
                    continue
                    
                if re.match(r'^\d+\.\d+\s*[-~至]\s*\d+\.\d+$', val_str):
                    continue

                extracted_items.append({
                    "Reporter": reporter,
                    "Period": period_name,
                    "Project": current_project,
                    "Content": val_str
                })
                    
        return True, json.dumps(extracted_items, ensure_ascii=False)
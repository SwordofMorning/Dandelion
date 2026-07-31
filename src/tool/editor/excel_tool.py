# src/tool/editor/excel_tool.py

import os
from ..base_tool import BaseTool

try:
    import pandas as pd
    from tabulate import tabulate
    HAS_EXCEL_LIBS = True
except ImportError:
    HAS_EXCEL_LIBS = False

class ReadExcelTool(BaseTool):
    def __init__(self, workspace_dir=None):
        super().__init__(workspace_dir)

    def get_name(self):
        return "read_excel"

    def get_description(self):
        return (
            "Read an Excel file (.xlsx/.xls) and return the specified worksheet as a Markdown table. "
            "Use this for general tabular data extraction. "
            "Parameters: file_path (required), sheet (name or index, default 0), "
            "header_row (0-based index or null, default 0), "
            "max_rows (limit data rows to save tokens, default 100)."
        )

    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute or relative path to the Excel file."},
                "sheet": {"type": ["string", "integer", "null"], "description": "Sheet name or 0-based index. Default: 0."},
                "header_row": {"type": ["integer", "null"], "description": "Row index for headers. Null = no header. Default: 0."},
                "max_rows": {"type": ["integer", "null"], "description": "Max data rows to read. Default: 100."}
            },
            "required": ["file_path"]
        }

    def execute(self, **kwargs):
        if not HAS_EXCEL_LIBS:
            return False, "Error: Missing libraries. Run: pip install pandas openpyxl tabulate"

        file_path = kwargs.get("file_path", "")
        if not file_path:
            return False, "Error: No file_path provided."

        if not os.path.isabs(file_path):
            file_path = os.path.join(self.workspace_dir, file_path)
        file_path = os.path.abspath(file_path)

        if not self.check_workspace_permission(file_path, "READ Excel File"):
            return False, f"Error: Permission denied for '{file_path}'."

        if not os.path.exists(file_path):
            return False, f"Error: File not found: {file_path}"

        sheet = kwargs.get("sheet", 0)
        header_row = kwargs.get("header_row", 0)
        max_rows = kwargs.get("max_rows", 100)

        try:
            df = pd.read_excel(
                file_path,
                sheet_name=sheet,
                header=header_row,
                nrows=max_rows if max_rows is not None else None,
                engine="openpyxl"
            )
        except Exception as e:
            return False, f"Error reading Excel: {str(e)}"

        if df.empty:
            return True, "(Empty table: no data rows found)"

        try:
            # Convert NaN to empty string for cleaner markdown
            df = df.fillna("")
            table_str = tabulate(
                df, 
                headers="keys" if header_row is not None else (),
                tablefmt="github", 
                showindex=False
            )
        except Exception as e:
            return False, f"Error formatting table: {str(e)}"

        meta = [
            f"[Excel Table Extract]",
            f"File: {os.path.basename(file_path)}",
            f"Shape: {df.shape[0]} rows x {df.shape[1]} cols",
            ""
        ]
        return True, "\n".join(meta) + "\n" + table_str


class WriteExcelTool(BaseTool):
    def __init__(self, workspace_dir=None):
        super().__init__(workspace_dir)

    def get_name(self):
        return "write_excel"

    def get_description(self):
        return (
            "Write tabular data to an Excel file (.xlsx) from a Markdown table string. "
            "Overwrites the file if it exists."
        )

    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute or relative path to output Excel file."},
                "markdown_table": {"type": "string", "description": "Markdown table string (GitHub flavored)."},
                "sheet_name": {"type": "string", "description": "Worksheet name. Default: 'Sheet1'."}
            },
            "required": ["file_path", "markdown_table"]
        }

    def execute(self, **kwargs):
        if not HAS_EXCEL_LIBS:
            return False, "Error: Missing libraries. Run: pip install pandas openpyxl"

        file_path = kwargs.get("file_path", "")
        md_table = kwargs.get("markdown_table", "")
        sheet_name = kwargs.get("sheet_name", "Sheet1")

        if not file_path or not md_table:
            return False, "Error: file_path and markdown_table are required."

        if not os.path.isabs(file_path):
            file_path = os.path.join(self.workspace_dir, file_path)
        file_path = os.path.abspath(file_path)

        if not self.check_workspace_permission(file_path, "WRITE Excel File"):
            return False, f"Error: Permission denied for '{file_path}'."

        try:
            df = self._parse_markdown_table(md_table)
            if df.empty:
                return False, "Error: No valid data parsed from markdown table."

            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with pd.ExcelWriter(file_path, engine="openpyxl") as writer:
                df.to_excel(writer, sheet_name=sheet_name, index=False)
            
            return True, f"Successfully wrote {df.shape[0]} rows to '{file_path}'."
        except Exception as e:
            return False, f"Error writing Excel: {str(e)}"

    def _parse_markdown_table(self, md: str) -> "pd.DataFrame":
        import re
        lines = [line.strip() for line in md.strip().split("\n") if line.strip()]
        data_lines = [line for line in lines if not re.match(r"^\|[\s\-\|:]+\|$", line)]
        
        if not data_lines:
            return pd.DataFrame()
            
        rows = []
        for line in data_lines:
            if line.startswith("|") and line.endswith("|"):
                line = line[1:-1]
            cells = [cell.strip() for cell in line.split("|")]
            rows.append(cells)
            
        if not rows:
            return pd.DataFrame()
            
        headers = rows[0]
        data = rows[1:] if len(rows) > 1 else []
        return pd.DataFrame(data, columns=headers)
---
name: weekly_report_parser
description: Skill to parse and summarize standard weekly report Excel files using generic read_excel tool.
---

You are an expert at extracting and summarizing information from weekly report Excel files.
When asked to summarize a weekly report, follow these strict rules to mimic the legacy Python parsing logic:

### Step 1: Read the Data
Use the `read_excel` tool to read the target file. Since weekly reports can be large, you may need to read it into a Markdown table.

### Step 2: Extract Meta Information
- **Reporter (Name)**: Look for a cell containing "汇报人". The name is usually in the same cell after a colon (":") or in the immediately adjacent cell. If missing, infer the name from the file name.
- **Period**: Infer this from the name of the parent directory of the file path.

### Step 3: Locate the Work Content
- Find the section starting with "本周工作" or "工作内容" (excluding cells that mention "下周").
- Stop reading items when you hit rows containing "下周工作", "下周起止", "下周计划", "后续工作", "遇及问题", "问题", "所需支持", or "困难".

### Step 4: Data Extraction Rules
Extract items into a structure containing: Reporter, Period, Project, Content.
Apply the following strict filtering rules (translated from legacy code):
1. **Empty Rows**: Ignore completely.
2. **Project Name**: Look under columns titled "项目名称" or "项目". If a project cell is empty or says "None" or is a header, inherit the `current_project` (defaults to "通用/其他").
3. **Content filtering**: Look under columns titled "具体工作内容" or "工作内容". 
   - Ignore if the content is empty, "None", equals the header names, or starts with "下周".
   - Ignore if the content is just a date range (e.g., matches regex `^\d+\.\d+\s*[-~至]\s*\d+\.\d+$`).

### Step 5: Output Format
Generate a clear Markdown summary listing the Reporter, Period, and a grouped list of Projects with their respective valid Contents.
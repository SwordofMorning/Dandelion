# src/core/sysprompt.py
import platform
import shutil
import datetime

class PromptBuilder:
    def __init__(self, memory_manager, skill_manager, config):
        self.memory = memory_manager
        self.skill = skill_manager
        self.config = config
        
        self.os_name = platform.system()
        self.has_pwsh = shutil.which("powershell") is not None
        self.has_bash = shutil.which("bash") is not None
        
        if self.os_name == "Windows" and self.has_pwsh:
            self.terminal_hint = "Windows Environment. Primary shell is 'powershell'. Avoid linux-specific arguments like 'ls -la'."
        elif self.os_name == "Windows" and self.has_bash:
            self.terminal_hint = "Windows Environment but using 'bash' (Git Bash/MSYS). Use standard unix commands."
        else:
            self.terminal_hint = f"{self.os_name} Environment. Primary shell is 'bash'."

    def build(self):
        sections = []
        
        # 0. Generate current human-readable time to ground the LLM's knowledge
        now = datetime.datetime.now()
        # Formats to something like "Friday, July 31, 2026 at 09:35 AM"
        time_str = now.strftime("%A, %B %d, %Y at %I:%M %p")
        # Add local timezone offset info if needed. We assume local timezone is UTC+8 based on user context.
        # Alternatively, we could fetch timezone info dynamically, but a static indication often suffices.
        time_str += " (Local Time)"

        # 1. Identity & Environment
        sections.append("You are a professional coding and management agent running locally.")
        sections.append(f"Current System Time: {time_str}. Please base any time-sensitive reasoning on this date.")
        sections.append(f"Environment Info:\n{self.terminal_hint}")
        
        # 2. SubAgent, if enable SUB_LIST, must palnt first
        sub_list = self.config.get("SUB_LIST", [])
        if sub_list:
            sections.append(
                "## SubAgent Orchestration (MANDATORY FOR COMPLEX TASKS)\n"
                "You have access to a SubAgent cluster system for handling complex, multi-step tasks.\n"
                "### When to Use SubAgents:\n"
                "- Tasks that involve 3+ independent sub-problems\n"
                "- Tasks that require different expertise\n"
                "### How to Use SubAgents:\n"
                "1. You MUST call 'plan_tool' first to break the complex task into a TaskPlan.\n"
                "2. For each SubTask, call 'spawn_subagent' with task_description, toolset, and role_prompt.\n"
                "3. Wait for each SubAgentResult before proceeding to dependent subtasks.\n"
                "4. Synthesize the final answer from all SubAgentResults.\n"
                "### Available Toolsets:\n"
                "- 'minimal': read_file, write_file, list_directory\n"
                "- 'filesystem': read_file, write_file, list_directory, grep_search, markdown_editor, edit_file\n"
                "- 'code_analysis': read_file, grep_search, list_directory, bash\n"
                "- 'data_processing': read_weekly_report, write_file, markdown_editor\n"
                "- 'full': bash, read_file, write_file, list_directory, grep_search, markdown_editor, edit_file"
            )
        
        # 3. Skills Catalog (Layer 1)
        catalog = self.skill.get_catalog()
        sections.append(
            f"Available Skills:\n{catalog}\n"
            "Use the 'load_skill' tool to fetch the full content of a skill when you need specific formats or rules."
        )
        
        # 4. Memories
        index = self.memory.get_index_text()
        if index:
            sections.append(f"Relevant Memories:\n{index}\nRespect user preferences from memory.")
            
        # 5. Security Rules
        sections.append("Security Rule: Do not attempt to access .env/ or escape the workspace directory.")
        
        return "\n\n".join(sections)
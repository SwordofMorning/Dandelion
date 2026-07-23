# src/core/sysprompt.py

import platform
import shutil
import json

class PromptBuilder:
    def __init__(self, memory_manager, skill_manager):
        self.memory = memory_manager
        self.skill = skill_manager
        
        # Detect Environment
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
        
        # 1. Identity & Environment
        sections.append("You are a professional coding and management agent running locally.")
        sections.append(f"Environment Info:\n{self.terminal_hint}")
        
        # 2. Skills Catalog (Layer 1)
        catalog = self.skill.get_catalog()
        sections.append(
            f"Available Skills:\n{catalog}\n"
            "Use the 'load_skill' tool to fetch the full content of a skill when you need specific formats or rules."
        )
        
        # 3. Memories
        index = self.memory.get_index_text()
        if index:
            sections.append(f"Relevant Memories:\n{index}\nRespect user preferences from memory.")
            
        # 4. Security Rules
        sections.append("Security Rule: Do not attempt to access .env/ or escape the workspace directory.")
        
        return "\n\n".join(sections)
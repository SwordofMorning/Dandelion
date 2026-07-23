# src/tool/agent/skill_tool.py

from ..base_tool import BaseTool

class LoadSkillTool(BaseTool):
    def __init__(self, skill_manager):
        self.skill_manager = skill_manager

    def get_name(self):
        return "load_skill"

    def get_description(self):
        return "Load the full markdown content of a specific skill by its name."

    def get_schema(self):
        return {
            "type": "object", 
            "properties": {
                "name": {"type": "string", "description": "The exact name of the skill to load."}
            }, 
            "required": ["name"]
        }

    def execute(self, **kwargs):
        name = kwargs.get("name", "")
        if not name:
            return False, "Error: Skill name is required."
            
        content = self.skill_manager.get_skill_content(name)
        if not content:
            return False, f"Error: Skill '{name}' not found."
            
        print(f"\n[+] [Skill Loaded] Agent fetched skill: {name}")
        return True, content
##
 # @file src/tool/agent/skill_tool.py
 # @date 2026/08/13
 # 
 # @brief Load Skill.
 #

from ..base_tool import BaseTool

##
 # @Brief Load Skill Class.
 #
class LoadSkillTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param skill_manager Skill manager class.
     #
     # @see src/core/skill.py
     #
    def __init__(self, skill_manager):
        self.skill_manager = skill_manager
    # End-def

    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "load_skill"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return "Load the full markdown content of a specific skill by its name."
    # End-def

    def get_schema(self):
        return {
            "type": "object", 
            "properties": {
                "name": {"type": "string", "description": "The exact name of the skill to load."}
            }, 
            "required": ["name"]
        }
    # End-def

    ##
     # @brief Return tool's schema.
     #
    def execute(self, **kwargs):
        name = kwargs.get("name", "")
        if not name:
            return False, "Error: Skill name is required."
            
        content = self.skill_manager.get_skill_content(name)
        if not content:
            return False, f"Error: Skill '{name}' not found."
            
        print(f"\n[+] [Skill Loaded] Agent fetched skill: {name}")
        return True, content
    # End-def
# End-class
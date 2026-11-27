##
 # @file packages/dandelion-core/src/dandelion_core/tool/agent/skill_tool.py
 # @date 2026/11/28
 #
 # @brief On-demand skill-loading tool.
 # Resolves a named skill through SkillManager and returns its full Markdown
 # instructions to the calling Agent.
 #

from ..base_tool import BaseTool


##
 # @brief Load Skill Class.
 #
class LoadSkillTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param skill_manager Skill manager class.
     # @param workspace_dir Optional workspace root directory forwarded to BaseTool.
     #
     # @see packages/dandelion-core/src/dandelion_core/agent/skill.py
     #
     # @return None.
     #
    def __init__(self, skill_manager, workspace_dir=None):
        super().__init__(workspace_dir=workspace_dir)
        self.skill_manager = skill_manager
    # End-def

    ##
     # @brief Return tool's name.
     #
     # @return Tool identifier string.
     #
    def get_name(self):
        return "load_skill"
    # End-def

    ##
     # @brief Return tool's description.
     #
     # @return Human-readable description string.
     #
    def get_description(self):
        return "Load the full markdown content of a specific skill by its name."
    # End-def

    ##
     # @brief Return tool's schema.
     #
     # @return JSON Schema dictionary.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The exact name of the skill to load."},
            },
            "required": ["name"],
        }
    # End-def

    ##
     # @brief Execute skill loading.
     #
     # @param kwargs Schema properties: name.
     #
     # @return (success_bool, result_string) tuple.
     #
    def execute(self, **kwargs):
        name = kwargs.get("name", "")
        if not name:
            return False, "Error: Skill name is required."
        # End-if

        content = self.skill_manager.get_skill_content(name)
        if not content:
            return False, f"Error: Skill '{name}' not found."
        # End-if

        self._emit(f"\n[+] [Skill Loaded] Agent fetched skill: {name}")
        return True, content
    # End-def
# End-class

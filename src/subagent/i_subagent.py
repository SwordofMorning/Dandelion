##
 # @file src/subagent/i_subagent.py
 # @date 2026/08/07
 # 
 # @brief Subagent virtual base class.
 # subagent.py provided a generic subagent class derivative from virtual parent,
 # which seems unnecessary;
 # but if you need to implement a specified subagent class, this virtual might be useful.
 #

##
 # @brief Subagent virtual base class.
 #
class ISubAgent:
    ##
     # @brief COnstructor.
     #
     # @param safe_client llm_request client.
     # @param logger log management.
     # @param tools toolset.
     # @param config environment config.
     #
    def __init__(self, safe_client, logger, tools, config):
        self.client = safe_client
        self.logger = logger
        self.tools = tools
        self.config = config
        
        self.system_prompt = (
            "You are a dedicated SubAgent. "
            "Complete the sub-task using available tools, then return a concise summary. "
            "Do not delegate further."
        )
        self.tool_schemas = [
            {
                "name": t.get_name(), 
                "description": t.get_description(), 
                "input_schema": t.get_schema()
            } for t in self.tools.values()
        ]
    # End-def

    ##
     # @brief Remaining token budget for media admission checks.
     #
     # @return int tokens available in the SubAgent's own context.
     #
     # @note SubAgents have no compaction step, so the whole window minus the
     # reserved output budget is available. The same conservative heuristic as
     # the main agent is reused (media pointers contribute their marker cost).
     #
    def available_token_budget(self):
        return None
    # End-def

    ##
     # @brief SubAgent's loop (interface only).
     #
     # @param task_description SubAgent's task prompt.
     #
     # @return SubAgentResult to parentAgent.
     # @see src/subagent/result.py
     # @see src/subagent/subagent.py (SubAgent.run, the real implementation)
     #
    def run(self, task_description):
        raise NotImplementedError("Subclasses must implement run() and return a SubAgentResult.")
    # End-def
# End-class
##
 # @file packages/dandelion-core/src/dandelion_core/subagent/i_subagent.py
 # @date 2026/11/28
 #
 # @brief Abstract SubAgent interface and shared execution state.
 # Defines ISubAgent(ABC) storing the model client, logger, configuration, and
 # mounted tool schemas, while requiring concrete workers to implement run().
 #

from abc import ABC, abstractmethod


##
 # @brief Abstract base class for delegated SubAgent workers.
 #
class ISubAgent(ABC):
    ##
     # @brief Constructor.
     #
     # @param safe_client SafeLLMClient instance.
     # @param logger SessionManager or compatible audit logger.
     # @param tools Toolset dictionary mapping tool name to BaseTool instance.
     # @param config Loaded configuration dictionary.
     #
     # @return None.
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
                "input_schema": t.get_schema(),
            }
            for t in self.tools.values()
        ]
    # End-def

    ##
     # @brief Remaining token budget for media admission checks.
     #
     # @return int tokens available in the SubAgent's own context, or None.
     #
    def available_token_budget(self):
        return None
    # End-def

    ##
     # @brief Execute the SubAgent loop for a delegated task.
     #
     # @param task_description SubAgent's task prompt.
     #
     # @return SubAgentResult returned to the parent agent.
     #
    @abstractmethod
    def run(self, task_description):
        pass
    # End-def
# End-class

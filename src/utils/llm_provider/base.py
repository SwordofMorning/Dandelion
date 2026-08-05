##
 # @file src/utils/llm_provider/base.py
 # @date 2026/08/05
 # 
 # @brief Abstract base class for LLM Providers.
 #

from abc import ABC, abstractmethod

##
# @brief Abstract base class for LLM Providers.
#
class LLMProvider(ABC):
    ##
     # @brief Constructor.
     # 
     # @param api_key: API key for the provider.
     # @param base_url: Custom base URL (optional).
     # @param model_id: Model identifier.
     # @param thinking: "enabled" or "disabled" - whether to enable extended thinking.
     # @param effort: Reasoning effort level: "low", "medium", "high", or "max".
     #
    @abstractmethod
    def __init__(self, api_key, base_url, model_id, thinking="disabled", effort="medium"):
        pass
    # End-def

    ##
     # @brief Non-streaming request.
     #
    @abstractmethod
    def safe_request(self, payload):
        pass
    # End-def

    ##
     # @brief Streaming request.
     #
    @abstractmethod
    def safe_stream_request(self, payload):
        pass
    # End-def

    ##
     # @brief Extract plain text from response blocks.
     #
    @abstractmethod
    def extract_text(self, content):
        pass
    # End-def
# End-class
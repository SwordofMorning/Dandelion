# src/utils/safe_llm.py
from .llm_provider import AnthropicProvider, GeminiProvider, OpenAIProvider

class SafeLLMClient:
    """Facade for LLM calls, delegates to specific providers based on SDK_TYPE."""
    
    def __init__(self, api_key, base_url, model_id, sdk_type="Anthropic"):
        self.model_id = model_id
        self.sdk_type = sdk_type.lower()
        
        if self.sdk_type in ["ai studio", "gemini"]:
            self.provider = GeminiProvider(api_key, base_url, model_id)
        elif self.sdk_type in ["openai", "nvidia"]:
            self.provider = OpenAIProvider(api_key, base_url, model_id)
        else:
            # Default fallback to Anthropic (Supports Anthropic, OpenRouter, DeepSeek, etc.)
            self.provider = AnthropicProvider(api_key, base_url, model_id)

    def safe_request(self, payload):
        """Non-streaming request wrapper."""
        return self.provider.safe_request(payload)

    def safe_stream_request(self, payload):
        """
        Wraps the SDK stream call for real-time console output.
        Automatically accumulates tool calls and text into a final message.
        
        Returns:
            (response_object, None) on success.
            (None, error_string) on failure.
        """
        return self.provider.safe_stream_request(payload)
            
    def extract_text(self, content):
        """Extract text wrapper."""
        return self.provider.extract_text(content)
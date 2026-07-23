# src/utils/safe_llm.py
from anthropic import Anthropic

class SafeLLMClient:
    def __init__(self, api_key, base_url, model_id):
        self.client = Anthropic(api_key=api_key, base_url=base_url)
        self.model_id = model_id

    def safe_request(self, payload):
        payload["model"] = self.model_id
        try:
            resp = self.client.messages.create(**payload)
            return resp, None
        except Exception as e:
            return None, str(e)

    def safe_stream_request(self, payload):
        """
        Wraps the SDK stream call for real-time console output.
        Automatically accumulates tool calls and text into a final message.
        
        Returns:
            (response_object, None) on success.
            (None, error_string) on failure.
        """
        payload["model"] = self.model_id
        try:
            # Print a distinct color or prefix for LLM output stream
            print("\n[Agent] ", end="", flush=True)
            
            with self.client.messages.stream(**payload) as stream:
                for text in stream.text_stream:
                    print(text, end="", flush=True)
            
            print() # Print newline after stream finishes
            
            # get_final_message() returns the exact same object structure 
            # as the non-streaming create() method.
            final_message = stream.get_final_message()
            return final_message, None
        except Exception as e:
            print() # Ensure newline on error
            return None, str(e)
            
    def extract_text(self, content):
        if not isinstance(content, list): 
            return str(content)
        return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")
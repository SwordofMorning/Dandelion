# src/utils/llm_provider/anthropic.py

from .base import LLMProvider


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key, base_url, model_id):
        from anthropic import Anthropic
        self.client = Anthropic(
            api_key=api_key,
            base_url=base_url if base_url else None,
            default_headers={
                "HTTP-Referer": "https://github.com/SwordofMorning/Regent",
                "X-Title": "Regent"
            }
        )
        self.model_id = model_id

    def safe_request(self, payload):
        payload["model"] = self.model_id
        try:
            resp = self.client.messages.create(**payload)
            return resp, None
        except Exception as e:
            return None, str(e)

    def safe_stream_request(self, payload):
        payload["model"] = self.model_id
        try:
            print("\n[Agent] ", end="", flush=True)
            with self.client.messages.stream(**payload) as stream:
                for event in stream:
                    if event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            # Print normal text
                            print(event.delta.text, end="", flush=True)
                        elif event.delta.type == "input_json_delta":
                            # Print tool arguments in dark gray to show streaming progress
                            print(f"\033[90m{event.delta.partial_json}\033[0m", end="", flush=True)
            print()
            final_message = stream.get_final_message()
            return final_message, None
        except Exception as e:
            print()
            return None, str(e)

    def extract_text(self, content):
        if not isinstance(content, list):
            return str(content)
        return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")
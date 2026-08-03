# src/utils/llm_provider/anthropic.py

from .base import LLMProvider

# Mapping from abstract effort level to Anthropic-compatible budget_tokens
# Used when thinking=enabled to control reasoning token budget
EFFORT_TO_BUDGET_TOKENS = {
    "low": 1024,
    "medium": 4096,
    "high": 8192,
    "max": 16384,
}
DEFAULT_EFFORT = "medium"


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key, base_url, model_id, thinking="disabled", effort=DEFAULT_EFFORT):
        """
        Args:
            api_key: API key for the provider
            base_url: Custom base URL (optional)
            model_id: Model identifier
            thinking: "enabled" or "disabled" — whether to enable extended thinking
            effort: Reasoning effort level: "low", "medium", "high", or "max"
        """
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
        self.thinking = thinking
        self.effort = effort

    def _inject_thinking(self, payload):
        """Inject Anthropic-compatible thinking configuration into payload when enabled."""
        if self.thinking == "enabled":
            budget = EFFORT_TO_BUDGET_TOKENS.get(self.effort, EFFORT_TO_BUDGET_TOKENS["medium"])
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": budget
            }
        # If thinking is "disabled", we intentionally do NOT add a thinking field

    def safe_request(self, payload):
        payload["model"] = self.model_id
        self._inject_thinking(payload)
        try:
            resp = self.client.messages.create(**payload)
            return resp, None
        except Exception as e:
            return None, str(e)

    def safe_stream_request(self, payload):
        payload["model"] = self.model_id
        self._inject_thinking(payload)
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

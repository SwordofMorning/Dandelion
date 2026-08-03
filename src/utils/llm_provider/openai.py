# src/utils/llm_provider/openai.py

import json
from .base import LLMProvider

# Mapping from abstract effort level to OpenAI reasoning_effort string
# Only applied when thinking=enabled. Standard GPT models will ignore this parameter.
# Note: "max" maps to "high" because OpenAI doesn't support "xhigh" broadly
# (only o3/o4 series models support "xhigh", so we stay safe with "high")
EFFORT_TO_REASONING_EFFORT = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "high",
}
DEFAULT_EFFORT = "medium"


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key, base_url, model_id, thinking="disabled", effort=DEFAULT_EFFORT):
        """
        Args:
            api_key: API key for the provider
            base_url: Custom base URL (optional)
            model_id: Model identifier
            thinking: "enabled" or "disabled" — whether to enable extended thinking
            effort: Reasoning effort level: "low", "medium", "high", or "max"
        """
        try:
            import openai
            # Custom base_url
            self.client = openai.OpenAI(
                api_key=api_key,
                base_url=base_url if base_url else None
            )
        except ImportError:
            raise RuntimeError("openai package is required. Please 'pip install openai'")
        self.model_id = model_id
        self.thinking = thinking
        self.effort = effort

    def _inject_reasoning_effort(self, req_kwargs):
        """Inject OpenAI reasoning_effort into request kwargs when thinking is enabled."""
        if self.thinking == "enabled":
            reasoning_level = EFFORT_TO_REASONING_EFFORT.get(
                self.effort, EFFORT_TO_REASONING_EFFORT["medium"]
            )
            req_kwargs["reasoning_effort"] = reasoning_level
        # If thinking is "disabled", we simply don't add the parameter

    def _convert_tools(self, anthropic_tools):
        if not anthropic_tools:
            return None
        openai_tools = []
        for t in anthropic_tools:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}})
                }
            })
        return openai_tools

    def _convert_messages(self, messages, system_prompt):
        openai_msgs = []

        # OpenAI usually expects system prompt as the first message
        if system_prompt:
            openai_msgs.append({"role": "system", "content": system_prompt})

        for msg in messages:
            role = msg["role"]
            content = msg["content"]

            if isinstance(content, str):
                openai_msgs.append({"role": role, "content": content})
            elif isinstance(content, list):
                if role == "user":
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                            elif block.get("type") == "tool_result":
                                # OpenAI requires a specific role="tool" message for each tool result
                                openai_msgs.append({
                                    "role": "tool",
                                    "tool_call_id": block.get("tool_use_id", ""),
                                    "content": str(block.get("content", ""))
                                })
                    # If there was standard text alongside tool results, append it as user
                    if text_parts:
                        openai_msgs.append({"role": "user", "content": "\n".join(text_parts)})

                elif role == "assistant":
                    text_parts = []
                    tool_calls = []
                    for block in content:
                        # Extract dict or object
                        btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)

                        if btype == "text":
                            t = block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
                            if t:
                                text_parts.append(t)
                        elif btype == "tool_use":
                            t_id = block.get("id", "") if isinstance(block, dict) else getattr(block, "id", "")
                            t_name = block.get("name", "") if isinstance(block, dict) else getattr(block, "name", "")
                            t_in = block.get("input", {}) if isinstance(block, dict) else getattr(block, "input", {})
                            tool_calls.append({
                                "id": t_id,
                                "type": "function",
                                "function": {
                                    "name": t_name,
                                    "arguments": json.dumps(t_in, ensure_ascii=False)
                                }
                            })

                    msg_obj = {"role": "assistant"}
                    if text_parts:
                        msg_obj["content"] = "\n".join(text_parts)
                    if tool_calls:
                        msg_obj["tool_calls"] = tool_calls
                    openai_msgs.append(msg_obj)

        return openai_msgs

    def _build_unified_response(self, text, tool_calls_dict):
        from types import SimpleNamespace

        content = []
        if text:
            content.append(SimpleNamespace(type="text", text=text))

        tool_calls = []
        for idx, tc in tool_calls_dict.items():
            try:
                args_dict = json.loads(tc["arguments"])
            except json.JSONDecodeError:
                args_dict = {}

            tool_calls.append(SimpleNamespace(
                type="tool_use",
                id=tc["id"],
                name=tc["name"],
                input=args_dict
            ))

        content.extend(tool_calls)
        stop_reason = "tool_use" if tool_calls else "end_turn"

        return SimpleNamespace(content=content, stop_reason=stop_reason)

    def _log_if_needed(self, logger, log_tag, payload):
        """Log the final payload after injection if logger is provided."""
        if logger and log_tag:
            logger.log_api_call(log_tag, payload)

    def safe_request(self, payload, logger=None, log_tag=""):
        tools = self._convert_tools(payload.get("tools"))
        messages = self._convert_messages(payload.get("messages", []), payload.get("system", ""))

        req_kwargs = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": payload.get("max_tokens", 8192),
            "temperature": 0.7
        }
        if tools:
            req_kwargs["tools"] = tools

        # Inject reasoning_effort when thinking is enabled
        self._inject_reasoning_effort(req_kwargs)
        self._log_if_needed(logger, log_tag, req_kwargs)

        try:
            resp = self.client.chat.completions.create(**req_kwargs)
            choice = resp.choices[0]

            # Pack non-stream result into unified dict structure
            tool_calls_dict = {}
            if choice.message.tool_calls:
                for i, tc in enumerate(choice.message.tool_calls):
                    tool_calls_dict[i] = {
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }

            text = choice.message.content or ""
            return self._build_unified_response(text, tool_calls_dict), None
        except Exception as e:
            return None, str(e)

    def safe_stream_request(self, payload, logger=None, log_tag=""):
        tools = self._convert_tools(payload.get("tools"))
        messages = self._convert_messages(payload.get("messages", []), payload.get("system", ""))

        req_kwargs = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": payload.get("max_tokens", 8192),
            "temperature": 0.7,
            "stream": True
        }
        if tools:
            req_kwargs["tools"] = tools

        # Inject reasoning_effort when thinking is enabled
        self._inject_reasoning_effort(req_kwargs)
        self._log_if_needed(logger, log_tag, req_kwargs)

        try:
            print("\n[Agent] ", end="", flush=True)
            response_stream = self.client.chat.completions.create(**req_kwargs)

            full_text = ""
            tool_calls_dict = {}

            for chunk in response_stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta

                    if getattr(delta, "content", None):
                        print(delta.content, end="", flush=True)
                        full_text += delta.content

                    if getattr(delta, "tool_calls", None):
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls_dict:
                                tool_calls_dict[idx] = {
                                    "id": tc.id or "",
                                    "name": tc.function.name or "",
                                    "arguments": ""
                                }
                            if tc.function.arguments:
                                # Print tool arguments in dark gray to show streaming progress
                                print(f"\033[90m{tc.function.arguments}\033[0m", end="", flush=True)
                                tool_calls_dict[idx]["arguments"] += tc.function.arguments

            print()
            return self._build_unified_response(full_text, tool_calls_dict), None
        except Exception as e:
            print()
            return None, str(e)

    def extract_text(self, content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for block in content:
                if hasattr(block, 'text') and getattr(block, 'type', None) == 'text':
                    texts.append(block.text)
            return "\n".join(texts)
        return str(content)

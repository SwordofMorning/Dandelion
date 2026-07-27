# src/utils/llm_provider.py

import json
from abc import ABC, abstractmethod

class LLMProvider(ABC):
    """Abstract base class for LLM Providers."""
    
    @abstractmethod
    def __init__(self, api_key, base_url, model_id):
        pass
        
    @abstractmethod
    def safe_request(self, payload):
        """Non-streaming request."""
        pass

    @abstractmethod
    def safe_stream_request(self, payload):
        """Streaming request."""
        pass

    @abstractmethod
    def extract_text(self, content):
        """Extract plain text from response blocks."""
        pass


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
                for text in stream.text_stream:
                    print(text, end="", flush=True)
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


class GeminiProvider(LLMProvider):
    def __init__(self, api_key, base_url, model_id):
        try:
            from google import genai
            self.types = genai.types
            # For Gemini, base_url is usually implicit, but supported if needed.
            self.client = genai.Client(api_key=api_key)
        except ImportError:
            raise RuntimeError("google-genai package is required for AI Studio. Please 'pip install google-genai'")
        self.model_id = model_id

    def _convert_schema(self, anthropic_schema):
        """Convert Anthropic JSON schema to Gemini FunctionDeclaration format."""
        if not anthropic_schema:
            return None
        return self.types.Schema(
            type=self.types.Type.OBJECT,
            properties={
                k: self.types.Schema(type=self.types.Type.STRING, description=v.get("description", ""))
                for k, v in anthropic_schema.get("properties", {}).items()
            },
            required=anthropic_schema.get("required", [])
        )

    def _convert_tools(self, anthropic_tools):
        if not anthropic_tools:
            return None
        
        declarations = []
        for t in anthropic_tools:
            declarations.append(self.types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=self._convert_schema(t.get("input_schema", {}))
            ))
        return [self.types.Tool(function_declarations=declarations)]

    def _find_tool_name_by_id(self, messages, tool_id):
        """Backward search to find tool name because Anthropic's tool_result lacks it."""
        for msg in reversed(messages):
            if msg["role"] == "assistant" and isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        if block.get("id") == tool_id:
                            return block.get("name")
                    # Handle object blocks if they are not dicts
                    elif hasattr(block, "type") and block.type == "tool_use":
                        if block.id == tool_id:
                            return block.name
        return "unknown_tool"

    def _convert_messages(self, messages):
        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            content = msg["content"]
            parts = []
            
            if isinstance(content, str):
                parts.append(self.types.Part.from_text(text=content))
            elif isinstance(content, list):
                for block in content:
                    # Dict blocks (usually from user history)
                    if isinstance(block, dict):
                        btype = block.get("type")
                        if btype == "text":
                            parts.append(self.types.Part.from_text(text=block.get("text", "")))
                        elif btype == "tool_result":
                            # Gemini requires tool name in the response part
                            tool_name = self._find_tool_name_by_id(messages, block.get("tool_use_id"))
                            result_val = block.get("content", "")
                            # Format strictly as dict for FunctionResponse
                            resp_dict = {"result": result_val} if isinstance(result_val, str) else result_val
                            parts.append(self.types.Part.from_function_response(
                                name=tool_name,
                                response=resp_dict
                            ))
                    # Object blocks (usually from assistant generated SDK objects)
                    else:
                        btype = getattr(block, "type", None)
                        if btype == "text":
                            parts.append(self.types.Part.from_text(text=getattr(block, "text", "")))
                        elif btype == "tool_use":
                            args_dict = dict(getattr(block, "input", {}))
                            parts.append(self.types.Part.from_function_call(
                                name=getattr(block, "name", ""),
                                args=args_dict
                            ))
                            
            if parts:
                contents.append(self.types.Content(role=role, parts=parts))
        return contents

    def _build_unified_response(self, text, raw_resp):
        """Construct a mock object that acts exactly like an Anthropic response object."""
        from types import SimpleNamespace
        import uuid
        
        tool_calls = []
        if raw_resp.candidates and raw_resp.candidates[0].content.parts:
            for part in raw_resp.candidates[0].content.parts:
                if part.function_call:
                    # Gemini might not provide a unique ID, we generate one to satisfy Anthropic logic
                    call_id = f"call_{uuid.uuid4().hex[:8]}"
                    tool_calls.append(SimpleNamespace(
                        type="tool_use",
                        id=call_id,
                        name=part.function_call.name,
                        input=dict(part.function_call.args)
                    ))
                    
        content = []
        if text:
            content.append(SimpleNamespace(type="text", text=text))
        content.extend(tool_calls)
        
        stop_reason = "tool_use" if tool_calls else "end_turn"
        
        return SimpleNamespace(
            content=content,
            stop_reason=stop_reason
        )

    def safe_request(self, payload):
        gemini_tools = self._convert_tools(payload.get("tools"))
        gemini_msgs = self._convert_messages(payload.get("messages", []))
        sys_prompt = payload.get("system", "")
        
        config = self.types.GenerateContentConfig(
            system_instruction=sys_prompt,
            tools=gemini_tools,
            temperature=0.7,
            max_output_tokens=payload.get("max_tokens", 8192)
        )
        
        try:
            resp = self.client.models.generate_content(
                model=self.model_id,
                contents=gemini_msgs,
                config=config
            )
            return self._build_unified_response(resp.text, resp), None
        except Exception as e:
            return None, str(e)

    def safe_stream_request(self, payload):
        gemini_tools = self._convert_tools(payload.get("tools"))
        gemini_msgs = self._convert_messages(payload.get("messages", []))
        sys_prompt = payload.get("system", "")
        
        config = self.types.GenerateContentConfig(
            system_instruction=sys_prompt,
            tools=gemini_tools,
            temperature=0.7,
            max_output_tokens=payload.get("max_tokens", 8192)
        )
        
        try:
            print("\n[Agent] ", end="", flush=True)
            full_text = ""
            resp_obj = None
            
            for chunk in self.client.models.generate_content_stream(
                model=self.model_id,
                contents=gemini_msgs,
                config=config
            ):
                # Keep reference to the last chunk for function call parsing
                resp_obj = chunk 
                if chunk.text:
                    print(chunk.text, end="", flush=True)
                    full_text += chunk.text
                    
            print()
            return self._build_unified_response(full_text, resp_obj), None
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
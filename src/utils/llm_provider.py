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
                            # Convert historical tool results to text to bypass strict Gemini schema validation
                            tool_name = self._find_tool_name_by_id(messages, block.get("tool_use_id"))
                            result_val = block.get("content", "")
                            transcript = f"\n[System: Tool '{tool_name}' returned successfully]\nOutput:\n{result_val}\n"
                            parts.append(self.types.Part.from_text(text=transcript))
                    else:
                        btype = getattr(block, "type", None)
                        if btype == "text":
                            parts.append(self.types.Part.from_text(text=getattr(block, "text", "")))
                        elif btype == "tool_use":
                            # Convert historical tool uses to text to bypass strict Gemini schema validation
                            args_dict = dict(getattr(block, "input", {}))
                            tool_name = getattr(block, "name", "")
                            args_str = json.dumps(args_dict, ensure_ascii=False)
                            transcript = f"\n[System: Assistant requested tool '{tool_name}' with args: {args_str}]\n"
                            parts.append(self.types.Part.from_text(text=transcript))
                            
            if parts:
                contents.append(self.types.Content(role=role, parts=parts))
        return contents

    def _build_unified_response(self, raw_resp):
        """Parse the full non-stream response object safely without touching .text shortcut."""
        from types import SimpleNamespace
        import uuid
        
        full_text = ""
        tool_calls = []
        
        if raw_resp and raw_resp.candidates and raw_resp.candidates[0].content.parts:
            for part in raw_resp.candidates[0].content.parts:
                if getattr(part, "text", None):
                    full_text += part.text
                elif getattr(part, "function_call", None):
                    call_id = f"call_{uuid.uuid4().hex[:8]}"
                    args_dict = part.function_call.args if part.function_call.args else {}
                    if not isinstance(args_dict, dict):
                        try:
                            args_dict = dict(args_dict)
                        except:
                            args_dict = {}
                            
                    tool_calls.append(SimpleNamespace(
                        type="tool_use",
                        id=call_id,
                        name=part.function_call.name,
                        input=args_dict
                    ))
                    
        content = []
        if full_text:
            content.append(SimpleNamespace(type="text", text=full_text))
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
            # Directly parse the full response safely
            return self._build_unified_response(resp), None
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
            tool_calls = []
            
            import uuid
            from types import SimpleNamespace
            
            for chunk in self.client.models.generate_content_stream(
                model=self.model_id,
                contents=gemini_msgs,
                config=config
            ):
                # Safely iterate through parts in every chunk to catch both text and tools
                if chunk.candidates and chunk.candidates[0].content.parts:
                    for part in chunk.candidates[0].content.parts:
                        if getattr(part, "text", None):
                            print(part.text, end="", flush=True)
                            full_text += part.text
                        elif getattr(part, "function_call", None):
                            call_id = f"call_{uuid.uuid4().hex[:8]}"
                            args_dict = part.function_call.args if part.function_call.args else {}
                            if not isinstance(args_dict, dict):
                                try:
                                    args_dict = dict(args_dict)
                                except:
                                    args_dict = {}
                                    
                            tool_calls.append(SimpleNamespace(
                                type="tool_use",
                                id=call_id,
                                name=part.function_call.name,
                                input=args_dict
                            ))
                            
            print()
            
            content = []
            if full_text:
                content.append(SimpleNamespace(type="text", text=full_text))
            content.extend(tool_calls)
            
            stop_reason = "tool_use" if tool_calls else "end_turn"
            
            final_message = SimpleNamespace(
                content=content,
                stop_reason=stop_reason
            )
            return final_message, None
            
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


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key, base_url, model_id):
        try:
            import openai
            # Custom base_url
            self.client = openai.OpenAI(
                api_key=api_key,
                base_url=base_url if base_url else None
            )
        except ImportError:
            raise RuntimeError("openai package is required for OpenAI API. Please 'pip install openai'")
        self.model_id = model_id

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
                            if t: text_parts.append(t)
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
        
        return SimpleNamespace(
            content=content,
            stop_reason=stop_reason
        )

    def safe_request(self, payload):
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

    def safe_stream_request(self, payload):
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
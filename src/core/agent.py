import os
import sys

from src.utils.safe_llm import SafeLLMClient
from src.core.memory import MemoryManager
from src.core.skill import SkillManager
from src.core.sysprompt import PromptBuilder

# Using the simplified imports from __init__.py
from src.tool import (
    BashTool, ReporterTool, LoadSkillTool, MarkdownTool,
    GrepSearchTool, WriteFileTool, ReadFileTool, ListDirectoryTool
)

class MyAgent:
    def __init__(self, config, session_manager, workspace_dir):
        self.config = config
        self.session = session_manager
        self.workspace_dir = workspace_dir
        self.error_count = 0
        
        # 1. Load history from the current session
        self.history = self.session.load_history()
        
        # 2. Init Sub-Systems with absolute paths
        self.client = SafeLLMClient(
            api_key=self.config["ANTHROPIC_API_KEY"],
            base_url=self.config["ANTHROPIC_BASE_URL"],
            model_id=self.config["MODEL_ID"]
        )
        
        # In passing session_manager as logger to maintain compatibility with legacy code
        self.memory = MemoryManager(
            memory_dir=os.path.join(self.workspace_dir, "llm", "memory"), 
            safe_client=self.client, 
            logger=self.session
        )
        self.skill = SkillManager(
            skill_dir=os.path.join(self.workspace_dir, "llm", "skill")
        )
        self.prompt_builder = PromptBuilder(self.memory, self.skill)
        
        # 3. Load Tools
        self._init_tools()

    def _init_tools(self):
        self.tools = {}
        # Pass BASE_DIR to all file-system related tools
        # Bash maintains its own command checking
        bash = BashTool(workspace_dir=self.workspace_dir)
        reporter = ReporterTool(self.client, self.session, self.config, workspace_dir=self.workspace_dir)
        skill_loader = LoadSkillTool(self.skill)
        md_editor = MarkdownTool(workspace_dir=self.workspace_dir)
        grep_tool = GrepSearchTool(workspace_dir=self.workspace_dir)
        write_tool = WriteFileTool(workspace_dir=self.workspace_dir)
        read_tool = ReadFileTool(workspace_dir=self.workspace_dir)
        list_tool = ListDirectoryTool(workspace_dir=self.workspace_dir)
        
        # Added all tools to the registration list
        tool_list = [
            bash, reporter, skill_loader, md_editor, 
            grep_tool, write_tool, read_tool, list_tool
        ]
        
        for t in tool_list:
            self.tools[t.get_name()] = t
            
        self.tool_schemas = [
            {
                "name": t.get_name(), 
                "description": t.get_description(), 
                "input_schema": t.get_schema()
            } for t in self.tools.values()
        ]

    # Implementation of size estimation and basic compaction
    # For C-Style safety, we just slice the history if it's too long
    def _compact_context(self):
        if len(self.history) > 40:
            print("[*] Context limit reaching threshold, compacting history...")
            snip_msg = {"role": "user", "content": "[snipped previous messages to save context]"}
            self.history = self.history[:5] + [snip_msg] + self.history[-10:]
            self.session.save_history(self.history)

    def step(self):
        # 1. Inject Memory & Build System Prompt
        memories_content = self.memory.load_memories_string(self.history)
        # Dynamic System Prompt injection
        system_prompt = self.prompt_builder.build()
        
        req_messages = self.history.copy()
        
        # Find last user message to inject memory
        last_user_idx = -1
        for i in range(len(req_messages) - 1, -1, -1):
            if req_messages[i].get("role") == "user":
                last_user_idx = i
                break
                
        if memories_content and last_user_idx != -1:
            req_messages[last_user_idx] = {
                **req_messages[last_user_idx],
                "content": memories_content + "\n\n" + str(req_messages[last_user_idx]["content"])
            }

        # 2. Main LLM API Call
        payload = {
            "tools": self.tool_schemas,
            "messages": req_messages,
            "max_tokens": self.config["MAX_TOKENS"],
            "system": system_prompt
        }
        
        self.session.log_api_call("PRE LLM CALL - MAIN", payload)
        
        # Streaming
        resp, err = self.client.safe_stream_request(payload)
        
        self.session.log_api_call("POST LLM CALL - MAIN", resp if resp else {"error": err})
        
        if err is not None:
            print(f"[-] API Error: {err}")
            return False 
            
        self.history.append({"role": "assistant", "content": resp.content})
        self.session.save_history(self.history)
        
        # 3. Handle Output or Tools
        if resp.stop_reason != "tool_use":
            return False 

        # Handle Tools
        results = []
        for block in resp.content:
            if block.type != "tool_use": 
                continue
                
            print(f"\n[*] Tool requested: {block.name}")
            handler = self.tools.get(block.name)
            
            if handler:
                success, output = handler.execute(**block.input)
            else:
                success, output = False, f"Unknown tool: {block.name}"
                
            print(f"    [>] Result length: {len(str(output))} chars")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
            
        self.history.append({"role": "user", "content": results})
        self.session.save_history(self.history)
        return True # Continue loop to process tool results

    def inject_user_message(self, text):
        self.history.append({"role": "user", "content": text})
        self.session.save_history(self.history)
        self._compact_context()

    def reload_history(self):
        self.history = self.session.load_history()
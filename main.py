# main.py

import os
import sys

from src.utils.config import load_api_config
from src.utils.logger import AgentLogger
from src.utils.safe_llm import SafeLLMClient
from src.tool.shell.bash_tool import BashTool
from src.tool.agent.reporter_tool import ReporterTool
from src.tool.agent.skill_tool import LoadSkillTool
from src.tool.editor.markdown_tool import MarkdownTool
from src.core.memory import MemoryManager
from src.core.skill import SkillManager
from src.core.sysprompt import PromptBuilder

# Get absolute base directory of the project
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class MyAgent:
    def __init__(self):
        print("[>] Initializing Custom Agent...")
        self.error_count = 0
        self.history = []
        
        # 1. Load Configurations, use absolute path for robustness
        cfg_path = os.path.join(BASE_DIR, ".env", "api.cfg")
        self.config = load_api_config(cfg_path)
        if not self.config:
            print(f"[-] FATAL: Failed to load config at {cfg_path}.")
            sys.exit(1)
            
        # 2. Init Sub-Systems with absolute paths
        self.logger = AgentLogger(log_dir=os.path.join(BASE_DIR, ".log"))
        self.client = SafeLLMClient(
            api_key=self.config["ANTHROPIC_API_KEY"],
            base_url=self.config["ANTHROPIC_BASE_URL"],
            model_id=self.config["MODEL_ID"]
        )
        
        self.memory = MemoryManager(
            memory_dir=os.path.join(BASE_DIR, "llm", "memory"), 
            safe_client=self.client, 
            logger=self.logger
        )
        self.skill = SkillManager(
            skill_dir=os.path.join(BASE_DIR, "llm", "skill")
        )
        self.prompt_builder = PromptBuilder(self.memory, self.skill)
        
        # 3. Load Tools
        self._init_tools()
        
        print(f"[+] Agent Initialization Successful. Model: {self.config['MODEL_ID']}")
        print(f"[*] Configured MAX_TOKENS: {self.config['MAX_TOKENS']}")

    def _init_tools(self):
        self.tools = {}        
        # Pass BASE_DIR to all file-system related tools
        # Bash maintains its own command checking
        bash = BashTool(workspace_dir=BASE_DIR)
        reporter = ReporterTool(self.client, self.logger, self.config, workspace_dir=BASE_DIR)
        skill_loader = LoadSkillTool(self.skill)
        md_editor = MarkdownTool(workspace_dir=BASE_DIR)
        
        for t in [bash, reporter, skill_loader, md_editor]:
            self.tools[t.get_name()] = t
            
        self.tool_schemas = [
            {
                "name": t.get_name(), 
                "description": t.get_description(), 
                "input_schema": t.get_schema()
            } for t in self.tools.values()
        ]

    def _check_api_error(self, err_str, step_name):
        if err_str is not None:
            print(f"[-] {step_name} API Error: {err_str}")
            self.error_count += 1
            return True
        self.error_count = 0
        return False

    def _compact_context(self):
        # Implementation of size estimation and basic compaction
        # For C-Style safety, we just slice the history if it's too long
        if len(self.history) > 40:
            print("[*] Context limit reaching threshold, compacting history...")
            # Keep system/early context, snip middle, keep last 10
            snip_msg = {"role": "user", "content": "[snipped previous messages to save context]"}
            self.history = self.history[:5] + [snip_msg] + self.history[-10:]

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
        
        self.logger.log_api_call("PRE LLM CALL - MAIN", payload)
        
        # Streaming
        resp, err = self.client.safe_stream_request(payload)
        
        self.logger.log_api_call("POST LLM CALL - MAIN", resp if resp else {"error": err})
        
        if self._check_api_error(err, "MainLLMCall"):
            return False 
            
        self.history.append({"role": "assistant", "content": resp.content})
        
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
        return True # Continue loop to process tool results

    def run(self):
        print("\n================ SYSTEM READY ================")
        print("Type your query and press Enter. Type 'q' to quit.")
        
        while True:
            try:
                if not self.history or self.history[-1]["role"] == "assistant":
                    query = input("\n[User]> ").strip()
                    if query.lower() in ["q", "exit", "quit"]:
                        print("[*] Exiting cleanly.")
                        break
                    if not query:
                        continue
                    self.history.append({"role": "user", "content": query})
                
                # Check for context limits before step
                self._compact_context()
                
                # Run the state machine step
                continue_loop = self.step()
                if not continue_loop:
                    pass # Waiting for user input again
                    
            except KeyboardInterrupt:
                print("\n[!] Interrupted by user. Exiting...")
                break

if __name__ == "__main__":
    agent = MyAgent()
    agent.run()
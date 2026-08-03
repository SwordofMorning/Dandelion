# src/core/agent.py

import os
import sys

from src.utils import SafeLLMClient
from src.utils import CLIPrinter
from src.core.memory import MemoryManager
from src.core.skill import SkillManager
from src.core.sysprompt import PromptBuilder
from src.subagent import SubAgentPool

from src.tool import (
    BashTool, LoadSkillTool, MarkdownTool,
    GrepSearchTool, WriteFileTool, ReadFileTool, ListDirectoryTool,
    EditFileTool, PlanTool, SpawnSubagentTool, WebSearchTool,
    ReadExcelTool, WriteExcelTool,
    StateTool, MemoryTool
)

# Create a module-level CLIPrinter instance for convenience
cli = CLIPrinter()

class MyAgent:
    def __init__(self, config, session_manager, workspace_dir):
        self.config = config
        self.session = session_manager
        self.workspace_dir = workspace_dir
        self.error_count = 0
        self.thinking = str(config.get("THINKING", "disabled")).strip().lower()
        self.effort = str(config.get("EFFORT", "medium")).strip().lower()

        # 1. Load history from the current session
        self.history = self.session.load_history()

        # 2. Init Sub-Systems with absolute paths
        self.client = SafeLLMClient(
            api_key=self.config["ANTHROPIC_API_KEY"],
            base_url=self.config["ANTHROPIC_BASE_URL"],
            model_id=self.config["MODEL_ID"],
            sdk_type=self.config.get("SDK_TYPE", "Anthropic"),
            all_models=self.config.get("ALL_MODELS", []),
            sub_list=self.config.get("SUB_LIST", []),
            thinking=self.thinking,
            effort=self.effort,
            logger=self.session
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
        self.prompt_builder = PromptBuilder(self.memory, self.skill, self.config)

        # 3. Load Tools
        self._init_tools()

    def _init_tools(self):
        self.tools = {}
        # Pass BASE_DIR to all file-system related tools
        # Bash maintains its own command checking
        bash = BashTool(workspace_dir=self.workspace_dir)
        skill_loader = LoadSkillTool(self.skill)
        # Editor
        md_editor = MarkdownTool(workspace_dir=self.workspace_dir)
        read_excel_tool = ReadExcelTool(workspace_dir=self.workspace_dir)
        write_excel_tool = WriteExcelTool(workspace_dir=self.workspace_dir)
        # FS
        grep_tool = GrepSearchTool(workspace_dir=self.workspace_dir)
        write_tool = WriteFileTool(workspace_dir=self.workspace_dir)
        read_tool = ReadFileTool(workspace_dir=self.workspace_dir)
        list_tool = ListDirectoryTool(workspace_dir=self.workspace_dir)
        edit_tool = EditFileTool(workspace_dir=self.workspace_dir)
        # Others
        web_search_tool = WebSearchTool(workspace_dir=self.workspace_dir, config=self.config)
        # Memory
        state_tool = StateTool(workspace_dir=self.workspace_dir)
        memory_tool = MemoryTool(self.memory)

        # Create full tools mapping for Orchestrator
        all_tools = {
            bash.get_name(): bash,
            skill_loader.get_name(): skill_loader,
            md_editor.get_name(): md_editor,
            grep_tool.get_name(): grep_tool,
            write_tool.get_name(): write_tool,
            read_tool.get_name(): read_tool,
            list_tool.get_name(): list_tool,
            edit_tool.get_name(): edit_tool,
            web_search_tool.get_name(): web_search_tool,
            read_excel_tool.get_name(): read_excel_tool,
            write_excel_tool.get_name(): write_excel_tool
        }

        self.pool = SubAgentPool(
            safe_client=self.client,
            logger=self.session,
            config=self.config,
            all_tools=all_tools,
            max_depth=int(self.config.get("MAX_SUBAGENT_DEPTH", 3))
        )

        plan_tool = PlanTool(self.client, self.config)
        spawn_subagent = SpawnSubagentTool(self.pool)

        # Added all tools to the registration list
        tool_list = [
            bash, skill_loader, md_editor,
            grep_tool, write_tool, read_tool, list_tool,
            edit_tool, plan_tool, spawn_subagent, web_search_tool,
            read_excel_tool, write_excel_tool,
            state_tool, memory_tool
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
        # Token estimation based on chars (approx 3.5 chars/token for mixed content)
        total_chars = sum(len(str(m.get("content", ""))) for m in self.history)
        est_tokens = total_chars / 3.5
        
        # Soft limit for compression (e.g., 128k tokens for a 1M model)
        soft_limit = int(self.config.get("MAX_CONTEXT_TOKENS", 128000))
        
        if est_tokens < soft_limit or len(self.history) < 20:
            return

        print(f"[*] Context limit reached (~{int(est_tokens)} tokens), compacting history via LLM...")
        
        # 1. Full archive backup
        archive_dir = os.path.join(self.session.current_session_dir, "archives")
        os.makedirs(archive_dir, exist_ok=True)
        archive_path = os.path.join(archive_dir, f"history_{len(self.history)}.json")
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(self.history, f, ensure_ascii=False, indent=2, default=self.session._default_serializer)

        head_size = 5
        recent_size = 15
        
        head = self.history[:head_size]
        recent = self.history[-recent_size:]
        
        # Ensure recent starts cleanly with a user message to prevent breaking tool_result pairs
        while recent and (recent[0]["role"] != "user" or isinstance(recent[0].get("content"), list)):
            recent.pop(0)
            if len(recent) <= 5: 
                break

        middle = self.history[head_size : len(self.history) - len(recent)]
        middle_text = json.dumps(middle, ensure_ascii=False, indent=2, default=self.session._default_serializer)
        
        if len(middle_text) > 200000:
            middle_text = middle_text[-200000:]

        # 2. Ask LLM to generate a structured summary
        summary_prompt = (
            "Please summarize the following conversation history.\n"
            "Focus on:\n"
            "1. <goals>: Current tasks and acceptance criteria.\n"
            "2. <completed>: What has been done so far.\n"
            "3. <decisions>: Key technical decisions and reasons.\n"
            "4. <artifacts>: Key file paths, variable names, or error codes.\n"
            "5. <pending>: What still needs to be done.\n\n"
            "Output strictly in XML format using the tags above."
        )

        summary_payload = {
            "messages": [{"role": "user", "content": summary_prompt + "\n\nHistory:\n" + middle_text}],
            "max_tokens": 2000,
            "system": "You are a concise memory summarization AI."
        }
        
        resp, err = self.client.safe_request(summary_payload, log_tag="COMPRESSION SUMMARY")
        if err:
            print(f"[-] Compression failed: {err}. Falling back to basic snip.")
            summary_content = "[Compression Failed. History snipped.]"
        else:
            summary_content = self.client.extract_text(resp.content)

        summary_msg = {
            "role": "user",
            "content": f"[System: Context compacted at {archive_path}]\n\n<conversation_summary>\n{summary_content}\n</conversation_summary>"
        }
        
        self.history = head + [summary_msg] + recent
        self.session.save_history(self.history)
        print("[+] Context compacted successfully.")

    def step(self):
        # 1. Inject Memory & Build System Prompt
        memories_content = self.memory.load_memories_string(self.history)
        # Dynamic System Prompt injection
        system_prompt = self.prompt_builder.build()

        # --- Memory ---
        # Append dynamic memories to system_prompt instead of mutating req_messages.
        # Since 'system' is the last field in the payload, this preserves the 
        # entire prefix cache of 'tools' + 'messages'.
        if memories_content:
            system_prompt += f"\n\n{memories_content}"

        # Pure append-only copy, ZERO mutations.
        req_messages = self.history.copy()

        # 2. Main LLM API Call
        payload = {
            "tools": self.tool_schemas,
            "messages": req_messages,
            "max_tokens": int(self.config["MAX_TOKENS"]),
            "system": system_prompt
        }

        # PRE-call logging is now handled inside SafeLLMClient -> Provider
        # (after thinking injection), so we only log POST here.

        # Streaming
        resp, err = self.client.safe_stream_request(payload)

        # POST-call logging
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

            cli.print(f"\nTool requested: {block.name}", level="info")
            handler = self.tools.get(block.name)

            if handler:
                success, output = handler.execute(**block.input)
            else:
                success, output = False, f"Unknown tool: {block.name}"

            output_str = str(output)
            cli.print(f"    Result length: {len(output_str)} chars", level="debug")
            
            # --- Large Output Offload ---
            # Prevents context explosion and delays the need for compression.
            MAX_INLINE_CHARS = 80000 # 80K
            if len(output_str) > MAX_INLINE_CHARS:
                artifact_dir = os.path.join(self.session.current_session_dir, "artifacts")
                os.makedirs(artifact_dir, exist_ok=True)
                artifact_path = os.path.join(artifact_dir, f"{block.id}.txt")

                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(output_str)

                trunc_output = output_str[:MAX_INLINE_CHARS]
                trunc_output += (
                    f"\n\n... [OUTPUT TRUNCATED. Full {len(output_str)} chars output "
                    f"saved to {artifact_path}. Use read_file to read specific missing parts.]"
                )
                output_str = trunc_output

            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output_str})

        if results:
            self.history.append({"role": "user", "content": results})
        else:
            self.history.append({"role": "user", "content": "You indicated a tool use but provided no valid tool calls."})

        self.session.save_history(self.history)
        return True

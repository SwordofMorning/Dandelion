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
    ReadExcelTool, WriteExcelTool
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
            read_excel_tool, write_excel_tool
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
            head = self.history[:5]

            # Note: Find a safe breakpoint (a plain text user message)
            # to prevent cutting off the tool_use and tool_result combination.
            safe_idx = len(self.history) - 10
            while safe_idx > 5:
                msg = self.history[safe_idx]
                if msg["role"] == "user":
                    content = msg.get("content", "")
                    # If it's not a list containing tool_result,
                    # it indicates a safe starting point.
                    is_tool_result = isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                    )
                    if not is_tool_result:
                        break
                safe_idx -= 1

            # Backup
            if safe_idx <= 5:
                safe_idx = len(self.history) - 10

            snip_msg = {"role": "user", "content": "[snipped previous messages to save context]"}
            self.history = head + [snip_msg] + self.history[safe_idx:]
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
            msg_content = req_messages[last_user_idx].get("content", "")

            # Preventing forced type casting to lists from violating API structure specifications
            if isinstance(msg_content, str):
                req_messages[last_user_idx] = {
                    **req_messages[last_user_idx],
                    "content": memories_content + "\n\n" + msg_content
                }
            # If it is a list (e.g., containing tool_result), insert a text block at the beginning.
            elif isinstance(msg_content, list):
                new_content = [{"type": "text", "text": memories_content + "\n\n"}] + msg_content
                req_messages[last_user_idx] = {
                    **req_messages[last_user_idx],
                    "content": new_content
                }

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

            cli.print(f"    Result length: {len(str(output))} chars", level="debug")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})

        if results:
            self.history.append({"role": "user", "content": results})
        else:
            self.history.append({"role": "user", "content": "You indicated a tool use but provided no valid tool calls."})

        self.session.save_history(self.history)
        return True # Continue loop to process tool results

    def inject_user_message(self, text):
        self.history.append({"role": "user", "content": text})
        self.session.save_history(self.history)
        self._compact_context()

    def reload_history(self):
        self.history = self.session.load_history()

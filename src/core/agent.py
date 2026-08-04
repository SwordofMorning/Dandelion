# src/core/agent.py

import os
import re
import sys
import json
import datetime

from review_fix.utils import SafeLLMClient
from review_fix.utils import CLIPrinter
from review_fix.core.memory import MemoryManager
from review_fix.core.skill import SkillManager
from review_fix.core.sysprompt import PromptBuilder
from review_fix.subagent import SubAgentPool

from review_fix.tool import (
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

        # 1b. One-time migration of the legacy global task state
        # (llm/task/task_state.json) into the current session. Idempotent:
        # existing session state is never overwritten and the legacy file is
        # backed up (renamed), so old code stops reading it.
        try:
            self.session.migrate_legacy_task_state(self.workspace_dir)
        except Exception as e:
            print(f"[-] Warning: task state migration failed: {e}")

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
        # Memory is two-tier: global (llm/memory/) + current session
        # (.log/sess_<id>/memory/). The session tier resolves dynamically via
        # session_manager so `checkout` switches memory scope without a rebuild.
        self.memory = MemoryManager(
            memory_dir=os.path.join(self.workspace_dir, "llm", "memory"),
            session_manager=self.session,
            safe_client=self.client,
            logger=self.session
        )
        self.skill = SkillManager(
            skill_dir=os.path.join(self.workspace_dir, "llm", "skill")
        )
        self.prompt_builder = PromptBuilder(self.memory, self.skill, self.config, self.workspace_dir,
                                            session_manager=self.session)

        # Memories cache: refresh only when the last plain-text user message changes,
        # so the tail of system_prompt stays stable during tool loops (cache-friendly).
        self._memories_key = None
        self._memories_cache = ""

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
        state_tool = StateTool(workspace_dir=self.workspace_dir, session_manager=self.session)
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

    # ------------------------------------------------------------------
    # Context compaction: token-aware, LLM-summarized, pair-safe
    # ------------------------------------------------------------------
    @staticmethod
    def _is_plain_user_msg(msg):
        """A user message that is plain text (not a tool_result payload)."""
        if msg.get("role") != "user":
            return False
        content = msg.get("content", "")
        if isinstance(content, list):
            return not any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
        return True

    @staticmethod
    def _msg_ends_with_tool_use(msg):
        """True if an assistant message ends with a tool_use block (handles both
        dict blocks loaded from history.log and SDK objects in memory)."""
        if msg.get("role") != "assistant":
            return False
        content = msg.get("content", "")
        if not isinstance(content, list) or not content:
            return False
        last = content[-1]
        if isinstance(last, dict):
            return last.get("type") == "tool_use"
        return getattr(last, "type", None) == "tool_use"

    @staticmethod
    def _trim_head_for_tool_use(history, head_size):
        """Trim head so it never ends with an assistant tool_use message. The
        trimmed tool_use message stays in middle (summarized) together with its
        matching tool_result, so the summary insertion can never split a pair."""
        head = history[:head_size]
        while head and MyAgent._msg_ends_with_tool_use(head[-1]):
            head = head[:-1]
        return head

    @staticmethod
    def _archive_path(archive_dir, history_len):
        """Append-only archive filename: history length + timestamp, so a second
        compaction at the same history length never overwrites the first."""
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return os.path.join(archive_dir, f"history_{history_len}_{ts}.json")

    @staticmethod
    def _artifact_path(session_dir, block_id):
        """Absolute artifact path (resolvable by read_file even when the process
        CWD differs from the workspace/session directory)."""
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(block_id))
        return os.path.abspath(os.path.join(session_dir, "artifacts", f"{safe_id}.txt"))

    def _estimate_tokens(self, history=None):
        """Heuristic token estimate: ASCII ~4 chars/token, CJK ~1.5 chars/token."""
        history = history if history is not None else self.history
        ascii_chars = 0
        non_ascii_chars = 0
        for m in history:
            content = m.get("content", "")
            if isinstance(content, list):
                content = str(content)
            for ch in str(content):
                if ord(ch) < 128:
                    ascii_chars += 1
                else:
                    non_ascii_chars += 1
        return ascii_chars / 4.0 + non_ascii_chars / 1.5

    def _compact_context(self):
        est_tokens = self._estimate_tokens()
        soft_limit = int(self.config.get("MAX_CONTEXT_TOKENS", 128000))

        if est_tokens < soft_limit or len(self.history) < 20:
            return

        print(f"[*] Context limit reached (~{int(est_tokens)} tokens), compacting history via LLM...")

        # 1. Full archive backup (append-only, restorable)
        archive_dir = os.path.join(self.session.current_session_dir, "archives")
        os.makedirs(archive_dir, exist_ok=True)
        archive_path = self._archive_path(archive_dir, len(self.history))
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(self.history, f, ensure_ascii=False, indent=2,
                      default=self.session._default_serializer)

        head_size = 5
        recent_size = 15

        # Trim head so it never ends with an assistant tool_use message; the
        # summary (role=user) is inserted right after head, and a trailing
        # tool_use with no matching tool_result would corrupt the pairing.
        head = self._trim_head_for_tool_use(self.history, head_size)
        trimmed_head_size = len(head)

        # 2. Find a safe start for the recent window: the latest plain-text user
        #    message within the look-back limit. Starting at a plain-text user
        #    message guarantees tool_use/tool_result pairs are never split.
        max_lookback = min(len(self.history) - trimmed_head_size, recent_size * 2)
        start_idx = None
        for i in range(len(self.history) - 1, len(self.history) - 1 - max_lookback, -1):
            if self._is_plain_user_msg(self.history[i]):
                start_idx = i
                break

        if start_idx is None or start_idx < trimmed_head_size:
            # Fallback: no usable plain-text user message outside the head
            # window; keep only head + summary to avoid dangling or duplicated
            # tool_result blocks.
            print("[-] No safe compaction breakpoint found; keeping head + summary only.")
            start_idx = len(self.history)

        recent = self.history[start_idx:]
        middle = self.history[trimmed_head_size:start_idx]

        # 3. Summarize head + middle (early goals are the most drift-prone part).
        summary_src = head + middle
        summary_text = json.dumps(summary_src, ensure_ascii=False, indent=2,
                                  default=self.session._default_serializer)
        if len(summary_text) > 200000:
            # Keep the head (goals/decisions) and the tail; drop the middle body.
            summary_text = (summary_text[:50000]
                            + "\n...[middle omitted from summarization input]...\n"
                            + summary_text[-150000:])

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
            "messages": [{"role": "user", "content": summary_prompt + "\n\nHistory:\n" + summary_text}],
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
            "content": (f"[System: Context compacted at {archive_path}]\n\n"
                        f"<conversation_summary>\n{summary_content}\n</conversation_summary>")
        }

        self.history = head + [summary_msg] + recent
        self.session.save_history(self.history)

        # Invalidate memories cache: history changed (plain-text user messages may shift).
        self._memories_key = None
        print("[+] Context compacted successfully.")

    def _get_memories(self):
        """Load relevant memories, cached until the last plain-text user message changes."""
        key = None
        for i in range(len(self.history) - 1, -1, -1):
            msg = self.history[i]
            if self._is_plain_user_msg(msg):
                key = (i, hash(str(msg.get("content", ""))[:2000]))
                break
        if key is not None and key == self._memories_key:
            return self._memories_cache
        self._memories_key = key
        self._memories_cache = self.memory.load_memories_string(self.history)
        return self._memories_cache

    def step(self):
        # 0. Check context budget every turn (not only on user messages).
        self._compact_context()

        # 1. Inject Memory & Build System Prompt
        # Memories are cached during tool loops so the system tail stays stable.
        memories_content = self._get_memories()
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
            # Threshold is intentionally low (8K chars ~ 2-4K tokens): outputs
            # beyond this are archived to disk and replaced with a truncated
            # pointer so the model can read_file the missing parts on demand.
            MAX_INLINE_CHARS = 8000
            if len(output_str) > MAX_INLINE_CHARS:
                # Absolute path: the truncated pointer is resolved by read_file
                # relative to the workspace, so it must not depend on the CWD.
                artifact_path = self._artifact_path(self.session.current_session_dir, block.id)
                os.makedirs(os.path.dirname(artifact_path), exist_ok=True)

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

    def inject_user_message(self, text):
        self.history.append({"role": "user", "content": text})
        self.session.save_history(self.history)
        self._compact_context()

    def reload_history(self):
        self.history = self.session.load_history()
        # Session switched: memory relevance cache must be recomputed because
        # the session tier (and possibly the whole history) changed.
        self._memories_key = None
        self._memories_cache = ""

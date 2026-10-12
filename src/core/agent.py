##
 # @file src/core/agent.py
 # @date 2026/08/06
 # 
 # @brief Agent-Loop and others helper functions.
 #
 # @note Agent runtime call chain:
 #   CLI interactive loop
 #     -> MyAgent.step()                 # LLM Round-Trip Step (tools iterator)
 #     -> _compact_context()             # token budget check + LLM summary
 #     -> _get_memories()                # memory injection (cached)
 #     -> SafeLLMClient.safe_stream_request()
 #     -> tool handlers (memory/state/fs/...)
 #     -> history append (tool_result)   # loop continues until stop_reason != tool_use
 #

import os
import re
import sys
import json
import time
import datetime

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
    StateTool, MemoryTool,
    SSHTool, TimeTool,
    ReadPdfTool, ReadImageTool
)
from src.tool.base_tool import BaseTool
from src.tool.media import (
    attach_media_blocks, DEFAULT_MEDIA_LIMITS
)
from .compress import (
    ContextCompressor,
    extract_usage_tokens,
    classify_compaction_summary,
    resolve_compact_summary_max_tokens,
    build_compaction_message,
)

# Create a module-level CLIPrinter instance for convenience
cli = CLIPrinter()

# Dynamic Context injection markers: the [Dandelion Context] block is
# appended to the newest plain-text user message (fresh region) instead of the
# system prompt, so the system prompt stays byte-identical for the whole
# session and DeepSeek's prefix cache keeps hitting across tool-loop iterations.
_DYN_CTX_START = "[Dandelion Context"
_DYN_CTX_END = "[Dandelion Context End]"

# Main-agent LLM call retry policy: 1 initial attempt + _LLM_RETRY_COUNT retries.
# Uniform for all error types (400/401/429/500/connection errors) per design
# decision; exponential backoff (seconds) between attempts.
_LLM_RETRY_COUNT = 3
_LLM_RETRY_BACKOFF = (2, 4, 8)

##
 # @brief Strip previously injected [Dandelion Context] blocks from a user
 #        message content string (all complete blocks, stacked included).
 #
 # @note Defensive: normally the target message is brand new (just added by
 #       inject_user_message) and contains no block. Used on resume/re-run
 #       when the same message may already carry a stale block, so a new
 #       injection replaces (instead of stacking on) the old one.
 #
 # @param content User message content string.
 #
 # @return Content with all injected blocks removed.
 #
def strip_dynamic_context(content):
    # Remove EVERY complete [Dandelion Context] block, including adjacent or
    # repeated (stacked) ones; only the tail of an unterminated final block
    # is dropped. All user content before the first marker is preserved
    # (only the injected "\n\n" separator whitespace is stripped).
    while True:
        start = content.find(_DYN_CTX_START)
        if start == -1:
            return content
        end = content.find(_DYN_CTX_END, start)
        if end == -1:
            # Unterminated block (e.g. manually truncated history): drop the tail.
            return content[:start].rstrip()
        # Complete block: keep the prefix, drop the block, then keep scanning
        # the suffix so stacked/adjacent blocks are removed as well.
        content = content[:start].rstrip() + content[end + len(_DYN_CTX_END):]
    # End-while
# End-def

##
 # @brief Agent Loop Wrapper Class.
 #
class MyAgent:
    ##
     # ========================================
     # @section I. Constructor and Init.
     # Construct MyAgent obj, and init all tools.
     # ========================================
     #

    ##
     # @brief Constructor.
     #
     # @param config api.cfg loaded from .env/.
     # @param session_manager SessionManager object.
     # @param workspace_dir current pwd, used to avoid agent(llm) escape.
     #
    def __init__(self, config, session_manager, workspace_dir):
        # ----- @par 1. Init members -----

        # Alignment members.
        self.config = config
        self.session = session_manager
        self.workspace_dir = workspace_dir
        # Clear error counts.
        self.error_count = 0
        # Inject thinking level.
        self.thinking = str(config.get("THINKING", "disabled")).strip().lower()
        self.effort = str(config.get("EFFORT", "medium")).strip().lower()
        # Endpoint flavor (link terminal model family) used for the reasoning-effort
        # injection; "auto" lets the provider detect it from base_url/model_id.
        self.compat = str(config.get("COMPAT", "auto")).strip().lower()

        # Load history from the current session
        self.history = self.session.load_history()

        # ----- @par 2. Init Subsystem -----

        # Init request client with absolute paths.
        self.client = SafeLLMClient(
            api_key=self.config["ANTHROPIC_API_KEY"],
            base_url=self.config["ANTHROPIC_BASE_URL"],
            model_id=self.config["MODEL_ID"],
            sdk_type=self.config.get("SDK_TYPE", "Message"),
            all_models=self.config.get("ALL_MODELS", []),
            sub_list=self.config.get("SUB_LIST", []),
            thinking=self.thinking,
            effort=self.effort,
            compat=self.compat,
            logger=self.session
        )

        # In passing session_manager as logger to maintain compatibility with legacy code.
        # Memory is two-tier: global (llm/memory/) + current session (.log/sess_<id>/memory/).
        # The session tier resolves dynamically via session_manager so `checkout` switches memory scope without a rebuild.
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
        # Last built system prompt, reused by _soft_token_limit() so the token
        # budget accounts for the real request overhead without rebuilding.
        self._last_system_prompt = ""

        # ----- Context compressor & usage metrics -----
        self.compressor = ContextCompressor(self.client, self.session, self.config)
        self._if_compress = self.compressor.condition
        self.last_usage = {"total": 0, "input": 0, "output": 0, "cached": 0}

        # Media payloads read during the current turn. They are hydrated into
        # the outgoing request as SIBLING parts of the tool_result, and are
        # never persisted in history (history keeps the pointer text only).
        self._pending_media_blocks = []

        # ----- @par 3. Load Tools -----

        # Init tools for Main Agent.
        self._init_tools()
    # End-def

    ##
     # @brief Init tools for Main Agent and Subagents (pool).
     #
    def _init_tools(self):
        # ----- @par 1. Create Tools Object -----

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
        time_tool = TimeTool(workspace_dir=self.workspace_dir, config=self.config)
        # Remote
        ssh_tool = SSHTool(workspace_dir=self.workspace_dir)
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
            time_tool.get_name(): time_tool,
            read_excel_tool.get_name(): read_excel_tool,
            write_excel_tool.get_name(): write_excel_tool,
            ssh_tool.get_name(): ssh_tool
        }

        # ----- @par 1-b. Multimodal (media) Tools -----

        # Registered ONLY when the active model declares media support through
        # its "file" whitelist. A text-only model never sees the media tools.
        media_exts = self.config.get("MEDIA_EXTS", []) or []
        media_limits = self.config.get("MEDIA_LIMITS", {}) or {}
        media_tools = []

        if ".pdf" in media_exts:
            read_pdf_tool = ReadPdfTool(
                workspace_dir=self.workspace_dir,
                host=self,
                session_dir_fn=lambda: self.session.current_session_dir,
                media_limits=media_limits,
                file_exts=media_exts,
            )
            media_tools.append(read_pdf_tool)
            all_tools[read_pdf_tool.get_name()] = read_pdf_tool
        # End-if

        if any(ext != ".pdf" for ext in media_exts):
            read_image_tool = ReadImageTool(
                workspace_dir=self.workspace_dir,
                host=self,
                session_dir_fn=lambda: self.session.current_session_dir,
                media_limits=media_limits,
                file_exts=media_exts,
            )
            media_tools.append(read_image_tool)
            all_tools[read_image_tool.get_name()] = read_image_tool
        # End-if

        # ----- @par 2. Subagent Pool and Tools -----

        self.pool = SubAgentPool(
            safe_client=self.client,
            logger=self.session,
            config=self.config,
            all_tools=all_tools,
            max_depth=int(self.config.get("MAX_SUBAGENT_DEPTH", 3))
        )

        # Decompose one descriptions to multi (or one) tasks.
        plan_tool = PlanTool(self.client, self.config)
        # Spawn a new subagent.
        spawn_subagent = SpawnSubagentTool(self.pool)

        # ----- @par 3. Register Tools  -----

        # Added all tools to the registration list
        tool_list = [
            bash, skill_loader, md_editor,
            grep_tool, write_tool, read_tool, list_tool,
            edit_tool, plan_tool, spawn_subagent, web_search_tool,
            read_excel_tool, write_excel_tool,
            state_tool, memory_tool,
            ssh_tool, time_tool
        ]
        # Media tools join the main-agent toolset only when registered above.
        tool_list.extend(media_tools)

        for t in tool_list:
            self.tools[t.get_name()] = t

        self.tool_schemas = [
            {
                "name": t.get_name(),
                "description": t.get_description(),
                "input_schema": t.get_schema()
            } for t in self.tools.values()
        ]
    # End-def

    ##
     # ========================================
     # @section II. Message Helper Functions.
     # ========================================
     #

    ##
     # @brief A user message that is plain text (not a tool_result payload).
     #
     # @return True or False.
     # @retval True is user input msg;.
     # @retval False is not user input msg.
     #
    @staticmethod
    def _is_plain_user_msg(msg):
        if msg.get("role") != "user":
            return False
        content = msg.get("content", "")
        if isinstance(content, list):
            return not any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
        return True
    # End-def

    ##
     # @brief True if an assistant message ends with a tool_use block (handles both
     # dict blocks loaded from history.log and SDK objects in memory).
     #
     # @return True of False.
     # @retval True is end with tool_use.
     # @retval False is not end with tool_use.
     #
    @staticmethod
    def _msg_ends_with_tool_use(msg):
        if msg.get("role") != "assistant":
            return False
        content = msg.get("content", "")
        if not isinstance(content, list) or not content:
            return False
        last = content[-1]
        if isinstance(last, dict):
            return last.get("type") == "tool_use"
        return getattr(last, "type", None) == "tool_use"
    # End-def

    ##
     # @brief Trim head so it never ends with an assistant tool_use message.
     # The trimmed tool_use message stays in middle (summarized) together with its
     # matching tool_result, so the summary insertion can never split a pair.
     #
     # @param history Full message history.
     # @param head_size Desired head size before trimming.
     #
     # @return Trimmed head list (never ends with an assistant tool_use).
     #
    @staticmethod
    def _trim_head_for_tool_use(history, head_size):
        head = history[:head_size]
        while head and MyAgent._msg_ends_with_tool_use(head[-1]):
            head = head[:-1]
        return head
    # End-def

    ##
     # @brief Append-only archive filename: history length + timestamp,
     # so a second compaction at the same history length never overwrites the first.
     #
     # @param archive_dir Session archives directory.
     # @param history_len History length at compaction time.
     #
     # @return Absolute path like <archive_dir>/history_<len>_<timestamp>.json.
     #
    @staticmethod
    def _archive_path(archive_dir, history_len):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return os.path.join(archive_dir, f"history_{history_len}_{ts}.json")
    # End-def

    ##
     # @brief Absolute artifact path (resolvable by read_file
     # even when the process CWD differs from the workspace/session directory).
     #
     # @param session_dir Current session directory.
     # @param block_id Tool_use block id (sanitized into the filename).
     #
     # @return Absolute path of the offloaded artifact file.
     #
    @staticmethod
    def _artifact_path(session_dir, block_id):
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(block_id))
        return os.path.abspath(os.path.join(session_dir, "artifacts", f"{safe_id}.txt"))
    # End-def

    ##
     # ========================================
     # @section III. Context Budget & Media Slots
     # ========================================
     #

    ##
     # @brief Available token budget (returns None so media tools do not block).
     #
    def available_token_budget(self):
        return None
    # End-def

    ##
     # @brief Remaining media slots in the current request.
     #
     # @return int slots (>= 0), or None when the model has no media support.
     #
    def available_media_slots(self):
        limit = int(self.config.get("MEDIA_LIMITS", {}).get(
            "max_media_per_request", DEFAULT_MEDIA_LIMITS["max_media_per_request"]))
        used = len(self._pending_media_blocks)
        return max(limit - used, 0)
    # End-def

    ##
     # @brief Drop both memory cache fields so the next _get_memories() call
     # reloads persisted memories instead of returning a stale value.
     #
    def _invalidate_memories_cache(self):
        self._memories_key = None
        self._memories_cache = ""
    # End-def

    ##
     # @brief Load relevant memories, cached until the last plain-text user message changes.
     #
     # @return Memory string cached until the last plain user message changes;
     #          ""(empty) when no relevant memory found.
     #
    def _get_memories(self):
        key = None
        for i in range(len(self.history) - 1, -1, -1):
            msg = self.history[i]
            if self._is_plain_user_msg(msg):
                key = (i, hash(str(msg.get("content", ""))[:2000]))
                break
        # End-for
        if key is not None and key == self._memories_key:
            return self._memories_cache
        self._memories_key = key
        self._memories_cache = self.memory.load_memories_string(self.history)
        return self._memories_cache
    # End-def

    ##
     # @brief Render the [Dandelion Context] block: memory index +
     #        relevant memories digest + task state (Attention Anchor).
     #
     # @note The block is appended to the newest plain-text user message
     #       (fresh region) instead of the system prompt, so the system prompt
     #       stays byte-identical for the whole session (prefix caching).
     #
     # @return Block text starting with the [System: ...] header, or "" when
     #         there is nothing dynamic to inject (no state file & no memories).
     #
    def _render_dynamic_context(self):
        sections = []

        # 1. Memory index (global + session tiers).
        index = self.memory.get_index_text()
        if index:
            sections.append(f"Relevant Memories:\n{index}")

        # 2. Relevant memories digest (<relevant_memories> style).
        memories_content = self._get_memories()
        if memories_content:
            sections.append(memories_content)

        # 3. Task State (Attention Anchor), session-scoped. Kept LAST so the
        #    anchor sits closest to the model's next output position.
        state_file = None
        if self.session is not None:
            state_file = self.session.ensure_task_state_file()
        if state_file and os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                if isinstance(state, dict):
                    session_hint = ""
                    if getattr(self.session, "current_session_id", None):
                        session_hint = f" (session: {self.session.current_session_id})"
                    sections.append(
                        PromptBuilder.render_task_state_text(state, session_hint))
                # End-if
            except Exception as e:
                # Log failure details instead of silently dropping the section.
                print(f"[-] Warning: Failed to load task state from {state_file}: {e}")
            # End-try
        # End-if

        if not sections:
            return ""
        return (f"{_DYN_CTX_START} (auto-injected reference data)]\n"
                + "\n\n".join(sections)
                + f"\n{_DYN_CTX_END}")
    # End-def

    ##
     # @brief Append the dynamic context block to the newest plain-text user
     #        message and persist it, so tool-loop iterations (which only read
     #        history) keep seeing it inside the stable messages prefix.
     #
     # @note The target message has just been added by inject_user_message()
     #       and has never been sent, so mutating it costs zero cache.
     # @note A stale block is stripped FIRST (also when rendering produces no
     #       replacement block), so resume/re-run never leaves stale
     #       reference data inside the provider-visible history.
     # @note inject = strip(ole context) -> render(new memory/task_state) -> append(to user) -> save.
     #
    def _inject_dynamic_context(self):
        msg = self.history[-1]
        content = msg.get("content", "")
        changed = False

        # 1. Strip any stale block first, so the cleaned message is what
        #    _render_dynamic_context (memory retrieval) and the provider see;
        #    a fresh block replaces (never stacks on) the old one.
        if isinstance(content, str) and _DYN_CTX_START in content:
            content = strip_dynamic_context(content)
            msg["content"] = content
            changed = True
        # End-if

        # 2. Render the fresh block AFTER cleaning.
        block = self._render_dynamic_context()

        # 3. Append only for string content; non-string/list content
        #    (multimodal messages) is left untouched - concatenating a str
        #    block onto a list would raise TypeError.
        if block and isinstance(content, str):
            msg["content"] = content + "\n\n" + block
            changed = True
        # End-if

        # Persist even when rendering produced no replacement block, so a
        # stale block never survives a resume/re-run.
        if changed:
            self.session.save_history(self.history)
    # End-def

    ##
     # ========================================
     # @section IV. Agent-Loop
     # ========================================
     #

    ##
     # @brief LLM Round-Trip Step. A tools iterator.
     #
     # @note This function only one round chat:
     # Compact Context -> Inject Memory -> Request LLM -> Execute All Tools -> Return.
     #
     # @note Agent-loop is held by CLI:
     # CLI -> agent.step() -> Request LLM -> Execute All Tools -> Return ->
     # CLI (continue? or stop?) -> agent.step() | Stop in CLI
     #
     # @see src/utils/cli/cli.py
     #
     # @return (continue_loop, error) tuple.
     # @retval (True, None) This round executed a tool call, need to feed back
     #                      result to LLM. Continue.
     # @retval (False, None) This round is a plain text reply (or unexpected).
     #                       Breakout; the turn completed normally.
     # @retval (False, err_str) An API error occurred and all bounded retries
     #                          (_LLM_RETRY_COUNT) were exhausted. Breakout.
     #
    ##
     # @brief Display token usage metrics in muted gray (Ubuntu / VSCode SSH style).
     #
    @staticmethod
    def _print_token_usage(usage_info):
        if not usage_info:
            return
        # End-if
        cli.print(
            f"{cli.C_GRAY}    Token: {usage_info['total']} | Input: {usage_info['input']} | "
            f"Output: {usage_info['output']} | Cached: {usage_info['cached']}{cli.C_RESET}",
            level="debug"
        )
    # End-def

    def step(self):
        # 1. Build System Prompt (STATIC)
        # Dynamic content (task state / memories) is injected as a
        # [Dandelion Context] block appended to the newest plain-text
        # user message (see _inject_dynamic_context), so the system prompt
        # stays byte-identical for the whole session -> prefix cache hits.
        system_prompt = self.prompt_builder.build()
        self._last_system_prompt = system_prompt

        # 1.1 Dynamic Context Injection
        # Only at a new user turn: the newest history message is a plain-text
        # user message that has never been sent, so appending the block costs
        # zero cache. Tool-loop iterations (newest message = tool_result)
        # never re-inject; the block persists in history and stays inside the
        # stable messages prefix.
        if self.history and self._is_plain_user_msg(self.history[-1]):
            self._inject_dynamic_context()
        # End-if

        # Pure append-only copy, plus hydrated media parts read during this turn
        # (siblings of their tool_result, never nested).
        req_messages = self.history.copy()
        if self._pending_media_blocks:
            req_messages = attach_media_blocks(req_messages, self._pending_media_blocks)
        # End-if

        # 2. Main LLM API Call
        max_tokens = int(self.config.get("MAX_TOKENS", 8192))
        payload = {
            "tools": self.tool_schemas,
            "messages": req_messages,
            "max_tokens": max_tokens,
            "system": system_prompt
        }

        # Streaming with bounded retry and reactive context compaction
        resp, err = None, None
        max_attempts = _LLM_RETRY_COUNT + 1
        attempt = 0
        max_compactions = 2
        compactions_done = 0

        while attempt < max_attempts:
            attempt += 1
            resp, err = self.client.safe_stream_request(
                payload,
                log_tag=f"PRE LLM CALL - MAIN (attempt {attempt}/{max_attempts})"
            )

            # POST-call logging (per attempt: failed attempts record the error).
            self.session.log_api_call("POST LLM CALL - MAIN", resp if resp else {"error": err})

            total_tokens = 0
            if err is None:
                # Succeeded! Extract usage:
                usage_info = extract_usage_tokens(getattr(resp, "usage", None))
                self.last_usage = usage_info
                total_tokens = usage_info["total"]
            # End-if

            # Check if compression is triggered:
            # 1. Upstream context overflow error (err matches overflow keywords)
            # 2. OR measured total tokens exceeds configured max_context_tokens
            if self._if_compress(err=err, total_tokens=total_tokens) and compactions_done < max_compactions:
                compactions_done += 1
                reason = f"API overflow error: {err}" if err else f"Tokens ({total_tokens}) exceeded max_context_tokens ({self.compressor.max_context_tokens})"
                print(f"\n[!] Context threshold reached: {reason}")
                print(f"[*] Triggering reactive context compaction ({compactions_done}/{max_compactions})...")
                new_hist, stats = self.compressor.compress(self.history)
                if new_hist is not None:
                    self.history = new_hist
                    self.session.save_history(self.history)
                    self._invalidate_memories_cache()

                    # Refresh payload messages with compacted history and retry immediately
                    req_messages = self.history.copy()
                    if self._pending_media_blocks:
                        req_messages = attach_media_blocks(req_messages, self._pending_media_blocks)
                    # End-if
                    payload["messages"] = req_messages
                    print(f"[*] Retrying request with compacted context...")
                    continue
                else:
                    print("[-] Context cannot be compacted further.")
                # End-if
            # End-if

            if err is None:
                # Succeeded and within token limits: media payload consumed, clear for subsequent rounds
                self._pending_media_blocks = []
                break
            # End-if

            if attempt < max_attempts:
                wait = _LLM_RETRY_BACKOFF[attempt - 1]
                print(f"[-] API Error: {err} (attempt {attempt}/{max_attempts})")
                print(f"[-] Retrying in {wait}s ...")
                time.sleep(wait)
            # End-if
        # End-while

        if err is not None:
            print(f"[-] API Error: {err}")
            self._pending_media_blocks = []
            return False, err
        # End-if

        self.history.append({"role": "assistant", "content": resp.content})
        self.session.save_history(self.history)

        # 3. Handle Output or Tools
        if resp.stop_reason != "tool_use":
            # Turn finished: the hydrated media parts are no longer needed (the
            # pointer text stays in history, so a re-read is always possible).
            self._pending_media_blocks = []
            self._print_token_usage(self.last_usage)
            return False, None
        # End-if

        # Handle Tools
        results = []

        # Tools Iterator.
        for block in resp.content:
            if block.type != "tool_use":
                continue

            cli.print(f"\nTool requested: {block.name}", level="info")
            handler = self.tools.get(block.name)

            if handler:
                success, output = handler.execute(**block.input)
                # A successful memory write changes what _get_memories() would
                # load for the next tool-loop iteration; drop the cache so the
                # system prompt tail reflects the newly persisted memory.
                # Failed executions keep the previous cache untouched.
                if success and handler.get_name() == "remember":
                    self._invalidate_memories_cache()
            else:
                success, output = False, f"Unknown tool: {block.name}"

            # ----- Media result dispatch -----
            # A media tool returns a structured dict: the base64 block stays OUT
            # of history (only the pointer text is persisted), and the block is
            # attached to THIS round's tool_result so the model can see it now.
            if isinstance(output, dict) and output.get("kind") in ("image", "document"):
                media_cost = int(output.get("media_cost", 0) or 0)

                # The tool_result itself carries TEXT ONLY (summary + pointer):
                # history must stay free of base64, and an inline part nested
                # inside tool_result.content is dropped by the gateway. The
                # block is kept aside and hydrated as a SIBLING part of this
                # user message when the request is built.
                media_block = output.get("block")
                if media_block:
                    self._pending_media_blocks.append(media_block)
                # End-if

                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": [
                        {"type": "text", "text": output.get("summary", "")},
                        {"type": "text", "text": output.get("pointer", "")},
                    ],
                })
                cli.print(f"    Media attached: {output.get('kind')} "
                          f"(~{media_cost} tokens, history keeps the pointer only)",
                          level="info")
                continue
            # End-if

            output_str = str(output)
            cli.print(f"{cli.C_GRAY}    Result length: {len(output_str)} chars{cli.C_RESET}", level="debug")
            
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
        # End-for Agent-Loop

        if results:
            self.history.append({"role": "user", "content": results})
        else:
            self.history.append({"role": "user", "content": "You indicated a tool use but provided no valid tool calls."})

        self.session.save_history(self.history)
        self._print_token_usage(self.last_usage)
        return True, None
    # End-def

    ##
     # @brief Append a user text message to history.
     #
     # @param text User input text to append to history.
     #
    def inject_user_message(self, text):
        self.history.append({"role": "user", "content": text})
        self.session.save_history(self.history)
    # End-def

    ##
     # @brief Reload history when session changed.
     #
    def reload_history(self):
        self.history = self.session.load_history()
        # Session switched: hydrated media belongs to the previous session.
        self._pending_media_blocks = []
        # Session switched: memory relevance cache must be recomputed because
        # the session tier (and possibly the whole history) changed.
        self._memories_key = None
        self._memories_cache = ""
        # The cached system prompt was built from the PREVIOUS session's task
        # state and memory index. Drop it so the next token-budget estimate
        # (_soft_token_limit) rebuilds from the new session instead of
        # reusing stale overhead from the old branch.
        self._last_system_prompt = ""
        # NOTE: previously injected [Dandelion Context] blocks inside
        # history are intentionally NOT stripped here: keeping the messages
        # prefix byte-identical lets the server-side prefix cache survive a
        # session resume. Stale blocks are harmless (the newest injected
        # block always carries the latest state) and compaction removes them.
    # End-def
# End-class
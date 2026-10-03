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
from src.tool.media import (
    estimate_messages_tokens, count_media_in_messages, attach_media_blocks,
    DEFAULT_MEDIA_LIMITS
)
from src.utils.llm_request.calibration import usage_total_tokens, update_ratio, ratio_cap

# Create a module-level CLIPrinter instance for convenience
cli = CLIPrinter()

# Dynamic Context injection markers: the [Dandelion Context] block is
# appended to the newest plain-text user message (fresh region) instead of the
# system prompt, so the system prompt stays byte-identical for the whole
# session and DeepSeek's prefix cache keeps hitting across tool-loop iterations.
_DYN_CTX_START = "[Dandelion Context"
_DYN_CTX_END = "[Dandelion Context End]"

# Media pointer cost marker: "[... cost=<n> tokens ...]" written into history by
# the media tools (see src/tool/media/media_base.py). Used to derive the media
# share of the context ledger from history instead of a mutable counter, so
# compaction / resume / rollback all converge automatically.
_MEDIA_COST_RE = re.compile(r"\[Multimodal asset:[^\]]*?cost=([\d,]+) tokens[^\]]*\]")

# Compaction summary budget: thinking and the summary share ONE max_tokens pool,
# so the cap must cover both the provider thinking budget (EFFORT_TO_BUDGET_TOKENS
# max = 64000) and the summary itself. 128000 keeps a 64k thinking chain plus 64k
# of summary room. Overridable per profile with COMPACT_SUMMARY_MAX_TOKENS.
DEFAULT_COMPACT_SUMMARY_MAX_TOKENS = 128000

# A response shorter than this is a FAILED summarization, never content. An empty
# summary used to be written into history verbatim, silently dropping the
# compacted middle of the conversation (measured incident 2026/09/30: the model
# burned the whole 2000-token output budget on thinking and returned no text, so
# ~578k tokens of context disappeared while the log said "compacted successfully").
MIN_COMPACT_SUMMARY_CHARS = 40

# Lower bound accepted for COMPACT_SUMMARY_MAX_TOKENS (guards against a profile
# value of 0 or a negative number silently disabling the summary).
MIN_COMPACT_SUMMARY_MAX_TOKENS = 1024

# Main-agent LLM call retry policy: 1 initial attempt + _LLM_RETRY_COUNT retries.
# Uniform for all error types (400/401/429/500/connection errors) per design
# decision; exponential backoff (seconds) between attempts.
_LLM_RETRY_COUNT = 3
_LLM_RETRY_BACKOFF = (2, 4, 8)

##
 # @brief Classify a compaction summary response.
 #
 # @param text Extracted text (client.extract_text result), may be None.
 # @param stop_reason Provider stop reason ("end_turn", "max_tokens", ...).
 # @param err Error string returned by safe_request, or None.
 #
 # @return (status, reason): "ok" (usable summary), "truncated" (usable but cut
 #         by the output cap), "empty" (no usable text) or "error" (the request
 #         itself failed).
 #
 # @note Pure helper shared by the compaction flow and the local smoke harness,
 #       so the accepted/rejected matrix has exactly one definition.
 #
def classify_compaction_summary(text, stop_reason, err):
    if err:
        return "error", str(err)
    # End-if

    body = (text or "").strip()
    if len(body) < MIN_COMPACT_SUMMARY_CHARS:
        return "empty", f"summary text shorter than {MIN_COMPACT_SUMMARY_CHARS} chars"
    # End-if

    if stop_reason == "max_tokens":
        return "truncated", "summary hit the output cap"
    # End-if

    return "ok", ""
# End-def

##
 # @brief Resolve the output cap of the compaction summarization call.
 #
 # @param raw Raw COMPACT_SUMMARY_MAX_TOKENS value from the profile.
 # @param profile_max_tokens The profile MAX_TOKENS (provider output limit).
 #
 # @return int usable output cap.
 #
 # @note The cap covers thinking + summary (they share max_tokens), so it must
 #       stay above the provider thinking budget; the Anthropic provider warns
 #       when a caller sends a smaller cap on a budget-based endpoint.
 # @note The cap is never raised above the profile MAX_TOKENS: asking a provider
 #       for more than its documented output limit is a 400 on some endpoints.
 #
def resolve_compact_summary_max_tokens(raw, profile_max_tokens=None):
    if raw is None:
        value = DEFAULT_COMPACT_SUMMARY_MAX_TOKENS
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            print(f"[-] Warning: invalid COMPACT_SUMMARY_MAX_TOKENS={raw!r}; "
                  f"using {DEFAULT_COMPACT_SUMMARY_MAX_TOKENS}.")
            value = DEFAULT_COMPACT_SUMMARY_MAX_TOKENS
        # End-try
    # End-if

    if value < MIN_COMPACT_SUMMARY_MAX_TOKENS:
        print(f"[-] Warning: COMPACT_SUMMARY_MAX_TOKENS={value} is below "
              f"{MIN_COMPACT_SUMMARY_MAX_TOKENS}; clamping.")
        value = MIN_COMPACT_SUMMARY_MAX_TOKENS
    # End-if

    if profile_max_tokens is not None:
        try:
            profile_max = int(profile_max_tokens)
        except (TypeError, ValueError):
            profile_max = 0
        # End-try
        if 0 < profile_max < value:
            print(f"[*] Compaction summary cap {value} exceeds the profile "
                  f"MAX_TOKENS={profile_max}; using {profile_max}.")
            value = profile_max
        # End-if
    # End-if

    return value
# End-def

##
 # @brief Build the history message that replaces the compacted middle.
 #
 # @param archive_path Full archive file path (always recorded, so the dropped
 #                     context stays reachable by a human).
 # @param status "ok" / "truncated" => summary present; "empty" / "error" =>
 #               failure marker.
 # @param summary_content Summary text (unused on failure).
 # @param reason Failure reason (unused on success).
 # @param kept_messages Number of trailing messages kept verbatim.
 #
 # @return dict: history message (role=user).
 #
 # @note The failure form never carries an empty <conversation_summary> tag: an
 #       empty summary is indistinguishable from a real one to every later reader
 #       (the model included), which is what made the 2026/09/30 incident silent.
 #
def build_compaction_message(archive_path, status, summary_content, reason, kept_messages):
    if status in ("ok", "truncated"):
        note = " (summary truncated by the output cap)" if status == "truncated" else ""
        return {
            "role": "user",
            "content": (f"[System: Context compacted at {archive_path}{note}]\n\n"
                        f"<conversation_summary>\n{summary_content}\n</conversation_summary>")
        }
    # End-if

    return {
        "role": "user",
        "content": (f"[System: Context compaction FAILED at {archive_path} "
                    f"(reason: {reason})]\n\n"
                    f"No summary could be produced, so the earlier conversation was NOT "
                    f"summarized. It is archived verbatim at the path above and the last "
                    f"{kept_messages} message(s) were kept in context. Re-read the archive "
                    f"if that history is needed.")
    }
# End-def

##
 # @brief Sum the media cost markers found in one text fragment.
 #
 # @param text Text that may contain one or more "[Multimodal asset: ... ]"
 # pointers (adjacent pointers are merged by _normalize_messages, so a single
 # string can legitimately carry several markers).
 #
 # @return int total cost in tokens (0 when no marker is present).
 #
def _marker_cost(text):
    total = 0
    for raw in _MEDIA_COST_RE.findall(text):
        try:
            total += int(raw.replace(",", ""))
        except (TypeError, ValueError):
            continue
        # End-try
    # End-for
    return total
# End-def

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

        # Load history from the current session
        self.history = self.session.load_history()

        # ----- @par 2. Init Subsystem -----

        # Init request client with absolute paths.
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

        # ----- Media / calibration state -----
        # Media cost is derived from the pointer markers in history; only the
        # in-flight window (tool ran, result not yet appended to history) needs
        # an explicit transient value.
        self._pending_media_tokens = 0
        # Provisional cost cap for tools that must reserve budget BEFORE the
        # result exists (base64 inflates the payload by ~1.37x; the formula also
        # rounds up, so the cap stays conservative).
        self._pending_media_reserve = 0
        # Number of media blocks consumed in the current turn.
        self._pending_media_count = 0
        # Media payloads read during the current turn. They are hydrated into
        # the outgoing request as SIBLING parts of the tool_result, and are
        # never persisted in history (history keeps the pointer text only).
        self._pending_media_blocks = []
        # Local estimate captured right before the last request, used as the
        # denominator of the post-call calibration.
        self._last_send_est = 0.0

        # Post-call calibration ratio (only grows, capped by config). It corrects
        # the systematic gap between the local heuristic and the provider count;
        # it never lowers the estimate, so "local >= provider" still holds.
        self._calib_ratio = 1.0
        getter = getattr(self.session, "get_calibration_ratio", None)
        if callable(getter):
            try:
                self._calib_ratio = max(float(getter() or 1.0), 1.0)
            except Exception:
                self._calib_ratio = 1.0
            # End-try
        # End-if

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
     # @section III. Context Compaction.
     # token-aware, LLM-summarized, pair-safe
     # ========================================
     #

    ##
     # @brief Heuristic token estimate: ASCII ~4 chars/token, CJK ~1.5 chars/token.
     #
     # @param history Message list to estimate; defaults to self.history.
     #
     # @return Estimated token count (float), already calibrated.
     #
     # @note Media handling (both directions must hold):
     # - image/document blocks are NEVER character-counted and their base64 data
     #   is never touched (the provider charges by pixels, not by payload size);
     # - media cost is derived from the "[Multimodal asset: ... cost=N tokens]"
     #   markers inside history pointers, so compaction, resume and rollback all
     #   converge without any bookkeeping;
     # - only the in-flight window (tool result produced, not yet in history) is
     #   covered by the transient _pending_media_tokens / _pending_media_reserve.
     #
     # @note The calibration ratio is applied LAST and only upward:
     # ratio >= 1.0 is enforced at update time (see calibration.update_ratio).
     #
    def _estimate_tokens(self, history=None):
        history = history if history is not None else self.history

        # ----- @par 1. Text part (heuristic, unchanged) -----

        ascii_chars, non_ascii_chars, media_cost = self._scan_history(history)

        # ----- @par 2. Media part (conservative, never base64 derived) -----

        media_cost += max(self._pending_media_tokens, self._pending_media_reserve)

        base = ascii_chars / 4.0 + non_ascii_chars / 1.5 + media_cost

        # ----- @par 3. Post-call calibration (upward only) -----

        return max(base, base * self._calib_ratio)
    # End-def

    ##
     # @brief Walk a history/message tree and collect text chars + media cost.
     #
     # @param history Message list.
     #
     # @return (ascii_chars, non_ascii_chars, media_cost)
     #
     # @note Media blocks contribute their recorded cost only; a base64 payload
     # is never counted as text, which is what keeps the budget meaningful.
     #
    @staticmethod
    def _scan_history(history):
        ascii_chars = 0
        non_ascii_chars = 0
        media_cost = 0

        def scan(value):
            nonlocal ascii_chars, non_ascii_chars, media_cost

            # 1. Plain text (including media pointers).
            if isinstance(value, str):
                for ch in value:
                    if ord(ch) < 128:
                        ascii_chars += 1
                    else:
                        non_ascii_chars += 1
                    # End-if
                # End-for
                media_cost += _marker_cost(value)
                return
            # End-if

            # 2. Structured block.
            if isinstance(value, dict):
                btype = value.get("type")

                # Media block: NEVER count the payload. The cost of this block
                # is already carried by its pointer text (single source of
                # truth), so adding anything here would double count. The
                # `return` keeps base64 out of the character estimator.
                if btype in ("image", "document"):
                    return
                # End-if

                # Tool result / nested content: recurse into the payload.
                if btype == "tool_result":
                    scan(value.get("content", ""))
                    return
                # End-if

                if btype == "text":
                    scan(value.get("text", ""))
                    return
                # End-if

                # Unknown block: count its text-ish fields, skipping base64.
                for key, item in value.items():
                    if key == "data" and isinstance(item, str):
                        continue
                    # End-if
                    scan(item)
                # End-for
                return
            # End-if

            # 3. Containers.
            if isinstance(value, (list, tuple)):
                for item in value:
                    scan(item)
                # End-for
            # End-if
        # End-def scan

        for msg in history or []:
            if isinstance(msg, dict):
                scan(msg.get("content", ""))
            else:
                scan(msg)
            # End-if
        # End-for

        return ascii_chars, non_ascii_chars, media_cost
    # End-def

    ##
     # @brief Remaining token budget for media admission checks.
     #
     # @return int tokens available in the current context (>= 0).
     #
     # @note Read-only hook for the media tools; the tools never mutate the
     # budget, they only refuse when the payload does not fit.
     #
    def available_token_budget(self):
        try:
            remaining = self._soft_token_limit() - self._estimate_tokens()
        except Exception:
            return 0
        # End-try
        return max(int(remaining), 0)
    # End-def

    ##
     # @brief Remaining media slots in the current request.
     #
     # @return int slots (>= 0), or None when the model has no media support.
     #
    def available_media_slots(self):
        limit = int(self.config.get("MEDIA_LIMITS", {}).get(
            "max_media_per_request", DEFAULT_MEDIA_LIMITS["max_media_per_request"]))
        # History contributes through its pointers; the current turn contributes
        # through the transient counter (its blocks are not in history yet).
        used = count_media_in_messages(self.history) + self._pending_media_count
        return max(limit - used, 0)
    # End-def

    ##
     # @brief Update the calibration ratio from a finished request.
     #
     # @param measured Total tokens reported by the provider usage object.
     #
    def _update_calibration(self, measured):
        cap = ratio_cap(self.config)
        new_ratio, observed = update_ratio(
            self._calib_ratio, self._last_send_est, measured, cap)

        # Audit trail: the observation (including the "healthy" case where the
        # provider needed fewer tokens than the local estimate expects).
        self.session.log_api_call("CONTEXT CALIBRATION", {
            "local_est": int(self._last_send_est),
            "measured": int(measured),
            "observed_ratio": round(observed, 4),
            "ratio": round(new_ratio, 4),
            "ratio_cap": cap,
        })

        self._calib_ratio = new_ratio
        setter = getattr(self.session, "set_calibration_ratio", None)
        if callable(setter):
            try:
                setter(new_ratio)
            except Exception:
                pass
            # End-try
        # End-if
    # End-def

    ##
     # @brief History-only token budget: MAX_CONTEXT_TOKENS minus the
     # output budget (max_tokens) and the fixed request overhead
     # (system prompt + tool schemas).
     #
     # @note The static system prompt + tool schemas are fixed overhead;
     # dynamic context (memory/task state) lives in history and is counted
     # by _estimate_tokens (injected before _compact_context in step()).
     # @note The provider context window is SHARED: history + max_tokens +
     # overhead must fit inside MAX_CONTEXT_TOKENS. The output budget is
     # therefore reserved here; compaction at this limit keeps the COMBINED
     # provider request within MAX_CONTEXT_TOKENS instead of silently
     # overflowing it (which providers reject with a 400 context-length error).
     #
     # @return int: MAX_CONTEXT_TOKENS minus max_tokens minus request
     # overhead (>= 1).
     #
    def _soft_token_limit(self):
        base = int(self.config.get("MAX_CONTEXT_TOKENS", 128000))
        max_tokens = int(self.config.get("MAX_TOKENS", 8192))
        overhead = self._estimate_tokens([
            {"role": "user", "content": self._last_system_prompt or self.prompt_builder.build()},
            {"role": "user", "content": json.dumps(self.tool_schemas, ensure_ascii=False)},
        ])
        return max(int(base - max_tokens - overhead), 1)
    # End-def

    ##
     # @brief Compact context and memory save.
     #
    def _compact_context(self):
        est_tokens = self._estimate_tokens()
        soft_limit = self._soft_token_limit()

        ## 
         # @brief The token budget is the SINGLE compaction switch: 
         # when history alone is already at/over the soft limit,
         # compaction must run regardless of history length (chat turns).
         #
         # @note A short-history bypass here (e.g. len < 20) would let
         # the request overflow MAX_CONTEXT_TOKENS (and provider rejection) in
         # setups with a large system prompt + tool schemas. Short-history
         # handling lives INSIDE the compaction flow below (head+summary-only
         # fallback), never before the budget check.
        if est_tokens < soft_limit:
            return

        print(f"[*] Context limit reached (~{int(est_tokens)} tokens), compacting history via LLM...")

        # ----- @par 1. Backup -----

        # Full archive backup (append-only, restorable)
        archive_dir = os.path.join(self.session.current_session_dir, "archives")
        os.makedirs(archive_dir, exist_ok=True)
        archive_path = self._archive_path(archive_dir, len(self.history))
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(self.history, f, ensure_ascii=False, indent=2,
                      default=self.session._default_serializer)

        head_size = 5
        recent_size = 15

        ## 
         # @brief Trim head so it never ends with an assistant tool_use message.
         #
         # @note The summary (role=user) is inserted right after head, and a trailing
         # tool_use with no matching tool_result would corrupt the pairing.
         #
        head = self._trim_head_for_tool_use(self.history, head_size)
        trimmed_head_size = len(head)

        # ----- @par 2. Context Window -----

        ## @note Find a safe start for the recent window: the latest plain-text user
         # @note message within the look-back limit. Starting at a plain-text user;
         # @note message guarantees tool_use/tool_result pairs are never split.
        max_lookback = min(len(self.history) - trimmed_head_size, recent_size * 2)
        start_idx = None
        for i in range(len(self.history) - 1, len(self.history) - 1 - max_lookback, -1):
            if self._is_plain_user_msg(self.history[i]):
                start_idx = i
                break
            # End-if
        # End-for

        if start_idx is None or start_idx < trimmed_head_size:
            # Fallback: no usable plain-text user message outside the head
            # window; keep only head + summary to avoid dangling or duplicated
            # tool_result blocks.
            print("[-] No safe compaction breakpoint found; keeping head + summary only.")
            start_idx = len(self.history)
        # End-if

        recent = self.history[start_idx:]
        middle = self.history[trimmed_head_size:start_idx]

        # ----- @par 3. Summarize -----

        # Summarize head + middle (early goals are the most drift-prone part)
        summary_src = head + middle
        summary_text = json.dumps(summary_src, ensure_ascii=False, indent=2,
                                  default=self.session._default_serializer)
        if len(summary_text) > 200000:
            # Keep the head (goals/decisions) and the tail; drop the middle body.
            summary_text = (summary_text[:50000]
                            + "\n...[middle omitted from summarization input]...\n"
                            + summary_text[-150000:])
        # End-if

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

        # @note thinking and the summary share ONE max_tokens pool: the cap must
        # cover the provider thinking budget (up to 64k) plus the summary itself,
        # hence the 128000 default. A cap the model cannot finish within comes back
        # as stop_reason=max_tokens and is handled by the validation below.
        summary_max_tokens = resolve_compact_summary_max_tokens(
            self.config.get("COMPACT_SUMMARY_MAX_TOKENS"),
            self.config.get("MAX_TOKENS"),
        )

        summary_payload = {
            "messages": [{"role": "user", "content": summary_prompt + "\n\nHistory:\n" + summary_text}],
            "max_tokens": summary_max_tokens,
            "system": "You are a concise memory summarization AI."
        }

        # ----- @par 4. Request + validation -----

        resp, err = self.client.safe_request(summary_payload, log_tag="COMPRESSION SUMMARY")
        summary_response_text = self.client.extract_text(resp.content) if resp else ""
        status, reason = classify_compaction_summary(
            summary_response_text,
            getattr(resp, "stop_reason", None) if resp else None,
            err,
        )
        summary_content = summary_response_text if status in ("ok", "truncated") else ""

        if status == "empty":
            print(f"[-] Compression failed: {reason}. Writing an explicit failure "
                  f"marker instead of an empty summary.")
        elif status == "error":
            print(f"[-] Compression failed: {reason}.")
        elif status == "truncated":
            print(f"[-] Warning: compression summary hit the output cap "
                  f"(max_tokens={summary_max_tokens}) and may be incomplete; "
                  f"consider raising COMPACT_SUMMARY_MAX_TOKENS.")
        # End-if

        # ----- @par 5. History assembly -----

        summary_msg = build_compaction_message(archive_path, status, summary_content,
                                               reason, len(recent))

        self.history = head + [summary_msg] + recent
        self.session.save_history(self.history)

        # Invalidate memories cache: history changed (plain-text user messages may shift).
        self._invalidate_memories_cache()
        if status in ("ok", "truncated"):
            print(f"[+] Context compacted successfully (summary {len(summary_content)} chars).")
        else:
            print("[!] Context compaction finished WITHOUT a summary (failure marker "
                  "written; the archive keeps the dropped history).")
        # End-if

        # ----- @par 6. Observability -----
        #
        # @note The main loop logs a POST record per call; the summarization call
        # used to log its request only, which made a failed or truncated summary
        # invisible in api.log (the 2026/09/30 incident had to be reconstructed
        # from history afterwards).
        self.session.log_api_call("POST LLM CALL - COMPRESSION SUMMARY", {
            "status": status,
            "reason": reason,
            "stop_reason": getattr(resp, "stop_reason", None) if resp else None,
            "block_types": [getattr(b, "type", None)
                            for b in (getattr(resp, "content", None) or [])],
            "summary_chars": len(summary_content),
            "max_tokens": summary_max_tokens,
            "kept_messages": len(recent),
            "usage_total": usage_total_tokens(getattr(resp, "usage", None)) if resp else 0,
        })

        # ----- @par 7. Post -----

        # Post-compaction guard: if the budget is still exceeded (e.g. the
        # configured MAX_CONTEXT_TOKENS is below the system-prompt + tools
        # overhead), warn loudly once per compaction instead of letting every
        # subsequent step re-trigger an LLM summarization call in a loop.
        remaining = self._estimate_tokens()
        if remaining >= soft_limit:
            print(f"[-] Warning: history still ~{int(remaining)} tokens after "
                  f"compaction (soft limit ~{int(soft_limit)}). Consider raising "
                  "MAX_CONTEXT_TOKENS or reducing system-prompt/tool overhead.")
    # End-def _compact_context

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

        # 1.2 Check context budget EVERY turn, AFTER injection so the dynamic
        #     block (memory + task state) is counted in the token budget.
        self._compact_context()

        # Pure append-only copy, ZERO mutations, plus the hydrated media parts
        # read during this turn (siblings of their tool_result, never nested).
        req_messages = self.history.copy()
        if self._pending_media_blocks:
            req_messages = attach_media_blocks(req_messages, self._pending_media_blocks)
        # End-if

        # 2. Main LLM API Call
        # Send-time safety clamp: even if the heuristic estimate undershoots
        # (or compaction was skipped), never let history + output + overhead
        # exceed MAX_CONTEXT_TOKENS - degrade the output size instead of
        # getting a provider 400 context-length rejection.
        max_tokens = int(self.config["MAX_TOKENS"])
        remaining_budget = (self._soft_token_limit()
                            - self._estimate_tokens() + max_tokens)
        if remaining_budget < max_tokens:
            max_tokens = max(int(remaining_budget), 1)
        # End-if
        payload = {
            "tools": self.tool_schemas,
            "messages": req_messages,
            "max_tokens": max_tokens,
            "system": system_prompt
        }

        # Denominator of the post-call calibration: the local estimate of the
        # payload that is about to be sent (media markers included). Captured
        # here because later mutation of history must not affect it.
        self._last_send_est = self._estimate_tokens()

        # PRE-call logging is now handled inside SafeLLMClient -> Provider
        # (after thinking injection), so we only log POST here.

        # Streaming, with bounded retry: 1 initial attempt + _LLM_RETRY_COUNT
        # retries (backoff _LLM_RETRY_BACKOFF). Only the API call itself is
        # retried: the payload is built once and never re-injected or
        # re-compacted between attempts (same semantics as route_request).
        # Each attempt is logged with an attempt tag (api.log audit trail).
        resp, err = None, None
        for attempt in range(1, _LLM_RETRY_COUNT + 2):
            resp, err = self.client.safe_stream_request(
                payload,
                log_tag=f"PRE LLM CALL - MAIN (attempt {attempt}/{_LLM_RETRY_COUNT + 1})"
            )

            # POST-call logging (per attempt: failed attempts record the error).
            self.session.log_api_call("POST LLM CALL - MAIN", resp if resp else {"error": err})

            if err is None:
                break
            # End-if
            if attempt <= _LLM_RETRY_COUNT:
                wait = _LLM_RETRY_BACKOFF[attempt - 1]
                print(f"[-] API Error: {err} (attempt {attempt}/{_LLM_RETRY_COUNT + 1})")
                print(f"[-] Retrying in {wait}s ...")
                time.sleep(wait)
            # End-if
        # End-for

        if err is not None:
            print(f"[-] API Error: {err}")
            self._pending_media_blocks = []
            return False, err

        # ----- @par 2-b. Post-call context calibration -----
        # The provider reports the real input size; the local heuristic is then
        # corrected UPWARD only (ratio >= 1) so "local >= provider" always holds.
        # A response without usage (other SDK paths) simply skips the step.
        measured = usage_total_tokens(getattr(resp, "usage", None))
        if measured > 0:
            self._update_calibration(measured)
        # End-if

        self.history.append({"role": "assistant", "content": resp.content})
        self.session.save_history(self.history)

        # 3. Handle Output or Tools
        if resp.stop_reason != "tool_use":
            # Turn finished: the hydrated media parts are no longer needed (the
            # pointer text stays in history, so a re-read is always possible).
            self._pending_media_blocks = []
            return False, None

        # Handle Tools
        results = []

        # Media blocks produced by this round, kept OUT of history (only their
        # pointer text is persisted) and reserved against the token budget.
        pending_media_cost = 0
        pending_media_reserve = 0
        pending_media_count = 0

        # Tools Iterator.
        for block in resp.content:
            if block.type != "tool_use":
                continue

            cli.print(f"\nTool requested: {block.name}", level="info")
            handler = self.tools.get(block.name)

            if handler:
                # Media tools must check the budget BEFORE building the payload
                # (the result it produces cannot be discarded afterwards), so the
                # already-reserved cost of this round is exposed first.
                self._pending_media_reserve = pending_media_reserve
                self._pending_media_count = pending_media_count
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
                pending_media_cost += media_cost
                pending_media_reserve = max(pending_media_reserve, media_cost)
                pending_media_count += 1

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
        # End-for Agent-Loop

        if results:
            self.history.append({"role": "user", "content": results})
        else:
            self.history.append({"role": "user", "content": "You indicated a tool use but provided no valid tool calls."})

        self.session.save_history(self.history)

        # The media cost now lives in the history pointers (the markers), so the
        # transient value must be dropped here: keeping it would double count
        # the very same media for every later estimate.
        self._pending_media_tokens = 0
        self._pending_media_reserve = 0
        self._pending_media_count = 0
        return True, None
    # End-def

    ##
     # @brief Append a user text message to history and run a context budget check.
     #
     # @param text User input text to append to history.
     #
    def inject_user_message(self, text):
        self.history.append({"role": "user", "content": text})
        self.session.save_history(self.history)
        self._compact_context()
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
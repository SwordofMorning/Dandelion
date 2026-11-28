##
 # @file packages/dandelion-core/src/dandelion_core/agent/agent.py
 # @date 2026/11/28
 #
 # @brief Main-agent request and tool-step execution.
 # Coordinates persisted history, model requests, cooperative cancellation,
 # ordered tool dispatch, media attachments and usage accounting for both
 # terminal and remote callers.
 #
 # @note Agent runtime call chain:
 #   CLI or Remote turn runner
 #     -> MyAgent.step()                 # one LLM round trip and tool dispatch
 #     -> PromptBuilder.build()          # static system instructions
 #     -> _inject_dynamic_context()      # memories and session task state
 #     -> SafeLLMClient.safe_stream_request()
 #     -> ContextCompressor.compress()   # when context overflow requires it
 #     -> dispatch_tool_calls()          # memory/state/filesystem/delegation
 #     -> history append (tool_result)   # loop continues until stop_reason != tool_use
 #
 # @note The outer runner owns turn completion and cancellation checkpoints;
 # step() keeps each tool call paired with its result before returning.
 #

import json
import os
import re
import shutil
import time

from ..runtime import Runtime
from ..subagent.dispatch import dispatch_tool_calls
from ..tool.media import attach_media_blocks, DEFAULT_MEDIA_LIMITS
from ..utils import SafeLLMClient
from ..utils.llm_provider.blocks import PartialStreamError, RequestCancelledError
from .compress import ContextCompressor, extract_usage_tokens
from .memory import MemoryManager, _UNSET
from .prune import ContextPruner, block_field
from .skill import SkillManager
from .sysprompt import PromptBuilder
from .tools import build_tools

# Dynamic Context injection markers: the [Dandelion Context] block is
# appended to the newest plain-text user message (fresh region) instead of the
# system prompt, so the system prompt stays byte-identical for the whole
# session and prefix cache keeps hitting across tool-loop iterations.
_DYN_CTX_START = "[Dandelion Context"
_DYN_CTX_END = "[Dandelion Context End]"

# Main-agent LLM call retry policy: 1 initial attempt + _LLM_RETRY_COUNT retry.
# _native.Provider already performs 3 internal attempts on transport/429/5xx
# errors, so keeping outer retries bounded prevents 4x3=12 retry amplification.
_LLM_RETRY_COUNT = 1
_LLM_RETRY_BACKOFF = (2,)
_RETRY_POLL_INTERVAL_SEC = 0.1
_NON_RETRYABLE_STATUS_RE = re.compile(
    r"\b(?:http\s*|error\s+code:\s*|status\s*=?\s*)(?:400|401|403|404|422)\b",
    re.IGNORECASE,
)


##
 # @brief Check whether an API error represents a deterministic 4xx client failure.
 #
 # @param err Error string returned by the provider.
 #
 # @return True for deterministic HTTP 400/401/403/404/422 errors.
 #
def _is_non_retryable_error(err):
    if not err:
        return False
    # End-if
    return bool(_NON_RETRYABLE_STATUS_RE.search(str(err)))
# End-def


##
 # @brief Strip previously injected [Dandelion Context] blocks from a user
 #        message content string (all complete blocks, stacked included).
 #
 # @param content User message content string.
 #
 # @return Content with all injected blocks removed.
 #
def strip_dynamic_context(content):
    while True:
        start = content.find(_DYN_CTX_START)
        if start == -1:
            return content
        # End-if
        end = content.find(_DYN_CTX_END, start)
        if end == -1:
            return content[:start].rstrip()
        # End-if
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
     # @param workspace_dir Current workspace root used to confine agent tools.
     # @param runtime Optional BaseRuntime instance for output, cancellation and approval.
     # @param tool_factory Optional callable constructing the main-agent tool mapping.
     # @param prompt_builder_cls Optional BasePromptBuilder subclass or factory.
     # @param config_dir Optional operator config directory (e.g. <root>/.env for Remote).
     # @param host Host tag string ("core", "cli", "remote") for hot-plug tool discovery.
     # @param tool_dirs Optional ordered list of operator hot-plug tool directories.
     # @param protected_dirs Optional iterable of sandbox-protected directory paths.
     #
     # @return None.
     #
    def __init__(self, config, session_manager, workspace_dir, runtime=None,
                 tool_factory=None, prompt_builder_cls=None, config_dir=None,
                 host="core", tool_dirs=None, protected_dirs=None):
        # ----- @par 1. Init members -----
        self.config = config
        self.session = session_manager
        self.workspace_dir = workspace_dir
        self.config_dir = config_dir
        self.host = str(host or "core").strip().lower() or "core"
        self.tool_dirs = list(tool_dirs) if tool_dirs else []
        self.protected_dirs = tuple(protected_dirs) if protected_dirs else ()
        self.runtime = runtime or Runtime()
        self.tool_factory = tool_factory or build_tools
        self.pool = None
        self.toolset_registry = None
        self.extension_tools = {}
        self.tool_load_errors = []
        self.sub_concurrency = max(int(config.get("SUB_CONCURRENCY", 4)), 1)
        self.error_count = 0
        self.thinking = str(config.get("THINKING", "disabled")).strip().lower()
        self.effort = str(config.get("EFFORT", "medium")).strip().lower()
        self.compat = str(config.get("COMPAT", "auto")).strip().lower()

        # Load history from the current session.
        self.history = self.session.load_history()

        # ----- @par 2. Init Subsystem -----
        self.client = SafeLLMClient(
            api_key=self.config["ANTHROPIC_API_KEY"],
            base_url=self.config["ANTHROPIC_BASE_URL"],
            model_id=self.config["MODEL_ID"],
            sdk_type=self.config.get("SDK_TYPE", "Message"),
            all_models=self.config.get("ALL_MODELS", []),
            sub_list=self.config.get("SUB_LIST", []),
            sub_depth=int(self.config.get("SUB_DEPTH", 1)),
            thinking=self.thinking,
            effort=self.effort,
            compat=self.compat,
            logger=self.session,
            runtime=self.runtime,
        )

        # Memory is two-tier: global (llm/memory/) + current session (.log/sess_<id>/memory/).
        self.memory = MemoryManager(
            memory_dir=os.path.join(self.workspace_dir, "llm", "memory"),
            session_manager=self.session,
            safe_client=self.client,
            logger=self.session,
            runtime=self.runtime,
            memory_config=self.config.get("MEMORY"),
        )
        self.skill = SkillManager(
            skill_dir=os.path.join(self.workspace_dir, "llm", "skill"),
            runtime=self.runtime,
        )
        builder_cls = prompt_builder_cls or PromptBuilder
        self.prompt_builder = builder_cls(
            self.memory, self.skill, self.config, self.workspace_dir,
            session_manager=self.session,
        )

        self._memories_key = None
        self._memories_cache = ""
        self._memory_query = _UNSET

        # ----- Context compressor, pruner & usage metrics -----
        self.compressor = ContextCompressor(self.client, self.session, self.config, runtime=self.runtime)
        self.pruner = ContextPruner(self.client, self.session, runtime=self.runtime, config=self.config)
        self._if_compress = self.compressor.condition
        self.last_usage = {"total": 0, "input": 0, "output": 0, "cached": 0}

        self._pending_media_blocks = []

        # ----- @par 3. Load Tools -----
        self._init_tools()
    # End-def

    ##
     # @brief Init tools for Main Agent and Subagents (pool).
     #
     # @return None.
     #
    def _init_tools(self):
        self.tools = self.tool_factory(self)
        for tool in self.tools.values():
            tool.runtime = self.runtime
            if self.protected_dirs and not getattr(tool, "protected_dirs", None):
                tool.protected_dirs = self.protected_dirs
            # End-if
        # End-for
        self.tool_schemas = [
            {
                "name": tool.get_name(),
                "description": tool.get_description(),
                "input_schema": tool.get_schema(),
            }
            for tool in self.tools.values()
        ]
        self.prompt_builder.tool_names = frozenset(self.tools)
        ts_registry = getattr(self.pool, "toolset_registry", None) or self.toolset_registry
        self.prompt_builder.toolset_registry = ts_registry
        self.prompt_builder.worker_tool_names = (
            frozenset(self.pool.all_tools) if self.pool is not None else None
        )
        ext_tools = dict(self.extension_tools) if self.extension_tools else {}
        if ext_tools:
            visible_names = set(self.tools.keys())
            if self.pool is not None:
                visible_names.update(self.pool.all_tools.keys())
            # End-if
            ext_tools = {k: v for k, v in ext_tools.items() if k in visible_names}
        # End-if
        self.prompt_builder.extension_tools = ext_tools
    # End-def

    ##
     # @brief Live-reload hot-plugged tools, SubAgent pool, schemas, and prompt metadata.
     #
     # Purges all dynamically loaded _dandelion_ext_* modules from sys.modules and
     # re-runs tool_factory(self) so newly dropped or edited plugin files take
     # effect without restarting the process.
     #
     # @return Summary dictionary with purged_modules, total_tools, tool_names,
     #         extension_tools, worker_toolsets, and errors.
     #
    def reload_tools(self) -> dict:
        from ..tool.loader import ToolLoader

        purged = ToolLoader.purge_loaded_modules()
        self.extension_tools = {}
        self.tool_load_errors = []
        self._init_tools()
        ts_registry = getattr(self.pool, "toolset_registry", None) or self.toolset_registry
        return {
            "purged_modules": purged,
            "total_tools": len(self.tools),
            "tool_names": sorted(self.tools.keys()),
            "extension_tools": sorted(self.extension_tools.keys()),
            "worker_toolsets": ts_registry.names() if ts_registry is not None else [],
            "errors": list(self.tool_load_errors),
        }
    # End-def

    ##
     # ========================================
     # @section II. Message Helper Functions.
     # ========================================
     #

    ##
     # @brief True when a message is a plain-text user message (not a tool_result payload).
     #
     # @param msg Message dictionary.
     #
     # @return True when plain user input, False otherwise.
     #
    @staticmethod
    def _is_plain_user_msg(msg):
        if msg.get("role") != "user":
            return False
        # End-if
        content = msg.get("content", "")
        if isinstance(content, list):
            return not any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
        # End-if
        return True
    # End-def

    ##
     # @brief Absolute artifact path resolvable regardless of process CWD.
     #
     # @param session_dir Current session directory.
     # @param block_id Tool_use block id sanitized into the filename.
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
     # @return None.
     #
    def available_token_budget(self):
        return None
    # End-def

    ##
     # @brief Remaining media slots in the current request.
     #
     # @return Non-negative integer slot count.
     #
    def available_media_slots(self):
        limit = int(self.config.get("MEDIA_LIMITS", {}).get(
            "max_media_per_request", DEFAULT_MEDIA_LIMITS["max_media_per_request"]))
        used = len(self._pending_media_blocks)
        return max(limit - used, 0)
    # End-def

    ##
     # @brief Drop both memory cache fields so the next _get_memories() call reloads.
     #
     # @return None.
     #
    def _invalidate_memories_cache(self):
        self._memories_key = None
        self._memories_cache = ""
    # End-def

    ##
     # @brief Load relevant memories, cached until the last plain-text user message changes.
     #
     # @return Memory string, or "" when no relevant memory is found.
     #
    def _get_memories(self):
        key = None
        for i in range(len(self.history) - 1, -1, -1):
            msg = self.history[i]
            if self._is_plain_user_msg(msg):
                key = (i, hash(str(msg.get("content", ""))[:2000]))
                break
            # End-if
        # End-for
        key = (key, self._memory_query if self._memory_query is not _UNSET else "auto")
        if key is not None and key == self._memories_key:
            return self._memories_cache
        # End-if
        self._memories_key = key
        self._memories_cache = self.memory.load_memories_string(self.history, query_override=self._memory_query)
        return self._memories_cache
    # End-def

    ##
     # @brief Render the [Dandelion Context] block: memory index, relevant memories,
     #        optional frontend context, and task state (Attention Anchor).
     #
     # @return Rendered block text, or "" when there is nothing dynamic to inject.
     #
    def _render_dynamic_context(self):
        sections = []

        index = self.memory.get_index_text()
        if index:
            sections.append(f"Relevant Memories:\n{index}")
        # End-if

        memories_content = self._get_memories()
        if memories_content:
            sections.append(memories_content)
        # End-if

        extra_context = self.prompt_builder.render_extra_dynamic_context()
        if extra_context:
            sections.append(extra_context)
        # End-if

        state_file = None
        if self.session is not None:
            state_file = self.session.ensure_task_state_file()
        # End-if
        if state_file and os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                # End-with
                if isinstance(state, dict):
                    session_hint = ""
                    if getattr(self.session, "current_session_id", None):
                        session_hint = f" (session: {self.session.current_session_id})"
                    # End-if
                    sections.append(
                        self.prompt_builder.render_task_state_text(state, session_hint)
                    )
                # End-if
            except Exception as e:
                self.runtime.emit(f"[-] Warning: Failed to load task state from {state_file}: {e}")
            # End-try
        # End-if

        if not sections:
            return ""
        # End-if
        return (
            f"{_DYN_CTX_START} (auto-injected reference data)]\n"
            + "\n\n".join(sections)
            + f"\n{_DYN_CTX_END}"
        )
    # End-def

    ##
     # @brief Append the dynamic context block to the newest plain-text user message.
     #
     # @return None.
     #
    def _inject_dynamic_context(self):
        msg = self.history[-1]
        content = msg.get("content", "")
        changed = False

        if isinstance(content, str) and _DYN_CTX_START in content:
            content = strip_dynamic_context(content)
            msg["content"] = content
            changed = True
        # End-if

        block = self._render_dynamic_context()

        if block and isinstance(content, str):
            msg["content"] = content + "\n\n" + block
            changed = True
        # End-if

        if changed:
            self.session.save_history(self.history)
        # End-if
    # End-def

    ##
     # ========================================
     # @section IV. Agent-Loop
     # ========================================
     #

    ##
     # @brief Display token usage metrics through the runtime output adapter.
     #
     # @param usage_info Token usage dictionary from the LLM response.
     #
     # @return None.
     #
    def _print_token_usage(self, usage_info):
        if not usage_info:
            return
        # End-if
        self.runtime.emit(
            f"    Token: {usage_info['total']} | Input: {usage_info['input']} | "
            f"Output: {usage_info['output']} | Cached: {usage_info['cached']}"
        )
    # End-def

    ##
     # @brief Sleep for up to wait_seconds while polling runtime.cancelled() in 100ms slices.
     #
     # @param wait_seconds Total backoff duration in seconds.
     #
     # @return True when interrupted early by runtime.cancelled(), False otherwise.
     #
    def _sleep_with_cancel(self, wait_seconds):
        deadline = time.monotonic() + float(wait_seconds)
        while time.monotonic() < deadline:
            if self.runtime.cancelled():
                return True
            # End-if
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # End-if
            time.sleep(min(_RETRY_POLL_INTERVAL_SEC, remaining))
        # End-while
        return bool(self.runtime.cancelled())
    # End-def

    ##
     # @brief LLM Round-Trip Step: one model call and ordered tool dispatch.
     #
     # @return (continue_loop, error) tuple.
     # @retval (True, None) Tool calls executed and appended; loop should continue.
     # @retval (False, None) Plain-text reply finished normally.
     # @retval (False, err_str) API error or cooperative cancellation ended the step.
     #
    def step(self):
        # 1. Build System Prompt (STATIC)
        system_prompt = self.prompt_builder.build()

        # 1.1 Dynamic Context Injection
        if self.history and self._is_plain_user_msg(self.history[-1]):
            self._inject_dynamic_context()
        # End-if

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
            "system": system_prompt,
        }

        resp, err = None, None
        max_attempts = _LLM_RETRY_COUNT + 1
        attempt = 0
        max_compactions = 2
        compactions_done = 0

        while attempt < max_attempts:
            if self.runtime.cancelled():
                err = RequestCancelledError("Request cancelled by runtime interrupt.")
                break
            # End-if
            attempt += 1
            if attempt == 1 and not self.runtime.echo:
                self.runtime.emit("Thinking...")
            # End-if
            resp, err = self.client.safe_stream_request(
                payload,
                log_tag=f"PRE LLM CALL - MAIN (attempt {attempt}/{max_attempts})",
                echo=self.runtime.echo,
                cancelled=self.runtime.cancelled,
            )

            self.session.log_api_call("POST LLM CALL - MAIN", resp if resp else {"error": err})

            if err is None:
                self._pending_media_blocks = []
                usage_info = extract_usage_tokens(getattr(resp, "usage", None))
                self.last_usage = usage_info
                break
            # End-if

            if isinstance(err, (PartialStreamError, RequestCancelledError)) or self.runtime.cancelled():
                break
            # End-if

            # Check if this error is due to context window overflow.
            if self.compressor.is_overflow_error(err) and compactions_done < max_compactions:
                compactions_done += 1
                self.runtime.emit(f"\n[!] Context overflow detected: {err}")
                self.runtime.emit(f"[*] Triggering reactive context compaction ({compactions_done}/{max_compactions})...")
                new_hist, stats = self.compressor.compress(self.history)
                if stats.get("status") == "archive_failed":
                    self._pending_media_blocks = []
                    return False, f"Compaction archive failed: {stats.get('error')}"
                # End-if

                if new_hist is not None:
                    self.history = new_hist
                    self.session.save_history(self.history)
                    self._invalidate_memories_cache()

                    req_messages = self.history.copy()
                    if self._pending_media_blocks:
                        req_messages = attach_media_blocks(req_messages, self._pending_media_blocks)
                    # End-if
                    payload["messages"] = req_messages
                    self.runtime.emit("[*] Retrying request with compacted context...")
                    continue
                else:
                    self.runtime.emit("[-] Context cannot be compacted further.")
                # End-if
            # End-if

            if _is_non_retryable_error(err):
                break
            # End-if

            if attempt < max_attempts:
                wait = _LLM_RETRY_BACKOFF[attempt - 1]
                self.runtime.emit(f"[-] API Error: {err} (attempt {attempt}/{max_attempts})")
                self.runtime.emit(f"[-] Retrying in {wait}s ...")
                if self._sleep_with_cancel(wait):
                    err = RequestCancelledError("Request cancelled by runtime interrupt.")
                    break
                # End-if
            # End-if
        # End-while

        if err is not None:
            if not isinstance(err, RequestCancelledError):
                self.runtime.emit(f"[-] API Error: {err}")
            # End-if
            self._pending_media_blocks = []
            return False, err
        # End-if

        self.history.append({"role": "assistant", "content": resp.content})
        self.session.save_history(self.history)

        # Post-response compaction check.
        total_tokens = self.last_usage.get("total", 0)
        if self._if_compress(err=None, total_tokens=total_tokens):
            reason = f"Tokens ({total_tokens}) exceeded max_context_tokens ({self.compressor.max_context_tokens})"
            self.runtime.emit(f"\n[!] Context threshold reached: {reason}")
            new_hist, stats = self.compressor.compress(self.history)
            if stats.get("status") == "archive_failed":
                if self.history and self.history[-1].get("role") == "assistant":
                    self.history.pop()
                    self.session.save_history(self.history)
                # End-if
                self._pending_media_blocks = []
                return False, f"Compaction archive failed: {stats.get('error')}"
            # End-if
            if new_hist is not None:
                self.history = new_hist
                self.session.save_history(self.history)
                self._invalidate_memories_cache()
            # End-if
        # End-if

        # 3. Handle Output or Tools
        if resp.stop_reason != "tool_use":
            self._pending_media_blocks = []
            self._print_token_usage(self.last_usage)
            return False, None
        # End-if

        # Cooperative cancel guard before dispatching any potentially destructive tools
        # (e.g. if TurnInterrupt fired during post-response compaction or stream tail).
        if self.runtime.cancelled():
            self.pop_unpaired_tool_use_tail()
            self.session.save_history(self.history)
            self._pending_media_blocks = []
            return False, RequestCancelledError("Request cancelled by runtime interrupt.")
        # End-if

        results = []
        outcomes = dispatch_tool_calls(
            self.tools, resp.content, max_workers=self.sub_concurrency,
            on_start=lambda b: self.runtime.emit(f"\nTool requested: {b.name}"),
        )
        for block, success, output in outcomes:
            if success and block.name in ("remember", "forget"):
                self._invalidate_memories_cache()
            # End-if

            if isinstance(output, dict) and output.get("kind") in ("image", "document"):
                media_cost = int(output.get("media_cost", 0) or 0)
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
                self.runtime.emit(
                    f"    Media attached: {output.get('kind')} "
                    f"(~{media_cost} tokens, history keeps the pointer only)"
                )
                continue
            # End-if

            output_str = str(output)
            self.runtime.emit(f"    Result length: {len(output_str)} chars")

            MAX_INLINE_CHARS = 8000
            if len(output_str) > MAX_INLINE_CHARS:
                artifact_path = self._artifact_path(self.session.current_session_dir, block.id)
                os.makedirs(os.path.dirname(artifact_path), exist_ok=True)

                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(output_str)
                # End-with

                trunc_output = output_str[:MAX_INLINE_CHARS]
                trunc_output += (
                    f"\n\n... [OUTPUT TRUNCATED. Full {len(output_str)} chars output "
                    f"saved to {artifact_path}. Use read_file to read specific missing parts.]"
                )
                output_str = trunc_output
            # End-if

            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output_str})
        # End-for

        if results:
            self.history.append({"role": "user", "content": results})
        else:
            self.history.append({"role": "user", "content": "You indicated a tool use but provided no valid tool calls."})
        # End-if

        self.session.save_history(self.history)
        self._print_token_usage(self.last_usage)
        return True, None
    # End-def

    ##
     # @brief Append a user text message to history.
     #
     # @param text User input text to append to history.
     # @param memory_query Optional explicit query override for memory retrieval.
     #
     # @return None.
     #
    def inject_user_message(self, text, *, memory_query=_UNSET):
        self._memory_query = memory_query
        self.history.append({"role": "user", "content": text})
        self.session.save_history(self.history)
    # End-def

    ##
     # @brief Reload history when session changed.
     #
     # @return None.
     #
    def reload_history(self):
        self.history = self.session.load_history()
        self._pending_media_blocks = []
        self._memories_key = None
        self._memories_cache = ""
        self._memory_query = _UNSET
    # End-def

    ##
     # @brief Pop trailing assistant messages whose tool_use has no tool_result yet.
     #
     # @note Does not save history; callers persist after their own edits.
     #
     # @return True when history was modified.
     #
    def pop_unpaired_tool_use_tail(self):
        changed = False
        while self.history and self.history[-1].get("role") == "assistant":
            tail_content = self.history[-1].get("content", [])
            if not (isinstance(tail_content, list) and any(
                    block_field(b, "type") == "tool_use" for b in tail_content)):
                break
            # End-if
            self.history.pop()
            changed = True
        # End-while
        return changed
    # End-def

    ##
     # @brief Truncate the running turn at the current step boundary while keeping progress.
     #
     # @param note Closing assistant marker appended when history ends on a user turn.
     #
     # @return None.
     #
    def stop_turn(self, note="[Turn stopped by user.]"):
        self._pending_media_blocks = []
        changed = self.pop_unpaired_tool_use_tail()
        if self.history and self.history[-1].get("role") == "user":
            self.history.append({
                "role": "assistant",
                "content": [{"type": "text", "text": str(note)}],
            })
            changed = True
        # End-if
        if changed:
            self.session.save_history(self.history)
        # End-if
        self.session.log_api_call("TURN STOPPED", {
            "status": "stopped",
            "note": str(note),
            "history_messages": len(self.history),
        })
    # End-def

    ##
     # @brief Restore the session state captured before the turn started.
     #
     # @param backup Optional SessionBackup instance; defaults to current session.
     # @param trigger Reason tag recorded in the audit log.
     #
     # @return (success_bool, restored_items, failed_items) tuple.
     #
    def rollback_turn(self, backup=None, trigger=""):
        from ..utils.logging.backup import SessionBackup

        snap = backup if backup is not None else SessionBackup(
            self.session.current_session_dir,
            session_id=getattr(self.session, "current_session_id", None),
        )
        if not snap.is_valid():
            return False, [], ["invalid_backup"]
        # End-if
        restored, failed = snap.restore()
        if failed:
            self.session.log_api_call("TURN ROLLBACK", {
                "trigger": str(trigger),
                "status": "failed",
                "failed": failed,
            })
            return False, restored, failed
        # End-if

        # Handle mid-turn prune_context(rollover=True): restore session pointer
        # back to backup's original session and remove the orphaned rollover dir.
        cur_dir = getattr(self.session, "current_session_dir", None)
        snap_dir = snap.session_dir
        removed_rollover_id = None
        if cur_dir and snap_dir and os.path.abspath(cur_dir) != os.path.abspath(snap_dir):
            orig_id = snap.session_id or os.path.basename(os.path.normpath(snap_dir))
            switched = False
            if hasattr(self.session, "switch_session"):
                switched = bool(self.session.switch_session(orig_id))
            # End-if
            if not switched:
                self.session.current_session_id = orig_id
                self.session.current_session_dir = snap_dir
            # End-if

            log_root = getattr(self.session, "log_dir", None)
            if log_root:
                abs_log = os.path.realpath(log_root)
                abs_cur = os.path.realpath(cur_dir)
                abs_snap = os.path.realpath(snap_dir)
                try:
                    inside_log = os.path.commonpath([abs_log, abs_cur]) == abs_log
                except ValueError:
                    inside_log = False
                # End-try
                if inside_log and abs_cur != abs_log and abs_cur != abs_snap and os.path.isdir(abs_cur):
                    try:
                        shutil.rmtree(abs_cur)
                        removed_rollover_id = os.path.basename(abs_cur)
                    except OSError:
                        pass
                    # End-try
                # End-if
            # End-if
        # End-if

        self.reload_history()
        log_payload = {
            "trigger": str(trigger),
            "status": "done",
            "restored": restored,
            "failed": failed,
            "history_messages": len(self.history),
        }
        if removed_rollover_id:
            log_payload["removed_rollover_session"] = removed_rollover_id
        # End-if
        self.session.log_api_call("TURN ROLLBACK", log_payload)
        return True, restored, failed
    # End-def
# End-class

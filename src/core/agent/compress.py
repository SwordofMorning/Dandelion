##
 # @file src/core/agent/compress.py
 # @date 2026/10/12
 #
 # @brief Reactive context compaction module.
 # Handles context overflow detection, 80% smooth sliding compression window,
 # incremental XML summarization, and archive persistence.
 #

import datetime
import json
import os
from src.core.runtime import Runtime

DEFAULT_COMPACT_SUMMARY_MAX_TOKENS = 128000
MIN_COMPACT_SUMMARY_CHARS = 40
MIN_COMPACT_SUMMARY_MAX_TOKENS = 1024
DEFAULT_HEAD_SIZE = 2
DEFAULT_RECENT_SIZE = 10
DEFAULT_TARGET_REDUCTION = 0.20

# Known provider overflow error signatures
OVERFLOW_KEYWORDS = (
    "context_length_exceeded",
    "prompt is too long",
    "prompt_too_long",
    "maximum context length",
    "context window exceeded",
    "exceeds the context window",
    "context window is full",
    "too many tokens",
    "too many input tokens",
    "token limit exceeded",
    "max_tokens exceeded",
    "request contains too many tokens",
    "token count exceeds maximum",
    "string exceeds the maximum length",
)


##
 # @brief Check whether an error message indicates context window overflow.
 #
 # @param error_str Error message returned from API provider.
 # @return True if error matches context overflow signature, False otherwise.
 #
def is_context_overflow_error(error_str: str) -> bool:
    if not error_str:
        return False
    # End-if
    lower_err = str(error_str).lower()
    return any(kw in lower_err for kw in OVERFLOW_KEYWORDS)
# End-def


##
 # @brief Classify a compaction summary response.
 #
 # @param text Output text from the summarization request.
 # @param stop_reason Provider stop reason ("end_turn", "max_tokens", etc.).
 # @param err Error message if call failed.
 #
 # @return (status, reason) tuple where status is "ok", "truncated", "empty", or "error".
 #
def classify_compaction_summary(text, stop_reason, err):
    if err is not None:
        return "error", f"API error: {err}"
    # End-if
    body = (text or "").strip()
    if not body:
        return "empty", "empty response"
    # End-if
    if len(body) < MIN_COMPACT_SUMMARY_CHARS:
        return "empty", f"summary text shorter than {MIN_COMPACT_SUMMARY_CHARS} chars"
    # End-if
    if stop_reason == "max_tokens":
        return "truncated", "hit max_tokens output cap"
    # End-if
    return "ok", "completed normally"
# End-def


##
 # @brief Resolve the output cap of the compaction summarization call.
 #
 # @param raw Raw COMPACT_SUMMARY_MAX_TOKENS value from the profile.
 # @param profile_max_tokens Profile output cap (config MAX_TOKENS).
 #
 # @return Clamped int output limit.
 #
def resolve_compact_summary_max_tokens(raw, profile_max_tokens=None, runtime=None):
    emit = (runtime if runtime is not None else Runtime()).emit
    if raw is None:
        value = DEFAULT_COMPACT_SUMMARY_MAX_TOKENS
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            emit(f"[-] Warning: invalid COMPACT_SUMMARY_MAX_TOKENS={raw!r}; "
                  f"using {DEFAULT_COMPACT_SUMMARY_MAX_TOKENS}.")
            value = DEFAULT_COMPACT_SUMMARY_MAX_TOKENS
        # End-try
    # End-if

    if value < MIN_COMPACT_SUMMARY_MAX_TOKENS:
        emit(f"[-] Warning: COMPACT_SUMMARY_MAX_TOKENS={value} is below "
              f"{MIN_COMPACT_SUMMARY_MAX_TOKENS}; clamping.")
        value = MIN_COMPACT_SUMMARY_MAX_TOKENS
    # End-if

    if profile_max_tokens is not None:
        try:
            cap = int(profile_max_tokens)
            if 0 < cap < value:
                emit(f"[*] Compaction summary cap {value} exceeds profile "
                      f"MAX_TOKENS={cap}; clamping to {cap}.")
                value = cap
            # End-if
        except (TypeError, ValueError):
            pass
        # End-try
    # End-if

    return value
# End-def


##
 # @brief Build the history message that replaces compacted conversation history.
 #
 # @param archive_path Path to the full raw backup archive file.
 # @param status Classification status ("ok", "truncated", "empty", "error").
 # @param summary_content XML summary body text.
 # @param reason Classification explanation.
 # @param kept_messages Count of uncompacted recent messages kept.
 #
 # @return Formatted user-role message dictionary.
 #
def build_compaction_message(archive_path, status, summary_content, reason, kept_messages):
    note = f" ({reason})" if reason and reason != "completed normally" else ""
    if status in ("ok", "truncated"):
        return {
            "role": "user",
            "content": (f"[System: Context compacted at {archive_path}{note}]\n\n"
                        f"{summary_content.strip()}")
        }
    # End-if

    return {
        "role": "user",
        "content": (f"[System: Context compaction FAILED at {archive_path} "
                    f"({status}: {reason}). {kept_messages} recent messages preserved; "
                    "older turns offloaded to archive.]")
    }
# End-def


##
 # @brief True if message is a user message containing plain text (not tool_result).
 #
def is_plain_user_msg(msg):
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    # End-if
    content = msg.get("content")
    if isinstance(content, str):
        return True
    # End-if
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") == "text"
            for b in content
        ) and not any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content
        )
    # End-if
    return False
# End-def


##
 # @brief True if message is an assistant message that ends with a tool_use block.
 #
def msg_ends_with_tool_use(msg):
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return False
    # End-if
    content = msg.get("content")
    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            return last.get("type") == "tool_use"
        # End-if
        return getattr(last, "type", None) == "tool_use"
    # End-if
    return False
# End-def


##
 # @brief Calculate character length of a message content for proportional slicing.
 #
def msg_char_length(msg):
    if not isinstance(msg, dict):
        return 0
    # End-if
    content = msg.get("content")
    if isinstance(content, str):
        return len(content)
    # End-if
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, str):
                total += len(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content") or ""
                if isinstance(text, str):
                    total += len(text)
                elif isinstance(text, list):
                    total += sum(len(str(x)) for x in text)
                # End-if
                if block.get("input"):
                    total += len(json.dumps(block.get("input"), ensure_ascii=False))
                # End-if
            else:
                total += len(str(block))
            # End-if
        # End-for
        return total
    # End-if
    return len(str(content or ""))
# End-def


##
 # @brief Extract token statistics from API provider usage object.
 #
 # @param usage Usage object or dict from response.
 # @return dict with keys: total, input, output, cached.
 #
def extract_usage_tokens(usage) -> dict:
    if not usage:
        return {"total": 0, "input": 0, "output": 0, "cached": 0}
    # End-if

    inp = None
    out = None
    cache_read = None
    cache_create = None
    total = None
    cached_openai = 0

    if isinstance(usage, dict):
        inp = usage.get("input_tokens") or usage.get("prompt_tokens")
        out = usage.get("output_tokens") or usage.get("completion_tokens")
        cache_read = usage.get("cache_read_input_tokens")
        cache_create = usage.get("cache_creation_input_tokens")
        total = usage.get("total_tokens")
    else:
        inp = getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", None)
        out = getattr(usage, "output_tokens", None) or getattr(usage, "completion_tokens", None)
        cache_read = getattr(usage, "cache_read_input_tokens", None)
        cache_create = getattr(usage, "cache_creation_input_tokens", None)
        total = getattr(usage, "total_tokens", None)
        details = getattr(usage, "prompt_tokens_details", None)
        if details:
            cached_openai = getattr(details, "cached_tokens", 0) or 0
        # End-if
    # End-if

    input_val = int(inp or 0)
    output_val = int(out or 0)
    cached_val = int(cache_read or cached_openai or 0)
    if cache_create:
        cached_val += int(cache_create)
    # End-if

    if total is not None:
        total_val = int(total)
    else:
        total_val = (
            input_val
            + output_val
            + (int(cache_read) if cache_read else 0)
            + (int(cache_create) if cache_create else 0)
        )
    # End-if

    return {
        "total": total_val,
        "input": input_val,
        "output": output_val,
        "cached": cached_val,
    }
# End-def


##
 # @brief Condition evaluator for triggering context compaction.
 # Evaluates whether compression should run based on:
 # 1. Provider context overflow error (error message matches overflow keywords).
 # 2. Real token usage exceeds the configured max_context_tokens threshold.
 #
class CompressCondition:
    def __init__(self, max_context_tokens: int = 128000):
        self.max_context_tokens = max_context_tokens
    # End-def

    def __call__(self, err: str = None, total_tokens: int = 0) -> bool:
        return self.should_compress(err=err, total_tokens=total_tokens)
    # End-def

    def should_compress(self, err: str = None, total_tokens: int = 0) -> bool:
        # 1. Upstream provider reported context overflow error
        if err and is_context_overflow_error(err):
            return True
        # End-if

        # 2. Measured tokens exceeded configured max_context_tokens
        if self.max_context_tokens and total_tokens > self.max_context_tokens:
            return True
        # End-if

        return False
    # End-def
# End-class


##
 # @brief Context compressor class.
 # Implements reactive error-driven compaction with 80% smooth sliding retention.
 #
class ContextCompressor:
    def __init__(self, safe_client, session_manager, config, runtime=None):
        self.client = safe_client
        self.session = session_manager
        self.config = config
        self.runtime = runtime if runtime is not None else Runtime()

        self.max_context_tokens = int(config.get("MAX_CONTEXT_TOKENS", 128000))
        self.condition = CompressCondition(self.max_context_tokens)
        self.head_size = int(config.get("COMPACT_HEAD_SIZE", DEFAULT_HEAD_SIZE))
        self.recent_size = int(config.get("COMPACT_RECENT_SIZE", DEFAULT_RECENT_SIZE))
        self.target_reduction = float(config.get("COMPACT_TARGET_REDUCTION", DEFAULT_TARGET_REDUCTION))
    # End-def

    def should_compress(self, err: str = None, total_tokens: int = 0) -> bool:
        return self.condition.should_compress(err=err, total_tokens=total_tokens)
    # End-def

    def is_overflow_error(self, error_str: str) -> bool:
        return is_context_overflow_error(error_str)
    # End-def

    ##
     # @brief Generate archive path for full backup before compaction.
     #
    def _archive_path(self, archive_dir, history_len):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return os.path.join(archive_dir, f"history_{history_len}_{ts}.json")
    # End-def

    ##
     # @brief Trim head messages so it never ends on an incomplete tool_use.
     #
    def _trim_head_for_tool_use(self, history, head_size):
        head = history[:head_size]
        while head and msg_ends_with_tool_use(head[-1]):
            head = head[:-1]
        # End-while
        return head
    # End-def

    ##
     # @brief Find safe cutting index inside candidate messages for target character reduction.
     # Guarantees tool_use and tool_result pairs are never separated.
     #
     # @param candidates Slice of messages available for compaction.
     # @param target_chars Number of characters targeted for reduction.
     #
     # @return Int index in candidates [0:cut_idx] to summarize.
     #
    def _find_safe_cut_index(self, candidates, target_chars):
        if not candidates:
            return 0
        # End-if

        accumulated = 0
        cut_idx = 0
        i = 0
        while i < len(candidates):
            msg = candidates[i]
            accumulated += msg_char_length(msg)

            # Check if this message ends with tool_use
            if msg_ends_with_tool_use(msg):
                # Must include following tool_result message if present
                if i + 1 < len(candidates):
                    i += 1
                    accumulated += msg_char_length(candidates[i])
                # End-if
            # End-if

            cut_idx = i + 1
            if accumulated >= target_chars:
                break
            # End-if
            i += 1
        # End-while

        return max(1, min(cut_idx, len(candidates)))
    # End-def

    ##
     # @brief Execute 80% smooth context compaction.
     #
     # @param history Conversation history list.
     # @param target_reduction Ratio of history volume to compress (default: 0.20).
     #
     # @return (new_history, stats_dict) on success, (None, stats_dict) if history cannot be compacted.
     #
    def compress(self, history, target_reduction=None):
        if target_reduction is None:
            target_reduction = self.target_reduction
        # End-if

        # 1. Check if history is long enough to compress
        min_required = self.head_size + self.recent_size + 2
        if len(history) < min_required:
            self.runtime.emit(f"[-] History length ({len(history)}) too short to compact (min {min_required}).")
            return None, {"status": "too_short", "len": len(history)}
        # End-if

        self.runtime.emit(f"[*] Compacting context (~{int(target_reduction * 100)}% reduction target) via LLM...")

        # 2. Backup raw history before any modification
        archive_dir = os.path.join(self.session.current_session_dir, "archives")
        archive_path = self._archive_path(archive_dir, len(history))
        try:
            os.makedirs(archive_dir, exist_ok=True)
            with open(archive_path, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2,
                          default=self.session._default_serializer)
            # End-with
        except OSError as e:
            self.runtime.emit(f"[-] Compaction archive failed: {e}")
            return None, {"status": "archive_failed", "error": str(e)}
        # End-try

        # 3. Separate Head
        head = self._trim_head_for_tool_use(history, self.head_size)
        trimmed_head_size = len(head)

        # 4. Separate Recent (ensure tool call pairs are not broken)
        # Look backwards for a safe start index
        lookback_limit = min(len(history) - trimmed_head_size, self.recent_size * 2)
        start_recent_idx = None
        for i in range(len(history) - 1, len(history) - 1 - lookback_limit, -1):
            if is_plain_user_msg(history[i]):
                if len(history) - i >= self.recent_size:
                    start_recent_idx = i
                    break
                # End-if
            # End-if
        # End-for

        if start_recent_idx is None or start_recent_idx <= trimmed_head_size:
            start_recent_idx = max(trimmed_head_size + 1, len(history) - self.recent_size)
        # End-if

        # Guard against splitting a tool_use / tool_result pair across middle and recent
        while start_recent_idx > trimmed_head_size and msg_ends_with_tool_use(history[start_recent_idx - 1]):
            start_recent_idx -= 1
        # End-while

        recent = history[start_recent_idx:]
        middle = history[trimmed_head_size:start_recent_idx]

        if not middle:
            self.runtime.emit("[-] No middle candidate messages available for compaction.")
            return None, {"status": "no_middle"}
        # End-if

        # 5. Check if middle begins with previous compaction summary
        prev_summary_text = ""
        candidates_start = 0
        first_middle_content = middle[0].get("content", "")
        if isinstance(first_middle_content, str) and first_middle_content.startswith("[System: Context compacted"):
            prev_summary_text = first_middle_content
            candidates_start = 1
        # End-if

        candidates = middle[candidates_start:]
        if not candidates:
            self.runtime.emit("[-] No candidates to compact beyond existing summary.")
            return None, {"status": "no_candidates"}
        # End-if

        # 6. Select ~20% slice from candidates
        total_chars = sum(msg_char_length(m) for m in history)
        target_chars = int(total_chars * target_reduction)
        cut_idx = self._find_safe_cut_index(candidates, target_chars)

        slice_to_compress = candidates[:cut_idx]
        remaining_middle = candidates[cut_idx:]

        # 7. Build incremental summary prompt
        slice_text = json.dumps(slice_to_compress, ensure_ascii=False, indent=2,
                                default=self.session._default_serializer)
        if len(slice_text) > 180000:
            slice_text = (slice_text[:40000]
                          + "\n...[middle omitted from summarization input]...\n"
                          + slice_text[-140000:])
        # End-if

        prompt_parts = ["Please summarize the following conversation history.\n"]
        if prev_summary_text:
            prompt_parts.append(f"Previous Summary:\n{prev_summary_text}\n\n")
        # End-if
        prompt_parts.append(f"New Historical Turns to Merge:\n{slice_text}\n\n")
        prompt_parts.append(
            "Synthesize into an updated concise memory summary.\n"
            "Focus on:\n"
            "1. <goals>: Active goals and tasks.\n"
            "2. <completed>: Accomplished milestones.\n"
            "3. <decisions>: Key technical decisions and reasons.\n"
            "4. <artifacts>: Key file paths, variable names, functions, error codes.\n"
            "5. <pending>: What still needs to be done.\n\n"
            "Output strictly in XML format using the tags above."
        )
        summary_prompt = "".join(prompt_parts)

        summary_max_tokens = resolve_compact_summary_max_tokens(
            self.config.get("COMPACT_SUMMARY_MAX_TOKENS"),
            self.config.get("MAX_TOKENS"),
            runtime=self.runtime,
        )

        summary_payload = {
            "messages": [{"role": "user", "content": summary_prompt}],
            "max_tokens": summary_max_tokens,
            "system": "You are a concise memory summarization AI."
        }

        # 8. Request summary from LLM
        resp, err = self.client.safe_request(summary_payload, log_tag="COMPRESSION SUMMARY")
        summary_response_text = self.client.extract_text(resp.content) if resp else ""
        status, reason = classify_compaction_summary(
            summary_response_text,
            getattr(resp, "stop_reason", None) if resp else None,
            err,
        )
        summary_content = summary_response_text if status in ("ok", "truncated") else ""

        if status in ("empty", "error"):
            self.runtime.emit(f"[-] Compression failed: {reason}. Preserving original history without truncation.")
            self.session.log_api_call("POST LLM CALL - COMPRESSION SUMMARY", {
                "status": status,
                "reason": reason,
                "stop_reason": getattr(resp, "stop_reason", None) if resp else None,
                "summary_chars": 0,
                "max_tokens": summary_max_tokens,
                "compressed_turns": 0,
                "retained_turns": len(history),
                "archive_path": archive_path,
            })
            return None, {
                "status": status,
                "reason": reason,
                "compressed_turns": 0,
                "archive_path": archive_path,
            }
        elif status == "truncated":
            self.runtime.emit(f"[-] Warning: compression summary hit output cap ({summary_max_tokens} tokens).")
        # End-if

        # 9. Assemble new history (only when summarization succeeded or was truncated)
        summary_msg = build_compaction_message(
            archive_path, status, summary_content, reason, len(recent) + len(remaining_middle)
        )

        new_history = head + [summary_msg] + remaining_middle + recent

        # 10. Audit log
        self.session.log_api_call("POST LLM CALL - COMPRESSION SUMMARY", {
            "status": status,
            "reason": reason,
            "stop_reason": getattr(resp, "stop_reason", None) if resp else None,
            "summary_chars": len(summary_content),
            "max_tokens": summary_max_tokens,
            "compressed_turns": len(slice_to_compress),
            "retained_turns": len(new_history),
            "archive_path": archive_path,
        })

        self.runtime.emit(f"[+] Context compacted successfully: compressed {len(slice_to_compress)} turns, "
              f"retained {len(new_history)} turns (~{int((1 - target_reduction) * 100)}% kept).")

        return new_history, {
            "status": status,
            "compressed_turns": len(slice_to_compress),
            "new_history_len": len(new_history),
            "archive_path": archive_path,
        }
    # End-def
# End-class

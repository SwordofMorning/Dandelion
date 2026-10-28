##
 # @file src/core/agent/prune.py
 # @date 2026/10/28
 #
 # @brief SubAgent-assisted context pruning and daily session inheritance.
 # Inspects conversation history in indivisible tool-pair units, archives a
 # full pre-prune backup with SDK block serialization, applies LLM-selected
 # folding or unit removal, and enforces strict tool_use/tool_result pairing.
 #

import copy
import datetime
import json
import os
import re

from src.core.runtime import Runtime

MAX_UNIT_DIGEST_CHARS = 24000

PRUNE_SYSTEM_PROMPT = """You are a Context Pruning SubAgent for Dandelion.
Your job is to inspect numbered conversation history units and decide which older units can be folded or dropped to free context window space while preserving all active knowledge and constraints.

Rules:
1. Each unit is either a standalone message or an indivisible [assistant(tool_use) + user(tool_result)] pair.
2. Choose one action per unit:
   - "keep": keep the unit unchanged (use for core user requirements, active constraints, and critical recent context).
   - "fold": keep the tool call/result pair structure and IDs, but replace large tool outputs with a concise, accurate summary of the actual facts/conclusions found in that unit's output. When choosing "fold", you MUST provide a non-empty, factual "summary" derived from the unit content.
   - "drop": completely remove obsolete conversational turns or superseded tool explorations that are no longer needed.
3. Never drop the very first user unit (unit 0) unless it is purely a greeting; prefer "keep" or "fold" for unit 0.
4. Respond ONLY with a valid JSON object matching this schema:
{
  "decisions": [
    {"unit_id": 0, "action": "keep", "summary": ""},
    {"unit_id": 1, "action": "fold", "summary": "Read config.py and confirmed [Remote] parser fields and key constraints."}
  ]
}
"""


##
 # @brief Convert Pydantic/SDK blocks (like ThinkingBlock or ToolUseBlock) to dicts.
 #
 # @param obj Object to serialize for JSON output.
 #
 # @return JSON-serializable dict or string representation.
 #
def _default_serializer(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    # End-if
    if hasattr(obj, "dict"):
        return obj.dict()
    # End-if
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    # End-if
    return str(obj)
# End-def


##
 # @brief Format a serialized unit string with head and tail preservation if oversized.
 #
 # @param raw Full JSON string of the unit.
 # @param max_chars Maximum characters to include in the SubAgent digest.
 #
 # @return Full string or head+tail bounded excerpt.
 #
def _bounded_excerpt(raw, max_chars=MAX_UNIT_DIGEST_CHARS):
    if len(raw) <= max_chars:
        return raw
    # End-if
    half = max_chars // 2
    return raw[:half] + "\n...[middle omitted]...\n" + raw[-half:]
# End-def


##
 # @brief Read a field from either a dict or an SDK content block object.
 #
 # @param block Dict or SDK object.
 # @param key Attribute or key name.
 # @param default Fallback value when absent.
 #
 # @return Extracted field value.
 #
def _block_field(block, key, default=None):
    if isinstance(block, dict):
        return block.get(key, default)
    # End-if
    return getattr(block, key, default)
# End-def


##
 # @brief Validate that a message list satisfies LLM role and tool-pair invariants.
 #
 # @param messages Candidate history list to verify.
 # @param allow_open_tail True when the final message may be an unclosed assistant tool_use.
 #
 # @return (is_valid, reason_string) tuple.
 #
def validate_history_protocol(messages, allow_open_tail=False):
    if not isinstance(messages, list) or not messages:
        return False, "History must be a non-empty list."
    # End-if
    if messages[0].get("role") != "user":
        return False, "First history message must have role 'user'."
    # End-if

    n = len(messages)
    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role not in ("user", "assistant"):
            return False, f"Invalid role at index {i}: {role!r}"
        # End-if
        content = msg.get("content", "")
        blocks = content if isinstance(content, list) else []

        tool_uses = [
            _block_field(b, "id") for b in blocks
            if _block_field(b, "type") == "tool_use"
        ]
        tool_results = [
            _block_field(b, "tool_use_id") for b in blocks
            if _block_field(b, "type") == "tool_result"
        ]

        if tool_uses:
            if role != "assistant":
                return False, f"tool_use found in non-assistant message at index {i}."
            # End-if
            if any(not tid for tid in tool_uses):
                return False, f"tool_use missing id at index {i}."
            # End-if
            if i == n - 1 and allow_open_tail:
                continue
            # End-if
            if i + 1 >= n or messages[i + 1].get("role") != "user":
                return False, f"assistant tool_use at index {i} has no following user tool_result."
            # End-if
            next_content = messages[i + 1].get("content", "")
            next_blocks = next_content if isinstance(next_content, list) else []
            next_results = [
                _block_field(b, "tool_use_id") for b in next_blocks
                if _block_field(b, "type") == "tool_result"
            ]
            if set(tool_uses) != set(next_results) or len(tool_uses) != len(next_results):
                return False, f"tool_use/tool_result ID mismatch between indices {i} and {i + 1}."
            # End-if
        # End-if

        if tool_results:
            if role != "user" or i == 0 or messages[i - 1].get("role") != "assistant":
                return False, f"Orphan tool_result at index {i}."
            # End-if
            prev_content = messages[i - 1].get("content", "")
            prev_blocks = prev_content if isinstance(prev_content, list) else []
            prev_uses = [
                _block_field(b, "id") for b in prev_blocks
                if _block_field(b, "type") == "tool_use"
            ]
            if set(tool_results) != set(prev_uses):
                return False, f"tool_result at index {i} does not match preceding assistant tool_use."
            # End-if
        # End-if
    # End-for
    return True, "ok"
# End-def


##
 # @brief Group history messages into indivisible atomic units.
 # Every [assistant(tool_use) + user(tool_result)] pair forms a single unit.
 #
 # @param messages Closed history slice (no dangling tail tool_use).
 #
 # @return List of unit dicts with unit_id, indices, kind, chars, and preview.
 #
def group_atomic_units(messages):
    units = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        content = msg.get("content", "")
        blocks = content if isinstance(content, list) else []
        has_tool_use = any(_block_field(b, "type") == "tool_use" for b in blocks)

        if msg.get("role") == "assistant" and has_tool_use and i + 1 < n:
            next_msg = messages[i + 1]
            raw_pair = json.dumps([msg, next_msg], ensure_ascii=False, default=_default_serializer)
            tool_names = [
                str(_block_field(b, "name", "tool"))
                for b in blocks if _block_field(b, "type") == "tool_use"
            ]
            units.append({
                "unit_id": len(units),
                "indices": [i, i + 1],
                "kind": "tool_pair",
                "tools": tool_names,
                "chars": len(raw_pair),
                "preview": _bounded_excerpt(raw_pair),
            })
            i += 2
        else:
            raw_single = json.dumps(msg, ensure_ascii=False, default=_default_serializer)
            units.append({
                "unit_id": len(units),
                "indices": [i],
                "kind": msg.get("role", "user"),
                "tools": [],
                "chars": len(raw_single),
                "preview": _bounded_excerpt(raw_single),
            })
            i += 1
        # End-if
    # End-while
    return units
# End-def


##
 # @brief SubAgent-driven context pruner and daily rollover helper.
 #
class ContextPruner:
    ##
     # @brief Constructor.
     #
     # @param safe_client SafeLLMClient instance for the pruning SubAgent call.
     # @param session_manager SessionManager used for pre-prune backup archiving.
     # @param runtime Optional Runtime callbacks for status messages.
     # @param config Optional loaded model configuration dictionary.
     #
     # @return None.
     #
    def __init__(self, safe_client, session_manager, runtime=None, config=None):
        self.client = safe_client
        self.session = session_manager
        self.runtime = runtime if runtime is not None else Runtime()
        self.config = config if isinstance(config, dict) else {}
    # End-def

    ##
     # @brief Save a full backup of history into <session_dir>/archive/ before pruning.
     #
     # @param history Full original message list.
     # @param tag Short label for the archive filename.
     #
     # @return Path to the saved archive file.
     #
    def archive_history(self, history, tag="prune"):
        session_dir = getattr(self.session, "current_session_dir", None)
        if not session_dir:
            raise RuntimeError("No active session directory for pre-prune backup.")
        # End-if
        archive_dir = os.path.join(session_dir, "archive")
        os.makedirs(archive_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(archive_dir, f"history_pre_{tag}_{stamp}_{len(history)}msgs.json")
        tmp_path = path + ".tmp"
        serializer = getattr(self.session, "_default_serializer", _default_serializer)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2, default=serializer)
        # End-with
        os.replace(tmp_path, path)
        return path
    # End-def

    ##
     # @brief Fold oversized tool_result bodies inside a tool_pair unit in place.
     #
     # @param user_msg User message holding tool_result blocks.
     # @param summary Non-empty factual summary replacement text from the SubAgent.
     # @param min_chars Minimum block content length before folding applies.
     #
     # @return Modified deep copy of user_msg.
     #
    @staticmethod
    def _fold_tool_results(user_msg, summary, min_chars=300):
        cloned = copy.deepcopy(user_msg)
        content = cloned.get("content", [])
        if not isinstance(content, list):
            return cloned
        # End-if
        note = summary.strip()
        if not note:
            return cloned
        # End-if
        new_blocks = []
        for block in content:
            b_type = _block_field(block, "type")
            if b_type == "tool_result":
                tid = _block_field(block, "tool_use_id", "")
                raw_text = str(_block_field(block, "content", ""))
                if len(raw_text) > min_chars:
                    folded_block = {
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": f"[Pruned by Context SubAgent ({len(raw_text)} chars folded): {note}]",
                    }
                    if _block_field(block, "is_error"):
                        folded_block["is_error"] = True
                    # End-if
                    new_blocks.append(folded_block)
                    continue
                # End-if
            # End-if
            new_blocks.append(block)
        # End-for
        cloned["content"] = new_blocks
        return cloned
    # End-def

    ##
     # @brief Coalesce adjacent plain-text messages of the same role after drops.
     #
     # @param messages Pruned message list.
     #
     # @return Normalized message list preserving strict role alternation.
     #
    @staticmethod
    def _coalesce_adjacent_roles(messages):
        merged = []
        for msg in messages:
            msg_copy = copy.deepcopy(msg)
            if (merged
                    and merged[-1].get("role") == msg_copy.get("role")
                    and isinstance(merged[-1].get("content"), str)
                    and isinstance(msg_copy.get("content"), str)):
                merged[-1]["content"] = merged[-1]["content"].rstrip() + "\n\n" + msg_copy["content"].lstrip()
            else:
                merged.append(msg_copy)
            # End-if
        # End-for
        return merged
    # End-def

    ##
     # @brief Ask the pruning SubAgent for keep/fold/drop decisions on candidate units.
     #
     # @param candidate_units List of atomic unit descriptors eligible for pruning.
     # @param focus_hint Optional caller hint describing what can be discarded.
     #
     # @return Mapping of unit_id -> {"action": str, "summary": str}, or None on failure.
     #
    def _query_prune_subagent(self, candidate_units, focus_hint=""):
        digest_lines = []
        for u in candidate_units:
            tools_str = f" tools={u['tools']}" if u["tools"] else ""
            digest_lines.append(
                f"Unit #{u['unit_id']} ({u['kind']}{tools_str}, {u['chars']} chars):\n{u['preview']}"
            )
        # End-for
        user_prompt = (
            f"Caller Pruning Hint: {focus_hint or 'Fold large read/tool outputs and drop obsolete explorations.'}\n\n"
            "Candidate History Units:\n" + "\n\n".join(digest_lines)
        )
        payload = {
            "model": self.client.model_id,
            "max_tokens": int(self.config.get("MAX_TOKENS", 64000)),
            "system": PRUNE_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        resp, err = self.client.safe_request(payload, log_tag="CONTEXT PRUNE SUBAGENT")
        if err or resp is None:
            return None
        # End-if
        raw = self.client.extract_text(resp.content).strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        # End-if
        try:
            parsed = json.loads(match.group(0))
            decisions = {}
            for item in parsed.get("decisions", []):
                if isinstance(item, dict) and isinstance(item.get("unit_id"), int):
                    decisions[item["unit_id"]] = {
                        "action": str(item.get("action", "keep")).lower(),
                        "summary": str(item.get("summary", "")).strip(),
                    }
                # End-if
            # End-for
            return decisions if decisions else None
        except (ValueError, TypeError):
            return None
        # End-try
    # End-def

    ##
     # @brief Prune history safely with pre-backup, SubAgent review and protocol validation.
     #
     # @param history Full agent history list.
     # @param focus_hint Optional hint from the main agent about what to prune.
     # @param keep_recent_units Number of newest closed atomic units to protect.
     # @param in_flight_tail True when the last message is an open assistant tool_use.
     # @param tag Backup archive tag ("prune" or "rollover").
     #
     # @return (new_history, stats_dict) tuple.
     #
    def prune(self, history, focus_hint="", keep_recent_units=4, in_flight_tail=False, tag="prune"):
        if not isinstance(history, list) or len(history) < 4:
            return history, {"status": "skipped", "reason": "History too short to prune."}
        # End-if

        try:
            archive_path = self.archive_history(history, tag=tag)
        except Exception as e:
            return history, {"status": "archive_failed", "error": str(e)}
        # End-try

        tail = [copy.deepcopy(history[-1])] if in_flight_tail else []
        closed = history[:-1] if in_flight_tail else history[:]
        units = group_atomic_units(closed)
        keep_recent_units = max(1, int(keep_recent_units))
        if len(units) <= keep_recent_units + 1:
            return history, {"status": "skipped", "reason": "Not enough older units outside protected tail."}
        # End-if

        candidates = units[:-keep_recent_units]
        protected = units[-keep_recent_units:]
        decisions = self._query_prune_subagent(candidates, focus_hint=focus_hint)
        if not decisions:
            return history, {
                "status": "llm_failed",
                "reason": "Pruning SubAgent did not return valid decisions; original history preserved.",
                "archive": archive_path,
            }
        # End-if

        rebuilt = []
        folded_count = 0
        dropped_count = 0
        for u in candidates:
            uid = u["unit_id"]
            decision = decisions.get(uid)
            if decision is None:
                action = "keep"
                summary = ""
            else:
                action = decision["action"]
                summary = decision["summary"]
            # End-if

            if uid == 0 and action == "drop":
                action = "keep"
            # End-if

            if action == "drop":
                dropped_count += 1
                continue
            elif action == "fold" and u["kind"] == "tool_pair" and summary:
                idx_a, idx_u = u["indices"]
                rebuilt.append(copy.deepcopy(closed[idx_a]))
                rebuilt.append(self._fold_tool_results(closed[idx_u], summary))
                folded_count += 1
            else:
                for idx in u["indices"]:
                    rebuilt.append(copy.deepcopy(closed[idx]))
                # End-for
            # End-if
        # End-for

        for u in protected:
            for idx in u["indices"]:
                rebuilt.append(copy.deepcopy(closed[idx]))
            # End-for
        # End-for

        rebuilt = self._coalesce_adjacent_roles(rebuilt)
        final_history = rebuilt + tail
        valid, reason = validate_history_protocol(final_history, allow_open_tail=in_flight_tail)
        if not valid:
            self.runtime.emit(f"[-] Context prune validation failed ({reason}); keeping original history.")
            return history, {"status": "validation_failed", "reason": reason, "archive": archive_path}
        # End-if

        before_chars = len(json.dumps(history, ensure_ascii=False, default=_default_serializer))
        after_chars = len(json.dumps(final_history, ensure_ascii=False, default=_default_serializer))
        return final_history, {
            "status": "ok",
            "folded_units": folded_count,
            "dropped_units": dropped_count,
            "before_chars": before_chars,
            "after_chars": after_chars,
            "archive": archive_path,
        }
    # End-def
# End-class

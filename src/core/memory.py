##
 # @file src/core/memory.py
 # @date 2026/08/05
 # 
 # @brief Agent's Memory Management.
 #
 # Two-tier memory storage:
 # - Global tier (llm/memory/): durable project/user knowledge that must
 #   survive across sessions (coding style, architectural decisions, ...).
 # - Session tier (.log/sess_<id>/memory/): facts that only apply to the
 #   current session branch and must not leak into other branches.
 #
 # @note Retrieval combines **both** tiers;
 # session memory is ranked first, because it is the current attention anchor.

import os
import json
import re

##
 # @brief Memory Management Class.
 #
class MemoryManager:
    ##
     # ========================================
     # @section I. Constructor.
     # Construct and tier path resolution.
     # ========================================
     #

    ##
     # @brief Constructor.
     #
     # @param memory_dir Global memory path.
     # @param session_memory_dir Session tier memory path.
     # @param session_manager Used to call session's functions.
     # @param safe_client SafeLLMClient instance, reserved for future LLM-assisted memory features (summarize/merge/extract). Currently stored but not consumed.
     # @param logger Save logs.
     #
    def __init__(self, memory_dir="./llm/memory", session_memory_dir=None,
                 session_manager=None, safe_client=None, logger=None):
        self.memory_dir = memory_dir
        # Static override (used by tests / standalone usage). When
        # session_manager is provided it wins, so `checkout` (session switch)
        # works without rebuilding the agent.
        self._static_session_memory_dir = session_memory_dir
        self.session_manager = session_manager
        self.client = safe_client
        self.logger = logger

        if not os.path.exists(self.memory_dir):
            os.makedirs(self.memory_dir)

        session_dir = self.session_memory_dir()
        if session_dir and not os.path.exists(session_dir):
            os.makedirs(session_dir)
    # End-def

    ##
     # @brief Resolve the session tier directory dynamically.
     #
    def session_memory_dir(self):
        if self.session_manager is not None:
            return self.session_manager.get_session_memory_dir()
        return self._static_session_memory_dir
    # End-def

    ##
     # @brief Resolve the tier directory for a WRITE operation.
     #
     # @param scope "global" or "session/local"
     #
     # @return path to memory dir.
     #         self.memory_dir, when "global" or scope is empty
     #         current session's memory path, when "session/local"
     #
     # @note Raises ValueError
     # when a session-scoped write is requested but no session tier is configured.
     # Silently falling back to the global tier
     # would leak session-local facts across branches (data-isolation bug). 
     #
     # @note Reads are unaffected (list_memories/get_index_text resolve tiers via 
     # session_memory_dir() and safely skip a missing session tier).
     #
    def _dir_for_scope(self, scope):
        scope = (scope or "global").lower()
        if scope in ("session", "local"):
            session_dir = self.session_memory_dir()
            if not session_dir:
                raise ValueError(
                    "Error: scope='session' requested but no session memory "
                    "directory is configured (no active session). Use "
                    "scope='global' instead, or attach a session manager."
                )
            return session_dir
        return self.memory_dir
    # End-def

    ##
     # @brief index to memory content.
     #
    def _index_file(self, tier_dir):
        return os.path.join(tier_dir, "MEMORY.md")
    # End-def

    ##
     # ========================================
     # @section II. Reading.
     # ========================================
     #

    ##
     # @brief Parse memory file frontmatter (YAML-like header) from raw file text.
     #
     # ---
     # name: terminal_reply_format
     # description: User preference: use numbered lists / bullets in terminal replies, avoid Markdown tables
     # tags: [preference, format, terminal, cli, reply-style]
     # updated_at: 2026-08-06 14:24:34
     # scope: global
     # ---
     # User prefers terminal/CLI responses formatted with numbered/list structure (e.g. '1. xxx' with '  - xxx' sub-bullets) instead of Markdown tables. Use this format for chat replies, especially when answering questions about code; tables are less preferred in terminal context (also applies to user-facing summaries).
     #
     # @param text Raw content of a memory file (frontmatter + body).
     #
     # @return [meta, body].
     # @retval meta Dict format header, like: 
     #        {
     #          "name": "terminal_reply_format", 
     #          "description": "User preference: ...", 
     #          "tags": "[preference, format, terminal, cli, reply-style]", 
     #          "updated_at": "2026-08-06 14:24:34", 
     #          "scope": "global"
     #        }
     # @retval body Memory's description paragraph, like:
     #              User prefers terminal/CLI responses.....
     #
    def _parse_frontmatter(self, text):
        if not text.startswith("---"): return {}, text
        parts = text.split("---", 2)
        if len(parts) < 3: return {}, text
        meta = {}
        for line in parts[1].strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
            # End-if
        # End-for
        return meta, parts[2].strip()
    # End-def

    ##
     # @brief Listing single tier (global/local) memory file.
     #
     # @param tier_dir Memory Scan path.
     # @param scope_label Global or local/session.
     #
     # @return List of single tier memory.
     #
    def _list_tier(self, tier_dir, scope_label):
        result = []
        if not tier_dir or not os.path.exists(tier_dir):
            return result

        # Iterate scope.
        for fname in sorted(os.listdir(tier_dir)):
            if not fname.endswith(".md") or fname == "MEMORY.md":
                continue

            fpath = os.path.join(tier_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    raw = f.read()
            except (OSError, UnicodeDecodeError) as e:
                # Skip unreadable / invalid-UTF-8 files instead of breaking the
                # whole memory scan (and the system prompt build).
                print(f"[-] Warning: skipping unreadable memory file {fname}: {e}")
                continue
            # End-try

            meta, body = self._parse_frontmatter(raw)
            name = meta.get("name", fname.replace(".md", ""))
            result.append({
                "filename": fname,
                "name": name,
                "description": meta.get("description", ""),
                "body": body,
                "scope": scope_label
            })
        # End-for
        # Return all memory.
        return result
    # End-def

    ##
     # @brief Listing memories.
     #
     # @param scope == 'all' | 'global' | 'session'
     #
     # @note if scope == all, then listing both global and local memory file.
     # @see _list_tier(), list_memories() -> _list_tier()
     #
     # @return List of (all) memory.
     #
    def list_memories(self, scope="all"):
        result = []
        if scope in ("all", "global"):
            result += self._list_tier(self.memory_dir, "global")
        if scope in ("all", "session"):
            result += self._list_tier(self.session_memory_dir(), "session")
        return result
    # End-def

    ##
     # @brief read index memory file (MEMORY.md), looks like:
     #
     # - [name a] description (tags) [updated: time]
     # - [name b] description (tags) [updated: time]
     #
     # @param tier_dir Memory Scan path.
     #
     # @return string of MEMORY.md.
     #
    def _read_index(self, tier_dir):
        if not tier_dir:
            return ""
        index_file = self._index_file(tier_dir)
        if not os.path.exists(index_file):
            return ""
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                return f.read().strip()
        except (OSError, UnicodeDecodeError) as e:
            # A corrupt/unreadable index must not break the system prompt
            # build; get_index_text() naturally skips this tier's section.
            print(f"[-] Warning: skipping unreadable memory index {index_file}: {e}")
            return ""
    # End-def

    ##
     # @brief Combined index of 2 tiers (injected into 1 system prompt).
     #
    def get_index_text(self):
        sections = []
        global_index = self._read_index(self.memory_dir)
        if global_index:
            sections.append(
                "## Global Project Memories (persist across sessions)\n" + global_index
            )
        session_index = self._read_index(self.session_memory_dir())
        if session_index:
            sections.append(
                "## Current Session Memories (session-scoped)\n" + session_index
            )
        return "\n\n".join(sections)
    # End-def

    ##
     #
     # @brief Keyword matching.
     # 1. Extract keywords from recent user messages;
     # 2. perform substring matching within the remembered name+description;
     # 3. prioritizing session-level matching;
     # 4. with a maximum of 5 messages.
     #
    def select_relevant_memories(self, messages, max_items=5):
        files = self.list_memories(scope="all")
        if not files: return []

        # Fallback keyword logic (skipping LLM call for speed/safety in C-style)
        recent_texts = []
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    # content blocks can be dicts (loaded from history.log) or
                    # SDK objects (in-memory); extract text from both.
                    texts = []
                    for b in content:
                        if isinstance(b, dict):
                            if b.get("type") == "text":
                                texts.append(str(b.get("text", "")))
                        else:
                            if getattr(b, "type", None) == "text":
                                texts.append(str(getattr(b, "text", "")))
                        # End-if
                    # End-for
                    content = " ".join(texts)
                if isinstance(content, str):
                    recent_texts.append(content)
                if len(recent_texts) >= 3:
                    break
            # End-if
        # End-for

        recent = " ".join(reversed(recent_texts))[:2000]
        if not recent.strip(): return []

        keywords = [w.lower() for w in recent.split() if len(w) > 3]

        # Session tier first (current attention anchor), then global tier.
        selected = []
        for scope in ("session", "global"):
            if len(selected) >= max_items:
                break
            tier_files = [f for f in files if f.get("scope") == scope]
            for f in tier_files:
                search_pool = (f["name"] + " " + f["description"]).lower()
                # If any keyword matches, add to selected
                if True in [kw in search_pool for kw in keywords]:
                    selected.append(f)
                    if len(selected) >= max_items:
                        break
                    # End-if
                # End-if
            # End-for
        # End-for

        return selected
    # End-def

    ##
     # @brief Formatting selected into <relevant_memories> style.
     #
     # @return Prompt string.
     #
    def load_memories_string(self, messages):
        selected = self.select_relevant_memories(messages)
        if not selected: return ""

        parts = ["<relevant_memories>"]
        for mem in selected:
            scope_tag = ""
            if mem.get("scope") == "session":
                scope_tag = " (session memory)"
            elif mem.get("scope") == "global":
                scope_tag = " (global memory)"
            parts.append(f"--- Memory: {mem['name']}{scope_tag} ---\n{mem['body']}")
        parts.append("</relevant_memories>")
        return "\n\n".join(parts)
    # End-def

    ##
     # ========================================
     # @section III. Writing.
     # ========================================
     #

    @staticmethod
    def _frontmatter_clean(value):
        """Keep frontmatter values single-line and unable to inject keys or
        split the '---' delimiters (protects _parse_frontmatter)."""
        return str(value or "").replace("\r", " ").replace("\n", " ").replace("---", "-")

    @staticmethod
    def _sanitize_filename(name):
        """Deterministic filename sanitization: two distinct original names must
        never map to the same sanitized filename.

        Replaces whitespace/CR/LF, path separators and Windows-reserved chars
        with '_'. The mapping is deterministic so re-writing the SAME memory
        (update semantics) resolves to the same file, while distinct names that
        would collide after sanitization are rejected later by the collision
        check in write_memory()."""
        return (name.replace(" ", "_").replace("/", "_").replace("\\", "_")
                .replace("\r", "_").replace("\n", "_")
                .replace(":", "_").replace("?", "_").replace("*", "_")
                .replace('"', "_").replace("<", "_").replace(">", "_")
                .replace("|", "_"))

    def write_memory(self, name, description, tags, content, scope="global"):
        import datetime
        target_dir = self._dir_for_scope(scope)
        os.makedirs(target_dir, exist_ok=True)

        safe_name = self._sanitize_filename(name)
        # Length guard: overlong names would raise ENAMETOOLONG/OSError at
        # open() time and crash the turn; reject them explicitly instead.
        if not safe_name or len(safe_name) > 100:
            raise ValueError(
                "Error: memory name must be 1-100 characters after "
                f"sanitization, got '{safe_name or ''}' ({len(safe_name)} chars). "
                "Please choose a shorter name."
            )
        # Reserve the tier index filename (case-insensitive): a memory named
        # "MEMORY" must never resolve to MEMORY.md, which would overwrite the
        # per-tier index file.
        if safe_name.lower() == "memory":
            raise ValueError(
                "Error: memory name 'MEMORY' is reserved for the tier index "
                "file (MEMORY.md). Please choose a different name."
            )
        filename = f"{safe_name}.md"
        filepath = os.path.join(target_dir, filename)

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Sanitize values used in the frontmatter so embedded newlines or '---'
        # text cannot create extra keys or break _parse_frontmatter.
        clean_name = self._frontmatter_clean(name)
        clean_desc = self._frontmatter_clean(description)
        clean_tags = self._frontmatter_clean(tags)

        # Collision guard: if the sanitized path already exists but stores a
        # DIFFERENT memory name, reject the write. Overwriting it would make the
        # index keep stale entries (content/index inconsistency) and silently
        # destroy the earlier memory. Re-writing the SAME name is the normal
        # update path and stays allowed.
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    raw = f.read()
                meta, _ = self._parse_frontmatter(raw)
                existing_name = meta.get("name", "").strip()
            except (OSError, UnicodeDecodeError) as e:
                raise ValueError(
                    f"Error: cannot verify existing memory file {filename}: {e}"
                )
            # _parse_frontmatter strips surrounding quotes, mirror that here so
            # names with leading/trailing quotes never false-positive.
            if existing_name and existing_name != clean_name.strip().strip('"').strip("'"):
                raise ValueError(
                    f"Error: memory name '{name}' sanitizes to '{filename}', "
                    f"which is already used by memory '{existing_name}'. Please "
                    "choose a distinct name."
                )

        frontmatter = (
            "---\n"
            f"name: {clean_name}\n"
            f"description: {clean_desc}\n"
            f"tags: [{clean_tags}]\n"
            f"updated_at: {now}\n"
            f"scope: {scope}\n"
            "---\n"
        )

        # Atomic write: crash mid-write must never leave a half-written memory
        # file behind (readers skip corrupt files silently).
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(frontmatter + content)
        os.replace(tmp_path, filepath)

        self._update_index(target_dir, clean_name, clean_desc, clean_tags, now)
        return True

    def _update_index(self, tier_dir, name, description, tags, updated_at):
        index_file = self._index_file(tier_dir)
        index_lines = []
        if os.path.exists(index_file):
            with open(index_file, "r", encoding="utf-8") as f:
                index_lines = f.readlines()

        # Remove old entry if it exists to avoid duplicates
        index_lines = [l for l in index_lines if not l.startswith(f"- [{name}]")]

        # Add new entry
        index_lines.append(f"- [{name}] {description} (tags: {tags}) [updated: {updated_at}]\n")

        # Keep under 200 lines to prevent MEMORY.md from blowing up System Prompt
        if len(index_lines) > 200:
            index_lines = index_lines[-200:]

        # Atomic write (see write_memory).
        tmp_path = index_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(index_lines)
        os.replace(tmp_path, index_file)
    # End-def
# End-class
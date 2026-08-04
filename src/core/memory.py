# src/core/memory.py

# Two-tier memory storage:
# - Global tier  (llm/memory/): durable project/user knowledge that must
#   survive across sessions (coding style, architectural decisions, ...).
# - Session tier (.log/sess_<id>/memory/): facts that only apply to the
#   current session branch and must not leak into other branches.
# Retrieval combines both tiers; session memory is ranked first because it
# is the current attention anchor.

import os
import json
import re

class MemoryManager:
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

    # ------------------------------------------------------------------
    # Tier path resolution
    # ------------------------------------------------------------------
    def session_memory_dir(self):
        """Resolve the session tier directory dynamically."""
        if self.session_manager is not None:
            return self.session_manager.get_session_memory_dir()
        return self._static_session_memory_dir

    def _dir_for_scope(self, scope):
        scope = (scope or "global").lower()
        if scope in ("session", "local"):
            session_dir = self.session_memory_dir()
            if not session_dir:
                print("[-] Warning: no session memory dir configured; "
                      "falling back to the global tier.")
                return self.memory_dir
            return session_dir
        return self.memory_dir

    def _index_file(self, tier_dir):
        return os.path.join(tier_dir, "MEMORY.md")

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def _parse_frontmatter(self, text):
        if not text.startswith("---"): return {}, text
        parts = text.split("---", 2)
        if len(parts) < 3: return {}, text
        meta = {}
        for line in parts[1].strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
        return meta, parts[2].strip()

    def _list_tier(self, tier_dir, scope_label):
        result = []
        if not tier_dir or not os.path.exists(tier_dir):
            return result

        for fname in sorted(os.listdir(tier_dir)):
            if not fname.endswith(".md") or fname == "MEMORY.md":
                continue

            fpath = os.path.join(tier_dir, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                raw = f.read()

            meta, body = self._parse_frontmatter(raw)
            name = meta.get("name", fname.replace(".md", ""))
            result.append({
                "filename": fname,
                "name": name,
                "description": meta.get("description", ""),
                "body": body,
                "scope": scope_label
            })
        return result

    def list_memories(self, scope="all"):
        """List memories: scope='all' | 'global' | 'session'."""
        result = []
        if scope in ("all", "global"):
            result += self._list_tier(self.memory_dir, "global")
        if scope in ("all", "session"):
            result += self._list_tier(self.session_memory_dir(), "session")
        return result

    def _read_index(self, tier_dir):
        if not tier_dir:
            return ""
        index_file = self._index_file(tier_dir)
        if not os.path.exists(index_file):
            return ""
        with open(index_file, "r", encoding="utf-8") as f:
            return f.read().strip()

    def get_index_text(self):
        """Combined index of both tiers (injected into the system prompt)."""
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
                    content = " ".join(texts)
                if isinstance(content, str):
                    recent_texts.append(content)
                if len(recent_texts) >= 3:
                    break

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

        return selected

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

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def write_memory(self, name, description, tags, content, scope="global"):
        import datetime
        target_dir = self._dir_for_scope(scope)
        os.makedirs(target_dir, exist_ok=True)

        safe_name = name.replace(" ", "_").replace("/", "_").replace("\\", "_")
        filename = f"{safe_name}.md"
        filepath = os.path.join(target_dir, filename)

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        frontmatter = (
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"tags: [{tags}]\n"
            f"updated_at: {now}\n"
            f"scope: {scope}\n"
            "---\n"
        )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(frontmatter + content)

        self._update_index(target_dir, name, description, tags, now)
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

        with open(index_file, "w", encoding="utf-8") as f:
            f.writelines(index_lines)

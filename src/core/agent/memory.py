##
 # @file src/core/agent/memory.py
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
 #
 # @note Memory read/write call chain:
 #   read:  agent._get_memories()
 #            -> MemoryManager.load_memories_string(messages)
 #            -> select_relevant_memories()      # keyword match, session first
 #            -> list_memories(scope="all")
 #            -> _list_tier(dir, "global"/"session")
 #            -> _parse_frontmatter(text)        # split meta/body
 #
 #   write: MemoryTool.execute()
 #            -> MemoryManager.write_memory(...)  # scope resolved by _dir_for_scope
 #            -> _sanitize_filename() + _frontmatter_clean()
 #            -> _update_index()                  # append to MEMORY.md (<=200 lines)
 #
 #   index: sysprompt.build()
 #            -> MemoryManager.get_index_text()
 #            -> _read_index(tier_dir)            # per-tier MEMORY.md
 #

import hashlib, json, os, re, sqlite3, time
from src.core.runtime import Runtime
from src.core.utils.embedding import EmbeddingError, create_embedding_provider, validate_and_normalize_vector

MAX_INDEX_LINES = 500
_UNSET = object()

##
 # @brief Load cached (content_hash, vector) entries from <tier_dir>/.vector_cache.sqlite3.
 #
def _load_tier_vectors(tier_dir, profile_key):
    db_path = os.path.join(tier_dir or "", ".vector_cache.sqlite3")
    if not os.path.exists(db_path): return {}
    try:
        with sqlite3.connect(db_path, timeout=2.0) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='profile'").fetchone()
            if not row or row[0] != profile_key: return {}
            return {fn: (h, json.loads(v)) for fn, h, v in conn.execute("SELECT filename, hash, vec FROM vectors")}
        # End-with
    except Exception:
        return {}
    # End-try
# End-def

##
 # @brief Persist (filename, content_hash, vector) entries into <tier_dir>/.vector_cache.sqlite3.
 #
def _save_tier_vectors(tier_dir, profile_key, entries):
    if not tier_dir or not entries: return
    os.makedirs(tier_dir, exist_ok=True)
    db_path = os.path.join(tier_dir, ".vector_cache.sqlite3")
    for attempt in range(2):
        try:
            with sqlite3.connect(db_path, timeout=2.0) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
                conn.execute("CREATE TABLE IF NOT EXISTS vectors (filename TEXT PRIMARY KEY, hash TEXT, vec TEXT)")
                row = conn.execute("SELECT value FROM meta WHERE key='profile'").fetchone()
                if not row or row[0] != profile_key:
                    conn.execute("DELETE FROM vectors")
                    conn.execute("INSERT OR REPLACE INTO meta VALUES ('profile', ?)", (profile_key,))
                # End-if
                conn.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)", [(fn, h, json.dumps(v)) for fn, h, v in entries])
            # End-with
            return
        except sqlite3.DatabaseError:
            if attempt == 0 and os.path.exists(db_path):
                try: os.remove(db_path)
                except OSError: pass
            # End-if
        # End-try
    # End-for
# End-def

##
 # @brief Delete one cached vector entry from <tier_dir>/.vector_cache.sqlite3.
 #
def _delete_tier_vector(tier_dir, filename):
    db_path = os.path.join(tier_dir or "", ".vector_cache.sqlite3")
    if not os.path.exists(db_path): return
    try:
        with sqlite3.connect(db_path, timeout=2.0) as conn:
            conn.execute("DELETE FROM vectors WHERE filename=?", (filename,))
        # End-with
    except Exception:
        pass
    # End-try
# End-def

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
     # @param runtime Optional Runtime callbacks for emitting warnings.
     #
     # @return None.
     #
    def __init__(self, memory_dir="./llm/memory", session_memory_dir=None,
                 session_manager=None, safe_client=None, logger=None, runtime=None,
                 memory_config=None, embedding_provider=None):
        self.memory_dir = memory_dir
        # Static override (used by tests / standalone usage). When
        # session_manager is provided it wins, so `checkout` (session switch)
        # works without rebuilding the agent.
        self._static_session_memory_dir = session_memory_dir
        self.session_manager = session_manager
        self.client = safe_client
        self.logger = logger
        self.runtime = runtime if runtime is not None else Runtime()
        cfg = memory_config if isinstance(memory_config, dict) else {}
        self.passive_top_k = max(1, int(cfg.get("passive_top_k", 5) or 5))
        self.min_similarity = float(cfg.get("min_similarity", 0.35) if cfg.get("min_similarity") is not None else 0.35)
        self.provider = embedding_provider or create_embedding_provider(cfg)
        self._cooldown_until = 0.0

        if not os.path.exists(self.memory_dir):
            os.makedirs(self.memory_dir)

        session_dir = self.session_memory_dir()
        if session_dir and not os.path.exists(session_dir):
            os.makedirs(session_dir)
    # End-def

    ##
     # @brief Resolve the session tier directory dynamically.
     #
     # @return Session tier directory path;
     #         None when no active session and no static override configured.
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
     # @param tier_dir Global or session tier directory.
     #
     # @return Path to the tier's MEMORY.md index file.
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
                self.runtime.emit(f"[-] Warning: skipping unreadable memory file {fname}: {e}")
                continue
            # End-try

            meta, body = self._parse_frontmatter(raw)
            name = meta.get("name", fname.replace(".md", ""))
            result.append({
                "filename": fname,
                "name": name,
                "description": meta.get("description", ""),
                "tags": meta.get("tags", "").strip("[]").strip(),
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
            self.runtime.emit(f"[-] Warning: skipping unreadable memory index {index_file}: {e}")
            return ""
    # End-def

    ##
     # @brief Combined index of 2 tiers (injected into 1 system prompt).
     #
     # @return Combined index text of both tiers (global first, then session);
     #         ""(empty) when neither tier has an index.
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
     # 4. with a maximum of max_items messages.
     #
     # @param messages Message history for keyword extraction.
     # @param max_items Max selected memories (session tier ranked first).
     #
     # @return List of memory dicts (name/description/body/scope/filename).
     #
    ##
     # @brief Build canonical embedding text and SHA-256 content hash for a memory entry.
     #
    def _doc_text_and_hash(self, name, description, tags, body):
        c = [self._frontmatter_clean(x).strip().strip("[]\"'") for x in (name, description, tags)]
        doc = f"{c[0]}\n{c[1]}\n{c[2]}\n{str(body or '').strip()}"
        return doc, hashlib.sha256(doc.encode("utf-8")).hexdigest()
    # End-def

    ##
     # @brief Pre-embed a memory entry before writing to disk.
     #
     # @note Write-time UTF-8/ASCII fallback guard: vector memory is primary and
     # supports UTF-8. When disabled or failing, preembed_entry raises EmbeddingError
     # so MemoryTool rejects non-ASCII input and asks the LLM to re-submit in ASCII
     # for keyword split compatibility. Do NOT remove this guard.
     #
    def preembed_entry(self, name, description, content="", tags=""):
        if not self.provider.is_enabled():
            raise EmbeddingError("Vector memory provider is disabled")
        # End-if
        return self.provider.embed_documents([self._doc_text_and_hash(name, description, tags, content)[0]])[0]
    # End-def

    ##
     # @brief Search memories via primary vector retrieval with automatic keyword fallback.
     #
     # @note Keyword split mode includes a CJK 2-gram fallback alongside ASCII words
     # (len > 3) so previously stored UTF-8/Chinese memories remain retrievable when
     # the embedding service is temporarily unreachable or in cooldown.
     #
    def search(self, query, scope="all", top_k=5):
        q, limit = str(query or "").split("[Dandelion Context")[0].strip(), max(1, int(top_k or self.passive_top_k))
        if not q: return {"backend": "keyword", "reason": "empty_query", "hits": []}
        tiers = ([ (self.session_memory_dir(), "session") ] if scope in ("all", "session") and self.session_memory_dir() else []) + (
                 [ (self.memory_dir, "global") ] if scope in ("all", "global") else [])
        reason = "provider_disabled"
        if self.provider.is_enabled() and time.monotonic() >= self._cooldown_until:
            try:
                pkey, scored, q_vec = self.provider.profile_key(), [], None
                for t_dir, s_label in tiers:
                    items = self._list_tier(t_dir, s_label)
                    if not items: continue
                    cached, m_items, m_docs, m_hashes, vecs = _load_tier_vectors(t_dir, pkey), [], [], [], {}
                    for it in items:
                        doc, h = self._doc_text_and_hash(it["name"], it["description"], it.get("tags", ""), it["body"])
                        ce = cached.get(it["filename"])
                        if ce and ce[0] == h:
                            try:
                                vecs[it["filename"]] = validate_and_normalize_vector(ce[1], self.provider.dimensions)
                                continue
                            except EmbeddingError: pass
                        # End-if
                        m_items.append(it); m_docs.append(doc); m_hashes.append(h)
                    # End-for
                    if m_docs:
                        new_vecs = self.provider.embed_documents(m_docs)
                        for it, h, v in zip(m_items, m_hashes, new_vecs): vecs[it["filename"]] = v
                        _save_tier_vectors(t_dir, pkey, [(it["filename"], h, v) for it, h, v in zip(m_items, m_hashes, new_vecs)])
                    # End-if
                    q_vec = q_vec or self.provider.embed_query(q)
                    for it in items:
                        v = vecs.get(it["filename"])
                        sim = sum(a * b for a, b in zip(q_vec, v)) if v and len(v) == len(q_vec) else -1.0
                        if sim >= self.min_similarity:
                            scored.append((sim, 0 if s_label == "session" else 1, dict(it, score=round(sim, 4))))
                        # End-if
                    # End-for
                # End-for
                scored.sort(key=lambda x: (-round(x[0], 6), x[1], x[2]["name"]))
                return {"backend": "vector", "reason": "ok", "hits": [x[2] for x in scored[:limit]]}
            except EmbeddingError as exc:
                self._cooldown_until, reason = time.monotonic() + 30.0, f"embedding_error: {exc}"
            # End-try
        elif self.provider.is_enabled():
            reason = "cooldown"
        # End-if
        ascii_kws = [w for w in re.findall(r"[a-z0-9_]+", q.lower()) if len(w) > 3]
        cjk_kws = [seg[i:i + 2] for seg in re.findall(r"[\u4e00-\u9fff]{2,}", q) for i in range(len(seg) - 1)]
        keywords = list(dict.fromkeys(ascii_kws + cjk_kws))
        if not keywords: return {"backend": "keyword", "reason": reason, "hits": []}
        selected = []
        for t_dir, s_label in tiers:
            for f in self._list_tier(t_dir, s_label):
                pool = (f["name"] + " " + f["description"] + " " + f.get("tags", "")).lower()
                cnt = sum(1 for kw in keywords if kw in pool)
                if cnt > 0:
                    selected.append(dict(f, score=float(cnt)))
                    if len(selected) >= limit: return {"backend": "keyword", "reason": reason, "hits": selected}
                # End-if
            # End-for
        # End-for
        return {"backend": "keyword", "reason": reason, "hits": selected}
    # End-def

    def select_relevant_memories(self, messages, max_items=None, *, query_override=_UNSET):
        if query_override is None: return []
        if query_override is not _UNSET:
            return self.search(str(query_override), scope="all", top_k=max_items or self.passive_top_k)["hits"]
        # End-if

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
                    clean = content.split("[Dandelion Context")[0].strip()
                    if clean.startswith(("[Interrupt: Scheduled Wakeup", "[Interrupt: CLI Session", "[Interrupt: Background Job")):
                        continue
                    # End-if
                    if clean.startswith("[Interrupt: User Message"):
                        clean = clean.split("\n", 2)[-1].strip()
                    # End-if
                    if clean: recent_texts.append(clean)
                if len(recent_texts) >= 3:
                    break
            # End-if
        # End-for

        recent = " ".join(reversed(recent_texts))[:2000]
        if not recent.strip(): return []
        return self.search(recent, scope="all", top_k=max_items or self.passive_top_k)["hits"]
    # End-def

    ##
     # @brief Formatting selected into <relevant_memories> style.
     #
     # @return Prompt string.
     #
    def load_memories_string(self, messages, *, query_override=_UNSET):
        selected = self.select_relevant_memories(messages, query_override=query_override)
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

    ##
     # @brief Keep frontmatter values single-line and unable to inject keys or
     # split the '---' delimiters (protects _parse_frontmatter).
     #
    @staticmethod
    def _frontmatter_clean(value):
        return str(value or "").replace("\r", " ").replace("\n", " ").replace("---", "-")
    # End-def

    ##
     # @brief Deterministic filename sanitization.
     # two distinct original names must never map to the same sanitized filename.
     #
     # @note Replaces whitespace/CR/LF, path separators and Windows-reserved chars with '_'.
     # The mapping is deterministic so re-writing the SAME memory (update semantics) resolves to the same file,
     # while distinct names that would collide after sanitization are rejected later by the collision check in write_memory().
     #
    @staticmethod
    def _sanitize_filename(name):
        return (name.replace(" ", "_").replace("/", "_").replace("\\", "_")
                .replace("\r", "_").replace("\n", "_")
                .replace(":", "_").replace("?", "_").replace("*", "_")
                .replace('"', "_").replace("<", "_").replace(">", "_")
                .replace("|", "_"))
    # End-def

    ##
     # @brief Write memory.
     #
     # @param name Unique memory name (sanitized to filename).
     # @param description One-sentence summary (used by keyword matching).
     # @param tags Comma-separated tags.
     # @param content Full memory body.
     # @param scope "global" or "session" (session requires active session).
     #
     # @return True on success.
     # @raise ValueError When name invalid/reserved/colliding, 
     # or scope='session' with no session configured.
     #
    def write_memory(self, name, description, tags="general", content=_UNSET, scope="global", *, precomputed_vector=None):
        import datetime
        if content is _UNSET: content, tags = tags, "general"
        scope = "session" if str(scope or "global").lower() in ("session", "local") else "global"
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
        # End-if

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
            # End-try
            # _parse_frontmatter strips surrounding quotes, mirror that here so
            # names with leading/trailing quotes never false-positive.
            if existing_name and existing_name != clean_name.strip().strip('"').strip("'"):
                raise ValueError(
                    f"Error: memory name '{name}' sanitizes to '{filename}', "
                    f"which is already used by memory '{existing_name}'. Please "
                    "choose a distinct name."
                )
            # End-if
        # End-if

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
        doc, h = self._doc_text_and_hash(clean_name, clean_desc, clean_tags, content)
        vec = precomputed_vector
        if vec is None and self.provider.is_enabled() and time.monotonic() >= self._cooldown_until:
            try: vec = self.provider.embed_documents([doc])[0]
            except EmbeddingError: self._cooldown_until = time.monotonic() + 30.0
        # End-if
        if vec is not None:
            _save_tier_vectors(target_dir, self.provider.profile_key(), [(filename, h, vec)])
        # End-if
        return True
    # End-def

    ##
     # @brief Delete a memory entry and remove it from the tier's MEMORY.md index.
     #
     # @param name Memory topic name to delete.
     # @param scope "global" or "session".
     #
     # @return True when the memory existed and was deleted; False when not found.
     #
    def delete_memory(self, name, scope="global"):
        target_dir = self._dir_for_scope(scope)
        if not os.path.exists(target_dir):
            return False
        # End-if

        safe_name = self._sanitize_filename(name)
        if not safe_name or len(safe_name) > 100 or safe_name.lower() == "memory":
            raise ValueError(f"Error: invalid memory name '{name}'.")
        # End-if

        filename = f"{safe_name}.md"
        filepath = os.path.join(target_dir, filename)
        if not os.path.exists(filepath):
            return False
        # End-if

        clean_name = self._frontmatter_clean(name).strip().strip('"').strip("'")
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                raw = f.read()
            # End-with
            meta, _ = self._parse_frontmatter(raw)
            existing_name = meta.get("name", "").strip()
        except (OSError, UnicodeDecodeError):
            existing_name = ""
        # End-try

        if existing_name and existing_name != clean_name and existing_name != safe_name:
            raise ValueError(
                f"Error: memory name '{name}' maps to '{filename}' which belongs to '{existing_name}'."
            )
        # End-if

        os.remove(filepath)
        _delete_tier_vector(target_dir, filename)
        self._remove_from_index(target_dir, existing_name or clean_name, safe_name)
        return True
    # End-def

    ##
     # @brief Remove a deleted memory entry from MEMORY.md atomically.
     #
     # @param tier_dir Tier directory holding MEMORY.md.
     # @param name Canonical memory name from frontmatter.
     # @param safe_name Sanitized filename stem fallback.
     #
     # @return None.
     #
    def _remove_from_index(self, tier_dir, name, safe_name=""):
        index_file = self._index_file(tier_dir)
        if not os.path.exists(index_file):
            return
        # End-if
        with open(index_file, "r", encoding="utf-8") as f:
            index_lines = f.readlines()
        # End-with
        prefixes = {f"- [{name}]"}
        if safe_name:
            prefixes.add(f"- [{safe_name}]")
        # End-if
        filtered = [l for l in index_lines if not any(l.startswith(p) for p in prefixes)]
        tmp_path = index_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(filtered)
        # End-with
        os.replace(tmp_path, index_file)
    # End-def

    ##
     # @brief Update MEMORY.md file.
     #
     # @param tier_dir Tier directory holding MEMORY.md.
     # @param name Memory name (index key).
     # @param description One-line description.
     # @param tags Comma-separated tags.
     # @param updated_at Timestamp string.
     #
    def _update_index(self, tier_dir, name, description, tags, updated_at):
        index_file = self._index_file(tier_dir)
        index_lines = []
        if os.path.exists(index_file):
            with open(index_file, "r", encoding="utf-8") as f:
                index_lines = f.readlines()
        # End-if

        # Remove old entry if it exists to avoid duplicates
        index_lines = [l for l in index_lines if not l.startswith(f"- [{name}]")]

        # Add new entry
        index_lines.append(f"- [{name}] {description} (tags: {tags}) [updated: {updated_at}]\n")

        # Keep under MAX_INDEX_LINES (500) to bound System Prompt memory index size.
        if len(index_lines) > MAX_INDEX_LINES:
            index_lines = index_lines[-MAX_INDEX_LINES:]
        # End-if

        # Atomic write (see write_memory).
        tmp_path = index_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(index_lines)
        os.replace(tmp_path, index_file)
    # End-def
# End-class

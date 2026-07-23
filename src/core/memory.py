# src/core/memory.py
import os
import json
import re

class MemoryManager:
    def __init__(self, memory_dir="./llm/memory", safe_client=None, logger=None):
        self.memory_dir = memory_dir
        self.index_file = os.path.join(memory_dir, "MEMORY.md")
        self.client = safe_client
        self.logger = logger
        
        if not os.path.exists(self.memory_dir):
            os.makedirs(self.memory_dir)

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

    def list_memories(self):
        result = []
        if not os.path.exists(self.memory_dir): return result
        
        for fname in sorted(os.listdir(self.memory_dir)):
            if not fname.endswith(".md") or fname == "MEMORY.md":
                continue
            
            fpath = os.path.join(self.memory_dir, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                raw = f.read()
            
            meta, body = self._parse_frontmatter(raw)
            name = meta.get("name", fname.replace(".md", ""))
            result.append({
                "filename": fname,
                "name": name,
                "description": meta.get("description", ""),
                "body": body
            })
        return result

    def get_index_text(self):
        if not os.path.exists(self.index_file):
            return ""
        with open(self.index_file, "r", encoding="utf-8") as f:
            return f.read().strip()

    def select_relevant_memories(self, messages, max_items=5):
        files = self.list_memories()
        if not files: return []
        
        # Fallback keyword logic (skipping LLM call for speed/safety in C-style)
        recent_texts = []
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(str(getattr(b, "text", "")) for b in content if getattr(b, "type", None) == "text")
                if isinstance(content, str): recent_texts.append(content)
                if len(recent_texts) >= 3: break
                
        recent = " ".join(reversed(recent_texts))[:2000]
        if not recent.strip(): return []
        
        keywords = [w.lower() for w in recent.split() if len(w) > 3]
        selected = []
        for f in files:
            search_pool = (f["name"] + " " + f["description"]).lower()
            # If any keyword matches, add to selected
            if True in [kw in search_pool for kw in keywords]:
                selected.append(f)
                if len(selected) >= max_items: break
                
        return selected

    def load_memories_string(self, messages):
        selected = self.select_relevant_memories(messages)
        if not selected: return ""
        
        parts = ["<relevant_memories>"]
        for mem in selected:
            parts.append(f"--- Memory: {mem['name']} ---\n{mem['body']}")
        parts.append("</relevant_memories>")
        return "\n\n".join(parts)
# src/core/skill.py

import os

class SkillManager:
    def __init__(self, skill_dir):
        self.skill_dir = skill_dir
        self.registry = {}
        
        if not os.path.exists(self.skill_dir):
            os.makedirs(self.skill_dir)
            
        self._scan_skills()

    def _parse_frontmatter(self, text):
        # Strip UTF-8 BOM and leading whitespaces
        text = text.lstrip("\ufeff").lstrip()
        
        if not text.startswith("---"):
            return {}, text
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}, text
            
        meta = {}
        for line in parts[1].strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
        return meta, parts[2].strip()

    def _scan_skills(self):
        for fname in sorted(os.listdir(self.skill_dir)):
            if not fname.endswith(".md"):
                continue
                
            fpath = os.path.join(self.skill_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    raw = f.read()
            except Exception as e:
                print(f"[-] Failed to read skill file {fname}: {e}")
                continue
                
            meta, body = self._parse_frontmatter(raw)
            name = meta.get("name", fname.replace(".md", ""))
            desc = meta.get("description", "No description provided.")
            
            self.registry[name] = {
                "name": name,
                "description": desc,
                "content": raw
            }

    def get_catalog(self):
        if not self.registry:
            return "(No specific skills loaded)"
            
        lines = []
        for s in self.registry.values():
            lines.append(f"- {s['name']}: {s['description']}")
        return "\n".join(lines)

    def get_skill_content(self, name):
        skill = self.registry.get(name)
        if not skill:
            return None
        return skill["content"]
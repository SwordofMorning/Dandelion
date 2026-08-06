##
 # @file src/core/skill.py
 # @date 2026/08/05
 # 
 # @brief Skill Package.
 # Provides agent (LLM request), memory, skill and prompt builder.
 #

import os

##
 # @brief Skill Interface.
 #
class SkillManager:
    ##
     # @brief Constructor.
     #
     # @param skill_dir Path to load skill, default as `llm/skill/`.
     #
    def __init__(self, skill_dir):
        self.skill_dir = skill_dir
        self.registry = {}
        
        if not os.path.exists(self.skill_dir):
            os.makedirs(self.skill_dir)

        self._scan_skills()
    # End-def

    ##
     # @brief Skill header (yaml style) extract.
     #
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
    # End-def

    ##
     # @brief Scan all skill file and register.
     #
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
                "content": body
            }
        # End-for
    # End-def

    ##
     # @brief Return all skills [name, description], used for prompt builder.
     #
    def get_catalog(self):
        if not self.registry:
            return "(No specific skills loaded)"
            
        lines = []
        for s in self.registry.values():
            lines.append(f"- {s['name']}: {s['description']}")
        return "\n".join(lines)
    # End-def

    ##
     # @brief Return all skill content.
     #
    def get_skill_content(self, name):
        skill = self.registry.get(name)
        if not skill:
            return None
        return skill["content"]
    # End-def
#End-class
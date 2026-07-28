# src/utils/routing_policy.py

class RoutingPolicy:
    CONDITION_KEYWORDS = {
        "simple": [
            "read", "list", "summary", "format", "convert",
            "extract", "simple", "read_file", "write_file"
        ],
        "complex": [
            "design", "architecture", "refactor", "implement",
            "develop", "create", "complex", "build", "generate",
            "plan"
        ],
        "reasoning": [
            "reason", "analyze", "prove", "derive", "math",
            "logic", "think", "reasoning", "debug", "troubleshoot"
        ],
        "tool_heavy": [
            "batch", "iterate", "all files", "recursive",
            "large scale", "tool_heavy", "grep", "bash",
            "search", "edit", "refactor"
        ],
        "long_context": [
            "long context", "whole file", "complete",
            "large file", "long_context", "full codebase"
        ],
    }

    def __init__(self, registry, rate_limiter):
        self.registry = registry
        self.rate_limiter = rate_limiter
        self.specs = registry.get_all_subagent_specs()

        from .rate_limiter import RateLimitConfig
        for spec in self.specs:
            self.rate_limiter.register(spec.alias, RateLimitConfig(
                tpm=spec.tpm, rpm=spec.rpm, rpd=spec.rpd
            ))

    def infer_conditions(self, task_description: str, toolset_name: str, depth: int) -> set:
        conditions = set()
        text = (task_description + " " + toolset_name).lower()

        for cond, keywords in self.CONDITION_KEYWORDS.items():
            if any(kw.lower() in text for kw in keywords):
                conditions.add(cond)

        if depth >= 2:
            conditions.add("complex")
        if toolset_name in ["code_analysis", "full"]:
            conditions.add("tool_heavy")
        if toolset_name in ["filesystem", "full"]:
            conditions.add("long_context")

        if not conditions:
            conditions.add("default")
        return conditions

    def select_model(self, task_description: str, toolset_name: str, depth: int, estimated_tokens: int = 2000) -> str:
        if not self.specs:
            return None # No sub_list defined
            
        task_conditions = self.infer_conditions(task_description, toolset_name, depth)

        # 1. Condition Match & Quota Check
        for spec in self.specs:
            if task_conditions & set(spec.conditions) or "default" in spec.conditions:
                if self.rate_limiter.acquire(spec.alias, estimated_tokens):
                    return spec.alias
                print(f"[Router] Model '{spec.alias}' rate limited, trying next...")

        # 2. Fallback: Any available model
        for spec in self.specs:
            if self.rate_limiter.acquire(spec.alias, estimated_tokens):
                print(f"[Router] Fallback to '{spec.alias}' (ignoring conditions)")
                return spec.alias

        raise RuntimeError("All SubAgent models exhausted. No available quota.")

    def get_fallback_chain(self, exclude_alias: str) -> list:
        chain = []
        found = False
        for spec in self.specs:
            if found and spec.alias != exclude_alias:
                chain.append(spec.alias)
            if spec.alias == exclude_alias:
                found = True
        return chain
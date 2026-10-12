##
 # @file src/utils/routing/routing_policy.py
 # @date 2026/08/05
 # 
 # @brief Subagent spawn (call/generate) policy.
 #

##
 # @brief Subagent routing policy.
 #
class RoutingPolicy:
    CONDITION_KEYWORDS = {
        "simple": [
            "read", "list", "summary", "format", "convert",
            "extract", "simple", "basic", "document", "documentation",
            "write a", "create a", "hello world", "boilerplate", 
            "skeleton", "print", "straightforward", "routine"
        ],
        "complex": [
            "design", "architecture", "refactor", "implement",
            "develop", "system", "framework", "integrate", "migrate",
            "optimize", "rewrite", "complex", "advanced"
        ],
        "reasoning": [
            "reason", "analyze", "prove", "derive", "math",
            "logic", "think", "debug", "troubleshoot", "fix", 
            "resolve", "plan", "decompose", "break down", "evaluate",
            "investigate"
        ],
        "tool_heavy": [
            "batch", "iterate", "all files", "recursive",
            "large scale", "grep", "search", "replace all", "find all",
            "tool_heavy"
        ],
        "long_context": [
            "entire codebase", "full context", "large file", "huge", 
            "comprehensive", "read all", "scan project", "long_context"
        ],
    }

    ##
     # @brief Constructor.
     #
    def __init__(self, registry):
        self.registry = registry
        self.specs = registry.get_all_subagent_specs()
    # End-def

    ##
     # @brief Dynamic set conditions.
     #
     # @param task_description Subagent's task description.
     # @param toolset_name Subagent's toolset.
     # @param depth Subagent's "SubSub" depth.
     #
     # @return Routing conditions.
     #
    def infer_conditions(self, task_description: str, toolset_name: str, depth: int) -> set:
        conditions = set()
        text = (task_description + " " + toolset_name).lower()

        # 1. Extract From Prompt
        for cond, keywords in self.CONDITION_KEYWORDS.items():
            if any(kw.lower() in text for kw in keywords):
                conditions.add(cond)

        # 2. Runtime
        if depth >= 2:
            conditions.add("complex")

        if toolset_name == "planning":
            conditions.add("reasoning")
            conditions.add("complex")
        elif toolset_name == "full":
            conditions.add("tool_heavy")
            conditions.add("long_context")
        elif toolset_name == "code_analysis":
            conditions.add("tool_heavy")
            conditions.add("reasoning")

        # 3. Conflict Resolution
        if "simple" in conditions and ("complex" in conditions or "reasoning" in conditions):
            conditions.remove("simple")

        # 4. Fallback to simple
        if not conditions:
            conditions.add("simple")

        return conditions
    # End-def

    ##
     # @brief Choose Subagent (LLM Model) from condition
     #
     # @param task_description Subagent's task description, used to call infer_conditions.
     # @param toolset_name Subagent's toolset, used to call infer_conditions.
     # @param depth Subagent's "SubSub" depth, used to call infer_conditions.
     # @param estimated_tokens Deprecated/unused token estimate.
     #
     # @return Selected model alias (str); None if no sub models defined.
     #
    def select_model(self, task_description: str, toolset_name: str, depth: int, estimated_tokens: int = 2000) -> str:
        if not self.specs:
            return None # No sub_list defined

        task_conditions = self.infer_conditions(task_description, toolset_name, depth)

        # 1. Condition Match
        for spec in self.specs:
            spec_conditions = set(spec.conditions)
            if task_conditions & spec_conditions or "default" in spec_conditions:
                return spec.alias
        # End-for

        # 2. Fallback to first registered sub-agent model
        fallback_alias = self.specs[0].alias
        print(f"[Router] Fallback to '{fallback_alias}' (ignoring conditions)")
        return fallback_alias
    # End-def

    ##
     # @brief Build the ordered fallback chain of sub-agent models.
     #
     # Returns every registered sub-agent alias except `exclude_alias`, in
     # SUB_LIST priority order. Used by route_request as the last-resort
     # candidates when the selected model fails; rate-limit checks are done
     # by the caller per candidate.
     #
     # @param exclude_alias  The alias that failed and must be excluded.
     #
     # @return List of candidate aliases (possibly empty).
     #
    def get_fallback_chain(self, exclude_alias: str) -> list:
        chain = []
        for spec in self.specs:
            if spec.alias != exclude_alias:
                chain.append(spec.alias)
        return chain
    # End-def
# End-class
##
 # @file src/core/utils/routing/routing_policy.py
 # @date 2026/08/05
 #
 # @brief Task-based SubAgent model selection.
 # Infers routing categories from the task and requested toolset, then selects
 # configured SubAgent models according to category priority and alias order.
 #
 # @note Four routing categories (breaking change, no legacy labels):
 #   media   - images / PDF / charts / waveforms (multimodal models)
 #   review  - code review / audit / verification (review-oriented models)
 #   complex - design / refactor / debugging / planning (strong reasoning models)
 #   simple  - routine work and the default (fast, cheap models)
 #
 # @note Selection: the inferred categories are tried in CATEGORY_PRIORITY
 #   order; for each one the first SUB_LIST model declaring it wins. So a
 #   "review" task is not captured by a "complex" model listed earlier.
 #
 # @note Categories are preferences, not capability checks: a model must still
 #   be configured correctly for media (see the "media" block of MODEL_LIST).
 #

import re

# Highest priority first. "simple" is the default and the last resort.
CATEGORY_PRIORITY = ("media", "review", "complex", "simple")

##
 # @brief Subagent routing policy.
 #
class RoutingPolicy:
    # Matched against the task description only, on word boundaries, so
    # "filesystem" does not count as "system" and "checkout" is not "check".
    CONDITION_KEYWORDS = {
        "media": [
            "image", "images", "pdf", "picture", "photo", "diagram", "screenshot",
            "visual", "media", "multimodal", "waveform", "chart", "figure",
        ],
        "review": [
            "review", "audit", "inspect", "critique", "verify", "verification",
            "check", "assess", "assessment", "evaluate", "evaluation",
            "post-review", "cross-check", "diff check", "lint",
        ],
        "complex": [
            "design", "architecture", "refactor", "implement", "develop",
            "system", "optimize", "rewrite", "complex", "advanced",
            "reason", "analyze", "analyse", "prove", "math", "logic", "think",
            "debug", "troubleshoot", "fix", "solve", "plan", "decompose",
        ],
        "simple": [
            "simple", "basic", "format", "extract", "summary", "summarize",
            "read", "list", "convert", "routine", "boilerplate", "skeleton", "print",
        ],
    }

    # A media file named in the task is a strong media signal on its own.
    MEDIA_FILE_RE = re.compile(r"\.(png|jpe?g|webp|gif|bmp|pdf)\b", re.IGNORECASE)

    # Toolsets that imply a category regardless of the wording.
    TOOLSET_CATEGORIES = {
        "planning": "complex",
        "code_analysis": "complex",
    }

    ##
     # @brief Constructor.
     #
    def __init__(self, registry):
        self.registry = registry
        self.specs = registry.get_all_subagent_specs()
        self._patterns = {
            cond: re.compile(
                r"(?<![a-z0-9_])(?:" + "|".join(re.escape(k) for k in kws) + r")(?![a-z0-9_])"
            )
            for cond, kws in self.CONDITION_KEYWORDS.items()
        }
    # End-def

    ##
     # @brief Infer routing categories for a task.
     #
     # @param task_description Subagent's task description.
     # @param toolset_name Subagent's toolset (matched by explicit rules only).
     # @param depth Subagent depth (root = 0, direct SubAgent = 1).
     #
     # @return Non-empty set drawn from CATEGORY_PRIORITY.
     #
    def infer_conditions(self, task_description: str, toolset_name: str, depth: int) -> set:
        text = (task_description or "").lower()
        conditions = {cond for cond, pat in self._patterns.items() if pat.search(text)}

        if self.MEDIA_FILE_RE.search(text):
            conditions.add("media")
        # End-if

        implied = self.TOOLSET_CATEGORIES.get(toolset_name)
        if implied:
            conditions.add(implied)
        # End-if

        # A delegation below a direct SubAgent is treated as hard work.
        if depth >= 2:
            conditions.add("complex")
        # End-if

        if conditions & {"media", "review", "complex"}:
            conditions.discard("simple")
        # End-if
        if not conditions:
            conditions.add("simple")
        # End-if
        return conditions
    # End-def

    ##
     # @brief Primary category of a condition set (highest priority).
     #
    @staticmethod
    def primary_category(conditions: set) -> str:
        for cat in CATEGORY_PRIORITY:
            if cat in conditions:
                return cat
            # End-if
        # End-for
        return "simple"
    # End-def

    ##
     # @brief Choose a SubAgent model for a task.
     #
     # @param task_description Subagent's task description.
     # @param toolset_name Subagent's toolset.
     # @param depth Subagent depth.
     #
     # @return Selected model alias (str); None if no sub models defined.
     #
    def select_model(self, task_description: str, toolset_name: str, depth: int) -> str:
        if not self.specs:
            return None
        # End-if

        task_conditions = self.infer_conditions(task_description, toolset_name, depth)

        # 1. Inferred categories in priority order, then "simple" as a fallback tier.
        ordered = [c for c in CATEGORY_PRIORITY if c in task_conditions]
        if "simple" not in ordered:
            ordered.append("simple")
        # End-if
        for cat in ordered:
            for spec in self.specs:
                if cat in spec.conditions:
                    return spec.alias
                # End-if
            # End-for
        # End-for

        # 2. Nothing declares a matching category: first registered sub-agent model.
        fallback_alias = self.specs[0].alias
        print(f"[Router] No model declares {sorted(task_conditions)}; falling back to '{fallback_alias}'")
        return fallback_alias
    # End-def

    ##
     # @brief Build the ordered fallback chain of sub-agent models.
     #
     # Returns every registered sub-agent alias except `exclude_alias`, in
     # SUB_LIST priority order. Used by route_request as the last-resort
     # candidates when the selected model fails.
     #
     # @param exclude_alias  The alias that failed and must be excluded.
     #
     # @return List of candidate aliases (possibly empty).
     #
    def get_fallback_chain(self, exclude_alias: str) -> list:
        return [spec.alias for spec in self.specs if spec.alias != exclude_alias]
    # End-def
# End-class

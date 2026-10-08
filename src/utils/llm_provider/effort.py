##
 # @file src/utils/llm_provider/effort.py
 # @date 2026/10/05
 #
 # @brief Single source of truth for the abstract reasoning-effort level and its
 #        translation into every provider family's own knob.
 #
 # @note Dandelion carries ONE abstract level per model ("low"/"medium"/"high"/
 # "max", validated by config.py and routing/model_registry.py). Each family then
 # exposes a different parameter, so the translation lives here instead of inside
 # the request paths:
 #   native Anthropic (Claude)          -> thinking.budget_tokens (+ output_config.effort)
 #   DeepSeek Anthropic-compatible      -> output_config.effort
 #   OpenAI bridge (Sub2API -> GPT)     -> reasoning_effort (+ output_config.effort)
 #   Gemini bridge (Antigravity -> G3)  -> thinking_level / thinkingLevel
 #

##
 # ========================================
 # @section I. Abstract level (config vocabulary)
 # ========================================
 #

# Abstract levels accepted by the configuration layer.
VALID_EFFORTS = ("low", "medium", "high", "max")

# Default level: applied when metadata omits "effort" or provides an invalid one.
DEFAULT_EFFORT = "medium"

##
 # ========================================
 # @section II. Endpoint flavor (link terminal family)
 # ========================================
 #

# @note A flavor describes the model family at the END of the link (the real
# upstream), not the relay product name: a Sub2API host that finally calls GPT is
# "openai", the same host calling Gemini is "gemini".
FLAVOR_ANTHROPIC    = "anthropic"
FLAVOR_DEEPSEEK     = "deepseek"
FLAVOR_OPENAI       = "openai"
FLAVOR_GEMINI       = "gemini"

# @note Opt-in flavor for native Claude models that support output_config.effort
# (Opus 4.5+ needs the effort-2025-11-24 beta header, 4.6+ is stable). It is NOT
# selected by auto-detection: a Claude model keeps the plain "anthropic" behavior
# unless the caller asks for this flavor explicitly.
FLAVOR_ANTHROPIC_EFFORT = "anthropic-effort"

# "auto" requests detection and is never a resolved flavor.
FLAVOR_AUTO = "auto"

# Flavors a caller may name explicitly.
VALID_FLAVORS = (
    FLAVOR_ANTHROPIC,
    FLAVOR_ANTHROPIC_EFFORT,
    FLAVOR_DEEPSEEK,
    FLAVOR_OPENAI,
    FLAVOR_GEMINI,
)

##
 # ========================================
 # @section III. Level -> family parameter tables
 # ========================================
 #

# --- native Anthropic: thinking.budget_tokens ---
# Token budget for extended thinking; must stay below max_tokens.
EFFORT_TO_BUDGET_TOKENS = {
    "low": 8000,
    "medium": 16000,
    "high": 32000,
    "max": 64000,
}

# --- native Anthropic: output_config.effort ---
# Official domain (model dependent): low|medium|high|xhigh|max.
EFFORT_TO_ANTHROPIC_EFFORT = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
}

# --- DeepSeek Anthropic-compatible endpoint: output_config.effort ---
# @note Kept as its own table on purpose: DeepSeek ignores budget_tokens and can
# gain or drop levels independently of Anthropic. The mapping is identity today,
# so a future divergence is a one-line edit here and nowhere else.
EFFORT_TO_DEEPSEEK_EFFORT = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
}

# --- OpenAI-compatible endpoints (Sub2API -> GPT / Codex): reasoning_effort ---
# @note "max" is honored by the newest GPT family only; an older model answers 400
# ("Unsupported value"). To clamp one model, lower its "effort" in MODEL_LIST
# instead of editing this table, so the global mapping stays predictable.
EFFORT_TO_OPENAI_EFFORT = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
}

# --- Gemini 3 (thinkingConfig.thinkingLevel): minimal|low|medium|high ---
# @note Gemini has no "max" level, so max degrades to high. Gemini also defaults to
# "high" when the level is omitted, which makes an explicit level a cost decision
# rather than a quality one.
EFFORT_TO_GEMINI_LEVEL = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "high",
}

##
 # ========================================
 # @section IV. Helpers
 # ========================================
 #

##
 # @brief Translate an abstract level through one family table.
 #
 # @param table Family mapping table (dict level -> family value).
 # @param effort Abstract level ("low"/"medium"/"high"/"max").
 # @param model_id Model ID, used by the warning message.
 #
 # @return Mapped value; the DEFAULT_EFFORT mapping when the level is unknown.
 #
def resolve_effort(table, effort, model_id=""):
    if effort in table:
        return table[effort]
    # End-if

    print(f"[!] Model '{model_id}': effort='{effort}' is not in the mapping "
          f"table. Falling back to '{DEFAULT_EFFORT}'.")
    return table[DEFAULT_EFFORT]
# End-def

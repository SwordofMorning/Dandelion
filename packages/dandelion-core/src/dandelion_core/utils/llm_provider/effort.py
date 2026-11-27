##
 # @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/effort.py
 # @date 2026/11/28
 #
 # @brief Reasoning-effort vocabulary and multi-protocol provider translation.
 # Defines the shared abstract effort levels ("low"/"medium"/"high"/"xhigh"/"max")
 # and translates each upstream model family into its native reasoning controls
 # instead of duplicating mappings in request paths.
 #
 # @note Family parameter mapping overview:
 #   native Anthropic (Claude)          -> thinking.budget_tokens (+ output_config.effort)
 #   DeepSeek (Messages / Chat)         -> output_config.effort / reasoning_effort
 #   OpenAI (Chat / Responses / Bridge) -> reasoning_effort / reasoning.effort
 #   Gemini 3 / 4 (Generate / Inter.)   -> thinkingLevel / thinking_level (xhigh on Gemini 4)
 #   Gemini 2.5                         -> thinkingBudget / thinking_budget
 #

##
 # ========================================
 # @section I. Abstract level (config vocabulary)
 # ========================================
 #

# Abstract levels accepted by the configuration layer.
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")

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
FLAVOR_ANTHROPIC        = "anthropic"
FLAVOR_DEEPSEEK         = "deepseek"
FLAVOR_OPENAI           = "openai"
FLAVOR_GEMINI           = "gemini"

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
    "xhigh": 48000,
    "max": 64000,
}

# --- native Anthropic: output_config.effort ---
# Official domain (model dependent): low|medium|high|xhigh|max.
EFFORT_TO_ANTHROPIC_EFFORT = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

# --- DeepSeek endpoints: output_config.effort / reasoning_effort ---
# @note Live DeepSeek V4 Flash/Pro models advertise supported_levels=["low","high","max"]
# (default_level="high"). Sending literal "xhigh" silently falls back to "high",
# whereas "max" genuinely engages DeepSeek's top reasoning tier with HTTP 200.
EFFORT_TO_DEEPSEEK_EFFORT = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}

# --- OpenAI Chat Completions & bridges: reasoning_effort ---
# @note "xhigh"/"max" are honored by newer GPT/Codex families; older models cap at
# "high". Lower a model's "effort" in MODEL_LIST rather than clamping globally.
EFFORT_TO_OPENAI_EFFORT = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

# --- OpenAI Responses API: reasoning.effort ---
# Official domain: none|minimal|low|medium|high|xhigh|max.
EFFORT_TO_RESPONSES_EFFORT = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

# --- Gemini 3 (thinkingConfig.thinkingLevel / thinking_level): minimal|low|medium|high ---
# @note Gemini 3 caps at "high", so "xhigh" and "max" degrade to "high".
EFFORT_TO_GEMINI_LEVEL = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}

# --- Gemini 4 / Neon / Hydrogen (thinkingLevel / thinking_level): low|medium|high|xhigh ---
# @note Gemini 4 family introduces the "xhigh" thinking tier; "max" maps to "xhigh".
EFFORT_TO_GEMINI_4_LEVEL = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
}

# --- Gemini 2.5 (thinkingConfig.thinkingBudget) ---
# Token budget fitting both Pro and Flash families (including Flash-Lite).
EFFORT_TO_GEMINI_25_BUDGET = {
    "low": 1024,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 24576,
}

# Substrings identifying Gemini 4 generation models that support "xhigh".
_GEMINI_4_MARKERS = ("gemini-4", "neon", "hydrogen")


##
 # ========================================
 # @section IV. Helpers
 # ========================================
 #

##
 # @brief Translate an abstract level through one family table.
 #
 # @param table Family mapping table (dict level -> family value).
 # @param effort Abstract level ("low"/"medium"/"high"/"xhigh"/"max").
 # @param model_id Model ID, used by the warning message.
 #
 # @return Mapped value; the DEFAULT_EFFORT mapping when the level is unknown.
 #
def resolve_effort(table, effort, model_id=""):
    if effort in table:
        return table[effort]
    # End-if

    print(
        f"[!] Model '{model_id}': effort='{effort}' is not in the mapping "
        f"table. Falling back to '{DEFAULT_EFFORT}'."
    )
    return table[DEFAULT_EFFORT]
# End-def


##
 # @brief Resolve a Gemini thinking level string, honoring Gemini 4 "xhigh".
 #
 # @param effort Abstract level ("low"/"medium"/"high"/"xhigh"/"max").
 # @param model_id Model identifier used to distinguish Gemini 4 from Gemini 3.
 #
 # @return Lowercase Gemini thinking level ("low"/"medium"/"high"/"xhigh").
 #
def resolve_gemini_level(effort, model_id=""):
    lowered = str(model_id or "").lower()
    use_g4 = False
    for marker in _GEMINI_4_MARKERS:
        if marker in lowered:
            use_g4 = True
            break
        # End-if
    # End-for
    table = EFFORT_TO_GEMINI_4_LEVEL if use_g4 else EFFORT_TO_GEMINI_LEVEL
    return resolve_effort(table, effort, model_id)
# End-def

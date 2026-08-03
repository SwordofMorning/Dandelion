# src/utils/config/config.py

import os
import configparser
import json

# ---------------------------------------------------------------------------
# Validation helpers (new)
# ---------------------------------------------------------------------------

_VALID_THINKING = {"enabled", "disabled"}
_VALID_EFFORT   = {"low", "medium", "high", "max"}

def _parse_thinking(model_data: dict, model_id: str = "") -> str:
    """Extract and validate the *thinking* field.

    Returns ``"disabled"`` on missing or invalid values (safe default).
    """
    raw = model_data.get("thinking", "disabled")
    if not isinstance(raw, str):
        print(f"[!] Model '{model_id}': 'thinking' must be a string, "
              f"got {type(raw).__name__}. Defaulting to 'disabled'.")
        return "disabled"
    value = raw.strip().lower()
    if value not in _VALID_THINKING:
        print(f"[!] Model '{model_id}': invalid thinking='{value}'. "
              f"Expected one of {sorted(_VALID_THINKING)}. "
              f"Defaulting to 'disabled'.")
        return "disabled"
    return value


def _parse_effort(model_data: dict, model_id: str = "") -> str:
    """Extract and validate the *effort* field.

    Returns ``"medium"`` on missing or invalid values (safe default).
    """
    raw = model_data.get("effort", "medium")
    if not isinstance(raw, str):
        print(f"[!] Model '{model_id}': 'effort' must be a string, "
              f"got {type(raw).__name__}. Defaulting to 'medium'.")
        return "medium"
    value = raw.strip().lower()
    if value not in _VALID_EFFORT:
        print(f"[!] Model '{model_id}': invalid effort='{value}'. "
              f"Expected one of {sorted(_VALID_EFFORT)}. "
              f"Defaulting to 'medium'.")
        return "medium"
    return value


# ---------------------------------------------------------------------------
# Main configuration loader (modified)
# ---------------------------------------------------------------------------

def load_api_config(file_path):
    if not os.path.exists(file_path):
        return None

    config = configparser.ConfigParser()
    config.read(file_path, encoding="utf-8")

    if not config.has_section("Main"):
        return None

    main_agent_id = config.get("Main", "MAIN_AGENT", fallback="")
    if not main_agent_id:
        return None

    # Parse SUB_LIST
    raw_sub_list = config.get("Main", "SUB_LIST", fallback="[]")
    try:
        sub_list = json.loads(raw_sub_list)
        # Verify it is a list and all elements are strings
        if not isinstance(sub_list, list) or not all(isinstance(item, str) for item in sub_list):
            sub_list = []
    except json.JSONDecodeError:
        sub_list = []

    # Read Search API Key
    tavily_api_key = config.get("Main", "TAVILY_API_KEY", fallback="")

    all_models = []
    active_profile = None

    # Iterate through all sections to parse providers and find the main agent
    for section in config.sections():
        if section == "Main":
            continue

        sdk_type = config.get(section, "SDK_TYPE", fallback="Anthropic").strip('"\'')
        base_url = config.get(section, "BASE_URL", fallback="")
        api_key = config.get(section, "API_KEY", fallback="")

        raw_models = config.get(section, "MODEL_LIST", fallback="[]")
        try:
            model_list = json.loads(raw_models)
        except json.JSONDecodeError:
            model_list = []

        for model_data in model_list:
            if not isinstance(model_data, dict):
                continue

            model_id = model_data.get("model_id", "")
            if not model_id:
                continue

            # ---- New: parse thinking & effort with validation ----
            thinking = _parse_thinking(model_data, model_id)
            effort   = _parse_effort(model_data, model_id)

            # Enrich model data with provider info
            enriched_model = {
                "provider_name": section,
                "sdk_type": sdk_type,
                "base_url": base_url,
                "api_key": api_key,
                **model_data,
                # Ensure canonical values override any raw values from **model_data
                "thinking": thinking,
                "effort": effort,
            }
            all_models.append(enriched_model)

            # Check if this is the target main agent
            if model_id == main_agent_id:
                active_profile = enriched_model

    if not active_profile:
        print(f"[-] FATAL: Main Agent '{main_agent_id}' not found in any MODEL_LIST.")
        return None

    # Return a flat dict compatible with existing agent.py expectations,
    # plus the ALL_MODELS registry for future dynamic routing.
    return {
        "ACTIVE_PROFILE": active_profile["provider_name"],
        "SDK_TYPE": active_profile["sdk_type"],
        # Legacy key name support
        "ANTHROPIC_BASE_URL": active_profile["base_url"],
        # Legacy key name support
        "ANTHROPIC_API_KEY": active_profile["api_key"],
        "MODEL_ID": active_profile["model_id"],
        "MAX_TOKENS": active_profile.get("max_token", 8192),
        "SUB_LIST": sub_list,
        "ALL_MODELS": all_models,
        # Search api key
        "TAVILY_API_KEY": tavily_api_key,
        # Think Level
        "THINKING": active_profile.get("thinking", "disabled"),
        "EFFORT": active_profile.get("effort", "medium"),
    }

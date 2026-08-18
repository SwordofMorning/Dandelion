##
 # @file src/utils/config/config.py
 # @date 2026/08/05
 # 
 # @brief Load global config.
 #
 # @note Model metadata (json) like:
 # {
 #     "model_id": "deepseek-v4-pro",
 #     "conditions": ["reasoning", "long_context", "complex"],
 #     "max_token": 819200,
 #     "max_context_tokens": 819200,
 #     "TPM": 0,
 #     "RPM": 10,
 #     "RPD": 500,
 #     "thinking": "enabled",
 #     "effort": "max"
 # }
 # max_token:          Provider output limit (payload "max_tokens").
 #                     Reserved inside the compaction threshold so that
 #                     history + output + overhead never overflow the
 #                     provider context window.
 # max_context_tokens: Context window size; compaction threshold
 #                     (read by agent.py _soft_token_limit via MAX_CONTEXT_TOKENS).
 #

import os
import configparser
import json

##
 # ========================================
 # @section I. Thinking Level
 # ========================================
 #

_VALID_THINKING = {"enabled", "disabled"}
_VALID_EFFORT   = {"low", "medium", "high", "max"}

##
 # @brief Extract and validate the *thinking* field.
 # 
 # @param model_data Model metadata (json).
 # @param model_id Model ID.
 #
 # @return "enabled" or "disabled".
 # @retval enabled when "enabled" is specified in metadata.
 # @retval disabled "disabled" on missing or invalid values (safe default).
 #
def _parse_thinking(model_data: dict, model_id: str = "") -> str:
    # Get model's thinking level.
    raw = model_data.get("thinking", "disabled")

    # 1. No "thinking" key.
    if not isinstance(raw, str):
        print(f"[!] Model '{model_id}': 'thinking' must be a string, "
              f"got {type(raw).__name__}. Defaulting to 'disabled'.")
        return "disabled"
    # End-if

    # Get value.
    value = raw.strip().lower()
    # 2. Invalid value.
    if value not in _VALID_THINKING:
        print(f"[!] Model '{model_id}': invalid thinking='{value}'. "
              f"Expected one of {sorted(_VALID_THINKING)}. "
              f"Defaulting to 'disabled'.")
        return "disabled"
    # End-if

    # 3. Return value of "thinking" in metadata.
    return value
# End-def

##
 # @brief Extract and validate the *effort* field.
 # 
 # @param model_data Model metadata (json).
 # @param model_id Model ID.
 #
 # @return "effort" field value, or "medium" on missing/invalid values (safe default).
 #
def _parse_effort(model_data: dict, model_id: str = "") -> str:
    # Get model's thinking level (effort).
    raw = model_data.get("effort", "medium")

    # 1. No "effort" key.
    if not isinstance(raw, str):
        print(f"[!] Model '{model_id}': 'effort' must be a string, "
              f"got {type(raw).__name__}. Defaulting to 'medium'.")
        return "medium"

    # Get value.
    value = raw.strip().lower()
    # 2. Invalid value.
    if value not in _VALID_EFFORT:
        print(f"[!] Model '{model_id}': invalid effort='{value}'. "
              f"Expected one of {sorted(_VALID_EFFORT)}. "
              f"Defaulting to 'medium'.")
        return "medium"

    # 3. Return value of "effort" in metadata.
    return value
# End-def

##
 # ========================================
 # @section II. Main Config Loader
 # ========================================
 #

##
 # @brief Load config.
 # 
 # @param file_path file path to config.
 #
 # @return Return a flat dict compatible with existing agent.py expectations,
 # with the ALL_MODELS registry for future dynamic routing.
 #
 # @note part.1 load "Main" section; 
 # @note part.2 load others sections i.e. LLM providers.
 #
def load_api_config(file_path):
    # ----- @par 1. "Main" Section Handle -----

    # No such file.
    if not os.path.exists(file_path):
        return None
    # End-if

    config = configparser.ConfigParser()
    config.read(file_path, encoding="utf-8")

    # No "Main" section.
    if not config.has_section("Main"):
        return None
    # End-if

    # In "Main" section, not specify MAIN_AGENT.
    main_agent_id = config.get("Main", "MAIN_AGENT", fallback="")
    if not main_agent_id:
        return None
    # End-if

    # Parse SUB_LIST
    raw_sub_list = config.get("Main", "SUB_LIST", fallback="[]")
    try:
        sub_list = json.loads(raw_sub_list)
        # Verify it is a list and all elements are strings
        if not isinstance(sub_list, list) or not all(isinstance(item, str) for item in sub_list):
            sub_list = []
    except json.JSONDecodeError:
        sub_list = []
    # End-try

    # Read Search API Key
    tavily_api_key = config.get("Main", "TAVILY_API_KEY", fallback="")

    # ----- @par 2. LLM Providers Section -----

    all_models = []
    active_profile = None

    # Iterate through all sections to parse providers and find the main agent.
    for section in config.sections():
        if section == "Main":
            continue

        # Get base info in section [xx].
        sdk_type = config.get(section, "SDK_TYPE", fallback="Anthropic").strip('"\'')
        base_url = config.get(section, "BASE_URL", fallback="")
        api_key = config.get(section, "API_KEY", fallback="")

        # Get all models in section [xx].
        raw_models = config.get(section, "MODEL_LIST", fallback="[]")
        try:
            model_list = json.loads(raw_models)
        except json.JSONDecodeError:
            model_list = []
        # End-try

        # Iterate through all models in model list of section [xx].
        for model_data in model_list:
            if not isinstance(model_data, dict):
                continue

            model_id = model_data.get("model_id", "")
            if not model_id:
                continue

            # Parse thinking level.
            thinking = _parse_thinking(model_data, model_id)
            effort   = _parse_effort(model_data, model_id)

            # Enrich model data with provider info.
            enriched_model = {
                "provider_name": section,
                "sdk_type": sdk_type,
                "base_url": base_url,
                "api_key": api_key,
                **model_data,
                # Ensure canonical values override any raw values from **model_data.
                "thinking": thinking,
                "effort": effort,
            }
            all_models.append(enriched_model)

            # Check if this is the target main agent.
            if model_id == main_agent_id:
                active_profile = enriched_model
            # End-if
        # End-for model in model_list.
    # End-for sections.

    # Main Agent must configure in any MODEL_LIST with any provider (section).
    if not active_profile:
        print(f"[-] FATAL: Main Agent '{main_agent_id}' not found in any MODEL_LIST.")
        return None
    # End-if

    # Return a flat dict compatible with existing agent.py expectations,
    # with the ALL_MODELS registry for future dynamic routing.
    return {
        "ACTIVE_PROFILE": active_profile["provider_name"],
        "SDK_TYPE": active_profile["sdk_type"],
        # Legacy key name support
        "ANTHROPIC_BASE_URL": active_profile["base_url"],
        # Legacy key name support
        "ANTHROPIC_API_KEY": active_profile["api_key"],
        "MODEL_ID": active_profile["model_id"],
        "MAX_TOKENS": active_profile.get("max_token", 8192),
        # Context window / compaction threshold (per-model)
        "MAX_CONTEXT_TOKENS": active_profile.get("max_context_tokens", 128000),
        "SUB_LIST": sub_list,
        "ALL_MODELS": all_models,
        # Search api key
        "TAVILY_API_KEY": tavily_api_key,
        # Think Level
        "THINKING": active_profile.get("thinking", "disabled"),
        "EFFORT": active_profile.get("effort", "medium"),
    }
# End-def

##
 # ========================================
 # @section III. Remote Device Config
 # ========================================
 #

##
 # @brief Load remote device config (devices.yaml).
 #
 # @param devices_path Path to devices.yaml; default: config_dir()/devices.yaml.
 #
 # @return (devices_dict, errors_list). devices_dict maps alias -> cfg
 # (each cfg carries an "alias" key and normalized defaults); errors_list
 # holds per-entry validation errors (invalid entries are skipped).
 #
 # @note Entry fields:
 #   ssh:    type, host, user required; auth = key_path or password (>=1);
 #           optional port(22), timeout(120), security{allow,block}.
 #   serial: type, port required; optional baudrate(115200), parity, bytesize,
 #           stopbits, newline, encoding, buf_size(65536), read_timeout(3).
 #   Unknown types are rejected. The file lives under the config dir
 #   (sandbox-shielded, agent tools cannot read it).
 #
 # @note Aliases are normalized to strings: YAML mapping keys may be
 # non-string (e.g. "123:"), and every alias key must be a string so
 # lookups and sorted(devices.keys()) work consistently.
 #
def load_devices_config(devices_path=None):
    if devices_path is None:
        try:
            from mk.lib.paths import config_dir
            devices_path = os.path.join(config_dir(), "devices.yaml")
        except Exception:
            devices_path = os.path.join(os.getcwd(), "devices.yaml")
        # End-try
    # End-if

    if not os.path.exists(devices_path):
        return {}, []
    # End-if

    import yaml

    try:
        with open(devices_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        # End-with
    except yaml.YAMLError as e:
        return {}, [f"devices.yaml parse error: {e}"]
    # End-try

    if not isinstance(raw, dict):
        return {}, ["devices.yaml root must be a mapping of aliases"]
    # End-if

    devices = {}
    errors = []
    for raw_alias, entry in raw.items():
        # Normalize: YAML keys may be non-string; all devices keys must be
        # strings for consistent lookups and sorted() output.
        alias = str(raw_alias)
        if not isinstance(entry, dict):
            errors.append(f"Device '{alias}': entry must be a mapping.")
            continue
        # End-if

        dtype = str(entry.get("type", "")).strip().lower()
        if dtype not in ("ssh", "serial"):
            errors.append(f"Device '{alias}': unknown type '{entry.get('type')}'. Allowed: ssh, serial.")
            continue
        # End-if

        if dtype == "ssh":
            if not entry.get("host"):
                errors.append(f"Device '{alias}': missing required field 'host'.")
                continue
            # End-if
            if not entry.get("user"):
                errors.append(f"Device '{alias}': missing required field 'user'.")
                continue
            # End-if
            if not entry.get("key_path") and not entry.get("password"):
                errors.append(f"Device '{alias}': missing auth (need 'key_path' or 'password').")
                continue
            # End-if
            entry.setdefault("port", 22)
            entry.setdefault("timeout", 120)
        else:
            if not entry.get("port"):
                errors.append(f"Device '{alias}': missing required field 'port'.")
                continue
            # End-if
            entry.setdefault("baudrate", 115200)
            entry.setdefault("read_timeout", 3)
            entry.setdefault("buf_size", 65536)
        # End-if

        entry["alias"] = alias
        entry["type"] = dtype
        devices[alias] = entry
    # End-for

    return devices, errors
# End-def
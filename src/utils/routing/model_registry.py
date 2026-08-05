##
 # @file src/utils/routing/model_registry.py
 # @date 2026/08/05
 # 
 # @brief Wrapped model into a class obj.
 #

from dataclasses import dataclass, field

##
 # ========================================
 # @section I. Validation helpers (Same logic with config.py)
 # ========================================
 #

_VALID_THINKING = {"enabled", "disabled"}
_VALID_EFFORT   = {"low", "medium", "high", "max"}

##
 # @brief Normalise a thinking value, falling back to ``"disabled"``.
 # 
 # @param value: Model metadata (json).
 # @param model_id: Model ID.
 #
 # @return "enabled" or "disabled".
 # @retval enabled: if specify "enabled" in metadata.
 # @retval disabled: "disabled" on missing or invalid values (safe default).
 #
def _safe_thinking(value, model_id: str = "") -> str:
    if not isinstance(value, str):
        print(f"[!] Model '{model_id}': 'thinking' must be a string. "
              f"Defaulting to 'disabled'.")
        return "disabled"
    v = value.strip().lower()
    if v not in _VALID_THINKING:
        print(f"[!] Model '{model_id}': invalid thinking='{v}'. "
              f"Defaulting to 'disabled'.")
        return "disabled"
    return v
# End-def

##
 # @brief Normalise an effort value, falling back to ``"medium"``.
 # 
 # @param value: Model metadata (json).
 # @param model_id: Model ID.
 #
 # @return config in metadata or "medium" on missing or invalid values (safe default).
 #
def _safe_effort(value, model_id: str = "") -> str:
    if not isinstance(value, str):
        print(f"[!] Model '{model_id}': 'effort' must be a string. "
              f"Defaulting to 'medium'.")
        return "medium"
    v = value.strip().lower()
    if v not in _VALID_EFFORT:
        print(f"[!] Model '{model_id}': invalid effort='{v}'. "
              f"Defaulting to 'medium'.")
        return "medium"
    return v
# End-def

##
 # ========================================
 # @section II. Dataclass
 # ========================================
 #

##
 # @brief Model Wrapper Struct.
 #
@dataclass
class RegistryModelSpec:
    alias: str
    provider: str
    model_id: str
    base_url: str = ""
    api_key: str = ""
    max_tokens: int = 8192
    conditions: list = field(default_factory=list)
    tpm: int = 0
    rpm: int = 0
    rpd: int = 0
    thinking: str = "disabled"
    effort: str = "medium"
# End-class

##
 # ========================================
 # @section III. Registry Factor
 # ========================================
 #

##
 # @brief Registry Factor of `RegistryModelSpec`.
 #
class ModelRegistry:
    ##
     # @brief Constructor.
     #
    def __init__(self, all_models, sub_list):
        self._specs = {}
        self._ordered_aliases = []
        self._load(all_models, sub_list)
    # End-def

    ##
     # @brief registry all models.
     #
     # @param all_models main and sub LLM models.
     # @param sub_list models in SUB_LIST.
     #
    def _load(self, all_models, sub_list):
        # Create lookup dict for quick model matching
        model_lookup = {m.get("model_id"): m for m in all_models}

        # Strictly follow the priority in SUB_LIST
        for model_id in sub_list:
            if model_id in model_lookup:
                m = model_lookup[model_id]
                alias = model_id

                thinking = _safe_thinking(m.get("thinking", "disabled"), model_id)
                effort   = _safe_effort(m.get("effort", "medium"), model_id)

                spec = RegistryModelSpec(
                    alias=alias,
                    provider=m.get("sdk_type", "Anthropic"),
                    model_id=model_id,
                    base_url=m.get("base_url", ""),
                    api_key=m.get("api_key", ""),
                    max_tokens=m.get("max_token", 8192),
                    conditions=m.get("conditions", []),
                    tpm=m.get("TPM", 0),
                    rpm=m.get("RPM", 0),
                    rpd=m.get("RPD", 0),
                    thinking=thinking,
                    effort=effort,
                )
                self._specs[alias] = spec
                self._ordered_aliases.append(alias)
            # End-if
        # End-for
    # End-def

    ##
     # @brief Get specified model's RegistryModelSpec.
     #
     # @param alias specified model.
     #
    def get_spec(self, alias: str) -> RegistryModelSpec:
        return self._specs.get(alias)
    # End-def

    ##
     # @brief Get all models' RegistryModelSpec.
     #
    def get_all_subagent_specs(self) -> list:
        return [self._specs[alias] for alias in self._ordered_aliases]
    # End-def
# End-class
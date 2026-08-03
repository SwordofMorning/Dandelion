# src/utils/routing/model_registry.py

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Validation helpers (mirror those in config.py for consistency)
# ---------------------------------------------------------------------------

_VALID_THINKING = {"enabled", "disabled"}
_VALID_EFFORT   = {"low", "medium", "high", "max"}

def _safe_thinking(value, model_id: str = "") -> str:
    """Normalise a thinking value, falling back to ``"disabled"``."""
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


def _safe_effort(value, model_id: str = "") -> str:
    """Normalise an effort value, falling back to ``"medium"``."""
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


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

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
    # ---- New: thinking & effort fields ----
    thinking: str = "disabled"
    effort: str = "medium"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ModelRegistry:
    def __init__(self, all_models, sub_list):
        self._specs = {}
        self._ordered_aliases = []
        self._load(all_models, sub_list)

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

    def get_spec(self, alias: str) -> RegistryModelSpec:
        return self._specs.get(alias)

    def get_all_subagent_specs(self) -> list:
        return [self._specs[alias] for alias in self._ordered_aliases]

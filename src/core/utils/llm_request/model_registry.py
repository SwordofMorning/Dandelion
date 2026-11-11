##
 # @file src/core/utils/llm_request/model_registry.py
 # @date 2026/11/11
 #
 # @brief Configured SubAgent model registry and normalized specifications.
 # Preserves ordered SUB_LIST aliases, model descriptions, reasoning settings,
 # output token limits, and media capabilities for LLM-driven SubAgent selection.
 #

from dataclasses import dataclass, field

from ..llm_provider.effort import FLAVOR_AUTO, VALID_FLAVORS
from ..config.config import normalize_description

_VALID_THINKING = {"enabled", "disabled"}
_VALID_EFFORT = {"low", "medium", "high", "max"}


##
 # @brief Normalise a thinking value, falling back to "disabled".
 #
def _safe_thinking(value, model_id: str = "") -> str:
    if not isinstance(value, str):
        return "disabled"
    # End-if
    v = value.strip().lower()
    return v if v in _VALID_THINKING else "disabled"
# End-def


##
 # @brief Normalise an effort value, falling back to "medium".
 #
def _safe_effort(value, model_id: str = "") -> str:
    if not isinstance(value, str):
        return "medium"
    # End-if
    v = value.strip().lower()
    return v if v in _VALID_EFFORT else "medium"
# End-def


##
 # @brief Normalise an endpoint flavor (compat) value, falling back to "auto".
 #
def _safe_compat(value, model_id: str = "") -> str:
    if not isinstance(value, str):
        return FLAVOR_AUTO
    # End-if
    v = value.strip().lower()
    if v != FLAVOR_AUTO and v not in VALID_FLAVORS:
        return FLAVOR_AUTO
    # End-if
    return v
# End-def


##
 # @brief Normalized SubAgent model specification.
 #
@dataclass
class RegistryModelSpec:
    alias: str
    provider: str
    model_id: str
    description: str = ""
    base_url: str = ""
    api_key: str = ""
    max_tokens: int = 8192
    thinking: str = "disabled"
    effort: str = "medium"
    compat: str = FLAVOR_AUTO
    media_exts: list = field(default_factory=list)
    media_limits: dict = field(default_factory=dict)
# End-class


##
 # @brief Ordered registry of configured SubAgent models from SUB_LIST.
 #
class ModelRegistry:
    ##
     # @brief Constructor.
     #
     # @param all_models Enriched model dictionaries from load_api_config.
     # @param sub_list Ordered model aliases allowed as SubAgents.
     #
    def __init__(self, all_models=None, sub_list=None):
        self._specs = {}
        self._ordered_aliases = []
        self._load(all_models or [], sub_list or [])
    # End-def

    ##
     # @brief Populate typed specifications strictly following SUB_LIST order.
     #
    def _load(self, all_models, sub_list):
        if not sub_list:
            return
        # End-if
        model_lookup = {m.get("name"): m for m in all_models if isinstance(m, dict)}

        for raw_alias in sub_list:
            alias = str(raw_alias or "").strip()
            if not alias or alias in self._specs:
                continue
            # End-if
            if alias not in model_lookup:
                raise ValueError(
                    f"SubAgent model '{alias}' in SUB_LIST is not defined in ALL_MODELS."
                )
            # End-if
            m = model_lookup[alias]
            if "conditions" in m:
                raise ValueError(
                    f"Model '{alias}' uses removed 'conditions' field; use 'description' instead."
                )
            # End-if
            description = normalize_description(m.get("description"), alias, required=True)
            max_tok = m.get("max_token", 8192)
            try:
                max_tok = int(max_tok) if int(max_tok) > 0 else 8192
            except (TypeError, ValueError):
                max_tok = 8192
            # End-try

            spec = RegistryModelSpec(
                alias=alias,
                provider=m.get("sdk_type", "Message"),
                model_id=str(m.get("model_id", "")).strip(),
                description=description,
                base_url=m.get("base_url", ""),
                api_key=m.get("api_key", ""),
                max_tokens=max_tok,
                thinking=_safe_thinking(m.get("thinking", "disabled"), alias),
                effort=_safe_effort(m.get("effort", "medium"), alias),
                compat=_safe_compat(m.get("compat", FLAVOR_AUTO), alias),
                media_exts=list(m.get("file") or []),
                media_limits=dict(m.get("media_limits") or {}),
            )
            self._specs[alias] = spec
            self._ordered_aliases.append(alias)
        # End-for
    # End-def

    ##
     # @brief Look up a SubAgent specification by alias.
     #
    def get_spec(self, alias: str) -> RegistryModelSpec:
        return self._specs.get(str(alias or "").strip())
    # End-def

    ##
     # @brief Return all registered SubAgent specifications in SUB_LIST order.
     #
    def get_all_specs(self) -> list:
        return [self._specs[alias] for alias in self._ordered_aliases]
    # End-def

    ##
     # @brief Alias kept for concise inspection of ordered SubAgent names.
     #
    def aliases(self) -> list:
        return list(self._ordered_aliases)
    # End-def

    ##
     # @brief Format '- <alias>: <description>' lines for tool descriptions.
     #
    def format_catalog(self) -> str:
        return "\n".join(
            f"- {self._specs[alias].alias}: {self._specs[alias].description}"
            for alias in self._ordered_aliases
        )
    # End-def
# End-class

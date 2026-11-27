##
 # @file packages/dandelion-core/src/dandelion_core/tool/agent/base_delegation_tool.py
 # @date 2026/11/28
 #
 # @brief Abstract base class for SubAgent and background-task delegation tools.
 # Centralizes ModelRegistry model-enum discovery, model catalog description
 # formatting, and model alias validation across CLI and Remote delegation tools.
 #

from abc import ABC

from ..base_tool import BaseTool


##
 # @brief Shared abstract base for tools that delegate work to configured SubAgent models.
 #
class BaseDelegationTool(BaseTool, ABC):
    ##
     # @brief Constructor.
     #
     # @param workspace_dir Optional workspace root directory.
     # @param registry Optional ModelRegistry providing allowed SUB_LIST models.
     #
     # @return None.
     #
    def __init__(self, workspace_dir=None, registry=None):
        super().__init__(workspace_dir=workspace_dir)
        self.registry = registry
    # End-def

    ##
     # @brief Resolve the active ModelRegistry from self.registry or self.pool.
     #
     # @return ModelRegistry instance, or None when unavailable.
     #
    def _get_registry(self):
        reg = getattr(self, "registry", None)
        if reg is not None:
            return reg
        # End-if
        pool = getattr(self, "pool", None)
        if pool is not None:
            return getattr(getattr(pool, "safe_client", None), "registry", None)
        # End-if
        return None
    # End-def

    ##
     # @brief Return ordered configured SubAgent model aliases for JSON Schema enum.
     #
     # @return List of model alias strings.
     #
    def _model_enum(self):
        registry = self._get_registry()
        return registry.aliases() if registry is not None else []
    # End-def

    ##
     # @brief Build the JSON Schema property dict for the 'model' parameter.
     #
     # @param description Optional custom description for the model field.
     #
     # @return JSON Schema property dictionary.
     #
    def _build_model_property(self, description=None):
        aliases = self._model_enum()
        prop = {
            "type": "string",
            "description": description or (
                "Name of the SubAgent model to execute this sub-task. "
                "Select according to the model descriptions in this tool."
            ),
        }
        if aliases:
            prop["enum"] = aliases
        # End-if
        return prop
    # End-def

    ##
     # @brief Format the SubAgent model catalog, optionally appended to a base description.
     #
     # @param base_desc Optional base tool description string. When empty, returns
     #        the formatted catalog lines alone.
     #
     # @return Formatted description or catalog string.
     #
    def _format_model_catalog(self, base_desc=""):
        registry = self._get_registry()
        catalog = registry.format_catalog() if registry is not None else ""
        if not base_desc:
            return catalog
        # End-if
        if not catalog:
            return base_desc
        # End-if
        return f"{base_desc}\nAvailable SubAgent models:\n{catalog}"
    # End-def

    ##
     # @brief Validate a requested SubAgent model alias against the ModelRegistry.
     #
     # @param model_name Requested SubAgent model alias string.
     #
     # @return (True, "") when valid, or (False, error_message) when invalid.
     #
    def _validate_model(self, model_name):
        alias = str(model_name or "").strip()
        if not alias:
            return False, "Error: 'model' is required."
        # End-if
        registry = self._get_registry()
        if registry is not None and registry.get_spec(alias) is None:
            return False, (
                f"Error: unknown SubAgent model {alias!r}. "
                f"Available SubAgent models: {self._model_enum()}"
            )
        # End-if
        return True, ""
    # End-def
# End-class

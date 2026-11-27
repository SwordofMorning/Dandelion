##
 # @file packages/dandelion-core/src/dandelion_core/utils/llm_request/llm_request.py
 # @date 2026/11/28
 #
 # @brief LLM request normalization, cooperative cancellation, and SubAgent dispatch.
 # Wraps native provider requests with shared logging, runtime callbacks, and
 # cooperative TurnInterrupt cancellation, dispatching SubAgent requests to the
 # explicitly chosen model without silent cross-model fallback ("Let LLM Decide").
 #
 # @note Request paths:
 # - Main Agent:
 #      MyAgent.step() -> SafeLLMClient.safe_stream_request() -> default provider
 # - SubAgent:
 #      SubAgent.run() -> SafeLLMClient.subagent_request(model_alias, payload) -> cached provider
 #

import threading

from ...runtime import Runtime
from ..llm_provider import create_provider, normalize_messages
from ..llm_provider.blocks import RequestCancelledError
from ..llm_provider.effort import FLAVOR_AUTO
from .model_registry import ModelRegistry


##
 # @brief LLM request wrapper.
 #
class SafeLLMClient:
    ##
     # ========================================
     # @section I. Constructor and Model (Provider).
     # ========================================
     #

    ##
     # @brief Constructor.
     #
     # @param api_key API key for the main agent.
     # @param base_url Base URL for the main agent.
     # @param model_id Model identifier for the main agent.
     # @param sdk_type Protocol ("Message", "Chat Completions", "Responses",
     #        "Generate Content", "Interactions").
     # @param all_models Full model list for sub-agent routing.
     # @param sub_list Allowed sub-agent model alias list.
     # @param sub_depth Maximum SubAgent recursion depth.
     # @param thinking "enabled" or "disabled" - extended thinking toggle for the main agent.
     # @param effort Reasoning effort: "low", "medium", "high", "xhigh", or "max".
     # @param compat Endpoint flavor of the main agent's link terminal family.
     # @param logger Optional SessionManager for logging final API payloads.
     # @param runtime Optional BaseRuntime for emitting messages and cancellation polling.
     #
     # @return None.
     #
    def __init__(self, api_key, base_url, model_id, sdk_type="Message",
                 all_models=None, sub_list=None, sub_depth=1,
                 thinking="disabled", effort="medium",
                 compat=FLAVOR_AUTO,
                 logger=None, runtime=None):
        self.model_id = model_id
        self.sdk_type = str(sdk_type).strip()
        self.thinking = thinking
        self.effort = effort
        self.compat = compat
        self.logger = logger
        self.runtime = runtime if runtime is not None else Runtime(echo=True)

        # Setup Default Provider for Main Agent.
        self.provider = self._create_provider(
            self.sdk_type, api_key, base_url, model_id, thinking, effort, compat
        )

        # Setup ModelRegistry and cached providers for explicit SubAgent selection.
        self._provider_cache = {}
        self._cache_lock = threading.Lock()
        try:
            sub_enabled = int(sub_depth) > 0
        except (TypeError, ValueError):
            sub_enabled = False
        # End-try
        self.registry = ModelRegistry(all_models, sub_list, enabled=sub_enabled)
    # End-def

    ##
     # @brief Native provider factory (raises ValueError for an unknown SDK_TYPE).
     #
     # @param sdk_type Configured protocol name.
     # @param api_key Provider API key.
     # @param base_url Provider base URL.
     # @param model_id Provider model identifier.
     # @param thinking "enabled" or "disabled".
     # @param effort Abstract effort level.
     # @param compat Endpoint flavor override or "auto".
     #
     # @return Native Provider instance.
     #
    def _create_provider(self, sdk_type, api_key, base_url, model_id,
                         thinking="disabled", effort="medium", compat=FLAVOR_AUTO):
        return create_provider(
            sdk_type,
            api_key,
            base_url,
            model_id,
            thinking=thinking,
            effort=effort,
            compat=compat,
        )
    # End-def

    ##
     # ========================================
     # @section II. Public request wrappers
     # ========================================
     #

    ##
     # @brief Return a copy of payload whose 'messages' have adjacent same-role
     # entries merged via the native normalizer.
     #
     # @param payload Request payload dictionary.
     #
     # @return Shallow-copied payload with normalized messages list.
     #
    def _with_normalized_messages(self, payload):
        if not payload.get("messages"):
            return payload
        # End-if
        out = dict(payload)
        out["messages"] = normalize_messages(payload["messages"])
        return out
    # End-def

    ##
     # @brief Resolve the cancellation predicate for an outgoing request.
     #
     # @param cancelled Optional explicit zero-argument cancellation predicate.
     #
     # @return Callable cancellation predicate (or None).
     #
    def _resolve_cancelled(self, cancelled=None):
        if cancelled is not None:
            return cancelled
        # End-if
        if self.runtime is not None:
            return self.runtime.cancelled
        # End-if
        return None
    # End-def

    ##
     # @brief Non-streaming request wrapper with logging and cooperative cancellation.
     #
     # @param payload Request payload dictionary.
     # @param log_tag Audit log header tag.
     # @param cancelled Optional zero-argument predicate; defaults to self.runtime.cancelled.
     #
     # @return (response_object, None) on success, or (None, error_string) on failure.
     #
    def safe_request(self, payload, log_tag="PRE LLM CALL - MAIN", cancelled=None):
        payload = self._with_normalized_messages(payload)
        cancel_cb = self._resolve_cancelled(cancelled)
        return self.provider.safe_request(
            payload,
            logger=self.logger,
            log_tag=log_tag,
            cancelled=cancel_cb,
        )
    # End-def

    ##
     # @brief Streaming request wrapper with logging, echo toggle, and cancellation.
     #
     # @param payload Request payload dictionary.
     # @param log_tag Audit log header tag.
     # @param echo Optional bool overriding self.runtime.echo.
     # @param cancelled Optional zero-argument predicate; defaults to self.runtime.cancelled.
     #
     # @return (response_object, None) on success, or (None, error_string) on failure.
     #
    def safe_stream_request(self, payload, log_tag="PRE LLM CALL - MAIN", echo=None, cancelled=None):
        payload = self._with_normalized_messages(payload)
        echo = self.runtime.echo if echo is None else echo
        cancel_cb = self._resolve_cancelled(cancelled)
        return self.provider.safe_stream_request(
            payload,
            logger=self.logger,
            log_tag=log_tag,
            echo=echo,
            cancelled=cancel_cb,
        )
    # End-def

    ##
     # @brief Extract concatenated text from provider response content blocks.
     #
     # @param content Response content list or string.
     #
     # @return Concatenated plain-text string.
     #
    def extract_text(self, content):
        return self.provider.extract_text(content)
    # End-def

    ##
     # ========================================
     # @section III. Explicit SubAgent model dispatch
     # ========================================
     #

    ##
     # @brief Get or create a cached provider for the given SubAgent alias.
     #
     # @param alias SubAgent model alias from ModelRegistry.
     #
     # @return Native Provider instance.
     #
    def _get_cached_provider(self, alias):
        with self._cache_lock:
            if alias not in self._provider_cache:
                spec = self.registry.get_spec(alias)
                self._provider_cache[alias] = self._create_provider(
                    spec.provider,
                    spec.api_key,
                    spec.base_url,
                    spec.model_id,
                    thinking=spec.thinking,
                    effort=spec.effort,
                    compat=spec.compat,
                )
            # End-if
            return self._provider_cache[alias]
        # End-with
    # End-def

    ##
     # @brief Dispatch a request to the explicitly selected SubAgent model.
     #
     # Normalizes message alternation, enforces the SubAgent model's own
     # max_tokens limit, propagates cooperative cancellation, and returns any
     # provider error directly to the parent agent without silent fallback.
     #
     # @param model_alias Selected SubAgent alias from SUB_LIST.
     # @param payload Request payload (tools/messages/max_tokens/system).
     # @param stream True -> safe_stream_request, False -> safe_request.
     # @param echo Print the stream (False for parallel SubAgents).
     # @param log_tag Optional audit tag written to api.log.
     # @param cancelled Optional zero-argument cancellation predicate.
     #
     # @return (response, None) on success; (None, error_string) on failure.
     #
    def subagent_request(self, model_alias, payload, stream=True, echo=None, log_tag=None, cancelled=None):
        echo = self.runtime.echo if echo is None else echo
        cancel_cb = self._resolve_cancelled(cancelled)
        spec = self.registry.get_spec(model_alias)
        if spec is None:
            return None, (
                f"Unknown SubAgent model '{model_alias}'. "
                f"Available models: {self.registry.aliases()}"
            )
        # End-if

        provider = self._get_cached_provider(spec.alias)
        req_payload = self._with_normalized_messages(payload)
        req_payload["model"] = spec.model_id

        raw_max = req_payload.get("max_tokens")
        if raw_max is None:
            req_payload["max_tokens"] = int(spec.max_tokens)
        else:
            try:
                req_payload["max_tokens"] = min(max(int(raw_max), 1), int(spec.max_tokens))
            except (TypeError, ValueError):
                req_payload["max_tokens"] = int(spec.max_tokens)
            # End-try
        # End-if

        tag = log_tag or f"SUBAGENT MODEL -> '{spec.alias}'"
        if stream:
            resp, err = provider.safe_stream_request(
                req_payload,
                logger=self.logger,
                log_tag=tag,
                echo=echo,
                cancelled=cancel_cb,
            )
        else:
            resp, err = provider.safe_request(
                req_payload,
                logger=self.logger,
                log_tag=tag,
                cancelled=cancel_cb,
            )
        # End-if

        if err is not None:
            if isinstance(err, RequestCancelledError):
                return None, err
            # End-if
            return None, f"SubAgent model '{spec.alias}' ({spec.model_id}) failed: {err}"
        # End-if
        return resp, None
    # End-def
# End-class

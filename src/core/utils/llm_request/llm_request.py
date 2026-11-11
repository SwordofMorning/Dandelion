##
 # @file src/core/utils/llm_request/llm_request.py
 # @date 2026/08/05
 #
 # @brief LLM request normalization and explicit SubAgent model dispatch.
 # Wraps provider requests with shared logging and runtime output callbacks,
 # and dispatches SubAgent requests to the explicitly chosen model without
 # hidden category guessing or cross-model fallback ("Let LLM Decide").
 #
 # @note Request paths:
 # - Main Agent:
 #      MyAgent.step() -> SafeLLMClient.safe_stream_request() -> default provider
 # - SubAgent:
 #      SubAgent.run() -> SafeLLMClient.subagent_request(model_alias, payload) -> cached provider
 #

import threading
from src.core.runtime import Runtime
from ..llm_provider import AnthropicProvider, OpenAIProvider
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
     # @param sdk_type SDK type ("Message", "Chat Completions").
     # @param all_models Full model list for sub-agent routing.
     # @param sub_list Allowed sub-agent model alias list.
     # @param thinking "enabled" or "disabled" - extended thinking toggle for the main agent.
     # @param effort Reasoning effort: "low", "medium", "high", or "max".
     # @param compat Endpoint flavor of the main agent's link terminal family
     # ("auto" detects it); see llm_provider/effort.py.
     # @param logger Optional SessionManager for logging final API payloads (post-injection).
     # @param runtime Optional Runtime callbacks for emitting routing messages.
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

        # Setup Default Provider for Main Agent
        self.provider = self._create_provider(
            self.sdk_type, api_key, base_url, model_id, thinking, effort, compat
        )

        # Setup ModelRegistry and cached providers for explicit SubAgent selection
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
     # @brief Provider factory.
     #
    def _create_provider(self, sdk_type, api_key, base_url, model_id,
                         thinking="disabled", effort="medium", compat=FLAVOR_AUTO):
        sdk = str(sdk_type).strip().lower()
        if sdk in ["message", "messages", "anthropic"]:
            return AnthropicProvider(api_key, base_url, model_id,
                                     thinking=thinking, effort=effort, compat=compat)
        elif sdk in ["chat completions", "chat completion", "chat_completions", "openai"]:
            return OpenAIProvider(api_key, base_url, model_id,
                                  thinking=thinking, effort=effort, compat=compat)
        elif sdk in ["responses", "response"]:
            raise ValueError("SDK_TYPE='Responses' (OpenAI Responses API) is not supported yet.")
        elif sdk in ["interactions", "interaction"]:
            raise ValueError("SDK_TYPE='Interactions' (Gemini Interactions API) is not supported yet.")
        elif sdk in ["generate content", "generatecontent", "ai studio", "gemini", "google"]:
            raise ValueError(
                f"Google/Gemini native SDK support has been removed. "
                f"Please use SDK_TYPE='Chat Completions' with Google's OpenAI-compatible endpoint instead."
            )
        else:
            raise ValueError(
                f"Unsupported SDK_TYPE: '{sdk_type}'. Supported types: 'Message' (Anthropic), 'Chat Completions' (OpenAI)."
            )
    # End-def

    ##
     # ========================================
     # @section II. Public request wrappers
     # ========================================
     #

    ##
     # @brief Merge two message contents into one Anthropic-style block list,
     # preserving the original order of both sides.
     #
     # @param a message contents a.
     # @param b message contents b.
     #
     # @return [a + b].
     #
    @staticmethod
    def _merge_content(a, b):
        def as_blocks(c):
            if isinstance(c, str):
                return [{"type": "text", "text": c}]
            if isinstance(c, list):
                return list(c)
            return []
        # End-def
        return as_blocks(a) + as_blocks(b)
    # End-def

    ##
     # @brief Merge adjacent messages with the same role (e.g. the consecutive
     # user turns produced by history compaction around the summary message),
     # preserving content order. Returns a new list; input is not mutated.
     #
    @classmethod
    def _normalize_messages(cls, messages):
        merged = []
        for msg in messages or []:
            if merged and merged[-1].get("role") == msg.get("role"):
                prev = merged[-1]
                prev["content"] = cls._merge_content(
                    prev["content"], msg.get("content", ""))
            else:
                merged.append(dict(msg))
        return merged
    # End-def

    ##
     # @brief Return a copy of payload whose 'messages' have adjacent same-role
     # entries merged, so providers that require strict role alternation
     # (Anthropic, Gemini) never receive consecutive identical roles.
     #
    def _with_normalized_messages(self, payload):
        if not payload.get("messages"):
            return payload
        out = dict(payload)
        out["messages"] = self._normalize_messages(payload["messages"])
        return out
    # End-def

    ##
     # @brief Non-streaming request wrapper. Logs final payload after thinking injection.
     #
    def safe_request(self, payload, log_tag="PRE LLM CALL - MAIN"):
        payload = self._with_normalized_messages(payload)
        return self.provider.safe_request(payload, logger=self.logger, log_tag=log_tag)
    # End-def

    ##
     # @brief Wraps the SDK stream call for real-time console output.
     # Automatically accumulates tool calls and text into a final message.
     # Logs the final payload (with thinking injected) if logger is configured.
     #
     # @return (response_object, None) on success.
     # @return (None, error_string) on failure.
     #
    def safe_stream_request(self, payload, log_tag="PRE LLM CALL - MAIN", echo=None):
        payload = self._with_normalized_messages(payload)
        echo = self.runtime.echo if echo is None else echo
        return self.provider.safe_stream_request(payload, logger=self.logger, log_tag=log_tag, echo=echo)
    # End-def

    ##
     # @brief Extract text wrapper.
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
    def _get_cached_provider(self, alias):
        with self._cache_lock:
            if alias not in self._provider_cache:
                spec = self.registry.get_spec(alias)
                self._provider_cache[alias] = self._create_provider(
                    spec.provider, spec.api_key, spec.base_url, spec.model_id,
                    thinking=spec.thinking,
                    effort=spec.effort,
                    compat=spec.compat,
                )
            return self._provider_cache[alias]
    # End-def

    ##
     # @brief Dispatch a request to the explicitly selected SubAgent model.
     #
     # Normalizes message alternation, enforces the SubAgent model's own
     # max_tokens limit, and returns any provider error directly to the parent
     # agent without silent cross-model fallback so the parent LLM decides next steps.
     #
     # @param model_alias Selected SubAgent alias from SUB_LIST.
     # @param payload Request payload (tools/messages/max_tokens/system).
     # @param stream True -> safe_stream_request, False -> safe_request.
     # @param echo Print the stream (False for parallel SubAgents).
     # @param log_tag Optional audit tag written to api.log.
     #
     # @return (response, None) on success; (None, error_string) on failure.
     #
    def subagent_request(self, model_alias, payload, stream=True, echo=None, log_tag=None):
        echo = self.runtime.echo if echo is None else echo
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
                req_payload, logger=self.logger, log_tag=tag, echo=echo
            )
        else:
            resp, err = provider.safe_request(
                req_payload, logger=self.logger, log_tag=tag
            )
        # End-if

        if err is not None:
            return None, f"SubAgent model '{spec.alias}' ({spec.model_id}) failed: {err}"
        # End-if
        return resp, None
    # End-def
# End-class

##
 # @file src/utils/safe_llm/safe_llm.py
 # @date 2026/08/05
 # 
 # @brief Thread-safe wrapper around LLM providers: message normalization,
 # logging, and sub-agent model routing with retry/fallback.
 # 
 # @note Two LLM Request:
 # - Main Agent
 #      MyAgent.step() -> payload -> SafeLLMClient.safe_stream_request() -> default provider (Main Agent's Model) -> request to LLM
 # - Sub Agent
 #      PlanTool.execute()  -> SafeLLMClient.route_request() -> Model by Routing -> cached provider -> request to LLM
 #      SubAgent.run()      -> SafeLLMClient.route_request() -> Model by Routing -> cached provider -> request to LLM
 #
 # For main: Message normalization (_normalize_messages) 
 # and memory/task state injection both occur in the main agent path.
 #
 # For sub: Dynamically selects models based on task_description/toolset/depth,
 # with retries and fallbacks; no memory/task-state injection (main-agent path only).
 #
 # BTW, PlanTool is a bypass LLM call which does not go through the Main Agent.
 #

import threading
from ..llm_provider import AnthropicProvider, GeminiProvider, OpenAIProvider

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
     # @param sdk_type SDK type ("Anthropic", "OpenAI", "Gemini", "AI Studio", "NVIDIA").
     # @param all_models Full model list for sub-agent routing.
     # @param thinking "enabled" or "disabled" — extended thinking toggle for the main agent.
     # @param effort Reasoning effort: "low", "medium", "high", or "max".
     # @param logger Optional SessionManager for logging final API payloads (post-injection).
     #
    def __init__(self, api_key, base_url, model_id, sdk_type="Anthropic",
                 all_models=None, sub_list=None,
                 thinking="disabled", effort="medium",
                 logger=None):
        self.model_id = model_id
        self.sdk_type = sdk_type.lower()
        self.thinking = thinking
        self.effort = effort
        self.logger = logger

        # Setup Default Provider for Main Agent
        self.provider = self._create_provider(
            self.sdk_type, api_key, base_url, model_id, thinking, effort
        )

        # Setup Router Components for SubAgents
        self._provider_cache = {}
        self._cache_lock = threading.Lock()
        self._registry = None
        self._policy = None
        self._rate_limiter = None

        if all_models and sub_list:
            from ..routing import ModelRegistry, RoutingPolicy, RateLimiter

            self._rate_limiter = RateLimiter()
            self._registry = ModelRegistry(all_models, sub_list)
            self._policy = RoutingPolicy(self._registry, self._rate_limiter)
        # End-if
    # End-def

    ##
     # @brief Provider factory.
     #
    def _create_provider(self, sdk_type, api_key, base_url, model_id,
                         thinking="disabled", effort="medium"):
        sdk = sdk_type.lower()
        if sdk in ["ai studio", "gemini"]:
            return GeminiProvider(api_key, base_url, model_id,
                                  thinking=thinking, effort=effort)
        elif sdk in ["openai", "nvidia"]:
            return OpenAIProvider(api_key, base_url, model_id,
                                  thinking=thinking, effort=effort)
        else:
            return AnthropicProvider(api_key, base_url, model_id,
                                     thinking=thinking, effort=effort)
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
    def safe_stream_request(self, payload, log_tag="PRE LLM CALL - MAIN"):
        payload = self._with_normalized_messages(payload)
        return self.provider.safe_stream_request(payload, logger=self.logger, log_tag=log_tag)
    # End-def

    ##
     # @brief Extract text wrapper.
     # 
    def extract_text(self, content):
        return self.provider.extract_text(content)
    # End-def

    ##
     # ========================================
     # @section III. Sub-agent routing
     # ========================================
     #

    ##
     # @brief Get or create a cached provider for the given sub-agent alias.
     #
     # @note Reads thinking & effort from RegistryModelSpec so each sub-agent
     # model receives its own thinking configuration.
     #
    def _get_cached_provider(self, alias):
        with self._cache_lock:
            if alias not in self._provider_cache:
                spec = self._registry.get_spec(alias)
                self._provider_cache[alias] = self._create_provider(
                    spec.provider, spec.api_key, spec.base_url, spec.model_id,
                    thinking=spec.thinking,
                    effort=spec.effort,
                )
            return self._provider_cache[alias]
    # End-def

    ##
     # @brief Route a request through the sub-agent model pool.
     #
     # Selects a model from SUB_LIST via RoutingPolicy (condition match + rate
     # limit), retries the selected model, then walks the fallback chain
     # (get_fallback_chain) when it keeps failing. Used by PlanTool and
     # SubAgent.run for all non-main-agent LLM calls; falls back to the main
     # provider when no sub-agent pool is configured.
     #
     # @param payload             Request payload (tools/messages/max_tokens/system).
     # @param task_description    Sub-agent task text, feeds condition inference.
     # @param toolset_name        Sub-agent toolset ("minimal"/"planning"/...).
     # @param depth               Sub-agent recursion depth (>=2 forces "complex").
     # @param stream              True -> safe_stream_request, False -> safe_request.
     # @param estimated_tokens    Token estimate used by the rate limiter.
     #
     # @return (response, None) on success; (None, error_string) on failure.
     #
     # @see PlanTool.execute()
     # @see SubAgent.run()
     #
    def route_request(self, payload, task_description="", toolset_name="minimal",
                      depth=0, stream=True, estimated_tokens=2000):
        # 0. Fallback to main agent model if SUB_LIST is empty.
        if not self._policy or not self._policy.specs:
            return self.safe_stream_request(payload) if stream else self.safe_request(payload)

        # 1. Get specified subagent model via conditions.
        try:
            alias = self._policy.select_model(task_description, toolset_name, depth, estimated_tokens)
        except RuntimeError as e:
            return None, str(e)

        # 2. Get subagent model's RegistryModelSpec.
        spec = self._registry.get_spec(alias)
        inferred = self._policy.infer_conditions(task_description, toolset_name, depth)

        print(f"\n[Router] Task -> '{alias}' | Conditions: {sorted(list(inferred))}"
              f" | thinking={spec.thinking} effort={spec.effort}")

        max_retries = 2
        current_alias = alias

        # 3. Retry the selected model up to `max_retries + 1` times.
        for attempt in range(max_retries + 1):
            provider = self._get_cached_provider(current_alias)

            # Use a copy to prevent payload mutation during retries
            req_payload = payload.copy()
            req_payload["model"] = spec.model_id

            log_tag = f"SUBAGENT ROUTE -> '{current_alias}' (attempt {attempt+1})"
            resp, err = provider.safe_stream_request(req_payload, logger=self.logger, log_tag=log_tag) if stream else provider.safe_request(req_payload, logger=self.logger, log_tag=log_tag)

            if err is None:
                return resp, None

            print(f"[Router] Model '{current_alias}' attempt {attempt+1} failed: {err}")

            # 4. All retries failed, walk the fallback chain.
            if attempt == max_retries:
                # Iterate all models; candidates come from `get_fallback_chain`.
                for fallback_alias in self._policy.get_fallback_chain(current_alias):
                    # If exhausted limits, ignore this one.
                    if not self._rate_limiter.acquire(fallback_alias, estimated_tokens):
                        continue

                    # Try to copy payload and request.
                    print(f"[Router] Falling back to '{fallback_alias}'...")
                    fb_provider = self._get_cached_provider(fallback_alias)
                    fb_spec = self._registry.get_spec(fallback_alias)

                    fb_payload = payload.copy()
                    fb_payload["model"] = fb_spec.model_id

                    fb_tag = f"SUBAGENT FALLBACK -> '{fallback_alias}'"
                    resp, err = fb_provider.safe_stream_request(fb_payload, logger=self.logger, log_tag=fb_tag) if stream else fb_provider.safe_request(fb_payload, logger=self.logger, log_tag=fb_tag)

                    # Request Success, return.
                    if err is None:
                        return resp, None

                    print(f"[Router] Fallback '{fallback_alias}' also failed: {err}")
                # End-for

                # All fallback fail, throw error.
                return None, f"All SubAgent models exhausted. Last error: {err}"
            # End-if
        # End-for

        return None, "Unexpected fallback exit"
    # End-def
# End-class
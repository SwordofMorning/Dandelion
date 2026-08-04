# src/utils/safe_llm/safe_llm.py

import threading
from ..llm_provider import AnthropicProvider, GeminiProvider, OpenAIProvider

class SafeLLMClient:
    def __init__(self, api_key, base_url, model_id, sdk_type="Anthropic",
                 all_models=None, sub_list=None,
                 thinking="disabled", effort="medium",
                 logger=None):
        """
        Args:
            api_key: API key for the main agent.
            base_url: Base URL for the main agent.
            model_id: Model identifier for the main agent.
            sdk_type: SDK type ("Anthropic", "OpenAI", "Gemini", "AI Studio", "NVIDIA").
            all_models: Full model list for sub-agent routing.
            sub_list: Sub-agent model ID list.
            thinking: "enabled" or "disabled" — extended thinking toggle for the main agent.
            effort: Reasoning effort: "low", "medium", "high", or "max".
            logger: Optional SessionManager for logging final API payloads (post-injection).
        """
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

    # ------------------------------------------------------------------
    # Provider factory (now passes thinking & effort)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Public request wrappers
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_content(a, b):
        """Merge two message contents into one Anthropic-style block list,
        preserving the original order of both sides."""
        def as_blocks(c):
            if isinstance(c, str):
                return [{"type": "text", "text": c}]
            if isinstance(c, list):
                return list(c)
            return []
        return as_blocks(a) + as_blocks(b)

    @classmethod
    def _normalize_messages(cls, messages):
        """Merge adjacent messages with the same role (e.g. the consecutive
        user turns produced by history compaction around the summary message),
        preserving content order. Returns a new list; input is not mutated."""
        merged = []
        for msg in messages or []:
            if merged and merged[-1].get("role") == msg.get("role"):
                prev = merged[-1]
                prev["content"] = cls._merge_content(
                    prev["content"], msg.get("content", ""))
            else:
                merged.append(dict(msg))
        return merged

    def _with_normalized_messages(self, payload):
        """Return a copy of payload whose 'messages' have adjacent same-role
        entries merged, so providers that require strict role alternation
        (Anthropic, Gemini) never receive consecutive identical roles."""
        if not payload.get("messages"):
            return payload
        out = dict(payload)
        out["messages"] = self._normalize_messages(payload["messages"])
        return out

    def safe_request(self, payload, log_tag="PRE LLM CALL - MAIN"):
        """Non-streaming request wrapper. Logs final payload after thinking injection."""
        payload = self._with_normalized_messages(payload)
        return self.provider.safe_request(payload, logger=self.logger, log_tag=log_tag)

    def safe_stream_request(self, payload, log_tag="PRE LLM CALL - MAIN"):
        """
        Wraps the SDK stream call for real-time console output.
        Automatically accumulates tool calls and text into a final message.
        Logs the final payload (with thinking injected) if logger is configured.

        Returns:
            (response_object, None) on success.
            (None, error_string) on failure.
        """
        payload = self._with_normalized_messages(payload)
        return self.provider.safe_stream_request(payload, logger=self.logger, log_tag=log_tag)

    def extract_text(self, content):
        """Extract text wrapper."""
        return self.provider.extract_text(content)

    # ------------------------------------------------------------------
    # Sub-agent routing
    # ------------------------------------------------------------------

    def _get_cached_provider(self, alias):
        """Get or create a cached provider for the given sub-agent alias.

        Reads thinking & effort from RegistryModelSpec so each sub-agent
        model receives its own thinking configuration.
        """
        with self._cache_lock:
            if alias not in self._provider_cache:
                spec = self._registry.get_spec(alias)
                self._provider_cache[alias] = self._create_provider(
                    spec.provider, spec.api_key, spec.base_url, spec.model_id,
                    thinking=spec.thinking,
                    effort=spec.effort,
                )
            return self._provider_cache[alias]

    def route_request(self, payload, task_description="", toolset_name="minimal",
                      depth=0, stream=True, estimated_tokens=2000):
        if not self._policy or not self._policy.specs:
            # Fallback to main agent model if SUB_LIST is empty
            return self.safe_stream_request(payload) if stream else self.safe_request(payload)

        try:
            alias = self._policy.select_model(task_description, toolset_name, depth, estimated_tokens)
        except RuntimeError as e:
            return None, str(e)

        spec = self._registry.get_spec(alias)
        inferred = self._policy.infer_conditions(task_description, toolset_name, depth)

        print(f"\n[Router] Task -> '{alias}' | Conditions: {sorted(list(inferred))}"
              f" | thinking={spec.thinking} effort={spec.effort}")

        max_retries = 2
        current_alias = alias

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

            if attempt == max_retries:
                for fallback_alias in self._policy.get_fallback_chain(current_alias):
                    if not self._rate_limiter.acquire(fallback_alias, estimated_tokens):
                        continue

                    print(f"[Router] Falling back to '{fallback_alias}'...")
                    fb_provider = self._get_cached_provider(fallback_alias)
                    fb_spec = self._registry.get_spec(fallback_alias)

                    fb_payload = payload.copy()
                    fb_payload["model"] = fb_spec.model_id

                    fb_tag = f"SUBAGENT FALLBACK -> '{fallback_alias}'"
                    resp, err = fb_provider.safe_stream_request(fb_payload, logger=self.logger, log_tag=fb_tag) if stream else fb_provider.safe_request(fb_payload, logger=self.logger, log_tag=fb_tag)
                    if err is None:
                        return resp, None

                    print(f"[Router] Fallback '{fallback_alias}' also failed: {err}")

                return None, f"All SubAgent models exhausted. Last error: {err}"

        return None, "Unexpected fallback exit"

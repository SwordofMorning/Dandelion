# src/utils/safe_llm.py
from .llm_provider import AnthropicProvider, GeminiProvider, OpenAIProvider

class SafeLLMClient:
    def __init__(self, api_key, base_url, model_id, sdk_type="Anthropic", all_models=None, sub_list=None):
        self.model_id = model_id
        self.sdk_type = sdk_type.lower()
        
        # Setup Default Provider for Main Agent
        self.provider = self._create_provider(self.sdk_type, api_key, base_url, model_id)
        
        # Setup Router Components for SubAgents
        self._provider_cache = {}
        self._registry = None
        self._policy = None
        self._rate_limiter = None
        
        if all_models and sub_list:
            from .model_registry import ModelRegistry
            from .routing_policy import RoutingPolicy
            from .rate_limiter import RateLimiter
            
            self._rate_limiter = RateLimiter()
            self._registry = ModelRegistry(all_models, sub_list)
            self._policy = RoutingPolicy(self._registry, self._rate_limiter)

    def _create_provider(self, sdk_type, api_key, base_url, model_id):
        sdk = sdk_type.lower()
        if sdk in ["ai studio", "gemini"]:
            return GeminiProvider(api_key, base_url, model_id)
        elif sdk in ["openai", "nvidia"]:
            return OpenAIProvider(api_key, base_url, model_id)
        else:
            return AnthropicProvider(api_key, base_url, model_id)

    def safe_request(self, payload):
        """Non-streaming request wrapper."""
        return self.provider.safe_request(payload)

    def safe_stream_request(self, payload):
        """
        Wraps the SDK stream call for real-time console output.
        Automatically accumulates tool calls and text into a final message.
        
        Returns:
            (response_object, None) on success.
            (None, error_string) on failure.
        """
        return self.provider.safe_stream_request(payload)
            
    def extract_text(self, content):
        """Extract text wrapper."""
        return self.provider.extract_text(content)

    def _get_cached_provider(self, alias):
        if alias not in self._provider_cache:
            spec = self._registry.get_spec(alias)
            self._provider_cache[alias] = self._create_provider(
                spec.provider, spec.api_key, spec.base_url, spec.model_id
            )
        return self._provider_cache[alias]

    def route_request(self, payload, task_description="", toolset_name="minimal", depth=0, stream=True, estimated_tokens=2000):
        if not self._policy or not self._policy.specs:
            # Fallback to main agent model if SUB_LIST is empty
            return self.safe_stream_request(payload) if stream else self.safe_request(payload)

        alias = self._policy.select_model(task_description, toolset_name, depth, estimated_tokens)
        spec = self._registry.get_spec(alias)
        inferred = self._policy.infer_conditions(task_description, toolset_name, depth)
        
        print(f"\n[Router] Task -> '{alias}' | Conditions: {sorted(list(inferred))}")

        max_retries = 2
        current_alias = alias

        for attempt in range(max_retries + 1):
            provider = self._get_cached_provider(current_alias)
            
            # Temporary replace model_id inside provider for API Call
            payload["model"] = spec.model_id
            
            resp, err = provider.safe_stream_request(payload) if stream else provider.safe_request(payload)
            
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
                    payload["model"] = fb_spec.model_id
                    
                    resp, err = fb_provider.safe_stream_request(payload) if stream else fb_provider.safe_request(payload)
                    if err is None:
                        return resp, None
                        
                    print(f"[Router] Fallback '{fallback_alias}' also failed: {err}")
                    
                return None, f"All models exhausted. Last error: {err}"

        return None, "Unexpected fallback exit"
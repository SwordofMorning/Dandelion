##
 # @file src/core/utils/embedding.py
 # @date 2026/08/19
 # @brief Pluggable text embedding providers and L2 vector normalization.
 #

import hashlib, json, math, requests

##
 # @brief Exception raised when an embedding request or vector validation fails.
 #
class EmbeddingError(Exception):
    pass
# End-class

##
 # @brief Validate finite numeric components and L2-normalize a vector.
 #
def validate_and_normalize_vector(vec, expected_dims=None):
    if not isinstance(vec, (list, tuple)) or not vec:
        raise EmbeddingError("Vector must be a non-empty list")
    # End-if
    if expected_dims is not None and len(vec) != int(expected_dims):
        raise EmbeddingError(f"Vector dim mismatch: got {len(vec)}, expected {expected_dims}")
    # End-if
    vals = []
    for x in vec:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)):
            raise EmbeddingError("Vector contains non-finite or non-numeric element")
        # End-if
        vals.append(float(x))
    # End-for
    norm = math.sqrt(sum(v * v for v in vals))
    if norm <= 1e-12:
        raise EmbeddingError("Vector has zero L2 norm")
    # End-if
    return [v / norm for v in vals]
# End-def

##
 # @brief Execute HTTP POST via injectable transport or requests.request.
 #
def _http_post(url, headers, payload, timeout_s, transport=None):
    try:
        if transport is not None:
            status, body = transport("POST", url, headers, payload, timeout_s)
        else:
            resp = requests.request("POST", url, headers=headers, json=payload, timeout=timeout_s)
            status, body = resp.status_code, resp.text
        # End-if
    except Exception as exc:
        raise EmbeddingError(f"Transport error: {exc}") from exc
    # End-try
    if int(status) != 200:
        raise EmbeddingError(f"HTTP {status}: {str(body)[:200]}")
    # End-if
    try:
        return body if isinstance(body, dict) else json.loads(body)
    except Exception as exc:
        raise EmbeddingError(f"Invalid JSON response: {exc}") from exc
    # End-try
# End-def

##
 # @brief Base interface for text embedding providers.
 #
class EmbeddingProvider:
    def __init__(self, provider_name="disabled", model="", dimensions=None, base_url="", timeout_ms=1500, batch_size=32):
        self.provider_name, self.model = provider_name, model
        self.dimensions = int(dimensions) if dimensions else None
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_s = max(0.1, float(timeout_ms or 1500) / 1000.0)
        self.batch_size = max(1, int(batch_size or 32))
    # End-def
    def is_enabled(self): return self.provider_name != "disabled"
    def profile_key(self): return f"{self.provider_name}|{self.model}|{self.dimensions or 0}|{self.base_url}"
    def embed_query(self, text): raise EmbeddingError("Embedding provider is disabled")
    def embed_documents(self, texts): raise EmbeddingError("Embedding provider is disabled")
# End-class

##
 # @brief No-op provider used when vector memory is disabled.
 #
class DisabledEmbeddingProvider(EmbeddingProvider):
    pass
# End-class

##
 # @brief Deterministic cross-lingual mock provider for offline tests.
 #
class MockEmbeddingProvider(EmbeddingProvider):
    _CLUSTERS = (
        ("gym", "workout", "fitness", "bench", "squat", "exercise", "\u5367\u63a8", "\u5065\u8eab", "\u6df1\u8e72", "\u953b\u70bc"),
        ("spicy", "rabbit", "sichuan", "chili", "food", "cuisine", "\u624b\u6495\u5154", "\u5ddd\u83dc", "\u9ebb\u8fa3", "\u7f8e\u98df"),
        ("ota", "sdio", "esp32", "stm32", "firmware", "partition", "\u56fa\u4ef6", "\u5206\u533a", "\u4e32\u53e3"),
        ("doxygen", "python", "style", "ascii", "comment", "\u4ee3\u7801\u98ce\u683c", "\u6ce8\u91ca"),
    )
    def __init__(self, model="mock-semantic-v1", dimensions=8, fail_mode=None, **kwargs):
        super().__init__("mock", model=model or "mock-semantic-v1", dimensions=dimensions or 8, **kwargs)
        self.fail_mode, self.call_log = fail_mode, []
    # End-def
    def _encode_one(self, text):
        if self.fail_mode: raise EmbeddingError(f"Mock failure: {self.fail_mode}")
        low, dims = str(text or "").lower(), self.dimensions or 8
        vec = [1.0 if i < len(self._CLUSTERS) and any(w in low for w in self._CLUSTERS[i]) else 0.0 for i in range(dims)]
        if not any(vec):
            digest = hashlib.sha256(low.encode("utf-8")).digest()
            for i in range(len(self._CLUSTERS) if dims > len(self._CLUSTERS) else 0, dims):
                vec[i] = (digest[i % len(digest)] / 255.0) - 0.5
            # End-for
        # End-if
        if all(abs(x) <= 1e-9 for x in vec): vec[-1] = 1.0
        return validate_and_normalize_vector(vec, dims)
    # End-def
    def embed_query(self, text):
        self.call_log.append(("query", str(text)))
        return self._encode_one(text)
    # End-def
    def embed_documents(self, texts):
        self.call_log.append(("documents", list(texts)))
        return [self._encode_one(t) for t in texts]
    # End-def
# End-class

##
 # @brief Google Gemini REST embedding provider (:embedContent & :batchEmbedContents).
 #
class GeminiEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model, api_key, dimensions=None, base_url="", timeout_ms=1500, batch_size=32, transport=None):
        url = base_url or "https://generativelanguage.googleapis.com/v1beta"
        super().__init__("gemini", model=model, dimensions=dimensions, base_url=url, timeout_ms=timeout_ms, batch_size=batch_size)
        self.api_key, self.transport = api_key, transport
        self.model_path = model if str(model).startswith("models/") else f"models/{model}"
    # End-def
    def _req(self, text, task_type):
        return {"model": self.model_path, "content": {"parts": [{"text": str(text)}]}, "taskType": task_type,
                **({"outputDimensionality": self.dimensions} if self.dimensions else {})}
    # End-def
    def embed_query(self, text):
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        data = _http_post(f"{self.base_url}/{self.model_path}:embedContent", headers, self._req(text, "RETRIEVAL_QUERY"), self.timeout_s, self.transport)
        return validate_and_normalize_vector(((data.get("embedding") or {}).get("values") if isinstance(data, dict) else None), self.dimensions)
    # End-def
    def embed_documents(self, texts):
        headers, out = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}, []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            data = _http_post(f"{self.base_url}/{self.model_path}:batchEmbedContents", headers,
                              {"requests": [self._req(t, "RETRIEVAL_DOCUMENT") for t in batch]}, self.timeout_s, self.transport)
            embs = data.get("embeddings") if isinstance(data, dict) else None
            if not isinstance(embs, list) or len(embs) != len(batch): raise EmbeddingError("Gemini batch count mismatch")
            out.extend(validate_and_normalize_vector((item or {}).get("values"), self.dimensions) for item in embs)
        # End-for
        return out
    # End-def
# End-class

##
 # @brief OpenAI-compatible /embeddings REST provider.
 #
class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model, api_key, base_url, dimensions=None, timeout_ms=1500, batch_size=32, transport=None):
        super().__init__("openai_compatible", model=model, dimensions=dimensions, base_url=base_url, timeout_ms=timeout_ms, batch_size=batch_size)
        self.api_key, self.transport = api_key, transport
        self.endpoint = self.base_url if self.base_url.endswith("/embeddings") else f"{self.base_url}/embeddings"
    # End-def
    def embed_query(self, text): return self.embed_documents([str(text)])[0]
    def embed_documents(self, texts):
        headers, out = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, []
        for i in range(0, len(texts), self.batch_size):
            batch = [str(t) for t in texts[i:i + self.batch_size]]
            payload = {"model": self.model, "input": batch, **({"dimensions": self.dimensions} if self.dimensions else {})}
            data = _http_post(self.endpoint, headers, payload, self.timeout_s, self.transport)
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list) or len(items) != len(batch): raise EmbeddingError("OpenAI batch count mismatch")
            for item in sorted(items, key=lambda x: x.get("index", 0) if isinstance(x, dict) else 0):
                out.append(validate_and_normalize_vector((item or {}).get("embedding"), self.dimensions))
            # End-for
        # End-for
        return out
    # End-def
# End-class

##
 # @brief Factory constructing an EmbeddingProvider from parsed memory config.
 #
def create_embedding_provider(memory_cfg=None, *, transport=None):
    cfg = (memory_cfg or {}).get("embedding", memory_cfg or {}) if isinstance(memory_cfg, dict) else {}
    provider = str(cfg.get("provider", "disabled") or "disabled").strip().lower()
    model, dims = str(cfg.get("model", "") or "").strip(), cfg.get("dimensions")
    base_url, api_key = str(cfg.get("base_url", "") or "").strip(), str(cfg.get("api_key", "") or "").strip()
    t_ms, b_sz = cfg.get("timeout_ms", 1500), cfg.get("batch_size", 32)
    if provider == "mock":
        return MockEmbeddingProvider(model=model or "mock-semantic-v1", dimensions=dims or 8, timeout_ms=t_ms, batch_size=b_sz, fail_mode=cfg.get("fail_mode"))
    # End-if
    if provider == "gemini" and model and api_key:
        return GeminiEmbeddingProvider(model=model, api_key=api_key, dimensions=dims, base_url=base_url, timeout_ms=t_ms, batch_size=b_sz, transport=transport)
    # End-if
    if provider == "openai_compatible" and model and api_key and base_url:
        return OpenAICompatibleEmbeddingProvider(model=model, api_key=api_key, base_url=base_url, dimensions=dims, timeout_ms=t_ms, batch_size=b_sz, transport=transport)
    # End-if
    return DisabledEmbeddingProvider()
# End-def

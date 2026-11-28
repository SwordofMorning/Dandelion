##
 # @file packages/dandelion-core/src/dandelion_core/utils/__init__.py
 # @date 2026/11/28
 #
 # @brief Shared Core utility exports.
 # Exposes configuration, native provider, logging, model registry,
 # LLM request, and embedding helpers.
 #

from .config import load_api_config, load_remote_config, load_devices_config
from .llm_provider import (
    Block,
    PartialStreamError,
    RequestCancelledError,
    Provider,
    create_provider,
    normalize_messages,
)
from .logging import SessionManager, SessionBackup
from .llm_request import SafeLLMClient, ModelRegistry, RegistryModelSpec
from .embedding import (
    EmbeddingError,
    validate_and_normalize_vector,
    EmbeddingProvider,
    DisabledEmbeddingProvider,
    MockEmbeddingProvider,
    GeminiEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    create_embedding_provider,
)

__all__ = [
    "load_api_config",
    "load_remote_config",
    "load_devices_config",
    "Block",
    "PartialStreamError",
    "RequestCancelledError",
    "Provider",
    "create_provider",
    "normalize_messages",
    "SessionManager",
    "SessionBackup",
    "ModelRegistry",
    "RegistryModelSpec",
    "SafeLLMClient",
    "EmbeddingError",
    "validate_and_normalize_vector",
    "EmbeddingProvider",
    "DisabledEmbeddingProvider",
    "MockEmbeddingProvider",
    "GeminiEmbeddingProvider",
    "OpenAICompatibleEmbeddingProvider",
    "create_embedding_provider",
]

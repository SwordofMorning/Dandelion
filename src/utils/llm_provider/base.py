# src/utils/llm_provider/base.py

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Abstract base class for LLM Providers."""

    @abstractmethod
    def __init__(self, api_key, base_url, model_id):
        pass

    @abstractmethod
    def safe_request(self, payload):
        """Non-streaming request."""
        pass

    @abstractmethod
    def safe_stream_request(self, payload):
        """Streaming request."""
        pass

    @abstractmethod
    def extract_text(self, content):
        """Extract plain text from response blocks."""
        pass
##
 # @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/__init__.py
 # @date 2026/11/28
 #
 # @brief LLM provider package backed by the native C++17 / nanobind `_native` extension.
 # Speaks Anthropic Messages, OpenAI Chat Completions, OpenAI Responses, Gemini
 # generateContent, and Gemini Interactions directly over HTTPS without vendor SDKs.
 #

from .blocks import Block, PartialStreamError, RequestCancelledError
from .effort import DEFAULT_EFFORT, FLAVOR_AUTO

try:
    from ._native import Provider, normalize_messages, set_fallback_ca_bundle
except ImportError as exc:
    raise ImportError(
        "Native LLM provider is not built. Install the precompiled 'dandelion-core' wheel "
        "or run 'pip install --no-build-isolation --no-deps -e packages/dandelion-core' "
        "(needs a C++17 compiler, CMake, Ninja, and libcurl on Linux/macOS)."
    ) from exc
# End-try

try:
    import certifi
    set_fallback_ca_bundle(certifi.where())
except Exception:
    pass
# End-try

__all__ = [
    "Block",
    "PartialStreamError",
    "RequestCancelledError",
    "Provider",
    "create_provider",
    "normalize_messages",
    "set_fallback_ca_bundle",
]


##
 # @brief Create a provider for one configured model.
 #
 # @param sdk_type Protocol: "Message" (Anthropic), "Chat Completions" (OpenAI),
 #        "Responses" (OpenAI), "Generate Content" (Gemini), or "Interactions" (Gemini).
 # @param api_key API key for the provider.
 # @param base_url Base URL; empty uses the vendor default.
 # @param model_id Model identifier.
 # @param thinking "enabled" or "disabled".
 # @param effort Abstract effort level ("low"/"medium"/"high"/"xhigh"/"max"), see effort.py.
 # @param compat Endpoint flavor for cross-protocol relays, see effort.py.
 #
 # @return Native Provider; raises ValueError for an unsupported SDK_TYPE.
 #
def create_provider(
    sdk_type,
    api_key,
    base_url,
    model_id,
    thinking="disabled",
    effort=DEFAULT_EFFORT,
    compat=FLAVOR_AUTO,
):
    return Provider(
        str(sdk_type),
        api_key or "",
        base_url or "",
        str(model_id),
        str(thinking),
        str(effort),
        str(compat or FLAVOR_AUTO),
    )
# End-def

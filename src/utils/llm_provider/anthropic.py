##
 # @file src/utils/llm_provider/anthropic.py
 # @date 2026/08/05
 # 
 # @brief Anthropic API Implement.
 #

import inspect

from .base import LLMProvider

# @note Effort vocabulary and every family mapping table live in effort.py, so
# this provider and the OpenAI provider translate the same abstract level the
# same way. EFFORT_TO_BUDGET_TOKENS / DEFAULT_EFFORT stay imported here for
# backward compatibility with existing callers.
from .effort import (
    DEFAULT_EFFORT,
    VALID_FLAVORS,
    FLAVOR_AUTO,
    FLAVOR_ANTHROPIC,
    FLAVOR_ANTHROPIC_EFFORT,
    FLAVOR_DEEPSEEK,
    FLAVOR_OPENAI,
    FLAVOR_GEMINI,
    EFFORT_TO_BUDGET_TOKENS,
    EFFORT_TO_ANTHROPIC_EFFORT,
    EFFORT_TO_DEEPSEEK_EFFORT,
    EFFORT_TO_OPENAI_EFFORT,
    EFFORT_TO_GEMINI_LEVEL,
    resolve_effort,
)

# Payload keys written by _inject_thinking that are ABSENT from the Anthropic SDK
# typed signature. They must leave through extra_body (see _sdk_kwargs): expanding
# them as **payload would raise TypeError before any HTTP request is sent, since
# Messages.create() accepts no **kwargs.
COMPAT_KEYS = ("reasoning_effort", "thinking_level", "thinkingLevel")

# Every key this provider owns inside the payload. They are cleared before each
# injection so a reused payload can never leak a previous flavor's values into a
# new request (the router retries and falls back with payload copies).
# @note "thinking" is owned as well: this provider is its only writer (callers
# build payloads from tools / messages / max_tokens / system only), so a stale
# thinking block left by another flavor is removed whenever the switch is off.
OWNED_KEYS = ("thinking", "output_config") + COMPAT_KEYS

# Flavor fallback hints, used only when MODEL_LIST does not declare "compat".
# @note Two passes, most specific first: the model id names the real family (a
# Claude model served by an Antigravity host stays anthropic), while the host name
# is a weaker signal about the family a relay mainly serves (Sub2API -> OpenAI).
# Order inside each tuple matters: the first hit wins.
_MODEL_ID_HINTS = (
    (("deepseek",), FLAVOR_DEEPSEEK),
    (("gemini",), FLAVOR_GEMINI),
    (("gpt", "codex"), FLAVOR_OPENAI),
    (("claude", "opus", "sonnet", "haiku"), FLAVOR_ANTHROPIC),
)
_BASE_URL_HINTS = (
    (("deepseek",), FLAVOR_DEEPSEEK),
    (("gemini", "antigravity", "generativelanguage"), FLAVOR_GEMINI),
    (("sub2api", "openai"), FLAVOR_OPENAI),
)

# Cache of SDK-accepted keyword names, keyed by the messages.* entry point used.
_SDK_PARAM_CACHE = {}

# The SDK refuses NON-STREAMING requests whose max_tokens implies more than ten
# minutes of generation (see _base_client._calculate_nonstreaming_timeout:
# expected = 3600 * max_tokens / 128000 must stay <= 600), i.e. it raises for
# max_tokens > 21333 unless the caller passes an explicit timeout. Callers that
# legitimately need a larger cap (the compaction summary shares max_tokens with
# the thinking budget) are served through the streaming API instead, which the
# SDK recommends for long requests; the stream is drained silently so a
# non-streaming caller still gets the final message.
SDK_NONSTREAMING_MAX_TOKENS = 21333

##
 # @brief Anthropic API Class.
 #
class AnthropicProvider(LLMProvider):
    ##
     # @brief Constructor.
     # 
     # @param api_key API key for the provider.
     # @param base_url Custom base URL (optional).
     # @param model_id Model identifier.
     # @param thinking "enabled" or "disabled" - whether to enable extended thinking.
     # @param effort Reasoning effort level: "low", "medium", "high", or "max".
     # @param compat Endpoint flavor: "auto", "anthropic", "anthropic-effort",
     #        "deepseek", "openai" or "gemini" (see effort.py). "auto" detects the
     #        family from base_url / model_id keywords.
     #
    def __init__(self, api_key, base_url, model_id, thinking="disabled",
                 effort=DEFAULT_EFFORT, compat=FLAVOR_AUTO):
        # Dynamic import.
        from anthropic import Anthropic

        # Construct request client (header).
        self.client = Anthropic(
            api_key=api_key,
            base_url=base_url if base_url else None,
            default_headers={
                "HTTP-Referer": "https://github.com/SwordofMorning/Dandelion",
                "X-Title": "Dandelion"
            }
        )
        self.model_id = model_id
        self.thinking = thinking
        self.effort = effort
        self.base_url = base_url or ""

        # @note Detect DeepSeek by base_url or model_id (case-insensitive). Kept for
        # backward compatibility; flavor resolution below supersedes it.
        self._is_deepseek = (
            "deepseek" in self.base_url.lower()
            or "deepseek" in model_id.lower()
        )

        # Resolve the link-terminal family that decides which reasoning knob is used.
        self.flavor = self._detect_flavor(str(compat or FLAVOR_AUTO).strip().lower())
    # End-def

    ##
     # @brief Resolve the endpoint flavor (link terminal model family).
     #
     # @param compat Value from the model metadata ("auto" when unset).
     #
     # @return One of VALID_FLAVORS plus the resolved "auto" result.
     #
     # @note Explicit metadata always wins; keyword detection is only a fallback so
     # that an unconfigured model never silently loses its reasoning knob. The model
     # id is consulted before the host name, and the final fallback is "anthropic",
     # the most conservative branch (its payload is byte-identical to the
     # pre-flavor behavior).
     #
    def _detect_flavor(self, compat):
        # 1. Explicit metadata wins.
        if compat in VALID_FLAVORS:
            return compat
        # End-if

        model_id = self.model_id.lower()
        base_url = self.base_url.lower()

        # 2. Model id pass: it names the real terminal family.
        for keywords, flavor in _MODEL_ID_HINTS:
            if any(k in model_id for k in keywords):
                self._warn_auto_flavor(flavor)
                return flavor
            # End-if
        # End-for

        # 3. Host pass: a relay host still reveals the family it mainly serves.
        for keywords, flavor in _BASE_URL_HINTS:
            if any(k in base_url for k in keywords):
                self._warn_auto_flavor(flavor)
                return flavor
            # End-if
        # End-for

        # 4. Conservative default (identical payload to the pre-flavor behavior).
        return FLAVOR_ANTHROPIC
    # End-def

    ##
     # @brief Warn once when a flavor was guessed from keywords.
     #
     # @param flavor Detected flavor.
     #
     # @note Only the new bridge flavors warn: a keyword-detected DeepSeek already
     # behaved this way before, so warning there would only add noise.
     #
    def _warn_auto_flavor(self, flavor):
        if flavor in (FLAVOR_OPENAI, FLAVOR_GEMINI):
            print(f"[!] Model '{self.model_id}': assuming compat flavor "
                  f"'{flavor}' from base_url/model keywords. Set \"compat\" in "
                  f"MODEL_LIST to make it explicit.")
        # End-if
    # End-def

    ##
     # @brief Inject thinking configuration into payload.
     #
     # @param payload Request payload to be mutated in place (the payload sent to the LLM).
     #
     # @note Written keys depend on the endpoint flavor (see effort.py):
     # - Anthropic: {"thinking": {"type": "enabled", "budget_tokens": N}}
     # - DeepSeek (Anthropic-compatible): {"output_config": {"effort": "low"|"medium"|"high"|"max"}}
     #   DeepSeek ignores budget_tokens; effort is the primary knob.
     # - OpenAI bridge (Sub2API -> GPT / Codex): thinking + "reasoning_effort"
     #   + {"output_config": {"effort": ...}}.
     # - Gemini bridge (Sub2API -> Antigravity -> Gemini 3): thinking
     #   + "reasoning_effort" + "thinking_level" + "thinkingLevel".
     # - anthropic-effort (opt-in): thinking + {"output_config": {"effort": ...}}.
     #
     # @note The effort keys are not gated by the thinking switch, because a GPT or
     # Gemini upstream keeps reasoning even when the thinking block is disabled.
     # When the switch is off, any pre-existing "thinking" key is removed instead
     # (this provider is its only writer, see OWNED_KEYS). The keys absent from the
     # SDK signature leave through extra_body, see _sdk_kwargs().
     # 
    def _inject_thinking(self, payload):
        # 0. Idempotency: drop every key this provider owns (including "thinking"),
        #    so a reused payload can never carry a previous flavor's values, nor a
        #    stale thinking block, into the current request.
        for key in OWNED_KEYS:
            payload.pop(key, None)
        # End-for

        flavor = self.flavor

        # 1. Native thinking block. Bridges may also read it as a budget hint even
        #    when they ultimately use reasoning_effort / thinking_level. DeepSeek
        #    keeps the legacy behavior of receiving output_config only.
        if self.thinking == "enabled" and flavor != FLAVOR_DEEPSEEK:
            budget = resolve_effort(EFFORT_TO_BUDGET_TOKENS, self.effort, self.model_id)

            # @note max_tokens covers thinking + answer, and the API requires
            # budget_tokens < max_tokens. A caller whose cap is not larger than the
            # thinking budget (a small summarization call, for instance) would
            # otherwise surface this only as a provider 400, so warn here.
            cap = payload.get("max_tokens")
            if isinstance(cap, int) and 0 < cap <= budget:
                print(f"[-] Warning: max_tokens={cap} <= thinking budget_tokens="
                      f"{budget} ({self.model_id}); the provider may reject this "
                      f"request. Raise max_tokens or lower the effort level.")
            # End-if

            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": budget
            }
        # End-if

        # 2. Family reasoning knob. Every value derives from self.effort (default
        #    "medium"); no level is hard-coded here. These keys are intentionally NOT
        #    gated by the thinking switch: a GPT / Gemini upstream keeps reasoning
        #    even when the Anthropic thinking block is off, so gating them would
        #    silently disable the effort knob.
        if flavor == FLAVOR_DEEPSEEK:
            # @note DeepSeek uses output_config.effort (like OpenAI's reasoning_effort).
            payload["output_config"] = {
                "effort": resolve_effort(EFFORT_TO_DEEPSEEK_EFFORT, self.effort, self.model_id)
            }
        elif flavor == FLAVOR_OPENAI:
            # Sub2API bridging to OpenAI / Codex reads both spellings.
            level = resolve_effort(EFFORT_TO_OPENAI_EFFORT, self.effort, self.model_id)
            payload["reasoning_effort"] = level
            payload["output_config"] = {"effort": level}
        elif flavor == FLAVOR_GEMINI:
            # Dual spelling: snake_case for REST-style adapters, camelCase for SDK
            # style adapters (Gemini 3 thinkingConfig.thinkingLevel).
            level = resolve_effort(EFFORT_TO_GEMINI_LEVEL, self.effort, self.model_id)
            payload["reasoning_effort"] = level
            payload["thinking_level"] = level
            payload["thinkingLevel"] = level
            payload["output_config"] = {"effort": level}
        elif flavor == FLAVOR_ANTHROPIC_EFFORT:
            # Opt-in: native Claude that supports output_config.effort.
            payload["output_config"] = {
                "effort": resolve_effort(EFFORT_TO_ANTHROPIC_EFFORT, self.effort, self.model_id)
            }
        # End-if
        # FLAVOR_ANTHROPIC writes no extra key: byte-identical to the pre-flavor behavior.
    # End-def

    ##
     # @brief Return the keyword names accepted by messages.create / messages.stream.
     #
     # @param method "create" or "stream" - the SDK entry point that will be called.
     #
     # @return Set of accepted keyword names (without "self" and without **kwargs).
     #
     # @note Read from the installed SDK signature instead of a hard-coded list, so a
     # newer SDK exposes new typed parameters automatically while anything unknown is
     # still routed through extra_body by _sdk_kwargs.
     #
    def _sdk_param_names(self, method):
        if method not in _SDK_PARAM_CACHE:
            from anthropic.resources.messages import Messages

            fn = getattr(Messages, "stream" if method == "stream" else "create")
            _SDK_PARAM_CACHE[method] = {
                name for name, param in inspect.signature(fn).parameters.items()
                if name != "self" and param.kind is not inspect.Parameter.VAR_KEYWORD
            }
        # End-if
        return _SDK_PARAM_CACHE[method]
    # End-def

    ##
     # @brief Split a payload into SDK kwargs plus extra_body contents.
     #
     # @param payload Request payload (after _inject_thinking).
     # @param method "create" or "stream" - which SDK entry point will be called.
     #
     # @return Tuple-free dict of keywords ready for `messages.<method>(**kwargs)`,
     #         where every key the SDK does not declare (COMPAT_KEYS) travels inside
     #         extra_body and therefore still lands at the top level of the JSON body.
     #
    def _sdk_kwargs(self, payload, method="create"):
        allowed = self._sdk_param_names(method)

        kwargs = {}
        extra = {}
        for key, value in payload.items():
            if key in allowed:
                kwargs[key] = value
            else:
                extra[key] = value
            # End-if
        # End-for

        if extra:
            kwargs["extra_body"] = extra
        # End-if

        return kwargs
    # End-def

    ##
     # @brief Report the two failure modes that used to be swallowed silently.
     #
     # @param err Exception raised by the SDK call.
     #
    def _report_request_error(self, err):
        msg = str(err)
        if isinstance(err, TypeError) and "unexpected keyword argument" in msg:
            print(f"[!] {self.model_id}: a payload key escaped the SDK keyword "
                  f"whitelist ({msg}). Check COMPAT_KEYS and _sdk_param_names().")
        elif "Extra inputs are not permitted" in msg:
            print(f"[!] {self.model_id}: the endpoint rejected an extra top-level "
                  f"field. Check the detected flavor (current: '{self.flavor}').")
        # End-if
    # End-def

    ##
     # @brief Non-streaming request.
     #
     # @param payload data send to LLM.
     # @param logger logger object, save log to file.
     # @param log_tag log tag saved in file.
     #
     # @return LLM's response and error.
     #
    def safe_request(self, payload, logger=None, log_tag=""):
        # 1. Patch payload: set model id and inject thinking.
        payload["model"] = self.model_id
        self._inject_thinking(payload)

        # 2. Save log.
        if logger and log_tag:
            logger.log_api_call(log_tag, payload)
        # End-if

        # 3. Request.
        try:
            cap = payload.get("max_tokens")
            if isinstance(cap, int) and cap > SDK_NONSTREAMING_MAX_TOKENS:
                # @note Large caps are rejected by the SDK on the non-streaming
                # path, so run them through the streaming API and drain the stream
                # without printing: the caller still receives the final message.
                # The "stream" entry point accepts a different keyword set than
                # "create", so the split is asked for explicitly.
                with self.client.messages.stream(**self._sdk_kwargs(payload, "stream")) as stream:
                    for _ in stream:
                        pass
                    # End-for
                # End-with
                return stream.get_final_message(), None
            # End-if

            resp = self.client.messages.create(**self._sdk_kwargs(payload, "create"))
            return resp, None
        except Exception as e:
            self._report_request_error(e)
            return None, str(e)
        # End-try
    # End-def

    ##
     # @brief Streaming request.
     #
     # @param payload data send to LLM.
     # @param logger logger object, save log to file.
     # @param log_tag log tag saved in file.
     #
     # @return LLM's response and error.
     #
    def safe_stream_request(self, payload, logger=None, log_tag=""):
        # 1. Patch payload: set model id and inject thinking.
        payload["model"] = self.model_id
        self._inject_thinking(payload)

        # 2. Save log.
        if logger and log_tag:
            logger.log_api_call(log_tag, payload)

        # 3. Request.
        try:
            print("\n[Agent] ", end="", flush=True)
            with self.client.messages.stream(**self._sdk_kwargs(payload, "stream")) as stream:
                for event in stream:
                    # Print streaming string for user check in terminal.
                    if event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            # Print normal text.
                            print(event.delta.text, end="", flush=True)
                        elif event.delta.type == "input_json_delta":
                            # Print tool arguments in dark gray to show streaming progress.
                            print(f"\033[90m{event.delta.partial_json}\033[0m", end="", flush=True)
                        # End-if text_delta
                    # End-if content_block_delta
                # End-for streaming
            print()

            # Get final (full) message and return.
            final_message = stream.get_final_message()
            return final_message, None
        except Exception as e:
            print()
            self._report_request_error(e)
            return None, str(e)
        # End-try
    # End-def

    ##
     # @brief Extract plain text from response blocks.
     #
    def extract_text(self, content):
        if not isinstance(content, list):
            return str(content)
        return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")
    # End-def
# End-class
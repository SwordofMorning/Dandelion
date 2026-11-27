/**
 * @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/native/api_messages.cpp
 * @author Xiaojintao
 * @brief Anthropic Messages API codec (POST {base}/v1/messages) with Sub2API cross-protocol bridge.
 * @version 0.1
 * @date 2026-11-28
 * @copyright Copyright (c) 2026
 */

#include "provider.hpp"

#include <map>
#include <set>
#include <string>
#include <vector>

namespace dandelion
{
namespace
{

/**
 * @brief Payload keys owned by this codec; cleared before each injection for idempotent retries.
 */
const char* const kOwnedKeys[] = {"thinking", "output_config", "reasoning_effort", "thinking_level", "thinkingLevel"};

/**
 * @brief Non-streaming token ceiling above which requests are routed through SSE streaming.
 */
constexpr long long kNonStreamingMaxTokens = 21333;

/**
 * @brief Return true for Anthropic tool-invocation content block types.
 */
bool IsToolLike(const std::string& type)
{
    return type == "tool_use" || type == "server_tool_use" || type == "mcp_tool_use";
}

/**
 * @brief Strip foreign protocol blocks, enforce non-empty content, and satisfy DeepSeek thinking replay rules.
 */
nb::object StripForeign(nb::object messages, const std::string& flavor, const std::string& thinking)
{
    if (!IsList(messages))
    {
        return messages;
    }
    nb::list out;
    for (nb::handle msg : messages)
    {
        nb::object content = Field(msg, "content");
        if (!IsList(content))
        {
            out.append(msg);
            continue;
        }
        const bool is_assistant = (TextOf(Field(msg, "role")) == "assistant");
        bool changed = false;
        bool has_thinking = false;
        bool has_tool_use = false;
        nb::list blocks;

        for (nb::handle block : content)
        {
            const std::string type = TextOf(Field(block, "type"));
            if (type == "gemini_thought" || type == "gemini_part" || type == "openai_reasoning")
            {
                changed = true;
                continue;
            }
            if (type == "thinking")
            {
                std::string sig = TextOf(Field(block, "signature"));
                std::string src = TextOf(Field(block, "reasoning_source"));
                if (flavor == "deepseek")
                {
                    has_thinking = true;
                    if (IsDict(block) && DictHas(block, "reasoning_source"))
                    {
                        nb::dict copy = CopyDict(block);
                        DictDelIfPresent(copy, "reasoning_source");
                        blocks.append(copy);
                        changed = true;
                    }
                    else
                    {
                        blocks.append(block);
                    }
                    continue;
                }
                if (sig.empty() || src == "openai")
                {
                    changed = true;
                    continue;
                }
                has_thinking = true;
                blocks.append(block);
                continue;
            }
            if (type == "text")
            {
                if (TextOf(Field(block, "text")).empty())
                {
                    // Drop empty text blocks (including signed empty Gemini text parts after stripping).
                    changed = true;
                    continue;
                }
            }
            if (IsToolLike(type))
            {
                has_tool_use = true;
            }
            if (IsDict(block) && (DictHas(block, "thought_signature") || DictHas(block, "gemini_function_call")))
            {
                nb::dict copy = CopyDict(block);
                DictDelIfPresent(copy, "thought_signature");
                DictDelIfPresent(copy, "gemini_function_call");
                blocks.append(copy);
                changed = true;
                continue;
            }
            blocks.append(block);
        }

        // DeepSeek /anthropic thinking mode requires a thinking block on every assistant tool_use turn.
        if (is_assistant && flavor == "deepseek" && thinking == "enabled" && has_tool_use && !has_thinking)
        {
            nb::dict empty_think;
            empty_think["type"] = "thinking";
            empty_think["thinking"] = "";
            blocks.insert(0, empty_think);
            changed = true;
        }

        // Unconditionally guard against content: [] (e.g. after max_tokens dropped all tool_use blocks).
        if (nb::len(blocks) == 0)
        {
            nb::dict placeholder;
            placeholder["type"] = "text";
            placeholder["text"] = "[reasoning omitted]";
            blocks.append(placeholder);
            changed = true;
        }

        if (!changed)
        {
            out.append(msg);
            continue;
        }
        nb::dict copy = CopyDict(msg);
        copy["content"] = blocks;
        out.append(copy);
    }
    return out;
}

/**
 * @brief Validate and append one Messages content block, dropping unfinished tools on max_tokens.
 */
void AppendMessageBlock(nb::list& content, nb::object block, const std::string& stop)
{
    std::string type = TextOf(Field(block, "type"));
    if (stop == "max_tokens" && IsToolLike(type))
    {
        return;
    }
    if (type == "tool_use")
    {
        if (TextOf(Field(block, "id")).empty() || TextOf(Field(block, "name")).empty() || !IsDict(Field(block, "input")))
        {
            throw ProtocolError("Incomplete Messages tool call.");
        }
    }
    content.append(block);
}

/**
 * @brief Parse a non-streaming Messages JSON body into a canonical response object.
 */
nb::object ParseMessage(nb::object msg)
{
    if (TextOf(Field(msg, "type")) == "error" || !Field(msg, "error").is_none())
    {
        throw ProtocolError("Error: " + JsonDumps(msg));
    }
    nb::list content;
    const std::string stop = TextOf(Field(msg, "stop_reason"));
    if (stop.empty())
    {
        throw ProtocolError("Messages response ended without stop_reason.");
    }
    nb::object raw = Field(msg, "content");
    if (IsList(raw))
    {
        for (nb::handle block : raw)
        {
            AppendMessageBlock(content, MakeBlock(CopyDict(block)), stop);
        }
    }
    nb::object usage = Field(msg, "usage");
    return MakeResponse(content, stop, usage.is_none() ? nb::object(nb::dict()) : usage);
}

/**
 * @brief Incremental SSE decoder for Anthropic Messages streams.
 *
 * Defers tool_use JSON parsing to Finish() so max_tokens truncations drop partial tool calls
 * cleanly instead of throwing JSONDecodeError inside content_block_stop.
 */
class MessagesStream : public StreamDecoder
{
public:
    using StreamDecoder::StreamDecoder;

    /**
     * @brief Process one Messages SSE event.
     */
    void OnEvent(const SseEvent& ev) override
    {
        if (ev.data.empty())
        {
            return;
        }
        nb::object d = JsonLoads(ev.data);
        std::string type = TextOf(Field(d, "type"));
        if (type.empty())
        {
            type = ev.event;
        }
        if (type == "error" || ev.event == "error" || !Field(d, "error").is_none())
        {
            throw ProtocolError("Stream error: " + ev.data);
        }
        if (type == "message_start")
        {
            m_started = true;
            nb::object usage = Field(Field(d, "message"), "usage");
            m_usage = IsDict(usage) ? CopyDict(usage) : nb::dict();
        }
        else if (type == "content_block_start")
        {
            size_t idx = ReadIndex(d);
            if (m_blocks.count(idx))
            {
                throw ProtocolError("Duplicate Messages block index.");
            }
            m_open.insert(idx);
            nb::dict raw_block = CopyDict(Field(d, "content_block"));
            nb::object block = MakeBlock(raw_block);
            std::string btype = TextOf(Field(block, "type"));
            if (btype == "text")
            {
                m_text_buf[idx] = TextOf(Field(block, "text"));
            }
            else if (btype == "thinking")
            {
                m_thinking_buf[idx] = TextOf(Field(block, "thinking"));
                m_signature_buf[idx] = TextOf(Field(block, "signature"));
            }
            m_blocks[idx] = block;
        }
        else if (type == "content_block_delta")
        {
            size_t idx = ReadIndex(d);
            if (!m_open.count(idx))
            {
                throw ProtocolError("Messages block is not open.");
            }
            nb::object block = m_blocks.at(idx);
            nb::object delta = Field(d, "delta");
            std::string dt = TextOf(Field(delta, "type"));
            if (dt == "text_delta")
            {
                std::string t = TextOf(Field(delta, "text"));
                m_text_buf[idx] += t;
                Emit(t);
            }
            else if (dt == "input_json_delta")
            {
                std::string part = TextOf(Field(delta, "partial_json"));
                m_json_buf[idx] += part;
                Emit(part, true);
            }
            else if (dt == "thinking_delta")
            {
                m_thinking_buf[idx] += TextOf(Field(delta, "thinking"));
            }
            else if (dt == "signature_delta")
            {
                m_signature_buf[idx] += TextOf(Field(delta, "signature"));
            }
            else if (dt == "citations_delta")
            {
                nb::object citations = Field(block, "citations");
                if (!IsList(citations))
                {
                    citations = nb::list();
                    block["citations"] = citations;
                }
                citations.attr("append")(Field(delta, "citation"));
            }
        }
        else if (type == "content_block_stop")
        {
            size_t idx = ReadIndex(d);
            if (!m_open.erase(idx))
            {
                throw ProtocolError("Messages block is not open.");
            }
        }
        else if (type == "message_stop")
        {
            m_stopped = true;
        }
        else if (type == "message_delta")
        {
            nb::object delta = Field(d, "delta");
            if (!Field(delta, "stop_reason").is_none())
            {
                std::string sr = TextOf(Field(delta, "stop_reason"));
                if (!sr.empty())
                {
                    m_stop_reason = sr;
                }
            }
            nb::object usage = Field(d, "usage");
            if (IsDict(usage))
            {
                for (auto kv : nb::borrow<nb::dict>(usage))
                {
                    if (!kv.second.is_none())
                    {
                        m_usage[kv.first] = kv.second;
                    }
                }
            }
        }
    }

    /**
     * @brief Validate stream boundaries, parse deferred tool inputs, and assemble the response.
     */
    nb::object Finish() override
    {
        if (!m_started || !m_stopped || m_stop_reason.empty() || (!m_open.empty() && m_stop_reason != "max_tokens"))
        {
            throw ProtocolError("Incomplete Messages stream (missing message or block boundary).");
        }
        nb::list content;
        for (auto& kv : m_blocks)
        {
            size_t idx = kv.first;
            nb::object block = kv.second;
            std::string btype = TextOf(Field(block, "type"));

            if (m_text_buf.count(idx))
            {
                block["text"] = SafeUtf8Str(m_text_buf[idx]);
            }
            if (m_thinking_buf.count(idx))
            {
                block["thinking"] = SafeUtf8Str(m_thinking_buf[idx]);
            }
            if (m_signature_buf.count(idx) && !m_signature_buf[idx].empty())
            {
                block["signature"] = SafeUtf8Str(m_signature_buf[idx]);
            }

            if (m_stop_reason == "max_tokens" && IsToolLike(btype))
            {
                continue;
            }
            if (IsToolLike(btype) && m_json_buf.count(idx) && !m_json_buf[idx].empty())
            {
                block["input"] = JsonLoads(m_json_buf[idx]);
            }
            AppendMessageBlock(content, block, m_stop_reason);
        }
        return MakeResponse(content, m_stop_reason, m_usage);
    }

private:
    /**
     * @brief Read and validate a non-negative Messages content block index.
     */
    size_t ReadIndex(nb::handle d) const
    {
        nb::object raw = Field(d, "index");
        if (!IsInt(raw))
        {
            throw ProtocolError("Missing Messages block index.");
        }
        long long v = nb::cast<long long>(raw);
        if (v < 0)
        {
            throw ProtocolError("Negative Messages block index.");
        }
        return static_cast<size_t>(v);
    }

    std::set<size_t> m_open;
    bool m_stopped = false;
    bool m_started = false;
    std::map<size_t, nb::object> m_blocks;
    std::map<size_t, std::string> m_text_buf;
    std::map<size_t, std::string> m_thinking_buf;
    std::map<size_t, std::string> m_signature_buf;
    std::map<size_t, std::string> m_json_buf;
    nb::dict m_usage;
    std::string m_stop_reason;
};

/**
 * @brief Anthropic Messages wire codec with Sub2API cross-protocol effort bridge injection.
 */
class MessagesCodec : public Codec
{
public:
    /**
     * @brief Construct codec and resolve terminal model flavor.
     */
    explicit MessagesCodec(const ProviderConfig& c)
        : Codec(c)
    {
        m_flavor = ResolveFlavor(c.compat, c.model_id, c.base_url, "anthropic");
    }

    /**
     * @brief Return the Messages protocol identifier ("anthropic").
     */
    const char* Protocol() const override
    {
        return "anthropic";
    }

    /**
     * @brief Return the resolved upstream flavor.
     */
    std::string Flavor() const override
    {
        return m_flavor;
    }

    /**
     * @brief Force streaming when max_tokens exceeds kNonStreamingMaxTokens (arbitrary-precision safe).
     */
    bool ForceStream(nb::dict payload) override
    {
        nb::object cap = payload.get("max_tokens", nb::none());
        return IsInt(cap) && (cap > nb::int_(kNonStreamingMaxTokens));
    }

    /**
     * @brief Build a Messages POST request on a copied body without mutating the caller payload.
     */
    Built Build(nb::dict payload, bool stream) override
    {
        nb::dict body = CopyDict(payload);
        body["model"] = m_cfg.model_id;
        InjectThinking(body);

        if (DictHas(body, "system") && Field(body, "system").is_none())
        {
            DictDelIfPresent(body, "system");
        }
        if (DictHas(body, "tools") && Field(body, "tools").is_none())
        {
            DictDelIfPresent(body, "tools");
        }
        if (DictHas(body, "messages"))
        {
            body["messages"] = StripForeign(body["messages"], m_flavor, m_cfg.thinking);
        }
        if (stream)
        {
            body["stream"] = true;
        }
        else
        {
            DictDelIfPresent(body, "stream");
        }

        std::string base = m_cfg.base_url.empty() ? EnvOr("ANTHROPIC_BASE_URL", "https://api.anthropic.com") : m_cfg.base_url;
        base = RstripSlash(base);

        Built b;
        if (EndsWith(base, "/messages"))
        {
            b.url = base;
        }
        else if (EndsWith(base, "/v1"))
        {
            b.url = base + "/messages";
        }
        else
        {
            b.url = base + "/v1/messages";
        }

        b.headers = {
            {"x-api-key", m_cfg.api_key},
            {"anthropic-version", "2023-06-01"},
            {"content-type", "application/json"},
            {"accept", stream ? "text/event-stream" : "application/json"},
            {"HTTP-Referer", "https://github.com/SwordofMorning/Dandelion"},
            {"X-Title", "Dandelion"},
        };
        if (m_flavor == "anthropic-effort" && ContainsAny(Lower(m_cfg.model_id), {"opus-4-5", "opus-4.5"}))
        {
            b.headers.push_back({"anthropic-beta", "effort-2025-11-24"});
        }
        b.body = body;
        return b;
    }

    /**
     * @brief Parse a non-streaming Messages JSON response body.
     */
    nb::object Parse(nb::object body) override
    {
        return ParseMessage(body);
    }

    /**
     * @brief Create a fresh MessagesStream SSE decoder.
     */
    std::unique_ptr<StreamDecoder> Decoder(bool echo) override
    {
        return std::make_unique<MessagesStream>(echo);
    }

private:
    /**
     * @brief Clear owned keys idempotently and inject flavor-appropriate thinking/effort knobs.
     */
    void InjectThinking(nb::dict body)
    {
        for (const char* key : kOwnedKeys)
        {
            DictDelIfPresent(body, key);
        }
        if (m_flavor == "none")
        {
            return;
        }

        if (m_cfg.thinking == "enabled" && m_flavor != "deepseek")
        {
            nb::object budget = ResolveEffort("EFFORT_TO_BUDGET_TOKENS", m_cfg.effort, m_cfg.model_id);
            nb::object cap = body.get("max_tokens", nb::none());
            if (IsInt(cap) && IsInt(budget) && (cap > nb::int_(0)) && (cap <= budget))
            {
                PrintLine("[-] Warning: max_tokens=" + TextOf(cap) + " <= thinking budget_tokens=" + TextOf(budget) + " (" + m_cfg.model_id +
                          "); the provider may reject this request. Raise max_tokens or lower the effort level.");
            }
            nb::dict thinking;
            thinking["type"] = "enabled";
            thinking["budget_tokens"] = budget;
            body["thinking"] = thinking;
        }
        else if (m_flavor == "deepseek" && m_cfg.thinking == "disabled")
        {
            nb::dict thinking;
            thinking["type"] = "disabled";
            body["thinking"] = thinking;
        }

        if (m_flavor == "deepseek")
        {
            nb::dict oc;
            oc["effort"] = ResolveEffort("EFFORT_TO_DEEPSEEK_EFFORT", m_cfg.effort, m_cfg.model_id);
            body["output_config"] = oc;
        }
        else if (m_flavor == "openai")
        {
            nb::object level = ResolveEffort("EFFORT_TO_OPENAI_EFFORT", m_cfg.effort, m_cfg.model_id);
            body["reasoning_effort"] = level;
            nb::dict oc;
            oc["effort"] = level;
            body["output_config"] = oc;
        }
        else if (m_flavor == "gemini")
        {
            std::string level = ResolveGeminiLevel(m_cfg.effort, m_cfg.model_id);
            body["reasoning_effort"] = level;
            body["thinking_level"] = level;
            body["thinkingLevel"] = level;
            nb::dict oc;
            oc["effort"] = level;
            body["output_config"] = oc;
        }
        else if (m_flavor == "anthropic-effort")
        {
            nb::dict oc;
            oc["effort"] = ResolveEffort("EFFORT_TO_ANTHROPIC_EFFORT", m_cfg.effort, m_cfg.model_id);
            body["output_config"] = oc;
        }
    }

    std::string m_flavor;
};

} // namespace

std::unique_ptr<Codec> MakeMessagesCodec(const ProviderConfig& cfg)
{
    return std::make_unique<MessagesCodec>(cfg);
}

} // namespace dandelion

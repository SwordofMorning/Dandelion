/**
 * @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/native/api_chat_completions.cpp
 * @author Xiaojintao
 * @brief OpenAI Chat Completions API codec (POST {base}/chat/completions) with DeepSeek reasoning_content & Sub2API compat.
 * @version 0.1
 * @date 2026-11-28
 * @copyright Copyright (c) 2026
 */

#include "provider.hpp"

#include <map>
#include <string>
#include <vector>

namespace dandelion
{
namespace
{

/**
 * @brief Payload keys owned by this codec's effort/thinking bridge; cleared before injection.
 */
const char* const kOwnedKeys[] = {"reasoning_effort", "thinking_level", "thinkingLevel", "thinking", "output_config"};

/**
 * @brief Accumulated tool call entry during streaming or non-streaming parsing.
 */
struct ChatToolCall
{
    std::string id;
    std::string name;
    std::string arguments;
};

/**
 * @brief Join non-empty string pieces with newline separators.
 */
std::string JoinLines(const std::vector<std::string>& parts)
{
    std::string out;
    for (size_t i = 0; i < parts.size(); ++i)
    {
        if (i > 0)
        {
            out += "\n";
        }
        out += parts[i];
    }
    return out;
}

/**
 * @brief Detect OpenAI reasoning models (o1/o3/o4/gpt-5) that reject temperature and require max_completion_tokens.
 */
bool IsOpenAIReasoningModel(const std::string& model_id)
{
    std::string m = Lower(Trim(model_id));
    size_t slash = m.rfind('/');
    if (slash != std::string::npos)
    {
        m = m.substr(slash + 1);
    }
    if (StartsWith(m, "gpt-5"))
    {
        return true;
    }
    for (const char* prefix : {"o1", "o3", "o4"})
    {
        if (m == prefix || StartsWith(m, std::string(prefix) + "-"))
        {
            return true;
        }
    }
    return false;
}

/**
 * @brief Extract reasoning_content (or OpenRouter/vLLM "reasoning" alias) from a message or delta object.
 */
std::string ExtractReasoningField(nb::handle obj)
{
    nb::object rc = Field(obj, "reasoning_content");
    if (!rc.is_none())
    {
        return TextOf(rc);
    }
    nb::object r = Field(obj, "reasoning");
    if (IsStr(r))
    {
        return TextOf(r);
    }
    return "";
}

/**
 * @brief Convert Chat Completions fields into canonical Anthropic-style blocks and response namespace.
 */
nb::object BuildChatResponse(const std::string& reasoning, const std::string& text, const std::vector<ChatToolCall>& calls, const std::string& finish, nb::object usage)
{
    nb::list content;
    if (!reasoning.empty())
    {
        nb::dict think;
        think["type"] = "thinking";
        think["thinking"] = SafeUtf8Str(reasoning);
        think["reasoning_source"] = "openai";
        content.append(MakeBlock(think));
    }
    if (!text.empty())
    {
        nb::dict tb;
        tb["type"] = "text";
        tb["text"] = SafeUtf8Str(text);
        content.append(MakeBlock(tb));
    }
    if (finish.empty())
    {
        throw ProtocolError("Chat completion ended without a finish_reason.");
    }

    bool has_calls = false;
    if (finish == "length")
    {
        // Truncated by max_tokens: drop partial tool calls cleanly.
    }
    else if (finish == "tool_calls" || (!calls.empty() && (finish == "stop" || finish == "function_call")))
    {
        if (finish == "tool_calls" && calls.empty())
        {
            throw ProtocolError("Missing tool calls.");
        }
        for (const ChatToolCall& tc : calls)
        {
            nb::object args = tc.arguments.empty() ? nb::object(nb::dict()) : JsonLoads(tc.arguments);
            if (tc.name.empty() || tc.id.empty() || !IsDict(args))
            {
                throw ProtocolError("Incomplete tool call.");
            }
            nb::dict tb;
            tb["type"] = "tool_use";
            tb["id"] = tc.id;
            tb["name"] = tc.name;
            tb["input"] = args;
            content.append(MakeBlock(tb));
            has_calls = true;
        }
    }
    else if (!calls.empty())
    {
        throw ProtocolError("Unexpected tool call with finish_reason=" + finish);
    }

    if (finish == "content_filter" && nb::len(content) == 0)
    {
        throw ProtocolError("Chat completion returned no content (finish_reason=content_filter).");
    }

    std::string stop = has_calls ? "tool_use" : (finish == "length" ? "max_tokens" : "end_turn");
    return MakeResponse(content, stop, usage);
}

/**
 * @brief Map OpenAI / DeepSeek Chat Completions usage keys into Dandelion's canonical usage dict.
 */
nb::object MapChatUsage(nb::object u)
{
    nb::dict out;
    if (u.is_none())
    {
        return out;
    }
    out["input_tokens"] = Field(u, "prompt_tokens");
    out["output_tokens"] = Field(u, "completion_tokens");
    out["total_tokens"] = Field(u, "total_tokens");

    nb::object cached = Field(Field(u, "prompt_tokens_details"), "cached_tokens");
    if (cached.is_none())
    {
        cached = Field(u, "prompt_cache_hit_tokens");
    }
    if (!cached.is_none())
    {
        out["cache_read_input_tokens"] = cached;
    }

    nb::object reasoning = Field(Field(u, "completion_tokens_details"), "reasoning_tokens");
    if (!reasoning.is_none())
    {
        out["reasoning_tokens"] = reasoning;
    }
    return out;
}

/**
 * @brief Incremental SSE decoder for OpenAI / DeepSeek Chat Completions streams.
 */
class ChatCompletionsStream : public StreamDecoder
{
public:
    using StreamDecoder::StreamDecoder;

    /**
     * @brief Consume one SSE data chunk (JSON object or [DONE] sentinel).
     */
    void OnEvent(const SseEvent& ev) override
    {
        if (ev.data.empty())
        {
            return;
        }
        if (Trim(ev.data) == "[DONE]")
        {
            m_done = true;
            return;
        }
        nb::object chunk = JsonLoads(ev.data);
        if (!Field(chunk, "error").is_none())
        {
            throw ProtocolError("Stream error: " + ev.data);
        }
        if (!Field(chunk, "usage").is_none())
        {
            m_usage = Field(chunk, "usage");
        }
        nb::object choices = Field(chunk, "choices");
        if (!IsList(choices) || nb::len(choices) == 0)
        {
            return;
        }
        nb::object choice = choices[0];
        if (!Field(choice, "finish_reason").is_none())
        {
            m_finish = TextOf(Field(choice, "finish_reason"));
        }
        nb::object delta = Field(choice, "delta");

        std::string reasoning = ExtractReasoningField(delta);
        if (!reasoning.empty())
        {
            m_reasoning += reasoning;
        }

        std::string text = TextOf(Field(delta, "content"));
        if (!text.empty())
        {
            Emit(text);
            m_text += text;
        }

        nb::object tool_calls = Field(delta, "tool_calls");
        if (!IsList(tool_calls))
        {
            return;
        }
        for (nb::handle tc : tool_calls)
        {
            nb::object raw_index = Field(tc, "index");
            long idx = IsInt(raw_index) ? nb::cast<long>(raw_index) : 0;
            nb::object fn = Field(tc, "function");
            std::string tc_id = TextOf(Field(tc, "id"));
            std::string fn_name = TextOf(Field(fn, "name"));
            std::string fn_args = TextOf(Field(fn, "arguments"));

            auto it = m_order.find(idx);
            if (it == m_order.end())
            {
                m_order[idx] = m_calls.size();
                m_calls.push_back({tc_id, fn_name, ""});
                it = m_order.find(idx);
            }
            else
            {
                // Accumulate late id/name fields delivered on subsequent delta chunks.
                if (!tc_id.empty() && m_calls[it->second].id.empty())
                {
                    m_calls[it->second].id = tc_id;
                }
                if (!fn_name.empty())
                {
                    if (m_calls[it->second].name.empty())
                    {
                        m_calls[it->second].name = fn_name;
                    }
                    else if (m_calls[it->second].name != fn_name)
                    {
                        m_calls[it->second].name += fn_name;
                    }
                }
            }
            if (!fn_args.empty())
            {
                Emit(fn_args, true);
                m_calls[it->second].arguments += fn_args;
            }
        }
    }

    /**
     * @brief Validate [DONE] termination and construct the canonical response.
     */
    nb::object Finish() override
    {
        if (!m_done)
        {
            throw ProtocolError("Chat completion stream ended before [DONE].");
        }
        return BuildChatResponse(m_reasoning, m_text, m_calls, m_finish, MapChatUsage(m_usage));
    }

private:
    bool m_done = false;
    std::string m_reasoning;
    std::string m_text;
    std::string m_finish;
    std::vector<ChatToolCall> m_calls;
    std::map<long, size_t> m_order;
    nb::object m_usage = nb::none();
};

/**
 * @brief OpenAI Chat Completions wire codec supporting DeepSeek reasoning replay and Sub2API compat flavors.
 */
class ChatCompletionsCodec : public Codec
{
public:
    /**
     * @brief Construct codec and resolve terminal flavor and effort support guards.
     */
    explicit ChatCompletionsCodec(const ProviderConfig& c)
        : Codec(c)
    {
        m_explicit_compat = IsExplicitCompat(c.compat);
        m_flavor = ResolveFlavor(c.compat, c.model_id, c.base_url, "openai");
        m_supports_effort = !(Contains(Lower(c.base_url), "nvidia") || ContainsAny(Lower(c.model_id), {"z-ai", "glm-5.2"}));
    }

    const char* Protocol() const override
    {
        return "openai";
    }

    std::string Flavor() const override
    {
        return m_flavor;
    }

    /**
     * @brief Build a Chat Completions JSON request with reasoning-model parameter rules and compat effort knobs.
     */
    Built Build(nb::dict payload, bool stream) override
    {
        nb::dict body;
        body["model"] = m_cfg.model_id;
        body["messages"] = ConvertMessages(payload.get("messages", nb::list()), payload.get("system", nb::str("")));

        nb::object cap = payload.get("max_tokens", nb::int_(8192));
        bool reasoning_model = IsOpenAIReasoningModel(m_cfg.model_id);
        if (reasoning_model)
        {
            body["max_completion_tokens"] = cap;
            if (DictHas(payload, "temperature"))
            {
                body["temperature"] = payload["temperature"];
            }
        }
        else
        {
            body["max_tokens"] = cap;
            body["temperature"] = DictHas(payload, "temperature") ? nb::object(payload["temperature"]) : nb::object(nb::float_(0.7));
        }

        if (stream)
        {
            body["stream"] = true;
            nb::dict sopt;
            sopt["include_usage"] = true;
            body["stream_options"] = sopt;
        }

        nb::list tools = ConvertTools(payload.get("tools", nb::none()));
        if (nb::len(tools) > 0)
        {
            body["tools"] = tools;
        }

        InjectEffort(body);

        std::string base = m_cfg.base_url.empty() ? EnvOr("OPENAI_BASE_URL", "https://api.openai.com/v1") : m_cfg.base_url;
        base = RstripSlash(base);

        Built b;
        b.url = EndsWith(base, "/chat/completions") ? base : (base + "/chat/completions");
        b.headers = {
            {"Authorization", "Bearer " + m_cfg.api_key},
            {"content-type", "application/json"},
            {"accept", stream ? "text/event-stream" : "application/json"},
        };
        b.body = body;
        return b;
    }

    /**
     * @brief Parse a non-streaming Chat Completions JSON response.
     */
    nb::object Parse(nb::object resp) override
    {
        if (!Field(resp, "error").is_none())
        {
            throw ProtocolError("Error: " + JsonDumps(resp));
        }
        nb::object choices = Field(resp, "choices");
        if (!IsList(choices) || nb::len(choices) == 0)
        {
            throw ProtocolError("Response has no choices: " + JsonDumps(resp));
        }
        nb::object choice = choices[0];
        nb::object message = Field(choice, "message");
        std::vector<ChatToolCall> calls;
        nb::object tool_calls = Field(message, "tool_calls");
        if (IsList(tool_calls))
        {
            for (nb::handle tc : tool_calls)
            {
                nb::object fn = Field(tc, "function");
                calls.push_back({TextOf(Field(tc, "id")), TextOf(Field(fn, "name")), TextOf(Field(fn, "arguments"))});
            }
        }
        return BuildChatResponse(ExtractReasoningField(message), TextOf(Field(message, "content")), calls, TextOf(Field(choice, "finish_reason")),
                                 MapChatUsage(Field(resp, "usage")));
    }

    std::unique_ptr<StreamDecoder> Decoder(bool echo) override
    {
        return std::make_unique<ChatCompletionsStream>(echo);
    }

private:
    /**
     * @brief Inject cross-protocol effort/thinking parameters into Chat Completions body idempotently.
     */
    void InjectEffort(nb::dict body)
    {
        for (const char* key : kOwnedKeys)
        {
            DictDelIfPresent(body, key);
        }
        if (!m_supports_effort && !m_explicit_compat)
        {
            return;
        }

        if (m_flavor == "openai")
        {
            if (m_cfg.thinking == "enabled" || m_explicit_compat)
            {
                body["reasoning_effort"] = ResolveEffort("EFFORT_TO_OPENAI_EFFORT", m_cfg.effort, m_cfg.model_id);
            }
        }
        else if (m_flavor == "deepseek")
        {
            if (m_cfg.thinking == "enabled")
            {
                nb::dict thinking;
                thinking["type"] = "enabled";
                body["thinking"] = thinking;
                body["reasoning_effort"] = ResolveEffort("EFFORT_TO_DEEPSEEK_EFFORT", m_cfg.effort, m_cfg.model_id);
            }
            else if (m_cfg.thinking == "disabled")
            {
                nb::dict thinking;
                thinking["type"] = "disabled";
                body["thinking"] = thinking;
            }
            else if (m_explicit_compat)
            {
                body["reasoning_effort"] = ResolveEffort("EFFORT_TO_DEEPSEEK_EFFORT", m_cfg.effort, m_cfg.model_id);
            }
        }
        else if (m_flavor == "gemini")
        {
            std::string level = ResolveGeminiLevel(m_cfg.effort, m_cfg.model_id);
            body["reasoning_effort"] = level;
            body["thinking_level"] = level;
            body["thinkingLevel"] = level;
        }
        else if (m_flavor == "anthropic" || m_flavor == "anthropic-effort")
        {
            if (m_cfg.thinking == "enabled")
            {
                nb::dict thinking;
                thinking["type"] = "enabled";
                thinking["budget_tokens"] = ResolveEffort("EFFORT_TO_BUDGET_TOKENS", m_cfg.effort, m_cfg.model_id);
                body["thinking"] = thinking;
            }
            if (m_flavor == "anthropic-effort")
            {
                nb::dict oc;
                oc["effort"] = ResolveEffort("EFFORT_TO_ANTHROPIC_EFFORT", m_cfg.effort, m_cfg.model_id);
                body["output_config"] = oc;
            }
        }
    }

    /**
     * @brief Translate canonical tool definitions into OpenAI function tool schemas.
     */
    static nb::list ConvertTools(nb::object tools)
    {
        nb::list out;
        if (!IsList(tools))
        {
            return out;
        }
        for (nb::handle t : tools)
        {
            nb::object schema = Field(t, "input_schema");
            if (schema.is_none())
            {
                nb::dict def_schema;
                def_schema["type"] = "object";
                def_schema["properties"] = nb::dict();
                schema = def_schema;
            }
            nb::dict fn;
            fn["name"] = Field(t, "name");
            fn["description"] = TextOf(Field(t, "description"));
            fn["parameters"] = schema;

            nb::dict entry;
            entry["type"] = "function";
            entry["function"] = fn;
            out.append(entry);
        }
        return out;
    }

    /**
     * @brief Translate canonical history and system prompt into Chat Completions message format.
     */
    static nb::list ConvertMessages(nb::object messages, nb::object system)
    {
        nb::list out;
        std::string sys_text = SystemText(system);
        if (!sys_text.empty())
        {
            nb::dict sm;
            sm["role"] = "system";
            sm["content"] = SafeUtf8Str(sys_text);
            out.append(sm);
        }
        if (!IsList(messages))
        {
            return out;
        }
        for (nb::handle msg : messages)
        {
            nb::object role = Field(msg, "role");
            nb::object content = Field(msg, "content");
            std::string r = TextOf(role);

            if (IsStr(content))
            {
                nb::dict m;
                m["role"] = role;
                m["content"] = content;
                out.append(m);
            }
            else if (IsList(content) && r == "user")
            {
                std::vector<std::string> texts;
                nb::list media;
                for (nb::handle block : content)
                {
                    if (!IsDict(block))
                    {
                        continue;
                    }
                    std::string type = TextOf(Field(block, "type"));
                    if (type == "text")
                    {
                        texts.push_back(TextOf(Field(block, "text")));
                    }
                    else if (type == "image")
                    {
                        nb::object src = Field(block, "source");
                        std::string mime = IsDict(src) ? TextOf(Field(src, "media_type")) : "";
                        std::string data = IsDict(src) ? TextOf(Field(src, "data")) : "";
                        if (!data.empty())
                        {
                            nb::dict img_url;
                            img_url["url"] = "data:" + (mime.empty() ? std::string("image/png") : mime) + ";base64," + data;
                            nb::dict item;
                            item["type"] = "image_url";
                            item["image_url"] = img_url;
                            media.append(item);
                        }
                    }
                    else if (type == "document")
                    {
                        nb::object src = Field(block, "source");
                        std::string mime = IsDict(src) ? TextOf(Field(src, "media_type")) : "";
                        std::string data = IsDict(src) ? TextOf(Field(src, "data")) : "";
                        std::string fname = TextOf(Field(block, "filename"));
                        if (fname.empty())
                        {
                            fname = "document.pdf";
                        }
                        if (!data.empty())
                        {
                            nb::dict file_obj;
                            file_obj["filename"] = fname;
                            file_obj["file_data"] = "data:" + (mime.empty() ? std::string("application/pdf") : mime) + ";base64," + data;
                            nb::dict item;
                            item["type"] = "file";
                            item["file"] = file_obj;
                            media.append(item);
                        }
                    }
                    else if (type == "tool_result")
                    {
                        nb::dict tm;
                        tm["role"] = "tool";
                        tm["tool_call_id"] = TextOf(Field(block, "tool_use_id"));
                        tm["content"] = SafeUtf8Str(ResultText(Field(block, "content")));
                        out.append(tm);
                    }
                }
                if (nb::len(media) > 0)
                {
                    nb::list user_parts;
                    if (!texts.empty())
                    {
                        nb::dict tp;
                        tp["type"] = "text";
                        tp["text"] = SafeUtf8Str(JoinLines(texts));
                        user_parts.append(tp);
                    }
                    for (nb::handle m : media)
                    {
                        user_parts.append(m);
                    }
                    nb::dict um;
                    um["role"] = "user";
                    um["content"] = user_parts;
                    out.append(um);
                }
                else if (!texts.empty())
                {
                    nb::dict um;
                    um["role"] = "user";
                    um["content"] = SafeUtf8Str(JoinLines(texts));
                    out.append(um);
                }
            }
            else if (IsList(content) && r == "assistant")
            {
                std::vector<std::string> texts;
                std::vector<std::string> reasonings;
                nb::list calls;
                for (nb::handle block : content)
                {
                    std::string type = TextOf(Field(block, "type"));
                    if (type == "thinking")
                    {
                        std::string thought = TextOf(Field(block, "thinking"));
                        if (!thought.empty())
                        {
                            reasonings.push_back(thought);
                        }
                    }
                    else if (type == "text")
                    {
                        std::string t = TextOf(Field(block, "text"));
                        if (!t.empty())
                        {
                            texts.push_back(t);
                        }
                    }
                    else if (type == "tool_use")
                    {
                        nb::object input = Field(block, "input");
                        nb::dict fn;
                        fn["name"] = TextOf(Field(block, "name"));
                        fn["arguments"] = JsonDumpsPlain(input.is_none() ? nb::object(nb::dict()) : input);

                        nb::dict call_obj;
                        call_obj["id"] = TextOf(Field(block, "id"));
                        call_obj["type"] = "function";
                        call_obj["function"] = fn;
                        calls.append(call_obj);
                    }
                }
                nb::dict m;
                m["role"] = "assistant";
                if (!reasonings.empty())
                {
                    m["reasoning_content"] = SafeUtf8Str(JoinLines(reasonings));
                }
                if (!texts.empty())
                {
                    m["content"] = SafeUtf8Str(JoinLines(texts));
                }
                else if (nb::len(calls) == 0)
                {
                    m["content"] = "";
                }
                if (nb::len(calls) > 0)
                {
                    m["tool_calls"] = calls;
                }
                out.append(m);
            }
        }
        return out;
    }

    std::string m_flavor;
    bool m_explicit_compat = false;
    bool m_supports_effort = true;
};

} // namespace

std::unique_ptr<Codec> MakeChatCompletionsCodec(const ProviderConfig& cfg)
{
    return std::make_unique<ChatCompletionsCodec>(cfg);
}

} // namespace dandelion

/**
 * @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/native/api_generate_content.cpp
 * @author Xiaojintao
 * @brief Google Gemini Generate Content API codec (:generateContent / :streamGenerateContent?alt=sse).
 * @version 0.1
 * @date 2026-11-28
 * @copyright Copyright (c) 2026
 */

#include "provider.hpp"

#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

namespace dandelion
{
namespace
{

/**
 * @brief Normalize base URL to include /v1beta unless an API version suffix is already present.
 */
std::string VersionedBase(const std::string& configured)
{
    std::string base = RstripSlash(configured.empty() ? std::string("https://generativelanguage.googleapis.com") : configured);
    for (const char* v : {"/v1", "/v1beta", "/v1alpha"})
    {
        if (EndsWith(base, v))
        {
            return base;
        }
    }
    return base + "/v1beta";
}

/**
 * @brief Return true for Gemini 1.x / 2.x models predating thought-signature validation and thinkingLevel.
 */
bool IsLegacyGemini(const std::string& bare_model)
{
    return ContainsAny(Lower(bare_model), {"gemini-1", "gemini-2"});
}

/**
 * @brief Return true for Gemini 2.5 models that accept thinkingBudget (case-insensitive).
 */
bool IsGemini25(const std::string& bare_model)
{
    return StartsWith(Lower(bare_model), "gemini-2.5-");
}

/**
 * @brief Build a map of tool_use id -> tool name, and record locally generated synthetic call IDs.
 */
std::map<std::string, std::string> CollectToolNames(nb::object messages, std::set<std::string>* local_ids)
{
    std::map<std::string, std::string> names;
    if (!IsList(messages))
    {
        return names;
    }
    for (nb::handle msg : messages)
    {
        nb::object content = Field(msg, "content");
        if (!IsList(content))
        {
            continue;
        }
        for (nb::handle block : content)
        {
            if (TextOf(Field(block, "type")) == "tool_use")
            {
                std::string id = TextOf(Field(block, "id"));
                names[id] = TextOf(Field(block, "name"));
                nb::object original = Field(block, "gemini_function_call");
                if (local_ids && IsDict(original) && Field(original, "id").is_none())
                {
                    local_ids->insert(id);
                }
            }
        }
    }
    return names;
}

/**
 * @brief Extract (mime_type, base64_data) from an image or document block.
 */
std::pair<std::string, std::string> ExtractMedia(nb::handle block)
{
    nb::object src = Field(block, "source");
    std::string mime = TextOf(Field(src, "media_type"));
    if (mime.empty())
    {
        mime = (TextOf(Field(block, "type")) == "document") ? "application/pdf" : "image/png";
    }
    return {mime, TextOf(Field(src, "data"))};
}

/**
 * @brief Return v if non-None, or an empty dict otherwise.
 */
nb::object AsObject(nb::object v)
{
    return v.is_none() ? nb::object(nb::dict()) : v;
}

/**
 * @brief Convert canonical Dandelion history into Gemini generateContent contents[] preserving thought signatures.
 */
nb::list BuildContents(nb::object messages, const std::string& bare_model)
{
    std::set<std::string> local_ids;
    auto names = CollectToolNames(messages, &local_ids);
    nb::list contents;
    if (!IsList(messages))
    {
        return contents;
    }

    for (nb::handle msg : messages)
    {
        bool user = TextOf(Field(msg, "role")) != "assistant";
        nb::object content = Field(msg, "content");
        nb::list parts;

        if (IsStr(content))
        {
            if (!TextOf(content).empty())
            {
                nb::dict p;
                p["text"] = content;
                parts.append(p);
            }
        }
        else if (IsList(content))
        {
            bool has_call = false;
            bool first_call_signed = false;
            for (nb::handle block : content)
            {
                std::string type = TextOf(Field(block, "type"));
                nb::object sig = Field(block, "thought_signature");
                nb::dict part;

                if (!user && type == "gemini_part")
                {
                    parts.append(Field(block, "part"));
                    continue;
                }
                if (type == "text")
                {
                    if (TextOf(Field(block, "text")).empty() && sig.is_none())
                    {
                        continue;
                    }
                    part["text"] = Field(block, "text");
                }
                else if (type == "image" || type == "document")
                {
                    auto media = ExtractMedia(block);
                    if (media.second.empty())
                    {
                        continue;
                    }
                    nb::dict inline_data;
                    inline_data["mimeType"] = media.first;
                    inline_data["data"] = media.second;
                    part["inlineData"] = inline_data;
                }
                else if (type == "tool_result")
                {
                    std::string id = TextOf(Field(block, "tool_use_id"));
                    std::string text = ResultText(Field(block, "content"));
                    bool is_err = static_cast<bool>(nb::bool_(Field(block, "is_error")));
                    nb::dict response;
                    response[is_err ? "error" : "output"] = SafeUtf8Str(text);

                    nb::dict fr;
                    fr["name"] = names.count(id) ? names[id] : id;
                    fr["response"] = response;
                    if (!local_ids.count(id))
                    {
                        fr["id"] = id;
                    }
                    part["functionResponse"] = fr;
                }
                else if (type == "tool_use")
                {
                    nb::object original = Field(block, "gemini_function_call");
                    if (IsDict(original))
                    {
                        part["functionCall"] = original;
                    }
                    else
                    {
                        nb::dict fc;
                        fc["id"] = TextOf(Field(block, "id"));
                        fc["name"] = TextOf(Field(block, "name"));
                        fc["args"] = AsObject(Field(block, "input"));
                        part["functionCall"] = fc;
                    }
                    if (!has_call)
                    {
                        first_call_signed = !sig.is_none();
                    }
                    has_call = true;
                }
                else
                {
                    continue;
                }

                if (!user && !sig.is_none())
                {
                    part["thoughtSignature"] = sig;
                }
                parts.append(part);
            }

            if (!user && has_call && !first_call_signed && !IsLegacyGemini(bare_model))
            {
                for (nb::handle part : parts)
                {
                    if (DictHas(part, "functionCall"))
                    {
                        part["thoughtSignature"] = "skip_thought_signature_validator";
                        break;
                    }
                }
            }
        }

        // Preserve strict user/model role alternation if all assistant blocks were foreign reasoning.
        if (!user && nb::len(parts) == 0)
        {
            nb::dict placeholder;
            placeholder["text"] = "[reasoning omitted]";
            parts.append(placeholder);
        }

        if (nb::len(parts) > 0)
        {
            nb::dict entry;
            entry["role"] = user ? "user" : "model";
            entry["parts"] = parts;
            contents.append(entry);
        }
    }
    return contents;
}

/**
 * @brief Map Gemini usageMetadata into Dandelion's canonical usage dict without signed overflow UB.
 */
nb::object MapGeminiUsage(nb::object u)
{
    nb::dict out;
    if (u.is_none())
    {
        return out;
    }
    nb::object output = nb::int_(0);
    for (const char* key : {"candidatesTokenCount", "thoughtsTokenCount"})
    {
        nb::object v = Field(u, key);
        if (IsInt(v))
        {
            output = output + v;
        }
    }
    out["input_tokens"] = Field(u, "promptTokenCount");
    out["output_tokens"] = output;
    out["total_tokens"] = Field(u, "totalTokenCount");
    nb::object cached = Field(u, "cachedContentTokenCount");
    if (!cached.is_none())
    {
        out["cache_read_input_tokens"] = cached;
    }
    return out;
}

/**
 * @brief Accumulator for GenerateContentResponse chunks (both streaming SSE and single JSON body).
 */
class GenerateContentStream : public StreamDecoder
{
public:
    using StreamDecoder::StreamDecoder;

    /**
     * @brief Consume one SSE event containing a GenerateContentResponse JSON chunk.
     */
    void OnEvent(const SseEvent& ev) override
    {
        if (ev.data.empty())
        {
            return;
        }
        AddChunk(JsonLoads(ev.data));
    }

    /**
     * @brief Merge one parsed GenerateContentResponse JSON object into accumulated blocks.
     */
    void AddChunk(nb::object chunk)
    {
        m_seen = true;
        if (!Field(chunk, "error").is_none())
        {
            throw ProtocolError("Stream error: " + JsonDumps(chunk));
        }
        nb::object feedback = Field(chunk, "promptFeedback");
        if (!Field(feedback, "blockReason").is_none())
        {
            std::string br = TextOf(Field(feedback, "blockReason"));
            if (!br.empty())
            {
                m_block_reason = br;
            }
        }
        if (!Field(chunk, "usageMetadata").is_none())
        {
            m_usage = Field(chunk, "usageMetadata");
        }
        nb::object candidates = Field(chunk, "candidates");
        if (!IsList(candidates) || nb::len(candidates) == 0)
        {
            return;
        }
        nb::object cand = candidates[0];
        if (!Field(cand, "finishReason").is_none())
        {
            std::string fr = TextOf(Field(cand, "finishReason"));
            if (!fr.empty())
            {
                m_finish = fr;
            }
        }
        nb::object parts = Field(Field(cand, "content"), "parts");
        if (!IsList(parts))
        {
            return;
        }
        for (nb::handle part : parts)
        {
            nb::object sig = Field(part, "thoughtSignature");
            nb::object call = Field(part, "functionCall");

            if (nb::bool_(Field(part, "thought")))
            {
                FlushPendingText();
                PreserveSignedPart(part, sig);
                continue;
            }
            if (!call.is_none())
            {
                FlushPendingText();
                nb::object args = AsObject(Field(call, "args"));
                bool incomplete = TextOf(Field(call, "name")).empty() || !IsDict(args) || !Field(call, "partialArgs").is_none() || nb::bool_(Field(call, "willContinue"));
                std::string id = TextOf(Field(call, "id"));
                nb::dict block;
                block["type"] = "tool_use";
                block["id"] = id.empty() ? RandomCallId() : id;
                block["name"] = TextOf(Field(call, "name"));
                block["input"] = IsDict(args) ? args : nb::object(nb::dict());
                if (id.empty() || !sig.is_none())
                {
                    block["gemini_function_call"] = call;
                }
                if (!sig.is_none())
                {
                    block["thought_signature"] = sig;
                }
                size_t slot = m_blocks.size();
                m_blocks.push_back(MakeBlock(block));
                if (incomplete)
                {
                    // Defer rejection until Finish() so MAX_TOKENS drops truncated tool calls cleanly.
                    m_incomplete_calls.insert(slot);
                }
                else if (m_echo)
                {
                    Emit(JsonDumpsPlain(args), true);
                }
                continue;
            }
            nb::object text = Field(part, "text");
            if (!text.is_none())
            {
                std::string t = TextOf(text);
                if (sig.is_none())
                {
                    m_has_pending_text = true;
                    m_pending_text += t;
                }
                else
                {
                    FlushPendingText();
                    nb::dict block;
                    block["type"] = "text";
                    block["text"] = SafeUtf8Str(t);
                    block["thought_signature"] = sig;
                    m_blocks.push_back(MakeBlock(block));
                }
                Emit(t);
                continue;
            }
            FlushPendingText();
            PreserveSignedPart(part, sig);
        }
    }

    /**
     * @brief Finalize accumulated blocks and surface prompt blocks or abnormal finishReason errors.
     */
    nb::object Finish() override
    {
        if (!m_seen)
        {
            throw ProtocolError("Empty stream response (endpoint did not return generateContent SSE).");
        }
        FlushPendingText();

        if (!m_block_reason.empty() && m_finish.empty())
        {
            throw ProtocolError("Gemini blocked the prompt (blockReason=" + m_block_reason + ").");
        }
        if (m_finish.empty())
        {
            throw ProtocolError("Gemini response ended before finishReason.");
        }
        if (m_finish != "STOP" && m_finish != "MAX_TOKENS")
        {
            throw ProtocolError("Gemini stopped abnormally (finishReason=" + m_finish + ").");
        }

        nb::list content;
        bool has_calls = false;
        for (size_t i = 0; i < m_blocks.size(); ++i)
        {
            nb::object& block = m_blocks[i];
            std::string type = TextOf(Field(block, "type"));
            if (type == "text" && TextOf(Field(block, "text")).empty() && Field(block, "thought_signature").is_none())
            {
                continue;
            }
            if (type == "tool_use")
            {
                if (m_finish == "MAX_TOKENS")
                {
                    continue;
                }
                if (m_finish != "STOP")
                {
                    throw ProtocolError("Incomplete Gemini function call (finishReason=" + m_finish + ").");
                }
                if (m_incomplete_calls.count(i))
                {
                    throw ProtocolError("Incomplete Gemini function call.");
                }
            }
            has_calls = has_calls || (type == "tool_use");
            content.append(block);
        }

        if (!has_calls && nb::len(content) == 0 && !m_block_reason.empty())
        {
            throw ProtocolError("Gemini blocked the prompt (blockReason=" + m_block_reason + ").");
        }

        std::string stop = has_calls ? "tool_use" : (m_finish == "MAX_TOKENS" ? "max_tokens" : "end_turn");
        return MakeResponse(content, stop, MapGeminiUsage(m_usage));
    }

private:
    /**
     * @brief Flush contiguous unsigned plain text buffered in C++ into a single text Block.
     */
    void FlushPendingText()
    {
        if (!m_has_pending_text)
        {
            return;
        }
        nb::dict block;
        block["type"] = "text";
        block["text"] = SafeUtf8Str(m_pending_text);
        m_blocks.push_back(MakeBlock(block));
        m_pending_text.clear();
        m_has_pending_text = false;
    }

    /**
     * @brief Preserve opaque signed Gemini parts verbatim for stateless round-tripping.
     */
    void PreserveSignedPart(nb::handle part, nb::object sig)
    {
        if (!sig.is_none())
        {
            nb::dict block;
            block["type"] = "gemini_part";
            block["part"] = part;
            m_blocks.push_back(MakeBlock(block));
        }
    }

    bool m_seen = false;
    bool m_has_pending_text = false;
    std::string m_pending_text;
    std::vector<nb::object> m_blocks;
    std::set<size_t> m_incomplete_calls;
    nb::object m_usage = nb::none();
    std::string m_finish;
    std::string m_block_reason;
};

/**
 * @brief Google Gemini Generate Content codec supporting Gemini 2.5 budgets, Gemini 3/4 thinkingLevel (xhigh), and compat.
 */
class GenerateContentCodec : public Codec
{
public:
    /**
     * @brief Construct codec, validate bare model identifier, and resolve terminal flavor.
     */
    explicit GenerateContentCodec(const ProviderConfig& c)
        : Codec(c)
    {
        m_bare_model = SanitizeBareModel(c.model_id);
        m_explicit_compat = IsExplicitCompat(c.compat);
        m_flavor = ResolveFlavor(c.compat, c.model_id, c.base_url, "gemini");
    }

    /**
     * @brief Return the Generate Content protocol identifier ("gemini").
     */
    const char* Protocol() const override
    {
        return "gemini";
    }

    /**
     * @brief Return the resolved upstream flavor.
     */
    std::string Flavor() const override
    {
        return m_flavor;
    }

    /**
     * @brief Build a Gemini :generateContent or :streamGenerateContent?alt=sse request.
     */
    Built Build(nb::dict payload, bool stream) override
    {
        nb::dict body;
        body["contents"] = BuildContents(payload.get("messages", nb::none()), m_bare_model);

        std::string system = SystemText(payload.get("system", nb::str("")));
        if (!system.empty())
        {
            nb::dict sp;
            sp["text"] = SafeUtf8Str(system);
            nb::list sparts;
            sparts.append(sp);
            nb::dict sinst;
            sinst["parts"] = sparts;
            body["systemInstruction"] = sinst;
        }

        nb::object tools = payload.get("tools", nb::none());
        if (IsList(tools) && nb::len(tools) > 0)
        {
            nb::list decls;
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
                nb::dict decl;
                decl["name"] = Field(t, "name");
                decl["description"] = TextOf(Field(t, "description"));
                decl["parametersJsonSchema"] = schema;
                decls.append(decl);
            }
            nb::dict tool_entry;
            tool_entry["functionDeclarations"] = decls;
            nb::list tool_list;
            tool_list.append(tool_entry);
            body["tools"] = tool_list;
        }

        nb::dict gen;
        nb::object cap = payload.get("max_tokens", nb::none());
        if (IsInt(cap))
        {
            gen["maxOutputTokens"] = cap;
        }

        if (m_cfg.thinking == "enabled" && m_flavor != "none")
        {
            if (IsGemini25(m_bare_model))
            {
                nb::dict tc;
                tc["thinkingBudget"] = ResolveEffort("EFFORT_TO_GEMINI_25_BUDGET", m_cfg.effort, m_cfg.model_id);
                gen["thinkingConfig"] = tc;
            }
            else if (!IsLegacyGemini(m_bare_model))
            {
                std::string level = Upper(ResolveGeminiLevel(m_cfg.effort, m_cfg.model_id));
                nb::dict tc;
                tc["thinkingLevel"] = level;
                gen["thinkingConfig"] = tc;
            }
        }
        else if (m_cfg.thinking == "disabled" && m_flavor != "none")
        {
            std::string bare_low = Lower(m_bare_model);
            if (StartsWith(bare_low, "gemini-2.5-flash"))
            {
                nb::dict tc;
                tc["thinkingBudget"] = 0;
                gen["thinkingConfig"] = tc;
            }
            else if (IsGemini25(m_bare_model))
            {
                nb::dict tc;
                tc["thinkingBudget"] = 128;
                gen["thinkingConfig"] = tc;
            }
            else if (!IsLegacyGemini(m_bare_model))
            {
                const char* level = (Contains(bare_low, "gemini-3") && Contains(bare_low, "flash")) ? "MINIMAL" : "LOW";
                nb::dict tc;
                tc["thinkingLevel"] = level;
                gen["thinkingConfig"] = tc;
            }
        }
        if (nb::len(gen) > 0)
        {
            body["generationConfig"] = gen;
        }

        if (m_explicit_compat && m_flavor != "none")
        {
            if (m_flavor == "openai")
            {
                body["reasoning_effort"] = ResolveEffort("EFFORT_TO_OPENAI_EFFORT", m_cfg.effort, m_cfg.model_id);
            }
            else if (m_flavor == "deepseek")
            {
                nb::object eff = ResolveEffort("EFFORT_TO_DEEPSEEK_EFFORT", m_cfg.effort, m_cfg.model_id);
                body["reasoning_effort"] = eff;
                nb::dict oc;
                oc["effort"] = eff;
                body["output_config"] = oc;
            }
            else if (m_flavor == "anthropic-effort")
            {
                nb::dict oc;
                oc["effort"] = ResolveEffort("EFFORT_TO_ANTHROPIC_EFFORT", m_cfg.effort, m_cfg.model_id);
                body["output_config"] = oc;
            }
        }

        Built b;
        b.url = VersionedBase(m_cfg.base_url) + "/models/" + m_bare_model + (stream ? ":streamGenerateContent?alt=sse" : ":generateContent");
        b.headers = {
            {"x-goog-api-key", m_cfg.api_key},
            {"content-type", "application/json"},
            {"accept", stream ? "text/event-stream" : "application/json"},
        };
        b.body = body;
        return b;
    }

    /**
     * @brief Parse a non-streaming GenerateContentResponse JSON body.
     */
    nb::object Parse(nb::object body) override
    {
        GenerateContentStream acc(false);
        acc.AddChunk(body);
        return acc.Finish();
    }

    /**
     * @brief Create a fresh GenerateContentStream SSE decoder.
     */
    std::unique_ptr<StreamDecoder> Decoder(bool echo) override
    {
        return std::make_unique<GenerateContentStream>(echo);
    }

private:
    std::string m_bare_model;
    std::string m_flavor;
    bool m_explicit_compat = false;
};

} // namespace

std::unique_ptr<Codec> MakeGenerateContentCodec(const ProviderConfig& cfg)
{
    return std::make_unique<GenerateContentCodec>(cfg);
}

} // namespace dandelion

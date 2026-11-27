/**
 * @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/native/api_interactions.cpp
 * @author Xiaojintao
 * @brief Google Gemini Interactions API codec (POST {base}/v1beta/interactions, stateless store=false).
 * @version 0.1
 * @date 2026-11-28
 * @copyright Copyright (c) 2026
 */

#include "provider.hpp"

#include <cstdint>
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
 * @brief Buffered thought_summary item allowing O(N) C++ text coalescing before Finish().
 */
struct SummaryEntry
{
    bool is_text = false;
    std::string text;
    nb::object raw = nb::none();
};

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
 * @brief Return true for Gemini 1.x / 2.x models predating thinking_level.
 */
bool IsLegacyGemini(const std::string& bare_model)
{
    return ContainsAny(Lower(bare_model), {"gemini-1", "gemini-2"});
}

/**
 * @brief Return true for Gemini 2.5 models that accept thinking_budget (case-insensitive).
 */
bool IsGemini25(const std::string& bare_model)
{
    return StartsWith(Lower(bare_model), "gemini-2.5-");
}

/**
 * @brief Return true if status is a terminal Interactions lifecycle state.
 */
bool IsTerminalStatus(const std::string& status)
{
    return status == "completed" || status == "requires_action" || status == "incomplete";
}

/**
 * @brief Collect tool_use id -> tool name mappings from canonical message history.
 */
std::map<std::string, std::string> CollectToolNames(nb::object messages)
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
                names[TextOf(Field(block, "id"))] = TextOf(Field(block, "name"));
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
 * @brief Convert canonical history into stateless Interactions input[] steps in strict chronological order.
 */
nb::list BuildSteps(nb::object messages)
{
    std::map<std::string, std::string> names = CollectToolNames(messages);
    nb::list steps;
    if (!IsList(messages))
    {
        return steps;
    }

    for (nb::handle msg : messages)
    {
        bool user = TextOf(Field(msg, "role")) != "assistant";
        nb::object content = Field(msg, "content");
        size_t before_steps = nb::len(steps);

        if (IsStr(content))
        {
            if (!TextOf(content).empty())
            {
                nb::dict item;
                item["type"] = "text";
                item["text"] = content;
                nb::list items;
                items.append(item);

                nb::dict step;
                step["type"] = user ? "user_input" : "model_output";
                step["content"] = items;
                steps.append(step);
            }
        }
        else if (IsList(content))
        {
            nb::list items;
            auto flush = [&](const char* type) {
                if (nb::len(items) > 0)
                {
                    nb::dict step;
                    step["type"] = type;
                    step["content"] = items;
                    steps.append(step);
                    items = nb::list();
                }
            };

            for (nb::handle block : content)
            {
                std::string type = TextOf(Field(block, "type"));
                if (type == "text")
                {
                    if (!TextOf(Field(block, "text")).empty())
                    {
                        nb::dict item;
                        item["type"] = "text";
                        item["text"] = Field(block, "text");
                        items.append(item);
                    }
                }
                else if (user && (type == "image" || type == "document"))
                {
                    auto media = ExtractMedia(block);
                    if (!media.second.empty())
                    {
                        nb::dict item;
                        item["type"] = type;
                        item["data"] = media.second;
                        item["mime_type"] = media.first;
                        items.append(item);
                    }
                }
                else if (user && type == "tool_result")
                {
                    flush("user_input");
                    std::string id = TextOf(Field(block, "tool_use_id"));
                    nb::dict step;
                    step["type"] = "function_result";
                    step["call_id"] = id;
                    step["name"] = names.count(id) ? names[id] : id;
                    step["result"] = SafeUtf8Str(ResultText(Field(block, "content")));
                    if (nb::bool_(Field(block, "is_error")))
                    {
                        step["is_error"] = true;
                    }
                    steps.append(step);
                }
                else if (!user && type == "gemini_thought")
                {
                    flush("model_output");
                    nb::dict step;
                    step["type"] = "thought";
                    nb::object sig = Field(block, "signature");
                    if (!sig.is_none())
                    {
                        step["signature"] = sig;
                    }
                    if (!Field(block, "summary").is_none())
                    {
                        step["summary"] = Field(block, "summary");
                    }
                    steps.append(step);
                }
                else if (!user && type == "tool_use")
                {
                    flush("model_output");
                    nb::dict step;
                    step["type"] = "function_call";
                    step["id"] = TextOf(Field(block, "id"));
                    step["name"] = TextOf(Field(block, "name"));
                    step["arguments"] = AsObject(Field(block, "input"));
                    steps.append(step);
                }
            }
            flush(user ? "user_input" : "model_output");
        }

        // Preserve turn alternation when an assistant turn had only stripped foreign blocks.
        if (!user && nb::len(steps) == before_steps)
        {
            nb::dict item;
            item["type"] = "text";
            item["text"] = "[reasoning omitted]";
            nb::list items;
            items.append(item);

            nb::dict placeholder;
            placeholder["type"] = "model_output";
            placeholder["content"] = items;
            steps.append(placeholder);
        }
    }
    return steps;
}

/**
 * @brief Map Interactions usage object into Dandelion's canonical usage dict without signed overflow UB.
 */
nb::object MapInteractionUsage(nb::object u)
{
    nb::dict out;
    if (u.is_none())
    {
        return out;
    }
    nb::object output = nb::int_(0);
    for (const char* key : {"total_output_tokens", "total_thought_tokens"})
    {
        nb::object v = Field(u, key);
        if (IsInt(v))
        {
            output = output + v;
        }
    }
    out["input_tokens"] = Field(u, "total_input_tokens");
    out["output_tokens"] = output;
    out["total_tokens"] = Field(u, "total_tokens");
    nb::object cached = Field(u, "total_cached_tokens");
    if (!cached.is_none())
    {
        out["cache_read_input_tokens"] = cached;
    }
    return out;
}

/**
 * @brief Extract text from an Interactions model_output content field.
 */
std::string ExtractStepText(nb::object content)
{
    if (IsStr(content))
    {
        return TextOf(content);
    }
    std::string out;
    if (IsList(content))
    {
        for (nb::handle item : content)
        {
            out += TextOf(Field(item, "text"));
        }
    }
    else if (!content.is_none())
    {
        out = TextOf(Field(content, "text"));
    }
    return out;
}

/**
 * @brief Slice echoed input steps from a stateless Interactions response so only new model steps remain.
 */
nb::object SliceGeneratedSteps(nb::object steps)
{
    if (!IsList(steps))
    {
        return steps;
    }
    size_t total = nb::len(steps);
    size_t start_idx = 0;
    for (size_t i = 0; i < total; ++i)
    {
        std::string stype = TextOf(Field(steps[i], "type"));
        if (stype == "user_input" || stype == "function_result")
        {
            start_idx = i + 1;
        }
    }
    if (start_idx == 0)
    {
        return steps;
    }
    nb::list sliced;
    for (size_t i = start_idx; i < total; ++i)
    {
        sliced.append(steps[i]);
    }
    return sliced;
}

/**
 * @brief Convert Interactions output steps into a canonical response namespace.
 */
nb::object BuildInteractionResponse(nb::object steps, const std::string& status, nb::object usage)
{
    if (!IsTerminalStatus(status))
    {
        throw ProtocolError("Interaction ended without a terminal status: " + status);
    }
    nb::list content;
    bool has_calls = false;
    if (IsList(steps))
    {
        for (nb::handle step : steps)
        {
            std::string type = TextOf(Field(step, "type"));
            if (type == "thought")
            {
                nb::dict block;
                block["type"] = "gemini_thought";
                nb::object sig = Field(step, "signature");
                if (!sig.is_none())
                {
                    block["signature"] = sig;
                }
                if (!Field(step, "summary").is_none())
                {
                    block["summary"] = Field(step, "summary");
                }
                content.append(MakeBlock(block));
            }
            else if (type == "model_output")
            {
                std::string text = ExtractStepText(Field(step, "content"));
                if (!text.empty())
                {
                    nb::dict block;
                    block["type"] = "text";
                    block["text"] = SafeUtf8Str(text);
                    content.append(MakeBlock(block));
                }
            }
            else if (type == "function_call" && status != "incomplete")
            {
                std::string id = TextOf(Field(step, "id"));
                std::string name = TextOf(Field(step, "name"));
                nb::object args = Field(step, "arguments");
                if (IsStr(args))
                {
                    std::string raw = TextOf(args);
                    args = raw.empty() ? nb::object(nb::dict()) : JsonLoads(raw);
                }
                if (id.empty() || name.empty() || !IsDict(args))
                {
                    throw ProtocolError("Incomplete Interactions function call.");
                }
                nb::dict block;
                block["type"] = "tool_use";
                block["id"] = id;
                block["name"] = name;
                block["input"] = args;
                content.append(MakeBlock(block));
                has_calls = true;
            }
        }
    }
    std::string stop = has_calls ? "tool_use" : (status == "incomplete" ? "max_tokens" : "end_turn");
    return MakeResponse(content, stop, MapInteractionUsage(usage));
}

/**
 * @brief Throw ProtocolError when an interaction reports failed or cancelled status.
 */
void CheckInteractionStatus(nb::object interaction)
{
    std::string status = TextOf(Field(interaction, "status"));
    if (status == "failed" || status == "cancelled")
    {
        throw ProtocolError("Interaction " + status + ": " + JsonDumps(AsObject(Field(interaction, "errors"))));
    }
}

/**
 * @brief Incremental SSE decoder for Gemini Interactions streams.
 *
 * Buffers both text and thought_summary deltas in C++ std::string for linear O(N) performance
 * and defers function_call argument JSON parsing to Finish() for clean incomplete truncation drops.
 */
class InteractionsStream : public StreamDecoder
{
public:
    using StreamDecoder::StreamDecoder;

    /**
     * @brief Consume one Interactions SSE event.
     */
    void OnEvent(const SseEvent& ev) override
    {
        if (ev.data.empty() || Trim(ev.data) == "[DONE]")
        {
            return;
        }
        nb::object d = JsonLoads(ev.data);
        std::string type = TextOf(Field(d, "event_type"));
        if (type.empty())
        {
            type = ev.event;
        }
        if (type == "error" || ev.event == "error" || !Field(d, "error").is_none())
        {
            throw ProtocolError("Stream error: " + ev.data);
        }
        if (type == "step.start")
        {
            int64_t idx = ReadIndex(d);
            if (m_steps.count(idx))
            {
                throw ProtocolError("Duplicate Interactions step index.");
            }
            m_open.insert(idx);
            nb::dict step = CopyDict(Field(d, "step"));
            SeedStepBuffers(idx, step);
            m_steps[idx] = step;
        }
        else if (type == "step.delta")
        {
            int64_t idx = ReadIndex(d);
            if (!m_open.count(idx))
            {
                throw ProtocolError("Interactions step is not open.");
            }
            ApplyDelta(idx, Field(d, "delta"));
        }
        else if (type == "step.stop")
        {
            int64_t idx = ReadIndex(d);
            if (!m_open.erase(idx))
            {
                throw ProtocolError("Interactions step is not open.");
            }
        }
        else if (type == "interaction.completed" || type == "interaction.status_update")
        {
            if (type == "interaction.completed")
            {
                m_completed = true;
            }
            nb::object inter = Field(d, "interaction");
            if (inter.is_none())
            {
                inter = d;
            }
            CheckInteractionStatus(inter);
            if (!Field(inter, "status").is_none())
            {
                std::string st = TextOf(Field(inter, "status"));
                if (!st.empty())
                {
                    m_status = st;
                }
            }
            if (type == "interaction.completed" && m_status.empty())
            {
                m_status = "completed";
            }
            if (!Field(inter, "usage").is_none())
            {
                m_usage = Field(inter, "usage");
            }
            nb::object inter_steps = Field(inter, "steps");
            if (IsList(inter_steps) && nb::len(inter_steps) > 0)
            {
                m_final_steps = inter_steps;
            }
        }
    }

    /**
     * @brief Validate terminal status and step boundaries, materialize C++ buffers, and build response.
     */
    nb::object Finish() override
    {
        bool terminal = m_completed || IsTerminalStatus(m_status);
        if (!terminal || (!m_open.empty() && m_status != "incomplete"))
        {
            throw ProtocolError("Incomplete Interactions stream (missing step.stop or terminal status).");
        }

        if (!m_steps.empty())
        {
            nb::list steps;
            for (auto& kv : m_steps)
            {
                int64_t idx = kv.first;
                nb::dict step = kv.second;
                if (m_text_buf.count(idx) && !m_text_buf[idx].empty())
                {
                    if (!DictHas(step, "type"))
                    {
                        step["type"] = "model_output";
                    }
                    nb::dict part;
                    part["type"] = "text";
                    part["text"] = SafeUtf8Str(m_text_buf[idx]);
                    nb::list parts;
                    parts.append(part);
                    step["content"] = parts;
                }
                if (m_summary_buf.count(idx) && !m_summary_buf[idx].empty())
                {
                    nb::list summary_list;
                    for (const SummaryEntry& entry : m_summary_buf[idx])
                    {
                        if (entry.is_text)
                        {
                            nb::dict part;
                            part["type"] = "text";
                            part["text"] = SafeUtf8Str(entry.text);
                            summary_list.append(part);
                        }
                        else if (!entry.raw.is_none())
                        {
                            summary_list.append(entry.raw);
                        }
                    }
                    step["summary"] = summary_list;
                }
                if (m_status != "incomplete" && m_args.count(idx) && !m_args[idx].empty())
                {
                    step["arguments"] = JsonLoads(m_args[idx]);
                }
                steps.append(step);
            }
            return BuildInteractionResponse(steps, m_status, m_usage);
        }

        if (!m_final_steps.is_none())
        {
            return BuildInteractionResponse(SliceGeneratedSteps(m_final_steps), m_status, m_usage);
        }

        return BuildInteractionResponse(nb::list(), m_status, m_usage);
    }

private:
    /**
     * @brief Seed C++ text, summary, and argument buffers from initial fields present on step.start.
     */
    void SeedStepBuffers(int64_t idx, nb::dict step)
    {
        std::string init_text = ExtractStepText(Field(step, "content"));
        if (!init_text.empty())
        {
            m_text_buf[idx] = init_text;
        }
        nb::object init_args = Field(step, "arguments");
        if (IsStr(init_args))
        {
            std::string raw_args = TextOf(init_args);
            if (!raw_args.empty())
            {
                m_args[idx] = raw_args;
            }
        }
        nb::object init_summary = Field(step, "summary");
        if (IsList(init_summary) && nb::len(init_summary) > 0)
        {
            for (nb::handle item : init_summary)
            {
                AppendSummaryItem(idx, nb::borrow<nb::object>(item));
            }
        }
    }

    /**
     * @brief Append one thought_summary item into C++ buffer, coalescing adjacent text entries in O(1).
     */
    void AppendSummaryItem(int64_t idx, nb::object item)
    {
        if (item.is_none())
        {
            return;
        }
        auto& entries = m_summary_buf[idx];
        if (IsDict(item) && TextOf(Field(item, "type")) == "text")
        {
            std::string chunk = TextOf(Field(item, "text"));
            if (!entries.empty() && entries.back().is_text)
            {
                entries.back().text += chunk;
            }
            else
            {
                SummaryEntry entry;
                entry.is_text = true;
                entry.text = std::move(chunk);
                entries.push_back(std::move(entry));
            }
            return;
        }
        SummaryEntry entry;
        entry.is_text = false;
        entry.raw = std::move(item);
        entries.push_back(std::move(entry));
    }

    /**
     * @brief Read and validate an explicit non-negative Interactions step index.
     */
    int64_t ReadIndex(nb::handle d) const
    {
        nb::object raw = Field(d, "index");
        if (!IsInt(raw))
        {
            throw ProtocolError("Missing Interactions step index.");
        }
        int64_t v = nb::cast<int64_t>(raw);
        if (v < 0)
        {
            throw ProtocolError("Negative Interactions step index.");
        }
        return v;
    }

    /**
     * @brief Apply one step.delta payload to the open step at idx.
     */
    void ApplyDelta(int64_t idx, nb::object delta)
    {
        nb::dict s = m_steps.at(idx);
        std::string type = TextOf(Field(delta, "type"));
        if (type == "thought_signature" || (!Field(delta, "signature").is_none() && type != "text"))
        {
            s["signature"] = Field(delta, "signature");
            return;
        }
        if (type == "thought_summary")
        {
            AppendSummaryItem(idx, Field(delta, "content"));
            return;
        }
        for (const char* key : {"arguments_delta", "partial_arguments", "arguments"})
        {
            nb::object part = Field(delta, key);
            if (IsStr(part))
            {
                std::string chunk = TextOf(part);
                m_args[idx] += chunk;
                Emit(chunk, true);
                return;
            }
            if (IsDict(part))
            {
                s["arguments"] = part;
                if (m_echo)
                {
                    Emit(JsonDumpsPlain(part), true);
                }
                return;
            }
        }
        if (!Field(delta, "text").is_none())
        {
            std::string t = TextOf(Field(delta, "text"));
            if (!DictHas(s, "type"))
            {
                s["type"] = "model_output";
            }
            m_text_buf[idx] += t;
            Emit(t);
        }
    }

    bool m_completed = false;
    std::set<int64_t> m_open;
    std::map<int64_t, nb::dict> m_steps;
    std::map<int64_t, std::string> m_text_buf;
    std::map<int64_t, std::vector<SummaryEntry>> m_summary_buf;
    std::map<int64_t, std::string> m_args;
    std::string m_status;
    nb::object m_usage = nb::none();
    nb::object m_final_steps = nb::none();
};

/**
 * @brief Google Gemini Interactions codec supporting Gemini 2.5 budgets, Gemini 3/4 xhigh, and compat.
 */
class InteractionsCodec : public Codec
{
public:
    /**
     * @brief Construct codec, validate bare model identifier, and resolve terminal flavor.
     */
    explicit InteractionsCodec(const ProviderConfig& c)
        : Codec(c)
    {
        m_bare_model = SanitizeBareModel(c.model_id);
        m_explicit_compat = IsExplicitCompat(c.compat);
        m_flavor = ResolveFlavor(c.compat, c.model_id, c.base_url, "gemini");
    }

    /**
     * @brief Return the Interactions protocol identifier ("interactions").
     */
    const char* Protocol() const override
    {
        return "interactions";
    }

    /**
     * @brief Return the resolved upstream flavor.
     */
    std::string Flavor() const override
    {
        return m_flavor;
    }

    /**
     * @brief Build a stateless /v1beta/interactions POST request.
     */
    Built Build(nb::dict payload, bool stream) override
    {
        nb::dict body;
        body["model"] = m_bare_model;
        body["input"] = BuildSteps(payload.get("messages", nb::none()));

        std::string system = SystemText(payload.get("system", nb::str("")));
        if (!system.empty())
        {
            body["system_instruction"] = SafeUtf8Str(system);
        }

        nb::object tools = payload.get("tools", nb::none());
        if (IsList(tools) && nb::len(tools) > 0)
        {
            nb::list out;
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
                nb::dict entry;
                entry["type"] = "function";
                entry["name"] = Field(t, "name");
                entry["description"] = TextOf(Field(t, "description"));
                entry["parameters"] = schema;
                out.append(entry);
            }
            body["tools"] = out;
        }

        nb::dict gen;
        nb::object cap = payload.get("max_tokens", nb::none());
        if (IsInt(cap))
        {
            gen["max_output_tokens"] = cap;
        }
        if (m_cfg.thinking == "enabled" && m_flavor != "none")
        {
            if (IsGemini25(m_bare_model))
            {
                gen["thinking_budget"] = ResolveEffort("EFFORT_TO_GEMINI_25_BUDGET", m_cfg.effort, m_cfg.model_id);
            }
            else if (!IsLegacyGemini(m_bare_model))
            {
                gen["thinking_level"] = ResolveGeminiLevel(m_cfg.effort, m_cfg.model_id);
            }
        }
        if (nb::len(gen) > 0)
        {
            body["generation_config"] = gen;
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

        body["store"] = false;
        if (stream)
        {
            body["stream"] = true;
        }

        Built b;
        b.url = VersionedBase(m_cfg.base_url) + "/interactions";
        b.headers = {
            {"x-goog-api-key", m_cfg.api_key},
            {"content-type", "application/json"},
            {"accept", stream ? "text/event-stream" : "application/json"},
        };
        b.body = body;
        return b;
    }

    /**
     * @brief Parse a non-streaming Interactions JSON body, checking errors and slicing echoed input steps.
     */
    nb::object Parse(nb::object body) override
    {
        if (!Field(body, "error").is_none())
        {
            throw ProtocolError("Error: " + JsonDumps(body));
        }
        CheckInteractionStatus(body);
        return BuildInteractionResponse(SliceGeneratedSteps(Field(body, "steps")), TextOf(Field(body, "status")), Field(body, "usage"));
    }

    /**
     * @brief Create a fresh InteractionsStream SSE decoder.
     */
    std::unique_ptr<StreamDecoder> Decoder(bool echo) override
    {
        return std::make_unique<InteractionsStream>(echo);
    }

private:
    std::string m_bare_model;
    std::string m_flavor;
    bool m_explicit_compat = false;
};

} // namespace

std::unique_ptr<Codec> MakeInteractionsCodec(const ProviderConfig& cfg)
{
    return std::make_unique<InteractionsCodec>(cfg);
}

} // namespace dandelion

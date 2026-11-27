/**
 * @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/native/api_responses.cpp
 * @author Xiaojintao
 * @brief OpenAI Responses API codec (POST {base}/responses) with stateless replay and Sub2API compat.
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
 * @brief Payload keys owned by this codec's reasoning/compat injection.
 */
const char* const kOwnedKeys[] = {"reasoning", "include", "thinking_level", "thinkingLevel", "output_config"};

/**
 * @brief Map OpenAI Responses usage fields into Dandelion's canonical usage dict.
 */
nb::object MapResponsesUsage(nb::object u)
{
    nb::dict out;
    if (u.is_none())
    {
        return out;
    }
    out["input_tokens"] = Field(u, "input_tokens");
    out["output_tokens"] = Field(u, "output_tokens");
    out["total_tokens"] = Field(u, "total_tokens");

    nb::object cached = Field(Field(u, "input_tokens_details"), "cached_tokens");
    if (!cached.is_none())
    {
        out["cache_read_input_tokens"] = cached;
    }
    nb::object reasoning = Field(Field(u, "output_tokens_details"), "reasoning_tokens");
    if (!reasoning.is_none())
    {
        out["reasoning_tokens"] = reasoning;
    }
    return out;
}

/**
 * @brief Extract visible text (output_text / text / refusal) from a Responses message output item.
 */
std::string ExtractMessageText(nb::object item)
{
    nb::object content = Field(item, "content");
    if (IsStr(content))
    {
        return TextOf(content);
    }
    std::string out;
    if (IsList(content))
    {
        for (nb::handle part : content)
        {
            std::string ptype = TextOf(Field(part, "type"));
            if (ptype == "output_text" || ptype == "text")
            {
                out += TextOf(Field(part, "text"));
            }
            else if (ptype == "refusal")
            {
                std::string r = TextOf(Field(part, "refusal"));
                if (!r.empty())
                {
                    if (!out.empty())
                    {
                        out += "\n";
                    }
                    out += r;
                }
            }
        }
    }
    return out;
}

/**
 * @brief Build the canonical response namespace from Responses output items, status, and usage.
 */
nb::object BuildResponsesResult(nb::object output_items, const std::string& status, const std::string& incomplete_reason, nb::object usage)
{
    if (status == "failed" || status == "cancelled")
    {
        throw ProtocolError("Responses API ended with status=" + status + ".");
    }
    bool truncated = (status == "incomplete");
    if (truncated)
    {
        if (!incomplete_reason.empty() && incomplete_reason != "max_output_tokens" && incomplete_reason != "max_tokens")
        {
            throw ProtocolError("Responses API incomplete (reason=" + incomplete_reason + ").");
        }
    }
    else if (status != "completed")
    {
        throw ProtocolError("Responses API ended with non-terminal status: " + status);
    }

    nb::list content;
    bool has_calls = false;
    if (IsList(output_items))
    {
        for (nb::handle item : output_items)
        {
            std::string itype = TextOf(Field(item, "type"));
            if (itype == "reasoning")
            {
                nb::object enc = Field(item, "encrypted_content");
                nb::object summary = Field(item, "summary");
                if (!enc.is_none() || (IsList(summary) && nb::len(summary) > 0))
                {
                    nb::dict block;
                    block["type"] = "openai_reasoning";
                    if (!enc.is_none())
                    {
                        block["encrypted_content"] = enc;
                    }
                    block["summary"] = IsList(summary) ? summary : nb::object(nb::list());
                    content.append(MakeBlock(block));
                }
            }
            else if (itype == "message")
            {
                std::string text = ExtractMessageText(nb::borrow<nb::object>(item));
                if (!text.empty())
                {
                    nb::dict block;
                    block["type"] = "text";
                    block["text"] = SafeUtf8Str(text);
                    content.append(MakeBlock(block));
                }
            }
            else if (itype == "function_call")
            {
                if (truncated)
                {
                    continue;
                }
                std::string call_id = TextOf(Field(item, "call_id"));
                std::string name = TextOf(Field(item, "name"));
                std::string raw_args = TextOf(Field(item, "arguments"));
                if (call_id.empty() || name.empty())
                {
                    throw ProtocolError("Incomplete Responses function_call (missing call_id or name).");
                }
                nb::object args = raw_args.empty() ? nb::object(nb::dict()) : JsonLoads(raw_args);
                if (!IsDict(args))
                {
                    throw ProtocolError("Responses function_call arguments must decode to a JSON object.");
                }
                nb::dict block;
                block["type"] = "tool_use";
                block["id"] = call_id;
                block["name"] = name;
                block["input"] = args;
                content.append(MakeBlock(block));
                has_calls = true;
            }
        }
    }

    std::string stop = has_calls ? "tool_use" : (truncated ? "max_tokens" : "end_turn");
    return MakeResponse(content, stop, MapResponsesUsage(usage));
}

/**
 * @brief Incremental semantic SSE decoder for OpenAI Responses streams.
 */
class ResponsesStream : public StreamDecoder
{
public:
    using StreamDecoder::StreamDecoder;

    /**
     * @brief Consume one Responses SSE event.
     */
    void OnEvent(const SseEvent& ev) override
    {
        if (ev.data.empty() || Trim(ev.data) == "[DONE]")
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
        if (type == "response.created" || type == "response.in_progress")
        {
            m_started = true;
            return;
        }
        if (type == "response.output_item.added")
        {
            long idx = ReadOutputIndex(d);
            if (m_items.count(idx))
            {
                throw ProtocolError("Duplicate Responses output_index.");
            }
            m_open_items.insert(idx);
            nb::dict item = CopyDict(Field(d, "item"));
            m_items[idx] = item;
            if (TextOf(Field(item, "type")) == "function_call")
            {
                m_fc_args[idx] = TextOf(Field(item, "arguments"));
            }
            return;
        }
        if (type == "response.output_text.delta")
        {
            long idx = ReadOutputIndex(d);
            if (!m_open_items.count(idx))
            {
                throw ProtocolError("Responses output_text.delta for closed output item.");
            }
            std::string delta = TextOf(Field(d, "delta"));
            if (!delta.empty())
            {
                long cidx = ReadIntField(d, "content_index", 0);
                m_text_parts[idx][cidx] += delta;
                Emit(delta);
            }
            return;
        }
        if (type == "response.reasoning_summary_text.delta")
        {
            long idx = ReadOutputIndex(d);
            if (!m_open_items.count(idx))
            {
                throw ProtocolError("Responses reasoning_summary_text.delta for closed output item.");
            }
            std::string delta = TextOf(Field(d, "delta"));
            if (!delta.empty())
            {
                long sidx = ReadIntField(d, "summary_index", 0);
                m_summary_parts[idx][sidx] += delta;
            }
            return;
        }
        if (type == "response.function_call_arguments.delta")
        {
            long idx = ReadOutputIndex(d);
            if (!m_open_items.count(idx))
            {
                throw ProtocolError("Responses function_call_arguments.delta for closed output item.");
            }
            std::string delta = TextOf(Field(d, "delta"));
            if (!delta.empty())
            {
                m_fc_args[idx] += delta;
                Emit(delta, true);
            }
            return;
        }
        if (type == "response.function_call_arguments.done")
        {
            long idx = ReadOutputIndex(d);
            if (m_open_items.count(idx))
            {
                nb::object full = Field(d, "arguments");
                if (IsStr(full))
                {
                    m_fc_args[idx] = TextOf(full);
                }
            }
            return;
        }
        if (type == "response.output_item.done")
        {
            long idx = ReadOutputIndex(d);
            if (!m_open_items.erase(idx))
            {
                throw ProtocolError("Responses output_item.done for unopened output item.");
            }
            nb::dict done_item = CopyDict(Field(d, "item"));
            if (TextOf(Field(done_item, "type")) == "function_call" && TextOf(Field(done_item, "arguments")).empty() && !m_fc_args[idx].empty())
            {
                done_item["arguments"] = SafeUtf8Str(m_fc_args[idx]);
            }
            m_items[idx] = done_item;
            return;
        }
        if (type == "response.completed" || type == "response.incomplete")
        {
            nb::object resp = Field(d, "response");
            if (resp.is_none())
            {
                resp = d;
            }
            m_status = TextOf(Field(resp, "status"));
            if (m_status.empty())
            {
                m_status = (type == "response.completed") ? "completed" : "incomplete";
            }
            m_incomplete_reason = TextOf(Field(Field(resp, "incomplete_details"), "reason"));
            if (!Field(resp, "usage").is_none())
            {
                m_usage = Field(resp, "usage");
            }
            nb::object output = Field(resp, "output");
            if (IsList(output) && nb::len(output) > 0)
            {
                m_final_output = output;
            }
            m_terminal = true;
            return;
        }
        if (type == "response.failed")
        {
            nb::object resp = Field(d, "response");
            nb::object err = Field(resp.is_none() ? nb::handle(d) : nb::handle(resp), "error");
            throw ProtocolError("Response failed: " + JsonDumps(err.is_none() ? resp : err));
        }
    }

    /**
     * @brief Validate terminal boundary and assemble streamed items with empty-list fallback.
     */
    nb::object Finish() override
    {
        if (!m_terminal || (!m_open_items.empty() && m_status != "incomplete"))
        {
            throw ProtocolError("Incomplete Responses stream (missing output_item.done or response.completed).");
        }
        if (!m_final_output.is_none())
        {
            return BuildResponsesResult(m_final_output, m_status, m_incomplete_reason, m_usage);
        }
        nb::list assembled;
        for (auto& kv : m_items)
        {
            long idx = kv.first;
            nb::dict item = kv.second;
            std::string itype = TextOf(Field(item, "type"));
            if (itype == "message")
            {
                nb::object cur_content = Field(item, "content");
                if ((!IsList(cur_content) || nb::len(cur_content) == 0) && m_text_parts.count(idx))
                {
                    nb::list parts;
                    for (const auto& tp : m_text_parts.at(idx))
                    {
                        nb::dict part;
                        part["type"] = "output_text";
                        part["text"] = SafeUtf8Str(tp.second);
                        parts.append(part);
                    }
                    item["content"] = parts;
                }
            }
            else if (itype == "reasoning")
            {
                nb::object cur_summary = Field(item, "summary");
                if ((!IsList(cur_summary) || nb::len(cur_summary) == 0) && m_summary_parts.count(idx))
                {
                    nb::list sums;
                    for (const auto& sp : m_summary_parts.at(idx))
                    {
                        nb::dict part;
                        part["type"] = "summary_text";
                        part["text"] = SafeUtf8Str(sp.second);
                        sums.append(part);
                    }
                    item["summary"] = sums;
                }
            }
            else if (itype == "function_call" && TextOf(Field(item, "arguments")).empty() && m_fc_args.count(idx))
            {
                item["arguments"] = SafeUtf8Str(m_fc_args.at(idx));
            }
            assembled.append(item);
        }
        return BuildResponsesResult(assembled, m_status, m_incomplete_reason, m_usage);
    }

private:
    /**
     * @brief Read an integer field with a default fallback.
     */
    static long ReadIntField(nb::handle d, const char* key, long fallback)
    {
        nb::object raw = Field(d, key);
        return IsInt(raw) ? nb::cast<long>(raw) : fallback;
    }

    /**
     * @brief Read and validate a non-negative output_index field.
     */
    static long ReadOutputIndex(nb::handle d)
    {
        nb::object raw = Field(d, "output_index");
        if (!IsInt(raw))
        {
            throw ProtocolError("Missing Responses output_index.");
        }
        long v = nb::cast<long>(raw);
        if (v < 0)
        {
            throw ProtocolError("Negative Responses output_index.");
        }
        return v;
    }

    bool m_started = false;
    bool m_terminal = false;
    std::string m_status;
    std::string m_incomplete_reason;
    std::set<long> m_open_items;
    std::map<long, nb::dict> m_items;
    std::map<long, std::string> m_fc_args;
    std::map<long, std::map<long, std::string>> m_text_parts;
    std::map<long, std::map<long, std::string>> m_summary_parts;
    nb::object m_usage = nb::none();
    nb::object m_final_output = nb::none();
};

/**
 * @brief Stateless OpenAI Responses API codec (store=false) with Sub2API cross-protocol compat support.
 */
class ResponsesCodec : public Codec
{
public:
    /**
     * @brief Construct codec and resolve terminal flavor.
     */
    explicit ResponsesCodec(const ProviderConfig& c)
        : Codec(c)
    {
        m_flavor = ResolveFlavor(c.compat, c.model_id, c.base_url, "openai");
    }

    const char* Protocol() const override
    {
        return "responses";
    }

    std::string Flavor() const override
    {
        return m_flavor;
    }

    /**
     * @brief Build a stateless /v1/responses request payload.
     */
    Built Build(nb::dict payload, bool stream) override
    {
        nb::dict body;
        body["model"] = m_cfg.model_id;

        std::string instructions = SystemText(payload.get("system", nb::str("")));
        if (!instructions.empty())
        {
            body["instructions"] = SafeUtf8Str(instructions);
        }

        body["input"] = ConvertInput(payload.get("messages", nb::list()));

        nb::object cap = payload.get("max_tokens", nb::none());
        if (IsInt(cap))
        {
            body["max_output_tokens"] = cap;
        }
        if (DictHas(payload, "temperature"))
        {
            body["temperature"] = payload["temperature"];
        }

        nb::list tools = ConvertTools(payload.get("tools", nb::none()));
        if (nb::len(tools) > 0)
        {
            body["tools"] = tools;
        }

        InjectEffort(body);

        body["store"] = false;
        if (stream)
        {
            body["stream"] = true;
        }

        std::string base = m_cfg.base_url.empty() ? EnvOr("OPENAI_BASE_URL", "https://api.openai.com/v1") : m_cfg.base_url;
        base = RstripSlash(base);

        Built b;
        b.url = EndsWith(base, "/responses") ? base : (base + "/responses");
        b.headers = {
            {"Authorization", "Bearer " + m_cfg.api_key},
            {"content-type", "application/json"},
            {"accept", stream ? "text/event-stream" : "application/json"},
        };
        b.body = body;
        return b;
    }

    /**
     * @brief Parse a non-streaming Responses JSON body.
     */
    nb::object Parse(nb::object resp) override
    {
        if (!Field(resp, "error").is_none())
        {
            throw ProtocolError("Response error: " + JsonDumps(Field(resp, "error")));
        }
        std::string status = TextOf(Field(resp, "status"));
        std::string incomplete_reason = TextOf(Field(Field(resp, "incomplete_details"), "reason"));
        return BuildResponsesResult(Field(resp, "output"), status, incomplete_reason, Field(resp, "usage"));
    }

    std::unique_ptr<StreamDecoder> Decoder(bool echo) override
    {
        return std::make_unique<ResponsesStream>(echo);
    }

private:
    /**
     * @brief Inject Responses reasoning/include parameters and Sub2API cross-protocol bridge knobs.
     */
    void InjectEffort(nb::dict body)
    {
        for (const char* key : kOwnedKeys)
        {
            DictDelIfPresent(body, key);
        }

        if (m_cfg.thinking == "enabled")
        {
            nb::object effort;
            if (m_flavor == "gemini")
            {
                std::string glevel = ResolveGeminiLevel(m_cfg.effort, m_cfg.model_id);
                effort = nb::str(glevel.data(), glevel.size());
            }
            else if (m_flavor == "deepseek")
            {
                effort = ResolveEffort("EFFORT_TO_DEEPSEEK_EFFORT", m_cfg.effort, m_cfg.model_id);
            }
            else
            {
                effort = ResolveEffort("EFFORT_TO_RESPONSES_EFFORT", m_cfg.effort, m_cfg.model_id);
            }
            nb::dict reasoning;
            reasoning["effort"] = effort;
            reasoning["summary"] = "auto";
            body["reasoning"] = reasoning;

            nb::list include;
            include.append("reasoning.encrypted_content");
            body["include"] = include;
        }

        if (m_flavor == "gemini")
        {
            std::string level = ResolveGeminiLevel(m_cfg.effort, m_cfg.model_id);
            body["thinking_level"] = level;
            body["thinkingLevel"] = level;
        }
        else if (m_flavor == "deepseek")
        {
            nb::dict oc;
            oc["effort"] = ResolveEffort("EFFORT_TO_DEEPSEEK_EFFORT", m_cfg.effort, m_cfg.model_id);
            body["output_config"] = oc;
        }
        else if (m_flavor == "anthropic-effort")
        {
            nb::dict oc;
            oc["effort"] = ResolveEffort("EFFORT_TO_ANTHROPIC_EFFORT", m_cfg.effort, m_cfg.model_id);
            body["output_config"] = oc;
        }
    }

    /**
     * @brief Translate canonical tool definitions into flat Responses function tool declarations.
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
            nb::dict entry;
            entry["type"] = "function";
            entry["name"] = Field(t, "name");
            entry["description"] = TextOf(Field(t, "description"));
            entry["parameters"] = schema;
            entry["strict"] = false;
            out.append(entry);
        }
        return out;
    }

    /**
     * @brief Translate canonical messages into stateless Responses input[] items (omitting server IDs).
     */
    static nb::list ConvertInput(nb::object messages)
    {
        nb::list items;
        if (!IsList(messages))
        {
            return items;
        }
        for (nb::handle msg : messages)
        {
            std::string role = TextOf(Field(msg, "role"));
            bool is_user = (role != "assistant");
            nb::object content = Field(msg, "content");

            if (IsStr(content))
            {
                std::string s = TextOf(content);
                if (s.empty())
                {
                    continue;
                }
                nb::dict part;
                part["type"] = is_user ? "input_text" : "output_text";
                part["text"] = SafeUtf8Str(s);
                nb::list parts;
                parts.append(part);

                nb::dict m;
                m["type"] = "message";
                m["role"] = is_user ? "user" : "assistant";
                m["content"] = parts;
                items.append(m);
                continue;
            }
            if (!IsList(content))
            {
                continue;
            }

            nb::list msg_parts;
            auto flush_message = [&]() {
                if (nb::len(msg_parts) > 0)
                {
                    nb::dict m;
                    m["type"] = "message";
                    m["role"] = is_user ? "user" : "assistant";
                    m["content"] = msg_parts;
                    items.append(m);
                    msg_parts = nb::list();
                }
            };

            for (nb::handle block : content)
            {
                std::string btype = TextOf(Field(block, "type"));
                if (btype == "text")
                {
                    std::string t = TextOf(Field(block, "text"));
                    if (!t.empty())
                    {
                        nb::dict part;
                        part["type"] = is_user ? "input_text" : "output_text";
                        part["text"] = SafeUtf8Str(t);
                        msg_parts.append(part);
                    }
                }
                else if (is_user && btype == "image")
                {
                    nb::object src = Field(block, "source");
                    std::string mime = IsDict(src) ? TextOf(Field(src, "media_type")) : "";
                    std::string data = IsDict(src) ? TextOf(Field(src, "data")) : "";
                    if (!data.empty())
                    {
                        std::string url = "data:" + (mime.empty() ? std::string("image/png") : mime) + ";base64," + data;
                        nb::dict part;
                        part["type"] = "input_image";
                        part["image_url"] = url;
                        part["detail"] = "auto";
                        msg_parts.append(part);
                    }
                }
                else if (is_user && btype == "document")
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
                        std::string fdata = "data:" + (mime.empty() ? std::string("application/pdf") : mime) + ";base64," + data;
                        nb::dict part;
                        part["type"] = "input_file";
                        part["filename"] = fname;
                        part["file_data"] = fdata;
                        msg_parts.append(part);
                    }
                }
                else if (is_user && btype == "tool_result")
                {
                    flush_message();
                    nb::dict out_item;
                    out_item["type"] = "function_call_output";
                    out_item["call_id"] = TextOf(Field(block, "tool_use_id"));
                    out_item["output"] = SafeUtf8Str(ResultText(Field(block, "content")));
                    items.append(out_item);
                }
                else if (!is_user && btype == "openai_reasoning")
                {
                    std::string enc = TextOf(Field(block, "encrypted_content"));
                    if (!enc.empty())
                    {
                        flush_message();
                        nb::object summary = Field(block, "summary");
                        nb::dict r_item;
                        r_item["type"] = "reasoning";
                        r_item["summary"] = IsList(summary) ? summary : nb::object(nb::list());
                        r_item["encrypted_content"] = enc;
                        items.append(r_item);
                    }
                }
                else if (!is_user && btype == "tool_use")
                {
                    flush_message();
                    nb::object input = Field(block, "input");
                    nb::dict fc_item;
                    fc_item["type"] = "function_call";
                    fc_item["call_id"] = TextOf(Field(block, "id"));
                    fc_item["name"] = TextOf(Field(block, "name"));
                    fc_item["arguments"] = JsonDumpsPlain(input.is_none() ? nb::object(nb::dict()) : input);
                    items.append(fc_item);
                }
            }
            flush_message();
        }
        return items;
    }

    std::string m_flavor;
};

} // namespace

std::unique_ptr<Codec> MakeResponsesCodec(const ProviderConfig& cfg)
{
    return std::make_unique<ResponsesCodec>(cfg);
}

} // namespace dandelion

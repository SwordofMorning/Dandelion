/**
 * @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/native/provider.cpp
 * @author Xiaojintao
 * @brief Shared request runner: logging, transient retries, GIL release, cooperative cancellation, and error safety.
 * @version 0.1
 * @date 2026-11-28
 * @copyright Copyright (c) 2026
 */

#include "provider.hpp"

#include "http.hpp"

#include <algorithm>
#include <chrono>
#include <thread>

namespace dandelion
{
namespace
{

constexpr int kMaxRetries = 2;
constexpr const char* kCancelledMessage = "Request cancelled by runtime interrupt.";

/**
 * @brief Determine whether a failed HTTP attempt is safe and worthwhile to retry.
 */
bool IsRetryable(const HttpResult& r)
{
    if (r.aborted || r.delivered || r.non_retryable || r.timed_out)
    {
        return false;
    }
    if (!r.error.empty())
    {
        return true;
    }
    return r.status == 408 || r.status == 409 || r.status == 429 || r.status >= 500;
}

/**
 * @brief Check both Python signals (on main thread only) and the optional cancelled() callback.
 */
bool PollCancelled(bool is_main_thread, nb::object& cancel_cb, std::unique_ptr<nb::python_error>& py_error, bool& user_cancelled)
{
    if (!is_main_thread && cancel_cb.is_none())
    {
        return false;
    }
    nb::gil_scoped_acquire gil;
    if (is_main_thread && PyErr_CheckSignals() != 0)
    {
        py_error = std::make_unique<nb::python_error>();
        return true;
    }
    if (!cancel_cb.is_none())
    {
        try
        {
            if (nb::bool_(cancel_cb()))
            {
                user_cancelled = true;
                return true;
            }
        }
        catch (nb::python_error& e)
        {
            py_error = std::make_unique<nb::python_error>(std::move(e));
            return true;
        }
    }
    return false;
}

/**
 * @brief Sleep between retries in 75ms increments while polling signals and cooperative cancellation.
 *
 * @return true if cancelled by user callback, false if backoff completed normally.
 */
bool BackoffSleep(const HttpResult& r, int attempt, bool is_main_thread, nb::object& cancel_cb)
{
    int shift = std::min(std::max(attempt, 0), 10);
    double exp_wait = 0.5 * static_cast<double>(1u << shift);
    double wait = (r.retry_after > 0 && r.retry_after <= 60) ? r.retry_after : std::min(exp_wait, 8.0);
    auto until = std::chrono::steady_clock::now() + std::chrono::milliseconds(static_cast<long long>(wait * 1000));
    while (std::chrono::steady_clock::now() < until)
    {
        {
            nb::gil_scoped_release nogil;
            auto remaining = until - std::chrono::steady_clock::now();
            auto step = std::chrono::milliseconds(75);
            if (remaining > std::chrono::milliseconds(0))
            {
                std::this_thread::sleep_for(remaining < step ? remaining : step);
            }
        }
        if (is_main_thread && PyErr_CheckSignals() != 0)
        {
            throw nb::python_error();
        }
        if (!cancel_cb.is_none() && nb::bool_(cancel_cb()))
        {
            return true;
        }
    }
    return false;
}

/**
 * @brief Close an open streaming echo line without propagating BrokenPipeError or OSError.
 */
void SafeCloseEcho(bool& echo_open)
{
    if (!echo_open)
    {
        return;
    }
    echo_open = false;
    try
    {
        Echo("\n");
    }
    catch (nb::python_error&)
    {
    }
}

/**
 * @brief Construct the concrete protocol Codec for sdk_type.
 */
std::unique_ptr<Codec> MakeCodec(const std::string& sdk_type, const ProviderConfig& cfg)
{
    std::string s = Lower(Trim(sdk_type));
    if (s == "message" || s == "messages" || s == "anthropic")
    {
        return MakeMessagesCodec(cfg);
    }
    if (s == "chat completions" || s == "chat completion" || s == "chat_completions" || s == "openai" || s == "nvidia")
    {
        return MakeChatCompletionsCodec(cfg);
    }
    if (s == "responses" || s == "response" || s == "openai responses" || s == "openai_responses")
    {
        return MakeResponsesCodec(cfg);
    }
    if (s == "generate content" || s == "generatecontent" || s == "generate_content" || s == "ai studio" || s == "gemini" || s == "google")
    {
        return MakeGenerateContentCodec(cfg);
    }
    if (s == "interactions" || s == "interaction")
    {
        return MakeInteractionsCodec(cfg);
    }
    std::string msg = "Unsupported SDK_TYPE: '" + sdk_type +
        "'. Supported types: 'Message' (Anthropic), 'Chat Completions' (OpenAI), "
        "'Responses' (OpenAI), 'Generate Content' (Gemini), 'Interactions' (Gemini).";
    throw nb::value_error(msg.c_str());
}

/**
 * @brief Append non-empty canonical blocks extracted from content directly onto target list.
 */
void AppendBlocks(nb::list& target, nb::handle content)
{
    if (IsStr(content))
    {
        std::string text = TextOf(content);
        if (!text.empty())
        {
            nb::dict block;
            block["type"] = "text";
            block["text"] = SafeUtf8Str(text);
            target.append(block);
        }
    }
    else if (IsList(content))
    {
        for (nb::handle item : content)
        {
            if (IsDict(item) && TextOf(Field(item, "type")) == "text" && TextOf(Field(item, "text")).empty() && Field(item, "thought_signature").is_none())
            {
                continue;
            }
            target.append(item);
        }
    }
}

/**
 * @brief Replace embedded NUL bytes so std::runtime_error::what() never truncates binary error bodies.
 */
std::string SanitizeNul(const std::string& raw)
{
    if (raw.find('\0') == std::string::npos)
    {
        return raw;
    }
    std::string out;
    out.reserve(raw.size());
    for (char c : raw)
    {
        if (c == '\0')
        {
            out += "\xEF\xBF\xBD";
        }
        else
        {
            out.push_back(c);
        }
    }
    return out;
}

/**
 * @brief Check whether a trimmed body string looks like a non-SSE JSON object or error envelope.
 */
bool LooksLikeJsonBody(const std::string& raw, const std::string& content_type)
{
    std::string ctype = Lower(content_type);
    if (StartsWith(ctype, "application/json") || Contains(ctype, "+json"))
    {
        return true;
    }
    std::string trimmed = Trim(raw);
    return !trimmed.empty() && (trimmed.front() == '{' || trimmed.front() == '[');
}

/**
 * @brief Format a Python exception as a guaranteed non-empty diagnostic string.
 */
std::string FormatPythonError(const nb::python_error& e)
{
    std::string msg = TextOf(e.value());
    if (!msg.empty())
    {
        return msg;
    }
    nb::object type_name = nb::getattr(e.type(), "__name__", nb::str("PythonError"));
    std::string name = TextOf(type_name);
    return name.empty() ? std::string("PythonError") : name;
}

} // namespace

Provider::Provider(const std::string& sdk_type,
                   const std::string& api_key,
                   const std::string& base_url,
                   const std::string& model_id,
                   const std::string& thinking,
                   const std::string& effort,
                   const std::string& compat)
{
    std::string clean_key = Trim(api_key);
    if (HasHeaderControlChars(clean_key))
    {
        throw nb::value_error("API key must not contain CR, LF, NUL, or control characters.");
    }
    ProviderConfig cfg{clean_key, Trim(base_url), Trim(model_id), Lower(Trim(thinking)), Lower(Trim(effort)), Trim(compat)};
    m_codec = MakeCodec(sdk_type, cfg);
}

nb::tuple Provider::SafeRequest(nb::dict payload, nb::object logger, const std::string& log_tag, nb::object cancelled)
{
    return Run(payload, logger, log_tag, false, false, cancelled);
}

nb::tuple Provider::SafeStreamRequest(nb::dict payload, nb::object logger, const std::string& log_tag, bool echo, nb::object cancelled)
{
    return Run(payload, logger, log_tag, true, echo, cancelled);
}

nb::tuple Provider::Run(nb::dict payload, nb::object logger, const std::string& log_tag, bool force_stream, bool echo, nb::object cancelled)
{
    bool echo_open = false;
    std::unique_ptr<StreamDecoder> dec;

    auto failure = [&](nb::str message) {
        nb::object error = message;
        if (dec && dec->HasOutput())
        {
            error = MakeBlocksError("PartialStreamError", message);
        }
        return MakePairTuple(nb::none(), error);
    };

    auto cancelled_failure = [&]() {
        SafeCloseEcho(echo_open);
        return MakePairTuple(nb::none(), MakeBlocksError("RequestCancelledError", SafeUtf8Str(kCancelledMessage)));
    };

    try
    {
        const bool is_main_thread = IsMainThread();
        if (!cancelled.is_none() && nb::bool_(cancelled()))
        {
            return cancelled_failure();
        }

        const bool stream = force_stream || m_codec->ForceStream(payload);
        Built built = m_codec->Build(payload, stream);
        if (!logger.is_none() && !log_tag.empty())
        {
            logger.attr("log_api_call")(log_tag, built.body);
        }

        HttpRequest req{built.url, built.headers, JsonDumps(built.body)};
        if (stream && echo)
        {
            Echo("\n[Agent] ");
            echo_open = true;
        }

        for (int attempt = 0;; ++attempt)
        {
            if (!cancelled.is_none() && nb::bool_(cancelled()))
            {
                return cancelled_failure();
            }

            dec = stream ? m_codec->Decoder(echo) : nullptr;
            SseParser sse;
            std::unique_ptr<nb::python_error> py_error;
            bool has_decode_error = false;
            std::string decode_error;
            bool user_cancelled = false;

            auto on_event = [&](const SseEvent& ev) {
                dec->OnEvent(ev);
            };

            auto check_cancelled = [&]() -> bool {
                return PollCancelled(is_main_thread, cancelled, py_error, user_cancelled);
            };

            DataSink sink;
            if (stream)
            {
                sink = [&](const char* p, size_t n) -> bool {
                    nb::gil_scoped_acquire gil;
                    try
                    {
                        if (is_main_thread && PyErr_CheckSignals() != 0)
                        {
                            throw nb::python_error();
                        }
                        if (!cancelled.is_none() && nb::bool_(cancelled()))
                        {
                            user_cancelled = true;
                            return false;
                        }
                        sse.Feed(p, n, on_event);
                        return true;
                    }
                    catch (nb::python_error& e)
                    {
                        py_error = std::make_unique<nb::python_error>(std::move(e));
                    }
                    catch (const std::exception& e)
                    {
                        has_decode_error = true;
                        const char* what = e.what();
                        decode_error = (what && *what) ? what : "Unknown stream decode error.";
                    }
                    return false;
                };
            }

            HttpResult res;
            {
                nb::gil_scoped_release nogil;
                res = HttpPost(req, sink, check_cancelled);
            }

            if (py_error)
            {
                throw std::move(*py_error);
            }
            if (user_cancelled)
            {
                return cancelled_failure();
            }
            if (has_decode_error)
            {
                throw ProtocolError(decode_error.empty() ? std::string("Unknown stream decode error.") : decode_error);
            }
            if (IsRetryable(res) && attempt < kMaxRetries)
            {
                if (BackoffSleep(res, attempt, is_main_thread, cancelled))
                {
                    return cancelled_failure();
                }
                continue;
            }
            if (!res.error.empty())
            {
                throw ProtocolError(res.error);
            }
            if (res.status < 200 || res.status >= 300)
            {
                throw ProtocolError("Error code: " + std::to_string(res.status) + " - " + SanitizeNul(res.body));
            }

            nb::object resp;
            if (stream)
            {
                sse.Finish(on_event);
                if (!sse.SawEvents() && !sse.RawPrefix().empty() && LooksLikeJsonBody(sse.RawPrefix(), res.content_type))
                {
                    throw ProtocolError("Error code: " + std::to_string(res.status) + " - " + SanitizeNul(sse.RawPrefix()));
                }
                resp = dec->Finish();
            }
            else
            {
                resp = m_codec->Parse(JsonLoads(res.body));
            }
            SafeCloseEcho(echo_open);
            return MakePairTuple(resp, nb::none());
        }
    }
    catch (nb::python_error& e)
    {
        SafeCloseEcho(echo_open);
        if (!e.matches(PyExc_Exception))
        {
            throw;
        }
        return failure(SafeUtf8Str(FormatPythonError(e)));
    }
    catch (const std::exception& e)
    {
        SafeCloseEcho(echo_open);
        const char* what = e.what();
        return failure(SafeUtf8Str((what && *what) ? what : "Native provider error."));
    }
}

nb::object Provider::ExtractText(nb::object content) const
{
    if (content.is_none())
    {
        return nb::str("");
    }
    if (IsStr(content))
    {
        return SafeUtf8Str(TextOf(content));
    }
    if (!IsList(content))
    {
        return SafeUtf8Str(TextOf(content));
    }
    std::string out;
    bool first = true;
    for (nb::handle block : content)
    {
        if (TextOf(Field(block, "type")) != "text")
        {
            continue;
        }
        if (!first)
        {
            out += "\n";
        }
        out += TextOf(Field(block, "text"));
        first = false;
    }
    return SafeUtf8Str(out);
}

nb::list NormalizeMessages(nb::object messages)
{
    nb::list merged;
    if (messages.is_none())
    {
        return merged;
    }

    nb::list prev_blocks;
    bool prev_has_owned_list = false;

    for (nb::handle msg : messages)
    {
        size_t n = nb::len(merged);
        if (n > 0)
        {
            nb::object prev = merged[n - 1];
            if (Field(prev, "role").equal(Field(msg, "role")))
            {
                if (!prev_has_owned_list)
                {
                    prev_blocks = nb::list();
                    AppendBlocks(prev_blocks, Field(prev, "content"));
                    prev_has_owned_list = true;
                }
                AppendBlocks(prev_blocks, Field(msg, "content"));
                if (nb::len(prev_blocks) > 0)
                {
                    prev["content"] = prev_blocks;
                }
                else
                {
                    prev["content"] = nb::str("");
                }
                continue;
            }
        }
        merged.append(CopyDict(msg));
        prev_has_owned_list = false;
    }
    return merged;
}

} // namespace dandelion

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
#include <cmath>
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
    double wait = (r.retry_after > 0 && r.retry_after <= 60) ? r.retry_after : std::min(0.5 * std::pow(2.0, attempt), 8.0);
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
 * @brief Convert string or list message content into a list of canonical content blocks.
 */
nb::list AsBlocks(nb::handle content)
{
    nb::list out;
    if (IsStr(content))
    {
        std::string text = TextOf(content);
        if (!text.empty())
        {
            nb::dict block;
            block["type"] = "text";
            block["text"] = content;
            out.append(block);
        }
    }
    else if (IsList(content))
    {
        for (nb::handle item : content)
        {
            out.append(item);
        }
    }
    return out;
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
        throw nb::value_error("API key must not contain CR, LF, or NUL control characters.");
    }
    ProviderConfig cfg{clean_key, Trim(base_url), Trim(model_id), Lower(Trim(thinking)), Lower(Trim(effort)), Trim(compat)};
    m_codec = MakeCodec(sdk_type, cfg);
}

nb::tuple Provider::SafeRequest(nb::dict payload, nb::object logger, const std::string& log_tag, nb::object cancelled)
{
    return Run(payload, logger, log_tag, m_codec->ForceStream(payload), false, cancelled);
}

nb::tuple Provider::SafeStreamRequest(nb::dict payload, nb::object logger, const std::string& log_tag, bool echo, nb::object cancelled)
{
    return Run(payload, logger, log_tag, true, echo, cancelled);
}

nb::tuple Provider::Run(nb::dict payload, nb::object logger, const std::string& log_tag, bool stream, bool echo, nb::object cancelled)
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
                        decode_error = e.what();
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
            if (!decode_error.empty())
            {
                throw ProtocolError(decode_error);
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
        return failure(SafeUtf8Str(TextOf(e.value())));
    }
    catch (const std::exception& e)
    {
        SafeCloseEcho(echo_open);
        return failure(SafeUtf8Str(e.what()));
    }
}

nb::object Provider::ExtractText(nb::object content) const
{
    if (IsStr(content))
    {
        return content;
    }
    if (!IsList(content))
    {
        return nb::str(content);
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
    for (nb::handle msg : messages)
    {
        size_t n = nb::len(merged);
        if (n > 0)
        {
            nb::object prev = merged[n - 1];
            if (Field(prev, "role").equal(Field(msg, "role")))
            {
                nb::object prev_content = Field(prev, "content");
                nb::object next_content = Field(msg, "content");
                nb::list joined = AsBlocks(prev_content);
                for (nb::handle item : AsBlocks(next_content))
                {
                    joined.append(item);
                }
                prev["content"] = joined;
                continue;
            }
        }
        merged.append(CopyDict(msg));
    }
    return merged;
}

} // namespace dandelion

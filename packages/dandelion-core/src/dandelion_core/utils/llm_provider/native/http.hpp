#pragma once

/**
 * @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/native/http.hpp
 * @author Xiaojintao
 * @brief Minimal blocking HTTPS POST transport interface (libcurl on POSIX, WinHTTP on Windows).
 * @version 0.1
 * @date 2026-11-28
 * @copyright Copyright (c) 2026
 */

#include <cstddef>
#include <functional>
#include <string>
#include <utility>
#include <vector>

namespace dandelion
{

/**
 * @brief Outgoing HTTP POST request parameters.
 */
struct HttpRequest
{
    std::string url;
    std::vector<std::pair<std::string, std::string>> headers;
    std::string body;
};

/**
 * @brief Completed HTTP POST transfer status, response metadata, and diagnostic fields.
 */
struct HttpResult
{
    long status = 0;
    std::string body;
    std::string content_type;
    std::string error;
    double retry_after = -1;
    bool delivered = false;
    bool aborted = false;
    bool timed_out = false;
    bool non_retryable = false;
};

/**
 * @brief Callback receiving 2xx body chunks; return false to abort the active transfer.
 */
using DataSink = std::function<bool(const char*, std::size_t)>;

/**
 * @brief Cooperative cancellation predicate polled periodically during blocking I/O.
 */
using CheckCancelled = std::function<bool()>;

/**
 * @brief Initialize the platform HTTP stack once in a thread-safe manner.
 */
void HttpInit();

/**
 * @brief Register an optional fallback TLS CA bundle path (e.g. certifi.where()) for POSIX builds.
 */
void HttpSetFallbackCaBundle(const std::string& path);

/**
 * @brief Execute one HTTPS POST request without holding the Python GIL.
 */
HttpResult HttpPost(const HttpRequest& req, const DataSink& sink, const CheckCancelled& cancelled);

} // namespace dandelion

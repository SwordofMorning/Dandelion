/**
 * @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/native/http_curl.cpp
 * @author Xiaojintao
 * @brief POSIX libcurl HTTPS transport with runtime CA probing, proxy bypass, and cooperative cancellation.
 * @version 0.1
 * @date 2026-11-28
 * @copyright Copyright (c) 2026
 */

#ifndef _WIN32

#include "http.hpp"

#include <curl/curl.h>

#include <arpa/inet.h>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <initializer_list>
#include <mutex>
#include <string>
#include <strings.h>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>

namespace dandelion
{
namespace
{

constexpr size_t kMaxErrorBodyBytes = 256 * 1024;

std::once_flag g_curl_init_once;
std::mutex g_ca_mutex;
std::string g_fallback_ca_bundle;

/**
 * @brief Per-transfer state passed into libcurl write and header callbacks.
 */
struct TransferContext
{
    const DataSink* sink;
    HttpResult* result;
    CURL* handle;
    bool error_body_truncated = false;
};

/**
 * @brief Resolved runtime TLS CA file and hashed CA directory paths.
 */
struct CaConfig
{
    std::string ca_file;
    std::string ca_path;
};

/**
 * @brief Trim ASCII whitespace from a string.
 */
std::string TrimAscii(const std::string& s)
{
    size_t b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos)
    {
        return "";
    }
    size_t e = s.find_last_not_of(" \t\r\n");
    return s.substr(b, e - b + 1);
}

/**
 * @brief Check whether a header name or value contains disallowed ASCII control characters.
 */
bool HasCtl(const std::string& s)
{
    for (char c : s)
    {
        unsigned char u = static_cast<unsigned char>(c);
        if (u == '\r' || u == '\n' || u == '\0' || u == 0x7F || (u < 0x20 && u != '\t'))
        {
            return true;
        }
    }
    return false;
}

/**
 * @brief Check whether a filesystem path points to a readable, non-empty regular file.
 */
bool FileReadable(const char* p)
{
    if (!p || !*p)
    {
        return false;
    }
    struct stat st;
    return ::stat(p, &st) == 0 && S_ISREG(st.st_mode) && st.st_size > 0 && ::access(p, R_OK) == 0;
}

/**
 * @brief Check whether a filesystem path points to a readable/traversable directory.
 */
bool DirReadable(const char* p)
{
    if (!p || !*p)
    {
        return false;
    }
    struct stat st;
    return ::stat(p, &st) == 0 && S_ISDIR(st.st_mode) && ::access(p, R_OK | X_OK) == 0;
}

/**
 * @brief Return true for deterministic libcurl TLS/certificate errors that must not be retried.
 */
bool IsDeterministicTlsError(CURLcode rc)
{
    if (rc == CURLE_SSL_CACERT_BADFILE || rc == CURLE_PEER_FAILED_VERIFICATION || rc == CURLE_SSL_CERTPROBLEM)
    {
        return true;
    }
#if defined(CURLE_SSL_CACERT) && (CURLE_SSL_CACERT != CURLE_PEER_FAILED_VERIFICATION)
    if (rc == CURLE_SSL_CACERT)
    {
        return true;
    }
#endif
    return false;
}

/**
 * @brief Resolve the runtime TLS CA bundle across Linux distributions and container environments.
 */
CaConfig ResolveCaConfig()
{
    CaConfig cfg;
    for (const char* env : {"SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE"})
    {
        const char* v = std::getenv(env);
        if (FileReadable(v))
        {
            cfg.ca_file = v;
            break;
        }
    }
    if (cfg.ca_file.empty())
    {
        static constexpr const char* kOsCaBundles[] = {
            "/etc/ssl/certs/ca-certificates.crt",
            "/etc/pki/tls/certs/ca-bundle.crt",
            "/etc/ssl/ca-bundle.pem",
            "/etc/pki/tls/cacert.pem",
            "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
            "/etc/ssl/cert.pem",
        };
        for (const char* path : kOsCaBundles)
        {
            if (FileReadable(path))
            {
                cfg.ca_file = path;
                break;
            }
        }
    }
    if (cfg.ca_file.empty())
    {
        std::lock_guard<std::mutex> lock(g_ca_mutex);
        if (FileReadable(g_fallback_ca_bundle.c_str()))
        {
            cfg.ca_file = g_fallback_ca_bundle;
        }
    }
    const char* env_dir = std::getenv("SSL_CERT_DIR");
    if (DirReadable(env_dir))
    {
        cfg.ca_path = env_dir;
    }
    else if (cfg.ca_file.empty() && DirReadable("/etc/ssl/certs"))
    {
        cfg.ca_path = "/etc/ssl/certs";
    }
    return cfg;
}

/**
 * @brief libcurl response body callback forwarding 2xx bytes to the DataSink with exception safety.
 */
size_t OnWrite(char* data, size_t size, size_t count, void* user)
{
    try
    {
        size_t len = size * count;
        if (len == 0)
        {
            return 0;
        }
        TransferContext* ctx = static_cast<TransferContext*>(user);
        long status = 0;
        curl_easy_getinfo(ctx->handle, CURLINFO_RESPONSE_CODE, &status);
        if (status >= 200 && status < 300)
        {
            if (*ctx->sink)
            {
                ctx->result->delivered = true;
                if (!(*ctx->sink)(data, len))
                {
                    ctx->result->aborted = true;
                    return 0;
                }
            }
            else
            {
                ctx->result->body.append(data, len);
            }
        }
        else
        {
            if (ctx->result->body.size() < kMaxErrorBodyBytes)
            {
                size_t room = kMaxErrorBodyBytes - ctx->result->body.size();
                size_t take = len < room ? len : room;
                ctx->result->body.append(data, take);
                if (take < len && !ctx->error_body_truncated)
                {
                    ctx->error_body_truncated = true;
                    ctx->result->body.append(" [truncated]");
                }
            }
            else if (!ctx->error_body_truncated)
            {
                ctx->error_body_truncated = true;
                ctx->result->body.append(" [truncated]");
            }
        }
        return len;
    }
    catch (...)
    {
        TransferContext* ctx = static_cast<TransferContext*>(user);
        if (ctx && ctx->result)
        {
            ctx->result->aborted = true;
            if (ctx->result->error.empty())
            {
                ctx->result->error = "Connection error: memory allocation failed while reading response.";
            }
        }
        return 0;
    }
}

/**
 * @brief libcurl response header callback extracting Retry-After and Content-Type with exception safety.
 */
size_t OnHeader(char* data, size_t size, size_t count, void* user)
{
    try
    {
        TransferContext* ctx = static_cast<TransferContext*>(user);
        size_t len = size * count;
        if (len >= 5 && strncasecmp(data, "HTTP/", 5) == 0)
        {
            // Reset per-response headers across 100-Continue or proxy CONNECT responses.
            ctx->result->retry_after = -1;
            ctx->result->content_type.clear();
            return len;
        }
        static const char kRetryAfter[] = "retry-after:";
        static const char kContentType[] = "content-type:";
        if (len > sizeof(kRetryAfter) - 1 && strncasecmp(data, kRetryAfter, sizeof(kRetryAfter) - 1) == 0)
        {
            std::string value(data + sizeof(kRetryAfter) - 1, len - (sizeof(kRetryAfter) - 1));
            double seconds = std::atof(value.c_str());
            ctx->result->retry_after = seconds > 0 ? seconds : -1;
        }
        else if (len > sizeof(kContentType) - 1 && strncasecmp(data, kContentType, sizeof(kContentType) - 1) == 0)
        {
            std::string value(data + sizeof(kContentType) - 1, len - (sizeof(kContentType) - 1));
            ctx->result->content_type = TrimAscii(value);
        }
        return len;
    }
    catch (...)
    {
        return 0;
    }
}

/**
 * @brief Extract a numeric 127.0.0.0/8 loopback host so older libcurl without CIDR support bypasses proxies.
 */
std::string Ipv4Loopback(const std::string& url)
{
    size_t scheme = url.find("://");
    if (scheme == std::string::npos)
    {
        return {};
    }
    size_t begin = scheme + 3;
    std::string authority = url.substr(begin, url.find_first_of("/?#", begin) - begin);
    size_t at = authority.rfind('@');
    std::string host = authority.substr(at == std::string::npos ? 0 : at + 1);
    host = host.substr(0, host.find(':'));
    in_addr address{};
    if (inet_aton(host.c_str(), &address) && (ntohl(address.s_addr) >> 24) == 127)
    {
        char numeric[INET_ADDRSTRLEN];
        if (inet_ntop(AF_INET, &address, numeric, sizeof(numeric)))
        {
            return numeric;
        }
    }
    return {};
}

} // namespace

void HttpInit()
{
    std::call_once(g_curl_init_once, []() {
        (void)curl_global_init(CURL_GLOBAL_DEFAULT);
    });
}

void HttpSetFallbackCaBundle(const std::string& path)
{
    std::lock_guard<std::mutex> lock(g_ca_mutex);
    g_fallback_ca_bundle = path;
}

HttpResult HttpPost(const HttpRequest& req, const DataSink& sink, const CheckCancelled& cancelled)
{
    HttpInit();

    HttpResult r;
    if (HasCtl(req.url) || req.url.empty())
    {
        r.error = "Invalid URL: " + req.url;
        r.non_retryable = true;
        return r;
    }
    for (const auto& kv : req.headers)
    {
        if (kv.first.empty() || kv.first.find(':') != std::string::npos || HasCtl(kv.first) || HasCtl(kv.second))
        {
            r.error = "Invalid HTTP header (contains control character or malformed name).";
            r.non_retryable = true;
            return r;
        }
    }

    CURL* h = curl_easy_init();
    if (!h)
    {
        r.error = "Connection error: curl_easy_init failed";
        return r;
    }

    curl_slist* headers = nullptr;
    for (const auto& kv : req.headers)
    {
        headers = curl_slist_append(headers, (kv.first + ": " + kv.second).c_str());
    }
    headers = curl_slist_append(headers, "Expect:");

    TransferContext ctx{&sink, &r, h, false};
    curl_easy_setopt(h, CURLOPT_URL, req.url.c_str());
    curl_easy_setopt(h, CURLOPT_POST, 1L);
    curl_easy_setopt(h, CURLOPT_POSTFIELDS, req.body.data());
    curl_easy_setopt(h, CURLOPT_POSTFIELDSIZE_LARGE, static_cast<curl_off_t>(req.body.size()));
    curl_easy_setopt(h, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(h, CURLOPT_USERAGENT, "Dandelion");
    curl_easy_setopt(h, CURLOPT_WRITEFUNCTION, OnWrite);
    curl_easy_setopt(h, CURLOPT_WRITEDATA, &ctx);
    curl_easy_setopt(h, CURLOPT_HEADERFUNCTION, OnHeader);
    curl_easy_setopt(h, CURLOPT_HEADERDATA, &ctx);
    curl_easy_setopt(h, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(h, CURLOPT_FOLLOWLOCATION, 0L);

#if LIBCURL_VERSION_NUM >= 0x075500
    curl_easy_setopt(h, CURLOPT_PROTOCOLS_STR, "http,https");
#else
    curl_easy_setopt(h, CURLOPT_PROTOCOLS, static_cast<long>(CURLPROTO_HTTP | CURLPROTO_HTTPS));
#endif

    CaConfig ca = ResolveCaConfig();
    if (!ca.ca_file.empty())
    {
        curl_easy_setopt(h, CURLOPT_CAINFO, ca.ca_file.c_str());
    }
    if (!ca.ca_path.empty())
    {
        curl_easy_setopt(h, CURLOPT_CAPATH, ca.ca_path.c_str());
    }

    // Preserve NO_PROXY=* verbatim for older libcurl (e.g. Ubuntu 18.04 curl 7.58).
    std::string user_no_proxy;
    for (const char* name : {"no_proxy", "NO_PROXY"})
    {
        const char* v = std::getenv(name);
        if (v && *v)
        {
            std::string trimmed = TrimAscii(v);
            if (!trimmed.empty())
            {
                user_no_proxy = trimmed;
                break;
            }
        }
    }
    std::string no_proxy;
    if (user_no_proxy == "*")
    {
        no_proxy = "*";
    }
    else
    {
        no_proxy = "localhost,127.0.0.1,::1";
        std::string local = Ipv4Loopback(req.url);
        if (!local.empty())
        {
            no_proxy += "," + local;
        }
        if (!user_no_proxy.empty())
        {
            no_proxy += "," + user_no_proxy;
        }
    }
    curl_easy_setopt(h, CURLOPT_NOPROXY, no_proxy.c_str());
    curl_easy_setopt(h, CURLOPT_CONNECTTIMEOUT, 30L);
    curl_easy_setopt(h, CURLOPT_LOW_SPEED_LIMIT, 1L);
    curl_easy_setopt(h, CURLOPT_LOW_SPEED_TIME, 600L);

    CURLcode rc = CURLE_FAILED_INIT;
    CURLM* multi = curl_multi_init();
    if (multi && curl_multi_add_handle(multi, h) == CURLM_OK)
    {
        int running = 0;
        CURLMcode mc = CURLM_OK;
        do
        {
            if (cancelled && cancelled())
            {
                r.aborted = true;
                break;
            }
            mc = curl_multi_perform(multi, &running);
            if (mc != CURLM_OK || !running)
            {
                break;
            }
            int ready = 0;
            mc = curl_multi_wait(multi, nullptr, 0, 75, &ready);
            if (!ready && mc == CURLM_OK)
            {
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            }
        } while (mc == CURLM_OK);

        int queued = 0;
        while (CURLMsg* msg = curl_multi_info_read(multi, &queued))
        {
            if (msg->msg == CURLMSG_DONE)
            {
                rc = msg->data.result;
            }
        }
        curl_multi_remove_handle(multi, h);
    }
    if (multi)
    {
        curl_multi_cleanup(multi);
    }
    curl_easy_getinfo(h, CURLINFO_RESPONSE_CODE, &r.status);
    if (rc != CURLE_OK && !r.aborted)
    {
        if (rc == CURLE_OPERATION_TIMEDOUT)
        {
            r.timed_out = true;
            r.error = "Request timed out.";
        }
        else
        {
            if (rc == CURLE_URL_MALFORMAT || rc == CURLE_UNSUPPORTED_PROTOCOL || IsDeterministicTlsError(rc))
            {
                r.non_retryable = true;
            }
            r.error = std::string("Connection error: ") + curl_easy_strerror(rc);
        }
    }
    curl_slist_free_all(headers);
    curl_easy_cleanup(h);
    return r;
}

} // namespace dandelion

#endif // !_WIN32

/**
 * @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/native/http_winhttp.cpp
 * @author Xiaojintao
 * @brief Windows WinHTTP async HTTPS transport with deadlock-free cancellation and proxy handling.
 * @version 0.1
 * @date 2026-11-28
 * @copyright Copyright (c) 2026
 */

#ifdef _WIN32

#include "http.hpp"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <winhttp.h>

#include <algorithm>
#include <cctype>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cwchar>
#include <cwctype>
#include <initializer_list>
#include <mutex>
#include <string>

#ifndef WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY
#define WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY 4
#endif
#ifndef WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_3
#define WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_3 0x00002000
#endif
#ifndef WINHTTP_QUERY_RETRY_AFTER
#define WINHTTP_QUERY_RETRY_AFTER 36
#endif
#ifndef WINHTTP_QUERY_CONTENT_TYPE
#define WINHTTP_QUERY_CONTENT_TYPE 1
#endif

namespace dandelion
{
namespace
{

constexpr size_t kInlineBodyLimit = 64 * 1024;
constexpr size_t kUploadChunkSize = 256 * 1024;
constexpr size_t kMaxErrorBodyBytes = 256 * 1024;

/**
 * @brief Parsed HTTP proxy host and optional basic authentication credentials.
 */
struct ProxySpec
{
    std::string host;
    std::string username;
    std::string password;
};

/**
 * @brief Check whether a string contains disallowed ASCII control characters.
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
 * @brief Trim ASCII whitespace from a UTF-8 string.
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
 * @brief Lowercase an ASCII string.
 */
std::string LowerAscii(std::string s)
{
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return s;
}

/**
 * @brief Convert UTF-8 string to UTF-16 wstring safely.
 */
std::wstring Widen(const std::string& s)
{
    if (s.empty())
    {
        return std::wstring();
    }
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
    if (n <= 0)
    {
        return std::wstring();
    }
    std::wstring w(static_cast<size_t>(n), L'\0');
    if (MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), &w[0], n) <= 0)
    {
        return std::wstring();
    }
    return w;
}

/**
 * @brief Convert UTF-16 buffer to UTF-8 string safely.
 */
std::string Narrow(const wchar_t* w, size_t len)
{
    if (!w || len == 0)
    {
        return std::string();
    }
    int n = WideCharToMultiByte(CP_UTF8, 0, w, static_cast<int>(len), nullptr, 0, nullptr, nullptr);
    if (n <= 0)
    {
        return std::string();
    }
    std::string s(static_cast<size_t>(n), '\0');
    if (WideCharToMultiByte(CP_UTF8, 0, w, static_cast<int>(len), &s[0], n, nullptr, nullptr) <= 0)
    {
        return std::string();
    }
    return s;
}

/**
 * @brief RAII wrapper for WinHTTP HINTERNET handles.
 */
class WinHandle
{
public:
    /**
     * @brief Construct an empty WinHandle holding nullptr.
     */
    WinHandle() = default;

    /**
     * @brief Copy construction is deleted to enforce unique handle ownership.
     */
    WinHandle(const WinHandle&) = delete;

    /**
     * @brief Copy assignment is deleted to enforce unique handle ownership.
     */
    WinHandle& operator=(const WinHandle&) = delete;

    /**
     * @brief Close the owned WinHTTP handle if non-null.
     */
    ~WinHandle()
    {
        if (m_h)
        {
            WinHttpCloseHandle(m_h);
        }
    }

    HINTERNET m_h = nullptr;
};

/**
 * @brief Format a WinHTTP / Win32 error code into a readable diagnostic message.
 */
std::string ErrorText(DWORD code)
{
    if (code == ERROR_WINHTTP_TIMEOUT)
    {
        return "Request timed out.";
    }
    wchar_t* buf = nullptr;
    DWORD n = FormatMessageW(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_HMODULE | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
                             GetModuleHandleW(L"winhttp.dll"), code, 0, reinterpret_cast<LPWSTR>(&buf), 0, nullptr);
    std::string msg = n ? Narrow(buf, n) : std::string();
    if (buf)
    {
        LocalFree(buf);
    }
    while (!msg.empty() && (msg.back() == '\r' || msg.back() == '\n' || msg.back() == ' ' || msg.back() == '.'))
    {
        msg.pop_back();
    }
    return "Connection error: " + (msg.empty() ? std::string("unknown error") : msg) + " (WinHTTP " + std::to_string(code) + ")";
}

/**
 * @brief Async WinHTTP request wrapper coordinating callback completions with cooperative cancellation.
 */
class AsyncRequest : public WinHandle
{
public:
    /**
     * @brief WinHTTP status callback delivering completion events and handle-closing notification.
     */
    static void CALLBACK Notify(HINTERNET, DWORD_PTR context, DWORD status, LPVOID info, DWORD length)
    {
        auto* request = reinterpret_cast<AsyncRequest*>(context);
        if (!request)
        {
            return;
        }
        if (status == WINHTTP_CALLBACK_STATUS_INTERMEDIATE_RESPONSE)
        {
            return;
        }
        std::lock_guard<std::mutex> lock(request->m_mutex);
        if (status == WINHTTP_CALLBACK_STATUS_HANDLE_CLOSING)
        {
            request->m_closing = true;
            request->m_changed.notify_all();
            return;
        }
        request->m_status = status;
        if (status == WINHTTP_CALLBACK_STATUS_REQUEST_ERROR && info)
        {
            request->m_value = static_cast<WINHTTP_ASYNC_RESULT*>(info)->dwError;
        }
        else if (status == WINHTTP_CALLBACK_STATUS_DATA_AVAILABLE && info)
        {
            request->m_value = *static_cast<DWORD*>(info);
        }
        else if (status == WINHTTP_CALLBACK_STATUS_WRITE_COMPLETE && info)
        {
            request->m_value = *static_cast<DWORD*>(info);
        }
        else
        {
            request->m_value = length;
        }
        request->m_changed.notify_one();
    }

    /**
     * @brief Return the opaque context pointer bound to the WinHTTP handle.
     */
    DWORD_PTR Context()
    {
        return reinterpret_cast<DWORD_PTR>(this);
    }

    /**
     * @brief Bind this object as the async status callback context for m_h.
     */
    bool Subscribe()
    {
        DWORD_PTR ctx = Context();
        if (!WinHttpSetOption(m_h, WINHTTP_OPTION_CONTEXT_VALUE, &ctx, sizeof(ctx)))
        {
            return false;
        }
        m_registered = WinHttpSetStatusCallback(m_h, Notify, WINHTTP_CALLBACK_FLAG_ALL_COMPLETIONS | WINHTTP_CALLBACK_FLAG_HANDLES, 0) != WINHTTP_INVALID_STATUS_CALLBACK;
        return m_registered;
    }

    /**
     * @brief Wait for an initiated async WinHTTP call while unlocking m_mutex around cancelled().
     */
    bool Wait(BOOL initiated, DWORD expected, const CheckCancelled& cancelled, HttpResult& result, DWORD* out_value = nullptr)
    {
        if (!initiated)
        {
            DWORD err = GetLastError();
            if (err == ERROR_WINHTTP_TIMEOUT)
            {
                result.timed_out = true;
            }
            result.error = ErrorText(err);
            return false;
        }
        if (cancelled && cancelled())
        {
            result.aborted = true;
            return false;
        }
        std::unique_lock<std::mutex> lock(m_mutex);
        while (!m_status)
        {
            if (m_changed.wait_for(lock, std::chrono::milliseconds(75)) == std::cv_status::timeout)
            {
                lock.unlock();
                bool stop = cancelled && cancelled();
                lock.lock();
                if (stop)
                {
                    result.aborted = true;
                    return false;
                }
            }
        }
        DWORD st = m_status;
        DWORD val = m_value;
        m_status = 0;
        m_value = 0;
        if (st != expected)
        {
            DWORD err = (st == WINHTTP_CALLBACK_STATUS_REQUEST_ERROR) ? val : ERROR_INVALID_DATA;
            if (err == ERROR_WINHTTP_TIMEOUT)
            {
                result.timed_out = true;
            }
            result.error = ErrorText(err);
            return false;
        }
        if (out_value)
        {
            *out_value = val;
        }
        return true;
    }

    /**
     * @brief Close the handle and wait for WINHTTP_CALLBACK_STATUS_HANDLE_CLOSING if close succeeded.
     */
    ~AsyncRequest()
    {
        if (!m_registered || !m_h)
        {
            return;
        }
        HINTERNET handle = m_h;
        m_h = nullptr;
        if (!WinHttpCloseHandle(handle))
        {
            return;
        }
        std::unique_lock<std::mutex> lock(m_mutex);
        m_changed.wait(lock, [this]() {
            return m_closing;
        });
    }

private:
    std::mutex m_mutex;
    std::condition_variable m_changed;
    DWORD m_status = 0;
    DWORD m_value = 0;
    bool m_registered = false;
    bool m_closing = false;
};

/**
 * @brief Return the first non-empty HTTP-compatible proxy env var, skipping unsupported socks*:// proxies.
 */
std::string FirstHttpProxy(std::initializer_list<const char*> names)
{
    for (const char* name : names)
    {
        const char* v = std::getenv(name);
        if (!v || !*v)
        {
            continue;
        }
        std::string val = TrimAscii(v);
        if (val.empty())
        {
            continue;
        }
        std::string low = LowerAscii(val);
        if (low.rfind("socks", 0) == 0)
        {
            continue;
        }
        return val;
    }
    return std::string();
}

/**
 * @brief Return the first non-empty trimmed environment variable from names.
 */
std::string EnvFirst(std::initializer_list<const char*> names)
{
    for (const char* name : names)
    {
        const char* v = std::getenv(name);
        if (v && *v)
        {
            std::string trimmed = TrimAscii(v);
            if (!trimmed.empty())
            {
                return trimmed;
            }
        }
    }
    return std::string();
}

/**
 * @brief Parse an HTTP proxy URL into host:port and optional username/password credentials.
 */
ProxySpec ParseProxyUrl(std::string p)
{
    ProxySpec spec;
    size_t scheme = p.find("://");
    if (scheme != std::string::npos)
    {
        p = p.substr(scheme + 3);
    }
    size_t slash = p.find_first_of("/?#");
    if (slash != std::string::npos)
    {
        p = p.substr(0, slash);
    }
    size_t at = p.rfind('@');
    if (at != std::string::npos)
    {
        std::string auth = p.substr(0, at);
        p = p.substr(at + 1);
        size_t colon = auth.find(':');
        if (colon != std::string::npos)
        {
            spec.username = auth.substr(0, colon);
            spec.password = auth.substr(colon + 1);
        }
        else
        {
            spec.username = auth;
        }
    }
    spec.host = p;
    return spec;
}

/**
 * @brief Translate comma-separated NO_PROXY rules into WinHTTP semicolon-separated bypass syntax.
 */
std::string FormatWinHttpBypass(const std::string& raw)
{
    std::string out;
    size_t start = 0;
    while (start <= raw.size())
    {
        size_t comma = raw.find(',', start);
        std::string token = TrimAscii(comma == std::string::npos ? raw.substr(start) : raw.substr(start, comma - start));
        if (!token.empty())
        {
            if (token[0] == '.')
            {
                token = "*" + token;
            }
            if (!out.empty())
            {
                out += ';';
            }
            out += token;
        }
        if (comma == std::string::npos)
        {
            break;
        }
        start = comma + 1;
    }
    return out;
}

/**
 * @brief Check whether a wide hostname is a loopback address (localhost, ::1, or strict IPv4 127.x.y.z).
 */
bool IsLoopbackHost(const std::wstring& host)
{
    std::wstring low = host;
    std::transform(low.begin(), low.end(), low.begin(), [](wchar_t c) {
        return static_cast<wchar_t>(std::towlower(c));
    });
    if (low == L"localhost" || low == L"localhost." || low == L"::1" || low == L"[::1]")
    {
        return true;
    }
    int octets[4] = {0, 0, 0, 0};
    int idx = 0;
    size_t pos = 0;
    while (pos < low.size() && idx < 4)
    {
        size_t dot = low.find(L'.', pos);
        std::wstring part = (dot == std::wstring::npos) ? low.substr(pos) : low.substr(pos, dot - pos);
        if (part.empty() || part.size() > 3)
        {
            return false;
        }
        int val = 0;
        for (wchar_t ch : part)
        {
            if (ch < L'0' || ch > L'9')
            {
                return false;
            }
            val = val * 10 + (ch - L'0');
        }
        if (val > 255)
        {
            return false;
        }
        octets[idx++] = val;
        if (dot == std::wstring::npos)
        {
            pos = low.size();
            break;
        }
        pos = dot + 1;
    }
    return idx == 4 && pos == low.size() && octets[0] == 127;
}

/**
 * @brief Open a WinHTTP async session respecting loopback bypass, NO_PROXY=*, and HTTP(S)_PROXY.
 */
HINTERNET OpenSession(bool https, bool local, ProxySpec* out_proxy)
{
    std::string raw_no_proxy = EnvFirst({"NO_PROXY", "no_proxy"});
    if (local || raw_no_proxy == "*")
    {
        return WinHttpOpen(L"Dandelion", WINHTTP_ACCESS_TYPE_NO_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, WINHTTP_FLAG_ASYNC);
    }
    std::string proxy = https ? FirstHttpProxy({"HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"}) : FirstHttpProxy({"HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"});
    if (!proxy.empty())
    {
        ProxySpec spec = ParseProxyUrl(proxy);
        if (out_proxy)
        {
            *out_proxy = spec;
        }
        std::string bypass = FormatWinHttpBypass(raw_no_proxy);
        std::wstring wproxy = Widen(spec.host);
        std::wstring wbypass = Widen(bypass);
        return WinHttpOpen(L"Dandelion", WINHTTP_ACCESS_TYPE_NAMED_PROXY, wproxy.c_str(), wbypass.empty() ? WINHTTP_NO_PROXY_BYPASS : wbypass.c_str(), WINHTTP_FLAG_ASYNC);
    }
    HINTERNET h = WinHttpOpen(L"Dandelion", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, WINHTTP_FLAG_ASYNC);
    if (!h)
    {
        h = WinHttpOpen(L"Dandelion", WINHTTP_ACCESS_TYPE_DEFAULT_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, WINHTTP_FLAG_ASYNC);
    }
    return h;
}

} // namespace

void HttpInit()
{
}

void HttpSetFallbackCaBundle(const std::string& path)
{
    (void)path;
}

HttpResult HttpPost(const HttpRequest& req, const DataSink& sink, const CheckCancelled& cancelled)
{
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

    std::wstring wurl = Widen(req.url);
    URL_COMPONENTS uc;
    ZeroMemory(&uc, sizeof(uc));
    uc.dwStructSize = sizeof(uc);
    uc.dwSchemeLength = static_cast<DWORD>(-1);
    uc.dwHostNameLength = static_cast<DWORD>(-1);
    uc.dwUrlPathLength = static_cast<DWORD>(-1);
    uc.dwExtraInfoLength = static_cast<DWORD>(-1);
    if (!WinHttpCrackUrl(wurl.c_str(), static_cast<DWORD>(wurl.size()), 0, &uc) || !uc.lpszHostName || (uc.nScheme != INTERNET_SCHEME_HTTP && uc.nScheme != INTERNET_SCHEME_HTTPS))
    {
        r.error = "Invalid URL: " + req.url;
        r.non_retryable = true;
        return r;
    }

    bool https = uc.nScheme == INTERNET_SCHEME_HTTPS;
    std::wstring host(uc.lpszHostName, uc.dwHostNameLength);
    std::wstring path = uc.lpszUrlPath ? std::wstring(uc.lpszUrlPath, uc.dwUrlPathLength) : std::wstring();
    if (uc.lpszExtraInfo && uc.dwExtraInfoLength > 0)
    {
        // Strip any #fragment before appending query parameters to the HTTP request-target.
        std::wstring extra(uc.lpszExtraInfo, uc.dwExtraInfoLength);
        size_t hash_pos = extra.find(L'#');
        if (hash_pos != std::wstring::npos)
        {
            extra.erase(hash_pos);
        }
        path.append(extra);
    }
    if (path.empty())
    {
        path = L"/";
    }

    WinHandle session;
    WinHandle connection;
    std::wstring headers;
    std::string buf(64 * 1024, '\0');
    AsyncRequest request;
    ProxySpec proxy_spec;

    session.m_h = OpenSession(https, IsLoopbackHost(host), &proxy_spec);
    if (!session.m_h)
    {
        r.error = ErrorText(GetLastError());
        return r;
    }
    WinHttpSetTimeouts(session.m_h, 30000, 30000, 600000, 600000);
    if (https)
    {
        DWORD protocols = WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_2 | WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_3;
        if (!WinHttpSetOption(session.m_h, WINHTTP_OPTION_SECURE_PROTOCOLS, &protocols, sizeof(protocols)))
        {
            protocols = WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_2;
            WinHttpSetOption(session.m_h, WINHTTP_OPTION_SECURE_PROTOCOLS, &protocols, sizeof(protocols));
        }
    }
    connection.m_h = WinHttpConnect(session.m_h, host.c_str(), uc.nPort, 0);
    if (connection.m_h)
    {
        request.m_h = WinHttpOpenRequest(connection.m_h, L"POST", path.c_str(), nullptr, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, https ? WINHTTP_FLAG_SECURE : 0);
    }
    if (!request.m_h)
    {
        r.error = ErrorText(GetLastError());
        return r;
    }

    DWORD redirect_policy = WINHTTP_OPTION_REDIRECT_POLICY_NEVER;
    if (!WinHttpSetOption(request.m_h, WINHTTP_OPTION_REDIRECT_POLICY, &redirect_policy, sizeof(redirect_policy)) || !request.Subscribe())
    {
        r.error = ErrorText(GetLastError());
        return r;
    }

    if (!proxy_spec.username.empty())
    {
        std::wstring wuser = Widen(proxy_spec.username);
        std::wstring wpass = Widen(proxy_spec.password);
        WinHttpSetCredentials(request.m_h, WINHTTP_AUTH_TARGET_PROXY, WINHTTP_AUTH_SCHEME_BASIC, wuser.c_str(), wpass.c_str(), nullptr);
    }

    for (const auto& kv : req.headers)
    {
        headers += Widen(kv.first + ": " + kv.second) + L"\r\n";
    }

    bool chunked_upload = req.body.size() > kInlineBodyLimit;
    if (chunked_upload)
    {
        if (!request.Wait(WinHttpSendRequest(request.m_h, headers.c_str(), static_cast<DWORD>(headers.size()), WINHTTP_NO_REQUEST_DATA, 0, static_cast<DWORD>(req.body.size()),
                                             request.Context()),
                          WINHTTP_CALLBACK_STATUS_SENDREQUEST_COMPLETE, cancelled, r))
        {
            return r;
        }
        size_t offset = 0;
        while (offset < req.body.size())
        {
            DWORD chunk = static_cast<DWORD>(std::min<size_t>(kUploadChunkSize, req.body.size() - offset));
            DWORD written = 0;
            if (!request.Wait(WinHttpWriteData(request.m_h, req.body.data() + offset, chunk, nullptr), WINHTTP_CALLBACK_STATUS_WRITE_COMPLETE, cancelled, r, &written))
            {
                return r;
            }
            if (written == 0)
            {
                r.error = "Connection error: WinHttpWriteData wrote 0 bytes.";
                return r;
            }
            offset += written;
        }
    }
    else
    {
        if (!request.Wait(WinHttpSendRequest(request.m_h, headers.c_str(), static_cast<DWORD>(headers.size()),
                                             req.body.empty() ? WINHTTP_NO_REQUEST_DATA : const_cast<char*>(req.body.data()), static_cast<DWORD>(req.body.size()),
                                             static_cast<DWORD>(req.body.size()), request.Context()),
                          WINHTTP_CALLBACK_STATUS_SENDREQUEST_COMPLETE, cancelled, r))
        {
            return r;
        }
    }

    if (!request.Wait(WinHttpReceiveResponse(request.m_h, nullptr), WINHTTP_CALLBACK_STATUS_HEADERS_AVAILABLE, cancelled, r))
    {
        return r;
    }

    DWORD status = 0;
    DWORD size = sizeof(status);
    WinHttpQueryHeaders(request.m_h, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, WINHTTP_HEADER_NAME_BY_INDEX, &status, &size, WINHTTP_NO_HEADER_INDEX);
    r.status = static_cast<long>(status);

    wchar_t retry[64];
    DWORD retry_size = sizeof(retry);
    if (WinHttpQueryHeaders(request.m_h, WINHTTP_QUERY_RETRY_AFTER, WINHTTP_HEADER_NAME_BY_INDEX, retry, &retry_size, WINHTTP_NO_HEADER_INDEX))
    {
        double seconds = std::wcstod(retry, nullptr);
        r.retry_after = seconds > 0 ? seconds : -1;
    }

    wchar_t ctype[256];
    DWORD ctype_size = sizeof(ctype);
    if (WinHttpQueryHeaders(request.m_h, WINHTTP_QUERY_CONTENT_TYPE, WINHTTP_HEADER_NAME_BY_INDEX, ctype, &ctype_size, WINHTTP_NO_HEADER_INDEX))
    {
        r.content_type = TrimAscii(Narrow(ctype, ctype_size / sizeof(wchar_t)));
    }

    bool ok = status >= 200 && status < 300;
    bool error_body_truncated = false;
    for (;;)
    {
        DWORD available = 0;
        if (!request.Wait(WinHttpQueryDataAvailable(request.m_h, nullptr), WINHTTP_CALLBACK_STATUS_DATA_AVAILABLE, cancelled, r, &available))
        {
            break;
        }
        DWORD want = static_cast<DWORD>(std::min<size_t>(available, buf.size()));
        if (want == 0)
        {
            break;
        }
        DWORD got = 0;
        if (!request.Wait(WinHttpReadData(request.m_h, &buf[0], want, nullptr), WINHTTP_CALLBACK_STATUS_READ_COMPLETE, cancelled, r, &got))
        {
            break;
        }
        if (got == 0)
        {
            break;
        }
        if (ok)
        {
            if (sink)
            {
                r.delivered = true;
                if (!sink(buf.data(), got))
                {
                    r.aborted = true;
                    break;
                }
            }
            else
            {
                r.body.append(buf.data(), got);
            }
        }
        else
        {
            if (r.body.size() < kMaxErrorBodyBytes)
            {
                size_t room = kMaxErrorBodyBytes - r.body.size();
                size_t take = got < room ? got : room;
                r.body.append(buf.data(), take);
                if (take < got && !error_body_truncated)
                {
                    error_body_truncated = true;
                    r.body.append(" [truncated]");
                }
            }
            else if (!error_body_truncated)
            {
                error_body_truncated = true;
                r.body.append(" [truncated]");
            }
        }
    }
    return r;
}

} // namespace dandelion

#endif // _WIN32

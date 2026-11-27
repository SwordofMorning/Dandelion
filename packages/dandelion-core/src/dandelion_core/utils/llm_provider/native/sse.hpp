#pragma once

/**
 * @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/native/sse.hpp
 * @author Xiaojintao
 * @brief Incremental WHATWG Server-Sent Events parser with BOM, CR/LF/CRLF, and raw non-SSE capture.
 * @version 0.1
 * @date 2026-11-28
 * @copyright Copyright (c) 2026
 */

#include <cstddef>
#include <string>

namespace dandelion
{

/**
 * @brief Single decoded SSE event carrying optional event name and joined data payload.
 */
struct SseEvent
{
    std::string event;
    std::string data;
};

/**
 * @brief Incremental byte-oriented SSE stream parser.
 *
 * Handles UTF-8 multi-byte sequences split arbitrarily across reads, leading UTF-8 BOM,
 * LF / CRLF / standalone CR line endings, multi-line data fields, and captures up to
 * 8 KiB of raw bytes when an endpoint sends a non-SSE body (such as HTTP 200 JSON errors).
 */
class SseParser
{
public:
    /**
     * @brief Maximum number of raw non-SSE body bytes retained for error diagnostics.
     */
    static constexpr size_t kMaxRawPrefix = 8192;

    /**
     * @brief Feed an incoming chunk of bytes and emit completed SSE events via on_event.
     */
    template <class F> void Feed(const char* p, size_t n, F&& on_event)
    {
        if (!p || n == 0)
        {
            return;
        }
        size_t i = 0;
        if (m_bom_stage < 3)
        {
            static constexpr unsigned char kBom[3] = {0xEF, 0xBB, 0xBF};
            while (i < n && m_bom_stage < 3)
            {
                unsigned char ch = static_cast<unsigned char>(p[i]);
                if (ch == kBom[m_bom_stage])
                {
                    m_bom_buf.push_back(p[i]);
                    ++m_bom_stage;
                    ++i;
                    if (m_bom_stage == 3)
                    {
                        m_bom_buf.clear();
                    }
                }
                else
                {
                    m_bom_stage = 3;
                    if (!m_bom_buf.empty())
                    {
                        std::string replay;
                        replay.swap(m_bom_buf);
                        ProcessBytes(replay.data(), replay.size(), on_event);
                    }
                    break;
                }
            }
            if (i >= n)
            {
                return;
            }
        }
        ProcessBytes(p + i, n - i, on_event);
    }

    /**
     * @brief Flush any buffered trailing line and open event at stream EOF.
     */
    template <class F> void Finish(F&& on_event)
    {
        if (!m_bom_buf.empty())
        {
            std::string replay;
            replay.swap(m_bom_buf);
            m_bom_stage = 3;
            ProcessBytes(replay.data(), replay.size(), on_event);
        }
        m_pending_cr = false;
        if (!m_line_buf.empty())
        {
            std::string rest;
            rest.swap(m_line_buf);
            ProcessLine(rest, on_event);
        }
        ProcessLine(std::string(), on_event);
    }

    /**
     * @brief Return true if at least one SSE event was dispatched.
     */
    bool SawEvents() const
    {
        return m_event_count > 0;
    }

    /**
     * @brief Return captured raw response prefix when no SSE fields have been observed.
     */
    const std::string& RawPrefix() const
    {
        return m_raw_prefix;
    }

private:
    /**
     * @brief Split bytes on LF, CRLF, or standalone CR without breaking split CRLF pairs.
     */
    template <class F> void ProcessBytes(const char* p, size_t n, F&& on_event)
    {
        if (!m_saw_sse_field && m_raw_prefix.size() < kMaxRawPrefix)
        {
            size_t take = kMaxRawPrefix - m_raw_prefix.size();
            if (n < take)
            {
                take = n;
            }
            m_raw_prefix.append(p, take);
        }

        size_t i = 0;
        if (m_pending_cr)
        {
            m_pending_cr = false;
            if (i < n && p[i] == '\n')
            {
                ++i;
            }
        }

        while (i < n)
        {
            char c = p[i];
            if (c == '\n')
            {
                std::string current;
                current.swap(m_line_buf);
                ProcessLine(current, on_event);
                ++i;
            }
            else if (c == '\r')
            {
                std::string current;
                current.swap(m_line_buf);
                ProcessLine(current, on_event);
                ++i;
                if (i < n)
                {
                    if (p[i] == '\n')
                    {
                        ++i;
                    }
                }
                else
                {
                    m_pending_cr = true;
                }
            }
            else
            {
                size_t start = i;
                while (i < n && p[i] != '\n' && p[i] != '\r')
                {
                    ++i;
                }
                m_line_buf.append(p + start, i - start);
            }
        }
    }

    /**
     * @brief Parse one logical SSE line and dispatch on blank line separators.
     */
    template <class F> void ProcessLine(const std::string& line, F&& on_event)
    {
        if (line.empty())
        {
            if (m_has_data)
            {
                SseEvent ev{m_event, m_data};
                m_event.clear();
                m_data.clear();
                m_has_data = false;
                ++m_event_count;
                on_event(ev);
            }
            m_event.clear();
            return;
        }
        if (line[0] == ':')
        {
            return;
        }
        size_t colon = line.find(':');
        std::string name = line.substr(0, colon);
        std::string value = colon == std::string::npos ? std::string() : line.substr(colon + 1);
        if (!value.empty() && value[0] == ' ')
        {
            value.erase(0, 1);
        }
        if (name == "event")
        {
            m_saw_sse_field = true;
            m_event = value;
        }
        else if (name == "data")
        {
            m_saw_sse_field = true;
            if (m_has_data)
            {
                m_data += '\n';
            }
            m_data += value;
            m_has_data = true;
        }
    }

    int m_bom_stage = 0;
    std::string m_bom_buf;
    bool m_pending_cr = false;
    std::string m_line_buf;
    std::string m_event;
    std::string m_data;
    bool m_has_data = false;
    bool m_saw_sse_field = false;
    size_t m_event_count = 0;
    std::string m_raw_prefix;
};

} // namespace dandelion

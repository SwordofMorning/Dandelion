#pragma once

/**
 * @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/native/common.hpp
 * @author Xiaojintao
 * @brief Shared nanobind helpers, string utilities, flavor resolution, and JSON/response builders.
 * @version 0.1
 * @date 2026-11-28
 * @copyright Copyright (c) 2026
 */

#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <initializer_list>
#include <stdexcept>
#include <string>

namespace nb = nanobind;
using namespace nanobind::literals;

namespace dandelion
{

/**
 * @brief Wire-format or protocol error encountered while decoding a response or SSE stream.
 */
struct ProtocolError : std::runtime_error
{
    using std::runtime_error::runtime_error;
};

/**
 * @brief Configuration snapshot shared by a Provider and its protocol Codec.
 */
struct ProviderConfig
{
    std::string api_key;
    std::string base_url;
    std::string model_id;
    std::string thinking;
    std::string effort;
    std::string compat;
};

/**
 * @brief Return the active Python package name hosting effort.py, blocks.py, and _native.
 */
const std::string& PackageName();

/**
 * @brief Update the Python package name derived from module.__package__ during module init.
 */
void SetPackageName(std::string pkg);

/**
 * @brief Read dict[key] for dicts or getattr(obj, key) otherwise; returns None if absent.
 */
nb::object Field(nb::handle obj, const char* key);

/**
 * @brief Convert a Python object to UTF-8 std::string (None -> "", str -> UTF-8 with surrogate replacement).
 */
std::string TextOf(nb::handle obj);

/**
 * @brief Decode raw bytes to a Python str using UTF-8 with replacement characters on invalid bytes.
 */
nb::str SafeUtf8Str(const std::string& s);

/**
 * @brief Check whether a Python handle is a str.
 */
bool IsStr(nb::handle obj);

/**
 * @brief Check whether a Python handle is a list.
 */
bool IsList(nb::handle obj);

/**
 * @brief Check whether a Python handle is a dict.
 */
bool IsDict(nb::handle obj);

/**
 * @brief Check whether a Python handle is an int (excluding bool).
 */
bool IsInt(nb::handle obj);

/**
 * @brief Check whether a dict contains a given C-string key.
 */
bool DictHas(nb::handle obj, const char* key);

/**
 * @brief Delete a C-string key from a dict if present.
 */
void DictDelIfPresent(nb::dict d, const char* key);

/**
 * @brief Return a shallow dict copy of a dict, model_dump() object, or __dict__ object.
 */
nb::dict CopyDict(nb::handle obj);

/**
 * @brief Build a 2-element Python tuple from two Python objects.
 */
nb::tuple MakePairTuple(nb::object first, nb::object second);

/**
 * @brief Return an ASCII-lowercased copy of s.
 */
std::string Lower(std::string s);

/**
 * @brief Return an ASCII-uppercased copy of s.
 */
std::string Upper(std::string s);

/**
 * @brief Strip leading and trailing ASCII whitespace from s.
 */
std::string Trim(const std::string& s);

/**
 * @brief Check whether hay contains needle.
 */
bool Contains(const std::string& hay, const std::string& needle);

/**
 * @brief Check whether hay contains any string in needles.
 */
bool ContainsAny(const std::string& hay, std::initializer_list<const char*> needles);

/**
 * @brief Check whether s starts with prefix.
 */
bool StartsWith(const std::string& s, const std::string& prefix);

/**
 * @brief Check whether s ends with suffix.
 */
bool EndsWith(const std::string& s, const std::string& suffix);

/**
 * @brief Strip trailing '/' characters from s.
 */
std::string RstripSlash(std::string s);

/**
 * @brief Check whether a string contains disallowed HTTP header control characters.
 */
bool HasHeaderControlChars(const std::string& s);

/**
 * @brief Strip vendor/models prefixes from a Gemini model ID and reject URL path/query injection characters.
 */
std::string SanitizeBareModel(const std::string& model);

/**
 * @brief Return true for OpenAI reasoning models (o1, o3, o4, gpt-5, gpt-6 families).
 */
bool IsOpenAIReasoningModel(const std::string& model_id);

/**
 * @brief Return environment variable name when non-empty, or fallback otherwise.
 */
std::string EnvOr(const char* name, const std::string& fallback);

/**
 * @brief Parse UTF-8 JSON bytes into Python objects via json.loads.
 */
nb::object JsonLoads(const std::string& text);

/**
 * @brief Serialize a Python object to RFC 8259 UTF-8 JSON (allow_nan=False) using blocks.json_default.
 */
std::string JsonDumps(nb::handle obj);

/**
 * @brief Serialize a Python object to RFC 8259 UTF-8 JSON (allow_nan=False) without a default hook.
 */
std::string JsonDumpsPlain(nb::handle obj);

/**
 * @brief Construct a blocks.Block from a dict.
 */
nb::object MakeBlock(nb::dict data);

/**
 * @brief Construct a SimpleNamespace(content, stop_reason, usage) response object.
 */
nb::object MakeResponse(nb::list content, const std::string& stop_reason, nb::object usage);

/**
 * @brief Construct a blocks error subclass (PartialStreamError or RequestCancelledError).
 */
nb::object MakeBlocksError(const char* class_name, nb::str message);

/**
 * @brief Write text to sys.stdout without a trailing newline and flush immediately.
 */
void Echo(const std::string& text);

/**
 * @brief Write ANSI dim-gray text to sys.stdout and flush immediately.
 */
void EchoGray(const std::string& text);

/**
 * @brief Write a line of text to sys.stdout followed by a newline and flush.
 */
void PrintLine(const std::string& text);

/**
 * @brief Fetch an attribute from effort.py.
 */
nb::object EffortAttr(const char* name);

/**
 * @brief Resolve an abstract effort string through a named table in effort.py.
 */
nb::object ResolveEffort(const char* table, const std::string& effort, const std::string& model_id);

/**
 * @brief Resolve a Gemini thinking level (Gemini 3 vs Gemini 4 xhigh) via effort.resolve_gemini_level.
 */
std::string ResolveGeminiLevel(const std::string& effort, const std::string& model_id);

/**
 * @brief Check whether compat explicitly names one of effort.VALID_FLAVORS.
 */
bool IsExplicitCompat(const std::string& compat);

/**
 * @brief Resolve the terminal model flavor for cross-protocol relays (Sub2API/etc.).
 */
std::string ResolveFlavor(const std::string& compat, const std::string& model_id, const std::string& base_url, const std::string& default_flavor, bool warn_auto = true);

/**
 * @brief Flatten a canonical system prompt (string or text block list) to plain text.
 */
std::string SystemText(nb::object system);

/**
 * @brief Flatten a canonical tool_result content (string or text block list) to plain text.
 */
std::string ResultText(nb::object content);

/**
 * @brief Check whether the calling thread is the Python main thread (fork-safe thread-local cache).
 */
bool IsMainThread();

/**
 * @brief Generate a random tool call identifier ("call_<24 hex chars>").
 */
std::string RandomCallId();

} // namespace dandelion

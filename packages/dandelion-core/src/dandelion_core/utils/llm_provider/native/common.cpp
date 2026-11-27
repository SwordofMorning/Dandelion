/**
 * @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/native/common.cpp
 * @author Xiaojintao
 * @brief Implementation of shared nanobind helpers, string utilities, and cross-protocol flavor detection.
 * @version 0.1
 * @date 2026-11-28
 * @copyright Copyright (c) 2026
 */

#include "common.hpp"

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <random>
#include <vector>

namespace dandelion
{
namespace
{

std::string g_package_name = "dandelion_core.utils.llm_provider";

/**
 * @brief Keyword-to-flavor mapping entry used during automatic flavor detection.
 */
struct FlavorHint
{
    std::vector<const char*> keywords;
    const char* flavor;
};

/**
 * @brief Model-name hints checked before host hints because model_id names the real upstream family.
 */
const std::vector<FlavorHint> kModelHints = {
    {{"deepseek"}, "deepseek"},
    {{"gemini", "neon", "hydrogen"}, "gemini"},
    {{"gpt", "codex"}, "openai"},
    {{"claude", "opus", "sonnet", "haiku"}, "anthropic"},
};

/**
 * @brief Base URL host hints checked when model_id gives no family clue.
 */
const std::vector<FlavorHint> kHostHints = {
    {{"deepseek"}, "deepseek"},
    {{"gemini", "antigravity", "generativelanguage"}, "gemini"},
    {{"sub2api", "openai"}, "openai"},
    {{"anthropic"}, "anthropic"},
};

/**
 * @brief Check whether a lowercased model_id refers to an OpenAI o1/o3/o4 reasoning model.
 */
bool IsOpenAIOSeries(const std::string& lowered_model)
{
    std::string m = lowered_model;
    size_t slash = m.rfind('/');
    if (slash != std::string::npos)
    {
        m = m.substr(slash + 1);
    }
    for (const char* prefix : {"o1", "o3", "o4"})
    {
        if (m == prefix || StartsWith(m, std::string(prefix) + "-"))
        {
            return true;
        }
    }
    return false;
}

/**
 * @brief Extract UTF-8 string bytes from a Python str, falling back to errors="replace" on lone surrogates.
 */
std::string ExtractPythonStr(nb::handle obj)
{
    try
    {
        return nb::cast<std::string>(obj);
    }
    catch (nb::python_error&)
    {
        nb::bytes encoded = nb::cast<nb::bytes>(obj.attr("encode")("utf-8", "replace"));
        return std::string(encoded.c_str(), encoded.size());
    }
}

} // namespace

const std::string& PackageName()
{
    return g_package_name;
}

void SetPackageName(std::string pkg)
{
    if (!pkg.empty())
    {
        g_package_name = std::move(pkg);
    }
}

nb::object Field(nb::handle obj, const char* key)
{
    if (!obj || obj.is_none())
    {
        return nb::none();
    }
    if (nb::isinstance<nb::dict>(obj))
    {
        PyObject* value = PyDict_GetItemString(obj.ptr(), key);
        return value ? nb::borrow<nb::object>(value) : nb::none();
    }
    return nb::getattr(obj, key, nb::none());
}

std::string TextOf(nb::handle obj)
{
    if (!obj || obj.is_none())
    {
        return "";
    }
    if (nb::isinstance<nb::str>(obj))
    {
        return ExtractPythonStr(obj);
    }
    return ExtractPythonStr(nb::str(obj));
}

nb::str SafeUtf8Str(const std::string& s)
{
    PyObject* p = PyUnicode_DecodeUTF8(s.data(), static_cast<Py_ssize_t>(s.size()), "replace");
    if (!p)
    {
        throw nb::python_error();
    }
    return nb::steal<nb::str>(p);
}

bool IsStr(nb::handle obj)
{
    return obj && nb::isinstance<nb::str>(obj);
}

bool IsList(nb::handle obj)
{
    return obj && nb::isinstance<nb::list>(obj);
}

bool IsDict(nb::handle obj)
{
    return obj && nb::isinstance<nb::dict>(obj);
}

bool IsInt(nb::handle obj)
{
    return obj && nb::isinstance<nb::int_>(obj) && !nb::isinstance<nb::bool_>(obj);
}

bool DictHas(nb::handle obj, const char* key)
{
    return IsDict(obj) && PyDict_GetItemString(obj.ptr(), key) != nullptr;
}

void DictDelIfPresent(nb::dict d, const char* key)
{
    if (PyDict_GetItemString(d.ptr(), key) != nullptr)
    {
        if (PyDict_DelItemString(d.ptr(), key) != 0)
        {
            throw nb::python_error();
        }
    }
}

nb::dict CopyDict(nb::handle obj)
{
    if (IsDict(obj))
    {
        PyObject* copied = PyDict_Copy(obj.ptr());
        if (!copied)
        {
            throw nb::python_error();
        }
        return nb::steal<nb::dict>(copied);
    }
    if (obj && nb::hasattr(obj, "model_dump"))
    {
        nb::object dumped = obj.attr("model_dump")();
        if (IsDict(dumped))
        {
            return CopyDict(dumped);
        }
    }
    if (obj && nb::hasattr(obj, "__dict__"))
    {
        nb::object attrs = obj.attr("__dict__");
        if (IsDict(attrs))
        {
            return CopyDict(attrs);
        }
    }
    return nb::dict();
}

nb::tuple MakePairTuple(nb::object first, nb::object second)
{
    PyObject* tup = PyTuple_New(2);
    if (!tup)
    {
        throw nb::python_error();
    }
    PyObject* p0 = first.release().ptr();
    PyObject* p1 = second.release().ptr();
    PyTuple_SetItem(tup, 0, p0);
    PyTuple_SetItem(tup, 1, p1);
    return nb::steal<nb::tuple>(tup);
}

std::string Lower(std::string s)
{
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return s;
}

std::string Upper(std::string s)
{
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) {
        return static_cast<char>(std::toupper(c));
    });
    return s;
}

std::string Trim(const std::string& s)
{
    size_t b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos)
    {
        return "";
    }
    size_t e = s.find_last_not_of(" \t\r\n");
    return s.substr(b, e - b + 1);
}

bool Contains(const std::string& hay, const std::string& needle)
{
    return hay.find(needle) != std::string::npos;
}

bool ContainsAny(const std::string& hay, std::initializer_list<const char*> needles)
{
    for (const char* n : needles)
    {
        if (Contains(hay, n))
        {
            return true;
        }
    }
    return false;
}

bool StartsWith(const std::string& s, const std::string& prefix)
{
    return s.size() >= prefix.size() && s.compare(0, prefix.size(), prefix) == 0;
}

bool EndsWith(const std::string& s, const std::string& suffix)
{
    return s.size() >= suffix.size() && s.compare(s.size() - suffix.size(), suffix.size(), suffix) == 0;
}

std::string RstripSlash(std::string s)
{
    while (!s.empty() && s.back() == '/')
    {
        s.pop_back();
    }
    return s;
}

bool HasHeaderControlChars(const std::string& s)
{
    for (char c : s)
    {
        if (c == '\r' || c == '\n' || c == '\0')
        {
            return true;
        }
    }
    return false;
}

std::string EnvOr(const char* name, const std::string& fallback)
{
    const char* v = std::getenv(name);
    return (v && *v) ? std::string(v) : fallback;
}

nb::object JsonLoads(const std::string& text)
{
    return nb::module_::import_("json").attr("loads")(nb::bytes(text.data(), text.size()));
}

std::string JsonDumps(nb::handle obj)
{
    nb::object hook = nb::module_::import_((PackageName() + ".blocks").c_str()).attr("json_default");
    nb::object out = nb::module_::import_("json").attr("dumps")(obj, "ensure_ascii"_a = false, "default"_a = hook);
    return TextOf(out);
}

std::string JsonDumpsPlain(nb::handle obj)
{
    nb::object out = nb::module_::import_("json").attr("dumps")(obj, "ensure_ascii"_a = false);
    return TextOf(out);
}

nb::object MakeBlock(nb::dict data)
{
    return nb::module_::import_((PackageName() + ".blocks").c_str()).attr("Block")(data);
}

nb::object MakeResponse(nb::list content, const std::string& stop_reason, nb::object usage)
{
    return nb::module_::import_("types").attr("SimpleNamespace")("content"_a = content, "stop_reason"_a = stop_reason, "usage"_a = usage);
}

nb::object MakeBlocksError(const char* class_name, nb::str message)
{
    return nb::module_::import_((PackageName() + ".blocks").c_str()).attr(class_name)(message);
}

void Echo(const std::string& text)
{
    nb::object sys_stdout = nb::module_::import_("sys").attr("stdout");
    sys_stdout.attr("write")(SafeUtf8Str(text));
    sys_stdout.attr("flush")();
}

void EchoGray(const std::string& text)
{
    Echo("\033[90m" + text + "\033[0m");
}

void PrintLine(const std::string& text)
{
    Echo(text + "\n");
}

nb::object EffortAttr(const char* name)
{
    return nb::module_::import_((PackageName() + ".effort").c_str()).attr(name);
}

nb::object ResolveEffort(const char* table, const std::string& effort, const std::string& model_id)
{
    return EffortAttr("resolve_effort")(EffortAttr(table), effort, model_id);
}

std::string ResolveGeminiLevel(const std::string& effort, const std::string& model_id)
{
    return TextOf(EffortAttr("resolve_gemini_level")(effort, model_id));
}

bool IsExplicitCompat(const std::string& compat)
{
    std::string c = Lower(Trim(compat));
    if (c.empty() || c == "auto")
    {
        return false;
    }
    nb::object valid = EffortAttr("VALID_FLAVORS");
    return static_cast<bool>(nb::bool_(valid.attr("__contains__")(nb::str(c.data(), c.size()))));
}

std::string ResolveFlavor(const std::string& compat, const std::string& model_id, const std::string& base_url, const std::string& default_flavor, bool warn_auto)
{
    std::string normalized = Lower(Trim(compat.empty() ? std::string("auto") : compat));
    if (IsExplicitCompat(normalized))
    {
        return normalized;
    }

    std::string model = Lower(model_id);
    std::string host = Lower(base_url);

    auto emit_warn = [&](const std::string& detected) {
        if (warn_auto && detected != default_flavor && (detected == "openai" || detected == "gemini"))
        {
            PrintLine("[!] Model '" + model_id + "': assuming compat flavor '" + detected + "' from base_url/model keywords. Set \"compat\" in MODEL_LIST to make it explicit.");
        }
    };

    if (IsOpenAIOSeries(model))
    {
        emit_warn("openai");
        return "openai";
    }

    for (const FlavorHint& hint : kModelHints)
    {
        for (const char* kw : hint.keywords)
        {
            if (Contains(model, kw))
            {
                emit_warn(hint.flavor);
                return hint.flavor;
            }
        }
    }

    for (const FlavorHint& hint : kHostHints)
    {
        for (const char* kw : hint.keywords)
        {
            if (Contains(host, kw))
            {
                emit_warn(hint.flavor);
                return hint.flavor;
            }
        }
    }

    return default_flavor;
}

std::string SystemText(nb::object system)
{
    if (IsStr(system))
    {
        return TextOf(system);
    }
    std::string out;
    if (IsList(system))
    {
        for (nb::handle block : system)
        {
            if (TextOf(Field(block, "type")) == "text")
            {
                std::string t = TextOf(Field(block, "text"));
                if (!t.empty())
                {
                    if (!out.empty())
                    {
                        out += "\n";
                    }
                    out += t;
                }
            }
        }
    }
    return out;
}

std::string ResultText(nb::object content)
{
    if (IsStr(content) || content.is_none())
    {
        return TextOf(content);
    }
    if (IsList(content))
    {
        std::string out;
        for (nb::handle block : content)
        {
            if (IsStr(block))
            {
                std::string t = TextOf(block);
                if (!t.empty())
                {
                    if (!out.empty())
                    {
                        out += "\n";
                    }
                    out += t;
                }
            }
            else if (TextOf(Field(block, "type")) == "text")
            {
                std::string t = TextOf(Field(block, "text"));
                if (!t.empty())
                {
                    if (!out.empty())
                    {
                        out += "\n";
                    }
                    out += t;
                }
            }
        }
        return out;
    }
    return TextOf(content);
}

bool IsMainThread()
{
    thread_local int t_is_main = -1;
    if (t_is_main < 0)
    {
        nb::module_ threading = nb::module_::import_("threading");
        nb::object current = threading.attr("current_thread")();
        nb::object main_t = threading.attr("main_thread")();
        t_is_main = current.is(main_t) ? 1 : 0;
    }
    return t_is_main == 1;
}

std::string RandomCallId()
{
    thread_local std::mt19937_64 rng{std::random_device{}()};
    static const char* kHex = "0123456789abcdef";
    std::string id = "call_";
    id.reserve(29);
    for (int i = 0; i < 24; ++i)
    {
        id += kHex[rng() & 15];
    }
    return id;
}

} // namespace dandelion

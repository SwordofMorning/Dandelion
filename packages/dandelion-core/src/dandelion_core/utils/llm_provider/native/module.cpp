/**
 * @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/native/module.cpp
 * @author Xiaojintao
 * @brief nanobind extension module entry point (_native) for dandelion_core.utils.llm_provider.
 * @version 0.1
 * @date 2026-11-28
 * @copyright Copyright (c) 2026
 */

#include "provider.hpp"

#include "http.hpp"

NB_MODULE(_native, m)
{
    using dandelion::Provider;

    m.doc() = "Dandelion native C++17 LLM provider (Messages, Chat Completions, Responses, Generate Content, Interactions).";

    nb::object pkg_obj = nb::getattr(m, "__package__", nb::str("dandelion_core.utils.llm_provider"));
    std::string pkg = dandelion::TextOf(pkg_obj);
    if (!pkg.empty())
    {
        dandelion::SetPackageName(pkg);
    }

    dandelion::HttpInit();

    nb::class_<Provider>(m, "Provider")
        .def(nb::init<const std::string&, const std::string&, const std::string&, const std::string&, const std::string&, const std::string&, const std::string&>(), "sdk_type"_a,
             "api_key"_a, "base_url"_a, "model_id"_a, "thinking"_a = "disabled", "effort"_a = "medium", "compat"_a = "auto")
        .def("safe_request", &Provider::SafeRequest, "payload"_a, "logger"_a = nb::none(), "log_tag"_a = "", "cancelled"_a = nb::none())
        .def("safe_stream_request", &Provider::SafeStreamRequest, "payload"_a, "logger"_a = nb::none(), "log_tag"_a = "", "echo"_a = true, "cancelled"_a = nb::none())
        .def("extract_text", &Provider::ExtractText, "content"_a)
        .def_prop_ro("model_id", &Provider::ModelId)
        .def_prop_ro("thinking", &Provider::Thinking)
        .def_prop_ro("effort", &Provider::Effort)
        .def_prop_ro("flavor", &Provider::Flavor)
        .def_prop_ro("protocol", &Provider::Protocol);

    m.def("normalize_messages", &dandelion::NormalizeMessages, "messages"_a);
    m.def("set_fallback_ca_bundle", &dandelion::HttpSetFallbackCaBundle, "path"_a);
}

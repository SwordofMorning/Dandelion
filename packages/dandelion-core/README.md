# dandelion-core

Reusable agent core and native C++17 multi-protocol LLM provider for Dandelion.

## Overview

`dandelion-core` bundles the Dandelion agent runtime (`MyAgent`, `BaseRuntime`, `BaseAgentHost`, tools, sub-agent pool, memory/skill/session managers) together with a compiled `nanobind` C++17 extension (`dandelion_core.utils.llm_provider._native`) that speaks five wire protocols directly over HTTPS without Python vendor SDKs:

- **Messages API** (`/v1/messages`, Anthropic + Sub2API cross-protocol bridge)
- **Chat Completions API** (`/v1/chat/completions`, OpenAI / DeepSeek / compatible relays)
- **Responses API** (`/v1/responses`, OpenAI stateless `store=false` + encrypted reasoning)
- **Generate Content API** (`:generateContent` / `:streamGenerateContent?alt=sse`, Gemini 2.5 / 3 / 4)
- **Interactions API** (`/v1beta/interactions`, Gemini stateless `store=false`)

## Installation

Install a precompiled wheel from PyPI:

```bash
pip install dandelion-core
```

For local editable development inside the Dandelion monorepo (requires a C++17 compiler, CMake, Ninja, and `libcurl` development files on Linux/macOS; WinHTTP is built into Windows):

```bash
pip install --no-build-isolation --no-deps -e packages/dandelion-core
```

## Quick Example

```python
from dandelion_core.utils.llm_provider import create_provider

provider = create_provider(
    sdk_type="Message",
    api_key="sk-...",
    base_url="https://api.anthropic.com",
    model_id="claude-sonnet-4-5",
    thinking="enabled",
    effort="high",
)
resp, err = provider.safe_stream_request(
    {"messages": [{"role": "user", "content": "Hello!"}], "max_tokens": 1024},
    echo=True,
)
```

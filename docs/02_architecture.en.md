# Architecture of Dandelion

This document outlines the architecture of the Dandelion monorepo, focusing on `packages/dandelion-core/`, `src/`, and `mk/`. It helps you quickly understand the layered OOP design, native C++17 LLM provider, and cross-platform build/release pipeline.

## 1. Main Architecture

### 1.1 Overview

Temporarily ignoring auxiliary files like `.gitignore` and `.env`, Dandelion is organized as a three-layer monorepo:

```sh
1. main.py                              # CLI & Remote entry point (-h/--help, -v/--version, remote ...)
    -> packages/dandelion-core/         # Standalone PyPI package `dandelion-core` (import `dandelion_core`)
    -> src/cli/                         # Interactive terminal host frontend
    -> src/remote/                      # 24/7 server daemon host frontend (excluded from CLI standalone)
2. mk/                                  # Unified local dev & CI build/script center + path locating
```

Including workspace and CI directories, the repository layout is:

```sh
1. main.py                              # Application entry point
    -> packages/dandelion-core/         # Standalone Core + native C++17/nanobind LLM engine
    -> src/cli/                         # Terminal CLI host (InteractiveCLI, CLIRuntime)
    -> src/remote/                      # Remote server daemon host (RemoteLoop, RemoteRuntime)
    -> llm/                             # Skills (llm/skill), global memories (llm/memory), prompt templates
    -> .log/                            # Persisted sessions (sess_xxx/) and logs
    -> .env/                            # Operator configs (cli.cfg, remote.cfg, devices.yaml, known_hosts)
2. mk/                                  # Unified build & script center (make.py, config/, lib/, scripts/)
3. docs/                                # Project documentation
4. .github/workflows/                   # CI/CD workflows (ascii-check.yml, release.yml)
```

### 1.2 Main Function

`main.py` stays concise and handles top-level dispatch and bootstrap:

1. Dispatches `python main.py remote ...` to `src/remote/service.py` (when available) and handles `-h`/`--help` and `-v`/`--version`.
2. Locates the workspace root (`mk/lib/paths.py::base_dir()`): when running from source it resolves the repository root; when running as a Nuitka standalone binary it resolves the parent of `bin/` (`<root>/bin/dandelion`). This root acts as the CLI sandbox boundary and intentionally does not accept runtime env overrides.
   > By default, CLI mode restricts file operations to the workspace root. Out-of-bounds access triggers an interactive y/N/C approval prompt via `BaseTool.check_workspace_permission()` and `CLIRuntime.approve()` (see [Chapter 7](#7-tool-classes)).
3. Loads `.env/cli.cfg` (falling back to legacy `.env/api.cfg`) via `dandelion_core.utils.config.load_api_config()`.
4. Initializes `SessionManager` (automatically restoring the most recently used session branch).
5. Constructs `MyAgent` with `cli_runtime()` (`CLIRuntime`), `CLIPromptBuilder`, and `config_dir=config_dir()`.
6. Starts `InteractiveCLI(agent_instance=agent, session_manager=session_mgr)` (`BaseAgentHost` subclass).

Overall request path:

```sh
main.py
  -> InteractiveCLI.run()                 # CLI Host Loop (inherits BaseAgentHost)
    -> _run_agent_loop()                  # Stop / Rollback / Continue checkpoint adapter
      -> dandelion_core.agent.runner.run_agent_loop()
        -> MyAgent.step()                 # Single LLM + tool-dispatch iteration
```

Remote starts via `python main.py remote ...` or `python -m src.remote ...` and drives the exact same `run_agent_loop()` from `RemoteLoop(BaseAgentHost)` with `RemoteRuntime(BaseRuntime)` (`sandbox=False`, `echo=False`), without importing terminal modules.

## 2. Build & Script Center (`mk/`)

`mk/` is the single unified build and script center for both local development and GitHub Actions CI.

Common local commands:

```bash
python mk/make.py            # Full Nuitka standalone build (same as 'all')
python mk/make.py all        # Build standalone executable into build/dandelion/
python mk/make.py native     # Build & editable-install packages/dandelion-core native extension
python mk/make.py wheel      # Build precompiled dandelion-core .whl into dist/
python mk/make.py sdist      # Build dandelion-core .tar.gz source distribution into dist/
python mk/make.py ascii      # Run 100% ASCII check across .py/.cpp/.hpp/.h/.ps1/.sh/.yml/CMakeLists.txt
python mk/make.py clean      # Remove caches, build/, dist/, build_info.py, *.egg-info, empty legacy dirs
python mk/make.py version    # Print project name and version
python mk/make.py sign       # Sign Windows executable (placeholder, P3)
```

On Windows, local MSVC + CMake + Ninja builds can also use `powershell -ExecutionPolicy Bypass -File mk/scripts/build_native.ps1 -Target build|wheel|clean`.

### 2.1 Architecture of `mk/`

```sh
mk/
├── config/
│   ├── build.config            # Nuitka & core package configuration (INI)
│   └── dandelion.ico           # Windows binary icon
├── lib/
│   ├── ascii_check.py          # Unified ASCII-only checker (.py/.cpp/.hpp/.h/.ps1/.sh/.yml/CMakeLists.txt)
│   ├── build_info.py           # Generated build metadata (created by make.py, removed by clean)
│   ├── __init__.py
│   └── paths.py                # Frozen/dev workspace root & stdlib fallback resolver
├── scripts/
│   ├── build_curl_static.sh    # Static zlib 1.3.1 + OpenSSL 3.0.15 + minimal HTTP/HTTPS libcurl 8.11.1
│   ├── build_native.ps1        # Windows MSVC (vswhere) + CMake + Ninja helper
│   └── docker_build.sh         # manylinux2014 Nuitka standalone build script (consumes prebuilt wheel)
├── __init__.py
└── make.py                     # Makefile-style target dispatcher
```

### 2.2 Nuitka Configuration (`mk/config/build.config`)

Because heavy Python LLM SDKs (`openai`, `anthropic`, `httpx`, `pydantic`, `anyio`) were replaced by the native C++17 engine, `mk/config/build.config` only bypasses lightweight pure-Python / data packages:

```ini
nofollow_imports = openpyxl,tabulate,certifi,urllib3,idna,charset_normalizer,requests,tzdata
copy_packages = openpyxl,tabulate,certifi,urllib3,idna,charset_normalizer,requests,tzdata
include_std_modules = http.cookies,http.cookiejar,email.parser,email.message,html.parser,urllib.parse,urllib.request,urllib.error,csv,ctypes,sqlite3,xml.etree.ElementTree
```

On Windows, `windows_jobs = 2` limits MSVC parallel heap usage to avoid `C1002: compiler is out of heap space`.

### 2.3 `mk/make.py` Standalone Pipeline (`target_all`)

1. **Metadata (`_gen_build_info`)**: Reads `pyproject.toml` (overridden by `DANDELION_VERSION` / `DANDELION_TARGET` in CI) and writes `mk/lib/build_info.py`.
2. **Compilation (`_run_nuitka`)**: Invokes Nuitka `--mode=standalone` with `--include-package=src.cli`, `--include-package=dandelion_core`, `--include-package-data=dandelion_core`, `--nofollow-import-to=src.remote`, `--include-module=_bisect`, and `--include-module=mk.lib.*`. Windows PE resources extract a purely numeric version via `_extract_numeric_pe_version()`.
3. **Post-Processing (`_postprocess`)**:
   - Moves `main.dist` into `build/dandelion/bin/`.
   - Copies the dependency closure of `copy_packages` (including `.dist-info`) and `.py`-only stdlib fallbacks into `bin/_stdlib_fallback/` (never copying host `lib-dynload/*.so` binaries).
   - Creates `.env/`, `llm/{memory,skill,sysprompt}/`, `.log/`, pre-fills `.env/cli.cfg`, and omits Remote-only `llm/sysprompt/remote.md` from CLI standalone bundles.
   - Generates symlink-safe `dandelion.sh` (Linux) or `dandelion.cmd` (Windows), strips ELF binaries, and forces `DT_RPATH=$ORIGIN` via `patchelf`.

### 2.4 CI Build, PyPI Publish, and 3-Artifact Release

`.github/workflows/release.yml` runs a unified pipeline:

1. **`meta`**: Resolves exact pushed tag (`${GITHUB_REF_VALUE#refs/tags/}`), validates PEP 440 version + numeric Windows PE version, and detects pre-releases (`-alpha`/`-beta`/`-rc`).
2. **`build-core-wheels` & `build-core-sdist`**: Uses `pypa/cibuildwheel@v3` on `manylinux2014` (`glibc 2.17`, `x86_64` & `aarch64`, statically linking cached `zlib 1.3.1` + `OpenSSL 3.0.15` + minimal HTTP/HTTPS `libcurl 8.11.1` via `mk/scripts/build_curl_static.sh`) and `windows-latest` (`AMD64`, WinHTTP). Produces `cp311-cp311` and forward-compatible `cp312-abi3` wheels, then verifies them on Python `3.11` and `3.13` inside `ubuntu:18.04`, `almalinux:8`, `ubuntu:24.04` (without `libcurl4`, checking `ldd`, `importlib.resources`, `SSHTool`, all 5 protocols, and verified OS CA TLS) and Windows.
3. **`build` (Streamlined 3-Artifact Nuitka Matrix)**: Builds `dandelion-win-x86_64` (Windows host), `dandelion-linux-x86_64` (`quay.io/pypa/manylinux2014_x86_64`), and `dandelion-linux-aarch64` (`quay.io/pypa/manylinux2014_aarch64`) via `mk/scripts/docker_build.sh`, consuming the prebuilt `cp311` wheel with zero C++ recompile while preserving `prompt-toolkit`. Each Linux binary is smoke-tested inside `ubuntu:18.04`, `almalinux:8`, and `ubuntu:24.04` containers.
4. **`publish-pypi` & `release`**: On `v*` tags, publishes `dandelion-core` via OIDC Trusted Publishing only after all wheel tests AND all 3 standalone builds pass, then uploads the 3 standalone zips plus wheels/sdist to GitHub Releases.

## 3. Package Layout Overview

1. **`packages/dandelion-core/src/dandelion_core/` (`dandelion-core`)**: Frontend-agnostic core package containing `agent/`, `subagent/`, `tool/`, `utils/`, `data/sysprompt/`, `runtime.py` (`BaseRuntime`, `DefaultRuntime`), `host.py` (`BaseAgentHost`), and `sync_protocol.py`.
2. **`src/cli/`**: Interactive terminal host (`InteractiveCLI(BaseAgentHost)`, `CLIRuntime(BaseRuntime)`, `CLIPromptBuilder(BasePromptBuilder)`, `SessionSync`, `CLIPrinter`).
3. **`src/remote/`**: 24/7 server daemon host (`RemoteLoop(BaseAgentHost)`, `RemoteRuntime(BaseRuntime)`, `RemotePromptBuilder(BasePromptBuilder)`, SQLite WAL store, Telegram/HTTP channels, background job workers, and Remote tools).

CLI and Remote both depend on `dandelion_core`; `dandelion_core` imports neither `src.*` nor `mk.*`.

## 4. Core Utilities (`dandelion_core/utils`)

```sh
dandelion_core/utils/
├── config/             # load_api_config(), load_remote_config(), load_devices_config()
├── embedding.py        # Pluggable text embedding providers
├── llm_provider/       # Native C++17 + nanobind multi-protocol HTTP/SSE engine
│   ├── __init__.py     # create_provider(), certifi fallback CA wiring
│   ├── blocks.py       # Canonical Block, PartialStreamError, RequestCancelledError
│   ├── effort.py       # Cross-protocol effort & Sub2API flavor mapping (incl. xhigh)
│   └── native/         # 14 C++17 source/header files (5 protocol codecs + curl/WinHTTP)
├── llm_request/        # SafeLLMClient & SubAgent ModelRegistry
└── logging/            # SessionManager & atomic SessionBackup
```

### 4.1 Config Loading (`config`)

`dandelion_core/utils/config/config.py` provides:

1. `load_api_config(file_path)` / `load_remote_config(file_path)` reading `cli.cfg` / `remote.cfg`.
2. `load_devices_config(devices_path=None, *, config_dir=None, workspace_dir=None)` reading `devices.yaml` for `SSHTool` (resolved via injected `config_dir` or `<workspace_dir>/.env`, with zero `mk.lib.paths` coupling).

### 4.2 Logging and Sessions (`logging`)

Runtime request/response records are appended by `SessionManager.log_api_call()` to the current session's `api.log` (rotated to `api.log.old` at 20 MB):

```sh
Agent/SubAgent -> SessionManager.log_api_call() -> .log/sess_xxx/api.log
```

`dandelion_core/utils/logging/session.py` (`SessionManager`) manages session branches (`sess_yyyymmdd_hhmmss_ms`) containing `api.log`, `archives/`, `artifacts/`, `history.log`, `memory/`, `meta.log`, `staged.md`, and `task_state.json`. `backup.py` (`SessionBackup`) provides atomic pre-turn snapshots supporting rollback even across mid-turn `prune_context(rollover=True)` session creation.

### 4.3 Native Multi-Protocol Engine (`llm_provider`)

`dandelion_core/utils/llm_provider/` replaces Python vendor SDKs with a native C++17 + `nanobind` (`cp312-abi3` on Python >= 3.12) extension (`_native`) implementing five wire protocols directly over `libcurl` (POSIX, with runtime OS CA + `certifi` fallback) or `WinHTTP` (Windows):

1. `api_messages.cpp`: Anthropic Messages API (`/v1/messages`)
2. `api_chat_completions.cpp`: OpenAI Chat Completions (`/v1/chat/completions`, including DeepSeek `reasoning_content` round-trip and OpenAI reasoning model parameter handling)
3. `api_responses.cpp`: OpenAI Responses API (`/v1/responses`, stateless `store=false` + `reasoning.encrypted_content`)
4. `api_generate_content.cpp`: Gemini `generateContent` / `streamGenerateContent`
5. `api_interactions.cpp`: Gemini `v1beta/interactions`

All 5 codecs support cooperative cancellation polling (`cancelled` callback -> `RequestCancelledError` within ~75ms) and cross-protocol `compat` / `effort` injection (`low` / `medium` / `high` / `xhigh` / `max`, including `gemini-4-neon` `xhigh`).

### 4.4 SubAgent Model Registry (`model_registry`) & Requests (`llm_request`)

`dandelion_core/utils/llm_request/model_registry.py` exposes `[Main] SUB_LIST` models directly to the LLM via `BaseDelegationTool` schema enums and catalogs ("Let LLM Decide"). `SafeLLMClient` calls native `normalize_messages()` before dispatch, passes `cancelled=self.runtime.cancelled` into `_native.Provider`, and avoids silent cross-model fallback on SubAgents.

### 4.5 CLI Host (`src/cli/`)

`InteractiveCLI(BaseAgentHost)` (`src/cli/cli.py`) uses `prompt_toolkit` (with `input` fallback) and `CLIRuntime(BaseRuntime)` (`src/cli/runtime.py`) to provide Git-style session commands (`branch`, `checkout`, `vim`, `load`, `status`, `commit`, `sync`, `remote`, `clear`), automatic interrupted-tool repair on startup/`checkout`, and unified `SIGINT` handling (`[S]top` / `[R]ollback` / `[C]ontinue`) that aborts in-flight native streams and resumes cleanly on `[C]ontinue` without dropping tool outputs.

## 5. Agent Core Design (`dandelion_core/agent/`)

```text
packages/dandelion-core/src/dandelion_core/
├── agent/              # MyAgent, runner, tools, sysprompt, memory, skill, compress, prune, interrupt
├── data/sysprompt/     # Packaged default templates (remote.md) loaded via importlib.resources
├── subagent/           # ISubAgent, SubAgent, SubAgentPool, dispatch, registry, result
├── tool/               # BaseTool, BaseDelegationTool, MediaToolBase, ShellCommandGuard, concrete tools
├── utils/              # config, logging, embedding, llm_provider (_native), llm_request
├── runtime.py          # BaseRuntime(ABC), DefaultRuntime / Runtime
├── host.py             # BaseAgentHost(ABC)
├── sync_protocol.py    # Shared CLI <-> Remote session sync wire protocol
└── __init__.py         # Lazy public exports
```

### 5.1 System Prompt (`sysprompt.py`)

`BasePromptBuilder(ABC)` (`dandelion_core/agent/sysprompt.py`) keeps the static system prompt byte-identical across a session for maximum KV prefix cache hits, while `MyAgent._inject_dynamic_context()` appends `[Dandelion Context]` (memory index, recalled memories, `task_state.json`) to the latest plain-text user message. Shared sections (`_build_subagent_orchestration_section()`, `_build_skills_section()`, `_build_language_section()`, `_build_memory_section()`) are reused across `DefaultPromptBuilder`, `CLIPromptBuilder`, and `RemotePromptBuilder`. Packaged templates under `dandelion_core/data/sysprompt/` are loaded via `importlib.resources` after checking `<workspace_dir>/llm/sysprompt/`.

### 5.2 Skills, Dual-Tier Memory, and Context Management

- **Skills (`dandelion_core/agent/skill.py`)**: Scans `llm/skill/` and exposes full content on demand via `LoadSkillTool`.
- **Memory (`dandelion_core/agent/memory.py`)**: Two-tier (global `llm/memory/` + session `.log/sess_<id>/memory/`) hybrid vector + keyword retrieval; ASCII policy enforced across source/script/workflow files by `python mk/make.py ascii` (`mk/lib/ascii_check.py`).
- **Context Compression & Pruning (`compress.py`, `prune.py`)**: Reactive overflow/budget compaction (`ContextCompressor`) paired with LLM-invoked `prune_context` (including `rollover=True` session rollover with rollback support).

## 6. SubAgent Design (`dandelion_core/subagent/`)

`ISubAgent(ABC)`, `SubAgent`, `SubAgentPool`, `TOOLSET_REGISTRY`, and `dispatch_tool_calls` (`dandelion_core/subagent/dispatch.py`) support bounded recursive delegation (`SUB_DEPTH`), deterministic DAG & disjoint-file validation (`plan_tool`), parallel wave execution (`SUB_CONCURRENCY`), and per-model multimodal tool adaptation (`MediaToolBase`).

## 7. Tool Classes (`dandelion_core/tool/`)

All tools inherit `BaseTool(ABC)` (`dandelion_core/tool/base_tool.py`) implementing `get_name()`, `get_description()`, `get_schema()`, and `execute(**kwargs)`. Delegation tools (`SpawnSubagentTool`, `RestrictedSpawnTool`, Remote's `SubmitTaskTool`) inherit `BaseDelegationTool(BaseTool, ABC)`.

Security boundaries at the tool layer:

1. `BaseTool` enforces workspace path checks, interactive approval via `runtime.approve()`, post-approval path re-verification (`_prepare_path()`), and `O_NOFOLLOW` opens (`_open_secure()`).
2. Shell tools (`BashTool`, `SSHTool`) share `ShellCommandGuard` (`dandelion_core/tool/shell/shell_guard.py`) — a single-pass quote-aware shell lexer handling escapes, heredocs/here-strings, redirections, subshells, command/parameter substitutions, globs/braces, wrapper verbs, and normalized sensitive/device path checks.

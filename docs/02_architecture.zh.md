# Architecture of Dandelion

本文档概述 Dandelion Monorepo 工程的整体架构，重点说明 `packages/dandelion-core/`、`src/` 与 `mk/` 三层的调用关系、原生 C++17 多协议引擎以及跨平台构建发布流程。

## 一、主体架构

### 1.1 概览

暂时忽略 `.gitignore`、`.env` 等辅助文件，Dandelion 采用清晰的三层 Monorepo 分层结构：

```sh
1. main.py                              # CLI 与 Remote 统一入口（支持 -h/--help、-v/--version、remote ...）
    -> packages/dandelion-core/         # 独立 PyPI 包 dandelion-core（导入名 dandelion_core）
    -> src/cli/                         # 本地终端宿主前端
    -> src/remote/                      # 24/7 远端守护进程宿主（不打入 CLI 独立产物）
2. mk/                                  # 本地开发与 CI 共用的统一构建脚本中心及路径定位
```

包含工作区与 CI 目录的完整结构如下：

```sh
1. main.py                              # 程序入口
    -> packages/dandelion-core/         # 独立内核 + 原生 C++17 / nanobind 多协议引擎
    -> src/cli/                         # 终端 CLI 宿主（InteractiveCLI、CLIRuntime）
    -> src/remote/                      # 远端服务宿主（RemoteLoop、RemoteRuntime）
    -> src/tool/                        # 共享热插拔工具落盘目录（CLI 与 Remote 零重建自动发现）
    -> llm/                             # Skill（llm/skill）、全局记忆（llm/memory）与提示词模板
    -> .log/                            # 持久化会话目录（sess_xxx/）与日志
    -> .env/                            # 运维配置（cli.cfg、remote.cfg、devices.yaml、known_hosts）
2. mk/                                  # 统一构建与脚本中心（make.py、config/、lib/、scripts/）
3. docs/                                # 项目文档
4. .github/workflows/                   # GitHub Actions 工作流（ascii-check.yml、release.yml）
```

### 1.2 主函数

`main.py` 保持简洁，完成入口分发与初始化：

1. 将 `python main.py remote ...` 分发到 `src/remote/service.py`，并直接处理 `-h`/`--help` 与 `-v`/`--version`。
2. 定位工作区根目录（`mk/lib/paths.py::base_dir()`）：源码运行取仓库根目录，Nuitka 独立二进制运行取 `bin/` 的父目录（`<root>/bin/dandelion`）。该目录作为 CLI 沙箱边界，不支持运行时环境变量覆盖。
   > CLI 默认仅允许访问工作区目录内的文件；越界操作会通过 `BaseTool.check_workspace_permission()` 与 `CLIRuntime.approve()` 触发交互式 y/N/C 审批（详见[第七章](#七工具类)）。
3. 通过 `dandelion_core.utils.config.load_api_config()` 加载 `.env/cli.cfg`（兼容回退 `.env/api.cfg`）。
4. 初始化 `SessionManager`（自动恢复最近使用的会话分支）。
5. 构造 `MyAgent`，注入 `cli_runtime()`（`CLIRuntime`）、`CLIPromptBuilder` 与 `config_dir=config_dir()`。
6. 启动继承 `BaseAgentHost` 的 `InteractiveCLI`。

整体调用路径：

```sh
main.py
  -> InteractiveCLI.run()                 # CLI 宿主循环（继承 BaseAgentHost）
    -> _run_agent_loop()                  # Stop / Rollback / Continue 中断检查点适配
      -> dandelion_core.agent.runner.run_agent_loop()
        -> MyAgent.step()                 # 单轮 LLM 请求与工具调度迭代
```

Remote 通过 `python main.py remote ...` 或 `python -m src.remote ...` 启动，由 `RemoteLoop(BaseAgentHost)` 注入 `RemoteRuntime(BaseRuntime)`（`sandbox=False`、`echo=False`）驱动同一个 `run_agent_loop()`，不依赖任何终端模块。

## 二、统一构建与脚本中心（`mk/`）

`mk/` 是本地开发与 GitHub Actions CI 的唯一构建与脚本中心。常用本地命令如下：

```bash
python mk/make.py            # 全量 Nuitka 独立构建（等同于 all）
python mk/make.py all        # 校验本地 dandelion-core 溯源并构建独立可执行程序到 build/dandelion/
python mk/make.py dev        # 本地优先原子安装 packages/dandelion-core 与根项目 [dev] 依赖
python mk/make.py native     # 编译并可编辑安装 packages/dandelion-core 原生扩展
python mk/make.py wheel      # 打包预编译 dandelion-core .whl 到 dist/
python mk/make.py sdist      # 打包 dandelion-core .tar.gz 源码包到 dist/
python mk/make.py ascii      # 检查 .py/.cpp/.hpp/.h/.ps1/.sh/.yml/CMakeLists.txt 100% ASCII 规范
python mk/make.py clean      # 清理缓存、build/、dist/、build_info.py、*.egg-info 与遗留空目录
python mk/make.py version    # 打印项目名与版本号
python mk/make.py sign       # Windows 可执行文件签名（占位，P3）
```

Windows 本地可通过 `powershell -ExecutionPolicy Bypass -File mk/scripts/build_native.ps1 -Target dev|build|wheel|clean` 自动定位 MSVC 并以 CMake + Ninja 编译原生扩展。

### 2.1 `mk/` 目录架构

```sh
mk/
├── config/
│   ├── build.config            # Nuitka 与核心包构建配置（INI）
│   └── dandelion.ico           # Windows 二进制图标
├── lib/
│   ├── ascii_check.py          # 统一 ASCII 检查器（覆盖源码、脚本、CMake 与 Workflow）
│   ├── build_info.py           # 构建元数据常量（由 make.py 生成，clean 自动清理）
│   ├── __init__.py
│   └── paths.py                # 源码/二进制工作区根目录定位与 stdlib fallback 挂载
├── scripts/
│   ├── build_curl_static.sh    # manylinux2014 静态 zlib 1.3.1 + OpenSSL 3.0.15 + libcurl 8.11.1 构建
│   ├── build_native.ps1        # Windows MSVC（vswhere）+ CMake + Ninja 辅助脚本
│   └── docker_build.sh         # manylinux2014 Nuitka 容器构建脚本（直接消费预编译 wheel）
├── __init__.py
└── make.py                     # Makefile 风格构建入口
```

### 2.2 Nuitka 配置（`mk/config/build.config`）

由于 `openai`、`anthropic`、`httpx`、`pydantic`、`anyio` 等重型 Python SDK 已彻底移除并由原生 C++17 引擎替代，`mk/config/build.config` 仅旁路拷贝少量轻量依赖与数据模块：

```ini
nofollow_imports = openpyxl,tabulate,certifi,urllib3,idna,charset_normalizer,requests,tzdata
copy_packages = openpyxl,tabulate,certifi,urllib3,idna,charset_normalizer,requests,tzdata
include_std_modules = http.cookies,http.cookiejar,email.parser,email.message,html.parser,urllib.parse,urllib.request,urllib.error,csv,ctypes,sqlite3,xml.etree.ElementTree,ssl,hmac,uuid,decimal,lzma,bz2
```

Windows 下通过 `windows_jobs = 2` 限制 MSVC 并行编译单元，避免触发 `C1002: compiler is out of heap space`。

### 2.3 `mk/make.py` 独立构建流程（`target_all`）

1. **本地内核溯源校验（`_verify_local_core_installed`）**：检查 `dandelion-core` 的 PEP 610 `direct_url.json`，强制确保其来自本仓库 `packages/dandelion-core`、`dist-wheels/` 或 `dist/`（防止误打入 PyPI 版本；显式设置 `DANDELION_ALLOW_EXTERNAL_CORE=1` 可豁免）。
2. **元数据生成（`_gen_build_info`）**：读取 `pyproject.toml`（CI 可通过 `DANDELION_VERSION` / `DANDELION_TARGET` 覆盖），生成 `mk/lib/build_info.py`。
3. **核心编译（`_run_nuitka`）**：调用 Nuitka `--mode=standalone`，显式包含 `--include-package=src.cli`、`--include-package=dandelion_core`、`--include-package-data=dandelion_core`、`--include-module=_bisect`、`--include-module=mk.lib.*`，并排除 `--nofollow-import-to=src.remote` 与 `--nofollow-import-to=src.tool`（保持插件工具可编辑热插拔）。Windows PE 版本号通过 `_extract_numeric_pe_version()` 提取纯数字版本。
4. **后处理（`_postprocess`）**：
   - 将 `main.dist` 迁入 `build/dandelion/bin/`；
   - 复制 `copy_packages` 依赖闭包与精简纯 `.py` 标准库（排除 `test/`、`tkinter/`、`site-packages/`、`lib-dynload/` 与原生动态库）到 `bin/_stdlib_fallback/`，使构建后零重建落盘的工具可直接 `import imaplib`/`smtplib`/`email.*` 等标准库；
   - 初始化 `.env/`、`llm/{memory,skill,sysprompt}/`、`tool/`、`.log/`，复制 `src/tool/` 与 `src/cli/tool/` 中的用户插件到 `tool/`，预填 `.env/cli.cfg`，并跳过 Remote 专属的 `llm/sysprompt/remote.md`；
   - 生成支持符号链接解引用的 `dandelion.sh`（Linux）或 `dandelion.cmd`（Windows），strip ELF 并用 `patchelf` 强制 `DT_RPATH=$ORIGIN`。

### 2.4 CI 构建、PyPI 发布与 3 产物发行矩阵

`.github/workflows/release.yml` 串联完整发布流水线：

1. **`meta`**：tag 推送时精确取 `${GITHUB_REF_VALUE#refs/tags/}`，严格校验 PEP 440 版本与数字 PE 版本，并对 `-alpha`/`-beta`/`-rc` 自动标记 `prerelease`。
2. **`build-core-wheels` 与 `build-core-sdist`**：基于 `pypa/cibuildwheel@v4.3` 在 `manylinux2014`（`glibc 2.17`，`x86_64` & `aarch64`，通过 `mk/scripts/build_curl_static.sh` 静态链接缓存的 `zlib 1.3.1` + `OpenSSL 3.0.15` + 极简 HTTP/HTTPS `libcurl 8.11.1`）与 `windows-latest`（`AMD64`，系统 WinHTTP）产出 `cp311-cp311` 与向后/向前兼容的 `cp312-abi3` Wheel，随后在 `ubuntu:18.04`、`almalinux:8`、`ubuntu:24.04`（不装 `libcurl4`，直接按本地 `.whl` 路径安装并校核 PEP 610 `direct_url.json` 与 SHA-256、`ldd`、`importlib.resources`、`SSHTool`、5 协议与真实 OS CA TLS 握手）及 Windows Python 3.11/3.13 隔离环境中全量验证。
3. **`build`（精简 3 产物 Nuitka 矩阵）**：分别构建 `dandelion-win-x86_64`、`dandelion-linux-x86_64`（`quay.io/pypa/manylinux2014_x86_64`）与 `dandelion-linux-aarch64`（`quay.io/pypa/manylinux2014_aarch64`）。Linux 侧通过 `mk/scripts/docker_build.sh` 直接消费预编译 `cp311` Wheel（零 C++ 重复编译、校验 `direct_url.json`、保留 `prompt-toolkit`），并将同一份二进制依次放入 `ubuntu:18.04`、`almalinux:8`、`ubuntu:24.04` 容器中落盘临时 `tool/smoke_imap_tool.py` 执行热插拔冒烟测试。
4. **`publish-pypi` 与 `release`**：仅当全部 Wheel 验证、sdist 与 3 份独立产物冒烟均通过后，在 `v*` tag 上先由 `publish-pypi` 通过 OIDC Trusted Publishing 发布 `dandelion-core` 至 PyPI，再由 `release` 将 3 份独立 zip 压缩包连同 Wheel/sdist 上传至 GitHub Releases。

## 三、包分层概览

1. **`packages/dandelion-core/src/dandelion_core/`（`dandelion-core`）**：前端无关内核，包含 `agent/`、`subagent/`、`tool/`、`utils/`、`data/sysprompt/`、`runtime.py`（`BaseRuntime`、`DefaultRuntime`）、`host.py`（`BaseAgentHost`）与 `sync_protocol.py`。
2. **`src/cli/`**：终端宿主（`InteractiveCLI(BaseAgentHost)`、`CLIRuntime(BaseRuntime)`、`CLIPromptBuilder(BasePromptBuilder)`、`SessionSync`、`CLIPrinter`）。
3. **`src/remote/`**：服务器守护进程宿主（`RemoteLoop(BaseAgentHost)`、`RemoteRuntime(BaseRuntime)`、`RemotePromptBuilder(BasePromptBuilder)`、SQLite WAL 存储、Telegram/HTTP 通道、后台作业与 Remote 工具）。

CLI 与 Remote 单向依赖 `dandelion_core`；`dandelion_core` 不导入任何 `src.*` 或 `mk.*`。

## 四、通用能力（`dandelion_core/utils`）

```sh
dandelion_core/utils/
├── config/             # load_api_config()、load_remote_config()、load_devices_config()
├── embedding.py        # 可插拔文本向量嵌入接口
├── llm_provider/       # 原生 C++17 + nanobind 多协议 HTTP/SSE 引擎
│   ├── __init__.py     # create_provider() 与 certifi CA 兜底挂载
│   ├── blocks.py       # 规范 Block、PartialStreamError、RequestCancelledError
│   ├── effort.py       # 跨协议 effort 与 Sub2API flavor 映射（含 xhigh）
│   └── native/         # 14 个 C++17 源码/头文件（5 协议编解码 + libcurl/WinHTTP 传输）
├── llm_request/        # SafeLLMClient 与 SubAgent ModelRegistry
└── logging/            # SessionManager 与原子 SessionBackup
```

### 4.1 配置加载（`config`）

`dandelion_core/utils/config/config.py` 提供 `load_api_config()`、`load_remote_config()` 以及支持显式注入 `config_dir`（默认 `<workspace_dir>/.env`，彻底解耦 `mk.lib.paths`）的 `load_devices_config()`。

### 4.2 日志与会话（`logging`）

运行期请求/响应记录由 `SessionManager.log_api_call()` 追加写入当前会话的 `api.log`（超过 20 MB 自动轮转为 `api.log.old`）：

```sh
Agent/SubAgent -> SessionManager.log_api_call() -> .log/sess_xxx/api.log
```

`dandelion_core/utils/logging/session.py`（`SessionManager`）管理会话分支目录（`api.log`、`archives/`、`artifacts/`、`history.log`、`memory/`、`meta.log`、`staged.md`、`task_state.json`）；`backup.py`（`SessionBackup`）支持包括跨 `prune_context(rollover=True)` 会话轮转在内的原子回滚恢复。

### 4.3 原生多协议引擎（`llm_provider`）

`dandelion_core/utils/llm_provider/` 以 C++17 + `nanobind`（Python >= 3.12 启用 `cp312-abi3` 稳定 ABI）直接通过 `libcurl`（POSIX，运行时自动探测系统 CA 并以 `certifi` 兜底）或 `WinHTTP`（Windows）实现五大协议：

1. `api_messages.cpp`：Anthropic Messages API（`/v1/messages`）
2. `api_chat_completions.cpp`：OpenAI Chat Completions（`/v1/chat/completions`，含 DeepSeek `reasoning_content` 完整回放与 OpenAI 推理模型参数适配）
3. `api_responses.cpp`：OpenAI Responses API（`/v1/responses`，无状态 `store=false` + `reasoning.encrypted_content`）
4. `api_generate_content.cpp`：Gemini `generateContent` / `streamGenerateContent`
5. `api_interactions.cpp`：Gemini `v1beta/interactions`

五大协议统一支持约 75ms 级协作中断轮询（`cancelled` 回调 -> `RequestCancelledError`）及跨协议 `compat` / `effort`（`low`/`medium`/`high`/`xhigh`/`max`，支持 `gemini-4-neon` 的 `xhigh` 档位）注入。

### 4.4 SubAgent 模型注册表与请求层（`llm_request`）

`dandelion_core/utils/llm_request/model_registry.py` 将 `[Main] SUB_LIST` 模型清单通过 `BaseDelegationTool` 暴露为工具 Schema 枚举与目录（Let LLM Decide）。`SafeLLMClient` 在发送前执行原生 `normalize_messages()`，将 `self.runtime.cancelled` 透传至 `_native.Provider`，并在 SubAgent 侧严格不做静默跨模型 fallback。

### 4.5 CLI 宿主（`src/cli/`）

`InteractiveCLI(BaseAgentHost)`（`src/cli/cli.py`）结合 `prompt_toolkit` 与 `CLIRuntime(BaseRuntime)`（`src/cli/runtime.py`）提供 Git 风格会话操作、启动/`checkout` 自动悬挂工具修复以及统一 `SIGINT` 菜单（`[S]top` / `[R]ollback` / `[C]ontinue`），支持实时中止在途原生流并在选择 `[C]ontinue` 时无损续跑。

## 五、Agent 核心设计（`dandelion_core/agent/`）

```text
packages/dandelion-core/src/dandelion_core/
├── agent/              # MyAgent、runner、tools、sysprompt、memory、skill、compress、prune、interrupt
├── data/sysprompt/     # 通过 importlib.resources 加载的内置模板（remote.md）
├── subagent/           # ISubAgent、SubAgent、SubAgentPool、dispatch、registry、result
├── tool/               # BaseTool、BaseDelegationTool、MediaToolBase、ShellCommandGuard 与具体工具
├── utils/              # config、logging、embedding、llm_provider（_native）、llm_request
├── runtime.py          # BaseRuntime(ABC)、DefaultRuntime / Runtime
├── host.py             # BaseAgentHost(ABC)
├── sync_protocol.py    # CLI <-> Remote 共用的会话同步线协议
└── __init__.py         # 延迟加载的公共导出
```

### 5.1 系统提示词（`sysprompt.py`）

`BasePromptBuilder(ABC)`（`dandelion_core/agent/sysprompt.py`）保持静态系统提示词字节级恒定以最大化 KV 前缀缓存命中率，动态上下文（记忆索引、被动召回与 `task_state.json`）由 `MyAgent._inject_dynamic_context()` 渲染为 `[Dandelion Context]` 附加在最新纯文本 user 消息尾部。`DefaultPromptBuilder`、`CLIPromptBuilder` 与 `RemotePromptBuilder` 共享 `_build_subagent_orchestration_section()`、`_build_skills_section()`、`_build_language_section()` 与 `_build_memory_section()`，模板加载优先读 `<workspace_dir>/llm/sysprompt/`，缺省回退至 `importlib.resources` 打包资源。

### 5.2 Skill、双层记忆与上下文治理

- **Skill（`dandelion_core/agent/skill.py`）**：扫描 `llm/skill/` 目录，由 `LoadSkillTool` 按需加载全文。
- **双层记忆（`dandelion_core/agent/memory.py`）**：全局（`llm/memory/`）+ 会话隔离（`.log/sess_<id>/memory/`）混合向量/关键词检索；全仓源码、脚本与工作流的 ASCII 规范由 `python mk/make.py ascii`（`mk/lib/ascii_check.py`）统一守门。
- **上下文压缩与裁剪（`compress.py`、`prune.py`）**：响应式溢出/预算压缩（`ContextCompressor`）配合 LLM 主动调用的 `prune_context`（含支持原子回滚的 `rollover=True` 会话轮转）。

## 六、子代理设计（`dandelion_core/subagent/`）

`ISubAgent(ABC)`、`SubAgent`、`SubAgentPool`、`TOOLSET_REGISTRY` 与并发调度器 `dandelion_core/subagent/dispatch.py` 支持有界递归委派（`SUB_DEPTH`）、零 LLM 确定性 DAG 与互斥文件校验（`plan_tool`）、波次并行执行（`SUB_CONCURRENCY`）以及按子模型能力动态裁剪/重绑定的多模态工具（`MediaToolBase`）。

## 七、工具类（`dandelion_core/tool/`）

所有工具继承 `BaseTool(ABC)`（`dandelion_core/tool/base_tool.py`），统一实现 `get_name()`、`get_description()`、`get_schema()` 与 `execute(**kwargs)`；委派类工具（`SpawnSubagentTool`、`RestrictedSpawnTool` 与 Remote 的 `SubmitTaskTool`）继承 `BaseDelegationTool(BaseTool, ABC)`。

工具层安全防线分为两类：

1. `BaseTool` 提供通用路径沙箱检查、通过 `runtime.approve()` 触发的交互审批、审批后路径二次核验（`_prepare_path()`）以及 POSIX `O_NOFOLLOW` 安全打开（`_open_secure()`），并在 CLI 沙箱下额外保护 `.env/`、`tool/`、`src/tool/`、`src/cli/tool/`、`src/remote/tool/` 及自定义 `TOOL_DIRS`。
2. Shell 工具（`BashTool`、`SSHTool`）共享 `ShellCommandGuard`（`dandelion_core/tool/shell/shell_guard.py`），以单趟引号感知词法分析器精确处理转义、heredoc/here-string、重定向、子 Shell、命令/参数替换、通配符/花括号、包装动词以及归一化敏感/设备/插件目录路径拦截。

### 7.1 零重建热插拔工具架构（`src/tool/`）

无需修改 `packages/dandelion-core` 或重新构建 C++/Nuitka，只需编写单个 `.py` 文件或子包并放入以下任一锚定目录（永不扫描不可信 `os.getcwd()`）：

1. `<root>/src/tool/`：CLI 与 Remote 共享的扩展工具；
2. `<root>/src/cli/tool/` 或 `<root>/src/remote/tool/`：宿主专属扩展工具；
3. `<root>/tool/`（如 Nuitka 独立产物 `dandelion.sh` / `dandelion.cmd` 同级目录）与 `[Main] TOOL_DIRS` 自定义目录。

内核侧由 `ToolContext`、`BaseTool.create_instance()`（按 `__init__` 签名自动依赖注入）、`ToolLoader`（基于 `importlib.util.spec_from_file_location` 故障隔离加载与防内置覆盖）及实例级 `ToolsetRegistry` 统一驱动；插件可在 `.env/cli.cfg` 或 `.env/remote.cfg` 中使用 `[Tool:<name>]` 声明配置。CLI 提供 `tools` 与 `reload` 实时热重载命令，Remote 在 `JobManager.submit()` 遇新 `toolset` 时自动刷新注册表且后台 Worker 每次执行均动态发现工具。

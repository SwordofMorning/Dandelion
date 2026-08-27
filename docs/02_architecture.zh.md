# Architecture of Dandelion

本文档旨在划定整个 dandelion 工程的架构，重点讲述 `src` 和 `mk` 的机制，这可以让你快速了解本工程的调用结构、构建逻辑。所有描述均与当前源码（`main.py`、`mk/`、`src/`、`.github/`）核对一致；标注"TODO/P3"的内容为已知待办。

## 一、主体架构

### 1.1 概览

暂时忽略 `.gitignore`、`.env` 等"辅助文件"，dandelion 可以被最简单地概括为：

```sh
1. main.py      # main() 函数入口
    -> src/         # 各个子模块的源码
2. mk/          # 编译构建脚本，同时 mk/lib/paths.py 还用于向 main.py 提供工作区根目录定位
```

加上其他的文件夹，那么工程可以表示为：

```sh
1. main.py      # main() 函数入口
    -> src/         # 各个子模块的源码
    -> llm/         # 保存 skill、全局持久化记忆（llm/memory、llm/skill）
    -> .log/        # 保存会话（sess_xxx/）、日志
    -> .env/        # 存储 API Key、routing、SSH 设备配置（api.cfg、devices.yaml）
2. mk/          # 编译构建脚本（Nuitka），同时提供路径定位
3. test/        # 测试代码/用例
4. docs/        # 项目文档
5. .github/     # GHA workflow
```

### 1.2 主函数

`main.py` 代码相对简洁，其中主要做如下几件事：

1. 定位工作区根目录（`mk/lib/paths.py::base_dir()`）：源码运行取仓库根目录（由 `__file__` 逐级上推得到）；Nuitka 二进制运行取 `bin/` 的父目录（产物布局为 `<root>/bin/dandelion`）。该根目录是沙箱边界，且刻意不支持任何运行时环境变量/配置覆盖。
    > 换句话说，dandelion 默认只允许对工作区根目录下的文件进行操作。越过边界的访问会触发 `BaseTool.check_workspace_permission()` 的交互式 y/N 人工审批（见[第七章](#七工具类)），而不是静默放行。
2. 加载 `.env/` 下的配置文件（`load_api_config()`）；
3. 初始化会话管理器（`SessionManager`，自动恢复最近使用的会话分支）；
4. 依据配置内容构造 Main Agent（`MyAgent`，含工具注册、SubAgent 池、记忆与 Skill 管理器）；
5. 将 Agent 和会话管理器赋值给 CLI（`InteractiveCLI`）并启动。

整体请求路径（各环节在后续章节展开）：

```sh
main.py
  -> InteractiveCLI.run()       # CLI Loop（交互层）
    -> _run_agent_loop()        # Agent Loop（多轮 tool 迭代）
      -> MyAgent.step()         # 单轮 Round-Trip（Tools Loop 的迭代）
```

## 二、构建脚本

`mk/` 相对简单，并且 `mk/lib/paths.py` 还为程序提供了工作区根目录定位的工作，因此我们先从项目的构建流程入手。如果你对工程构建没有兴趣，可以直接跳转到[下一章](#三src概览)。

构建工具选用 Nuitka（standalone 模式），只对项目中的"小文件"进行编译；较大的文件（例如，Google 的一系列 SDK）则采用拷贝 Python 源码的方式添加到构建产物中。

对于本地的构建，可以直接使用命令：

```bash
#   python mk/make.py            : full build (same as 'all')
#   python mk/make.py clean      : remove runtime caches and build artifacts
#   python mk/make.py all        : build standalone executable into build/
#   python mk/make.py version    : print project name and version
#   python mk/make.py sign       : sign Windows executable (placeholder, P3)
```

其输出结果将输出到 `build` 文件夹下。

对于 GHA 构建，则通过 `.github/workflows/release.yml -> mk/make.py` 的顺序来调用（详见[2.4 节](#24-ci构建发布)）。

### 2.1 mk 的架构

`mk` 文件夹下主要有如此的内容：

```sh
.
├── config                  # 配置文件夹
│   ├── build.config            # Nuitka 配置文件（INI）
│   └── dandelion.ico           # 二进制程序图标
├── __init__.py             # 导出包，主要用于 main.py 读取
├── lib                     # 辅助函数
│   ├── build_info.py           # 构建元数据常量（由 make.py 生成，勿手改）
│   ├── __init__.py             # 导出包
│   └── paths.py                # 为`源码`或者`二进制`程序定位工作区根目录
└── make.py                 # 构建主函数（Makefile 风格的 target 分发）
```

### 2.2 Nuitka 配置

`mk/config/build.config` 中定义了一些基本配置，其中值得注意的是：

```sh
# Nuitka config: Do not compile these heavy SDKs and libraries to C.
nofollow_imports = pydantic,anthropic,openai,google,pandas,openpyxl,tabulate,httpx,anyio,certifi,urllib3,idna,charset_normalizer,requests

# Custom config: Manually copy these packages to the dist folder (bypassing Nuitka).
copy_packages = pydantic,anthropic,openai,google,pandas,openpyxl,tabulate,httpx,anyio,certifi,urllib3,idna,charset_normalizer,requests

# Custom config: Force compile standard libraries required by the bypassed packages.
include_std_modules = http.cookies,http.cookiejar,email.parser,email.message,html.parser,urllib.parse,urllib.request,urllib.error,csv,ctypes,sqlite3
```

为了减少编译时间，对上述的 package 指定了不编译与手动拷贝。如果需要"全编译"，可以删除这几行配置。

同时，配置中的：

```sh
windows_jobs = 2
```

这里是为了避免 GHA 的 Windows 平台潜在的内存不足（MSVC 对单个编译单元堆占用大，会触发 "C1002: compiler is out of heap space"），而手动限制 `make -j` 的任务数量——针对大的 Python 文件，还是指 Google 的 SDK。

此外，`[build]` 段还定义了 `entry = main.py`、`output_dir = build`、`cache_dir = .nuitka-cache`、`icon`、`company/description`（写入 Windows PE 版本资源）等；`[sign]` 段目前 `enabled = false`，对应 `make sign` 占位目标（P3 规划）。

### 2.3 `mk/make.py`

在文件的最下方的 `main()` 中，根据用户的输入进行不同的函数分发（all/clean/version/sign/help）。对于核心构建函数 `target_all()` 来说，它按顺序执行如下的三个工作：

#### 2.3.1 前置信息

1. 读取元数据：通过 `_project_meta()` 从 `pyproject.toml` 中读取项目的名称、版本号；如果设置了 `DANDELION_VERSION` 环境变量（CI 中由 git tag 计算后注入，带前导 `v` 则剥离），则以其覆盖版本号。
2. 生成静态信息：通过 `_gen_build_info()` 生成 `mk/lib/build_info.py`，该文件将编译时间、版本等放入其中作为常量，以类似于 C/C++ 中 `.h` 的方式暴露给 `main.py`，例如：
    ```py
        VERSION = "0.0.0-dev-260807-abc123-ci"
        BUILD_MODE = "standalone"
        BUILD_DATE = "2026-08-10 09:30:28"
        PLATFORM = "linux"
    ```
    > 在 CI 中若设置了 `DANDELION_TARGET`（目标发行版，如 `18.04`），`PLATFORM` 会变成 `linux-x86_64-ubuntu18.04` 之类的形式，用于标识产物对应的 glibc 目标。

#### 2.3.2 核心编译

对于函数 `_run_nuitka()` 来说，其核心功能是将 Python 代码转换为二进制产物。对于这个函数来说：

1. 首先读取相应的配置，对于 Windows 平台，则确保其 `make -j` 为 `windows_jobs`（2），避免内存溢出；
2. 显式的配置 `anti-bloat` 插件与 `--noinclude-*-mode=nofollow`，去除 `pytest/unittest/setuptools` 等不必要的测试/构建库；
3. 通过 `nofollow_imports` 明确 Nuitka 不应该追踪哪些大文件（`--nofollow-import-to`）；
4. 通过 `include_std_modules` 引入哪些标准库必须包含，避免后期 C 编译时的错误；
5. Windows 下追加 PE 版本资源（`--product-name/--product-version/--file-version`，版本号必须为纯数字，故使用 pyproject 静态版本而非 CI tag）与图标参数；
6. 最后通过 `subprocess.run` 调用 Nuitka（`--mode=standalone`、`--include-package=src`、`--include-module=mk.lib.*`），生成构建产物。

#### 2.3.3 后处理

由于在上一节中，为了节约编译时间和资源，我们手动排除了一些库，因此在这里我们需要重新将其引入到构建产物中。我们通过函数 `_postprocess()` 来实现这些工作。

1. 目录迁移：将 Nuitka 默认生成的 `<entry>.dist`，重命名并拷贝到 `build/<name>/bin` 下；
    > 外层是工作区，将 bin 文件包裹在内层。最终产物布局为：
    > ```sh
    > build/Dandelion/
    > ├── bin/                  # 实际二进制 + 拷贝的依赖
    > ├── .env/                 # api.cfg（由 api.cfg.example 预填）
    > ├── llm/                  # memory/、skill/（空目录以 .keep 占位）
    > ├── .log/
    > ├── dandelion.sh/.cmd     # 启动脚本
    > └── version.txt           # 版本号 + 可选 target=ubuntu18.04 标识
    > ```
2. 将之前排除的依赖项重新拷贝到 `bin` 中；
    > `_copy_dependency_closure()` 利用 BFS 搜索相应的库、被其调用的底层库、以及 `.dist-info`，确保运行时不会出现版本校验错误。
3. 重新引入标准库（`_copy_stdlib_fallback()`）；
    > 以 `requests` 为例，假如我们让 Nuitka 忽略构建这个库，而这个库用到了 `ssl`。如果我们在核心代码中没有用到 `ssl`，那么可能出现遗失的问题。
    > 因此，我们利用 Python 内置的 `ast` 扫描在 (2) 中拷贝的依赖，然后将其中被使用的标准库放入 `bin/_stdlib_fallback` 目录中（迭代至不动点，覆盖 `ssl -> _ssl` 这类传递依赖）。该目录在 `paths.py` 中被追加到 `sys.path` 末尾，保证永不遮蔽 Nuitka 编译模块。
4. 构建工作区：为实现"开箱即用"，在 `build/Dandelion` "根目录"下创建 `.env`、`llm/memory`、`llm/skill` 和 `.log/` 等文件夹（空目录以 `.keep` 占位）；若存在 `.env/api.cfg.example`，则拷贝为 `.env/api.cfg` 并删除对应 `.keep`；
5. 生成运行脚本：为了可以在 `build/Dandelion` 这一层中运行程序，而不需要进到 `build/Dandelion/bin` 中，在外层放置了 `dandelion.cmd` 和 `dandelion.sh`，用于执行 `bin` 中的实际二进制程序；
    > Linux 侧采用 `exec` 包装脚本而非符号链接，以避免双路径解析（`$ORIGIN` 与 Nuitka 模块目录）不一致导致的崩溃；同时若环境存在 `patchelf`，会对二进制强制 `--force-rpath --set-rpath $ORIGIN`（DT_RPATH 而非 DT_RUNPATH），保证 `bin/` 内置的 libpython 等库先于 `LD_LIBRARY_PATH` 被搜索，防止外部 libpython 抢先加载。
6. 收尾：写入 `version.txt`；遍历产物清理 `__pycache__`。

### 2.4 CI 构建发布

`.github/workflows/release.yml` 承担"版本计算 + 矩阵构建 + 上传产物"：

1. `meta` job 计算版本：push tag 时用 `git describe --tags --match "v*"`；否则生成 `v0.0.0-dev-<yyMMdd>-<short-sha>`；非正式发布（未打 tag）再追加 `-ci` 后缀。版本通过 `DANDELION_VERSION` 注入 `mk/make.py`。
2. `build` job 矩阵：
    - Windows x86_64：GitHub host runner（Nuitka + MSVC + clcache）；
    - Linux x86_64 与 arm64：各 5 个 Ubuntu 目标（18.04/20.04/22.04/24.04/26.04），全部走 `docker-build.sh` 容器构建。
3. 为什么 Docker per-distro：Nuitka standalone 产物链接构建机的 glibc，新发行版上构建的二进制在旧发行版上会报 `GLIBC_x.y not found`。在目标发行版自己的容器内构建可保证产物只依赖该发行版的 glibc；GitHub runner 镜像只覆盖 22.04/24.04，因此 18.04/20.04 无论如何都要走容器，统一用 Docker 使流程确定。
4. 缓存策略：venv 与 Nuitka 缓存均按 `os_name-arch-distro` 隔离（编译产物携带构建容器的 glibc 要求，跨发行版共享缓存会"投毒"产物）。曾使用 sccache，Linux 增量构建在 `codeobject.c` 处崩溃，而 Windows 的 clcache 无此问题，故已移除 sccache 改用 clcache。
5. 门禁：`.github/workflows/ascii-check.yml` 在主干分支执行 `test/ascii_check.py`，对 `src/`、`mk/`、`main.py` 等做 ASCII-only 扫描（与 5.3 的语言策略呼应，任何非 ASCII 源码字符都会导致构建失败）。

## 三、src 概览

`src/` 下的源码按照功能分为了四个模块：

1. `core`，Agent 的核心逻辑，涉及上下文压缩、记忆管理等；
2. `subagent`，涉及 SubAgent 的"Agent 池"、消息传递结构等；
3. `tool`，Agent 调用的各类工具；
4. `utils`，包含配置调用、CLI、日志管理等多种通用的接口。

## 四、通用类

`src/utils` 下提供了一些通用的方法，比如读取配置；但也有一些复杂的、涉及 Agent 的交互逻辑，比如 CLI。为此，这里将和 Agent 按照相关性，按照从低到高的顺序来讲述工程的核心逻辑。

在本章节中的[CLI 小节](#46-cli)中了解 Agent Loop 是如何实现之后，我们进入[下一章](#五agent核心设计)，查看 Agent 类的具体实现。

```sh
.
├── cli                 # CLI 终端
├── config              # 配置加载
├── __init__.py
├── llm_provider        # API SDK 动态配置
├── logging             # 日志/会话管理
├── routing             # 模型路由
└── safe_llm            # LLM request 实现
```

### 4.1 配置加载 config

`src/utils/config/config.py` 提供了读取 `.env/` 下各项配置的函数。其中：

1. `load_api_config()` 读取 `api.cfg`：
2. `load_devices_config()` 读取 `devices.yaml`，包含 SSH 设备的别名、地址、认证方式等，在 `src/tool/shell/ssh_tool.py` 中的执行阶段被调用（LLM 只可见别名，不可见凭据）。

### 4.2 日志与会话 logging

`src/utils/logging/logger.py`（`AgentLogger`）主要职责是将程序运行中的数据写入日志文件（按天 `YYYY_MM_DD.log`），供上层 `SessionManager` 复用同一套序列化逻辑：

```sh
Agent/SubAgent -> log_api_call() -> `.log/`
```

`src/utils/logging/session.py`（`SessionManager`）则为 CLI 实现了会话管理功能，每一个会话都以 `sess_yyyymmdd_hhmmss_ms` 的格式存放在 `.log/` 下。每一个会话文件夹下的内容有如下内容：

```log
.
├── api.log
├── archives/            # （压缩时产生）上下文完整备份，追加式、可恢复
├── artifacts
│   ├── call_00_52wgHo88lnWaSbwa1LVi0390.txt
│   ├── call_00_d3RYUb6puaMqEbF6qHPz5544.txt
│   ├── ...
│   └── call_01_QwIjMQ4IxwJxKq6P2WP18965.txt
├── history.log
├── memory
│   ├── feat_staged_retry_decisions.md
│   ├── feat_staged_retry_impl_status.md
│   ├── feat_staged_retry_recheck_status.md
│   └── MEMORY.md
├── meta.log
├── staged.md            # （有草稿时产生）CLI 暂存区草稿
└── task_state.json
```

其中：

1. `api.log` 记录了每一次 Agent 向 LLM 的 request 以及其回复的原始数据（json 格式）；
2. `artifacts/` 下是 tool call 的大输出产物。`agent.step()` 对任何工具输出超过 8000 字符（`MAX_INLINE_CHARS`）时，将完整结果落盘为 `<tool_use_id>.txt`，上下文内只保留截断后的前 8000 字符加"输出已保存到 <绝对路径>"的指针，LLM 可用 read_file 按需分次读取，从而避免上下文爆炸、推迟压缩；
3. `history.log` 是完整的消息数组（`messages`，含 user/assistant/tool_result 块以及注入的 `[Dandelion Context]` 块），是每次构建 payload 的持久化来源，压缩与恢复均基于它；
4. `memory/` 下包含了 LLM 认为需要存放的 session（local）层记忆，随会话分支隔离；
5. `meta.log` 保留了会话本身的信息；
6. `task_state.json` 是 LLM 自己规划的任务，包含目标（target）、代办（todos）、已完成（completed）三个内容，用于保持 LLM 的注意力；
7. `archives/` 与 `staged.md` 并非所有会话都有：前者在上下文压缩时产生，后者在 `vim`/`load` 编辑草稿后产生。

### 4.3 各 API 请求 llm_provider

`src/utils/llm_provider` 下针对 OpenAI、Anthropic 和 Google AI 的不同 SDK 的 API 请求格式进行了派生。其中 `base.py` 提供了通用的基类方法（`LLMProvider`，抽象 `safe_request/safe_stream_request/extract_text`），剩下的几个类（`OpenAIProvider`、`AnthropicProvider`、`GeminiProvider`）由它派生而来，`SafeLLMClient._create_provider()` 按 `SDK_TYPE` 选择。

对于 Anthropic API 来说，其中 DeepSeek 使用的 Reasoning Effort (Think Level) 采用 OpenAI/Google 风格的 `low`、`med`、`high` 等字符，而不是像 Anthropic 一样使用 Think Budget（`thinking/effort` 注入逻辑在 Provider 内完成）。

对于 OpenAI 和 Google AI 来说，其实现了基本的框架与内容，后续需要更新为 Response API 和 Interactive API（TODO）。

### 4.4 模型路由 routing

`src/utils/routing` 旨在解决两个问题：

1. SubAgent 的模型自动选择：高推理能力的模型执行复杂任务、低推理能力的模型执行简单的工具调用任务。
2. 部分 LLM 服务商存在 TPM、RPM 等访问限制，需要实现一个本地的速率控制，避免 429 too many requests。

注意，这里的 Rate Limit 并没有实现一个全局的、动态的控制逻辑，只是基于滑动窗口实现了单次启用程序的计数统计。这里需要后期完善（代码中标注 `@todo Future: global real-time limit checking`）。

### 4.5 LLM 请求 safe_llm

`src/utils/safe_llm` 核心职责是为程序发送 request 到 LLM，其流程如下：

```py
# - Main Agent
#      MyAgent.step() -> payload -> SafeLLMClient.safe_stream_request() -> default provider (Main Agent's Model) -> request to LLM
# - Sub Agent
#      PlanTool.execute()  -> SafeLLMClient.route_request() -> Model by Routing -> cached provider -> request to LLM
#      SubAgent.run()      -> SafeLLMClient.route_request() -> Model by Routing -> cached provider -> request to LLM
```

其中：

1. Main Agent 将在它自身的 Tool iterate 中直接通过 `safe_stream_request()` 向 LLM 发起 request 请求；
2. SubAgent 则需要通过 `route_request`：
    - 首先，根据它（类）的属性（成员：task_description/toolset/depth），选择合适的 LLM（在 `SUB_LIST` 中配置，经 `RoutingPolicy` 条件匹配 + 速率限制）；
    - 然后，确认没有超过 Rate Limit；
    - 最后，发送请求。同一 alias 的 provider 实例在 `_provider_cache` 中缓存（带线程锁），每个 SubAgent 模型使用自己的 `thinking/effort` 配置。
3. 无论哪条路径，发送前都会做消息归一化 `_normalize_messages()`：将相邻的同 role 消息合并（例如压缩摘要产生的连续 user 消息），满足 Anthropic/Gemini 对严格角色交替的要求。
4. 重试策略：
    - Main Agent：1 次初始 + `_LLM_RETRY_COUNT=3` 次重试，指数退避 `(2, 4, 8)` 秒，对 400/401/429/500/连接错误统一处理；
    - SubAgent 路由：选中模型 1 次初始 + 2 次重试，仍失败则沿 `get_fallback_chain()`（SUB_LIST 顺序、排除当前 alias）逐个尝试，全部失败才返回错误。

### 4.6 CLI

`src/utils/cli/cli_printer.py` 提供了终端彩色打印的功能，无其他核心功能。

`src/utils/cli/interactive_cli.py` 基于 `prompt_toolkit`（缺失时降级为普通 `input`），模仿 git 风格的命令行控制，提供了一套交互式的 CLI。其中值得关注的点有：

1. `_build_completer()` 中定义了命令以及其补全的次级命令；
2. `run()` 中通过死循环实现"CLI Loop"，其内部又分为两层：
    ```sh
        # Interactive **CLI** Loop
        run(True)
            # **Agent** Loop
            -> _run_agent_loop()
                # Call **Tools** Loop/Iterate
                -> agent.step()
    ```
    其中 `_run_agent_loop()` 在两种时机被调用：`commit` 命令发送消息后的工具循环，以及 `run()` 每次等待输入前对 history 尾部的"后台检查"——若发现最后一条是 tool_result（上次中断遗留的未完成工具轮次），则先自动续跑 Agent Loop，失败则丢弃该悬挂的 tool_use/tool_result 对（`_drop_pending_tool_turn()`），避免下一次请求 400；
3. staged 暂存区：`vim`/`load` 编辑的消息先写入当前会话的 `staged.md`，提示符中以 `*` 显示脏标记。

## 五、Agent 核心设计

在本章节，将介绍 `src/core` 下的设计，主要是 Main Agent 中的记忆管理、上下文压缩、Skill 加载。这里我们将从最简单的 `sysprompt` 入手，开始讲解每一个文件的作用。在完成本章节的阅读之后，对 Main Agent 有了初步的了解，我们将在下一章中阅读 SubAgent 的设计。

### 5.1 sysprompt

Dandelion 的 sysprompt 被以常量的方式定义在 `src/core/sysprompt.py`（`PromptBuilder`）中，而不是采用文件加载的方式来实现。对于 sysprompt，其中的拼接方式是：

1. 首先，定义 `[Dandelion]` 身份与环境信息，以便于 LLM 使用正确的 shell 命令；
2. 其次，如果启用了 SubAgent，那么添加相应的 SubAgent 调用规则；
3. 接着，拼接 Skill 目录；
4. 然后，定义安全规则、语言策略、回复文本风格；
5. 最后，明确 Memory 系统使用指南（双层记忆、`[Dandelion Context]` 块语义、`remember`/`update_state` 工具用法）。

需要强调的是：为了最大程度提高缓存命中率，**动态内容**（记忆索引、相关记忆摘要、task state）并没有直接插入到 `system` 中，而是由 `agent._inject_dynamic_context()` 渲染为 `[Dandelion Context]` 块追加在最新纯文本 user 消息的结尾。这样，system prompt 在整个会话期间保持字节级不变，DeepSeek 等提供方的前缀缓存可以在工具循环的各轮迭代间持续命中。整个 request 的构建就像是：

```json
{
    "system": "You are Dandelion, ......",
    "messages": [
        {
            "role": "user",
            "content": "用户输入或者 Tools 调用结果。[Dandelion Context (auto-injected reference data)]\nRelevant Memories: ...... [Dandelion Context End]"
        },
    ],
}
```

### 5.2 skill

`src/core/skill.py`（`SkillManager`）负责将 `llm/skill` 下的各 skill 文件扫描注册为"名称 -> 描述/内容"的目录，`get_catalog()` 将 `- name: description` 列表插入到 sysprompt 中。如果 LLM 需要读取 skill 全文，则通过 `load_skill` 工具（`LoadSkillTool`）按名称获取。

### 5.3 memory

Dandelion 的记忆管理可以简要地分为：

1. 索引：`agent._render_dynamic_context()` -> `MemoryManager.get_index_text()`；
2. 读取：`agent._get_memories()` -> `MemoryManager.load_memories_string()`；
3. 写入：`MemoryTool.execute()` -> `MemoryManager.write_memory()`；

同时，记忆分为两层：

1. Global：存放在 `llm/memory` 下，跨会话持久（编码风格、架构决策、用户偏好）；
2. Local：存放在 `.log/sess_<id>/memory/` 下，随会话分支隔离，`checkout` 切换会话时通过 `session_manager` 动态解析目录，无需重建 agent。

记忆写入的目录（层级）由 LLM 通过 `remember` 工具的 `scope` 参数（`global`/`session`）指定；session 层未配置时写入 session 层会明确报错，而不是落盘在 global 中。

值得注意的是，为了方便 LLM 进行索引（提取），记忆中的内容均采用英文（ASCII）撰写（语言策略）。在 `src/core/memory.py` 中，以 `class MemoryManager` 的形式将记忆的读、写进行了封装；工具层（`remember`/`update_state`）会拒绝非 ASCII 输入并提示翻译，`test/ascii_check.py` 则保证源码/记忆文件本身不混入非 ASCII。详细的函数调用链可以在 `@file` 部分注释中查看。

### 5.4 agent

`src/core/agent.py` 中定义了 Main Agent（`MyAgent`）所需要的全部功能。在前述的[CLI 小节](#46-cli)中我们可以看到，agent 中的 `step()` 并不是传统意义上的 Agent Loop，而是每一次 Agent Loop 中的、针对 Tools 的 Loop/Iterate。因此，让我们直接从 `step()` 开始阅读。

#### 5.4.1 step()

1. 首先，我们构造 sysprompt；
2. 其次，如果最新消息是纯文本 user 消息，则注入动态上下文（`[Dandelion Context]`）；工具循环迭代因最新消息是 tool_result 则不会重复注入；
3. 然后，进行上下文管理：检查 token 预算，如果超过软上限则压缩（详见 5.4.2）；
4. 接着，构建 payload；
5. 之后，进行 request；
6. 最后，根据 LLM 的返回结果（`stop_reason == "tool_use"` 时），顺序执行所有工具调用，将结果写入 result 并追加回 history；单个工具输出超过 8000 字符时执行大输出 Offload（见 4.2）。

于是，我们完成了这一次的 `step()`。如果 LLM 任务没有到结束，那么通过 `step()` 的返回值（`continue_loop`），CLI 会判断：是发起下一次 `step()`，继续循环；还是将结束 Agent Loop，将终端交还给用户。

#### 5.4.2 上下文压缩

Dandelion 上下文的 token 计算并没有引入分词器，而是简单地通过 `_estimate_tokens()` 进行启发式估算（ASCII 约 4 字符/token，CJK 约 1.5 字符/token）。上下文的软上限在 `_soft_token_limit()` 中计算：

```py
soft_limit = MAX_CONTEXT_TOKENS - MAX_TOKENS - 固定开销(sysprompt + tool schemas)
```

即：上下文窗口是"共享"的（history + 输出 + 开销必须一起放进去），因此压缩阈值提前为输出预算与固定开销留出空间，避免 provider 返回 400 context-length 错误。

压缩在 `_compact_context()` 中实现：

1. 备份：将完整 history 以追加式 JSON 写入会话的 `archives/history_<len>_<时间戳>.json`（可恢复）；
2. 选择压缩范围：head 取前 5 条（并确保不以未配对的 assistant tool_use 结尾），recent 取最近 15 条之内、以"最近的纯文本 user 消息"为安全断点（保证 tool_use/tool_result 配对不被拆散）；找不到安全断点则只保留 head + summary；
3. 通过 LLM 构建摘要（独立请求，XML 格式输出：goals/completed/decisions/artifacts/pending，摘要输入超过 200k 字符时截断中间部分；失败则回落基础剪断）；
4. 以 `head + summary(user 角色) + recent` 的形式重写 history 并持久化，同时失效记忆缓存；
5. 压缩后校验：若仍超限（如 MAX_CONTEXT_TOKENS 配置过小），打印一次显式告警，避免每轮重复触发摘要调用。

## 六、子代理设计

子 agent 的设计大致如下：

```sh
.
├── pool.py             # SubAgent "池"（调度器，由 MyAgent 持有）
├── registry.py         # 工具集注册机（TOOLSET_REGISTRY + resolve_toolset）
├── result.py           # SubAgent 与 parent 之间传递消息的数据结构
├── i_subagent.py       # 虚基类（ISubAgent，为特化 subagent 保留接口）
└── subagent.py         # "通用" subagent（SubAgent，实际实现）
```

`i_subagent` 旨在为特化 subagent 保留相同的调用接口，例如，我们期望通过特化的 subagent 来撰写相应的报告文档。在 `subagent.py` 内部，同样与 Main Agent 一样有 sysprompt、agent-loop 等机制，但是省去了复杂的上下文、记忆管理：

1. 每一个 subagent 都在 `run()` 中完成它的全部工作（上限 30 轮工具迭代），然后将结果封装为 `SubAgentResult`返回；
2. subagent 也能派生出"subsubagent"，但是有其最大递归深度的限制：只有 `depth < max_depth` 时才会注入受限的 `RestrictedSpawnTool`，达到最大深度后系统提示词明确要求"必须自己完成，不得再委派"；
3. 每个 subagent 有一个专属的 ID（`sa-<hex>`），以便于 pool 进行管理、为 parent 界定返回值与日志记录；
4. 子代理的系统提示词由三部分拼接：父代理 `plan_tool` 生成的 `role_prompt` + 深度信息（depth 0 / 中间层 / 最大深度三种措辞）+ 安全规则（Trust Boundary 提示注入防御）；
5. LLM 请求走 `route_request()`，即按任务复杂度自动选择模型，而不是复用 Main Agent 的模型。

`pool.py` 创建了一个 agent 池（`SubAgentPool`，`MyAgent` 持有的单例）来管理 subagents。工具集注册表 `registry.py` 预定义了五类（`minimal`/`filesystem`/`code_analysis`/`data_processing`/`full`），LLM 通过名称引用。通常来说，subagent 的调用方式为：

```sh
Main Agent (Root)
    # 不生成 subsubagent
    -> plan_tool:                               # 创建多少个不同的任务，并分配给 subagent
    -> SpawnSubagentTool:                       # 创建 agent 池
        -> pool.create_and_run:                 # agent 池创建调用对象
            -> resolve_toolset:                 # 获取工具集
            -> SubAgent.run:                    # SubAgent-Loop
        <- parent.sub_results.append(result)    # 返回结果（to_context_string 回灌父代理）
    # 生成 subsubagent
    -> plan_tool:
    -> SpawnSubagentTool:
        -> pool.create_and_run:
            -> resolve_toolset:
            -> SubAgent.run:
                -> RestrictedSpawnTool:                     # 生成 subsubagent（depth+1，工具集受限）
                    -> pool.create_and_run:                 # SubSubAgent-Loop
                    # ...
                    <- parent.sub_results.append(result)    # 返回结果
            # ...
        <- parent.sub_results.append(result)                # 返回结果
```

详细设计可以参考相应代码中的注释部分。

## 七、工具类

`src/tool` 下的工具类都由 `base_tool.py`（`BaseTool`）派生而来，通过 `get_name()/get_description()/get_schema()/execute()` 四个接口向 Agent 暴露，注册于 `MyAgent._init_tools()`。

安全模型（沙箱保护）是工具层最重要的横切关注点：

1. `BaseTool` 中实现了大部分基于路径的沙箱保护：
    - 检查目标路径是否严格位于工作区根目录内，越界时弹出交互式 y/N 人工审批；
    - `_prepare_path()` 在审批后对解析路径做二次校验；
    - `_open_secure()` 在 POSIX 下使用 `O_NOFOLLOW` 打开文件，防止审批后最终组件被符号链接交换而逃逸；
2. 少部分针对特殊工具（比如 shell）的防护则在派生类中增加额外的成员函数进行保护：
    - `bash` 工具：黑名单子串（`../`、`/etc`、`.env`、`~/` 等）+ 分词后的路径 token 沙箱检查；120 秒超时、输出 50KB 截断；
    - `ssh` 工具：适用于对嵌入式设备的远程控制，因此采用命令匹配而非目录控制的方式来进行管理（比如，我可能想要访问 `/etc/init.d/` 下的各种启动脚本）。

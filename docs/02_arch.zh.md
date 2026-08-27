# Architecture of Dandelion

本文档旨在划定整个dandelion工程的架构，重点讲述`src`和`mk`的机制，这可以让你快速了解本工程的调用结构、构建逻辑。

## 一、主体架构

### 1.1 概览

暂时忽略`.gitignore`、`.env`等“辅助文件”，dandelion可以被最简单地概括为：

```sh
1. main.py      # main() 函数入口
    -> src/         # 各个子模块的源码
2. mk/          # 编译构建脚本，同时`mk/lib/paths.py`还用于向 main.py 提供PWD路径定位
```

加上其他的文件夹，那么工程可以表示为：

```sh
1. main.py      # main() 函数入口
    -> src/         # 各个子模块的源码
    -> llm/         # 保存skill、全局持久化记忆
    -> .log/        # 保存会话、日志
    -> .env/        # 存储API Key、routing、SSH config等配置
2. mk/          # 编译构建脚本，同时`mk/lib/paths.py`还用于向 main.py 提供PWD路径定位
3. test/        # 测试代码/用例
4. docs/        # 项目文档
5. .github/     # GHA workflow
```

### 1.2 主函数

`main.py`代码相对简洁，其中主要做如下几件事：

1. 为sandbox定位当前的pwd，避免dandelion潜在的击穿沙箱的风险。
    > 换句话说，dandelion只默认允许对当前pwd下的文件进行操作。当然，你可以可以通过在其他位置，或者配置到环境变量中来调用dandelion来解除这一限制。
2. 加载`.env/`下的配置文件；
3. 初始化会话管理器；
4. 依据配置内容构造Main Agent；
5. 将Agent和会话管理器赋值给CLI并启动。

## 二、构建脚本

`mk/`相对简单，并且`mk/lib/paths.py`还为程序提供了pwd定位的工作，因此我们先从项目的构建流程入手。如果你对工程构建没有兴趣，可以直接跳转到[下一章](#三src概览)。

构建工具选用Nuitka，只对项目中的“小文件”进行编译；较大的文件（例如，Google的一系列SDK）则采用拷贝Python源码的方式添加到构建产物中。

对于本地的构建，可以直接使用命令：

```bash
#   python mk/make.py            : full build (same as 'all')
#   python mk/make.py clean      : remove runtime caches and build artifacts
#   python mk/make.py all        : build standalone executable into build/
#   python mk/make.py version    : print project name and version
#   python mk/make.py sign       : sign Windows executable (placeholder)
```

其输结果将输出到`build`文件夹下。

对于GHA构建，则通过`.github/workflows/release.yml -> mk/make.py`的顺序来调用。

### 2.1 mk的架构

`mk`文件夹下主要有如此的内容：

```sh
.
├── config                  # 配置文件夹
│   ├── build.config            # Nuitka 配置文件
│   └── dandelion.ico           # 二进制程序图标
├── __init__.py             # 导出包，主要用于main.py读取
├── lib                     # 辅助函数
│   ├── build_info.py           # 仅用于添加一些常量，用于打印
│   ├── __init__.py             # 导出包
│   └── paths.py                # 为`源码`或者`二进制`程序定位PWD
└── make.py                 # 构建主函数
```

### 2.2 Nuitka配置

`mk/config/build.config`中定义了一些基本配置，其中值得注意的是：

```sh
# Nuitka config: Do not compile these heavy SDKs and libraries to C.
nofollow_imports = pydantic,anthropic,openai,google,pandas,openpyxl,tabulate,httpx,anyio,certifi,urllib3,idna,charset_normalizer,requests

# Custom config: Manually copy these packages to the dist folder (bypassing Nuitka).
copy_packages = pydantic,anthropic,openai,google,pandas,openpyxl,tabulate,httpx,anyio,certifi,urllib3,idna,charset_normalizer,requests

# Custom config: Force compile standard libraries required by the bypassed packages.
include_std_modules = http.cookies,http.cookiejar,email.parser,email.message,html.parser,urllib.parse,urllib.request,urllib.error,csv,ctypes,sqlite3
```

为了减少编译时间，对上述的package指定了不编译与手动拷贝。如果需要“全编译”，可以删除这几行配置。

同时，配置中的：

```sh
windows_jobs = 2
```

这里是为了避免GHA的Windows平台潜在的内存不足，而手动限制`make -j`的任务数量 - 针对大的Python文件，还是指Google的SDK。

### 2.3 `mk/make.py`

在文件的最下方的`main()`中，根据用户的输入进行不同的函数分发，对于核心构建函数`target_all()`来说，它按顺序执行如下的三个工作：

#### 2.3.1 前置信息

1. 读取元数据：通过`_project_meta()`从`pyproject.toml`中读取项目的名称、版本号；如果是GHA环境，则读取git tag以获取精确的版本号。
2. 生成静态信息：通过`_gen_build_info()`生成`mk/lib/build_info.py`，该文件将编译时间、版本等放入其中作为常量，以类似于C/C++中`.h`的方式暴露给`main.py`，例如：
    ```py
        VERSION = "0.0.0-dev-260807-abc123-ci"
        BUILD_MODE = "standalone"
        BUILD_DATE = "2026-08-10 09:30:28"
        PLATFORM = "linux"
    ```

#### 2.3.2 核心编译

对于函数`_run_nuitka()`来说，其核心功能是将Python代码转换为二进制产物。对于这个函数来说：

1. 首先读取相应的配置，对于Windows平台，则确保其`make -j`应该为2，避免内存溢出；
2. 显式的配置`anti-bloat`，去除`pytest/unittest`等不必要的测试库；
3. 通过`nofollow_imports`明确Nuitka不应该追踪哪些大文件；
4. 通过`include_std_modules`引入哪些包应该必要包含，避免后期C编译时的错误
5. 最后通过`subprocess.run`来调用Nuitka，生成构建后产物。

#### 2.3.3 后处理

由于在上一节中，为了节约编译时间和资源，我们手动排除了一些库，因此在这里我们需要重新将其引入到构建产物中。我们通过函数`_postprocess()`来实现这些工作。

1. 目录迁移，将Nuitka默认生成的`<entry>.dist`，重命名并拷贝到`build/Dandelion/bin`下；
    > 外层是工作区，将bin文件包裹在内层。
2. 将之前排除的依赖项重新拷贝到`bin`中；
    > `_copy_dependency_closure()`利用BFS搜索相应的库、被其调用的底层库、以及`.dist-info`，确保运行时不会出现版本校验错误。
3. 重新引入标准库；
    > 以`requests`为例，假如我们让Nuitka忽略构建这个库，而这个库用到了`ssl`。如果我们在核心代码中没有用到`ssl`，那么可能出现遗失的问题。
    > 因此，我们利用Python内置的`ast`扫描在(2)中拷贝的依赖，然后将其中被使用的标准库放入`_stdlib_fallback`目录中。
4. 构建工作区；
    > 为例实现“开箱即用”，因此在`build/Dandelion`“根目录”下创建`.env`、`llm/memory`、`llm/skill`和`.log/`等文件夹，并将`.sample`拷贝入其中并移除后缀。
5. 生成运行脚本；
    > 为了可以在`build/Dandelion`这一层中运行程序，而不需要进到`build/Dandelion/bin`中，在外层放置了`dandelion.cmd`和`dandelion.sh`，用于执行`bin`中的实际二进制程序。

## 三、src概览

`src/`下的源码按照功能分为了四个模块：

1. `core`，Agent的核心逻辑，涉及上下文压缩、记忆管理等；
2. `subagent`，涉及SubAgent的“Agent池”、消息传递结构等；
3. `tool`，Agent调用的各类工具；
3. `utils`，包含配置调用、CLI、日志管理等多种通用的接口。

## 四、通用类

`src/utils`下提供了一些通用的方法，比如读取配置；但也有一些复杂的、涉及Agent的交互逻辑，比如CLI。为此，这里将和Agent按照相关性，按照从低到高的顺序来讲述工程的核心逻辑。

在本章节中的[CLI小节](#46-cli)中了解Agent Loop是如何实现之后，我们进入[下一章](#五agent核心设计)，查看Agent类的具体实现。

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

`src/utils/config/config.py`提供了读取`.env/`下各项配置的函数。其中：

1. `load_api_config()`用于读取`api.cfg`，包含API Key、模型名称等配置项，在`main.py`的最初阶段被调用；
2. `load_devices_config()`读取`devices.yaml`，包含SSH、串口的设备号，在`src/tool/shell/ssh_tool.py`中的执行阶段被调用。

### 4.2 日志与会话 logging

`src/utils/logging/logger.py`主要职责是将程序运行中的数据写入日志文件：

```sh
Agent/SubAgent -> log_api_call() -> `.log/`
```

`src/utils/logging/session.py`则为CLI实现了会话管理功能，每一个会话都以`sess_yyyymmdd_hhmmss_ms`的格式存放在`.log/`下。每一个会话文件夹下的内容有如下内容：

```log
.
├── api.log
├── artifacts
│   ├── call_00_52wgHo88lnWaSbwa1LVi0390.txt
│   ├── call_00_d3RYUb6puaMqEbF6qHPz5544.txt
│   ├── call_00_ET_pDF2P1ouXrYlgUznHSfw1398.txt
│   ├── call_00_ET_qXiy3nRHIkejK7HmsswS1871.txt
│   ├── call_00_XWPproKZtD09QOr9HMpM4566.txt
│   ├── call_01_ChyyGZs00djfLGsVV9No1945.txt
│   ├── call_01_ET_UmuzMbEiDTM9EOBC5Qfm2566.txt
│   └── call_01_QwIjMQ4IxwJxK6qP2WP18965.txt
├── history.log
├── memory
│   ├── feat_staged_retry_decisions.md
│   ├── feat_staged_retry_impl_status.md
│   ├── feat_staged_retry_recheck_status.md
│   └── MEMORY.md
├── meta.log
└── task_state.json
```

其中：

1. `api.log`记录了每一次Agent向LLM的request以及其回复的原始数据（json格式），包含tools、sysprompt、user、assist等内容；
2. `artifacts/`下是tool call的产物，对于read file来说，一次只能允许读取8000个字符；因此需要先将结果保存到本地，然后让LLM分多次读取；
3. `history.log`简化后的`api.log`，只包含用户发送的信息、LLM回复的结果（即`messages`字段中的内容），就像是Web/APP端对话一样；
4. `memory/`下包含了LLM认为需要存放的“local”记忆；
5. `meta.log`保留了会话本身的信息，比如会话的名字、最后使用的时间等；
6. `task_state.json`是LLM自己规划的任务，包含目标、代办、已完成三个内容，用于保持LLM的注意力。

### 4.3 各API请求 llm_provider

`src/utils/llm_provider`下针对OpenAI、Anthropic和Google AI的不同SDK的API请求的格式进行了派生。其中`base.py`提供了通用的基类方法，剩下的几个类由`LLMProvider`派生而来。

对于Anthropic API来说，其中DeepSeek使用的Reasoning Effort (Think Level)采用OpenAI/Google风格的`low`、`med`、`high`等字符，而不是像Anthropic一样使用Think Budget。

对于OpenAI和Google AI来说，其实现了基本的框架与内容，后续需要更新为Response API和Interactive API。

### 4.4 模型路由 routing

`src/utils/routing`旨在解决两个问题：

1. SubAgent的模型自动选择，高推理能力的模型执复杂任务、低推理能力的模型执行简单的工具调用任务；
2. 部分LLM服务商存在TPM、RPM等访问限制，需要实现一个本地的速率控制，避免429 too many request.

注意，这里的Rate Limit并没有实现一个全局的、动态的控制逻辑，只是实现了单次启用程序的计数统计。这里需要后期完善。

### 4.5 LLM请求 safe_llm

`src/utils/safe_llm`核心职责是为程序发送request到LLM，其流程如下：

```py
# - Main Agent
#      MyAgent.step() -> payload -> SafeLLMClient.safe_stream_request() -> default provider (Main Agent's Model) -> request to LLM
# - Sub Agent
#      PlanTool.execute()  -> SafeLLMClient.route_request() -> Model by Routing -> cached provider -> request to LLM
#      SubAgent.run()      -> SafeLLMClient.route_request() -> Model by Routing -> cached provider -> request to LLM
```

其中：

1. Main Agent将在它自身的Tool iterate中直接通过`safe_stream_request()`向LLM发起request请求；
2. SubAgent则需要通过`route_request`：
    - 首先，根据它（类）的属性（成员），选择合适的LLM（在config中配置）；
    - 然后，再确认没有超过Rate Limit；
    - 最后，发送请求。

### 4.6 CLI

`src/utils/cli/cli_printer.py`提供了终端彩色打印的功能，无其他核心功能。

`src/utils/cli/interactive_cli.py`基于`prompt_toolkit`，模仿git风格的命令行控制，提供了一套交互式的CLI。其中值得关注的点有：

1. `_build_completer()`中定义了命令以及其补全的次级命令；
2. `run()`中通过死循环来实现“Agent Loop”：
    ```py
        # Interactive **CLI** Loop
        run(True)
            # **Agent** Loop
            -> _run_agent_loop()
                # Call **Tools** Loop/Iterate
                -> agent.step()
    ```
3. 其余内容则是命令分发、异常管理等。

## 五、Agent核心设计

在本章节，将介绍`src/core`下的设计，主要是Main Agent中的记忆管理、上下文压缩、Skill加载。这里我们将从最简单的`sysprompt`入手，开始讲解每一个文件的作用。在完成本章节的阅读之后，对Main Agent有了初步的了解，我们将在下一章中阅读SubAgent的设计。

### 5.1 sysprompt

Dandelion的systprompt被以常量的方式定义在`src/core/sysprompt.py`中，而不是采用文件加载的方式来实现。对于sysprompt，其中的拼接方式是：

1. 首先，定义`[Dandelion]`身份，以便于后期拼接Memory和Target (Task State)；
2. 其次，定义环境是Linux还是Windows，以便于LLM使用正确的shell命令；
3. 然后，如果启用了SubAgent，那么添加相应的SubAgent调用规则；
4. 接着，定义Skill、安全规则、语言规则、回复文本风格；
5. 最后，明确Memory读取逻辑。

为了最大程度提高缓存命中率，我们没有将Memory直接插入到`system`中而是将其放在`user`的结尾部分，这样，整个request的构建就像是：

```json
{
    "system": "You are Dandelion, ......",
    "messages": [
        {
            "role": "user",
            "content": "用户输入或者Tools调用结果。[Dandelion Context (auto-injected reference data)]\nRelevant Memories: ...... [Dandelion Context End]"
        },
    ],
}
```

### 5.2 skill

`src/core/skill.py`负责将`llm/skill`中的各类skill提取出头部，插入到sysprompt中。如果LLM需要读取skill，则通过`skill_tool`来调用。

### 5.3 memory

Dandelion的记忆管理可以简要地分为：

1. 索引：`sysprompt.build()`；
2. 读取：`agent._get_memories()`；
3. 写入：`MemoryTool.execute()`。

同时，记忆分为两层：

1. Global：存放在`llm/memory`下；
2. Local：存放在`.log/sess_<id>/memory/`下。

记忆写入的目录（层级）由LLM指定。

值得注意的是，为了方便LLM进行索引（提取），记忆中的内容均采用英文（ASCII）攥写。在`src/core/memory.py`中，以`class MemoryManager`的形式将记忆的读、写进行了封装。详细的函数调用链可以在`@file`部分注释中查看。

### 5.4 agent

`src/core/agent.py`中定义了Main Agent所需要的全部功能。在前述的[CLI小节](#46-cli)中我们可以看到，agent中的`step()`并不是传统意义上的Agent Loop，而是每一次Agent Loop中的、针对Tools的Loop/Iterate。因此，让我们直接从`step()`开始阅读。

#### 5.4.1 step()

1. 首先，我们构造sysprompt；
2. 其次，我们进行上下文管理（包含记忆、task state等）：
    - 插入用户/Tool的输入内容；
    - 判断是否超过相应的阈值，如果超过，则进行压缩；
3. 然后，我们构建payload；
4. 接着，我们进行request；
5. 最后，根据LLM的返回结果，调用相应的工具，并将结果写入result中。

于是，我们完成了这一次的`step()`，如果LLM任务没有到结束，那么通过`step()`的返回值，CLI会判断：是发起下一次`step()`，继续循环；还是将结束Agent Loop，将终端交还给用户。

#### 5.4.2 上下文压缩

Dandelion上下文的token计算并没有引入分词器，而是简单地通过`_estimate_tokens()`进行估算。上下文的压缩在`_compact_context()`中实现：

1. 备份现有上下文；
2. 选择压缩上下文范围；
3. 通过LLM构建摘要；
4. 以`head + summary + tail`的形式重新返回上下文。

## 六、子代理设计

子agent的设计大致如下：

```sh
.
├── pool.py             # “线程(agent)”池
├── registry.py         # subagent注册机
├── result.py           # subagent与parent之间传递消息的数据结构
├── i_subagent.py       # 虚基类
└── subagent.py         # “通用”subagent
```

`i_subagent`旨在为特化subagent保留相同的调用接口，例如，我们期望通过特化的subagent来攥写相应的报告文档。在`subagent.py`内部，同样与Main Agent一样有sysprompt、agent-loop等机制，但是省去了复杂的上下文、记忆管理：

1. 每一个subagent都在`run()`中完成它的全部工作，然后将结果封装到`result.py`中返回；
2. subagent也能派生出“subsubagent”，但是有其最大递归深度的限制；
3. 每个subagent有一个专属的ID，以便于pool进行管理、为parent界定返回值与日志记录。

`pool.py`创建了一个agent池来管理subagents，通常来说，subagent的调用方式通常为：

```sh
Main Agent (Root)
    # 不生成 subsubagent
    -> plan_tool:                               # 创建多少个不同的任务，并分配给subagent
    -> SpawnSubagentTool:                       # 创建agent池
        -> pool.create_and_run:                 # agent池创建调用对象
            -> resolve_toolset:                 # 获取工具集
            -> SubAgent.run:                    # SubAgent-Loop
        <- parent.sub_results.append(result)    # 返回结果
    # 生成 subsubagent
    -> plan_tool:
    -> SpawnSubagentTool:
        -> pool.create_and_run:
            -> resolve_toolset:
            -> SubAgent.run:
                -> RestrictedSpawnTool:                     # 生成subsubagent
                    -> pool.create_and_run:                 # SubSubAgent-Loop
                    # ...
                    <- parent.sub_results.append(result)    # 返回结果
            # ...
        <- parent.sub_results.append(result)                # 返回结果
```

详细设计可以参考相应代码中的注释部分。

## 七、工具类

`src/tool`下的工具类都由`base_tool.py`派生而来，在`base_too.py`中实现了大部分的基于路径的沙箱保护，少部分（比如针对shell）的特殊防护则在派生类中增加额外的成员函数进行保护。
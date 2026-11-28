[![English](https://img.shields.io/badge/English-README-blue)](docs/README.en.md)   [![Run](https://img.shields.io/badge/下载并运行-8CA1AF?logo=readthedocs&logoColor=fff)](docs/01_begin.zh.md)

# Dandelion

一个 CLI 式的 Agent 工具，底层内核以独立 PyPI 包 `dandelion-core`（含原生 C++17 + `nanobind` 多协议 LLM 引擎，支持 Messages / Chat Completions / Responses / Generate Content / Interactions 五大协议）发布，上层宿主采用 Nuitka 构建跨平台独立可执行产物。仓库采用 Monorepo 分层结构：

1. `packages/dandelion-core/` 提供独立的 `dandelion-core` 包（`dandelion_core.agent`、`dandelion_core.subagent`、`dandelion_core.tool`、`dandelion_core.utils`、`dandelion_core.runtime`、`dandelion_core.host`）；
2. `src/cli/` 提供终端 CLI Loop（继承 `BaseAgentHost` / `CLIRuntime`），`src/remote/` 提供 Remote 外层 Event Loop（继承 `BaseAgentHost` / `RemoteRuntime`）；
3. `mk/` 是本地开发与 CI 共用的统一构建与脚本中心（`mk/make.py`、`mk/scripts/`、`mk/lib/`）。


## 一、功能特性

- CLI 式交互，无 GUI；
- 独立 `dandelion-core` 包，支持本地优先开发（`python mk/make.py dev`）及在 Ubuntu 18.04–26.04、CentOS 8+ 与 Windows 上直接通过 `pip install dandelion-core` 安装预编译二进制 Wheel；
- 零重建工具热插拔：将继承 `BaseTool` 的单文件或插件包放入 `src/tool/`、`src/cli/tool/`、`src/remote/tool/` 或独立二进制旁的 `tool/` 目录，无需修改 `dandelion-core` 或重新构建即可生效（CLI 支持 `tools` / `reload` 命令）；
- 原生 C++17 LLM 引擎，彻底移除 `openai`、`anthropic`、`httpx`、`pydantic`、`python-dotenv` 等重型 Python SDK；
- 完善的日志、Git 风格会话分支管理与三选一中断控制（`Stop` / `Rollback` / `Continue`）；
- 手动将会话原始历史和元数据备份到 Remote；
- 复用同一内核的 Dandelion Remote：后台任务、Telegram 与持久化提醒，详见 [Remote 使用说明](docs/03_remote.zh.md)。

## 二、使用源码

Python 版本建议采用 3.11 及以上。

### 2.1 克隆仓库

```bash
git clone git@github.com:SwordofMorning/Dandelion.git
# 或者
git clone https://github.com/SwordofMorning/Dandelion.git
# 进入目录
cd Dandelion
```

### 2.2 安装依赖与构建

建议先通过 `conda` 或 `venv` 管理环境：

```bash
# 方式 A（推荐，本地优先）：原子化可编辑安装本地 packages/dandelion-core + 根项目开发依赖
python mk/make.py dev
# 等价 pip 命令：
# pip install -e packages/dandelion-core -e ".[dev]"
# 或使用 uv（根目录 pyproject.toml 已声明 [tool.uv.sources] 本地优先）：
# uv sync --extra dev

# 方式 B（无本地 C++ 编译器时的回退方案）：安装 PyPI 预编译内核 + 本地 CLI/Remote
pip install dandelion-core
pip install --no-deps -e .
pip install prompt-toolkit requests

# Windows PowerShell 本地原生构建与开发安装辅助脚本（CMake + Ninja + MSVC）
powershell -ExecutionPolicy Bypass -File mk/scripts/build_native.ps1 -Target dev
```

常用统一构建命令（`mk/make.py`）：

```bash
python mk/make.py dev      # 本地优先可编辑安装 packages/dandelion-core 与根项目 [dev] 依赖
python mk/make.py native   # 仅编译并可编辑安装 packages/dandelion-core 原生扩展
python mk/make.py wheel    # 将 packages/dandelion-core 打包为 .whl 到 dist/
python mk/make.py all      # 校验本地 dandelion-core 溯源并构建 Nuitka 独立可执行文件到 build/dandelion/
python mk/make.py ascii    # 检查 .py/.cpp/.hpp/.h 源码是否满足 100% ASCII 规范
python mk/make.py clean    # 清理缓存、build/、dist/ 及遗留空目录
```

### 2.3 配置环境

对于初次使用程序的用户来说，需要在 `.env/` 下配置：

1. `cli.cfg`（可参考 `.env/cli.cfg.example`），包含 API Key、LLM 模型、推理档位与路由配置；
2. `devices.yaml`（可参考 `.env/devices.yaml.example`），如果需要使用 SSH 访问其他设备，请配置此表。

### 2.4 运行程序

完成上述配置后，可以直接通过 `python main.py` 来运行程序：

```log
(dandelion) xiaojintao@U26S:~/Workspace/Dandelion$ python main.py
[>] Initializing Dandelion Project (v0.1.0)...
[+] Agent Initialization Successful. Model: deepseek-v4-flash
[+] MAX_TOKENS=160000, MAX_CONTEXT_TOKENS=819200

================ SHELL READY ================
[+] Bash-style Tab completion enabled (Powered by prompt_toolkit).

================= WORKSPACE =================
 Git-Style Session Management:
   branch -a             : List all available sessions.
   branch -d <name/id>   : Delete one session.
   checkout <name/id>    : Switch to an existing session.
   checkout -b <name>    : Create and switch to a new session.

 Vim-Style Editing:
   vim                   : Open system editor (Vim/Notepad) to draft prompt.
   load <filepath>       : Load a local file into the prompt buffer.

 Core Operations:
   status                : View current session and staged buffer.
   commit                : Send the staged buffer to LLM.
   clear                 : Clear the staged buffer.
   help / quit / exit    : System commands.
====================================================

deepseek-v4-flash:(staged_msg)> exit
[*] Terminating Dandelion Shell. Goodbye.
```

其中：

1. `MAX_TOKENS` 表示 LLM 最大输出长度；
2. `MAX_CONTEXT_TOKENS` 表示 LLM 上下文长度，包含前述 LLM 的输出长度；
3. `prompt_toolkit` 用来提供 `tab` 补全功能；
4. `deepseek-v4-flash` 是当前的 Main Agent 名称；
5. `staged_msg` 是当前会话（Session Branch）的名称。

## 三、使用发行版

### 3.1 下载

在 [Releases][releases-url] 页面，Dandelion 提供精简的 **3 份跨平台独立可执行产物**（以及同步发布到 PyPI 的 `dandelion-core` 预编译 Wheel）：

1. `dandelion-linux-x86_64-<version>.zip` — 基于 `manylinux2014`（`glibc 2.17` + 静态 OpenSSL 3 / libcurl 8）构建，单份二进制直接兼容 **Ubuntu 18.04–26.04** 与 **CentOS 8.2 / AlmaLinux 8+**；
2. `dandelion-linux-aarch64-<version>.zip` — 基于 `manylinux2014_aarch64` 构建，单份二进制直接兼容 ARM64 下的 **Ubuntu 18.04–26.04** 与 **CentOS 8+ / AlmaLinux 8+**；
3. `dandelion-win-x86_64-<version>.zip` — Windows 64 位独立可执行包（内置系统 WinHTTP 传输层，零外部 DLL 依赖）。

### 3.2 使用

解压进入 `dandelion` 目录后，直接运行对应的启动脚本即可：

```sh
# Windows 平台
./dandelion.cmd
# Linux 平台
./dandelion.sh
```

两个脚本分别指向 `bin/` 子目录下的 `dandelion` 可执行文件。

## 四、阅读清单

1. 下载并运行：[快速开始](docs/01_begin.zh.md)
2. [项目架构][02_arch.zh.path]
3. [Dandelion Remote：同步与服务器部署](docs/03_remote.zh.md)

[releases-url]: https://github.com/SwordofMorning/Dandelion/releases
[02_arch.zh.path]: docs/02_architecture.zh.md

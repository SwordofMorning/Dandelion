[![Chinese](https://img.shields.io/badge/中文-README-blue)](../README.md)   [![Run](https://img.shields.io/badge/Download%20and%20Run-8CA1AF?logo=readthedocs&logoColor=fff)](01_begin.en.md)

# Dandelion

A CLI-style Agent tool powered by the standalone `dandelion-core` PyPI package (featuring a native C++17 + `nanobind` multi-protocol LLM engine supporting Messages, Chat Completions, Responses, Generate Content, and Interactions) and packaged as cross-platform standalone binaries with Nuitka. The monorepo layout is organized into three layers:

1. `packages/dandelion-core/` provides the standalone `dandelion-core` package (`dandelion_core.agent`, `dandelion_core.subagent`, `dandelion_core.tool`, `dandelion_core.utils`, `dandelion_core.runtime`, `dandelion_core.host`);
2. `src/cli/` provides the terminal CLI host (`InteractiveCLI(BaseAgentHost)` / `CLIRuntime(BaseRuntime)`), while `src/remote/` provides the Remote daemon host (`RemoteLoop(BaseAgentHost)` / `RemoteRuntime(BaseRuntime)`);
3. `mk/` is the unified local-dev and CI build & script center (`mk/make.py`, `mk/scripts/`, `mk/lib/`).

## 1. Features

- CLI-based interaction with no GUI;
- Standalone `dandelion-core` package with strict local-first monorepo development (`python mk/make.py dev`) plus precompiled binary wheels (`pip install dandelion-core`) for Ubuntu 18.04–26.04, CentOS 8+ / AlmaLinux 8+, and Windows;
- Zero-rebuild tool hot-plugging: drop any `BaseTool` module or package into `src/tool/`, `src/cli/tool/`, `src/remote/tool/`, or `<install>/tool/` next to the standalone binary without editing `dandelion-core` or rebuilding (CLI includes `tools` and `reload` commands);
- Native C++17 LLM provider engine with zero heavy SDK dependencies (`openai`, `anthropic`, `httpx`, `pydantic`, and `python-dotenv` removed);
- Comprehensive logging, Git-style session branches, and unified `Stop` / `Rollback` / `Continue` turn interruption;
- Manual backup of raw session history and metadata to Remote;
- Dandelion Remote reuses the same core for background jobs, Telegram, and persistent reminders; see the [Remote guide](03_remote.en.md).

## 2. Using Source Code

Python 3.11 or higher is recommended.

### 2.1 Clone the Repository

```bash
git clone git@github.com:SwordofMorning/Dandelion.git
# Or
git clone https://github.com/SwordofMorning/Dandelion.git
# Enter the directory
cd Dandelion
```

### 2.2 Install Dependencies and Build

It is recommended to manage your environment with `conda` or `venv`:

```bash
# Option A (Recommended, Local-First): Atomic editable install of local packages/dandelion-core + root [dev]
python mk/make.py dev
# Equivalent pip command:
# pip install -e packages/dandelion-core -e ".[dev]"
# Or with uv (root pyproject.toml pins [tool.uv.sources] to local packages/dandelion-core):
# uv sync --extra dev

# Option B (Fallback without a local C++ compiler): Prebuilt dandelion-core wheel from PyPI + local CLI/Remote
pip install dandelion-core
pip install --no-deps -e .
pip install prompt-toolkit requests

# Windows PowerShell helper for local native builds & dev install (CMake + Ninja + MSVC)
powershell -ExecutionPolicy Bypass -File mk/scripts/build_native.ps1 -Target dev
```

Unified `mk/make.py` targets:

```bash
python mk/make.py dev      # Local-first editable install of packages/dandelion-core + root [dev]
python mk/make.py native   # Build and editable-install packages/dandelion-core only
python mk/make.py wheel    # Build precompiled dandelion-core .whl into dist/
python mk/make.py all      # Verify local dandelion-core provenance and build Nuitka standalone into build/dandelion/
python mk/make.py ascii    # Verify 100% ASCII policy across .py/.cpp/.hpp/.h files
python mk/make.py clean    # Clean caches, build/, dist/, and empty legacy dirs
```

### 2.3 Configure the Environment

For first-time users, configure the following under `.env/`:

1. `cli.cfg` (see `.env/cli.cfg.example`): API keys, LLM models, reasoning effort, and SubAgent configuration;
2. `devices.yaml` (see `.env/devices.yaml.example`): optional device inventory for SSH access.

### 2.4 Run the Program

After completing the configuration above, run the CLI directly with `python main.py`:

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

Where:

1. `MAX_TOKENS` represents the maximum output length of the LLM;
2. `MAX_CONTEXT_TOKENS` represents the total context window length including output tokens;
3. `prompt_toolkit` provides `Tab` completion;
4. `deepseek-v4-flash` is the active Main Agent name;
5. `staged_msg` is the current session branch name.

## 3. Using Releases

### 3.1 Download

On the [Releases][releases-url] page, Dandelion ships a streamlined **3-artifact standalone release** (alongside `dandelion-core` prebuilt wheels published to PyPI):

1. `dandelion-linux-x86_64-<version>.zip` — built inside `manylinux2014` (`glibc 2.17` with statically linked OpenSSL 3 and minimal HTTP/HTTPS libcurl 8), running out of the box across **Ubuntu 18.04–26.04** and **CentOS 8.2 / AlmaLinux 8+**;
2. `dandelion-linux-aarch64-<version>.zip` — built inside `manylinux2014_aarch64`, running out of the box on ARM64 **Ubuntu 18.04–26.04** and **CentOS 8+ / AlmaLinux 8+**;
3. `dandelion-win-x86_64-<version>.zip` — Windows x86_64 standalone build using native WinHTTP with zero external DLL dependencies.

### 3.2 Usage

After extracting the archive and entering the `dandelion` directory, run:

```sh
# Windows Platform
./dandelion.cmd
# Linux Platform
./dandelion.sh
```

Both wrapper scripts invoke the `dandelion` binary inside the `bin/` subdirectory.

## 4. Reading List

1. Download and run: [Quick Start](01_begin.en.md)
2. [Architecture][02_arch.en.path]
3. [Dandelion Remote: sync and server deployment](03_remote.en.md)

[releases-url]: https://github.com/SwordofMorning/Dandelion/releases
[02_arch.en.path]: 02_architecture.en.md

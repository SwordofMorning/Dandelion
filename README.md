[![English](https://img.shields.io/badge/English-README-blue)](docs/README.en.md)   [![Run](https://img.shields.io/badge/下载并运行-8CA1AF?logo=readthedocs&logoColor=fff)](docs/01_begin.zh.md)

# Dandelion

一个CLI式的Agent工具，采用Nuitka构建，可以配合交叉编译工具链实现跨平台。基础结构如下：

1. 在`src/utils/cli/interactive_cli.py`中的“Interactive Loop”相当于Agent Loop；
2. 并将每一次的Agent Iterate (Tool Call)动作放到`src/core/agent.py`中的“step”执行。


## 一、功能特性

- CLI式交互，无GUI；
- Tools均在`src/tool`下实现，可跟随主程序一并构建；
- 完善的日志、会话管理。

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

### 2.2 安装依赖

建议先通过`conda`或者`venv`来管理环境，随后按照如下的方式安装依赖：

```bash
# 适用于只想直接运行 Dandelion 源码的用户
pip install .

# 包含打包工具、测试框架等开发所需依赖
pip install ".[dev]"
# 编辑模式
pip install -e ".[dev]"
```

### 2.3 配置环境

对于初次使用程序的用户来说，需要在`.env/`下配置：

1. `api.cfg`，包含API Key、LLM模型、路由配置等内容；
2. `devices.yaml`，如果需要使用串口或者SSH访问其他设备，需要配置此表。

### 2.4 运行程序

完成上述配置后，可以直接通过`python main.py`来运行程序：

```log
(dandelion) xiaojintao@U26S:~/Workspace/Dandelion$ python main.py
[>] Initializing Dandelion Project (v0.0.0-dev-260807-abc123-ci)...
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

1. `MAX_TOKENS`表示LLM最大输出长度；
2. `MAX_CONTEXT_TOKENS`表示LLM上下文长度，包含前述LLM的输出长度；
3. `prompt_toolkit`用来提供`tab`补全功能；
4. `deepseek-v4-flash`是当前的Main Agent (Router)的名称；
5. `staged_msg`是当前会话(Session Branch)的名称。

## 三、使用发行版

### 3.1 下载

在[Releases][releases-url]页面，Dandelion提供了多个架构、平台的发行版。其中，针对Ubuntu上的glibc版本问题，本项目采用docker直接为不同版本的系统进行构建。因此建议Linux用户可以直接下载对应的glibc版本的产物。

同时，由于长度等原因，可能需要点击`Show all xx assets`才能显示完成的构建产物，这里请注意点击。

### 3.2 使用

这里的使用方式和前述的源码类似，在进入到`Dandelion`文件夹后，通过：

```sh
# Windows 平台
./dandelion.cmd
# Linux 平台
./dandelion.sh
```

来执行程序，两者均指向其子目录下的`dandelion`二进制文件。这里旨在通过以脚本的形式简化目录。

## 四、阅读清单

1. 下载并运行：TODO
2. [项目架构][02_arch.zh.path]

[releases-url]: https://github.com/SwordofMorning/Dandelion/releases
[02_arch.zh.path]: docs/02_arch.zh.md
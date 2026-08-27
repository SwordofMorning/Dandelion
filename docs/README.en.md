[![Chinese](https://img.shields.io/badge/中文-README-blue)](../README.md)   [![Run](https://img.shields.io/badge/Download%20and%20Run-8CA1AF?logo=readthedocs&logoColor=fff)](01_begin.en.md)

# Dandelion

A CLI-style Agent tool built with Nuitka, capable of cross-platform deployment through cross-compilation toolchains. The basic architecture is as follows:

1. The "Interactive Loop" in `src/utils/cli/interactive_cli.py` functions as the Agent Loop;
2. Each Agent Iterate (Tool Call) action is executed in the `step` function within `src/core/agent.py`.

## 1. Features

- CLI-based interaction, no GUI;
- All Tools are implemented under `src/tool` and can be built together with the main program;
- Comprehensive logging and session management.

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

### 2.2 Install Dependencies

It is recommended to manage your environment using `conda` or `venv` first, and then install the dependencies as follows:

```bash
# For users who just want to run the Dandelion source code directly
pip install .

# Includes dependencies required for development, such as packaging tools and testing frameworks
pip install ".[dev]"
# Editable mode
pip install -e ".[dev]"
```

### 2.3 Configure the Environment

For first-time users, the following configurations are required under the `.env/` directory:

1. `api.cfg`: Contains the API Key, LLM model, routing configuration, etc.;
2. `devices.yaml`: Configure this table if you need to access other devices via serial port or SSH.

### 2.4 Run the Program

After completing the configurations above, you can run the program directly with `python main.py`:

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

Where:

1. `MAX_TOKENS` represents the maximum output length of the LLM;
2. `MAX_CONTEXT_TOKENS` represents the maximum context length of the LLM, including the aforementioned output length;
3. `prompt_toolkit` is used to provide `tab` completion functionality;
4. `deepseek-v4-flash` is the name of the current Main Agent (Router);
5. `staged_msg` is the name of the current session (Session Branch).

## 3. Using Releases

### 3.1 Download

On the [Releases][releases-url] page, Dandelion provides distributions for multiple architectures and platforms. Specifically, to address `glibc` version compatibility issues on Ubuntu, this project uses Docker to build directly for different system versions. Therefore, it is highly recommended that Linux users download the artifact corresponding to their `glibc` version.

Additionally, due to layout length limitations, you may need to click `Show all xx assets` to display all complete build artifacts. Please keep this in mind.

### 3.2 Usage

The usage here is similar to the source code method mentioned above. After entering the `Dandelion` folder, execute the program via:

```sh
# Windows Platform
./dandelion.cmd
# Linux Platform
./dandelion.sh
```

Both scripts point to the `dandelion` binary file located in their subdirectories. This approach is designed to simplify directory navigation using wrapping scripts.

## 4. Reading List

1. Download and run TODO.
2. [Architecture][02_arch.en.path]

[releases-url]: https://github.com/SwordofMorning/Dandelion/releases
[02_arch.en.path]: 02_architecture.en.md
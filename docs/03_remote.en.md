# Dandelion Remote

## 一、Architecture Overview

`src/` is organized into three top-level packages:

1. `src/core/`: Frontend-agnostic execution mechanisms and abstract base classes, including `agent/` (`MyAgent`, `BasePromptBuilder`, `MemoryManager`, `SkillManager`, `ContextCompressor`, `ContextPruner`, `run_agent_loop`), `tool/` (built-in tools including `remember`, `forget`, and `prune_context`), `subagent/`, and `utils/`.
2. `src/cli/`: Local terminal frontend reading `cli.cfg` (with automatic fallback to legacy `api.cfg`), featuring workspace sandbox protection (`cli_runtime()`), `CLIPromptBuilder`, and manual session sync client `sync.py`.
3. `src/remote/`: Always-on personal coding and companion assistant for dedicated server deployment (`sandbox=False`), configured via `remote.cfg` and managed via `./remote.sh`.

```text
src/remote/
├── execution/   # Main agent assembly, single blocking interrupt loop, background workers
├── storage/     # SQLite lifecycle store, pure-filesystem syncs (assets/syncs/), and reply persistence
├── channels/    # HTTP API (MD5 ACCESS_KEY verification) and allowlisted Telegram polling/delivery
├── tool/        # Remote tools (agent/task_tool.py, communication/message_tool.py, drive/drive_tool.py)
├── sysprompt.py # RemotePromptBuilder for the 24/7 server assistant
├── service.py   # remote.cfg loader and service lifecycle management
└── __main__.py  # Thin module entrypoint for python -m src.remote
```

---

## 二、Configuration (`cli.cfg` & `remote.cfg`)

### 2.1 Local Client Configuration (`cli.cfg`)

Configure `[Remote]` in `cli.cfg`:

```ini
[Remote]
NAME = origin
URL = https://sub2api.swordofmorning.com/dandelion-sync
ACCESS_KEY = dr-your-shared-secret-key
DEVICE_ID = work-pc
```

1. `ACCESS_KEY` is hashed with MD5 by `SessionSync` before transmission and verified on the server via constant-time MD5 digest comparison.
2. Run `remote -v` to inspect connection status and `sync` / `sync --session <id/name>` to upload `history.log`, `meta.log`, and optional `task_state.json` to `<workspace>/assets/syncs/<session_id>/`.

### 2.2 Remote Server Configuration (`remote.cfg`)

`remote.cfg` contains `[Main]` and model provider sections alongside `[Server]`, `[Telegram]`, and `[Drive]`:

```ini
[Server]
HOST = 0.0.0.0
PORT = 8765
WORKSPACE = ./.data/workspace
DATA_DIR = ./.data/remote
WORKERS = 4
ACCESS_KEY = dr-your-shared-secret-key

[Telegram]
BOT_TOKEN = 123456789:AAHxxxxxxxxxxxxxxxx
ALLOWED_CHATS = 987654321

[Drive]
ACCESS_TOKEN =
CLIENT_ID =
CLIENT_SECRET =
REFRESH_TOKEN =
FOLDER_ID =
```

---

## 三、Google Drive Tool (`google_drive`) & Self-Backup

`GoogleDriveTool` (`src/remote/tool/drive/drive_tool.py`) exposes sandboxed Google Drive operations to the Remote Main Agent:

1. Automatic Root Creation & Confinement: Automatically detects or creates the `Dandelion Remote` root folder (with `reports/`, `memory/`, `data/`, and `files/` subfolders) and confines all relative paths strictly inside `Dandelion Remote/`.
2. Supported Actions:
   - `backup`: Self-backup global memories (`llm/memory/*.md` -> `memory/`), synced session summaries (`assets/syncs/` -> `data/`), and daily Google Docs reports (`reports/Dandelion YYYY-MM-DD`).
   - `restore_memory`: Restore memory files from `Dandelion Remote/memory/` to local `llm/memory/`.
   - `list` / `upload` / `download`: List, upload, or download files within `Dandelion Remote/`.

---

## 四、Server Lifecycle Script (`remote.sh`)

Manage the Dandelion Remote daemon directly from the repository root:

```sh
./remote.sh init      # Seed workspace skills and generate remote.cfg with a random ACCESS_KEY
./remote.sh start     # Start Dandelion Remote in background
./remote.sh status    # Check daemon PID and status
./remote.sh logs      # Tail runtime logs
./remote.sh restart   # Gracefully restart the service
./remote.sh stop      # Gracefully stop the service (SIGTERM)
./remote.sh fg        # Run in foreground
```

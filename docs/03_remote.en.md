# Dandelion Remote

## 一、Architecture Overview

`src/` is organized into three top-level packages:

1. `src/core/`: Frontend-agnostic execution mechanisms and abstract base classes, including `agent/` (`MyAgent`, `BasePromptBuilder`, `MemoryManager`, `SkillManager`, `ContextCompressor`, `ContextPruner`, `run_agent_loop`), `tool/` (built-in tools including `remember`, `forget`, and `prune_context`), `subagent/`, and `utils/`.
2. `src/cli/`: Local terminal frontend with workspace sandbox protection (`cli_runtime()`), `CLIPromptBuilder`, and manual session sync client `sync.py`. Standalone Nuitka CLI builds include `src.cli` and `src.core` only.
3. `src/remote/`: Always-on personal coding and companion assistant for dedicated server deployment (`sandbox=False`), featuring a single blocking interrupt loop and tool-driven external communication.

```text
src/remote/
├── execution/   # Main agent assembly, single blocking interrupt loop, background workers, delegation tools
├── storage/     # SQLite lifecycle store, pure-filesystem syncs (assets/syncs/), and reply persistence
├── channels/    # Authenticated HTTP API and allowlisted Telegram polling/delivery
├── tool/        # Remote-specific tools (tool/communication/message_tool.py)
├── exports/     # Explicit Google Drive daily report export from assets/syncs/
├── sysprompt.py # RemotePromptBuilder for the 24/7 server assistant
├── service.py   # CLI entrypoint and service lifecycle management
└── __main__.py  # Thin module entrypoint for python -m src.remote
```

---

## 二、Manual Session Sync (`sync`)

Configure `[Remote]` explicitly in `.env/api.cfg`:

```ini
[Remote]
NAME=origin
URL=https://dr.example.com
TOKEN=${DANDELION_DR_TOKEN}
DEVICE_ID=work-laptop
```

Available CLI commands:

```text
remote
remote -v
sync
sync --session sess_20261028_091213_736513
sync --session project-notes
```

1. `remote` prints the remote name; `remote -v` displays the URL, Device ID, and Token configuration state without exposing credentials.
2. `sync` uploads `history.log`, `meta.log`, and optional `task_state.json` (excluding `api.log` and `artifacts/`) to `<workspace>/assets/syncs/<session_id>/` via atomic `.tmp` + `os.replace()` writes.
3. When a synced session changes or lacks `summary.md`, `POST /sync` enqueues a `sync_received` interrupt that wakes Dandelion to inspect the directory, write `<workspace>/assets/syncs/<session_id>/summary.md`, and consolidate global memory via `remember` and `forget`.

---

## 三、Server Execution & Communication

### 3.1 Starting the Server

```sh
export DANDELION_DR_TOKEN='<admin-token>'
export DANDELION_DR_DEVICES='{"work-laptop":"<device-token>"}'
python -m src.remote serve \
  --host 127.0.0.1 --port 8765 \
  --data-dir .remote --workspace /srv/dandelion-workspace \
  --config .env/api.cfg --workers 4
```

### 3.2 Interrupt Loop & Proactive Messaging (`send_message`)

1. `RemoteLoop` blocks on a single condition variable woken by four interrupt sources: `message`, `sync_received`, `job_done`, and `analysis`.
2. Outgoing communication is exposed as `send_message` (`SendMessageTool`). On background interrupts (`sync_received`, `analysis`), the agent may work silently or call `send_message` when it decides to notify the user.

### 3.3 Self-Organizing Memory & Context Pruning

1. `remember` and `forget` maintain up to 500 memory index entries per tier.
2. `prune_context` invokes `ContextPruner` (an independent pruning SubAgent) to fold verbose tool outputs or drop obsolete turns after archiving a full pre-prune backup under `archive/` and verifying `tool_use`/`tool_result` protocol invariants.
3. On daily date changes, `RemoteLoop._check_daily_rollover()` prunes yesterday's history before switching to a new daily session branch `dr_<YYYY-MM-DD>`.

#!/usr/bin/env bash
##
 # @file remote.sh
 # @date 2026/10/28
 #
 # @brief One-stop setup and daemon lifecycle manager for Dandelion Remote (DR).
 #
 # @usage
 #   ./remote.sh init      : Initialize workspace, skills, and remote.cfg template
 #   ./remote.sh start     : Start Dandelion Remote in background (daemon mode)
 #   ./remote.sh stop      : Gracefully stop Dandelion Remote (SIGTERM)
 #   ./remote.sh restart   : Restart Dandelion Remote
 #   ./remote.sh status    : Show running status and PID
 #   ./remote.sh logs      : Tail the runtime log file
 #   ./remote.sh fg        : Run Dandelion Remote in foreground
 #

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CFG_FILE="$("$PYTHON_BIN" -c 'import sys; sys.path.insert(0, "."); from mk.lib.paths import remote_config_path; print(remote_config_path())')"
DATA_DIR="${DR_DATA_DIR:-$ROOT_DIR/.remote}"
WORKSPACE_DIR="${DR_WORKSPACE_DIR:-$ROOT_DIR/workspace}"
PID_FILE="$DATA_DIR/remote.pid"
LOG_FILE="$DATA_DIR/remote.log"

_init_env() {
    mkdir -p "$DATA_DIR" "$WORKSPACE_DIR/llm" "$(dirname "$CFG_FILE")"
    if [ -d "$ROOT_DIR/llm/skill" ] && [ ! -d "$WORKSPACE_DIR/llm/skill" ]; then
        cp -r "$ROOT_DIR/llm/skill" "$WORKSPACE_DIR/llm/skill"
        echo "[+] Seeded built-in skills into $WORKSPACE_DIR/llm/skill"
    fi

    if [ ! -f "$CFG_FILE" ]; then
        GEN_KEY="$("$PYTHON_BIN" -c 'import secrets; print("dr-" + secrets.token_hex(16))')"
        cat > "$CFG_FILE" <<EOF
[Main]
MAIN_AGENT = gemini
SUB_LIST = ["gemini"]
SUB_DEPTH = 1
SUB_CONCURRENCY = 4
TAVILY_API_KEY =

[Sub2API_Antigravity]
SDK_TYPE = "Message"
BASE_URL = https://sub2api.swordofmorning.com
API_KEY = sk-replace-with-your-llm-api-key
MODEL_LIST = [
    {
        "provider": {
            "model_id": "gemini-3.8-flash-tiered",
            "compat": "gemini"
        },
        "basic": {
            "name": "gemini",
            "conditions": ["media", "simple"],
            "max_token": 128000,
            "max_context_tokens": 819200
        },
        "reasoning": {
            "thinking": "enabled",
            "effort": "high"
        },
        "media": {
            "file": [".png", ".jpg", ".jpeg", ".webp", ".pdf"],
            "max_image_bytes": "15MB",
            "max_pdf_bytes": "15MB",
            "max_pdf_pages": 100,
            "max_media_per_request": 8
        }
    }
    ]

[Server]
HOST = 0.0.0.0
PORT = 8765
WORKSPACE = ./workspace
DATA_DIR = ./.remote
WORKERS = 4
ACCESS_KEY = ${GEN_KEY}

[Telegram]
BOT_TOKEN =
ALLOWED_CHATS =

[Drive]
ACCESS_TOKEN =
CLIENT_ID =
CLIENT_SECRET =
REFRESH_TOKEN =
FOLDER_ID =
EOF
        echo "[+] Generated initial remote config at: $CFG_FILE"
        echo "[+] Generated ACCESS_KEY: $GEN_KEY"
        echo "    (Copy this ACCESS_KEY into [Remote] ACCESS_KEY of your local cli.cfg)"
    else
        echo "[*] Remote config already exists at: $CFG_FILE"
    fi
}

_is_running() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

cmd_init() {
    echo "[>] Initializing Dandelion Remote environment..."
    _init_env
    echo "[+] Initialization complete. Edit $CFG_FILE then run: ./remote.sh start"
}

cmd_start() {
    _init_env
    if _is_running; then
        echo "[*] Dandelion Remote is already running (PID: $(cat "$PID_FILE"))."
        exit 0
    fi
    echo "[>] Starting Dandelion Remote in background..."
    cd "$ROOT_DIR"
    nohup "$PYTHON_BIN" -m src.remote serve --config "$CFG_FILE" >> "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        echo "[+] Dandelion Remote started (PID: $pid, Log: $LOG_FILE)."
    else
        echo "[-] Dandelion Remote failed to start. Check $LOG_FILE:"
        tail -n 20 "$LOG_FILE" || true
        exit 1
    fi
}

cmd_stop() {
    if ! _is_running; then
        echo "[*] Dandelion Remote is not running."
        rm -f "$PID_FILE"
        return 0
    fi
    local pid
    pid="$(cat "$PID_FILE")"
    echo "[>] Stopping Dandelion Remote (PID: $pid)..."
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
        sleep 1
    done
    rm -f "$PID_FILE"
    echo "[+] Dandelion Remote stopped."
}

cmd_status() {
    if _is_running; then
        echo "[+] Dandelion Remote is RUNNING (PID: $(cat "$PID_FILE"))."
    else
        echo "[-] Dandelion Remote is STOPPED."
    fi
}

cmd_logs() {
    mkdir -p "$DATA_DIR"
    touch "$LOG_FILE"
    tail -n 50 -f "$LOG_FILE"
}

cmd_fg() {
    _init_env
    cd "$ROOT_DIR"
    exec "$PYTHON_BIN" -m src.remote serve --config "$CFG_FILE"
}

ACTION="${1:-help}"
case "$ACTION" in
    init)
        cmd_init
        ;;
    start)
        cmd_start
        ;;
    stop)
        cmd_stop
        ;;
    restart)
        cmd_stop
        cmd_start
        ;;
    status)
        cmd_status
        ;;
    logs)
        cmd_logs
        ;;
    fg)
        cmd_fg
        ;;
    *)
        echo "Usage: ./remote.sh {init|start|stop|restart|status|logs|fg}"
        exit 1
        ;;
esac
# End-main

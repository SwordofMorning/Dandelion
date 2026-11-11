##
 # @file src/remote/service.py
 # @date 2026/10/28
 #
 # @brief Remote service assembly and daemon lifecycle management.
 # Absorbs daemon lifecycle commands (init, start, stop, restart, status, logs, fg)
 # alongside foreground service execution (serve) for `python main.py remote ...`
 # and `python -m src.remote ...`.
 #

import argparse
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time

from mk.lib.paths import base_dir, remote_config_path
from src.core.utils.config import load_remote_config
from .channels.telegram import Telegram
from .storage.replies import ChannelHub
from .storage.backups import BackupStore
from .channels.http import make_server
from .storage.store import Store

STARTUP_WAIT_SEC = 1.0
STOP_TIMEOUT_SEC = 30.0
STOP_POLL_SEC = 0.5

_FALLBACK_CFG_TEMPLATE = """[Main]
MAIN_AGENT = gemini
SUB_LIST = ["gemini"]
SUB_DEPTH = 2
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
            "description": "Fast multimodal model supporting image/PDF inspection, file operations, and general background tasks.",
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
WORKSPACE = ./.data/workspace
DATA_DIR = ./.data/remote
WORKERS = 4
ACCESS_KEY = dr-replace-with-your-shared-secret-key

[Telegram]
BOT_TOKEN =
ALLOWED_CHATS =

[Drive]
ACCESS_TOKEN =
CLIENT_ID =
CLIENT_SECRET =
REFRESH_TOKEN =
FOLDER_ID =
"""


##
 # @brief Parse `serve` arguments and run the Dandelion Remote server.
 #
 # @param argv Command arguments beginning with `"serve"`.
 #
 # @return Process exit code (0).
 #
def _run_serve_command(argv=None):
    default_cfg = remote_config_path()
    parser = argparse.ArgumentParser(description="Run the Dandelion Remote service.")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--workspace", default=None)
    serve.add_argument("--workers", type=int, default=None)
    serve.add_argument("--config", default=default_cfg)
    serve.add_argument("--data-dir", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_remote_config(args.config)
    if not config:
        parser.error("Cannot load remote config: " + str(args.config))
    # End-if
    srv_cfg = config.get("SERVER") or {}

    raw_data_dir = args.data_dir or srv_cfg.get("data_dir") or ".data/remote"
    raw_workspace = args.workspace or srv_cfg.get("workspace") or "./.data/workspace"
    data_dir = Path(raw_data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    workspace = str(Path(raw_workspace).resolve())
    Path(workspace).mkdir(parents=True, exist_ok=True)

    store = Store(str(data_dir / "remote.sqlite3"))
    try:
        _serve(args, parser, config, store, data_dir, workspace)
    finally:
        store.close()
    # End-try
    return 0
# End-def


##
 # @brief Run the authenticated remote service and optional Telegram transport.
 #
def _serve(args, parser, config, store, data_dir, workspace):
    from .execution.jobs import JobExecutor, JobManager
    from .execution.loop import RemoteLoop
    from .execution.agent import make_agent_factory

    srv_cfg = config.get("SERVER") or {}
    tg_cfg = config.get("TELEGRAM") or {}

    host = args.host or srv_cfg.get("host") or "0.0.0.0"
    port = args.port if args.port is not None else int(srv_cfg.get("port", 8765))
    workers = args.workers if args.workers is not None else int(srv_cfg.get("workers", 4))

    access_key = (
        srv_cfg.get("access_key")
        or os.environ.get("DANDELION_DR_TOKEN", "")
    ).strip()
    if not access_key:
        parser.error("ACCESS_KEY is required in [Server] section of " + str(args.config))
    # End-if

    telegram_token = (
        tg_cfg.get("bot_token")
        or os.environ.get("DANDELION_TELEGRAM_TOKEN", "")
    ).strip()
    chats = list(tg_cfg.get("allowed_chats") or [])
    if not chats and os.environ.get("DANDELION_TELEGRAM_CHATS"):
        chats = [c.strip() for c in os.environ.get("DANDELION_TELEGRAM_CHATS", "").split(",") if c.strip()]
    # End-if
    if telegram_token and not chats:
        parser.error("Set ALLOWED_CHATS in [Telegram] section of " + str(args.config))
    # End-if

    backups = BackupStore(str(data_dir), workspace_dir=workspace)
    jobs = JobManager(store, JobExecutor(config, str(data_dir)), max_workers=workers)
    hub = ChannelHub(store)
    factory = make_agent_factory(config, str(data_dir), workspace, jobs, hub=hub, backups=backups)
    loop = RemoteLoop(store, jobs, factory, hub.notify, backups=backups)
    server = make_server(host, port, store, jobs, loop, hub, access_key, backups=backups)
    telegram = (
        Telegram(telegram_token, chats, loop, hub, workspace_dir=workspace)
        if telegram_token else None
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)

    def stop(*_):
        loop.stop()
    # End-def

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        jobs.start(notify=hub.notify)
        server_thread.start()
        if telegram:
            telegram.start()
        # End-if
        logging.info("Dandelion Remote listening on %s:%s", host, server.server_address[1])
        loop.run()
    finally:
        server.shutdown()
        server.server_close()
        if telegram:
            telegram.close()
        # End-if
        jobs.close()
    # End-try
# End-def


##
 # @brief Resolve runtime paths for the Remote daemon lifecycle manager.
 #
 # @return Tuple of (root_dir, cfg_file, data_dir, workspace_dir, pid_file, log_file).
 #
def _resolve_paths():
    root_override = os.environ.get("DR_ROOT_DIR")
    root_dir = Path(root_override if root_override else base_dir()).resolve()

    cfg_override = os.environ.get("DR_CONFIG_FILE")
    if cfg_override:
        cfg_file = Path(cfg_override).resolve()
    elif root_override:
        cfg_file = (root_dir / ".env" / "remote.cfg").resolve()
    else:
        cfg_file = Path(remote_config_path()).resolve()
    # End-if

    data_override = os.environ.get("DR_DATA_DIR")
    data_dir = Path(data_override).resolve() if data_override else (root_dir / ".data" / "remote").resolve()

    ws_override = os.environ.get("DR_WORKSPACE_DIR")
    workspace_dir = Path(ws_override).resolve() if ws_override else (root_dir / ".data" / "workspace").resolve()

    pid_file = data_dir / "remote.pid"
    log_file = data_dir / "remote.log"
    return root_dir, cfg_file, data_dir, workspace_dir, pid_file, log_file
# End-def


##
 # @brief Initialize directories, seed workspace skills, and create remote.cfg if missing.
 #
 # @return Resolved configuration file Path.
 #
def _init_env():
    root_dir, cfg_file, data_dir, workspace_dir, _, _ = _resolve_paths()
    data_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "llm").mkdir(parents=True, exist_ok=True)
    cfg_file.parent.mkdir(parents=True, exist_ok=True)

    src_skill = root_dir / "llm" / "skill"
    dst_skill = workspace_dir / "llm" / "skill"
    if src_skill.is_dir() and not dst_skill.exists():
        shutil.copytree(src_skill, dst_skill)
        print(f"[+] Seeded built-in skills into {dst_skill}")
    # End-if

    if not cfg_file.is_file():
        gen_key = "dr-" + secrets.token_hex(16)
        example_path = root_dir / ".env" / "remote.cfg.example"
        template = example_path.read_text(encoding="utf-8") if example_path.is_file() else _FALLBACK_CFG_TEMPLATE
        rendered, count = re.subn(
            r"(?m)^(\s*ACCESS_KEY\s*=\s*).*$",
            rf"\g<1>{gen_key}",
            template,
        )
        if count == 0:
            rendered = rendered.rstrip() + f"\nACCESS_KEY = {gen_key}\n"
        # End-if
        cfg_file.write_text(rendered, encoding="utf-8")
        print(f"[+] Generated initial remote config at: {cfg_file}")
        print(f"[+] Generated ACCESS_KEY: {gen_key}")
        print("    (Copy this ACCESS_KEY into [Remote] ACCESS_KEY of your local cli.cfg)")
    else:
        print(f"[*] Remote config already exists at: {cfg_file}")
    # End-if
    return cfg_file
# End-def


##
 # @brief Check whether a given process ID is currently alive.
 #
def _is_pid_alive(pid):
    if pid <= 0:
        return False
    # End-if
    try:
        waited_pid, _ = os.waitpid(pid, os.WNOHANG)
        if waited_pid == pid:
            return False
        # End-if
    except (ChildProcessError, OSError):
        pass
    # End-try

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # End-try
# End-def


##
 # @brief Read PID from pid_file and return it if the process is alive.
 #
def _get_running_pid(pid_file):
    if not pid_file.is_file():
        return None
    # End-if
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    # End-try
    return pid if _is_pid_alive(pid) else None
# End-def


##
 # @brief Print the last N lines of a log file if present.
 #
def _print_log_tail(log_file, count=20):
    if not log_file.is_file():
        return
    # End-if
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines(True)
    except OSError:
        return
    # End-try
    tail = "".join(lines[-count:])
    if tail:
        sys.stdout.write(tail if tail.endswith("\n") else tail + "\n")
        sys.stdout.flush()
    # End-if
# End-def


##
 # @brief Handle `remote init`.
 #
def cmd_init():
    print("[>] Initializing Dandelion Remote environment...")
    cfg_file = _init_env()
    print(f"[+] Initialization complete. Edit {cfg_file} then run: python main.py remote start")
    return 0
# End-def


##
 # @brief Handle `remote start` by daemonizing the service in background.
 #
def cmd_start():
    root_dir, cfg_file, _, _, pid_file, log_file = _resolve_paths()
    _init_env()

    running_pid = _get_running_pid(pid_file)
    if running_pid is not None:
        print(f"[*] Dandelion Remote is already running (PID: {running_pid}).")
        return 0
    # End-if

    print("[>] Starting Dandelion Remote in background...")
    with open(log_file, "a", encoding="utf-8") as log_f:
        proc = subprocess.Popen(
            [sys.executable, "-m", "src.remote", "serve", "--config", str(cfg_file)],
            cwd=str(root_dir),
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    # End-with

    pid = proc.pid
    pid_file.write_text(f"{pid}\n", encoding="utf-8")
    time.sleep(STARTUP_WAIT_SEC)

    if proc.poll() is None and _is_pid_alive(pid):
        proc.returncode = 0
        print(f"[+] Dandelion Remote started (PID: {pid}, Log: {log_file}).")
        return 0
    # End-if

    pid_file.unlink(missing_ok=True)
    print(f"[-] Dandelion Remote failed to start. Check {log_file}:")
    _print_log_tail(log_file, count=20)
    return 1
# End-def


##
 # @brief Handle `remote stop` by sending SIGTERM and polling for exit.
 #
def cmd_stop():
    _, _, _, _, pid_file, _ = _resolve_paths()
    pid = _get_running_pid(pid_file)
    if pid is None:
        print("[*] Dandelion Remote is not running.")
        pid_file.unlink(missing_ok=True)
        return 0
    # End-if

    print(f"[>] Stopping Dandelion Remote (PID: {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    # End-try

    deadline = time.monotonic() + STOP_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if not _is_pid_alive(pid):
            break
        # End-if
        time.sleep(STOP_POLL_SEC)
    # End-while

    if _is_pid_alive(pid):
        print(
            f"[-] Dandelion Remote (PID: {pid}) did not exit within "
            f"{int(STOP_TIMEOUT_SEC)}s; keeping {pid_file}."
        )
        return 1
    # End-if

    pid_file.unlink(missing_ok=True)
    print("[+] Dandelion Remote stopped.")
    return 0
# End-def


##
 # @brief Handle `remote restart`.
 #
def cmd_restart():
    rc = cmd_stop()
    return rc if rc != 0 else cmd_start()
# End-def


##
 # @brief Handle `remote status`.
 #
def cmd_status():
    _, _, _, _, pid_file, _ = _resolve_paths()
    pid = _get_running_pid(pid_file)
    if pid is not None:
        print(f"[+] Dandelion Remote is RUNNING (PID: {pid}).")
    else:
        print("[-] Dandelion Remote is STOPPED.")
    # End-if
    return 0
# End-def


##
 # @brief Handle `remote logs` by printing the last 50 lines and following output.
 #
def cmd_logs(follow=True):
    _, _, data_dir, _, _, log_file = _resolve_paths()
    data_dir.mkdir(parents=True, exist_ok=True)
    log_file.touch(exist_ok=True)

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            tail = "".join(f.readlines()[-50:])
            if tail:
                sys.stdout.write(tail)
                sys.stdout.flush()
            # End-if
            while follow:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.2)
                # End-if
            # End-while
        # End-with
    except KeyboardInterrupt:
        print("")
    # End-try
    return 0
# End-def


##
 # @brief Handle `remote fg` by initializing environment and running in foreground.
 #
def cmd_fg(extra_args=None):
    root_dir, cfg_file, _, _, _, _ = _resolve_paths()
    _init_env()
    os.chdir(str(root_dir))
    serve_argv = ["serve", "--config", str(cfg_file)]
    if extra_args:
        serve_argv.extend(extra_args)
    # End-if
    return _run_serve_command(serve_argv)
# End-def


##
 # @brief Dispatch Dandelion Remote lifecycle and service commands.
 #
 # @param argv Optional command arguments; None reads sys.argv[1:].
 #
 # @return Integer process exit code.
 #
def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "help"
    rest = args[1:]

    if action == "init":
        return cmd_init()
    elif action == "start":
        return cmd_start()
    elif action == "stop":
        return cmd_stop()
    elif action == "restart":
        return cmd_restart()
    elif action == "status":
        return cmd_status()
    elif action == "logs":
        return cmd_logs(follow=True)
    elif action == "fg":
        return cmd_fg(rest)
    elif action == "serve":
        return _run_serve_command(["serve"] + rest)
    else:
        print("Usage: python main.py remote {init|start|stop|restart|status|logs|fg}")
        return 1
    # End-if
# End-def

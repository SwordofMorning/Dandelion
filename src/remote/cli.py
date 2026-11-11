##
 # @file src/remote/cli.py
 # @date 2026/11/03
 #
 # @brief Daemon lifecycle manager and CLI dispatcher for Dandelion Remote.
 # Absorbs the legacy remote.sh workflow into `python main.py remote ...` and
 # `python -m src.remote ...` with native background daemonization.
 #

import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time

from mk.lib.paths import base_dir, remote_config_path

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
 # @brief Resolve runtime paths for the Remote lifecycle manager.
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
        if example_path.is_file():
            template = example_path.read_text(encoding="utf-8")
        else:
            template = _FALLBACK_CFG_TEMPLATE
        # End-if
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
 # @param pid Integer process ID.
 #
 # @return True if the process exists and is not an exited child zombie.
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
 # @param pid_file Path to remote.pid.
 #
 # @return Running integer PID or None.
 #
def _get_running_pid(pid_file):
    if not pid_file.is_file():
        return None
    # End-if
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except (OSError, ValueError):
        return None
    # End-try
    if _is_pid_alive(pid):
        return pid
    # End-if
    return None
# End-def


##
 # @brief Print the last N lines of a log file if present.
 #
 # @param log_file Path to remote.log.
 # @param count Maximum number of trailing lines to print.
 #
 # @return None.
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
        sys.stdout.write(tail)
        if not tail.endswith("\n"):
            sys.stdout.write("\n")
        # End-if
        sys.stdout.flush()
    # End-if
# End-def


##
 # @brief Handle `remote init`.
 #
 # @return Process exit code (0).
 #
def cmd_init():
    print("[>] Initializing Dandelion Remote environment...")
    cfg_file = _init_env()
    print(f"[+] Initialization complete. Edit {cfg_file} then run: python main.py remote start")
    return 0
# End-def


##
 # @brief Handle `remote start` by auto-daemonizing the server in background.
 #
 # @return Process exit code (0 on success or already running, 1 on startup failure).
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
 # @return Process exit code (0 when stopped, 1 on timeout).
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
 # @return Process exit code.
 #
def cmd_restart():
    rc = cmd_stop()
    if rc != 0:
        return rc
    # End-if
    return cmd_start()
# End-def


##
 # @brief Handle `remote status`.
 #
 # @return Process exit code (0).
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
 # @brief Handle `remote logs` by printing the last 50 lines and following new output.
 #
 # @param follow Whether to keep following the file until KeyboardInterrupt.
 #
 # @return Process exit code (0).
 #
def cmd_logs(follow=True):
    _, _, data_dir, _, _, log_file = _resolve_paths()
    data_dir.mkdir(parents=True, exist_ok=True)
    log_file.touch(exist_ok=True)

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            tail = "".join(lines[-50:])
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
 # @param extra_args Optional extra CLI flags forwarded to `serve`.
 #
 # @return Process exit code (0).
 #
def cmd_fg(extra_args=None):
    from .service import main as service_main

    root_dir, cfg_file, _, _, _, _ = _resolve_paths()
    _init_env()
    os.chdir(str(root_dir))
    serve_argv = ["serve", "--config", str(cfg_file)]
    if extra_args:
        serve_argv.extend(extra_args)
    # End-if
    service_main(serve_argv)
    return 0
# End-def


##
 # @brief Dispatch Dandelion Remote lifecycle and service commands.
 #
 # @param argv Optional argument list; defaults to sys.argv[1:].
 #
 # @return Integer exit code.
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
        from .service import main as service_main

        service_main(["serve"] + rest)
        return 0
    else:
        print("Usage: python main.py remote {init|start|stop|restart|status|logs|fg}")
        return 1
    # End-if
# End-def

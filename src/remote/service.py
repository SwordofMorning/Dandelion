##
 # @file src/remote/service.py
 # @date 2026/10/28
 #
 # @brief Remote service assembly and command dispatch.
 # Loads remote.cfg (containing LLM providers plus [Server], [Telegram], and
 # [Drive] sections), initializes the unrestricted Remote runtime and pure-FS
 # SyncStore, and coordinates HTTP, workers and Telegram.
 #

import argparse
import logging
import os
from pathlib import Path
import signal
import threading

from mk.lib.paths import remote_config_path
from src.core.utils.config import load_remote_config
from .channels.telegram import Telegram
from .storage.replies import ChannelHub
from .storage.backups import BackupStore
from .channels.http import make_server
from .storage.store import Store


##
 # @brief Dispatch Dandelion Remote service and Drive export commands.
 #
 # @param argv Optional command arguments; None reads the process arguments.
 #
 # @return None.
 #
def main(argv=None):
    default_cfg = remote_config_path()
    parser = argparse.ArgumentParser(
        description="Run the Dandelion Remote service or export daily synced session reports to Google Drive."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--workspace", default=None)
    serve.add_argument("--workers", type=int, default=None)
    drive = commands.add_parser("drive-export")
    drive.add_argument("--workspace", default=None)
    for command in (serve, drive):
        command.add_argument("--config", default=default_cfg)
        command.add_argument("--data-dir", default=None)
    # End-for
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_remote_config(args.config)
    if not config:
        parser.error("Cannot load remote config: " + str(args.config))
    # End-if
    srv_cfg = config.get("SERVER") or {}

    raw_data_dir = args.data_dir or srv_cfg.get("data_dir") or ".remote"
    raw_workspace = args.workspace or srv_cfg.get("workspace") or "./workspace"
    data_dir = Path(raw_data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    workspace = str(Path(raw_workspace).resolve())
    Path(workspace).mkdir(parents=True, exist_ok=True)

    store = Store(str(data_dir / "remote.sqlite3"))
    try:
        if args.command == "drive-export":
            from .exports.drive import export_drive
            result = export_drive(store, workspace_dir=workspace, drive_cfg=config.get("DRIVE"))
            print(f"Exported {result['exported']} daily documents to Google Drive.")
        else:
            _serve(args, parser, config, store, data_dir, workspace)
        # End-if
    finally:
        store.close()
    # End-try
# End-def


##
 # @brief Run the authenticated remote service and optional Telegram transport.
 #
 # @param args Parsed remote-service command arguments.
 # @param parser Argument parser used to report invalid service configuration.
 # @param config Loaded remote.cfg dictionary.
 # @param store Shared durable store with its connection lock.
 # @param data_dir Private remote-service data directory.
 # @param workspace Resolved server workspace directory.
 #
 # @return None.
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
    loop = RemoteLoop(store, jobs, factory, hub.notify)
    server = make_server(
        host, port, store, jobs, loop, hub, access_key, backups=backups,
    )
    telegram = Telegram(telegram_token, chats, loop, hub) if telegram_token else None
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)

    ##
     # @brief Request remote-loop shutdown from a process signal.
     #
     # @param _ Unused signal callback arguments.
     #
     # @return None.
     #
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

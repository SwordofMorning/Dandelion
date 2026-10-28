##
 # @file src/remote/service.py
 # @date 2026/10/28
 #
 # @brief Remote service assembly and command dispatch.
 # Parses serve, activity import/export and Drive export commands, loads the
 # configured model runtime, and coordinates HTTP, workers and optional Telegram.
 #
 # @note The existing data directory and command names are independent of this layout.
 #

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import threading

from .channels.telegram import Telegram
from .storage.replies import ChannelHub
from .channels.http import make_server, validate_activity
from .storage.store import Store


##
 # @brief Dispatch service and activity transfer commands.
 #
 # @param argv Optional command arguments; None reads the process arguments.
 #
 # @return None.
 #
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the optional Dandelion Remote service or transfer activity JSONL.")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--workspace", default=".")
    serve.add_argument("--config", default=".env/api.cfg")
    serve.add_argument("--workers", type=int, default=4)
    drive = commands.add_parser("drive-export")
    for command in (serve, commands.add_parser("import"), commands.add_parser("export"), drive):
        command.add_argument("--data-dir", default=".remote")
        if command not in (serve, drive):
            command.add_argument("path")
        # End-if
    # End-for
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(str(data_dir / "remote.sqlite3"))
    try:
        if args.command == "import":
            # Validate the entire file before accepting records.
            with open(args.path, encoding="utf-8") as source:
                records = [validate_activity(json.loads(line)) for line in source if line.strip()]
            # End-with
            count = sum(bool(store.ingest_activity(record)) for record in records)
            print(f"Imported {count} new activities ({len(records)} records checked).")
        elif args.command == "export":
            records = store.list_activities(limit=None)
            with open(args.path, "w", encoding="utf-8") as target:
                for record in reversed(records):
                    target.write(json.dumps(record, ensure_ascii=False) + "\n")
                # End-for
            # End-with
            print(f"Exported {len(records)} activities.")
        elif args.command == "drive-export":
            from .exports.drive import export_drive
            result = export_drive(store)
            print(f"Exported {result['exported']} daily documents to Google Drive.")
        else:
            _serve(args, parser, store, data_dir)
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
 # @param store Shared durable store with its connection lock.
 # @param data_dir Private remote-service data directory.
 #
 # @return None.
 #
def _serve(args, parser, store, data_dir):
    # Import the Agent only when serving; transfer commands need no LLM setup.
    from src.core.utils.config import load_api_config
    from .execution.jobs import JobManager
    from .execution.loop import RemoteLoop
    from .execution.agent import JobExecutor, make_agent_factory

    token = os.environ.get("DANDELION_DR_TOKEN", "")
    if not token:
        parser.error("DANDELION_DR_TOKEN is required")
    # End-if
    device_tokens = json.loads(os.environ.get("DANDELION_DR_DEVICES") or "{}")
    if not isinstance(device_tokens, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not v
            for k, v in device_tokens.items()):
        parser.error("DANDELION_DR_DEVICES must map device names to nonempty tokens")
    # End-if
    telegram_token = os.environ.get("DANDELION_TELEGRAM_TOKEN", "")
    chats = [chat.strip() for chat in os.environ.get("DANDELION_TELEGRAM_CHATS", "").split(",") if chat.strip()]
    if telegram_token and not chats:
        parser.error("Set DANDELION_TELEGRAM_CHATS to authorized private chat IDs")
    # End-if
    config = load_api_config(args.config)
    if not config:
        parser.error("Cannot load LLM config: " + args.config)
    # End-if
    workspace = str(Path(args.workspace).resolve())
    Path(workspace).mkdir(parents=True, exist_ok=True)
    jobs = JobManager(store, JobExecutor(config, str(data_dir)), max_workers=args.workers)
    hub = ChannelHub(store)
    factory = make_agent_factory(config, str(data_dir), workspace, jobs)
    loop = RemoteLoop(store, jobs, factory, hub.notify)
    server = make_server(args.host, args.port, store, jobs, loop, hub, token, device_tokens)
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
        jobs.start()
        server_thread.start()
        if telegram:
            telegram.start()
        # End-if
        logging.info("Dandelion Remote listening on %s:%s", args.host, server.server_address[1])
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

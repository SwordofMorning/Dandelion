"""Run the optional Dandelion Remote service or transfer activity JSONL."""

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import threading

from .channels import ChannelHub, Telegram
from .server import make_server, validate_activity
from .store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
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
            count = sum(bool(store.ingest_activity(record)) for record in records)
            print(f"Imported {count} new activities ({len(records)} records checked).")
        elif args.command == "export":
            records = store.list_activities(limit=None)
            with open(args.path, "w", encoding="utf-8") as target:
                for record in reversed(records):
                    target.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"Exported {len(records)} activities.")
        elif args.command == "drive-export":
            from .drive import export_drive
            result = export_drive(store)
            print(f"Exported {result['exported']} daily documents to Google Drive.")
        else:
            _serve(args, parser, store, data_dir)
    finally:
        store.close()


def _serve(args, parser, store, data_dir):
    # Import the Agent only when serving; transfer commands need no LLM setup.
    from src.utils.config import load_api_config
    from .jobs import JobManager
    from .loop import RemoteLoop
    from .runtime import JobExecutor, make_agent_factory

    token = os.environ.get("DANDELION_DR_TOKEN", "")
    if not token:
        parser.error("DANDELION_DR_TOKEN is required")
    device_tokens = json.loads(os.environ.get("DANDELION_DR_DEVICES") or "{}")
    if not isinstance(device_tokens, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not v
            for k, v in device_tokens.items()):
        parser.error("DANDELION_DR_DEVICES must map device names to nonempty tokens")
    telegram_token = os.environ.get("DANDELION_TELEGRAM_TOKEN", "")
    chats = [chat.strip() for chat in os.environ.get("DANDELION_TELEGRAM_CHATS", "").split(",") if chat.strip()]
    if telegram_token and not chats:
        parser.error("Set DANDELION_TELEGRAM_CHATS to authorized private chat IDs")
    config = load_api_config(args.config)
    if not config:
        parser.error("Cannot load LLM config: " + args.config)
    workspace = str(Path(args.workspace).resolve())
    Path(workspace).mkdir(parents=True, exist_ok=True)
    jobs = JobManager(store, JobExecutor(config, str(data_dir)), max_workers=args.workers)
    hub = ChannelHub(store)
    factory = make_agent_factory(config, str(data_dir), workspace, jobs)
    loop = RemoteLoop(store, jobs, factory, hub.notify)
    server = make_server(args.host, args.port, store, jobs, loop, hub, token, device_tokens)
    telegram = Telegram(telegram_token, chats, loop, hub) if telegram_token else None
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)

    def stop(*_):
        loop.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        jobs.start()
        server_thread.start()
        if telegram:
            telegram.start()
        logging.info("Dandelion Remote listening on %s:%s", args.host, server.server_address[1])
        loop.run()
    finally:
        server.shutdown()
        server.server_close()
        if telegram:
            telegram.close()
        jobs.close()


if __name__ == "__main__":
    main()

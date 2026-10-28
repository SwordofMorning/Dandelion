# Remote deployment

Prepare `.env/api.cfg` in the repository root with the server's model settings.
Set `DANDELION_DR_TOKEN` in the shell or a private Compose environment file;
optionally set `DANDELION_DR_DEVICES`, `DANDELION_TELEGRAM_TOKEN` and
`DANDELION_TELEGRAM_CHATS` as described in the [Remote guide](../docs/03_remote.en.md).

From this directory:

```sh
docker compose up -d --build
docker compose logs -f remote
```

Only the API configuration file is mounted from the host; it is never copied
into the image. Named volumes preserve Remote state and the Worker workspace.
Add an HTTPS reverse proxy to the loopback port before connecting other devices.
The service runs as UID 1000; any replacement bind mounts must allow that UID
to write. Mount additional skills under `/workspace/llm/skill` if needed.
CLI devices configure their destination explicitly in the `[Remote]` section of
their local `api.cfg`; manual `sync` uploads raw session history and metadata.
It does not run on startup or generate activity summaries.

For backups, stop the service and back up both named volumes before restarting.
Keep the same data volume when moving from the earlier package layout; the
`/data/remote.sqlite3` path and existing task tables remain compatible.
The supplied configuration is a deployment example; no server is provisioned
or published by installing the Python package.

Optional Drive credentials are passed through the Compose environment. To
export existing activity reports explicitly:

```sh
docker compose exec remote python -m src.remote drive-export --data-dir /data
```

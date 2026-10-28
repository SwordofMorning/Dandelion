# Dandelion Remote

`src/` has three main packages: `src/core/` is the shared foundation, containing the `tool/`, `subagent/` and `utils/` subpackages; `src/cli/` provides terminal interaction, and `src/remote/` provides the server event loop. The CLI keeps synchronous `spawn_subagent`, Ctrl+C rollback and its existing dependencies. Remote is excluded from CLI Nuitka releases.

## Manually back up sessions

Explicitly configure one Remote in the local `.env/api.cfg`:

```ini
[Remote]
NAME=origin
URL=https://dr.example.com
TOKEN=${DANDELION_DR_TOKEN}
DEVICE_ID=work-laptop
```

`TOKEN` accepts a credential or a complete `$VAR` / `${VAR}` environment placeholder. The URL and device ID come from this configuration section; environment variables are not implicit destination fallbacks. When using a device credential, `DEVICE_ID` must match the server's device mapping.

Inside the CLI:

```text
remote
remote -v
sync
sync --session sess_20261027_123456_123456
sync --session project-notes
```

`remote` shows the configured name; `remote -v` also shows its URL. Neither exposes TOKEN. `sync` manually backs up every valid session. `sync --session <ID/name>` selects one session without switching the current session or modifying local files. Each backup transfers the original `history.log` and `meta.log` bytes. It generates no summary and makes no model call; API logs, artifacts, memories and drafts are outside this backup.

Fixed snapshots are uploaded in chunks and checked against file sizes and SHA-256 hashes. A snapshot is published atomically only when both files are complete. Local files stay unchanged on failure; run `sync` again manually. CLI startup does not initiate synchronization, and no automatic retry queue is used.

`GET /sessions` lists the latest published backup metadata, optionally filtered by `device` and `project`. Query with an admin credential:

```sh
curl 'https://dr.example.com/sessions' \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN"
```

Use a backup ID returned by the list to page through messages or download the original files:

```text
GET /sessions/<id>?offset=0&limit=100
GET /sessions/<id>/history.log
GET /sessions/<id>/meta.log
```

These read endpoints require an admin credential. Session history contains raw conversation data; protect server storage accordingly.

## Run the server

Use Python 3.11 or newer, the [existing installation steps](README.en.md#22-install-dependencies), and a server-specific `api.cfg`. From the repository root:

```sh
export DANDELION_DR_TOKEN='<admin credential>'
export DANDELION_DR_DEVICES='{"work-laptop":"<device credential>"}'
python -m src.remote serve \
  --host 127.0.0.1 --port 8765 \
  --data-dir .remote --workspace /srv/dandelion-workspace \
  --config .env/api.cfg --workers 4
```

The admin credential controls messages, jobs and backup queries. Optional `DANDELION_DR_DEVICES` maps device IDs to credentials that can only upload session backups for their own device. Do not distribute the admin credential to every computer. `/health` is a liveness endpoint; other HTTP endpoints require `Authorization: Bearer <token>`. `POST /sync` accepts file chunks and a final commit request; a snapshot becomes visible to queries only after commit acknowledgement.

Other endpoints are `/activities`, `/messages`, `/replies`, `/jobs` and `/schedules`. Network handlers only enqueue events; the Agent thread owns main-agent history and processes conversations serially. The server binds to loopback by default. Expose it through an HTTPS reverse proxy with authentication retained.

### HTTP examples

Submit a message with the admin credential, then read replies using an ID cursor:

```sh
curl "https://dr.example.com/messages" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN" \
  --json '{"session_id":"web:me","text":"Summarize my project progress."}'
curl "https://dr.example.com/replies?session_id=web%3Ame&after=0" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN"
```

Set `after` to the largest reply `id` already received. This creates a plain reminder recurring every 86400 seconds from the specified date; replace the date as needed. `due_at` requires an explicit timezone. Omit `interval` for a one-shot schedule, or use `kind: "analysis"` to ask the main agent to process `text`:

```sh
curl "https://dr.example.com/schedules" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN" \
  --json '{"session_id":"web:me","due_at":"2030-01-01T20:00:00+08:00","text":"Review project notes.","kind":"reminder","interval":86400}'
```

Cancel a background task using its actual `job_id`; use `/cancel` for the active main-agent turn:

```sh
curl -X DELETE "https://dr.example.com/jobs/<job_id>?session_id=web%3Ame" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN"
curl "https://dr.example.com/cancel" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN" \
  --json '{"session_id":"web:me"}'
```

### Telegram

Optionally set these variables before starting:

```sh
export DANDELION_TELEGRAM_TOKEN='<Bot token>'
export DANDELION_TELEGRAM_CHATS='123456789'
```

Separate multiple allowed chat IDs with commas. Only allowlisted private chats are accepted. `/cancel` requests cancellation at the next safe boundary. Keep the Bot token out of the repository. Long polling requires no public Telegram webhook. Computers connect only to DR; the server connects to Telegram and model providers. An existing US VPS can be used first, with a move to Hong Kong based on actual connection quality.

### Independent activity data and optional Google Drive reports

Existing activity JSONL `import` / `export` and Drive reports remain available for independent activity data. CLI `sync` only backs up sessions; it does not create activity records or daily reports:

```sh
python -m src.remote import activity.jsonl --data-dir .remote
python -m src.remote export activities.jsonl --data-dir .remote
```

Enable the Drive API in your Google Cloud project and complete OAuth consent yourself. Prefer the `drive.file` scope for documents created by this application; an existing Gmail grant does not grant Drive access. The command does not launch an interactive login. With a valid access token:

```sh
export DANDELION_DRIVE_ACCESS_TOKEN='<OAuth access token>'
# Optional: place new documents in an already authorized folder
export DANDELION_DRIVE_FOLDER_ID='<folder ID>'
python -m src.remote drive-export --data-dir .remote
```

Alternatively supply `DANDELION_DRIVE_CLIENT_ID`, `DANDELION_DRIVE_CLIENT_SECRET` and `DANDELION_DRIVE_REFRESH_TOKEN` to refresh access tokens. Keep credentials in the server environment. Imported independent activities are grouped into UTC daily reports. SQLite retains each date's document ID; changed reports update the same Google Doc, and unchanged reports are skipped. Updates replace the full document body, so keep source information in DR rather than relying on manual edits to exported documents.

Export only runs when explicitly requested and has no automatic link to session backups. Gemini can read the documents through its configured Drive connection; uploading does not make Gemini automatically remember every report in every conversation. The implementation uses Google's [Drive upload and conversion API](https://developers.google.com/workspace/drive/api/guides/manage-uploads#import_to_google_docs_types) and [OAuth refresh endpoint](https://developers.google.com/identity/protocols/oauth2/web-server#offline).

## Execution and recovery

The CLI and Remote share `run_agent_loop()`, the tool factory and `Runtime` output, approval and cancellation callbacks. Main and Worker tools are selected independently, with prompts matching the registered tools. Remote never reads terminal approval and denies out-of-workspace access.

`submit_task` returns an accepted job ID immediately, letting the main agent continue chatting. Each background Worker calls the existing `SubAgentPool.create_and_run()` with separate session, logs and cancellation state. Background jobs for the same workspace execute serially. Nested Worker delegation stays synchronous. Completion is persisted and delivered as a later event to the owning conversation, without adding a second `tool_result` for the original tool call.

SQLite stores jobs, activities, schedules and pending events. Idle agents block without model polling. A separate scheduler sends ordinary reminders directly; only schedules requiring analysis invoke the agent, so reminders do not wait for a long main-agent turn.

- Cancellation is cooperative between complete steps. It does not kill an in-flight request or tool, or roll back workspace files. Late results from cancelled jobs do not resume normal conversation processing.
- Restart recovers queued jobs and retains unprocessed events. Main-agent turns and background jobs that were processing during a crash become `interrupted` and generate a notification. They are not rerun automatically; inspect task status and resubmit explicitly to avoid repeating side effects. SubAgent execution checkpoints are not restored.
- Notifications and Telegram outbound messages use at-least-once delivery. A crash after sending but before recording acknowledgement can produce duplicate notifications.
- Stop the service before backing up the entire `--data-dir`, including SQLite, session snapshots under `backups/`, runtime session logs and artifacts. Back up workspace files separately. Do not copy only a live SQLite file while it is being written.

Export server activities for inspection or migration:

```sh
python -m src.remote export activities.jsonl --data-dir .remote
```

The [deployment directory](../deploy/) contains a container example. Moving source packages does not change `--data-dir` or the existing SQLite job tables; continue using the same data directory. A web interface can be added later. Google Docs are rebuildable copies of independent activity reports.

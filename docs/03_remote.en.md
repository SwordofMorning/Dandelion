# Dandelion Remote

DR is a persistent server assistant built on Dandelion's synchronous Core. The CLI keeps its terminal interaction, synchronous `spawn_subagent` and Ctrl+C rollback. Remote storage, networking and scheduling live in the separate `dandelion_remote/` package, excluded from CLI Nuitka releases. No third-party dependencies were added.

## Sync activity summaries

Configure the CLI environment:

```sh
export DANDELION_DR_URL=https://dr.example.com
export DANDELION_DR_TOKEN='<device sync credential>'
export DANDELION_DEVICE_ID=work-laptop
python main.py
```

Inside the CLI:

```text
sync
sync Completed the login endpoint; deployment notes remain.
sync --retry
sync --export activity.jsonl
```

`sync` produces a bounded summary from recent final replies and task state, or accepts explicit summary text. It does not upload raw API logs, images or large tool outputs. Records contain a stable `event_id`, device, project, session, timestamp, summary and source references. IDs do not depend on history indexes, so context compaction does not invalidate them.

The `.log/sync/` outbox retains records until the server acknowledges their IDs. Failed uploads are retried with `sync --retry` or on the next launch. The server deduplicates IDs. Exporting keeps the pending records; copy the JSONL file to the server and import it:

```sh
python -m dandelion_remote import activity.jsonl --data-dir .remote
```

`DR_URL` and `DR_TOKEN` remain supported aliases. Keep each device ID stable; when using a device credential, its ID must match the server configuration.

## Run the server

Use Python 3.11 or newer, the [existing installation steps](README.en.md#22-install-dependencies), and a server-specific `api.cfg`. From the repository root:

```sh
export DANDELION_DR_TOKEN='<admin credential>'
export DANDELION_DR_DEVICES='{"work-laptop":"<device credential>"}'
python -m dandelion_remote serve \
  --host 127.0.0.1 --port 8765 \
  --data-dir .remote --workspace /srv/dandelion-workspace \
  --config .env/api.cfg --workers 4
```

The admin credential controls messages, jobs and queries. Optional `DANDELION_DR_DEVICES` maps device IDs to credentials that can only upload summaries for their own device. Do not distribute the admin credential to every computer. `/health` is a liveness endpoint; other HTTP endpoints require `Authorization: Bearer <token>`. `POST /sync` accepts `{"records":[...]}` and returns persisted record IDs in `accepted`.

Other endpoints are `/activities`, `/messages`, `/replies`, `/jobs` and `/schedules`. Network handlers only enqueue events; the Agent thread owns main-agent history and processes conversations serially. The server binds to loopback by default. Expose it through an HTTPS reverse proxy with authentication retained.

### HTTP examples

Submit a message with the admin credential, then read replies using an ID cursor:

```sh
curl "$DANDELION_DR_URL/messages" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN" \
  --json '{"session_id":"web:me","text":"Summarize my project progress."}'
curl "$DANDELION_DR_URL/replies?session_id=web%3Ame&after=0" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN"
```

Set `after` to the largest reply `id` already received. This creates a plain reminder recurring every 86400 seconds from the specified date; replace the date as needed. `due_at` requires an explicit timezone. Omit `interval` for a one-shot schedule, or use `kind: "analysis"` to ask the main agent to process `text`:

```sh
curl "$DANDELION_DR_URL/schedules" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN" \
  --json '{"session_id":"web:me","due_at":"2030-01-01T20:00:00+08:00","text":"Review project notes.","kind":"reminder","interval":86400}'
```

Cancel a background task using its actual `job_id`; use `/cancel` for the active main-agent turn:

```sh
curl -X DELETE "$DANDELION_DR_URL/jobs/<job_id>?session_id=web%3Ame" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN"
curl "$DANDELION_DR_URL/cancel" \
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

### Optional Google Drive daily reports

Enable the Drive API in your Google Cloud project and complete OAuth consent yourself. Prefer the `drive.file` scope for documents created by this application; an existing Gmail grant does not grant Drive access. The command does not launch an interactive login. With a valid access token:

```sh
export DANDELION_DRIVE_ACCESS_TOKEN='<OAuth access token>'
# Optional: place new documents in an already authorized folder
export DANDELION_DRIVE_FOLDER_ID='<folder ID>'
python -m dandelion_remote drive-export --data-dir .remote
```

Alternatively supply `DANDELION_DRIVE_CLIENT_ID`, `DANDELION_DRIVE_CLIENT_SECRET` and `DANDELION_DRIVE_REFRESH_TOKEN` to refresh access tokens. Keep credentials in the server environment. Activities are grouped into UTC daily reports. SQLite retains each date's document ID; changed reports update the same Google Doc, and unchanged reports are skipped. Updates replace the full document body, so keep source information in DR rather than relying on manual edits to exported documents.

Export only runs when explicitly requested. Gemini can read the documents through its configured Drive connection; uploading does not make Gemini automatically remember every report in every conversation. The implementation uses Google's [Drive upload and conversion API](https://developers.google.com/workspace/drive/api/guides/manage-uploads#import_to_google_docs_types) and [OAuth refresh endpoint](https://developers.google.com/identity/protocols/oauth2/web-server#offline).

## Execution and recovery

The CLI and Remote share `run_agent_loop()`, the tool factory and `Runtime` output, approval and cancellation callbacks. Main and Worker tools are selected independently, with prompts matching the registered tools. Remote never reads terminal approval and denies out-of-workspace access.

`submit_task` returns an accepted job ID immediately, letting the main agent continue chatting. Each background Worker calls the existing `SubAgentPool.create_and_run()` with separate session, logs and cancellation state. Background jobs for the same workspace execute serially. Nested Worker delegation stays synchronous. Completion is persisted and delivered as a later event to the owning conversation, without adding a second `tool_result` for the original tool call.

SQLite stores jobs, activities, schedules and pending events. Idle agents block without model polling. A separate scheduler sends ordinary reminders directly; only schedules requiring analysis invoke the agent, so reminders do not wait for a long main-agent turn.

- Cancellation is cooperative between complete steps. It does not kill an in-flight request or tool, or roll back workspace files. Late results from cancelled jobs do not resume normal conversation processing.
- Restart recovers queued jobs and retains unprocessed events. Main-agent turns and background jobs that were processing during a crash become `interrupted` and generate a notification. They are not rerun automatically; inspect task status and resubmit explicitly to avoid repeating side effects. SubAgent execution checkpoints are not restored.
- Notifications and Telegram outbound messages use at-least-once delivery. A crash after sending but before recording acknowledgement can produce duplicate notifications.
- Stop the service before backing up the entire `--data-dir`, including SQLite, session logs and artifacts. Back up workspace files separately. Do not copy only a live SQLite file while it is being written.

Export server activities for inspection or migration:

```sh
python -m dandelion_remote export activities.jsonl --data-dir .remote
```

The [deployment directory](../deploy/) contains a container example. A web interface can be added later. DR's local records remain authoritative; Google Docs are rebuildable daily-report copies.

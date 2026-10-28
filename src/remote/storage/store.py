##
 # @file src/remote/storage/store.py
 # @date 2026/10/27
 #
 # @brief SQLite remote lifecycle persistence.
 # Stores background jobs, interrupt events and schedules, coordinates
 # single-loop condition wakeups, and recovers interrupted work on restart.
 #

import datetime as dt
import json
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path


##
 # @brief Parse finite Unix seconds or an ISO timestamp with an explicit timezone.
 #
 # @param value Numeric Unix timestamp or timezone-aware ISO8601 string.
 #
 # @return Parsed Unix timestamp in seconds.
 #
def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Timestamp must be finite")
        # End-if
        return result
    # End-if
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("due_at must include a timezone")
    # End-if
    return parsed.timestamp()
# End-def


##
 # @brief Persist remote jobs, interrupt events and schedules in SQLite.
 #
class Store:
    ##
     # @brief Open the lifecycle database and initialize thread-safe storage.
     #
     # @param db_path Filesystem path to the SQLite database file.
     #
     # @return None.
     #
    def __init__(self, db_path):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir = path.resolve().parent
        self.backup_lock = threading.RLock()
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock, self.conn:
            self.conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    workspace TEXT NOT NULL, task_description TEXT NOT NULL,
                    role_prompt TEXT NOT NULL, toolset TEXT NOT NULL,
                    context TEXT NOT NULL, status TEXT NOT NULL,
                    result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    session_id TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_pending ON events(status,created_at);
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, due_at REAL NOT NULL,
                    payload TEXT NOT NULL, interval REAL, kind TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
            """)
        # End-with
    # End-def

    ##
     # @brief Decode an optional job row and its structured result.
     #
     # @param row SQLite job row or None.
     #
     # @return Decoded job dictionary, or None.
     #
    @staticmethod
    def _job(row):
        if row is None:
            return None
        # End-if
        record = dict(row)
        record["result"] = json.loads(record["result"]) if record["result"] else None
        return record
    # End-def

    ##
     # @brief Insert one durable event inside the caller-owned transaction.
     #
     # @param kind Event category string.
     # @param session_id Conversation identifier used to scope persisted records.
     # @param payload JSON-serializable event payload.
     # @param event_id Optional deterministic event identifier.
     #
     # @return Persisted event identifier.
     #
    def _event(self, kind, session_id, payload, event_id=None):
        event_id = event_id or "evt_" + uuid.uuid4().hex
        self.conn.execute(
            "INSERT OR IGNORE INTO events(id,kind,session_id,payload,created_at) VALUES(?,?,?,?,?)",
            (event_id, kind, str(session_id), json.dumps(payload, ensure_ascii=False), time.time()),
        )
        return event_id
    # End-def

    ##
     # @brief Persist an interrupt event and notify the blocked event loop.
     #
     # @param kind Event category string.
     # @param session_id Conversation identifier used to scope persisted records.
     # @param payload JSON-serializable event payload.
     # @param event_id Optional deterministic event identifier.
     #
     # @return Persisted event identifier.
     #
    def add_event(self, kind, session_id, payload, event_id=None):
        with self.changed, self.conn:
            result = self._event(kind, session_id, payload, event_id)
            self.changed.notify_all()
            return result
        # End-with
    # End-def

    ##
     # @brief Claim the next pending interrupt event atomically.
     #
     # @return Claimed event dictionary, or None when none are pending.
     #
    def claim_event(self):
        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM events WHERE status='pending' ORDER BY created_at,rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            # End-if
            self.conn.execute("UPDATE events SET status='processing' WHERE id=?", (row["id"],))
            return dict(row, payload=json.loads(row["payload"]))
        # End-with
    # End-def

    ##
     # @brief Mark a processed event complete.
     #
     # @param event_id Completed event identifier.
     #
     # @return None.
     #
    def complete_event(self, event_id):
        with self.lock, self.conn:
            self.conn.execute("UPDATE events SET status='done' WHERE id=?", (event_id,))
        # End-with
    # End-def

    ##
     # @brief Cancel all unclaimed pending events for a conversation session.
     #
     # @param session_id Conversation identifier whose queued events should be dropped.
     #
     # @return Number of pending events cancelled.
     #
    def cancel_pending_events(self, session_id):
        with self.changed, self.conn:
            cur = self.conn.execute(
                "UPDATE events SET status='cancelled' WHERE session_id=? AND status='pending'",
                (str(session_id),),
            )
            self.changed.notify_all()
            return int(cur.rowcount)
        # End-with
    # End-def

    ##
     # @brief Recover interrupted events and jobs after a server restart.
     #
     # @param notify Optional callback(session_id, text) for interrupted turn notices.
     #
     # @return None.
     #
    def recover(self, notify=None):
        interrupted_notices = []
        with self.changed, self.conn:
            events = self.conn.execute("SELECT * FROM events WHERE status='processing'").fetchall()
            for event in events:
                self.conn.execute("UPDATE events SET status='interrupted' WHERE id=?", (event["id"],))
                interrupted_notices.append((
                    event["session_id"],
                    "Server restarted during an active turn. The interrupted turn was not replayed.",
                ))
            # End-for
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE status IN ('running','cancel_requested')"
            ).fetchall()
            for row in rows:
                self._finish(row, "interrupted", {
                    "status": "interrupted",
                    "error_message": "Server stopped during execution; resubmit explicitly.",
                })
            # End-for
            self.changed.notify_all()
        # End-with
        if notify is not None:
            for session_id, text in interrupted_notices:
                try:
                    notify(session_id, text)
                except Exception:
                    pass
                # End-try
            # End-for
        # End-if
    # End-def

    ##
     # @brief Save a queued job with its fixed submission context.
     #
     # @param session_id Conversation identifier used to scope persisted records.
     # @param workspace Resolved workspace directory assigned to the job.
     # @param task_description Nonempty natural-language task description.
     # @param role_prompt System role for the delegated worker.
     # @param toolset Registered worker toolset name.
     # @param context Immutable submission context snapshot.
     #
     # @return Persisted job dictionary.
     #
    def create_job(self, session_id, workspace, task_description, role_prompt, toolset, context):
        job_id = "job_" + uuid.uuid4().hex
        now = time.time()
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,'queued',NULL,?,?)",
                (job_id, str(session_id), workspace, task_description, role_prompt, toolset, context, now, now),
            )
        # End-with
        return self.get_job(job_id)
    # End-def

    ##
     # @brief Read one persisted job by its stable identifier.
     #
     # @param job_id Persistent job identifier.
     #
     # @return Decoded job dictionary, or None when absent.
     #
    def get_job(self, job_id):
        with self.lock:
            return self._job(self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        # End-with
    # End-def

    ##
     # @brief List jobs, optionally restricted to one conversation.
     #
     # @param session_id Optional conversation identifier filter.
     #
     # @return Ordered list of job dictionaries, newest first.
     #
    def list_jobs(self, session_id=None):
        with self.lock:
            query, args = "SELECT * FROM jobs", ()
            if session_id is not None:
                query, args = query + " WHERE session_id=?", (str(session_id),)
            # End-if
            return [self._job(row) for row in self.conn.execute(query + " ORDER BY created_at DESC", args)]
        # End-with
    # End-def

    ##
     # @brief Transition a queued job to running exactly once.
     #
     # @param job_id Persistent job identifier.
     #
     # @return True when the job transitioned from queued to running.
     #
    def start_job(self, job_id):
        with self.lock, self.conn:
            return bool(self.conn.execute(
                "UPDATE jobs SET status='running',updated_at=? WHERE id=? AND status='queued'",
                (time.time(), job_id),
            ).rowcount)
        # End-with
    # End-def

    ##
     # @brief Save a terminal job result and completion interrupt in one transaction.
     #
     # @param row Persisted job row.
     # @param status Terminal job status string.
     # @param result Structured worker result dictionary.
     #
     # @return None.
     #
    def _finish(self, row, status, result):
        self.conn.execute(
            "UPDATE jobs SET status=?,result=?,updated_at=? WHERE id=?",
            (status, json.dumps(result, ensure_ascii=False), time.time(), row["id"]),
        )
        self._event("job_done", row["session_id"], {
            "job_id": row["id"],
            "status": status,
            "task_description": row["task_description"],
            "result": result,
        }, "done_" + row["id"])
    # End-def

    ##
     # @brief Complete an active job while preserving cancellation semantics.
     #
     # @param job_id Persistent job identifier.
     # @param result Structured worker result dictionary.
     #
     # @return None.
     #
    def finish_job(self, job_id, result):
        with self.changed, self.conn:
            row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] not in ("running", "cancel_requested"):
                return
            # End-if
            status = result.get("status", "failed")
            if row["status"] == "cancel_requested":
                status = "cancelled"
                result = dict(result, status=status)
            # End-if
            self._finish(row, status, result)
            self.changed.notify_all()
        # End-with
    # End-def

    ##
     # @brief Cancel queued work or request cooperative cancellation of running work.
     #
     # @param job_id Persistent job identifier.
     #
     # @return True when the job was cancelled or marked cancel_requested.
     #
    def cancel_job(self, job_id):
        with self.changed, self.conn:
            row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return False
            # End-if
            if row["status"] == "queued":
                self._finish(row, "cancelled", {"status": "cancelled", "summary": "Cancelled before execution."})
            elif row["status"] == "running":
                self.conn.execute(
                    "UPDATE jobs SET status='cancel_requested',updated_at=? WHERE id=?",
                    (time.time(), job_id),
                )
            else:
                return row["status"] == "cancel_requested"
            # End-if
            self.changed.notify_all()
            return True
        # End-with
    # End-def

    ##
     # @brief Persist a reminder or analysis schedule and wake the event loop.
     #
     # @param session_id Conversation identifier used to scope persisted records.
     # @param due_at Timezone-aware ISO8601 timestamp or Unix timestamp.
     # @param payload Schedule text or dictionary payload.
     # @param interval Optional repeat interval in seconds.
     # @param kind Delivery kind ("reminder" or "analysis").
     #
     # @return Persisted schedule dictionary.
     #
    def create_schedule(self, session_id, due_at, payload, interval=None, kind="reminder"):
        due = timestamp(due_at)
        interval = None if interval is None else float(interval)
        if kind not in ("reminder", "analysis") or (interval is not None and (not math.isfinite(interval) or interval < 1)):
            raise ValueError("kind must be reminder/analysis; interval must be at least one second")
        # End-if
        if not isinstance(payload, (dict, str)):
            raise ValueError("Schedule payload must be text or an object")
        # End-if
        record = dict(
            id="schedule_" + uuid.uuid4().hex,
            session_id=str(session_id),
            due_at=due,
            payload=payload,
            interval=interval,
            kind=kind,
            active=True,
        )
        with self.changed, self.conn:
            self.conn.execute("INSERT INTO schedules VALUES(?,?,?,?,?,?,1)", (
                record["id"], record["session_id"], due,
                json.dumps(payload, ensure_ascii=False), interval, kind,
            ))
            self.changed.notify_all()
        # End-with
        return record
    # End-def

    ##
     # @brief List schedules by conversation and active state.
     #
     # @param session_id Optional conversation identifier filter.
     # @param active_only Whether to restrict results to active schedules.
     #
     # @return Ordered list of schedule dictionaries sorted by due_at.
     #
    def list_schedules(self, session_id=None, active_only=True):
        query, args = "SELECT * FROM schedules WHERE 1=1", []
        if session_id is not None:
            query += " AND session_id=?"
            args.append(str(session_id))
        # End-if
        if active_only:
            query += " AND active=1"
        # End-if
        with self.lock:
            return [
                dict(row, payload=json.loads(row["payload"]))
                for row in self.conn.execute(query + " ORDER BY due_at", args)
            ]
        # End-with
    # End-def

    ##
     # @brief Deactivate a matching schedule without deleting its record.
     #
     # @param schedule_id Persistent schedule identifier.
     # @param session_id Optional conversation identifier scope check.
     #
     # @return True when an active schedule was deactivated.
     #
    def cancel_schedule(self, schedule_id, session_id=None):
        query, args = "UPDATE schedules SET active=0 WHERE id=? AND active=1", [schedule_id]
        if session_id is not None:
            query += " AND session_id=?"
            args.append(str(session_id))
        # End-if
        with self.changed, self.conn:
            result = bool(self.conn.execute(query, args).rowcount)
            self.changed.notify_all()
            return result
        # End-with
    # End-def

    ##
     # @brief Compute the seconds until the next active schedule is due.
     #
     # @param max_wait Upper bound on wait timeout in seconds.
     #
     # @return Timeout in seconds (0.0 .. max_wait) for Condition.wait().
     #
    def next_schedule_delay(self, max_wait=1.0):
        with self.lock:
            row = self.conn.execute(
                "SELECT MIN(due_at) AS next_due FROM schedules WHERE active=1"
            ).fetchone()
            if row is None or row["next_due"] is None:
                return max_wait
            # End-if
            delta = float(row["next_due"]) - time.time()
            return max(0.0, min(delta, max_wait))
        # End-with
    # End-def

    ##
     # @brief Fire due schedules: deliver reminders directly or enqueue analysis interrupts.
     #
     # @param now Optional evaluation timestamp in Unix seconds.
     # @param notify Optional callback(session_id, text) for direct reminders.
     #
     # @return None.
     #
    def fire_due_schedules(self, now=None, notify=None):
        now = time.time() if now is None else now
        with self.changed:
            rows = self.conn.execute("SELECT * FROM schedules WHERE active=1 AND due_at<=?", (now,)).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                if isinstance(payload, str):
                    payload = {"text": payload}
                # End-if
                payload = dict(payload, schedule_id=row["id"], due_at=row["due_at"])
                if row["kind"] == "reminder" and notify is not None:
                    # Persist the outgoing reply record before advancing the schedule.
                    notify(row["session_id"], str(payload.get("text", "")))
                else:
                    with self.conn:
                        self._event("analysis", row["session_id"], payload)
                    # End-with
                # End-if
                with self.conn:
                    if row["interval"]:
                        next_due = row["due_at"] + (int((now - row["due_at"]) // row["interval"]) + 1) * row["interval"]
                        self.conn.execute("UPDATE schedules SET due_at=? WHERE id=?", (next_due, row["id"]))
                    else:
                        self.conn.execute("UPDATE schedules SET active=0 WHERE id=?", (row["id"],))
                    # End-if
                # End-with
            # End-for
            if rows:
                self.changed.notify_all()
            # End-if
        # End-with
    # End-def

    ##
     # @brief Close the SQLite connection after runtime workers stop.
     #
     # @return None.
     #
    def close(self):
        with self.lock:
            self.conn.close()
        # End-with
    # End-def
# End-class

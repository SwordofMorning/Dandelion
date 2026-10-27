"""SQLite lifecycle records. Model execution state stays in Core."""

import datetime as dt
import json
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path


def timestamp(value):
    """Accept Unix seconds or an ISO timestamp with an explicit timezone."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Timestamp must be finite")
        return result
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("due_at must include a timezone")
    return parsed.timestamp()


class Store:
    def __init__(self, db_path):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS activities (
                    id TEXT PRIMARY KEY, record TEXT NOT NULL,
                    occurred_at TEXT NOT NULL, received_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, due_at REAL NOT NULL,
                    payload TEXT NOT NULL, interval REAL, kind TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
            """)

    @staticmethod
    def _job(row):
        if row is None:
            return None
        record = dict(row)
        record["result"] = json.loads(record["result"]) if record["result"] else None
        return record

    def _event(self, kind, session_id, payload, event_id=None):
        event_id = event_id or "evt_" + uuid.uuid4().hex
        self.conn.execute(
            "INSERT OR IGNORE INTO events(id,kind,session_id,payload,created_at) VALUES(?,?,?,?,?)",
            (event_id, kind, str(session_id), json.dumps(payload, ensure_ascii=False), time.time()),
        )
        return event_id

    def add_event(self, kind, session_id, payload, event_id=None):
        with self.changed, self.conn:
            result = self._event(kind, session_id, payload, event_id)
            self.changed.notify_all()
            return result

    def pending_events(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE status='pending' ORDER BY created_at,rowid"
            ).fetchall()
            return [dict(row, payload=json.loads(row["payload"])) for row in rows]

    def claim_event(self, notifications=False):
        with self.lock, self.conn:
            clause = "kind='notification'" if notifications else "kind!='notification'"
            row = self.conn.execute(
                "SELECT * FROM events WHERE status='pending' AND " + clause +
                " ORDER BY created_at,rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self.conn.execute("UPDATE events SET status='processing' WHERE id=?", (row["id"],))
            return dict(row, payload=json.loads(row["payload"]))

    def ack_event(self, event_id):
        with self.lock, self.conn:
            self.conn.execute("UPDATE events SET status='done' WHERE id=?", (event_id,))

    def complete_event(self, event_id, session_id, reply):
        """Save delivery before acknowledging a turn; never rerun model side effects."""
        with self.changed, self.conn:
            self.conn.execute("UPDATE events SET status='done' WHERE id=?", (event_id,))
            self._event("notification", session_id, {"text": reply}, "reply_" + event_id)
            self.changed.notify_all()

    def retry_event(self, event_id):
        with self.changed, self.conn:
            self.conn.execute("UPDATE events SET status='pending' WHERE id=?", (event_id,))
            self.changed.notify_all()

    def recover(self):
        """Replay notifications; report interrupted turns without repeating tool work."""
        with self.changed, self.conn:
            self.conn.execute("UPDATE events SET status='pending' WHERE status='processing' AND kind='notification'")
            events = self.conn.execute("SELECT * FROM events WHERE status='processing'").fetchall()
            for event in events:
                self.conn.execute("UPDATE events SET status='interrupted' WHERE id=?", (event["id"],))
                self._event("notification", event["session_id"], {
                    "text": "Server stopped during a conversation turn. The turn was not replayed; "
                            "inspect task status and resubmit explicitly if needed.",
                    "interrupted_event_id": event["id"],
                }, "interrupted_" + event["id"])
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE status IN ('running','cancel_requested')"
            ).fetchall()
            for row in rows:
                self._finish(row, "interrupted", {
                    "status": "interrupted", "error_message": "Server stopped during execution; resubmit explicitly."
                })
            self.changed.notify_all()

    def create_job(self, session_id, workspace, task_description, role_prompt, toolset, context):
        job_id = "job_" + uuid.uuid4().hex
        now = time.time()
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,'queued',NULL,?,?)",
                (job_id, str(session_id), workspace, task_description, role_prompt, toolset, context, now, now),
            )
        return self.get_job(job_id)

    def get_job(self, job_id):
        with self.lock:
            return self._job(self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list_jobs(self, session_id=None):
        with self.lock:
            query, args = "SELECT * FROM jobs", ()
            if session_id is not None:
                query, args = query + " WHERE session_id=?", (str(session_id),)
            return [self._job(row) for row in self.conn.execute(query + " ORDER BY created_at DESC", args)]

    def start_job(self, job_id):
        with self.lock, self.conn:
            return bool(self.conn.execute(
                "UPDATE jobs SET status='running',updated_at=? WHERE id=? AND status='queued'",
                (time.time(), job_id),
            ).rowcount)

    def _finish(self, row, status, result):
        self.conn.execute(
            "UPDATE jobs SET status=?,result=?,updated_at=? WHERE id=?",
            (status, json.dumps(result, ensure_ascii=False), time.time(), row["id"]),
        )
        self._event("job_done", row["session_id"], {
            "job_id": row["id"], "status": status,
            "task_description": row["task_description"], "result": result,
        }, "done_" + row["id"])

    def finish_job(self, job_id, result):
        with self.changed, self.conn:
            row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] not in ("running", "cancel_requested"):
                return
            status = result.get("status", "failed")
            if row["status"] == "cancel_requested":
                status = "cancelled"
                result = dict(result, status=status)
            self._finish(row, status, result)
            self.changed.notify_all()

    def cancel_job(self, job_id):
        with self.changed, self.conn:
            row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return False
            if row["status"] == "queued":
                self._finish(row, "cancelled", {"status": "cancelled", "summary": "Cancelled before execution."})
            elif row["status"] == "running":
                self.conn.execute("UPDATE jobs SET status='cancel_requested',updated_at=? WHERE id=?", (time.time(), job_id))
            else:
                return row["status"] == "cancel_requested"
            self.changed.notify_all()
            return True

    def ingest_activity(self, record):
        required = ("event_id", "device", "project", "session_id", "occurred_at", "summary", "sources")
        if not isinstance(record, dict) or any(key not in record for key in required):
            raise ValueError("Activity requires: " + ", ".join(required))
        if not all(isinstance(record[key], str) and record[key].strip() for key in required[:-1]):
            raise ValueError("Activity identity, time and summary must be non-empty strings")
        if not isinstance(record["sources"], list):
            raise ValueError("Activity sources must be a list")
        timestamp(record["occurred_at"])
        encoded = json.dumps(record, ensure_ascii=False)
        with self.lock, self.conn:
            existing = self.conn.execute("SELECT record FROM activities WHERE id=?", (record["event_id"],)).fetchone()
            if existing is not None:
                if json.loads(existing["record"]) != record:
                    raise ValueError("event_id already exists with different content")
                return False
            self.conn.execute("INSERT INTO activities VALUES(?,?,?,?)", (
                record["event_id"], encoded, record["occurred_at"], time.time(),
            ))
            return True

    def list_activities(self, limit=100):
        with self.lock:
            query = "SELECT record FROM activities ORDER BY received_at DESC"
            args = ()
            if limit is not None:
                query += " LIMIT ?"
                args = (max(0, int(limit)),)
            return [json.loads(row["record"]) for row in self.conn.execute(query, args)]

    def create_schedule(self, session_id, due_at, payload, interval=None, kind="reminder"):
        due = timestamp(due_at)
        interval = None if interval is None else float(interval)
        if kind not in ("reminder", "analysis") or (interval is not None and (not math.isfinite(interval) or interval < 1)):
            raise ValueError("kind must be reminder/analysis; interval must be at least one second")
        if not isinstance(payload, (dict, str)):
            raise ValueError("Schedule payload must be text or an object")
        record = dict(id="schedule_" + uuid.uuid4().hex, session_id=str(session_id),
                      due_at=due, payload=payload, interval=interval, kind=kind, active=True)
        with self.changed, self.conn:
            self.conn.execute("INSERT INTO schedules VALUES(?,?,?,?,?,?,1)", (
                record["id"], record["session_id"], due, json.dumps(payload, ensure_ascii=False), interval, kind,
            ))
            self.changed.notify_all()
        return record

    def list_schedules(self, session_id=None, active_only=True):
        query, args = "SELECT * FROM schedules WHERE 1=1", []
        if session_id is not None:
            query += " AND session_id=?"
            args.append(str(session_id))
        if active_only:
            query += " AND active=1"
        with self.lock:
            return [dict(row, payload=json.loads(row["payload"])) for row in self.conn.execute(query + " ORDER BY due_at", args)]

    def cancel_schedule(self, schedule_id, session_id=None):
        query, args = "UPDATE schedules SET active=0 WHERE id=? AND active=1", [schedule_id]
        if session_id is not None:
            query += " AND session_id=?"
            args.append(str(session_id))
        with self.changed, self.conn:
            result = bool(self.conn.execute(query, args).rowcount)
            self.changed.notify_all()
            return result

    def fire_due_schedules(self, now=None):
        now = time.time() if now is None else now
        with self.changed, self.conn:
            rows = self.conn.execute("SELECT * FROM schedules WHERE active=1 AND due_at<=?", (now,)).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                if isinstance(payload, str):
                    payload = {"text": payload}
                payload = dict(payload, schedule_id=row["id"], due_at=row["due_at"])
                kind = "notification" if row["kind"] == "reminder" else "analysis"
                self._event(kind, row["session_id"], payload)
                if row["interval"]:
                    # Missed repeats coalesce into one delivery after downtime.
                    next_due = row["due_at"] + (int((now - row["due_at"]) // row["interval"]) + 1) * row["interval"]
                    self.conn.execute("UPDATE schedules SET due_at=? WHERE id=?", (next_due, row["id"]))
                else:
                    self.conn.execute("UPDATE schedules SET active=0 WHERE id=?", (row["id"],))
            if rows:
                self.changed.notify_all()

    def close(self):
        with self.lock:
            self.conn.close()
